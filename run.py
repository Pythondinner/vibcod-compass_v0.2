"""把observer/analysis/ledger串成完整流水线，能处理正在进行中的真实session（不是只能事后指定一个固定路径批量跑）。

读transcript -> observer按N轮划检查点 -> 每个检查点问ledger拿已知标签和当前状态 -> analysis提取 -> 写回ledger。
默认循环轮询(session还在继续、transcript文件还在涨)，--once只跑一轮就退出(方便测试)。

用法:
    python run.py <transcript路径> [--once] [--interval 秒数] [--n N]
"""
import argparse
import sys
import time
from pathlib import Path

import analysis
import observer
from storage import capture_log, ledger

DEFAULT_N = 5
DEFAULT_INTERVAL = 30


def session_id_from_path(transcript_path: str) -> str:
    """Claude Code的transcript文件名本身就是session的uuid，直接拿文件名当session_id。"""
    return Path(transcript_path).stem


def run_once(transcript_path: str, session_id: str, n: int) -> int:
    """处理从上次进度之后到目前为止的所有新检查点。返回本次处理了几个检查点。"""
    turns = observer.parse_transcript(transcript_path)
    start_after = ledger.get_progress(session_id)

    processed = 0
    for end_turn_index, window in observer.get_checkpoints(turns, n, start_after_user_turn=start_after):
        known_topics = ledger.get_known_topics()
        current_state_raw = {topic: ledger.get_current_state(topic) for topic in known_topics}
        current_state = {
            topic: {
                "want": state["want"]["content"] if state["want"] else None,
                "obstacle": state["obstacle"]["content"] if state["obstacle"] else None,
            }
            for topic, state in current_state_raw.items()
        }
        threads_state = {topic: ledger.get_threads(topic) for topic in known_topics}

        print(f"[{session_id[:8]}] 检查点(第{end_turn_index}条真人消息)")
        try:
            records = analysis.extract(
                window, existing_labels=known_topics, current_state=current_state, threads_state=threads_state
            )
        except Exception as e:
            print(f"  提取失败: {e}，本检查点跳过，进度不推进，下次重试")
            capture_log.record_failure(session_id, str(e))
            continue

        if not records:
            print("  无实质进展，跳过")
        else:
            for rec in records:
                inserted = ledger.insert_record(
                    topic_label=rec["topic_label"],
                    want=rec.get("want"),
                    obstacle=rec.get("obstacle"),
                    node=rec.get("node"),
                    source_start_ts=window[0]["timestamp"],
                    source_end_ts=window[-1]["timestamp"],
                    session_id=session_id,
                    want_thread=rec.get("want_thread"),
                    want_thread_status=rec.get("want_thread_status"),
                    obstacle_thread=rec.get("obstacle_thread"),
                    obstacle_thread_status=rec.get("obstacle_thread_status"),
                    node_thread=rec.get("node_thread"),
                    node_thread_status=rec.get("node_thread_status"),
                )
                print(f"  [{rec['topic_label']}] 写入了: {inserted}")

        ledger.set_progress(session_id, end_turn_index)
        processed += 1

    return processed


def watch(transcript_path: str, n: int, interval: int) -> None:
    """轮询模式：session还在继续，transcript文件还在涨，定期重新读、处理新增的检查点。Ctrl+C退出。"""
    session_id = session_id_from_path(transcript_path)
    print(f"开始监听 {transcript_path}（session_id={session_id}），每{interval}秒检查一次，Ctrl+C退出")
    try:
        while True:
            processed = run_once(transcript_path, session_id, n)
            if processed == 0:
                print(".", end="", flush=True)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("transcript_path")
    parser.add_argument("--once", action="store_true", help="只跑一轮就退出，不循环轮询")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="轮询间隔秒数")
    parser.add_argument("--n", type=int, default=DEFAULT_N, help="每N条真人消息一个检查点")
    args = parser.parse_args()

    ledger.init_db()

    if args.once:
        sid = session_id_from_path(args.transcript_path)
        count = run_once(args.transcript_path, sid, args.n)
        print(f"\n本次共处理了{count}个检查点")
    else:
        watch(args.transcript_path, args.n, args.interval)
