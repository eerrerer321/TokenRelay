"""帳號：註冊門檻、登入、API key 生命週期、越權存取。"""

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
