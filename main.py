import asyncio
import json
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.star.filter.command import GreedyStr

from .client import VoiceClient


# 同一轮对话、同一类型、同一参数只提交一单。
# 大模型可能在一轮里重复调用同一个工具，重复提交会合成并发送多份音频。
#
# 去重状态挂在这一轮的事件对象上，不放在插件实例里。AstrBot 的
# tool_loop_agent_runner 在 reset() 时绑定一次 run_context，之后每个工具调用
# 都从 run_context.context.event 取事件，所以同一轮的多次调用拿到的是同一个
# 对象，不同轮次必然不同。轮次结束、事件被回收，记录跟着消失，不需要清理。
#
# 放在插件级字典里按 20 秒窗口清理是不行的：A 轮语音已经投递、但 agent 还在
# 跑，B 轮这时提交会触发全局清理，把 A 的记录当成陈旧数据删掉，A 再调一次就
# 重新下单。投递状态只能说明音频结束，说明不了轮次结束 —— 只有事件知道。


def _dedupe_cache(event):
    """这一轮的去重表。挂在事件对象上，跟着轮次一起生灭。"""
    cache = getattr(event, "_feiyu_seen", None)
    if not isinstance(cache, dict):
        cache = {}
        try:
            event._feiyu_seen = cache
        except Exception:
            # 挂不上属性就只能退化成不去重：宁可漏拦，也不能误删别的轮次的记录
            pass
    return cache


