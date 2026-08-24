"""从diff_PR借鉴的思路：Hook直接抓PostToolUse payload里的精确diff（structuredPatch，
或新建文件的完整内容），存起来给Brain用，不用每次都重新读一遍整个项目目录。
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(PROJECT_ROOT, "edit_logs")
WRITE_CONTENT_CAP = 4000


def _sanitize(project_path: str) -> str:
    """跟hook_setup同样的道理：可读前缀+hash后缀，避免中文路径压成短横线后撞车。"""
    resolved = os.path.normpath(os.path.abspath(project_path))
    readable = re.sub(r"[^a-zA-Z0-9]", "-", resolved)
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]
    return f"{readable}-{digest}"


def _log_file_for(project_path: str) -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    return os.path.join(LOG_DIR, _sanitize(project_path) + ".jsonl")


def append_from_hook_payload(payload: dict) -> None:
    """Hook调用的入口：从PostToolUse的原始payload里提取要存的字段，几乎不做处理，快速写入。"""
    cwd = payload.get("cwd")
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response") or {}
    file_path = tool_input.get("file_path", "未知文件")

    patch = tool_response.get("structuredPatch") or []
    if patch:
        diff_lines = []
        for hunk in patch:
            diff_lines.extend(hunk.get("lines", []))
        diff_text = "\n".join(diff_lines)
    elif tool_name == "Write":
        content = tool_input.get("content", "")
        if len(content) > WRITE_CONTENT_CAP:
            diff_text = f"（新建文件，完整内容共{len(content)}字符，截断到前{WRITE_CONTENT_CAP}字符）\n{content[:WRITE_CONTENT_CAP]}"
        else:
            diff_text = f"（新建文件，完整内容如下）\n{content}"
    else:
        diff_text = "（无可用diff）"

    if not cwd:
        return

    record = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "tool_name": tool_name,
        "file_path": file_path,
        "diff_text": diff_text,
    }
    with open(_log_file_for(cwd), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_edits(project_path: str) -> list[dict]:
    path = _log_file_for(project_path)
    if not os.path.exists(path):
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def format_edits(edits: list[dict]) -> str:
    chunks = []
    for e in edits:
        chunks.append(f"=== {e['file_path']} ({e['tool_name']}, {e['logged_at']}) ===\n{e['diff_text']}")
    return "\n\n".join(chunks)
