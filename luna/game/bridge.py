"""Game1 AI 无人直播：Luna 侧的最小桥（规则版，先不接大模型）。

职责：
- 起一个 WebSocket 服务，让浏览器 Game1 连进来；
- 收 state，按“text 直接放 / image 开观众窗口”决定落子；
- 收 event：Q1~Q4 讲 3+2（释义/出处），Q5 跳过；通关 next，失败 retry；
- 讲之前 cmd:pause，讲完 cmd:resume；
- 主播说话先走 TTS 模板，不接 LLM；
- 弹幕通过 My_handle._interaction_pipeline 转发给 on_comment。

协议对应 Game1AiProtocol v2。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import threading
import time
import traceback
from collections import Counter

from websockets.asyncio.server import serve

from luna.core.message import build as build_message
from luna.game.host_brain import HostBrain
from luna.infra.my_log import logger

DEFAULT_SCRIPTS = r"C:\Users\97550\Desktop\大鹅抓成语AI\assets\game1\levels\game1_scripts.json"
DEFAULT_GAME2_SCRIPTS = r"C:\Users\97550\Desktop\大鹅抓成语AI\assets\game2\level\game2_scripts.json"
SCRIPTS_PATH = os.environ.get("GAME1_SCRIPTS_PATH", DEFAULT_SCRIPTS)
GAME2_SCRIPTS_PATH = os.environ.get("GAME2_SCRIPTS_PATH", DEFAULT_GAME2_SCRIPTS)
PORT = int(os.environ.get("GAME_BRIDGE_PORT", "9001"))
WINDOW_SECONDS = float(os.environ.get("GAME1_WINDOW", "6"))
GAME2_WINDOW_SECONDS = float(os.environ.get("GAME2_WINDOW", "10"))
TTS_ENABLE = os.environ.get("GAME_BRIDGE_TTS", "1") != "0"
TTS_WAIT_LIMIT = float(os.environ.get("GAME_BRIDGE_TTS_WAIT", "120"))


class GameBridge:
    def __init__(self, my_handle):
        self.my_handle = my_handle
        self.host_brain = HostBrain(my_handle)
        self.questions: dict[tuple[int, int], dict] = {}
        self.game2_levels: dict[int, dict] = {}
        self.plans: dict[int, dict] = {}
        self.load_scripts()

        self.thread: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ws = None

        self.active = False
        self.game_kind: str | None = None
        self.action_seq = 0
        self.command_seq = 0
        self.fill_seq = 0
        self.last_state_seq = 0
        self.last_level: int | None = None
        self.fail_streak: dict[int, int] = {}
        self.solo_level: int | None = None
        self.solo_announced = False

        self.window_open = False
        self.window_pool: set[str] = set()
        self.window_votes: dict[str, str] = {}
        self.vote_lock = threading.Lock()

        # 游戏语音优先于普通弹幕，低于聊天
        try:
            priority = self.my_handle.config.get("filter", "priority_mapping")
            if isinstance(priority, dict):
                priority.setdefault("game_host", 25)
        except Exception:
            pass

    # ---------- 数据 ----------
    def load_scripts(self) -> None:
        try:
            with open(SCRIPTS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            for q in data.get("questions", []):
                self.questions[(int(q["levelId"]), int(q["itemIndex"]))] = q
            logger.info(f"[GAME_BRIDGE] 已加载 Game1 题目 {len(self.questions)} 条 | {SCRIPTS_PATH}")
        except Exception:
            logger.error(f"[GAME_BRIDGE] Game1 题目加载失败：{SCRIPTS_PATH}\n{traceback.format_exc()}")

        try:
            with open(GAME2_SCRIPTS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            for level in data.get("levels", []):
                self.game2_levels[int(level["levelId"])] = level
            logger.info(f"[GAME_BRIDGE] 已加载 Game2 关卡 {len(self.game2_levels)} 个 | {GAME2_SCRIPTS_PATH}")
        except Exception:
            logger.error(f"[GAME_BRIDGE] Game2 关卡加载失败：{GAME2_SCRIPTS_PATH}\n{traceback.format_exc()}")

    def _plan(self, level: int) -> dict:
        if level not in self.plans:
            self.plans[level] = {
                "meaning": set(random.sample(range(4), 3)),
                "origin": set(random.sample(range(4), 2)),
            }
        return self.plans[level]

    # ---------- 启动 ----------
    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="game-bridge", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._serve())
        except Exception:
            logger.error(f"[GAME_BRIDGE] 服务启动失败\n{traceback.format_exc()}")

    async def _serve(self) -> None:
        async with serve(self._handler, "127.0.0.1", PORT, max_size=4 * 1024 * 1024):
            logger.info(f"[GAME_BRIDGE] ws://127.0.0.1:{PORT} 监听中（规则版，TTS={TTS_ENABLE}）")
            await asyncio.Future()

    async def _handler(self, websocket) -> None:
        self.ws = websocket
        self.active = True
        self.game_kind = None
        self.last_state_seq = 0
        self.last_level = None
        logger.info("[GAME_BRIDGE] 游戏已连接")
        try:
            async for raw in websocket:
                try:
                    message = json.loads(raw)
                except Exception:
                    continue
                try:
                    await self._on_message(message)
                except websockets.ConnectionClosed:
                    # 页面刷新/关闭导致的正常断开，不要打 ERROR + traceback
                    logger.info("[GAME_BRIDGE] 游戏连接已关闭")
                    break
                except Exception:
                    logger.error(f"[GAME_BRIDGE] 处理消息失败 {raw}\n{traceback.format_exc()}")
        finally:
            self.active = False
            self.ws = None
            logger.info("[GAME_BRIDGE] 游戏已断开")

    # ---------- 消息 ----------
    async def _on_message(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "state":
            if message.get("candidateGroups") is not None and message.get("maxAttempts"):
                await self._on_game2_state(message)
            else:
                await self._on_state(message)
        elif kind == "ack":
            logger.info(
                f"[GAME_BRIDGE] ack seq={message.get('seq')} result={message.get('result')} "
                f"pct={message.get('pct')} reason={message.get('reason')} lives={message.get('remainingLives')}"
            )
            if self.game_kind == "game2":
                await self._on_game2_ack(message)
        elif kind == "event":
            await self._on_event(message)

    async def _on_state(self, state: dict) -> None:
        if self.game_kind != "game1":
            self.game_kind = "game1"
            self.last_state_seq = 0
            self.last_level = None
        level = int(state.get("levelId", 0))
        item = int(state.get("itemIndex", 0))
        seq = state.get("seq")
        if seq is None:
            return
        if level != self.last_level:
            self.last_state_seq = 0
            self.last_level = level
            self.solo_announced = False
            await self._host_say(
                f"第{level}关，开始。",
                {"game": "game1", "level": level, "event": "level_start"},
            )
            if self.solo_level == level:
                await self._host_say(
                    "这一关主播单人挑战，弹幕先休息。",
                    {"game": "game1", "level": level, "event": "solo_start", "solo": True},
                )
                self.solo_announced = True
        if seq <= self.last_state_seq:
            return
        self.last_state_seq = int(seq)

        slots = state.get("slots") or []
        if slots and all(s.get("filled") for s in slots):
            return
        slot = next((i for i, s in enumerate(slots) if not s.get("filled")), None)
        if slot is None:
            return
        question = self.questions.get((level, item))
        if not question:
            logger.warning(f"[GAME_BRIDGE] 缺少题目 {level}-{item}")
            return
        answer = str(question.get("answer", ""))
        if len(answer) < 4:
            logger.warning(f"[GAME_BRIDGE] 题目答案异常 {level}-{item} {answer}")
            return
        char = answer[slot]
        elements = state.get("elements") or []
        element_type = "text"
        if slot < len(elements) and elements[slot].get("type"):
            element_type = elements[slot]["type"]
        else:
            types = question.get("elementTypes") or []
            if slot < len(types):
                element_type = types[slot]

        logger.info(f"[GAME_BRIDGE] state {level}-{item} slot={slot} type={element_type} char={char}")
        if element_type == "image":
            if self.solo_level == level:
                # 单人关：不弹观众窗口，主播直接选
                await self._speak(f"第{slot + 1}个，我选{char}。", wait=True)
                await self._send_action(int(seq), char, slot)
            else:
                await self._guess_image(state, slot, char)
        else:
            await self._speak(f"这个直接给，{char}。", wait=True)
            await self._send_action(int(seq), char, slot)

    async def _guess_image(self, state: dict, slot: int, answer_char: str) -> None:
        pool = {c.get("char") for c in (state.get("charPool") or []) if c.get("available")}
        if answer_char not in pool:
            logger.warning(f"[GAME_BRIDGE] 答案字不可用：{answer_char}")
            return
        with self.vote_lock:
            self.window_open = True
            self.window_pool = pool
            self.window_votes = {}
        await self._speak(f"第{slot + 1}个，看图猜字，{int(WINDOW_SECONDS)}秒。", wait=True)
        await asyncio.sleep(WINDOW_SECONDS)
        with self.vote_lock:
            self.window_open = False
            votes = dict(self.window_votes)
        pick = self._pick(votes, answer_char)
        logger.info(f"[GAME_BRIDGE] 窗口关闭 slot={slot} 票={votes} 选={pick}")
        await self._speak(f"我选{pick}。", wait=True)
        await self._send_action(int(state.get("seq", 0)), pick, slot)

    def _pick(self, votes: dict[str, str], fallback: str) -> str:
        if not votes:
            return fallback
        counts = Counter(votes.values())
        top_char, top_count = counts.most_common(1)[0]
        if top_count >= 2 and top_count / max(1, len(votes)) >= 0.3:
            return top_char
        return fallback

    async def _on_game2_state(self, state: dict) -> None:
        if self.game_kind != "game2":
            self.game_kind = "game2"
            self.last_state_seq = 0
            self.last_level = None
        level = int(state.get("levelId", 0))
        attempt = int(state.get("attempt", 0))
        seq = state.get("seq")
        if seq is None:
            return
        if state.get("phase") == "locked":
            return
        if level != self.last_level:
            self.last_state_seq = 0
            self.last_level = level
            await self._host_say(
                f"第{level}关，开始。",
                {"game": "game2", "level": level, "event": "level_start"},
            )
        if seq <= self.last_state_seq:
            return
        self.last_state_seq = int(seq)

        groups = state.get("candidateGroups") or []
        if len(groups) < 5:
            logger.warning(f"[GAME_BRIDGE] Game2 候选组异常 {level}-{attempt}")
            return
        level_data = self.game2_levels.get(level)
        answer = str(level_data.get("answer", "")) if level_data else ""
        if len(answer) < 5:
            answer = "".join(str(group[0]) for group in groups[:5] if group)

        await self._send_command("pause")
        picks: list[str] = []
        for slot in range(5):
            pool = [str(c) for c in groups[slot] if c]
            if not pool:
                picks.append(answer[slot] if slot < len(answer) else "")
                continue
            host = self._game2_host_guess(level_data, attempt, slot, answer, pool)
            with self.vote_lock:
                self.window_open = True
                self.window_pool = set(pool)
                self.window_votes = {}
            desc = self._game2_stroke_desc(level_data, attempt, slot)
            await self._speak(f"第{slot + 1}个字，{desc}，{int(GAME2_WINDOW_SECONDS)}秒。", wait=True)
            logger.info(f"[GAME_BRIDGE] Game2 窗口开始 slot={slot}")
            await asyncio.sleep(GAME2_WINDOW_SECONDS)
            with self.vote_lock:
                self.window_open = False
                votes = dict(self.window_votes)
            pick = self._pick(votes, host)
            logger.info(f"[GAME_BRIDGE] Game2 窗口 slot={slot} 票={votes} 选={pick}")
            await self._speak(f"我选{pick}。", wait=True)
            await self._send_game2_fill(int(seq), slot, pick)
            await asyncio.sleep(0.2)
            picks.append(pick)

        guess = "".join(picks)
        # 等最后一个 fill 的落格动画完成，再提交；否则游戏侧会看到第 5 格还空着
        await asyncio.sleep(0.8)
        await self._send_command("resume")
        await self._send_game2_action(int(seq), guess)

    def _game2_host_guess(self, level_data: dict | None, attempt: int, slot: int, answer: str, pool: list[str]) -> str:
        host = ""
        if level_data:
            rounds = level_data.get("rounds") or []
            if attempt < len(rounds):
                slots = rounds[attempt].get("slots") or []
                if slot < len(slots):
                    host = str(slots[slot].get("hostGuess") or "")
        if not host and slot < len(answer):
            host = answer[slot]
        if host not in pool:
            if slot < len(answer) and answer[slot] in pool:
                host = answer[slot]
            else:
                host = pool[0]
        return host

    def _game2_stroke_desc(self, level_data: dict | None, attempt: int, slot: int) -> str:
        if level_data:
            rounds = level_data.get("rounds") or []
            if attempt < len(rounds):
                slots = rounds[attempt].get("slots") or []
                if slot < len(slots):
                    desc = slots[slot].get("strokeDesc")
                    if desc:
                        return str(desc)
        return "看这个字"

    async def _on_game2_ack(self, message: dict) -> None:
        result = message.get("result")
        pct = message.get("pct")
        if result == "correct":
            await self._host_say(
                "对了，100%。",
                {"game": "game2", "pct": 100, "event": "correct"},
            )
            return
        if result == "rejected":
            await self._host_say(
                "这轮没提交上。",
                {"game": "game2", "event": "rejected"},
            )
            return
        if pct is None:
            return
        pct = int(pct)
        if pct >= 80:
            await self._host_say(
                f"{pct}%，很接近了。",
                {"game": "game2", "pct": pct, "event": "guess"},
            )
        elif pct >= 50:
            await self._host_say(
                f"{pct}%，对了一半。",
                {"game": "game2", "pct": pct, "event": "guess"},
            )
        else:
            await self._host_say(
                f"{pct}%，还没对上。",
                {"game": "game2", "pct": pct, "event": "guess"},
            )

    async def _on_game2_event(self, event: dict) -> None:
        name = event.get("event")
        if name == "level_clear":
            self.last_state_seq = 0
            await self._send_command("next_level")
            return
        if name == "level_fail":
            self.last_state_seq = 0
            await self._host_say(
                "没过，重来。",
                {"game": "game2", "event": "fail"},
            )
            await asyncio.sleep(0.6)
            await self._send_command("retry_level")
            return
        if name == "all_clear":
            self.last_state_seq = 0
            await self._host_say(
                "全部通关了。",
                {"game": "game2", "event": "all_clear"},
            )

    async def _send_game2_fill(self, state_seq: int, slot: int, char: str) -> None:
        if self.ws is None:
            return
        self.fill_seq += 1
        message = {
            "type": "fill",
            "seq": self.fill_seq,
            "stateSeq": state_seq,
            "slotIndex": slot,
            "char": char,
        }
        await self.ws.send(json.dumps(message, ensure_ascii=False))
        logger.info(f"[GAME_BRIDGE] Game2 fill -> {char}@{slot} state={state_seq}")

    async def _send_game2_action(self, state_seq: int, guess: str) -> None:
        if self.ws is None:
            return
        self.action_seq += 1
        message = {
            "type": "action",
            "seq": self.action_seq,
            "stateSeq": state_seq,
            "guess": guess,
            "submit": True,
        }
        await self.ws.send(json.dumps(message, ensure_ascii=False))
        logger.info(f"[GAME_BRIDGE] Game2 action -> {guess} state={state_seq}")

    async def _on_event(self, event: dict) -> None:
        if self.game_kind == "game2":
            await self._on_game2_event(event)
            return
        name = event.get("event")
        level = int(event.get("levelId", 0))
        item = event.get("itemIndex")
        if name == "question_clear":
            if item is not None and int(item) < 4:
                await self._talk_question(level, int(item))
            return
        if name == "level_clear":
            self.last_state_seq = 0
            self.fail_streak[level] = 0
            if self.solo_level == level:
                self.solo_level = None
                self.solo_announced = False
            await self._host_say(
                f"第{level}关过了，下一关。",
                {"game": "game1", "level": level, "event": "level_clear"},
            )
            await asyncio.sleep(1.0)
            await self._send_command("next_level")
            return
        if name == "level_fail":
            self.last_state_seq = 0
            self.fail_streak[level] = self.fail_streak.get(level, 0) + 1
            if self.fail_streak[level] >= 2:
                # 同一关连败 2 次：不再重玩，进下一关并改成主播单人
                self.fail_streak[level] = 0
                self.solo_level = level + 1
                self.solo_announced = False
                await self._host_say(
                    "这关连败两次，下一关主播单人。",
                    {"game": "game1", "level": level, "event": "solo_next"},
                )
                await asyncio.sleep(0.5)
                await self._send_command("next_level")
                return
            await self._host_say(
                "这关没过，重来。",
                {"game": "game1", "level": level, "event": "fail"},
            )
            await asyncio.sleep(1.2)
            await self._send_command("retry_level")
            return
        if name == "all_clear":
            self.last_state_seq = 0
            self.fail_streak.clear()
            self.solo_level = None
            self.solo_announced = False
            await self._host_say(
                "全部通关了。",
                {"game": "game1", "event": "all_clear"},
            )

    async def _talk_question(self, level: int, item: int) -> None:
        question = self.questions.get((level, item))
        if not question:
            return
        plan = self._plan(level)
        texts = []
        if item in plan["meaning"] and question.get("meaning"):
            texts.append(question["meaning"])
        if item in plan["origin"] and question.get("origin"):
            texts.append(question["origin"])
        if not texts:
            await self._speak("好，下一张。")
            await asyncio.sleep(0.5)
            return
        await self._send_command("pause")
        await self._speak(f"第{item + 1}题过了。")
        for text in texts:
            await self._speak(text)
        await self._wait_playback()
        await self._send_command("resume")

    # ---------- 发送 ----------
    async def _send_action(self, state_seq: int, char: str, slot: int) -> None:
        if self.ws is None:
            return
        self.action_seq += 1
        message = {
            "type": "action",
            "seq": self.action_seq,
            "stateSeq": state_seq,
            "char": char,
            "slotIndex": slot,
        }
        await self.ws.send(json.dumps(message, ensure_ascii=False))
        logger.info(f"[GAME_BRIDGE] action -> {char}@{slot} state={state_seq}")

    async def _send_command(self, op: str) -> None:
        if self.ws is None:
            return
        self.command_seq += 1
        message = {"type": "cmd", "seq": self.command_seq, "op": op}
        await self.ws.send(json.dumps(message, ensure_ascii=False))
        logger.info(f"[GAME_BRIDGE] cmd -> {op}")

    # ---------- 语音 ----------
    async def _host_say(self, fallback: str, context: dict | None = None, wait: bool = False) -> None:
        """主播话术位：LLM 成功就替换，失败/超时回退模板。"""
        text = fallback
        if self.host_brain is not None and self.host_brain.enabled:
            try:
                generated = await asyncio.wait_for(
                    self.host_brain.say(context or {}),
                    timeout=2.8,
                )
                if generated:
                    text = generated
            except Exception:
                text = fallback
        await self._speak(text, wait=wait)

    async def _speak(self, text: str, wait: bool = False) -> None:
        text = (text or "").strip()
        if not text:
            return
        logger.info(f"[GAME_BRIDGE] 主播：{text}")
        if not TTS_ENABLE:
            return
        try:
            config = getattr(self.my_handle, "config", None)
            if config is None:
                return
            data = build_message(
                config,
                "game_host",
                "林小满",
                text,
                tts_type=config.get("audio_synthesis_type"),
            )
            await asyncio.to_thread(self.my_handle.audio_synthesis_handle, data)
        except Exception:
            logger.error(f"[GAME_BRIDGE] TTS 失败\n{traceback.format_exc()}")
            return
        if wait:
            await self._wait_playback()

    async def _wait_playback(self) -> None:
        if not TTS_ENABLE:
            return
        deadline = time.time() + TTS_WAIT_LIMIT
        while time.time() < deadline:
            try:
                if not self.my_handle.is_playback_busy():
                    break
            except Exception:
                break
            await asyncio.sleep(0.25)
        await asyncio.sleep(0.4)

    # ---------- 弹幕 ----------
    def on_comment(self, data: dict) -> bool:
        """由 My_handle 的弹幕流水线调用；返回 True 表示游戏模式接管这条弹幕。"""
        if not self.active:
            return False
        content = str(data.get("content", ""))
        chars = re.findall(r"[\u4e00-\u9fff]", content)
        user = str(data.get("username") or "unknown")
        with self.vote_lock:
            if not self.window_open:
                return True
            char = next((c for c in chars if c in self.window_pool), None)
            if char:
                self.window_votes.setdefault(user, char)
                logger.info(f"[GAME_BRIDGE] Game2 票+ {user}:{char}")
            else:
                logger.info(f"[GAME_BRIDGE] Game2 投票忽略 {user}:{content}")
        return True