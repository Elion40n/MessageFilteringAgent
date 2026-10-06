"""Background polling lifecycle for enabled sources.

管理邮箱/SIWX 轮询；各来源错误不应静默推进游标。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
from pathlib import Path
import threading

from ..credentials import CredentialStore
from ..models import Message
from ..storage import DedupStore
from ..workflow import MessageWorkflow
from .qq_mail import QQMailSource
from .siwx_export import SIWXExportSource
from .siwx_client import SIWXExportClient


class SourceRunner:
    """Run enabled source adapters and hand normalized messages to the workflow."""

    def __init__(self, runtime: object) -> None:
        self.runtime = runtime
        self._stop = threading.Event()
        self._poll_thread: threading.Thread | None = None

    def start(self) -> None:
        """Start workers once; repeated calls do not create duplicate threads."""
        self._stop.clear()
        if not self._poll_thread or not self._poll_thread.is_alive():
            self._poll_thread = threading.Thread(
                target=self._poll_loop,
                name="message-source-poller",
                daemon=True,
            )
            self._poll_thread.start()

    def stop(self) -> None:
        """Signal all workers and wait briefly for cooperative shutdown.

        通知全部来源线程退出，并等待其完成协作式清理。
        """
        self.request_stop()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)

    def request_stop(self) -> None:
        """Prevent listeners from accepting more messages before joining workers.

        在等待线程退出前，先阻止监听器继续接收和处理消息。
        """
        self._stop.set()

    def _poll_loop(self) -> None:
        """Poll enabled sources until stopped, logging errors before retrying."""
        while not self._stop.is_set():
            try:
                self._poll_enabled_sources()
            except Exception as error:
                # Keep the worker alive on transient network/auth failures; the same cursor is retried.
                # 网络或认证暂时失败时保留原游标，下一轮再尝试，而不是跳过消息。
                print(f"来源同步暂时失败，将重试：{error}")
            self._stop.wait(self.runtime.settings.poll_interval_seconds)

    def _poll_enabled_sources(self) -> None:
        """Run one polling pass for each enabled durable source."""
        settings = self.runtime.settings
        if settings.mail_ingestion_enabled:
            source = QQMailSource(
                settings,
                CredentialStore(),
                self.runtime.storage,
            )
            for envelope in source.fetch_new():
                # Do not start another message after a config change requested shutdown.
                # 配置变更请求关闭后，不再开始处理下一封邮件。
                if self._stop.is_set():
                    return
                # A message is checkpointed only after the workflow returns successfully.
                # 只有工作流成功返回后才推进 UID；失败时下轮仍会重新读取该邮件。
                self.runtime.workflow.process(envelope.message)
                self.runtime.storage.save_checkpoint(
                    "qq_mail",
                    envelope.message.account_id,
                    envelope.message.conversation_id,
                    envelope.cursor,
                    profile_id=settings.profile_id,
                )

        # Future placeholder: Settings.load and the UI/API keep SIWX inactive for now.
        if settings.wechat_input_mode == "siwx":
            export_directory = Path(settings.siwx_export_dir).expanduser()
            if not export_directory.is_dir():
                raise FileNotFoundError(f"SIWX 导出目录不存在：{export_directory}")
            if settings.siwx_auto_cleanup_enabled:
                self._cleanup_siwx_auto_exports(settings, export_directory)
            if settings.siwx_auto_export_enabled:
                self._run_siwx_auto_export(settings, export_directory)
            self._process_siwx_exports(export_directory)

    def _run_siwx_auto_export(self, settings: object, export_directory: Path) -> None:
        """Periodically request incremental JSON exports from the local SIWX server.

        按账号/会话集合隔离调度游标；成功导入后才更新日期，失败由下一轮重试。
        """
        chats = [line.strip() for line in settings.siwx_chats.splitlines() if line.strip()]
        chat_set = "\n".join(sorted(set(chats)))
        schedule_id = "chatset:" + hashlib.sha256(chat_set.encode("utf-8")).hexdigest()
        source = "siwx_auto_export"
        profile_id = settings.profile_id
        last_export = self.runtime.storage.get_checkpoint(
            source, settings.siwx_account, schedule_id, profile_id=profile_id
        )
        today = date.today()
        start_date: str | None = settings.siwx_auto_export_start_date or None
        if last_export:
            try:
                last_date = date.fromisoformat(last_export)
            except ValueError:
                last_date = today - timedelta(days=settings.siwx_auto_export_interval_days)
            if today < last_date + timedelta(days=settings.siwx_auto_export_interval_days):
                return
            start_date = last_date.isoformat()

        client = SIWXExportClient(
            settings.siwx_api_url,
            export_root=export_directory,
        )
        exported_files = client.export_chat_json(
            settings.siwx_account,
            chats,
            start_date,
            today.isoformat(),
            self._stop,
        )
        if exported_files is None or self._stop.is_set():
            return
        for path in exported_files:
            self.runtime.storage.record_siwx_auto_export(profile_id, path)
        self._process_siwx_files(exported_files, skip_processed=False)
        self.runtime.storage.save_checkpoint(
            source,
            settings.siwx_account,
            schedule_id,
            today.isoformat(),
            profile_id=profile_id,
        )

    def _cleanup_siwx_auto_exports(self, settings: object, export_directory: Path) -> None:
        """Delete only expired JSON files previously returned by SIWX auto-export.

        不按目录或文件名猜测来源；只清理本 profile 登记且仍位于 exports 根内的文件。
        """
        export_root = export_directory.resolve()
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=settings.siwx_auto_cleanup_retention_days
        )
        for tracked_path in self.runtime.storage.list_expired_siwx_auto_exports(
            settings.profile_id, cutoff
        ):
            try:
                path = tracked_path.resolve()
                path.relative_to(export_root)
                if path.is_dir():
                    continue
                path.unlink(missing_ok=True)
                self.runtime.storage.forget_siwx_auto_export(
                    settings.profile_id, tracked_path
                )
                parent = path.parent
                while parent != export_root:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
            except (OSError, ValueError):
                continue

    def _process_siwx_exports(self, export_directory: Path) -> None:
        """Resume each exported conversation from its own durable cursor.

        领取文件后按 profile/会话游标继续处理，并在异常时释放文件以便重试。
        """
        self._process_siwx_files(sorted(export_directory.rglob("*.json")))

    def _process_siwx_files(
        self, export_files: list[Path], skip_processed: bool = True
    ) -> None:
        """Process selected SIWX exports through claims and message deduplication.

        自动导出的重叠日期会重放，由消息去重拦截旧项，避免漏掉延迟到达的旧时间戳消息。
        """
        for path in export_files:
            # The content claim filters renamed copies before parsing their messages.
            # 文件内容指纹会先拦截改名副本，再进入消息解析。
            claim = self.runtime.memory.claim_file(path)
            if claim is None:
                continue
            try:
                for envelope in SIWXExportSource().iter_file(path):
                    # Leave the current and remaining entries for a later run during shutdown.
                    # Agent 正在关闭时保留当前及后续消息，供下次启动继续处理。
                    if self._stop.is_set():
                        self.runtime.memory.release_file(path, claim)
                        return
                    message = envelope.message
                    previous = self.runtime.storage.get_checkpoint(
                        "siwx", message.account_id, message.conversation_id,
                        profile_id=self.runtime.settings.profile_id,
                    )
                    if skip_processed and previous and envelope.cursor <= previous:
                        continue
                    self.runtime.workflow.process(message)
                    if previous is None or envelope.cursor > previous:
                        self.runtime.storage.save_checkpoint(
                            "siwx",
                            message.account_id,
                            message.conversation_id,
                            envelope.cursor,
                            profile_id=self.runtime.settings.profile_id,
                        )
                self.runtime.memory.complete_file(path, claim)
            except Exception:
                # A handled processing error releases this claim immediately for the next poll.
                # 捕获到处理异常时立即释放文件占用，使下一轮轮询可以重试。
                self.runtime.memory.release_file(path, claim)
                raise
