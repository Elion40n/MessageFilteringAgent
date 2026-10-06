"""Offline fixtures for mail/SIWX parsing and notification formatting.

本文件只验证协议解析边界与游标逻辑，不连接真实 QQ 邮箱或微信账号。
"""

from datetime import date, datetime, timezone
from email import message_from_bytes
from email.message import EmailMessage
import hashlib
from io import BytesIO
import json
from pathlib import Path
from threading import Event
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from message_filtering_agent.config import Settings
from message_filtering_agent.models import AgentDecision, DecisionLabel, Message, MessageSource
from message_filtering_agent.delivery.qq_mail import QQMailDelivery
from message_filtering_agent.memory import MemoryStore
from message_filtering_agent.sources.qq_mail import QQMailSource
from message_filtering_agent.sources.runner import SourceRunner
from message_filtering_agent.sources.siwx_export import SIWXExportSource
from message_filtering_agent.sources.siwx_client import SIWXExportClient
from message_filtering_agent.storage import DedupStore
from message_filtering_agent.workflow import MessageWorkflow


class FakeCredentials:
    """Supply a dummy credential so source tests never open the OS keyring."""

    def get_secret(self, account: str) -> str:
        """Return a non-secret fixture value for the requested key name."""
        self.account = account
        return "test-only"

    def get_qq_mail_app_password(self, username: str) -> str:
        """Return a mailbox-scoped fixture credential without using keyring."""
        self.account = username
        return "test-only"


class FakeIMAP:
    """Minimal deterministic IMAP surface for UID recovery tests.

    仅模拟 IMAP 命令响应形状，不代表真实 QQ 邮箱服务器兼容性。
    """

    def __init__(self, uidvalidity: str, uids: list[int], messages: dict[int, bytes]) -> None:
        self.uidvalidity = uidvalidity
        self.uids = uids
        self.messages = messages
        self.search_calls: list[tuple[object, ...]] = []

    def login(self, username: str, password: str):
        """Accept the test login without sending credentials anywhere."""
        return "OK", [b"logged in"]

    def select(self, mailbox: str, readonly: bool = False):
        """Return the fixture mailbox's selected message count."""
        return "OK", [str(len(self.uids)).encode()]

    def response(self, name: str):
        """Expose the configured UIDVALIDITY generation."""
        return "UIDVALIDITY", [self.uidvalidity.encode()]

    def uid(self, command: str, *args: object):
        """Implement only search/fetch cases used by the source tests."""
        if command == "search":
            self.search_calls.append(args)
            if "ALL" in args:
                return "OK", [" ".join(map(str, self.uids)).encode()]
            start = int(str(args[-1]).split(":")[0])
            # Return only UIDs at or after the requested cursor, like the incremental test expects.
            # 只返回游标之后的 UID，用来验证增量搜索边界。
            return "OK", [" ".join(str(uid) for uid in self.uids if uid >= start).encode()]
        if command == "fetch":
            uid = int(str(args[0]))
            return "OK", [(b"RFC822", self.messages[uid])]
        raise AssertionError(f"Unexpected IMAP command: {command}")

    def logout(self):
        """End the fake session without external side effects."""
        return "BYE", [b"logout"]


def make_email(subject: str, body: str, sender: str = "sender@example.test") -> bytes:
    """Create a standard-library MIME fixture for parser tests."""
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "user@qq.com"
    message["Subject"] = subject
    message["Date"] = "Sat, 03 Oct 2026 10:00:00 +0000"
    message.set_content(body)
    return message.as_bytes()


def make_html_email(subject: str, body: str) -> bytes:
    """Create an HTML-only MIME email fixture without external services."""
    message = EmailMessage()
    message["From"] = "sender@example.test"
    message["To"] = "user@qq.com"
    message["Subject"] = subject
    message["Date"] = "Sat, 03 Oct 2026 10:00:00 +0000"
    message.add_alternative(body, subtype="html")
    return message.as_bytes()


