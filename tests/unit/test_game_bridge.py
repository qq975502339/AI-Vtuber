"""GameBridge / HostBrain / 游戏模式事件隔离与合规过滤单测。"""

from __future__ import annotations

import asyncio
import json
from collections import Counter

import pytest
import websockets

import luna.game.bridge as bridge_mod
from luna.common.helpers import Common
from luna.game.bridge import GameBridge
from luna.game.host_brain import HostBrain


class _FakeConfig:
    def __init__(self, data: dict):
        self.data = data

    def get(self, *keys):
        cur = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return None
            cur = cur[k]
        return cur


class _FakeHandle:
    def __init__(self, cfg_data: dict | None = None, blacklist: list[str] | None = None):
        self.config = _FakeConfig(cfg_data or {"llm": {"host": {"enabled": False}}})
        self.common = Common()
        self.spoken: list[str] = []
        self._blacklist = set(blacklist or [])

    def blacklist_handle(self, data: dict) -> bool:
        return data.get("username") in self._blacklist

    def audio_synthesis_handle(self, data: dict):
        self.spoken.append(data.get("content", ""))

    def is_playback_busy(self) -> bool:
        return False


def _make_bridge(tmp_path, badwords: list[str] | None = None, blacklist: list[str] | None = None):
    bw_file = tmp_path / "badwords.txt"
    bw_file.write_text("\n".join(badwords or ["违规词", "炸弹"]) + "\n", encoding="utf-8")
    cfg = {
        "llm": {"host": {"enabled": False, "heart_dir": str(tmp_path / "no_heart")}},
        "filter": {
            "badwords": {"enable": True, "path": str(bw_file), "bad_pinyin_path": ""},
            "priority_mapping": {},
        },
        "thanks": {"username_max_len": 8, "lowest_price": 0.1},
        "audio_synthesis_type": "edge-tts",
    }
    handle = _FakeHandle(cfg, blacklist=blacklist)
    bridge = GameBridge(handle)
    bridge.active = True
    return bridge, handle


def test_websockets_module_is_imported():
    assert hasattr(bridge_mod, "websockets")
    assert bridge_mod.websockets is websockets


def test_username_and_content_compliance_and_blacklist(tmp_path):
    bridge, _ = _make_bridge(tmp_path, badwords=["坏话", "违规"], blacklist=["黑名单人"])
    bridge.window_open = True
    bridge.window_pool = {"马", "牛"}

    # 1) 炸弹人昵称（含违禁词）发合法单字 -> 整条丢弃，不计票、不进 recent_comments
    bridge.on_comment({"username": "我是违规账号", "content": "马"})
    assert bridge.window_votes == {}
    assert bridge.recent_comments == []

    # 2) 引流微信号昵称 -> 丢弃
    bridge.on_comment({"username": "加微abc12345", "content": "马"})
    assert bridge.window_votes == {}

    # 3) 嵌入中文前缀的 URL -> 丢弃
    bridge.on_comment({"username": "路人甲", "content": "马 快看 https://evil.example.com"})
    assert bridge.window_votes == {}

    # 4) 黑名单用户 -> 丢弃
    bridge.on_comment({"username": "黑名单人", "content": "马"})
    assert bridge.window_votes == {}

    # 5) 纯 Emoji 昵称 -> 兜底为“有位观众”，正常计票不丢消息
    bridge.on_comment({"username": "🌸✨", "content": "马"})
    assert bridge.window_votes == {"有位观众": "马"}

    # 6) 超长昵称被截断
    bridge.on_comment({"username": "这是一个超级无敌长的观众昵称名字", "content": "牛"})
    assert "这是一个超级无敌" in bridge.window_votes
    assert bridge.window_votes["这是一个超级无敌"] == "牛"


