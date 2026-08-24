"""新的"功能集合体"账本——跟老ledger.py（want/obstacle单句快照链）完全隔离，不共用数据库
文件、不共用任何一行代码。核心区别：没有"当前主线"这个单句快照，功能是一份持续增长的清单，
每条功能独立存在、独立追踪"实现了没有"，不是被新内容顶替的快照。

topic_label直接用Hook给的cwd（标准化路径），不再靠AI猜"这段内容属于哪个项目"——
这是这次重新设计的核心改动之一：cwd是Hook自带的确定信号，比语义判断可靠，
从根源上减少"话题分类不稳定"的判断空间（今天真实撞到过21条记录被误判的bug）。

跟老ledger.py一样是"只增不减"：同一个label的记录多次出现时，全部保留、不覆盖，
按topic_label+record_type+label分组，取最新一条当"当前状态"，历史留着当证据。
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = "features.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS feature_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_label TEXT NOT NULL,
    record_type TEXT NOT NULL CHECK(record_type IN ('feature', 'obstacle', 'node')),
    label TEXT,  -- node可以为NULL：项目级别的决定，不挂靠任何具体功能
    content TEXT NOT NULL,
    reason TEXT,
    status TEXT CHECK(status IN ('open', 'resolved') OR status IS NULL),
    related_feature TEXT,
    session_id TEXT,
    source_prompt_ids TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intake_progress (
    session_id TEXT PRIMARY KEY,
    processed_turn_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def normalize_topic(cwd: str) -> str:
    return os.path.normpath(cwd)


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


def insert_batch(
    topic_label: str,
    features: list[dict],
    obstacles: list[dict],
    nodes: list[dict],
    session_id: str | None,
    source_prompt_ids: list[str],
    db_path: str = DB_PATH,
) -> int:
    """一次intake提取可能同时产出多个功能/卡点/决定，共享同一批来源信息。返回写入的行数。"""
    conn = get_connection(db_path)
    now = _now()
    prompt_ids_json = json.dumps(source_prompt_ids, ensure_ascii=False)
    written = 0

    for f in features:
        conn.execute(
            "INSERT INTO feature_records (topic_label, record_type, label, content, status, session_id, source_prompt_ids, created_at) "
            "VALUES (?, 'feature', ?, ?, ?, ?, ?, ?)",
            (topic_label, f["label"], f["content"], f.get("status"), session_id, prompt_ids_json, now),
        )
        written += 1

    for o in obstacles:
        conn.execute(
            "INSERT INTO feature_records (topic_label, record_type, label, content, status, related_feature, session_id, source_prompt_ids, created_at) "
            "VALUES (?, 'obstacle', ?, ?, ?, ?, ?, ?, ?)",
            (topic_label, o["label"], o["content"], o.get("status"), o.get("related_feature"), session_id, prompt_ids_json, now),
        )
        written += 1

    for n in nodes:
        conn.execute(
            "INSERT INTO feature_records (topic_label, record_type, label, content, reason, session_id, source_prompt_ids, created_at) "
            "VALUES (?, 'node', ?, ?, ?, ?, ?, ?)",
            (topic_label, n["feature_label"], n["content"], n.get("reason"), session_id, prompt_ids_json, now),
        )
        written += 1

    conn.commit()
    conn.close()
    return written


def get_known(topic_label: str, record_type: str, db_path: str = DB_PATH) -> list[dict]:
    """这个话题下某个类型（feature/obstacle）目前的清单，每个label取最新一条当"当前状态"。"""
    conn = get_connection(db_path)
    labels = conn.execute(
        "SELECT DISTINCT label FROM feature_records WHERE topic_label=? AND record_type=?",
        (topic_label, record_type),
    ).fetchall()
    result = []
    for row in labels:
        latest = conn.execute(
            "SELECT * FROM feature_records WHERE topic_label=? AND record_type=? AND label=? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (topic_label, record_type, row["label"]),
        ).fetchone()
        if latest:
            result.append(dict(latest))
    conn.close()
    return result


def get_history(topic_label: str, db_path: str = DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM feature_records WHERE topic_label=? ORDER BY created_at ASC, id ASC",
        (topic_label,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_progress(session_id: str, db_path: str = DB_PATH) -> int:
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT processed_turn_count FROM intake_progress WHERE session_id=?",
        (session_id,),
    ).fetchone()
    conn.close()
    return row["processed_turn_count"] if row else 0


def set_progress(session_id: str, count: int, db_path: str = DB_PATH) -> None:
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO intake_progress (session_id, processed_turn_count, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET processed_turn_count=excluded.processed_turn_count, updated_at=excluded.updated_at",
        (session_id, count, _now()),
    )
    conn.commit()
    conn.close()
