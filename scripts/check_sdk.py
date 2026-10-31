"""Offline integration check against an installed, real AstrBot SDK.

Example: .build/sdk-venv/bin/python scripts/check_sdk.py --sdk-path .build/AstrBot
Uses temporary host/config/database directories and rejects network connections.
This checks SDK compatibility, not a running QQ adapter or real model service.
"""

import argparse
import asyncio
from copy import deepcopy
import importlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def forbid_network(event, args):
    if event in ("socket.connect", "socket.getaddrinfo"):
        raise AssertionError("The SDK compatibility check must not use the network")


async def check(workspace: Path):
    from astrbot.api import AstrBotConfig
    from astrbot.api.event import AstrMessageEvent, MessageChain
    from astrbot.api.star import Context, Star
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import star_handlers_registry
    from astrbot.core.star.star_manager import PluginManager
    from packaging.specifiers import SpecifierSet
    import yaml

    metadata = PluginManager._load_plugin_metadata(str(ROOT))
    raw_metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text())
    sdk_version = version("AstrBot")
    assert sdk_version in SpecifierSet(raw_metadata["astrbot_version"])
    assert metadata.name == "astrbot_plugin_paper_bot"

    schema = json.loads((ROOT / "_conf_schema.json").read_text())
    config_file = workspace / "plugin_config.json"
    config = AstrBotConfig(str(config_file), schema=schema)
    assert config["enabled"] is False
    assert config["topics"] == [] and config["subscriptions"] == []
    assert "targets" not in config

    package = ModuleType("_paperbot_sdk_check")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
    plugin_module = importlib.import_module(f"{package.__name__}.main")
    assert issubclass(plugin_module.PaperBotPlugin, Star)
    assert star_map[plugin_module.__name__].star_cls_type is plugin_module.PaperBotPlugin
    assert star_map[plugin_module.__name__].name == metadata.name
    assert star_map[plugin_module.__name__].version == metadata.version

    def event(message: str, *, admin: bool = True):
        message_obj = AstrBotMessage()
        message_obj.type = MessageType.GROUP_MESSAGE
        message_obj.sender = MessageMember("10001", "SDK check")
        message_obj.message = []
        message_obj.self_id = "10002"
        message_obj.message_id = "sdk-check"
        result = AstrMessageEvent(
            message, message_obj, PlatformMetadata("aiocqhttp", "Local SDK check", "qq-check"), "12345"
        )
        result.role = "admin" if admin else "member"
        result.is_at_or_wake_command = True
        return result

    handlers = star_handlers_registry.get_handlers_by_module_name(plugin_module.__name__)
    commands = {}
    for handler in handlers:
        command_filters = [f for f in handler.event_filters if isinstance(f, CommandFilter)]
        permission_filters = [f for f in handler.event_filters if isinstance(f, PermissionTypeFilter)]
        assert len(command_filters) == 1 and len(permission_filters) == 1
        permission = permission_filters[0]
        assert permission.permission_type == PermissionType.ADMIN
        assert permission.filter(event("", admin=True), config)
        assert not permission.filter(event("", admin=False), config)
        commands[command_filters[0].command_name] = command_filters[0]
    expected = {
        "paper_run", "paper_bind", "paper_unbind", "paper_subscriptions", "paper_retry",
        "paper_read", "paper_fetch", "paper_status", "paper_topics", "paper_target",
    }
    assert set(commands) == expected

    for text, name, params in (
        ("paper_bind", "paper_bind", {"topic_ids": "", "interval_minutes": 60}),
        ("paper_bind graph,reasoning 120", "paper_bind", {"topic_ids": "graph,reasoning", "interval_minutes": 120}),
        ("paper_read 2610.00001", "paper_read", {"paper_id": "2610.00001"}),
        ("paper_fetch graph 2026-10-01 2026-10-31", "paper_fetch",
         {"topic_id": "graph", "start_date": "2026-10-01", "end_date": "2026-10-31"}),
    ):
        incoming = event(text)
        assert commands[name].filter(incoming, config)
        assert incoming.get_extra("parsed_params") == params

    # The actual Context is used; only managers that would connect to external
    # providers/platforms are empty. The actual PaperService and SQLite store run.
    context = Context(
        event_queue=asyncio.Queue(), config=config, db=None,
        provider_manager=SimpleNamespace(inst_map={}),
        platform_manager=SimpleNamespace(platform_insts=[]),
        conversation_manager=None, message_history_manager=None, persona_manager=None,
        astrbot_config_mgr=None, knowledge_base_manager=None,
    )
    plugin = plugin_module.PaperBotPlugin(context, config)
    await plugin.initialize()
    assert plugin.service is not None, plugin._startup_error
    try:
        await asyncio.sleep(0)
        assert plugin.service.config is config
        assert await plugin.service.run()  # disabled: no retrieval/model/send
        assert not await plugin._send("qq-check:GroupMessage:12345", "local check")
        assert MessageChain().message("local check").chain

        config["topics"] = [{
            "__template_key": "research_topic", "id": "graph", "name": "Graph reasoning",
            "description": "Graph reasoning methods and reproducible evaluation",
            "categories": ["cs.AI"], "keywords": [], "exclude_keywords": [],
            "threshold": 70, "retrieval_mode": "broad", "enabled": True,
        }]
        results = [r async for r in plugin.paper_bind(event("paper_bind graph 120"), "graph", 120)]
        assert results
        stored = json.loads(config_file.read_text(encoding="utf-8-sig"))
        assert stored["subscriptions"][0]["target"] == "qq-check:GroupMessage:12345"
        assert stored["subscriptions"][0]["topic_ids"] == ["graph"]
        assert stored["subscriptions"][0]["interval_minutes"] == 120

        # Exercise AstrBot's actual WebUI validation/save function, then mimic
        # its documented plugin reload with terminate + a fresh plugin instance.
        from astrbot.dashboard.routes.config import save_config

        updated = deepcopy(dict(config))
        updated["subscriptions"][0]["interval_minutes"] = 180
        save_config(updated, config)
        assert config["subscriptions"][0]["interval_minutes"] == 180
    finally:
        await plugin.terminate()

    reloaded_config = AstrBotConfig(str(config_file), schema=schema)
    reloaded = plugin_module.PaperBotPlugin(context, reloaded_config)
    await reloaded.initialize()
    assert reloaded.service is not None, reloaded._startup_error
    try:
        assert "180" in reloaded.service.subscriptions("qq-check:GroupMessage:12345")
        assert reloaded.service.config is reloaded_config
        [r async for r in reloaded.paper_unbind(event("paper_unbind"))]
        stored = json.loads(config_file.read_text(encoding="utf-8-sig"))
        assert not any(s["target"] == "qq-check:GroupMessage:12345" and s.get("enabled", True)
                       for s in stored["subscriptions"])
        assert "没有启用" in reloaded.service.subscriptions("qq-check:GroupMessage:12345")
    finally:
        await reloaded.terminate()

    print(f"AstrBot {sdk_version}: real plugin import, metadata/schema, 10 admin commands, parameter parsing,")
    print("native message chain, disabled lifecycle, command persistence and WebUI save/reload checks passed.")
    print("No QQ messages, LLM requests or arXiv requests were sent.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-path", type=Path, help="Optional official AstrBot source checkout")
    args = parser.parse_args()
    if args.sdk_path:
        sys.path.insert(0, str(args.sdk_path.resolve()))
    sys.addaudithook(forbid_network)
    (ROOT / ".build").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sdk-check-", dir=ROOT / ".build") as temporary:
        workspace = Path(temporary)
        os.environ["ASTRBOT_ROOT"] = str(workspace)
        asyncio.run(check(workspace))


if __name__ == "__main__":
    main()
