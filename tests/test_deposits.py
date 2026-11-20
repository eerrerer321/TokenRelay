"""儲值：尾數訂單、掃鏈入帳，以及假幣、重複入帳、金額不符、掃鏈延遲等情境。"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app import deposits
from app.config import TokenConfig
from app.db import utcnow
from app.deposits import assign_transfer, record_transfer
from app.models import ChainCursor, ChainTransfer, DepositOrder, User
from tests.conftest import FAKE_USDT, RECEIVE, USDC, USDT, auth


async def create_order(h, session_token: str, token: str = "USDT", amount: int = 10):
    return await h.client.post("/api/deposits", json={"token": token, "amount_usd": amount},
                               headers=auth(session_token))


def to_raw(pay_amount: str, decimals: int = 18) -> int:
    return int(Decimal(pay_amount) * 10**decimals)


def to_micro(pay_amount: str) -> int:
    return int(Decimal(pay_amount) * 10**6)


async def order_status(h, order_id: int) -> str:
    async with h.sm() as s:
        return (await s.execute(select(DepositOrder.status).where(DepositOrder.id == order_id))).scalar_one()


async def test_orders_are_blocked_until_chain_watcher_has_run(h):
    """掃鏈程式還沒跑過就開單，第一次掃描會從當下開始，之前的付款會被整個跳過。"""
    session_token = await h.register_login()
    assert (await create_order(h, session_token)).status_code == 503
    await h.chain_ready()
    assert (await create_order(h, session_token)).status_code == 201


async def test_orders_get_distinct_tails_and_pending_limit(h):
    await h.chain_ready()
    session_token = await h.register_login()
    amounts = []
    for _ in range(3):
        r = await create_order(h, session_token)
        assert r.status_code == 201, r.text
        order = r.json()
        assert order["to_address"] == RECEIVE and order["token_contract"] == USDT
        assert Decimal("10.0001") <= Decimal(order["pay_amount"]) <= Decimal("10.0999")
        amounts.append(order["pay_amount"])
    assert len(set(amounts)) == 3
    assert (await create_order(h, session_token)).status_code == 429  # 未付款訂單上限 3 張


@pytest.mark.parametrize("first_order_state", ["pending", "paid", "expired"])
@pytest.mark.parametrize("age, reusable", [(timedelta(days=7) - timedelta(minutes=1), False),
                                           (timedelta(days=7) + timedelta(minutes=1), True)])
async def test_same_pay_amount_is_never_reissued_within_window(h, monkeypatch, first_order_state, age, reusable):
    """不管前一張同金額的訂單是待付、已付還是已作廢，7 天內都不能再發給別人；超過 7 天才可以。"""
    await h.chain_ready()
    monkeypatch.setattr(deposits, "TAIL_SLOTS", 1)  # 只有一個尾數可用 → 兩張單一定搶同一個金額
    alice = await h.register_login("alice@example.com")
    bob = await h.register_login("bob@example.com")
    first = (await create_order(h, alice)).json()
    assert first["pay_amount"] == "10.0001"
    async with h.sm.begin() as s:
        await s.execute(update(DepositOrder).values(status=first_order_state, created_at=utcnow() - age))
    r = await create_order(h, bob)
    if reusable:
        assert r.status_code == 201 and r.json()["pay_amount"] == "10.0001"
    else:
        assert r.status_code == 503  # 寧可拒絕，也不發出重複金額


async def test_last_free_tail_is_still_found(h):
    """999 個尾數只剩 1 個時，仍然要能開單（「隨機抽、撞到重抽」的寫法這時幾乎一定失敗）。"""
    await h.chain_ready()
    session_token = await h.register_login()
    now = utcnow()
    async with h.sm.begin() as s:
        user_id = (await s.execute(select(User.id))).scalar_one()
        s.add_all(DepositOrder(user_id=user_id, chain="bsc-test", token="USDT", amount_usd=10,
                               pay_amount_micro=10_000_000 + i * 100, min_block=0, status="expired",
                               created_at=now, expires_at=now)
                  for i in range(1, 1000) if i != 500)
    r = await create_order(h, session_token)
    assert r.status_code == 201, r.text
    assert r.json()["pay_amount"] == "10.0500"


async def test_confirmed_transfer_is_credited_exactly_once(h):
    await h.chain_ready()
    session_token = await h.register_login()
    order = (await create_order(h, session_token)).json()
    h.chain.add_transfer(contract=USDT, to=RECEIVE, amount_raw=to_raw(order["pay_amount"]), block=5)
    h.chain.head = 10

    result = await h.app.state.watcher.poll_once()
    assert result["matched"] == 1
    assert await h.balance() == to_micro(order["pay_amount"])
    assert await order_status(h, order["id"]) == "paid"

    # 模擬「入帳後、更新掃鏈進度前」當機：進度倒回去重掃，同一筆不可以再入帳一次
    async with h.sm.begin() as s:
        await s.execute(update(ChainCursor).values(last_block=0))
    result = await h.app.state.watcher.poll_once()
    assert result["duplicate"] == 1 and result["matched"] == 0
    assert await h.balance() == to_micro(order["pay_amount"])


async def test_unconfirmed_transfer_waits_for_confirmations(h):
    await h.chain_ready()
    session_token = await h.register_login()
    order = (await create_order(h, session_token, token="USDC")).json()
    h.chain.add_transfer(contract=USDC, to=RECEIVE, amount_raw=to_raw(order["pay_amount"]), block=9)
    h.chain.head = 10  # 確認數 3 → 只處理到區塊 7

    await h.app.state.watcher.poll_once()
    assert await h.balance() == 0
    h.chain.head = 12
    result = await h.app.state.watcher.poll_once()
    assert result["matched"] == 1
    assert await h.balance() == to_micro(order["pay_amount"])


async def test_fake_token_with_same_amount_is_ignored(h):
    """攻擊者自己發一個叫 USDT 的代幣轉過來。就算 RPC 沒照合約地址過濾，也不能入帳。"""
    await h.chain_ready()
    session_token = await h.register_login()
    order = (await create_order(h, session_token)).json()
    h.chain.ignore_address_filter = True
    h.chain.add_transfer(contract=FAKE_USDT, to=RECEIVE, amount_raw=to_raw(order["pay_amount"]), block=5)
    h.chain.head = 10

    result = await h.app.state.watcher.poll_once()
    assert result["matched"] == 0 and result["ignored"] >= 1
    assert await h.balance() == 0


async def test_payment_older_than_the_order_is_not_matched(h):
    """掃鏈落後時，開單前就發生的同金額轉帳（例如很久以前別人的付款）不能拿來付這張單。"""
    h.chain.head = 100
    await h.chain_ready()  # 記下目前鏈高 100
    async with h.sm.begin() as s:  # 模擬掃鏈進度落後在區塊 0
        await s.execute(update(ChainCursor).values(last_block=0))
    session_token = await h.register_login()
    order = (await create_order(h, session_token)).json()  # 開單高度 = 100
    h.chain.add_transfer(contract=USDT, to=RECEIVE, amount_raw=to_raw(order["pay_amount"]), block=50)
    # 區塊 100 在開單時就已經存在（開單高度就是它），裡面的轉帳也一定早於開單
    h.chain.add_transfer(contract=USDT, to=RECEIVE, amount_raw=to_raw(order["pay_amount"]), block=100)
    h.chain.head = 103  # 確認數 3 → 這一輪處理到區塊 100

    result = await h.app.state.watcher.poll_once()
    assert result["unmatched"] == 2
    assert await h.balance() == 0
    assert await order_status(h, order["id"]) == "pending"

    # 開單之後的區塊才算數
    h.chain.add_transfer(contract=USDT, to=RECEIVE, amount_raw=to_raw(order["pay_amount"]), block=101)
    h.chain.head = 110
    result = await h.app.state.watcher.poll_once()
    assert result["matched"] == 1
    assert await order_status(h, order["id"]) == "paid"


async def test_chain_head_seen_at_order_time_never_goes_backwards(h):
    """RPC 節點落後、或兩個掃描同時跑時，記下的鏈高不能倒退（倒退 = 開單高度變低 = 舊款可能對上新單）。"""
    h.chain.head = 100
    await h.chain_ready()
    h.chain.head = 90
    await h.app.state.watcher.poll_once()
    async with h.sm() as s:
        assert (await s.get(ChainCursor, "bsc-test")).last_head == 100


async def test_future_start_block_does_not_skip_payments(h):
    """[chain] start_block 設在未來：建單已經開放，起點之前的付款也必須掃得到。"""
    h.app.state.settings.chain_start_block = 120
    h.chain.head = 100
    await h.chain_ready()
    session_token = await h.register_login()
    order = (await create_order(h, session_token)).json()
    h.chain.add_transfer(contract=USDT, to=RECEIVE, amount_raw=to_raw(order["pay_amount"]), block=105)
    h.chain.head = 130
    while await h.app.state.watcher.poll_once() is not None:
        pass
    assert await order_status(h, order["id"]) == "paid"


async def test_on_time_payment_is_not_lost_while_watcher_catches_up(h):
    """掃鏈積壓時，不能因為「還沒掃到」就先把準時付款的訂單作廢。"""
    await h.chain_ready()
    session_token = await h.register_login()
    order = (await create_order(h, session_token)).json()
    h.chain.add_transfer(contract=USDT, to=RECEIVE, amount_raw=to_raw(order["pay_amount"]), block=50)
    h.chain.head = 100
    h.app.state.settings.chain_max_block_range = 10  # 一次只掃 10 個區塊 → 要掃好幾輪才追得上
    async with h.sm.begin() as s:  # 依牆上時鐘，這張單早就過期又過了寬限期
        await s.execute(update(DepositOrder).values(expires_at=utcnow() - timedelta(hours=2)))

    first = await h.app.state.watcher.poll_once()
    assert first["to_block"] < first["safe_block"]  # 還在補掃
    assert await order_status(h, order["id"]) == "pending"  # 還沒追上 → 不作廢
    while (result := await h.app.state.watcher.poll_once()) is not None:
        pass
    assert await order_status(h, order["id"]) == "paid"
    assert await h.balance() == to_micro(order["pay_amount"])


@pytest.mark.parametrize("delta_raw, reason", [
    (None, "找不到對應訂單"),  # 忘了打尾數，只轉 10 整
    (1, "金額小數位數太多"),  # 多轉了 1 wei
])
async def test_mismatched_amount_is_held_for_manual_review(h, delta_raw, reason):
    await h.chain_ready()
    session_token = await h.register_login()
    order = (await create_order(h, session_token)).json()
    raw = to_raw("10") if delta_raw is None else to_raw(order["pay_amount"]) + delta_raw
    h.chain.add_transfer(contract=USDT, to=RECEIVE, amount_raw=raw, block=5)
    h.chain.head = 10

    result = await h.app.state.watcher.poll_once()
    assert result["unmatched"] == 1
    assert await h.balance() == 0
    async with h.sm() as s:
        transfer = (await s.execute(select(ChainTransfer))).scalar_one()
    assert transfer.note == reason

    # 之後就算設定檔裡的精度被改錯，人工入帳也要用入帳當時記下的精度（18 位）
    h.app.state.settings.tokens = {"USDT": TokenConfig(symbol="USDT", contract=USDT, decimals=6),
                                   "USDC": TokenConfig(symbol="USDC", contract=USDC)}
    await assign_transfer(h.sm, h.app.state.settings, transfer.id, "alice@example.com")
    assert await h.balance() == raw // 10**12  # 捨去 micro 以下的位數
    with pytest.raises(ValueError):  # 只能處理一次
        await assign_transfer(h.sm, h.app.state.settings, transfer.id, "alice@example.com")


async def test_expired_order_is_not_credited(h):
    await h.chain_ready()
    session_token = await h.register_login()
    order = (await create_order(h, session_token)).json()
    async with h.sm.begin() as s:  # 讓訂單過期而且超過寬限期
        await s.execute(update(DepositOrder).values(expires_at=utcnow() - timedelta(hours=2)))
    h.chain.head = 4
    await h.app.state.watcher.poll_once()  # 已追上最新區塊 → 作廢過期訂單
    assert await order_status(h, order["id"]) == "expired"

    h.chain.add_transfer(contract=USDT, to=RECEIVE, amount_raw=to_raw(order["pay_amount"]), block=5)
    h.chain.head = 10
    result = await h.app.state.watcher.poll_once()
    assert result["unmatched"] == 1
    assert await h.balance() == 0


async def test_integrity_error_counts_as_duplicate_only_if_transfer_really_exists(h, monkeypatch):
    token = h.app.state.settings.token_configs()["USDT"]
    kwargs = dict(tx_hash="0x" + "aa" * 32, log_index=0, block_number=5, from_address="0x" + "11" * 20,
                  amount_raw=to_raw("1"))
    assert await record_transfer(h.sm, "bsc-test", token, **kwargs) == "unmatched"

    real_exists = deposits._transfer_exists
    calls = []

    async def skip_first_check(session, *args):
        calls.append(1)
        return False if len(calls) == 1 else await real_exists(session, *args)

    # 模擬兩個程序同時通過「是否已存在」的檢查 → 撞到唯一限制 → 確認真的存在 → 算重複
    monkeypatch.setattr(deposits, "_transfer_exists", skip_first_check)
    assert await record_transfer(h.sm, "bsc-test", token, **kwargs) == "duplicate"

    # 撞到唯一限制、但查不到既有紀錄（代表是別的資料庫錯誤）→ 不能默默當成重複跳過
    async def never_exists(*_args):
        return False

    monkeypatch.setattr(deposits, "_transfer_exists", never_exists)
    with pytest.raises(IntegrityError):
        await record_transfer(h.sm, "bsc-test", token, **kwargs)


async def test_users_only_see_their_own_orders(h):
    await h.chain_ready()
    alice = await h.register_login("alice@example.com")
    bob = await h.register_login("bob@example.com")
    await create_order(h, alice)
    assert (await h.client.get("/api/deposits", headers=auth(bob))).json() == []
