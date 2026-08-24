"""真正的入口：给一个项目文件夹路径，自动找到Claude Code里对应这个项目的session transcript，
持续监控、自动喂进流水线，不用再手动去翻transcript文件在哪。

发现session优先靠session_registry.json（Hook主动写的，可靠——见hook_register.py）；
如果这个项目从没接过Hook，退回到旧的cwd搜索方式兜底（不可靠但至少有个后备）。

每一轮轮询都会重新查一次，所以哪怕你后来在这个文件夹里开了新的Claude Code session，
也会被自动捕捉到，不用重启。
"""
import glob
import json
import os
import time

import run as run_module
from storage import ledger, session_registry

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


def find_sessions_for_project(project_folder: str) -> list[str]:
    """兜底方案：在~/.claude/projects下搜所有transcript文件，找cwd匹配目标文件夹的那些。
    只看每个文件的前20行——cwd在会话一开始就有，不用把整个大文件读完才能判断。
    只有这个项目从没接过Hook（session_registry里查不到）时才会用到这个方法。
    """
    project_folder = os.path.normpath(project_folder)
    matches = []
    pattern = os.path.join(CLAUDE_PROJECTS_DIR, "*", "*.jsonl")
    for fpath in glob.glob(pattern):
        try:
            with open(fpath, encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i > 20:
                        break
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = obj.get("cwd")
                    if cwd and os.path.normpath(cwd) == project_folder:
                        matches.append(fpath)
                        break
        except OSError:
            continue
    return matches


def discover_sessions(project_folder: str) -> list[str]:
    """优先用Hook注册表，查不到再退回cwd搜索兜底。"""
    entry = session_registry.get(project_folder)
    if entry:
        return [entry["transcript_path"]]
    return find_sessions_for_project(project_folder)


def monitor(project_folder: str, n: int = run_module.DEFAULT_N, interval: int = run_module.DEFAULT_INTERVAL) -> None:
    print(f"监控项目文件夹: {project_folder}")
    ledger.init_db()
    try:
        while True:
            sessions = discover_sessions(project_folder)
            if not sessions:
                print(".", end="", flush=True)
            for session_path in sessions:
                session_id = run_module.session_id_from_path(session_path)
                processed = run_module.run_once(session_path, session_id, n)
                if processed:
                    print(f"\n[{session_id[:8]}] 处理了{processed}个新检查点")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python monitor.py <项目文件夹路径>")
        raise SystemExit(1)
    monitor(sys.argv[1])
