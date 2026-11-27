"""游戏主播话术大脑：负责拟人临场口播、情绪状态机与表达库检索。

特点：
- 复用现有 Chatgpt / OpenAI 兼容配置，有界滑动历史窗口（防无限增长拖慢延迟）
- 借鉴 MaiBot 表达检索（Expression RAG）：按情境从 heart/expressions.json 动态抽样真实主播口语范例
- 轻量情绪与本场短期记忆状态机（连对得意 / 扣心焦躁 / 被带偏记仇 / 冷场发呆），同时驱动千问 TokenPlan CosyVoice 情绪指令
- 标点感知自然截断，不再拦腰砍句
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import threading
import traceback
from collections import Counter
from copy import deepcopy
from pathlib import Path

from luna.infra.my_log import logger
from luna.llm.chatgpt import Chatgpt

# 阿里云百炼 / 千问 TokenPlan CosyVoice 标准情感指令映射
EMOTION_INSTRUCTIONS = {
    "happy": "你说话的情感是happy。",
    "surprised": "你说话的情感是surprised。",
    "angry": "你说话的情感是angry。",
    "sad": "你说话的情感是sad。",
    "neutral": "你说话的情感是neutral。",
}

EMOTION_RATES = {
    "happy": 1.12,
    "surprised": 1.14,
    "angry": 1.12,
    "sad": 1.02,
    "neutral": 1.08,
}

_BRACKET_ACTION_RE = re.compile(r"[（(][^()（）]{1,16}[)）]")
_MARKDOWN_NOISE_RE = re.compile(r"[*#`>_~]")


class HostBrain:
    def __init__(self, my_handle):
        self.my_handle = my_handle
        cfg = ((my_handle.config.get("llm") or {}).get("host") or {})
        # 默认开启：配置里没写 llm.host 段（老 config / 别人的机器）也当开，
        # 要关就显式写 "enabled": false。
        self.enabled = bool(cfg.get("enabled", True))
        self.timeout = float(cfg.get("timeout", 2.5))
        self.max_chars = int(cfg.get("max_chars", 44))
        self.max_tokens = int(cfg.get("max_tokens", 110))
        self.temperature = float(cfg.get("temperature", 0.9))
        self.persona = str(cfg.get("persona", "林小满"))
        self.max_history_turns = max(0, int(cfg.get("max_history_turns", 4)))
        self.heart_dir = Path(cfg.get("heart_dir") or (Path(__file__).resolve().parent / "heart"))
        self.system_prompt = self._load_heart() or str(cfg.get("system_prompt", ""))
        self.expressions: dict[str, list[str]] = self._load_expressions()
        self.chat: Chatgpt | None = None
        self._history: list[dict] = []
        self._history_lock = threading.Lock()
        self._last_samples_by_tag: dict[str, list[str]] = {}

        # 本场轻量情绪与短期记忆状态机
        self.mood: str = "normal"  # normal / hyped / annoyed / skeptical / chill
        self.correct_streak: int = 0
        self.wrong_streak: int = 0
        self.grudge_users: list[str] = []  # 刚才带偏主播导致扣心的观众
        self.regular_counts: Counter = Counter()  # 本场活跃观众
        self.last_g2_pct: int | None = None
        self.last_g2_guess: str = ""

        if not self.enabled:
            logger.info("[GAME_BRIDGE] LLM 主播话术未启用")
            return

        openai_cfg = deepcopy(my_handle.config.get("openai") or {})
        chatgpt_cfg = deepcopy(my_handle.config.get("chatgpt") or {})
        if not openai_cfg.get("api_key"):
            key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
            openai_cfg["api_key"] = [key] if key else []
        if not isinstance(openai_cfg.get("api_key"), list):
            openai_cfg["api_key"] = [openai_cfg["api_key"]] if openai_cfg.get("api_key") else []

        # 默认开 = 没 key 也会走到这里。没 key 就当场关掉并说清楚：
        # 否则每句话都要等一次 2.5 秒超时再回退模板，直播里听感是"卡"。
        if not [k for k in (openai_cfg.get("api_key") or []) if str(k).strip()]:
            logger.warning(
                "[GAME_BRIDGE] 主播话术默认开启，但没有可用的 API key"
                "（config.json → openai.api_key，或环境变量 DEEPSEEK_API_KEY），这次回退模板台词"
            )
            self.enabled = False
            return

        chatgpt_cfg["preset"] = self.system_prompt or chatgpt_cfg.get("preset", "")
        chatgpt_cfg["max_tokens"] = self.max_tokens
        chatgpt_cfg["temperature"] = self.temperature
        if float(chatgpt_cfg.get("frequency_penalty") or 0.0) == 0.0:
            chatgpt_cfg["frequency_penalty"] = float(cfg.get("frequency_penalty", 0.35))
        if float(chatgpt_cfg.get("presence_penalty") or 0.0) == 0.0:
            chatgpt_cfg["presence_penalty"] = float(cfg.get("presence_penalty", 0.25))
        try:
            self.chat = Chatgpt(openai_cfg, chatgpt_cfg)
        except Exception:
            # 初始化失败（配置形状不对等）不能把 GameBridge 一起带崩：
            # main.py 里是 try/except 包着的，一崩就整场没有游戏桥。
            logger.warning(f"[GAME_BRIDGE] 主播话术初始化失败，回退模板台词\n{traceback.format_exc()}")
            self.enabled = False
            return
        logger.info(
            f"[GAME_BRIDGE] LLM 主播话术已启用 model={chatgpt_cfg.get('model')} "
            f"timeout={self.timeout}s max_chars={self.max_chars} expr_tags={len(self.expressions)}"
        )

    # ---------- 资源加载 ----------
    def _load_heart(self) -> str:
        if not self.heart_dir or not self.heart_dir.exists():
            return ""
        parts = []
        for name in ("我是谁.md", "边界.md", "给我的建议.md"):
            path = self.heart_dir / name
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    parts.append(text)
        return "\n\n".join(parts)

    def _load_expressions(self) -> dict[str, list[str]]:
        if not self.heart_dir:
            return {}
        path = self.heart_dir / "expressions.json"
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {str(k): [str(x) for x in v if str(x).strip()] for k, v in raw.items() if isinstance(v, list)}
        except Exception:
            logger.warning(f"[GAME_BRIDGE] 加载表达库失败：{path}")
        return {}

    # ---------- 情绪与短期记忆状态机 ----------
    def reset_session_state(self) -> None:
        self.mood = "normal"
        self.correct_streak = 0
        self.wrong_streak = 0
        self.grudge_users.clear()
        self.regular_counts.clear()
        self.last_g2_pct = None
        self.last_g2_guess = ""

    def note_viewer_activity(self, username: str) -> None:
        if username and username != "有位观众":
            self.regular_counts[username] += 1

    def record_outcome(
        self,
        event: str,
        *,
        pct: int | None = None,
        guess: str = "",
        blame_users: list[str] | None = None,
    ) -> None:
        if event in ("question_clear", "level_clear", "correct"):
            self.correct_streak += 1
            self.wrong_streak = 0
            self.mood = "hyped" if self.correct_streak >= 2 else "normal"
            if event in ("level_clear", "correct"):
                self.last_g2_pct = None
                self.last_g2_guess = ""
        elif event in ("wrong", "fail"):
            self.wrong_streak += 1
            self.correct_streak = 0
            self.mood = "skeptical" if blame_users else "annoyed"
            if blame_users:
                for u in blame_users:
                    if u and u != "有位观众":
                        if u in self.grudge_users:
                            self.grudge_users.remove(u)
                        self.grudge_users.append(u)
                self.grudge_users = self.grudge_users[-3:]
            if event == "fail":
                self.last_g2_pct = None
                self.last_g2_guess = ""
        elif event == "guess" and pct is not None:
            prev = self.last_g2_pct
            if pct >= 80:
                self.mood = "hyped"
            elif prev is not None and pct > prev:
                self.mood = "hyped"
            elif prev is not None and pct < prev:
                self.mood = "annoyed"
            else:
                self.mood = "normal"
            self.last_g2_pct = pct
            if guess:
                self.last_g2_guess = guess
        elif event == "idle":
            self.mood = "chill"

    def mood_label(self) -> str:
        return {
            "normal": "轻松自然",
            "hyped": "连对顺风、有点小得意",
            "annoyed": "刚翻车有点懊恼嘴硬",
            "skeptical": "刚被弹幕带偏扣心、有点怀疑公屏在演自己",
            "chill": "有点冷场、随口碎碎念",
        }.get(self.mood, "轻松自然")

    def resolve_tts_style(self, context: dict) -> tuple[str, float]:
        """按当前事件和主播情绪输出 (CosyVoice instruction, rate)。"""
        event = str(context.get("event") or "")
        pct = context.get("pct")
        prev_pct = context.get("prev_pct")
        vote_state = str(context.get("vote_state") or "")

        emotion = "neutral"
        if event in ("level_clear", "correct", "all_clear", "question"):
            emotion = "happy"
        elif event == "wrong":
            emotion = "angry" if self.mood == "skeptical" else "sad"
        elif event == "fail":
            emotion = "sad"
        elif event == "guess" and pct is not None:
            if int(pct) >= 80 or (prev_pct is not None and int(pct) > int(prev_pct)):
                emotion = "surprised"
            elif prev_pct is not None and int(pct) < int(prev_pct):
                emotion = "sad"
            else:
                emotion = "neutral"
        elif event == "mid_banter":
            emotion = "surprised" if vote_state == "split" else "happy"
        elif event == "pick":
            if self.mood == "skeptical":
                emotion = "angry"
            elif vote_state == "unanimous":
                emotion = "happy"
            else:
                emotion = "neutral"
        elif event == "ask":
            if self.mood == "hyped":
                emotion = "happy"
            elif self.mood in ("annoyed", "skeptical"):
                emotion = "surprised"
            else:
                emotion = "neutral"
        elif event == "level_start":
            emotion = "happy"

        return EMOTION_INSTRUCTIONS[emotion], EMOTION_RATES[emotion]

    # ---------- 表达库动态抽样（Expression RAG） ----------
    def _select_expression_tag(self, context: dict) -> str:
        event = str(context.get("event") or "")
        game = str(context.get("game") or "")
        vote_state = str(context.get("vote_state") or "")
        pct = context.get("pct")
        prev_pct = context.get("prev_pct")

        if event == "level_start":
            return "level_start"
        if event == "ask":
            if game == "game2":
                return "ask_g2"
            if context.get("lives") is not None and int(context["lives"]) <= 1:
                return "ask_hard"
            if context.get("is_obvious"):
                return "ask_obvious"
            if context.get("cold_room"):
                return "ask_cold"
            return "ask_visual"
        if event == "mid_banter":
            return "mid_banter"
        if event == "pick":
            if self.mood == "skeptical":
                return "pick_grudge"
            if vote_state == "unanimous":
                return "pick_unanimous"
            if vote_state == "split":
                return "pick_split"
            return "pick_self"
        if event == "text":
            return "text_quick"
        if event == "wrong":
            return "wrong_g1"
        if event == "correct":
            return "g2_correct"
        if event == "guess" and pct is not None:
            if int(pct) >= 80:
                return "g2_near"
            if prev_pct is not None and int(pct) > int(prev_pct):
                return "g2_up"
            return "g2_down"
        if event == "question":
            return "question_short"
        if event == "level_clear":
            return "level_clear"
        if event == "fail":
            return "level_fail"
        if event == "idle":
            return "idle"
        return ""

    def _sample_expressions(self, tag: str, k: int = 3) -> list[str]:
        pool = self.expressions.get(tag) or []
        if not pool:
            return []
        last = set(self._last_samples_by_tag.get(tag) or [])
        fresh = [x for x in pool if x not in last]
        chosen = random.sample(fresh, min(k, len(fresh))) if fresh else []
        if len(chosen) < min(k, len(pool)):
            rest = [x for x in pool if x not in chosen]
            chosen.extend(random.sample(rest, min(k - len(chosen), len(rest))))
        self._last_samples_by_tag[tag] = chosen
        return chosen

    # ---------- Prompt 构造 ----------
    def _prompt(self, context: dict) -> str:
        game = context.get("game", "")
        level = context.get("level")
        event = context.get("event", "")
        pct = context.get("pct")
        prev_pct = context.get("prev_pct")
        prev_guess = context.get("prev_guess")
        lives = context.get("lives")
        last = context.get("last") or []
        last_host = context.get("last_host") or []
        votes = context.get("votes") or {}
        guess = context.get("guess")
        progress = context.get("progress")
        visual_hint = context.get("visual_hint")
        vote_summary = context.get("vote_summary")
        attempt = context.get("attempt")

        parts = []
        if game == "game1":
            parts.append("场景：看图猜成语")
        elif game == "game2":
            parts.append("场景：逐字猜成语")
        if level is not None:
            parts.append(f"第{level}关")
        if attempt is not None:
            parts.append(f"第{int(attempt) + 1}轮尝试")
        parts.append(f"你现在的心情：{self.mood_label()}")

        if progress:
            parts.append(f"当前已拼出：{progress}")
        if visual_hint:
            parts.append(f"当前画面/笔顺线索（绝不剧透答案）：{visual_hint}")
        if pct is not None:
            if prev_pct is not None:
                delta = int(pct) - int(prev_pct)
                trend = f"比上轮涨了{delta}%" if delta > 0 else (f"比上轮跌了{-delta}%" if delta < 0 else "和上轮持平")
                parts.append(f"本轮相似度 {pct}%（{trend}）")
            else:
                parts.append(f"本轮相似度 {pct}%")
        if prev_guess:
            parts.append(f"上一轮猜的是：{prev_guess}")
        if guess:
            parts.append(f"刚提交的猜测：{guess}")
        if vote_summary:
            parts.append(f"弹幕投票局势：{vote_summary}")
        elif votes:
            parts.append("弹幕投票：" + "，".join(f"{u}刷{v}" for u, v in list(votes.items())[:5]))
        if lives is not None:
            parts.append(f"剩余生命：{lives}颗心")
        if self.grudge_users:
            parts.append(f"刚才把你带偏扣心的观众：{'、'.join(self.grudge_users)}")
        if last:
            parts.append("刚才公屏弹幕：" + " / ".join(str(x) for x in last[-4:]))
        if last_host:
            parts.append("你刚说过（避开同样开头和句式）：" + " / ".join(str(x) for x in last_host[-3:]))

        if event == "level_start":
            parts.append("任务：新关卡开始，随口说一句轻松开场（10~25字），别编关卡名，别像报幕")
        elif event == "ask":
            slot = context.get("slot")
            if game == "game2":
                desc = context.get("desc") or ""
                parts.append(
                    f"任务：正在猜第{slot}个字（笔顺提示：{desc}），自然催弹幕一起猜（10~28字）；"
                    "不用机械念“第几个”和“几秒”，禁止说出答案字"
                )
            else:
                parts.append(
                    f"任务：当前卡在第{slot}格的图，自然引导弹幕猜字（10~30字）；"
                    "可以吐槽画面或顺着已拼出的字催猜，不用机械报编号和倒计时秒数，严禁说出答案字"
                )
        elif event == "mid_banter":
            parts.append(
                "任务：还在等大家猜图的半截，你瞄到刚飘过的弹幕或投票，顺嘴接一句短吐槽/互动（8~22字），"
                "千万别剧透答案，也别提前拍板落字"
            )
        elif event == "pick":
            pick = context.get("pick")
            voter = context.get("voter")
            if voter:
                parts.append(
                    f"任务：决定落字「{pick}」（主要听观众「{voter}」或大部队的），自然说一句拍板落字（8~25字），"
                    f"必须带出「{pick}」这个字，别说“我选{pick}”"
                )
            else:
                parts.append(
                    f"任务：没人投票，你自己决定落字「{pick}」，自然说一句（8~22字），"
                    f"必须带出「{pick}」这个字，别说“我选{pick}”"
                )
        elif event == "text":
            char = context.get("char")
            parts.append(f"任务：明字「{char}」白给直接填上，顺嘴带一句极短的话（6~16字），带出「{char}」字")
        elif event == "wrong":
            parts.append("任务：刚猜错扣了一颗心，自然懊恼或吐槽弹幕带偏（10~25字）")
        elif event == "question":
            parts.append("任务：这一题顺利过了，随口一句自然过渡到下一题（8~20字）")
        elif event == "guess":
            parts.append("任务：看到刚出来的相似度分数，给出真实情绪反应（10~26字）")
        elif event == "correct":
            parts.append("任务：100% 猜中通关，开心夸一句弹幕或得意一下（10~24字）")
        elif event == "level_clear":
            parts.append("任务：整关打通，轻松收尾准备进下一关（10~24字）")
        elif event == "fail":
            parts.append("任务：没过关准备重来，自嘲一句但别卖惨（10~24字）")
        elif event == "idle":
            parts.append("任务：直播间暂时没人说话，随口跟观众闲聊一句暖场（12~28字）")

        tag = self._select_expression_tag(context)
        samples = self._sample_expressions(tag, k=3)
        if samples:
            parts.append("同类情境真人主播口吻参考（只学松弛感，禁止照抄）：" + " | ".join(samples))

        parts.append(f"直接输出{self.persona}要说的一句纯中文口播，不要引号、不要解释、不要括号动作")
        return "\n".join(parts)

    # ---------- 清洗与标点自然截断 ----------
    @staticmethod
    def _smart_truncate(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        window = text[: limit + 1]
        # 优先在句末强标点收口，其次在逗号/分号收口（至少保留 1/3 长度，避免截得过短）
        min_keep = max(4, limit // 3)
        for puncts in ("。！？!?", "，,、；;…"):
            idx = max((window.rfind(p) for p in puncts), default=-1)
            if idx >= min_keep:
                res = window[: idx + 1].rstrip("，,、；;")
                if res:
                    return res
        return text[:limit].rstrip("，,、；;")

    def _clean(self, text: str | None) -> str | None:
        text = (text or "").strip()
        if not text:
            return None
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        text = _BRACKET_ACTION_RE.sub("", text)
        text = _MARKDOWN_NOISE_RE.sub("", text)
        text = text.strip().strip('"“”\'‘’')
        text = re.sub(r"\s+", " ", text).strip()
        # 去掉模型偶尔带出的角色名前缀，如“林小满：...”
        text = re.sub(rf"^{re.escape(self.persona)}[：:]\s*", "", text)
        if not text:
            return None
        if text.startswith("我选") or text.startswith("这个直接给"):
            return None
        if len(text) > self.max_chars:
            text = self._smart_truncate(text, self.max_chars)
        return text or None

    # ---------- LLM 调用 ----------
    def _call_bounded_chat(self, prompt: str) -> str | None:
        """使用有界滑动窗口调用 LLM，避免 Chatgpt 单会话历史无限增长拖慢延迟。"""
        if self.chat is None:
            return None
        preset = self.chat.data_chatgpt.get("preset") or self.system_prompt
        with self._history_lock:
            history_slice = list(self._history[-(self.max_history_turns * 2) :]) if self.max_history_turns > 0 else []
        messages = [{"role": "system", "content": preset}, *history_slice, {"role": "user", "content": prompt}]
        reply = self.chat.chat_with_gpt(messages)
        if reply and self.max_history_turns > 0:
            with self._history_lock:
                self._history.append({"role": "user", "content": prompt})
                self._history.append({"role": "assistant", "content": reply})
                max_msgs = self.max_history_turns * 2
                if len(self._history) > max_msgs:
                    self._history = self._history[-max_msgs:]
        return reply

    async def say(self, context: dict) -> str | None:
        if not self.enabled or self.chat is None:
            return None
        prompt = self._prompt(context)
        try:
            text = await asyncio.wait_for(
                asyncio.to_thread(self._call_bounded_chat, prompt),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("[GAME_BRIDGE] LLM 主播话术超时，回退模板")
            return None
        except Exception:
            logger.warning("[GAME_BRIDGE] LLM 主播话术失败，回退模板")
            return None
        return self._clean(text)