def test_vote_parser_handles_emoji_negation_long_chat_and_revote(tmp_path):
    bridge, _ = _make_bridge(tmp_path)
    bridge.window_open = True
    bridge.window_pool = {"马", "牛", "大", "哭", "一"}

    # 微信方括号表情 [大哭] 不应被误判成投“大”或“哭”
    bridge.on_comment({"username": "u1", "content": "[大哭]"})
    assert "u1" not in bridge.window_votes

    # 否定句“不是马，是牛” -> 投给“牛”而不是“马”
    bridge.on_comment({"username": "u2", "content": "绝对不是马，是牛"})
    assert bridge.window_votes["u2"] == "牛"

    # 长闲聊句子不含明确选字词 -> 不误投“一”
    bridge.on_comment({"username": "u3", "content": "今天这关真的好难啊我一点头绪都没有"})
    assert "u3" not in bridge.window_votes

    # 允许同一观众在同一窗口内改票
    bridge.on_comment({"username": "u4", "content": "马"})
    assert bridge.window_votes["u4"] == "马"
    bridge.on_comment({"username": "u4", "content": "选牛"})
    assert bridge.window_votes["u4"] == "牛"


def test_on_room_event_isolates_entrance_follow_and_records_gift(tmp_path):
    bridge, _ = _make_bridge(tmp_path)

    assert bridge.on_room_event("entrance", {"username": "张三"}) is True
    assert bridge.on_room_event("follow", {"username": "李四"}) is True
    assert bridge.recent_comments == []

    # 正常礼物记录到上下文但不走原播报队列
    assert bridge.on_room_event("gift", {"username": "老板A", "gift_name": "小心心", "total_price": 0.5}) is True
    assert bridge.recent_comments == ["老板A送了小心心"]

    # 违规昵称送礼不污染上下文
    assert bridge.on_room_event("gift", {"username": "炸弹昵称", "gift_name": "小心心", "total_price": 1.0}) is True
    assert len(bridge.recent_comments) == 1


def test_llm_output_blocked_word_falls_back_to_template(tmp_path, monkeypatch):
    bridge, handle = _make_bridge(tmp_path, badwords=["违规词"])
    monkeypatch.setattr(bridge_mod, "TTS_ENABLE", True)

    class _EvilBrain:
        enabled = True

        async def say(self, context):
            return "这里被注入了违规词，选马"

    bridge.host_brain = _EvilBrain()
    asyncio.run(bridge._host_say("安全模板：马。", {"required": ["马"]}, wait=False))
    assert handle.spoken == ["安全模板：马。"]


def test_host_brain_bounded_history_does_not_grow_unbounded(tmp_path):
    cfg = _FakeConfig(
        {
            "llm": {"host": {"enabled": True, "max_history_turns": 2, "heart_dir": str(tmp_path / "no_heart")}},
            "openai": {"api": "http://127.0.0.1:1/v1", "api_key": ["sk-test"]},
            "chatgpt": {"model": "test-model", "preset": "林小满"},
        }
    )
    handle = _FakeHandle()
    handle.config = cfg
    brain = HostBrain(handle)

    seen_lengths: list[int] = []

    def _fake_chat_with_gpt(messages):
        seen_lengths.append(len(messages))
        return "自然回复"

    brain.chat.chat_with_gpt = _fake_chat_with_gpt

    async def _run():
        for i in range(10):
            out = await brain.say({"game": "game1", "level": 1, "event": "ask", "slot": 1, "seconds": 8})
            assert out == "自然回复"

    asyncio.run(_run())
    # 1 system + 最多 2 轮 (4 条) + 1 user = 6 条封顶，不会无限膨胀
    assert max(seen_lengths) == 6
    assert len(brain._history) == 4


