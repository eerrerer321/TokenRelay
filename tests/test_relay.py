"""轉發與計費：正常扣款，以及各種「想白嫖」的攻擊手法。

每個測試結束時，conftest 還會自動檢查：帳本加總 == 餘額、沒有停在 pending 的預扣、名額全部歸還。
"""

import asyncio
import json

import pytest
from sqlalchemy import select

from app import relay
from app.billing import TOOLS_MARGIN_TOKENS, HoldRejected, calc_cost, estimate_hold, place_hold
from app.config import ModelConfig, UpstreamConfig
from app.main import create_app
from app.models import ApiKey, UsageLog
from app.relay import encode_upstream_body, prepare_upstream_body
from tests.conftest import auth
from tests.fakes import UPSTREAM_SECRET

CREDIT = 1_000_000  # 1 美元
# add_model 預設：輸入 1 micro/token、輸出 2 micro/token；假上游回 10 + 20 tokens → 實際費用 50
ACTUAL_COST = 10 * 1 + 20 * 2


def chat_body(**extra) -> bytes:
    return json.dumps({"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}], **extra}).encode()


def expected_hold(raw: bytes, *, image_tokens: int = 0, max_output_tokens: int = 100) -> int:
    """用跟站方相同的步驟算出預扣金額（公式本身另有 test_hold_formula 單獨驗證）。"""
    model = ModelConfig(name="gpt-test", upstream="fake", upstream_model="up-gpt-test", input_price_micro=1_000_000,
                        output_price_micro=2_000_000, max_output_tokens=max_output_tokens,
                        max_tokens_field="max_tokens", image_tokens=image_tokens)
    body, cap, _stream, images = prepare_upstream_body(json.loads(raw), model)
    return estimate_hold(len(encode_upstream_body(body)), images, image_tokens, cap, 1_000_000, 2_000_000,
                         has_tools=bool(body.get("tools")))


async def post_chat(h, key: str, raw):
    return await h.client.post("/v1/chat/completions", content=raw,
                               headers={**auth(key), "Content-Type": "application/json"})


async def last_usage(h) -> UsageLog:
    async with h.sm() as s:
        return (await s.execute(select(UsageLog).order_by(UsageLog.id.desc()).limit(1))).scalar_one()


async def setup_user(h, credit: int = CREDIT, **model_kwargs) -> str:
    await h.add_model(**model_kwargs)
    _, key = await h.user_with_key(credit_micro=credit)
    return key


async def wait_until(condition, timeout: float = 10.0) -> None:
    """等條件成立或逾時。逾時不報錯，交給後面的斷言判斷結果。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition() and loop.time() < deadline:
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------- 計費公式


def test_cost_rounds_up_so_tiny_requests_are_never_free():
    # 每 1M tokens 只要 1 micro-USD，送 10 個 token → 精確值 0.00001 micro，必須進位成 1
    assert calc_cost(10, 0, 1, 1) == 1
    assert calc_cost(0, 0, 1, 1) == 0
    assert calc_cost(1_000_000, 1_000_000, 3_000_000, 15_000_000) == 18_000_000


def test_cost_uses_cached_price_only_for_cached_tokens():
    # 100 個輸入裡 80 個命中快取：20 × 1 + 80 × 0.1 = 28 micro
    assert calc_cost(100, 0, 1_000_000, 0, cached_tokens=80, cached_input_price_micro=100_000) == 28
    assert calc_cost(100, 0, 1_000_000, 0, cached_tokens=80) == 100  # 沒設快取價 → 照輸入價
    # 命中數比總輸入還多（上游回報有誤）→ 最多只算到總輸入，不會變成負數
    assert calc_cost(100, 0, 1_000_000, 0, cached_tokens=500, cached_input_price_micro=0) == 0


def test_hold_formula():
    # 100 bytes + 64 margin + 2 張圖 × 每張 1500，輸出上限 50；價格 1 / 2 micro per token
    assert estimate_hold(100, 2, 1500, 50, 1_000_000, 2_000_000) == (100 + 64 + 3000) * 1 + 50 * 2


# ---------------------------------------------------------------- 正常流程


async def test_models_list_requires_key(h):
    await h.add_model()
    assert (await h.client.get("/v1/models")).status_code == 401
    _, key = await h.user_with_key()
    r = await h.client.get("/v1/models", headers=auth(key))
    assert [m["id"] for m in r.json()["data"]] == ["gpt-test"]


async def test_chat_charges_actual_usage_and_uses_station_key(h):
    key = await setup_user(h)
    r = await post_chat(h, key, chat_body())
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "Hello"
    assert await h.balance() == CREDIT - ACTUAL_COST

    sent = h.upstream.requests[-1]
    assert sent["body"]["model"] == "up-gpt-test"
    assert sent["headers"]["authorization"] == f"Bearer {UPSTREAM_SECRET}"  # 用站方的 key，不是使用者的
    usage = await last_usage(h)
    assert (usage.status, usage.cost_micro, usage.prompt_tokens, usage.completion_tokens) == ("ok", 50, 10, 20)


async def test_stream_forces_usage_and_charges_actual(h):
    key = await setup_user(h)
    r = await post_chat(h, key, chat_body(stream=True, stream_options={"include_usage": False}))
    assert r.status_code == 200
    assert "Hel" in r.text and "[DONE]" in r.text
    assert h.upstream.requests[-1]["body"]["stream_options"]["include_usage"] is True
    assert await h.balance() == CREDIT - ACTUAL_COST
    assert (await last_usage(h)).status == "ok"


CACHE_REPORTS = {  # 假上游回報「10 個輸入裡 8 個命中快取」的各家寫法
    "openai": {"prompt_tokens_details": {"cached_tokens": 8}},
    "deepseek": {"prompt_cache_hit_tokens": 8, "prompt_cache_miss_tokens": 2},
}


@pytest.mark.parametrize("stream", [False, True], ids=["json", "stream"])
@pytest.mark.parametrize("report", CACHE_REPORTS)
async def test_cached_input_tokens_use_cached_price(h, report, stream):
    # 快取價 0.1 micro/token：2 × 1 + 8 × 0.1 + 20 × 2 = 42.8 → 進位 43
    key = await setup_user(h, cached_input_price=100_000)
    h.upstream.usage_extra = CACHE_REPORTS[report]
    assert (await post_chat(h, key, chat_body(stream=stream))).status_code == 200
    assert (await last_usage(h)).cost_micro == 43
    assert await h.balance() == CREDIT - 43


async def test_cache_hits_without_cached_price_are_full_price(h):
    key = await setup_user(h)  # 沒設定 cached_input_price
    h.upstream.usage_extra = CACHE_REPORTS["openai"]
    await post_chat(h, key, chat_body())
    assert await h.balance() == CREDIT - ACTUAL_COST


@pytest.mark.parametrize("extra", [
    {"prompt_tokens_details": {"cached_tokens": 11}},  # 比輸入總數還多
    {"prompt_tokens_details": {"cached_tokens": -1}},
    {"prompt_tokens_details": {"cached_tokens": "8"}},
    {"prompt_tokens_details": "8"},
    {"prompt_cache_hit_tokens": True},
], ids=["more-than-prompt", "negative", "text", "not-object", "bool"])
async def test_unreasonable_cache_numbers_charge_full_input_price(h, extra):
    key = await setup_user(h, cached_input_price=0)  # 快取免費：如果誤信這些數字，費用會明顯變少
    h.upstream.usage_extra = extra
    assert (await post_chat(h, key, chat_body())).status_code == 200
    assert (await last_usage(h)).cost_micro == ACTUAL_COST


async def test_image_request_holds_model_specific_image_tokens(h):
    key = await setup_user(h, image_tokens=5000)
    h.upstream.mode = "no_usage"  # 拿不到用量 → 收全額預扣，正好用來檢查預扣有算進圖片
    raw = chat_body(messages=[{"role": "user", "content": [
        {"type": "text", "text": "這是什麼"},
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png", "detail": "high"}},
    ]}])
    assert (await post_chat(h, key, raw)).status_code == 200
    hold = expected_hold(raw, image_tokens=5000)
    assert hold > 5000
    assert await h.balance() == CREDIT - hold


# ---------------------------------------------------------------- 攻擊：沒錢也想用


async def test_insufficient_balance_is_rejected_before_calling_upstream(h):
    key = await setup_user(h, credit=10)
    r = await post_chat(h, key, chat_body())
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "insufficient_balance"
    assert h.upstream.requests == []
    assert await h.balance() == 10


async def test_concurrent_requests_cannot_overspend(h):
    """餘額只夠 2 次預扣時，同時打 8 個請求，只能有 2 個成功，餘額不能變負。

    注意：SQLite 會把交易排隊執行，這個測試在 SQLite 上無法揭露「先查再扣」寫法的競態；
    要真正驗證，請設定 TEST_DATABASE_URL 在 PostgreSQL 上跑。
    """
    raw = chat_body()
    credit = expected_hold(raw) * 2 + expected_hold(raw) // 2
    key = await setup_user(h, credit=credit)
    h.app.state.limiter.limit = 8  # 這裡只驗證預扣，不讓「同時請求數上限」先擋掉
    # 成功預扣的請求停在上游、不結算，餘額就不會被退回：8 個預扣一定是在同一段時間內競爭
    h.upstream.gate = asyncio.Event()
    tasks = [asyncio.create_task(post_chat(h, key, raw)) for _ in range(8)]
    # 等到「2 個停在上游、6 個已被拒」。如果預扣會透支，停在上游的會超過 2 個，這裡就等不到，
    # 逾時後放行，下面的斷言會失敗
    await wait_until(lambda: len(h.upstream.requests) >= 2 and sum(t.done() for t in tasks) >= 6)
    h.upstream.gate.set()

    responses = await asyncio.gather(*tasks)
    assert sorted(r.status_code for r in responses) == [200, 200] + [402] * 6
    assert len(h.upstream.requests) == 2
    assert await h.balance() == credit - 2 * ACTUAL_COST


async def test_per_user_concurrency_limit(h):
    key = await setup_user(h)
    h.app.state.limiter.limit = 1
    h.upstream.gate = asyncio.Event()  # 第一個請求停在上游，佔住名額
    tasks = [asyncio.create_task(post_chat(h, key, chat_body())) for _ in range(2)]
    await wait_until(lambda: len(h.upstream.requests) >= 1 and sum(t.done() for t in tasks) >= 1)
    h.upstream.gate.set()
    responses = await asyncio.gather(*tasks)
    assert sorted(r.status_code for r in responses) == [200, 429]
    assert (await post_chat(h, key, chat_body())).status_code == 200  # 名額有正確釋放


async def test_key_revoked_before_hold_cannot_spend(h):
    """key 驗證通過後、預扣之前被停用（例如請求還在慢慢上傳）→ 不能再花到錢。"""
    await setup_user(h)
    async with h.sm.begin() as s:
        api_key = (await s.execute(select(ApiKey))).scalar_one()
        api_key.is_active = False
    with pytest.raises(HoldRejected) as exc:
        await place_hold(h.sm, user_id=api_key.user_id, api_key_id=api_key.id, model="gpt-test",
                         stream=False, hold=100)
    assert exc.value.reason == "key_revoked"
    assert await h.balance() == CREDIT


async def test_negative_hold_is_refused(h):
    """站長把模型上限填成負數時，預扣會變成「先送額度」。計費層要直接拒絕。"""
    await setup_user(h)
    async with h.sm() as s:
        api_key = (await s.execute(select(ApiKey))).scalar_one()
    with pytest.raises(ValueError):
        await place_hold(h.sm, user_id=api_key.user_id, api_key_id=api_key.id, model="gpt-test",
                         stream=False, hold=-1000)
    assert await h.balance() == CREDIT


async def test_holds_left_pending_by_a_crash_are_settled_on_startup(h):
    """請求途中伺服器當機 → 那筆預扣停在 pending。下次啟動要自動結算，不能永遠卡著。"""
    await setup_user(h)
    async with h.sm() as s:
        api_key = (await s.execute(select(ApiKey))).scalar_one()
    await place_hold(h.sm, user_id=api_key.user_id, api_key_id=api_key.id, model="gpt-test", stream=True, hold=500)

    restarted = create_app(h.app.state.settings, start_watcher=False)  # 模擬重啟：同一個資料庫再啟動一次
    async with restarted.router.lifespan_context(restarted):
        pass
    usage = await last_usage(h)
    # 跟「拿不到用量」同一個規則：上游可能已經在算錢，收全額預扣
    assert (usage.status, usage.cost_micro) == ("abandoned", 500)
    assert await h.balance() == CREDIT - 500


@pytest.mark.parametrize("fail_after_commit", [False, True])
async def test_settle_is_retried_without_double_booking(h, monkeypatch, fail_after_commit):
    """結算時資料庫短暫出錯 → 要重試；就算錯誤發生在「其實已經寫進去」之後，重試也不能重複記帳。"""
    key = await setup_user(h)
    real_settle = relay.settle
    calls = []

    async def flaky_settle(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            if fail_after_commit:
                await real_settle(*args, **kwargs)
            raise ConnectionError("模擬資料庫連線中斷")
        return await real_settle(*args, **kwargs)

    monkeypatch.setattr(relay, "settle", flaky_settle)
    assert (await post_chat(h, key, chat_body())).status_code == 200
    assert len(calls) == 2
    assert await h.balance() == CREDIT - ACTUAL_COST  # 帳本是否重複，由 conftest 的不變式檢查


async def test_deepseek_reasoning_content_is_forwarded(h):
    """DeepSeek 思考模式的工具多輪對話，要把上一輪的 reasoning_content 帶回去，不能被白名單擋掉。"""
    key = await setup_user(h)
    messages = [
        {"role": "user", "content": "幾點？"},
        {"role": "assistant", "content": None, "reasoning_content": "要查時間",
         "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "now", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "12:00"},
    ]
    r = await post_chat(h, key, chat_body(messages=messages))
    assert r.status_code == 200, r.text
    assert h.upstream.requests[-1]["body"]["messages"][1]["reasoning_content"] == "要查時間"


async def test_unknown_model_is_rejected_without_charge(h):
    key = await setup_user(h)
    r = await post_chat(h, key, chat_body(model="gpt-free"))
    assert r.status_code == 404
    assert h.upstream.requests == []
    assert await h.balance() == CREDIT


@pytest.mark.parametrize("extra", [
    # 會讓上游多收錢、或改用更貴的模型
    {"n": 2},
    {"n": True},
    {"models": ["openai/o1-pro"]},  # OpenRouter 備援模型：實際跑的可能是更貴的模型
    {"service_tier": "priority"},  # 更貴的服務等級
    {"plugins": [{"id": "web"}]},
    {"web_search_options": {}},
    {"prediction": {"type": "content", "content": "x"}},
    {"audio": {"voice": "alloy", "format": "wav"}},
    {"modalities": ["text", "audio"]},
    {"tools": [{"type": "web_search"}]},  # 上游代執行、另外計費的工具
    {"tools": [{"type": "function", "function": {"name": "f"}, "cache_control": {"type": "ephemeral"}}]},
    {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi",
                                                "cache_control": {"type": "ephemeral"}}]}]},
    {"messages": [{"role": "user", "content": "hi", "cache_control": {"type": "ephemeral"}}]},
    {"messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "x"}}]}]},
    {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://x/a.png"}}]}]},
    {"stream": True, "stream_options": {"include_usage": True, "extra": 1}},
    # 格式錯誤
    {"messages": [{"role": "user", "content": [{"type": ["text"], "text": "hi"}]}]},  # 之前會變成 500
    {"max_tokens": 0},
    {"max_tokens": True},
    {"max_tokens": "999"},
    {"stream": "yes"},
    {"messages": []},
])
async def test_cost_amplifying_or_malformed_params_are_rejected(h, extra):
    key = await setup_user(h)  # 預設模型 image_tokens=0 → 不接受圖片
    r = await post_chat(h, key, chat_body(**extra))
    assert r.status_code == 400, r.text
    assert h.upstream.requests == []
    assert await h.balance() == CREDIT


@pytest.mark.parametrize("raw", [
    b'{"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}], "temperature": NaN}',
    b'{"model": "gpt-test", "messages": ' + b"[" * 100_000 + b"]" * 100_000 + b"}",
    b'{"model": "gpt-test\\ud800", "messages": [{"role": "user", "content": "hi"}]}',
    b'{"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}], "x\\ud800": 1}',
    b'{"model": "gpt-test", "messages": [{"role": "user", "content": "hi\\udfff"}]}',
], ids=["nan", "deep-nesting", "surrogate-in-model", "surrogate-in-field-name", "surrogate-in-content"])
async def test_unforwardable_json_is_rejected_before_hold(h, raw):
    """Python 讀得進來、卻無法轉送給上游的內容，不能變成「扣了錢卻沒送出去」，也不能變成 500。"""
    key = await setup_user(h)
    r = await post_chat(h, key, raw)
    assert r.status_code == 400, r.text
    assert h.upstream.requests == []
    async with h.sm() as s:  # 「先預扣再退款」也不行：根本不該有用量紀錄
        assert (await s.execute(select(UsageLog))).first() is None
    assert await h.balance() == CREDIT


async def test_max_tokens_is_normalized_to_the_models_field(h):
    await h.add_model()
    await h.add_model("o-test", max_tokens_field="max_completion_tokens")
    _, key = await h.user_with_key(credit_micro=CREDIT)

    await post_chat(h, key, chat_body())
    assert h.upstream.requests[-1]["body"]["max_tokens"] == 100  # 沒填 → 補上模型上限
    await post_chat(h, key, chat_body(max_tokens=999_999))
    assert h.upstream.requests[-1]["body"]["max_tokens"] == 100  # 填太大 → 壓回上限
    # 用了上游不認得的欄位：改寫成模型設定的欄位，避免上游改用自己的（可能很大的）預設值
    await post_chat(h, key, chat_body(max_completion_tokens=30))
    sent = h.upstream.requests[-1]["body"]
    assert sent["max_tokens"] == 30 and "max_completion_tokens" not in sent
    await post_chat(h, key, chat_body(max_tokens=50, max_completion_tokens=20))
    sent = h.upstream.requests[-1]
    assert sent["body"]["max_tokens"] == 20 and "max_completion_tokens" not in sent["body"]  # 兩個都填 → 取小的
    # 預扣也要用同一個上限，而且是按「上游實際收到的 byte 數」算：
    # (byte 數 + 64) × 1 micro + 20 × 2 micro（不經過站方的計算函式，獨立核對）
    assert (await last_usage(h)).hold_micro == len(sent["raw"]) + 64 + 20 * 2
    await post_chat(h, key, chat_body(model="o-test", max_tokens=40))
    sent = h.upstream.requests[-1]["body"]
    assert sent["max_completion_tokens"] == 40 and "max_tokens" not in sent


async def test_tiny_request_is_charged_at_least_one_micro(h):
    key = await setup_user(h, input_price=1, output_price=1)
    h.upstream.prompt_tokens, h.upstream.completion_tokens = 10, 0
    assert (await post_chat(h, key, chat_body())).status_code == 200
    assert await h.balance() == CREDIT - 1


async def test_oversized_body_is_rejected(h):
    key = await setup_user(h)
    assert (await post_chat(h, key, chat_body(padding="x" * 1_000_001))).status_code == 413
    assert h.upstream.requests == []


async def test_oversized_chunked_body_without_content_length_is_rejected(h):
    """不帶 Content-Length、分段上傳的大請求，也要在讀到上限時就中止。"""
    key = await setup_user(h)

    async def chunks():
        yield b'{"model": "gpt-test", "messages": [{"role": "user", "content": "'
        for _ in range(20):
            yield b"x" * 60_000
        yield b'"}]}'

    r = await post_chat(h, key, chunks())
    assert r.status_code == 413
    assert h.upstream.requests == []


# ---------------------------------------------------------------- 攻擊：讓站方拿不到用量


async def test_missing_usage_charges_full_hold(h):
    key = await setup_user(h)
    h.upstream.mode = "no_usage"
    raw = chat_body()
    assert (await post_chat(h, key, raw)).status_code == 200
    usage = await last_usage(h)
    assert (usage.status, usage.cost_micro) == ("no_usage", expected_hold(raw))
    assert usage.settled_at is not None
    assert await h.balance() == CREDIT - expected_hold(raw)


async def test_stream_without_usage_charges_full_hold(h):
    key = await setup_user(h)
    h.upstream.mode = "no_usage"
    raw = chat_body(stream=True)
    assert (await post_chat(h, key, raw)).status_code == 200
    usage = await last_usage(h)
    assert (usage.status, usage.cost_micro) == ("no_usage", expected_hold(raw))
    assert usage.settled_at is not None
    assert await h.balance() == CREDIT - expected_hold(raw)


async def call_and_disconnect(h, key: str, raw: bytes, spec_version: str) -> None:
    """直接呼叫 ASGI app，模擬使用者看到第一段內容後就斷線。"""
    received = False
    first_chunk_sent = asyncio.Event()

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await first_chunk_sent.wait()
        return {"type": "http.disconnect"}  # 2.3：伺服器偵測到使用者斷線

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            if spec_version == "2.4":
                raise OSError("client went away")  # 2.4：寫第一段資料時發現連線已斷
            first_chunk_sent.set()
        await asyncio.sleep(0.01)  # 真實網路寫入會讓出執行權，斷線的取消才有機會生效

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": spec_version}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions",
        "query_string": b"", "root_path": "", "client": ("127.0.0.1", 50000), "server": ("test", 80),
        "headers": [(b"authorization", f"Bearer {key}".encode()), (b"content-type", b"application/json"),
                    (b"content-length", str(len(raw)).encode())],
    }
    try:
        await h.app(scope, receive, send)
    except Exception:
        pass  # 斷線會讓例外往上拋，這是預期的


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_stream_client_disconnect_is_still_charged(h, spec_version):
    """串流途中斷線：兩種 ASGI 版本的斷線路徑都要結算（名額歸還由 conftest 檢查）。"""
    key = await setup_user(h)
    raw = chat_body(stream=True)
    await call_and_disconnect(h, key, raw, spec_version)

    usage = await last_usage(h)
    assert usage.status == "interrupted"
    assert usage.cost_micro == expected_hold(raw)
    assert await h.balance() == CREDIT - expected_hold(raw)


async def test_disconnect_closes_upstream_stream_that_is_still_producing(h):
    """上游還在一段一段吐內容時使用者斷線：站方要主動關掉上游連線，不能讓它繼續生成（繼續花站方的錢）。"""
    key = await setup_user(h)
    h.upstream.mode = "slow_stream"
    raw = chat_body(stream=True)
    await call_and_disconnect(h, key, raw, "2.3")

    assert h.upstream.stream_closed is True
    assert (await last_usage(h)).status == "interrupted"
    assert await h.balance() == CREDIT - expected_hold(raw)


# ---------------------------------------------------------------- 上游要求的標頭（例如 opencode Go）


def use_upstream(h, **extra) -> None:
    """等同在 config.ini 的 [upstream:fake] 多寫幾個設定。"""
    h.app.state.settings.upstreams["fake"] = UpstreamConfig(
        name="fake", base_url="http://upstream/v1", api_key=UPSTREAM_SECRET, responses=True,
        anthropic_base_url="http://upstream/anthropic", **extra)


async def sent_headers(h, key: str, **client_headers) -> dict:
    r = await h.client.post("/v1/chat/completions", content=chat_body(),
                            headers={**auth(key), "Content-Type": "application/json", **client_headers})
    assert r.status_code == 200, r.text
    return h.upstream.requests[-1]["headers"]


async def test_upstream_fixed_headers_are_sent_but_cannot_replace_auth(h):
    key = await setup_user(h)
    use_upstream(h, headers={"User-Agent": "my-relay/1.0", "X-Title": "relay"})
    sent = await sent_headers(h, key)
    assert sent["user-agent"] == "my-relay/1.0" and sent["x-title"] == "relay"
    assert sent["authorization"] == f"Bearer {UPSTREAM_SECRET}"
    assert "x-opencode-session" not in sent  # 沒設 session_header 就不送


async def test_upstream_session_id_is_stable_per_key_and_isolated_between_users(h):
    use_upstream(h, session_header="x-opencode-session")
    await h.add_model()
    alice_session, alice = await h.user_with_key(credit_micro=CREDIT)
    alice2 = await h.create_key(alice_session)  # 同一個使用者的第二把 key
    _, bob = await h.user_with_key("bob@example.com", credit_micro=CREDIT)

    def sid(headers):
        return headers["x-opencode-session"]

    # 工具沒帶：每把 key 一個固定值，不同 key 不同
    first = sid(await sent_headers(h, alice))
    assert first.startswith("ses_") and first == sid(await sent_headers(h, alice))
    assert first != sid(await sent_headers(h, alice2)) != sid(await sent_headers(h, bob))
    # 工具有帶：同一個使用者同一段對話固定；別的使用者送一樣的值也不會撞在一起；原始值不會送到上游
    mine = sid(await sent_headers(h, alice, **{"x-opencode-session": "conv-1"}))
    assert mine == sid(await sent_headers(h, alice2, **{"x-opencode-session": "conv-1"}))
    assert mine not in (first, sid(await sent_headers(h, bob, **{"x-opencode-session": "conv-1"})))
    assert mine != sid(await sent_headers(h, alice, **{"x-opencode-session": "conv-2"}))
    assert all("conv-1" not in str(r["headers"]) for r in h.upstream.requests)


@pytest.mark.parametrize("style", ["x-api-key", "bearer"])
async def test_anthropic_upstream_auth_style(h, style):
    key = await setup_user(h)
    use_upstream(h, anthropic_auth=style)
    r = await h.client.post("/v1/messages", headers={"x-api-key": key}, json={
        "model": "gpt-test", "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    sent = h.upstream.requests[-1]["headers"]
    if style == "bearer":
        assert sent["authorization"] == f"Bearer {UPSTREAM_SECRET}" and "x-api-key" not in sent
    else:
        assert sent["x-api-key"] == UPSTREAM_SECRET and "authorization" not in sent
    assert sent["anthropic-version"] == "2023-06-01"


# opencode 文件的用量格式：cost 是扣站方餘額的美元，opencode 裡有進價與用掉多少方案額度
UPSTREAM_COST = {"cost": 0.00412, "cost_details": {"upstream_inference_cost": 0.004},
                 "opencode": {"billing_source": "go", "list_cost": 0.00412, "allowance": 0.00412}}


@pytest.mark.parametrize("stream", [False, True])
async def test_upstream_cost_is_not_shown_to_users(h, stream):
    """上游在用量裡附上的成本是站方的進價：不轉給使用者，但照常按 token 計費。"""
    key = await setup_user(h)
    h.upstream.usage_extra = UPSTREAM_COST
    r = await post_chat(h, key, chat_body(stream=stream))
    assert r.status_code == 200
    for name in ("cost", "opencode", "allowance", "0.00412"):
        assert name not in r.text, name
    assert "prompt_tokens" in r.text  # 用量本身照常回給使用者
    usage = await last_usage(h)
    assert usage.cost_micro == ACTUAL_COST
    # 拿掉的成本記進用量紀錄，給後台的收益試算用（0.00412 美元 = 4120 micro）
    assert (usage.upstream, usage.upstream_cost_micro, usage.upstream_cost_estimated,
            usage.upstream_allowance_micro) == ("fake", 4120, False, 4120)


@pytest.mark.parametrize("value, expected", [
    (0.00412, 4120), (0, 0), (0.0000011, 2),  # 比 micro 更小的部分無條件進位
    (True, None), ("0.1", None), (-1, None), (float("nan"), None), (float("inf"), None), (10**7, None),
])
def test_reported_upstream_cost_is_validated(value, expected):
    """上游回報的花費只是記錄用，但亂七八糟的數字（字串、負數、NaN、天文數字）要當作沒回報。"""
    assert relay.reported_usd_to_micro(value) == expected


# ---------------------------------------------------------------- 上游出錯


@pytest.mark.parametrize("stream", [False, True])
async def test_upstream_auth_error_is_masked_and_refunded(h, stream):
    key = await setup_user(h)
    h.upstream.mode = "error401"
    r = await post_chat(h, key, chat_body(stream=stream))
    assert r.status_code == 502
    assert UPSTREAM_SECRET not in r.text  # 上游錯誤訊息裡的 key 不能漏給使用者
    assert await h.balance() == CREDIT
    assert (await last_usage(h)).status == "upstream_error"


@pytest.mark.parametrize("stream", [False, True])
async def test_upstream_request_error_reason_is_shown_but_sanitized(h, stream):
    key = await setup_user(h)
    h.upstream.mode = "error400"
    r = await post_chat(h, key, chat_body(stream=stream))
    assert r.status_code == 400
    assert "context too long" in r.text
    assert UPSTREAM_SECRET not in r.text
    assert await h.balance() == CREDIT


@pytest.mark.parametrize("stream", [False, True])
async def test_upstream_error_with_unreadable_body_is_still_refunded(h, stream):
    key = await setup_user(h)
    h.upstream.mode = "error400_broken_body"
    assert (await post_chat(h, key, chat_body(stream=stream))).status_code == 400
    assert await h.balance() == CREDIT


async def test_upstream_server_error_is_refunded(h):
    key = await setup_user(h)
    h.upstream.mode = "error500"
    assert (await post_chat(h, key, chat_body())).status_code == 502
    assert await h.balance() == CREDIT


@pytest.mark.parametrize("mode", ["error504", "error524"])
@pytest.mark.parametrize("stream", [False, True])
async def test_upstream_gateway_timeout_keeps_hold_for_owner_review(h, mode, stream):
    """閘道逾時時上游可能已經跑完並收費，站方無從得知 → 先收預扣，標成 upstream_timeout 讓站長對帳。"""
    key = await setup_user(h)
    h.upstream.mode = mode
    raw = chat_body(stream=stream)
    r = await post_chat(h, key, raw)
    assert r.status_code == 504
    assert r.json()["error"]["code"] == "upstream_timeout"
    usage = await last_usage(h)
    assert usage.status == "upstream_timeout"
    assert usage.upstream_status == int(mode[5:])
    assert usage.cost_micro == expected_hold(raw)
    assert await h.balance() == CREDIT - expected_hold(raw)


async def test_request_with_tools_holds_extra_for_hidden_tool_prompt(h):
    """上游會自動加一段看不到的工具說明並算進輸入，帶 tools 的請求預扣要多留這一段。"""
    key = await setup_user(h)
    h.upstream.gate = asyncio.Event()  # 讓請求停在上游，才看得到「預扣中」的金額
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    raw = chat_body(tools=tools)
    task = asyncio.create_task(post_chat(h, key, raw))
    await wait_until(lambda: h.upstream.requests)
    held = CREDIT - await h.balance()
    h.upstream.gate.set()
    assert (await task).status_code == 200

    body, cap, _stream, _images = prepare_upstream_body(json.loads(raw), ModelConfig(
        name="gpt-test", upstream="fake", upstream_model="up-gpt-test", input_price_micro=1_000_000,
        output_price_micro=2_000_000, max_output_tokens=100, max_tokens_field="max_tokens"))
    without_margin = estimate_hold(len(encode_upstream_body(body)), 0, 0, cap, 1_000_000, 2_000_000)
    assert held == without_margin + calc_cost(TOOLS_MARGIN_TOKENS, 0, 1_000_000, 0)


@pytest.mark.parametrize("mode", ["pool_timeout", "connect_error"])
@pytest.mark.parametrize("stream", [False, True])
async def test_request_that_never_left_is_refunded(h, mode, stream):
    key = await setup_user(h)
    h.upstream.mode = mode
    r = await post_chat(h, key, chat_body(stream=stream))
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_unreachable"
    assert await h.balance() == CREDIT


async def test_timeout_after_sending_keeps_the_hold(h):
    """送出後才逾時：上游可能已經在計費，站方不退預扣。"""
    key = await setup_user(h)
    h.upstream.mode = "read_timeout"
    raw = chat_body()
    assert (await post_chat(h, key, raw)).status_code == 502
    assert (await last_usage(h)).status == "upstream_broken"
    assert await h.balance() == CREDIT - expected_hold(raw)


async def test_upstream_key_in_stream_error_event_is_masked(h):
    key = await setup_user(h)
    h.upstream.mode = "stream_leak"
    r = await post_chat(h, key, chat_body(stream=True))
    assert r.status_code == 200
    assert UPSTREAM_SECRET not in r.text
    assert "[已遮蔽]" in r.text


async def test_upstream_key_in_http_200_error_body_is_masked(h):
    """有些上游用 HTTP 200 回報錯誤，內容一樣不能夾帶站方 key。沒有用量 → 照規則收全額預扣。"""
    key = await setup_user(h)
    h.upstream.mode = "error200"
    r = await post_chat(h, key, chat_body())
    assert UPSTREAM_SECRET not in r.text
    assert "[已遮蔽]" in r.text
    assert await h.balance() == CREDIT - expected_hold(chat_body())


@pytest.mark.parametrize("stream", [False, True])
async def test_empty_upstream_key_does_not_corrupt_responses(h, stream):
    """不需要 key 的本機上游（key 是空字串）：遮蔽 key 的邏輯不能把回應弄壞。"""
    key = await setup_user(h)
    h.app.state.settings.upstreams["fake"].api_key = ""
    r = await post_chat(h, key, chat_body(stream=stream))
    assert r.status_code == 200
    assert "[已遮蔽]" not in r.text
    if not stream:
        assert r.json()["choices"][0]["message"]["content"] == "Hello"
