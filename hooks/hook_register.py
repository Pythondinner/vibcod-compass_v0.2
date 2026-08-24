"""挂在Claude Code的UserPromptSubmit/PostToolUse/Stop三个事件上的Hook脚本，三个事件共用一个入口。
唯一职责：把事件带来的信息原样记一笔，不做任何判断、不调用大模型、几毫秒内跑完退出
——这是diff_PR项目验证过的原则，照着做。

- UserPromptSubmit：记用户这轮的原文(prompt字段) -> turns.py，同时顺手更新session_registry(cwd路由)
- PostToolUse：有代码编辑的才会触发(matcher限定Edit|Write|NotebookEdit) -> edit_log.py(精确diff)
- Stop：记这一轮助手的完整文字回复(last_assistant_message字段) -> turns.py

这次是"从Hook拿数据"整个重新设计的第一步——prompt/prompt_id/last_assistant_message三个
字段名已经拿真实payload验证过(2026-08-24)，不是凭文档猜的：新开会话真实触发过UserPromptSubmit
和Stop，session_id+prompt_id能正确配对出完整的一轮对话，读写链路端到端跑通过。
"""
import json
import os
import sys

# 这个脚本被Claude Code当独立子进程调用,cwd不可控,必须显式把项目根目录(hooks/上一级)
# 加进sys.path,才能跨包导入storage里的模块——单纯的相对导入在"直接运行脚本"场景下用不了。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage import edit_log, session_registry, turns


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return  # 读不到就算了，不阻塞Claude Code，也不报错吓用户

    event = payload.get("hook_event_name")
    session_id = payload.get("session_id")

    if event == "PostToolUse":
        edit_log.append_from_hook_payload(payload)
        return

    if event == "UserPromptSubmit":
        cwd = payload.get("cwd")
        transcript_path = payload.get("transcript_path")
        prompt_id = payload.get("prompt_id")
        prompt_text = payload.get("prompt")

        if cwd and transcript_path and session_id:
            session_registry.update(cwd, transcript_path, session_id)
        if session_id and prompt_id and prompt_text:
            turns.record_user_prompt(session_id, prompt_id, cwd, prompt_text)
        return

    if event == "Stop":
        prompt_id = payload.get("prompt_id")
        assistant_text = payload.get("last_assistant_message")
        if session_id and prompt_id and assistant_text:
            turns.record_assistant_message(session_id, prompt_id, assistant_text)
        return


if __name__ == "__main__":
    main()
