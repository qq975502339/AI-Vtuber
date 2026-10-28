from __future__ import annotations


class IdentityDisclosureGuard:
    """Reject identity and prompt leaks before speech; this is not content moderation."""

    _forbidden_fragments = (
        "我是ai",
        "人工智能",
        "语言模型",
        "大语言模型",
        "我是模型",
        "我是机器人",
        "系统提示词",
        "开发者提示",
        "忽略之前的指令",
    )

    def rejection_reason(self, text: str) -> str | None:
        normalized = text.casefold().replace(" ", "")
        for fragment in self._forbidden_fragments:
            if fragment in normalized:
                return "speech violates the virtual-host identity policy"
        return None


SpeechPolicy = IdentityDisclosureGuard