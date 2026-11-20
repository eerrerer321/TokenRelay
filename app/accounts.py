"""帳號 API：註冊、登入、API key 管理、用量與帳本查詢。

網頁登入用 sess- 開頭的 token；轉發 API 用 sk- 開頭的 key。兩者分開，
key 外洩頂多被拿去用額度，不能拿來登入帳號、建新 key 或看紀錄。
"""

import hmac
import math
import re
from datetime import datetime, timedelta

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.billing import format_usd
from app.db import utcnow
from app.models import ApiKey, Ledger, PasswordReset, UsageLog, User, WebSession
from app.security import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    new_api_key,
    new_session_token,
    sha256_hex,
    verify_password,
)

router = APIRouter(prefix="/api", tags=["帳號"])
_bearer = HTTPBearer(auto_error=False, description="登入後拿到的 sess- token")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def iso_utc(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") + "Z" if value else None


def client_ip(request: Request) -> str:
    # 放在 Caddy／nginx 後面時，uvicorn 預設只相信本機代理送來的 X-Forwarded-For，這裡拿到的就是真實 IP
    return request.client.host if request.client else "unknown"


def check_not_locked(request: Request, *keys: str) -> None:
    wait = request.app.state.login_guard.retry_after(*keys)
    if wait:
        raise HTTPException(429, f"失敗太多次，請 {math.ceil(wait / 60)} 分鐘後再試",
                            headers={"Retry-After": str(wait)})


async def current_user(request: Request, cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> User:
    token = cred.credentials if cred else ""
    if not token.startswith("sess-"):
        raise HTTPException(401, "請先登入（Authorization: Bearer sess-...）")
    async with request.app.state.sessionmaker() as session:
        user = (
            await session.execute(
                select(User)
                .join(WebSession, WebSession.user_id == User.id)
                .where(WebSession.token_hash == sha256_hex(token), WebSession.expires_at > utcnow())
            )
        ).scalar_one_or_none()
    if user is None:
        raise HTTPException(401, "登入已失效，請重新登入")
    return user


class RegisterIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=10, max_length=128)
    invite_code: str = Field(max_length=128)


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


class KeyCreateIn(BaseModel):
    name: str = Field(default="default", min_length=1, max_length=64)


@router.post("/register", status_code=201)
async def register(body: RegisterIn, request: Request):
    settings = request.app.state.settings
    ip_key = f"ip:{client_ip(request)}"
    check_not_locked(request, ip_key)  # 擋一直猜邀請碼
    if not settings.invite_code or not hmac.compare_digest(body.invite_code.encode(), settings.invite_code.encode()):
        request.app.state.login_guard.record_failure(ip_key)
        raise HTTPException(403, "邀請碼錯誤，或註冊未開放")
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "email 格式錯誤")
    password_hash = await anyio.to_thread.run_sync(hash_password, body.password)
    try:
        async with request.app.state.sessionmaker.begin() as session:
            user = User(email=email, password_hash=password_hash)
            session.add(user)
            await session.flush()
            user_id = user.id
    except IntegrityError:
        raise HTTPException(409, "這個 email 已經註冊過") from None
    return {"id": user_id, "email": email}


@router.post("/login")
async def login(body: LoginIn, request: Request):
    sm = request.app.state.sessionmaker
    guard = request.app.state.login_guard
    email = body.email.strip().lower()
    keys = (f"ip:{client_ip(request)}", f"email:{email}")
    # 不管帳號存不存在都一樣計數，回應才不會洩漏「這個 email 有沒有註冊」
    check_not_locked(request, *keys)
    async with sm() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    stored = user.password_hash if user else DUMMY_PASSWORD_HASH
    ok = await anyio.to_thread.run_sync(verify_password, body.password, stored)
    if user is None or not ok:
        guard.record_failure(*keys)
        raise HTTPException(401, "email 或密碼錯誤")
    guard.reset(keys[1])  # 本人登入成功，之前打錯的次數不再累計；IP 的不清（攻擊者可以用自己的帳號登入來洗掉）
    token = new_session_token()
    expires_at = utcnow() + timedelta(hours=request.app.state.settings.session_ttl_hours)
    async with sm.begin() as session:
        session.add(WebSession(user_id=user.id, token_hash=sha256_hex(token), expires_at=expires_at))
    return {"token": token, "expires_at": iso_utc(expires_at)}


class PasswordResetIn(BaseModel):
    token: str = Field(max_length=128)
    password: str = Field(min_length=10, max_length=128)


@router.post("/password-reset", status_code=204)
async def reset_password(body: PasswordResetIn, request: Request):
    """用站長給的一次性連結設定新密碼。成功後這個帳號所有登入狀態都失效（被盜用的也一起踢掉）。"""
    guard = request.app.state.login_guard
    ip_key = f"ip:{client_ip(request)}"
    check_not_locked(request, ip_key)  # 擋一直猜連結
    password_hash = await anyio.to_thread.run_sync(hash_password, body.password)
    now = utcnow()
    async with request.app.state.sessionmaker.begin() as session:
        row = (await session.execute(
            select(PasswordReset.id, PasswordReset.user_id)
            .where(PasswordReset.token_hash == sha256_hex(body.token), PasswordReset.used_at.is_(None),
                   PasswordReset.expires_at > now)
        )).first()
        # 「還沒用過才標記」是同一句 SQL：同一個連結同時送兩次，只有一次會成功
        used = row and (await session.execute(
            update(PasswordReset).where(PasswordReset.id == row.id, PasswordReset.used_at.is_(None))
            .values(used_at=now)
        )).rowcount == 1
        if not used:
            guard.record_failure(ip_key)
            raise HTTPException(400, "連結無效、已經用過或已過期，請跟站長要一個新的")
        await session.execute(update(User).where(User.id == row.user_id).values(password_hash=password_hash))
        await session.execute(delete(WebSession).where(WebSession.user_id == row.user_id))
        email = (await session.execute(select(User.email).where(User.id == row.user_id))).scalar_one()
    guard.reset(f"email:{email}")  # 被別人故意打錯而鎖住的帳號，重設後就能馬上登入


