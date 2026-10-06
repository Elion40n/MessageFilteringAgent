"""Reserved reader for future SIWX chat JSON import support.

保留本地 SIWX JSON 解析与会话游标实现；当前版本不启用微信自动输入。
真实导出字段差异仍需用用户样本验证后才能重新采用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Iterator

from ..models import Message, MessageSource


@dataclass(frozen=True, slots=True)
class ExportEnvelope:
    """Pair one imported chat message with its ordered recovery cursor."""

    message: Message
    cursor: str


class SIWXExportSource:
    """Convert supported SIWX JSON message shapes into normalized records."""

    def iter_exports(self, export_directory: Path) -> Iterator[ExportEnvelope]:
        """Yield JSON files in stable filename order for repeatable recovery."""
        for path in sorted(export_directory.glob("*.json")):
            # Stable file order makes cursor progression deterministic across polling passes.
            # 固定文件顺序便于多次轮询重现相同处理顺序。
            yield from self.iter_file(path)

    def iter_file(self, path: Path) -> Iterator[ExportEnvelope]:
        """Parse one SIWX export and yield messages sorted by UTC timestamp and ID."""
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        session = data.get("session", {}) if isinstance(data, dict) else {}
        account = str(
            data.get("account") or session.get("ownerId")
            or session.get("account") or "siwx-account"
        )
        conversation = str(
            session.get("wxid")
            or session.get("username")
            or session.get("chat")
            or session.get("chat_name")
            or data.get("chat")
            or path.stem
        )
        raw_messages = data.get("messages", []) if isinstance(data, dict) else []
        parsed: list[tuple[datetime, str, dict[str, object]]] = []
        for raw in raw_messages:
            if not isinstance(raw, dict):
                continue
            content = str(raw.get("text") or raw.get("content") or "").strip()
            if not content:
                continue
            timestamp = self._timestamp(
                raw.get("ts") or raw.get("timestamp") or raw.get("time")
                or raw.get("createTime")
            )
            external_id = str(
                raw.get("platformMessageId") or raw.get("server_id")
                or raw.get("serverId") or raw.get("id")
                or raw.get("local_id") or raw.get("localId") or ""
            )
            if not external_id:
                # Exports without a source ID get a deterministic fallback, not a claimed platform ID.
                # 导出缺少稳定 ID 时用内容构造确定性替代值，不冒充平台原生消息 ID。
                identity = f"{timestamp.isoformat()}|{raw.get('sender_name', '')}|{content}"
                external_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            parsed.append((timestamp, external_id, raw))

        # Sorting is required because checkpoints compare this composite cursor lexicographically.
        # 检查点按组合游标比较，因此先按时间和 ID 排序，确保顺序一致。
        parsed.sort(key=lambda item: (item[0], item[1]))
        for timestamp, external_id, raw in parsed:
            content = str(raw.get("text") or raw.get("content") or "").strip()
            sender = str(
                raw.get("sender_name") or raw.get("senderDisplayName")
                or raw.get("sender") or raw.get("sender_wxid") or ""
            )
            yield ExportEnvelope(
                Message(
                    source=MessageSource.SIWX,
                    account_id=account,
                    conversation_id=conversation,
                    timestamp=timestamp,
                    sender=sender,
                    content=content,
                    external_id=external_id,
                    source_ref=f"siwx-export://{path.name}/{external_id}",
                ),
                f"{timestamp.isoformat()}|{external_id}",
            )

    @staticmethod
    def _timestamp(value: object) -> datetime:
        """Normalize numeric seconds/milliseconds or ISO text to timezone-aware UTC."""
        if isinstance(value, (int, float)):
            seconds = float(value)
            if seconds > 100_000_000_000:
                # SIWX may expose millisecond epochs; convert them before fromtimestamp().
                # SIWX 有时返回毫秒时间戳，先换算为秒再构造 datetime。
                seconds /= 1000
            return datetime.fromtimestamp(seconds, timezone.utc)
        if isinstance(value, str):
            try:
                numeric = float(value)
                return SIWXExportSource._timestamp(numeric)
            except ValueError:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
        return datetime.now(timezone.utc)