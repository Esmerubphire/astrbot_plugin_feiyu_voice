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
import time
import types
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 旧实现里的去重窗口。新实现把状态挂在事件对象上，已经没有窗口了，
# 用例仍然按「窗口外」构造场景，这样同一份测试拿到旧代码上跑会真的红。
WINDOW = 20.0


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


@contextmanager
def ticking(clock):
    """让插件读到测试时钟。

    旧实现靠 time.monotonic() 判断窗口，所以要真的「把时间推过去」才能
    复现那条清理逻辑；新实现不看时钟，包这一层对结果没有影响。
    只在调用 enqueue 的这几行里生效，不去动 asyncio 自己的计时。
    """
    real = time.monotonic
    time.monotonic = clock.monotonic
    try:
        yield
    finally:
        time.monotonic = real


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
    """一条入站消息对应的事件对象 —— 也就是一轮对话。

    真实宿主里同一轮的所有工具调用拿到的是同一个实例（agent runner 在
    reset() 绑定一次 run_context），所以这里用对象标识区分轮次就够。
    """

    def __init__(self, message_id="m1", origin="aiocqhttp:FriendMessage:1"):
        self.message_obj = Message(message_id)
        self.unified_msg_origin = origin


def build_plugin():
    instance = plugin.FeiyuVoice(None, {"api_key": "test-key", "voice": "feiyu"})
    instance.client = FakeClient()
    instance.watch = lambda job_id, origin: None
    instance.persist = lambda: None
    instance.jobs = {}
    return instance


def seen_of(event):
    """这一轮的去重表放在事件对象上；没写过就是空的。"""
    return getattr(event, "_feiyu_seen", {})


# --- 用例 -----------------------------------------------------------------
async def _placeholder_blocks_concurrent_call():
    voice = build_plugin()
    voice.client.gate = asyncio.Event()
    event = Event("m1")

    first = asyncio.ensure_future(voice.enqueue(event, "speech", {"text": "睡吧"}))
    await asyncio.sleep(0)  # 让第一单停在 await submit 上
    second = await voice.enqueue(event, "speech", {"text": "睡吧"})
    voice.client.gate.set()
    await first

    assert "正在提交" in second, second
    assert len(voice.client.calls) == 1, voice.client.calls


async def _placeholder_survives_beyond_window():
    """提交中超过去重窗口，同一轮再调一次仍应被占位挡住。"""
    clock = Clock()
    voice = build_plugin()
    voice.client.gate = asyncio.Event()
    event = Event("m1")

    with ticking(clock):
        first = asyncio.ensure_future(voice.enqueue(event, "speech", {"text": "睡吧"}))
        await asyncio.sleep(0)  # 第一单停在 await submit 上
        clock.advance(WINDOW + 5)

        # 不直接 await：旧实现里这一单会一路走到 submit 并卡在闸门上，
        # 这里只看它有没有真的又下发了一次。
        second = asyncio.ensure_future(voice.enqueue(event, "speech", {"text": "睡吧"}))
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
    voice = build_plugin()
    event = Event("m1")

    with ticking(clock):
        await voice.enqueue(event, "speech", {"text": "睡吧"})
        assert len(voice.client.calls) == 1

        clock.advance(WINDOW + 5)
        second = await voice.enqueue(event, "speech", {"text": "睡吧"})

    assert "正在生成" in second, second
    assert len(voice.client.calls) == 1, voice.client.calls


async def _delivered_in_same_turn_still_blocks():
    clock = Clock()
    voice = build_plugin()
    event = Event("m1")

    with ticking(clock):
        await voice.enqueue(event, "speech", {"text": "睡吧"})
        voice.jobs["job1"] = {"origin": "o", "pending": False, "delivered": True}

        clock.advance(WINDOW + 5)
        second = await voice.enqueue(event, "speech", {"text": "睡吧"})

    assert "已经发过" in second, second
    assert len(voice.client.calls) == 1, voice.client.calls


async def _different_turn_not_blocked():
    voice = build_plugin()

    await voice.enqueue(Event("m1"), "speech", {"text": "睡吧"})
    await voice.enqueue(Event("m2"), "speech", {"text": "睡吧"})

    assert len(voice.client.calls) == 2, voice.client.calls


