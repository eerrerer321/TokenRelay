"""資料庫備份（SQLite）。用 SQLite 內建的 backup：伺服器還在寫入時也能拿到一致的快照，
不會像直接複製檔案那樣，複製到「寫到一半」的狀態。"""

import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import make_url

PREFIX = "relay-"


def backup_sqlite(database_url: str, out_dir: Path, keep: int, now: datetime | None = None) -> Path:
    """備份到 out_dir/relay-年月日-時分秒.db，只留最新的 keep 份。回傳這次的備份檔。"""
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database:
        raise ValueError("只支援 SQLite 檔案資料庫；PostgreSQL 請改用 pg_dump 備份")
    if keep < 1:
        raise ValueError("keep 至少要 1")
    source_path = Path(url.database)
    if not source_path.is_file():
        raise ValueError(f"找不到資料庫檔案：{source_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{PREFIX}{(now or datetime.now()).strftime('%Y%m%d-%H%M%S')}.db"
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    for old in sorted(out_dir.glob(f"{PREFIX}*.db"))[:-keep]:  # 檔名就是時間，排序 = 由舊到新
        old.unlink()
    return dest
