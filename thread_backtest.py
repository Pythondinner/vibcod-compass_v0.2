"""一次性回测脚本：验证"分线"提取靠不靠谱，不接线上流水线、不写数据库。
拿读码机真实的历史obstacle记录（已经手工读过一遍、心里有预期分组）喂给分线prompt，
看模型分出来的线跟人工预期对不对得上，对得上才考虑接进analysis.py的正式流程。
"""
import json

from chat import call_deepseek
from prompt_safety import INJECTION_DEFENSE_NOTE, wrap_untrusted
from storage import ledger

THREAD_BACKTEST_PROMPT = """以下是"__TOPIC__"这个项目按时间顺序排列的历史记录（__COUNT__条），__RECORD_TYPE_DESC__。

请你按时间顺序过一遍，判断每一条记录属于哪条"关注线"（thread）——同一条关注线指的是持续在讨论/
推进同一个具体问题或方向，哪怕措辞逐次变化很大。

关键判断标准：**如果这条记录是在回答"上一条线提到的问题为什么还没解决/具体原因是什么/进一步
排查发现了什么"，就算延续同一条线，不算新线——哪怕诊断出来的具体原因跟上一条完全不同**（比如
"Claude Code登录失败"这个大问题，先诊断出是"401无效令牌"，再发现其实是"环境变量冲突"，再发现是
"packyapi代理拦截"——这些都是同一次排查里不断深入发现的新原因，属于同一条线"Claude Code登录
排查"，不是每发现一个新原因就开一条新线）。

只有当讨论的其实是**不同的具体问题/不同的功能模块**（比如从"算法基础引导"换到"JSON重试机制"），
才算新开一条线。判断时先问自己："这条记录想解决的，是不是跟上一条同一个大问题？"是的话就是同一
条线；不要因为具体切入点、具体原因不一样就拆线。

给每条线起一个简短的名字（3-10个字，能概括这条线在讨论什么，不要用"问题1"这种没有信息量的名字）。

如果某条记录的内容明确表示它所在的线已经解决/修复/搞定了，把这条记录的thread_status设为
"resolved"；其余情况留null，不要臆测有没有解决。

只输出一个JSON对象，格式：
{"assignments": [{"id": 133, "thread_label": "算法基础较弱", "thread_status": null}, ...]}
每条输入记录必须在assignments里出现且只出现一次。

__RECORDS__
"""


def run_backtest(topic_label: str, record_type: str = "obstacle") -> None:
    records = ledger.get_history(topic_label, record_type=record_type)
    if not records:
        print(f"'{topic_label}'下没有{record_type}记录，无法回测。")
        return

    lines = "\n".join(f"[id={r['id']}][{r['source_end_ts']}] {r['content']}" for r in records)
    type_desc = {"obstacle": "都是obstacle（当时的卡点）", "want": "都是want（当时的目标）", "node": "都是node（做过的决定）"}.get(
        record_type, "混合了want/obstacle/node"
    )
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

    by_thread: dict[str, list[dict]] = {}
    missing = []
    for r in records:
        a = assignments.get(r["id"])
        if not a:
            missing.append(r["id"])
            continue
        by_thread.setdefault(a["thread_label"], []).append({**r, "thread_status": a.get("thread_status")})

    print(f"=== {topic_label} / {record_type}：{len(records)}条记录，分出{len(by_thread)}条线 ===\n")
    for thread_label, items in sorted(by_thread.items(), key=lambda kv: kv[1][0]["source_end_ts"]):
        status = items[-1].get("thread_status") or "open"
        print(f"【{thread_label}】（{len(items)}条，最新状态:{status}）")
        for it in items:
            print(f"  [{it['source_end_ts'][:16]}] {it['content'][:60]}")
        print()

    if missing:
        print(f"警告：{len(missing)}条记录没有被分配到任何线：{missing}")


if __name__ == "__main__":
    import sys

    topic = sys.argv[1] if len(sys.argv) > 1 else "读码机"
    rtype = sys.argv[2] if len(sys.argv) > 2 else "obstacle"
    run_backtest(topic, rtype)
