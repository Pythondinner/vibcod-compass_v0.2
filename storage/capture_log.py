"""记录checkpoint处理失败的日志——JSON解析失败、模型调用异常等。之前是完全静默的，
run_once()抓到异常只print到控制台，终端一关就再也看不到，用户没法知道"最近是不是有checkpoint
没处理成功"。这里持久化最近的失败记录（JSON文件，跟session_registry.json同一套模式），
配合UI显示"最近失败次数"，让失败从"完全看不见"变成"至少能主动查到"。
"""
import json
import os
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(PROJECT_ROOT, "capture_failures.json")
MAX_ENTRIES = 200  # 只保留最近200条，不无限增长


def record_failure(session_id: str, error: str) -> None:
    entries = _load()
    entries.append({
        "session_id": session_id,
        "error": error,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    entries = entries[-MAX_ENTRIES:]
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


def recent_failures(session_id: str | None = None, since_hours: int = 24) -> list[dict]:
    entries = _load()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    result = []
    for e in entries:
        try:
            ts = datetime.fromisoformat(e["ts"])
        except (KeyError, ValueError):
            continue
        if ts < cutoff:
            continue
        if session_id and e.get("session_id") != session_id:
            continue
        result.append(e)
    return result


def _load() -> list[dict]:
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, encoding="utf-8") as f:
        return json.load(f)
