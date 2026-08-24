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

CREATE TABLE IF NOT EXISTS check_progress (
    topic_label TEXT PRIMARY KEY,
    last_checked_ts TEXT,
    updated_at TEXT NOT NULL
);
"""
# check_progress按topic_label（不是session_id）记游标——"检查一下这个项目"是用户对着一个
# 项目触发的动作，不是对着某一次Claude Code会话，同一个项目可能被开过很多次会话。游标存的是
# "处理到哪个时间点了"（user_ts），不是"处理了几条"——因为轮次现在是跨session合并读出来的
# （turns.get_paired_turns_for_topic），合并之后的顺序不是任何单个session内部的顺序，
# 用计数当游标会对不上，用时间戳可以直接做">"过滤，不用关心到底来自哪个session。


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


def get_known_topics(db_path: str = DB_PATH) -> list[str]:
    conn = get_connection(db_path)
    rows = conn.execute("SELECT DISTINCT topic_label FROM feature_records ORDER BY topic_label").fetchall()
    conn.close()
    return [r["topic_label"] for r in rows]


def get_history(topic_label: str, db_path: str = DB_PATH) -> list[dict]:
    conn = get_connection(db_path)
    rows = conn.execute(
        "SELECT * FROM feature_records WHERE topic_label=? ORDER BY created_at ASC, id ASC",
        (topic_label,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_last_checked(topic_label: str, db_path: str = DB_PATH) -> str | None:
    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT last_checked_ts FROM check_progress WHERE topic_label=?",
        (topic_label,),
    ).fetchone()
    conn.close()
    return row["last_checked_ts"] if row else None


def set_last_checked(topic_label: str, ts: str, db_path: str = DB_PATH) -> None:
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO check_progress (topic_label, last_checked_ts, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(topic_label) DO UPDATE SET last_checked_ts=excluded.last_checked_ts, updated_at=excluded.updated_at",
        (topic_label, ts, _now()),
    )
    conn.commit()
    conn.close()


def migrate_topic(old_cwd: str, new_cwd: str, db_path: str = DB_PATH) -> dict:
    """项目文件夹改名之后，把老topic_label(旧路径)下的全部记录搬到新topic_label(新路径)下——
    这是"cwd当身份"这个设计已知的代价：文件夹一改名，topic_label就变了，不搬的话历史就跟
    新路径断开。不自动检测改名（改名频率低，检测的复杂度不值得），需要用户手动触发。

    **重要限制**：这个函数只搬feature_records和check_progress这两张表里已经落地的数据，
    不动turns/目录下还没被check_now()处理过的原始对话——那些行里存的cwd还是旧路径的
    字符串，改名之后get_paired_turns_for_topic(新路径)找不到它们，会变成孤儿数据。
    所以正确的操作顺序是：改名前先跑一次check_now()把积压的对话清空，改完名再调这个函数。
    """
    old_topic = normalize_topic(old_cwd)
    new_topic = normalize_topic(new_cwd)
    conn = get_connection(db_path)

    cur = conn.execute(
        "UPDATE feature_records SET topic_label=? WHERE topic_label=?",
        (new_topic, old_topic),
    )
    moved = cur.rowcount

    old_progress = conn.execute(
        "SELECT last_checked_ts FROM check_progress WHERE topic_label=?", (old_topic,)
    ).fetchone()
    if old_progress:
        new_progress = conn.execute(
            "SELECT last_checked_ts FROM check_progress WHERE topic_label=?", (new_topic,)
        ).fetchone()
        # 新路径理论上不该已经有游标(刚改名，没检查过)，但防御一下：真有的话保留较晚的时间戳，
        # 不要用旧路径的游标覆盖掉可能更新的进度。
        candidates = [t for t in (old_progress["last_checked_ts"], new_progress["last_checked_ts"] if new_progress else None) if t]
        merged_ts = max(candidates) if candidates else None
        conn.execute(
            "INSERT INTO check_progress (topic_label, last_checked_ts, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(topic_label) DO UPDATE SET last_checked_ts=excluded.last_checked_ts, updated_at=excluded.updated_at",
            (new_topic, merged_ts, _now()),
        )
        conn.execute("DELETE FROM check_progress WHERE topic_label=?", (old_topic,))

    conn.commit()
    conn.close()
    return {"old_topic": old_topic, "new_topic": new_topic, "records_moved": moved}


def set_feature_status(topic_label: str, feature_label: str, status: str, db_path: str = DB_PATH) -> bool:
    """人工确认一个功能的完成状态——不是intake/verify自动写的，是"AI建议、人工确认"这条原则
    落地的地方：verify.py只返回判断文字，从不自己改这个字段，只有用户看过判断之后主动确认，
    这里才会真的更新。只更新这个功能最新那条feature记录的status，不新增记录、不动历史。
    找不到这个功能返回False。"""
    conn = get_connection(db_path)
    latest = conn.execute(
        "SELECT id FROM feature_records WHERE topic_label=? AND record_type='feature' AND label=? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (topic_label, feature_label),
    ).fetchone()
    if not latest:
        conn.close()
        return False
    conn.execute("UPDATE feature_records SET status=? WHERE id=?", (status, latest["id"]))
    conn.commit()
    conn.close()
    return True
