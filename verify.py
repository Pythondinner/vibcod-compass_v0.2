""""代码说话"——拿一个功能名下积累的决定，对照真实代码，判断这个功能有没有真的被实现。
跟老brain.py的check_implementation是同一种机制（决定vs代码逐条比对，找不到证据就说找不到，
不猜），但这次是围绕"功能"而不是"话题当前状态"组织的，且完全独立于ledger.py/brain.py，
不共用一行代码——跟feature_ledger.py/intake.py/turns.py保持同一条隔离原则。

只负责判断+给证据，不负责写库改状态——功能状态要不要标"已实现"，是后面单独一层的事，
判断权留给用户，这条原则沿用老项目已经验证过的结论，不因为这次换了数据模型就破例。

真实测试撞到过一个问题：自由文本输出时，模型会在核对完列出的决定之后，自己额外补充一段
"顺带一提XX模块也是这样"，内容是编的（引用的文件确实存在，但下的结论没有依据）——这种
跑题不受"不要编"这条prompt要求的约束，因为它发生在"结论"之外的自由发挥区域。改成结构化
输出后，decision_checks数组的条目数在解析完之后用代码强制跟输入的决定数一一对应，多出来的
直接丢弃——这部分是真正的硬约束，不是靠prompt说服模型；但每条evidence文字本身写了什么，
仍然只能靠prompt里"只能围绕这一条决定本身展开"这句话去约束，这部分做不到硬堵，是软约束。
"""
import json
import os

from chat import call_deepseek
from prompt_safety import INJECTION_DEFENSE_NOTE, wrap_untrusted
from storage import edit_log, feature_ledger

CODE_EXTENSIONS = {".py"}
CODE_EXCLUDE_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules", "工程日志"}
CODE_EXCLUDE_FILES_PREFIX = ("test_",)

VERDICT_LABELS = {
    "confirmed": "已兑现",
    "partial": "部分兑现",
    "not_found": "未兑现",
    "unclear": "无法判断",
}
OVERALL_VERDICTS = {"已实现", "部分实现", "未实现", "无法判断"}


def read_project_code(project_path: str) -> str:
    """第一版不做检索/分块，直接读整个项目目录的源码文件——跟老brain.py同样的简化，
    大项目会不会顶不住还没测过，先在小项目上验证判断逻辑本身对不对。"""
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


def read_code_for_check(project_path: str) -> str | None:
    """优先用Hook抓的精确diff（edit_log，已经是保留下来的共用基础设施，不是要隔离掉的老逻辑），
    没有diff记录时退回读整个目录。"""
    edits = edit_log.read_edits(project_path)
    if edits:
        return f"（以下是{len(edits)}次代码改动的精确diff记录，不是完整代码库）\n" + edit_log.format_edits(edits)
    code = read_project_code(project_path)
    return (f"（Hook还没有积累任何改动记录，以下是读取整个项目目录得到的完整代码）\n" + code) if code else None


VERIFY_PROMPT = """你是一个诚实的代码复核员，要检查一个功能"要做的事情"和"实际写出来的代码"对不对得上。

功能名称："__FEATURE_LABEL__"
功能描述：__FEATURE_CONTENT__

这个功能名下积累的具体技术决定，按顺序编号（越靠后越新）：
__NODES__

下面是这个项目目前的真实源代码：
__CODE__

任务：
1. 先用你自己的话简短复述"这个功能主要要做到什么"（2-3句话，不要照抄原文）。
2. 对上面编号的每一条决定，**逐条**判断有没有真的体现在代码里，引用具体文件名和代码片段/函数名
   作为证据，找不到证据就说找不到、不要猜。**每一条的判断只能围绕这一条决定本身展开，不要在这里
   评价决定列表之外的其他模块/文件/功能——哪怕你在代码里看到了什么，只要跟当前这条决定无关，
   都不要写进来。**
3. 给一个整体结论。

只输出JSON，格式：
{
  "understanding": "复述内容",
  "decision_checks": [
    {"verdict": "confirmed"或"partial"或"not_found"或"unclear", "evidence": "只围绕这一条决定的证据或说明"},
    ...
  ],
  "overall_verdict": "已实现"或"部分实现"或"未实现"或"无法判断"
}

decision_checks数组必须严格按顺序对应上面编号的决定，一条决定对应一个条目，不多不少——
不要因为想补充别的发现就多加条目，也不要合并多条决定成一条。整体结论只有当所有决定都
confirmed才算"已实现"，不要为了显得有用就放宽标准。
"""


