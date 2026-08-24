"""Ledger层：纯确定性的状态存储，不调用模型。

设计参照工程日志里定的schema：
- records：want/obstacle/node统一存一张表，用record_type区分。
  want/obstacle不需要"取代"指针——按topic_label+record_type分组，取source_end_ts最新的一条就是"当前状态"，
  历史记录留着不删，就是之后做"证据对比"用的底稿。
- capture_progress：轮询脚本用的游标，记录每个session处理到哪条真人消息了，跟笔记本内容本身分开存。
"""
import sqlite3
from datetime import datetime, timezone

DB_PATH = "notebook.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type TEXT NOT NULL CHECK(record_type IN ('want', 'obstacle', 'node')),
    topic_label TEXT NOT NULL,
    content TEXT NOT NULL,
    reason TEXT,
    source_excerpt TEXT,
    source_start_ts TEXT,
    source_end_ts TEXT,
    session_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capture_progress (
    session_id TEXT PRIMARY KEY,
    last_processed_user_turn_index INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str = DB_PATH) -> None:
    conn = get_connection(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def insert_record(
    topic_label: str,
    want: str | None = None,
    obstacle: str | None = None,
    node: dict | None = None,
    source_excerpt: str | None = None,
    source_start_ts: str | None = None,
    source_end_ts: str | None = None,
    session_id: str | None = None,
    db_path: str = DB_PATH,
) -> list[str]:
    """一次提取可能同时产出want/obstacle/node中的多个，分别插入多行，共享同一段来源信息。"""
    conn = get_connection(db_path)
    now = _now()
    inserted = []

    if want:
        conn.execute(
            "INSERT INTO records (record_type, topic_label, content, source_excerpt, source_start_ts, source_end_ts, session_id, created_at) "
            "VALUES ('want', ?, ?, ?, ?, ?, ?, ?)",
            (topic_label, want, source_excerpt, source_start_ts, source_end_ts, session_id, now),
        )
        inserted.append("want")

    if obstacle:
        conn.execute(
            "INSERT INTO records (record_type, topic_label, content, source_excerpt, source_start_ts, source_end_ts, session_id, created_at) "
            "VALUES ('obstacle', ?, ?, ?, ?, ?, ?, ?)",
            (topic_label, obstacle, source_excerpt, source_start_ts, source_end_ts, session_id, now),
        )
        inserted.append("obstacle")

    if node:
        conn.execute(
            "INSERT INTO records (record_type, topic_label, content, reason, source_excerpt, source_start_ts, source_end_ts, session_id, created_at) "
            "VALUES ('node', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                topic_label,
                node.get("decision"),
                node.get("reason"),
                source_excerpt,
                source_start_ts,
                source_end_ts,
                session_id,
                now,
            ),
        )
        inserted.append("node")

    conn.commit()
    conn.close()
    return inserted


def reassign_topic(record_ids: list[int], new_topic_label: str, db_path: str = DB_PATH) -> int:
    """把指定id的记录改到另一个话题标签下——用于修正误合并/误分类的历史记录。返回实际改动的行数。"""
    conn = get_connection(db_path)
    placeholders = ",".join("?" for _ in record_ids)
    cur = conn.execute(
        f"UPDATE records SET topic_label=? WHERE id IN ({placeholders})",
        (new_topic_label, *record_ids),
    )
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed


def get_known_topics(db_path: str = DB_PATH) -> list[str]:
    conn = get_connection(db_path)
    rows = conn.execute("SELECT DISTINCT topic_label FROM records ORDER BY topic_label").fetchall()
    conn.close()
    return [r["topic_label"] for r in rows]


def get_current_state(topic_label: str, db_path: str = DB_PATH) -> dict:
    """拿某个话题当前最新的want和obstacle快照。"""
    conn = get_connection(db_path)
    result = {}
    for rtype in ("want", "obstacle"):
        row = conn.execute(
            "SELECT * FROM records WHERE topic_label=? AND record_type=? "
            "ORDER BY source_end_ts DESC, id DESC LIMIT 1",
            (topic_label, rtype),
        ).fetchone()
        result[rtype] = dict(row) if row else None
    conn.close()
    return result


def get_history(topic_label: str, record_type: str | None = None, db_path: str = DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    if record_type:
        rows = conn.execute(
            "SELECT * FROM records WHERE topic_label=? AND record_type=? ORDER BY source_end_ts ASC, id ASC",
            (topic_label, record_type),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM records WHERE topic_label=? ORDER BY source_end_ts ASC, id ASC",
            (topic_label,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_topics_by_session(session_id: str, db_path: str = DB_PATH) -> list[str]:
    """反查：哪些话题曾经有过来自这个session的记录——配合session_registry按项目文件夹过滤话题用。"""
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT DISTINCT topic_label FROM records WHERE session_id=?",
        (session_id,),
    ).fetchall()
    conn.close()
    return [r["topic_label"] for r in rows]


def get_progress(session_id: str, db_path: str = DB_PATH) -> int:
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT last_processed_user_turn_index FROM capture_progress WHERE session_id=?",
        (session_id,),
    ).fetchone()
    conn.close()
    return row["last_processed_user_turn_index"] if row else 0


def set_progress(session_id: str, turn_index: int, db_path: str = DB_PATH) -> None:
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO capture_progress (session_id, last_processed_user_turn_index, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "last_processed_user_turn_index=excluded.last_processed_user_turn_index, "
        "updated_at=excluded.updated_at",
        (session_id, turn_index, _now()),
    )
    conn.commit()
    conn.close()
