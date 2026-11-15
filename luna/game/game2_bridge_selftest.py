"""临时联调：不启动完整 Luna，只验证 GameBridge 的 Game2 分支。

5 个位置各 1 秒窗口，两个用户投正确字，验证：
- 5 字提交
- pause / resume
- level_clear -> next_level
- level_fail -> retry_level
"""

import asyncio
import json
import os

os.environ["GAME_BRIDGE_TTS"] = "0"
os.environ["GAME_BRIDGE_PORT"] = "9012"
os.environ["GAME2_WINDOW"] = "2.0"

import websockets

from luna.game.bridge import GameBridge

ALL_LEVELS = r"C:\Users\97550\Desktop\大鹅抓成语AI\assets\game2\level\all_levels.json"


class DummyHandle:
    def audio_synthesis_handle(self, data):
        print("[tts]", data.get("content", "")[:40])

    def is_playback_busy(self):
        return False


async def recv_json(ws, timeout=12):
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    message = json.loads(raw)
    print("[test] <-", message)
    return message


async def wait_for_action(ws, timeout=12):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        message = await recv_json(ws, timeout)
        if message.get("type") == "action":
            return message
    raise TimeoutError("no action")


async def main():
    with open(ALL_LEVELS, encoding="utf-8") as f:
        all_levels = json.load(f)
    config = all_levels["1"]
    answer = config["idiom"]
    groups = config["options"]

    bridge = GameBridge(DummyHandle())
    bridge.start()
    await asyncio.sleep(0.8)

    async with websockets.connect("ws://127.0.0.1:9012") as ws:
        await ws.send(json.dumps({
            "type": "state", "v": 2, "seq": 1, "levelId": 1, "attempt": 0, "maxAttempts": 5,
            "visibleStrokes": [1, 1, 1, 1, 1], "totalStrokes": [8, 8, 8, 8, 8],
            "candidateGroups": groups,
            "slots": [{"index": i, "filled": False} for i in range(5)],
            "history": [], "phase": "ready",
        }, ensure_ascii=False))

        # 5 个 2 秒窗口，每个窗口两个用户投正确答案
        for slot in range(5):
            await asyncio.sleep(0.5 if slot == 0 else 2.2)
            bridge.on_comment({"username": f"u{slot}a", "content": answer[slot]})
            bridge.on_comment({"username": f"u{slot}b", "content": answer[slot]})
        await asyncio.sleep(2.2)

        action = await wait_for_action(ws, 10)
        assert action["guess"] == answer, (action, answer)
        await ws.send(json.dumps({
            "type": "ack", "seq": action["seq"], "stateSeq": action["stateSeq"],
            "result": "correct", "pct": 100, "attempt": 1, "maxAttempts": 5, "levelCompleted": True,
        }))
        await ws.send(json.dumps({"type": "event", "v": 2, "event": "level_clear", "levelId": 1, "nextLevelId": 2}))
        cmd = await recv_json(ws, 10)
        assert cmd.get("op") == "next_level", cmd

        await ws.send(json.dumps({"type": "event", "v": 2, "event": "level_fail", "levelId": 1}))
        cmd = await recv_json(ws, 10)
        assert cmd.get("op") == "retry_level", cmd

    print("GAME2 BRIDGE OK")
    os._exit(0)


if __name__ == "__main__":
    asyncio.run(main())