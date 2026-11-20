"""計費：費用計算、預扣、結算、帳本。金額單位一律 micro-USD（1 = 0.000001 美元）。"""

from collections import defaultdict
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import utcnow
from app.models import ApiKey, Ledger, UsageLog, User

MICRO = 1_000_000
INPUT_MARGIN_TOKENS = 64  # 上游套對話模板時額外加的少量 token
# 請求帶工具時，上游會自動加一段看不到的「工具使用說明」並算進輸入（Anthropic 約 300～500 tokens），
# 請求的 byte 數裡沒有它，所以預扣另外加這麼多
TOOLS_MARGIN_TOKENS = 1024


def calc_cost(prompt_tokens: int, completion_tokens: int, input_price_micro: int, output_price_micro: int, *,
              cached_tokens: int = 0, cached_input_price_micro: int | None = None) -> int:
    """價格單位是「每 1M tokens 幾 micro-USD」。

    cached_tokens 是 prompt_tokens 裡「快取命中」的部分，改用快取價；沒設定快取價就照輸入價收。
    無條件進位：如果用四捨五入或捨去，「每次只送 1 個 token」的請求會被算成 0 元，可以無限白嫖。
    """
    cached = min(cached_tokens, prompt_tokens)
    cached_price = input_price_micro if cached_input_price_micro is None else cached_input_price_micro
    total = ((prompt_tokens - cached) * input_price_micro + cached * cached_price
             + completion_tokens * output_price_micro)
    return -(-total // MICRO)


def estimate_hold(
    body_bytes: int, image_count: int, image_tokens: int, output_cap: int, input_price_micro: int,
    output_price_micro: int, *, has_tools: bool = False,
) -> int:
    """這個請求「最貴可能花多少」。

    輸入：主流 tokenizer 一個 token 至少 1 byte，所以請求的 byte 數一定 ≥ 文字 token 數。
    圖片：請求裡只佔一個網址，上游卻按圖片大小收費，所以每張再加上該模型設定的最壞 token 數。
    工具：上游加的工具使用說明不在請求裡，帶工具時再加 TOOLS_MARGIN_TOKENS。
    輸出：上限欄位已經被站方改寫成模型上限以內，上游最多只會生成這麼多。
    """
    input_upper = (body_bytes + INPUT_MARGIN_TOKENS + image_count * image_tokens
                   + (TOOLS_MARGIN_TOKENS if has_tools else 0))
    return calc_cost(input_upper, output_cap, input_price_micro, output_price_micro)


def format_usd(micro: int) -> str:
    return f"{Decimal(micro) / MICRO:.6f}"


def usd_to_micro(text: str) -> int:
    """「1.23」這種美元字串 → micro 整數。最多 6 位小數（比 micro 更小的單位無法表示）。"""
    try:
        value = Decimal(text) * MICRO
    except InvalidOperation:
        raise ValueError(f"不是數字：{text}") from None
    if not value.is_finite():
        raise ValueError(f"不是數字：{text}")
    if value != value.to_integral_value():
        raise ValueError("最多 6 位小數")
    return int(value)


async def change_balance(
    session: AsyncSession, user_id: int, delta: int, kind: str, ref: str, *, require_sufficient: bool = False
) -> bool:
    """改餘額並寫帳本，必須在呼叫端的交易裡執行。

    require_sufficient=True 時，「檢查餘額夠不夠」和「扣款」是同一句 SQL，
    資料庫保證不會有兩個請求同時通過檢查（這就是防透支的關鍵）。
    """
    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(balance_micro=User.balance_micro + delta)
        .execution_options(synchronize_session=False)
    )
    if require_sufficient:
        stmt = stmt.where(User.balance_micro >= -delta)
    result = await session.execute(stmt)
    if result.rowcount != 1:
        return False
    session.add(Ledger(user_id=user_id, amount_micro=delta, kind=kind, ref=ref))
    return True


