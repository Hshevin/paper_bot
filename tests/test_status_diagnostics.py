"""Status and legacy pause migration must not start external work."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from paperbot.arxiv import ArxivError
from paperbot.errors import diagnose, external_error, public_error
from paperbot.scoring import ScoringError
from paperbot.service import PaperService


SECRET = "sk-private-legacy-provider-secret"
OLD_STATUS = "本轮抓取 14 条，分析 1 篇，成功投递 0 条，失败 1 项；模型筛选已暂停"
MISSING_DETAIL = "旧版未保存详细原因，无法恢复"


class StatusDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.generate = AsyncMock(side_effect=AssertionError("Status must not call a model"))
        self.send = AsyncMock(side_effect=AssertionError("Status must not send a message"))
        self.feed = AsyncMock()
        self.feed.fetch_page.side_effect = AssertionError("Status must not fetch arXiv")
        self.service = PaperService(
            Path(self.directory.name), {"enabled": True, "topics": [], "subscriptions": []},
            self.generate, self.send, client=self.feed,
            clock=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc),
        )
        self.service.store.set_meta("last_status", OLD_STATUS)

    async def asyncTearDown(self):
        self.generate.assert_not_awaited()
        self.send.assert_not_awaited()
        self.feed.fetch_page.assert_not_awaited()
        await self.service.close()
        self.directory.cleanup()

    def pause(self, value):
        self.service.store.set_meta("analysis_pause", json.dumps(value, ensure_ascii=False))

    def later_arxiv_error(self):
        report = diagnose(ArxivError("arXiv 请求失败（HTTP 429），稍后重试"), "arXiv 抓取")
        self.service.store.set_meta("last_error", json.dumps(report, ensure_ascii=False))
        return report

    async def restart_service(self):
        await self.service.close()
        self.service = PaperService(
            Path(self.directory.name), self.service.config,
            self.generate, self.send, client=self.feed,
            clock=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc),
        )

    @staticmethod
    def provider_report(status):
        error = RuntimeError(f'private API key {SECRET}')
        error.status_code = status
        return diagnose(external_error(error, '模型'), '模型筛选', context='论文 2610.00001v1')

    async def test_status_shows_core_version_without_starting_work(self):
        status = self.service.status()
        self.assertTrue(status.startswith("Paper Bot 核心版本：1.0.3"))
        self.assertIn("运行中：否", status)

    async def test_current_phase_is_shown_only_while_work_is_running(self):
        self.service._phase = "arXiv 抓取"
        async with self.service.lock:
            running = self.service.status()
        self.assertIn("运行中：是", running)
        self.assertIn("arXiv 抓取", running)
        idle = self.service.status()
        self.assertIn("运行中：否", idle)
        self.assertNotIn("arXiv 抓取", idle)

    async def test_old_type_only_pause_reports_what_is_known_and_what_was_lost(self):
        self.pause({"signature": "x", "error": "ScoringError"})
        self.assertEqual(self.service.store.meta("last_error"), "")
        status = self.service.status()
        self.assertIn("ScoringError", status)
        self.assertIn(MISSING_DETAIL, status)
        self.assertIn("/paper_retry", status)
        self.assertNotIn("HTTP 401", status)
        self.assertNotIn("认证失败", status)

    async def test_legacy_public_pause_survives_a_new_unrelated_arxiv_failure(self):
        report = self.provider_report(401)
        self.pause({"signature": "x", "error": public_error(report)})
        later = self.later_arxiv_error()
        status = self.service.status()
        self.assertIn("HTTP 401", status)
        self.assertIn(report["id"], status)
        self.assertNotEqual(report["id"], later["id"])
        self.assertNotIn(MISSING_DETAIL, status)

    async def test_structured_pause_report_has_priority_over_legacy_error_and_last_error(self):
        report = self.provider_report(429)
        legacy = self.provider_report(401)
        self.pause({"signature": "x", "report": report, "error": public_error(legacy)})
        self.later_arxiv_error()
        status = self.service.status()
        self.assertIn(report["message"], status)
        self.assertIn(report["id"], status)
        self.assertIn("2610.00001v1", status)
        self.assertNotIn(legacy["message"], status)
        self.assertNotIn(legacy["id"], status)

    async def test_startup_releases_legacy_scoring_pauses_without_any_network_work(self):
        report = diagnose(ScoringError('模型提供的匹配依据不在论文摘要中，请重试'), '模型筛选')
        for value in (
            {'signature': 'x', 'error': 'ScoringError'},
            {'signature': 'x', 'error': public_error(report)},
            {'signature': 'x', 'error': public_error(report), 'report': report},
        ):
            with self.subTest(value=value):
                self.pause(value)
                later = self.later_arxiv_error()
                await self.restart_service()
                self.assertEqual(self.service.store.meta('analysis_pause'), '')
                status = self.service.status()
                self.assertIn('模型筛选：就绪', status)
                self.assertIn(later['id'], status)

    async def test_startup_preserves_provider_pauses_and_never_exposes_original_exception(self):
        report = self.provider_report(401)
        for value in (
            {'signature': 'x', 'error': 'RuntimeError'},
            {'signature': 'x', 'error': public_error(report)},
            {'signature': 'x', 'error': public_error(report), 'report': report},
        ):
            with self.subTest(value=value):
                self.pause(value)
                await self.restart_service()
                self.assertEqual(json.loads(self.service.store.meta('analysis_pause')), value)
                status = self.service.status()
                self.assertIn('模型筛选：已暂停', status)
                self.assertNotIn(SECRET, status)
                self.assertNotIn('private API key', status)

    async def test_arbitrary_legacy_text_is_not_treated_as_safe_diagnostic(self):
        for raw in (
            f"ValueError: API key {SECRET}; URL https://example.test/?key={SECRET}",
            f"[Paper Bot] provider response: {SECRET}",
            f"[Paper Bot] 失败阶段：模型筛选\n原因：API key {SECRET}\n建议：重试\n诊断号：abcdef12",
        ):
            with self.subTest(raw=raw.splitlines()[0]):
                self.pause({"signature": "x", "error": raw})
                status = self.service.status()
                self.assertNotIn(SECRET, status)
                self.assertNotIn("example.test", status)
                self.assertIn("暂停", status)
                self.assertIn("/paper_retry", status)


if __name__ == "__main__":
    unittest.main()
