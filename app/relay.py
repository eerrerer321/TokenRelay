"""OpenAI 相容的轉發端點：/v1/models、/v1/chat/completions。

一次請求的生命週期：驗 key → 查模型 → 檢查參數 → 預扣 → 轉發 → 結算。
每一條路徑（成功、上游錯誤、使用者斷線、程式例外）都必須「剛好結算一次」：
沒結算到 → 使用者的錢卡在預扣；結算兩次 → 帳本錯亂。
"""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import NoReturn

import anyio
import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select

from app.billing import HoldRejected, calc_cost, estimate_hold, format_usd, place_hold, settle
from app.config import ModelConfig, Settings, UpstreamConfig
from app.models import ApiKey
from app.security import sha256_hex

log = logging.getLogger("relay")
router = APIRouter(prefix="/v1", tags=["轉發（OpenAI 相容）"])

# 白名單：只放行已知「不會讓上游額外收費、或費用已被 max_tokens 限住」的欄位。
# 沒列在這裡的一律拒絕，例如 OpenRouter 的 models（備援到更貴的模型）、plugins、
# OpenAI 的 service_tier（更貴的服務等級）、web_search_options、audio、prediction。
ALLOWED_FIELDS = frozenset({
    "model", "messages", "stream", "stream_options", "max_tokens", "max_completion_tokens", "n",
    "temperature", "top_p", "stop", "presence_penalty", "frequency_penalty", "logit_bias",
    "logprobs", "top_logprobs", "seed", "response_format", "tools", "tool_choice",
    "parallel_tool_calls", "user", "reasoning_effort",
})
# reasoning_content：DeepSeek 思考模式在工具呼叫的多輪對話裡，要把上一輪的思考內容帶回去。
# 它算輸入 token，已經包含在預扣用的請求 byte 數裡
MESSAGE_FIELDS = frozenset({"role", "content", "name", "tool_calls", "tool_call_id", "refusal", "reasoning_content"})
CONTENT_PART_FIELDS = {  # 也擋掉 cache_control 這類會改變計費方式的附加欄位
    "text": frozenset({"type", "text"}),
    "refusal": frozenset({"type", "refusal"}),
    "image_url": frozenset({"type", "image_url"}),
}
IMAGE_URL_FIELDS = frozenset({"url", "detail"})
TOOL_FIELDS = frozenset({"type", "function"})
FUNCTION_FIELDS = frozenset({"name", "description", "parameters", "strict"})
STREAM_OPTION_FIELDS = frozenset({"include_usage", "include_obfuscation"})
MAX_TOKEN_FIELDS = ("max_tokens", "max_completion_tokens")

# 這些錯誤代表請求「一定還沒送到上游」，上游不可能收費 → 全額退款
NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)
# 這些上游錯誤是「請求本身有問題」，把（遮蔽過的）原因告訴使用者；其他錯誤一律改成通用訊息
CLIENT_ERROR_STATUS = {400, 404, 413, 422}
# 上游前面的閘道（例如 Cloudflare 的 524）等太久先回逾時，後面的模型可能照樣跑完、照樣收費。
# 當下無法知道上游有沒有扣錢 → 先收預扣（跟中途斷線一樣），站長對照上游帳單，沒扣錢再用補帳退回
GATEWAY_TIMEOUT_STATUS = {504, 524}
KEY_LIKE = re.compile(r"\bsk-[A-Za-z0-9_\-]{4,}")


class OpenAIError(Exception):
    """用 OpenAI 的錯誤格式回應，各種客戶端工具才能正確顯示錯誤訊息。"""

    def __init__(self, status: int, message: str, code: str, type_: str = "invalid_request_error"):
        super().__init__(message)
        self.status, self.message, self.code, self.type = status, message, code, type_


