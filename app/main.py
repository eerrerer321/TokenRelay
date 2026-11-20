"""程式進入點。啟動：uvicorn app.main:create_app --factory --port 8000（設定讀自 config.ini）"""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import accounts, admin, apis, deposits, relay
from app.billing import ConcurrencyLimiter, settle_abandoned_holds
from app.chain import ChainWatcher
from app.config import CONFIG_PATH, Settings, load_settings
from app.db import create_tables, make_engine, make_sessionmaker
from app.security import WEB_SECURITY_HEADERS, LoginGuard

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class WebFiles(StaticFiles):
    """提供 app/static 裡的網頁，並加上安全標頭。"""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.update(WEB_SECURITY_HEADERS)
        return response


def create_app(
    settings: Settings | None = None,
    *,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    rpc_transport: httpx.AsyncBaseTransport | None = None,
    start_watcher: bool = True,
    config_path: Path | None = None,
) -> FastAPI:
    """建立 app。測試時可以把上游與 RPC 換成假的 transport。

    config_path：後台改模型時要寫回的設定檔。沒給 settings 時就是 config.ini；
    直接給 settings（測試）又沒給 config_path 時，後台只能看、不能改。
    """
    if settings is None:
        config_path = config_path or CONFIG_PATH
        settings = load_settings(config_path)  # config.ini 有錯會在這裡直接報錯，伺服器不會啟動

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = app.state
        state.settings = settings
        state.config_path = config_path
        state.config_lock = asyncio.Lock()  # 後台同時改兩次設定檔時排隊，不會互相蓋掉
        engine = make_engine(settings.database_url)
        await create_tables(engine)
        state.sessionmaker = make_sessionmaker(engine)
        abandoned = await settle_abandoned_holds(state.sessionmaker)
        if abandoned:
            log.warning("上次關機時有 %s 筆請求還沒結算，已照預扣金額收費（狀態 abandoned）", abandoned)
        state.limiter = ConcurrencyLimiter(settings.max_concurrent_per_user)
        state.login_guard = LoginGuard(settings.login_max_failures, settings.login_lock_minutes * 60)
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
    app.include_router(apis.router)
    app.include_router(accounts.router)
    app.include_router(deposits.router)
    app.include_router(admin.router)

    @app.get("/health", tags=["系統"])
    async def health():
        return {"ok": True}

    # 一定要放最後：上面的 API 路由優先，其他路徑才交給網頁檔案
    app.mount("/", WebFiles(directory=STATIC_DIR, html=True), name="web")
    return app
