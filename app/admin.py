"""站長後台（/admin）：只限「在跑伺服器的這台電腦」用瀏覽器開，不用帳密。

不用帳密，所以「怎麼確認是本機」就是全部的防線，四道檢查缺一不可：
1. 連線來源是 127.0.0.1 / ::1：從網路連進來的請求一律不是
2. 沒有代理標頭（X-Forwarded-For 等）：之後如果在本機架反向代理，外面的請求經過代理後來源也會變成 127.0.0.1
3. Host 標頭是 localhost / 127.0.0.1：擋「DNS rebinding」——惡意網站把自己的網域指到 127.0.0.1，
   讓你的瀏覽器替它讀後台資料
4. 會改東西的請求必須帶自訂標頭 X-Admin-Request：別的網站沒辦法讓你的瀏覽器跨站送出這種請求（CSRF）
不符合的一律回 404，不承認後台存在。
"""

import secrets
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app import config_edit
from app.accounts import iso_utc
from app.billing import change_balance, format_usd, usd_to_micro
from app.config import _MODEL_KEYS, ConfigError, Settings
from app.db import utcnow
from app.models import PasswordReset, UsageLog, User
from app.security import WEB_SECURITY_HEADERS, sha256_hex

ADMIN_STATIC = Path(__file__).parent / "admin_static"
ADMIN_FILES = {"admin.html": "text/html", "admin.js": "text/javascript"}
LOCAL_CLIENTS = {"127.0.0.1", "::1"}
LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "[::1]"}
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "forwarded", "x-real-ip", "cf-connecting-ip")
RESET_TTL = timedelta(minutes=30)


def _hostname(host: str) -> str:
    if host.startswith("["):  # IPv6：[::1]:8000
        return host[: host.find("]") + 1]
    return host.split(":")[0].lower()


async def local_only(request: Request) -> None:
    client = request.client.host if request.client else ""
    if (client not in LOCAL_CLIENTS
            or any(name in request.headers for name in PROXY_HEADERS)
            or _hostname(request.headers.get("host", "")) not in LOCAL_HOSTNAMES):
        raise HTTPException(404, "Not Found")
    if request.method not in ("GET", "HEAD") and request.headers.get("x-admin-request") != "1":
        raise HTTPException(403, "缺少 X-Admin-Request 標頭")


router = APIRouter(prefix="/admin", tags=["後台（只限本機）"], dependencies=[Depends(local_only)],
                   include_in_schema=False)


# ---------------------------------------------------------------- 網頁檔案


@router.get("")
async def admin_page():
    return _file("admin.html")


@router.get("/{name}")
async def admin_file(name: str):
    if name not in ADMIN_FILES:  # 只給固定的幾個檔案，路徑怎麼寫都跳不出資料夾
        raise HTTPException(404, "Not Found")
    return _file(name)


def _file(name: str) -> FileResponse:
    return FileResponse(ADMIN_STATIC / name, media_type=ADMIN_FILES[name],
                        headers={**WEB_SECURITY_HEADERS, "Cache-Control": "no-store"})


# ---------------------------------------------------------------- 總覽


@router.get("/api/overview")
async def overview(request: Request):
    state = request.app.state
    since = utcnow() - timedelta(days=30)
    async with state.sessionmaker() as session:
        users, owed = (await session.execute(
            select(func.count(), func.coalesce(func.sum(User.balance_micro), 0)).select_from(User)
        )).one()
        revenue, requests = (await session.execute(
            select(func.coalesce(func.sum(UsageLog.cost_micro), 0), func.count())
            .where(UsageLog.created_at >= since, UsageLog.status != "pending")
        )).one()
    wallet, wallet_error = None, None
    if state.watcher is not None:
        try:
            wallet = await state.watcher.wallet_balances()
        except Exception as exc:  # RPC 掛掉不該讓整個後台打不開
            wallet_error = str(exc)[:300]
    settings: Settings = state.settings
    return {"users": users, "owed_usd": format_usd(owed), "revenue_30d_usd": format_usd(revenue),
            "requests_30d": requests, "chain": settings.chain_name, "receive_address": settings.receive_address,
            "wallet": wallet, "wallet_error": wallet_error}


# ---------------------------------------------------------------- 模型


def _price(micro: int | None) -> str | None:
    return None if micro is None else format_usd(micro)


def _model_out(m) -> dict:
    return {"name": m.name, "enabled": m.enabled, "kind": m.kind, "upstream": m.upstream,
            "upstream_model": m.upstream_model,
            "input_price": _price(m.input_price_micro), "output_price": _price(m.output_price_micro),
            "image_price": _price(m.image_price_micro),
            "cached_input_price": _price(m.cached_input_price_micro),
            "cost_input_price": _price(m.cost_input_price_micro),
            "cost_output_price": _price(m.cost_output_price_micro),
            "cost_cached_input_price": _price(m.cost_cached_input_price_micro),
            "max_output_tokens": m.max_output_tokens, "max_tokens_field": m.max_tokens_field,
            "image_tokens": m.image_tokens}


@router.get("/api/models")
async def list_models(request: Request):
    settings: Settings = request.app.state.settings
    return {"editable": request.app.state.config_path is not None,
            "upstreams": sorted(settings.upstreams),
            "models": [_model_out(m) for _, m in sorted(settings.models.items())]}


class ModelValues(BaseModel):
    # 名稱 → 值（跟 config.ini 寫法一樣的文字）。空字串 = 用預設值
    values: dict[str, str] = Field(default_factory=dict)


def _check_keys(values: dict[str, str]) -> None:
    unknown = set(values) - _MODEL_KEYS
    if unknown:
        raise HTTPException(400, f"不認得的名稱：{', '.join(sorted(unknown))}")


