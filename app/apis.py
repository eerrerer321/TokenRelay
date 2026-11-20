"""chat/completions 以外的端點：embeddings、responses（Codex CLI）、messages（Claude Code）、產生圖片、文字轉語音。

原則跟 chat/completions 一樣（見 app/relay.py）：參數白名單 → 估「最壞情況」預扣 → 轉發 → 按上游回報的用量結算。
上游要原生支援同一種格式（[upstream] 的 responses、anthropic_base_url），這裡不做格式轉換。
"""

import json

from fastapi import APIRouter, Depends, Request

from app.billing import calc_cost, estimate_hold
from app.relay import (
    ApiFormat,
    KeyAuth,
    OpenAIError,
    Usage,
    api_key_auth,
    encode_upstream_body,
    load_route,
    parse_json_object,
    read_body_limited,
    reject_unknown_fields,
    start_job,
)

router = APIRouter(prefix="/v1", tags=["轉發（其他 API）"])


def _count(value) -> int | None:
    """上游回報的數量：必須是 0 以上的整數，否則當作沒有。"""
    return value if type(value) is int and value >= 0 else None


def _sse_json(line: str) -> dict | None:
    if not line.startswith("data:"):
        return None
    try:
        obj = json.loads(line[5:].strip())
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


class NoStream:
    """不支援串流的 API 用的佔位。"""

    usage = None

    def feed(self, line: str) -> None:
        pass


async def read_request(request: Request) -> dict:
    return parse_json_object(await read_body_limited(request, request.app.state.settings.max_body_bytes))


def output_cap(requested, field: str, model_cap: int) -> int:
    if requested is None:
        return model_cap
    if type(requested) is not int or requested <= 0:
        raise OpenAIError(400, f"{field} 必須是正整數", "invalid_parameter")
    return min(requested, model_cap)


def stream_flag(body: dict) -> bool:
    stream = body.get("stream")
    if stream is not None and type(stream) is not bool:
        raise OpenAIError(400, "stream 必須是 true 或 false", "invalid_parameter")
    return stream is True


