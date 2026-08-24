"""从turns.py读Hook抓到的真实对话，判断有没有新功能/新卡点/新决定，写进feature_ledger.py。
跟老analysis.py是同一种机制（一次LLM调用，读真实文本做结构化判断），区别是：
1. 输入源是Hook抓的turns（用户原话+助手完整回复），不是重新解析transcript文件
2. 输出是"功能集合体"的变更集，不是单句主线快照——这个项目没有固定终点，只有一份
   持续增长的功能清单，每条功能独立追踪"实现了没有"
3. topic_label直接用cwd决定，不靠AI猜话题标签——cwd是Hook自带的确定信号，
   今天真实撞到过AI每个checkpoint独立猜话题标签、猜不稳导致21条记录被误判的bug，
   这次从源头上减少这个判断空间

编排方式：不再是"攒够N轮自动在后台触发"——那是比老系统还激进的自动化，老系统的原则一直是
"判断权留给用户，Brain从不自动触发"，只有纯计数（不调模型）是自动的。这次把这条原则原样
搬回来：count_pending()纯计数、不调模型，实时告诉用户攒了多少新对话、多少代码改动；
真正的判断（check_now()）只在用户主动确认时才触发，一次性把距离上次确认以来的全部内容
喂进去，不再人为切成固定大小的小批——这样也不需要一个后台调度器去决定"什么时候该跑"，
触发时机就是用户点确认的那一刻。"""
import json

from chat import call_deepseek
from prompt_safety import INJECTION_DEFENSE_NOTE, wrap_untrusted
from storage import edit_log, feature_ledger, turns

import verify

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


def _pending_turns(topic_label: str) -> list[dict]:
    all_turns = turns.get_paired_turns_for_topic(topic_label)
    last_checked = feature_ledger.get_last_checked(topic_label)
    if not last_checked:
        return all_turns
    return [t for t in all_turns if t["user_ts"] > last_checked]


def count_pending(cwd: str) -> dict:
    """纯计数，不调模型：距离上次用户确认以来，这个项目新增了多少轮完整对话、多少次代码改动。
    跟老observer.count_pending_activity是同一个定位——永远自动、永远不花钱，负责让用户
    知道"现在攒了多少东西"，判断要不要点确认是用户自己的事。"""
    topic_label = feature_ledger.normalize_topic(cwd)
    pending_turns = _pending_turns(topic_label)

    last_checked = feature_ledger.get_last_checked(topic_label)
    edits = edit_log.read_edits(cwd)
    pending_edits = [e for e in edits if not last_checked or e["logged_at"] > last_checked]

    return {
        "topic_label": topic_label,
        "pending_turns": len(pending_turns),
        "pending_edits": len(pending_edits),
        "last_checked": last_checked,
    }


def check_now(cwd: str) -> dict:
    """用户主动触发的"检查一下这个项目"：把距离上次确认以来全部积累的完整对话一次性喂给
    intake判断有没有新功能/新卡点/新决定；这批里新出现或者有新决定的功能，紧接着自动跑一次
    代码核对——一次操作直接拿到"新增了什么+这些新增的东西实现了多少"，不需要用户先跑提取、
    再一个个手动点检查代码。没有积累任何新内容时直接返回，不空跑一次模型调用。"""
    topic_label = feature_ledger.normalize_topic(cwd)
    pending = _pending_turns(topic_label)

    if not pending:
        return {"topic_label": topic_label, "new_turns": 0, "extraction": None, "verify_results": {}}

    known_features = feature_ledger.get_known(topic_label, "feature")
    known_obstacles = feature_ledger.get_known(topic_label, "obstacle")

    result = extract(pending, known_features, known_obstacles)
    prompt_ids = [t["prompt_id"] for t in pending]
    feature_ledger.insert_batch(
        topic_label,
        result["features"],
        result["obstacles"],
        result["nodes"],
        session_id=None,
        source_prompt_ids=prompt_ids,
    )
    feature_ledger.set_last_checked(topic_label, pending[-1]["user_ts"])

    touched_labels = {f["label"] for f in result["features"]}
    touched_labels |= {n["feature_label"] for n in result["nodes"] if n.get("feature_label")}
    verify_results = {label: verify.check_feature(topic_label, label, cwd) for label in touched_labels}

    return {
        "topic_label": topic_label,
        "new_turns": len(pending),
        "extraction": result,
        "verify_results": verify_results,
    }


def _confirm_loop(topic_label: str, verify_results: dict) -> None:
    """"AI建议、人工确认"这条原则在CLI阶段的落地——只有verify.py判断出"已实现"的功能才问，
    部分实现/未实现/无法判断没有什么好确认的，不问。真正写库的set_feature_status()只在
    用户明确输入y之后才调用，AI自己从来不直接改状态。"""
    for label, result in verify_results.items():
        print(f"\n=== {label} ===\n{result['text']}")
        if result["verdict"] != "已实现":
            continue
        answer = input(f"\n『{label}』核对结果是已实现，要标记为完成状态吗？[y/N] ").strip().lower()
        if answer == "y":
            ok = feature_ledger.set_feature_status(topic_label, label, "resolved")
            print("已标记完成" if ok else "标记失败：找不到这个功能")
        else:
            print("跳过，状态不变")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python intake.py <cwd> [--check] [--migrate <新cwd>]")
        print("  不带参数：只显示待处理数量，不调模型")
        print("  --check：真正触发一次检查，已实现的功能会问要不要标记完成")
        print("  --migrate <新cwd>：项目文件夹改名后，把旧路径下的记录搬到新路径")
        sys.exit(1)

    feature_ledger.init_db()
    cwd_arg = sys.argv[1]

    if "--migrate" in sys.argv:
        idx = sys.argv.index("--migrate")
        if idx + 1 >= len(sys.argv):
            print("用法: python intake.py <旧cwd> --migrate <新cwd>")
            sys.exit(1)
        new_cwd = sys.argv[idx + 1]
        r = feature_ledger.migrate_topic(cwd_arg, new_cwd)
        print(f"已把{r['records_moved']}条记录从'{r['old_topic']}'搬到'{r['new_topic']}'")
    elif "--check" in sys.argv:
        r = check_now(cwd_arg)
        print(f"处理了{r['new_turns']}轮新对话")
        if r["extraction"]:
            print(f"新功能{len(r['extraction']['features'])}个, 新卡点{len(r['extraction']['obstacles'])}个, 新决定{len(r['extraction']['nodes'])}条")
        _confirm_loop(r["topic_label"], r["verify_results"])
    else:
        p = count_pending(cwd_arg)
        print(f"话题: {p['topic_label']}")
        print(f"待处理: {p['pending_turns']}轮对话, {p['pending_edits']}次代码改动")
        print(f"上次确认: {p['last_checked'] or '（从未确认过）'}")
