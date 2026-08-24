"""Brain层：读一个话题从开始到现在的完整历史，判断有没有值得指出的漂移/矛盾，引用证据。
这是这个项目第一次真正需要"决策"而不只是"提取"的地方——第一版先做最朴素的实现：
把整个话题的完整历史一次性喂给模型判断。规模一旦变大会不会顶不住，这次就是要拿真实数据测这个。
"""
import os

from chat import call_deepseek
from prompt_safety import INJECTION_DEFENSE_NOTE, wrap_untrusted
from storage import edit_log, ledger

# 第一版代码比对：不做检索/分块，直接读整个项目目录的源码文件。
# 大项目会顶不住，先在小项目（这个项目自己）上验证想法能不能走通。
CODE_EXTENSIONS = {".py"}
CODE_EXCLUDE_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", "工程日志"}
CODE_EXCLUDE_FILES_PREFIX = ("test_",)  # 测试/一次性验证脚本不算"实现"，跳过


def read_project_code(project_path: str) -> str:
    """读一个项目目录下所有源码文件，拼成一份带文件路径标注的文本。"""
    chunks = []
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in CODE_EXCLUDE_DIRS]
        for fname in files:
            if not any(fname.endswith(ext) for ext in CODE_EXTENSIONS):
                continue
            if fname.startswith(CODE_EXCLUDE_FILES_PREFIX):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, project_path)
            try:
                with open(fpath, encoding="utf-8") as f:
                    content = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            chunks.append(f"=== {rel} ===\n{content}")
    return "\n\n".join(chunks)

DRIFT_PROMPT = """你是一个复盘助手，要检查一个项目从开始到现在的完整记录，判断这个项目的方向有没有出现值得注意的漂移——
不是自然的演进/推进，而是看起来跟早期的想法产生了矛盾、被悄悄改变了方向、或者早期的一个决定后来被违背了却没有明确说明原因。

下面是"__TOPIC__"这个项目从开始到现在的完整记录（want=当时的目标快照，obstacle=当时的卡点，node=做过的决定及理由），按时间顺序排列：

__HISTORY__

第一步：先用你自己的话，简短概括一下你从这份记录里理解到的"这个项目大致是怎么演进的"（2-3句话），不要照抄原文——这一步是为了让人能核对你有没有理解错，再往下看你的判断。

第二步，判断：
1. 这个项目整体上是自然演进（后面的内容建立在前面的基础上、方向一致），还是存在某个点跟更早的记录出现了矛盾或未说明的转向？
2. 如果存在，具体是哪条记录跟哪条记录冲突，冲突点是什么，引用具体的时间戳和原文内容作为证据。
3. 如果没有发现明显的漂移，直接说明"未发现明显漂移"，不要为了显得有用而牵强附会。

用中文回答，不要输出JSON，直接给出可读的分析文字。
"""


IMPLEMENTATION_PROMPT = """你是一个诚实的代码复核员，要检查"决定要做的事情"和"实际写出来的代码"对不对得上。

下面是"__TOPIC__"这个项目目前的大方向和卡点：
- 主线目标：__WANT__
- 当前卡点：__OBSTACLE__

下面是这条线上做过的具体技术决定（按时间顺序，越靠后越新）：
__NODES__

下面是这个项目目前的真实源代码：
__CODE__

第一步：先用你自己的话，简短复述一遍你理解到的"这条线上主要做了哪些决定"（2-3句话，不要照抄原文），方便核对你有没有理解错需求。

第二步，判断：
1. 上面列出的具体技术决定，有没有真的体现在代码里？哪些兑现了，哪些没有兑现或者代码写的跟决定的不一样？
2. 引用具体的文件名和代码片段（或函数名）作为证据，不要笼统地说"大体一致"。
3. 如果某个决定没法从代码里判断（比如代码里根本没有相关部分），明确说"无法判断"，不要猜。
4. 如果全部对得上，直接说"决定和代码对得上，未发现明显落差"，不要为了显得有用就挑毛病。

用中文回答，不要输出JSON，直接给出可读的分析文字。
"""


SYNTHESIS_PROMPT = """你手上有两份独立做出来的检查结果，彼此互相不知道对方的结论：
1. 漂移检测——只看对话历史，判断"决定/想法本身"有没有前后矛盾
2. 代码落地检测——只看代码，判断"决定有没有真的体现在代码里"

请把两者放在一起看，重点找：
1. 有没有哪个发现能互相印证？（比如漂移检测说某个决定被无声放弃，代码里也确实没有相关实现——两边独立得出同一个结论，这种一致性比单独一份报告更可信）
2. 有没有哪个发现其实是对方的盲区造成的假象？（比如代码落地检测说"某决定没被授权"，但会不会是这个决定压根没被记成一条node，而不是真的没被授权——这种情况下问题出在记录缺失，不是真的有矛盾）
3. 综合以上两点，给出一个整合后的判断：这个项目现在有没有真实值得关注的问题，还是两份报告各自的疑点其实互相解释清楚了、可以放心。

漂移检测结果：
__DRIFT__

代码落地检测结果：
__IMPLEMENTATION__

第一步：先各用一句话概括这两份报告各自的结论是什么（不要照抄原文），确认你理解对了两边在说什么。

第二步，按上面三点交叉核对，用中文回答，不要输出JSON，直接给出可读的分析文字，重点讲清楚"两边合起来看，跟单独看有什么不一样的结论"。
"""