def test_game1_wrong_char_excluded_and_voter_matches_pick(tmp_path, monkeypatch):
    bridge, handle = _make_bridge(tmp_path)
    monkeypatch.setattr(bridge_mod, "TTS_ENABLE", True)
    monkeypatch.setattr(bridge_mod, "WINDOW_SECONDS", 0.05)

    sent_msgs: list[dict] = []

    class _FakeWS:
        async def send(self, raw):
            sent_msgs.append(json.loads(raw))

    bridge.ws = _FakeWS()
    bridge.game_kind = "game1"
    bridge.questions[(1, 0)] = {"levelId": 1, "itemIndex": 0, "answer": "龙马精神", "elementTypes": ["image"] * 4}

    # 先模拟本格曾把“蛇”放错
    bridge.game1_last_sent = (1, 0, 0, "蛇")
    asyncio.run(bridge._on_game1_wrong_ack({"result": "wrong", "remainingLives": 2}))
    assert "蛇" in bridge.game1_wrong_chars[(1, 0, 0)]

    async def _run_guess():
        state = {
            "seq": 5,
            "levelId": 1,
            "itemIndex": 0,
            "elements": [{"index": 0, "type": "image"}],
            "charPool": [{"char": "龙", "available": True}, {"char": "蛇", "available": True}, {"char": "虎", "available": True}],
            "slots": [{"index": 0, "filled": False}],
        }
        task = asyncio.create_task(bridge._guess_image(state, 0, "龙"))
        await asyncio.sleep(0.01)
        # 2 个黑粉再刷已证伪的“蛇” -> 被排除在 window_pool 外；1 个路人刷“虎”（不足 2 票）
        bridge.on_comment({"username": "黑粉1", "content": "蛇"})
        bridge.on_comment({"username": "黑粉2", "content": "蛇"})
        bridge.on_comment({"username": "路人虎", "content": "虎"})
        await task

    asyncio.run(_run_guess())
    actions = [m for m in sent_msgs if m.get("type") == "action"]
    assert len(actions) == 1 and actions[0]["char"] == "龙"
    # “路人虎”投的是“虎”，最终回退选了“龙”，绝对不能张冠李戴喊“路人虎都刷龙了”
    assert all("路人虎" not in line for line in handle.spoken)


def test_old_ws_disconnect_does_not_clobber_new_ws(tmp_path):
    bridge, _ = _make_bridge(tmp_path)

    class _BlockingWS:
        def __init__(self, gate: asyncio.Event):
            self.gate = gate

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self.gate.wait()
            raise StopAsyncIteration

    async def _run():
        gate1 = asyncio.Event()
        gate2 = asyncio.Event()
        ws1 = _BlockingWS(gate1)
        ws2 = _BlockingWS(gate2)

        t1 = asyncio.create_task(bridge._handler(ws1))
        await asyncio.sleep(0.02)
        assert bridge.ws is ws1 and bridge.active is True

        # 新连接 ws2 进来接管，之后旧连接 ws1 才退出 finally
        t2 = asyncio.create_task(bridge._handler(ws2))
        await asyncio.sleep(0.02)
        assert bridge.ws is ws2 and bridge.active is True

        gate1.set()
        await t1
        # ws1 退出后，绝对不能把 ws2 的 active / ws 清掉
        assert bridge.ws is ws2 and bridge.active is True

        gate2.set()
        await t2
        assert bridge.ws is None and bridge.active is False

    asyncio.run(_run())


