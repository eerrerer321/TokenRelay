import os
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.billing import change_balance
from app.config import ModelConfig, Settings, TokenConfig, UpstreamConfig
from app.main import create_app
from app.models import Ledger, UsageLog, User
from tests.fakes import UPSTREAM_SECRET, FakeChain, FakeUpstream

RECEIVE = "0x" + "ab" * 20
USDT = "0x" + "01" * 20
USDC = "0x" + "02" * 20
FAKE_USDT = "0x" + "66" * 20
INVITE = "friends-only"


@dataclass
class Harness:
    app: FastAPI
    client: httpx.AsyncClient
    upstream: FakeUpstream
    chain: FakeChain

    @property
    def sm(self):
        return self.app.state.sessionmaker

    async def register_login(self, email: str = "alice@example.com", password: str = "correct-horse-1") -> str:
        r = await self.client.post("/api/register", json={"email": email, "password": password, "invite_code": INVITE})
        assert r.status_code == 201, r.text
        r = await self.client.post("/api/login", json={"email": email, "password": password})
        assert r.status_code == 200, r.text
        return r.json()["token"]

    async def create_key(self, session_token: str) -> str:
        r = await self.client.post("/api/keys", json={"name": "test"}, headers=auth(session_token))
        assert r.status_code == 201, r.text
        return r.json()["key"]

    async def user_with_key(self, email: str = "alice@example.com", credit_micro: int = 0) -> tuple[str, str]:
        session_token = await self.register_login(email)
        key = await self.create_key(session_token)
        if credit_micro:
            await self.credit(email, credit_micro)
        return session_token, key

    async def credit(self, email: str, micro: int) -> None:
        async with self.sm.begin() as s:
            user_id = (await s.execute(select(User.id).where(User.email == email))).scalar_one()
            await change_balance(s, user_id, micro, "adjust", "test")

    async def balance(self, email: str = "alice@example.com") -> int:
        async with self.sm() as s:
            return (await s.execute(select(User.balance_micro).where(User.email == email))).scalar_one()

    async def chain_ready(self) -> None:
        """讓掃鏈程式跑第一輪（建立掃描進度），之後才能建立儲值訂單。"""
        await self.app.state.watcher.poll_once()

    async def add_model(self, name: str = "gpt-test", *, input_price: int | None = 1_000_000,
                        output_price: int | None = 2_000_000, max_output_tokens: int = 100,
                        max_tokens_field: str = "max_tokens", image_tokens: int = 0,
                        cached_input_price: int | None = None, kind: str = "chat", image_price: int | None = None,
                        upstream: str = "fake") -> None:
        """等同在 config.ini 加一個 [model:名稱] 區段（上游預設是假上游 fake）。"""
        self.app.state.settings.models[name] = ModelConfig(
            name=name, upstream=upstream, upstream_model=f"up-{name}", kind=kind,
            input_price_micro=input_price, output_price_micro=output_price, image_price_micro=image_price,
            cached_input_price_micro=cached_input_price,
            max_output_tokens=max_output_tokens, max_tokens_field=max_tokens_field, image_tokens=image_tokens,
        )

    async def assert_invariants(self) -> None:
        """每個測試結束時都要成立：
        1. 帳本加總 == 餘額
        2. 沒有任何用量紀錄停在 pending（每個預扣都有結算）
        3. 同時請求數的名額全部歸還
        """
        assert self.app.state.limiter._active == {}, f"名額沒有歸還：{self.app.state.limiter._active}"
        async with self.sm() as s:
            pending = (await s.execute(
                select(func.count()).select_from(UsageLog).where(UsageLog.status == "pending")
            )).scalar_one()
            assert pending == 0, f"有 {pending} 筆用量沒有結算"
            users = (await s.execute(select(User.id, User.balance_micro))).all()
            for user_id, balance in users:
                total = (await s.execute(
                    select(func.coalesce(func.sum(Ledger.amount_micro), 0)).where(Ledger.user_id == user_id)
                )).scalar_one()
                assert total == balance, f"user {user_id}: 帳本加總 {total} != 餘額 {balance}"


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def h(tmp_path):
    upstream, chain = FakeUpstream(), FakeChain()
    # 設定 TEST_DATABASE_URL 就改用 PostgreSQL 跑測試（請指向一個「專門給測試用、可清空」的資料庫）
    database_url = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    settings = Settings(
        database_url=database_url,
        invite_code=INVITE,
        max_concurrent_per_user=50,
        chain_name="bsc-test",
        chain_rpc_url="http://rpc.test",
        chain_confirmations=3,
        chain_start_block=1,
        receive_address=RECEIVE,
        tokens={"USDT": TokenConfig(symbol="USDT", contract=USDT), "USDC": TokenConfig(symbol="USDC", contract=USDC)},
        upstreams={
            # 假上游三種格式都支援：OpenAI chat、OpenAI responses、Anthropic messages
            "fake": UpstreamConfig(name="fake", base_url="http://upstream/v1", api_key=UPSTREAM_SECRET,
                                   responses=True, anthropic_base_url="http://upstream/anthropic"),
            "plain": UpstreamConfig(name="plain", base_url="http://upstream/v1", api_key=UPSTREAM_SECRET),
        },
    )
    app = create_app(
        settings,
        upstream_transport=upstream.transport,
        rpc_transport=httpx.MockTransport(chain.handle),
        start_watcher=False,
    )
    async with app.router.lifespan_context(app):
        if os.environ.get("TEST_DATABASE_URL"):
            await _reset_postgres(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            harness = Harness(app, client, upstream, chain)
            yield harness
            await harness.assert_invariants()


async def _reset_postgres(app: FastAPI) -> None:
    from app.db import Base

    async with app.state.sessionmaker.begin() as s:
        for table in reversed(Base.metadata.sorted_tables):
            await s.execute(table.delete())