class QQMailSourceTests(unittest.TestCase):
    """Verify initial scan, incremental UID lookup, and mailbox-generation reset."""

    def test_html_only_email_body_is_extracted_as_readable_text(self) -> None:
        """HTML-only mail retains visible text and links, not script/style markup.

        HTML-only 邮件应提取可见正文和链接，并排除脚本、样式内容。
        """
        raw = make_html_email(
            "HTML 招聘",
            "<html><head><style>.hidden{display:none}</style></head>"
            "<body><h1>招聘信息</h1><p>工作地点：上海</p>"
            "<a href='https://example.test/job'>查看职位</a>"
            "<script>ignore this secret</script></body></html>",
        )

        body = QQMailSource._read_body(message_from_bytes(raw))

        self.assertIn("招聘信息", body)
        self.assertIn("工作地点：上海", body)
        self.assertIn("https://example.test/job", body)
        self.assertNotIn("display:none", body)
        self.assertNotIn("ignore this secret", body)

    def test_plain_text_body_is_preferred_over_html_alternative(self) -> None:
        """Keep the existing plain-text selection when both alternatives exist."""
        message = EmailMessage()
        message.set_content("纯文本正文")
        message.add_alternative("<p>HTML 正文</p>", subtype="html")

        body = QQMailSource._read_body(message)

        self.assertEqual(body, "纯文本正文")

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.settings = Settings(
            data_dir=Path(self.temporary_directory.name),
            mail_username="user@qq.com",
        )
        self.storage = DedupStore(self.settings.database_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_initial_import_fetches_messages_and_returns_uid_cursors(self) -> None:
        """A first run emits stable generation/UID cursors for fetched mail."""
        client = FakeIMAP(
            "88",
            [1, 2],
            {1: make_email("招聘 A", "职位正文 A"), 2: make_email("招聘 B", "职位正文 B")},
        )
        source = QQMailSource(
            self.settings, FakeCredentials(), self.storage,
            client_factory=lambda *args, **kwargs: client,
        )
        envelopes = source.fetch_new()
        self.assertEqual([item.cursor for item in envelopes], ["88:1", "88:2"])
        self.assertIn("主题：招聘 A", envelopes[0].message.content)

    def test_existing_uidvalidity_uses_incremental_uid_search(self) -> None:
        """Unchanged UIDVALIDITY resumes strictly after the last committed UID."""
        self.storage.save_checkpoint("qq_mail", "user@qq.com", "INBOX", "88:2")
        client = FakeIMAP(
            "88", [1, 2, 3], {3: make_email("新邮件", "新增职位")}
        )
        source = QQMailSource(
            self.settings, FakeCredentials(), self.storage,
            client_factory=lambda *args, **kwargs: client,
        )
        envelopes = source.fetch_new()
        self.assertEqual([item.cursor for item in envelopes], ["88:3"])
        self.assertEqual(client.search_calls[0][-1], "3:*")

    def test_profile_checkpoint_resumes_incremental_search(self) -> None:
        """Non-default agents must resume from their own persisted mailbox cursor.

        验证非默认 Agent 从自己的邮箱 UID 检查点恢复。
        """
        settings = Settings(
            data_dir=Path(self.temporary_directory.name),
            profile_id="finance",
            mail_username="user@qq.com",
        )
        self.storage.save_checkpoint(
            "qq_mail", "user@qq.com", "INBOX", "88:2", profile_id="finance"
        )
        client = FakeIMAP("88", [1, 2, 3], {3: make_email("新邮件", "新增职位")})
        source = QQMailSource(
            settings, FakeCredentials(), self.storage,
            client_factory=lambda *args, **kwargs: client,
        )

        envelopes = source.fetch_new()

        self.assertEqual([item.cursor for item in envelopes], ["88:3"])
        self.assertEqual(client.search_calls[0][-1], "3:*")

    def test_uidvalidity_change_triggers_bounded_full_rescan(self) -> None:
        """A mailbox generation change abandons old UID ordering and starts a bounded rescan."""
        self.storage.save_checkpoint("qq_mail", "user@qq.com", "INBOX", "88:99")
        client = FakeIMAP("89", [1], {1: make_email("重新同步", "邮箱标识变化")})
        source = QQMailSource(
            self.settings, FakeCredentials(), self.storage,
            client_factory=lambda *args, **kwargs: client,
        )
        envelopes = source.fetch_new()
        self.assertEqual([item.cursor for item in envelopes], ["89:1"])
        self.assertEqual(client.search_calls[0][-1], "ALL")


class SIWXExportTests(unittest.TestCase):
    """Verify supported JSON shape normalization using a local fixture only."""

    def test_reads_and_sorts_messages_with_stable_cursors(self) -> None:
        """Out-of-order export records become ordered UTC messages and stable cursors."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            export_path = Path(temporary_directory) / "chat.json"
            export_path.write_text(
                '{"account":"wxid-1","session":{"username":"chat-1"},'
                '"messages":[{"id":2,"ts":1791021600,"text":"later"},'
                '{"id":1,"ts":1791018000,"text":"earlier"}]}',
                encoding="utf-8",
            )
            envelopes = list(SIWXExportSource().iter_file(export_path))
        self.assertEqual([item.message.content for item in envelopes], ["earlier", "later"])
        self.assertEqual(envelopes[0].message.account_id, "wxid-1")
        self.assertEqual(envelopes[0].message.timestamp.tzinfo, timezone.utc)
        self.assertTrue(envelopes[1].cursor > envelopes[0].cursor)

    def test_reads_native_siwx_json_shape(self) -> None:
        """Normalize SIWX's actual ownerId/wxid/createTime/localId JSON fields."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            export_path = Path(temporary_directory) / "chat.json"
            export_path.write_text(
                json.dumps({
                    "exportInfo": {"generator": "stories-in-wx"},
                    "session": {
                        "wxid": "group-1",
                        "ownerId": "account-1",
                        "displayName": "测试群",
                    },
                    "messages": [{
                        "localId": 42,
                        "platformMessageId": "server-42",
                        "createTime": 1791018000,
                        "content": "SIWX 原生导出正文",
                        "senderDisplayName": "发送者",
                    }],
                }),
                encoding="utf-8",
            )

            envelope = next(SIWXExportSource().iter_file(export_path))

        self.assertEqual(envelope.message.account_id, "account-1")
        self.assertEqual(envelope.message.conversation_id, "group-1")
        self.assertEqual(envelope.message.content, "SIWX 原生导出正文")
        self.assertEqual(envelope.message.sender, "发送者")
        self.assertEqual(envelope.message.external_id, "server-42")
        self.assertEqual(
            envelope.message.timestamp,
            datetime.fromtimestamp(1791018000, timezone.utc),
        )


class SIWXExportClientTests(unittest.TestCase):
    """Verify scheduled export API requests without a running SIWX service."""

    def test_starts_export_and_returns_json_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            export_file = Path(temporary_directory) / "session.json"
            export_file.write_text("{}", encoding="utf-8")
            responses = [
                {"started": True},
                {
                    "running": False,
                    "done": True,
                    "ok": True,
                    "mode": "sync",
                    "report": {"kind": "sync"},
                },
                {"started": True},
                {
                    "running": False,
                    "done": True,
                    "ok": True,
                    "mode": "export",
                    "report": {"sessions": [{"chat": "group-1", "file": str(export_file)}]},
                },
            ]
            requests = []

            def fake_urlopen(request, timeout):
                requests.append(request)
                return BytesIO(json.dumps(responses.pop(0)).encode("utf-8"))

            client = SIWXExportClient(
                "http://127.0.0.1:8787",
                export_root=Path(temporary_directory),
                poll_interval=0,
            )
            with patch("message_filtering_agent.sources.siwx_client.urlopen", fake_urlopen):
                paths = client.export_chat_json(
                    "account-1", ["group-1"], "2026-10-04", "2026-10-05", Event()
                )

        self.assertEqual(paths, [export_file.resolve()])
        self.assertEqual(requests[0].full_url, "http://127.0.0.1:8787/api/run")
        sync_payload = json.loads(requests[0].data)
        self.assertEqual(sync_payload["mode"], "sync")
        self.assertEqual(requests[1].full_url, "http://127.0.0.1:8787/api/job")
        self.assertEqual(requests[2].full_url, "http://127.0.0.1:8787/api/run")
        payload = json.loads(requests[2].data)
        self.assertEqual(payload["mode"], "export")
        self.assertEqual(payload["export_opts"]["account"], "account-1")
        self.assertEqual(payload["export_opts"]["chats"], [{"chat": "group-1", "display": "group-1"}])
        self.assertEqual(payload["export_opts"]["start"], "2026-10-04")
        self.assertEqual(requests[3].full_url, "http://127.0.0.1:8787/api/job")


class SourceRunnerTests(unittest.TestCase):
    """Verify local source-import and cross-source cursor behavior."""

    def test_siwx_auto_export_imports_json_and_respects_daily_cursor(self) -> None:
        """Request an export once, import it, and persist the successful schedule date."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            exports = root / "exports"
            export_file = exports / "export_batch" / "group.json"
            export_file.parent.mkdir(parents=True)
            export_file.write_text(
                json.dumps({
                    "session": {"ownerId": "account", "wxid": "group-1"},
                    "messages": [{
                        "localId": 1,
                        "createTime": 1791018000,
                        "content": "导出后的新消息",
                    }],
                }),
                encoding="utf-8",
            )
            settings = Settings(
                data_dir=root / "state",
                wechat_input_mode="siwx",
                siwx_export_dir=str(exports),
                siwx_auto_export_enabled=True,
                siwx_api_url="http://127.0.0.1:8787",
                siwx_account="account",
                siwx_chats="group-1",
                siwx_auto_export_interval_days=1,
            )
            storage = DedupStore(settings.database_path)
            memory = MemoryStore(settings.database_path, settings.profile_id)

            class RuntimeFixture:
                def __init__(self) -> None:
                    self.settings = settings
                    self.storage = storage
                    self.memory = memory
                    self.processed = []
                    self.workflow = self

                def process(self, message):
                    self.processed.append(message)

            class ExportClient:
                calls = []

                def __init__(self, base_url, export_root):
                    self.export_root = export_root

                def export_chat_json(self, account, chats, start_date, end_date, stop_event):
                    self.calls.append((account, chats, start_date, end_date))
                    return [export_file]

            runtime = RuntimeFixture()
            runner = SourceRunner(runtime)
            with patch("message_filtering_agent.sources.runner.SIWXExportClient", ExportClient):
                runner._poll_enabled_sources()
                runner._poll_enabled_sources()

            schedule_id = "chatset:" + hashlib.sha256(b"group-1").hexdigest()
            self.assertEqual(len(ExportClient.calls), 1)
            self.assertEqual(ExportClient.calls[0][0:3], ("account", ["group-1"], None))
            self.assertEqual(ExportClient.calls[0][3], date.today().isoformat())
            self.assertEqual([message.content for message in runtime.processed], ["导出后的新消息"])
            self.assertEqual(
                storage.get_checkpoint(
                    "siwx_auto_export", "account", schedule_id,
                    profile_id=settings.profile_id,
                ),
                date.today().isoformat(),
            )

    def test_siwx_auto_export_failure_does_not_advance_schedule_cursor(self) -> None:
        """A failed SIWX request remains retryable on the next poll."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            exports = root / "exports"
            exports.mkdir()
            settings = Settings(
                data_dir=root / "state",
                wechat_input_mode="siwx",
                siwx_export_dir=str(exports),
                siwx_auto_export_enabled=True,
                siwx_account="account",
                siwx_chats="group-1",
            )
            storage = DedupStore(settings.database_path)
            memory = MemoryStore(settings.database_path, settings.profile_id)

            class RuntimeFixture:
                pass

            runtime = RuntimeFixture()
            runtime.settings = settings
            runtime.storage = storage
            runtime.memory = memory
            runtime.workflow = runtime

            class FailedExportClient:
                def __init__(self, *args, **kwargs):
                    pass

                def export_chat_json(self, *args, **kwargs):
                    raise RuntimeError("SIWX 暂不可用")

            schedule_id = "chatset:" + hashlib.sha256(b"group-1").hexdigest()
            with patch(
                "message_filtering_agent.sources.runner.SIWXExportClient",
                FailedExportClient,
            ):
                with self.assertRaisesRegex(RuntimeError, "暂不可用"):
                    SourceRunner(runtime)._poll_enabled_sources()

            self.assertIsNone(
                storage.get_checkpoint(
                    "siwx_auto_export", "account", schedule_id,
                    profile_id=settings.profile_id,
                )
            )

    def test_auto_export_overlap_processes_late_message_before_cursor(self) -> None:
        """Message deduplication, not the newest cursor, handles the overlap window."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            export_file = root / "late.json"
            export_file.write_text(
                '{"account":"account","session":{"username":"chat"},'
                '"messages":[{"id":"late","ts":1791018000,"text":"late message"}]}',
                encoding="utf-8",
            )
            settings = Settings(data_dir=root / "state", profile_id="recruitment")
            storage = DedupStore(settings.database_path)
            previous_cursor = (
                f"{datetime.fromtimestamp(1791018060, timezone.utc).isoformat()}|newer"
            )
            storage.save_checkpoint(
                "siwx", "account", "chat", previous_cursor,
                profile_id=settings.profile_id,
            )
            memory = MemoryStore(settings.database_path, settings.profile_id)

            class RuntimeFixture:
                pass

            runtime = RuntimeFixture()
            runtime.settings = settings
            runtime.storage = storage
            runtime.memory = memory
            runtime.workflow = runtime
            runtime.processed = []
            runtime.process = lambda message: runtime.processed.append(message)

            SourceRunner(runtime)._process_siwx_files([export_file], skip_processed=False)

            self.assertEqual([item.content for item in runtime.processed], ["late message"])
            self.assertEqual(
                storage.get_checkpoint(
                    "siwx", "account", "chat", profile_id=settings.profile_id
                ),
                previous_cursor,
            )

    def test_cleanup_deletes_only_expired_registered_exports_inside_root(self) -> None:
        """Cleanup leaves manual files and paths outside the configured SIWX root."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "exports"
            nested = root / "auto-export"
            nested.mkdir(parents=True)
            expired_file = nested / "old.json"
            expired_file.write_text("{}", encoding="utf-8")
            manual_file = root / "manual.json"
            manual_file.write_text("{}", encoding="utf-8")
            external_file = Path(temporary_directory) / "outside.json"
            external_file.write_text("{}", encoding="utf-8")

            class StorageFixture:
                def __init__(self) -> None:
                    self.forgotten = []

                def list_expired_siwx_auto_exports(self, profile_id, cutoff):
                    return [expired_file, external_file]

                def forget_siwx_auto_export(self, profile_id, path):
                    self.forgotten.append(path)

            settings = Settings(
                data_dir=Path(temporary_directory) / "state",
                siwx_auto_cleanup_retention_days=7,
            )
            runtime = SimpleNamespace(storage=StorageFixture())
            runner = SourceRunner(runtime)

            runner._cleanup_siwx_auto_exports(settings, root)

            self.assertFalse(expired_file.exists())
            self.assertTrue(manual_file.exists())
            self.assertTrue(external_file.exists())
            self.assertEqual(runtime.storage.forgotten, [expired_file])

    def test_manual_wechat_mode_does_not_run_siwx_auto_cleanup(self) -> None:
        """The SIWX cleanup toggle is inert while the source mode is manual."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            exports = Path(temporary_directory) / "exports"
            exports.mkdir()
            settings = Settings(
                data_dir=Path(temporary_directory) / "state",
                wechat_input_mode="manual",
                siwx_export_dir=str(exports),
                siwx_auto_cleanup_enabled=True,
            )
            runtime = SimpleNamespace(settings=settings)
            runner = SourceRunner(runtime)

            with patch.object(runner, "_cleanup_siwx_auto_exports") as cleanup:
                runner._poll_enabled_sources()

            cleanup.assert_not_called()

    def test_source_runner_skips_identical_export_with_different_name(self) -> None:
        """The importer processes byte-identical files only once per profile.

        验证同一 profile 对内容完全相同的改名导出只处理一次。
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            exports = root / "exports"
            exports.mkdir()
            batch = exports / "export_20261005"
            batch.mkdir()
            first = batch / "first.json"
            first.write_text(
                '{"account":"account","session":{"username":"chat"},'
                '"messages":[{"id":1,"ts":1791018000,"text":"one message"}]}',
                encoding="utf-8",
            )
            renamed = batch / "renamed.json"
            renamed.write_bytes(first.read_bytes())

            class RuntimeFixture:
                settings = Settings(data_dir=root, profile_id="recruitment")
                storage = DedupStore(settings.database_path)
                memory = MemoryStore(settings.database_path, settings.profile_id)

                def __init__(self) -> None:
                    self.processed = []
                    self.workflow = self

                def process(self, message):
                    self.processed.append(message)

            runtime = RuntimeFixture()
            SourceRunner(runtime)._process_siwx_exports(exports)
            self.assertEqual(len(runtime.processed), 1)
            self.assertEqual(runtime.processed[0].content, "one message")

    def test_qq_mail_seen_message_is_skipped_by_siwx_and_cursor_advances(self) -> None:
        """A prior mail message is deduplicated when the matching export arrives.

        验证 QQ 邮件已处理的相同正文由 SIWX 跳过，且 SIWX 会话游标仍前进。
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            exports = root / "exports"
            exports.mkdir()
            export_path = exports / "chat.json"
            export_path.write_text(
                '{"account":"account","session":{"username":"chat"},'
                '"messages":[{"id":"siwx-1","ts":1791018000,'
                '"text":"Same normalized message"}]}',
                encoding="utf-8",
            )
            settings = Settings(data_dir=root / "state", profile_id="recruitment")
            storage = DedupStore(settings.database_path)
            memory = MemoryStore(settings.database_path, settings.profile_id)

            class Classifier:
                calls = 0

                def classify(self, message, active_settings):
                    self.calls += 1
                    return AgentDecision(DecisionLabel.NOT_SATISFIED)

            classifier = Classifier()
            workflow = MessageWorkflow(
                settings, storage, classifier, lambda *_: None, lambda *_: "question",
                memory.active_context,
            )
            prior_message = Message(
                source=MessageSource.QQ_MAIL,
                account_id="account",
                conversation_id="chat",
                timestamp=datetime.fromtimestamp(1791018000, timezone.utc),
                sender="sender",
                content=" Same   normalized message ",
                external_id="mail-1",
            )
            workflow.process(prior_message)

            class RuntimeFixture:
                pass

            runtime = RuntimeFixture()
            runtime.settings = settings
            runtime.storage = storage
            runtime.memory = memory
            runtime.workflow = workflow
            SourceRunner(runtime)._process_siwx_exports(exports)

            self.assertEqual(classifier.calls, 1)
            envelope = next(SIWXExportSource().iter_file(export_path))
            self.assertEqual(
                storage.get_checkpoint(
                    "siwx", "account", "chat", profile_id="recruitment"
                ),
                envelope.cursor,
            )

    def test_orderly_stop_during_siwx_releases_file_claim(self) -> None:
        """An orderly mode-change shutdown makes the partial export immediately retryable.

        验证切换输入方式触发的有序关闭会释放未处理完的文件占用。
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            exports = root / "exports"
            exports.mkdir()
            export_path = exports / "chat.json"
            export_path.write_text(
                '{"account":"account","session":{"username":"chat"},'
                '"messages":[{"id":1,"ts":1791018000,"text":"first"},'
                '{"id":2,"ts":1791018060,"text":"second"}]}',
                encoding="utf-8",
            )
            settings = Settings(data_dir=root / "state", profile_id="recruitment")
            storage = DedupStore(settings.database_path)
            memory = MemoryStore(settings.database_path, settings.profile_id)

            class RuntimeFixture:
                pass

            runtime = RuntimeFixture()
            runtime.settings = settings
            runtime.storage = storage
            runtime.memory = memory
            runner = SourceRunner(runtime)

            class WorkflowFixture:
                def __init__(self) -> None:
                    self.calls = 0

                def process(self, message) -> None:
                    self.calls += 1
                    runner.request_stop()

            runtime.workflow = WorkflowFixture()
            runner._process_siwx_exports(exports)

            self.assertEqual(runtime.workflow.calls, 1)
            self.assertIsNotNone(memory.claim_file(export_path))


class QQMailDeliveryTests(unittest.TestCase):
    """Verify notice content without opening an SMTP connection."""

    def test_notification_contains_source_and_original_location(self) -> None:
        """A notification must preserve where and when its source was received."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = Settings(
                data_dir=Path(temporary_directory),
                mail_username="sender@qq.com",
                notification_recipient="receiver@example.test",
            )
            delivery = QQMailDelivery(settings, FakeCredentials())
            message = Message(
                source=MessageSource.SIWX,
                account_id="wxid-1",
                conversation_id="group-1",
                timestamp=datetime(2026, 10, 3, tzinfo=timezone.utc),
                sender="招聘者",
                content="职位正文",
                external_id="message-8",
                source_ref="siwx-export://group.json/message-8",
            )
            mail = delivery.build_message(
                message,
                AgentDecision(
                    DecisionLabel.SATISFIED,
                    evidence=("职位正文",),
                    extracted_fields={"岗位": "工程师"},
                    explanation="条件要求上海或杭州；原文明确写明上海，地点条件吻合。",
                ),
            )
        body = mail.get_content()
        self.assertEqual(mail["To"], "receiver@example.test")
        self.assertIn("siwx-export://group.json/message-8", body)
        self.assertIn("判断理由：", body)
        self.assertIn("地点条件吻合", body)
        self.assertIn("岗位: 工程师", body)


if __name__ == "__main__":
    unittest.main()