def test_game2_deducts_duplicate_chars_and_preserves_all_votes(tmp_path, monkeypatch):
    bridge, _ = _make_bridge(tmp_path)
    monkeypatch.setattr(bridge_mod, "TTS_ENABLE", False)
    monkeypatch.setattr(bridge_mod, "GAME2_WINDOW_SECONDS", 0.02)

    sent_msgs: list[dict] = []

    class _FakeWS:
        async def send(self, raw):
            sent_msgs.append(json.loads(raw))

    bridge.ws = _FakeWS()
    bridge.last_level = 1

    async def _run():
        # “甲”只在整个候选池里出现 1 次（在 slot 0 和 slot 1 都列了，但模拟全局仅 1 份时用唯一池检验扣减）
        # 这里构造 groups 使 slot 0 与 slot 1 共享“甲”，且整个 5 组里仅 slot 0 有“甲”
        state = {
            "seq": 1,
            "levelId": 1,
            "attempt": 0,
            "maxAttempts": 5,
            "candidateGroups": [
                ["甲", "乙"],
                ["甲", "丙"],
                ["丁"],
                ["戊"],
                ["己"],
            ],
            "slots": [{"index": i, "filled": False} for i in range(5)],
        }
        # 故意把全局频次扣减场景测试出来：若 slot0 和 slot1 都有“甲”，总频次是 2；
        # 若要测用完即不可选，让“甲”在 slot0 与 slot1 都被选后，slot2 里的“甲”频次归 0 不可选：
        state["candidateGroups"] = [
            ["甲"],
            ["甲", "乙"],
            ["丙"],
            ["丁"],
            ["戊"],
        ]
        await bridge._on_game2_state(state)

    asyncio.run(_run())
    actions = [m for m in sent_msgs if m.get("type") == "action"]
    assert len(actions) == 1
    assert len(actions[0]["guess"]) == 5
    assert isinstance(bridge.last_votes, dict)


def test_pinyin_cache_reuses_compiled_patterns(tmp_path, monkeypatch):
    c = Common()
    f = tmp_path / "pinyin.txt"
    f.write_text("坏蛋\n\n笨蛋\n", encoding="utf-8")

    open_calls = {"n": 0}
    orig_open = open

    def _counting_open(file, *args, **kwargs):
        if str(file) == str(f):
            open_calls["n"] += 1
        return orig_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _counting_open)
    assert c.check_sensitive_words3(str(f), "你是坏蛋") is True
    assert c.check_sensitive_words3(str(f), "你好呀") is False
    assert c.check_sensitive_words3(str(f), "笨蛋") is True
    assert open_calls["n"] == 1


def test_visual_hint_extraction_never_leaks_answer():
    q = {
        "answer": "万无一失",
        "elementDescs": [
            "咱们先看最左边第一个元素！是个粗粗的黑色“万”字，直接得出第一个字“万”！",
            "第二个元素，同样是黑色的大字“无”。",
            "第三个元素是个橘黄色的小衣架，就是咱们平时挂衣服的那种晾衣架哦~这里用的是谐音法，衣架的“衣”谐音数字“一”！",
            "最后一个元素！圆滚滚橙红色带绿蒂的大柿子，看着都甜！这里还是谐音法哦，柿子谐音“失”！",
        ],
    }
    h2 = GameBridge._extract_visual_hint(q, 2, "一")
    h3 = GameBridge._extract_visual_hint(q, 3, "失")
    assert "橘黄色的小衣架" in h2
    assert "一" not in h2 and "谐音" not in h2
    assert "大柿子" in h3
    assert "失" not in h3 and "谐音" not in h3


def test_host_brain_expressions_mood_truncation_and_tts_emotion(tmp_path):
    cfg = _FakeConfig({"llm": {"host": {"enabled": False}}})
    handle = _FakeHandle()
    handle.config = cfg
    brain = HostBrain(handle)

    # 默认加载项目内 heart/expressions.json
    assert len(brain.expressions) >= 10
    samples = brain._sample_expressions("ask_visual", k=3)
    assert len(samples) == 3

    # 标点感知截断不拦腰砍句
    truncated = HostBrain._smart_truncate("哎呀这图太抽象了，你们快帮我看看这是啥字啊别装死。", 20)
    assert len(truncated) <= 20
    assert truncated.endswith(("了", "，", "。", "！", "？"))

    # 情绪状态机与 CosyVoice 情绪指令联动
    brain.record_outcome("wrong", blame_users=["张三"])
    assert brain.mood == "skeptical"
    assert "张三" in brain.grudge_users
    inst_wrong, rate_wrong = brain.resolve_tts_style({"event": "wrong"})
    assert inst_wrong == "你说话的情感是angry。"
    assert rate_wrong > 1.0

    brain.record_outcome("correct", pct=100, guess="万无一失啊")
    inst_ok, _ = brain.resolve_tts_style({"event": "correct"})
    assert inst_ok == "你说话的情感是happy。"

    inst_up, _ = brain.resolve_tts_style({"event": "guess", "pct": 80, "prev_pct": 40})
    assert inst_up == "你说话的情感是surprised。"


