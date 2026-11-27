"""Game1 / Game2 AI 无人直播：Luna 侧游戏桥。

职责：
- 起一个 WebSocket 服务，让浏览器 Game1 / Game2 连进来；
- 收 state，按“text 直接放 / image 开观众窗口”决定落子；
- 收 event：过题自然过渡，通关 next，失败 retry；
- 弹幕通过 My_handle._interaction_pipeline 转发给 on_comment。

协议对应 Game1AiProtocol v2 / Game2AiProtocol。
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

import websockets
from websockets.asyncio.server import serve

from luna.core.message import build as build_message
from luna.game.host_brain import HostBrain
from luna.infra.my_log import logger

DEFAULT_SCRIPTS = r"C:\Users\97550\Desktop\大鹅抓成语AI\assets\game1\levels\game1_scripts.json"
DEFAULT_GAME2_SCRIPTS = r"C:\Users\97550\Desktop\大鹅抓成语AI\assets\game2\level\game2_scripts.json"
SCRIPTS_PATH = os.environ.get("GAME1_SCRIPTS_PATH", DEFAULT_SCRIPTS)
GAME2_SCRIPTS_PATH = os.environ.get("GAME2_SCRIPTS_PATH", DEFAULT_GAME2_SCRIPTS)
PORT = int(os.environ.get("GAME_BRIDGE_PORT", "9001"))
WINDOW_SECONDS = float(os.environ.get("GAME1_WINDOW", "8"))
GAME2_WINDOW_SECONDS = float(os.environ.get("GAME2_WINDOW", "10"))
TTS_ENABLE = os.environ.get("GAME_BRIDGE_TTS", "1") != "0"
TTS_WAIT_LIMIT = float(os.environ.get("GAME_BRIDGE_TTS_WAIT", "120"))

_VISUAL_LEAK_KEYWORDS = (
    "谐音", "得出", "对应", "会意", "答案", "锁定", "拿下", "拼起来", "串起来",
    "打在公屏", "宝子", "家人们", "热热身", "呼之欲出", "蹦到嘴边", "读音",
)
_VISUAL_LEAD_RE = re.compile(
    r"^(?:咱们先看|先看|再看|接着往右看|再往右看|最后看|接着看|来)?\s*"
    r"(?:最左边|最右边)?(?:第[一二三四1234]个|最后一个|最后)?(?:元素|图|字)?[，,：:！!？?、\s]*"
)

_BRACKET_EMOJI_RE = re.compile(r"\[[^\[\]]{1,10}\]")
_UNICODE_EMOJI_RE = re.compile(
    r"[\U00010000-\U0010FFFF\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF\uFE00-\uFE0F\u200D\u20E3\u00A9\u00AE\u2122]+"
)
_NEGATED_CHAR_RE = re.compile(r"(?:绝对不是|肯定不是|一定不是|不是|别选|不要|别填|排除|非)\s*[\u4e00-\u9fff]")
_INTENT_CHAR_RE = re.compile(r"(?:选|是|填|投|猜|打|扣)\s*([\u4e00-\u9fff])")
_CONTACT_SPAM_RE = re.compile(
    r"(?:https?://|www\.)\S+|(?:加[微vV信]|vx|wechat|扣扣|qq)\s*[:：]?\s*[a-zA-Z0-9_-]{4,}",
    re.IGNORECASE,
)
_USER_SPECIAL_CHARS = "！!@#￥$%^&*_-+/——=()（）【】}|{:;<>~`\\[]\"'“”‘’"

# 违禁词自动机缓存：check_sensitive_words2 每次调用都会重读 1.3MB 词库并重建
# Aho-Corasick（实测 75ms/次），游戏模式每条弹幕都过一遍会明显拖慢投票，
# 所以这里按「路径 + 修改时间」缓存一份，词库改了才重建。
_BADWORD_CACHE: dict = {"path": None, "mtime": 0.0, "automaton": None}


def _badword_automaton(path: str):
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if (_BADWORD_CACHE["path"] == path
            and abs(_BADWORD_CACHE["mtime"] - mtime) < 1e-6
            and _BADWORD_CACHE["automaton"] is not None):
        return _BADWORD_CACHE["automaton"]
    try:
        import ahocorasick

        with open(path, encoding="utf-8") as f:
            words = [w.strip() for w in f if w.strip()]
        automaton = ahocorasick.Automaton()
        for word in words:
            automaton.add_word(word, word)
        automaton.make_automaton()
    except Exception:
        logger.warning(f"[GAME_BRIDGE] 违禁词库加载失败：{path}\n{traceback.format_exc()}")
        return None
    _BADWORD_CACHE.update(path=path, mtime=mtime, automaton=automaton)
    return automaton


class GameBridge:
    def __init__(self, my_handle):
        self.my_handle = my_handle
        self.host_brain = HostBrain(my_handle)
        self.questions: dict[tuple[int, int], dict] = {}
        self.game2_levels: dict[int, dict] = {}
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
        self.last_guess = ""
        self.last_votes: dict[str, str] = {}
        self.recent_comments: list[str] = []
        self.last_host_lines: list[str] = []
        self.last_template = ""
        self.last_template_key = ""
        self.last_template_by_key: dict[str, str] = {}
        self.filler_last = 0.0
        self.filler_last_say = ""
        self.idle_task: asyncio.Task | None = None
        self.turn_task: asyncio.Task | None = None
        self.turn_kind: str = ""
        self.game1_last_sent: tuple[int, int, int, str] | None = None
        self.game1_last_pick_voters: list[str] = []
        self.game1_wrong_chars: dict[tuple[int, int, int], set[str]] = {}
        self.game1_text_silent_streak: int = 0
        self.comment_seq: int = 0
        self.mid_banter_last: float = 0.0

        self.window_open = False
        self.window_deadline = 0.0
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

    def _cancel_turn_task(self) -> None:
        if self.turn_task is not None and not self.turn_task.done():
            self.turn_task.cancel()
        self.turn_task = None
        self.turn_kind = ""
        with self.vote_lock:
            self.window_open = False
            self.window_deadline = 0.0

    def _schedule_turn(self, kind: str, coro) -> None:
        self._cancel_turn_task()
        self.turn_kind = kind

        async def _runner():
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except websockets.ConnectionClosed:
                logger.info("[GAME_BRIDGE] 游戏连接已关闭")
            except Exception:
                logger.error(f"[GAME_BRIDGE] 回合任务执行失败 ({kind})\n{traceback.format_exc()}")
            finally:
                current = asyncio.current_task()
                if self.turn_task is current:
                    self.turn_task = None
                    self.turn_kind = ""

        self.turn_task = asyncio.create_task(_runner())

    async def _handler(self, websocket) -> None:
        # 新连接接管前，先取消可能残留的旧连接回合任务与闲时任务
        self._cancel_turn_task()
        if self.idle_task is not None:
            self.idle_task.cancel()
            self.idle_task = None

        self.ws = websocket
        self.active = True
        self.game_kind = None
        self.last_state_seq = 0
        self.last_level = None
        self.game1_last_sent = None
        self.game1_last_pick_voters.clear()
        self.game1_wrong_chars.clear()
        self.game1_text_silent_streak = 0
        if hasattr(self.host_brain, "reset_session_state"):
            self.host_brain.reset_session_state()
        with self.vote_lock:
            self.recent_comments.clear()
        self.last_host_lines.clear()
        self.idle_task = asyncio.create_task(self._idle_loop())
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
        except websockets.ConnectionClosed:
            logger.info("[GAME_BRIDGE] 游戏连接已关闭")
        finally:
            # 仅当当前活跃连接仍是本 websocket 时才清理，避免旧连接退出把新连接清掉
            if self.ws is websocket:
                self.active = False
                self.ws = None
                self._cancel_turn_task()
                if self.idle_task is not None:
                    self.idle_task.cancel()
                    self.idle_task = None
            logger.info("[GAME_BRIDGE] 游戏已断开")

    # ---------- 消息 ----------
    def _is_stale_state(self, state: dict, is_game2: bool) -> bool:
        seq = state.get("seq")
        if seq is None:
            return True
        if is_game2 and state.get("phase") == "locked":
            return True
        expected_kind = "game2" if is_game2 else "game1"
        level = int(state.get("levelId", 0))
        if self.game_kind == expected_kind and level == self.last_level and int(seq) <= self.last_state_seq:
            return True
        return False

    def _dispatch_state(self, message: dict) -> None:
        is_game2 = message.get("candidateGroups") is not None and bool(message.get("maxAttempts"))
        if self._is_stale_state(message, is_game2):
            return
        if is_game2:
            self._schedule_turn("state", self._on_game2_state(message))
        else:
            self._schedule_turn("state", self._on_state(message))

    async def _on_message(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "state":
            self._dispatch_state(message)
        elif kind == "ack":
            logger.info(
                f"[GAME_BRIDGE] ack seq={message.get('seq')} result={message.get('result')} "
                f"pct={message.get('pct')} reason={message.get('reason')} lives={message.get('remainingLives')}"
            )
            if self.game_kind == "game2":
                asyncio.create_task(self._on_game2_ack(message))
            elif self.game_kind == "game1" and message.get("result") == "wrong":
                asyncio.create_task(self._on_game1_wrong_ack(message))
        elif kind == "event":
            name = message.get("event")
            if name in ("level_clear", "level_fail", "all_clear"):
                self._schedule_turn("level_end", self._on_event(message))
            elif name == "question_clear":
                asyncio.create_task(self._on_event(message))
            else:
                self._schedule_turn("event", self._on_event(message))

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
            self.game1_wrong_chars.clear()
            self.game1_text_silent_streak = 0
            with self.vote_lock:
                self.recent_comments.clear()
            await self._host_say(
                self._choose([
                    f"行，第{level}关来了，看看这波给啥图。",
                    f"走着第{level}关，弹幕先别走神。",
                    f"开新局第{level}关，希望别上来就整抽象画。",
                ], key="g1start"),
                {"game": "game1", "level": level, "event": "level_start"},
            )
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
            self.game1_text_silent_streak = 0
            await self._guess_image(state, slot, char, question)
        else:
            # 打破每格明字都停下来喊一句的报站节拍：大多数明字静默快填，偶尔顺嘴带半句
            should_speak = (self.game1_text_silent_streak >= 2 and random.random() < 0.45) or (
                slot == 0 and self.game1_text_silent_streak == 0 and random.random() < 0.25
            )
            if should_speak:
                self.game1_text_silent_streak = 0
                text_line = self._choose_fmt([
                    "这字白给，先点个{char}。",
                    "明摆着个{char}，顺手落了。",
                    "{char}直接填上。",
                ], key="g1text", char=char)
                await self._host_say(
                    text_line,
                    {"game": "game1", "level": level, "event": "text", "char": char, "required": [char]},
                    wait=False,
                )
                await asyncio.sleep(0.3)
            else:
                self.game1_text_silent_streak += 1
                await asyncio.sleep(0.18)
            self.game1_last_sent = (level, item, slot, char)
            self.game1_last_pick_voters = []
            await self._send_action(int(seq), char, slot)

    @staticmethod
    def _extract_visual_hint(question: dict | None, slot: int, answer_char: str) -> str:
        """从 Game1 elementDescs 安全提取纯画面描述，严格剔除答案字与谐音推导。"""
        if not question:
            return ""
        descs = question.get("elementDescs") or []
        if slot >= len(descs) or not descs[slot]:
            return ""
        raw = str(descs[slot])
        for seg in re.split(r"[。！？!?~\n]", raw):
            seg = _VISUAL_LEAD_RE.sub("", seg.strip()).strip()
            if not seg or len(seg) < 4:
                continue
            if answer_char and answer_char in seg:
                continue
            if any(kw in seg for kw in _VISUAL_LEAK_KEYWORDS):
                continue
            # 去掉引号里可能残留的单字提示
            if re.search(r"[“\"「][\u4e00-\u9fff][”\"」]", seg):
                continue
            return seg[:32].rstrip("，,、 ")
        return ""

    @staticmethod
    def _build_game1_progress(state: dict, answer: str) -> tuple[str, int]:
        slots = state.get("slots") or []
        chars = []
        filled = 0
        for i in range(4):
            s = slots[i] if i < len(slots) else {}
            if s.get("filled"):
                filled += 1
                ch = str(s.get("char") or (answer[i] if i < len(answer) else ""))
                chars.append(ch or "_")
            else:
                chars.append("_")
        return "".join(chars), filled

    @staticmethod
    def _summarize_votes(votes: dict[str, str]) -> tuple[str, str]:
        if not votes:
            return "none", "冷场，目前没人投票"
        counts = Counter(votes.values()).most_common(3)
        total = len(votes)
        top_char, top_n = counts[0]
        if len(counts) == 1:
            if total >= 2:
                return "unanimous", f"满屏一边倒，{total}个人都在刷「{top_char}」"
            return "single", f"只有1个人刷了「{top_char}」"
        if top_n / total >= 0.7:
            return "unanimous", f"大部队刷「{top_char}」（{top_n}/{total}票）"
        split_desc = " vs ".join(f"「{c}」{n}票" for c, n in counts[:2])
        return "split", f"公屏吵成两派：{split_desc}"

    async def _wait_window_with_banter(self, duration: float, base_ctx: dict) -> None:
        """等待投票窗口结束；中段若飘来新弹幕/投票且播放空闲，顺嘴插一句临场互动。"""
        if duration <= 3.0:
            await asyncio.sleep(duration)
            return
        start_comment_seq = self.comment_seq
        deadline = time.time() + duration
        first_half = min(duration * 0.48, max(2.8, duration - 3.6))
        await asyncio.sleep(first_half)
        remaining = deadline - time.time()
        if remaining >= 3.0 and (time.time() - self.mid_banter_last) >= 10.0:
            with self.vote_lock:
                votes_now = dict(self.window_votes)
                recent_now = list(self.recent_comments[-4:])
            has_activity = (self.comment_seq > start_comment_seq) or bool(votes_now)
            busy = False
            try:
                busy = bool(self.my_handle.is_playback_busy())
            except Exception:
                busy = False
            if not busy and (has_activity or random.random() < 0.25):
                self.mid_banter_last = time.time()
                vote_state, vote_summary = self._summarize_votes(votes_now)
                if has_activity:
                    if vote_state == "split":
                        fb = self._choose(
                            ["诶公屏怎么分两派了，我听谁的呀。", "别吵别吵，到底哪个靠谱点。"],
                            key="mid_split",
                        )
                    elif votes_now:
                        fb = self._choose(
                            ["看见你们刷字了啊，再瞅两秒看看大部队。", "诶已经有人给答案了，其他人觉得对吗？"],
                            key="mid_vote",
                        )
                    else:
                        fb = self._choose(
                            ["看见弹幕了，你们倒是顺手猜个字呀。", "哈哈哈别光聊天，帮我看看这格。"],
                            key="mid_chat",
                        )
                else:
                    fb = self._choose(
                        ["公屏怎么突然安静了，都在思考人生呢？", "行吧没人说话，我再盯着这图琢磨两秒。"],
                        key="mid_cold",
                    )
                mid_ctx = {
                    **base_ctx,
                    "event": "mid_banter",
                    "votes": votes_now,
                    "vote_state": vote_state,
                    "vote_summary": vote_summary,
                    "last": recent_now,
                }
                mid_ctx.pop("required", None)
                await self._host_say(fb, mid_ctx, wait=False)
        left = deadline - time.time()
        if left > 0:
            await asyncio.sleep(left)

    async def _on_game1_wrong_ack(self, message: dict) -> None:
        last = self.game1_last_sent
        if last is not None:
            level, item, slot, char = last
            self.game1_wrong_chars.setdefault((level, item, slot), set()).add(char)
            logger.info(f"[GAME_BRIDGE] Game1 记录已证伪错字 {level}-{item}@{slot}: {char}")
        if hasattr(self.host_brain, "record_outcome"):
            self.host_brain.record_outcome("wrong", blame_users=list(self.game1_last_pick_voters))
        lives = message.get("remainingLives")
        if lives is not None and int(lives) <= 0:
            return
        line = self._choose(
            ["哎呀翻车了，谁带偏的我站出来。", "不对不对，啪一下扣颗心，血亏。", "我就知道不能盲信公屏，又被演了。"],
            key="g1wrong",
        )
        await self._host_say(
            line,
            {"game": "game1", "level": self.last_level, "event": "wrong", "lives": lives},
            wait=False,
        )

    async def _guess_image(
        self,
        state: dict,
        slot: int,
        answer_char: str,
        question: dict | None = None,
    ) -> None:
        level = int(state.get("levelId", 0))
        item = int(state.get("itemIndex", 0))
        if question is None:
            question = self.questions.get((level, item))
        answer = str(question.get("answer", "")) if question else ""
        raw_pool = {c.get("char") for c in (state.get("charPool") or []) if c.get("available")}
        if answer_char not in raw_pool:
            logger.warning(f"[GAME_BRIDGE] 答案字不可用：{answer_char}")
            return
        # 排除本格已证伪的错字（永远保留正确答案字），防黑粉连刷同一错字团灭
        wrong_set = self.game1_wrong_chars.get((level, item, slot), set())
        pool = {c for c in raw_pool if c not in wrong_set or c == answer_char}

        visual_hint = self._extract_visual_hint(question, slot, answer_char)
        progress, filled_count = self._build_game1_progress(state, answer)
        lives = state.get("remainingLives")
        with self.vote_lock:
            cold_room = len(self.recent_comments) == 0
            self.window_open = True
            self.window_deadline = time.time() + WINDOW_SECONDS
            self.window_pool = pool
            self.window_votes = {}
        ask_ctx = {
            "game": "game1",
            "level": level,
            "event": "ask",
            "slot": slot + 1,
            "seconds": int(WINDOW_SECONDS),
            "progress": progress,
            "visual_hint": visual_hint,
            "is_obvious": filled_count >= 2,
            "cold_room": cold_room,
            "lives": lives,
        }
        try:
            if filled_count >= 2:
                ask = self._choose([
                    f"都拼出{progress.replace('_', '什么')}了，这不送分题吗快刷。",
                    "前面都明牌了，后面还能是啥呀，公屏打字。",
                    "这题白给了吧，手快的赶紧打出来。",
                ], key="g1ask_obvious")
            elif visual_hint:
                ask = self._choose([
                    f"诶这图画了个{visual_hint[:14]}，你们猜是哪个字？",
                    "我咋越看越抽象呢，弹幕有看懂这图的没。",
                    "来吧兄弟们，这图我先交给公屏了。",
                ], key="g1ask_visual")
            else:
                ask = self._choose([
                    "来来来瞅一眼这图，公屏给点提示。",
                    "这格我拿不准，弹幕先打在公屏上。",
                    "诶有点眼熟但卡嘴边了，帮我想一个。",
                ], key="g1ask")
            await self._host_say(ask, ask_ctx, wait=True)
            with self.vote_lock:
                self.window_deadline = time.time() + WINDOW_SECONDS
            await self._wait_window_with_banter(WINDOW_SECONDS, ask_ctx)
        finally:
            with self.vote_lock:
                self.window_open = False
                self.window_deadline = 0.0
                votes = dict(self.window_votes)

        pick = self._pick(votes, answer_char)
        logger.info(f"[GAME_BRIDGE] 窗口关闭 slot={slot} 票={votes} 选={pick}")
        pick_voters = [u for u, c in votes.items() if c == pick]
        vote_state, vote_summary = self._summarize_votes(votes)
        voter = random.choice(pick_voters) if pick_voters else ""
        if voter:
            line = self._choose_fmt([
                "{voter}都刷{pick}了，行，信你们一回。",
                "满屏都在刷{pick}，那肯定冲{pick}啊。",
                "行，听{voter}的，就点{pick}。",
            ], key="g1pick", voter=voter, pick=pick)
        else:
            line = self._choose_fmt([
                "没人刷那我自己蒙个{pick}啊，翻车别笑我。",
                "看着就像{pick}，我先冲了。",
            ], key="g1pick_self", pick=pick)
        await self._host_say(
            line,
            {
                "game": "game1",
                "level": level,
                "event": "pick",
                "pick": pick,
                "voter": voter,
                "votes": votes,
                "vote_state": vote_state,
                "vote_summary": vote_summary,
                "progress": progress,
                "required": [pick],
            },
            wait=True,
        )
        self.game1_last_sent = (level, item, slot, pick)
        self.game1_last_pick_voters = pick_voters
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
            with self.vote_lock:
                self.recent_comments.clear()
            await self._host_say(
                self._choose([
                    f"第{level}关来了，五个字，弹幕帮我盯第一笔。",
                    f"走着第{level}关，看笔顺猜字，兄弟们跟上。",
                ], key="g2start"),
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

        # 按整关候选字多重集频次扣减已选字，防止跨组重复字导致后续格子 char_unavailable
        remaining_counts = Counter(str(c) for group in groups[:5] for c in (group or []) if c)
        all_votes: dict[str, str] = {}
        prev_pct = getattr(self.host_brain, "last_g2_pct", None)
        prev_guess = getattr(self.host_brain, "last_g2_guess", "")

        await self._send_command("pause")
        picks: list[str] = []
        try:
            for slot in range(5):
                raw_group = [str(c) for c in (groups[slot] or []) if c]
                pool = [c for c in raw_group if remaining_counts[c] > 0]
                if not pool:
                    pool = [c for c, cnt in remaining_counts.items() if cnt > 0] or raw_group
                if not pool:
                    picks.append(answer[slot] if slot < len(answer) else "")
                    continue
                host = self._game2_host_guess(level_data, attempt, slot, answer, pool)
                progress = "".join(picks) + "_" * (5 - len(picks))
                with self.vote_lock:
                    self.window_open = True
                    self.window_deadline = time.time() + GAME2_WINDOW_SECONDS
                    self.window_pool = set(pool)
                    self.window_votes = {}
                desc = self._game2_stroke_desc(level_data, attempt, slot)
                ask_ctx = {
                    "game": "game2",
                    "level": level,
                    "attempt": attempt,
                    "event": "ask",
                    "slot": slot + 1,
                    "desc": desc,
                    "visual_hint": desc,
                    "progress": progress,
                    "prev_pct": prev_pct,
                    "prev_guess": prev_guess,
                    "seconds": int(GAME2_WINDOW_SECONDS),
                }
                try:
                    if slot == 0:
                        ask = self._choose_fmt([
                            "先盯起笔，{desc}，你们觉得是啥？",
                            "来来来瞅第一格，{desc}，有想法直接打。",
                        ], key="g2ask0", desc=desc)
                    elif slot == 4:
                        ask = self._choose_fmt([
                            "最后一个字了，{desc}，稳住别翻车。",
                            "压轴这格，{desc}，公屏给个准信。",
                        ], key="g2ask4", desc=desc)
                    else:
                        ask = self._choose_fmt([
                            "接着下一个，{desc}，挺明显的吧？",
                            "看这格笔顺，{desc}，弹幕帮我瞄一眼。",
                            "顺手看下面这字，{desc}，直接刷。",
                        ], key="g2ask", desc=desc)
                    await self._host_say(ask, ask_ctx, wait=True)
                    logger.info(f"[GAME_BRIDGE] Game2 窗口开始 slot={slot}")
                    with self.vote_lock:
                        self.window_deadline = time.time() + GAME2_WINDOW_SECONDS
                    await self._wait_window_with_banter(GAME2_WINDOW_SECONDS, ask_ctx)
                finally:
                    with self.vote_lock:
                        self.window_open = False
                        self.window_deadline = 0.0
                        votes = dict(self.window_votes)
                pick = self._pick(votes, host)
                if remaining_counts[pick] > 0:
                    remaining_counts[pick] -= 1
                all_votes.update(votes)
                logger.info(f"[GAME_BRIDGE] Game2 窗口 slot={slot} 票={votes} 选={pick}")
                pick_voters = [u for u, c in votes.items() if c == pick]
                vote_state, vote_summary = self._summarize_votes(votes)
                voter = random.choice(pick_voters) if pick_voters else ""

                # 打破 5 格必唱票 10 句话的机械节拍：中间格无人投票时，偶尔静默顺手落字
                skip_solo_pick = (not voter) and (slot in (1, 2, 3)) and (random.random() < 0.45)
                if not skip_solo_pick:
                    if voter:
                        line = self._choose_fmt([
                            "{voter}都刷{pick}了，行，就它。",
                            "大伙刷{pick}，直接落上。",
                            "听{voter}的，点{pick}。",
                        ], key="g2pick", voter=voter, pick=pick)
                    else:
                        line = self._choose_fmt([
                            "没人刷我先蒙个{pick}。",
                            "看着像{pick}，放上去试试。",
                        ], key="g2pick_self", pick=pick)
                    await self._host_say(
                        line,
                        {
                            "game": "game2",
                            "level": level,
                            "attempt": attempt,
                            "event": "pick",
                            "pick": pick,
                            "voter": voter,
                            "votes": votes,
                            "vote_state": vote_state,
                            "vote_summary": vote_summary,
                            "progress": progress,
                            "required": [pick],
                        },
                        wait=True,
                    )
                await self._send_game2_fill(int(seq), slot, pick)
                await asyncio.sleep(0.2)
                picks.append(pick)

            guess = "".join(picks)
            self.last_guess = guess
            self.last_votes = all_votes
            # 等最后一个 fill 的落格动画完成，再提交；否则游戏侧会看到第 5 格还空着
            await asyncio.sleep(0.8)
            await self._send_command("resume")
            await self._send_game2_action(int(seq), guess)
        except asyncio.CancelledError:
            with self.vote_lock:
                self.window_open = False
                self.window_deadline = 0.0
            raise

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
        prev_pct = getattr(self.host_brain, "last_g2_pct", None)
        prev_guess = getattr(self.host_brain, "last_g2_guess", "")
        if result == "correct":
            if hasattr(self.host_brain, "record_outcome"):
                self.host_brain.record_outcome("correct", pct=100, guess=self.last_guess)
            await self._host_say(
                self._choose(["满格！漂亮，弹幕这波配合绝了。", "百分百直接拿下，走下一关！"], key="g2ok"),
                {"game": "game2", "pct": 100, "prev_pct": prev_pct, "prev_guess": prev_guess,
                 "event": "correct", "guess": self.last_guess, "votes": self.last_votes},
            )
            return
        if result == "rejected":
            await self._host_say(
                "哎呀这轮没提交上，我的锅。",
                {"game": "game2", "event": "rejected", "guess": self.last_guess},
            )
            return
        if pct is None:
            return
        pct = int(pct)
        if hasattr(self.host_brain, "record_outcome"):
            self.host_brain.record_outcome("guess", pct=pct, guess=self.last_guess)
        ctx = {
            "game": "game2",
            "pct": pct,
            "prev_pct": prev_pct,
            "prev_guess": prev_guess,
            "event": "guess",
            "guess": self.last_guess,
            "votes": self.last_votes,
        }
        if pct >= 80:
            await self._host_say(f"{pct}%！就差临门一脚了，肯定只错了一个字。", ctx)
        elif prev_pct is not None and pct > prev_pct:
            await self._host_say(f"诶涨到{pct}%了，方向对了继续冲！", ctx)
        elif prev_pct is not None and pct < prev_pct:
            await self._host_say(f"啊这掉到{pct}%了，跑偏了咱换个思路。", ctx)
        elif pct >= 50:
            await self._host_say(f"{pct}%，对了一大半，思路没错。", ctx)
        else:
            await self._host_say(f"{pct}%，有点偏啊，当探路了。", ctx)

    async def _on_game2_event(self, event: dict) -> None:
        name = event.get("event")
        if name == "level_clear":
            self.last_state_seq = 0
            if hasattr(self.host_brain, "record_outcome"):
                self.host_brain.record_outcome("level_clear")
            await self._send_command("next_level")
            return
        if name == "level_fail":
            self.last_state_seq = 0
            if hasattr(self.host_brain, "record_outcome"):
                self.host_brain.record_outcome("fail")
            await self._host_say(
                "哎呀没顶住，重开一把找回场子。",
                {"game": "game2", "event": "fail"},
            )
            await asyncio.sleep(0.6)
            await self._send_command("retry_level")
            return
        if name == "all_clear":
            self.last_state_seq = 0
            await self._host_say(
                "全通了！今天大伙太给力了，收工！",
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
            if hasattr(self.host_brain, "record_outcome"):
                self.host_brain.record_outcome("question_clear")
            if item is not None and int(item) < 4:
                await self._host_say(
                    self._choose(["稳，这张拿下，看下一张。", "过得挺丝滑啊，接着来。"], key="g1qshort"),
                    {"game": "game1", "level": level, "event": "question", "item": int(item) + 1},
                    wait=False,
                )
            return
        if name == "level_clear":
            self.last_state_seq = 0
            if hasattr(self.host_brain, "record_outcome"):
                self.host_brain.record_outcome("level_clear")
            await self._host_say(
                self._choose(["漂亮，整关拿下，走下一关！", "稳稳过关，公屏功劳占一半，下一关走起。"], key="g1clear"),
                {"game": "game1", "level": level, "event": "level_clear"},
            )
            await asyncio.sleep(0.8)
            await self._send_command("next_level")
            return
        if name == "level_fail":
            self.last_state_seq = 0
            self.game1_wrong_chars.clear()
            if hasattr(self.host_brain, "record_outcome"):
                self.host_brain.record_outcome("fail")
            await self._host_say(
                "哎呀血条清空了，这把我背一半锅，重开干回来。",
                {"game": "game1", "level": level, "event": "fail"},
            )
            await asyncio.sleep(1.0)
            await self._send_command("retry_level")
            return
        if name == "all_clear":
            self.last_state_seq = 0
            await self._host_say(
                "全通啦，今天太丝滑了，收工！",
                {"game": "game1", "event": "all_clear"},
            )

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
    def _resolve_tts_style(self, context: dict | None) -> tuple[str, float]:
        resolver = getattr(self.host_brain, "resolve_tts_style", None)
        if callable(resolver):
            try:
                return resolver(context or {})
            except Exception:
                pass
        return "你说话的情感是neutral。", 1.08

    async def _host_say(self, fallback: str, context: dict | None = None, wait: bool = False) -> None:
        """主播话术位：LLM 成功且通过违禁词复核就替换，失败/超时/违规回退模板，并附带情绪指令。"""
        text = fallback
        context = dict(context or {})
        with self.vote_lock:
            recent_snapshot = list(self.recent_comments[-5:])
        context.setdefault("last", recent_snapshot)
        context.setdefault("last_host", self.last_host_lines[-3:])
        if self.host_brain is not None and self.host_brain.enabled:
            try:
                generated = await asyncio.wait_for(
                    self.host_brain.say(context or {}),
                    timeout=2.8,
                )
                if generated and generated not in self.last_host_lines and not self._blocked_content(generated):
                    required = [str(r) for r in (context.get("required") or [])]
                    if all(r in generated for r in required):
                        text = generated
            except Exception:
                text = fallback
        tts_inst, tts_rate = self._resolve_tts_style(context)
        await self._speak(text, wait=wait, tts_instruction=tts_inst, tts_rate=tts_rate)

    def _choose(self, options: list[str], key: str = "") -> str:
        key = key or "|".join(options)
        last = self.last_template_by_key.get(key, "")
        if last:
            options = [o for o in options if o != last] or options
        line = random.choice(options)
        self.last_template_by_key[key] = line
        return line

    def _choose_fmt(self, options: list[str], key: str, **kwargs) -> str:
        last = self.last_template_by_key.get(key, "")
        if last:
            options = [o for o in options if o != last] or options
        tpl = random.choice(options)
        self.last_template_by_key[key] = tpl
        return tpl.format(**kwargs)

    def _note_host(self, text: str) -> None:
        if not text:
            return
        self.last_host_lines.append(text)
        self.last_host_lines = self.last_host_lines[-3:]

    async def _idle_loop(self) -> None:
        try:
            while self.active:
                await asyncio.sleep(40)
                if not self.active or self.window_open:
                    continue
                if self.turn_task is not None and not self.turn_task.done():
                    continue
                try:
                    if self.my_handle.is_playback_busy():
                        continue
                except Exception:
                    pass
                await self._host_say(
                    "嗯……没人说话，那我先自己念叨两句。",
                    {"game": self.game_kind or "game1", "event": "idle"},
                )
        except asyncio.CancelledError:
            return

    async def _filler(self) -> None:
        self.filler_last = time.time()
        says = [s for s in ["嗯……", "啊这", "我看看", "诶", "等一下", "这个嘛"] if s != self.filler_last_say]
        self.filler_last_say = random.choice(says)
        await self._speak(self.filler_last_say, wait=False)

    async def _speak(
        self,
        text: str,
        wait: bool = False,
        *,
        tts_instruction: str = "",
        tts_rate: float | None = None,
    ) -> None:
        text = (text or "").strip()
        if not text:
            return
        logger.info(f"[GAME_BRIDGE] 主播：{text}")
        self._note_host(text)
        if not TTS_ENABLE:
            return
        try:
            config = getattr(self.my_handle, "config", None)
            if config is None:
                return
            extra = {}
            if tts_instruction:
                extra["tts_instruction"] = tts_instruction
            if tts_rate is not None:
                extra["tts_rate"] = tts_rate
            data = build_message(
                config,
                "game_host",
                "林小满",
                text,
                tts_type=config.get("audio_synthesis_type"),
                **extra,
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
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.12)

    # ---------- 弹幕 ----------
    def _blocked_content(self, content: str) -> bool:
        """命中违禁规则 → 这条文本直接丢弃（不进上下文、不计票、不播报）。

        规则与 luna/core/filters.prohibitions_handle 一致：链接 / 引流联系方式 / 脏话库 /
        本地违禁词 / 同音违禁词。区别是这里**一律丢弃**，不像正常流水线那样
        把词替换成 *——游戏模式没有念稿环节，带 * 的句子留着只会污染 LLM 上下文。
        """
        if not content:
            return False
        if _CONTACT_SPAM_RE.search(content):
            logger.info(f"[GAME_BRIDGE] 含链接或引流联系方式，丢弃：{content[:30]}")
            return True
        try:
            handle = self.my_handle
            if handle is None:
                return False
            cfg = handle.config
            common = getattr(handle, "common", None)

            if common is not None and common.is_url_check(content):
                logger.info(f"[GAME_BRIDGE] 含链接，丢弃：{content[:30]}")
                return True

            if cfg is not None and cfg.get("filter", "badwords", "enable"):
                if common is not None and common.profanity_content(content):
                    logger.info(f"[GAME_BRIDGE] 命中脏话库，丢弃：{content[:20]}")
                    return True

                matched = _badword_automaton(cfg.get("filter", "badwords", "path"))
                if matched is not None:
                    hit = next(matched.iter(content), None)
                    if hit is not None:
                        logger.info(f"[GAME_BRIDGE] 命中违禁词「{hit[1]}」，丢弃：{content[:20]}")
                        return True

                pinyin_path = cfg.get("filter", "badwords", "bad_pinyin_path")
                if pinyin_path and common is not None and common.check_sensitive_words3(pinyin_path, content):
                    logger.info(f"[GAME_BRIDGE] 命中同音违禁词，丢弃：{content[:20]}")
                    return True
        except Exception:
            logger.warning(f"[GAME_BRIDGE] 违禁词检查异常\n{traceback.format_exc()}")
        return False

    def _sanitize_username(self, raw_username) -> str | None:
        """清洗并审核观众昵称：命中违禁词/引流返回 None；纯表情昵称兜底为通用称呼。"""
        raw = str(raw_username or "").strip()
        if not raw:
            return "有位观众"
        if self._blocked_content(raw):
            return None
        cleaned = _BRACKET_EMOJI_RE.sub("", raw)
        cleaned = _UNICODE_EMOJI_RE.sub("", cleaned)
        for ch in _USER_SPECIAL_CHARS:
            cleaned = cleaned.replace(ch, "")
        cleaned = re.sub(r"\s+", "", cleaned).strip()
        if cleaned and self._blocked_content(cleaned):
            return None
        if not cleaned:
            return "有位观众"
        max_len = 8
        try:
            cfg = getattr(self.my_handle, "config", None)
            if cfg is not None:
                cfg_len = cfg.get("thanks", "username_max_len")
                if cfg_len:
                    max_len = min(int(cfg_len), 10)
        except Exception:
            pass
        return cleaned[:max_len]

    def _extract_vote_char(self, content: str, pool: set[str]) -> str | None:
        """从弹幕提取投票字：先去微信/抖音 [表情] 和否定句，再优先匹配明确指向或短猜字。"""
        cleaned = _BRACKET_EMOJI_RE.sub("", content or "")
        cleaned = _UNICODE_EMOJI_RE.sub("", cleaned).strip()
        if not cleaned:
            return None
        # 剔除“不是X”“别选X”等否定字，避免“不是马是牛”误投给“马”
        without_neg = _NEGATED_CHAR_RE.sub("", cleaned)
        # 1) 优先匹配带明确选字意图的表达（如“选牛”“是牛”“填牛”）
        for m in _INTENT_CHAR_RE.finditer(without_neg):
            c = m.group(1)
            if c in pool:
                return c
        # 2) 短弹幕（<= 6 个汉字）按首次命中字池取字；长闲聊不误算投票
        chars = re.findall(r"[\u4e00-\u9fff]", without_neg)
        if not chars or len(chars) > 6:
            return None
        return next((c for c in chars if c in pool), None)

    def on_room_event(self, kind: str, data: dict) -> bool:
        """游戏模式下拦截入场/关注/礼物，避免抢麦人设割裂或堵死 _wait_playback()。"""
        if not self.active:
            return False
        if kind == "gift":
            safe_user = self._sanitize_username(data.get("username"))
            gift_name = _BRACKET_EMOJI_RE.sub("", str(data.get("gift_name") or "")).strip()
            if safe_user and gift_name and not self._blocked_content(gift_name):
                try:
                    lowest = float((self.my_handle.config.get("thanks") or {}).get("lowest_price", 0.1))
                except Exception:
                    lowest = 0.1
                try:
                    total_price = float(data.get("total_price") or 0.0)
                except (TypeError, ValueError):
                    total_price = 0.0
                if total_price >= lowest:
                    with self.vote_lock:
                        self.recent_comments.append(f"{safe_user}送了{gift_name}")
                        self.recent_comments = self.recent_comments[-5:]
        return True

    def on_comment(self, data: dict) -> bool:
        """由 My_handle 的弹幕流水线调用；返回 True 表示游戏模式接管这条弹幕。"""
        if not self.active:
            return False
        try:
            blacklist_fn = getattr(self.my_handle, "blacklist_handle", None)
            if callable(blacklist_fn) and blacklist_fn(data):
                return True
        except Exception:
            pass
        content = str(data.get("content", ""))
        # 游戏模式是在 my_handle 的过滤链**之前**接管弹幕的（badwords/违禁词都会被跳过），
        # 所以这里自己再过一遍：命中的直接丢掉——不进主播上下文（防 LLM 复述被 TTS 念出来），
        # 也不计票（脏话本来也投不了票）。
        if self._blocked_content(content):
            return True
        user = self._sanitize_username(data.get("username"))
        if user is None:
            logger.info("[GAME_BRIDGE] 昵称命中违禁规则，整条弹幕丢弃")
            return True

        clean_comment = _BRACKET_EMOJI_RE.sub("", content).strip()
        if hasattr(self.host_brain, "note_viewer_activity"):
            self.host_brain.note_viewer_activity(user)
        with self.vote_lock:
            if clean_comment:
                self.recent_comments.append(f"{user}：{clean_comment[:40]}")
                self.recent_comments = self.recent_comments[-5:]
                self.comment_seq += 1
            if not self.window_open:
                return True
            char = self._extract_vote_char(content, self.window_pool)
            if char:
                # 允许观众在同一投票窗口内改票
                self.window_votes[user] = char
                logger.info(f"[GAME_BRIDGE] {self.game_kind or 'game'} 票+ {user}:{char}")
            else:
                logger.info(f"[GAME_BRIDGE] {self.game_kind or 'game'} 投票忽略 {user}:{content}")

        return True