"""给目标项目接入Hook：往它的.claude/settings.json里合并写入UserPromptSubmit hook配置。
照update-config skill教的规矩来：先读现有文件、合并不覆盖、再写回，不会破坏已有配置。
"""
import json
import os
import sys

HOOK_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook_register.py")


def _hook_command() -> str:
    return f'"{sys.executable}" "{HOOK_SCRIPT_PATH}"'


def settings_path_for(project_path: str) -> str:
    return os.path.join(project_path, ".claude", "settings.json")


def _entries_have_our_hook(entries: list) -> bool:
    for entry in entries:
        for h in entry.get("hooks", []):
            if h.get("type") == "command" and HOOK_SCRIPT_PATH in h.get("command", ""):
                return True
    return False


def is_attached(project_path: str) -> bool:
    path = settings_path_for(project_path)
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return _entries_have_our_hook(data.get("hooks", {}).get("UserPromptSubmit", []))


def attach(project_path: str) -> None:
    """幂等——已经接过就不会重复添加。同时接UserPromptSubmit（登记活跃session）
    和PostToolUse（抓精确diff，matcher限定Edit|Write|NotebookEdit，照diff_PR的做法）。
    """
    settings_dir = os.path.join(project_path, ".claude")
    path = settings_path_for(project_path)
    os.makedirs(settings_dir, exist_ok=True)

    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}

    data.setdefault("hooks", {})

    ups_entries = data["hooks"].setdefault("UserPromptSubmit", [])
    if not _entries_have_our_hook(ups_entries):
        ups_entries.append({
            "hooks": [{"type": "command", "command": _hook_command(), "timeout": 5}]
        })

    ptu_entries = data["hooks"].setdefault("PostToolUse", [])
    if not any(
        e.get("matcher") == "Edit|Write|NotebookEdit" and _entries_have_our_hook([e])
        for e in ptu_entries
    ):
        ptu_entries.append({
            "matcher": "Edit|Write|NotebookEdit",
            "hooks": [{"type": "command", "command": _hook_command(), "timeout": 5}],
        })

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
