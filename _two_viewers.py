"""用两个模拟观众给 Game2 当前字投票。

- 批量读 Luna 日志，不在每行上 sleep
- 只在“游戏已连接”时发送；游戏断开立即停
- 看到“Game2 窗口开始 slot=X”时，用当前关答案的第 X 个字投两票
- A 发“京”，B 发“京666”：内容不同，避免 AI-Vtuber 按 content 去重把 A 丢掉
"""

import json
import re
import time
from pathlib import Path

import requests

LOG_DIR = Path("log")
ALL_LEVELS = Path(r"C:\Users\97550\Desktop\大鹅抓成语AI\assets\game2\level\all_levels.json")
API = "http://127.0.0.1:8082/send"

with ALL_LEVELS.open(encoding="utf-8") as f:
    LEVELS = json.load(f)

log_files = sorted(LOG_DIR.glob("log-*.txt"))
LOG = log_files[-1] if log_files else LOG_DIR / "log.txt"
print("tail", LOG, flush=True)

connected = False
level = None
slot = None
answer = ""


def send_vote(user: str, content: str) -> None:
    body = {
        "type": "comment",
        "data": {"type": "comment", "username": user, "content": content},
    }
    try:
        requests.post(API, json=body, timeout=2)
    except Exception as e:
        print("send error", user, content, e, flush=True)


def set_level(new_level: int) -> None:
    global level, slot, answer
    if new_level == level:
        return
    level = new_level
    slot = None
    answer = str(LEVELS.get(str(level), {}).get("idiom", ""))
    print("level", level, "answer", answer, flush=True)


def feed(line: str) -> None:
    global connected, slot
    if "游戏已连接" in line:
        connected = True
        slot = None
        print("game connected", flush=True)
        return
    if "游戏已断开" in line:
        connected = False
        slot = None
        print("game disconnected", flush=True)
        return

    m = re.search(r"第(\d+)关", line)
    if m:
        set_level(int(m.group(1)))

    m = re.search(r"Game2 窗口开始 slot=(\d+)", line)
    if m:
        # 桥日志是 0-based，第 1 个字是 slot=0
        new_slot = int(m.group(1)) + 1
        if new_slot != slot:
            slot = new_slot
            print("window", slot, "level", level, "char", answer[slot - 1] if len(answer) >= 5 else "?", flush=True)
        return

    m = re.search(r"第(\d+)个字", line)
    if m and slot is None:
        slot = int(m.group(1))
        print("ask", slot, "level", level, "char", answer[slot - 1] if len(answer) >= 5 else "?", flush=True)


with LOG.open(encoding="utf-8", errors="ignore") as f:
    lines = f.readlines()[-2000:]
    for line in lines:
        feed(line)
    # 启动后只认新的“游戏已连接”，不要沿用旧日志里的连接状态
    connected = False
    slot = None
    print("resume level", level, "answer", answer, "waiting for a fresh game connection...", flush=True)

    while True:
        while True:
            line = f.readline()
            if not line:
                break
            feed(line)

        if not connected:
            time.sleep(0.3)
            continue
        if level is None or slot is None or len(answer) < 5 or not (1 <= slot <= 5):
            time.sleep(0.2)
            continue

        char = answer[slot - 1]
        send_vote("模拟观众A", char)
        send_vote("模拟观众B", char + "666")
        time.sleep(0.6)