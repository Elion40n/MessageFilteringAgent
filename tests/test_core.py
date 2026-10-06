"""Offline tests for shared models, settings, and SQLite persistence.

覆盖公共数据契约、设置校验及本地持久化，不访问任何外部账号。
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from message_filtering_agent.config import Settings
from message_filtering_agent.app import Runtime
from message_filtering_agent.credentials import (
    CredentialStore,
    model_api_key_account,
    normalize_model_url,
    qq_mail_password_account,
)
from message_filtering_agent.models import AgentDecision, DecisionLabel, Message, MessageSource
from message_filtering_agent.memory import MemoryStore
from message_filtering_agent.storage import DedupStore
from message_filtering_agent.llm import (
    OpenAICompatibleClassifier,
    _StructuredDecision,
    _StructuredMessageSplit,
)


class MessageModelTests(unittest.TestCase):
    """Protect timestamp normalization and the fixed user-facing decision set.

    确保无时区时间统一处理，且三态标签保持用户可识别的中文形式。
    """

    def test_naive_timestamp_is_normalized_to_utc(self) -> None:
        """A naive timestamp must not leak into timezone comparisons."""
        message = Message(
            source=MessageSource.MANUAL,
            account_id="local",
            conversation_id="import",
            timestamp=datetime(2026, 1, 1),
            sender="sender",
            content="message",
        )
        self.assertEqual(message.timestamp.tzinfo, timezone.utc)

    def test_decision_has_required_chinese_label(self) -> None:
        """The uncertainty state is displayed using the agreed Chinese label."""
        self.assertEqual(DecisionLabel.UNCERTAIN.display_name, "不确定")

    def test_credential_accounts_are_scoped_and_normalized(self) -> None:
        self.assertEqual(
            model_api_key_account("HTTPS://API.EXAMPLE.TEST/v1/"),
            model_api_key_account("https://api.example.test/v1"),
        )
        self.assertNotEqual(
            model_api_key_account("https://one.example.test/v1"),
            model_api_key_account("https://two.example.test/v1"),
        )
        self.assertEqual(
            qq_mail_password_account(" User@Example.test "),
            qq_mail_password_account("user@example.test"),
        )
        self.assertNotEqual(
            qq_mail_password_account("one@example.test"),
            qq_mail_password_account("two@example.test"),
        )
        self.assertEqual(normalize_model_url(""), "default")

    def test_legacy_model_key_migrates_to_default_but_not_custom_endpoints(self) -> None:
        class FakeKeyring:
            class errors:
                class PasswordDeleteError(Exception):
                    pass

            def __init__(self) -> None:
                self.values = {("MessageFilteringAgent", "llm_api_key"): "legacy-key"}

            def get_password(self, service: str, account: str):
                return self.values.get((service, account))

            def set_password(self, service: str, account: str, value: str) -> None:
                self.values[(service, account)] = value

            def delete_password(self, service: str, account: str) -> None:
                self.values.pop((service, account), None)

        store = object.__new__(CredentialStore)
        store._keyring = FakeKeyring()
        self.assertEqual(store.get_model_api_key(""), "legacy-key")
        self.assertIsNone(store.get_model_api_key("https://one.example.test/v1"))
        self.assertIsNone(store.get_secret("llm_api_key"))
        self.assertEqual(
            store.get_secret(model_api_key_account("")),
            "legacy-key",
        )

    def test_classifier_output_requires_nonblank_reason(self) -> None:
        """Every structured classification must include an explicit reason.

        三种分类结果都必须带非空的理由。
        """
        with self.assertRaises(ValueError):
            _StructuredDecision(decision="满足")
        with self.assertRaises(ValueError):
            _StructuredDecision(decision="满足", explanation="   ")

    def test_transcript_split_requires_nonempty_fragments(self) -> None:
        """The split tool cannot silently return no content or blank fragments."""
        split = _StructuredMessageSplit(segments=[" 目标消息 ", " 闲聊噪声 "])
        self.assertEqual(split.segments, ["目标消息", "闲聊噪声"])
        with self.assertRaises(ValueError):
            _StructuredMessageSplit(segments=[])
        with self.assertRaises(ValueError):
            _StructuredMessageSplit(segments=["目标消息", "  "])


class ClassifierPromptTests(unittest.TestCase):
    """Keep message-level OR and decision-consistency rules in the prompt."""

    def test_prompt_uses_generic_message_level_attribute_matching(self) -> None:
        """Message-level matching rules must not assume a specific information domain.

        消息级属性匹配规则不应假设特定信息领域。
        """
        class CapturingModel:
            def invoke(self, messages):
                self.messages = messages
                return SimpleNamespace(
                    decision="满足",
                    evidence=["活动城市：上海"],
                    extracted_fields={},
                    missing_fields=[],
                    explanation="消息列出上海以及符合条件的活动主题。",
                )

        classifier = object.__new__(OpenAICompatibleClassifier)
        classifier._model = CapturingModel()
        message = Message(
            source=MessageSource.MANUAL,
            account_id="local",
            conversation_id="manual-input",
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            sender="手动输入",
            content="候选城市：上海、武汉；主题：人工智能应用、产品交流",
        )
        classifier.classify_with_memory(
            message,
            Settings(criteria="活动城市为上海或杭州，且主题涉及人工智能"),
            [],
        )

        prompt = classifier._model.messages[0]["content"]
        self.assertIn("命中任一备选即可满足", prompt)
        self.assertIn("按用户表达的粒度判断", prompt)
        self.assertIn("不要擅自要求列表中的每个实体或条目逐一建立属性配对", prompt)
        self.assertIn("用户没有要求同一实体关联时，不能因为关联信息未提供而判不确定或不满足", prompt)
        self.assertIn("也不得列入missing_fields", prompt)
        self.assertIn("只有用户明确要求同一实体同时具备多个属性时", prompt)
        self.assertIn("不得把无法确认写成不满足", prompt)
        self.assertIn("判断标签与理由必须一致", prompt)
        self.assertIn("闲聊", prompt)
        self.assertIn("判不满足", prompt)
        self.assertNotIn("招聘", prompt)
        self.assertNotIn("岗位", prompt)

    def test_split_tool_preserves_order_and_includes_noise_segments(self) -> None:
        """The preprocessing call splits boundaries without deleting noisy text."""
        transcript = "上午好\n招聘信息：上海招工程师\n收到，谢谢"

        class CapturingModel:
            def invoke(self, messages):
                self.messages = messages
                return SimpleNamespace(segments=["上午好", "招聘信息：上海招工程师", "收到，谢谢"])

        classifier = object.__new__(OpenAICompatibleClassifier)
        classifier._split_model = CapturingModel()

        segments = classifier.split_messages(transcript)

        self.assertEqual(segments, ["上午好", "招聘信息：上海招工程师", "收到，谢谢"])
        self.assertEqual(classifier._split_model.messages[1]["content"], transcript)
        self.assertIn("不得删除任何片段", classifier._split_model.messages[0]["content"])
        self.assertIn("不负责筛选", classifier._split_model.messages[0]["content"])


class SettingsTests(unittest.TestCase):
    """Check non-secret settings persistence and input validation."""

    def test_save_without_overwrite_preserves_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "existing.json"
            path.write_text("existing data", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                Settings(data_dir=Path(temporary_directory)).save(path, overwrite=False)

            self.assertEqual(path.read_text(encoding="utf-8"), "existing data")

    def test_siwx_auto_export_requires_loopback_service_and_configuration(self) -> None:
        """Automatic export can only target a configured local SIWX instance."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            export_dir = Path(temporary_directory)
            with self.assertRaisesRegex(ValueError, "数据来源必须选择 SIWX"):
                Settings(
                    data_dir=export_dir,
                    wechat_input_mode="manual",
                    siwx_auto_export_enabled=True,
                    siwx_export_dir=str(export_dir),
                    siwx_account="account",
                    siwx_chats="chat",
                )
            with self.assertRaisesRegex(ValueError, "本机 HTTP"):
                Settings(
                    data_dir=export_dir,
                    wechat_input_mode="siwx",
                    siwx_auto_export_enabled=True,
                    siwx_api_url="http://example.test:8787",
                    siwx_export_dir=str(export_dir),
                    siwx_account="account",
                    siwx_chats="chat",
                )
            with self.assertRaisesRegex(ValueError, "SIWX exports 目录"):
                Settings(
                    data_dir=export_dir,
                    wechat_input_mode="siwx",
                    siwx_auto_export_enabled=True,
                    siwx_account="account",
                    siwx_chats="chat",
                )
            with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
                Settings(siwx_auto_export_start_date="10/05/2026")

    def test_retired_wechat_modes_fall_back_to_manual(self) -> None:
        """Legacy listener and reserved SIWX configs must not enable imports."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "settings.json"
            for mode in ("wxauto", "siwx"):
                with self.subTest(mode=mode):
                    path.write_text(
                        json.dumps({
                            "wechat_input_mode": mode,
                            "wxauto_chats": "old chat",
                            "siwx_auto_export_enabled": True,
                        }),
                        encoding="utf-8",
                    )
                    settings = Settings.load(path)
                    self.assertEqual(settings.wechat_input_mode, "manual")
                    self.assertFalse(settings.siwx_auto_export_enabled)
                    self.assertFalse(hasattr(settings, "wxauto_chats"))

    def test_siwx_auto_export_cleanup_defaults_and_settings_round_trip(self) -> None:
        """Cleanup defaults to enabled for seven days and persists both controls."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings_path = Path(temporary_directory) / "settings.json"
            data_dir = Path(temporary_directory)
            settings = Settings(data_dir=data_dir)

            self.assertTrue(settings.siwx_auto_cleanup_enabled)
            self.assertEqual(settings.siwx_auto_cleanup_retention_days, 7)
            Settings(
                data_dir=data_dir,
                siwx_auto_cleanup_enabled=False,
                siwx_auto_cleanup_retention_days=12,
            ).save(settings_path)
            loaded = Settings.load(settings_path)

        self.assertFalse(loaded.siwx_auto_cleanup_enabled)
        self.assertEqual(loaded.siwx_auto_cleanup_retention_days, 12)

    def test_settings_round_trip_without_secrets(self) -> None:
        """Persisted preferences reconstruct an equivalent immutable settings object."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "settings.json"
            expected = Settings(
                data_dir=Path(temporary_directory),
                profile_id="recruitment",
                dedup_retention_days=14,
                port=9001,
            )
            expected.save(path)
            actual = Settings.load(path)
            self.assertEqual(actual, expected)

    def test_selected_config_file_receives_its_own_updates(self) -> None:
        """A selected profile config must not write changes into the default file.

        验证保存选定 profile 时不会覆盖默认设置文件。
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            default_path = root / "settings.json"
            selected_path = root / "profiles" / "research.json"
            Settings(data_dir=root, criteria="default").save(default_path)

            selected = Settings.load(selected_path)
            updated = Runtime(selected).update_settings({"criteria": "research only"})

            self.assertEqual(Settings.load(selected_path).criteria, "research only")
            self.assertEqual(Settings.load(default_path).criteria, "default")
            self.assertEqual(updated.settings_path, selected_path)

    def test_first_profile_name_renames_config_and_cannot_be_changed_later(self) -> None:
        """The initial profile binding renames its config without requiring memory data.

        首次绑定 profile 时，即使记忆为空也应完成重命名，之后拒绝修改名称。
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = Runtime(Settings(data_dir=root, config_file=root / "settings.json"))
            try:
                self.assertEqual(runtime.memory.list_entries(), [])

                updated = runtime.update_settings({"profile_id": "research"})

                expected_path = root / "research.json"
                self.assertEqual(updated.settings_path, expected_path)
                self.assertTrue(expected_path.exists())
                self.assertFalse((root / "settings.json").exists())
                self.assertTrue(updated.profile_name_bound)
                self.assertEqual(Settings.load(expected_path).profile_id, "research")
                self.assertEqual(runtime.memory.list_entries(), [])
                with self.assertRaisesRegex(ValueError, "初始化后不可更改"):
                    runtime.update_settings({"profile_id": "finance"})
            finally:
                runtime.stop_sources()

    def test_omitted_initial_profile_name_defaults_and_locks(self) -> None:
        """Leaving the initial default name unchanged still permanently binds it.

        首次保存不指定名称时使用 default，并将配置保存为 default.json 后锁定。
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            runtime = Runtime(Settings(data_dir=root, config_file=root / "settings.json"))
            try:
                updated = runtime.update_settings({"criteria": "initial criteria"})

                self.assertEqual(updated.profile_id, "default")
                self.assertEqual(updated.settings_path, root / "default.json")
                self.assertTrue(updated.profile_name_bound)
                with self.assertRaisesRegex(ValueError, "初始化后不可更改"):
                    runtime.update_settings({"profile_id": "later-name"})
            finally:
                runtime.stop_sources()

    def test_legacy_existing_config_without_binding_marker_is_locked(self) -> None:
        """A pre-feature saved config does not get a new profile-name opportunity.

        已存在且缺少绑定标记的旧配置视为已初始化，不再允许重命名。
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "settings.json"
            Settings(data_dir=root).save(config_path)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            payload.pop("profile_name_bound")
            config_path.write_text(json.dumps(payload), encoding="utf-8")

            runtime = Runtime(Settings.load(config_path))
            try:
                self.assertTrue(runtime.settings.profile_name_bound)
                with self.assertRaisesRegex(ValueError, "初始化后不可更改"):
                    runtime.update_settings({"profile_id": "new-profile"})
                self.assertTrue(config_path.exists())
                self.assertFalse((root / "new-profile.json").exists())
            finally:
                runtime.stop_sources()

    def test_named_config_files_get_distinct_default_profile_ids(self) -> None:
        """Config filenames provide distinct profile IDs when none is stored.

        验证未显式填写 profile ID 时，不同配置文件名会得到不同 ID。
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = Settings.load(root / "recruitment.json")
            second = Settings.load(root / "finance.json")
            self.assertEqual(first.profile_id, "recruitment")
            self.assertEqual(second.profile_id, "finance")
            self.assertEqual(first.data_dir, second.data_dir)

    def test_retention_range_is_validated(self) -> None:
        """Reject a dedupe window that could disable useful duplicate protection."""
        with self.assertRaises(ValueError):
            Settings(dedup_retention_days=0)

    def test_runtime_parses_checkbox_values_as_booleans(self) -> None:
        """HTML checkbox strings must not become truthy strings such as 'false'."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime = Runtime(Settings(data_dir=Path(temporary_directory)))
            updated = runtime.update_settings({"mail_ingestion_enabled": "false"})
            self.assertFalse(updated.mail_ingestion_enabled)
            runtime.stop_sources()

    def test_reserved_siwx_mode_cannot_be_enabled_from_settings(self) -> None:
        """The future SIWX placeholder cannot be activated through runtime settings.

        验证当前设置入口拒绝启用尚未采用的 SIWX 输入方式。
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime = Runtime(Settings(data_dir=Path(temporary_directory)))

            with self.assertRaisesRegex(ValueError, "未来预留，暂未启用"):
                runtime.update_settings({"wechat_input_mode": "siwx"})

            self.assertEqual(runtime.settings.wechat_input_mode, "manual")
            runtime.stop_sources()


