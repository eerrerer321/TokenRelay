"""chat/completions 以外的端點：embeddings、responses、messages、產生圖片、文字轉語音。

每個測試結束時，conftest 還會自動檢查：帳本加總 == 餘額、沒有停在 pending 的預扣、名額全部歸還。
"""

import json

from sqlalchemy import select

from app.billing import TOOLS_MARGIN_TOKENS
from app.models import UsageLog
from tests.conftest import auth
from tests.fakes import UPSTREAM_SECRET

CREDIT = 1_000_000  # 1 美元
# add_model 預設：輸入 1 micro/token、輸出 2 micro/token；假上游回 10 + 20 tokens → 實際費用 50
ACTUAL_COST = 10 * 1 + 20 * 2


async def last_usage(h) -> UsageLog:
    async with h.sm() as s:
        return (await s.execute(select(UsageLog).order_by(UsageLog.id.desc()).limit(1))).scalar_one()


async def setup_user(h, **model_kwargs) -> str:
    await h.add_model(**model_kwargs)
    _, key = await h.user_with_key(credit_micro=CREDIT)
    return key


async def post(h, path: str, key: str, body: dict, headers: dict | None = None):
    return await h.client.post(path, content=json.dumps(body).encode(),
                               headers={**auth(key), "Content-Type": "application/json", **(headers or {})})


