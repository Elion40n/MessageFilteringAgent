"""LangGraph workflow for duplicate filtering and three-way message routing.

统一处理顺序：去重、分类，再投递、过滤或创建可恢复的人工确认事项。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from .config import Settings
from .models import AgentDecision, DecisionLabel, Message
from .storage import DedupStore


class Classifier(Protocol):
    """Injectable classifier contract; tests can provide an offline fake."""

    def classify(self, message: Message, settings: Settings) -> AgentDecision: ...


class WorkflowState(TypedDict, total=False):
    """Small transient graph state; durable pending work lives in SQLite."""

    message: Message
    status: str
    decision: AgentDecision
    pending_question_id: str
    error: str


class MessageWorkflow:
    """Compose the decision graph with persistence and output callbacks.

    图只负责单条消息的业务分流；模型或网络错误向上抛出，由来源轮询器重试。
    """

    def __init__(
        self,
        settings: Settings,
        storage: DedupStore,
        classifier: Classifier,
        deliver: Callable[[Message, AgentDecision], None],
        create_question: Callable[[Message, AgentDecision], str],
        memory_context: Callable[[], list[str]] | None = None,
        save_feedback_memory: Callable[[Message, str, str], str] | None = None,
        archive_filtered: Callable[[Message, AgentDecision], None] | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.classifier = classifier
        self.deliver = deliver
        self.create_question = create_question
        self.memory_context = memory_context or (lambda: [])
        self.save_feedback_memory = save_feedback_memory
        self.archive_filtered = archive_filtered or (lambda message, decision: None)
        self._graph = self._build_graph()

    def _build_graph(self):
        """Create the fixed three-way graph and its duplicate short-circuit."""
        graph = StateGraph(WorkflowState)
        graph.add_node("deduplicate", self._deduplicate)
        graph.add_node("classify", self._classify)
        graph.add_node("deliver", self._deliver)
        graph.add_node("filter", self._filter)
        graph.add_node("clarify", self._clarify)

        graph.add_edge(START, "deduplicate")
        graph.add_conditional_edges(
            "deduplicate",
            lambda state: "classify" if state.get("status") == "new" else END,
            {"classify": "classify", END: END},
        )
        graph.add_conditional_edges(
            "classify",
            self._route_decision,
            {"deliver": "deliver", "filter": "filter", "clarify": "clarify"},
        )
        graph.add_edge("deliver", END)
        graph.add_edge("filter", END)
        graph.add_edge("clarify", END)
        return graph.compile()

    def _deduplicate(self, state: WorkflowState) -> WorkflowState:
        """Claim the message before invoking a potentially costly classifier."""
        message = state["message"]
        registration = self.storage.register_message(
            message,
            self.settings.profile_id,
            self.settings.dedup_retention_days,
        )
        return {"status": "new" if registration.is_new else "duplicate"}

    def _classify(self, state: WorkflowState) -> WorkflowState:
        """Classify once; release the claim if the model call fails operationally."""
        try:
            decision = self._classify_message(state["message"])
        except Exception:
            self.storage.release_for_retry(state["message"], self.settings.profile_id)
            raise
        return {"decision": decision, "status": "classified"}

    def _classify_message(self, message: Message) -> AgentDecision:
        """Use profile memories only when the classifier supports supplemental context.

        仅在分类器支持时加入当前 profile 的补充记忆上下文。
        """
        classify_with_memory = getattr(self.classifier, "classify_with_memory", None)
        if callable(classify_with_memory):
            return classify_with_memory(message, self.settings, self.memory_context())
        return self.classifier.classify(message, self.settings)

    @staticmethod
    def _route_decision(state: WorkflowState) -> str:
        """Map the constrained business label to one graph branch."""
        decision = state["decision"].label
        if decision is DecisionLabel.SATISFIED:
            return "deliver"
        if decision is DecisionLabel.NOT_SATISFIED:
            return "filter"
        return "clarify"

    def _deliver(self, state: WorkflowState) -> WorkflowState:
        """Deliver matches and release dedupe state if the adapter raises."""
        try:
            self.deliver(state["message"], state["decision"])
        except Exception:
            self.storage.release_for_retry(state["message"], self.settings.profile_id)
            raise
        return {"status": "delivered"}

    def _filter(self, state: WorkflowState) -> WorkflowState:
        """Archive a formal non-match, then end without calling output adapters.

        正式流程中的不满足消息进入归档；归档失败时释放去重占用以便重试。
        """
        try:
            self.archive_filtered(state["message"], state["decision"])
        except Exception:
            self.storage.release_for_retry(state["message"], self.settings.profile_id)
            raise
        return {"status": "filtered"}

    def _clarify(self, state: WorkflowState) -> WorkflowState:
        """Persist an uncertain case before returning its review ID."""
        try:
            pending_id = self.create_question(state["message"], state["decision"])
        except Exception:
            self.storage.release_for_retry(state["message"], self.settings.profile_id)
            raise
        return {"status": "needs_input", "pending_question_id": pending_id}

    def process(self, message: Message) -> WorkflowState:
        """Run one normalized message through dedupe and three-way routing."""
        return self._graph.invoke({"message": message})

    def split_manual_transcript(self, transcript: str) -> list[str]:
        """Split pasted chat history before formal per-fragment processing.

        拆分调用只确定片段边界；所有片段（包括噪声）都保留并进入正式分类。
        """
        split_messages = getattr(self.classifier, "split_messages", None)
        if not callable(split_messages):
            raise RuntimeError("当前分类器未配置聊天记录拆分功能")
        segments = split_messages(transcript)
        if not isinstance(segments, list) or not segments:
            raise ValueError("模型未从聊天记录中拆分出可处理片段")
        if len(segments) > 100:
            raise ValueError("单段聊天记录最多拆分为 100 条消息")
        if any(not isinstance(segment, str) or not segment.strip() for segment in segments):
            raise ValueError("模型返回了空的聊天记录片段")
        normalized_segments = [segment.strip() for segment in segments]
        original_content = "".join(transcript.split())
        split_content = "".join("".join(normalized_segments).split())
        if split_content != original_content:
            raise ValueError("模型拆分结果未完整保留原聊天记录；未处理任何片段，请重试")
        return normalized_segments

    def resume_question(
        self, question_id: str, answer: str, choice: str
    ) -> WorkflowState:
        """Apply an explicit human choice and optionally save its reason as memory.

        明确的人工选择直接作为最终结果；理由只保存为记忆，不再次分类。
        """
        question = self.storage.get_pending_question(question_id, self.settings.profile_id)
        if not question or question["status"] != "pending":
            raise ValueError("待确认问题不存在或已处理")
        choices = {
            "satisfied": DecisionLabel.SATISFIED,
            "not_satisfied": DecisionLabel.NOT_SATISFIED,
            "satisfied_reason": DecisionLabel.SATISFIED,
            "not_satisfied_reason": DecisionLabel.NOT_SATISFIED,
        }
        if choice not in choices:
            raise ValueError("必须选择满足、不满足或对应的带理由判断")
        reasoned = choice.endswith("_reason")
        if reasoned and not answer.strip():
            raise ValueError("选择说明理由时必须填写理由")
        label = choices[choice]
        display = label.display_name
        if reasoned:
            if self.save_feedback_memory is None:
                raise ValueError("当前 Agent 未配置记忆保存功能")
            self.save_feedback_memory(question["message"], display, answer.strip())
        if not self.storage.append_question_answer(
            question_id, f"人工判断：{display}", self.settings.profile_id
        ):
            raise ValueError("无法保存对待确认问题的回答")
        decision = AgentDecision(
            label=label,
            explanation=f"用户对当前消息作出人工判断：{display}；未修改筛选规则。",
        )
        if label is DecisionLabel.SATISFIED:
            self.deliver(question["message"], decision)
            status = "delivered"
        else:
            if not question.get("source_archive_id"):
                self.archive_filtered(question["message"], decision)
            status = "filtered"
        self.storage.resolve_pending_question(question_id, status, self.settings.profile_id)
        return {"status": status, "decision": decision}