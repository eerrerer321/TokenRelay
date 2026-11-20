"""站長後台：只限本機、改 config.ini（保留註解）並立即生效、錢包餘額、重設密碼連結。"""

from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import delete, select

from app.admin import _money, revenue_periods
from app.billing import format_usd
from app.config import ConfigError
from app.db import utcnow
from app.models import UsageLog
from app.config_edit import add_section, remove_section, set_values
from app.main import create_app
from tests.conftest import INVITE, RECEIVE, USDT, USDC, auth
from tests.fakes import UPSTREAM_SECRET, FakeChain, FakeUpstream

ADMIN = {"X-Admin-Request": "1"}

CONFIG = """# 開頭的註解
[site]
invite_code = {invite}
database_url = {db}
public_url = https://relay.example.com

[upstream:fake]
base_url = http://upstream/v1
api_key = {key}

# ==== 模型（這段註解要原樣保留）
[model:gpt-test]
upstream = fake
# 賣價
input_price = 1
output_price = 2
max_output_tokens = 100

# ==== 鏈上儲值
[chain]
name = bsc-test
rpc_url = http://rpc.test
receive_address = {receive}
confirmations = 3
start_block = 1

[token:USDT]
contract = {usdt}

[token:USDC]
contract = {usdc}
decimals = 6
"""


class AdminHarness:
    def __init__(self, app, client, upstream, chain, path):
        self.app, self.client, self.upstream, self.chain, self.path = app, client, upstream, chain, path

    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    async def put_model(self, name, **values):
        return await self.client.put(f"/admin/api/models/{name}", json={"values": values}, headers=ADMIN)


@pytest.fixture
async def ah(tmp_path):
    upstream, chain = FakeUpstream(), FakeChain()
    path = tmp_path / "config.ini"
    text = CONFIG.format(invite=INVITE, db=f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}",
                         key=UPSTREAM_SECRET, receive=RECEIVE, usdt=USDT, usdc=USDC)
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))  # Windows 記事本存的檔案是 \r\n 換行
    app = create_app(config_path=path, upstream_transport=upstream.transport,
                     rpc_transport=httpx.MockTransport(chain.handle), start_watcher=False)
    async with app.router.lifespan_context(app):
        # base_url 決定 Host 標頭：從本機瀏覽器打開後台時就是 localhost
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000") as c:
            yield AdminHarness(app, c, upstream, chain, path)


# ---------------------------------------------------------------- 只限本機


async def test_admin_page_is_served_locally_with_security_headers(ah):
    r = await ah.client.get("/admin")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert (await ah.client.get("/admin/admin.js")).status_code == 200
    assert (await ah.client.get("/admin/..%2fconfig.py")).status_code == 404


async def remote_client(app, *, client=("127.0.0.1", 5000), base_url="http://localhost:8000"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=client), base_url=base_url)


@pytest.mark.parametrize("client, base_url, headers", [
    (("203.0.113.9", 5000), "http://localhost:8000", {}),  # 從網路連進來
    (("192.168.1.20", 5000), "http://localhost:8000", {}),  # 從區網的其他電腦
    (("127.0.0.1", 5000), "http://relay.example.com", {}),  # Host 不是 localhost（DNS rebinding、或經過代理）
    (("127.0.0.1", 5000), "http://localhost:8000", {"X-Forwarded-For": "203.0.113.9"}),  # 經過本機的反向代理
    (("127.0.0.1", 5000), "http://localhost:8000", {"CF-Connecting-IP": "203.0.113.9"}),
], ids=["internet", "lan", "foreign-host", "proxied", "cloudflare"])
async def test_admin_is_invisible_from_anywhere_but_this_computer(ah, client, base_url, headers):
    async with await remote_client(ah.app, client=client, base_url=base_url) as other:
        for path in ("/admin", "/admin/admin.js", "/admin/api/overview", "/admin/api/users"):
            assert (await other.get(path, headers=headers)).status_code == 404, path
        r = await other.put("/admin/api/models/gpt-test", json={"values": {"input_price": "0"}},
                            headers={**headers, **ADMIN})
        assert r.status_code == 404
    assert "input_price = 1" in ah.text()


