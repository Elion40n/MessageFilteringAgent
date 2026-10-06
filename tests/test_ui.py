"""Local HTTP API tests using an ephemeral loopback-only server.

使用临时端口验证本机界面与写接口；不启动生产端口或外部服务。
"""

from dataclasses import replace
from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
import tempfile
import unittest
from unittest.mock import patch

from message_filtering_agent.app import Runtime
from message_filtering_agent.config import Settings
from message_filtering_agent.credentials import model_api_key_account, qq_mail_password_account
from message_filtering_agent.models import AgentDecision, DecisionLabel, Message, MessageSource
from message_filtering_agent.ui import create_handler


class LocalUITests(unittest.TestCase):
    """Exercise read/write boundaries without launching a browser or account client."""

    def setUp(self) -> None:
        """Start the handler on an OS-assigned loopback port for this test."""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.runtime = Runtime(Settings(data_dir=Path(self.temporary_directory.name)))
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), create_handler(self.runtime)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = HTTPConnection("127.0.0.1", self.server.server_port)

    def tearDown(self) -> None:
        """Stop server threads before deleting the SQLite fixture directory."""
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.runtime.stop_sources()
        self.temporary_directory.cleanup()

    def test_ui_is_served_and_api_hides_data_directory(self) -> None:
        """Static UI is available while private storage paths stay server-side."""
        self.connection.request("GET", "/")
        response = self.connection.getresponse()
        html = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("消息筛选台", html)
        self.assertIn('data-tab="memory"', html)
        self.assertIn('id="panel-memory"', html)
        self.assertIn('data-tab="test-output"', html)
        self.assertIn('手动输入', html)
        self.assertIn('处理结果', html)
        self.assertIn('id="panel-test-output"', html)
        self.assertIn('data-tab="filtered"', html)
        self.assertIn('id="filtered-count"', html)
        self.assertIn('id="manual-transcripts"', html)
        self.assertIn('id="add-manual-transcript"', html)
        self.assertIn("maxlength=\"200000\"", html)
        self.assertIn('id="panel-filtered"', html)
        self.assertIn('id="shutdown-agent"', html)
        self.assertIn('id="agent-status"', html)
        self.assertIn('value="siwx" disabled>SIWX（未来预留，暂未启用）</option>', html)
        self.assertIn("当前版本暂不采用微信自动输入", html)
        self.assertIn('name="siwx_auto_export_enabled"', html)
        self.assertIn('name="siwx_auto_export_start_date"', html)
        self.assertIn('name="siwx_auto_cleanup_enabled"', html)
        self.assertIn('name="siwx_auto_cleanup_retention_days"', html)

        self.connection.request("GET", "/assets/app.js")
        response = self.connection.getresponse()
        script = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("正在保存理由记忆并应用人工判断，请稍候", script)
        self.assertIn('api("/api/messages"', script)
        self.assertIn('api("/api/messages"', script)
        self.assertIn("JSON.stringify({ transcripts })", script)
        self.assertIn("正在拆分聊天记录", script)
        self.assertIn("card.remove();", script)
        self.assertIn('"保存处理结果"', script)
        self.assertIn('api("/api/shutdown"', script)
        self.assertIn("window.confirm(", script)
        self.assertIn("window.close();", script)
        self.assertIn('window.location.replace("about:blank")', script)

        self.connection.request("GET", "/api/settings")
        response = self.connection.getresponse()
        settings = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertNotIn("data_dir", settings)
        self.assertNotIn("config_file", settings)
        self.assertNotIn("llm_api_key", settings)

    def test_memory_api_supports_create_update_list_and_delete(self) -> None:
        """Exercise profile-memory CRUD through the local HTTP boundary.

        验证本机 HTTP 接口可新增、编辑、列出和删除 profile 记忆。
        """
        origin = f"http://127.0.0.1:{self.server.server_port}"
        self.connection.request(
            "POST",
            "/api/memories",
            body=json.dumps({"content": "招聘地点需在上海", "decision": "满足", "tags": ["地点"]}),
            headers={"Content-Type": "application/json", "Origin": origin},
        )
        response = self.connection.getresponse()
        payload = json.loads(response.read())
        self.assertEqual(response.status, 201)
        memory_id = payload["memory"]["memory_id"]

        self.connection.request(
            "POST",
            f"/api/memories/{memory_id}",
            body=json.dumps({"content": "只接受上海", "decision": "满足", "enabled": False, "tags": []}),
            headers={"Content-Type": "application/json", "Origin": origin},
        )
        response = self.connection.getresponse()
        self.assertEqual(response.status, 200)
        response.read()

        self.connection.request("GET", "/api/memories")
        response = self.connection.getresponse()
        memories = json.loads(response.read())["memories"]
        self.assertEqual(memories[0]["content"], "只接受上海")
        self.assertFalse(memories[0]["enabled"])

        self.connection.request("DELETE", f"/api/memories/{memory_id}", headers={"Origin": origin})
        response = self.connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(response.read())["deleted"])

    def test_settings_api_saves_preferences_without_secrets(self) -> None:
        """Allowed local-origin writes persist only the submitted non-secret fields."""
        body = json.dumps({
            "criteria": "地点为上海",
            "dedup_retention_days": 12,
            "filtered_retention_days": 14,
            "siwx_api_url": "http://127.0.0.1:8787",
            "siwx_account": "wxid-test",
            "siwx_chats": "group-test",
            "siwx_auto_export_interval_days": 3,
            "siwx_auto_cleanup_enabled": False,
            "siwx_auto_cleanup_retention_days": 12,
            "mail_ingestion_enabled": False,
            "profile_id": "research",
        })
        self.connection.request(
            "POST",
            "/api/settings",
            body=body,
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.server.server_port}"},
        )
        response = self.connection.getresponse()
        settings = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(settings["criteria"], "地点为上海")
        self.assertEqual(settings["dedup_retention_days"], 12)
        self.assertEqual(settings["filtered_retention_days"], 14)
        self.assertEqual(settings["siwx_account"], "wxid-test")
        self.assertEqual(settings["siwx_auto_export_interval_days"], 3)
        self.assertFalse(settings["siwx_auto_cleanup_enabled"])
        self.assertEqual(settings["siwx_auto_cleanup_retention_days"], 12)
        self.assertFalse(settings["mail_ingestion_enabled"])
        self.assertEqual(settings["config_name"], "research.json")
        self.assertTrue(settings["profile_name_bound"])
        self.assertTrue(settings["restart_required"])
        self.assertTrue((Path(self.temporary_directory.name) / "research.json").exists())

    def test_secret_api_saves_model_and_mail_credentials_in_scoped_accounts(self) -> None:
        """Secret requests are keyed by endpoint URL and mailbox identity."""
        origin = f"http://127.0.0.1:{self.server.server_port}"
        with patch("message_filtering_agent.credentials.CredentialStore") as store_type:
            store = store_type.return_value
            for payload in (
                {
                    "name": "llm_api_key",
                    "secret": "test-model-key",
                    "model_url": "https://model.example.test/v1",
                },
                {
                    "name": "qq_mail_app_password",
                    "secret": "test-mail-password",
                    "mail_username": "user@qq.example",
                },
            ):
                self.connection.request(
                    "POST",
                    "/api/secrets",
                    body=json.dumps(payload),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": origin,
                    },
                )
                response = self.connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read()), {"saved": True})

        self.assertEqual(
            [call.args for call in store.set_secret.call_args_list],
            [
                (model_api_key_account("https://model.example.test/v1"), "test-model-key"),
                (qq_mail_password_account("user@qq.example"), "test-mail-password"),
            ],
        )

    def test_filtered_archive_api_lists_metadata_and_promotes_once(self) -> None:
        """Filtered items are profile-local and promote idempotently to review.

        过滤列表展示来源、日期和 profile；提升后只创建一条待处理事项。
        """
        message = Message(
            source=MessageSource.QQ_MAIL,
            account_id="mailbox",
            conversation_id="MFA_TEST",
            timestamp=datetime(2026, 10, 3, tzinfo=timezone.utc),
            sender="发布者",
            content="过滤归档原文",
            external_id="uid-archive-1",
            source_ref="qqmail://MFA_TEST/uid-archive-1",
        )
        archive_id = self.runtime.storage.archive_filtered_message(
            message,
            AgentDecision(
                label=DecisionLabel.NOT_SATISFIED,
                evidence=("不符合条件",),
                explanation="原判断理由。",
            ),
            self.runtime.settings.profile_id,
            self.runtime.settings.filtered_retention_days,
        )
        origin = f"http://127.0.0.1:{self.server.server_port}"

        self.connection.request("GET", "/api/filtered-messages")
        response = self.connection.getresponse()
        payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(len(payload["messages"]), 1)
        item = payload["messages"][0]
        self.assertEqual(item["archive_id"], archive_id)
        self.assertEqual(item["profile_id"], self.runtime.settings.profile_id)
        self.assertEqual(item["source"], "qq_mail")
        self.assertEqual(item["timestamp"], message.timestamp.isoformat())
        self.assertEqual(item["content"], "过滤归档原文")
        self.assertEqual(item["decision"], "不满足")

        self.connection.request(
            "POST",
            f"/api/filtered-messages/{archive_id}/promote",
            body="{}",
            headers={"Content-Type": "application/json", "Origin": origin},
        )
        response = self.connection.getresponse()
        promoted = json.loads(response.read())
        self.assertEqual(response.status, 200)

        self.connection.request(
            "POST",
            f"/api/filtered-messages/{archive_id}/promote",
            body="{}",
            headers={"Content-Type": "application/json", "Origin": origin},
        )
        response = self.connection.getresponse()
        repeated = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(repeated["question_id"], promoted["question_id"])
        self.connection.request("GET", "/api/filtered-messages")
        response = self.connection.getresponse()
        self.assertEqual(json.loads(response.read())["messages"], [])
        pending = self.runtime.storage.list_pending_questions(self.runtime.settings.profile_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["message"].content, "过滤归档原文")

    def test_question_answer_api_requires_choice_and_returns_saved_ack(self) -> None:
        """The review endpoint accepts the final choice without a classification payload.

        待处理接口保存用户最终选择，只返回已处理确认，不返回未使用的分类结果。
        """
        question_id = "a" * 32
        origin = f"http://127.0.0.1:{self.server.server_port}"
        with patch.object(self.runtime, "answer_question") as answer_question:
            self.connection.request(
                "POST",
                f"/api/questions/{question_id}/answer",
                body=json.dumps({"answer": "要求明确为上海", "choice": "not_satisfied_reason"}),
                headers={"Content-Type": "application/json", "Origin": origin},
            )
            response = self.connection.getresponse()
            payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(payload, {"processed": True})
        answer_question.assert_called_once_with(
            question_id, "要求明确为上海", "not_satisfied_reason"
        )

    def test_activity_api_returns_profile_scoped_change_summary(self) -> None:
        """Activity polling exposes current-profile counts, not other agents' data.

        网页轮询摘要只返回当前 profile 的待确认和过滤计数。
        """
        timestamp = datetime(2026, 10, 4, tzinfo=timezone.utc)
        current_message = Message(
            source=MessageSource.MANUAL,
            account_id="local",
            conversation_id="manual",
            timestamp=timestamp,
            sender="手动输入",
            content="当前 profile 消息",
        )
        other_message = Message(
            source=MessageSource.MANUAL,
            account_id="local",
            conversation_id="manual",
            timestamp=timestamp,
            sender="手动输入",
            content="其他 profile 消息",
        )
        uncertain = AgentDecision(DecisionLabel.UNCERTAIN, explanation="待补充")
        self.runtime.storage.create_pending_question(
            current_message, uncertain, self.runtime.settings.profile_id
        )
        self.runtime.storage.create_pending_question(other_message, uncertain, "other-profile")
        self.runtime.storage.archive_filtered_message(
            current_message,
            AgentDecision(DecisionLabel.NOT_SATISFIED, explanation="过滤原因"),
            self.runtime.settings.profile_id,
        )
        self.runtime.storage.archive_filtered_message(
            other_message,
            AgentDecision(DecisionLabel.NOT_SATISFIED, explanation="其他 profile 原因"),
            "other-profile",
        )

        self.connection.request("GET", "/api/activity")
        response = self.connection.getresponse()
        snapshot = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(snapshot["pending_count"], 1)
        self.assertEqual(snapshot["filtered_count"], 1)
        self.assertGreater(snapshot["pending_updated_at"], 0)
        self.assertGreater(snapshot["filtered_updated_at"], 0)

    def test_manual_transcript_api_splits_then_formally_processes_each_segment(self) -> None:
        """One pasted transcript is split first, then each fragment is routed.

        单段聊天记录先拆分，随后每个片段独立进入正式工作流。
        """
        satisfied = AgentDecision(
            label=DecisionLabel.SATISFIED,
            evidence=("岗位符合",),
            explanation="满足条件。",
        )
        filtered = AgentDecision(
            label=DecisionLabel.NOT_SATISFIED,
            explanation="不符合条件。",
        )
        uncertain = AgentDecision(
            label=DecisionLabel.UNCERTAIN,
            missing_fields=("地点",),
            explanation="缺少地点信息。",
        )
        origin = f"http://127.0.0.1:{self.server.server_port}"
        outcomes = [
            {"status": "delivered", "decision": satisfied},
            {"status": "filtered", "decision": filtered},
            {"status": "needs_input", "decision": uncertain, "pending_question_id": "pending-1"},
            RuntimeError("模型暂时不可用"),
        ]
        transcript = "早上好\n目标招聘信息\n收到谢谢\n周末愉快"
        segments = ["早上好", "目标招聘信息", "收到谢谢", "周末愉快"]
        with (
            patch.object(
                self.runtime.workflow,
                "split_manual_transcript",
                return_value=segments,
            ) as split,
            patch.object(self.runtime.workflow, "process", side_effect=outcomes) as process,
        ):
            self.connection.request(
                "POST",
                "/api/messages",
                body=json.dumps({"content": transcript}),
                headers={"Content-Type": "application/json", "Origin": origin},
            )
            response = self.connection.getresponse()
            payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["segments"], segments)
        split.assert_called_once_with(transcript)
        results = payload["results"]
        self.assertEqual([result["status"] for result in results], [
            "delivered", "filtered", "needs_input", "error",
        ])
        self.assertEqual([result.get("decision") for result in results], [
            "满足", "不满足", "不确定", None,
        ])
        self.assertEqual(results[2]["pending_question_id"], "pending-1")
        self.assertEqual(results[3]["error"], "模型暂时不可用")
        self.assertEqual(
            [call.args[0].content for call in process.call_args_list],
            segments,
        )
        self.assertTrue(all(call.args[0].source is MessageSource.MANUAL for call in process.call_args_list))

    def test_manual_api_processes_multiple_transcript_inputs_separately(self) -> None:
        """Each added transcript is split independently and retains its group index.

        多个输入框分别拆分，返回结果保留各自的聊天记录序号。
        """
        transcripts = ["记录一：闲聊\n目标一", "记录二：目标二"]
        split_groups = [["记录一：闲聊", "目标一"], ["记录二：目标二"]]
        decisions = [
            {"status": "filtered", "decision": AgentDecision(
                DecisionLabel.NOT_SATISFIED, explanation="无关闲聊。"
            )},
            {"status": "delivered", "decision": AgentDecision(
                DecisionLabel.SATISFIED, explanation="命中目标。"
            )},
            {"status": "needs_input", "decision": AgentDecision(
                DecisionLabel.UNCERTAIN, explanation="需补充信息。"
            )},
        ]
        origin = f"http://127.0.0.1:{self.server.server_port}"
        with (
            patch.object(
                self.runtime.workflow,
                "split_manual_transcript",
                side_effect=split_groups,
            ) as split,
            patch.object(self.runtime.workflow, "process", side_effect=decisions) as process,
        ):
            self.connection.request(
                "POST",
                "/api/messages",
                body=json.dumps({"transcripts": transcripts}),
                headers={"Content-Type": "application/json", "Origin": origin},
            )
            response = self.connection.getresponse()
            payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(
            [(group["transcript_index"], group["segments"]) for group in payload["groups"]],
            [(1, split_groups[0]), (2, split_groups[1])],
        )
        self.assertEqual(
            [[result["status"] for result in group["results"]] for group in payload["groups"]],
            [["filtered", "delivered"], ["needs_input"]],
        )
        self.assertEqual(split.call_count, 2)
        self.assertEqual(
            [call.args[0].content for call in process.call_args_list],
            ["记录一：闲聊", "目标一", "记录二：目标二"],
        )

    def test_manual_transcript_api_rejects_oversized_input_before_splitting(self) -> None:
        """Reject an oversized transcript before any model or workflow call.

        超长聊天记录必须在模型拆分和正式处理前拒绝。
        """
        origin = f"http://127.0.0.1:{self.server.server_port}"
        with (
            patch.object(self.runtime.workflow, "split_manual_transcript") as split,
            patch.object(self.runtime.workflow, "process") as process,
        ):
            self.connection.request(
                "POST",
                "/api/messages",
                body=json.dumps({"content": "x" * 200_001}),
                headers={"Content-Type": "application/json", "Origin": origin},
            )
            response = self.connection.getresponse()
            payload = json.loads(response.read())

        self.assertEqual(response.status, 400)
        self.assertIn("200,000", payload["error"])
        split.assert_not_called()
        process.assert_not_called()

    def test_manual_transcript_api_returns_split_segments_and_results(self) -> None:
        """Even a one-fragment transcript uses the explicit split response shape."""
        decision = AgentDecision(
            label=DecisionLabel.SATISFIED,
            explanation="单条结果。",
        )
        origin = f"http://127.0.0.1:{self.server.server_port}"
        with (
            patch.object(self.runtime.workflow, "split_manual_transcript", return_value=["单条消息"]),
            patch.object(
                self.runtime.workflow,
                "process",
                return_value={"status": "delivered", "decision": decision},
            ),
        ):
            self.connection.request(
                "POST",
                "/api/messages",
                body=json.dumps({"content": "单条消息"}),
                headers={"Content-Type": "application/json", "Origin": origin},
            )
            response = self.connection.getresponse()
            payload = json.loads(response.read())

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["segments"], ["单条消息"])
        self.assertEqual(payload["results"][0]["status"], "delivered")
        self.assertEqual(payload["results"][0]["decision"], "满足")

    def test_api_rejects_non_local_origin(self) -> None:
        """A foreign browser origin cannot use the local settings write endpoint."""
        self.connection.request(
            "POST",
            "/api/settings",
            body="{}",
            headers={"Content-Type": "application/json", "Origin": "https://attacker.invalid"},
        )
        response = self.connection.getresponse()
        payload = json.loads(response.read())
        self.assertEqual(response.status, 403)
        self.assertIn("本机", payload["error"])

    def test_shutdown_api_only_accepts_local_origin_and_acknowledges_first(self) -> None:
        """Launcher shutdown returns its acknowledgment before scheduling stop."""
        origin = f"http://127.0.0.1:{self.server.server_port}"
        shutdown_requested = threading.Event()
        with patch.object(self.server, "shutdown", side_effect=shutdown_requested.set):
            self.connection.request(
                "POST",
                "/api/shutdown",
                body="{}",
                headers={"Content-Type": "application/json", "Origin": origin},
            )
            response = self.connection.getresponse()
            payload = json.loads(response.read())

            self.assertEqual(response.status, 200)
            self.assertEqual(payload, {"stopping": True})
            self.assertTrue(shutdown_requested.wait(timeout=1))

    def test_reserved_siwx_mode_is_rejected_without_shutting_down_agent(self) -> None:
        """Reject the reserved SIWX mode without changing or stopping the Agent.

        验证当前版本拒绝启用 SIWX，且本机服务继续运行。
        """
        body = json.dumps({"wechat_input_mode": "siwx"})
        self.connection.request(
            "POST",
            "/api/settings",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{self.server.server_port}",
            },
        )
        response = self.connection.getresponse()
        payload = json.loads(response.read())

        self.assertEqual(response.status, 400)
        self.assertIn("未来预留，暂未启用", payload["error"])
        self.assertTrue(self.thread.is_alive())
        self.assertEqual(self.runtime.settings.wechat_input_mode, "manual")


if __name__ == "__main__":
    unittest.main()