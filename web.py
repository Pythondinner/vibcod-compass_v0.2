"""一个简单的只读前端，直接连真实的ledger.db，看现在笔记本里到底记了什么。
不是Executor的报告功能，纯粹是给自己浏览数据用的。
"""
import os
import threading
import time

from flask import Flask, jsonify, render_template, request

import brain
import observer
import run as run_module
from hooks import hook_setup
from storage import capture_log, ledger, reports, session_registry, topic_paths

os.chdir(os.path.dirname(os.path.abspath(__file__)))

app = Flask(__name__)

DISCOVERY_INTERVAL = 30  # 秒,跟run.py原来给单项目轮询用的间隔对齐


def _discovery_loop():
    """接上Hook这个动作本身只登记session、抓diff，不会触发提取——一个全新项目、还没有
    任何话题的时候，网页端原来没有任何东西会主动去处理它，必须手动跑一次run.py才会第一次
    冒出来，这跟"接上Hook=会被自动监控"的预期不符。这个后台线程补上这个缺口：定期把
    session_registry里登记过的所有项目各自跑一遍run_once，不用等用户点开某个已存在的话题。
    没有新检查点时run_once本身很轻(不会真的调模型)，多个来源并发调用run_once现在也安全——
    今天已经在ledger层加了去重的唯一索引，撞了也只会被忽略,不会插出重复记录。
    """
    while True:
        try:
            for project_path, entry in session_registry.list_all().items():
                session_id = entry.get("session_id")
                transcript_path = entry.get("transcript_path")
                if not session_id or not transcript_path or not os.path.exists(transcript_path):
                    continue
                try:
                    run_module.run_once(transcript_path, session_id, run_module.DEFAULT_N)
                except Exception as e:
                    capture_log.record_failure(session_id, str(e))
        except Exception:
            pass  # 后台线程本身绝不能因为任何异常挂掉,不然发现能力就悄悄失效了
        time.sleep(DISCOVERY_INTERVAL)


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
    history = ledger.get_history(topic_label)
    return jsonify({
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
                "stalled": t["stalled"],
                "priority": t["priority"],
                "priority_reason": t["priority_reason"],
                "content": t["content"],
                "source_end_ts": t["source_end_ts"],
                "record_count": t["record_count"],
                "related_want_thread": t["related_want_thread"] if t["record_type"] == "obstacle" else None,
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


@app.route("/api/topic/<path:topic_label>/thread/<path:thread_label>/priority", methods=["POST", "DELETE"])
def api_thread_priority(topic_label, thread_label):
    record_type = request.args.get("record_type")
    if not record_type:
        return jsonify({"error": "缺少record_type"}), 400

    if request.method == "DELETE":
        ledger.clear_thread_priority(topic_label, record_type, thread_label)
        return jsonify({"ok": True})

    body = request.get_json(silent=True) or {}
    priority = body.get("priority")
    if priority not in ("now", "later"):
        return jsonify({"error": "priority只能是'now'或'later'"}), 400
    ledger.set_thread_priority(topic_label, record_type, thread_label, priority, reason=body.get("reason") or None)
    return jsonify({"ok": True})


@app.route("/api/topic/<path:topic_label>/thread/<path:thread_label>/status", methods=["POST"])
def api_thread_status(topic_label, thread_label):
    """手动把一条线标成resolved/open——resolved的唯一来源本来是AI在提取时看到对话里明说
    "解决了"，大量真实完成是安静的、没人在对话里宣布，这个入口让用户自己纠正。

    "消消乐"联动：如果标resolved的是一条want线（特点），顺手查一下有没有关联着它、还没解决的
    obstacle线（提取时AI判断的可选关联，见related_want_thread），有的话一并返回给前端，
    由前端弹出来问"要不要顺便也标掉"——只返回建议，不自动写库，跟AI建议已解决走的是同一条
    "判断权留给用户"的原则，不因为这次的关联信号更具体就破例自动清。反方向（标resolved一条
    obstacle）不触发联动：卡点解决了不代表对应的特点就完整做完了，不能反推。"""
    record_type = request.args.get("record_type")
    if not record_type:
        return jsonify({"error": "缺少record_type"}), 400
    body = request.get_json(silent=True) or {}
    status = body.get("status")
    if status not in ("resolved", "open"):
        return jsonify({"error": "status只能是'resolved'或'open'"}), 400
    ok = ledger.mark_thread_status(topic_label, record_type, thread_label, status)
    if not ok:
        return jsonify({"error": "没找到这条线"}), 404

    linked_obstacles = []
    if status == "resolved" and record_type == "want":
        linked_obstacles = ledger.get_linked_obstacles(topic_label, thread_label)
    return jsonify({"ok": True, "linked_obstacles": linked_obstacles})


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
    suggested = brain.suggest_resolved_threads(topic_label, result)
    return jsonify({
        "result": result,
        "report": entry,
        "suggested_resolved_threads": [{"thread_label": t, "record_type": "node"} for t in suggested],
    })


def _health_fields(topic_label: str, session_id: str | None) -> dict:
    """失败次数/Hook活跃时间——不依赖transcript还在不在，跟"待同步计数"是独立的两件事，
    transcript没了也应该照样能看到"最近是不是出过问题"。"""
    failures = capture_log.recent_failures(session_id, since_hours=24) if session_id else []
    project_path = topic_paths.get_path(topic_label)
    hook_entry = session_registry.get(project_path) if project_path else None
    return {
        "recent_failure_count": len(failures),
        "last_failure": failures[-1] if failures else None,
        "hook_last_seen": hook_entry["last_seen"] if hook_entry else None,
    }


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
        return jsonify({"available": False, "reason": "找不到这个话题关联的session", **_health_fields(topic_label, None)})

    transcript_path = observer.find_transcript_by_session_id(session_id)
    if not transcript_path:
        return jsonify({"available": False, "reason": "transcript文件已经不在了", **_health_fields(topic_label, session_id)})

    progress = ledger.get_progress(session_id)
    result = observer.count_pending_activity(transcript_path, progress)
    result["available"] = True
    result.update(_health_fields(topic_label, session_id))
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
    # 之前只有run.py/monitor.py的入口会调init_db,单独跑web.py(比如只接了Hook、
    # 还没手动跑过run.py)时新加的表/字段不会被建出来——这次加related_want_thread字段时
    # 真实撞到了：本地notebook.db已经很久没被run.py的__main__碰过,新字段缺失。
    # 这里补上,保证只启动web.py也能拿到最新schema,不用记着"记得先跑一次run.py"。
    ledger.init_db()
    threading.Thread(target=_discovery_loop, daemon=True).start()

    # use_reloader=False：debug模式默认的自动重载会在源文件变化时重启进程，
    # 如果这时候正好有一个长时间请求(漂移检测/综合复盘)还没跑完，连接会被腰斩，
    # 浏览器端看到的就是"Failed to fetch"——真实踩过这个坑，关掉重载，改代码后手动重启服务器。
    # threaded=True：允许并发处理多个请求，避免一个长请求卡住其他请求。
    app.run(debug=True, use_reloader=False, threaded=True, port=5178)