async def openai_error_handler(request: Request, exc: OpenAIError) -> JSONResponse:
    if request.url.path.startswith("/v1/messages"):  # Claude Code 這類 Anthropic 格式的工具看得懂的錯誤格式
        return JSONResponse({"type": "error", "error": {"type": exc.type, "message": exc.message}},
                            status_code=exc.status)
    return JSONResponse(
        {"error": {"message": exc.message, "type": exc.type, "code": exc.code}}, status_code=exc.status
    )


@dataclass
class KeyAuth:
    user_id: int
    api_key_id: int


async def api_key_auth(request: Request) -> KeyAuth:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        token = request.headers.get("x-api-key", "")  # Anthropic 格式的工具（Claude Code）用這個標頭帶 key
    if not token.startswith("sk-"):
        raise OpenAIError(401, "缺少 API key（Authorization: Bearer sk-...）", "invalid_api_key", "authentication_error")
    async with request.app.state.sessionmaker() as session:
        row = (
            await session.execute(
                select(ApiKey.id, ApiKey.user_id).where(
                    ApiKey.key_hash == sha256_hex(token), ApiKey.is_active.is_(True)
                )
            )
        ).first()
    if row is None:
        raise OpenAIError(401, "API key 無效或已停用", "invalid_api_key", "authentication_error")
    return KeyAuth(user_id=row.user_id, api_key_id=row.id)


@router.get("/models")
async def list_models(request: Request, _auth: KeyAuth = Depends(api_key_auth)):
    names = sorted(request.app.state.settings.open_models())
    # created 是 OpenAI 格式的必填欄位；模型寫在設定檔裡，沒有「建立時間」，填 0
    return {"object": "list", "data": [{"id": name, "object": "model", "created": 0, "owned_by": "relay"}
                                       for name in names]}


# ---------------------------------------------------------------- 請求檢查


async def read_body_limited(request: Request, limit: int) -> bytes:
    """邊讀邊算大小，超過上限立刻中止（不能先整包讀進記憶體再檢查）。"""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise OpenAIError(413, f"請求內容超過上限 {limit} bytes", "request_too_large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise OpenAIError(413, f"請求內容超過上限 {limit} bytes", "request_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_json_object(raw: bytes) -> dict:
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):  # RecursionError：故意塞超深的巢狀陣列
        raise OpenAIError(400, "請求內容不是合法的 JSON", "invalid_json") from None
    if not isinstance(body, dict):
        raise OpenAIError(400, "請求內容必須是 JSON 物件", "invalid_json")
    try:
        # JSON 的 "\ud800" 這種孤立代理字元讀得進來，卻無法轉成 UTF-8：查資料庫、回錯誤訊息時才會爆成 500
        json.dumps(body, ensure_ascii=False).encode("utf-8")
    except (UnicodeEncodeError, RecursionError):
        raise OpenAIError(400, "請求內容含有無效的 Unicode 字元", "invalid_json") from None
    return body


KIND_NAMES = {"chat": "對話", "embedding": "embeddings", "image": "產生圖片", "speech": "文字轉語音"}


def load_route(settings: Settings, name, kind: str = "chat") -> tuple[ModelConfig, UpstreamConfig]:
    if not isinstance(name, str) or not name:
        raise OpenAIError(400, "缺少 model 參數", "missing_model")
    model = settings.open_models().get(name)
    if model is None:  # 下架的模型跟不存在一樣回應
        raise OpenAIError(404, f"模型 {name[:100]} 不存在或未開放", "model_not_found")
    if model.kind != kind:  # 例如拿對話模型打 embeddings：價格的算法不一樣，不能混用
        raise OpenAIError(400, f"模型 {name[:100]} 是{KIND_NAMES[model.kind]}模型，不能用在這個 API",
                          "model_not_supported")
    return model, settings.upstreams[model.upstream]


def reject_unknown_fields(obj: dict, allowed: frozenset, where: str) -> None:
    extra = set(obj) - allowed
    if extra:
        names = ", ".join(sorted(str(k)[:40] for k in extra))[:200]
        raise OpenAIError(400, f"{where}不支援的欄位：{names}（為了避免額外計費，只開放常見參數）",
                          "unsupported_parameter")


