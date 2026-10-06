"""Offline tests for three-way routing, retries, and human clarification resume.

分类器与投递器均由假对象替代，测试不会发起模型请求或发送邮件。
"""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from message_filtering_agent.config import Settings
from message_filtering_agent.models import AgentDecision, DecisionLabel, Message, MessageSource
from message_filtering_agent.storage import DedupStore
from message_filtering_agent.workflow import MessageWorkflow


class FakeClassifier:
    """Deterministic classifier fixture that records invocation count."""

    def __init__(self, label: DecisionLabel) -> None:
        self.label = label
        self.calls = 0
        self.messages = []

    def classify(self, message: Message, settings: Settings) -> AgentDecision:
        """Return the selected test state without any model/network calls."""
        self.calls += 1
        self.messages.append(message)
        return AgentDecision(label=self.label, explanation="test")


class FlakyClassifier(FakeClassifier):
    """Fail once to prove operational errors leave a message retryable."""

    def __init__(self, label: DecisionLabel) -> None:
        super().__init__(label)
        self.should_fail = True

    def classify(self, message: Message, settings: Settings) -> AgentDecision:
        """Simulate one transient failure, then return the configured decision."""
        self.calls += 1
        if self.should_fail:
            self.should_fail = False
            raise RuntimeError("temporary model failure")
        return AgentDecision(label=self.label, explanation="test")