class ManualTranscriptTests(unittest.TestCase):
    """Verify transcript splitting precedes every formal per-segment workflow."""

    def test_runtime_processes_multiple_transcripts_as_independent_groups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime = Runtime(Settings(data_dir=Path(temporary_directory)))
            transcripts = ["闲聊\n目标信息", "拆分异常的记录"]
            outcomes = [
                {"status": "filtered"},
                {"status": "delivered"},
            ]
            try:
                with (
                    patch.object(
                        runtime.workflow,
                        "split_manual_transcript",
                        side_effect=[["闲聊", "目标信息"], RuntimeError("拆分失败")],
                    ),
                    patch.object(
                        runtime.workflow,
                        "process",
                        side_effect=outcomes,
                    ) as process,
                ):
                    groups = runtime.process_manual_transcripts(transcripts)

                self.assertEqual([group["transcript_index"] for group in groups], [1, 2])
                self.assertEqual(groups[0]["segments"], ["闲聊", "目标信息"])
                self.assertEqual(
                    [item["status"] for item in groups[0]["results"]],
                    ["filtered", "delivered"],
                )
                self.assertEqual(groups[1]["segments"], [])
                self.assertEqual(groups[1]["results"][0]["status"], "error")
                self.assertEqual(len(process.call_args_list), 2)
            finally:
                runtime.stop_sources()

    def test_runtime_processes_every_split_segment_including_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime = Runtime(Settings(data_dir=Path(temporary_directory)))
            transcript = "闲聊\n目标信息\n另一段杂讯"
            segments = ["闲聊", "目标信息", "另一段杂讯"]
            outcomes = [
                {"status": "filtered"},
                {"status": "delivered"},
                {"status": "filtered"},
            ]
            try:
                with (
                    patch.object(
                        runtime.workflow,
                        "split_manual_transcript",
                        return_value=segments,
                    ),
                    patch.object(
                        runtime.workflow,
                        "process",
                        side_effect=outcomes,
                    ) as process,
                ):
                    result = runtime.process_manual_transcript(transcript)

                self.assertEqual(result["segments"], segments)
                self.assertEqual(
                    [item["status"] for item in result["results"]],
                    ["filtered", "delivered", "filtered"],
                )
                self.assertEqual(
                    [call.args[0].content for call in process.call_args_list],
                    segments,
                )
            finally:
                runtime.stop_sources()

    def test_runtime_does_not_classify_if_transcript_splitting_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime = Runtime(Settings(data_dir=Path(temporary_directory)))
            try:
                with (
                    patch.object(
                        runtime.workflow,
                        "split_manual_transcript",
                        side_effect=RuntimeError("拆分失败"),
                    ),
                    patch.object(runtime.workflow, "process") as process,
                    self.assertRaisesRegex(RuntimeError, "拆分失败"),
                ):
                    runtime.process_manual_transcript("原始聊天记录")
                process.assert_not_called()
            finally:
                runtime.stop_sources()


