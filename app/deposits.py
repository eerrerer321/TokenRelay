"""儲值（方案 B）：使用者建立訂單 → 系統給一個帶尾數的金額 → 鏈上看到完全相同的金額就入帳。"""

import secrets
from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.accounts import current_user, iso_utc
from app.billing import MICRO, change_balance, format_usd
from app.config import Settings, TokenConfig
from app.db import utcnow
from app.models import ChainCursor, ChainTransfer, DepositOrder, User

router = APIRouter(prefix="/api", tags=["儲值"])

TAIL_STEP_MICRO = 100  # 尾數最小單位 0.0001
TAIL_SLOTS = 999  # 尾數範圍 0.0001 ~ 0.0999
# 同一個付款金額 7 天內不重複發給別張訂單。重發之後，舊單的付款人如果才匯款，錢會被新單認領；
# 拉長間隔讓這件事幾乎不會發生（每個整數金額每 7 天有 999 個尾數可用）
AMOUNT_REUSE_WINDOW = timedelta(days=7)


class DepositIn(BaseModel):
    token: str = Field(max_length=16, description="USDT 或 USDC")
    amount_usd: int = Field(description="要儲值的整數美元")


def pay_amount_str(pay_amount_micro: int) -> str:
    return f"{Decimal(pay_amount_micro) / MICRO:.4f}"


def order_out(order: DepositOrder, settings: Settings) -> dict:
    token = settings.token_configs().get(order.token)
    return {
        "id": order.id,
        "chain": order.chain,
        "token": order.token,
        "token_contract": token.contract if token else None,
        "to_address": settings.receive_address,
        "amount_usd": order.amount_usd,
        "pay_amount": pay_amount_str(order.pay_amount_micro),
        "status": order.status,
        "expires_at": iso_utc(order.expires_at),
        "paid_at": iso_utc(order.paid_at),
        "tx_hash": order.tx_hash,
        "note": "請轉帳「完全相同」的金額（含小數點後 4 位）。從交易所提領時，請確認『實際到帳』金額等於這個數字。",
    }


@router.post("/deposits", status_code=201)
async def create_deposit(body: DepositIn, request: Request, user: User = Depends(current_user)):
    settings: Settings = request.app.state.settings
    if not settings.chain_enabled:
        raise HTTPException(503, "儲值功能未啟用")
    token = body.token.upper()
    if token not in settings.token_configs():
        raise HTTPException(400, f"不支援的幣種，可用：{', '.join(settings.token_configs())}")
    if not settings.deposit_min_usd <= body.amount_usd <= settings.deposit_max_usd:
        raise HTTPException(400, f"金額需介於 {settings.deposit_min_usd} ~ {settings.deposit_max_usd} 美元")
    # 單一程序內排隊建立訂單，避免兩張訂單同時抽到同一個尾數
    async with request.app.state.deposit_lock:
        order = await create_order(request.app.state.sessionmaker, settings, user.id, token, body.amount_usd)
    return order_out(order, settings)


async def create_order(sm: async_sessionmaker, settings: Settings, user_id: int, token: str,
                       amount_usd: int) -> DepositOrder:
    async with sm.begin() as session:
        now = utcnow()
        # 掃鏈程式至少要成功跑過一次才開放建單：不然第一次掃描會從「當下」開始，
        # 在那之前就付款的訂單會被整個跳過，連人工處理的紀錄都沒有
        cursor = await session.get(ChainCursor, settings.chain_name)
        if cursor is None:
            raise HTTPException(503, "儲值系統正在初始化，請稍後再試")
        pending = (await session.execute(
            select(func.count()).select_from(DepositOrder)
            .where(DepositOrder.user_id == user_id, DepositOrder.status == "pending")
        )).scalar_one()
        if pending >= settings.max_pending_deposits:
            raise HTTPException(429, f"未付款的訂單最多 {settings.max_pending_deposits} 張，請先完成或等它過期")
        # 先查出這個整數金額已經用掉的尾數，再從剩下的裡面隨機挑。
        # 不能用「隨機抽、撞到再重抽」：快用完時（例如只剩 1 個）幾乎一定抽不到
        base = amount_usd * MICRO
        used = set((await session.execute(
            select(DepositOrder.pay_amount_micro).where(
                DepositOrder.chain == settings.chain_name,
                DepositOrder.token == token,
                DepositOrder.pay_amount_micro > base,
                DepositOrder.pay_amount_micro < base + MICRO,
                DepositOrder.created_at >= now - AMOUNT_REUSE_WINDOW,
            )
        )).scalars())
        free = [base + i * TAIL_STEP_MICRO for i in range(1, TAIL_SLOTS + 1)]
        free = [pay for pay in free if pay not in used]
        if not free:
            raise HTTPException(503, "這個金額目前沒有可用的尾數，請換個金額或稍後再試")
        pay = secrets.choice(free)
        order = DepositOrder(
            user_id=user_id, chain=settings.chain_name, token=token, amount_usd=amount_usd,
            pay_amount_micro=pay, min_block=cursor.last_head, created_at=now,
            expires_at=now + timedelta(minutes=settings.deposit_ttl_minutes),
        )
        session.add(order)
        await session.flush()
    return order


@router.get("/deposits")
async def list_deposits(request: Request, user: User = Depends(current_user)):
    async with request.app.state.sessionmaker() as session:
        rows = (await session.execute(
            select(DepositOrder).where(DepositOrder.user_id == user.id).order_by(DepositOrder.id.desc()).limit(50)
        )).scalars().all()
    return [order_out(o, request.app.state.settings) for o in rows]