def check_messages(messages: list) -> int:
    """檢查訊息格式並回傳圖片數量。"""
    images = 0
    for message in messages:
        if not isinstance(message, dict):
            raise OpenAIError(400, "messages 格式錯誤", "invalid_messages")
        reject_unknown_fields(message, MESSAGE_FIELDS, "messages 裡")
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            kind = part.get("type") if isinstance(part, dict) else None
            allowed = CONTENT_PART_FIELDS.get(kind) if isinstance(kind, str) else None  # type 是陣列時不能拿來查表
            if allowed is None:
                raise OpenAIError(400, f"不支援的內容類型：{str(kind)[:40]}", "unsupported_content")
            reject_unknown_fields(part, allowed, "內容區塊裡")
            if kind == "image_url":
                image = part.get("image_url")
                if not isinstance(image, dict):
                    raise OpenAIError(400, "image_url 格式錯誤", "invalid_messages")
                reject_unknown_fields(image, IMAGE_URL_FIELDS, "image_url 裡")
                images += 1
    return images


def check_tools(tools) -> None:
    if tools is None:
        return
    if not isinstance(tools, list):
        raise OpenAIError(400, "tools 必須是陣列", "invalid_parameter")
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            raise OpenAIError(400, "tools 只支援 type=function（由上游代執行的工具會另外計費）",
                              "unsupported_parameter")
        reject_unknown_fields(tool, TOOL_FIELDS, "tools 裡")
        reject_unknown_fields(tool["function"], FUNCTION_FIELDS, "tools.function 裡")


def prepare_upstream_body(body: dict, model: ModelConfig) -> tuple[dict, int, bool, int]:
    """檢查並改寫請求。回傳（要送給上游的 body、輸出 token 上限、是否串流、圖片數）。"""
    reject_unknown_fields(body, ALLOWED_FIELDS, "")
    body = dict(body)

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise OpenAIError(400, "messages 必須是非空陣列", "invalid_messages")
    images = check_messages(messages)
    if images and model.image_tokens <= 0:
        raise OpenAIError(400, "這個模型沒有開放圖片輸入", "images_not_supported")
    check_tools(body.get("tools"))

    n = body.get("n")
    if n is not None and not (type(n) is int and n == 1):
        raise OpenAIError(400, "只支援 n=1（一次要多份回答，費用會乘以 n）", "unsupported_parameter")

    # 輸出上限：兩種欄位都先拿掉，只用「這個模型的上游認得的那一個欄位」送出，值壓在模型上限內。
    # 如果照使用者給的欄位原樣轉送，上游不認得那個欄位時會改用自己的預設值，實際輸出就可能超過預扣。
    requested = []
    for field in MAX_TOKEN_FIELDS:
        value = body.pop(field, None)
        if value is None:
            continue
        if type(value) is not int or value <= 0:
            raise OpenAIError(400, f"{field} 必須是正整數", "invalid_parameter")
        requested.append(value)
    output_cap = min(requested + [model.max_output_tokens])
    body[model.max_tokens_field] = output_cap

    stream = body.get("stream")
    if stream is not None and type(stream) is not bool:
        raise OpenAIError(400, "stream 必須是 true 或 false", "invalid_parameter")
    stream = stream is True
    if "stream_options" in body:
        options = body["stream_options"]
        if not isinstance(options, dict):
            raise OpenAIError(400, "stream_options 格式錯誤", "invalid_parameter")
        reject_unknown_fields(options, STREAM_OPTION_FIELDS, "stream_options 裡")
    if stream:
        # 強制要求上游在串流最後附上用量，否則站方無法按實際 token 計費
        body["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}

    body["model"] = model.upstream_model
    return body, output_cap, stream, images


def encode_upstream_body(body: dict) -> bytes:
    """在預扣之前就先編碼好：Python 讀得進來、卻無法轉送的值（例如 NaN）在這裡就擋掉，
    不會發生「錢已經扣了，請求卻根本送不出去」。"""
    try:
        return json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, RecursionError):  # RecursionError：超深的巢狀（parse_json_object 通常已先擋下）
        raise OpenAIError(400, "請求包含無法轉送的內容（NaN、Infinity 或過深的巢狀）", "invalid_json") from None


