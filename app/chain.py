"""掃鏈：定期用 JSON-RPC 的 eth_getLogs 查「轉進收款地址的 USDT/USDC」。

只需要收款地址，不需要私鑰：伺服器就算被攻破，攻擊者也動不了錢包裡的錢。
注意：這裡完全信任 RPC 節點回傳的資料。RPC 被冒充的話可以偽造入帳，所以只用可信的 RPC 網址。
"""

import asyncio
import logging

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import ADDRESS_RE, Settings, TokenConfig
from app.deposits import expire_stale_orders, record_transfer
from app.models import ChainCursor

log = logging.getLogger("chain")

# keccak256("Transfer(address,address,uint256)")：ERC-20 轉帳事件的固定識別碼
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def address_topic(address: str) -> str:
    """地址放在 topic 裡時，會在前面補 0 補滿 32 bytes。"""
    return "0x" + "0" * 24 + address.lower()[2:]


class RpcError(Exception):
    pass


class ChainWatcher:
    def __init__(self, settings: Settings, sm: async_sessionmaker, http: httpx.AsyncClient):
        if not ADDRESS_RE.match(settings.receive_address):
            raise ValueError(f"config.ini [chain] receive_address 格式錯誤：{settings.receive_address}")
        self.settings, self.sm, self.http = settings, sm, http
        self.chain = settings.chain_name
        self.tokens = settings.token_configs()
        self.to_topic = address_topic(settings.receive_address)

    async def _rpc(self, method: str, params: list):
        resp = await self.http.post(
            self.settings.chain_rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            # 很多公開 RPC 不開放 eth_getLogs（回 limit exceeded），新手很容易卡在這裡
            hint = "（這個 RPC 可能不支援 eth_getLogs，請換一個，見 config.example.ini 的 rpc_url 說明）" \
                if method == "eth_getLogs" else ""
            raise RpcError(f"{method} 失敗：{data['error']}{hint}")
        return data["result"]

    def _is_expected_log(self, entry: dict, token: TokenConfig, from_block: int, to_block: int) -> bool:
        """再檢查一次 RPC 回來的資料。正常節點已經幫我們過濾過，這是多一層保險。"""
        topics = [t.lower() for t in entry.get("topics", [])]
        return (
            entry.get("address", "").lower() == token.contract  # 只認官方合約，假 USDT 在這裡被擋掉
            and len(topics) == 3
            and topics[0] == TRANSFER_TOPIC
            and topics[2] == self.to_topic
            and not entry.get("removed", False)
            and from_block <= int(entry["blockNumber"], 16) <= to_block
        )

    async def poll_once(self) -> dict | None:
        """掃一段已確認的區塊。沒有新區塊可掃時回 None。"""
        head = int(await self._rpc("eth_blockNumber", []), 16)
        safe = head - self.settings.chain_confirmations  # 只處理已經過 N 個區塊確認的轉帳

        async with self.sm.begin() as session:
            cursor = await session.get(ChainCursor, self.chain)
            if cursor is None:
                start = self.settings.chain_start_block
                last = max(safe, 0)
                if start is not None:
                    # 起點設在未來時不能照用：建單會立刻開放，起點之前的付款就永遠不會被掃到
                    last = min(start - 1, last)
                cursor = ChainCursor(chain=self.chain, last_block=last, last_head=head)
                session.add(cursor)
                last_block = last
            else:
                # last_head 是建單時的「開單高度」，只能往前推。用條件式 UPDATE 而不是讀出來再寫回去：
                # 兩個掃描同時跑時，慢的那個不會把較新的高度蓋回舊值
                await session.execute(
                    update(ChainCursor)
                    .where(ChainCursor.chain == self.chain, ChainCursor.last_head < head)
                    .values(last_head=head)
                    .execution_options(synchronize_session=False)
                )
                last_block = cursor.last_block

        from_block = last_block + 1
        if from_block > safe:
            # 已經追上最新的已確認區塊：到目前為止的付款都處理過了，現在作廢過期訂單才安全
            await expire_stale_orders(self.sm, self.settings)
            return None
        to_block = min(safe, from_block + self.settings.chain_max_block_range - 1)

        results = {"matched": 0, "unmatched": 0, "duplicate": 0, "ignored": 0}
        for token in self.tokens.values():
            logs = await self._rpc("eth_getLogs", [{
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "address": token.contract,
                "topics": [TRANSFER_TOPIC, None, self.to_topic],
            }])
            for entry in logs:
                if not self._is_expected_log(entry, token, from_block, to_block):
                    log.warning("忽略不符條件的鏈上事件：%s", entry.get("transactionHash"))
                    results["ignored"] += 1
                    continue
                outcome = await record_transfer(
                    self.sm, self.chain, token,
                    tx_hash=entry["transactionHash"].lower(),
                    log_index=int(entry["logIndex"], 16),
                    block_number=int(entry["blockNumber"], 16),
                    from_address="0x" + entry["topics"][1][-40:].lower(),
                    amount_raw=int(entry["data"], 16),
                )
                results[outcome] += 1

        # 所有事件處理完才推進進度；中途當機的話下次會重掃，靠唯一限制避免重複入帳。
        # 只能往前推：同時有兩個掃描（例如背景程式 + CLI 的 poll-chain）時，慢的那個不會把進度倒退
        async with self.sm.begin() as session:
            await session.execute(
                update(ChainCursor)
                .where(ChainCursor.chain == self.chain, ChainCursor.last_block < to_block)
                .values(last_block=to_block)
            )
        if to_block >= safe:
            # 追上了才作廢過期訂單。還在補掃積壓區塊時作廢，準時付款的訂單會因為「還沒掃到」而被誤殺
            await expire_stale_orders(self.sm, self.settings)
        return {"from_block": from_block, "to_block": to_block, "safe_block": safe, **results}

    async def run_forever(self) -> None:
        while True:
            try:
                # 落後很多區塊時連續掃，直到追上已確認的最新區塊
                while (result := await self.poll_once()) and result["to_block"] < result["safe_block"]:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("掃鏈失敗，%s 秒後重試", self.settings.chain_poll_seconds)
            await asyncio.sleep(self.settings.chain_poll_seconds)
