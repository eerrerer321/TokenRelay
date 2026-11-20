"""帳號：註冊門檻、登入、API key 生命週期、越權存取。"""

import httpx

from tests.conftest import INVITE, auth


async def test_register_requires_correct_invite_code(h):
    r = await h.client.post("/api/register", json={"email": "x@example.com", "password": "long-enough-pw",
                                                   "invite_code": "wrong"})
    assert r.status_code == 403

    h.app.state.settings.invite_code = ""  # 沒設定邀請碼 = 關閉註冊，連空字串都不能通過
    r = await h.client.post("/api/register", json={"email": "x@example.com", "password": "long-enough-pw",
                                                   "invite_code": ""})
    assert r.status_code == 403


async def test_register_validation_and_duplicate_email(h):
    r = await h.client.post("/api/register", json={"email": "x@example.com", "password": "short",
                                                   "invite_code": INVITE})
    assert r.status_code == 422
    await h.register_login("alice@example.com")
    r = await h.client.post("/api/register", json={"email": "ALICE@example.com", "password": "long-enough-pw",
                                                   "invite_code": INVITE})
    assert r.status_code == 409


async def test_login_wrong_password_and_unknown_user(h):
    await h.register_login("alice@example.com", "correct-horse-1")
    r = await h.client.post("/api/login", json={"email": "alice@example.com", "password": "wrong-password"})
    assert r.status_code == 401
    r = await h.client.post("/api/login", json={"email": "nobody@example.com", "password": "whatever-pw"})
    assert r.status_code == 401


async def test_api_key_and_session_token_are_not_interchangeable(h):
    await h.add_model()
    session_token, key = await h.user_with_key()
    assert (await h.client.get("/api/me", headers=auth(key))).status_code == 401
    assert (await h.client.get("/v1/models", headers=auth(session_token))).status_code == 401


async def test_key_list_never_shows_full_key(h):
    session_token, key = await h.user_with_key()
    r = await h.client.get("/api/keys", headers=auth(session_token))
    assert r.status_code == 200
    assert key not in r.text
    assert r.json()[0]["key_prefix"].startswith(key[:10])


async def test_revoked_key_is_rejected(h):
    await h.add_model()
    session_token, key = await h.user_with_key()
    assert (await h.client.get("/v1/models", headers=auth(key))).status_code == 200
    key_id = (await h.client.get("/api/keys", headers=auth(session_token))).json()[0]["id"]
    assert (await h.client.delete(f"/api/keys/{key_id}", headers=auth(session_token))).status_code == 204
    assert (await h.client.get("/v1/models", headers=auth(key))).status_code == 401


async def test_cannot_revoke_someone_elses_key(h):
    await h.add_model()
    alice_session, alice_key = await h.user_with_key("alice@example.com")
    bob_session = await h.register_login("bob@example.com")
    alice_key_id = (await h.client.get("/api/keys", headers=auth(alice_session))).json()[0]["id"]

    r = await h.client.delete(f"/api/keys/{alice_key_id}", headers=auth(bob_session))
    assert r.status_code == 404
    assert (await h.client.get("/v1/models", headers=auth(alice_key))).status_code == 200


async def test_logout_invalidates_session(h):
    session_token = await h.register_login()
    assert (await h.client.get("/api/me", headers=auth(session_token))).status_code == 200
    assert (await h.client.post("/api/logout", headers=auth(session_token))).status_code == 204
    assert (await h.client.get("/api/me", headers=auth(session_token))).status_code == 401


# ---------------------------------------------------------------- 一直猜密碼／邀請碼


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def login(client, email, password):
    return await client.post("/api/login", json={"email": email, "password": password})


async def test_password_guessing_is_locked_then_released(h):
    clock = FakeClock()
    h.app.state.login_guard.clock = clock
    await h.register_login("alice@example.com", "correct-horse-1")
    for _ in range(5):
        assert (await login(h.client, "alice@example.com", "guess-guess")).status_code == 401
    # 第 6 次就算猜對也擋：不然攻擊者看得出哪次猜對了
    r = await login(h.client, "alice@example.com", "correct-horse-1")
    assert r.status_code == 429
    assert "分鐘後再試" in r.json()["detail"] and int(r.headers["retry-after"]) == 15 * 60
    # 不存在的帳號一樣被擋，回應不能洩漏「這個 email 有沒有註冊」
    assert (await login(h.client, "nobody@example.com", "whatever-pw")).status_code == 429

    clock.now += 15 * 60 + 1
    assert (await login(h.client, "alice@example.com", "correct-horse-1")).status_code == 200


async def test_guessing_one_account_from_many_ips_is_locked(h):
    await h.register_login("alice@example.com", "correct-horse-1")
    for i in range(5):
        transport = httpx.ASGITransport(app=h.app, client=(f"10.0.0.{i}", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as other:
            assert (await login(other, "alice@example.com", "guess-guess")).status_code == 401
    # 換了 5 個 IP，但都在猜同一個 email → email 也會被鎖
    assert (await login(h.client, "alice@example.com", "correct-horse-1")).status_code == 429
    # 這些 IP 沒猜過的帳號、測試主機的 IP 都不受影響
    await h.register_login("bob@example.com", "correct-horse-2")


async def test_successful_login_clears_own_typos(h):
    await h.register_login("alice@example.com", "correct-horse-1")
    for _ in range(4):
        await login(h.client, "alice@example.com", "typo-typo")
    assert (await login(h.client, "alice@example.com", "correct-horse-1")).status_code == 200
    # 成功登入後重新計數，再打錯一次不會馬上被鎖（email 的計數歸零）
    transport = httpx.ASGITransport(app=h.app, client=("10.9.9.9", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as other:
        assert (await login(other, "alice@example.com", "typo-typo")).status_code == 401
        assert (await login(other, "alice@example.com", "correct-horse-1")).status_code == 200


async def test_invite_code_guessing_is_locked(h):
    for _ in range(5):
        r = await h.client.post("/api/register", json={"email": "x@example.com", "password": "long-enough-pw",
                                                       "invite_code": "guess"})
        assert r.status_code == 403
    r = await h.client.post("/api/register", json={"email": "x@example.com", "password": "long-enough-pw",
                                                   "invite_code": INVITE})
    assert r.status_code == 429