def sse_events(text: str) -> list[dict]:
    return [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:") and line[5:].strip()]


# ---------------------------------------------------------------- 模型類型不能混用


async def test_model_kind_must_match_api(h):
    await h.add_model("emb", kind="embedding", output_price=None)
    key = await setup_user(h)  # gpt-test：對話模型
    r = await post(h, "/v1/chat/completions", key, {"model": "emb", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "model_not_supported"
    r = await post(h, "/v1/embeddings", key, {"model": "gpt-test", "input": "x"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "model_not_supported"
    r = await post(h, "/v1/responses", key, {"model": "emb", "input": "x"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "model_not_supported"
    assert h.upstream.requests == []
    assert await h.balance() == CREDIT


# ---------------------------------------------------------------- embeddings


async def test_embeddings_charges_input_only(h):
    key = await setup_user(h, name="emb", kind="embedding", output_price=None)
    r = await post(h, "/v1/embeddings", key, {"model": "emb", "input": ["hello", "world"]})
    assert r.status_code == 200, r.text
    assert r.json()["data"][0]["embedding"] == [0.1, 0.2]
    sent = h.upstream.requests[-1]
    assert sent["path"] == "/v1/embeddings" and sent["body"]["model"] == "up-emb"
    assert await h.balance() == CREDIT - 10
    usage = await last_usage(h)
    assert (usage.status, usage.cost_micro, usage.prompt_tokens, usage.completion_tokens) == ("ok", 10, 10, 0)


async def test_embeddings_rejects_unknown_fields_and_empty_input(h):
    key = await setup_user(h, name="emb", kind="embedding", output_price=None)
    r = await post(h, "/v1/embeddings", key, {"model": "emb", "input": "x", "extra_billing": True})
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_parameter"
    r = await post(h, "/v1/embeddings", key, {"model": "emb", "input": []})
    assert r.status_code == 400
    assert h.upstream.requests == []


async def test_embeddings_without_usage_keeps_hold(h):
    key = await setup_user(h, name="emb", kind="embedding", output_price=None)
    h.upstream.mode = "no_usage"
    r = await post(h, "/v1/embeddings", key, {"model": "emb", "input": "hello"})
    assert r.status_code == 200
    usage = await last_usage(h)
    assert usage.status == "no_usage" and usage.cost_micro == usage.hold_micro > 0


# ---------------------------------------------------------------- responses（Codex CLI）


async def test_responses_bills_usage_and_forces_store_off(h):
    key = await setup_user(h, cached_input_price=100_000)
    h.upstream.usage_extra = {"input_tokens_details": {"cached_tokens": 8}}
    r = await post(h, "/v1/responses", key, {
        "model": "gpt-test", "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "store": True, "max_output_tokens": 999999, "tools": [{"type": "function", "name": "f", "parameters": {}}],
        "include": ["reasoning.encrypted_content"],
    })
    assert r.status_code == 200, r.text
    sent = h.upstream.requests[-1]
    assert sent["path"] == "/v1/responses"
    assert sent["body"]["store"] is False  # 共用上游帳號，不能讓上游保存對話
    assert sent["body"]["max_output_tokens"] == 100  # 壓在模型上限
    assert sent["body"]["model"] == "up-gpt-test"
    # 10 個輸入裡 8 個命中快取：2 × 1 + 8 × 0.1 + 20 × 2 = 42.8 → 進位 43
    usage = await last_usage(h)
    assert (usage.status, usage.cost_micro) == ("ok", 43)
    assert await h.balance() == CREDIT - 43


async def test_responses_stream_uses_completed_event(h):
    key = await setup_user(h)
    r = await post(h, "/v1/responses", key, {"model": "gpt-test", "input": "hi", "stream": True})
    assert r.status_code == 200
    assert sse_events(r.text)[-1]["type"] == "response.completed"
    usage = await last_usage(h)
    assert (usage.status, usage.cost_micro) == ("ok", ACTUAL_COST)


async def test_responses_stream_without_completed_event_keeps_hold(h):
    key = await setup_user(h)
    h.upstream.mode = "cut_stream"  # 開頭的 response.created 裡 usage 是 null，不能當成用量
    r = await post(h, "/v1/responses", key, {"model": "gpt-test", "input": "hi", "stream": True})
    assert r.status_code == 200
    usage = await last_usage(h)
    assert usage.status == "no_usage" and usage.cost_micro == usage.hold_micro > ACTUAL_COST


async def test_responses_rejects_risky_features(h):
    key = await setup_user(h)
    cases = [
        {"previous_response_id": "resp_someone_elses"},  # 讀取上游保存的對話（可能是別人的）
        {"background": True},
        {"service_tier": "priority"},  # 上游加價
        {"tools": [{"type": "web_search"}]},  # 上游代執行、另外計費
        {"tools": [{"type": "code_interpreter", "container": {"type": "auto"}}]},
        {"input": [{"role": "user", "content": [{"type": "input_file", "file_id": "file-1"}]}]},
        {"input": [{"type": "item_reference", "id": "msg_1"}]},
        {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "https://x/y.png"}]}]},  # 模型不收圖
        {"include": ["file_search_call.results"]},
        {"max_output_tokens": 0},
        # 工具結果的 output 也可以是內容陣列：一樣不能夾帶上游帳號裡的檔案、不收圖的模型不能夾圖
        {"input": [{"type": "function_call_output", "call_id": "c1",
                    "output": [{"type": "input_file", "file_id": "file-someone-elses"}]}]},
        {"input": [{"type": "custom_tool_call_output", "call_id": "c1",
                    "output": [{"type": "input_image", "image_url": "https://x/y.png"}]}]},
        {"input": [{"type": ["message"], "role": "user", "content": "hi"}]},  # 型別怪異：要 400 不是 500
        {"include": [{}]},
        {"include": [["reasoning.encrypted_content"]]},
        # 內容區塊也有欄位白名單：不能在其他類型的區塊偷塞 file_id 之類的欄位
        {"input": [{"role": "user", "content": [{"type": "input_text", "text": "hi", "file_id": "file-1"}]}]},
    ]
    for extra in cases:
        r = await post(h, "/v1/responses", key, {"model": "gpt-test", "input": "hi", **extra})
        assert r.status_code == 400, (extra, r.text)
    assert h.upstream.requests == []
    assert await h.balance() == CREDIT


async def test_responses_strips_fields_tools_send_by_default(h):
    """Codex 預設會送、但站方不轉送的欄位：拿掉後照常轉發，不回錯誤。"""
    key = await setup_user(h)
    r = await post(h, "/v1/responses", key, {"model": "gpt-test", "input": "hi", "prompt_cache_retention": "24h",
                                             "service_tier": "auto"})
    assert r.status_code == 200, r.text
    sent = h.upstream.requests[-1]["body"]
    assert "prompt_cache_retention" not in sent and "service_tier" not in sent


async def test_responses_counts_images_in_hold(h):
    key = await setup_user(h, image_tokens=5000)
    r = await post(h, "/v1/responses", key, {"model": "gpt-test", "input": [{"role": "user", "content": [
        {"type": "input_image", "image_url": "https://x/a.png"}, {"type": "input_image", "image_url": "https://x/b.png"},
    ]}]})
    assert r.status_code == 200
    assert (await last_usage(h)).hold_micro > 2 * 5000  # 兩張圖各算 5000 tokens 的最壞情況
    # 工具結果裡的圖片也要算進預扣
    r = await post(h, "/v1/responses", key, {"model": "gpt-test", "input": [{
        "type": "function_call_output", "call_id": "c1",
        "output": [{"type": "input_image", "image_url": "https://x/a.png"}] * 3,
    }]})
    assert r.status_code == 200
    assert (await last_usage(h)).hold_micro > 3 * 5000


async def tools_hold_increase(h, key: str, path: str, body: dict, tools: list) -> int:
    """同一個請求帶 tools 與不帶 tools，預扣差多少。"""
    holds = []
    for extra in ({}, {"tools": tools}):
        r = await post(h, path, key, {**body, **extra})
        assert r.status_code == 200, r.text
        holds.append((await last_usage(h)).hold_micro)
    return holds[1] - holds[0]


async def test_responses_and_messages_hold_extra_for_tools(h):
    """上游自動加的工具說明不在請求裡，帶 tools 的預扣至少要多 TOOLS_MARGIN_TOKENS 的輸入費
    （tools 本身只有幾十 byte，差額遠小於 1024，所以少了這段預扣就會失敗）。"""
    key = await setup_user(h)
    margin = TOOLS_MARGIN_TOKENS * 1  # add_model 預設輸入 1 micro/token
    assert await tools_hold_increase(h, key, "/v1/responses", {"model": "gpt-test", "input": "hi"},
                                     [{"type": "function", "name": "f", "parameters": {}}]) >= margin
    assert await tools_hold_increase(h, key, "/v1/messages", claude_body(),
                                     [{"name": "f", "input_schema": {"type": "object"}}]) >= margin


async def test_responses_and_messages_hide_upstream_cost(h):
    """Responses 串流的用量包在 response 裡、Anthropic 串流開頭包在 message 裡，也都要拿掉上游成本。"""
    key = await setup_user(h)
    h.upstream.usage_extra = {"cost": 0.00412, "opencode": {"allowance": 0.00412}}
    cases = [("/v1/responses", {"model": "gpt-test", "input": "hi"}), ("/v1/messages", claude_body())]
    for path, body in cases:
        for stream in (False, True):
            r = await post(h, path, key, {**body, "stream": stream})
            assert r.status_code == 200, r.text
            assert "cost" not in r.text and "allowance" not in r.text, (path, stream, r.text)
            assert "input_tokens" in r.text
            assert (await last_usage(h)).status == "ok"


async def test_responses_needs_upstream_support(h):
    key = await setup_user(h, upstream="plain")  # plain 上游沒設 responses = true
    r = await post(h, "/v1/responses", key, {"model": "gpt-test", "input": "hi"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_api"
    assert h.upstream.requests == []


# ---------------------------------------------------------------- messages（Claude Code）


def claude_body(**extra) -> dict:
    return {"model": "gpt-test", "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}], **extra}


async def test_messages_with_x_api_key_and_anthropic_cache_usage(h):
    key = await setup_user(h, cached_input_price=100_000)
    h.upstream.anthropic_cache_read, h.upstream.anthropic_cache_write = 100, 50
    r = await h.client.post("/v1/messages", json=claude_body(),
                            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                     "anthropic-beta": "context-1m-2025-08-07"})
    assert r.status_code == 200, r.text
    assert r.json()["content"][0]["text"] == "Hello"
    sent = h.upstream.requests[-1]
    assert sent["path"] == "/anthropic/v1/messages"
    assert sent["headers"]["x-api-key"] == UPSTREAM_SECRET
    assert sent["headers"]["anthropic-version"] == "2023-06-01"
    assert "authorization" not in sent["headers"]
    assert "anthropic-beta" not in sent["headers"]  # beta 功能可能改變計費，不轉送
    # 輸入 = 10（沒快取）+ 100（讀快取）+ 50（寫快取，照一般輸入價）= 160，其中 100 用快取價
    # (60 × 1 + 100 × 0.1 + 20 × 2) = 110
    usage = await last_usage(h)
    assert (usage.status, usage.cost_micro, usage.prompt_tokens) == ("ok", 110, 160)


async def test_messages_strips_cache_control_everywhere(h):
    key = await setup_user(h)
    cc = {"type": "ephemeral"}
    r = await post(h, "/v1/messages", key, claude_body(
        system=[{"type": "text", "text": "sys", "cache_control": cc}],
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": cc}]},
                  {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "f", "input": {}}]},
                  {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "cache_control": cc,
                                                "content": [{"type": "text", "text": "ok", "cache_control": cc}]}]}],
        tools=[{"name": "f", "description": "d", "input_schema": {"type": "object"}, "cache_control": cc}],
    ))
    assert r.status_code == 200, r.text
    assert "cache_control" not in h.upstream.requests[-1]["raw"].decode()


