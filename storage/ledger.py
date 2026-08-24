"""Ledger层：纯确定性的状态存储，不调用模型。

设计参照工程日志里定的schema：
- records：want/obstacle/node统一存一张表，用record_type区分。
  want/obstacle不需要"取代"指针——按topic_label+record_type分组，取source_end_ts最新的一条就是"当前状态"，
  历史记录留着不删，就是之后做"证据对比"用的底稿。
- capture_progress：轮询脚本用的游标，记录每个session处理到哪条真人消息了，跟笔记本内容本身分开存。

后续补充：加了thread_label/thread_status。起因是发现一个话题内部其实同时存在好几条并行的
关注线（比如"算法基础较弱"和"Claude Code登录失败"是完全不同的两件事），之前全塞进一个
want/obstacle快照里，新话题一来就把旧的顶掉，旧关注线就静默消失了，没法查它到底解决没解决。
thread_label在topic_label下面再分一层，同一条线内部保持时间顺序（漂移检测需要），跨线可以
并存，不用互相覆盖。
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
    created_at TEXT NOT NULL,
    thread_label TEXT,
    thread_status TEXT CHECK(thread_status IN ('open', 'resolved') OR thread_status IS NULL)
);

CREATE TABLE IF NOT EXISTS capture_progress (
    session_id TEXT PRIMARY KEY,
    last_processed_user_turn_index INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_records_dedup
ON records (topic_label, record_type, content, session_id, source_end_ts);

CREATE TABLE IF NOT EXISTS thread_priorities (
    topic_label TEXT NOT NULL,
    record_type TEXT NOT NULL,
    thread_label TEXT NOT NULL,
    priority TEXT NOT NULL CHECK(priority IN ('now', 'later')),
    reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (topic_label, record_type, thread_label)
);
"""
# thread_priorities故意没放进records表——优先级是用户手动打的标记，不是从对话里提取出来的
# 事实，跟着records的"只增不减"逻辑走会很别扭（改优先级=插一条新记录，会让线的历史时间线
# 里混进"用户点了个按钮"这种噪音，跟真实内容混在一起）。独立一张表，一条线一行，可以直接
# 覆盖更新，不需要保留"优先级改过几次"这种历史。
# 这个唯一索引是回填/回测时真实撞见的一个bug倒逼加的：run_once()是"读进度->处理->写进度"三步，
# 中间没有锁，两次几乎同时的调用（比如自动轮询和手动"立即同步"撞在一起）会各自读到同一个旧进度、
# 各自把同一个checkpoint处理一遍，写进度是幂等的看不出问题，但insert_record在它之前已经真实
# 插入了两遍完全一样的记录。真实数据里读码机就有一组，时间戳只差8.5毫秒。加索引在数据库层面
# 堵死这个竞态——判定"重复"的标准是topic_label+record_type+content+session_id+source_end_ts
# 五个字段完全一样，配合insert_record改成INSERT OR IGNORE，两次并发写只会有一次真正落地，
# 不需要在应用层加锁。


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """给已经存在的老数据库补上新字段——CREATE TABLE IF NOT EXISTS对已存在的表不会加新列，
    要显式ALTER。新建的数据库这里等于什么都不用做(列已经在SCHEMA里了)，加个存在性检查避免
    对已经迁移过的库重复ALTER报错。"""
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(records)")}
    if "thread_label" not in existing_cols:
        conn.execute("ALTER TABLE records ADD COLUMN thread_label TEXT")
    if "thread_status" not in existing_cols:
        conn.execute("ALTER TABLE records ADD COLUMN thread_status TEXT")