class WorkflowTests(unittest.TestCase):
    """Assert each decision branch and durable resume behavior in isolation."""

    def setUp(self) -> None:
        """Create fresh storage and output spies for each workflow scenario."""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.temporary_directory.name))
        self.storage = DedupStore(self.settings.database_path)
        self.delivered = []
        self.questions = []
        self.archived = []

    def tearDown(self) -> None:
        """Close the temporary persistence fixture after each test."""
        self.temporary_directory.cleanup()

    @staticmethod
    def message(content: str, external_id: str | None = None) -> Message:
        """Build a local test record with a stable, timezone-aware timestamp."""
        return Message(
            source=MessageSource.MANUAL,
            account_id="local",
            conversation_id="inbox",
            timestamp=datetime(2026, 10, 3, tzinfo=timezone.utc),
            sender="sender",
            content=content,
            external_id=external_id,
        )

    def workflow(self, label: DecisionLabel) -> tuple[MessageWorkflow, FakeClassifier]:
        """Wire fake output callbacks while persisting questions in real SQLite."""
        classifier = FakeClassifier(label)

        def create_question(message: Message, decision: AgentDecision) -> str:
            question_id = self.storage.create_pending_question(message, decision)
            self.questions.append((message, decision))
            return question_id

        def archive_filtered(message: Message, decision: AgentDecision) -> None:
            self.archived.append((message, decision))

        workflow = MessageWorkflow(
            self.settings,
            self.storage,
            classifier,
            lambda message, decision: self.delivered.append((message, decision)),
            create_question,
            archive_filtered=archive_filtered,
        )
        return workflow, classifier

    def test_manual_split_keeps_every_fragment_in_order(self) -> None:
        workflow, classifier = self.workflow(DecisionLabel.SATISFIED)
        transcript = "闲聊：早上好\n目标信息：上海招聘"
        classifier.split_messages = lambda _text: ["闲聊：早上好", "目标信息：上海招聘"]

        segments = workflow.split_manual_transcript(transcript)

        self.assertEqual(segments, ["闲聊：早上好", "目标信息：上海招聘"])
        self.assertEqual(classifier.calls, 0)

    def test_manual_split_rejects_dropped_text_before_classification(self) -> None:
        workflow, classifier = self.workflow(DecisionLabel.SATISFIED)
        classifier.split_messages = lambda _text: ["目标信息：上海招聘"]

        with self.assertRaisesRegex(ValueError, "未完整保留"):
            workflow.split_manual_transcript("闲聊：早上好\n目标信息：上海招聘")

        self.assertEqual(classifier.calls, 0)
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.archived, [])
        self.assertEqual(self.questions, [])

    def test_satisfied_is_delivered(self) -> None:
        """Only the satisfied outcome reaches the delivery callback."""
        workflow, _ = self.workflow(DecisionLabel.SATISFIED)
        result = workflow.process(self.message("Suitable listing"))
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(len(self.delivered), 1)

    def test_not_satisfied_is_filtered(self) -> None:
        """A non-match terminates without sending or asking the user."""
        workflow, _ = self.workflow(DecisionLabel.NOT_SATISFIED)
        result = workflow.process(self.message("Unrelated message"))
        self.assertEqual(result["status"], "filtered")
        self.assertEqual(self.delivered, [])
        self.assertEqual(self.archived[0][0].content, "Unrelated message")
        self.assertEqual(self.archived[0][1].label, DecisionLabel.NOT_SATISFIED)

    def test_promoted_item_is_not_rearchived_after_either_human_rejection(self) -> None:
        """A promoted filtered entry stays removed regardless of review outcome.

        过滤信息转待处理后，直接或带理由的人工判断都不重新归档。
        """
        direct_message = self.message("Direct rejection", "promoted-direct")
        reasoned_message = self.message("Reasoned rejection", "promoted-reasoned")
        original_decision = AgentDecision(
            DecisionLabel.NOT_SATISFIED,
            explanation="首次过滤理由。",
        )
        direct_archive = self.storage.archive_filtered_message(
            direct_message,
            original_decision,
            self.settings.profile_id,
        )
        reasoned_archive = self.storage.archive_filtered_message(
            reasoned_message,
            original_decision,
            self.settings.profile_id,
        )
        direct_question = self.storage.promote_filtered_message(
            direct_archive, self.settings.profile_id
        )
        reasoned_question = self.storage.promote_filtered_message(
            reasoned_archive, self.settings.profile_id
        )
        workflow, classifier = self.workflow(DecisionLabel.UNCERTAIN)
        workflow.save_feedback_memory = lambda *_: ""

        direct_result = workflow.resume_question(
            direct_question, "", "not_satisfied"
        )
        reasoned_result = workflow.resume_question(
            reasoned_question, "条件不符合", "not_satisfied_reason"
        )

        self.assertEqual(direct_result["status"], "filtered")
        self.assertEqual(reasoned_result["status"], "filtered")
        self.assertEqual(classifier.calls, 0)
        self.assertEqual(self.archived, [])
        self.assertEqual(
            self.storage.list_filtered_messages(self.settings.profile_id), []
        )
        self.assertEqual(
            self.storage.list_pending_questions(self.settings.profile_id), []
        )

    def test_uncertain_is_paused_for_user(self) -> None:
        """An uncertain result becomes a durable item for the local review UI."""
        workflow, _ = self.workflow(DecisionLabel.UNCERTAIN)
        result = workflow.process(self.message("Incomplete listing"))
        self.assertEqual(result["status"], "needs_input")
        self.assertIsNotNone(self.storage.get_pending_question(result["pending_question_id"]))

    def test_direct_human_judgment_is_authoritative_without_reclassification(self) -> None:
        """A direct human decision settles the current item without another model call.

        验证直接人工判断立即生效，且不会再次调用分类器。
        """
        workflow, classifier = self.workflow(DecisionLabel.UNCERTAIN)
        workflow.process(self.message("Listing"))
        question_id = self.storage.list_pending_questions()[0]["question_id"]

        result = workflow.resume_question(question_id, "", "satisfied")

        self.assertEqual(result["status"], "delivered")
        self.assertEqual(result["decision"].label, DecisionLabel.SATISFIED)
        self.assertEqual(classifier.calls, 1)
        self.assertEqual(self.storage.list_pending_questions(), [])

    def test_invalid_human_choice_does_not_change_pending_item(self) -> None:
        """A missing/unknown choice is rejected without reclassification.

        缺少人工判断选项时拒绝提交，问题保持待处理且不再次分类。
        """
        workflow, classifier = self.workflow(DecisionLabel.UNCERTAIN)
        workflow.process(self.message("Listing"))
        question_id = self.storage.list_pending_questions()[0]["question_id"]

        with self.assertRaisesRegex(ValueError, "必须选择"):
            workflow.resume_question(question_id, "任意补充", "")

        self.assertEqual(classifier.calls, 1)
        self.assertEqual(len(self.storage.list_pending_questions()), 1)

    def test_reasoned_feedback_is_saved_and_human_choice_is_final(self) -> None:
        """A reasoned human decision is stored without another classifier call.

        验证理由保存为记忆，人工选择直接生效且不再次调用分类器。
        """
        workflow, classifier = self.workflow(DecisionLabel.UNCERTAIN)
        saved_memories = []
        def save_memory(message, decision, reason):
            """Capture the memory callback without invoking an external model.

            用测试替身记录记忆回调，避免访问外部模型。
            """
            saved_memories.append((message.content, decision, reason))
            return "地点必须明确为上海"

        workflow.save_feedback_memory = save_memory
        workflow.process(self.message("Listing"))
        question_id = self.storage.list_pending_questions()[0]["question_id"]
        result = workflow.resume_question(
            question_id, "岗位地点必须明确为上海", "not_satisfied_reason"
        )

        self.assertEqual(result["status"], "filtered")
        self.assertEqual(result["decision"].label, DecisionLabel.NOT_SATISFIED)
        self.assertEqual(saved_memories, [("Listing", "不满足", "岗位地点必须明确为上海")])
        self.assertEqual(classifier.calls, 1)
        self.assertEqual(len(classifier.messages), 1)
        self.assertEqual(self.archived[-1][0].content, "Listing")
        self.assertEqual(self.storage.list_pending_questions(), [])
        completed = self.storage.get_pending_question(question_id)
        self.assertNotIn("岗位地点必须明确为上海", " ".join(completed["answers"]))

    def test_reasoned_satisfied_choice_delivers_without_reclassification(self) -> None:
        """A reasoned satisfied choice delivers the original item as selected.

        带理由的“满足”直接投递原消息，不进行二次分类。
        """
        workflow, classifier = self.workflow(DecisionLabel.UNCERTAIN)
        saved_memories = []
        workflow.save_feedback_memory = lambda message, decision, reason: saved_memories.append(
            (message.content, decision, reason)
        ) or ""
        workflow.process(self.message("Listing"))
        question_id = self.storage.list_pending_questions()[0]["question_id"]

        result = workflow.resume_question(
            question_id, "薪资范围符合我的要求", "satisfied_reason"
        )

        self.assertEqual(result["status"], "delivered")
        self.assertEqual(result["decision"].label, DecisionLabel.SATISFIED)
        self.assertEqual(classifier.calls, 1)
        self.assertEqual(self.delivered[0][0].content, "Listing")
        self.assertEqual(saved_memories, [("Listing", "满足", "薪资范围符合我的要求")])
        self.assertEqual(self.storage.list_pending_questions(), [])

    def test_active_profile_memories_are_passed_to_classifier(self) -> None:
        """Supply only the active profile's memory context to classification.

        验证工作流向分类器传入当前 profile 的记忆上下文。
        """
        workflow, classifier = self.workflow(DecisionLabel.SATISFIED)
        classifier.received_memories = None

        def classify_with_memory(message, settings, memories):
            classifier.received_memories = memories
            return classifier.classify(message, settings)

        classifier.classify_with_memory = classify_with_memory
        workflow.memory_context = lambda: ["仅处理上海岗位"]
        workflow.process(self.message("Listing"))
        self.assertEqual(classifier.received_memories, ["仅处理上海岗位"])

    def test_reasoned_choice_requires_a_reason(self) -> None:
        """Reject a reasoned choice with an empty explanation.

        验证选择“说明理由”时必须提供非空理由。
        """
        workflow, _ = self.workflow(DecisionLabel.UNCERTAIN)
        workflow.process(self.message("Listing"))
        question_id = self.storage.list_pending_questions()[0]["question_id"]
        workflow.save_feedback_memory = lambda *_: None

        with self.assertRaisesRegex(ValueError, "必须填写理由"):
            workflow.resume_question(question_id, "  ", "satisfied_reason")

    def test_duplicate_is_not_classified_or_delivered_twice(self) -> None:
        """A replay short-circuits before model invocation and output delivery."""
        workflow, classifier = self.workflow(DecisionLabel.SATISFIED)
        message = self.message("Suitable listing", "message-1")
        workflow.process(message)
        result = workflow.process(message)
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(classifier.calls, 1)
        self.assertEqual(len(self.delivered), 1)

    def test_model_failure_releases_message_for_retry(self) -> None:
        """A transient model exception must not leave a permanent dedupe claim."""
        classifier = FlakyClassifier(DecisionLabel.SATISFIED)
        workflow = MessageWorkflow(
            self.settings,
            self.storage,
            classifier,
            lambda message, decision: self.delivered.append((message, decision)),
            lambda message, decision: self.storage.create_pending_question(message, decision),
        )
        message = self.message("Retry this message", "message-retry")
        with self.assertRaisesRegex(RuntimeError, "temporary model failure"):
            workflow.process(message)
        result = workflow.process(message)
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(classifier.calls, 2)


if __name__ == "__main__":
    unittest.main()