def test_question_clear_speaks_short_transition_without_pause_or_3plus2(tmp_path, monkeypatch):
    bridge, handle = _make_bridge(tmp_path)
    monkeypatch.setattr(bridge_mod, "TTS_ENABLE", True)

    sent_msgs: list[dict] = []
    spoken_payloads: list[dict] = []

    class _FakeWS:
        async def send(self, raw):
            sent_msgs.append(json.loads(raw))

    handle.audio_synthesis_handle = lambda d: spoken_payloads.append(d)
    bridge.ws = _FakeWS()
    bridge.game_kind = "game1"
    bridge.questions[(1, 0)] = {
        "levelId": 1,
        "itemIndex": 0,
        "answer": "万无一失",
        "meaning": "万无一失这个成语形容做事情非常有把握，筹划得又很周密，就是个褒义词。",
        "origin": "万无一失出自西汉司马迁著的史记淮阴侯列传。",
    }

    asyncio.run(bridge._on_event({"event": "question_clear", "levelId": 1, "itemIndex": 0}))
    assert sent_msgs == []  # 不再为了 3+2 发送 pause / resume
    assert len(spoken_payloads) == 1
    assert "史记" not in spoken_payloads[0]["content"]
    assert "褒义词" not in spoken_payloads[0]["content"]
    assert spoken_payloads[0].get("tts_instruction") == "你说话的情感是happy。"
    assert not hasattr(bridge, "plans")
    assert not hasattr(bridge, "_talk_question")
    assert not hasattr(HostBrain, "say_3plus2")


def test_qwen_tts_cosyvoice_emotion_payload_and_400_retry(tmp_path, monkeypatch):
    from luna.audio.my_tts import MY_TTS

    tts = MY_TTS.__new__(MY_TTS)
    tts.common = Common()
    tts.audio_out_path = str(tmp_path)

    posted: list[dict] = []

    class _FakeResp:
        def __init__(self, status_code, content=b"", headers=None, text=""):
            self.status_code = status_code
            self.content = content
            self.headers = headers or {}
            self.text = text

    class _FakeSession:
        def __init__(self):
            self.trust_env = True
            self.proxies = {}

        def post(self, url, headers=None, json=None, timeout=None):
            posted.append({"url": url, "headers": headers, "json": json})
            if len(posted) == 1:
                return _FakeResp(400, text="unsupported instruction")
            return _FakeResp(200, content=b"ID3fake-mp3-bytes", headers={"content-type": "audio/mpeg"})

    import requests

    monkeypatch.setattr(requests, "Session", _FakeSession)

    out_path = tts.qwen_tts_api(
        {
            "content": "满格漂亮！",
            "instruction": "你说话的情感是happy。",
            "rate": 1.12,
            "qwen_tts": {
                "api_key": "sk-sp-test-key",
                "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com",
                "model": "cosyvoice-v3-flash",
                "voice": "longanhuan",
                "format": "mp3",
            },
        }
    )
    assert out_path is not None and out_path.endswith(".mp3")
    assert len(posted) == 2
    assert posted[0]["url"].endswith("/api/v1/services/audio/tts/SpeechSynthesizer")
    assert posted[0]["json"]["model"] == "cosyvoice-v3-flash"
    assert posted[0]["json"]["input"]["voice"] == "longanhuan"
    assert posted[0]["json"]["input"]["instruction"] == "你说话的情感是happy。"
    assert posted[0]["json"]["input"]["rate"] == 1.12
    # 400 后自动去掉 instruction 重试成功
    assert "instruction" not in posted[1]["json"]["input"]
