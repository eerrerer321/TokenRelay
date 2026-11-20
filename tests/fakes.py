"""測試用的假上游（OpenAI 相容 API）與假區塊鏈 RPC。"""

import asyncio
import json
import secrets

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.chain import TRANSFER_TOPIC, address_topic

UPSTREAM_SECRET = "sk-upstream-secret-should-never-leak"


def _sse(obj) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


class FakeUpstream:
    """mode：
    ok / no_usage / error200 / error400 / error401 / error500 / stream_leak（交給假 FastAPI app 處理）
    pool_timeout / connect_error / read_timeout / error400_broken_body / slow_stream（交給 transport 模擬網路狀況）
    """

    def __init__(self):
        self.mode = "ok"
        self.gate: asyncio.Event | None = None  # 設定後，請求會停在上游，直到測試呼叫 gate.set()
        self.prompt_tokens = 10
        self.completion_tokens = 20
        self.usage_extra: dict = {}  # 額外加進 usage 的欄位，例如快取命中數
        self.requests: list[dict] = []
        self.stream_closed = False  # slow_stream 模式：站方有沒有主動關掉上游連線
        self.app = FastAPI()
        self.app.post("/v1/chat/completions")(self._chat)
        self.app.get("/v1/models")(self._models)
        self.app.post("/v1/responses")(self._responses)
        self.app.post("/v1/embeddings")(self._embeddings)
        self.app.post("/v1/images/generations")(self._images)
        self.app.post("/v1/audio/speech")(self._speech)
        self.app.post("/anthropic/v1/messages")(self._messages)
        self.image_count = 1  # 產生圖片時回幾張
        self.anthropic_cache_read = 0
        self.anthropic_cache_write = 0
        self.transport = FakeUpstreamTransport(self)

    def usage(self) -> dict:
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens, **self.usage_extra}

    async def _models(self, request: Request):
        """仿 OpenRouter：附上「每 token 幾美元」的價格。"""
        self.requests.append({"headers": dict(request.headers), "body": None})
        return {"data": [
            {"id": "vendor/big-model", "pricing": {"prompt": "0.000003", "completion": "0.000015",
                                                   "input_cache_read": "0.0000003"}},
            {"id": "plain-model"},
        ]}

    async def _chat(self, request: Request):
        raw = await request.body()
        body = json.loads(raw)
        self.requests.append({"headers": dict(request.headers), "body": body, "raw": raw})
        if self.gate is not None:
            await self.gate.wait()
        if self.mode == "error200":  # 有些上游（例如 OpenRouter）會用 HTTP 200 回報錯誤
            return JSONResponse({"error": {"message": f"provider failed (key {UPSTREAM_SECRET})"}})
        if self.mode == "error400":
            return JSONResponse({"error": {"message": f"context too long (key {UPSTREAM_SECRET})"}}, status_code=400)
        if self.mode == "error401":
            return JSONResponse({"error": {"message": f"Incorrect API key provided: {UPSTREAM_SECRET}"}},
                                status_code=401)
        if self.mode == "error500":
            return JSONResponse({"error": {"message": "boom"}}, status_code=500)

        if body.get("stream"):
            include_usage = (body.get("stream_options") or {}).get("include_usage") and self.mode != "no_usage"

            async def events():
                for word in ("Hel", "lo"):
                    yield _sse({"id": "c1", "object": "chat.completion.chunk", "usage": None,
                                "choices": [{"index": 0, "delta": {"content": word}}]})
                if self.mode == "stream_leak":
                    yield _sse({"error": {"message": f"internal error for key {UPSTREAM_SECRET}"}})
                if include_usage:
                    yield _sse({"id": "c1", "choices": [], "usage": self.usage()})
                yield b"data: [DONE]\n\n"

            return StreamingResponse(events(), media_type="text/event-stream")

        data = {"id": "c1", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello"},
                             "finish_reason": "stop"}]}
        if self.mode != "no_usage":
            data["usage"] = self.usage()
        return JSONResponse(data)


    async def _record(self, request: Request) -> dict:
        raw = await request.body()
        body = json.loads(raw)
        self.requests.append({"headers": dict(request.headers), "body": body, "raw": raw, "path": request.url.path})
        return body

    def _error(self):
        if self.mode == "error400":
            return JSONResponse({"error": {"message": f"bad request (key {UPSTREAM_SECRET})"}}, status_code=400)
        if self.mode == "error500":
            return JSONResponse({"error": {"message": "boom"}}, status_code=500)
        return None

    async def _responses(self, request: Request):
        body = await self._record(request)
        if (error := self._error()) is not None:
            return error
        usage = {"input_tokens": self.prompt_tokens, "output_tokens": self.completion_tokens,
                 "total_tokens": self.prompt_tokens + self.completion_tokens, **self.usage_extra}
        response = {"id": "resp_1", "object": "response", "status": "completed",
                    "output": [{"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": "Hello"}]}]}
        if self.mode != "no_usage":
            response["usage"] = usage
        if not body.get("stream"):
            return JSONResponse(response)

        async def events():
            yield _sse({"type": "response.created", "response": {**response, "status": "in_progress", "usage": None}})
            yield _sse({"type": "response.output_text.delta", "delta": "Hello"})
            if self.mode != "cut_stream":
                yield _sse({"type": "response.completed", "response": response})

        return StreamingResponse(events(), media_type="text/event-stream")

    async def _embeddings(self, request: Request):
        await self._record(request)
        if (error := self._error()) is not None:
            return error
        data = {"object": "list", "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}]}
        if self.mode != "no_usage":
            data["usage"] = {"prompt_tokens": self.prompt_tokens, "total_tokens": self.prompt_tokens}
        return JSONResponse(data)

    async def _images(self, request: Request):
        await self._record(request)
        if (error := self._error()) is not None:
            return error
        return JSONResponse({"created": 0, "data": [{"b64_json": "aW1n"}] * self.image_count})

    async def _speech(self, request: Request):
        await self._record(request)
        if (error := self._error()) is not None:
            return error
        if self.mode == "leak200":  # 用 HTTP 200 回錯誤，還夾帶 key
            return JSONResponse({"error": {"message": f"quota exceeded for key {UPSTREAM_SECRET}"}})
        return Response(b"ID3-fake-mp3", media_type="audio/mpeg")

    async def _messages(self, request: Request):
        """Anthropic 格式：input_tokens 不含快取；串流的用量分散在 message_start 和 message_delta。"""
        body = await self._record(request)
        if (error := self._error()) is not None:
            return error
        start_usage = {"input_tokens": self.prompt_tokens, "output_tokens": 1,
                       "cache_read_input_tokens": self.anthropic_cache_read,
                       "cache_creation_input_tokens": self.anthropic_cache_write, **self.usage_extra}
        message = {"id": "msg_1", "type": "message", "role": "assistant", "model": body.get("model"),
                   "content": [{"type": "text", "text": "Hello"}], "stop_reason": "end_turn"}
        if not body.get("stream"):
            if self.mode != "no_usage":
                message["usage"] = {**start_usage, "output_tokens": self.completion_tokens}
            return JSONResponse(message)

        def event(name, obj):
            return f"event: {name}\ndata: {json.dumps(obj)}\n\n".encode()

        async def events():
            yield event("message_start", {"type": "message_start",
                                          "message": {**message, "content": [], "usage": start_usage}})
            yield event("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                "delta": {"type": "text_delta", "text": "Hello"}})
            if self.mode == "delta_zero_input":  # 異常的上游：結尾把輸入數回報成 0
                yield event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                                              "usage": {"input_tokens": 0, "output_tokens": self.completion_tokens}})
            elif self.mode == "delta_without_usage":  # 異常的上游：結尾沒附輸出數
                yield event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
            elif self.mode != "cut_stream":
                yield event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                                              "usage": {"output_tokens": self.completion_tokens,
                                                        **self.usage_extra}})
                yield event("message_stop", {"type": "message_stop"})

        return StreamingResponse(events(), media_type="text/event-stream")


