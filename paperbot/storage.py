"""SQLite state lives under AstrBot's persistent plugin data directory."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3

from .models import Paper, Screening, Topic


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
                error TEXT, excluded INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(topic, fingerprint, id)
            );
            CREATE TABLE IF NOT EXISTS delivered (
                target TEXT NOT NULL, topic TEXT NOT NULL, fingerprint TEXT NOT NULL,
                id TEXT NOT NULL, version INTEGER NOT NULL, sent_at TEXT NOT NULL,
                PRIMARY KEY(target, topic, fingerprint, id, version)
            );
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(scans)")}
        if "horizon" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE scans ADD COLUMN horizon TEXT")
                self.db.execute("UPDATE scans SET horizon=until")

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
            raise ValueError("单分钟结果超过 arXiv 上限，请缩小主题分类")
        with self.db:
            self.db.execute("UPDATE scans SET until=?,offset=0,error=NULL WHERE topic=?", (middle.isoformat(), topic.id))

    def accept_page(self, topic: Topic, papers: list[Paper], offset: int, done: bool, scan_id: str | None = None):
        # The rows and pagination cursor commit together, including after process crashes.
        with self.db:
            for paper in papers:
                self.db.execute("""INSERT INTO papers(topic,fingerprint,id,version,data) VALUES (?,?,?,?,?)
                    ON CONFLICT(topic,fingerprint,id) DO UPDATE SET
                    version=excluded.version,data=excluded.data,analysis=NULL,model=NULL,error=NULL,excluded=0
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

    def result(self, topic: Topic, paper: Paper, analysis: Screening | None, model: str = "", error: str | None = None, excluded=False):
        with self.db:
            self.db.execute("""UPDATE papers SET analysis=?,model=?,error=?,excluded=?
                WHERE topic=? AND fingerprint=? AND id=? AND version=?""",
                (json.dumps(asdict(analysis), ensure_ascii=False) if analysis else None, model, error, int(excluded),
                 topic.id, topic.fingerprint, paper.id, paper.version))

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
            self.db.execute("UPDATE papers SET error=NULL WHERE analysis IS NULL")
            self.db.execute("DELETE FROM meta WHERE key='analysis_pause'")

    def close(self):
        self.db.close()
