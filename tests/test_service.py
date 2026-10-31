import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock, Mock

from paperbot.models import Paper, Topic
from paperbot.service import PaperService, validate_target
from paperbot.arxiv import ArxivError
from paperbot.errors import external_error

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)
PAPER = Paper('2610.00001', 1, 'Search for reasoning', 'We study search and reasoning with explicit experiments.', ('Alice',), ('cs.AI',), NOW, NOW)
TARGET_A = 'qq:GroupMessage:123'
TARGET_B = 'qq:FriendMessage:456'


class Feed:
    def __init__(self):
        self.calls = []
        self.topic_calls = []
        self.fail = False
        self.fail_offsets = set()
        self.page_size = 100
        self.papers_by_topic = {}
    async def fetch_page(self, topic, start, end, offset=0):
        self.calls.append((start, end, offset))
        self.topic_calls.append(topic.id)
        if self.fail or offset in self.fail_offsets:
            raise OSError('private provider response must not be logged')
        papers = self.papers_by_topic.get(topic.id, [PAPER])
        return papers[offset:offset + self.page_size], len(papers)


class WindowFeed:
    """A bounded fake with inclusive dates, one row per page and oversized windows."""
    def __init__(self, papers, max_window):
        self.papers = papers
        self.max_window = max_window
        self.calls = []
        self.fail_requests = set()

    async def fetch_page(self, topic, start, end, offset=0):
        request = (start, end, offset)
        self.calls.append(request)
        if request in self.fail_requests:
            raise OSError('test second-window page failure')
        if end - start > self.max_window:
            return self.papers[:1], 30001
        rows = [paper for paper in self.papers if start <= paper.published <= end]
        return rows[offset:offset + 1], len(rows)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = {'enabled': True, 'provider_id': 'test',
                       'subscriptions': [{'target': target, 'topic_ids': ['reasoning'], 'interval_minutes': 60}
                                         for target in (TARGET_A, TARGET_B)],
                       'topics': [{'id': 'reasoning', 'name': 'Reasoning', 'description': 'Search methods with experiments', 'categories': ['cs.AI']}],
                       'max_analyses_per_run': 2, 'max_push_per_target': 5}
        self.feed = Feed()
        self.generated = 0
        self.sent = []
        self.fail_target = None
        self.bad_model = False
        self.provider_error = None
        self.now = NOW
        self.scores = {}
        async def generate(system, user):
            self.generated += 1
            if self.provider_error is not None:
                raise self.provider_error
            if self.bad_model:
                return 'invalid output'
            source = json.loads(user)
            paper_id = source['untrusted_paper']['id']
            key = (source['research_profile']['topic'], paper_id)
            score = self.scores.get(key, self.scores.get(paper_id, 85))
            return json.dumps({'score': score, 'summary': '中文摘要', 'reason': '符合关注方向', 'evidence': ['search and reasoning']})
        async def send(target, text):
            self.sent.append((target, text))
            return target != self.fail_target
        self.generate_fn, self.send_fn = generate, send
        self.service = PaperService(Path(self.temp.name), self.config, generate, send, client=self.feed, clock=lambda: self.now)
        self.sleep = patch('paperbot.service.asyncio.sleep', new=AsyncMock())
        self.sleep.start()

    async def asyncTearDown(self):
        await self.service.close()
        self.sleep.stop()
        self.temp.cleanup()

    async def test_delivery_dedup_is_per_destination_and_failure_retries(self):
        self.fail_target = 'qq:FriendMessage:456'
        await self.service.run(force=True)
        self.assertEqual(self.generated, 1)
        self.fail_target = None
        await self.service.run(force=True)
        self.assertEqual(self.generated, 1)
        self.assertEqual([t for t, _ in self.sent].count('qq:GroupMessage:123'), 1)
        self.assertEqual([t for t, _ in self.sent].count('qq:FriendMessage:456'), 2)
        self.assertIn('search and reasoning', self.sent[0][1])
        self.assertIn('https://arxiv.org/abs/2610.00001v1', self.sent[0][1])

    async def test_config_failure_identifies_webui_row_and_field_in_reply_log_and_status(self):
        self.config['topics'][0]['categories'] = ['cat:cs.AI']
        reports = []
        self.service.report_error = reports.append
        reply = await self.service.run(force=True)
        self.assertIn('配置校验', reply)
        self.assertIn('研究主题第 1 项', reply)
        self.assertIn('topics[0].categories', reply)
        self.assertIn('cs.AI', reply)
        self.assertEqual(len(reports), 1)
        self.assertIn(reports[0]['id'], reply)
        self.assertTrue(reports[0]['trace'])
        self.assertIn(reports[0]['id'], self.service.status())
        self.assertFalse(self.feed.calls)
        self.assertEqual(self.generated, 0)
        self.assertFalse(self.sent)

    async def test_screenshot_category_spacing_is_accepted_by_full_pipeline(self):
        self.config['topics'][0].update(id='llm', name='1', categories=['cs. AI'],
            description='我想了解cv，nlp，llm infra领域的最新进展', keywords=['cv', 'nlp', 'llm infra'])
        for subscription in self.config['subscriptions']:
            subscription['topic_ids'] = ['llm']
        reply = await self.service.run(force=True)
        self.assertNotIn('失败阶段', reply)
        self.assertEqual(self.service._topics()[0].categories, ('cs.AI',))
        self.assertEqual(self.generated, 1)
        self.assertEqual(len(self.sent), 2)

    async def test_bad_subscription_reports_its_field_without_revealing_value(self):
        self.config['subscriptions'][1]['topic_ids'] = ['secret-config-value']
        reply = await self.service.run(force=True)
        self.assertIn('subscriptions[1].topic_ids', reply)
        self.assertIn('会话订阅第 2 项', reply)
        self.assertIn('/paper_topics', reply)
        self.assertNotIn('secret-config-value', reply)
        self.assertFalse(self.feed.calls)

    async def test_arxiv_failure_keeps_http_status_and_resume_context(self):
        self.config['topics'].insert(0, {'enabled': False})
        self.feed.fetch_page = AsyncMock(side_effect=ArxivError('arXiv 请求失败（HTTP 429），请稍后重试'))
        reports = []
        self.service.report_error = reports.append
        reply = await self.service.run(force=True)
        self.assertIn('arXiv 抓取', reply)
        self.assertIn('HTTP 429', reply)
        self.assertIn('分页偏移 0', reply)
        self.assertIn('研究主题第 2 项（topics[1]）', reply)
        self.assertEqual(len(reports), 1)
        self.assertIn(reports[0]['id'], self.service.store.scan(self.service._topics()[0])['error'])
        self.assertIsNone(self.service.store.scan(self.service._topics()[0])['success'])

    async def test_invalid_model_output_has_persistent_diagnostic_without_global_pause(self):
        self.bad_model = True
        reports = []
        self.service.report_error = reports.append
        reply = await self.service.run(force=True)
        self.assertIn('模型输出校验', reply)
        self.assertIn('模型未返回有效 JSON', reply)
        self.assertIn('单篇', reply)
        saved = json.loads(self.service.store.meta('last_error'))
        self.assertEqual(reports[-1]['id'], saved['id'])
        self.assertEqual(self.service.store.meta('analysis_pause'), '')
        self.assertEqual(self.generated, 2)
        self.assertIn('分析 1 篇', reply)
        self.assertIn('模型调用 2 次', reply)
        self.assertFalse(self.sent)
        self.assertFalse(self.service.store.pending(self.service._topics()[0], 100))
        row = self.service.store.db.execute('SELECT analysis,error FROM papers').fetchone()
        self.assertIsNone(row['analysis'])
        self.assertIn(saved['id'], row['error'])
        self.assertIn('2610.00001v1', saved['context'])
        await self.restart_service()
        self.assertIn(saved['id'], self.service.status())

    async def test_unknown_remote_error_never_enters_reply_log_or_database(self):
        secret = 'Authorization=Bearer SECRET_KEY; https://private.example/api?key=TOKEN'
        self.feed.fetch_page = AsyncMock(side_effect=ValueError(secret))
        reports = []
        self.service.report_error = reports.append
        reply = await self.service.run(force=True)
        saved = self.service.store.meta('last_error') + self.service.store.meta('last_status')
        rendered = reply + saved + json.dumps(reports)
        for value in ('SECRET_KEY', 'private.example', 'TOKEN', 'Authorization'):
            self.assertNotIn(value, rendered)
        self.assertIn('诊断号', reply)
        self.assertIn('ValueError', reply)

    async def test_delivery_failure_identifies_stage_without_losing_other_targets(self):
        error = RuntimeError('private response must not be shown')
        error.status_code = 403
        async def send(target, text):
            if target == TARGET_A:
                raise external_error(error, 'QQ')
            self.sent.append((target, text))
            return True
        self.service.send = send
        reply = await self.service.run(force=True)
        self.assertIn('QQ 推送', reply)
        self.assertIn('HTTP 403', reply)
        self.assertIn('发言权限', reply)
        self.assertEqual([target for target, _ in self.sent], [TARGET_B])
        self.assertNotIn('private response', reply)

    async def test_manual_range_and_pdf_failures_preserve_safe_detail(self):
        self.feed.fetch_page = AsyncMock(side_effect=ArxivError('arXiv 请求失败（HTTP 503）'))
        reply = await self.service.fetch_range('reasoning', '2026-10-01', '2026-10-02')
        self.assertIn('日期范围抓取', reply)
        self.assertIn('HTTP 503', reply)
        self.assertIn('续传', reply)
        topic = self.service._topics()[0]
        self.service.store.begin_scan(topic, NOW, NOW)
        self.service.store.accept_page(topic, [PAPER], 1, True)
        from paperbot.reading import ReadingError
        with patch('paperbot.reading.read_paper', new=AsyncMock(side_effect=ReadingError('PDF 没有可提取的文字，可能是扫描件；未调用模型'))):
            reply = await self.service.read(PAPER.id)
        self.assertIn('全文精读', reply)
        self.assertIn('扫描件', reply)
        self.assertEqual(self.generated, 0)

    async def test_low_scores_retained_and_lower_threshold_no_rebill(self):
        self.config['topics'][0]['threshold'] = 90
        await self.service.run(force=True)
        self.assertFalse(self.sent)
        self.config['topics'][0]['threshold'] = 70
        await self.service.run(force=True)
        self.assertEqual(self.generated, 1)
        self.assertEqual(len(self.sent), 2)

    async def test_provider_failure_pauses_across_rounds(self):
        self.provider_error = RuntimeError('private provider response')
        await self.service.run(force=True)
        await self.service.run(force=True)
        self.assertEqual(self.generated, 1)
        self.assertFalse(self.sent)
        self.provider_error = None
        self.service.retry()
        await self.service.run(force=True)
        self.assertEqual(self.generated, 2)
        self.assertEqual(len(self.sent), 2)

    async def test_invalid_paper_is_isolated_and_next_paper_still_delivered(self):
        second = replace(PAPER, id='2610.00002', title='Another reasoning paper')
        self.feed.papers_by_topic['reasoning'] = [PAPER, second]
        self.config['max_analyses_per_run'] = 3
        calls = []
        async def generate(system, user):
            paper_id = json.loads(user)['untrusted_paper']['id']
            calls.append(paper_id)
            if paper_id == PAPER.id + 'v1':
                return 'invalid output'
            return await self.generate_fn(system, user)
        self.service.generate = generate
        reply = await self.service.run(force=True)
        self.assertEqual(calls, [PAPER.id + 'v1', PAPER.id + 'v1', second.id + 'v1'])
        self.assertIn('分析 2 篇', reply)
        self.assertIn('模型调用 3 次', reply)
        self.assertEqual(self.service.store.meta('analysis_pause'), '')
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(second.url in message and PAPER.url not in message for _, message in self.sent))
        invalid = self.service.store.db.execute('SELECT analysis,error FROM papers WHERE id=?', (PAPER.id,)).fetchone()
        self.assertIsNone(invalid['analysis'])
        self.assertTrue(invalid['error'])
        await self.service.run(force=True)
        self.assertEqual(len(calls), 3, 'A permanently invalid paper must not be retried automatically')

    async def test_local_preflight_rejection_does_not_spend_model_call_budget(self):
        empty = replace(PAPER, abstract='')
        second = replace(PAPER, id='2610.00002')
        self.feed.papers_by_topic['reasoning'] = [empty, second]
        self.config['max_analyses_per_run'] = 1
        reply = await self.service.run(force=True)
        self.assertEqual(self.generated, 1)
        self.assertIn('模型调用 1 次', reply)
        self.assertEqual(self.service.store.meta('analysis_pause'), '')
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(second.url in message and empty.url not in message for _, message in self.sent))

    async def test_correction_keeps_original_paper_and_accepts_only_verbatim_evidence(self):
        prompts = []
        async def generate(system, user):
            prompts.append(json.loads(user))
            if len(prompts) == 1:
                return json.dumps({'score': 85, 'summary': '中文摘要', 'reason': '符合关注方向',
                                   'evidence': ['我们研究搜索与推理']})
            return json.dumps({'score': 85, 'summary': '中文摘要', 'reason': '符合关注方向',
                               'evidence_ids': [1], 'evidence': ['我们研究搜索与推理']})
        self.service.generate = generate
        reply = await self.service.run(force=True)
        self.assertEqual(len(prompts), 2)
        self.assertEqual(prompts[0]['untrusted_paper'], prompts[1]['untrusted_paper'])
        self.assertEqual(''.join(segment['text'] for segment in prompts[1]['untrusted_paper']['abstract_segments']),
                         PAPER.abstract)
        self.assertEqual(self.service.store.meta('analysis_pause'), '')
        self.assertIn('模型调用 2 次', reply)
        self.assertIn('分析 1 篇', reply)
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all('search and reasoning' in message and '我们研究搜索与推理' not in message
                            for _, message in self.sent))
        analysis = json.loads(self.service.store.db.execute('SELECT analysis FROM papers').fetchone()[0])
        self.assertEqual(analysis['evidence'], [PAPER.abstract])

    async def test_budget_one_defers_correction_across_restart_then_isolates_invalid_paper(self):
        second = replace(PAPER, id='2610.00002')
        self.feed.papers_by_topic['reasoning'] = [PAPER, second]
        self.config['max_analyses_per_run'] = 1
        calls = []
        async def generate(system, user):
            paper_id = json.loads(user)['untrusted_paper']['id']
            calls.append(paper_id)
            if paper_id == PAPER.id + 'v1':
                return 'invalid output'
            return await normal_generate(system, user)
        normal_generate = self.generate_fn
        self.generate_fn = self.service.generate = generate
        first = await self.service.run(force=True)
        self.assertEqual(len(calls), 1)
        self.assertIn('模型调用 1 次', first)
        self.assertIn('待纠正 1 篇', first)
        row = self.service.store.db.execute('SELECT analysis,error,correction FROM papers WHERE id=?', (PAPER.id,)).fetchone()
        self.assertIsNone(row['analysis'])
        self.assertIsNone(row['error'])
        self.assertTrue(row['correction'])
        self.assertFalse(self.sent)
        await self.restart_service()
        second_round = await self.service.run(force=True)
        self.assertEqual(calls, [PAPER.id + 'v1', PAPER.id + 'v1'])
        self.assertIn('模型调用 1 次', second_round)
        self.assertIn('待纠正 0 篇', second_round)
        self.assertEqual(self.service.store.meta('analysis_pause'), '')
        self.assertFalse(self.sent)
        await self.restart_service()
        third = await self.service.run(force=True)
        self.assertEqual(calls, [PAPER.id + 'v1', PAPER.id + 'v1', second.id + 'v1'])
        self.assertIn('模型调用 1 次', third)
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(all(second.url in message for _, message in self.sent))
        await self.service.run(force=True)
        self.assertEqual(len(calls), 3)

    async def test_provider_timeout_counts_as_a_call_and_pauses_without_leaking_detail(self):
        self.provider_error = TimeoutError('Authorization=Bearer sk-private-provider-secret')
        self.config['max_analyses_per_run'] = 1
        reports = []
        self.service.report_error = reports.append
        reply = await self.service.run(force=True)
        self.assertIn('模型调用 1 次', reply)
        self.assertEqual(self.generated, 1)
        self.assertTrue(self.service.store.meta('analysis_pause'))
        self.assertFalse(self.sent)
        rendered = reply + self.service.status() + json.dumps(reports) + self.service.store.meta('analysis_pause')
        self.assertNotIn('sk-private-provider-secret', rendered)
        self.assertNotIn('Authorization', rendered)
        await self.restart_service()
        await self.service.run(force=True)
        self.assertEqual(self.generated, 1)

    async def test_provider_failure_during_correction_counts_both_calls_and_pauses(self):
        second = replace(PAPER, id='2610.00002')
        self.feed.papers_by_topic['reasoning'] = [PAPER, second]
        calls = []
        error = RuntimeError('Authorization=Bearer sk-private-provider-secret')
        error.status_code = 429
        async def generate(system, user):
            calls.append(json.loads(user)['untrusted_paper']['id'])
            if len(calls) == 1:
                return 'invalid output'
            raise external_error(error, '模型')
        self.service.generate = generate
        reply = await self.service.run(force=True)
        self.assertEqual(calls, [PAPER.id + 'v1', PAPER.id + 'v1'])
        self.assertIn('模型调用 2 次', reply)
        self.assertIn('分析 1 篇', reply)
        self.assertIn('HTTP 429', reply)
        self.assertTrue(self.service.store.meta('analysis_pause'))
        self.assertFalse(self.sent)
        await self.service.run(force=True)
        self.assertEqual(len(calls), 2)
        self.assertNotIn('sk-private-provider-secret', reply + self.service.status())

    async def test_invalid_output_correction_does_not_need_model_config_changes(self):
        config_before = deepcopy(self.config)
        calls = []
        secret = 'sk-private-output-secret'
        async def generate(system, user):
            calls.append((system, user))
            if len(calls) == 1:
                return json.dumps({'score': '85', 'summary': secret, 'reason': 'reason',
                                   'evidence': ['search and reasoning']})
            return await self.generate_fn(system, user)
        self.service.generate = generate
        reports = []
        self.service.report_error = reports.append
        reply = await self.service.run(force=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.config, config_before)
        self.assertEqual(len(self.sent), 2)
        rendered = reply + self.service.status() + json.dumps(reports) + calls[1][0] + calls[1][1]
        self.assertNotIn(secret, rendered)
        self.assertEqual(self.service.store.meta('analysis_pause'), '')

    async def test_paused_reason_survives_later_fetch_errors_and_repeated_runs(self):
        error = RuntimeError('private provider response')
        error.status_code = 401
        self.provider_error = external_error(error, '模型')
        first = await self.service.run(force=True)
        pause = json.loads(self.service.store.meta('analysis_pause'))
        cause_id = pause['report']['id']
        self.assertIn('HTTP 401', first)
        self.feed.fetch_page = AsyncMock(side_effect=ArxivError('arXiv 请求失败（HTTP 429）'))
        second = await self.service.run(force=True)
        self.assertEqual(self.generated, 1)
        self.assertIn('HTTP 429', second)
        self.assertIn('HTTP 401', second)
        self.assertIn(cause_id, second)
        self.assertNotEqual(json.loads(self.service.store.meta('last_error'))['id'], cause_id)
        status = self.service.status()
        self.assertLess(status.index('HTTP 401'), status.index('上一轮统计'))
        self.assertIn(cause_id, status)
        self.assertFalse(self.sent)

    async def test_fetch_failure_does_not_advance_cursor(self):
        self.feed.fail = True
        await self.service.run(force=True)
        topic = self.service._topics()[0]
        state = self.service.store.scan(topic)
        self.assertIsNone(state['success'])
        until = state['until']
        self.feed.fail = False
        await self.service.run(force=True)
        self.assertEqual(self.service.store.scan(topic)['success'], until)

    async def test_disabled_scheduler_does_not_call_network(self):
        self.config['enabled'] = False
        await self.service.run()
        self.assertFalse(self.feed.calls)
        self.assertEqual(self.generated, 0)

    async def test_unbind_disables_config_and_bind_restores(self):
        target = TARGET_A
        self.service.unbind(target)
        await self.service.run(force=True)
        self.assertEqual([t for t, _ in self.sent], [TARGET_B])
        self.service.bind(target, 'reasoning', 60)
        await self.service.run(force=True)
        self.assertEqual(len(self.sent), 2)

    async def test_no_targets_or_provider_does_not_spend(self):
        subscriptions = self.config['subscriptions']
        self.config['subscriptions'] = []
        await self.service.run(force=True)
        self.assertFalse(self.feed.calls)
        self.config['subscriptions'] = subscriptions
        self.config['provider_id'] = ''
        await self.service.run(force=True)
        self.assertFalse(self.feed.calls)

    async def test_cancel_does_not_record_error_or_pause(self):
        async def cancelled(*args):
            raise asyncio.CancelledError()
        self.service.generate = cancelled
        with self.assertRaises(asyncio.CancelledError):
            await self.service.run(force=True)
        self.assertEqual(self.service.store.meta('analysis_pause'), '')
        self.assertFalse(self.sent)

    async def test_invalid_target_rejected(self):
        for value in ('123', 'qq:Other:123', 'qq:GroupMessage:abc', 'a:b:GroupMessage:123'):
            with self.assertRaises(ValueError):
                validate_target(value)

    def add_topic(self, ident):
        self.config['topics'].append({'id': ident, 'name': ident.title(),
                                      'description': f'Research about {ident}', 'categories': ['cs.AI']})

    async def restart_service(self):
        await self.service.close()
        self.service = PaperService(Path(self.temp.name), self.config, self.generate_fn, self.send_fn,
                                    client=self.feed, clock=lambda: self.now)

    async def test_different_topics_stay_with_their_subscribers_and_unused_topics_not_fetched(self):
        self.add_topic('robotics')
        self.add_topic('unused')
        self.config['subscriptions'][1]['topic_ids'] = ['robotics']
        robotics = replace(PAPER, id='2610.00002', title='Robotics paper')
        self.feed.papers_by_topic = {'reasoning': [PAPER], 'robotics': [robotics]}
        await self.service.run(force=True)
        self.assertEqual(set(self.feed.topic_calls), {'reasoning', 'robotics'})
        deliveries = dict(self.sent)
        self.assertIn(PAPER.url, deliveries[TARGET_A])
        self.assertNotIn(robotics.url, deliveries[TARGET_A])
        self.assertIn(robotics.url, deliveries[TARGET_B])
        self.assertNotIn(PAPER.url, deliveries[TARGET_B])
        self.assertEqual(self.generated, 2)

    async def test_two_subscribers_share_one_fetch_and_model_score(self):
        await self.service.run(force=True)
        self.assertEqual(self.feed.topic_calls, ['reasoning'])
        self.assertEqual(self.generated, 1)
        self.assertEqual({target for target, _ in self.sent}, {TARGET_A, TARGET_B})

    async def test_independent_intervals_and_bound_subscription_survive_restart(self):
        self.service.bind(TARGET_A, 'reasoning', 15)
        await self.service.run()
        self.now = NOW + timedelta(minutes=14)
        await self.service.run()
        self.assertEqual(len(self.feed.calls), 1)
        second = replace(PAPER, id='2610.00002', title='Later paper')
        self.feed.papers_by_topic['reasoning'] = [PAPER, second]
        await self.restart_service()
        self.assertEqual({s['target']: s['interval_minutes'] for s in self.service._subscriptions()},
                         {TARGET_A: 15, TARGET_B: 60})
        self.now = NOW + timedelta(minutes=16)
        await self.service.run()
        self.assertEqual(len(self.feed.calls), 2)
        self.assertEqual([target for target, text in self.sent if second.url in text], [TARGET_A])
        self.now = NOW + timedelta(minutes=60)
        await self.service.run()
        self.assertEqual([target for target, text in self.sent if second.url in text], [TARGET_A, TARGET_B])
        self.assertEqual(self.generated, 2)

    async def test_global_top_n_across_topics_and_cross_round_duplicate_suppression(self):
        self.add_topic('robotics')
        self.config['subscriptions'] = [{'target': TARGET_A, 'topic_ids': ['reasoning', 'robotics'],
                                          'interval_minutes': 60}]
        self.config['max_analyses_per_run'] = 20
        self.config['max_push_per_target'] = 2
        shared = replace(PAPER, id='2610.00002', title='Shared paper')
        best = replace(PAPER, id='2610.00003', title='Highest scoring paper')
        self.feed.papers_by_topic = {'reasoning': [PAPER, shared], 'robotics': [shared, best]}
        self.scores = {PAPER.id + 'v1': 81, shared.id + 'v1': 92, best.id + 'v1': 99,
                       ('Robotics', shared.id + 'v1'): 90}
        await self.service.run(force=True)
        self.assertEqual(len(self.sent), 2)
        self.assertIn(best.url, self.sent[0][1])
        self.assertIn(shared.url, self.sent[1][1])
        self.assertIn('92/100', self.sent[1][1])
        await self.restart_service()
        await self.service.run(force=True)
        self.assertEqual(len(self.sent), 3)
        self.assertIn(PAPER.url, self.sent[2][1])
        self.assertEqual(sum(shared.url in message for _, message in self.sent), 1)
        self.assertEqual(self.generated, 4)

    async def test_unbind_during_analysis_prevents_pending_delivery(self):
        self.config['subscriptions'] = [self.config['subscriptions'][0]]
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_model(system, user):
            entered.set()
            await release.wait()
            return await self.generate_fn(system, user)
        self.service.generate = slow_model
        task = asyncio.create_task(self.service.run(force=True))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.service.unbind(TARGET_A)
        finally:
            release.set()
            await task
        self.assertFalse(self.sent)
        await self.restart_service()
        self.assertFalse(self.service._subscriptions())

    async def test_rebind_during_analysis_does_not_send_previous_topic(self):
        self.add_topic('robotics')
        robotics = replace(PAPER, id='2610.00002', title='Robotics paper')
        self.feed.papers_by_topic['robotics'] = [robotics]
        self.config['subscriptions'] = [self.config['subscriptions'][0]]
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_model(system, user):
            entered.set()
            await release.wait()
            return await self.generate_fn(system, user)
        self.service.generate = slow_model
        task = asyncio.create_task(self.service.run(force=True))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.service.bind(TARGET_A, 'robotics', 60)
        finally:
            release.set()
            await task
        self.assertFalse(self.sent)
        await self.service.run(force=True)
        self.assertEqual(len(self.sent), 1)
        self.assertIn(robotics.url, self.sent[0][1])
        self.assertNotIn(PAPER.url, self.sent[0][1])

    async def test_unbind_during_first_send_stops_remaining_deliveries(self):
        self.config['subscriptions'] = [self.config['subscriptions'][0]]
        self.feed.papers_by_topic['reasoning'] = [PAPER, replace(PAPER, id='2610.00002')]
        async def send_and_unbind(target, message):
            self.sent.append((target, message))
            self.service.unbind(target)
            return True
        self.service.send = send_and_unbind
        await self.service.run(force=True)
        self.assertEqual(len(self.sent), 1)

    async def test_manual_date_collection_does_not_change_automatic_cursor_or_call_model(self):
        await self.service.run(force=True)
        topic = self.service._topics()[0]
        auto_before = dict(self.service.store.scan(topic))
        generated_before, sent_before = self.generated, len(self.sent)
        await self.service.fetch_range('reasoning', '2026-01-01', '2026-01-31')
        self.assertEqual(dict(self.service.store.scan(topic)), auto_before)
        self.assertEqual(self.generated, generated_before)
        self.assertEqual(len(self.sent), sent_before)
        self.assertEqual(self.feed.calls[-1][:2], (datetime(2026, 1, 1, tzinfo=timezone.utc),
                                                datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc)))
        fetches = len(self.feed.calls)
        await self.service.fetch_range('reasoning', '2026-01-01', '2026-01-31')
        self.assertEqual(len(self.feed.calls), fetches)

    async def test_failed_second_page_resumes_exact_offset_after_restart(self):
        self.feed.page_size = 1
        self.feed.papers_by_topic['reasoning'] = [PAPER, replace(PAPER, id='2610.00002'),
                                                replace(PAPER, id='2610.00003')]
        self.feed.fail_offsets = {1}
        await self.service.run(force=True)
        topic = self.service._topics()[0]
        state = self.service.store.scan(topic)
        self.assertEqual(state['offset'], 1)
        self.assertIsNone(state['success'])
        until = state['until']
        await self.restart_service()
        self.feed.fail_offsets.clear()
        self.now += timedelta(hours=1)
        await self.service.run(force=True)
        self.assertEqual([call[2] for call in self.feed.calls], [0, 1, 1, 2])
        state = self.service.store.scan(topic)
        self.assertEqual(state['success'], until)
        self.assertIsNone(state['until'])
        self.assertEqual(self.generated, 3)

    async def test_manual_pagination_failure_resumes_without_creating_automatic_cursor(self):
        self.feed.page_size = 1
        self.feed.papers_by_topic['reasoning'] = [PAPER, replace(PAPER, id='2610.00002')]
        self.feed.fail_offsets = {1}
        await self.service.fetch_range('reasoning', '2026-01-01', '2026-01-31')
        self.feed.fail_offsets.clear()
        await self.restart_service()
        await self.service.fetch_range('reasoning', '2026-01-01', '2026-01-31')
        self.assertEqual([call[2] for call in self.feed.calls], [0, 1, 1])
        self.assertIsNone(self.service.store.scan(self.service._topics()[0]))
        self.assertEqual(self.generated, 0)
        self.assertFalse(self.sent)

    async def test_bind_updates_webui_config_and_webui_edit_routes_next_run(self):
        self.add_topic('robotics')
        self.config['subscriptions'] = [self.config['subscriptions'][0]]
        saved = []
        self.service.save_config = lambda: saved.append(deepcopy(self.config['subscriptions']))
        self.service.bind(TARGET_A, 'robotics', 30)
        entry = self.config['subscriptions'][0]
        self.assertEqual(entry['topic_ids'], ['robotics'])
        self.assertEqual(entry['interval_minutes'], 30)
        self.assertTrue(entry['enabled'])
        self.assertEqual(saved, [self.config['subscriptions']])
        # Simulate the same config dictionary being changed through AstrBot WebUI.
        entry['topic_ids'] = ['reasoning']
        entry['interval_minutes'] = 45
        await self.service.run(force=True)
        self.assertEqual(self.feed.topic_calls, ['reasoning'])
        self.assertEqual(self.service._subscriptions()[0]['interval_minutes'], 45)
        self.assertEqual(len(self.sent), 1)
        self.assertIn(PAPER.url, self.sent[0][1])

    async def test_webui_can_reenable_chat_unbound_subscription(self):
        self.config['subscriptions'] = [self.config['subscriptions'][0]]
        self.service.unbind(TARGET_A)
        self.assertFalse(self.config['subscriptions'][0]['enabled'])
        self.assertFalse(self.service._subscriptions())
        self.config['subscriptions'][0]['enabled'] = True
        await self.service.run(force=True)
        self.assertEqual([target for target, _ in self.sent], [TARGET_A])

    async def test_chat_subscription_saved_by_callback_can_be_loaded_after_restart(self):
        self.add_topic('robotics')
        saved_path = Path(self.temp.name) / 'test-config.json'
        self.service.save_config = lambda: saved_path.write_text(json.dumps(self.config), encoding='utf-8')
        self.service.bind(TARGET_A, 'robotics', 30)
        self.service.unbind(TARGET_B)
        self.config = json.loads(saved_path.read_text(encoding='utf-8'))
        await self.restart_service()
        self.assertEqual(self.service._subscriptions(), [
            {'target': TARGET_A, 'topic_ids': ['robotics'], 'interval_minutes': 30}])

    async def test_save_failure_rolls_back_bind_and_unbind_in_memory(self):
        before = deepcopy(self.config['subscriptions'])
        self.service.save_config = Mock(side_effect=OSError('test disk failure'))
        for operation in (lambda: self.service.bind(TARGET_A, 'reasoning', 30),
                          lambda: self.service.unbind(TARGET_A)):
            with self.subTest(operation=operation), self.assertRaises(OSError):
                operation()
            self.assertEqual(self.config['subscriptions'], before)
            self.assertEqual(len(self.service._subscriptions()), 2)
        self.assertEqual(self.service.save_config.call_count, 2)

    async def test_equal_scores_choose_newest_paper_before_per_topic_limit(self):
        self.config['subscriptions'] = [self.config['subscriptions'][0]]
        self.config['max_push_per_target'] = 1
        self.config['max_analyses_per_run'] = 3
        older = replace(PAPER, id='2610.00002', published=NOW - timedelta(days=3))
        middle = replace(PAPER, id='2610.00003', published=NOW - timedelta(days=1))
        self.feed.papers_by_topic['reasoning'] = [older, middle, PAPER]
        await self.service.run(force=True)
        self.assertEqual(len(self.sent), 1)
        self.assertIn(PAPER.url, self.sent[0][1])

    async def test_cancelled_running_work_can_close_and_reject_future_operations(self):
        entered = asyncio.Event()
        async def waiting_model(system, user):
            entered.set()
            await asyncio.Event().wait()
        self.service.generate = waiting_model
        task = asyncio.create_task(self.service.run(force=True))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.service.close()
        self.assertIn('停止', await self.service.run(force=True))
        self.assertIn('停止', await self.service.fetch_range('reasoning', '2026-01-01', '2026-01-31'))
        self.assertIn('停止', await self.service.read(PAPER.id))
        self.assertFalse(self.sent)

    async def test_oversized_automatic_window_preserves_horizon_and_advances_only_after_all_segments(self):
        # Establish a previous completed cursor, then open a larger frozen window.
        self.feed.papers_by_topic['reasoning'] = []
        await self.service.run(force=True)
        topic = self.service._topics()[0]
        previous_success = self.service.store.scan(topic)['success']
        start, horizon = NOW - timedelta(days=7), NOW + timedelta(days=9)
        middle = NOW + timedelta(days=1)
        self.now = horizon
        self.config['max_pages_per_run'] = 1
        papers = [replace(PAPER, id='2610.00002', published=NOW - timedelta(days=1)),
                  replace(PAPER, id='2610.00003', published=middle),
                  replace(PAPER, id='2610.00004', published=NOW + timedelta(days=6))]
        self.feed = WindowFeed(papers, timedelta(days=8))
        self.service.client = self.feed

        await self.service.run(force=True)  # The full window exceeds 30000.
        state = self.service.store.scan(topic)
        self.assertEqual(state['until'], middle.isoformat())
        self.assertEqual(state['horizon'], horizon.isoformat())
        self.assertEqual(state['success'], previous_success)
        self.assertEqual(self.generated, 0)  # Oversized first-page data was not accepted.

        await self.service.run(force=True)  # First segment, page 0.
        self.assertEqual(self.service.store.scan(topic)['offset'], 1)
        await self.restart_service()
        self.now += timedelta(hours=2)
        await self.service.run(force=True)  # First segment complete; keep old success.
        state = self.service.store.scan(topic)
        self.assertEqual(state['since'], middle.isoformat())
        self.assertEqual(state['until'], horizon.isoformat())
        self.assertEqual(state['horizon'], horizon.isoformat())
        self.assertEqual(state['success'], previous_success)
        self.assertEqual(state['offset'], 0)

        await self.service.run(force=True)  # Inclusive boundary paper is seen again.
        self.feed.fail_requests.add((middle, horizon, 1))
        await self.service.run(force=True)  # Second segment, page 1 fails.
        state = self.service.store.scan(topic)
        self.assertEqual(state['offset'], 1)
        self.assertEqual(state['success'], previous_success)
        self.assertEqual(state['horizon'], horizon.isoformat())
        await self.restart_service()
        self.feed.fail_requests.clear()
        await self.service.run(force=True)
        state = self.service.store.scan(topic)
        self.assertEqual(state['success'], horizon.isoformat())
        self.assertIsNone(state['until'])
        self.assertIsNone(state['horizon'])
        self.assertEqual(self.feed.calls, [(start, horizon, 0), (start, middle, 0),
                                        (start, middle, 1), (middle, horizon, 0),
                                        (middle, horizon, 1), (middle, horizon, 1)])
        self.assertEqual(self.generated, 3)
        for target in (TARGET_A, TARGET_B):
            self.assertEqual(sum(t == target and papers[1].url in text for t, text in self.sent), 1)

    async def test_oversized_manual_window_resumes_segments_without_advancing_automatic_cursor(self):
        await self.service.run(force=True)
        topic = self.service._topics()[0]
        automatic_before = dict(self.service.store.scan(topic))
        generated_before, sent_before = self.generated, len(self.sent)
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        horizon = datetime(2026, 1, 7, 23, 59, 59, tzinfo=timezone.utc)
        middle = (start + (horizon - start) / 2).replace(second=0, microsecond=0)
        papers = [replace(PAPER, id='2601.00001', published=start + timedelta(days=1)),
                  replace(PAPER, id='2601.00002', published=middle),
                  replace(PAPER, id='2601.00003', published=horizon - timedelta(days=1))]
        self.feed = WindowFeed(papers, timedelta(days=4))
        self.service.client = self.feed
        self.config['max_pages_per_run'] = 1

        def manual_scan():
            return self.service.store.db.execute("SELECT * FROM scans WHERE topic LIKE 'manual_%'").fetchone()

        await self.service.fetch_range('reasoning', '2026-01-01', '2026-01-07')
        self.assertEqual(manual_scan()['until'], middle.isoformat())
        self.assertEqual(manual_scan()['horizon'], horizon.isoformat())
        self.assertIsNone(manual_scan()['success'])
        await self.service.fetch_range('reasoning', '2026-01-01', '2026-01-07')
        self.assertEqual(manual_scan()['offset'], 1)
        await self.restart_service()
        await self.service.fetch_range('reasoning', '2026-01-01', '2026-01-07')
        self.assertEqual(manual_scan()['since'], middle.isoformat())
        self.assertEqual(manual_scan()['until'], horizon.isoformat())
        self.assertIsNone(manual_scan()['success'])
        await self.service.fetch_range('reasoning', '2026-01-01', '2026-01-07')
        self.assertEqual(manual_scan()['offset'], 1)
        self.assertIsNone(manual_scan()['success'])
        result = await self.service.fetch_range('reasoning', '2026-01-01', '2026-01-07')
        self.assertIn('收集完成', result)
        self.assertEqual(manual_scan()['success'], horizon.isoformat())
        self.assertIsNone(manual_scan()['horizon'])
        self.assertEqual(self.feed.calls, [(start, horizon, 0), (start, middle, 0),
                                        (start, middle, 1), (middle, horizon, 0),
                                        (middle, horizon, 1)])
        self.assertEqual(dict(self.service.store.scan(topic)), automatic_before)
        self.assertEqual(self.generated, generated_before)
        self.assertEqual(len(self.sent), sent_before)
        stored_ids = {row[0] for row in self.service.store.db.execute('SELECT id FROM papers')}
        self.assertTrue({paper.id for paper in papers}.issubset(stored_ids))

    async def test_manual_today_or_future_end_refreshes_as_time_advances_but_past_days_stay_cached(self):
        def latest_manual():
            return self.service.store.db.execute(
                "SELECT * FROM scans WHERE topic LIKE 'manual_%' ORDER BY rowid DESC LIMIT 1").fetchone()

        for start_date, end_date, start in [
            ('2026-10-31', '2026-10-31', NOW),
            ('2026-10-29', '2026-11-02', NOW - timedelta(days=2)),
        ]:
            with self.subTest(end_date=end_date):
                calls_before = len(self.feed.calls)
                self.now = NOW + timedelta(hours=1)
                await self.service.fetch_range('reasoning', start_date, end_date)
                self.assertEqual(len(self.feed.calls), calls_before + 1)
                self.assertEqual(self.feed.calls[-1], (start, self.now, 0))
                self.assertEqual(latest_manual()['success'], self.now.isoformat())

                self.now = NOW + timedelta(hours=13)
                await self.service.fetch_range('reasoning', start_date, end_date)
                self.assertEqual(len(self.feed.calls), calls_before + 2)
                overlap_start = max(start, NOW + timedelta(hours=1) - timedelta(days=1))
                self.assertEqual(self.feed.calls[-1], (overlap_start, self.now, 0))
                self.assertEqual(latest_manual()['success'], self.now.isoformat())
                self.assertIsNone(latest_manual()['until'])
                await self.service.fetch_range('reasoning', start_date, end_date)
                self.assertEqual(len(self.feed.calls), calls_before + 2)

        await self.service.fetch_range('reasoning', '2026-10-30', '2026-10-30')
        past_success = latest_manual()['success']
        self.assertEqual(past_success, (NOW - timedelta(seconds=1)).isoformat())
        calls_before = len(self.feed.calls)
        self.now = NOW + timedelta(days=2)
        await self.service.fetch_range('reasoning', '2026-10-30', '2026-10-30')
        self.assertEqual(len(self.feed.calls), calls_before)
        self.assertEqual(latest_manual()['success'], past_success)
        self.assertEqual(self.generated, 0)
        self.assertFalse(self.sent)


if __name__ == '__main__':
    unittest.main()