def _format_nodes(nodes: list[dict]) -> str:
    if not nodes:
        return "（这个功能名下还没有记录任何具体决定，只有功能描述本身）"
    lines = []
    for i, n in enumerate(nodes):
        line = f"{i + 1}. [{n['created_at']}] {n['content']}"
        if n["reason"]:
            line += f"（理由：{n['reason']}）"
        lines.append(line)
    return "\n".join(lines)


def _render(feature_label: str, understanding: str, nodes: list[dict], checks: list[dict], overall: str) -> str:
    lines = [f"【结论：{overall}】", "", f"**功能理解复述**：{understanding}", "", "**逐条核对**："]
    for i, n in enumerate(nodes):
        check = checks[i] if i < len(checks) else {"verdict": "unclear", "evidence": "（模型没有对这条决定给出判断）"}
        verdict_label = VERDICT_LABELS.get(check.get("verdict"), "无法判断")
        lines.append(f"\n{i + 1}. {n['content']} —— {verdict_label}\n   {check.get('evidence', '')}")
    if len(checks) > len(nodes):
        lines.append(
            f"\n（模型额外输出了{len(checks) - len(nodes)}条不对应任何输入决定的内容，已丢弃，不采信——"
            "这类跑题内容不受“不要编”这条要求的约束，所以在解析结果时直接截断，不展示给你看。）"
        )
    return "\n".join(lines)


def check_feature(topic_label: str, feature_label: str, project_path: str) -> dict:
    """返回{"text": 给人看的完整判断文字, "verdict": 已实现/部分实现/未实现/无法判断/None}。
    verdict单独拎出来是为了让调用方（比如确认流程）能用代码判断"这次要不要提示用户确认"，
    不用去解析text里那行【结论：...】的字符串。verdict为None代表流程本身失败了
    （功能不存在/读不到代码/模型没输出合法JSON），跟"无法判断"这个真实判断结果是两回事。"""
    history = feature_ledger.get_history(topic_label)
    feature_records = [h for h in history if h["record_type"] == "feature" and h["label"] == feature_label]
    if not feature_records:
        return {"text": f"'{topic_label}'下没有名为'{feature_label}'的功能记录。", "verdict": None}
    feature_content = feature_records[-1]["content"]

    nodes = [h for h in history if h["record_type"] == "node" and h["label"] == feature_label]

    code = read_code_for_check(project_path)
    if not code:
        return {"text": f"在'{project_path}'下没有读到任何源码文件，无法比对。", "verdict": None}

    prompt = (
        VERIFY_PROMPT.replace("__FEATURE_LABEL__", feature_label)
        .replace("__FEATURE_CONTENT__", feature_content)
        .replace("__NODES__", wrap_untrusted("DECISIONS", _format_nodes(nodes)))
        .replace("__CODE__", wrap_untrusted("CODE", code))
    )
    reply = call_deepseek(
        [
            {
                "role": "system",
                "content": f"你是一个诚实、不夸大结论的代码复核员，找不到证据就说找不到，不要编。你只输出JSON。{INJECTION_DEFENSE_NOTE}",
            },
            {"role": "user", "content": prompt},
        ],
        json_mode=True,
    )
    try:
        data = json.loads(reply)
    except json.JSONDecodeError:
        return {"text": f"（模型返回的不是合法JSON，原始内容如下）\n{reply}", "verdict": None}

    understanding = data.get("understanding", "")
    checks = data.get("decision_checks", [])
    overall = data.get("overall_verdict", "无法判断")
    if overall not in OVERALL_VERDICTS:
        overall = "无法判断"

    # 硬约束在这里执行：decision_checks只取跟nodes数量对应的前N条，多出来的丢弃不采信。
    checks = checks[: len(nodes)]

    return {"text": _render(feature_label, understanding, nodes, checks, overall), "verdict": overall}


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 4:
        print("用法: python verify.py <topic_label> <feature_label> <project_path>")
        sys.exit(1)
    print(check_feature(sys.argv[1], sys.argv[2], sys.argv[3])["text"])
