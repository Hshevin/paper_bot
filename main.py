"""AstrBot adapter: scheduling, administrator commands, model and QQ delivery."""

import asyncio
import inspect

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import Provider
from astrbot.api.star import Context, Star, StarTools, register

from .paperbot.service import PaperService


@register("astrbot_plugin_paper_bot", "Hshevin", "按研究方向筛选并推送 arXiv 论文", "1.0.0")
class PaperBotPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.service = None
        self._task = None
        self._operations = set()
        self._closing = False
        self._startup_error = "插件尚未初始化。"

    async def initialize(self):
        try:
            self.service = PaperService(
                StarTools.get_data_dir("astrbot_plugin_paper_bot"),
                self.config, self._generate, self._send,
                reader_generate=self._read_generate,
                save_config=self.config.save_config,
            )
        except Exception as exc:
            # Provider/HTTP exceptions can contain API keys and request payloads.
            logger.error(f"Paper Bot 初始化失败 ({type(exc).__name__})")
            self._startup_error = "插件初始化失败，请检查插件配置与数据目录权限；现有数据未重置。"
            return
        self._task = asyncio.create_task(self._scheduler(), name="paper-bot-scheduler")

    async def _scheduler(self):
        while True:
            try:
                await self.service.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"Paper Bot 定时任务失败 ({type(exc).__name__})")
            await asyncio.sleep(60)

    async def terminate(self):
        self._closing = True
        pending = list(self._operations)
        if self._task is not None:
            pending.append(self._task)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._task = None
        if self.service is not None:
            await self.service.close()
            self.service = None

    async def _generate(self, system: str, user: str) -> str:
        provider_id = str(self.config.get("provider_id", "")).strip()
        return await self._generate_for(provider_id, system, user)

    async def _read_generate(self, system: str, user: str) -> str:
        provider_id = str(self.config.get("reading_provider_id", "")).strip()
        provider_id = provider_id or str(self.config.get("provider_id", "")).strip()
        return await self._generate_for(provider_id, system, user)

    async def _generate_for(self, provider_id: str, system: str, user: str) -> str:
        if not provider_id:
            raise ValueError("请在插件配置中选择用于论文分析的聊天模型。")
        provider = self.context.get_provider_by_id(provider_id)
        if not isinstance(provider, Provider):
            raise ValueError("选定的聊天模型不存在或类型不匹配，请重新选择模型。")
        try:
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id, system_prompt=system, prompt=user,
                ),
                timeout=120,
            )
        except Exception as exc:
            raise RuntimeError(f"论文模型调用失败 ({type(exc).__name__})") from None
        text = response.completion_text
        if not isinstance(text, str) or not text.strip():
            raise ValueError("论文模型返回了空内容。")
        return text

    async def _send(self, umo: str, text: str) -> bool:
        parts = umo.split(":", 2)
        if len(parts) != 3 or not all(parts) or parts[1] not in ("GroupMessage", "FriendMessage"):
            raise ValueError("推送目标应是 QQ 群聊或私聊的完整会话标识，请使用 /paper_target 获取。")
        platform = self.context.get_platform_inst(parts[0])
        if platform is None:
            return False
        if platform.meta().name != "aiocqhttp":
            raise ValueError("QQ 定时推送需要 OneBot v11 适配器，例如 NapCat。")
        try:
            return await self.context.send_message(umo, MessageChain().message(text))
        except Exception as exc:
            raise RuntimeError(f"QQ 推送失败 ({type(exc).__name__})") from None

    async def _call(self, event, method, *args, **kwargs):
        # Keep the in-handler check as well as framework filters: these operations
        # can spend API credits or change which conversations receive papers.
        if not event.is_admin():
            return event.plain_result("此命令仅限 AstrBot 管理员。")
        return event.plain_result(await self._invoke(method, *args, **kwargs))

    async def _invoke(self, method, *args, **kwargs) -> str:
        if self._closing:
            return "插件正在停止，请等待重载完成后重试。"
        if self.service is None:
            return self._startup_error
        try:
            result = getattr(self.service, method)(*args, **kwargs)
            if inspect.isawaitable(result):
                operation = asyncio.ensure_future(result)
                self._operations.add(operation)
                try:
                    result = await operation
                finally:
                    self._operations.discard(operation)
            return str(result)
        except Exception as exc:
            logger.warning(f"Paper Bot 管理命令失败 ({type(exc).__name__})")
            return "操作失败，请检查插件配置、模型连接及数据目录；现有进度已保留。"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("paper_run")
    async def paper_run(self, event: AstrMessageEvent):
        """立即抓取并分析论文，推送至已配置目标。"""
        if event.is_admin() and self.service is not None:
            yield event.plain_result("开始抓取和筛选论文，完成后推送至已配置目标。")
        yield await self._call(event, "run", force=True)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("paper_bind")
    async def paper_bind(self, event: AstrMessageEvent, topic_ids: str = "", interval_minutes: int = 60):
        """当前会话订阅主题：/paper_bind graph,reasoning 120（每 120 分钟），不填间隔为 60 分钟。"""
        if not event.is_admin():
            yield event.plain_result("此命令仅限 AstrBot 管理员。")
            return
        if not topic_ids.strip():
            yield event.plain_result(
                "用法：/paper_bind 主题ID1,主题ID2 [间隔分钟]\n"
                "例如 /paper_bind graph,reasoning 120，仅为当前群聊或私聊订阅这两个方向，每 120 分钟推送最相关论文。\n"
                "先用 /paper_topics 查看主题 ID；不填写主题不会创建订阅。"
            )
            return
        if isinstance(interval_minutes, bool) or not isinstance(interval_minutes, int) or not 15 <= interval_minutes <= 1440:
            yield event.plain_result("订阅间隔应为 15–1440 分钟的整数。")
            return
        yield await self._call(event, "bind", event.unified_msg_origin, topic_ids, interval_minutes)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("paper_unbind")
    async def paper_unbind(self, event: AstrMessageEvent):
        """移除当前会话的论文推送订阅。"""
        yield await self._call(event, "unbind", event.unified_msg_origin)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("paper_subscriptions")
    async def paper_subscriptions(self, event: AstrMessageEvent):
        """查看当前群聊或私聊订阅的研究方向与推送间隔。"""
        yield await self._call(event, "subscriptions", event.unified_msg_origin)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("paper_retry")
    async def paper_retry(self, event: AstrMessageEvent):
        """重试待处理的论文分析与失败推送。"""
        if event.is_admin() and self.service is not None:
            yield event.plain_result("开始重试待处理的论文分析与推送。")
        yield await self._call(event, "retry")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("paper_read")
    async def paper_read(self, event: AstrMessageEvent, paper_id: str):
        """手动全文精读：/paper_read 2610.00001，使用精读模型（未设置则复用摘要模型）。"""
        if not event.is_admin():
            yield event.plain_result("此命令仅限 AstrBot 管理员。")
            return
        if self.service is not None:
            yield event.plain_result("开始下载论文全文并调用配置的精读模型，完成后在当前会话返回报告。")
        report = await self._invoke("read", paper_id)
        for offset in range(0, len(report), 1500):
            yield event.plain_result(report[offset:offset + 1500])

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("paper_fetch")
    async def paper_fetch(self, event: AstrMessageEvent, topic_id: str, start_date: str, end_date: str):
        """日期抓取：/paper_fetch 主题ID YYYY-MM-DD YYYY-MM-DD（UTC，包含起止整天），随后用 /paper_run 分析推送。"""
        if event.is_admin() and self.service is not None:
            yield event.plain_result("开始按 UTC 日期范围抓取候选论文；完成后可用 /paper_run 分析并推送。")
        yield await self._call(event, "fetch_range", topic_id, start_date, end_date)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("paper_status")
    async def paper_status(self, event: AstrMessageEvent):
        """查看论文抓取、待处理队列和推送状态。"""
        yield await self._call(event, "status")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("paper_topics")
    async def paper_topics(self, event: AstrMessageEvent):
        """查看已配置的研究主题。"""
        yield await self._call(event, "topics")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("paper_target")
    async def paper_target(self, event: AstrMessageEvent):
        """显示当前会话标识，可复制到插件独立订阅配置。"""
        if not event.is_admin():
            yield event.plain_result("此命令仅限 AstrBot 管理员。")
            return
        yield event.plain_result(
            f"当前会话：{event.unified_msg_origin}\n"
            "先用 /paper_topics 查看主题，再用 /paper_bind graph,reasoning 120 订阅指定方向；每个群聊和私聊独立设置。"
        )
