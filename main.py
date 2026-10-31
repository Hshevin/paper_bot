"""AstrBot adapter: scheduling, administrator commands, model and QQ delivery."""

import asyncio
import inspect
import json
import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import Provider
from astrbot.api.star import Context, Star, StarTools, register

from .paperbot.service import PaperService
from .paperbot.errors import ConfigError, diagnose, external_error, public_error


PLUGIN_VERSION = "1.0.3"

COMMAND_CONTEXT = {
    "run": "立即抓取与分析", "bind": "绑定当前会话订阅", "unbind": "取消当前会话订阅",
    "subscriptions": "查看当前会话订阅", "retry": "重试论文处理", "read": "手动全文精读",
    "fetch_range": "指定日期抓取", "status": "查看运行状态", "topics": "查看研究主题",
}


@register("astrbot_plugin_paper_bot", "Hshevin", "按研究方向筛选并推送 arXiv 论文", PLUGIN_VERSION)
class PaperBotPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self._log = getattr(self, "logger", logger)
        self.config = config
        self.service = None
        self._task = None
        self._operations = set()
        self._closing = False
        self._startup_error = "插件尚未初始化。"
        self._scheduler_error = ""

    def _report_error(self, report: dict):
        record = {"plugin": "astrbot_plugin_paper_bot", "event": "operation_failed", **report}
        log = self._log.warning if report.get("scope") == "paper" else self._log.error
        log(f"[Paper Bot] {json.dumps(record, ensure_ascii=False)}")

    @staticmethod
    def _safe_package(module_name) -> str:
        if not isinstance(module_name, str):
            return ""
        match = re.match(r"^(data\.plugins\.[A-Za-z0-9_-]{1,100})(?:\.|$)", module_name)
        return match.group(1) if match else ""

    def _runtime_identity(self) -> str:
        core_version = getattr(self.service, "runtime_version", None)
        if not isinstance(core_version, str) or not re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}", core_version):
            core_version = "未知/旧版"
        package = self._safe_package(self.__class__.__module__)
        lines = [f"[Paper Bot] 入口运行版本：{PLUGIN_VERSION}；核心运行版本：{core_version}",
                 f"加载包：{package or '非标准加载路径'}"]
        if core_version != PLUGIN_VERSION:
            lines.append("提示：入口与核心运行版本不一致，请检查实际加载的插件目录与更新来源。")
        # Read only the official registry's name/module path. Never expose reprs,
        # absolute paths or arbitrary metadata while diagnosing duplicate installs.
        packages = {package} if package else set()
        get_stars = getattr(self.context, "get_all_stars", None)
        if callable(get_stars):
            try:
                stars = get_stars()
                if isinstance(stars, (list, tuple)):
                    for star in stars:
                        if getattr(star, "name", None) == "astrbot_plugin_paper_bot":
                            other = self._safe_package(getattr(star, "module_path", None))
                            if other:
                                packages.add(other)
            except Exception:
                # Registry introspection must not hide the original status/error.
                pass
        if len(packages) > 1:
            lines.append(f"提示：同名插件存在 {len(packages)} 个安装目录：{'、'.join(sorted(packages))}。请核对保留的实例，避免旧副本响应命令。")
        return "\n".join(lines)

    async def initialize(self):
        try:
            self.service = PaperService(
                StarTools.get_data_dir("astrbot_plugin_paper_bot"),
                self.config, self._generate, self._send,
                reader_generate=self._read_generate,
                save_config=self.config.save_config,
                report_error=self._report_error,
            )
        except Exception as exc:
            self._log.info(self._runtime_identity())
            report = diagnose(exc, "初始化")
            self._report_error(report)
            self._startup_error = public_error(report)
            return
        self._log.info(self._runtime_identity())
        self._task = asyncio.create_task(self._scheduler(), name="paper-bot-scheduler")

    async def _scheduler(self):
        while True:
            try:
                await self.service.run()
                self._scheduler_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report = diagnose(exc, "调度")
                self._report_error(report)
                self._scheduler_error = public_error(report)
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
        field = "reading_provider_id" if provider_id else "provider_id"
        provider_id = provider_id or str(self.config.get("provider_id", "")).strip()
        return await self._generate_for(provider_id, system, user, field=field)

    async def _generate_for(self, provider_id: str, system: str, user: str, *, field: str = "provider_id") -> str:
        if not provider_id:
            raise ConfigError("请在插件配置中选择用于论文分析的聊天模型。", field=field)
        provider = self.context.get_provider_by_id(provider_id)
        if not isinstance(provider, Provider):
            raise ConfigError("选定的聊天模型不存在或类型不匹配，请重新选择模型。", field=field)
        try:
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id, system_prompt=system, prompt=user,
                ),
                timeout=120,
            )
        except Exception as exc:
            raise external_error(exc, "模型") from exc
        # A successful but empty response is invalid content, not a provider
        # outage. Screening/reading owns its validation and retry policy.
        text = getattr(response, "completion_text", None)
        return text if isinstance(text, str) else ""

    async def _send(self, umo: str, text: str) -> bool:
        parts = umo.split(":", 2)
        if len(parts) != 3 or not all(parts) or parts[1] not in ("GroupMessage", "FriendMessage"):
            raise ConfigError("推送目标应是 QQ 群聊或私聊的完整会话标识，请使用 /paper_target 获取。", field="subscriptions.target")
        platform = self.context.get_platform_inst(parts[0])
        if platform is None:
            return False
        if platform.meta().name != "aiocqhttp":
            raise ConfigError("QQ 定时推送需要 OneBot v11 适配器，例如 NapCat。", field="subscriptions.target")
        try:
            return await self.context.send_message(umo, MessageChain().message(text))
        except Exception as exc:
            raise external_error(exc, "QQ") from exc

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
            text = str(result)
            if method == "status" and self._scheduler_error:
                text += "\n" + self._scheduler_error
            return text
        except Exception as exc:
            report = diagnose(exc, "管理员命令", context=COMMAND_CONTEXT.get(method, "插件操作"))
            self._report_error(report)
            return public_error(report)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("paper_run")
    async def paper_run(self, event: AstrMessageEvent):
        """立即抓取并分析论文，推送至已配置目标。"""
        if event.is_admin() and self.service is not None:
            yield event.plain_result("[Paper Bot] 开始抓取和筛选论文，完成后推送至已配置目标。")
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
            yield event.plain_result("[Paper Bot] 开始重试待处理的论文分析与推送。")
        yield await self._call(event, "retry")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("paper_read")
    async def paper_read(self, event: AstrMessageEvent, paper_id: str):
        """手动全文精读：/paper_read 2610.00001，使用精读模型（未设置则复用摘要模型）。"""
        if not event.is_admin():
            yield event.plain_result("此命令仅限 AstrBot 管理员。")
            return
        if self.service is not None:
            yield event.plain_result("[Paper Bot] 开始下载论文全文并调用配置的精读模型，完成后在当前会话返回报告。")
        report = await self._invoke("read", paper_id)
        for offset in range(0, len(report), 1500):
            yield event.plain_result(report[offset:offset + 1500])

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("paper_fetch")
    async def paper_fetch(self, event: AstrMessageEvent, topic_id: str, start_date: str, end_date: str):
        """日期抓取：/paper_fetch 主题ID YYYY-MM-DD YYYY-MM-DD（UTC，包含起止整天），随后用 /paper_run 分析推送。"""
        if event.is_admin() and self.service is not None:
            yield event.plain_result("[Paper Bot] 开始按 UTC 日期范围抓取候选论文；完成后可用 /paper_run 分析并推送。")
        yield await self._call(event, "fetch_range", topic_id, start_date, end_date)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("paper_status")
    async def paper_status(self, event: AstrMessageEvent):
        """查看论文抓取、待处理队列和推送状态。"""
        if not event.is_admin():
            yield event.plain_result("此命令仅限 AstrBot 管理员。")
            return
        status = await self._invoke("status")
        yield event.plain_result(self._runtime_identity() + "\n" + status)

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
