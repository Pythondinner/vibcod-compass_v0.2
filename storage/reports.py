"""复盘报告的持久化：每次跑漂移检测/代码落地检测/综合复盘，存一份markdown文件下来，
支持备注/改名、支持删除。用一个flat的reports/目录+index.json索引，
不用话题名字做目录名——避免中文路径的老问题（之前在session_registry/edit_log都踩过一次）。
"""
import json
import os
import uuid
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")
INDEX_FILE = os.path.join(REPORTS_DIR, "index.json")

REPORT_TYPE_LABELS = {
    "drift": "漂移检测",
    "implementation": "代码落地检测",
    "synthesis": "综合复盘",
}


def _load_index() -> list[dict]:
    if not os.path.exists(INDEX_FILE):
        return []
    with open(INDEX_FILE, encoding="utf-8") as f:
        return json.load(f)


def _save_index(index: list[dict]) -> None:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)


def save_report(topic_label: str, report_type: str, sections: dict, note: str = "") -> dict:
    """sections形如 {"drift": "...", "implementation": "...", "synthesis": "..."}，只填有内容的键。"""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    report_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    filename = f"{report_id}.md"

    lines = [
        f"# {REPORT_TYPE_LABELS.get(report_type, report_type)} · {topic_label}",
        "",
        f"生成时间：{now}",
        f"备注：{note or '（无）'}",
        "",
        "---",
        "",
    ]
    for key, label in (("synthesis", "综合复盘结论"), ("drift", "漂移检测原始结果"), ("implementation", "代码落地检测原始结果")):
        if sections.get(key):
            lines.append(f"## {label}")
            lines.append("")
            lines.append(sections[key])
            lines.append("")

    with open(os.path.join(REPORTS_DIR, filename), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    entry = {
        "id": report_id,
        "topic_label": topic_label,
        "report_type": report_type,
        "created_at": now,
        "note": note,
        "filename": filename,
    }
    index = _load_index()
    index.append(entry)
    _save_index(index)
    return entry


def list_reports(topic_label: str | None = None) -> list[dict]:
    index = _load_index()
    if topic_label:
        index = [r for r in index if r["topic_label"] == topic_label]
    return sorted(index, key=lambda r: r["created_at"], reverse=True)


def update_note(report_id: str, note: str) -> bool:
    index = _load_index()
    for r in index:
        if r["id"] == report_id:
            r["note"] = note
            _save_index(index)
            return True
    return False


def delete_report(report_id: str) -> bool:
    index = _load_index()
    target = next((r for r in index if r["id"] == report_id), None)
    if not target:
        return False
    filepath = os.path.join(REPORTS_DIR, target["filename"])
    if os.path.exists(filepath):
        os.remove(filepath)
    index = [r for r in index if r["id"] != report_id]
    _save_index(index)
    return True


def read_report_content(report_id: str) -> str | None:
    index = _load_index()
    target = next((r for r in index if r["id"] == report_id), None)
    if not target:
        return None
    filepath = os.path.join(REPORTS_DIR, target["filename"])
    if not os.path.exists(filepath):
        return None
    with open(filepath, encoding="utf-8") as f:
        return f.read()
