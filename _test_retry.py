import asyncio
import json

import websockets


async def main():
    async with websockets.connect("ws://127.0.0.1:9001") as ws:
        await ws.send(json.dumps({"type": "event", "v": 2, "event": "level_fail", "levelId": 1}))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            msg = json.loads(raw)
            print(msg, flush=True)
            if msg.get("type") == "cmd" and msg.get("op") == "retry_level":
                print("GAME1 RETRY OK", flush=True)
                return
            if msg.get("type") == "cmd" and msg.get("op") != "pause":
                pass


asyncio.run(main())