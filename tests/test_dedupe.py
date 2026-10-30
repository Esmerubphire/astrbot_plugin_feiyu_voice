"""去重逻辑的回归测试。

不依赖 astrbot 运行时：这里给 astrbot 的几个模块打最小桩，再把插件
main.py 加载成一个临时包，直接驱动 FeiyuVoice.enqueue。

运行：
    python tests/test_dedupe.py
或：
    pytest tests/test_dedupe.py -q
"""

import asyncio
import importlib
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


# --- astrbot 最小桩 -------------------------------------------------------
def _identity(*args, **kwargs):
    def deco(fn):
        return fn

    return deco


def _install_stubs():
    filter_mod = types.SimpleNamespace(
        command=_identity,
        llm_tool=_identity,
        event_message_type=_identity,
        on_decorating_result=_identity,
        after_message_sent=_identity,
    )

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event_mod.filter = filter_mod

    class MessageChain:
        def __init__(self, components=None):
            self.components = components or []

    event_mod.MessageChain = MessageChain

    class Star:
        def __init__(self, context=None):
            pass

    class StarTools:
        @staticmethod
        def get_data_dir(name):
            path = Path(tempfile.gettempdir()) / "feiyu_test_data" / name
            path.mkdir(parents=True, exist_ok=True)
            return path

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = type("Context", (), {})
    star_mod.Star = Star
    star_mod.StarTools = StarTools
    star_mod.register = _identity

    class Comp:
        class Plain:
            def __init__(self, text):
                self.text = text

        class Record:
            @staticmethod
            def fromFileSystem(path):
                return path

    api_mod = types.ModuleType("astrbot.api")
    api_mod.AstrBotConfig = dict
    api_mod.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        exception=lambda *a, **k: None,
    )
    api_mod.message_components = Comp

    cmd_mod = types.ModuleType("astrbot.core.star.filter.command")
    cmd_mod.GreedyStr = str

    for name in ("astrbot.core", "astrbot.core.star", "astrbot.core.star.filter"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules.update(
        {
            "astrbot": types.ModuleType("astrbot"),
            "astrbot.api": api_mod,
            "astrbot.api.event": event_mod,
            "astrbot.api.star": star_mod,
            "astrbot.api.message_components": Comp,
            "astrbot.core.star.filter.command": cmd_mod,
        }
    )


def _load_plugin():
    """把插件复制成一个临时包，避免相对导入 import 失败。"""
    _install_stubs()
    root = Path(tempfile.mkdtemp(prefix="feiyu_pkg_"))
    pkg = root / "feiyu"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy(REPO / "main.py", pkg / "main.py")
    (pkg / "client.py").write_text(
        "class VoiceClient:\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        pass\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(root))
    return importlib.import_module("feiyu.main")


plugin = _load_plugin()


# --- 测试用的假对象 -------------------------------------------------------
class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeClient:
    """记录 submit 次数；gate 可以让一次 submit 挂起，用来模拟「提交中」。"""

    def __init__(self):
        self.calls = []
        self.gate = None
        self.fail_next = False

    async def submit(self, kind, payload):
        self.calls.append((kind, dict(payload)))
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("submit failed")
        return {"id": "job%d" % len(self.calls)}

    async def wait(self, job_id):
        return None

    async def download(self, job_id, directory):
        return Path("unused.wav")


class Message:
    def __init__(self, message_id):
        self.message_id = message_id


class Event:
    def __init__(self, message_id="m1"):
        self.message_obj = Message(message_id)
        self.unified_msg_origin = "aiocqhttp:FriendMessage:1"


def build_plugin(clock):
    plugin.time = clock
    instance = plugin.FeiyuVoice(None, {"api_key": "test-key", "voice": "feiyu"})
    instance.client = FakeClient()
    instance.watch = lambda job_id, origin: None
    instance.persist = lambda: None
    instance.jobs = {}
    instance._recent = {}
    return instance


def case_dedupe_window():
    return plugin._DEDUPE_WINDOW


# --- 用例 -----------------------------------------------------------------
async def _placeholder_blocks_concurrent_call():
    clock = Clock()
    voice = build_plugin(clock)
    voice.client.gate = asyncio.Event()
    event = Event("m1")

    first = asyncio.ensure_future(
        voice.enqueue(event, "speech", {"text": "睡吧"})
    )
    await asyncio.sleep(0)  # 让第一单停在 await submit 上
    second = await voice.enqueue(event, "speech", {"text": "睡吧"})
    voice.client.gate.set()
    await first

    assert "正在提交" in second, second
    assert len(voice.client.calls) == 1, voice.client.calls


async def _placeholder_survives_beyond_window():
    """提交中超过去重窗口，同一轮再调一次仍应被占位挡住。"""
    clock = Clock()
    voice = build_plugin(clock)
    voice.client.gate = asyncio.Event()
    event = Event("m1")

    first = asyncio.ensure_future(
        voice.enqueue(event, "speech", {"text": "睡吧"})
    )
    await asyncio.sleep(0)  # 第一单停在 await submit 上
    clock.advance(case_dedupe_window() + 5)

    # 不直接 await：修复前这一单会一路走到 submit 并卡在闸门上，
    # 这里只看它有没有真的又下发了一次。
    second = asyncio.ensure_future(
        voice.enqueue(event, "speech", {"text": "睡吧"})
    )
    await asyncio.sleep(0)
    submitted = len(voice.client.calls)

    voice.client.gate.set()
    await first
    reply = await second

    assert submitted == 1, "占位被清掉，第二次真的又提交了"
    assert "正在提交" in reply, reply


async def _pending_survives_beyond_window():
    """生成中超过去重窗口，同一轮再调一次不应重新下单。"""
    clock = Clock()
    voice = build_plugin(clock)
    event = Event("m1")

    await voice.enqueue(event, "speech", {"text": "睡吧"})
    assert len(voice.client.calls) == 1

    clock.advance(case_dedupe_window() + 5)
    second = await voice.enqueue(event, "speech", {"text": "睡吧"})

    assert "正在生成" in second, second
    assert len(voice.client.calls) == 1, voice.client.calls


async def _delivered_in_same_turn_still_blocks():
    clock = Clock()
    voice = build_plugin(clock)
    event = Event("m1")

    await voice.enqueue(event, "speech", {"text": "睡吧"})
    job_id = voice.client.calls and "job1"
    voice.jobs[job_id] = {"origin": "o", "pending": False, "delivered": True}

    clock.advance(case_dedupe_window() + 5)
    second = await voice.enqueue(event, "speech", {"text": "睡吧"})

    assert "已经发过" in second, second
    assert len(voice.client.calls) == 1, voice.client.calls


async def _different_turn_not_blocked():
    clock = Clock()
    voice = build_plugin(clock)

    await voice.enqueue(Event("m1"), "speech", {"text": "睡吧"})
    clock.advance(2.0)
    await voice.enqueue(Event("m2"), "speech", {"text": "睡吧"})

    assert len(voice.client.calls) == 2, voice.client.calls


async def _failed_job_allows_retry():
    clock = Clock()
    voice = build_plugin(clock)
    event = Event("m1")

    await voice.enqueue(event, "speech", {"text": "睡吧"})
    voice.jobs["job1"] = {"origin": "o", "pending": False, "delivered": False}

    second = await voice.enqueue(event, "speech", {"text": "睡吧"})

    assert "已提交肥鱼任务" in second, second
    assert len(voice.client.calls) == 2, voice.client.calls


async def _submit_error_releases_placeholder():
    clock = Clock()
    voice = build_plugin(clock)
    event = Event("m1")
    voice.client.fail_next = True

    try:
        await voice.enqueue(event, "speech", {"text": "睡吧"})
    except RuntimeError:
        pass
    else:
        raise AssertionError("submit 失败应向外抛")

    assert voice._recent == {}, voice._recent

    second = await voice.enqueue(event, "speech", {"text": "睡吧"})
    assert "已提交肥鱼任务" in second, second


async def _stale_entries_are_reclaimed():
    """别的轮次、已经不 pending 的记录，过期后必须回收，避免无限增长。"""
    clock = Clock()
    voice = build_plugin(clock)
    event = Event("m1")

    await voice.enqueue(event, "speech", {"text": "睡吧"})
    voice.jobs["job1"] = {"origin": "o", "pending": False, "delivered": True}
    assert len(voice._recent) == 1

    clock.advance(case_dedupe_window() + 5)
    await voice.enqueue(Event("m2"), "speech", {"text": "睡吧"})

    assert len(voice._recent) == 1, voice._recent  # 旧记录被清，只剩新的


CASES = [
    ("同一轮并发：占位挡住第二次调用", _placeholder_blocks_concurrent_call),
    ("提交中超过窗口：仍被占位挡住", _placeholder_survives_beyond_window),
    ("生成中超过窗口：不重新下单", _pending_survives_beyond_window),
    ("同轮已投递：超过窗口仍判重复", _delivered_in_same_turn_still_blocks),
    ("跨轮：相同内容互不拦截", _different_turn_not_blocked),
    ("失败单：放行重新提交", _failed_job_allows_retry),
    ("提交异常：回收占位并可立即重试", _submit_error_releases_placeholder),
    ("陈旧记录：别的轮次过期后回收", _stale_entries_are_reclaimed),
]


def test_all():
    for name, case in CASES:
        asyncio.run(case())
        print("  PASS %s" % name)


def main():
    print("feiyu dedupe regression (%d cases)" % len(CASES))
    test_all()
    print("all passed")


if __name__ == "__main__":
    main()
