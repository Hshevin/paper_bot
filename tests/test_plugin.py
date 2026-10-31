"""Adapter contract tests with an AstrBot API stub, not a live QQ integration test."""

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]


class FakeConfig(dict):
    def save_config(self):
        self.saved = dict(self)


class Provider:
    pass


class MessageChain:
    def message(self, text):
        self.text = text
        return self


class Star:
    def __init__(self, context):
        self.context = context


def decorator(*args, **kwargs):
    return lambda function: function


def load_plugin():
    modules = {name: ModuleType(name) for name in (
        "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star",
        "astrbot.api.provider", "paper_plugin_test", "paper_plugin_test.paperbot",
        "paper_plugin_test.paperbot.service",
    )}
    modules["astrbot.api"].AstrBotConfig = FakeConfig
    modules["astrbot.api"].logger = Mock()
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].MessageChain = MessageChain
    modules["astrbot.api.event"].filter = SimpleNamespace(
        command=decorator, permission_type=decorator,
        PermissionType=SimpleNamespace(ADMIN="admin"),
    )
    modules["astrbot.api.provider"].Provider = Provider
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].register = decorator
    modules["astrbot.api.star"].StarTools = SimpleNamespace(
        get_data_dir=lambda name: Path("/tmp/paper-plugin-contract-test")
    )
    modules["paper_plugin_test"].__path__ = [str(ROOT)]
    modules["paper_plugin_test.paperbot"].__path__ = [str(ROOT / "paperbot")]
    modules["paper_plugin_test.paperbot.service"].PaperService = Mock
    spec = importlib.util.spec_from_file_location("paper_plugin_test.main", ROOT / "main.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_plugin()
        self.context = SimpleNamespace(
            get_provider_by_id=Mock(return_value=Provider()),
            llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text="result")),
            send_message=AsyncMock(return_value=True),
            get_platform_inst=Mock(return_value=SimpleNamespace(
                meta=lambda: SimpleNamespace(name="aiocqhttp")
            )),
        )
        self.plugin = self.module.PaperBotPlugin(self.context, FakeConfig(provider_id="chosen"))
        self.service = SimpleNamespace(
            run=AsyncMock(return_value="done"), close=AsyncMock(),
            bind=Mock(return_value="bound"), unbind=Mock(return_value="unbound"),
            retry=AsyncMock(return_value="retry queued"), status=Mock(return_value="status"),
            topics=Mock(return_value="topics"),
            subscriptions=Mock(return_value="graph,reasoning / 120 分钟"),
            read=AsyncMock(return_value="报告" * 1600), fetch_range=AsyncMock(return_value="range queued"),
        )
        self.plugin.service = self.service

    def event(self, admin=True):
        return SimpleNamespace(is_admin=lambda: admin, plain_result=lambda text: text,
                               unified_msg_origin="qq-main:GroupMessage:12345")

    async def test_nonadmin_cannot_run_bind_retry_or_read_targets(self):
        for command in ("paper_run", "paper_bind", "paper_unbind", "paper_retry",
                        "paper_status", "paper_topics", "paper_target", "paper_subscriptions"):
            result = [x async for x in getattr(self.plugin, command)(self.event(False))]
            self.assertIn("管理员", result[0])
            self.assertNotIn("12345", result[0])
        self.service.run.assert_not_awaited()
        self.service.bind.assert_not_called()
        self.service.retry.assert_not_awaited()

    async def test_admin_binds_exact_session_and_forces_manual_run(self):
        self.assertEqual([x async for x in self.plugin.paper_bind(self.event(), "graph,reasoning", 120)], ["bound"])
        self.service.bind.assert_called_once_with("qq-main:GroupMessage:12345", "graph,reasoning", 120)
        self.assertEqual([x async for x in self.plugin.paper_run(self.event())][-1], "done")
        self.service.run.assert_awaited_once_with(force=True)

    async def test_bind_without_topics_shows_usage_and_never_subscribes_to_all(self):
        for topics in ("", "   "):
            result = [x async for x in self.plugin.paper_bind(self.event(), topics)]
            self.assertIn("/paper_bind graph,reasoning 120", result[0])
        self.service.bind.assert_not_called()

    async def test_bind_defaults_to_hourly_and_rejects_invalid_interval(self):
        result = [x async for x in self.plugin.paper_bind(self.event(), "graph")]
        self.assertEqual(result, ["bound"])
        self.service.bind.assert_called_once_with("qq-main:GroupMessage:12345", "graph", 60)
        self.service.bind.reset_mock()
        for interval in (0, 14, 1441):
            result = [x async for x in self.plugin.paper_bind(self.event(), "graph", interval)]
            self.assertIn("15", result[0])
        self.service.bind.assert_not_called()

    async def test_subscriptions_and_unbind_are_scoped_to_current_session(self):
        result = [x async for x in self.plugin.paper_subscriptions(self.event())]
        self.assertEqual(result, ["graph,reasoning / 120 分钟"])
        self.service.subscriptions.assert_called_once_with("qq-main:GroupMessage:12345")
        await anext(self.plugin.paper_unbind(self.event()))
        self.service.unbind.assert_called_once_with("qq-main:GroupMessage:12345")

    async def test_explicit_provider_and_plain_result_are_used_without_tools(self):
        self.assertEqual(await self.plugin._generate("system", "abstract"), "result")
        self.context.llm_generate.assert_awaited_once_with(
            chat_provider_id="chosen", system_prompt="system", prompt="abstract"
        )

    async def test_missing_or_nonchat_provider_never_falls_back(self):
        for config, provider in (({}, Provider()), ({"provider_id": "chosen"}, object())):
            self.plugin.config = config
            self.context.get_provider_by_id.return_value = provider
            with self.assertRaises(ValueError):
                await self.plugin._generate("system", "abstract")
        self.context.llm_generate.assert_not_awaited()

    async def test_delivery_preserves_false_result_and_uses_native_chain(self):
        self.context.send_message.return_value = False
        self.assertFalse(await self.plugin._send("qq-main:FriendMessage:42", "paper"))
        target, chain = self.context.send_message.await_args.args
        self.assertEqual(target, "qq-main:FriendMessage:42")
        self.assertIsInstance(chain, MessageChain)
        self.assertEqual(chain.text, "paper")

    async def test_official_qq_adapter_is_rejected_before_send(self):
        self.context.get_platform_inst.return_value.meta = lambda: SimpleNamespace(name="qq_official")
        with self.assertRaises(ValueError):
            await self.plugin._send("official:GroupMessage:42", "paper")
        self.context.send_message.assert_not_awaited()

    async def test_unconfigured_plugin_loads_and_termination_cleans_task(self):
        self.plugin = self.module.PaperBotPlugin(self.context, FakeConfig())
        with patch.object(self.module, "PaperService", return_value=self.service) as factory:
            await self.plugin.initialize()
        self.assertIs(factory.call_args.args[1], self.plugin.config)
        self.assertIs(factory.call_args.kwargs["save_config"].__self__, self.plugin.config)
        self.plugin.config["subscriptions"] = [{"target": "qq-main:GroupMessage:12345", "topic_ids": ["graph"]}]
        factory.call_args.kwargs["save_config"]()
        self.assertEqual(self.plugin.config.saved["subscriptions"], self.plugin.config["subscriptions"])
        await asyncio.sleep(0)
        task = self.plugin._task
        await self.plugin.terminate()
        self.assertTrue(task.done())
        self.service.close.assert_awaited_once()

    async def test_termination_cancels_and_awaits_manual_work_before_closing_store(self):
        started, cleaned = asyncio.Event(), asyncio.Event()

        async def slow_read(paper_id):
            started.set()
            try:
                await asyncio.Future()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        async def close():
            self.assertTrue(cleaned.is_set(), "store closed while manual work was still active")

        self.service.read.side_effect = slow_read
        self.service.close.side_effect = close

        async def read_command():
            return [x async for x in self.plugin.paper_read(self.event(), "2610.00001")]

        command = asyncio.create_task(read_command())
        await asyncio.wait_for(started.wait(), timeout=1)
        await self.plugin.terminate()
        with self.assertRaises(asyncio.CancelledError):
            await command
        self.assertTrue(cleaned.is_set())
        self.service.close.assert_awaited_once()
        result = [x async for x in self.plugin.paper_run(self.event())]
        self.assertIn("停止", result[-1])
        self.service.run.assert_not_awaited()

    async def test_provider_exception_does_not_echo_credentials(self):
        self.context.llm_generate.side_effect = RuntimeError("token=private-secret")
        with self.assertRaises(RuntimeError) as raised:
            await self.plugin._generate("system", "abstract")
        self.assertNotIn("private-secret", str(raised.exception))

    async def test_command_failure_does_not_echo_provider_exception(self):
        self.service.run.side_effect = RuntimeError("token=private-secret")
        result = [x async for x in self.plugin.paper_run(self.event())]
        self.assertIn("失败", result[-1])
        self.assertNotIn("private-secret", "".join(result))

    async def test_manual_read_chunks_report_and_uses_reading_model(self):
        self.plugin.config["reading_provider_id"] = "reading"
        await self.plugin._read_generate("system", "fulltext")
        self.assertEqual(self.context.llm_generate.await_args.kwargs["chat_provider_id"], "reading")
        result = [x async for x in self.plugin.paper_read(self.event(), "2610.00001")]
        self.assertIn("全文", result[0])
        self.assertEqual("".join(result[1:]), "报告" * 1600)
        self.assertTrue(all(len(chunk) <= 1500 for chunk in result[1:]))
        self.service.read.assert_awaited_once_with("2610.00001")

    async def test_manual_date_range_and_read_reject_nonadmins(self):
        result = [x async for x in self.plugin.paper_fetch(
            self.event(), "graph", "2026-10-01", "2026-10-31")]
        self.assertEqual(result[-1], "range queued")
        self.service.fetch_range.assert_awaited_once_with("graph", "2026-10-01", "2026-10-31")
        self.service.fetch_range.reset_mock()
        for result in (
            [x async for x in self.plugin.paper_read(self.event(False), "2610.00001")],
            [x async for x in self.plugin.paper_fetch(self.event(False), "graph", "2026-10-01", "2026-10-31")],
        ):
            self.assertIn("管理员", result[0])
        self.service.read.assert_not_awaited()
        self.service.fetch_range.assert_not_awaited()
