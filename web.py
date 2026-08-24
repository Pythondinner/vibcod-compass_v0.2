"""一个简单的只读前端，直接连真实的ledger.db，看现在笔记本里到底记了什么。
不是Executor的报告功能，纯粹是给自己浏览数据用的。
"""
import os

from flask import Flask, jsonify, render_template, request

import brain
import observer
import run as run_module
from hooks import hook_setup
from storage import ledger, reports, session_registry, topic_paths

os.chdir(os.path.dirname(os.path.abspath(__file__)))

app = Flask(__name__)


def clean_path(raw: str) -> str:
    """从资源管理器"复制为路径"粘贴过来的字符串两端总带着引号，这里统一清掉，
    不用每次手动删。"""
    return raw.strip().strip('"\'“”‘’') if raw else raw


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/topics")
def api_topics():
    """不带project_path就是全部话题;带了就只返回跟这个项目文件夹关联得上的话题——
    关联靠两条线索:topic_paths里手动记过的路径,加上session_registry反查这个文件夹当前
    活跃session名下写过哪些话题(顺手把反查出来的关系记进topic_paths,下次不用再反查)。"""
    project_path = clean_path(request.args.get("project_path"))
    allowed = None
    if project_path:
        allowed = set(topic_paths.topics_for_path(project_path))
        entry = session_registry.get(project_path)
        if entry and entry.get("session_id"):
            for t in ledger.get_topics_by_session(entry["session_id"]):
                allowed.add(t)
                if not topic_paths.get_path(t):
                    topic_paths.set_path(t, project_path)

    topics = ledger.get_known_topics()
    result = []
    for topic in topics:
        if allowed is not None and topic not in allowed:
            continue
        history = ledger.get_history(topic)
        if not history:
            continue
        result.append({
            "name": topic,
            "record_count": len(history),
            "first_ts": history[0]["source_end_ts"],
            "last_ts": history[-1]["source_end_ts"],
        })
    result.sort(key=lambda t: t["last_ts"] or "", reverse=True)
    return jsonify(result)


@app.route("/api/topic/<path:topic_label>")
def api_topic_detail(topic_label):
    current = ledger.get_current_state(topic_label)
    history = ledger.get_history(topic_label)
    return jsonify({
        "current": {
            "want": current["want"]["content"] if current["want"] else None,
            "obstacle": current["obstacle"]["content"] if current["obstacle"] else None,
        },
        "project_path": topic_paths.get_path(topic_label),
        "hook_attached": (
            hook_setup.is_attached(topic_paths.get_path(topic_label))
            if topic_paths.get_path(topic_label) and os.path.isdir(topic_paths.get_path(topic_label))
            else False
        ),
        "history": [
            {
                "record_type": h["record_type"],
                "content": h["content"],
                "reason": h["reason"],
                "source_end_ts": h["source_end_ts"],
            }
            for h in history
        ],
        "threads": [
            {
                "thread_label": t["thread_label"],
                "record_type": t["record_type"],
                "thread_status": t["thread_status"] or "open",
                "content": t["content"],
                "source_end_ts": t["source_end_ts"],
                "record_count": t["record_count"],
            }
            for t in sorted(ledger.get_threads(topic_label), key=lambda t: t["source_end_ts"] or "", reverse=True)
        ],
    })


@app.route("/api/topic/<path:topic_label>/thread/<path:thread_label>")
def api_thread_history(topic_label, thread_label):
    record_type = request.args.get("record_type")
    history = ledger.get_thread_history(topic_label, thread_label, record_type=record_type)
    return jsonify([
        {
            "record_type": h["record_type"],
            "content": h["content"],
            "reason": h["reason"],
            "source_end_ts": h["source_end_ts"],
            "thread_status": h["thread_status"],
        }
        for h in history
    ])


@app.route("/api/topic/<path:topic_label>/thread/<path:thread_label>/drift", methods=["POST"])
def api_thread_drift(topic_label, thread_label):
    record_type = request.args.get("record_type")
    result = brain.check_thread_drift(topic_label, thread_label, record_type=record_type)
    entry = reports.save_report(topic_label, "drift", {"drift": result}, note=f"关注线：{thread_label}")
    return jsonify({"result": result, "report": entry})


@app.route("/api/topic/<path:topic_label>/drift", methods=["POST"])
def api_topic_drift(topic_label):
    result = brain.check_drift(topic_label)
    entry = reports.save_report(topic_label, "drift", {"drift": result})
    return jsonify({"result": result, "report": entry})


@app.route("/api/topic/<path:topic_label>/implementation", methods=["POST"])
def api_topic_implementation(topic_label):
    body = request.get_json(silent=True) or {}
    project_path = clean_path(body.get("project_path")) or topic_paths.get_path(topic_label)
    if not project_path:
        return jsonify({"error": "没有设置项目代码路径"}), 400
    if not os.path.isdir(project_path):
        return jsonify({"error": f"路径不存在或不是目录：{project_path}"}), 400

    topic_paths.set_path(topic_label, project_path)
    result = brain.check_implementation(topic_label, project_path)
    entry = reports.save_report(topic_label, "implementation", {"implementation": result})
    return jsonify({"result": result, "report": entry})