class ProfileDeletionTests(unittest.TestCase):
    """Deleting one profile clears its durable state without touching others."""

    def test_delete_profile_removes_all_scoped_rows_and_preserves_other_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "agent.sqlite3"
            storage = DedupStore(database_path)
            target_memory = MemoryStore(database_path, "remove-me")
            other_memory = MemoryStore(database_path, "keep-me")
            now = datetime.now(timezone.utc)
            target_message = Message(
                source=MessageSource.MANUAL,
                account_id="local",
                conversation_id="manual",
                timestamp=now,
                sender="sender",
                content="target profile message",
                external_id="target-id",
            )
            other_message = Message(
                source=MessageSource.MANUAL,
                account_id="local",
                conversation_id="manual",
                timestamp=now,
                sender="sender",
                content="other profile message",
                external_id="other-id",
            )
            decision = AgentDecision(
                DecisionLabel.UNCERTAIN,
                missing_fields=("地点",),
                explanation="缺少地点。",
            )
            storage.register_message(target_message, "remove-me", 30)
            storage.register_message(other_message, "keep-me", 30)
            storage.save_checkpoint("qq_mail", "mail", "INBOX", "5", profile_id="remove-me")
            storage.save_checkpoint("qq_mail", "mail", "INBOX", "9", profile_id="keep-me")
            storage.create_pending_question(target_message, decision, "remove-me")
            storage.create_pending_question(other_message, decision, "keep-me")
            storage.archive_filtered_message(
                target_message,
                AgentDecision(DecisionLabel.NOT_SATISFIED, explanation="无关内容。"),
                "remove-me",
            )
            storage.archive_filtered_message(
                other_message,
                AgentDecision(DecisionLabel.NOT_SATISFIED, explanation="无关内容。"),
                "keep-me",
            )
            storage.record_siwx_auto_export("remove-me", root / "remove.json")
            storage.record_siwx_auto_export("keep-me", root / "keep.json")
            target_memory.add_entry("目标记忆", "补充")
            other_memory.add_entry("保留记忆", "补充")
            target_export = root / "target-export.json"
            other_export = root / "other-export.json"
            target_export.write_text("target", encoding="utf-8")
            other_export.write_text("other", encoding="utf-8")
            target_memory.claim_file(target_export)
            other_memory.claim_file(other_export)

            storage.delete_profile("remove-me")

            for table in (
                "content_fingerprints",
                "transport_ids",
                "source_checkpoints_by_profile",
                "pending_questions_by_profile",
                "filtered_messages",
                "siwx_auto_export_files",
                "memory_entries",
                "imported_file_fingerprints",
            ):
                with storage._connection() as connection:
                    target_count = connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE profile_id = ?",
                        ("remove-me",),
                    ).fetchone()[0]
                    other_count = connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE profile_id = ?",
                        ("keep-me",),
                    ).fetchone()[0]
                self.assertEqual(target_count, 0, table)
                self.assertEqual(other_count, 1, table)