class _SlowStream(httpx.AsyncByteStream):
    """一段一段慢慢吐的串流，記錄站方有沒有關閉它（真實網路連線的行為）。"""

    def __init__(self, upstream: FakeUpstream, chunks: list[bytes]):
        self.upstream, self.chunks = upstream, chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(0.05)
            yield chunk

    async def aclose(self):
        self.upstream.stream_closed = True


class _BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        raise httpx.ReadTimeout("body read timed out")
        yield b""  # pragma: no cover  讓它成為 async generator


class FakeUpstreamTransport(httpx.AsyncBaseTransport):
    def __init__(self, upstream: FakeUpstream):
        self.upstream = upstream
        self.asgi = httpx.ASGITransport(app=upstream.app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        mode = self.upstream.mode
        if mode == "pool_timeout":
            raise httpx.PoolTimeout("connection pool exhausted", request=request)
        if mode == "connect_error":
            raise httpx.ConnectError("connection refused", request=request)
        if mode == "read_timeout":
            raise httpx.ReadTimeout("upstream timed out", request=request)
        if mode in ("error504", "error524"):  # 上游前面的閘道等太久先回逾時（後面的模型可能照樣跑完）
            return httpx.Response(int(mode[5:]), json={"error": {"message": "gateway timeout"}}, request=request)
        if mode == "error400_broken_body":
            return httpx.Response(400, headers={"content-type": "application/json"}, stream=_BrokenStream(),
                                  request=request)
        if mode == "slow_stream":
            self.upstream.requests.append({"headers": dict(request.headers), "body": json.loads(request.content)})
            chunks = [_sse({"id": "c1", "usage": None, "choices": [{"index": 0, "delta": {"content": str(i)}}]})
                      for i in range(20)]
            chunks += [_sse({"id": "c1", "choices": [], "usage": self.upstream.usage()}), b"data: [DONE]\n\n"]
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=_SlowStream(self.upstream, chunks), request=request)
        return await self.asgi.handle_async_request(request)


