"""Incremental QQ Mail reader using IMAP UID/UIDVALIDITY checkpoints.

用 UID 和 UIDVALIDITY 持续读取 QQ 邮箱；该适配器不负责发送邮件。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from html.parser import HTMLParser
import imaplib
from email.utils import parsedate_to_datetime
from typing import Callable

from ..config import Settings
from ..credentials import CredentialStore
from ..models import Message, MessageSource
from ..storage import DedupStore


@dataclass(frozen=True, slots=True)
class MailEnvelope:
    """Pair a normalized email with the cursor to persist after processing.

    将邮件记录与可恢复游标绑定；调用方处理成功后再保存该游标。
    """

    message: Message
    cursor: str


class _HTMLBodyTextParser(HTMLParser):
    """Extract readable body text and links while ignoring active markup."""

    _BLOCK_TAGS = {
        "address", "article", "blockquote", "br", "dd", "div", "dl", "dt",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
        "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "ol",
        "p", "section", "table", "tbody", "td", "tfoot", "th", "thead",
        "tr", "ul",
    }
    _IGNORED_TAGS = {"head", "noscript", "script", "style", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._ignored_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._ignored_stack:
            if tag in self._IGNORED_TAGS:
                self._ignored_stack.append(tag)
            return
        if tag in self._IGNORED_TAGS:
            self._ignored_stack.append(tag)
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")
        if tag == "a":
            href = next((value for name, value in attrs if name == "href"), None)
            if href and href.strip() and not href.strip().startswith("#"):
                self._parts.extend((" ", href.strip(), " "))

    def handle_endtag(self, tag: str) -> None:
        if self._ignored_stack:
            if tag == self._ignored_stack[-1]:
                self._ignored_stack.pop()
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_stack:
            self._parts.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self._parts).splitlines())
        return "\n".join(line for line in lines if line)


class QQMailSource:
    """Fetch QQ Mail messages incrementally without relying on shutdown time.

    UIDVALIDITY 改变代表服务器 UID 空间重置；此时执行有界重扫并依靠去重降低重复。
    """

    RESCAN_LIMIT = 2000

    def __init__(
        self,
        settings: Settings,
        credentials: CredentialStore,
        storage: DedupStore,
        client_factory: Callable[..., imaplib.IMAP4_SSL] = imaplib.IMAP4_SSL,
    ) -> None:
        self.settings = settings
        self.credentials = credentials
        self.storage = storage
        self.client_factory = client_factory

    def fetch_new(self) -> list[MailEnvelope]:
        """Authenticate, select a folder, and return unseen UID records.

        登录并选择邮箱文件夹，再按当前 profile 的 UID 检查点读取新邮件。
        """
        username = self.settings.mail_username.strip()
        if not username:
            raise ValueError("请先配置 QQ 邮箱账号")
        password = self.credentials.get_qq_mail_app_password(username)
        if not password:
            raise ValueError("请先在本地界面保存 QQ 邮箱授权码")

        client = self.client_factory(
            self.settings.mail_imap_host,
            self.settings.mail_imap_port,
            timeout=20,
        )
        try:
            self._require_ok(client.login(username, password), "QQ 邮箱登录失败")
            self._require_ok(
                client.select(self.settings.mail_folder, readonly=True),
                "无法打开 QQ 邮箱文件夹",
            )
            uidvalidity = self._uidvalidity(client)
            # Keep mailbox progress independent for each Agent/profile.
            # 每个 Agent/profile 单独读取邮箱检查点，互不推进对方的游标。
            previous = self.storage.get_checkpoint(
                "qq_mail",
                username,
                self.settings.mail_folder,
                profile_id=self.settings.profile_id,
            )
            previous_validity, previous_uid = self._parse_checkpoint(previous)
            reset = previous_validity is not None and previous_validity != uidvalidity

            if reset:
                # UID values are no longer comparable after UIDVALIDITY changes; rescan a bounded tail.
                # UIDVALIDITY 改变后旧游标不可比较，因此只重扫有上限的最近邮件。
                status, response = client.uid("search", None, "ALL")
                self._require_ok((status, response), "UIDVALIDITY 重置后无法扫描邮箱")
                uids = self._decode_uids(response)[-self.RESCAN_LIMIT :]
            elif previous_uid is None:
                status, response = client.uid("search", None, "ALL")
                self._require_ok((status, response), "无法读取邮箱消息列表")
                uids = self._decode_uids(response)[-self.RESCAN_LIMIT :]
            else:
                status, response = client.uid(
                    "search", None, "UID", f"{previous_uid + 1}:*"
                )
                self._require_ok((status, response), "无法增量读取邮箱消息")
                uids = [uid for uid in self._decode_uids(response) if uid > previous_uid]

            envelopes: list[MailEnvelope] = []
            for uid in uids:
                # Do not advance persistent state here; SourceRunner commits each cursor after handling.
                # 此处只构造待处理记录，不推进持久游标；由运行器处理完成后逐封保存。
                status, response = client.uid("fetch", str(uid), "(RFC822)")
                self._require_ok((status, response), f"无法读取邮件 UID {uid}")
                raw_message = self._message_bytes(response)
                if raw_message is None:
                    continue
                parsed = message_from_bytes(raw_message)
                subject = self._decode_header_value(parsed.get("Subject", ""))
                sender = self._decode_header_value(parsed.get("From", ""))
                body = self._read_body(parsed)
                content = f"主题：{subject}\n\n{body}".strip()
                timestamp = self._parse_date(parsed.get("Date"))
                cursor = f"{uidvalidity}:{uid}"
                envelopes.append(
                    MailEnvelope(
                        Message(
                            source=MessageSource.QQ_MAIL,
                            account_id=username,
                            conversation_id=self.settings.mail_folder,
                            timestamp=timestamp,
                            sender=sender,
                            content=content,
                            external_id=cursor,
                            source_ref=f"imap://{username}/{self.settings.mail_folder}/UID/{uid}",
                        ),
                        cursor,
                    )
                )
            return envelopes
        finally:
            # Logout failure must not mask the original fetch/parse error.
            # 退出连接失败不能覆盖前面真实的读取或解析异常。
            try:
                client.logout()
            except Exception:
                pass

    @staticmethod
    def _require_ok(result: tuple[object, object], message: str) -> None:
        """Convert a non-OK IMAP status into a retryable operational error."""
        if not result or result[0] != "OK":
            raise ConnectionError(message)

    @staticmethod
    def _uidvalidity(client: imaplib.IMAP4) -> str:
        """Read the server mailbox generation required for safe UID recovery."""
        _, values = client.response("UIDVALIDITY")
        if not values:
            raise ConnectionError("服务器未返回 UIDVALIDITY，无法安全恢复邮箱游标")
        return str(values[0]).strip("b'\"")

    @staticmethod
    def _parse_checkpoint(checkpoint: str | None) -> tuple[str | None, int | None]:
        """Decode a stored generation/UID pair; malformed state triggers bounded rescan."""
        if not checkpoint or ":" not in checkpoint:
            return None, None
        validity, uid = checkpoint.split(":", 1)
        try:
            return validity, int(uid)
        except ValueError:
            return None, None

    @staticmethod
    def _decode_uids(response: object) -> list[int]:
        """Extract unique integer UIDs from imaplib's byte response shape."""
        if not isinstance(response, (list, tuple)):
            return []
        values: list[int] = []
        for item in response:
            # IMAP search commonly returns one space-separated bytes item; ignore malformed tokens.
            # 搜索响应通常是以空格分隔的字节串，无法解析的项不作为 UID 使用。
            if isinstance(item, bytes):
                for value in item.split():
                    if value.isdigit():
                        values.append(int(value))
        return sorted(set(values))

    @staticmethod
    def _message_bytes(response: object) -> bytes | None:
        """Find RFC822 bytes in imaplib's mixed fetch response."""
        if not isinstance(response, (list, tuple)):
            return None
        for item in response:
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], bytes):
                return item[1]
        return None

    @staticmethod
    def _decode_header_value(value: str) -> str:
        """Decode RFC 2047 headers while retaining undecodable source text."""
        try:
            return str(make_header(decode_header(value)))
        except (LookupError, UnicodeError, ValueError):
            return value

    @classmethod
    def _read_body(cls, message: object) -> str:
        """Prefer plain text, falling back to readable text from an HTML body.

        优先使用非附件纯文本；邮件只有 HTML 正文时提取可读文本和链接。
        """
        parts = message.walk() if message.is_multipart() else (message,)
        html_body: str | None = None
        for part in parts:
            if part.get_content_disposition() == "attachment" or part.get_filename():
                # Attachment text is not part of the message body used for classification.
                # 附件内容不混入正文，避免把附件元数据误当成消息文本。
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except LookupError:
                decoded = payload.decode("utf-8", errors="replace")
            if part.get_content_type() == "text/plain":
                return decoded.strip()
            if part.get_content_type() == "text/html" and html_body is None:
                html_body = decoded
        if html_body is None:
            return ""
        parser = _HTMLBodyTextParser()
        parser.feed(html_body)
        parser.close()
        return parser.text()

    @staticmethod
    def _parse_date(value: str | None) -> datetime:
        """Normalize a parsed mail date to UTC, using current UTC only as fallback."""
        if value:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except (TypeError, ValueError, OverflowError):
                pass
        return datetime.now(timezone.utc)