def check_dict(value, allowed: frozenset, where: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise OpenAIError(400, f"{where} 格式錯誤", "invalid_parameter")
    reject_unknown_fields(value, allowed, f"{where} 裡")


# ================================================================ embeddings

EMBEDDING_FIELDS = frozenset({"model", "input", "dimensions", "encoding_format", "user"})


def embedding_usage(data: dict) -> Usage | None:
    usage = data.get("usage")
    prompt = _count(usage.get("prompt_tokens")) if isinstance(usage, dict) else None
    return None if prompt is None else (prompt, 0, 0)


EMBEDDINGS_API = ApiFormat("/embeddings", embedding_usage, NoStream)


@router.post("/embeddings")
async def embeddings(request: Request, auth: KeyAuth = Depends(api_key_auth)):
    state = request.app.state
    body = await read_request(request)
    model, upstream = load_route(state.settings, body.get("model"), "embedding")
    reject_unknown_fields(body, EMBEDDING_FIELDS, "")
    text = body.get("input")
    if not (isinstance(text, str) and text) and not (isinstance(text, list) and text):
        raise OpenAIError(400, "input 必須是非空字串或陣列", "invalid_parameter")
    content = encode_upstream_body({**body, "model": model.upstream_model})
    # 文字或 token 編號陣列，每個 token 至少佔 1 byte，所以 byte 數就是輸入 token 的上限
    hold = estimate_hold(len(content), 0, 0, 0, model.input_price_micro, 0)
    job = await start_job(state, auth, model, upstream, content, hold, False, EMBEDDINGS_API)
    return await job.run_once()


# ================================================================ responses（OpenAI 新格式，Codex CLI 用）

RESPONSES_FIELDS = frozenset({
    "model", "input", "instructions", "max_output_tokens", "stream", "stream_options", "temperature", "top_p",
    "tools", "tool_choice", "parallel_tool_calls", "reasoning", "text", "store", "include", "metadata", "user",
    "prompt_cache_key", "truncation", "safety_identifier",
})
# 只開放「由使用者那邊執行」的工具。web_search、file_search、code_interpreter、image_generation、mcp
# 這些是上游代執行、另外計費的，一律不開
RESPONSE_TOOL_FIELDS = {
    "function": frozenset({"type", "name", "description", "parameters", "strict"}),
    "custom": frozenset({"type", "name", "description", "format"}),
    "local_shell": frozenset({"type"}),
}
RESPONSE_ITEM_TYPES = frozenset({
    "message", "function_call", "function_call_output", "reasoning", "custom_tool_call",
    "custom_tool_call_output", "local_shell_call", "local_shell_call_output",
})
# 內容區塊的類型 → 可以有的欄位。input_file（PDF 等）的 token 數無法事先估計；
# file_id 指的是上游帳號裡的檔案，那是站方共用的帳號，可能是別人上傳的，所以 input_image 也不收 file_id
RESPONSE_CONTENT_FIELDS = {
    "input_text": frozenset({"type", "text"}),
    "output_text": frozenset({"type", "text", "annotations", "logprobs"}),
    "input_image": frozenset({"type", "image_url", "detail"}),
    "refusal": frozenset({"type", "refusal"}),
    "summary_text": frozenset({"type", "text"}),
    "reasoning_text": frozenset({"type", "text"}),
}
RESPONSE_INCLUDE = frozenset({"reasoning.encrypted_content"})  # 不保存對話時，多輪推理要靠它帶回去
# 工具會送、但站方不轉送的欄位（直接拿掉，不回錯誤，工具才能正常用）：
# prompt_cache_retention 在 GPT-5.6 之後會讓上游另外收「寫入快取」的費用，站方算不到
RESPONSES_STRIPPED = frozenset({"prompt_cache_retention"})


def check_response_input(items) -> int:
    """檢查 input 並回傳圖片數。"""
    if isinstance(items, str):
        return 0
    if not isinstance(items, list) or not items:
        raise OpenAIError(400, "input 必須是字串或非空陣列", "invalid_parameter")
    images = 0
    for item in items:
        if not isinstance(item, dict):
            raise OpenAIError(400, "input 格式錯誤", "invalid_parameter")
        kind = item.get("type", "message")
        if not isinstance(kind, str) or kind not in RESPONSE_ITEM_TYPES:  # type 是陣列時不能拿來查表
            raise OpenAIError(400, f"不支援的 input 類型：{str(kind)[:40]}", "unsupported_content")
        # 不只訊息的 content：工具結果（function_call_output 等）的 output 也可以是內容陣列，可以夾帶圖片或檔案
        parts = [part for field in ("content", "output", "summary")
                 if isinstance(item.get(field), list) for part in item[field]]
        for part in parts:
            part_type = part.get("type") if isinstance(part, dict) else None
            allowed = RESPONSE_CONTENT_FIELDS.get(part_type) if isinstance(part_type, str) else None
            if allowed is None:
                raise OpenAIError(400, f"不支援的內容類型：{str(part_type)[:40]}", "unsupported_content")
            reject_unknown_fields(part, allowed, "內容區塊裡")
            if part_type == "input_image":
                if not isinstance(part.get("image_url"), str):
                    raise OpenAIError(400, "input_image 只支援 image_url", "unsupported_content")
                images += 1
    return images


def check_response_tools(tools) -> None:
    if tools is None:
        return
    if not isinstance(tools, list):
        raise OpenAIError(400, "tools 必須是陣列", "invalid_parameter")
    for tool in tools:
        kind = tool.get("type") if isinstance(tool, dict) else None
        allowed = RESPONSE_TOOL_FIELDS.get(kind) if isinstance(kind, str) else None
        if allowed is None:
            raise OpenAIError(400, f"不支援的工具類型：{str(kind)[:40]}（由上游代執行的工具會另外計費）",
                              "unsupported_parameter")
        reject_unknown_fields(tool, allowed, "tools 裡")


def responses_usage_from(usage) -> Usage | None:
    if not isinstance(usage, dict):
        return None
    prompt, completion = _count(usage.get("input_tokens")), _count(usage.get("output_tokens"))
    if prompt is None or completion is None:
        return None
    details = usage.get("input_tokens_details")
    cached = _count(details.get("cached_tokens")) if isinstance(details, dict) else None
    return prompt, completion, cached if cached is not None and cached <= prompt else 0


class ResponsesStreamUsage:
    """串流最後的 response.completed（或 incomplete／failed）事件裡有整個回應的用量。"""

    DONE_EVENTS = frozenset({"response.completed", "response.incomplete", "response.failed"})

    def __init__(self):
        self.usage: Usage | None = None

    def feed(self, line: str) -> None:
        event = _sse_json(line)
        if event and event.get("type") in self.DONE_EVENTS and isinstance(event.get("response"), dict):
            self.usage = responses_usage_from(event["response"].get("usage")) or self.usage


RESPONSES_API = ApiFormat("/responses", lambda data: responses_usage_from(data.get("usage")), ResponsesStreamUsage)


@router.post("/responses")
async def responses(request: Request, auth: KeyAuth = Depends(api_key_auth)):
    state = request.app.state
    body = await read_request(request)
    model, upstream = load_route(state.settings, body.get("model"), "chat")
    if not upstream.responses:
        raise OpenAIError(400, f"模型 {model.name} 的上游不支援 /v1/responses，請改用 /v1/chat/completions",
                          "unsupported_api")
    body = {k: v for k, v in body.items() if k not in RESPONSES_STRIPPED}
    if body.get("service_tier") in ("auto", "default"):  # 一般價格，拿掉就好；priority、flex 等其他值照樣拒絕
        del body["service_tier"]
    reject_unknown_fields(body, RESPONSES_FIELDS, "")
    if body.get("previous_response_id") is not None:  # 白名單已擋；寫明原因
        raise OpenAIError(400, "不支援 previous_response_id", "unsupported_parameter")
    images = check_response_input(body.get("input"))
    if images and model.image_tokens <= 0:
        raise OpenAIError(400, "這個模型沒有開放圖片輸入", "images_not_supported")
    check_response_tools(body.get("tools"))
    check_dict(body.get("reasoning"), frozenset({"effort", "summary", "generate_summary"}), "reasoning")
    check_dict(body.get("text"), frozenset({"format", "verbosity"}), "text")
    check_dict(body.get("stream_options"), frozenset({"include_obfuscation"}), "stream_options")
    include = body.get("include")
    if include is not None and (not isinstance(include, list)
                                or not all(isinstance(x, str) and x in RESPONSE_INCLUDE for x in include)):
        raise OpenAIError(400, "include 只支援 reasoning.encrypted_content", "unsupported_parameter")
    stream = stream_flag(body)
    cap = output_cap(body.get("max_output_tokens"), "max_output_tokens", model.max_output_tokens)

    # store 一律關掉：站方所有使用者共用同一個上游帳號，上游存下來的對話，別人拿到 ID 就可能讀到
    upstream_body = {**body, "model": model.upstream_model, "max_output_tokens": cap, "store": False}
    content = encode_upstream_body(upstream_body)
    hold = estimate_hold(len(content), images, model.image_tokens, cap, model.input_price_micro,
                         model.output_price_micro, has_tools=bool(upstream_body.get("tools")))
    job = await start_job(state, auth, model, upstream, content, hold, stream, RESPONSES_API)
    return await (job.run_stream() if stream else job.run_once())


# ================================================================ messages（Anthropic 格式，Claude Code 用）

MESSAGES_FIELDS = frozenset({
    "model", "messages", "system", "max_tokens", "stream", "temperature", "top_p", "top_k", "stop_sequences",
    "tools", "tool_choice", "metadata", "thinking", "output_config",
})
# effort 只影響思考多少（仍受 max_tokens 限制），format 是結構化輸出；task_budget 是 beta 功能，不開放
OUTPUT_CONFIG_FIELDS = frozenset({"effort", "format"})
# context_management（清掉舊的工具結果、自動摘要）要 anthropic-beta 標頭才能用，站方不轉送那個標頭，
# 原樣轉送只會被上游拒絕；直接拿掉，Claude Code 才能正常用（代價：長對話不會被上游自動縮減）
MESSAGES_STRIPPED = frozenset({"context_management"})
ANTHROPIC_IMAGE_SOURCE_FIELDS = {"base64": frozenset({"type", "media_type", "data"}), "url": frozenset({"type", "url"})}
ANTHROPIC_BLOCK_FIELDS = {
    "text": frozenset({"type", "text", "citations"}),
    "image": frozenset({"type", "source"}),
    "tool_use": frozenset({"type", "id", "name", "input"}),
    "tool_result": frozenset({"type", "tool_use_id", "content", "is_error"}),
    "thinking": frozenset({"type", "thinking", "signature"}),
    "redacted_thinking": frozenset({"type", "data"}),
}
ANTHROPIC_TOOL_FIELDS = frozenset({"type", "name", "description", "input_schema", "strict", "eager_input_streaming"})


def _strip_cache_control(block: dict) -> dict:
    # cache_control 會讓上游收「寫入快取」的費用（比一般輸入貴）。站方按一般輸入價收，所以拿掉；
    # 不直接拒絕，是因為 Claude Code 幾乎每個請求都會帶
    return {k: v for k, v in block.items() if k != "cache_control"}


TOOL_RESULT_BLOCK_TYPES = frozenset({"text", "image"})  # tool_result 裡只能再放文字和圖片（不能再巢狀）


def check_anthropic_blocks(blocks, where: str, kinds: frozenset | None = None) -> tuple[list | str, int]:
    """檢查內容區塊（可能是字串），回傳（拿掉 cache_control 後的內容, 圖片數）。kinds：只接受這些類型。"""
    if isinstance(blocks, str):
        return blocks, 0
    if not isinstance(blocks, list):
        raise OpenAIError(400, f"{where} 格式錯誤", "invalid_messages")
    cleaned, images = [], 0
    for block in blocks:
        kind = block.get("type") if isinstance(block, dict) else None
        allowed = ANTHROPIC_BLOCK_FIELDS.get(kind) if isinstance(kind, str) else None
        if kinds is not None and kind not in kinds:
            allowed = None
        if allowed is None:  # document（PDF）、search_result 等的 token 數無法事先估計
            raise OpenAIError(400, f"不支援的內容類型：{str(kind)[:40]}", "unsupported_content")
        block = _strip_cache_control(block)
        reject_unknown_fields(block, allowed, f"{where}裡")
        if kind == "image":
            source = block.get("source")
            source_type = source.get("type") if isinstance(source, dict) else None
            source_fields = ANTHROPIC_IMAGE_SOURCE_FIELDS.get(source_type) if isinstance(source_type, str) else None
            if source_fields is None:  # file（上游帳號裡的檔案）不開放
                raise OpenAIError(400, "image 只支援 base64 或 url", "unsupported_content")
            reject_unknown_fields(source, source_fields, "image.source 裡")
            images += 1
        if kind == "tool_result" and "content" in block:
            block["content"], nested = check_anthropic_blocks(block["content"], "tool_result",
                                                              TOOL_RESULT_BLOCK_TYPES)
            images += nested
        cleaned.append(block)
    return cleaned, images


def check_anthropic_tools(tools) -> list | None:
    if tools is None:
        return None
    if not isinstance(tools, list):
        raise OpenAIError(400, "tools 必須是陣列", "invalid_parameter")
    cleaned = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type", "custom") != "custom":
            kind = tool.get("type") if isinstance(tool, dict) else None
            raise OpenAIError(400, f"不支援的工具類型：{str(kind)[:40]}（由上游代執行的工具會另外計費）",
                              "unsupported_parameter")
        tool = _strip_cache_control(tool)
        reject_unknown_fields(tool, ANTHROPIC_TOOL_FIELDS, "tools 裡")
        cleaned.append(tool)
    return cleaned


def anthropic_usage_from(usage) -> Usage | None:
    """Anthropic 的 input_tokens 不含快取的部分，要把讀快取、寫快取的 token 加回來才是輸入總數。"""
    if not isinstance(usage, dict):
        return None
    fresh, completion = _count(usage.get("input_tokens")), _count(usage.get("output_tokens"))
    if fresh is None or completion is None:
        return None
    read = _count(usage.get("cache_read_input_tokens")) or 0
    written = _count(usage.get("cache_creation_input_tokens")) or 0  # 照一般輸入價收（見攻防清單 B）
    return fresh + read + written, completion, read


class AnthropicStreamUsage:
    """message_start 帶輸入的用量，message_delta 帶最後的輸出 token 數。兩個都看到才算拿到用量：
    只看到開頭就斷線的話，輸出數量不明，照「拿不到用量」收全額預扣。"""

    def __init__(self):
        self.start: dict | None = None
        self.usage: Usage | None = None

    def feed(self, line: str) -> None:
        event = _sse_json(line)
        if not event:
            return
        if event.get("type") == "message_start" and isinstance(event.get("message"), dict):
            usage = event["message"].get("usage")
            self.start = usage if isinstance(usage, dict) else None
        elif event.get("type") == "message_delta" and self.start is not None:
            delta = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            if _count(delta.get("output_tokens")) is None:
                # message_start 裡的輸出數只是開頭的暫估值（通常是 1），沒有最後的輸出數就不能當成用量
                return
            merged = {**self.start, "output_tokens": delta["output_tokens"]}
            # message_delta 也可能帶輸入相關的數字（累計值）。取兩邊較大的：異常的上游在結尾回報 0，
            # 不能把開頭的輸入用量蓋掉變成免費
            for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                if _count(delta.get(key)) is not None:
                    merged[key] = max(_count(self.start.get(key)) or 0, delta[key])
            self.usage = anthropic_usage_from(merged)


MESSAGES_API = ApiFormat("/v1/messages", lambda data: anthropic_usage_from(data.get("usage")),
                         AnthropicStreamUsage, anthropic=True)


@router.post("/messages")
async def messages(request: Request, auth: KeyAuth = Depends(api_key_auth)):
    state = request.app.state
    body = await read_request(request)
    model, upstream = load_route(state.settings, body.get("model"), "chat")
    if not upstream.anthropic_base_url:
        raise OpenAIError(400, f"模型 {model.name} 的上游沒有 Anthropic 格式的入口（anthropic_base_url）",
                          "unsupported_api")
    body = {k: v for k, v in body.items() if k not in MESSAGES_STRIPPED}
    # Anthropic 的 service_tier 預設就是 auto，拿掉等於沒變；standard_only 只會更便宜或一樣，也拿掉；
    # 其他值照樣拒絕
    if body.get("service_tier") in ("auto", "standard_only"):
        del body["service_tier"]
    reject_unknown_fields(body, MESSAGES_FIELDS, "")
    items = body.get("messages")
    if not isinstance(items, list) or not items:
        raise OpenAIError(400, "messages 必須是非空陣列", "invalid_messages")
    images, cleaned = 0, []
    for message in items:
        if not isinstance(message, dict):
            raise OpenAIError(400, "messages 格式錯誤", "invalid_messages")
        reject_unknown_fields(message, frozenset({"role", "content"}), "messages 裡")
        content, count = check_anthropic_blocks(message.get("content"), "messages")
        cleaned.append({**message, "content": content})
        images += count
    body["messages"] = cleaned
    if "system" in body:
        body["system"], count = check_anthropic_blocks(body["system"], "system")
        images += count
    if images and model.image_tokens <= 0:
        raise OpenAIError(400, "這個模型沒有開放圖片輸入", "images_not_supported")
    if "tools" in body:
        body["tools"] = check_anthropic_tools(body["tools"])
    check_dict(body.get("thinking"), frozenset({"type", "budget_tokens", "display"}), "thinking")
    check_dict(body.get("metadata"), frozenset({"user_id"}), "metadata")
    check_dict(body.get("output_config"), OUTPUT_CONFIG_FIELDS, "output_config")
    stream = stream_flag(body)
    if body.get("max_tokens") is None:
        raise OpenAIError(400, "max_tokens 必填", "invalid_parameter")
    cap = output_cap(body.get("max_tokens"), "max_tokens", model.max_output_tokens)  # 思考的 token 也算在裡面

    content = encode_upstream_body({**body, "model": model.upstream_model, "max_tokens": cap})
    hold = estimate_hold(len(content), images, model.image_tokens, cap, model.input_price_micro,
                         model.output_price_micro, has_tools=bool(body.get("tools")))
    job = await start_job(state, auth, model, upstream, content, hold, stream, MESSAGES_API)
    return await (job.run_stream() if stream else job.run_once())


# ================================================================ 產生圖片

IMAGE_FIELDS = frozenset({
    "model", "prompt", "n", "size", "quality", "style", "response_format", "background", "output_format",
    "output_compression", "moderation", "user",
})


@router.post("/images/generations")
async def generate_image(request: Request, auth: KeyAuth = Depends(api_key_auth)):
    state = request.app.state
    body = await read_request(request)
    model, upstream = load_route(state.settings, body.get("model"), "image")
    reject_unknown_fields(body, IMAGE_FIELDS, "")
    if not isinstance(body.get("prompt"), str) or not body["prompt"]:
        raise OpenAIError(400, "prompt 必須是非空字串", "invalid_parameter")
    n = body.get("n")
    if n is not None and not (type(n) is int and n == 1):
        raise OpenAIError(400, "只支援 n=1", "unsupported_parameter")
    price = model.image_price_micro
    # 用量 = (0, 圖片張數, 0)；按張數計費。size、quality 會影響上游價格，所以售價要抓最貴的組合（見 README）
    # HTTP 200 卻一張圖都沒有：上游有沒有收費不確定，照「拿不到用量」收預扣（站方不承擔風險，同 chat 的規則）
    api = ApiFormat("/images/generations",
                    lambda data: (0, len(data["data"]), 0) if isinstance(data.get("data"), list) and data["data"]
                    else None, NoStream)
    # n 明確寫成 1：沒給 n 時，不是每個上游的預設都是 1，多出來的張數會超過預扣
    content = encode_upstream_body({**body, "model": model.upstream_model, "n": 1})
    job = await start_job(state, auth, model, upstream, content, price, False, api, cost=lambda u: u[1] * price)
    return await job.run_once()


# ================================================================ 文字轉語音

SPEECH_FIELDS = frozenset({"model", "input", "voice", "response_format", "speed", "instructions"})


@router.post("/audio/speech")
async def speech(request: Request, auth: KeyAuth = Depends(api_key_auth)):
    state = request.app.state
    body = await read_request(request)
    model, upstream = load_route(state.settings, body.get("model"), "speech")
    reject_unknown_fields(body, SPEECH_FIELDS, "")
    text, instructions = body.get("input"), body.get("instructions", "")
    if not isinstance(text, str) or not text or not isinstance(instructions, str):
        raise OpenAIError(400, "input 必須是非空字串", "invalid_parameter")
    chars = len(text) + len(instructions)
    cost = calc_cost(chars, 0, model.input_price_micro, 0)  # 按字元數計費，送出前就知道金額
    content = encode_upstream_body({**body, "model": model.upstream_model})
    job = await start_job(state, auth, model, upstream, content, cost, False,
                          ApiFormat("/audio/speech", lambda data: None, NoStream))
    return await job.run_binary((chars, 0, 0))