# ---------------------------------------------------------------- 入帳


def raw_to_micro(amount_raw: int, decimals: int, *, exact: bool = True) -> int | None:
    """鏈上原始整數 → micro 單位。

    exact=True：小數位比 micro 還細（不可能是我們開出的金額）就回 None。
    exact=False：直接捨去多餘的位數（人工入帳用）。
    """
    if decimals < 6:
        return amount_raw * 10 ** (6 - decimals)
    factor = 10 ** (decimals - 6)
    if exact and amount_raw % factor:
        return None
    return amount_raw // factor


async def record_transfer(sm: async_sessionmaker, chain: str, token: TokenConfig, *, tx_hash: str, log_index: int,
                          block_number: int, from_address: str, amount_raw: int) -> str:
    """記錄一筆鏈上轉入並嘗試對上訂單。回傳 duplicate / matched / unmatched。

    同一個資料庫交易裡完成「記錄轉帳 + 標記訂單已付 + 加餘額 + 寫帳本」，
    任何一步失敗就全部撤銷，不會出現「訂單標成已付但錢沒加」的半套狀態。
    """
    try:
        async with sm.begin() as session:
            if await _transfer_exists(session, chain, tx_hash, log_index):
                return "duplicate"
            transfer = ChainTransfer(
                chain=chain, token=token.symbol, token_contract=token.contract, decimals=token.decimals,
                tx_hash=tx_hash, log_index=log_index, block_number=block_number,
                from_address=from_address, amount_raw=str(amount_raw), status="unmatched",
            )
            session.add(transfer)
            await session.flush()  # 唯一限制在這裡生效：兩個程序同時處理同一筆，其中一個會失敗

            micro = raw_to_micro(amount_raw, token.decimals)
            if micro is None:
                transfer.note = "金額小數位數太多"
                return "unmatched"
            orders = (await session.execute(
                select(DepositOrder).where(
                    DepositOrder.chain == chain, DepositOrder.token == token.symbol,
                    DepositOrder.status == "pending", DepositOrder.pay_amount_micro == micro,
                    # 開單時記下的鏈高（含）以前的區塊，在開單前就已經存在，裡面的轉帳不可能是付這張單
                    DepositOrder.min_block < block_number,
                ).limit(2)
            )).scalars().all()
            if len(orders) != 1:
                transfer.note = "找不到對應訂單" if not orders else "多張訂單金額相同"
                return "unmatched"
            order = orders[0]
            result = await session.execute(
                update(DepositOrder)
                .where(DepositOrder.id == order.id, DepositOrder.status == "pending")
                .values(status="paid", paid_at=utcnow(), tx_hash=tx_hash)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                transfer.note = "訂單狀態已改變"
                return "unmatched"
            await change_balance(session, order.user_id, micro, "deposit", f"deposit:{order.id}")
            transfer.status, transfer.order_id = "matched", order.id
            return "matched"
    except IntegrityError:
        # 只有「同一筆轉帳已經被另一個程序寫入」才算重複；其他資料庫錯誤要往上拋，
        # 讓這一輪掃描中止、進度不推進，下次重掃，而不是默默跳過一筆真的付款
        async with sm() as session:
            if await _transfer_exists(session, chain, tx_hash, log_index):
                return "duplicate"
        raise


async def _transfer_exists(session, chain: str, tx_hash: str, log_index: int) -> bool:
    found = (await session.execute(
        select(ChainTransfer.id).where(
            ChainTransfer.chain == chain, ChainTransfer.tx_hash == tx_hash, ChainTransfer.log_index == log_index
        )
    )).scalar_one_or_none()
    return found is not None


async def expire_stale_orders(sm: async_sessionmaker, settings: Settings) -> int:
    """過期 + 寬限期都過了的訂單才作廢。寬限期內晚到的轉帳仍然可以對上。

    只能在掃鏈追上最新的已確認區塊後呼叫（見 ChainWatcher.poll_once）。
    """
    cutoff = utcnow() - timedelta(minutes=settings.deposit_grace_minutes)
    async with sm.begin() as session:
        result = await session.execute(
            update(DepositOrder)
            .where(DepositOrder.status == "pending", DepositOrder.expires_at < cutoff)
            .values(status="expired")
            .execution_options(synchronize_session=False)
        )
    return result.rowcount


async def assign_transfer(sm: async_sessionmaker, settings: Settings, transfer_id: int, email: str) -> str:
    """站長人工處理對不上的轉帳（例如金額打錯）：把這筆錢加給指定帳號。回傳入帳金額（美元字串）。"""
    async with sm.begin() as session:
        transfer = await session.get(ChainTransfer, transfer_id)
        if transfer is None or transfer.status != "unmatched":
            raise ValueError("找不到這筆轉帳，或它已經處理過")
        user = (await session.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()
        if user is None:
            raise ValueError(f"找不到使用者 {email}")
        micro = raw_to_micro(int(transfer.amount_raw), transfer.decimals, exact=False)  # 用入帳當時記下的精度
        result = await session.execute(
            update(ChainTransfer)
            .where(ChainTransfer.id == transfer_id, ChainTransfer.status == "unmatched")
            .values(status="manual", note=f"人工入帳：{email}"[:64])
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise ValueError("這筆轉帳剛剛被處理過了")
        await change_balance(session, user.id, micro, "deposit_manual", f"transfer:{transfer_id}")
    return format_usd(micro)
