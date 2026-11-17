"""在真实 Game1 页面里故意投错，驱动同一关连败 2 次，验证下一关单人。

盯 Luna 日志里 type=image 的 state，用两个用户投“错误答案”；
text 元素不投，桥会直接给正确答案。
"""

import json
import re
import time
from pathlib import Path

import requests

LOG = Path("log/log-2026-11-15.txt")
SCRIPTS = Path(r"C:\Users\97550\Desktop\大鹅抓成语AI\assets\game1\levels\game1_scripts.json")
API = "http://127.0.0.1:8082/send"

with SCRIPTS.open(encoding="utf-8") as f:
    data = json.load(f)


def answer_of(level: int, item: int) -> str:
    q = next((x for x in data["questions"] if x["levelId"] == level and x["itemIndex"] == item), None)
    return q["answer"] if q else ""


def send_vote(user: str, content: str) -> None:
    body = {"type": "comment", "data": {"type": "comment", "username": user, "content": content}}
    try:
        requests.post(API, json=body, timeout=2)
    except Exception as e:
        print("send error", e, flush=True)


with LOG.open(encoding="utf-8", errors="ignore") as f:
    f.seek(0, 2)
    while True:
        line = f.readline()
        if not line:
            time.sleep(0.1)
            continue
        line = re.sub(r"\x1b\[[0-9;]*m", "", line)
        m = re.search(r"state (\d+)-(\d+) slot=(\d+) type=image char=(\S+)", line)
        if not m:
            continue
        level, item, slot, current = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
        answer = answer_of(level, item)
        if len(answer) < 4:
            continue
        wrong = next((c for c in answer if c != current), "")
        if not wrong:
            continue
        print(f"state {level}-{item} slot={slot} current={current} wrong={wrong}", flush=True)
        send_vote("模拟观众A", wrong)
        send_vote("模拟观众B", wrong + "666")