async def test_changes_require_custom_header_against_csrf(ah):
    """別的網站可以讓你的瀏覽器送 POST 到 localhost，但沒辦法加自訂標頭（瀏覽器會先問、後台不會答應）。"""
    r = await ah.client.put("/admin/api/models/gpt-test", json={"values": {"input_price": "0"}})
    assert r.status_code == 403
    r = await ah.client.post("/admin/api/users/1/credit", json={"usd": "100"})
    assert r.status_code == 403
    assert "input_price = 1" in ah.text()


# ---------------------------------------------------------------- 改模型：寫回 config.ini、立即生效


async def setup_friend(ah, credit_usd="1"):
    c = ah.client
    assert (await c.post("/api/register", json={"email": "f@example.com", "password": "friend-pass-1",
                                                "invite_code": INVITE})).status_code == 201
    token = (await c.post("/api/login", json={"email": "f@example.com", "password": "friend-pass-1"})).json()["token"]
    key = (await c.post("/api/keys", json={"name": "k"}, headers=auth(token))).json()["key"]
    user_id = (await c.get("/admin/api/users")).json()[0]["id"]
    assert (await c.post(f"/admin/api/users/{user_id}/credit", json={"usd": credit_usd}, headers=ADMIN)).status_code == 200
    return token, key, user_id


async def chat(ah, key, model="gpt-test"):
    return await ah.client.post("/v1/chat/completions", headers=auth(key),
                                json={"model": model, "messages": [{"role": "user", "content": "hi"}]})


async def test_price_change_is_saved_with_comments_and_applies_immediately(ah):
    token, key, _ = await setup_friend(ah)
    r = await ah.put_model("gpt-test", input_price="3", output_price="4", cached_input_price="0.3",
                           cost_input_price="1.5")
    assert r.status_code == 200, r.text
    assert r.json()["input_price"] == "3.000000" and r.json()["cost_input_price"] == "1.500000"

    text = ah.text()
    assert "input_price = 3\n" in text and "output_price = 4\n" in text
    assert "cached_input_price = 0.3\n" in text and "cost_input_price = 1.5\n" in text
    for comment in ("# 開頭的註解", "# ==== 模型（這段註解要原樣保留）", "# 賣價", "# ==== 鏈上儲值"):
        assert comment in text
    assert "input_price = 1" in (ah.path.with_name("config.ini.bak")).read_text(encoding="utf-8")
    # 只有改到的行不一樣，其他每一行（含換行字元）都跟原本一模一樣
    old_lines = ah.path.with_name("config.ini.bak").read_bytes().split(b"\r\n")
    new_lines = ah.path.read_bytes().split(b"\r\n")
    assert b"\n" not in b"".join(new_lines)  # 沒有混進只有 \n 的換行
    changed = [line for line in new_lines if line not in old_lines]
    assert sorted(changed) == sorted([b"input_price = 3", b"output_price = 4", b"cached_input_price = 0.3",
                                      b"cost_input_price = 1.5"])

    # 不用重啟：網頁上的價格和實際收費都已經是新價格（假上游回 10 輸入 + 20 輸出 tokens）
    prices = (await ah.client.get("/api/models", headers=auth(token))).json()
    assert prices[0]["input_price_usd"] == "3.000000"
    await chat(ah, key)
    usage = (await ah.client.get("/api/usage", headers=auth(token))).json()[0]
    assert usage["cost_usd"] == "0.000110"  # 10 × 3 + 20 × 4 = 110 micro


async def test_disabled_model_disappears_for_users(ah):
    token, key, _ = await setup_friend(ah)
    assert (await ah.put_model("gpt-test", enabled="false")).status_code == 200
    assert "enabled = false" in ah.text()
    assert (await ah.client.get("/v1/models", headers=auth(key))).json()["data"] == []
    assert (await ah.client.get("/api/models", headers=auth(token))).json() == []
    r = await chat(ah, key)
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"
    # 後台還看得到，可以再打開
    assert (await ah.client.get("/admin/api/models")).json()["models"][0]["enabled"] is False
    assert (await ah.put_model("gpt-test", enabled="true")).status_code == 200
    assert (await chat(ah, key)).status_code == 200


