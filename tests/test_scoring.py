from datetime import datetime, timezone
import json
import unittest

from paperbot.models import Paper, Topic
from paperbot.scoring import ScoringError, eligible, matched_keywords, parse_screening, screen


def paper(abstract="We improve search with a graph neural network and test time reasoning."):
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    return Paper("2610.00001", 1, "Graph reasoning", abstract, ("Author",), ("cs.AI",), now, now)


def topic(mode="broad", exclude=()):
    return Topic("graph", "图推理", "关注可复现的推理方法，排除纯应用案例", ("cs.AI",),
                 ("graph neural network", "GNN", "reasoning"), exclude, 70, mode)


def response(**updates):
    value = {"score": 85, "summary": "研究一种图推理方法。", "reason": "方法与关注方向直接相关。",
             "evidence": ["a graph neural network"]}
    value.update(updates)
    return json.dumps(value)


class ScoringTests(unittest.TestCase):
    def test_keyword_matches_are_casefolded_whole_words(self):
        item = paper("We use a GRAPH NEURAL NETWORK. beginner contains no acronym.")
        self.assertEqual(matched_keywords(item, topic()), ("graph neural network", "reasoning"))
        self.assertNotIn("GNN", matched_keywords(paper("gnnish models"), topic()))

    def test_broad_mode_accepts_semantically_relevant_nonkeyword_paper(self):
        item = Paper(**{**paper().__dict__, "title": "Inference methods", "abstract": "Inference using verifiers."})
        self.assertTrue(eligible(item, topic()))
        self.assertFalse(eligible(item, topic("strict")))
        self.assertFalse(eligible(item, topic(exclude=("verifiers",))))

    def test_parse_evidence_and_local_matches(self):
        result = parse_screening("```json\n" + response() + "\n```", paper(), topic())
        self.assertEqual(result.score, 85)
        self.assertIn("a graph neural network", result.evidence)
        self.assertEqual(result.matched_keywords, ("graph neural network", "reasoning"))

    def test_invalid_scores_fields_and_invented_evidence_are_errors(self):
        for raw in [response(score=True), response(score=85.5), response(score=101),
                    response(summary=""), response(reason=123), response(evidence=["not in this abstract"]),
                    response(evidence=[]), response(evidence="a graph neural network"), "not json"]:
            with self.subTest(raw=raw), self.assertRaises(ScoringError):
                parse_screening(raw, paper(), topic())


class ScoringAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_receives_research_description_and_untrusted_paper(self):
        captured = []
        async def generate(system, user):
            captured.extend([system, user])
            return response()
        result = await screen(paper(), topic(), generate)
        self.assertEqual(result.score, 85)
        self.assertIn(topic().description, captured[1])
        self.assertIn("不可信", captured[0])
        self.assertIn(paper().abstract, captured[1])

    async def test_model_failure_is_not_a_neutral_score(self):
        async def generate(system, user):
            raise RuntimeError("provider unavailable")
        with self.assertRaises(RuntimeError):
            await screen(paper(), topic(), generate)
