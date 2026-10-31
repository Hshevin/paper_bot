"""arXiv Atom API with UTC windows and serialized request pacing.

Query syntax: https://info.arxiv.org/help/api/user-manual.html#51-details-of-query-construction
The client is shared by all plugin topics; keep one instance per running plugin.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import re
from urllib.parse import urlsplit

import aiohttp
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from xml.etree.ElementTree import ParseError

from .models import Paper, Topic
from .errors import SafeError

API_URL = "https://export.arxiv.org/api/query"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
ATOM = "{http://www.w3.org/2005/Atom}"
OPEN_SEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
ID_PATTERN = re.compile(
    r"(?P<id>\d{2}(?:0[1-9]|1[0-2])\.\d{4,5}|"
    r"[A-Za-z][A-Za-z.-]*/\d{2}(?:0[1-9]|1[0-2])\d{3})(?:v(?P<version>[1-9]\d*))?"
)


class ArxivError(SafeError):
    """A safe user-facing failure without remote response bodies."""


def build_query(topic: Topic, start: datetime, end: datetime) -> str:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("日期必须包含时区")
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    if topic.retrieval_mode not in ("broad", "strict"):
        raise ValueError("未知检索模式")
    clauses: list[str] = []
    if topic.categories:
        if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]*(?:\.\*)?", value) for value in topic.categories):
            raise ValueError("arXiv 分类格式错误")
        clauses.append("(" + " OR ".join(f"cat:{value}" for value in topic.categories) + ")")
    # Broad retrieval deliberately leaves keywords to local evidence/LLM scoring.
    # With no category restriction, keywords are required to bound retrieval.
    if topic.keywords and (topic.retrieval_mode == "strict" or not topic.categories):
        if any(not value.strip() or '"' in value or "\\" in value for value in topic.keywords):
            raise ValueError("关键词不能为空或包含引号、反斜杠")
        clauses.append("(" + " OR ".join(f'all:"{value}"' for value in topic.keywords) + ")")
    if not clauses:
        raise ValueError("必须提供 arXiv 分类或关键词以限定检索范围")
    begin = start.astimezone(timezone.utc).strftime("%Y%m%d%H%M")
    finish = end.astimezone(timezone.utc).strftime("%Y%m%d%H%M")
    clauses.append(f"submittedDate:[{begin} TO {finish}]")
    return " AND ".join(clauses)


def _text(element, name: str) -> str:
    child = element.find(ATOM + name)
    return " ".join("".join(child.itertext()).split()) if child is not None else ""


def _date(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("missing timezone")
        return result.astimezone(timezone.utc)
    except ValueError as exc:
        raise ArxivError("arXiv 论文日期字段无效") from exc


def parse_feed(data: bytes, *, expected_offset: int | None = None) -> tuple[list[Paper], int]:
    if len(data) > MAX_RESPONSE_BYTES:
        raise ArxivError("arXiv 响应超过 8 MiB 上限")
    try:
        root = ElementTree.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ParseError, DefusedXmlException, ValueError) as exc:
        raise ArxivError("arXiv 返回了无效或不安全的 XML") from exc
    if root.tag != ATOM + "feed":
        raise ArxivError("arXiv 返回的内容不是 Atom 论文列表")
    total_text = root.findtext(OPEN_SEARCH + "totalResults", default="")
    try:
        total = int(total_text)
    except (ValueError, TypeError) as exc:
        raise ArxivError("arXiv 响应缺少有效的总数") from exc
    if total < 0:
        raise ArxivError("arXiv 响应总数不能为负")
    if expected_offset is not None:
        try:
            actual_offset = int(root.findtext(OPEN_SEARCH + "startIndex", default=""))
        except (ValueError, TypeError) as exc:
            raise ArxivError("arXiv 响应缺少有效的分页位置") from exc
        if actual_offset != expected_offset:
            raise ArxivError("arXiv 返回了错误的分页，不能推进成功游标")
    papers: list[Paper] = []
    seen: set[str] = set()
    for entry in root.findall(ATOM + "entry"):
        parsed_url = urlsplit(_text(entry, "id"))
        if parsed_url.scheme not in ("http", "https") or parsed_url.hostname not in ("arxiv.org", "www.arxiv.org") or not parsed_url.path.startswith("/abs/"):
            raise ArxivError("arXiv 查询被拒绝或论文标识无效")
        match = ID_PATTERN.fullmatch(parsed_url.path.removeprefix("/abs/"))
        if match is None or parsed_url.query or parsed_url.fragment:
            raise ArxivError("arXiv 论文标识无效")
        if match["id"] in seen:
            raise ArxivError("arXiv 分页包含重复论文，不能推进成功游标")
        seen.add(match["id"])
        title, abstract = _text(entry, "title"), _text(entry, "summary")
        if not title or not abstract or len(title) > 3000 or len(abstract) > 60000:
            raise ArxivError("arXiv 论文缺少标题/摘要或字段异常过长")
        papers.append(Paper(
            id=match["id"], version=int(match["version"] or 1),
            title=title, abstract=abstract,
            authors=tuple(_text(author, "name") for author in entry.findall(ATOM + "author") if _text(author, "name")),
            categories=tuple(dict.fromkeys(tag.get("term") for tag in entry.findall(ATOM + "category") if tag.get("term"))),
            published=_date(_text(entry, "published")), updated=_date(_text(entry, "updated")),
        ))
    if len(papers) > total:
        raise ArxivError("arXiv 响应的论文数量与总数不一致")
    return papers, total


class ArxivClient:
    def __init__(self, session: aiohttp.ClientSession, *, min_interval: float = 3.1):
        if min_interval < 0:
            raise ValueError("请求间隔不能为负")
        self.session = session
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last_request: float | None = None

    async def fetch_page(self, topic: Topic, start: datetime, end: datetime,
                         offset: int = 0, page_size: int = 100) -> tuple[list[Paper], int]:
        if type(offset) is not int or not 0 <= offset < 30000:
            raise ValueError("arXiv 分页偏移须为 0–29999")
        if type(page_size) is not int or not 1 <= page_size <= 2000:
            raise ValueError("每页数量须为 1–2000")
        params = {"search_query": build_query(topic, start, end), "start": offset,
                  "max_results": min(page_size, 30000 - offset), "sortBy": "submittedDate", "sortOrder": "ascending"}
        async with self._lock:
            loop = asyncio.get_running_loop()
            if self._last_request is not None:
                delay = self.min_interval - (loop.time() - self._last_request)
                if delay > 0:
                    await asyncio.sleep(delay)
            self._last_request = loop.time()
            try:
                async with self.session.get(API_URL, params=params, allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=90, connect=15),
                        headers={"User-Agent": "paper_bot/1.0 (+https://github.com/Hshevin/paper_bot)"}) as response:
                    if response.status != 200:
                        raise ArxivError(f"arXiv 请求失败（HTTP {response.status}），已保留进度，稍后重试")
                    if response.content_length is not None and response.content_length > MAX_RESPONSE_BYTES:
                        raise ArxivError("arXiv 响应超过 8 MiB 上限")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE_BYTES:
                            raise ArxivError("arXiv 响应超过 8 MiB 上限")
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise ArxivError("arXiv 网络请求失败或超时，已保留进度，稍后重试") from exc
        papers, total = parse_feed(bytes(data), expected_offset=offset)
        if papers and (len(papers) > params["max_results"] or offset + len(papers) > total):
            raise ArxivError("arXiv 分页大小与请求或总数不一致")
        if not papers and offset < total:
            raise ArxivError("arXiv 分页暂不完整，不能推进成功游标")
        return papers, total
