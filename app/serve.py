"""正式啟動：python -m app.serve（設定讀自 config.ini 的 [server] 區段）

同一個程式開兩個入口，共用同一份狀態（同時請求數、登入失敗次數、掃鏈程式都只有一份）：
- 對外：HTTPS，用 win-acme 申請的憑證（預設 0.0.0.0:443）
- 對內：http://127.0.0.1:8000，只有這台電腦連得到，給你開後台（/admin）用
  （後台本身也會檢查是不是本機；這個入口是讓你不用拿網域憑證連 localhost 而跳出警告）

win-acme 續簽時會覆蓋憑證檔：這裡定期檢查檔案有沒有變，變了就換上新憑證，不用重啟伺服器。
開發時繼續用 uvicorn app.main:create_app --factory 就好（不用憑證）。
"""

import argparse
import asyncio
import logging
import ssl
from pathlib import Path

import uvicorn

from app.config import CONFIG_PATH, ConfigError, load_settings
from app.main import create_app

log = logging.getLogger("serve")


def _mtimes(*paths: str) -> tuple:
    return tuple(Path(p).stat().st_mtime_ns for p in paths)


async def watch_certificate(context: ssl.SSLContext, certfile: str, keyfile: str, interval: float) -> None:
    """憑證檔有變就重新載入。已經連上的連線不受影響，之後的新連線用新憑證。"""
    last = _mtimes(certfile, keyfile)
    while True:
        await asyncio.sleep(interval)
        try:
            current = _mtimes(certfile, keyfile)
            if current == last:
                continue
            context.load_cert_chain(certfile, keyfile)
            last = current
            log.info("已換上新的 HTTPS 憑證：%s", certfile)
        except (OSError, ssl.SSLError):
            # 例如 win-acme 正在寫檔、只寫了一半：舊憑證繼續用，下次再試
            log.exception("重新載入 HTTPS 憑證失敗，繼續用舊憑證")


def _config(app, host: str, port: int, *, lifespan: str, **ssl_files) -> uvicorn.Config:
    # proxy_headers=False：前面沒有反向代理，連線來源就是真實 IP，不相信任何人送來的 X-Forwarded-For
    return uvicorn.Config(app, host=host, port=port, lifespan=lifespan, proxy_headers=False, server_header=False,
                          **ssl_files)


async def serve(cert_check_seconds: float) -> None:
    settings = load_settings(CONFIG_PATH)
    app = create_app(settings, config_path=CONFIG_PATH)

    # 本機入口負責啟動／關閉（資料庫、掃鏈程式）；HTTPS 入口只是多一個門
    local = uvicorn.Server(_config(app, "127.0.0.1", settings.server_local_port, lifespan="on"))
    local_task = asyncio.create_task(local.serve())
    while not local.started:
        if local_task.done():  # 啟動失敗（例如埠被占用）：把錯誤丟出來
            await local_task
            return
        await asyncio.sleep(0.1)
    log.info("後台：http://localhost:%s/admin（只有這台電腦能開）", settings.server_local_port)

    if not settings.server_https_port:
        log.warning("[server] https_port = 0：沒有開對外的 HTTPS 入口")
        await local_task
        return

    for name, path in (("ssl_certfile", settings.server_ssl_certfile), ("ssl_keyfile", settings.server_ssl_keyfile)):
        if not path or not Path(path).is_file():
            raise ConfigError(f"[server] {name} 找不到檔案：{path or '（沒填）'}。win-acme 申請好憑證後填進來，"
                              "或把 https_port 設成 0 先只開本機入口")
    https_config = _config(app, settings.server_host, settings.server_https_port, lifespan="off",
                           ssl_certfile=settings.server_ssl_certfile, ssl_keyfile=settings.server_ssl_keyfile)
    https_config.load()  # 先載入，才拿得到 SSLContext 做續簽後的重新載入
    https = uvicorn.Server(https_config)
    https_task = asyncio.create_task(https.serve())
    watcher = asyncio.create_task(watch_certificate(
        https_config.ssl, settings.server_ssl_certfile, settings.server_ssl_keyfile, cert_check_seconds))
    try:
        await asyncio.wait({local_task, https_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # 先關對外入口，再關本機入口（它會收掉資料庫連線和掃鏈程式）
        watcher.cancel()
        https.should_exit = True
        await asyncio.gather(https_task, return_exceptions=True)
        local.should_exit = True
        await asyncio.gather(local_task, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="正式啟動轉發站（HTTPS + 本機後台）")
    parser.add_argument("--cert-check-seconds", type=float, default=3600, help="多久檢查一次憑證檔有沒有更新")
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.cert_check_seconds))
    except ConfigError as exc:
        raise SystemExit(f"設定檔錯誤：{exc}") from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