def _valid_count(value) -> bool:
    return type(value) is int and value >= 0


def parse_cached_tokens(usage: dict, prompt: int) -> int:
    """prompt_tokens 裡有多少是快取命中。各家欄位不同：
    OpenAI 格式（OpenAI、OpenRouter、Gemini 等）放在 prompt_tokens_details.cached_tokens；
    DeepSeek 放在 prompt_cache_hit_tokens。看不懂或數字不合理 → 當作 0，照一般輸入價收。"""
    details = usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if cached is None:
        cached = usage.get("prompt_cache_hit_tokens")
    return cached if _valid_count(cached) and cached <= prompt else 0


Usage = tuple[int, int, int]  # (輸入 tokens, 輸出 tokens, 輸入裡快取命中的 tokens)


def parse_usage(usage) -> tuple[int, int, int] | None:
    """回傳 (輸入 tokens, 輸出 tokens, 輸入裡快取命中的 tokens)。"""
    if not isinstance(usage, dict):
        return None
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if _valid_count(prompt) and _valid_count(completion):
        return prompt, completion, parse_cached_tokens(usage, prompt)
    return None


def usage_from_sse_line(line: str) -> tuple[int, int, int] | None:
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except ValueError:
        return None
    return parse_usage(obj.get("usage")) if isinstance(obj, dict) else None


def upstream_error_message(content: bytes) -> str:
    try:
        data = json.loads(content)
    except ValueError:
        data = None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(error, str):
            return error
        if isinstance(data.get("message"), str):
            return data["message"]
    return content.decode("utf-8", "replace")


# ---------------------------------------------------------------- 轉發與結算