async def _edit_config(request: Request, edit) -> None:
    """在鎖裡讀檔 → 修改 → 檢查 → 寫回 → 套用新的模型與上游設定。"""
    state = request.app.state
    if state.config_path is None:
        raise HTTPException(409, "這個伺服器不是從 config.ini 啟動的，無法從後台修改")
    async with state.config_lock:
        try:
            text = edit(config_edit.read_config(state.config_path))
            new = config_edit.write_config(state.config_path, text)
        except ConfigError as exc:
            raise HTTPException(400, str(exc)) from None
        # 只即時套用模型與上游。收款地址、資料庫這類設定被手動改過的話，要重啟才生效：
        # 掃鏈程式還拿著舊設定，半套用會造成「網頁顯示新地址、卻在掃舊地址」
        state.settings = state.settings.model_copy(update={"models": new.models, "upstreams": new.upstreams})


@router.put("/api/models/{name:path}")
async def update_model(name: str, body: ModelValues, request: Request):
    _check_keys(body.values)
    if name not in request.app.state.settings.models:
        raise HTTPException(404, "找不到這個模型")
    await _edit_config(request, lambda text: config_edit.set_values(text, f"model:{name}", body.values))
    return _model_out(request.app.state.settings.models[name])


@router.post("/api/models/{name:path}", status_code=201)
async def create_model(name: str, body: ModelValues, request: Request):
    _check_keys(body.values)
    if not config_edit.MODEL_NAME_RE.match(name):
        raise HTTPException(400, "模型名稱只能用英數字和 . _ : / @ + -，最多 100 字")
    if name in request.app.state.settings.models:
        raise HTTPException(409, "這個模型名稱已經存在")
    await _edit_config(request, lambda text: config_edit.add_section(text, f"model:{name}", body.values))
    return _model_out(request.app.state.settings.models[name])


@router.delete("/api/models/{name:path}", status_code=204)
async def delete_model(name: str, request: Request):
    if name not in request.app.state.settings.models:
        raise HTTPException(404, "找不到這個模型")
    await _edit_config(request, lambda text: config_edit.remove_section(text, f"model:{name}"))


def _per_million(per_token) -> str | None:
    """OpenRouter 的價格是「每 token 幾美元」的文字 → 每 100 萬 tokens，無條件進位到 6 位小數。"""
    try:
        value = Decimal(str(per_token)) * 1_000_000
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    return f"{value.quantize(Decimal('0.000001'), rounding=ROUND_CEILING):f}"


@router.get("/api/upstreams/{name}/models")
async def upstream_models(name: str, request: Request):
    """問上游有哪些模型可以用。OpenRouter 會附上價格，可以直接當進價。"""
    state = request.app.state
    upstream = state.settings.upstreams.get(name)
    if upstream is None:
        raise HTTPException(404, "找不到這個上游")
    headers = {"Authorization": f"Bearer {upstream.api_key}"} if upstream.api_key else {}
    try:
        resp = await state.http.get(upstream.base_url.rstrip("/") + "/models", headers=headers, timeout=20)
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"問不到上游的模型清單：{type(exc).__name__}") from None
    items = data.get("data") if isinstance(data, dict) else None
    if resp.status_code >= 400 or not isinstance(items, list):
        raise HTTPException(502, f"上游回應 HTTP {resp.status_code}，格式也不是模型清單")
    result = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
        result.append({"id": item["id"][:200],
                       "cost_input_price": _per_million(pricing["prompt"]) if "prompt" in pricing else None,
                       "cost_output_price": _per_million(pricing["completion"]) if "completion" in pricing else None,
                       "cost_cached_input_price": (_per_million(pricing["input_cache_read"])
                                                   if "input_cache_read" in pricing else None)})
    return sorted(result, key=lambda x: x["id"])


# ---------------------------------------------------------------- 使用者


@router.get("/api/users")
async def list_users(request: Request):
    async with request.app.state.sessionmaker() as session:
        rows = (await session.execute(select(User).order_by(User.id))).scalars().all()
    return [{"id": u.id, "email": u.email, "balance_usd": format_usd(u.balance_micro),
             "created_at": iso_utc(u.created_at)} for u in rows]


@router.post("/api/users/{user_id}/reset-link")
async def create_reset_link(user_id: int, request: Request):
    """一次性的重設密碼連結，30 分鐘內有效。token 放在網址的 # 後面：瀏覽器不會把它送到伺服器的紀錄裡。"""
    token = secrets.token_urlsafe(32)
    async with request.app.state.sessionmaker.begin() as session:
        if await session.get(User, user_id) is None:
            raise HTTPException(404, "找不到這個使用者")
        session.add(PasswordReset(user_id=user_id, token_hash=sha256_hex(token), expires_at=utcnow() + RESET_TTL))
    base = request.app.state.settings.public_url.rstrip("/")
    return {"link": f"{base}/#reset/{token}", "expires_minutes": int(RESET_TTL.total_seconds() // 60)}


class CreditIn(BaseModel):
    usd: str = Field(max_length=32)  # 可為負數（扣回）
    note: str = Field(default="admin", max_length=64)


@router.post("/api/users/{user_id}/credit")
async def credit_user(user_id: int, body: CreditIn, request: Request):
    try:
        micro = usd_to_micro(body.usd)
    except ValueError as exc:
        raise HTTPException(400, f"金額格式錯誤：{exc}") from None
    if micro == 0:
        raise HTTPException(400, "金額不能是 0")
    async with request.app.state.sessionmaker.begin() as session:
        if not await change_balance(session, user_id, micro, "adjust", body.note or "admin"):
            raise HTTPException(404, "找不到這個使用者")
        balance = (await session.execute(select(User.balance_micro).where(User.id == user_id))).scalar_one()
    return {"balance_usd": format_usd(balance)}
