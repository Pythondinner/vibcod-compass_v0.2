"""Analysis层：拿一个检查点窗口，判断有没有实质进展，提取/更新want/obstacle/node。
这是整条流水线里唯一真正调用模型做判断的地方。

两个还没独立验证过的机制,写在这里但要标注清楚:
1. 已有话题标签复用——已验证有效(工程日志04)。
2. 把当前已记录的want/obstacle喂回去,让模型判断是"延续(不变)"还是"推进/调整(要更新)"——
   这是这次重构时才发现必须要有的机制(不然want/obstacle只是孤立快照,不是真正的主线),
   还没有单独测试过，是下一步要验证的东西。
"""
import json

from chat import call_deepseek
from prompt_safety import INJECTION_DEFENSE_NOTE, wrap_untrusted

ANALYSIS_PROMPT = """你是一个观察者，负责从一段开发过程的对话记录里，提取关键的项目状态，维护一条持续演进的主线，而不是每次都独立重新判断。

这段对话可能只涉及一个话题/项目，也可能中途切换到了完全不同的话题/项目——如果发生了话题切换，要把它们分开、分别提取，不要混在一起当成一个话题处理。

已经存在的话题标签有：__EXISTING_LABELS__
如果这段内容明显属于上面某个已有标签所代表的项目/大方向，请直接复用那个标签，不要因为具体任务点不同就另开一个新标签——标签的颗粒度应该是"项目/大方向"级别，不是"具体任务"级别。只有确实不属于任何已有标签所代表的方向，才新建一个标签。

判断能不能复用某个已有标签，必须对照下面"当前状态"里该标签实际记录的want/obstacle内容来判断语义是否真的相关，不能仅仅因为这段对话紧跟在该标签的讨论后面就顺势归并——对话前后紧邻不代表话题相同。如果新内容描述的是一个独立的新项目/新目标，即使前一段还在聊别的已有话题，也应该判断为新话题、新建标签。

这是已有话题目前记录的当前状态（可能已经过时，需要你结合下面的新对话内容判断要不要更新）：
__CURRENT_STATE__

请只输出一个JSON对象，格式为 {"records": [...]}，records数组里每个元素对应一个独立出现过实质内容的话题/项目，字段如下：
- "topic_label": 字符串
- "want": 字符串或null —— 只有当前状态发生了实质变化（推进/调整/重新定义）时才输出新内容。**必须是一到两句话，概括现在最核心在做什么/想达成什么，不要罗列这段时间发生的所有细节变化**——细节不会丢，会被完整记录在历史日志里（node），want只负责让人一眼看清"现在主线在哪"。如果这段对话只是延续、没有变化，设为null，不要重复输出旧内容。
- "obstacle": 字符串或null —— 同样的更新原则：变了才输出，没变设为null，**同样要求一到两句话，只说清楚当前最主要卡在哪，不要罗列所有子问题**。
- "node": 对象（{"decision": "...", "reason": "..."}）或null —— 如果发生了一个新的决定/结论，这里可以包含具体细节，不受"简洁"的约束——细节应该沉淀在这里，不是在want/obstacle里。

如果整段对话里没有任何话题存在实质进展，输出 {"records": []}。

对话内容（用分隔符包起来，见下方说明）：
__TURNS__
"""


def _call_and_parse(prompt: str, retry: bool = True) -> list[dict]:
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
    try:
        data = json.loads(cleaned)
        return data.get("records", [])
    except json.JSONDecodeError:
        if retry:
            # json_mode理论上应该保证格式对，但留一次重试兜底，不是每次失败都要用户自己重跑
            return _call_and_parse(prompt, retry=False)
        raise


def extract(turns: list[dict], existing_labels: list[str], current_state: dict[str, dict]) -> list[dict]:
    """核心Analysis调用。返回解析好的记录列表，每条含topic_label/want/obstacle/node。"""
    prompt = (
        ANALYSIS_PROMPT.replace("__EXISTING_LABELS__", "、".join(existing_labels) or "（还没有）")
        .replace("__CURRENT_STATE__", format_current_state(current_state))
        .replace("__TURNS__", wrap_untrusted("CONVERSATION", format_turns(turns)))
    )
    return _call_and_parse(prompt)


def format_turns(turns: list[dict]) -> str:
    lines = []
    for t in turns:
        role = "用户" if t["role"] == "user" else "助手"
        lines.append(f"[{role}] {t['text']}")
    return "\n\n".join(lines)


def format_current_state(current_state: dict[str, dict]) -> str:
    """current_state形如 {"话题标签": {"want": "...", "obstacle": "..."}}，来自ledger.get_current_state的汇总。"""
    if not current_state:
        return "（目前还没有任何已记录的话题）"
    lines = []
    for topic, state in current_state.items():
        want = state.get("want") or "（无记录）"
        obstacle = state.get("obstacle") or "（无记录）"
        lines.append(f"- {topic}: want={want}; obstacle={obstacle}")
    return "\n".join(lines)
