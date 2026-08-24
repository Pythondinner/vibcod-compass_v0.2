"""一次性脚本：把老数据（还没有thread_label的历史记录）按分线逻辑重新分一遍线，写回数据库。
跟thread_backtest.py用同一套prompt(已经拿读码机真实数据回测过、确认续接规则改对了)，区别是
这个真的写库。

want/obstacle/node分开三次独立处理，不混在一起——最初想省事把三种类型混在一次调用里做，
拿读码机真实数据一测直接暴露问题：同一个checkpoint里want在推进全新的"MCP参数改造"，
obstacle却还卡在持续了好几天的"Claude Code登录"，混在一起时模型会被同一时间点的其他字段
带偏，导致obstacle这条本该持续的线被错误拆断。分开跑，各自保持自己的连续性判断，跟最初
单独测obstacle时的结果完全吻合。
"""
import json

from chat import call_deepseek
from prompt_safety import INJECTION_DEFENSE_NOTE, wrap_untrusted
from storage import ledger
from thread_backtest import THREAD_BACKTEST_PROMPT


def backfill_topic_type(topic_label: str, record_type: str, db_path: str = ledger.DB_PATH) -> None:
    records = ledger.get_history(topic_label, record_type=record_type, db_path=db_path)
    already_threaded = [r for r in records if r["thread_label"]]
    if already_threaded:
        print(f"'{topic_label}'/{record_type}已经有{len(already_threaded)}条记录带thread_label，跳过。")
        return
    if not records:
        print(f"'{topic_label}'/{record_type}没有记录，跳过。")
        return

    lines = "\n".join(f"[id={r['id']}][{r['source_end_ts']}] {r['content']}" for r in records)
    type_desc = {"obstacle": "都是obstacle（当时的卡点）", "want": "都是want（当时的目标）", "node": "都是node（做过的决定）"}[
        record_type
    ]
    prompt = (
        THREAD_BACKTEST_PROMPT.replace("__TOPIC__", topic_label)
        .replace("__COUNT__", str(len(records)))
        .replace("__RECORD_TYPE_DESC__", type_desc)
        .replace("__RECORDS__", wrap_untrusted("HISTORY", lines))
    )
    reply = call_deepseek(
        [
            {"role": "system", "content": f"你只输出JSON，不输出任何解释。{INJECTION_DEFENSE_NOTE}"},
            {"role": "user", "content": prompt},
        ],
        json_mode=True,
    )
    data = json.loads(reply)
    assignments = {a["id"]: a for a in data["assignments"]}

    missing = [r["id"] for r in records if r["id"] not in assignments]
    if missing:
        print(f"警告：{len(missing)}条{record_type}记录没有被分配到任何线：{missing}")

    by_thread: dict[tuple[str, str | None], list[int]] = {}
    for r in records:
        a = assignments.get(r["id"])
        if not a:
            continue
        by_thread.setdefault((a["thread_label"], a.get("thread_status")), []).append(r["id"])

    total_written = 0
    for (thread_label, thread_status), ids in by_thread.items():
        changed = ledger.set_thread(ids, thread_label, thread_status, db_path=db_path)
        total_written += changed

    print(f"'{topic_label}'/{record_type}: {len(records)}条 -> {len(by_thread)}条线，写入{total_written}条。")


def backfill_topic(topic_label: str, db_path: str = ledger.DB_PATH) -> None:
    for record_type in ("want", "obstacle", "node"):
        backfill_topic_type(topic_label, record_type, db_path=db_path)


if __name__ == "__main__":
    import sys

    topics = sys.argv[1:] if len(sys.argv) > 1 else ["读码机"]
    for t in topics:
        backfill_topic(t)
