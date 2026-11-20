"""資料庫備份：內容完整、只留最新幾份、不支援的資料庫要說清楚。"""

import sqlite3
from datetime import datetime

import pytest

from app.backup import backup_sqlite


def make_db(path, rows):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS t (v INTEGER)")
        conn.executemany("INSERT INTO t VALUES (?)", [(r,) for r in rows])
    conn.close()


def test_backup_copies_data_and_keeps_newest(tmp_path):
    db = tmp_path / "relay.db"
    make_db(db, [1, 2])
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    out = tmp_path / "backups"
    first = backup_sqlite(url, out, keep=2, now=datetime(2026, 11, 1, 4, 0, 0))
    make_db(db, [3])
    second = backup_sqlite(url, out, keep=2, now=datetime(2026, 11, 2, 4, 0, 0))
    third = backup_sqlite(url, out, keep=2, now=datetime(2026, 11, 3, 4, 0, 0))

    assert sorted(p.name for p in out.iterdir()) == [second.name, third.name]  # 最舊的那份被刪掉
    assert not first.exists()
    conn = sqlite3.connect(third)
    assert [r[0] for r in conn.execute("SELECT v FROM t ORDER BY v")] == [1, 2, 3]
    conn.close()


@pytest.mark.parametrize("url, message", [
    ("postgresql+asyncpg://u:p@localhost/relay", "pg_dump"),
    ("sqlite+aiosqlite:///./does-not-exist.db", "找不到資料庫檔案"),
])
def test_backup_explains_what_it_cannot_do(tmp_path, url, message):
    with pytest.raises(ValueError, match=message):
        backup_sqlite(url, tmp_path, keep=3)
