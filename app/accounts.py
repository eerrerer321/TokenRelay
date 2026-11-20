"""帳號 API：註冊、登入、API key 管理、用量與帳本查詢。

網頁登入用 sess- 開頭的 token；轉發 API 用 sk- 開頭的 key。兩者分開，
key 外洩頂多被拿去用額度，不能拿來登入帳號、建新 key 或看紀錄。
"""

import hmac
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
from app.models import ApiKey, Ledger, UsageLog, User, WebSession
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
    if not settings.invite_code or not hmac.compare_digest(body.invite_code.encode(), settings.invite_code.encode()):
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
    async with sm() as session:
        user = (await session.execute(select(User).where(User.email == body.email.strip().lower()))).scalar_one_or_none()
    stored = user.password_hash if user else DUMMY_PASSWORD_HASH
    ok = await anyio.to_thread.run_sync(verify_password, body.password, stored)
    if user is None or not ok:
        raise HTTPException(401, "email 或密碼錯誤")
    token = new_session_token()
    expires_at = utcnow() + timedelta(hours=request.app.state.settings.session_ttl_hours)
    async with sm.begin() as session:
        session.add(WebSession(user_id=user.id, token_hash=sha256_hex(token), expires_at=expires_at))
    return {"token": token, "expires_at": iso_utc(expires_at)}


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