@app.route("/api/topic/<path:topic_label>/pending")
def api_topic_pending(topic_label):
    """不调模型，纯计数：自上次处理的检查点以来，新增了多少条消息、多少次代码改动。"""
    current = ledger.get_current_state(topic_label)
    session_id = None
    for key in ("want", "obstacle"):
        if current[key]:
            session_id = current[key]["session_id"]
            break
    if not session_id:
        return jsonify({"available": False, "reason": "找不到这个话题关联的session"})

    transcript_path = observer.find_transcript_by_session_id(session_id)
    if not transcript_path:
        return jsonify({"available": False, "reason": "transcript文件已经不在了"})

    progress = ledger.get_progress(session_id)
    result = observer.count_pending_activity(transcript_path, progress)
    result["available"] = True
    return jsonify(result)


@app.route("/api/topic/<path:topic_label>/sync", methods=["POST"])
def api_topic_sync(topic_label):
    """立即同步：手动触发一次追平，把新增的消息喂进流水线。"""
    current = ledger.get_current_state(topic_label)
    session_id = None
    for key in ("want", "obstacle"):
        if current[key]:
            session_id = current[key]["session_id"]
            break
    if not session_id:
        return jsonify({"error": "找不到这个话题关联的session"}), 400

    transcript_path = observer.find_transcript_by_session_id(session_id)
    if not transcript_path:
        return jsonify({"error": "transcript文件已经不在了"}), 400

    processed = run_module.run_once(transcript_path, session_id, run_module.DEFAULT_N)
    return jsonify({"processed_checkpoints": processed})


@app.route("/api/attach_hook", methods=["POST"])
def api_attach_hook_standalone():
    """独立入口：不需要先有话题。给一个全新项目接监控用这个，话题会在真实对话发生后自己长出来。"""
    body = request.get_json(silent=True) or {}
    project_path = clean_path(body.get("project_path"))
    if not project_path:
        return jsonify({"error": "没有填项目文件夹路径"}), 400
    if not os.path.isdir(project_path):
        return jsonify({"error": f"路径不存在或不是目录：{project_path}"}), 400

    already_attached = hook_setup.is_attached(project_path)
    hook_setup.attach(project_path)
    return jsonify({
        "attached": True,
        "already_attached": already_attached,
        "settings_path": hook_setup.settings_path_for(project_path),
    })


@app.route("/api/topic/<path:topic_label>/attach_hook", methods=["POST"])
def api_attach_hook(topic_label):
    body = request.get_json(silent=True) or {}
    project_path = clean_path(body.get("project_path")) or topic_paths.get_path(topic_label)
    if not project_path:
        return jsonify({"error": "没有设置项目代码路径"}), 400
    if not os.path.isdir(project_path):
        return jsonify({"error": f"路径不存在或不是目录：{project_path}"}), 400

    topic_paths.set_path(topic_label, project_path)
    already_attached = hook_setup.is_attached(project_path)
    hook_setup.attach(project_path)
    return jsonify({
        "attached": True,
        "already_attached": already_attached,
        "settings_path": hook_setup.settings_path_for(project_path),
    })


@app.route("/api/topic/<path:topic_label>/synthesis", methods=["POST"])
def api_topic_synthesis(topic_label):
    body = request.get_json(silent=True) or {}
    project_path = clean_path(body.get("project_path")) or topic_paths.get_path(topic_label)
    if not project_path:
        return jsonify({"error": "没有设置项目代码路径"}), 400
    if not os.path.isdir(project_path):
        return jsonify({"error": f"路径不存在或不是目录：{project_path}"}), 400

    topic_paths.set_path(topic_label, project_path)
    result = brain.synthesize(topic_label, project_path)
    entry = reports.save_report(topic_label, "synthesis", result)
    result["report"] = entry
    return jsonify(result)


@app.route("/api/topic/<path:topic_label>/reports")
def api_list_reports(topic_label):
    return jsonify(reports.list_reports(topic_label))


@app.route("/api/reports/<report_id>")
def api_get_report(report_id):
    content = reports.read_report_content(report_id)
    if content is None:
        return jsonify({"error": "报告不存在"}), 404
    return jsonify({"content": content})


@app.route("/api/reports/<report_id>/note", methods=["POST"])
def api_update_report_note(report_id):
    body = request.get_json(silent=True) or {}
    note = body.get("note", "")
    ok = reports.update_note(report_id, note)
    if not ok:
        return jsonify({"error": "报告不存在"}), 404
    return jsonify({"ok": True})


@app.route("/api/reports/<report_id>", methods=["DELETE"])
def api_delete_report(report_id):
    ok = reports.delete_report(report_id)
    if not ok:
        return jsonify({"error": "报告不存在"}), 404
    return jsonify({"ok": True})


if __name__ == "__main__":
    # use_reloader=False：debug模式默认的自动重载会在源文件变化时重启进程，
    # 如果这时候正好有一个长时间请求(漂移检测/综合复盘)还没跑完，连接会被腰斩，
    # 浏览器端看到的就是"Failed to fetch"——真实踩过这个坑，关掉重载，改代码后手动重启服务器。
    # threaded=True：允许并发处理多个请求，避免一个长请求卡住其他请求。
    app.run(debug=True, use_reloader=False, threaded=True, port=5178)
