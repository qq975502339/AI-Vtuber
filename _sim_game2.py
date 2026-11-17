"""模拟 Game2 客户端：连 Luna 9001，发一份 state，收 fill/action。

验证：
- 5 个窗口里模拟观众脚本的 /send 是否进桥
- 第 1 个字是否最终选“京”
- 最终 5 字 guess 是否等于 level2 答案“京河泪呵刮”
"""

import asyncio
import json
from pathlib import Path

import websockets

ALL_LEVELS = Path(r"C:\Users\97550\Desktop\大鹅抓成语AI\assets\game2\level\all_levels.json")
LEVEL_ID = 2

with ALL_LEVELS.open(encoding="utf-8") as f:
    levels = json.load(f)
config = levels[str(LEVEL_ID)]
answer = config["idiom"]
groups = config["options"]
print("answer", answer, flush=True)


async def main():
    async with websockets.connect("ws://127.0.0.1:9001") as ws:
        await ws.send(json.dumps({
            "type": "state", "v": 2, "seq": 1, "levelId": LEVEL_ID,
            "attempt": 0, "maxAttempts": 5,
            "visibleStrokes": [1, 1, 1, 1, 1], "totalStrokes": [8, 8, 8, 8, 8],
            "candidateGroups": groups,
            "slots": [{"index": i, "filled": False} for i in range(5)],
            "history": [], "phase": "ready",
        }, ensure_ascii=False))

        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=120)
            msg = json.loads(raw)
            if msg.get("type") == "fill":
                print("fill", msg.get("slotIndex"), msg.get("char"), flush=True)
            elif msg.get("type") == "cmd":
                print("cmd", msg.get("op"), flush=True)
                if msg.get("op") == "next_level":
                    print("DONE", flush=True)
                    break
            elif msg.get("type") == "action":
                guess = msg.get("guess")
                print("ACTION", guess, "MATCH", guess == answer, flush=True)
                await ws.send(json.dumps({
                    "type": "ack", "seq": msg.get("seq"), "stateSeq": msg.get("stateSeq"),
                    "result": "correct" if guess == answer else "wrong",
                    "pct": 100 if guess == answer else 0,
                    "attempt": 1, "maxAttempts": 5, "levelCompleted": True,
                }))
                await ws.send(json.dumps({
                    "type": "event", "v": 2, "event": "level_clear",
                    "levelId": LEVEL_ID, "nextLevelId": LEVEL_ID + 1,
                }, ensure_ascii=False))
            elif msg.get("type") == "event":
                print("event", msg, flush=True)
            else:
                print("other", msg, flush=True)


asyncio.run(main())