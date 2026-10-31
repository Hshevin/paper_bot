from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re

from .errors import ConfigError


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
        if not isinstance(data, dict):
            raise ConfigError("每个研究主题必须是配置对象；例如在主题列表中新增一项并填写 id、名称和研究描述", field="topics")

        def required_text(key: str, label: str, example: str) -> str:
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{label}必须填写非空文本；例如：{example}", field=key)
            return value.strip()

        ident = required_text("id", "主题 ID", "llm_infra")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ident):
            raise ConfigError("主题 ID 只能包含 1–64 个英文字母、数字、下划线或连字符；例如：llm_infra", field="id")
        name = required_text("name", "主题名称", "大模型基础设施")
        description = required_text("description", "研究描述", "关注大模型推理加速、部署与资源调度的最新进展")

        def words(key: str, label: str, example: str) -> tuple[str, ...]:
            value = data.get(key, [])
            if isinstance(value, str):
                value = value.split(",")
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ConfigError(f"{label}必须是文本列表或用英文逗号分隔的文本；例如：{example}", field=key)
            return tuple(dict.fromkeys(x.strip() for x in value if x.strip()))

        categories = words("categories", "arXiv 分类", '["cs.AI", "cs.CV"]')
        # Normalize only whitespace next to dots; never accept query operators.
        categories = tuple(dict.fromkeys(re.sub(r"\s*\.\s*", ".", value) for value in categories))
        category_pattern = r"[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][A-Za-z0-9-]*)*(?:\.\*)?"
        if any(not re.fullmatch(category_pattern, value) for value in categories):
            raise ConfigError("arXiv 分类格式无效，不能填写查询表达式；例如：cs.AI、cs.CV、cs.CL 或 cs.*", field="categories")
        keywords = words("keywords", "关键词", '["computer vision", "llm infra"]')
        if not categories and not keywords:
            raise ConfigError("至少填写一个 arXiv 分类或关键词；例如分类填写 cs.AI，或关键词填写 llm infra", field="categories")
        if any('"' in x or '\\' in x for x in keywords):
            raise ConfigError("关键词不能包含双引号或反斜杠，请直接填写词语；例如：llm infra", field="keywords")
        exclusions = words("exclude_keywords", "排除关键词", '["robotics", "biology"]')
        threshold = data.get("threshold", 70)
        if type(threshold) is not int or not 0 <= threshold <= 100:
            raise ConfigError("推荐阈值必须是 0–100 的整数，不能填写文本或布尔值；例如：70", field="threshold")
        mode = data.get("retrieval_mode", "broad")
        if not isinstance(mode, str) or mode not in ("broad", "strict"):
            raise ConfigError("检索模式只能选择 broad（宽泛）或 strict（严格）；例如：broad", field="retrieval_mode")
        enabled = data.get("enabled", True)
        if type(enabled) is not bool:
            raise ConfigError("启用状态必须是布尔开关，不能填写文本或数字；例如：true", field="enabled")
        return cls(ident, name, description, categories, keywords, exclusions, threshold, mode, enabled)


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
