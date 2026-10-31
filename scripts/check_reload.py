"""Offline regression check of real AstrBot plugin reload/update behavior.

Run with the SDK environment: .build/sdk-venv/bin/python scripts/check_reload.py
Copies plugin code into a temporary host directory. Only the archive-download
step is replaced with local file writes; PluginManager.load/reload/update_plugin
and the plugin's lifecycle/service are real. No external requests are allowed.
"""

import argparse
import asyncio
import importlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from check_sdk import ROOT, forbid_network


async def check(workspace: Path):
    from astrbot.api import AstrBotConfig
    from astrbot.api.star import Context
    from astrbot.core.star import star_manager
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_manager import PluginManager

    package = "data.plugins.paper_bot_reload_probe"
    plugin_dir = workspace / "data/plugins/paper_bot_reload_probe"
    plugin_dir.mkdir(parents=True)
    for folder in (workspace / "data", workspace / "data/plugins", plugin_dir):
        (folder / "__init__.py").write_text("", encoding="utf-8")
    for name in ("main.py", "metadata.yaml", "_conf_schema.json"):
        shutil.copy2(ROOT / name, plugin_dir / name)
    shutil.copytree(ROOT / "paperbot", plugin_dir / "paperbot", ignore=shutil.ignore_patterns("__pycache__"))
    (workspace / "data/config").mkdir(exist_ok=True)
    sources = {name: (plugin_dir / name).read_text(encoding="utf-8")
               for name in ("main.py", "paperbot/service.py", "paperbot/errors.py")}

    def write_revision(marker):
        # Revision lengths differ, so Python's timestamp/size bytecode validation
        # can distinguish edits made within one filesystem timestamp interval.
        additions = {
            "main.py": f"\nSDK_MAIN_PROBE = {marker!r}\n",
            "paperbot/errors.py": f"\nSDK_ERRORS_PROBE = {marker!r}\n",
            "paperbot/service.py": (
                "\n_SDK_STATUS = PaperService.status\ndef _sdk_status(self):\n"
                f"    return {marker + ': '!r} + _SDK_STATUS(self)\n"
                "PaperService.status = _sdk_status\n"
            ),
        }
        for name, original in sources.items():
            (plugin_dir / name).write_text(original + additions[name], encoding="utf-8")
        importlib.invalidate_caches()

    write_revision("OLD")
    host_config = AstrBotConfig(str(workspace / "host_config.json"))
    context = Context(
        event_queue=asyncio.Queue(), config=host_config, db=None,
        provider_manager=SimpleNamespace(inst_map={}), platform_manager=SimpleNamespace(platform_insts=[]),
        conversation_manager=None, message_history_manager=None, persona_manager=None,
        astrbot_config_mgr=None, knowledge_base_manager=None,
    )
    manager = PluginManager(context, host_config)

    async def global_get(key, default=None):
        return default

    # Shared preferences/command-management DB initialization is unrelated to
    # module reload; leave these host stores empty while exercising the manager.
    with patch.object(star_manager.sp, "global_get", side_effect=global_get), \
         patch.object(star_manager, "sync_command_configs", new=AsyncMock()):
        try:
            success, error = await manager.load(specified_dir_name="paper_bot_reload_probe")
            assert success, error
            old = star_map[package + ".main"].star_cls
            assert old.service is not None, old._startup_error
            old_service, old_task = old.service, old._task
            before = {name: module for name, module in sys.modules.items() if name.startswith(package + ".") or name == package}
            assert old_service.status().startswith("OLD: ")

            write_revision("NEW_UPDATED")
            success, error = await manager.reload("astrbot_plugin_paper_bot")
            assert success, error
            current = star_map[package + ".main"].star_cls
            assert current.service.status().startswith("NEW_UPDATED: ")
            assert sys.modules[package + ".main"].SDK_MAIN_PROBE == "NEW_UPDATED"
            assert sys.modules[package + ".paperbot.errors"].SDK_ERRORS_PROBE == "NEW_UPDATED"
            assert current.service.__class__ is not old_service.__class__
            assert old_task.done() and old_service.closed
            assert sys.modules[package + ".main"].ConfigError is sys.modules[package + ".paperbot.errors"].ConfigError
            unchanged = [name for name, module in before.items() if sys.modules.get(name) is module]
            assert not unchanged, unchanged
            print(f"RELOAD PASS: {len(before)} plugin modules replaced; main/service/errors updated; old task/store closed.")

            async def local_update(plugin, proxy=""):
                write_revision("UPDATE_PATH_THIRD_REVISION")

            with patch.object(manager.updator, "update", side_effect=local_update):
                await manager.update_plugin("astrbot_plugin_paper_bot")
            newest = star_map[package + ".main"].star_cls
            assert newest.service.status().startswith("UPDATE_PATH_THIRD_REVISION: ")
            assert sys.modules[package + ".main"].SDK_MAIN_PROBE == "UPDATE_PATH_THIRD_REVISION"
            assert sys.modules[package + ".paperbot.errors"].SDK_ERRORS_PROBE == "UPDATE_PATH_THIRD_REVISION"
            print("UPDATE PASS: local file replacement + real update_plugin/reload updated all three modules.")
        finally:
            metadata = star_map.get(package + ".main")
            if metadata:
                await manager._terminate_plugin(metadata)
                await manager._unbind_plugin("astrbot_plugin_paper_bot", package + ".main")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-path", type=Path, default=ROOT / ".build/AstrBot")
    args = parser.parse_args()
    sys.path.insert(0, str(args.sdk_path.resolve()))
    sys.addaudithook(forbid_network)
    (ROOT / ".build").mkdir(exist_ok=True)
    previous_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="paperbot-reload-", dir=ROOT / ".build") as temporary:
        workspace = Path(temporary)
        os.environ["ASTRBOT_ROOT"] = str(workspace)
        os.environ.pop("ASTRBOT_RELOAD", None)
        sys.path.insert(0, str(workspace))
        try:
            os.chdir(workspace)
            asyncio.run(check(workspace))
        finally:
            os.chdir(previous_cwd)


if __name__ == "__main__":
    main()
