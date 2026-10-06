"""Application composition and local server entry point.

组合存储、模型、投递与来源运行器，并提供本地服务命令入口。
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import socket
from uuid import uuid4

from .config import Settings, default_config_path, profile_config_path
from .credentials import CredentialStore
from .delivery.qq_mail import QQMailDelivery
from .llm import OpenAICompatibleClassifier
from .memory import MemoryStore
from .models import AgentDecision, Message, MessageSource
from .power import KeepAwake
from .sources.runner import SourceRunner
from .storage import DedupStore
from .ui import serve
from .workflow import MessageWorkflow


def _available_port(start: int) -> int:
    """Choose a free loopback port at or above the selected profile's port.

    从当前配置端口开始，查找可供 profile 使用的本机空闲端口。
    """
    for port in range(start, min(start + 100, 65536)):
        # The probe socket closes before the HTTP server binds the selected port.
        # 探测套接字会先关闭，随后本地 HTTP 服务再绑定选中的端口。
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise OSError("附近没有可用的本地界面端口，请通过 --port 指定")


class LazyClassifier:
    """Create the remote model client only when classification is first needed.

    设置页和手动输入页可先启动；缺少 API 密钥时仅在真正分类时提示用户。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._classifier: OpenAICompatibleClassifier | None = None

    def classify(self, message: Message, settings: Settings) -> AgentDecision:
        """Delegate to a cached classifier after its first on-demand creation."""
        if self._classifier is None:
            self._classifier = OpenAICompatibleClassifier(settings, CredentialStore())
        return self._classifier.classify(message, settings)

    def classify_with_memory(
        self, message: Message, settings: Settings, memories: list[str]
    ) -> AgentDecision:
        """Lazily initialize the model and classify with profile memories.

        按需初始化模型，并将当前 profile 的记忆用于分类。
        """
        if self._classifier is None:
            self._classifier = OpenAICompatibleClassifier(settings, CredentialStore())
        return self._classifier.classify_with_memory(message, settings, memories)

    def split_messages(self, transcript: str) -> list[str]:
        """Lazily split a pasted transcript with the shared model client.

        使用和正式分类相同的延迟模型客户端拆分聊天记录。
        """
        if self._classifier is None:
            self._classifier = OpenAICompatibleClassifier(self.settings, CredentialStore())
        return self._classifier.split_messages(transcript)

    def summarize_feedback(self, message: Message, decision: str, reason: str) -> str:
        """Summarize user feedback through the same lazily loaded model client.

        复用延迟初始化的模型客户端，总结用户反馈理由。
        """
        if self._classifier is None:
            self._classifier = OpenAICompatibleClassifier(self.settings, CredentialStore())
        return self._classifier.summarize_feedback(message, decision, reason)


