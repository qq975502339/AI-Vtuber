import asyncio
import json
from pathlib import Path

import websockets

SCRIPTS = Path(r"C:\Users\97550\Desktop\大鹅抓成语AI\assets\game1\levels\game1_scripts.json")
LEVEL_ID = 2

with SCRIPTS.open(encoding="utf-8") as f:
    data = json.load(f)
question = next(q for q in data["questions"] if q["levelId"] == LEVEL_ID and q["itemIndex"] == 0)
answer = question["answer"]
print("solo level", LEVEL_ID, "answer", answer, flush=True)

char_pool = answer + "哀经栋炒"
slots = [{"index": i, "filled": False} for i in range(4)]
elements = [{"index": i, "type": "image", "desc": ""} for i in range(4)]
state_seq = 0


def make_state():
    return {
        "type": "state", "v": 2, "seq": state_seq, "questionNo": question.get("questionNo", 1),
        "levelId": LEVEL_ID, "itemIndex": 0, "remainingLives": 3,
        "elements": elements,
        "charPool": [{"char": c, "available": True} for c in char_pool],
        "slots": slots,
    }


async def recv(ws, timeout=20):
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


async def main():
    global state_seq
    async with websockets.connect("ws://127.0.0.1:9001") as ws:
        # 第一次失败 -> retry
        await ws.send(json.dumps({"type": "event", "v": 2, "event": "level_fail", "levelId": 1}))
        first = await recv(ws, 10)
        print("first fail ->", first, flush=True)
        assert first.get("op") == "retry_level", first

        # 第二次失败 -> next_level，并标记下一关单人
        await ws.send(json.dumps({"type": "event", "v": 2, "event": "level_fail", "levelId": 1}))
        second = await recv(ws, 10)
        print("second fail ->", second, flush=True)
        assert second.get("op") == "next_level", second

        # 下一关 state，应该单人：不弹 6 秒窗口，直接 action
        for slot in range(4):
            state_seq += 1
            await ws.send(json.dumps(make_state(), ensure_ascii=False))
            while True:
                msg = await recv(ws, 60)
                if msg.get("type") == "cmd":
                    print("cmd", msg.get("op"), flush=True)
                    continue
                if msg.get("type") == "action":
                    print("solo action", msg.get("char"), msg.get("slotIndex"), flush=True)
                    assert msg.get("char") == answer[slot], (msg, answer)
                    slots[slot]["filled"] = True
                    await ws.send(json.dumps({
                        "type": "ack", "seq": msg.get("seq"), "stateSeq": msg.get("stateSeq"),
                        "result": "correct", "remainingLives": 3,
                    }))
                    break

        for item in range(4):
            await ws.send(json.dumps({"type": "event", "v": 2, "event": "question_clear", "levelId": LEVEL_ID, "itemIndex": item}))
            await asyncio.sleep(0.3)
        await ws.send(json.dumps({"type": "event", "v": 2, "event": "level_clear", "levelId": LEVEL_ID, "nextLevelId": 3}))

        pauses = resumes = 0
        while True:
            msg = await recv(ws, 120)
            print("event", msg, flush=True)
            if msg.get("type") == "cmd":
                if msg.get("op") == "pause":
                    pauses += 1
                elif msg.get("op") == "resume":
                    resumes += 1
                elif msg.get("op") == "next_level":
                    break

    print("pauses", pauses, "resumes", resumes, flush=True)
    print("GAME1 SOLO OK", flush=True)


asyncio.run(main())