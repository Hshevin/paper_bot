from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Awaitable, Callable

import aiohttp

from .arxiv import ArxivClient
from .errors import ConfigError, SafeError, diagnose, public_error
from .models import Paper, Topic
from .scoring import eligible, screen
from .storage import Storage, decode_paper

UTC = timezone.utc


def validate_target(target: str) -> str:
    # The prefix is the adapter INSTANCE id, not the platform type name.
    if not isinstance(target, str) or not re.fullmatch(r"[^:\s]{1,100}:(?:GroupMessage|FriendMessage):[0-9]{1,30}", target):
        raise ConfigError("目标格式应为 适配器实例ID:GroupMessage:群号 或 适配器实例ID:FriendMessage:QQ号；请用 /paper_target 获取", field="target")
    return target


def bounded(config: dict, key: str, default: int, low: int, high: int) -> int:
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{key} 必须为 {low}–{high} 的整数；请在 WebUI 对应数值框修改", field=key)
    return value


class PaperService:
    def __init__(self, data_dir: Path, config: dict,
                 generate: Callable[[str, str], Awaitable[str]],
                 send: Callable[[str, str], Awaitable[bool]], *, reader_generate=None, save_config=None, report_error=None, client=None, clock=None):
        self.config = config
        self.data_dir = data_dir
        self.store = Storage(data_dir)
        self.generate = generate
        self.reader_generate = reader_generate or generate
        self.save_config = save_config
        self.report_error = report_error
        self.send = send
        self.client = client
        self.session = None
        self.lock = asyncio.Lock()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.closed = False
        self._phase = "配置校验"

    def _failure(self, exc: Exception, stage: str, context: str = "") -> str:
        report = diagnose(exc, stage, context=context)
        report["time"] = self.clock().isoformat()
        if self.report_error is not None:
            self.report_error(report)
        # A broken store must not hide the original diagnostic or get reset.
        try:
            self.store.set_meta("last_error", json.dumps(report, ensure_ascii=False))
        except (sqlite3.Error, OSError):
            pass
        return public_error(report)

    def _topics(self) -> list[Topic]:
        raw = self.config.get("topics", [])
        if not isinstance(raw, list):
            raise ConfigError("研究主题必须是列表；请在 WebUI 的共享研究主题目录中添加主题", field="topics")
        topics = []
        for index, item in enumerate(raw):
            if isinstance(item, dict) and item.get("enabled") is False:
                continue
            try:
                topic = Topic.from_dict(item)
                if any(t.id == topic.id for t in topics):
                    raise ConfigError("主题 ID 重复，请为每个启用的主题设置不同 ID", field="id")
            except ConfigError as exc:
                field = f"topics[{index}]" + (f".{exc.field}" if exc.field != "topics" else "")
                raise ConfigError(f"研究主题第 {index + 1} 项：{exc}", field=field) from exc
            topics.append(topic)
        return topics

    def _subscriptions(self) -> list[dict]:
        raw = self.config.get("subscriptions", [])
        if not isinstance(raw, list):
            raise ConfigError("会话订阅必须是列表；请在 WebUI 新建群聊或私聊订阅", field="subscriptions")
        known = {t.id for t in self._topics()}
        all_ids = {t["id"].strip() for t in self.config.get("topics", []) if isinstance(t, dict) and isinstance(t.get("id"), str)}
        result = []
        seen = set()
        default_interval = bounded(self.config, "interval_minutes", 60, 15, 1440)
        for index, item in enumerate(raw):
            try:
                if not isinstance(item, dict):
                    raise ConfigError("订阅必须是配置对象，请在 WebUI 新建会话订阅")
                if type(item.get("enabled", True)) is not bool:
                    raise ConfigError("启用状态必须是布尔开关", field="enabled")
                if not item.get("enabled", True):
                    continue
                target = validate_target(item.get("target"))
                if target in seen:
                    raise ConfigError("同一目标只能有一条启用的订阅，请合并主题 ID 列表", field="target")
                seen.add(target)
                ids = item.get("topic_ids", [])
                if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in all_ids for i in ids):
                    raise ConfigError("主题 ID 列表为空或引用了不存在的主题；请用 /paper_topics 查看，并填写主题 ID 而非主题名称", field="topic_ids")
                interval = bounded(item, "interval_minutes", default_interval, 15, 1440)
            except ConfigError as exc:
                field = f"subscriptions[{index}]" + (f".{exc.field}" if exc.field else "")
                raise ConfigError(f"会话订阅第 {index + 1} 项：{exc}", field=field) from exc
            active = sorted(set(ids) & known)
            if active:
                result.append({"target": target, "topic_ids": active, "interval_minutes": interval})
        return result

    def _topic_context(self, topic: Topic) -> str:
        for index, item in enumerate(self.config.get("topics", [])):
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip() == topic.id:
                return f"研究主题第 {index + 1} 项（topics[{index}]）"
        return "研究主题"

    def bind(self, umo: str, topic_ids: str, interval_minutes: int = 60):
        validate_target(umo)
        ids = list(dict.fromkeys(x.strip() for x in topic_ids.split(",") if x.strip()))
        known = {t.id for t in self._topics()}
        if not ids or any(i not in known for i in ids):
            return "请指定启用的主题 ID，例如 /paper_bind rag,agents 120；先用 /paper_topics 查看"
        interval = bounded({"interval_minutes": interval_minutes}, "interval_minutes", 60, 15, 1440)
        subscriptions = [dict(s) for s in self.config.get("subscriptions", []) if s["target"] != umo]
        subscriptions.append({"__template_key": "subscription", "target": umo, "topic_ids": ids, "interval_minutes": interval, "enabled": True})
        self._save_subscriptions(subscriptions)
        return f"当前会话已订阅：{', '.join(ids)}；每 {interval} 分钟检查一次，已同步到 WebUI 插件配置。"

    def unbind(self, umo: str):
        validate_target(umo)
        subscriptions = [dict(s, enabled=False) if s["target"] == umo else dict(s) for s in self.config.get("subscriptions", [])]
        self._save_subscriptions(subscriptions)
        return "当前会话已取消订阅；/paper_bind 可重新指定研究方向。"

    def _save_subscriptions(self, subscriptions: list[dict]):
        # WebUI and chat commands share AstrBotConfig; no hidden database overrides.
        previous = self.config.get("subscriptions", [])
        self.config["subscriptions"] = subscriptions
        try:
            if self.save_config is not None:
                self.save_config()
        except Exception:
            self.config["subscriptions"] = previous
            raise

    def subscriptions(self, umo: str) -> str:
        item = next((s for s in self._subscriptions() if s["target"] == umo), None)
        if not item:
            return "当前会话没有启用的订阅；先 /paper_topics，再 /paper_bind 主题ID 60"
        names = {t.id: t.name for t in self._topics()}
        topics = "、".join(f"{names[i]} ({i})" for i in item["topic_ids"])
        return f"当前会话订阅：{topics}\n间隔：{item['interval_minutes']} 分钟\n仅推送达到主题阈值的论文，按相关性取前 {self.config.get('max_push_per_target', 5)} 篇。"

    @staticmethod
    def _schedule_key(subscription: dict) -> str:
        return "subscription_attempt:" + hashlib.sha256(json.dumps(subscription, sort_keys=True).encode()).hexdigest()

    async def _pace_arxiv(self):
        # Coordinate API and PDF requests, including across plugin restarts.
        previous = self.store.meta("arxiv_request_at")
        if previous:
            wait = 3.1 - (datetime.now(UTC) - datetime.fromisoformat(previous)).total_seconds()
            if wait > 0:
                await asyncio.sleep(min(wait, 3.1))
        self.store.set_meta("arxiv_request_at", datetime.now(UTC).isoformat())

    def retry(self):
        if self.lock.locked():
            raise SafeError("任务运行中，请完成后再重试")
        self.store.retry()
        return "已解除模型暂停并恢复失败队列；/paper_run 可立即重试，否则等待下次订阅检查。"

    def topics(self) -> str:
        return "\n".join(f"{t.id} · {t.name} · 阈值 {t.threshold} · {t.retrieval_mode}\n{t.description}" for t in self._topics()) or "未配置主题，请在插件配置中添加。"

    def status(self) -> str:
        pause = json.loads(self.store.meta("analysis_pause", "null"))
        counts = [len(self.config.get(k, [])) if isinstance(self.config.get(k, []), list) else 0 for k in ("topics", "subscriptions")]
        message = (f"自动任务：{'已启用' if self.config.get('enabled', False) else '未启用'}\n"
                f"运行中：{'是' if self.lock.locked() else '否'}\n"
                f"主题配置 {counts[0]} 条，订阅配置 {counts[1]} 条（含停用项）\n"
                f"最近结果：{self.store.meta('last_status', '尚未运行')}\n"
                f"模型筛选：{'已暂停，请检查提供商后 /paper_retry' if pause else '就绪'}")
        last_error = json.loads(self.store.meta("last_error", "null"))
        if last_error and last_error["id"] not in message:
            message += "\n最近一次错误（历史记录）：\n" + public_error(last_error)
        return message

    async def run(self, force=False) -> str:
        if self.closed:
            return "插件已停止"
        if self.lock.locked():
            return "已有任务运行，未重复启动"
        if not force and not self.config.get("enabled", False):
            return "自动任务未启用"
        async with self.lock:
            self._phase = "配置校验"
            try:
                return await self._run(force)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = self._failure(exc, self._phase)
                return message

    async def _run(self, force: bool) -> str:
        all_topics = self._topics()
        subscriptions = self._subscriptions()
        if not all_topics:
            return "请先配置研究主题（包括详细研究描述）"
        if not subscriptions:
            return "请先为目标配置主题订阅，或在目标会话执行 /paper_bind 主题ID 60"
        provider = str(self.config.get("provider_id", "")).strip()
        if not provider:
            return "请先在插件配置中选择 AstrBot 聊天模型提供商 provider_id"
        lookback = bounded(self.config, "lookback_days", 7, 1, 365)
        pages_budget = bounded(self.config, "max_pages_per_run", 3, 1, 30)
        analysis_budget = bounded(self.config, "max_analyses_per_run", 20, 1, 200)
        push_limit = bounded(self.config, "max_push_per_target", 5, 1, 50)
        self._phase = "读取任务进度"
        now = self.clock()
        due = []
        for subscription in subscriptions:
            last = self.store.meta(self._schedule_key(subscription))
            if force or not last or now - datetime.fromisoformat(last) >= timedelta(minutes=subscription["interval_minutes"]):
                due.append(subscription)
        if not due:
            return "尚无到期订阅"
        for subscription in due:
            self.store.set_meta(self._schedule_key(subscription), now.isoformat())
        needed = {i for s in due for i in s["topic_ids"]}
        topics = [t for t in all_topics if t.id in needed]
        if self.client is None:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
            self.client = ArxivClient(self.session)
        fetched = analyzed = sent = failed = 0
        failures = []
        # Rotate topics across bounded rounds so a large first topic cannot starve later ones.
        rotation = int(self.store.meta("rotation", "0")) % len(topics)
        ordered = topics[rotation:] + topics[:rotation]
        self.store.set_meta("rotation", str((rotation + 1) % len(topics)))
        for topic in ordered:
            self._phase = "arXiv 抓取"
            scan = self.store.scan(topic)
            if not scan or not scan["until"]:
                since = datetime.fromisoformat(scan["success"]) - timedelta(days=7) if scan and scan["success"] else now - timedelta(days=lookback)
                self.store.begin_scan(topic, since, now)
            for _ in range(pages_budget):
                scan = self.store.scan(topic)
                if not scan["until"]:
                    break
                try:
                    await self._pace_arxiv()
                    papers, total = await self.client.fetch_page(topic, datetime.fromisoformat(scan["since"]), datetime.fromisoformat(scan["until"]), scan["offset"])
                    offset = scan["offset"] + len(papers)
                    if not papers and offset < total:
                        raise SafeError("arXiv 返回空页但仍有待抓取记录；进度未推进，请稍后重试")
                    if total > 30000:
                        self.store.shrink_scan(topic)
                        continue
                    self.store.accept_page(topic, papers, offset, offset >= total)
                    fetched += len(papers)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failed += 1
                    detail = self._failure(exc, "arXiv 抓取", f"{self._topic_context(topic)}，分页偏移 {scan['offset']}")
                    failures.append(detail)
                    self.store.scan_error(topic, detail)
                    break
        self._phase = "模型筛选"
        signature = hashlib.sha256((provider + "|" + "|".join(sorted(t.fingerprint for t in all_topics))).encode()).hexdigest()
        pause = json.loads(self.store.meta("analysis_pause", "null"))
        paused = pause is not None and pause["signature"] == signature
        if pause and not paused:
            self.store.retry()
        if not paused:
            for topic in ordered:
                if analyzed >= analysis_budget or paused:
                    break
                # Include excluded candidates without spending the LLM budget; bound CPU work.
                for row in self.store.pending(topic, max(analysis_budget * 10, 100)):
                    paper = decode_paper(row["data"])
                    if not eligible(paper, topic):
                        self.store.result(topic, paper, None, excluded=True)
                        continue
                    if analyzed >= analysis_budget:
                        break
                    analyzed += 1
                    try:
                        result = await screen(paper, topic, self.generate)
                        self.store.result(topic, paper, result, model=provider)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        failed += 1
                        detail = self._failure(exc, "模型筛选", self._topic_context(topic))
                        failures.append(detail)
                        self.store.result(topic, paper, None, error=detail)
                        self.store.set_meta("analysis_pause", json.dumps({"signature": signature, "error": detail}))
                        paused = True
                        break
        for subscription in due:
            self._phase = "QQ 推送"
            target = subscription["target"]
            candidates = []
            for topic in topics:
                if topic.id in subscription["topic_ids"]:
                    for row in self.store.recommendations(topic, target, push_limit):
                        candidates.append((topic, decode_paper(row["data"]), json.loads(row["analysis"])))
            candidates.sort(key=lambda x: (x[2]["score"], x[1].published), reverse=True)
            target_sent = 0
            seen = set()
            for topic, paper, result in candidates:
                if target_sent >= push_limit:
                    break
                if (paper.id, paper.version) in seen:
                    continue
                current = next((s for s in self._subscriptions() if s["target"] == target), None)
                if not current:
                    break
                if topic.id not in current["topic_ids"]:
                    continue
                try:
                    accepted = await asyncio.wait_for(self.send(target, format_paper(topic, paper, result)), timeout=60)
                    if accepted is not True:
                        raise SafeError("消息平台未接受推送，可能未找到目标的平台实例；请确认 OneBot 已连接，并用 /paper_target 核对完整会话标识")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failed += 1
                    failures.append(self._failure(exc, "QQ 推送", f"本轮订阅第 {due.index(subscription) + 1} 项"))
                    break
                self.store.mark_sent(topic, target, paper, self.clock())
                seen.add((paper.id, paper.version))
                sent += 1; target_sent += 1
                await asyncio.sleep(1)
        message = f"本轮抓取 {fetched} 条，分析 {analyzed} 篇，成功投递 {sent} 条，失败 {failed} 项"
        if paused:
            message += "；模型筛选已暂停，请修复后 /paper_retry"
        if failures:
            message += "\n\n" + "\n\n".join(failures[:3])
            if len(failures) > 3:
                message += "\n其余错误请按诊断编号查看 Paper Bot 插件日志。"
        self.store.set_meta("last_status", message)
        return message

    async def fetch_range(self, topic_id: str, start_date: str, end_date: str) -> str:
        if self.closed:
            return "插件已停止"
        if self.lock.locked():
            return "已有任务运行，请稍后重试"
        async with self.lock:
            topic = next((t for t in self._topics() if t.id == topic_id), None)
            if topic is None:
                return "未找到启用的主题 id，请 /paper_topics 查看"
            try:
                start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=UTC)
                end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1) - timedelta(seconds=1)
            except ValueError:
                return "日期格式应为 YYYY-MM-DD（UTC）"
            if start > end or start > self.clock():
                return "日期范围无效"
            end = min(end, self.clock())
            key = hashlib.sha256(f"{topic.id}:{start_date}:{end_date}".encode()).hexdigest()[:24]
            manual = replace(topic, id="manual_" + key)
            scan = self.store.scan(manual)
            if scan and scan["success"] and not scan["until"]:
                completed = datetime.fromisoformat(scan["success"])
                if completed >= end:
                    return "这个日期范围已收集完成；执行 /paper_run 处理待分析论文"
                # A request ending today may have completed only up to an earlier clock time.
                self.store.begin_scan(manual, max(start, completed - timedelta(days=1)), end)
            if not scan:
                self.store.begin_scan(manual, start, end)
            if self.client is None:
                self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
                self.client = ArxivClient(self.session)
            count = 0
            for _ in range(bounded(self.config, "max_pages_per_run", 3, 1, 30)):
                scan = self.store.scan(manual)
                try:
                    await self._pace_arxiv()
                    papers, total = await self.client.fetch_page(topic, datetime.fromisoformat(scan["since"]), datetime.fromisoformat(scan["until"]), scan["offset"])
                    offset = scan["offset"] + len(papers)
                    if total > 30000:
                        self.store.shrink_scan(manual)
                        continue
                    if not papers and offset < total:
                        return "分页不完整，请稍后重试；已保存进度"
                    self.store.accept_page(topic, papers, offset, offset >= total, scan_id=manual.id)
                    count += len(papers)
                    if not self.store.scan(manual)["until"]:
                        return f"日期范围收集完成，本轮 {count} 条；执行 /paper_run 分析并推送推荐论文"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    return self._failure(exc, "日期范围抓取", f"分页偏移 {scan['offset']}") + "\n重复同一命令可续传。"
            return f"本轮收集 {count} 条；尚有分页，重复同一命令继续。已收集论文可用 /paper_run 分析"

    async def read(self, paper_id: str) -> str:
        if self.closed:
            return "插件已停止"
        if self.lock.locked():
            return "已有任务运行，请稍后重试"
        if not self.config.get("reading_provider_id") and not self.config.get("provider_id"):
            return "请先配置模型提供商"
        async with self.lock:
            row = self.store.db.execute("SELECT data FROM papers WHERE id=? ORDER BY version DESC LIMIT 1", (paper_id,)).fetchone()
            if row is None:
                return "该论文未入库，请使用已抓取论文的 arXiv ID（不含 v 版本后缀）"
            from .reading import read_paper
            paper = decode_paper(row["data"])
            try:
                await self._pace_arxiv()
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
                    report = await read_paper(paper, self.data_dir, session, self.reader_generate)
                self.store.set_meta("full:" + paper.id, json.dumps({"version": paper.version, "report": report, "model": self.config.get("reading_provider_id") or self.config.get("provider_id"), "at": self.clock().isoformat()}, ensure_ascii=False))
                return f"【全文精读】{paper.title}\n{paper.url}\n\n{report}"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return self._failure(exc, "全文精读")

    async def close(self):
        if self.closed:
            return
        self.closed = True
        async with self.lock:
            if self.session:
                await self.session.close()
            self.store.close()


def format_paper(topic: Topic, paper: Paper, result: dict) -> str:
    # Plain text only: no model-produced URL or executable message component.
    evidence = "；".join(result.get("evidence", []))[:350] or "模型未提供原文摘录"
    keywords = "、".join(result.get("matched_keywords", []))[:150] or "无字面命中（按研究描述评估）"
    return (f"【论文推荐 · {topic.name[:80]}】\n{paper.title[:260]}\n"
            f"相关性 {result['score']}/100 · {paper.published.date()} · v{paper.version}\n"
            f"摘要：{result['summary'][:650]}\n推荐理由：{result['reason'][:400]}\n"
            f"关键词：{keywords}\n原文依据：{evidence}\n{paper.url}\n"
            "基于标题与摘要的模型判断，请核对原文。")