@pytest.mark.parametrize("values, message", [
    ({"input_price": "abc"}, "input_price：不是數字"),
    ({"cached_input_price": "5"}, "cached_input_price：不可高於 input_price"),
    ({"enabled": "maybe"}, "enabled：只能是 true 或 false"),
    ({"upstream": "nope"}, "上游 nope 不存在"),
    ({"input_price": "1\n[site]\ninvite_code = hacked"}, "不能包含換行"),  # 想偷塞新區段
    ({"api_key": "x"}, "不認得的名稱"),
])
async def test_bad_values_are_rejected_and_file_is_untouched(ah, values, message):
    before = ah.text()
    r = await ah.put_model("gpt-test", **values)
    assert r.status_code == 400 and message in r.json()["detail"], r.text
    assert ah.text() == before
    assert ah.app.state.settings.models["gpt-test"].input_price_micro == 1_000_000


async def test_create_and_delete_model(ah):
    r = await ah.client.post("/admin/api/models/vendor/big-model", headers=ADMIN,
                             json={"values": {"upstream": "fake", "input_price": "3.6", "output_price": "18"}})
    assert r.status_code == 201, r.text
    text = ah.text()
    # 新區段放在最後一個模型後面、鏈的註解前面
    assert text.index("[model:vendor/big-model]") < text.index("# ==== 鏈上儲值")
    names = [m["name"] for m in (await ah.client.get("/admin/api/models")).json()["models"]]
    assert names == ["gpt-test", "vendor/big-model"]
    assert ah.app.state.settings.models["vendor/big-model"].upstream_model == "vendor/big-model"

    bad = await ah.client.post("/admin/api/models/bad]name", headers=ADMIN, json={"values": {"upstream": "fake"}})
    assert bad.status_code == 400

    assert (await ah.client.delete("/admin/api/models/vendor/big-model", headers=ADMIN)).status_code == 204
    assert "big-model" not in ah.text() and "# ==== 鏈上儲值" in ah.text()
    assert "vendor/big-model" not in ah.app.state.settings.models


async def test_switching_model_kind_clears_unused_prices(ah):
    """後台把對話模型改成圖片模型時，網頁會把用不到的欄位送空字串（= 用預設值）。"""
    r = await ah.put_model("gpt-test", kind="image", image_price="0.04", input_price="", output_price="",
                           max_output_tokens="")
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "image" and r.json()["image_price"] == "0.040000"
    model = ah.app.state.settings.models["gpt-test"]
    assert (model.kind, model.input_price_micro, model.output_price_micro) == ("image", None, None)
    # 圖片模型沒填每張價格 → 拒絕，檔案不動
    before = ah.text()
    r = await ah.put_model("gpt-test", image_price="")
    assert r.status_code == 400 and "image_price：必填" in r.json()["detail"]
    assert ah.text() == before


async def test_upstream_model_list_converts_prices_per_million(ah):
    r = await ah.client.get("/admin/api/upstreams/fake/models")
    assert r.status_code == 200
    assert r.json() == [
        {"id": "plain-model", "cost_input_price": None, "cost_output_price": None, "cost_cached_input_price": None},
        {"id": "vendor/big-model", "cost_input_price": "3.000000", "cost_output_price": "15.000000",
         "cost_cached_input_price": "0.300000"},
    ]
    assert ah.upstream.requests[-1]["headers"]["authorization"] == f"Bearer {UPSTREAM_SECRET}"


# ---------------------------------------------------------------- 收益試算


async def usage_rows(ah) -> list[UsageLog]:
    async with ah.app.state.sessionmaker() as s:
        return list((await s.execute(select(UsageLog).order_by(UsageLog.id))).scalars())


