"""从turns.py读Hook抓到的真实对话，判断有没有新功能/新卡点/新决定，写进feature_ledger.py。
跟老analysis.py是同一种机制（一次LLM调用，读真实文本做结构化判断），区别是：
1. 输入源是Hook抓的turns（用户原话+助手完整回复），不是重新解析transcript文件
2. 输出是"功能集合体"的变更集，不是单句主线快照——这个项目没有固定终点，只有一份
   持续增长的功能清单，每条功能独立追踪"实现了没有"
3. topic_label直接用cwd决定，不靠AI猜话题标签——cwd是Hook自带的确定信号，
   今天真实撞到过AI每个checkpoint独立猜话题标签、猜不稳导致21条记录被误判的bug，
   这次从源头上减少这个判断空间
"""
import json

from chat import call_deepseek
from prompt_safety import INJECTION_DEFENSE_NOTE, wrap_untrusted
from storage import feature_ledger, turns

BATCH_SIZE = 3  # 攒够3轮完整对话才触发一次判断，单轮信息量通常不够，跟老版本的权衡一致

INTAKE_PROMPT = """你是一个观察者，负责从一段真实的开发对话里，判断有没有出现新的功能诉求、
新的卡点（阻碍）、或者具体的技术决定。这个项目没有固定的"主线终点"——它是由一个个具体功能
组成的集合体，你的任务不是判断"整体方向对不对"，是识别"这批对话里出现了哪些具体的功能/卡点/决定"。

这个项目目前已经记录过的功能清单（可能已经过时，需要结合下面的新对话判断要不要更新/新增）：
__KNOWN_FEATURES__

这个项目目前已经记录过的卡点堆积：
__KNOWN_OBSTACLES__

判断标准：
- 功能：对话里有没有出现"想要一个能做XX的能力"——可能是用户直接提需求，也可能是助手提方案、
  用户认可，关键是这段对话最后达成了什么样的能力诉求。跟已知功能清单比对：是同一个功能在推进，
  还是确实是个新东西——是同一个功能就复用已有的label，不要因为具体切入点不同就另开一条。
- 卡点：对话里有没有出现"某件事被卡住/报错/不确定/失败了"——用户报告的、助手报告的都算，
  关键特征是在描述一个阻碍，不是在描述一个想要的能力。如果这条卡点明显是在挡上面某条具体的
  功能，把related_feature填上那个功能的label；看不出明确对应哪个就填null，不要勉强凑。
  如果对话明确说某条卡点已经解决了，把status设为"resolved"，其余情况留null，不要臆测。
- 决定：对话里有没有出现具体的选择或结论（"改成用XX方案"这种能落到代码里、以后能拿去跟真实
  代码对照检查的具体主张，也包括"要不要重写/要不要换文件夹"这种项目级别的结构性决定）。
  如果这条决定明显是在给某条具体功能做实现细节，把feature_label填上那个功能的label
  （必须是已知功能清单里的、或者你这次新提出的功能）；如果是项目级别的决定、不属于任何
  具体功能，feature_label填null——不要因为找不到对应的功能就把这条决定整个丢掉不输出。

只输出JSON，格式：
{"features": [{"label": "...", "content": "...", "status": null或"resolved"}],
 "obstacles": [{"label": "...", "content": "...", "status": null或"resolved", "related_feature": "..."或null}],
 "nodes": [{"feature_label": "..."或null, "content": "...", "reason": "..."}]}
没有任何新内容就对应数组输出空的，不要因为要有输出就硬凑。

对话内容（用分隔符包起来，见下方说明）：
__TURNS__
"""


def _format_known(items: list[dict]) -> str:
    if not items:
        return "（还没有任何记录）"
    return "\n".join(f"- 「{i['label']}」({i.get('status') or 'open'}): {i['content']}" for i in items)


def _format_turns(turn_batch: list[dict]) -> str:
    lines = []
    for t in turn_batch:
        lines.append(f"[用户] {t['user_text']}\n\n[助手] {t['assistant_text']}")
    return "\n\n---\n\n".join(lines)


def extract(turn_batch: list[dict], known_features: list[dict], known_obstacles: list[dict]) -> dict:
    """核心intake调用。返回{"features": [...], "obstacles": [...], "nodes": [...]}。"""
    prompt = (
        INTAKE_PROMPT.replace("__KNOWN_FEATURES__", _format_known(known_features))
        .replace("__KNOWN_OBSTACLES__", _format_known(known_obstacles))
        .replace("__TURNS__", wrap_untrusted("CONVERSATION", _format_turns(turn_batch)))
    )
    reply = call_deepseek(
        [
            {
                "role": "system",
                "content": f"你只输出JSON，不输出markdown代码块标记，不输出任何解释。{INJECTION_DEFENSE_NOTE}",
            },
            {"role": "user", "content": prompt},
        ],
        json_mode=True,
    )
    cleaned = reply.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    data = json.loads(cleaned)
    return {
        "features": data.get("features", []),
        "obstacles": data.get("obstacles", []),
        "nodes": data.get("nodes", []),
    }


def run_once(session_id: str, cwd: str) -> int:
    """处理一个session从上次进度之后到目前为止攒够的新一批checkpoint。返回处理了几批。"""
    topic_label = feature_ledger.normalize_topic(cwd)
    all_turns = turns.get_paired_turns(session_id)
    start_after = feature_ledger.get_progress(session_id)
    pending = all_turns[start_after:]

    processed = 0
    for i in range(0, len(pending) - len(pending) % BATCH_SIZE, BATCH_SIZE):
        batch = pending[i : i + BATCH_SIZE]
        known_features = feature_ledger.get_known(topic_label, "feature")
        known_obstacles = feature_ledger.get_known(topic_label, "obstacle")

        result = extract(batch, known_features, known_obstacles)
        prompt_ids = [t["prompt_id"] for t in batch]
        feature_ledger.insert_batch(
            topic_label,
            result["features"],
            result["obstacles"],
            result["nodes"],
            session_id,
            prompt_ids,
        )
        start_after += len(batch)
        feature_ledger.set_progress(session_id, start_after)
        processed += 1

    return processed


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("用法: python intake.py <session_id> <cwd>")
        sys.exit(1)

    feature_ledger.init_db()
    count = run_once(sys.argv[1], sys.argv[2])
    print(f"处理了{count}批")
