"""Hook直接抓UserPromptSubmit(prompt原文)和Stop(last_assistant_message)，按JSONL追加写——
跟edit_log.py同一个理由：Hook必须几毫秒跑完退出，不能做数据库更新/查找这种有锁风险的操作，
纯追加最安全。一行一个事件，不是一行一轮对话——配对user/assistant是读的时候按prompt_id分组做，
不是写的时候做，这样Stop先到还是UserPromptSubmit先到都不用互相等、不用处理"找不到对应行"这种
边界情况。

这一层是新数据源（Hook）替代老路径（重新解析transcript文件）的核心——这次重新设计的第一块，
先把它跑通、拿真实数据核对过payload字段名之后，再往上叠功能提取这一层。
"""
import json
import os
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TURNS_DIR = os.path.join(PROJECT_ROOT, "turns")


def _log_file_for(session_id: str) -> str:
    os.makedirs(TURNS_DIR, exist_ok=True)
    return os.path.join(TURNS_DIR, f"{session_id}.jsonl")


def _append(session_id: str, record: dict) -> None:
    with open(_log_file_for(session_id), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def record_user_prompt(session_id: str, prompt_id: str, cwd: str | None, text: str) -> None:
    _append(session_id, {
        "role": "user",
        "prompt_id": prompt_id,
        "cwd": cwd,
        "text": text,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


def record_assistant_message(session_id: str, prompt_id: str, text: str) -> None:
    _append(session_id, {
        "role": "assistant",
        "prompt_id": prompt_id,
        "text": text,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


def get_paired_turns(session_id: str) -> list[dict]:
    """按prompt_id配对user/assistant两行，只返回两边都到齐的完整轮次，按user那行先出现的顺序排列。
    还差assistant那半的（Stop还没触发，正在进行中的一轮）不返回，留给下一次读。"""
    path = _log_file_for(session_id)
    if not os.path.exists(path):
        return []

    by_prompt: dict[str, dict] = {}
    order: list[str] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            pid = rec.get("prompt_id")
            role = rec.get("role")
            if not pid or role not in ("user", "assistant"):
                continue
            if pid not in by_prompt:
                by_prompt[pid] = {}
                order.append(pid)
            by_prompt[pid][role] = rec

    turns = []
    for pid in order:
        pair = by_prompt[pid]
        if "user" in pair and "assistant" in pair:
            turns.append({
                "prompt_id": pid,
                "user_text": pair["user"]["text"],
                "assistant_text": pair["assistant"]["text"],
                "user_ts": pair["user"]["ts"],
                "assistant_ts": pair["assistant"]["ts"],
            })
    return turns
