"""使用者網頁：網頁檔案與安全標頭、給網頁用的 API、前端程式的安全規則。"""

import re

import pytest

from app.admin import ADMIN_STATIC
from app.main import STATIC_DIR
from tests.conftest import RECEIVE, USDC, USDT, UPSTREAM_SECRET, auth


async def test_index_is_served_with_security_headers(h):
    r = await h.client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "'unsafe-inline'" not in csp  # 允許 inline script 的話，CSP 擋 XSS 的效果就沒了
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"

    js = await h.client.get("/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "content-security-policy" in js.headers


async def test_api_routes_are_not_shadowed_by_web_files(h):
    """網頁掛在 / 底下，不能蓋掉 API，錯誤格式也要維持原樣。"""
    assert (await h.client.get("/health")).json() == {"ok": True}
    r = await h.client.get("/v1/models")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"  # 工具看得懂的 OpenAI 錯誤格式
    assert (await h.client.get("/api/me")).status_code == 401
    docs = await h.client.get("/docs")
    assert docs.status_code == 200
    assert "content-security-policy" not in docs.headers  # /docs 用 inline script，不能套網頁的 CSP


async def test_web_files_cannot_reach_outside_static_dir(h):
    for path in ("/%2e%2e/config.py", "/..%2fconfig.py", "/missing.html"):
        r = await h.client.get(path)
        assert r.status_code == 404, path
        assert "ConfigError" not in r.text


async def test_model_prices_require_login_and_hide_upstream(h):
    assert (await h.client.get("/api/models")).status_code == 401
    await h.add_model("text-only", input_price=300_000, output_price=1_200_000, max_output_tokens=8192,
                      cached_input_price=30_000)
    await h.add_model("vision", image_tokens=1000)
    await h.add_model("painter", kind="image", input_price=None, output_price=None, image_price=40_000)
    session_token = await h.register_login()
    r = await h.client.get("/api/models", headers=auth(session_token))
    assert r.status_code == 200
    assert r.json() == [
        # 圖片模型只有每張價格，其他價格欄位是 null（網頁顯示「—」）
        {"name": "painter", "kind": "image", "input_price_usd": None, "cached_input_price_usd": None,
         "output_price_usd": None, "image_price_usd": "0.040000", "max_output_tokens": 100,
         "accepts_images": False},
        {"name": "text-only", "kind": "chat", "input_price_usd": "0.300000", "cached_input_price_usd": "0.030000",
         "output_price_usd": "1.200000", "image_price_usd": None, "max_output_tokens": 8192,
         "accepts_images": False},
        # 沒設定快取價 → 顯示跟輸入價一樣（快取不打折）
        {"name": "vision", "kind": "chat", "input_price_usd": "1.000000", "cached_input_price_usd": "1.000000",
         "output_price_usd": "2.000000", "image_price_usd": None, "max_output_tokens": 100,
         "accepts_images": True},
    ]
    # 上游的 key、網址、真實模型名稱都不能出現在給使用者看的資料裡
    for secret in (UPSTREAM_SECRET, "http://upstream", "up-text-only", "fake"):
        assert secret not in r.text


async def test_deposit_options(h):
    assert (await h.client.get("/api/deposits/options")).status_code == 401
    session_token = await h.register_login()
    r = await h.client.get("/api/deposits/options", headers=auth(session_token))
    assert r.status_code == 200
    assert r.json() == {
        "enabled": True, "chain": "bsc-test", "to_address": RECEIVE,
        "tokens": [{"symbol": "USDT", "contract": USDT}, {"symbol": "USDC", "contract": USDC}],
        "min_usd": 1, "max_usd": 1000, "ttl_minutes": 30, "confirmations": 3,
    }

    h.app.state.settings.chain_rpc_url = ""  # 沒設定 RPC = 儲值關閉，網頁要能顯示「未開啟」
    r = await h.client.get("/api/deposits/options", headers=auth(session_token))
    assert r.json()["enabled"] is False


@pytest.mark.parametrize("page, count", [(STATIC_DIR / "index.html", 3), (ADMIN_STATIC / "admin.html", 2)])
def test_external_files_are_pinned_with_integrity(page, count):
    """CDN 上的檔案被竄改時，integrity 讓瀏覽器拒絕執行；沒鎖版本的話 integrity 也會隨更新失效。"""
    html = page.read_text(encoding="utf-8")
    tags = re.findall(r"<(?:script|link)\b[^>]*\bhttps://[^>]*>", html)
    assert len(tags) == count
    for tag in tags:
        assert re.search(r'integrity="sha(384|512)-[A-Za-z0-9+/=]{40,}"', tag), tag
        assert re.search(r"@\d+\.\d+\.\d+/|/\d+\.\d+\.\d+/", tag), f"沒有鎖定版本：{tag}"


@pytest.mark.parametrize("js_file, html_file", [(STATIC_DIR / "app.js", STATIC_DIR / "index.html"),
                                               (ADMIN_STATIC / "admin.js", ADMIN_STATIC / "admin.html")])
def test_frontend_never_injects_html(js_file, html_file):
    """伺服器回來的資料一律當純文字放進頁面；用 innerHTML 這類寫法就可能被塞入惡意程式（XSS）。
    後台更要小心：使用者自己填的 email 會顯示在站長的瀏覽器裡。"""
    js = js_file.read_text(encoding="utf-8")
    code = "\n".join(line for line in js.splitlines() if not line.strip().startswith("//"))
    for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert risky not in code, risky
    html = html_file.read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "不能有 inline script（CSP 會擋，也代表寫法不安全）"
    assert not re.search(r"\son[a-z]+\s*=", html), "不能用 onclick= 這類 inline 事件"