@router.post("/logout", status_code=204)
async def logout(request: Request, cred: HTTPAuthorizationCredentials | None = Depends(_bearer)):
    if cred:
        async with request.app.state.sessionmaker.begin() as session:
            await session.execute(delete(WebSession).where(WebSession.token_hash == sha256_hex(cred.credentials)))


@router.get("/me")
async def me(user: User = Depends(current_user)):
    return {"id": user.id, "email": user.email, "balance_usd": format_usd(user.balance_micro),
            "balance_micro": user.balance_micro}


@router.get("/keys")
async def list_keys(request: Request, user: User = Depends(current_user)):
    async with request.app.state.sessionmaker() as session:
        rows = (await session.execute(
            select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.id)
        )).scalars().all()
    return [
        {"id": k.id, "name": k.name, "key_prefix": k.key_prefix + "…", "is_active": k.is_active,
         "created_at": iso_utc(k.created_at)}
        for k in rows
    ]


@router.post("/keys", status_code=201)
async def create_key(body: KeyCreateIn, request: Request, user: User = Depends(current_user)):
    limit = request.app.state.settings.max_keys_per_user
    key = new_api_key()
    async with request.app.state.sessionmaker.begin() as session:
        # 先鎖住這個使用者：同一人同時建 key 的請求會排隊，「先數再新增」才不會一起超過上限
        await session.execute(select(User.id).where(User.id == user.id).with_for_update())
        active = (await session.execute(
            select(func.count()).select_from(ApiKey).where(ApiKey.user_id == user.id, ApiKey.is_active.is_(True))
        )).scalar_one()
        if active >= limit:
            raise HTTPException(400, f"啟用中的 key 最多 {limit} 把，請先停用不用的")
        row = ApiKey(user_id=user.id, name=body.name, key_prefix=key[:10], key_hash=sha256_hex(key))
        session.add(row)
        await session.flush()
        key_id = row.id
    return {"id": key_id, "name": body.name, "key": key, "note": "這是唯一一次顯示完整 key，請立刻保存"}


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_key(key_id: int, request: Request, user: User = Depends(current_user)):
    async with request.app.state.sessionmaker.begin() as session:
        # 條件一定要包含 user_id：只靠 key_id 的話，任何人都能停用別人的 key（IDOR 漏洞）
        result = await session.execute(
            update(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id).values(is_active=False)
        )
    if result.rowcount != 1:
        raise HTTPException(404, "找不到這把 key")


@router.get("/models")
async def list_model_prices(request: Request, _user: User = Depends(current_user)):
    """給網頁看的模型與價格。/v1/models 是給工具用的 OpenAI 格式，沒有價格欄位。"""
    models = request.app.state.settings.open_models()

    def usd(micro):  # 對話以外的模型只有部分價格（例如圖片只有每張價格），沒有的回 null
        return None if micro is None else format_usd(micro)

    return [
        {"name": name, "kind": m.kind, "input_price_usd": usd(m.input_price_micro),
         # 沒設定快取價 = 快取命中也照輸入價收；只有對話模型有快取
         "cached_input_price_usd": usd(None if m.kind != "chat" else m.input_price_micro
                                       if m.cached_input_price_micro is None else m.cached_input_price_micro),
         "output_price_usd": usd(m.output_price_micro if m.kind == "chat" else None),
         "image_price_usd": usd(m.image_price_micro if m.kind == "image" else None),
         "max_output_tokens": m.max_output_tokens, "accepts_images": m.kind == "chat" and m.image_tokens > 0}
        for name, m in sorted(models.items())
    ]


@router.get("/usage")
async def list_usage(request: Request, limit: int = 50, user: User = Depends(current_user)):
    limit = max(1, min(limit, 200))
    async with request.app.state.sessionmaker() as session:
        rows = (await session.execute(
            select(UsageLog).where(UsageLog.user_id == user.id).order_by(UsageLog.id.desc()).limit(limit)
        )).scalars().all()
    return [
        {"id": u.id, "model": u.model, "stream": u.stream, "status": u.status,
         "prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
         "hold_usd": format_usd(u.hold_micro), "cost_usd": format_usd(u.cost_micro),
         "created_at": iso_utc(u.created_at)}
        for u in rows
    ]


@router.get("/ledger")
async def list_ledger(request: Request, limit: int = 50, user: User = Depends(current_user)):
    limit = max(1, min(limit, 200))
    async with request.app.state.sessionmaker() as session:
        rows = (await session.execute(
            select(Ledger).where(Ledger.user_id == user.id).order_by(Ledger.id.desc()).limit(limit)
        )).scalars().all()
    return [
        {"id": e.id, "amount_usd": format_usd(e.amount_micro), "kind": e.kind, "ref": e.ref,
         "created_at": iso_utc(e.created_at)}
        for e in rows
    ]
