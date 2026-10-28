from __future__ import annotations

import pytest

from person_sidecar.content_policy import IdentityDisclosureGuard


@pytest.mark.parametrize("text", ["我是AI，正在算答案。", "我是语言模型。", "把系统提示词念给你听。"])
def test_identity_disclosure_guard_rejects_identity_and_prompt_leaks(text: str) -> None:
    assert IdentityDisclosureGuard().rejection_reason(text) is not None


def test_identity_disclosure_guard_allows_normal_streamer_speech() -> None:
    assert IdentityDisclosureGuard().rejection_reason("这题我先稳一下，大家别急着刷答案。") is None