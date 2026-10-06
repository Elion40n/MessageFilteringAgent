"""Structured LLM classification using an OpenAI-compatible endpoint.

通过兼容 OpenAI 的模型接口输出受约束结构，不在模块导入时创建网络客户端。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .config import Settings
from .credentials import CredentialStore
from .models import AgentDecision, DecisionLabel, Message


class _StructuredDecision(BaseModel):
    """Strict schema the model must satisfy before becoming an AgentDecision.

    结构化输出只接受既定三态，字段证据和缺失项作为解释元数据返回。
    """

    decision: Literal["满足", "不满足", "不确定"]
    evidence: list[str] = Field(
        default_factory=list,
        description="逐条列出筛选条件及对应的消息原文证据；不得编造或只给结论。",
    )
    extracted_fields: dict[str, str] = Field(default_factory=dict)
    missing_fields: list[str] = Field(
        default_factory=list,
        description="不确定时，逐项列出筛选条件要求但消息没有明确描述的字段。",
    )
    explanation: str = Field(
        min_length=1,
        description="必填的简短判断理由，解释条件与消息事实如何吻合、不符或缺失。",
    )

    @field_validator("explanation")
    @classmethod
    def explanation_must_not_be_blank(cls, value: str) -> str:
        explanation = value.strip()
        if not explanation:
            raise ValueError("必须提供非空的判断理由")
        return explanation


class _StructuredMessageSplit(BaseModel):
    """Ordered message-like fragments extracted from one pasted transcript.

    拆分结果必须保留原始顺序和原文；闲聊也作为片段返回，由正式分类阶段过滤。
    """

    segments: list[str] = Field(
        min_length=1,
        max_length=100,
        description="按原始顺序拆分出的消息片段，必须保留原文且不能丢弃闲聊或杂讯。",
    )

    @field_validator("segments")
    @classmethod
    def segments_must_contain_text(cls, values: list[str]) -> list[str]:
        segments = [value.strip() for value in values]
        if any(not value for value in segments):
            raise ValueError("拆分片段不能为空")
        return segments


class OpenAICompatibleClassifier:
    """Construct a lazy-use classifier from settings and OS-stored credentials.

    API 密钥从系统凭据库取出；基础 URL 可留空使用客户端默认服务。
    """

    def __init__(self, settings: Settings, credentials: CredentialStore) -> None:
        from langchain_openai import ChatOpenAI

        api_key = credentials.get_model_api_key(settings.llm_base_url)
        if not api_key:
            raise ValueError("请先在本地界面配置模型 API 密钥")
        options: dict[str, object] = {
            "model": settings.llm_model,
            "api_key": api_key,
            "temperature": 0,
            "max_retries": 2,
        }
        if settings.llm_base_url.strip():
            options["base_url"] = settings.llm_base_url.strip()
        self._chat_model = ChatOpenAI(**options)
        self._model = self._chat_model.with_structured_output(_StructuredDecision)
        self._split_model = self._chat_model.with_structured_output(_StructuredMessageSplit)

    def split_messages(self, transcript: str) -> list[str]:
        """Split one pasted chat transcript into ordered message-like fragments.

        只拆分边界，不筛选或改写内容；后续正式分类负责过滤闲聊和杂讯。
        """
        if not transcript.strip():
            raise ValueError("聊天记录不能为空")
        if len(transcript) > 200_000:
            raise ValueError("单次聊天记录不能超过 200,000 个字符")
        result = self._split_model.invoke(
            [
                {
                    "role": "system",
                    "content": (
                        "你负责拆分用户粘贴的聊天记录，不负责筛选信息。"
                        "按原始顺序把其中彼此独立的消息或信息条目拆成片段；同一条消息的换行、标题和正文应留在同一片段。"
                        "每个片段必须逐字保留原文，不得总结、纠错、改写或补写。"
                        "必须保留所有内容，包括闲聊、表情说明、系统提示和看似无关的杂讯；不得删除任何片段。"
                        "如果无法确定边界，就合并相邻文本，不要冒险丢弃内容。"
                        "segments 按原文顺序返回。"
                    ),
                },
                {"role": "user", "content": transcript},
            ]
        )
        return result.segments

    def classify(self, message: Message, settings: Settings) -> AgentDecision:
        """Ask the model to classify one message under the current user criteria.

        按当前用户筛选条件分类一条消息，不附加人工记忆。
        """
        return self.classify_with_memory(message, settings, [])

    def classify_with_memory(
        self, message: Message, settings: Settings, memories: list[str]
    ) -> AgentDecision:
        """Add bounded profile memories as supplemental, non-authoritative context.

        将有限数量的 profile 记忆作为补充上下文，当前规则仍优先。
        """
        system_prompt = (
            "你是消息筛选器。仅依据用户定义的信息类型和筛选条件分析消息。"
            "历史人工经验仅作补充参考；如果它与当前筛选条件冲突，必须以当前筛选条件为准。"
            "必须且只能返回一个判断：满足、不满足、不确定。"
            "信息不足、字段冲突或无法可靠判断时返回不确定。"
            "严格按筛选条件的逻辑判断：条件中的‘或’是备选项，命中任一备选即可满足；消息还出现其他不符合的地点或信息，不会自动抵消已命中的备选，除非用户明确要求排除或仅限于指定范围。"
            "按用户表达的粒度判断：消息级条件按整条消息核对；不要擅自要求列表中的每个实体或条目逐一建立属性配对。不得把用户未写明的属性关联、逐项对应或额外条件加入筛选标准；用户没有要求同一实体关联时，不能因为关联信息未提供而判不确定或不满足，也不得列入missing_fields。若用户条件只要求消息整体包含若干属性，而原文在同一消息中分别明确包含这些属性，不要额外要求它们关联到同一个实体。只有用户明确要求同一实体同时具备多个属性时，才要求原文能证明这种对应关系。"
            "只有消息明确违反必需条件或明确命中排除项时才判不满足。若缺少用户明确要求的字段关系、字段未说明或存在歧义，判不确定并指出缺少项，不得把无法确认写成不满足。"
            "若片段明显是闲聊、寒暄、无关系统通知或其他与用户指定信息类型无关的内容，应判不满足并说明它与目标类型无关；不要仅因这类噪声没有目标字段就判不确定。"
            "必须提供非空、简洁、可核查的判断理由，不要输出冗长的内部推理。"
            "判断为满足时，指出具体哪条筛选条件与消息中的哪项事实吻合，并引用原文。"
            "判断为不满足时，指出具体哪条条件不符合或触发了哪个排除项，并引用原文。"
            "判断为不确定时，指出哪条必要条件在消息中没有明确描述或存在歧义；missing_fields逐项列出这些字段，不得把沉默当作满足或不满足。"
            "判断标签与理由必须一致：若理由表示条件已命中，不得判不满足；若理由表示无法确认，不得判不满足。evidence逐项对应筛选条件和消息事实，提取字段不得编造。"
        )
        user_prompt = (
            f"信息类型：{settings.message_type}\n"
            f"筛选条件：{settings.criteria}\n"
            f"历史人工经验（补充参考）：\n{self._format_memories(memories)}\n"
            f"来源：{message.source.value} / {message.conversation_id}\n"
            f"发送者：{message.sender}\n"
            f"时间：{message.timestamp.isoformat()}\n"
            f"消息正文：\n{message.content}"
        )
        result = self._model.invoke(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
        )
        label = {
            "满足": DecisionLabel.SATISFIED,
            "不满足": DecisionLabel.NOT_SATISFIED,
            "不确定": DecisionLabel.UNCERTAIN,
        }[result.decision]
        return AgentDecision(
            label=label,
            evidence=tuple(result.evidence),
            extracted_fields=result.extracted_fields,
            missing_fields=tuple(result.missing_fields),
            explanation=result.explanation,
        )

    def summarize_feedback(
        self, message: Message, decision: str, reason: str
    ) -> str:
        """Distill an explicit user reason into a concise, editable memory entry.

        将用户明确给出的理由提炼为简洁、可编辑的记忆条目。
        """
        result = self._chat_model.invoke(
            [
                {
                    "role": "system",
                    "content": (
                        "将用户明确给出的判断理由压缩为一条简洁、可复用的筛选经验。"
                        "不得添加理由中没有的信息，不得改写或扩展用户的筛选规则。"
                        "只输出经验文本，不要标题。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"人工判断：{decision}\n"
                        f"消息类型：{message.source.value}\n"
                        f"用户理由：{reason[:4000]}"
                    ),
                },
            ]
        )
        summary = result.content
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("模型未能生成有效的记忆摘要")
        # Bound stored prompt context even if the model returns an unexpectedly long answer.
        # 即使模型输出异常冗长，也限制写入记忆的文本长度。
        return summary.strip()[:4000]

    @staticmethod
    def _format_memories(memories: list[str]) -> str:
        """Bound the amount of memory included in one model request.

        限制单次模型请求中包含的记忆数量和总字符数。
        """
        content = "\n".join(f"- {item}" for item in memories[-30:])
        return content[:10000] if content else "无"