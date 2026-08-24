"""Hook维护的"项目文件夹 -> 当前活跃session"注册表，一个简单的JSON文件。
比靠cwd去transcript文件里搜要可靠——这是Hook自己在事件发生时主动写的，不是事后猜的。
"""
import json
import os
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY_FILE = os.path.join(PROJECT_ROOT, "session_registry.json")


def update(cwd: str, transcript_path: str, session_id: str) -> None:
    data = _load()
    data[os.path.normpath(cwd)] = {
        "transcript_path": transcript_path,
        "session_id": session_id,
        "last_seen": datetime.now(timezone.utc).isoformat(),
    }
    with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get(project_folder: str) -> dict | None:
    data = _load()
    return data.get(os.path.normpath(project_folder))


def _load() -> dict:
    if not os.path.exists(REGISTRY_FILE):
        return {}
    with open(REGISTRY_FILE, encoding="utf-8") as f:
        return json.load(f)
