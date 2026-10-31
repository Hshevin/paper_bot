from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from paperbot.models import Paper, Screening, Topic
from paperbot.storage import Storage, encode_paper


NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
TOPIC = Topic('reasoning', 'Reasoning', 'Reasoning methods', ('cs.AI',))
PAPER = Paper('2610.00001', 1, 'Reasoning', 'Evidence from experiments.', (), ('cs.AI',), NOW, NOW)
SCORE = Screening(80, '中文摘要', '相关理由', ('Evidence from experiments.',))
TARGET = 'qq:GroupMessage:123'
CORRECTION = '模型未返回有效 JSON，请按指定格式重新输出。'


class StorageCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Storage(self.directory)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add(self, paper=PAPER, topic=TOPIC):
        self.store.begin_scan(topic, NOW, NOW)
        self.store.accept_page(topic, [paper], 1, False)

    def row(self, paper=PAPER, topic=TOPIC):
        return self.store.db.execute('SELECT * FROM papers WHERE topic=? AND fingerprint=? AND id=?',
                                     (topic.id, topic.fingerprint, paper.id)).fetchone()

    def reopen(self):
        self.store.close()
        self.store = Storage(self.directory)

    def test_deferred_correction_survives_restart_and_is_pending(self):
        self.add()
        self.store.result(TOPIC, PAPER, None, error='old failure')
        self.store.defer_correction(TOPIC, PAPER, CORRECTION)
        self.reopen()
        pending = self.store.pending(TOPIC, 10)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['correction'], CORRECTION)
        self.assertIsNone(pending[0]['error'])

    def test_defer_only_touches_exact_topic_profile_paper_and_version(self):
        self.add()
        self.store.result(TOPIC, PAPER, None, error='keep')
        for topic, paper in ((replace(TOPIC, id='other'), PAPER),
                             (replace(TOPIC, description='changed profile'), PAPER),
                             (TOPIC, replace(PAPER, id='2610.00002')),
                             (TOPIC, replace(PAPER, version=2))):
            self.store.defer_correction(topic, paper, CORRECTION)
        self.assertIsNone(self.row()['correction'])
        self.assertEqual(self.row()['error'], 'keep')
        self.store.result(TOPIC, PAPER, SCORE, model='original-model')
        before = dict(self.row())
        self.store.defer_correction(TOPIC, PAPER, CORRECTION)
        self.assertEqual(dict(self.row()), before)

    def test_result_clears_correction_on_success_final_failure_and_exclusion(self):
        for index, kwargs in enumerate(({'analysis': SCORE, 'model': 'test'},
                                        {'analysis': None, 'error': 'final validation failure'},
                                        {'analysis': None, 'excluded': True}), 1):
            paper = replace(PAPER, id=f'2610.0000{index}')
            self.add(paper)
            self.store.defer_correction(TOPIC, paper, CORRECTION)
            self.store.result(TOPIC, paper, **kwargs)
            self.assertIsNone(self.row(paper)['correction'])
        self.assertEqual(self.store.pending(TOPIC, 10), [])

    def test_same_version_preserves_correction_and_new_version_resets_it(self):
        self.add()
        self.store.defer_correction(TOPIC, PAPER, CORRECTION)
        self.store.accept_page(TOPIC, [PAPER], 1, False)
        self.assertEqual(self.row()['correction'], CORRECTION)
        new = replace(PAPER, version=2)
        self.store.accept_page(TOPIC, [new], 1, False)
        row = self.row()
        self.assertEqual(row['version'], 2)
        self.assertIsNone(row['correction'])
        self.assertIsNone(row['error'])
        self.assertIsNone(row['analysis'])
        self.assertEqual(len(self.store.pending(TOPIC, 10)), 1)

    def test_explicit_retry_resets_corrections_without_reanalyzing_success_or_republishing(self):
        corrected, failed, success = (replace(PAPER, id=f'2610.0000{i}') for i in range(1, 4))
        for paper in (corrected, failed, success):
            self.add(paper)
        self.store.defer_correction(TOPIC, corrected, CORRECTION)
        self.store.result(TOPIC, failed, None, error='provider failed')
        self.store.result(TOPIC, success, SCORE)
        self.store.mark_sent(TOPIC, TARGET, success, NOW)
        self.store.set_meta('analysis_pause', json.dumps({'error': 'provider failed'}))
        before = dict(self.row(success))
        self.store.retry()
        self.assertEqual({row['id'] for row in self.store.pending(TOPIC, 10)}, {corrected.id, failed.id})
        self.assertTrue(all(row['correction'] is None for row in self.store.pending(TOPIC, 10)))
        self.assertEqual(dict(self.row(success)), before)
        self.assertEqual(self.store.recommendations(TOPIC, TARGET, 10), [])
        self.assertEqual(self.store.meta('analysis_pause'), '')

    def test_legacy_scoring_type_migrates_matching_failures_once_and_preserves_other_rows(self):
        matching, unrelated, success = (replace(PAPER, id=f'2610.0000{i}') for i in range(1, 4))
        for paper in (matching, unrelated, success):
            self.add(paper)
        self.store.result(TOPIC, matching, None, error='ScoringError')
        self.store.result(TOPIC, unrelated, None, error='ExternalError')
        self.store.result(TOPIC, success, SCORE)
        self.store.mark_sent(TOPIC, TARGET, success, NOW)
        self.store.set_meta('analysis_pause', json.dumps({'signature': 'old', 'error': 'ScoringError'}))
        before_success, before_other = dict(self.row(success)), dict(self.row(unrelated))
        self.assertTrue(self.store.resume_legacy_scoring_pause())
        self.reopen()
        self.assertFalse(self.store.resume_legacy_scoring_pause())
        self.assertEqual([row['id'] for row in self.store.pending(TOPIC, 10)], [matching.id])
        self.assertIsNone(self.row(matching)['correction'])
        self.assertEqual(dict(self.row(success)), before_success)
        self.assertEqual(dict(self.row(unrelated)), before_other)
        self.assertEqual(self.store.recommendations(TOPIC, TARGET, 10), [])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM delivered').fetchone()[0], 1)

    def test_structured_scoring_pause_matches_exact_saved_error_only(self):
        detail = '[Paper Bot] saved safe validation diagnostic'
        for paper in (PAPER, replace(PAPER, id='2610.00002')):
            self.add(paper)
        self.store.result(TOPIC, PAPER, None, error=detail)
        other = replace(PAPER, id='2610.00002')
        self.store.result(TOPIC, other, None, error='a different scoring diagnostic')
        self.store.set_meta('analysis_pause', json.dumps({
            'signature': 'old', 'error': detail, 'report': {'type': 'ScoringError'}}))
        self.assertTrue(self.store.resume_legacy_scoring_pause())
        self.assertIsNone(self.row()['error'])
        self.assertEqual(self.row(other)['error'], 'a different scoring diagnostic')
        self.assertFalse(self.store.resume_legacy_scoring_pause())

    def test_confirmed_scoring_pause_without_matching_paper_is_removed_without_other_changes(self):
        self.add()
        self.store.result(TOPIC, PAPER, None, error='unrelated error')
        before = dict(self.row())
        self.store.set_meta('analysis_pause', json.dumps({'error': 'ScoringError'}))
        self.assertTrue(self.store.resume_legacy_scoring_pause())
        self.assertEqual(dict(self.row()), before)
        self.assertEqual(self.store.meta('analysis_pause'), '')

    def test_provider_and_unknown_pauses_are_not_automatically_resumed(self):
        self.add()
        for pause in ({'error': 'ExternalError'}, {'error': 'ScoringError: remote details'},
                      {'error': 'contains ScoringError'}, {'error': 'same', 'report': {'type': 'ExternalError'}},
                      {'error': 'same', 'report': {'type': 'ScoringErrorExtra'}}):
            with self.subTest(pause=pause):
                self.store.result(TOPIC, PAPER, None, error=pause['error'])
                raw = json.dumps(pause)
                self.store.set_meta('analysis_pause', raw)
                before = dict(self.row())
                self.assertFalse(self.store.resume_legacy_scoring_pause())
                self.assertEqual(self.store.meta('analysis_pause'), raw)
                self.assertEqual(dict(self.row()), before)

    def test_malformed_pause_json_is_not_swallowed_or_deleted(self):
        self.store.set_meta('analysis_pause', '{invalid json')
        with self.assertRaises(json.JSONDecodeError):
            self.store.resume_legacy_scoring_pause()
        self.assertEqual(self.store.meta('analysis_pause'), '{invalid json')

    def test_nonobject_pause_is_not_silently_replaced(self):
        self.store.set_meta('analysis_pause', '["ScoringError"]')
        with self.assertRaises(ValueError):
            self.store.resume_legacy_scoring_pause()
        self.assertEqual(self.store.meta('analysis_pause'), '["ScoringError"]')

    def test_resume_rolls_back_paper_changes_if_pause_deletion_fails(self):
        self.add()
        self.store.result(TOPIC, PAPER, None, error='ScoringError')
        self.store.set_meta('analysis_pause', '{"error":"ScoringError"}')
        self.store.db.execute('''CREATE TRIGGER test_reject_pause_delete BEFORE DELETE ON meta
            WHEN OLD.key = 'analysis_pause' BEGIN SELECT RAISE(ABORT, 'test transaction abort'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.resume_legacy_scoring_pause()
        self.assertEqual(self.row()['error'], 'ScoringError')
        self.assertEqual(self.store.meta('analysis_pause'), '{"error":"ScoringError"}')

    def test_defer_rejects_invalid_feedback_without_echo_or_row_mutation(self):
        self.add()
        before = dict(self.row())
        for value in (None, '', '   ', {'secret': 'private-sentinel'}, 'x' * 2001):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError) as raised:
                self.store.defer_correction(TOPIC, PAPER, value)
            self.assertNotIn('private-sentinel', str(raised.exception))
            self.assertEqual(dict(self.row()), before)

    def test_schema_migration_preserves_existing_data_and_delivery_dedup(self):
        legacy = self.directory / 'legacy'
        legacy.mkdir()
        with sqlite3.connect(legacy / 'paperbot.sqlite3') as db:
            db.executescript('''
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE scans (topic TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    since TEXT, until TEXT, offset INTEGER NOT NULL DEFAULT 0, success TEXT, error TEXT);
                CREATE TABLE papers (topic TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    id TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL, analysis TEXT,
                    model TEXT, error TEXT, excluded INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(topic, fingerprint, id));
                CREATE TABLE delivered (target TEXT NOT NULL, topic TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    id TEXT NOT NULL, version INTEGER NOT NULL, sent_at TEXT NOT NULL,
                    PRIMARY KEY(target, topic, fingerprint, id, version));
            ''')
            db.execute('INSERT INTO meta VALUES (?, ?)', ('analysis_pause', '{"error":"ScoringError"}'))
            db.execute('INSERT INTO scans VALUES (?,?,?,?,?,?,?)',
                       (TOPIC.id, TOPIC.fingerprint, NOW.isoformat(), NOW.isoformat(), 1, None, None))
            db.execute('INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?)',
                       (TOPIC.id, TOPIC.fingerprint, PAPER.id, 1, encode_paper(PAPER), None, None, 'ScoringError', 0))
            db.execute('INSERT INTO delivered VALUES (?,?,?,?,?,?)',
                       (TARGET, TOPIC.id, TOPIC.fingerprint, PAPER.id, 1, NOW.isoformat()))
        migrated = Storage(legacy)
        try:
            row = migrated.db.execute('SELECT * FROM papers').fetchone()
            self.assertIsNone(row['correction'])
            self.assertEqual(row['data'], encode_paper(PAPER))
            self.assertEqual(row['error'], 'ScoringError')
            self.assertEqual(migrated.scan(TOPIC)['horizon'], NOW.isoformat())
            self.assertEqual(migrated.db.execute('SELECT count(*) FROM delivered').fetchone()[0], 1)
            self.assertTrue(migrated.resume_legacy_scoring_pause())
            migrated.result(TOPIC, PAPER, SCORE)
            self.assertEqual(migrated.recommendations(TOPIC, TARGET, 10), [])
        finally:
            migrated.close()


if __name__ == '__main__':
    unittest.main()