async def _delivered_turn_survives_other_session():
    """A 轮已投递但 agent 还没结束，B 轮（另一个会话）在窗口外提交。

    插件级缓存按时间清理时，B 这次提交会顺手把 A 的记录当陈旧数据删掉，
    A 再调一次就重新下单。pending=False 只说明音频结束了，说明不了轮次结束。
    """
    clock = Clock()
    voice = build_plugin()
    turn_a = Event("m1", origin="aiocqhttp:FriendMessage:1")
    turn_b = Event("m9", origin="aiocqhttp:FriendMessage:2")

    with ticking(clock):
        await voice.enqueue(turn_a, "speech", {"text": "睡吧"})
        voice.jobs["job1"] = {
            "origin": turn_a.unified_msg_origin,
            "pending": False,
            "delivered": True,
        }

        clock.advance(WINDOW + 5)
        await voice.enqueue(turn_b, "speech", {"text": "睡吧"})
        assert len(voice.client.calls) == 2, voice.client.calls

        clock.advance(WINDOW + 5)
        again = await voice.enqueue(turn_a, "speech", {"text": "睡吧"})

    assert "已经发过" in again, again
    assert len(voice.client.calls) == 2, "A 轮被当成新请求重新下单了"


async def _delivered_turn_survives_other_message():
    """同上，但 B 是同会话里的另一条消息 —— 同会话的轮次交错也要挡住。"""
    clock = Clock()
    voice = build_plugin()
    turn_a = Event("m1")
    turn_b = Event("m2")  # 同一个会话，另一条用户消息

    with ticking(clock):
        await voice.enqueue(turn_a, "speech", {"text": "睡吧"})
        voice.jobs["job1"] = {
            "origin": turn_a.unified_msg_origin,
            "pending": False,
            "delivered": True,
        }

        clock.advance(WINDOW + 5)
        await voice.enqueue(turn_b, "speech", {"text": "睡吧"})
        assert len(voice.client.calls) == 2, voice.client.calls

        clock.advance(WINDOW + 5)
        again = await voice.enqueue(turn_a, "speech", {"text": "睡吧"})

    assert "已经发过" in again, again
    assert len(voice.client.calls) == 2, "同会话的 A 轮被重新下单了"


async def _failed_job_allows_retry():
    voice = build_plugin()
    event = Event("m1")

    await voice.enqueue(event, "speech", {"text": "睡吧"})
    voice.jobs["job1"] = {"origin": "o", "pending": False, "delivered": False}

    second = await voice.enqueue(event, "speech", {"text": "睡吧"})

    assert "已提交肥鱼任务" in second, second
    assert len(voice.client.calls) == 2, voice.client.calls


async def _submit_error_releases_placeholder():
    voice = build_plugin()
    event = Event("m1")
    voice.client.fail_next = True

    try:
        await voice.enqueue(event, "speech", {"text": "睡吧"})
    except RuntimeError:
        pass
    else:
        raise AssertionError("submit 失败应向外抛")

    assert seen_of(event) == {}, seen_of(event)

    second = await voice.enqueue(event, "speech", {"text": "睡吧"})
    assert "已提交肥鱼任务" in second, second


async def _different_turn_keeps_own_cache():
    """两轮各自记各自的，谁也不覆盖谁。"""
    voice = build_plugin()
    turn_a = Event("m1")
    turn_b = Event("m2")

    await voice.enqueue(turn_a, "speech", {"text": "睡吧"})
    await voice.enqueue(turn_b, "speech", {"text": "睡吧"})

    assert len(seen_of(turn_a)) == 1, seen_of(turn_a)
    assert len(seen_of(turn_b)) == 1, seen_of(turn_b)


CASES = [
    ("同一轮并发：占位挡住第二次调用", _placeholder_blocks_concurrent_call),
    ("提交中超过窗口：仍被占位挡住", _placeholder_survives_beyond_window),
    ("生成中超过窗口：不重新下单", _pending_survives_beyond_window),
    ("同轮已投递：超过窗口仍判重复", _delivered_in_same_turn_still_blocks),
    ("跨轮：相同内容互不拦截", _different_turn_not_blocked),
    ("A→B→A 跨会话：B 不清 A 的已投递记录", _delivered_turn_survives_other_session),
    ("A→B→A 同会话不同消息：同上", _delivered_turn_survives_other_message),
    ("失败单：放行重新提交", _failed_job_allows_retry),
    ("提交异常：回收占位并可立即重试", _submit_error_releases_placeholder),
    ("跨轮：各自保留自己的去重表", _different_turn_keeps_own_cache),
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
