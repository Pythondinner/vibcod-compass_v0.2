"""Observer层：盯着transcript文件，判断"现在有没有新的检查点该处理了"。纯规则判断，不调用模型。

两件事：
1. parse_transcript —— 从Claude Code的session transcript文件里，过滤出干净的真实对话轮次。
   一行transcript JSONL里混着很多跟对话内容无关的东西：
   - queue-operation/attachment/custom-title/ai-title/mode等，是客户端记账事件，直接跳过
   - type=user的行里，content是list且含tool_result块的，是工具调用结果回声（Claude API的约定），
     不是真人在说话，跳过；只有content是纯字符串的才是真实人类输入
   - type=assistant的行里，content是block列表，混着thinking/text/tool_use；
     只取text块拼起来，跳过thinking（内部推理）和tool_use（工具调用参数，不是自然语言）；
     如果拼出来是空字符串，说明这一轮纯粹是在调用工具、没有对用户说什么，跳过
2. get_checkpoints —— 每N条真人消息划一个检查点窗口，交给Analysis层处理。N是外置参数，不写死。
"""
import json
from collections.abc import Iterator


def parse_transcript(path: str) -> list[dict]:
    turns = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            t = obj.get("type")
            if t not in ("user", "assistant"):
                continue

            content = obj.get("message", {}).get("content")

            if t == "user":
                if not isinstance(content, str):
                    continue
                text = content.strip()
            else:
                if not isinstance(content, list):
                    continue
                text = "".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ).strip()

            if not text:
                continue

            turns.append({
                "role": t,
                "text": text,
                "timestamp": obj.get("timestamp"),
                "uuid": obj.get("uuid"),
                "parentUuid": obj.get("parentUuid"),
            })
    return turns


def get_checkpoints(turns: list[dict], n: int, start_after_user_turn: int = 0) -> Iterator[tuple[int, list[dict]]]:
    """按每N条真人消息划一个检查点窗口。

    start_after_user_turn：上次已经处理到第几条真人消息了（对应ledger.get_progress的返回值），
    从这之后开始重新划窗口，避免重复处理。

    每个窗口产出 (窗口结束时是第几条真人消息, 窗口内的完整轮次列表)。
    窗口内容是"上一个检查点之后到这一个检查点为止"的所有轮次(含穿插的助手回复)，不是孤立的N条消息。
    """
    user_count = 0
    window: list[dict] = []
    for t in turns:
        if t["role"] == "user":
            user_count += 1
        if user_count <= start_after_user_turn:
            continue
        window.append(t)
        if t["role"] == "user" and (user_count - start_after_user_turn) % n == 0:
            yield user_count, window
            window = []


def find_transcript_by_session_id(session_id: str) -> str | None:
    """按session_id在~/.claude/projects下找对应的transcript文件（文件名就是session_id）。"""
    import glob
    import os

    pattern = os.path.join(os.path.expanduser("~/.claude/projects"), "*", f"{session_id}.jsonl")
    matches = glob.glob(pattern)
    return matches[0] if matches else None


EDIT_TOOL_NAMES = {"Write", "Edit", "NotebookEdit"}


def count_pending_activity(path: str, since_user_turn: int) -> dict:
    """不调模型，纯计数：从上次处理到的检查点之后，新增了多少条真人消息、多少次代码改动（Write/Edit/NotebookEdit）。
    直接读transcript原始JSONL，不复用parse_transcript——因为parse_transcript会把tool_use块整个丢掉，
    这里恰恰要数tool_use块，需要单独一遍轻量扫描。
    """
    user_count = 0
    new_user_turns = 0
    new_edits = 0
    last_activity_ts = None

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            t = obj.get("type")
            if t not in ("user", "assistant"):
                continue

            content = obj.get("message", {}).get("content")

            if t == "user":
                if not isinstance(content, str):
                    continue
                user_count += 1
                if user_count > since_user_turn:
                    new_user_turns += 1
                    last_activity_ts = obj.get("timestamp")
                continue

            # assistant: 数编辑类工具调用,只在已经过了游标之后才算
            if user_count < since_user_turn or not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in EDIT_TOOL_NAMES:
                    new_edits += 1
                    last_activity_ts = obj.get("timestamp")

    return {
        "new_user_turns": new_user_turns,
        "new_edits": new_edits,
        "last_activity_ts": last_activity_ts,
    }


if __name__ == "__main__":
    import sys

    path = sys.argv[1]
    turns = parse_transcript(path)
    user_turns = [t for t in turns if t["role"] == "user"]
    assistant_turns = [t for t in turns if t["role"] == "assistant"]

    print(f"总轮次: {len(turns)}  真人消息: {len(user_turns)}  助手文字回复: {len(assistant_turns)}")
    print()
    print("--- 前8轮预览 ---")
    for turn in turns[:8]:
        preview = turn["text"][:70].replace("\n", " ")
        print(f"[{turn['role']}] {turn['timestamp']} | {preview}")

    print()
    print("--- 每5条真人消息一个检查点，第1~3个检查点里各自最后一条真人消息 ---")
    for i in range(4, min(len(user_turns), 15), 5):
        preview = user_turns[i]["text"][:70].replace("\n", " ")
        print(f"检查点(第{i + 1}条真人消息) | {preview}")