async def test_messages_caps_max_tokens_and_requires_it(h):
    key = await setup_user(h)
    r = await post(h, "/v1/messages", key, claude_body(max_tokens=10**9))
    assert r.status_code == 200
    assert h.upstream.requests[-1]["body"]["max_tokens"] == 100
    body = claude_body()
    del body["max_tokens"]
    r = await post(h, "/v1/messages", key, body)
    assert r.status_code == 400
    # Claude Code 看得懂的 Anthropic 錯誤格式
    assert r.json()["type"] == "error" and "max_tokens" in r.json()["error"]["message"]


async def test_messages_errors_use_anthropic_format(h):
    await h.add_model()
    r = await h.client.post("/v1/messages", json=claude_body(), headers={"x-api-key": "sk-wrong"})
    assert r.status_code == 401
    assert r.json()["type"] == "error" and r.json()["error"]["type"] == "authentication_error"


async def test_messages_rejects_server_tools_and_documents(h):
    key = await setup_user(h)
    cases = [
        {"tools": [{"type": "web_search_20250305", "name": "web_search"}]},  # 上游代執行、另外計費
        {"tools": [{"type": "code_execution_20250522", "name": "code_execution"}]},
        {"messages": [{"role": "user", "content": [{"type": "document", "source": {"type": "url", "url": "x"}}]}]},
        {"messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "url", "url": "x"}}]}]},  # 不收圖
        {"mcp_servers": [{"type": "url", "url": "https://evil"}]},
        {"container": "abc"},
        {"service_tier": "priority"},  # auto、standard_only 會被拿掉；其他值拒絕
        {"speed": "fast"},  # 快速模式：上游兩倍價
        # tool_result 裡只能放文字和圖片，不能再巢狀 tool_result
        {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": [
            {"type": "tool_result", "tool_use_id": "y", "content": "z"}]}]}]},
        {"output_config": {"effort": "high", "task_budget": {"type": "tokens", "total": 20000}}},  # beta 功能
    ]
    for extra in cases:
        r = await post(h, "/v1/messages", key, claude_body(**extra))
        assert r.status_code == 400, (extra, r.text)
    assert h.upstream.requests == []
    assert await h.balance() == CREDIT