async def test_revenue_splits_reported_estimated_and_unknown_upstream_cost(ah):
    """假上游回 10 輸入 + 20 輸出 tokens；售價 1、2 美元 / 100 萬 tokens → 每次向使用者收 50 micro。"""
    token, key, _ = await setup_friend(ah)
    # 1. 上游有回報花費（opencode：額度內實際花費、用掉的方案額度）→ 用回報值
    ah.upstream.usage_extra = {"cost": 0.00003, "opencode": {"allowance": 0.00004}}
    assert (await chat(ah, key)).status_code == 200
    # 2. 沒回報、模型也沒填進價 → 不知道
    ah.upstream.usage_extra = {}
    assert (await chat(ah, key)).status_code == 200
    # 3. 沒回報、有填進價 → 用進價估算（含快取命中）：6 × 0.5 + 4 × 0.1 + 20 × 1 = 23.4 → 進位 24
    assert (await ah.put_model("gpt-test", cost_input_price="0.5", cost_output_price="1",
                               cost_cached_input_price="0.1")).status_code == 200
    ah.upstream.usage_extra = {"prompt_tokens_details": {"cached_tokens": 4}}
    assert (await chat(ah, key)).status_code == 200
    # 4. 上游沒給用量 → 收全額預扣，上游花多少不知道（不能當成全賺）
    ah.upstream.mode = "no_usage"
    assert (await chat(ah, key)).status_code == 200
    # 5. 上游回錯誤 → 全額退款：沒收錢，所以不算「不知道上游花多少」
    ah.upstream.mode = "error500"
    assert (await chat(ah, key)).status_code == 502

    rows = await usage_rows(ah)
    assert [(r.upstream, r.upstream_cost_micro, r.upstream_cost_estimated, r.upstream_allowance_micro)
            for r in rows] == [("fake", 30, False, 40), ("fake", None, None, None), ("fake", 24, True, None),
                               ("fake", None, None, None), ("fake", None, None, None)]
    assert rows[2].cached_tokens == 4
    hold = rows[3].cost_micro

    periods = (await ah.client.get("/admin/api/revenue")).json()["periods"]
    assert [p["code"] for p in periods] == ["today", "7d", "30d"]
    for period in periods:
        assert period["rows"] == [{
            "upstream": "fake", "model": "gpt-test", "requests": 5, "revenue": format_usd(150 + hold),
            "upstream_cost": format_usd(54), "allowance": format_usd(40), "estimated": 1, "unknown": 2,
            "unknown_revenue": format_usd(50 + hold), "profit": format_usd(150 - 50 - 54),
        }]
        assert period["total"]["profit"] == format_usd(46)


async def test_revenue_periods(ah):
    # 台灣時間 11/20 23:30 → 今天從台灣 11/20 00:00（= UTC 11/19 16:00）算起；過了台灣午夜就換一天
    assert revenue_periods(datetime(2026, 11, 20, 15, 30))[0][2] == datetime(2026, 11, 19, 16, 0)
    assert revenue_periods(datetime(2026, 11, 20, 16, 30))[0][2] == datetime(2026, 11, 20, 16, 0)

    _, key, user_id = await setup_friend(ah)
    assert (await chat(ah, key)).status_code == 200
    async with ah.app.state.sessionmaker.begin() as s:  # 8 天前的一筆：只算進 30 天
        key_id = (await s.execute(select(UsageLog.api_key_id))).scalar_one()
        s.add(UsageLog(user_id=user_id, api_key_id=key_id, model="gpt-test", upstream="fake", stream=False,
                       hold_micro=1000, cost_micro=1000, upstream_cost_micro=100, upstream_cost_estimated=False,
                       status="ok", created_at=utcnow() - timedelta(days=8)))
        s.add(UsageLog(user_id=user_id, api_key_id=key_id, model="gpt-test", upstream="fake", stream=False,
                       hold_micro=1000, status="pending"))  # 還沒結算的不算
    totals = {p["code"]: p["total"] for p in (await ah.client.get("/admin/api/revenue")).json()["periods"]}
    assert totals["7d"]["requests"] == 1 and totals["30d"]["requests"] == 2
    assert totals["30d"]["revenue"] == format_usd(1050)
    async with ah.app.state.sessionmaker.begin() as s:  # 讓 conftest 以外的結尾檢查也乾淨：把假的 pending 刪掉
        await s.execute(delete(UsageLog).where(UsageLog.status == "pending"))