class HoldRejected(Exception):
    """預扣失敗。reason：insufficient_balance（餘額不足）/ key_revoked（key 在請求途中被停用）。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def place_hold(
    sm: async_sessionmaker, *, user_id: int, api_key_id: int, model: str, stream: bool, hold: int,
    upstream: str = "",
) -> int:
    """建立 pending 的用量紀錄並預扣，回傳用量紀錄 id。失敗時整個交易撤銷，什麼都沒寫入。

    預扣成功 = 站方正式接受這個請求，所以「key 仍然啟用」也在同一個交易裡再確認一次：
    驗證 key 之後、讀完請求內容之前被停用的 key，不能再花到錢。
    """
    if hold < 0:  # 只有站長把模型設定填錯才會發生；負的預扣等於先送使用者額度
        raise ValueError(f"預扣金額不可為負：{hold}")
    async with sm.begin() as session:
        # FOR SHARE 鎖住這把 key 直到預扣提交：停用 key 的 UPDATE 會等這裡結束，
        # 所以「停用成功」之後不會再有請求預扣成功。SQLite 會略過這個語法（它本來就整筆交易排隊）
        active = (await session.execute(
            select(ApiKey.is_active).where(ApiKey.id == api_key_id, ApiKey.user_id == user_id)
            .with_for_update(read=True)
        )).scalar_one_or_none()
        if not active:
            raise HoldRejected("key_revoked")
        usage = UsageLog(user_id=user_id, api_key_id=api_key_id, model=model, upstream=upstream, stream=stream,
                         hold_micro=hold)
        session.add(usage)
        await session.flush()
        if not await change_balance(session, user_id, -hold, "hold", f"usage:{usage.id}", require_sufficient=True):
            raise HoldRejected("insufficient_balance")
        return usage.id


async def settle(
    sm: async_sessionmaker,
    usage_id: int,
    *,
    cost: int,
    status: str,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    upstream_status: int | None = None,
    cached_tokens: int | None = None,
    upstream_cost: int | None = None,
    upstream_cost_estimated: bool | None = None,
    upstream_allowance: int | None = None,
) -> bool:
    """結算一筆預扣：退回 hold - cost（若實際比預扣還貴，差額照扣）。保證只會結算一次。

    upstream_* 只記下來給收益試算用，不影響向使用者收的錢。
    """
    async with sm.begin() as session:
        # 先 UPDATE 再讀：status='pending' 條件讓第二次結算改到 0 列，直接結束
        result = await session.execute(
            update(UsageLog)
            .where(UsageLog.id == usage_id, UsageLog.status == "pending")
            .values(
                status=status,
                cost_micro=cost,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                upstream_status=upstream_status,
                cached_tokens=cached_tokens,
                upstream_cost_micro=upstream_cost,
                upstream_cost_estimated=upstream_cost_estimated,
                upstream_allowance_micro=upstream_allowance,
                settled_at=utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return False
        user_id, hold = (
            await session.execute(select(UsageLog.user_id, UsageLog.hold_micro).where(UsageLog.id == usage_id))
        ).one()
        refund = hold - cost
        if refund:
            await change_balance(session, user_id, refund, "settle", f"usage:{usage_id}")
    return True


async def settle_abandoned_holds(sm: async_sessionmaker) -> int:
    """伺服器啟動時呼叫：上次當機或關機時還沒結算的預扣，照「拿不到用量」的規則收全額預扣，回傳處理了幾筆。

    只適用單一程序：啟動當下不會有其他程序的請求還在進行中（同攻防清單 C）。
    """
    async with sm() as session:
        rows = (await session.execute(
            select(UsageLog.id, UsageLog.hold_micro).where(UsageLog.status == "pending")
        )).all()
    for usage_id, hold in rows:
        await settle(sm, usage_id, cost=hold, status="abandoned")
    return len(rows)


class ConcurrencyLimiter:
    """每個使用者同時進行中的請求數上限。存在記憶體裡，只適用單一程序。"""

    def __init__(self, limit: int):
        self.limit = limit
        self._active: dict[int, int] = defaultdict(int)

    def try_acquire(self, user_id: int) -> bool:
        if self._active[user_id] >= self.limit:
            return False
        self._active[user_id] += 1
        return True

    def release(self, user_id: int) -> None:
        self._active[user_id] -= 1
        if self._active[user_id] <= 0:
            del self._active[user_id]