async def test_messages_image_source_is_allowlisted(h):
    key = await setup_user(h, image_tokens=2000)  # 這個模型收圖，確認擋下來的原因是 source 的欄位
    ok = {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}
    assert (await post(h, "/v1/messages", key, claude_body(
        messages=[{"role": "user", "content": [ok]}]))).status_code == 200
    for source in ({"type": "url", "url": "https://x/y.png", "file_id": "file-1"}, {"type": "file", "file_id": "f"}):
        r = await post(h, "/v1/messages", key, claude_body(
            messages=[{"role": "user", "content": [{"type": "image", "source": source}]}]))
        assert r.status_code == 400, source
    assert len(h.upstream.requests) == 1


async def test_messages_accepts_fields_claude_code_sends(h):
    """Claude Code 正常會送、不影響單價的欄位要能通過；context_management 要 beta 標頭，直接拿掉。"""
    key = await setup_user(h)
    r = await post(h, "/v1/messages", key, claude_body(
        output_config={"effort": "high"},
        thinking={"type": "adaptive", "display": "summarized"},
        tools=[{"name": "f", "input_schema": {"type": "object"}, "strict": True, "eager_input_streaming": True}],
        context_management={"edits": [{"type": "clear_tool_uses_20250919"}]},
        service_tier="auto",
    ))
    assert r.status_code == 200, r.text
    sent = h.upstream.requests[-1]["body"]
    assert sent["output_config"] == {"effort": "high"} and sent["tools"][0]["strict"] is True
    assert "context_management" not in sent and "service_tier" not in sent


async def test_messages_stream_end_cannot_zero_the_input(h):
    key = await setup_user(h)
    h.upstream.mode = "delta_zero_input"
    r = await post(h, "/v1/messages", key, claude_body(stream=True))
    assert r.status_code == 200
    usage = await last_usage(h)
    assert (usage.status, usage.prompt_tokens, usage.cost_micro) == ("ok", 10, ACTUAL_COST)


