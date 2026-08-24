"""Hook维护的"项目文件夹 -> 当前活跃session"注册表，一个简单的JSON文件。
比靠cwd去transcript文件里搜要可靠——这是Hook自己在事件发生时主动写的，不是事后猜的。

真实撞过一次这个文件被截断成非法JSON的情况——大概率是写到一半进程被强制杀掉（比如强制
结束一个卡住的python进程），旧版本是直接原地`open(...,"w")`覆盖写，写一半被打断就是半个
文件。改成先写临时文件再os.replace()原子替换，跟数据库"要么全写成功要么不写"是一个思路。
另外_load()原来遇到损坏文件会直接抛异常——而调用方hook_register.py完全没有try/except接住，
一旦文件损坏，之后**每一次**Hook触发都会静默崩溃、整个session_registry从此再也更新不了，
不只是某一个项目失效。现在遇到损坏文件会把坏文件备份一份，然后当空表处理，不再让读损坏了
的注册表变成"全系统Hook从此失灵"。
"""
import json
import os
import shutil
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
    tmp_path = REGISTRY_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, REGISTRY_FILE)  # 同一文件系统内是原子操作,不会写一半就被打断成半个文件


def get(project_folder: str) -> dict | None:
    data = _load()
    return data.get(os.path.normpath(project_folder))


def _load() -> dict:
    if not os.path.exists(REGISTRY_FILE):
        return {}
    try:
        with open(REGISTRY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        backup_path = REGISTRY_FILE + f".corrupted-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(REGISTRY_FILE, backup_path)
        return {}
