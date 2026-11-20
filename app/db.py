"""資料庫連線。同一份程式碼可接 PostgreSQL（正式）或 SQLite（開發／測試）。"""

from datetime import datetime, timezone

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """一律存「不含時區資訊的 UTC 時間」。SQLite 不保存時區，統一格式才不會比較出錯。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_engine(url: str) -> AsyncEngine:
    if not url.startswith("sqlite"):
        return create_async_engine(url, pool_pre_ping=True)

    engine = create_async_engine(url, connect_args={"timeout": 30})

    # SQLite 預設的交易到第一次寫入才上鎖，兩個交易同時「先讀後寫」會互卡（database is locked）。
    # 改成 BEGIN IMMEDIATE：交易一開始就拿寫入鎖，所有交易排隊執行。只影響 SQLite。
    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _on_begin(connection):
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_tables(engine: AsyncEngine) -> None:
    from app import models  # noqa: F401  匯入後所有資料表才會註冊到 Base.metadata

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