@register("feiyu_voice", "kaiyuey03", "肥鱼说话、换声与按谱唱歌统一接口", "0.1.0")
class FeiyuVoice(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.client = VoiceClient(
            config.get("base_url", "http://127.0.0.1:9880"), config.get("api_key", "")
        )
        self.directory = StarTools.get_data_dir("feiyu_voice")
        self.state_path = self.directory / "jobs.json"
        self.jobs = (
            json.loads(self.state_path.read_text(encoding="utf-8"))
            if self.state_path.exists()
            else {}
        )
        self.tasks = set()

    async def initialize(self):
        for job_id, row in self.jobs.items():
            if row.get("pending"):
                self.watch(job_id, row["origin"])

    def persist(self):
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(self.jobs, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.state_path)

    def watch(self, job_id, origin):
        task = asyncio.create_task(self.deliver(job_id, origin))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def deliver(self, job_id, origin):
        try:
            await self.client.wait(job_id)
            audio = await self.client.download(job_id, self.directory)
            record = Comp.Record.fromFileSystem(str(audio))
            caption = (self.jobs.get(job_id) or {}).get("caption")
            if caption and self.config.get("caption_enable", True):
                # 语音条单独发；文字等音频发送成功后再补，顺序因此是结构性的。
                chain = MessageChain([record])
            else:
                caption = None
                chain = MessageChain([Comp.Plain("肥鱼音频已生成。"), record])
            sent = await self.context.send_message(origin, chain)
            if not sent:
                raise RuntimeError(
                    "The platform could not deliver the audio; retrieve it using the job command"
                )
            if caption:
                try:
                    await self.context.send_message(
                        origin, MessageChain([Comp.Plain(caption)])
                    )
                except Exception:
                    # 回执失败不影响音频已交付的事实，不要把任务标记成失败。
                    logger.exception("Feiyu caption could not be delivered")
            self.jobs[job_id]["pending"] = False
            self.jobs[job_id]["delivered"] = True
            self.persist()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Feiyu voice delivery failed: {exc}")
            self.jobs[job_id]["pending"] = False
            self.persist()
            try:
                await self.context.send_message(
                    origin,
                    MessageChain([Comp.Plain(f"肥鱼任务 {job_id} 未完成：{exc}")]),
                )
            except Exception:
                logger.exception("Feiyu failure notification could not be delivered")

    def _live_job(self, job_id):
        """这一单是否还值得拿来挡重复调用：还在路上，或者已经成功投递。

        失败的任务既不在路上也没投递成功，重试应当放行，不能回一句「音频会照常发出」。
        """
        row = self.jobs.get(job_id)
        if not row:
            return False
        return bool(row.get("pending") or row.get("delivered"))

    async def enqueue(self, event, kind, payload, caption=None):
        if not self.config.get("api_key"):
            raise ValueError("请先在插件配置中填写声音服务 API Key。")
        payload["voice"] = self.config.get("voice", "feiyu")
        origin = event.unified_msg_origin
        seen = _dedupe_cache(event)
        signature = json.dumps([origin, kind, payload], sort_keys=True, ensure_ascii=False)
        previous = seen.get(signature)
        if previous:
            job_id = previous["job"]
            if job_id is None:
                # 第一单还在提交，第二次调用撞在占位上；此时还不知道成败，不做任何承诺
                return "同一段语音正在提交，不需要重复调用。"
            if self._live_job(job_id):
                row = self.jobs.get(job_id) or {}
                if row.get("delivered"):
                    return f"这条语音已经发过了（任务 {job_id}），不需要重复提交。"
                return (
                    f"同一段语音已经提交过（任务 {job_id}），正在生成，"
                    "不需要重复调用。"
                )
            # 上一单失败或没投递出去：不构成重复，清掉记录重新提交
            seen.pop(signature, None)
        # 先占位再 await：检查与占位之间没有 await，并发的第二次调用只能看到占位。
        reserved = {"job": None}
        seen[signature] = reserved
        try:
            row = await self.client.submit(kind, payload)
        except BaseException:
            # 只回收自己这个占位
            if seen.get(signature) is reserved:
                seen.pop(signature, None)
            raise
        seen[signature] = {"job": row["id"]}
        self.jobs[row["id"]] = dict(origin=origin, pending=True, caption=caption)
        self.persist()
        self.watch(row["id"], origin)
        return f"已提交肥鱼任务 {row['id']}，生成后会在本会话发送音频。"

    async def attached_asset(self, event, suffixes):
        components = list(event.get_messages())
        for component in list(components):
            if isinstance(component, Comp.Reply) and isinstance(
                getattr(component, "chain", None), list
            ):
                components.extend(component.chain)
        for component in components:
            if isinstance(component, Comp.Record) and ".wav" in suffixes:
                path = await component.convert_to_file_path()
                return await self.client.upload(path, "vocal.wav")
            if isinstance(component, Comp.File):
                name = component.name or ""
                if Path(name).suffix.lower() in suffixes:
                    path = await component.get_file()
                    return await self.client.upload(path, name)
        raise ValueError("请在本条消息中附上所需音频或 MIDI／ABC 文件。")

    @filter.command("肥鱼状态", alias={"feiyu_status"})
    async def capabilities(self, event: AstrMessageEvent):
        try:
            data = (await self.client.request("GET", "/v1/capabilities")).json()
            tts = any(row["available"] for row in data["tts"])
            rvc = any(row["available"] for row in data["rvc"])
            yield event.plain_result(
                f"肥鱼服务：说话 {'可用' if tts else '未安装'}；换声 {'可用' if rvc else '未安装'}；按谱唱歌 {'可用' if data['singing']['available'] else '未安装'}。"
            )
        except Exception as exc:
            yield event.plain_result(str(exc))

    @filter.command("肥鱼说话", alias={"feiyu_tts"})
    async def speech(self, event: AstrMessageEvent, text: GreedyStr):
        try:
            yield event.plain_result(
                await self.enqueue(
                    event,
                    "speech",
                    dict(text=str(text), style=self.config.get("style", "cute")),
                )
            )
        except Exception as exc:
            yield event.plain_result(str(exc))

    @filter.command("肥鱼换声", alias={"feiyu_rvc"})
    async def conversion(self, event: AstrMessageEvent):
        try:
            audio_id = await self.attached_asset(event, {".wav", ".flac"})
            yield event.plain_result(
                await self.enqueue(event, "conversion", dict(audio_id=audio_id))
            )
        except Exception as exc:
            yield event.plain_result(str(exc))

    @filter.command("肥鱼唱歌", alias={"feiyu_sing"})
    async def singing(self, event: AstrMessageEvent, lyrics: GreedyStr):
        try:
            score_id = await self.attached_asset(event, {".mid", ".midi", ".abc"})
            yield event.plain_result(
                await self.enqueue(
                    event,
                    "singing",
                    dict(score_id=score_id, lyrics=str(lyrics) or None),
                )
            )
        except Exception as exc:
            yield event.plain_result(str(exc))

    @filter.command("肥鱼任务", alias={"feiyu_job"})
    async def job(self, event: AstrMessageEvent, job_id: str):
        try:
            if self.jobs.get(job_id, {}).get("origin") != event.unified_msg_origin:
                raise ValueError("本会话没有这个任务。")
            result = await self.client.status(job_id)
            if result["status"] == "success":
                audio = await self.client.download(job_id, self.directory)
                yield event.chain_result([Comp.Record.fromFileSystem(str(audio))])
            else:
                yield event.plain_result("肥鱼任务状态：" + result["status"])
        except Exception as exc:
            yield event.plain_result(str(exc))

    @filter.llm_tool(name="feiyu_speak")
    async def speak_tool(
        self,
        event: AstrMessageEvent,
        text: str,
        style: str = "cute",
        caption: str = "",
    ) -> str:
        """用肥鱼声音朗读或说话。需要演唱谱子时使用 feiyu_sing；已有音频换声使用 feiyu_convert。一次调用就完成，不要重复调用。

        Args:
            text(string): 要念出来的原文，最多2000字。
            style(string): cute 或 gentle。
            caption(string): 语音条后面紧跟的一句话，20字以内、口语化；不想补话就留空。
        """
        try:
            return await self.enqueue(
                event,
                "speech",
                dict(text=text, style=style),
                caption=(caption or "").strip()[:60] or None,
            )
        except Exception as exc:
            return str(exc)

    @filter.llm_tool(name="feiyu_convert")
    async def convert_tool(
        self, event: AstrMessageEvent, source: str = "attachment"
    ) -> str:
        """把当前消息所附的清唱或语音转换成肥鱼音色。需要已有音频；不能直接把谱子交给RVC。

        Args:
            source(string): 固定为attachment，表示使用本消息附件，不能填写文件路径或URL。
        """
        try:
            if source != "attachment":
                return "只能使用本消息附件。"
            audio_id = await self.attached_asset(event, {".wav", ".flac"})
            return await self.enqueue(event, "conversion", dict(audio_id=audio_id))
        except Exception as exc:
            return str(exc)

    @filter.llm_tool(name="feiyu_sing")
    async def sing_tool(self, event: AstrMessageEvent, lyrics: str = "") -> str:
        """根据当前消息所附的MIDI或ABC主旋律和中文歌词唱歌，服务自动使用SVS后再调用RVC。一字多音用_占位。

        Args:
            lyrics(string): 按音符顺序排列的歌词；内嵌完整歌词的MIDI可留空。
        """
        try:
            score_id = await self.attached_asset(event, {".mid", ".midi", ".abc"})
            return await self.enqueue(
                event, "singing", dict(score_id=score_id, lyrics=lyrics or None)
            )
        except Exception as exc:
            return str(exc)

    async def terminate(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.client.close()
