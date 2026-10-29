import asyncio
import json
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.star.filter.command import GreedyStr

from .client import VoiceClient


@register("feiyu_voice", "kaiyuey03", "宇智波白饭鱼说话、换声与按谱唱歌统一接口", "0.1.0")
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
            chain = MessageChain(
                [Comp.Plain("宇智波白饭鱼音频已生成。"), Comp.Record.fromFileSystem(str(audio))]
            )
            sent = await self.context.send_message(origin, chain)
            if not sent:
                raise RuntimeError(
                    "The platform could not deliver the audio; retrieve it using the job command"
                )
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
                    MessageChain([Comp.Plain(f"宇智波白饭鱼任务 {job_id} 未完成：{exc}")]),
                )
            except Exception:
                logger.exception("Feiyu failure notification could not be delivered")

    async def enqueue(self, event, kind, payload):
        if not self.config.get("api_key"):
            raise ValueError("请先在插件配置中填写声音服务 API Key。")
        payload["voice"] = self.config.get("voice", "feiyu")
        row = await self.client.submit(kind, payload)
        origin = event.unified_msg_origin
        self.jobs[row["id"]] = dict(origin=origin, pending=True)
        self.persist()
        self.watch(row["id"], origin)
        return f"已提交宇智波白饭鱼任务 {row['id']}，生成后会在本会话发送音频。"

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

    @filter.command("宇智波白饭鱼状态", alias={"feiyu_status"})
    async def capabilities(self, event: AstrMessageEvent):
        try:
            data = (await self.client.request("GET", "/v1/capabilities")).json()
            tts = any(row["available"] for row in data["tts"])
            rvc = any(row["available"] for row in data["rvc"])
            yield event.plain_result(
                f"宇智波白饭鱼服务：说话 {'可用' if tts else '未安装'}；换声 {'可用' if rvc else '未安装'}；按谱唱歌 {'可用' if data['singing']['available'] else '未安装'}。"
            )
        except Exception as exc:
            yield event.plain_result(str(exc))

    @filter.command("宇智波白饭鱼说话", alias={"feiyu_tts"})
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

    @filter.command("宇智波白饭鱼换声", alias={"feiyu_rvc"})
    async def conversion(self, event: AstrMessageEvent):
        try:
            audio_id = await self.attached_asset(event, {".wav", ".flac"})
            yield event.plain_result(
                await self.enqueue(event, "conversion", dict(audio_id=audio_id))
            )
        except Exception as exc:
            yield event.plain_result(str(exc))

    @filter.command("宇智波白饭鱼唱歌", alias={"feiyu_sing"})
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

    @filter.command("宇智波白饭鱼任务", alias={"feiyu_job"})
    async def job(self, event: AstrMessageEvent, job_id: str):
        try:
            if self.jobs.get(job_id, {}).get("origin") != event.unified_msg_origin:
                raise ValueError("本会话没有这个任务。")
            result = await self.client.status(job_id)
            if result["status"] == "success":
                audio = await self.client.download(job_id, self.directory)
                yield event.chain_result([Comp.Record.fromFileSystem(str(audio))])
            else:
                yield event.plain_result("宇智波白饭鱼任务状态：" + result["status"])
        except Exception as exc:
            yield event.plain_result(str(exc))

    @filter.llm_tool(name="feiyu_speak")
    async def speak_tool(
        self, event: AstrMessageEvent, text: str, style: str = "cute"
    ) -> str:
        """用宇智波白饭鱼声音朗读或说话。需要演唱谱子时使用 feiyu_sing；已有音频换声使用 feiyu_convert。

        Args:
            text(string): 要说的文字，最多2000字。
            style(string): cute 或 gentle。
        """
        try:
            return await self.enqueue(event, "speech", dict(text=text, style=style))
        except Exception as exc:
            return str(exc)

    @filter.llm_tool(name="feiyu_convert")
    async def convert_tool(
        self, event: AstrMessageEvent, source: str = "attachment"
    ) -> str:
        """把当前消息所附的清唱或语音转换成宇智波白饭鱼音色。需要已有音频；不能直接把谱子交给RVC。

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
