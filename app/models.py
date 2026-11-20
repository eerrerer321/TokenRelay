"""資料表定義。金額欄位一律是整數 micro-USD（1 = 0.000001 美元），欄位名以 _micro 結尾。

上游與模型價格不在資料庫裡，寫在 config.ini（見 app/config.py）。
"""

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    balance_micro: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class WebSession(Base):
    """網頁／管理 API 用的登入狀態。只存 token 的雜湊。"""

    __tablename__ = "web_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PasswordReset(Base):
    """站長在後台產生的一次性重設密碼連結。只存 token 的雜湊。"""

    __tablename__ = "password_resets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ApiKey(Base):
    """給 Cherry Studio 之類工具用的 sk- key。只存雜湊，完整 key 只在建立時顯示一次。"""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    key_prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class UsageLog(Base):
    """每一次轉發請求。建立時是 pending（已預扣），結算後改成最終狀態。"""

    __tablename__ = "usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    api_key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id"))
    model: Mapped[str] = mapped_column(String(100))  # config.ini 裡的 [model:名稱]
    upstream: Mapped[str] = mapped_column(String(100), default="")  # 當時用的 [upstream:名稱]，給收益試算分上游
    stream: Mapped[bool]
    hold_micro: Mapped[int] = mapped_column(BigInteger)
    cost_micro: Mapped[int] = mapped_column(BigInteger, default=0)  # 向使用者收了多少
    prompt_tokens: Mapped[int | None]
    completion_tokens: Mapped[int | None]
    cached_tokens: Mapped[int | None]  # prompt_tokens 裡快取命中的部分
    # 上游收站方多少錢：上游有回報就是回報值，沒有就用進價（cost_*）× token 數估算；None = 不知道
    upstream_cost_micro: Mapped[int | None] = mapped_column(BigInteger)
    upstream_cost_estimated: Mapped[bool | None]  # True = 上面那個數字是估的
    upstream_allowance_micro: Mapped[int | None] = mapped_column(BigInteger)  # opencode Go 用掉的方案額度
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    upstream_status: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    settled_at: Mapped[datetime | None]


class Ledger(Base):
    """帳本：每一筆餘額變動都記一列。SUM(amount_micro) 必須等於 users.balance_micro。"""

    __tablename__ = "ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount_micro: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))  # hold / settle / deposit / deposit_manual / adjust
    ref: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class DepositOrder(Base):
    __tablename__ = "deposit_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    chain: Mapped[str] = mapped_column(String(32))
    token: Mapped[str] = mapped_column(String(16))
    amount_usd: Mapped[int]
    pay_amount_micro: Mapped[int] = mapped_column(BigInteger, index=True)  # 含尾數，例：10.0037 → 10003700
    # 開單當下的鏈上高度：比這更早的轉帳不可能是付這張單（避免舊款對到同金額的新單）
    min_block: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending / paid / expired
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    paid_at: Mapped[datetime | None]
    tx_hash: Mapped[str | None] = mapped_column(String(66))


class ChainTransfer(Base):
    """鏈上看到的每一筆轉入。(chain, tx_hash, log_index) 唯一 → 同一筆轉帳不可能入帳兩次。"""

    __tablename__ = "chain_transfers"
    __table_args__ = (UniqueConstraint("chain", "tx_hash", "log_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(32))
    token: Mapped[str] = mapped_column(String(16))
    token_contract: Mapped[str] = mapped_column(String(42))  # 記下當時的合約與精度，設定日後改了也不會算錯
    decimals: Mapped[int]
    tx_hash: Mapped[str] = mapped_column(String(66))
    log_index: Mapped[int]
    block_number: Mapped[int] = mapped_column(BigInteger)
    from_address: Mapped[str] = mapped_column(String(42))
    amount_raw: Mapped[str] = mapped_column(String(80))  # 鏈上原始整數（18 位小數會超過 BigInteger）
    status: Mapped[str] = mapped_column(String(16))  # matched / unmatched / manual
    order_id: Mapped[int | None] = mapped_column(ForeignKey("deposit_orders.id"))
    note: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ChainCursor(Base):
    """掃鏈進度：已經處理到哪個區塊，以及最近一次看到的鏈上最新高度。"""

    __tablename__ = "chain_cursors"

    chain: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_block: Mapped[int] = mapped_column(BigInteger)
    last_head: Mapped[int] = mapped_column(BigInteger)
