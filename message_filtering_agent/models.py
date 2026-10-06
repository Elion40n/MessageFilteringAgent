"""Shared message and decision contracts for adapters and the workflow.

适配器与工作流共用的消息、字段来源及三态决策契约。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import unicodedata


class MessageSource(str, Enum):
    """Stable source identifiers stored with each normalized message.

    统一消息来源枚举；这些值会持久化，因此避免随意重命名。
    """

    MANUAL = "manual"
    # Retained only so existing SQLite records from the retired adapter remain readable.
    WXAUTO = "wxauto"
    SIWX = "siwx"
    QQ_MAIL = "qq_mail"


class DecisionLabel(str, Enum):
    """The only three business outcomes exposed by the agent.

    Agent 对外只允许这三种业务结论；网络和模型故障由工作流单独处理。
    """

    SATISFIED = "satisfied"
    NOT_SATISFIED = "not_satisfied"
    UNCERTAIN = "uncertain"

    @property
    def display_name(self) -> str:
        """Return the user-facing Chinese label for this outcome.

        返回界面使用的中文标签，与内部稳定枚举值解耦。
        """
        return {
            DecisionLabel.SATISFIED: "满足",
            DecisionLabel.NOT_SATISFIED: "不满足",
            DecisionLabel.UNCERTAIN: "不确定",
        }[self]


@dataclass(frozen=True, slots=True)
class Message:
    """Normalized input record retaining source provenance.

    归一化后的输入记录保留账号、会话、时间和源定位，方便恢复与追溯。
    """

    source: MessageSource
    account_id: str
    conversation_id: str
    timestamp: datetime
    sender: str
    content: str
    external_id: str | None = None
    source_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.account_id.strip():
            raise ValueError("account_id must not be empty")
        if not self.conversation_id.strip():
            raise ValueError("conversation_id must not be empty")
        if not isinstance(self.timestamp, datetime):
            raise TypeError("timestamp must be a datetime")
        normalized = self.timestamp
        if normalized.tzinfo is None:
            # Naive timestamps have no recoverable local zone; treat them as UTC consistently.
            # 无时区时间无法推断来源机器的本地时区，因此统一按 UTC 解释。
            normalized = normalized.replace(tzinfo=timezone.utc)
        object.__setattr__(self, "timestamp", normalized.astimezone(timezone.utc))

    @property
    def content_fingerprint(self) -> str | None:
        """Build an exact-content fingerprint shared across source adapters.

        指纹忽略全半角差异、大小写和连续空白，但不做语义近似合并，避免误删相似信息。
        """
        normalized = unicodedata.normalize("NFKC", self.content)
        normalized = " ".join(normalized.casefold().split())
        if not normalized:
            return None
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AgentDecision:
    """Structured model result with evidence and unresolved fields.

    保存判断依据和缺失字段，使“不确定”问题可向用户解释并在补充后续判。
    """

    label: DecisionLabel
    evidence: tuple[str, ...] = ()
    extracted_fields: dict[str, str] = field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    explanation: str = ""