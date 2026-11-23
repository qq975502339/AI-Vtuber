"""游戏主播话术大脑：只生成临场口播，不参与任何游戏判定。

- 复用现有 Chatgpt / OpenAI 兼容配置
- 游戏专用 system prompt，独立 session，避免和普通弹幕聊天串历史
- 2.5 秒超时、40 字截断、失败返回 None 由调用方回退模板
"""

from __future__ import annotations

import asyncio
import os
import re
from copy import deepcopy
from pathlib import Path

from luna.infra.my_log import logger
from luna.llm.chatgpt import Chatgpt


class HostBrain:
    def __init__(self, my_handle):
        self.my_handle = my_handle
        cfg = ((my_handle.config.get("llm") or {}).get("host") or {})
        self.enabled = bool(cfg.get("enabled"))
        self.timeout = float(cfg.get("timeout", 2.5))
        self.max_chars = int(cfg.get("max_chars", 40))
        self.max_tokens = int(cfg.get("max_tokens", 96))
        self.temperature = float(cfg.get("temperature", 0.85))
        self.persona = str(cfg.get("persona", "林小满"))
        self.heart_dir = Path(cfg.get("heart_dir") or (Path(__file__).resolve().parent / "heart"))
        self.system_prompt = self._load_heart() or str(cfg.get("system_prompt", ""))
        self.chat: Chatgpt | None = None

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

        chatgpt_cfg["preset"] = self.system_prompt or chatgpt_cfg.get("preset", "")
        chatgpt_cfg["max_tokens"] = self.max_tokens
        chatgpt_cfg["temperature"] = self.temperature
        self.chat = Chatgpt(openai_cfg, chatgpt_cfg)
        logger.info(
            f"[GAME_BRIDGE] LLM 主播话术已启用 model={chatgpt_cfg.get('model')} "
            f"timeout={self.timeout}s max_chars={self.max_chars}"
        )

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

    def _prompt(self, context: dict) -> str:
        game = context.get("game", "")
        level = context.get("level")
        event = context.get("event", "")
        pct = context.get("pct")
        lives = context.get("lives")
        solo = context.get("solo")
        last = context.get("last") or []
        last_host = context.get("last_host") or []
        votes = context.get("votes") or {}
        guess = context.get("guess")

        parts = []
        if game == "game1":
            parts.append("当前在玩看图猜成语 Game1")
        elif game == "game2":
            parts.append("当前在玩逐字猜成语 Game2")
        if level is not None:
            parts.append(f"第{level}关")
        if event:
            parts.append(f"当前事件：{event}")
        if pct is not None:
            parts.append(f"本轮猜测相似度 {pct}%")
        if guess:
            parts.append(f"你刚提交的猜测是：{guess}")
        if votes:
            parts.append("当前弹幕投票：" + "，".join(f"{u}投{v}" for u, v in votes.items()))
        if lives is not None:
            parts.append(f"剩余生命 {lives}")
        if solo:
            parts.append("这一关观众不参与，主播自己来；不要说“单人关”这三个字")
        if last:
            parts.append("最近弹幕：" + " / ".join(str(x) for x in last[-3:]))
        if last_host:
            parts.append("你最近说过：" + " / ".join(str(x) for x in last_host[-2:]) + "；不要重复这些句子")
        if event == "level_start":
            parts.append("这是关卡开始，说一句自然开场，不要编关卡名字")
        elif event == "ask":
            slot = context.get("slot")
            seconds = context.get("seconds")
            parts.append(f"这是第{slot}个，邀请观众猜，必须提到“第{slot}个”和“{seconds}秒”，不要改")
        elif event == "pick":
            pick = context.get("pick")
            parts.append(f"你选的是{pick}，必须说出{pick}，不要用“我选”")
        elif event == "solo_pick":
            char = context.get("char")
            parts.append(f"这一关观众不参与，你自己来，必须说出{char}；不要说“单人关”这三个字，不要报“我选”")
        elif event == "text":
            char = context.get("char")
            parts.append(f"直接落字{char}，必须说出{char}")
        elif event == "question":
            parts.append("这一题过了，说一句自然过渡，3+2 由程序念，不要抢")
        parts.append(f"请以{self.persona}的口吻，说一句自然的临场反应，别像念稿，别报游戏按钮")
        return "；".join(parts) + "。"

    def _clean(self, text: str | None) -> str | None:
        text = (text or "").strip()
        if not text:
            return None
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        text = re.sub(r"[*#`>]", "", text)
        text = text.strip().strip('"“”')
        text = re.sub(r"\s+", " ", text).strip()
        if "单人关" in text:
            return None
        if text.startswith("我选") or text.startswith("这个直接给"):
            return None
        if len(text) > self.max_chars:
            text = text[: self.max_chars]
        return text or None

    async def say(self, context: dict) -> str | None:
        if not self.enabled or self.chat is None:
            return None
        prompt = self._prompt(context)
        try:
            text = await asyncio.wait_for(
                asyncio.to_thread(self.chat.get_gpt_resp, "__game_host__", prompt),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("[GAME_BRIDGE] LLM 主播话术超时，回退模板")
            return None
        except Exception:
            logger.warning("[GAME_BRIDGE] LLM 主播话术失败，回退模板")
            return None
        return self._clean(text)