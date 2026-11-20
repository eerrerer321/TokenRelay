"""測試用的假上游（OpenAI 相容 API）與假區塊鏈 RPC。"""

import asyncio
import json
import secrets

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

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
        self.requests: list[dict] = []
        self.stream_closed = False  # slow_stream 模式：站方有沒有主動關掉上游連線
        self.app = FastAPI()
        self.app.post("/v1/chat/completions")(self._chat)
        self.transport = FakeUpstreamTransport(self)

    def usage(self) -> dict:
        return {"prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens}

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