class Runtime:
    """Own long-lived application services and expose UI-friendly operations.

    将可变运行状态集中在此处，UI 不直接操作数据库、线程或外部协议。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage = DedupStore(settings.database_path)
        self.memory = MemoryStore(settings.database_path, settings.profile_id)
        self.keep_awake = KeepAwake()
        self.workflow = self._build_workflow()
        self.source_runner = SourceRunner(self)

    def _build_workflow(self) -> MessageWorkflow:
        """Wire classification, delivery, and durable clarification callbacks.

        连接分类、投递、profile 记忆和持久化人工确认回调。
        """
        def deliver(message: Message, decision: AgentDecision) -> None:
            if self.settings.output_channel == "qq_message":
                raise NotImplementedError("QQ 消息输出尚未配置可接受的接入方式")
            # A positive decision is the only route that reaches the selected output adapter.
            # 只有“满足”分支会进入投递回调，其他判断不会误触发发送。
            QQMailDelivery(self.settings, CredentialStore()).send(message, decision)

        classifier = LazyClassifier(self.settings)
        return MessageWorkflow(
            self.settings,
            self.storage,
            classifier,
            deliver,
            lambda message, decision: self.storage.create_pending_question(
                message, decision, self.settings.profile_id
            ),
            self.memory.active_context,
            lambda message, decision, reason: self._save_feedback_memory(
                classifier, message, decision, reason
            ),
            lambda message, decision: self.storage.archive_filtered_message(
                message,
                decision,
                self.settings.profile_id,
                self.settings.filtered_retention_days,
            ),
        )

    def _save_feedback_memory(
        self, classifier: LazyClassifier, message: Message, decision: str, reason: str
    ) -> str:
        """Summarize and store one explicitly reasoned judgment for this profile.

        总结并保存一条明确给出理由的人工判断，归属当前 profile。
        """
        summary = classifier.summarize_feedback(message, decision, reason)
        self.memory.add_entry(
            summary,
            decision,
            source=f"{message.source.value}:{message.conversation_id}",
            tags=["人工理由"],
        )
        return summary

    def update_settings(self, changes: dict[str, object]) -> Settings:
        """Persist settings and bind the profile name once during initialization.

        首次保存时按 profile 名命名配置文件，此后锁定该名称。
        """
        allowed = {
            "profile_id", "dedup_retention_days", "filtered_retention_days", "port", "wechat_input_mode",
            "siwx_export_dir", "siwx_auto_export_enabled", "siwx_api_url",
            "siwx_account", "siwx_chats", "siwx_auto_export_start_date",
            "siwx_auto_export_interval_days", "siwx_auto_cleanup_enabled",
            "siwx_auto_cleanup_retention_days",
            "message_type", "criteria",
            "llm_model", "llm_base_url", "output_channel", "mail_imap_host",
            "mail_imap_port", "mail_folder", "mail_ingestion_enabled",
            "poll_interval_seconds", "mail_smtp_host", "mail_smtp_port",
            "mail_username", "notification_recipient",
        }
        normalized: dict[str, object] = {}
        for key, value in changes.items():
            if key not in allowed:
                continue
            current = getattr(self.settings, key)
            if isinstance(current, bool):
                normalized[key] = (
                    value
                    if isinstance(value, bool)
                    else str(value).strip().lower() in {"true", "1", "yes", "on"}
                )
            elif isinstance(current, int):
                normalized[key] = int(value)
            else:
                normalized[key] = str(value)
        if "profile_id" in normalized:
            normalized["profile_id"] = str(normalized["profile_id"]).strip()
        if normalized.get("wechat_input_mode") == "siwx":
            raise ValueError("SIWX 微信输入目前仅作为未来预留，暂未启用")

        previous_settings = self.settings
        requested_mode = normalized.get("wechat_input_mode", self.settings.wechat_input_mode)
        mode_changed = requested_mode != self.settings.wechat_input_mode
        updated = replace(self.settings, **normalized)
        current_path = previous_settings.settings_path
        profile_changed = updated.profile_id != previous_settings.profile_id
        if previous_settings.profile_name_bound and profile_changed:
            raise ValueError("筛选配置名称初始化后不可更改")

        target_path = (
            profile_config_path(current_path, updated.profile_id)
            if not previous_settings.profile_name_bound
            else current_path
        )
        same_path = os.path.normcase(str(current_path.resolve())) == os.path.normcase(
            str(target_path.resolve())
        )
        if target_path.exists() and not same_path:
            raise ValueError(f"同目录已存在配置文件 {target_path.name}，未保存筛选配置名称")
        config_path_changed = not same_path
        must_stop_sources = profile_changed or config_path_changed
        if must_stop_sources:
            self.source_runner.stop()

        profile_migrated = False
        target_created = False
        try:
            if profile_changed:
                self.storage.rename_profile(previous_settings.profile_id, updated.profile_id)
                profile_migrated = True
            updated = replace(
                updated,
                config_file=target_path,
                profile_name_bound=True,
            )
            updated.save()
            target_created = config_path_changed
            if config_path_changed and current_path.exists():
                current_path.unlink()
        except Exception:
            if target_created:
                target_path.unlink(missing_ok=True)
            if profile_migrated:
                self.storage.rename_profile(updated.profile_id, previous_settings.profile_id)
            if must_stop_sources:
                self.source_runner.start()
            raise

        if mode_changed and not must_stop_sources:
            self.source_runner.request_stop()
        self.settings = updated
        if updated.profile_id != self.memory.profile_id:
            self.memory = MemoryStore(updated.database_path, updated.profile_id)
        if mode_changed or profile_changed or config_path_changed:
            return updated
        self.workflow = self._build_workflow()
        self.source_runner.start()
        return updated

    def _manual_message(self, content: str) -> Message:
        """Wrap pasted text in the normalized message contract."""
        if not content.strip():
            raise ValueError("消息正文不能为空")
        return Message(
            source=MessageSource.MANUAL,
            account_id="local-user",
            conversation_id="manual-input",
            timestamp=datetime.now(timezone.utc),
            sender="手动输入",
            content=content.strip(),
            external_id=uuid4().hex,
            source_ref="manual://local-input",
        )

    def process_manual_message(self, content: str) -> dict[str, object]:
        """Process one pasted message through the regular production workflow."""
        return self.workflow.process(self._manual_message(content))

    def process_manual_transcript(self, transcript: str) -> dict[str, object]:
        """Split one pasted chat log, then formally process each fragment.

        拆分前不产生副作用；随后每个片段独立去重、分类并分流。
        """
        if not transcript.strip():
            raise ValueError("聊天记录不能为空")
        if len(transcript) > 200_000:
            raise ValueError("单次聊天记录不能超过 200,000 个字符")
        segments = self.workflow.split_manual_transcript(transcript)
        results = []
        for segment in segments:
            try:
                results.append(self.process_manual_message(segment))
            except Exception as error:
                results.append({"status": "error", "error": str(error)})
        return {"segments": segments, "results": results}

    def process_manual_transcripts(
        self, transcripts: list[str]
    ) -> list[dict[str, object]]:
        """Process several independent chat transcripts as one UI submission.

        每段记录独立拆分；某段失败只影响本段，不阻断其他记录。
        """
        if not transcripts:
            raise ValueError("至少填写一段聊天记录")
        if len(transcripts) > 20:
            raise ValueError("每次最多添加 20 段聊天记录")
        if any(not isinstance(item, str) or not item.strip() for item in transcripts):
            raise ValueError("聊天记录不能为空")
        if any(len(item) > 200_000 for item in transcripts):
            raise ValueError("单段聊天记录不能超过 200,000 个字符")
        if sum(len(item) for item in transcripts) > 1_000_000:
            raise ValueError("本次提交的聊天记录总长度不能超过 1,000,000 个字符")

        groups = []
        for index, transcript in enumerate(transcripts, start=1):
            try:
                result = self.process_manual_transcript(transcript)
            except Exception as error:
                result = {
                    "segments": [],
                    "results": [{"status": "error", "error": str(error)}],
                }
            groups.append({"transcript_index": index, **result})
        return groups

    def answer_question(
        self, question_id: str, answer: str, choice: str
    ) -> None:
        """Apply the user's final judgment to one durable review item.

        将用户的最终判断交给工作流，不进行二次分类。
        """
        self.workflow.resume_question(question_id, answer, choice)

    def add_memory(self, data: dict[str, object]) -> dict[str, object]:
        """Create one profile-scoped memory record from local UI input.

        从本机界面新增一条归属当前 profile 的记忆。
        """
        tags = data.get("tags", [])
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise ValueError("记忆标签必须是字符串列表")
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("记忆启用状态必须是布尔值")
        return self.memory.add_entry(
            str(data.get("content", "")),
            decision=str(data.get("decision", "补充")),
            source=str(data.get("source", "manual")),
            tags=tags,
            enabled=enabled,
        )

    def update_memory(self, memory_id: str, changes: dict[str, object]) -> bool:
        """Update one memory through the active profile store.

        通过当前 profile 的记忆存储更新记录。
        """
        return self.memory.update_entry(memory_id, changes)

    def delete_memory(self, memory_id: str) -> bool:
        """Delete one memory through the active profile store.

        通过当前 profile 的记忆存储删除记录。
        """
        return self.memory.delete_entry(memory_id)

    def start_sources(self) -> None:
        """Start enabled polling and optional live-listener workers."""
        self.source_runner.start()

    def stop_sources(self) -> None:
        """Stop background source workers before the process exits."""
        self.source_runner.stop()


def main(argv: list[str] | None = None) -> None:
    """Parse local overrides and guarantee source/power cleanup on exit.

    读取启动参数，并确保退出时清理来源线程与防睡眠请求。
    """
    parser = argparse.ArgumentParser(description="本地消息筛选 Agent")
    parser.add_argument("--port", type=int, help="本地网页端口，默认读取设置")
    parser.add_argument("--data-dir", type=Path, help="覆盖本地数据目录")
    parser.add_argument("--config", type=Path, help="指定此 Agent 使用的 JSON 配置文件")
    args = parser.parse_args(argv)
    settings_path = args.config
    if settings_path is None:
        settings_path = default_config_path(args.data_dir)
    settings = Settings.load(settings_path)
    if args.data_dir:
        settings = replace(
            settings, data_dir=args.data_dir, config_file=settings.settings_path
        )
    if args.port:
        settings = replace(settings, port=args.port)
    elif args.config:
        # Separate concurrent profiles when their configured localhost port is already in use.
        # 并行启动多个 profile 时，若首选端口占用则为本机界面另选端口。
        settings = replace(settings, port=_available_port(settings.port))

    runtime = Runtime(settings)
    try:
        if runtime.keep_awake.start():
            print("Windows 防睡眠请求已启用；屏幕仍可关闭。")
        runtime.start_sources()
        serve(runtime, settings.port)
    finally:
        # Always release keep-awake state even if server startup or shutdown fails.
        # 即使服务启动或关闭时异常，也必须释放系统防睡眠请求。
        runtime.stop_sources()
        runtime.keep_awake.stop()


if __name__ == "__main__":
    main()