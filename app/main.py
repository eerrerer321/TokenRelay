"""程式進入點。啟動：uvicorn app.main:create_app --factory --port 8000（設定讀自 config.ini）"""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI

from app import accounts, deposits, relay
from app.billing import ConcurrencyLimiter
from app.chain import ChainWatcher
from app.config import Settings, get_settings
from app.db import create_tables, make_engine, make_sessionmaker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(
    settings: Settings | None = None,
    *,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    rpc_transport: httpx.AsyncBaseTransport | None = None,
    start_watcher: bool = True,
) -> FastAPI:
    """建立 app。測試時可以把上游與 RPC 換成假的 transport。"""
    settings = settings or get_settings()  # config.ini 有錯會在這裡直接報錯，伺服器不會啟動

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = app.state
        state.settings = settings
        engine = make_engine(settings.database_url)
        await create_tables(engine)
        state.sessionmaker = make_sessionmaker(engine)
        state.limiter = ConcurrencyLimiter(settings.max_concurrent_per_user)
        state.deposit_lock = asyncio.Lock()
        state.http = httpx.AsyncClient(
            transport=upstream_transport, timeout=httpx.Timeout(settings.upstream_timeout_seconds, connect=10)
        )
        rpc_http = httpx.AsyncClient(transport=rpc_transport, timeout=20)
        state.watcher = ChainWatcher(settings, state.sessionmaker, rpc_http) if settings.chain_enabled else None
        task = asyncio.create_task(state.watcher.run_forever()) if state.watcher and start_watcher else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await state.http.aclose()
            await rpc_http.aclose()
            await engine.dispose()

    app = FastAPI(title="AI 模型轉發站", lifespan=lifespan)
    app.add_exception_handler(relay.OpenAIError, relay.openai_error_handler)
    app.include_router(relay.router)
    app.include_router(accounts.router)
    app.include_router(deposits.router)

    @app.get("/health", tags=["系統"])
    async def health():
        return {"ok": True}

    return app
