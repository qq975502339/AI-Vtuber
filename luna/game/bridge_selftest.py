"""临时联调：不启动完整 Luna，只验证 GameBridge 的 state/action/event/cmd。"""

import asyncio
import json
import os
import tempfile
from pathlib import Path

os.environ["GAME_BRIDGE_TTS"] = "0"
os.environ["GAME_BRIDGE_PORT"] = "9011"

import websockets

from luna.game.bridge import GameBridge


class _NoConfig:
    """GameBridge / HostBrain 只用到 config.get(*keys) 的嵌套取值语义。

    一律返回 None（LLM 主播话术与优先级映射都不生效，联调只走规则版 + 模板 TTS）。
    heart_dir 指到临时目录：HostBrain 启动时会读 heart/*.md 拼 system prompt，
    测试不该依赖真人设，更不该在仓库里留下任何文件。
    """

    heart_dir = str(Path(tempfile.gettempdir()) / "luna_selftest_heart")

    def get(self, *keys):
        if keys == ("llm",):
            return {"host": {"enabled": False, "heart_dir": self.heart_dir}}
        return None


class DummyHandle:
    config = _NoConfig()

    def audio_synthesis_handle(self, data):
        print("[tts]", data.get("content", "")[:40])

    def is_playback_busy(self):
        return False


async def recv_json(ws, timeout=12):
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    message = json.loads(raw)
    print("[test] <-", message)
    return message


async def main():
    bridge = GameBridge(DummyHandle())
    bridge.start()
    await asyncio.sleep(0.8)

    async with websockets.connect("ws://127.0.0.1:9011") as ws:
        answer = "万无一失"
        elements = [
            {"index": 0, "type": "text", "desc": ""},
            {"index": 1, "type": "text", "desc": ""},
            {"index": 2, "type": "image", "desc": ""},
            {"index": 3, "type": "image", "desc": ""},
        ]

        async def send_state(seq, filled):
            await ws.send(json.dumps({
                "type": "state", "v": 2, "seq": seq, "levelId": 1, "itemIndex": 0,
                "questionNo": 1, "remainingLives": 3, "elements": elements,
                "charPool": [{"char": c, "available": True} for c in answer],
                "slots": [{"index": i, "filled": filled[i]} for i in range(4)],
                "phase": "ready",
            }, ensure_ascii=False))

        await send_state(1, [False, False, False, False])
        action = await recv_json(ws)
        assert action["type"] == "action" and action["char"] == "万" and action["slotIndex"] == 0, action
        await ws.send(json.dumps({"type": "ack", "seq": action["seq"], "stateSeq": 1, "result": "correct"}))

        await send_state(2, [True, False, False, False])
        action = await recv_json(ws)
        assert action["char"] == "无" and action["slotIndex"] == 1, action
        await ws.send(json.dumps({"type": "ack", "seq": action["seq"], "stateSeq": 2, "result": "correct"}))

        await send_state(3, [True, True, False, False])
        await asyncio.sleep(0.8)
        bridge.on_comment({"username": "u1", "content": "一"})
        bridge.on_comment({"username": "u2", "content": "一"})
        action = await recv_json(ws, 10)
        assert action["char"] == "一" and action["slotIndex"] == 2, action
        await ws.send(json.dumps({"type": "ack", "seq": action["seq"], "stateSeq": 3, "result": "correct"}))

        await send_state(4, [True, True, True, False])
        await asyncio.sleep(0.8)
        bridge.on_comment({"username": "u1", "content": "失"})
        bridge.on_comment({"username": "u2", "content": "失"})
        action = await recv_json(ws, 10)
        assert action["char"] == "失" and action["slotIndex"] == 3, action
        await ws.send(json.dumps({"type": "ack", "seq": action["seq"], "stateSeq": 4, "result": "correct"}))

        await ws.send(json.dumps({"type": "event", "v": 2, "event": "question_clear", "levelId": 1, "itemIndex": 0}))
        await asyncio.sleep(0.2)

        await ws.send(json.dumps({"type": "event", "v": 2, "event": "level_clear", "levelId": 1, "nextLevelId": 2}))
        cmd = await recv_json(ws, 10)
        assert cmd["type"] == "cmd" and cmd["op"] == "next_level", cmd

        await ws.send(json.dumps({"type": "event", "v": 2, "event": "level_fail", "levelId": 1}))
        cmd = await recv_json(ws, 10)
        assert cmd["type"] == "cmd" and cmd["op"] == "retry_level", cmd

    # 必须 flush：下面 os._exit 直接走人，不刷缓冲区的话这行永远看不到，
    # 「跑通」和「静默退出」就分不出来了
    print("BRIDGE TEST OK", flush=True)
    os._exit(0)


if __name__ == "__main__":
    asyncio.run(main())