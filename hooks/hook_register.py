"""挂在Claude Code的UserPromptSubmit和PostToolUse事件上的Hook脚本，两个事件共用一个入口。
唯一职责：把事件带来的信息原样记一笔，不做任何判断、不调用大模型、几毫秒内跑完退出
——这是diff_PR项目验证过的原则，照着做。

- UserPromptSubmit的payload没有tool_name字段，记进session_registry（活跃session登记表）
- PostToolUse的payload有tool_name字段，记进edit_log（精确diff日志），配合hook_setup.py
  注册时用的matcher（Edit|Write|NotebookEdit）,只有真正的代码改动才会触发这条
"""
import json
import os
import sys

# 这个脚本被Claude Code当独立子进程调用,cwd不可控,必须显式把项目根目录(hooks/上一级)
# 加进sys.path,才能跨包导入storage里的模块——单纯的相对导入在"直接运行脚本"场景下用不了。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage import edit_log, session_registry


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return  # 读不到就算了，不阻塞Claude Code，也不报错吓用户

    try:
        if payload.get("tool_name"):
            edit_log.append_from_hook_payload(payload)
            return

        cwd = payload.get("cwd")
        transcript_path = payload.get("transcript_path")
        session_id = payload.get("session_id")

        if cwd and transcript_path and session_id:
            session_registry.update(cwd, transcript_path, session_id)
    except Exception:
        # 这个脚本的唯一职责是"记一笔",不该因为存储层任何异常(文件损坏/权限/磁盘满)
        # 崩掉整个Hook调用——记不上这一笔不影响Claude Code继续用,静默跳过就好。
        return


if __name__ == "__main__":
    main()
