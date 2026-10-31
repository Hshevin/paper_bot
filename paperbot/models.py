from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re


@dataclass(frozen=True)
class Topic:
    id: str
    name: str
    description: str
    categories: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    threshold: int = 70
    retrieval_mode: str = "broad"
    enabled: bool = True

    @property
    def fingerprint(self) -> str:
        # Threshold changes don't require another paid model call.
        value = (self.name, self.description, self.categories, self.keywords,
                 self.exclude_keywords, self.retrieval_mode)
        return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()

    @classmethod
    def from_dict(cls, data: dict) -> Topic:
        ident = str(data.get("id", "")).strip()
        name = str(data.get("name", "")).strip()
        description = str(data.get("description", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ident) or not name or not description:
            raise ValueError("主题需有唯一英文 id、名称和详细研究描述")
        def words(key):
            value = data.get(key, [])
            if isinstance(value, str):
                value = value.split(",")
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ValueError(f"{key} 必须是字符串列表")
            return tuple(dict.fromkeys(x.strip() for x in value if x.strip()))
        categories, keywords = words("categories"), words("keywords")
        if not categories and not keywords:
            raise ValueError("主题至少填写分类或关键词")
        if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]*(?:\.\*)?", x) for x in categories):
            raise ValueError("arXiv 分类格式错误")
        if any('"' in x or '\\' in x for x in keywords):
            raise ValueError("关键词不能包含引号或反斜杠")
        threshold = data.get("threshold", 70)
        if isinstance(threshold, bool) or not isinstance(threshold, int) or not 0 <= threshold <= 100:
            raise ValueError("推荐阈值须为 0–100 的整数")
        mode = data.get("retrieval_mode", "broad")
        if mode not in ("broad", "strict"):
            raise ValueError("retrieval_mode 必须为 broad 或 strict")
        return cls(ident, name, description, categories, keywords, words("exclude_keywords"), threshold, mode, bool(data.get("enabled", True)))


@dataclass(frozen=True)
class Paper:
    id: str
    version: int
    title: str
    abstract: str
    authors: tuple[str, ...]
    categories: tuple[str, ...]
    published: datetime
    updated: datetime

    @property
    def url(self) -> str:
        return f"https://arxiv.org/abs/{self.id}v{self.version}"


@dataclass(frozen=True)
class Screening:
    score: int
    summary: str
    reason: str
    evidence: tuple[str, ...] = ()
    matched_keywords: tuple[str, ...] = ()
