"""一次性脚本：把现有话题当前过于臃肿的want/obstacle压缩成简洁的一两句话主线概括，
作为新快照写入（历史不会丢，旧的臃肿版本还在历史记录里，只是不再是"当前状态"）。
这是为了修复之前prompt没管住导致want/obstacle越滚越长的问题，只需要跑一次。
"""
from chat import call_deepseek
from storage import ledger

DISTILL_PROMPT = """下面是一段关于项目当前状态的描述，内容偏长、堆砌了很多细节。
请把它压缩成一到两句话，只保留"现在最核心在做什么/最主要卡在哪"这个主线信息，不要罗列所有细节变化。
不要输出任何解释文字，只输出压缩后的结果。

原文：
__TEXT__
"""


def distill(text: str) -> str:
    prompt = DISTILL_PROMPT.replace("__TEXT__", text)
    return call_deepseek([
        {"role": "system", "content": "你只输出压缩后的结果，不输出任何解释或前缀。"},
        {"role": "user", "content": prompt},
    ]).strip()


if __name__ == "__main__":
    for topic in ledger.get_known_topics():
        current = ledger.get_current_state(topic)
        want = current["want"]["content"] if current["want"] else None
        obstacle = current["obstacle"]["content"] if current["obstacle"] else None
        ts = None
        if current["want"]:
            ts = current["want"]["source_end_ts"]
        elif current["obstacle"]:
            ts = current["obstacle"]["source_end_ts"]

        if not want and not obstacle:
            continue

        new_want = distill(want) if want else None
        new_obstacle = distill(obstacle) if obstacle else None

        print(f"=== {topic} ===")
        if new_want:
            print(f"want: {new_want}")
        if new_obstacle:
            print(f"obstacle: {new_obstacle}")

        ledger.insert_record(
            topic_label=topic,
            want=new_want,
            obstacle=new_obstacle,
            source_end_ts=ts,
            session_id="distill-cleanup",
        )
