"""新数据模型（功能集合体）的网页界面——独立的Flask app，独立端口，不共用老web.py一行代码，
也不共用session_registry.py（老系统cwd路由用的，这次topic_label直接来自Hook给的cwd，
不需要单独一层路由表）。项目列表靠扫turns/目录里真实出现过的cwd + features.db里已经有
记录的topic_label取并集，两者都不依赖老系统的任何状态。
"""
import os

from flask import Flask, jsonify, render_template, request

import intake
from hooks import hook_setup
from storage import edit_log, feature_ledger, turns

os.chdir(os.path.dirname(os.path.abspath(__file__)))

app = Flask(__name__, template_folder="templates_v2")


def clean_path(raw: str) -> str:
    return raw.strip().strip('"\'“”‘’') if raw else raw


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/projects")
def api_projects():
    topics = set(feature_ledger.get_known_topics())
    for cwd in turns.list_known_cwds():
        topics.add(feature_ledger.normalize_topic(cwd))

    result = []
    for topic in sorted(topics):
        pending = intake.count_pending(topic)
        history = feature_ledger.get_history(topic)
        feature_count = len([h for h in history if h["record_type"] == "feature"])
        result.append({
            "topic_label": topic,
            "feature_count": feature_count,
            "pending_turns": pending["pending_turns"],
            "pending_edits": pending["pending_edits"],
            "last_checked": pending["last_checked"],
            "hook_attached": hook_setup.is_attached(topic) if os.path.isdir(topic) else False,
        })
    return jsonify(result)


@app.route("/api/project/<path:topic_label>")
def api_project_detail(topic_label):
    history = feature_ledger.get_history(topic_label)
    nodes_by_feature: dict[str | None, list[dict]] = {}
    for h in history:
        if h["record_type"] == "node":
            nodes_by_feature.setdefault(h["label"], []).append(h)

    features = feature_ledger.get_known(topic_label, "feature")
    for f in features:
        f["nodes"] = nodes_by_feature.get(f["label"], [])
    unlinked_nodes = nodes_by_feature.get(None, [])

    obstacles = feature_ledger.get_known(topic_label, "obstacle")

    pending = intake.count_pending(topic_label)

    return jsonify({
        "topic_label": topic_label,
        "hook_attached": hook_setup.is_attached(topic_label) if os.path.isdir(topic_label) else False,
        "features": features,
        "obstacles": obstacles,
        "unlinked_nodes": unlinked_nodes,
        "pending": pending,
    })


@app.route("/api/project/<path:topic_label>/preview", methods=["POST"])
def api_project_preview(topic_label):
    """只判断、不写库——带上真实对话原文和真实代码diff，让用户自己核对AI判断得对不对，
    确认了才调/commit真正落地。"""
    result = intake.preview_check(topic_label)
    return jsonify(result)


@app.route("/api/project/<path:topic_label>/commit", methods=["POST"])
def api_project_commit(topic_label):
    """用户看完预览、确认没问题之后才调用——真正写库+推进游标+跑代码核对。
    pending_turns和extraction必须是/preview返回的原样内容，不是重新生成的，
    保证"用户看到并确认的"和"真正写进去的"是同一批。"""
    body = request.get_json(silent=True) or {}
    pending_turns = body.get("pending_turns")
    extraction = body.get("extraction")
    if pending_turns is None or extraction is None:
        return jsonify({"error": "缺少pending_turns或extraction，必须先调/preview拿到这两样"}), 400
    result = intake.commit_check(topic_label, pending_turns, extraction)
    return jsonify(result)


@app.route("/api/project/<path:topic_label>/confirm", methods=["POST"])
def api_project_confirm(topic_label):
    body = request.get_json(silent=True) or {}
    feature_label = body.get("feature_label")
    if not feature_label:
        return jsonify({"error": "缺少feature_label"}), 400
    ok = feature_ledger.set_feature_status(topic_label, feature_label, "resolved")
    if not ok:
        return jsonify({"error": "找不到这个功能"}), 404
    return jsonify({"ok": True})


@app.route("/api/attach_hook", methods=["POST"])
def api_attach_hook():
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


if __name__ == "__main__":
    feature_ledger.init_db()
    app.run(debug=True, use_reloader=False, threaded=True, port=5179)
