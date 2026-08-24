"""话题标签 -> 项目代码目录的映射，纯配置，不是笔记本内容，跟ledger.db分开存成一个小JSON文件。"""
import json
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATHS_FILE = os.path.join(PROJECT_ROOT, "topic_paths.json")


def get_path(topic_label: str) -> str | None:
    data = _load()
    return data.get(topic_label)


def topics_for_path(project_path: str) -> list[str]:
    """反查：哪些话题的项目代码目录记的是这个路径——配合入口的"按项目过滤"用。"""
    target = os.path.normpath(project_path)
    data = _load()
    return [label for label, p in data.items() if p and os.path.normpath(p) == target]


def set_path(topic_label: str, project_path: str) -> None:
    data = _load()
    data[topic_label] = project_path
    with open(PATHS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load() -> dict:
    if not os.path.exists(PATHS_FILE):
        return {}
    with open(PATHS_FILE, encoding="utf-8") as f:
        return json.load(f)