async def test_messages_stream_needs_both_start_and_delta(h):
    key = await setup_user(h)
    r = await post(h, "/v1/messages", key, claude_body(stream=True))
    assert r.status_code == 200
    assert [e["type"] for e in sse_events(r.text)][-1] == "message_stop"
    usage = await last_usage(h)
    assert (usage.status, usage.cost_micro) == ("ok", ACTUAL_COST)

    # 只有 message_start（輸出只寫 1），或結尾的 message_delta 沒附輸出數 → 收全額預扣
    for mode in ("cut_stream", "delta_without_usage"):
        h.upstream.mode = mode
        r = await post(h, "/v1/messages", key, claude_body(stream=True))
        assert r.status_code == 200
        usage = await last_usage(h)
        assert usage.status == "no_usage" and usage.cost_micro == usage.hold_micro, mode


async def test_messages_needs_anthropic_upstream(h):
    key = await setup_user(h, upstream="plain")
    r = await post(h, "/v1/messages", key, claude_body())
    assert r.status_code == 400 and r.json()["error"]["message"].find("anthropic_base_url") >= 0
    assert h.upstream.requests == []


# ---------------------------------------------------------------- 產生圖片


async def test_image_generation_charges_per_image(h):
    key = await setup_user(h, name="painter", kind="image", input_price=None, output_price=None, image_price=40_000)
    r = await post(h, "/v1/images/generations", key, {"model": "painter", "prompt": "a cat", "size": "1024x1024"})
    assert r.status_code == 200, r.text
    assert h.upstream.requests[-1]["body"]["model"] == "up-painter"
    assert h.upstream.requests[-1]["body"]["n"] == 1  # 沒給 n 也明確寫 1，不靠上游的預設值
    usage = await last_usage(h)
    assert (usage.status, usage.hold_micro, usage.cost_micro) == ("ok", 40_000, 40_000)

    h.upstream.image_count = 2  # 上游多給了圖：照實際張數收
    r = await post(h, "/v1/images/generations", key, {"model": "painter", "prompt": "a cat"})
    assert (await last_usage(h)).cost_micro == 80_000
    assert await h.balance() == CREDIT - 120_000


async def test_image_generation_without_images_keeps_hold(h):
    """HTTP 200 卻沒有任何圖片：上游有沒有收費不確定，跟其他 API 一樣照「拿不到用量」收預扣。"""
    key = await setup_user(h, name="painter", kind="image", input_price=None, output_price=None, image_price=40_000)
    h.upstream.image_count = 0
    r = await post(h, "/v1/images/generations", key, {"model": "painter", "prompt": "a cat"})
    assert r.status_code == 200
    usage = await last_usage(h)
    assert (usage.status, usage.cost_micro) == ("no_usage", 40_000)


async def test_image_generation_rejects_n_and_refunds_upstream_errors(h):
    key = await setup_user(h, name="painter", kind="image", input_price=None, output_price=None, image_price=40_000)
    r = await post(h, "/v1/images/generations", key, {"model": "painter", "prompt": "cats", "n": 10})
    assert r.status_code == 400 and h.upstream.requests == []
    h.upstream.mode = "error400"
    r = await post(h, "/v1/images/generations", key, {"model": "painter", "prompt": "cats"})
    assert r.status_code == 400
    assert UPSTREAM_SECRET not in r.text
    assert await h.balance() == CREDIT


# ---------------------------------------------------------------- 文字轉語音


async def test_speech_returns_audio_and_charges_per_character(h):
    # 每 100 萬字元 2 美元
    key = await setup_user(h, name="tts", kind="speech", input_price=2_000_000, output_price=None)
    r = await post(h, "/v1/audio/speech", key, {"model": "tts", "input": "你好，世界", "voice": "alloy"})
    assert r.status_code == 200, r.text
    assert r.content == b"ID3-fake-mp3"
    assert r.headers["content-type"] == "audio/mpeg"
    usage = await last_usage(h)
    assert (usage.status, usage.cost_micro, usage.prompt_tokens) == ("ok", 10, 5)  # 5 字 × 2 micro


async def test_speech_masks_upstream_key_in_200_errors(h):
    key = await setup_user(h, name="tts", kind="speech", input_price=2_000_000, output_price=None)
    h.upstream.mode = "leak200"
    r = await post(h, "/v1/audio/speech", key, {"model": "tts", "input": "hi", "voice": "alloy"})
    assert UPSTREAM_SECRET not in r.text


async def test_speech_refunds_on_upstream_error(h):
    key = await setup_user(h, name="tts", kind="speech", input_price=2_000_000, output_price=None)
    h.upstream.mode = "error500"
    r = await post(h, "/v1/audio/speech", key, {"model": "tts", "input": "hi", "voice": "alloy"})
    assert r.status_code == 502
    assert await h.balance() == CREDIT