def format_history(history: list[dict]) -> str:
    lines = []
    for h in history:
        line = f"[{h['record_type']}][{h['source_end_ts']}] {h['content']}"
        if h["reason"]:
            line += f"（原因：{h['reason']}）"
        lines.append(line)
    return "\n".join(lines)


def check_drift(topic_label: str) -> str:
    history = ledger.get_history(topic_label)
    if not history:
        return f"没有关于'{topic_label}'的记录。"
    prompt = DRIFT_PROMPT.replace("__TOPIC__", topic_label).replace(
        "__HISTORY__", wrap_untrusted("HISTORY", format_history(history))
    )
    return call_deepseek([
        {
            "role": "system",
            "content": f"你是一个诚实、不夸大结论的复盘助手。{INJECTION_DEFENSE_NOTE}",
        },
        {"role": "user", "content": prompt},
    ])


def check_implementation(topic_label: str, project_path: str) -> str:
    """比对"当前决定的事情"和"实际代码"对不对得上。第一版不做检索，直接读整个项目目录。"""
    current = ledger.get_current_state(topic_label)
    want = current["want"]["content"] if current["want"] else "（无记录）"
    obstacle = current["obstacle"]["content"] if current["obstacle"] else "（无记录）"

    nodes = ledger.get_history(topic_label, record_type="node")
    if not nodes:
        return f"'{topic_label}'目前没有任何node（具体决定）记录，无法做代码比对。"
    node_text = "\n".join(
        f"[{n['source_end_ts']}] {n['content']}" + (f"（原因：{n['reason']}）" if n["reason"] else "")
        for n in nodes
    )

    # 优先用Hook攒下来的精确diff（借鉴diff_PR）——只喂"实际变了什么"，不用每次重读整个代码库。
    # 项目还没接过Hook、或者接了但还没有任何改动记录时，退回到读整个目录兜底。
    edits = edit_log.read_edits(project_path)
    if edits:
        code = edit_log.format_edits(edits)
        code_source_note = f"（以下是{len(edits)}次代码改动的精确diff记录，不是完整代码库）\n"
    else:
        code = read_project_code(project_path)
        code_source_note = "（Hook还没有积累任何改动记录，以下是读取整个项目目录得到的完整代码）\n"
    if not code:
        return f"在'{project_path}'下没有读到任何源码文件，无法比对。"
    code = code_source_note + code

    prompt = (
        IMPLEMENTATION_PROMPT.replace("__TOPIC__", topic_label)
        .replace("__WANT__", want)
        .replace("__OBSTACLE__", obstacle)
        .replace("__NODES__", wrap_untrusted("DECISIONS", node_text))
        .replace("__CODE__", wrap_untrusted("CODE", code))
    )
    return call_deepseek([
        {
            "role": "system",
            "content": f"你是一个诚实、不夸大结论的代码复核员，找不到证据就说找不到，不要编。{INJECTION_DEFENSE_NOTE}",
        },
        {"role": "user", "content": prompt},
    ])


def synthesize(topic_label: str, project_path: str) -> dict:
    """综合复盘：分别跑漂移检测和代码落地检测，再让模型把两份结果放一起交叉核对。
    返回三段结果，前端可以分别展示，也可以只看synthesis这一段。
    """
    drift_result = check_drift(topic_label)
    impl_result = check_implementation(topic_label, project_path)

    prompt = SYNTHESIS_PROMPT.replace("__DRIFT__", wrap_untrusted("DRIFT_REPORT", drift_result)).replace(
        "__IMPLEMENTATION__", wrap_untrusted("IMPLEMENTATION_REPORT", impl_result)
    )
    synthesis_result = call_deepseek([
        {
            "role": "system",
            "content": (
                "你是一个诚实的综合复盘助手，只基于给你的两份材料做交叉核对，不额外编造新证据。"
                f"{INJECTION_DEFENSE_NOTE}"
            ),
        },
        {"role": "user", "content": prompt},
    ])

    return {
        "drift": drift_result,
        "implementation": impl_result,
        "synthesis": synthesis_result,
    }


if __name__ == "__main__":
    import sys

    topic = sys.argv[1] if len(sys.argv) > 1 else "自动剧本生成Agent"
    print(check_drift(topic))
