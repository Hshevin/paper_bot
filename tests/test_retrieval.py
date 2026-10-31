import asyncio
from datetime import datetime, timedelta, timezone
import unittest

from paperbot.arxiv import ArxivClient, ArxivError, build_query, parse_feed
from paperbot.models import Topic

UTC = timezone.utc
START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 2, tzinfo=UTC)
FEED = b'''<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:os="http://a9.com/-/spec/opensearch/1.1/">
<os:totalResults>1</os:totalResults><os:startIndex>0</os:startIndex><entry>
<id>http://arxiv.org/abs/2610.00001v2</id><title>A paper\n about reasoning</title>
<summary>We study search and reasoning.</summary><author><name>Alice Example</name></author>
<category term="cs.AI"/><published>2026-10-01T12:00:00Z</published>
<updated>2026-10-02T12:00:00Z</updated></entry></feed>'''


def topic(mode="broad", categories=("cs.AI", "cs.CL")):
    return Topic("reasoning", "Reasoning", "Search for reasoning methods", categories,
                 ("reasoning", "chain of thought"), retrieval_mode=mode)


class RetrievalTests(unittest.TestCase):
    def test_broad_retrieval_does_not_require_keyword(self):
        query = build_query(topic(), START, END)
        self.assertIn("(cat:cs.AI OR cat:cs.CL)", query)
        self.assertNotIn("all:", query)
        self.assertIn("submittedDate:[202610010000 TO 202610020000]", query)

    def test_strict_retrieval_and_keyword_only_query(self):
        query = build_query(topic("strict"), START, END)
        self.assertIn('(all:"reasoning" OR all:"chain of thought")', query)
        self.assertIn(" AND ", query)
        query = build_query(topic(categories=()), START, END)
        self.assertIn('all:"reasoning"', query)
        self.assertNotIn("cat:", query)

    def test_dates_are_utc_and_invalid_ranges_rejected(self):
        local = START.astimezone(timezone(timedelta(hours=9)))
        self.assertEqual(build_query(topic(), local, END), build_query(topic(), START, END))
        for start, end in [(END, START), (START.replace(tzinfo=None), END)]:
            with self.assertRaises(ValueError):
                build_query(topic(), start, end)

    def test_parse_versions_and_legacy_ids(self):
        papers, total = parse_feed(FEED)
        self.assertEqual(total, 1)
        self.assertEqual(papers[0].id, "2610.00001")
        self.assertEqual(papers[0].version, 2)
        self.assertEqual(papers[0].title, "A paper about reasoning")
        self.assertEqual(papers[0].authors, ("Alice Example",))
        self.assertEqual(papers[0].published.tzinfo, UTC)
        old, _ = parse_feed(FEED.replace(b"2610.00001v2", b"hep-th/9901001"))
        self.assertEqual(old[0].id, "hep-th/9901001")
        self.assertEqual(old[0].version, 1)

    def test_invalid_metadata_and_xml_entities_rejected(self):
        values = [b"<html>oops</html>", b"<feed>",
                  FEED.replace(b"2610.00001v2", b"../../secrets"),
                  FEED.replace(b"2610.00001v2", b"2613.00001v2"),
                  FEED.replace(b"2610.00001v2", b"hep-th/9913001"),
                  FEED.replace(b"http://arxiv.org/abs/2610.00001v2", b"http://arxiv.org/api/errors#bad"),
                  FEED.replace(b"<os:totalResults>1", b"<os:totalResults>-1"),
                  b'<!DOCTYPE feed [<!ENTITY secret SYSTEM "file:///etc/passwd">]><feed>&secret;</feed>']
        for value in values:
            with self.subTest(value=value[:80]), self.assertRaises(ArxivError):
                parse_feed(value)


class FakeContent:
    def __init__(self, data):
        self.data = data

    async def iter_chunked(self, size):
        for i in range(0, len(self.data), size):
            yield self.data[i:i + size]


class FakeResponse:
    def __init__(self, data=FEED, status=200):
        self.status = status
        self.content_length = len(data)
        self.content = FakeContent(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeSession:
    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.starts = []
        self.params = None

    def get(self, url, **kwargs):
        self.starts.append(asyncio.get_running_loop().time())
        self.params = kwargs["params"]
        return self.response


class RetrievalAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_shared_by_concurrent_calls(self):
        session = FakeSession()
        client = ArxivClient(session, min_interval=0.03)
        results = await asyncio.gather(client.fetch_page(topic(), START, END),
                                       client.fetch_page(topic(), START, END))
        self.assertEqual(len(results), 2)
        self.assertGreaterEqual(session.starts[1] - session.starts[0], 0.025)
        self.assertEqual(session.params["sortOrder"], "ascending")

    async def test_failed_and_oversized_responses_raise(self):
        oversized = FakeResponse(data=b"x" * (8 * 1024 * 1024 + 1))
        chunked = FakeResponse(data=b"x" * (8 * 1024 * 1024 + 1))
        chunked.content_length = None
        for response in [FakeResponse(status=429), oversized, chunked]:
            client = ArxivClient(FakeSession(response), min_interval=0)
            with self.assertRaises(ArxivError):
                await client.fetch_page(topic(), START, END)

    async def test_wrong_cached_page_cannot_advance_cursor(self):
        data = FEED.replace(b"<os:totalResults>1", b"<os:totalResults>200")
        client = ArxivClient(FakeSession(FakeResponse(data)), min_interval=0)
        with self.assertRaisesRegex(ArxivError, "分页"):
            await client.fetch_page(topic(), START, END, offset=100)

    async def test_missing_page_index_and_duplicate_entries_are_rejected(self):
        entry = FEED[FEED.index(b"<entry>"):FEED.index(b"</entry>") + len(b"</entry>")]
        duplicate = FEED.replace(b"</feed>", entry + b"</feed>").replace(b"<os:totalResults>1", b"<os:totalResults>2")
        for data in [FEED.replace(b"<os:startIndex>0</os:startIndex>", b""), duplicate]:
            with self.subTest(data=data[:150]), self.assertRaises(ArxivError):
                client = ArxivClient(FakeSession(FakeResponse(data)), min_interval=0)
                await client.fetch_page(topic(), START, END)