class SettlingStreamingResponse(StreamingResponse):
    """不論正常結束、使用者中途斷線或出錯，最後一定會呼叫 on_close（用來結算）。

    使用者斷線時 Starlette 會取消串流；寫在 generator 裡的 finally 不一定有機會執行
    （例如 generator 還沒開始跑就斷線了），所以結算掛在回應物件最外層，並用 shield 擋住取消。
    """

    def __init__(self, content, *, on_close, **kwargs):
        super().__init__(content, **kwargs)
        self._on_close = on_close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                aclose = getattr(self.body_iterator, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:  # generator 已經因錯誤結束，關閉失敗不影響結算
                        pass
                await self._on_close()


@dataclass
class StreamProgress:
    usage: Usage | None = None
    completed: bool = False


class ChatStreamUsage:
    """從串流的每一行找用量。不同 API 的串流格式不同，各自有一個這樣的類別（見 app/apis.py）。"""

    def __init__(self):
        self.usage: Usage | None = None

    def feed(self, line: str) -> None:
        usage = usage_from_sse_line(line)
        if usage is not None:
            self.usage = usage


@dataclass(frozen=True)
class ApiFormat:
    """一種上游 API 的轉發方式：打哪個網址、怎麼從回應和串流讀出用量。"""

    path: str  # 接在上游 base_url 後面，例如 /chat/completions
    usage_from_json: Callable[[dict], Usage | None]
    stream_usage: Callable[[], object]  # 回傳有 feed(line) 方法和 usage 屬性的物件
    anthropic: bool = False  # True：打上游的 anthropic_base_url，用 x-api-key 驗證


CHAT_API = ApiFormat("/chat/completions", lambda data: parse_usage(data.get("usage")), ChatStreamUsage)
ANTHROPIC_VERSION = "2023-06-01"


class RelayJob:
    """一次已經預扣過的轉發。finish() 只會生效一次，並釋放同時請求數的名額。"""

    SETTLE_ATTEMPTS = 3

    def __init__(self, state, auth: KeyAuth, model: ModelConfig, upstream: UpstreamConfig, content: bytes,
                 hold: int, usage_id: int, api: ApiFormat = CHAT_API, cost: Callable[[Usage], int] | None = None):
        self.sm = state.sessionmaker
        self.http: httpx.AsyncClient = state.http
        self.limiter = state.limiter
        self.auth, self.model, self.content = auth, model, content
        self.hold, self.usage_id = hold, usage_id
        self.api = api
        self._cost = cost
        self.upstream_key = upstream.api_key
        # 網址格式在讀設定檔時就驗證過（UpstreamConfig），這裡建立請求不會因為網址失敗
        if api.anthropic:
            self.url = upstream.anthropic_base_url.rstrip("/") + api.path
            # 不轉送使用者的 anthropic-beta 標頭：beta 功能可能改變計費（例如更長的 context 價格不同）
            self.headers = {"x-api-key": upstream.api_key, "anthropic-version": ANTHROPIC_VERSION,
                            "Content-Type": "application/json"}
        else:
            self.url = upstream.base_url.rstrip("/") + api.path
            self.headers = {"Authorization": f"Bearer {upstream.api_key}", "Content-Type": "application/json"}
        self.settled = False

    def cost(self, usage: Usage) -> int:
        if self._cost is not None:  # 例如圖片按張數計費
            return self._cost(usage)
        return calc_cost(usage[0], usage[1], self.model.input_price_micro, self.model.output_price_micro or 0,
                         cached_tokens=usage[2], cached_input_price_micro=self.model.cached_input_price_micro)

    def sanitize(self, text: str) -> str:
        """上游的錯誤訊息可能夾帶站方的 key 或類似 key 的字串，轉給使用者前先遮掉。"""
        if self.upstream_key:  # 空字串的 key（不需要 key 的本機上游）用 replace 會在每個字元之間插入遮蔽字樣
            text = text.replace(self.upstream_key, "[已遮蔽]")
        return KEY_LIKE.sub("sk-***", text)[:300]

    async def finish(self, cost: int, status: str, usage: Usage | None = None,
                     upstream_status: int | None = None) -> None:
        if self.settled:
            return
        self.settled = True
        try:
            with anyio.CancelScope(shield=True):
                for attempt in range(1, self.SETTLE_ATTEMPTS + 1):
                    try:
                        await settle(
                            self.sm, self.usage_id, cost=cost, status=status,
                            prompt_tokens=usage[0] if usage else None,
                            completion_tokens=usage[1] if usage else None,
                            upstream_status=upstream_status,
                        )
                        break
                    except Exception:
                        # settle 靠 status='pending' 條件保證冪等，重試不會重複結算
                        if attempt == self.SETTLE_ATTEMPTS:
                            log.exception("用量 #%s 結算失敗，預扣維持不退，請人工檢查", self.usage_id)
                        else:
                            await anyio.sleep(0.5 * attempt)
        finally:
            self.limiter.release(self.auth.user_id)

    async def _send_failed(self, exc: Exception) -> NoReturn:
        # 請求一定沒送出 → 上游不可能收費，全額退；其他情況（逾時、中途斷線）上游可能已經在算錢 → 不退
        if isinstance(exc, NOT_SENT_ERRORS):
            await self.finish(0, "upstream_unreachable")
            raise OpenAIError(502, "連不到上游，這次不收費", "upstream_unreachable", "api_error") from exc
        await self.finish(self.hold, "upstream_broken")
        raise OpenAIError(502, "上游連線中途中斷", "upstream_broken", "api_error") from exc

    async def upstream_error(self, status: int, content: bytes) -> NoReturn:
        if status in GATEWAY_TIMEOUT_STATUS:
            await self.finish(self.hold, "upstream_timeout", upstream_status=status)
            log.warning("上游逾時 HTTP %s（model=%s，用量 #%s），已收預扣，請對照上游帳單確認",
                        status, self.model.name, self.usage_id)
            raise OpenAIError(504, "上游處理逾時：上游可能已經產生費用，這次先收預扣；站長確認上游沒收費會退回",
                              "upstream_timeout", "api_error")
        await self.finish(0, "upstream_error", upstream_status=status)
        log.warning("上游回應 HTTP %s（model=%s）", status, self.model.name)
        if status in CLIENT_ERROR_STATUS:
            message = self.sanitize(upstream_error_message(content))
            raise OpenAIError(status, f"上游拒絕這個請求：{message}", "upstream_rejected")
        if status == 429:
            raise OpenAIError(429, "上游忙碌或額度受限，請稍後再試", "upstream_rate_limited", "rate_limit_error")
        if status in (401, 403):
            raise OpenAIError(502, "上游驗證失敗，請通知站長", "upstream_auth_error", "api_error")
        raise OpenAIError(502, f"上游錯誤（HTTP {status}），這次不收費", "upstream_error", "api_error")

    async def _open(self) -> httpx.Response:
        """送出請求並拿到上游的狀態碼與標頭，內文還沒讀。"""
        request = self.http.build_request("POST", self.url, content=self.content, headers=self.headers)
        try:
            return await self.http.send(request, stream=True)
        except httpx.HTTPError as exc:
            await self._send_failed(exc)

    async def _fail_from_error_response(self, resp: httpx.Response) -> NoReturn:
        try:
            content = await resp.aread()
        except httpx.HTTPError:
            content = b""  # 讀錯誤內文失敗不影響「上游已明確拒絕」這個事實，照樣退款
        finally:
            await resp.aclose()
        await self.upstream_error(resp.status_code, content)

    async def run_once(self) -> Response:
        try:
            resp = await self._open()
            if resp.status_code >= 400:
                await self._fail_from_error_response(resp)
            try:
                content = await resp.aread()
            except httpx.HTTPError as exc:
                await self.finish(self.hold, "upstream_broken")
                raise OpenAIError(502, "上游連線中途中斷", "upstream_broken", "api_error") from exc
            finally:
                await resp.aclose()
            # 有些上游在 HTTP 200 的回應裡回報錯誤；跟串流一樣，夾帶站方 key 就先遮掉
            if self.upstream_key:
                content = content.replace(self.upstream_key.encode(), "[已遮蔽]".encode())
            try:
                data = json.loads(content)
            except ValueError:
                data = None
            if not isinstance(data, dict):
                await self.finish(self.hold, "bad_upstream_response")
                raise OpenAIError(502, "上游回應格式錯誤", "bad_upstream_response", "api_error")
            usage = self.api.usage_from_json(data)
            if usage is None:
                await self.finish(self.hold, "no_usage")
            else:
                await self.finish(self.cost(usage), "ok", usage)
            return JSONResponse(data)
        finally:
            if not self.settled:  # 任何沒預料到的例外或取消：站方不承擔風險，預扣不退
                await self.finish(self.hold, "error")

    async def run_stream(self) -> Response:
        try:
            resp = await self._open()
            if resp.status_code >= 400:
                await self._fail_from_error_response(resp)
        except BaseException:
            if not self.settled:
                await self.finish(self.hold, "error")
            raise

        progress = StreamProgress()
        return SettlingStreamingResponse(
            self._relay_lines(resp, progress),
            on_close=partial(self._close_stream, resp, progress),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    async def run_binary(self, usage: Usage) -> Response:
        """回應不是 JSON（例如語音檔）、費用在送出前就算好的請求：成功就收預扣的金額。"""
        try:
            resp = await self._open()
            if resp.status_code >= 400:
                await self._fail_from_error_response(resp)
            try:
                content = await resp.aread()
            except httpx.HTTPError as exc:
                await self.finish(self.hold, "upstream_broken")
                raise OpenAIError(502, "上游連線中途中斷", "upstream_broken", "api_error") from exc
            finally:
                await resp.aclose()
            # 上游可能用 HTTP 200 回傳夾帶 key 的錯誤 JSON，跟 run_once 一樣先遮掉
            if self.upstream_key:
                content = content.replace(self.upstream_key.encode(), "[已遮蔽]".encode())
            await self.finish(self.hold, "ok", usage)
            return Response(content, media_type=resp.headers.get("content-type", "application/octet-stream"))
        finally:
            if not self.settled:
                await self.finish(self.hold, "error")

    async def _relay_lines(self, resp: httpx.Response, progress: StreamProgress):
        tracker = self.api.stream_usage()
        async for line in resp.aiter_lines():
            tracker.feed(line)
            if tracker.usage is not None:
                progress.usage = tracker.usage
            if self.upstream_key and self.upstream_key in line:  # 串流裡的錯誤事件若夾帶站方 key，也不能轉出去
                line = line.replace(self.upstream_key, "[已遮蔽]")
            yield line + "\n"
        progress.completed = True

    async def _close_stream(self, resp: httpx.Response, progress: StreamProgress) -> None:
        try:
            await resp.aclose()
        finally:
            if progress.usage is not None:
                status = "ok" if progress.completed else "interrupted"
                await self.finish(self.cost(progress.usage), status, progress.usage)
            else:
                # 沒拿到用量（使用者中途斷線、上游沒給）→ 無法知道實際花多少，預扣全收
                await self.finish(self.hold, "no_usage" if progress.completed else "interrupted")


@router.post("/chat/completions")
async def chat_completions(request: Request, auth: KeyAuth = Depends(api_key_auth)):
    state = request.app.state
    raw = await read_body_limited(request, state.settings.max_body_bytes)
    body = parse_json_object(raw)
    model, upstream = load_route(state.settings, body.get("model"))
    upstream_body, output_cap, stream, images = prepare_upstream_body(body, model)
    content = encode_upstream_body(upstream_body)
    # 用「實際要送給上游的內容」估算，上游計算 token 的對象就是它
    hold = estimate_hold(len(content), images, model.image_tokens, output_cap,
                         model.input_price_micro, model.output_price_micro, has_tools=bool(upstream_body.get("tools")))
    job = await start_job(state, auth, model, upstream, content, hold, stream)
    return await (job.run_stream() if stream else job.run_once())


async def start_job(state, auth: KeyAuth, model: ModelConfig, upstream: UpstreamConfig, content: bytes, hold: int,
                    stream: bool, api: ApiFormat = CHAT_API, cost: Callable[[Usage], int] | None = None) -> RelayJob:
    """佔用同時請求數的名額並預扣。之後由 RelayJob 負責結算與歸還名額。"""
    limiter = state.limiter
    if not limiter.try_acquire(auth.user_id):
        raise OpenAIError(
            429, f"同時進行中的請求太多（上限 {limiter.limit}）", "too_many_concurrent_requests", "rate_limit_error"
        )
    try:
        usage_id = await place_hold(
            state.sessionmaker, user_id=auth.user_id, api_key_id=auth.api_key_id,
            model=model.name, stream=stream, hold=hold,
        )
    except HoldRejected as exc:
        limiter.release(auth.user_id)
        if exc.reason == "key_revoked":
            raise OpenAIError(401, "API key 無效或已停用", "invalid_api_key", "authentication_error") from None
        raise OpenAIError(
            402, f"餘額不足：這個請求最多可能花 {format_usd(hold)} 美元", "insufficient_balance", "insufficient_quota"
        ) from None
    except BaseException:
        limiter.release(auth.user_id)
        raise

    return RelayJob(state, auth, model, upstream, content, hold, usage_id, api, cost)
