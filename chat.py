"""阶段0：最基础的对话循环，不带工具、不带记忆。
目的只有一个——先确认DeepSeek API能正常调用，跑通最简单的一问一答。
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

API_URL = "https://api.deepseek.com/chat/completions"
API_KEY = os.getenv("DEEPSEEK_API_KEY")


def call_deepseek(messages: list, json_mode: bool = False) -> str:
    """json_mode=True时用DeepSeek的JSON强制输出模式（response_format），从根源上防止模型
    输出格式错乱的JSON（比如漏逗号）——之前analysis.py的~97%解析成功率问题，
    很大一部分就是这类格式错误，用API自带的约束比事后修复字符串靠谱。
    要求prompt里必须出现"json"字样，我们的prompt本来就有"只输出JSON"，满足这个前提。
    """
    body = {"model": "deepseek-chat", "messages": messages}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    resp = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {API_KEY}"},
        json=body,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def main():
    messages = [{"role": "system", "content": "你是一个乐于助人的助手。"}]
    print("研究笔记本Agent - 阶段0：最小对话循环（输入exit退出）")
    while True:
        user_input = input("\n你: ").strip()
        if user_input.lower() == "exit":
            break
        messages.append({"role": "user", "content": user_input})
        reply = call_deepseek(messages)
        messages.append({"role": "assistant", "content": reply})
        print(f"\nAgent: {reply}")


if __name__ == "__main__":
    main()
