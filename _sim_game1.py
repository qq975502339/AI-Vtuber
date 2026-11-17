"""模拟 Game1 客户端：连 Luna 9001，跑一题 4 个图片元素 + 3+2 + next/retry。

- 每个图片窗口用两个用户 /send 正确答案
- 4 个元素后发 question_clear
- 验证 action、pause/resume、next_level、retry_level
"""

import asyncio
import json
from pathlib import Path

import requests
import websockets

SCRIPTS = Path(r"C:\Users\97550\Desktop\大鹅抓成语AI\assets\game1\levels\game1_scripts.json")
API = "http://127.0.0.1:8082/send"

with SCRIPTS.open(encoding="utf-8") as f:
    data = json.load(f)
question = next(q for q in data["questions"] if q["levelId"] == 1 and q["itemIndex"] == 0)
answer = question["answer"]
print("answer", answer, flush=True)

char_pool = answer + "哀经栋炒"
slots = [{"index": i, "filled": False} for i in range(4)]
elements = [{"index": i, "type": "image", "desc": ""} for i in range(4)]
state_seq = 0


def send_vote(user: str, content: str) -> None:
    body = {"type": "comment", "data": {"type": "comment", "username": user, "content": content}}
    try:
        requests.post(API, json=body, timeout=2)
    except Exception as e:
        print("send error", e, flush=True)


def make_state() -> dict:
    return {
        "type": "state", "v": 2, "seq": state_seq, "questionNo": 1,
        "levelId": 1, "itemIndex": 0, "remainingLives": 3,
        "elements": elements,
        "charPool": [{"char": c, "available": True} for c in char_pool],
        "slots": slots,
    }


async def recv(ws, timeout=20):
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    msg = json.loads(raw)
    return msg


async def main():
    global state_seq
    pauses = resumes = 0
    next_seen = retry_seen = False
    async with websockets.connect("ws://127.0.0.1:9001") as ws:
        for slot in range(4):
            state_seq += 1
            await ws.send(json.dumps(make_state(), ensure_ascii=False))
            await asyncio.sleep(0.4)
            send_vote("模拟观众A", answer[slot])
            send_vote("模拟观众B", answer[slot] + "666")

            # 读 action；中间可能有 cmd
            while True:
                msg = await recv(ws, 20)
                if msg.get("type") == "cmd":
                    print("cmd", msg.get("op"), flush=True)
                    continue
                if msg.get("type") == "action":
                    print("action", msg.get("char"), msg.get("slotIndex"), flush=True)
                    assert msg.get("char") == answer[slot], (msg, answer)
                    slots[slot]["filled"] = True
                    await ws.send(json.dumps({
                        "type": "ack", "seq": msg.get("seq"), "stateSeq": msg.get("stateSeq"),
                        "result": "correct", "remainingLives": 3,
                        "questionCompleted": all(s["filled"] for s in slots),
                    }))
                    break

        # 触发 Q1~Q4 的讲解；item 4(Q5) 不触发
        for item in range(4):
            await ws.send(json.dumps({"type": "event", "v": 2, "event": "question_clear", "levelId": 1, "itemIndex": item}))
            await asyncio.sleep(0.5)

        # 讲解事件之后立刻发 level_clear，桥会先处理完 4 个讲解再 next
        await ws.send(json.dumps({"type": "event", "v": 2, "event": "level_clear", "levelId": 1, "nextLevelId": 2}))

        # 等讲解完成：统计 pause/resume；看到 next_level 或 timeout
        end = asyncio.get_event_loop().time() + 150
        while asyncio.get_event_loop().time() < end:
            msg = await recv(ws, 120)
            print("event-loop", msg, flush=True)
            if msg.get("type") == "cmd":
                if msg.get("op") == "pause":
                    pauses += 1
                elif msg.get("op") == "resume":
                    resumes += 1
                elif msg.get("op") == "next_level":
                    next_seen = True
                    break
            if msg.get("type") == "action":
                print("unexpected action", msg, flush=True)

        await ws.send(json.dumps({"type": "event", "v": 2, "event": "level_fail", "levelId": 1}))
        msg = await recv(ws, 60)
        print("after level_fail", msg, flush=True)
        retry_seen = msg.get("type") == "cmd" and msg.get("op") == "retry_level"

    print("pauses", pauses, "resumes", resumes, "next", next_seen, "retry", retry_seen, flush=True)
    if pauses >= 1 and resumes >= 1 and next_seen and retry_seen:
        print("GAME1 SIM FULL OK", flush=True)
    else:
        print("GAME1 SIM PARTIAL", flush=True)


asyncio.run(main())