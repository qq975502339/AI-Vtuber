"""假直播助手：连 5001，按 Luna 日志里的 Game2 窗口发两个观众的弹幕。

5001 的格式是扁平的：
{"type":"comment","username":"模拟观众A","content":"京"}

不经过 8082 /send，完整走：
5001 on_message -> process_data -> 单字闸门 -> GameBridge.on_comment
"""

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path

import websockets

LOG = Path(r"C:\Users\97550\Desktop\AI-Vtuber\log") / f"log-{datetime.now():%Y-%m-%d}.txt"
SCRIPTS = Path(r"C:\Users\97550\Desktop\大鹅抓成语AI\assets\game2\level\game2_scripts.json")
WS_URL = "ws://127.0.0.1:5001"

with SCRIPTS.open(encoding="utf-8") as f:
    data = json.load(f)
levels = {int(level["levelId"]): level for level in data["levels"]}


async def main() -> None:
    level = 2
    answer = levels[level]["answer"]
    print("fake assistant ready, level", level, "answer", answer, flush=True)

    async with websockets.connect(WS_URL) as ws:
        with LOG.open(encoding="utf-8", errors="ignore") as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if not line:
                    await asyncio.sleep(0.1)
                    continue
                line = re.sub(r"\x1b\[[0-9;]*m", "", line)

                m = re.search(r"第(\d+)关，开始", line)
                if m:
                    level = int(m.group(1))
                    answer = levels.get(level, {}).get("answer", answer)
                    print("level", level, "answer", answer, flush=True)
                    continue

                m = re.search(r"Game2 窗口开始 slot=(\d+)", line)
                if not m or len(answer) < 5:
                    continue
                slot = int(m.group(1))
                char = answer[slot]
                await ws.send(json.dumps({"type": "comment", "username": "模拟观众A", "content": char}, ensure_ascii=False))
                await ws.send(json.dumps({"type": "comment", "username": "模拟观众B", "content": char + "666"}, ensure_ascii=False))
                print("sent slot", slot, char, flush=True)


asyncio.run(main())