def init_db(db_path: str = DB_PATH) -> None:
    conn = get_connection(db_path)
    conn.executescript(SCHEMA)
    _migrate(conn)
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
    want_thread: str | None = None,
    want_thread_status: str | None = None,
    obstacle_thread: str | None = None,
    obstacle_thread_status: str | None = None,
    node_thread: str | None = None,
    node_thread_status: str | None = None,
    db_path: str = DB_PATH,
) -> list[str]:
    """一次提取可能同时产出want/obstacle/node中的多个，分别插入多行，共享同一段来源信息，
    但thread_label各自独立——真实数据验证过，同一个checkpoint里want可能在推进"MCP root参数
    改造"这条线，obstacle却还卡在持续了好几天的"Claude Code登录"这条线，两者完全不是一回事，
    不能共用一个thread_label（最初这么设计过，拿读码机真实数据回测直接暴露了这个问题）。"""
    conn = get_connection(db_path)
    now = _now()
    inserted = []

    if want:
        conn.execute(
            "INSERT OR IGNORE INTO records (record_type, topic_label, content, source_excerpt, source_start_ts, source_end_ts, session_id, created_at, thread_label, thread_status) "
            "VALUES ('want', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (topic_label, want, source_excerpt, source_start_ts, source_end_ts, session_id, now, want_thread, want_thread_status),
        )
        inserted.append("want")

    if obstacle:
        conn.execute(
            "INSERT OR IGNORE INTO records (record_type, topic_label, content, source_excerpt, source_start_ts, source_end_ts, session_id, created_at, thread_label, thread_status) "
            "VALUES ('obstacle', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (topic_label, obstacle, source_excerpt, source_start_ts, source_end_ts, session_id, now, obstacle_thread, obstacle_thread_status),
        )
        inserted.append("obstacle")

    if node:
        conn.execute(
            "INSERT OR IGNORE INTO records (record_type, topic_label, content, reason, source_excerpt, source_start_ts, source_end_ts, session_id, created_at, thread_label, thread_status) "
            "VALUES ('node', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                topic_label,
                node.get("decision"),
                node.get("reason"),
                source_excerpt,
                source_start_ts,
                source_end_ts,
                session_id,
                now,
                node_thread,
                node_thread_status,
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


def set_thread(record_ids: list[int], thread_label: str, thread_status: str | None = None, db_path: str = DB_PATH) -> int:
    """把指定id的记录标上关注线归属——用于分线提取时写入，或者回填脚本批量补老数据。"""
    conn = get_connection(db_path)
    placeholders = ",".join("?" for _ in record_ids)
    cur = conn.execute(
        f"UPDATE records SET thread_label=?, thread_status=? WHERE id IN ({placeholders})",
        (thread_label, thread_status, *record_ids),
    )
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed


STALLED_DAYS = 7  # 一条线超过这么多天没有新记录、又没被标resolved，就算"搁置"——纯读取时计算，不存字段


def _is_stalled(source_end_ts: str | None, thread_status: str | None) -> bool:
    if thread_status == "resolved" or not source_end_ts:
        return False
    try:
        last = datetime.fromisoformat(source_end_ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - last).days >= STALLED_DAYS


def set_thread_priority(
    topic_label: str, record_type: str, thread_label: str, priority: str, reason: str | None = None, db_path: str = DB_PATH
) -> None:
    """用户手动给一条线打优先级标记——只有'now'/'later'两档，不做更多级别，AI不参与判断，
    纯粹是用户自己点。覆盖写,不保留"改过几次优先级"这种历史,跟records的只增不减是两回事。"""
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO thread_priorities (topic_label, record_type, thread_label, priority, reason, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(topic_label, record_type, thread_label) DO UPDATE SET "
        "priority=excluded.priority, reason=excluded.reason, updated_at=excluded.updated_at",
        (topic_label, record_type, thread_label, priority, reason, _now()),
    )
    conn.commit()
    conn.close()


def clear_thread_priority(topic_label: str, record_type: str, thread_label: str, db_path: str = DB_PATH) -> None:
    conn = get_connection(db_path)
    conn.execute(
        "DELETE FROM thread_priorities WHERE topic_label=? AND record_type=? AND thread_label=?",
        (topic_label, record_type, thread_label),
    )
    conn.commit()
    conn.close()


def get_threads(topic_label: str, db_path: str = DB_PATH) -> list[dict]:
    """这个话题下出现过的所有关注线，每条线取最新一条记录当"当前状态"，附上这条线一共有几条记录，
    是不是"搁置"了（超过STALLED_DAYS天没更新、又没被标resolved——纯读取时计算，不单独存字段，
    这样阈值以后想调随时能调，不用回填历史数据），以及用户有没有手动打过优先级标记。
    按(record_type, thread_label)分组，不是只按thread_label——want/obstacle/node各自独立起名字，
    理论上可能撞名（比如want和node都恰好叫"MCP集成"），不能当成同一条线合并。"""
    conn = get_connection(db_path)
    groups = conn.execute(
        "SELECT record_type, thread_label, COUNT(*) as cnt FROM records "
        "WHERE topic_label=? AND thread_label IS NOT NULL GROUP BY record_type, thread_label",
        (topic_label,),
    ).fetchall()
    threads = []
    for row in groups:
        latest = conn.execute(
            "SELECT * FROM records WHERE topic_label=? AND record_type=? AND thread_label=? "
            "ORDER BY source_end_ts DESC, id DESC LIMIT 1",
            (topic_label, row["record_type"], row["thread_label"]),
        ).fetchone()
        priority_row = conn.execute(
            "SELECT priority, reason FROM thread_priorities WHERE topic_label=? AND record_type=? AND thread_label=?",
            (topic_label, row["record_type"], row["thread_label"]),
        ).fetchone()
        entry = dict(latest)
        entry["record_count"] = row["cnt"]
        entry["stalled"] = _is_stalled(entry["source_end_ts"], entry["thread_status"])
        entry["priority"] = priority_row["priority"] if priority_row else None
        entry["priority_reason"] = priority_row["reason"] if priority_row else None
        threads.append(entry)
    conn.close()
    return threads


def get_thread_history(
    topic_label: str, thread_label: str, record_type: str | None = None, db_path: str = DB_PATH
) -> list[dict]:
    """record_type建议总是传——want/obstacle/node各自独立起名字，理论上可能撞名，
    不传record_type会把撞名的不同线混在一起读，跟get_threads的分组逻辑要保持一致。"""
    conn = get_connection(db_path)
    if record_type:
        rows = conn.execute(
            "SELECT * FROM records WHERE topic_label=? AND thread_label=? AND record_type=? "
            "ORDER BY source_end_ts ASC, id ASC",
            (topic_label, thread_label, record_type),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM records WHERE topic_label=? AND thread_label=? ORDER BY source_end_ts ASC, id ASC",
            (topic_label, thread_label),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