class FakeChain:
    """模擬 BSC 節點的 eth_blockNumber / eth_getLogs。"""

    def __init__(self):
        self.head = 0
        self.logs: list[dict] = []
        self.ignore_address_filter = False  # 模擬有問題的 RPC：不照合約地址過濾
        self.native_balance = 0  # 收款地址的 BNB（最小單位）
        self.token_balances: dict[str, int] = {}  # 合約地址（小寫）→ 收款地址的代幣餘額（最小單位）

    def add_transfer(self, *, contract: str, to: str, amount_raw: int, block: int, tx_hash: str | None = None,
                     log_index: int = 0, sender: str = "0x" + "11" * 20) -> str:
        tx_hash = tx_hash or "0x" + secrets.token_hex(32)
        self.logs.append({
            "address": contract,
            "topics": [TRANSFER_TOPIC, address_topic(sender), address_topic(to)],
            "data": "0x" + format(amount_raw, "064x"),
            "blockNumber": hex(block),
            "transactionHash": tx_hash,
            "logIndex": hex(log_index),
            "removed": False,
        })
        return tx_hash

    def handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload["method"]
        if method == "eth_blockNumber":
            result = hex(self.head)
        elif method == "eth_getBalance":
            result = hex(self.native_balance)
        elif method == "eth_call":  # 只模擬 balanceOf
            call = payload["params"][0]
            result = "0x" + format(self.token_balances.get(call["to"].lower(), 0), "064x")
        elif method == "eth_getLogs":
            flt = payload["params"][0]
            low, high = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
            result = [
                entry for entry in self.logs
                if low <= int(entry["blockNumber"], 16) <= high
                and (self.ignore_address_filter or entry["address"].lower() == flt["address"].lower())
                and entry["topics"][2] == flt["topics"][2]
            ]
        else:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"],
                                             "error": {"code": -32601, "message": "method not found"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})