class DedupStoreTests(unittest.TestCase):
    """Exercise durable de-duplication with an isolated temporary database."""

    def setUp(self) -> None:
        """Give each test a fresh database to prevent cross-test state leakage."""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = DedupStore(Path(self.temporary_directory.name) / "state.sqlite3")
        self.now = datetime(2026, 10, 3, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        """Release the temporary database after each isolated scenario."""
        self.temporary_directory.cleanup()

    @staticmethod
    def message(
        source: MessageSource,
        content: str,
        external_id: str | None = None,
        conversation_id: str = "chat-1",
    ) -> Message:
        """Build concise normalized records for storage assertions."""
        return Message(
            source=source,
            account_id="account-1",
            conversation_id=conversation_id,
            timestamp=datetime(2026, 10, 3, tzinfo=timezone.utc),
            sender="sender",
            content=content,
            external_id=external_id,
        )

    def test_transport_id_deduplicates_same_source_message(self) -> None:
        """An exact replay from the same source identity is rejected first."""
        message = self.message(MessageSource.QQ_MAIL, "Opening", "uid-1")
        first = self.store.register_message(message, "profile", 30, self.now)
        second = self.store.register_message(message, "profile", 30, self.now)
        self.assertTrue(first.is_new)
        self.assertEqual(second.reason, "transport_id")

    def test_canonical_content_deduplicates_across_sources(self) -> None:
        """Whitespace/case normalization catches an exact copy from another adapter."""
        first = self.message(MessageSource.SIWX, "Senior Python Engineer", "wx-1")
        duplicate = self.message(MessageSource.QQ_MAIL, "  senior   python engineer ", "mail-1")
        self.assertTrue(self.store.register_message(first, "profile", 30, self.now).is_new)
        result = self.store.register_message(duplicate, "profile", 30, self.now)
        self.assertEqual(result.reason, "content_fingerprint")

    def test_profiles_are_isolated(self) -> None:
        """Different user filters may process the same text independently."""
        message = self.message(MessageSource.SIWX, "Same content")
        self.assertTrue(self.store.register_message(message, "profile-a", 30, self.now).is_new)
        self.assertTrue(self.store.register_message(message, "profile-b", 30, self.now).is_new)

    def test_content_is_allowed_again_after_retention_expires(self) -> None:
        """An old fingerprint stops suppressing a message after its TTL."""
        message = self.message(MessageSource.SIWX, "Same content")
        self.store.register_message(message, "profile", 2, self.now)
        after_expiry = self.store.register_message(
            message,
            "profile",
            2,
            self.now + timedelta(days=2, seconds=1),
        )
        self.assertTrue(after_expiry.is_new)

    def test_duplicate_sighting_extends_suppression_window(self) -> None:
        """A recurring duplicate refreshes the rolling retention window."""
        first = self.message(MessageSource.SIWX, "Same content", "wx-1")
        later_copy = self.message(MessageSource.QQ_MAIL, "Same content", "mail-1")
        self.store.register_message(first, "profile", 2, self.now)
        self.store.register_message(later_copy, "profile", 2, self.now + timedelta(days=1))
        still_duplicate = self.store.register_message(
            self.message(MessageSource.MANUAL, "Same content"),
            "profile",
            2,
            self.now + timedelta(days=2, hours=1),
        )
        self.assertEqual(still_duplicate.reason, "content_fingerprint")

    def test_checkpoint_round_trip(self) -> None:
        """Source cursors survive a storage write/read boundary."""
        self.store.save_checkpoint("qq_mail", "account", "inbox", "uidvalidity:17:42", self.now)
        self.assertEqual(
            self.store.get_checkpoint("qq_mail", "account", "inbox"),
            "uidvalidity:17:42",
        )

    def test_checkpoints_and_pending_questions_are_profile_scoped(self) -> None:
        """Separate profiles cannot read one another's cursors or questions.

        验证不同 profile 不能读取彼此的游标和待确认问题。
        """
        message = self.message(MessageSource.SIWX, "question")
        decision = AgentDecision(label=DecisionLabel.UNCERTAIN)
        question_a = self.store.create_pending_question(message, decision, "profile-a")
        question_b = self.store.create_pending_question(message, decision, "profile-b")
        self.store.save_checkpoint(
            "siwx", "account", "chat", "cursor-a", self.now, profile_id="profile-a"
        )
        self.store.save_checkpoint(
            "siwx", "account", "chat", "cursor-b", self.now, profile_id="profile-b"
        )

        self.assertEqual(self.store.list_pending_questions("profile-a")[0]["question_id"], question_a)
        self.assertEqual(self.store.list_pending_questions("profile-b")[0]["question_id"], question_b)
        self.assertIsNone(self.store.get_pending_question(question_a, "profile-b"))
        self.assertEqual(
            self.store.get_checkpoint("siwx", "account", "chat", "profile-a"),
            "cursor-a",
        )
        self.assertEqual(
            self.store.get_checkpoint("siwx", "account", "chat", "profile-b"),
            "cursor-b",
        )


class FilteredArchiveTests(unittest.TestCase):
    """Verify filtered-message retention, profile isolation, and promotion.

    验证不满足归档的保留期、profile 隔离和原子提升待处理。
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = DedupStore(Path(self.temporary_directory.name) / "state.sqlite3")
        self.now = datetime(2026, 10, 4, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def message(external_id: str) -> Message:
        return Message(
            source=MessageSource.QQ_MAIL,
            account_id="mailbox",
            conversation_id="MFA_TEST",
            timestamp=datetime(2026, 10, 3, tzinfo=timezone.utc),
            sender="招聘者",
            content="测试招聘消息原文",
            external_id=external_id,
            source_ref=f"qqmail://MFA_TEST/{external_id}",
        )

    def test_filtered_archive_is_profile_scoped_and_expires(self) -> None:
        message = self.message("uid-1")
        decision = AgentDecision(
            label=DecisionLabel.NOT_SATISFIED,
            explanation="未命中筛选条件。",
        )
        archive_id = self.store.archive_filtered_message(
            message, decision, "recruitment", 30, self.now
        )

        entries = self.store.list_filtered_messages("recruitment", self.now)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["archive_id"], archive_id)
        self.assertEqual(entries[0]["profile_id"], "recruitment")
        self.assertEqual(entries[0]["message"].content, message.content)
        self.assertEqual(entries[0]["message"].source, MessageSource.QQ_MAIL)
        self.assertEqual(self.store.list_filtered_messages("finance", self.now), [])
        self.assertEqual(
            self.store.list_filtered_messages(
                "recruitment", self.now + timedelta(days=30)
            ),
            [],
        )

    def test_promoting_archive_creates_one_pending_question(self) -> None:
        message = self.message("uid-2")
        archive_id = self.store.archive_filtered_message(
            message,
            AgentDecision(
                label=DecisionLabel.NOT_SATISFIED,
                explanation="原始过滤理由。",
            ),
            "recruitment",
            30,
            self.now,
        )

        pending_id = self.store.promote_filtered_message(archive_id, "recruitment", self.now)
        repeated_id = self.store.promote_filtered_message(archive_id, "recruitment", self.now)

        self.assertEqual(repeated_id, pending_id)
        pending = self.store.list_pending_questions("recruitment")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["question_id"], pending_id)
        self.assertEqual(pending[0]["message"].content, message.content)
        self.assertEqual(pending[0]["decision"].label, DecisionLabel.UNCERTAIN)
        self.assertEqual(pending[0]["source_archive_id"], archive_id)
        self.assertEqual(self.store.list_filtered_messages("recruitment", self.now), [])

    def test_promoting_archive_deletes_archive_and_is_idempotent(self) -> None:
        """Promotion removes the archive row and repeat requests reuse its question.

        提升后删除归档记录，重复提升仍返回原待处理 ID。
        """
        message = self.message("uid-reprocess")
        archive_id = self.store.archive_filtered_message(
            message,
            AgentDecision(DecisionLabel.NOT_SATISFIED, explanation="首次过滤理由。"),
            "recruitment",
            30,
            self.now,
        )
        self.store.promote_filtered_message(archive_id, "recruitment", self.now)

        self.assertEqual(self.store.list_filtered_messages("recruitment"), [])
        self.assertEqual(
            self.store.promote_filtered_message(archive_id, "recruitment"),
            self.store.list_pending_questions("recruitment")[0]["question_id"],
        )

    def test_runtime_archives_formal_non_match_for_active_profile(self) -> None:
        """The production Runtime archives not-satisfied results under its profile.

        正式 Runtime 的不满足结果写入当前 profile 归档。
        """
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = Settings(
                data_dir=Path(temporary_directory),
                profile_id="recruitment",
            )
            runtime = Runtime(settings)
            try:
                runtime.workflow.classifier.classify_with_memory = (
                    lambda message, current_settings, memories: AgentDecision(
                        label=DecisionLabel.NOT_SATISFIED,
                        evidence=("未命中城市条件",),
                        explanation="原文城市不符合当前条件。",
                    )
                )
                message = self.message("uid-runtime")

                result = runtime.workflow.process(message)

                self.assertEqual(result["status"], "filtered")
                archived = runtime.storage.list_filtered_messages("recruitment")
                self.assertEqual(len(archived), 1)
                self.assertEqual(archived[0]["message"].content, message.content)
                self.assertEqual(archived[0]["decision"].label, DecisionLabel.NOT_SATISFIED)
                self.assertEqual(archived[0]["profile_id"], "recruitment")
            finally:
                runtime.stop_sources()


class MemoryStoreTests(unittest.TestCase):
    """Keep learned memories and imported files isolated by profile.

    确保人工记忆与导入文件指纹按 profile 隔离。
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "state.sqlite3"
        self.store = MemoryStore(self.database, "profile-a")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_memory_crud_and_profile_isolation(self) -> None:
        """Exercise add, context, edit, disable, and delete behavior.

        覆盖记忆新增、上下文读取、编辑、停用和删除。
        """
        entry = self.store.add_entry("地点必须在上海", "满足", tags=["地点"])
        self.assertEqual(self.store.active_context(), ["人工判断：满足；经验：地点必须在上海"])
        other_profile = MemoryStore(self.database, "profile-b")
        self.assertEqual(other_profile.list_entries(), [])
        self.assertTrue(self.store.update_entry(entry["memory_id"], {"enabled": False}))
        self.assertEqual(self.store.active_context(), [])
        self.assertTrue(self.store.delete_entry(entry["memory_id"]))
        self.assertEqual(self.store.list_entries(), [])

    def test_identical_file_content_is_deduplicated_even_when_renamed(self) -> None:
        """Identical bytes are duplicates regardless of their filenames.

        验证内容相同的文件即使改名也会被识别为重复。
        """
        root = Path(self.temporary_directory.name)
        first = root / "first.json"
        renamed = root / "renamed.json"
        first.write_text('{"messages": []}', encoding="utf-8")
        renamed.write_text('{"messages": []}', encoding="utf-8")
        token = self.store.claim_file(first)
        self.assertIsNotNone(token)
        self.assertTrue(self.store.complete_file(first, token))
        self.assertIsNone(self.store.claim_file(renamed))
        self.assertIsNotNone(MemoryStore(self.database, "profile-b").claim_file(renamed))

    def test_failed_file_claim_can_be_retried(self) -> None:
        """A released failed claim can be acquired again.

        验证处理失败并释放占用后，文件可以重新领取。
        """
        path = Path(self.temporary_directory.name) / "retry.json"
        path.write_text("retry", encoding="utf-8")
        token = self.store.claim_file(path)
        self.assertIsNotNone(token)
        self.store.release_file(path, token)
        self.assertIsNotNone(self.store.claim_file(path))


if __name__ == "__main__":
    unittest.main()