def test_negative_profit_is_shown_with_minus_sign():
    row = {"requests": 1, "revenue": 10, "upstream_cost": 30, "allowance": 0, "estimated": 1, "unknown": 0,
           "unknown_revenue": 0}
    assert _money(row)["profit"] == "-0.000020"


# ---------------------------------------------------------------- 錢包與使用者


async def test_overview_shows_wallet_balances(ah):
    ah.chain.native_balance = 5 * 10**15  # 0.005 BNB
    ah.chain.token_balances = {USDT: 12_500_000_000_000_000_000, USDC: 7_250_000}  # USDT 18 位、USDC 6 位小數
    await setup_friend(ah, credit_usd="2.5")
    data = (await ah.client.get("/admin/api/overview")).json()
    assert data["wallet"] == {"BNB": "0.005", "USDT": "12.5", "USDC": "7.25"}
    assert data["users"] == 1 and data["owed_usd"] == "2.500000"
    assert data["receive_address"] == RECEIVE


async def test_reset_link_sets_new_password_once_and_logs_out_everywhere(ah):
    old_token, _, user_id = await setup_friend(ah)
    r = await ah.client.post(f"/admin/api/users/{user_id}/reset-link", headers=ADMIN)
    link = r.json()["link"]
    assert link.startswith("https://relay.example.com/#reset/")
    reset_token = link.rsplit("/", 1)[1]

    body = {"token": reset_token, "password": "brand-new-pass-1"}
    assert (await ah.client.post("/api/password-reset", json=body)).status_code == 204
    assert (await ah.client.get("/api/me", headers=auth(old_token))).status_code == 401  # 舊的登入全部失效
    login = {"email": "f@example.com"}
    assert (await ah.client.post("/api/login", json={**login, "password": "friend-pass-1"})).status_code == 401
    assert (await ah.client.post("/api/login", json={**login, "password": "brand-new-pass-1"})).status_code == 200
    # 同一個連結不能用第二次
    again = await ah.client.post("/api/password-reset", json={**body, "password": "another-pass-1"})
    assert again.status_code == 400


async def test_reset_link_expires(ah, monkeypatch):
    _, _, user_id = await setup_friend(ah)
    token = (await ah.client.post(f"/admin/api/users/{user_id}/reset-link", headers=ADMIN)).json()["link"]
    from datetime import timedelta

    from app import accounts, db
    later = db.utcnow() + timedelta(minutes=31)
    monkeypatch.setattr(accounts, "utcnow", lambda: later)
    r = await ah.client.post("/api/password-reset", json={"token": token.rsplit("/", 1)[1], "password": "x" * 12})
    assert r.status_code == 400


async def test_guessing_reset_tokens_is_locked(ah):
    for _ in range(5):
        r = await ah.client.post("/api/password-reset", json={"token": "guess", "password": "x" * 12})
        assert r.status_code == 400
    assert (await ah.client.post("/api/password-reset", json={"token": "guess", "password": "x" * 12})).status_code == 429


# ---------------------------------------------------------------- 只改指定的行（純文字處理）


def test_editor_keeps_everything_else_byte_for_byte():
    text = "# top\r\n[model:a]\r\nupstream = u\r\n# note\r\ninput_price = 1\r\n\r\n# next\r\n[chain]\r\nname = x\r\n"
    edited = set_values(text, "model:a", {"input_price": "2", "enabled": "false"})
    assert edited == ("# top\r\n[model:a]\r\nupstream = u\r\n# note\r\ninput_price = 2\r\nenabled = false\r\n"
                      "\r\n# next\r\n[chain]\r\nname = x\r\n")
    added = add_section(edited, "model:b", {"upstream": "u"})
    assert "enabled = false\r\n\r\n[model:b]\r\nupstream = u\r\n\r\n# next" in added
    assert remove_section(added, "model:b") == edited
    with pytest.raises(ConfigError):
        set_values(text, "model:missing", {"input_price": "1"})
    with pytest.raises(ConfigError):
        set_values(text, "model:a", {"input_price": "1\r\n[site]"})
