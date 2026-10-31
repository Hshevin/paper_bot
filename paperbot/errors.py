"""Useful diagnostics without provider bodies, request URLs, keys or locals."""
from __future__ import annotations

import builtins
from pathlib import Path
import re
import secrets
import traceback
import unicodedata


class SafeError(ValueError):
    """Only raise with application-authored text; never wrap raw exception text."""

    def __init__(self, message: str, *, hint: str = ""):
        super().__init__(message)
        self.hint = hint


class ConfigError(SafeError):
    def __init__(self, message: str, *, field: str = ""):
        super().__init__(message, hint="在 AstrBot WebUI 的 Paper Bot 插件配置中修正该字段，然后重试。")
        self.field = field


class ExternalError(SafeError):
    pass


def _attribute(value, name):
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _status(exc: Exception) -> int | None:
    response = _attribute(exc, "response")
    for value in (_attribute(exc, "status_code"), _attribute(exc, "status"),
                  _attribute(response, "status_code"), _attribute(response, "status")):
        if isinstance(value, str) and re.fullmatch(r"[1-5][0-9]{2}", value):
            value = int(value)
        if type(value) is int and 100 <= value <= 599:
            return value
    return None


def external_error(exc: Exception, service: str) -> SafeError:
    service = service if service in ("模型", "QQ") else "外部服务"
    status = _status(exc)
    advice = {
        401: ("认证失败", "检查模型提供商的 API 密钥是否正确或已失效。" if service == "模型" else "检查 OneBot 连接与访问令牌。"),
        403: ("访问被拒绝", "检查模型权限、账户额度及服务商访问限制。" if service == "模型" else "检查机器人是否在目标群内，以及账号和群聊发言权限。"),
        404: ("接口或目标不存在", "检查提供商 API 地址和模型名称。" if service == "模型" else "检查机器人实例 ID、群号或 QQ 号。"),
        429: ("请求受限或额度不足", "等待服务限流恢复，并检查账户额度、调用频率和提供商配额。"),
    }
    if status in advice:
        reason, hint = advice[status]
        return ExternalError(f"{service}{reason}（HTTP {status}）", hint=hint)
    if status is not None:
        reason = "服务暂时异常" if status >= 500 else "请求被拒绝"
        return ExternalError(f"{service}{reason}（HTTP {status}）", hint="稍后重试；若持续失败，请检查对应服务状态和连接配置。")
    name = type(exc).__name__
    if isinstance(exc, TimeoutError) or name in {"APITimeoutError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "ServerTimeoutError"}:
        return ExternalError(f"{service}请求超时", hint="检查网络、代理和服务可用性；模型响应较慢时可减少输入或切换模型后重试。")
    if isinstance(exc, ConnectionError) or name in {"APIConnectionError", "ConnectError", "ClientConnectionError", "ClientConnectorError", "ClientConnectorDNSError", "ClientConnectorCertificateError"}:
        return ExternalError(f"无法连接{service}服务", hint="检查网络、代理、服务地址及 TLS 证书；QQ 还需确认 OneBot 适配器在线。")
    return ExternalError(f"{service}调用失败", hint="检查对应服务配置与运行状态；使用诊断号及项目调用位置定位，原始响应未展示。")


def _clip(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value[:limit * 4].split())
    return "".join(char for char in text if not unicodedata.category(char).startswith("C"))[:limit]


_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_FILES = {str((_ROOT / relative).resolve()): Path(relative).name for relative in (
    "main.py", "paperbot/errors.py", "paperbot/models.py", "paperbot/service.py",
    "paperbot/storage.py", "paperbot/arxiv.py", "paperbot/scoring.py", "paperbot/reading.py",
)}
_SAFE_TYPES = {"SafeError", "ConfigError", "ExternalError", "ArxivError", "ScoringError", "ReadingError"}


def _type_name(exc: Exception) -> str:
    cls = type(exc)
    name = cls.__name__
    if getattr(builtins, name, None) is cls:
        return name
    if isinstance(exc, SafeError):
        return name if name in _SAFE_TYPES else "SafeError"
    # Arbitrary provider class names can themselves contain remote text.
    return "Exception"


def diagnose(exc: Exception, stage: str, *, context: str = "") -> dict:
    locations = []
    for frame, line in traceback.walk_tb(exc.__traceback__):
        filename = _PROJECT_FILES.get(str(Path(frame.f_code.co_filename).resolve()))
        if filename is None:
            continue
        function = frame.f_code.co_name
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", function):
            function = "module" if function == "<module>" else "function"
        locations.append(f"{filename}:{function}:{line}")
    if isinstance(exc, SafeError):
        message = _clip(str(exc), 400)
        if isinstance(exc, ConfigError) and exc.field:
            message = _clip(f"配置字段 {_clip(exc.field, 100)}：{message}", 400)
        hint = _clip(exc.hint, 250) or "核对相关配置后重试；若持续失败，请使用诊断号和项目调用位置排查。"
    else:
        message = f"出现未预期的 {_type_name(exc)}，本次操作未完成。"
        hint = "检查配置与运行环境；使用诊断号和项目调用位置排查。为保护凭据，未展示异常原文。"
    return {"id": secrets.token_hex(4), "stage": _clip(stage, 50), "type": _type_name(exc),
            "message": message, "hint": hint, "context": _clip(context, 180), "trace": locations[-8:]}


def public_error(report: dict) -> str:
    context = _clip(report.get("context"), 180)
    location = f"\n位置：{context}" if context else ""
    return (f"[Paper Bot] 失败阶段：{_clip(report.get('stage'), 50)}{location}\n"
            f"原因：{_clip(report.get('message'), 400)}\n"
            f"建议：{_clip(report.get('hint'), 250)}\n"
            f"诊断号：{_clip(report.get('id'), 16)}")
