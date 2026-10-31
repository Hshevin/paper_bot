"""SQLite state lives under AstrBot's persistent plugin data directory."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3

from .models import Paper, Screening, Topic
from .errors import SafeError, is_scoring_pause


def encode_paper(paper: Paper) -> str:
    value = asdict(paper)
    for key in ("published", "updated"):
        value[key] = value[key].isoformat()
    return json.dumps(value, ensure_ascii=False)


def decode_paper(raw: str) -> Paper:
    value = json.loads(raw)
    for key in ("published", "updated"):
        value[key] = datetime.fromisoformat(value[key])
    for key in ("authors", "categories"):
        value[key] = tuple(value[key])
    return Paper(**value)


class Storage:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "paperbot.sqlite3", timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS scans (
                topic TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                since TEXT, until TEXT, offset INTEGER NOT NULL DEFAULT 0,
                success TEXT, error TEXT, horizon TEXT
            );
            CREATE TABLE IF NOT EXISTS papers (
                topic TEXT NOT NULL, fingerprint TEXT NOT NULL,
                id TEXT NOT NULL, version INTEGER NOT NULL,
                data TEXT NOT NULL, analysis TEXT, model TEXT,
                error TEXT, excluded INTEGER NOT NULL DEFAULT 0, correction TEXT,
                PRIMARY KEY(topic, fingerprint, id)
            );
            CREATE TABLE IF NOT EXISTS delivered (
                target TEXT NOT NULL, topic TEXT NOT NULL, fingerprint TEXT NOT NULL,
                id TEXT NOT NULL, version INTEGER NOT NULL, sent_at TEXT NOT NULL,
                PRIMARY KEY(target, topic, fingerprint, id, version)
            );
        """)
        # Lock before inspecting the schema so simultaneous opens cannot both
        # attempt the same migration. Existing rows and delivery history survive.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(scans)")}
            if "horizon" not in columns:
                self.db.execute("ALTER TABLE scans ADD COLUMN horizon TEXT")
                self.db.execute("UPDATE scans SET horizon=until")
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(papers)")}
            if "correction" not in columns:
                self.db.execute("ALTER TABLE papers ADD COLUMN correction TEXT")

    def meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    def scan(self, topic: Topic):
        return self.db.execute("SELECT * FROM scans WHERE topic=? AND fingerprint=?", (topic.id, topic.fingerprint)).fetchone()

    def begin_scan(self, topic: Topic, since: datetime, until: datetime):
        old = self.scan(topic)
        with self.db:
            self.db.execute("""INSERT OR REPLACE INTO scans
                (topic,fingerprint,since,until,offset,success,error,horizon) VALUES (?,?,?,?,0,?,NULL,?)""",
                (topic.id, topic.fingerprint, since.isoformat(), until.isoformat(), old["success"] if old else None, until.isoformat()))

    def shrink_scan(self, topic: Topic):
        scan = self.scan(topic)
        start, end = datetime.fromisoformat(scan["since"]), datetime.fromisoformat(scan["until"])
        middle = (start + (end - start) / 2).replace(second=0, microsecond=0)
        if middle <= start or end - start < timedelta(minutes=2):
            raise SafeError("单分钟结果超过 arXiv 上限，请缩小主题分类")
        with self.db:
            self.db.execute("UPDATE scans SET until=?,offset=0,error=NULL WHERE topic=?", (middle.isoformat(), topic.id))

    def accept_page(self, topic: Topic, papers: list[Paper], offset: int, done: bool, scan_id: str | None = None):
        # The rows and pagination cursor commit together, including after process crashes.
        with self.db:
            for paper in papers:
                self.db.execute("""INSERT INTO papers(topic,fingerprint,id,version,data) VALUES (?,?,?,?,?)
                    ON CONFLICT(topic,fingerprint,id) DO UPDATE SET
                    version=excluded.version,data=excluded.data,analysis=NULL,model=NULL,error=NULL,excluded=0,correction=NULL
                    WHERE excluded.version > papers.version""",
                    (topic.id, topic.fingerprint, paper.id, paper.version, encode_paper(paper)))
            if done:
                key = scan_id or topic.id
                scan = self.db.execute("SELECT * FROM scans WHERE topic=?", (key,)).fetchone()
                if scan["horizon"] and scan["until"] < scan["horizon"]:
                    # Inclusive minute boundaries deliberately overlap; paper upserts deduplicate.
                    self.db.execute("UPDATE scans SET since=until,until=horizon,offset=0,error=NULL WHERE topic=?", (key,))
                else:
                    self.db.execute("UPDATE scans SET success=until,since=NULL,until=NULL,horizon=NULL,offset=0,error=NULL WHERE topic=?", (key,))
            else:
                self.db.execute("UPDATE scans SET offset=?,error=NULL WHERE topic=?", (offset, scan_id or topic.id))

    def scan_error(self, topic: Topic, message: str):
        with self.db:
            self.db.execute("UPDATE scans SET error=? WHERE topic=?", (message, topic.id))

    def pending(self, topic: Topic, limit: int):
        return self.db.execute("""SELECT * FROM papers WHERE topic=? AND fingerprint=?
            AND analysis IS NULL AND excluded=0 AND error IS NULL ORDER BY rowid ASC LIMIT ?""",
            (topic.id, topic.fingerprint, limit)).fetchall()

    def correction_count(self, topics: list[Topic]) -> int:
        return sum(self.db.execute("""SELECT COUNT(*) FROM papers
            WHERE topic=? AND fingerprint=? AND analysis IS NULL AND excluded=0
            AND error IS NULL AND correction IS NOT NULL""", (topic.id, topic.fingerprint)).fetchone()[0]
            for topic in topics)

    def result(self, topic: Topic, paper: Paper, analysis: Screening | None, model: str = "", error: str | None = None, excluded=False):
        with self.db:
            self.db.execute("""UPDATE papers SET analysis=?,model=?,error=?,excluded=?,correction=NULL
                WHERE topic=? AND fingerprint=? AND id=? AND version=?""",
                (json.dumps(asdict(analysis), ensure_ascii=False) if analysis else None, model, error, int(excluded),
                 topic.id, topic.fingerprint, paper.id, paper.version))

    def defer_correction(self, topic: Topic, paper: Paper, message: str):
        """Persist one correction opportunity; message must be application-authored."""
        if not isinstance(message, str) or not message.strip() or len(message) > 2000:
            raise ValueError("纠正提示必须是非空文本且不超过 2000 字符")
        with self.db:
            self.db.execute("""UPDATE papers SET correction=?,error=NULL
                WHERE topic=? AND fingerprint=? AND id=? AND version=? AND analysis IS NULL""",
                (message, topic.id, topic.fingerprint, paper.id, paper.version))

    def resume_legacy_scoring_pause(self) -> bool:
        """Recover only a confirmed old validation pause, leaving provider failures alone."""
        with self.db:
            # Read and update the pause under the same SQLite write lock. A
            # concurrent worker must not replace it between inspection/deletion.
            self.db.execute("BEGIN IMMEDIATE")
            pause = json.loads(self.meta("analysis_pause", "null"))
            if pause is None:
                return False
            if not isinstance(pause, dict):
                raise ValueError("模型暂停记录必须是 JSON 对象")
            error = pause.get("error")
            if not is_scoring_pause(pause):
                return False
            if isinstance(error, str):
                self.db.execute("""UPDATE papers SET error=NULL,correction=NULL
                    WHERE analysis IS NULL AND error=?""", (error,))
            self.db.execute("DELETE FROM meta WHERE key='analysis_pause'")
            return True

    def recommendations(self, topic: Topic, target: str, limit: int):
        rows = self.db.execute("""SELECT p.* FROM papers p WHERE topic=? AND fingerprint=?
            AND analysis IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM delivered d WHERE d.target=?
                AND d.id=p.id AND d.version=p.version)""", (topic.id, topic.fingerprint, target)).fetchall()
        accepted = [r for r in rows if json.loads(r["analysis"])["score"] >= topic.threshold]
        return sorted(accepted, key=lambda r: (json.loads(r["analysis"])["score"], decode_paper(r["data"]).published), reverse=True)[:limit]

    def mark_sent(self, topic: Topic, target: str, paper: Paper, now: datetime):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO delivered VALUES (?,?,?,?,?,?)", (target, topic.id, topic.fingerprint, paper.id, paper.version, now.isoformat()))

    def retry(self):
        with self.db:
            self.db.execute("UPDATE papers SET error=NULL,correction=NULL WHERE analysis IS NULL")
            self.db.execute("DELETE FROM meta WHERE key='analysis_pause'")

    def close(self):
        self.db.close()
