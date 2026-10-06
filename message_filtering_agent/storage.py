"""SQLite-backed source checkpoints and time-limited duplicate detection.

基于 SQLite 的来源游标、限期去重和待确认状态存储。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
from typing import Iterator
from uuid import uuid4

from .models import AgentDecision, DecisionLabel, Message, MessageSource


@dataclass(frozen=True, slots=True)
class Registration:
    """Result of claiming a message identity for workflow processing.

    返回是否首次出现及命中哪种去重条件，便于调用方记录原因。
    """

    is_new: bool
    reason: str | None = None


class DedupStore:
    """Persist message identities and canonical content fingerprints.

    通过短事务保存来源检查点、内容指纹及人工确认状态，支持进程重启后续接。
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS content_fingerprints (
                    profile_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    PRIMARY KEY (profile_id, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS transport_ids (
                    profile_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (
                        profile_id, source, account_id, conversation_id, external_id
                    )
                );
                CREATE TABLE IF NOT EXISTS source_checkpoints (
                    source TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (source, account_id, conversation_id)
                );
                CREATE TABLE IF NOT EXISTS pending_questions (
                    question_id TEXT PRIMARY KEY,
                    message_json TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    answers_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_checkpoints_by_profile (
                    profile_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (profile_id, source, account_id, conversation_id)
                );
                CREATE TABLE IF NOT EXISTS pending_questions_by_profile (
                    question_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    source_archive_id TEXT,
                    message_json TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    answers_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS filtered_messages (
                    archive_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    message_json TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS siwx_auto_export_files (
                    profile_id TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (profile_id, file_path)
                );
                CREATE INDEX IF NOT EXISTS filtered_messages_profile_expiry
                    ON filtered_messages(profile_id, expires_at, created_at);
                """
            )
            pending_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(pending_questions_by_profile)"
                ).fetchall()
            }
            if "source_archive_id" not in pending_columns:
                connection.execute(
                    "ALTER TABLE pending_questions_by_profile ADD COLUMN source_archive_id TEXT"
                )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS pending_questions_archive_origin
                   ON pending_questions_by_profile(profile_id, source_archive_id)
                   WHERE source_archive_id IS NOT NULL"""
            )
            # Preserve pre-profile checkpoints/questions as records in the default profile.
            # 将升级前的来源游标和待确认事项迁移到 default profile，保留旧数据可见性。
            connection.execute(
                """INSERT OR IGNORE INTO source_checkpoints_by_profile
                   (profile_id, source, account_id, conversation_id, cursor, updated_at)
                   SELECT 'default', source, account_id, conversation_id, cursor, updated_at
                   FROM source_checkpoints"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO pending_questions_by_profile
                   (question_id, profile_id, message_json, decision_json, answers_json,
                    status, created_at, updated_at)
                   SELECT question_id, 'default', message_json, decision_json, answers_json,
                          status, created_at, updated_at
                   FROM pending_questions"""
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Open one connection and always commit/rollback then close it.

        SQLite 连接的上下文管理器本身不会关闭句柄；这里显式关闭以免 Windows 锁住数据库。
        """
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _timestamp(value: datetime | None) -> datetime:
        """Normalize supplied or current time to timezone-aware UTC."""
        current = value or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc)

    def register_message(
        self,
        message: Message,
        profile_id: str,
        retention_days: int,
        now: datetime | None = None,
    ) -> Registration:
        """Atomically deduplicate by transport ID, then canonical content.

        窗口按最近一次看见重复消息滚动；先删过期项，再在同一写事务中检查并登记，
        避免两个监听线程同时把同一条消息当成新消息。
        """
        if not profile_id.strip():
            raise ValueError("profile_id must not be empty")
        if not 1 <= retention_days <= 3650:
            raise ValueError("retention_days must be between 1 and 3650")

        observed_at = self._timestamp(now)
        observed_ts = observed_at.timestamp()
        expires_ts = (observed_at + timedelta(days=retention_days)).timestamp()
        cutoff_ts = (observed_at - timedelta(days=retention_days)).timestamp()
        fingerprint = message.content_fingerprint
        source = message.source.value

        with self._lock, self._connection() as connection:
            # Reserve the write transaction before checking, so concurrent listeners cannot race.
            # 先取得写事务，再进行查询，避免并发监听器同时通过“尚不存在”的检查。
            connection.execute("BEGIN IMMEDIATE")
            # Expiration is based on last_seen: recurring copies remain suppressed while they recur.
            # 保留期从最近一次出现开始计算；持续转发的重复内容会继续被抑制。
            connection.execute(
                "DELETE FROM content_fingerprints WHERE last_seen < ?", (cutoff_ts,)
            )
            connection.execute(
                "DELETE FROM transport_ids WHERE expires_at <= ?", (observed_ts,)
            )

            if message.external_id:
                identity = connection.execute(
                    """SELECT 1 FROM transport_ids
                       WHERE profile_id = ? AND source = ? AND account_id = ?
                         AND conversation_id = ? AND external_id = ?
                         AND expires_at > ?""",
                    (
                        profile_id,
                        source,
                        message.account_id,
                        message.conversation_id,
                        message.external_id,
                        observed_ts,
                    ),
                ).fetchone()
                if identity:
                    connection.commit()
                    return Registration(False, "transport_id")

            if fingerprint:
                existing = connection.execute(
                    """SELECT first_seen FROM content_fingerprints
                       WHERE profile_id = ? AND fingerprint = ? AND last_seen >= ?""",
                    (profile_id, fingerprint, cutoff_ts),
                ).fetchone()
                if existing:
                    # Refresh the rolling window and remember this transport ID for later replays.
                    # 延长内容指纹窗口，并记录本来源 ID，拦截后续同一消息的再次投递。
                    connection.execute(
                        """UPDATE content_fingerprints SET last_seen = ?
                           WHERE profile_id = ? AND fingerprint = ?""",
                        (observed_ts, profile_id, fingerprint),
                    )
                    self._insert_transport_id(
                        connection, message, profile_id, source, expires_ts
                    )
                    connection.commit()
                    return Registration(False, "content_fingerprint")

                connection.execute(
                    """INSERT INTO content_fingerprints
                       (profile_id, fingerprint, first_seen, last_seen)
                       VALUES (?, ?, ?, ?)""",
                    (profile_id, fingerprint, observed_ts, observed_ts),
                )

            self._insert_transport_id(connection, message, profile_id, source, expires_ts)
            connection.commit()
            return Registration(True)

    @staticmethod
    def _insert_transport_id(
        connection: sqlite3.Connection,
        message: Message,
        profile_id: str,
        source: str,
        expires_at: float,
    ) -> None:
        """Remember one source-specific identifier until its retention deadline."""
        if not message.external_id:
            return
        connection.execute(
            """INSERT OR REPLACE INTO transport_ids
               (profile_id, source, account_id, conversation_id, external_id, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                profile_id,
                source,
                message.account_id,
                message.conversation_id,
                message.external_id,
                expires_at,
            ),
        )

    def save_checkpoint(
        self,
        source: str,
        account_id: str,
        conversation_id: str,
        cursor: str,
        updated_at: datetime | None = None,
        profile_id: str = "default",
    ) -> None:
        """Atomically store the latest successfully handled source cursor.

        调用方应在消息完成分类/过滤或待确认状态落库后再推进游标。
        游标按 profile、来源、账号和会话分别保存。
        """
        updated_ts = self._timestamp(updated_at).timestamp()
        with self._lock, self._connection() as connection:
            connection.execute(
                     """INSERT INTO source_checkpoints_by_profile
                         (profile_id, source, account_id, conversation_id, cursor, updated_at)
                         VALUES (?, ?, ?, ?, ?, ?)
                         ON CONFLICT(profile_id, source, account_id, conversation_id) DO UPDATE SET
                     cursor = excluded.cursor, updated_at = excluded.updated_at""",
                     (profile_id, source, account_id, conversation_id, cursor, updated_ts),
            )

    def release_for_retry(self, message: Message, profile_id: str) -> None:
        """Remove a newly claimed message identity after an operational failure.

        发生模型、投递或待确认落库故障时释放占位，允许来源重试。
        This does not guarantee exactly-once external SMTP delivery; an ambiguous network result may still need review.
        此方法不保证 SMTP 恰好投递一次；网络确认不明时仍可能需要人工核对。
        """
        fingerprint = message.content_fingerprint
        with self._lock, self._connection() as connection:
            if fingerprint:
                connection.execute(
                    """DELETE FROM content_fingerprints
                       WHERE profile_id = ? AND fingerprint = ?""",
                    (profile_id, fingerprint),
                )
            if message.external_id:
                connection.execute(
                    """DELETE FROM transport_ids
                       WHERE profile_id = ? AND source = ? AND account_id = ?
                         AND conversation_id = ? AND external_id = ?""",
                    (
                        profile_id,
                        message.source.value,
                        message.account_id,
                        message.conversation_id,
                        message.external_id,
                    ),
                )

    def get_checkpoint(
        self, source: str, account_id: str, conversation_id: str,
        profile_id: str = "default",
    ) -> str | None:
        """Return the last durable cursor for one source/account/conversation.

        按 profile 读取指定来源、账号和会话的最近持久游标。
        """
        with self._connection() as connection:
            row = connection.execute(
                     """SELECT cursor FROM source_checkpoints_by_profile
                         WHERE profile_id = ? AND source = ? AND account_id = ?
                            AND conversation_id = ?""",
                     (profile_id, source, account_id, conversation_id),
            ).fetchone()
        return str(row["cursor"]) if row else None

    def record_siwx_auto_export(self, profile_id: str, file_path: Path) -> None:
        """Register one file created by the Agent's SIWX auto-export job.

        只登记自动导出返回的具体文件，供安全清理；手动导出不会登记。
        """
        with self._lock, self._connection() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO siwx_auto_export_files
                   (profile_id, file_path, created_at) VALUES (?, ?, ?)""",
                (profile_id, str(file_path.resolve()), datetime.now(timezone.utc).timestamp()),
            )

    def list_expired_siwx_auto_exports(
        self, profile_id: str, older_than: datetime
    ) -> list[Path]:
        """List this profile's auto-export files older than the cleanup cutoff."""
        cutoff = self._timestamp(older_than).timestamp()
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT file_path FROM siwx_auto_export_files
                   WHERE profile_id = ? AND created_at <= ? ORDER BY created_at""",
                (profile_id, cutoff),
            ).fetchall()
        return [Path(row["file_path"]) for row in rows]

    def forget_siwx_auto_export(self, profile_id: str, file_path: Path) -> None:
        """Remove one auto-export tracking record after deletion or prior removal."""
        with self._lock, self._connection() as connection:
            connection.execute(
                "DELETE FROM siwx_auto_export_files WHERE profile_id = ? AND file_path = ?",
                (profile_id, str(file_path.resolve())),
            )

    def rename_profile(self, previous_profile_id: str, new_profile_id: str) -> None:
        """Move all durable records to a new profile ID without merging profiles.

        在单个 SQLite 事务中迁移 profile 状态；目标已有记录时拒绝合并。
        """
        if previous_profile_id == new_profile_id:
            return
        tables = (
            "content_fingerprints",
            "transport_ids",
            "source_checkpoints_by_profile",
            "pending_questions_by_profile",
            "filtered_messages",
            "memory_entries",
            "imported_file_fingerprints",
        )
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for table in tables:
                existing = connection.execute(
                    f"SELECT 1 FROM {table} WHERE profile_id = ? LIMIT 1",
                    (new_profile_id,),
                ).fetchone()
                if existing:
                    raise ValueError("目标 profile 已有持久化数据，拒绝合并")
            for table in tables:
                connection.execute(
                    f"UPDATE {table} SET profile_id = ? WHERE profile_id = ?",
                    (new_profile_id, previous_profile_id),
                )
            if previous_profile_id == "default":
                # Old unscoped records were copied into default during store initialization.
                # 旧版无 profile 记录已复制到 default；绑定后删除源行，避免下次启动重建旧副本。
                connection.execute("DELETE FROM source_checkpoints")
                connection.execute("DELETE FROM pending_questions")

    def delete_profile(self, profile_id: str) -> None:
        """Delete all durable rows owned by one profile in a single transaction.

        删除指定 profile 的记忆、待确认、游标、归档、去重和文件指纹状态。
        """
        if not profile_id.strip():
            raise ValueError("profile_id must not be empty")
        tables = (
            "content_fingerprints",
            "transport_ids",
            "source_checkpoints_by_profile",
            "pending_questions_by_profile",
            "filtered_messages",
            "siwx_auto_export_files",
            "memory_entries",
            "imported_file_fingerprints",
        )
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            for table in tables:
                if table in existing_tables:
                    connection.execute(
                        f"DELETE FROM {table} WHERE profile_id = ?",
                        (profile_id,),
                    )
            if profile_id == "default":
                # Legacy rows without profile columns belong to the default profile.
                # 旧版无 profile 字段的数据归属于 default。
                connection.execute("DELETE FROM source_checkpoints")
                connection.execute("DELETE FROM pending_questions")

    def create_pending_question(
        self, message: Message, decision: AgentDecision, profile_id: str = "default"
    ) -> str:
        """Persist the original input and decision before exposing a question ID.

        在返回问题 ID 前，先按 profile 保存原消息和当前判断。
        """
        question_id = uuid4().hex
        now = datetime.now(timezone.utc).timestamp()
        with self._lock, self._connection() as connection:
            connection.execute(
                     """INSERT INTO pending_questions_by_profile
                         (question_id, profile_id, message_json, decision_json, answers_json,
                    status, created_at, updated_at)
                         VALUES (?, ?, ?, ?, '[]', 'pending', ?, ?)""",
                (
                    question_id,
                          profile_id,
                    json.dumps(self._message_to_dict(message), ensure_ascii=False),
                    json.dumps(self._decision_to_dict(decision), ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return question_id

    def list_pending_questions(self, profile_id: str = "default") -> list[dict[str, object]]:
        """Load unanswered questions in creation order for the local review UI.

        仅列出指定 profile 的未回答问题，并按创建时间排序。
        """
        with self._connection() as connection:
            rows = connection.execute(
                     """SELECT * FROM pending_questions_by_profile
                         WHERE profile_id = ? AND status = 'pending' ORDER BY created_at""",
                     (profile_id,),
            ).fetchall()
        return [self._question_from_row(row) for row in rows]

    def get_activity_snapshot(self, profile_id: str = "default") -> dict[str, int | float]:
        """Return lightweight profile-scoped counters for live UI refresh.

        返回 profile 级轻量活动计数；不读取消息正文，供网页自动刷新使用。
        """
        with self._connection() as connection:
            pending = connection.execute(
                """SELECT COUNT(*) AS item_count, COALESCE(MAX(updated_at), 0) AS updated_at
                   FROM pending_questions_by_profile
                   WHERE profile_id = ? AND status = 'pending'""",
                (profile_id,),
            ).fetchone()
            filtered = connection.execute(
                                """SELECT
                                         COUNT(*) AS filtered_count,
                     COALESCE(MAX(created_at), 0) AS updated_at
                   FROM filtered_messages
                                     WHERE profile_id = ? AND status = 'filtered' AND expires_at > ?""",
                (profile_id, datetime.now(timezone.utc).timestamp()),
            ).fetchone()
        return {
            "pending_count": int(pending["item_count"]),
            "pending_updated_at": float(pending["updated_at"]),
            "filtered_count": int(filtered["filtered_count"] or 0),
            "filtered_updated_at": float(filtered["updated_at"]),
        }

    def get_pending_question(
        self, question_id: str, profile_id: str = "default"
    ) -> dict[str, object] | None:
        """Reload one question and reconstruct typed message/decision values.

        只在问题属于当前 profile 时重建消息和判断对象。
        """
        with self._connection() as connection:
            row = connection.execute(
                     """SELECT * FROM pending_questions_by_profile
                         WHERE question_id = ? AND profile_id = ?""",
                     (question_id, profile_id),
            ).fetchone()
        return self._question_from_row(row) if row else None

    def append_question_answer(
        self, question_id: str, answer: str, profile_id: str = "default"
    ) -> bool:
        """Persist one non-empty user answer only while the question is pending.

        仅为指定 profile 的待确认问题追加非空回答。
        """
        if not answer.strip():
            raise ValueError("answer must not be empty")
        with self._lock, self._connection() as connection:
            row = connection.execute(
                     """SELECT answers_json FROM pending_questions_by_profile
                         WHERE question_id = ? AND profile_id = ? AND status = 'pending'""",
                     (question_id, profile_id),
            ).fetchone()
            if not row:
                return False
            answers = json.loads(row["answers_json"])
            answers.append(answer.strip())
            connection.execute(
                     """UPDATE pending_questions_by_profile SET answers_json = ?, updated_at = ?
                         WHERE question_id = ? AND profile_id = ? AND status = 'pending'""",
                (
                    json.dumps(answers, ensure_ascii=False),
                    datetime.now(timezone.utc).timestamp(),
                    question_id,
                    profile_id,
                ),
            )
        return True

    def resolve_pending_question(
        self, question_id: str, status: str, profile_id: str = "default"
    ) -> None:
        """Mark a pending question terminal after delivery or filtering.

        投递或过滤后，将指定 profile 的问题标记为终态。
        """
        if status not in {"delivered", "filtered"}:
            raise ValueError("status must be delivered or filtered")
        with self._lock, self._connection() as connection:
            connection.execute(
                     """UPDATE pending_questions_by_profile SET status = ?, updated_at = ?
                         WHERE question_id = ? AND profile_id = ? AND status = 'pending'""",
                     (status, datetime.now(timezone.utc).timestamp(), question_id, profile_id),
            )

    def archive_filtered_message(
        self,
        message: Message,
        decision: AgentDecision,
        profile_id: str,
        retention_days: int = 30,
        now: datetime | None = None,
    ) -> str:
        """Store a rejected formal message for profile-scoped self-review.

        按 profile 归档正式流程中判为不满足的消息，并设置自动过期时间。
        """
        if not profile_id.strip():
            raise ValueError("profile_id must not be empty")
        if not 1 <= retention_days <= 3650:
            raise ValueError("retention_days must be between 1 and 3650")
        created_at = self._timestamp(now)
        created_ts = created_at.timestamp()
        expires_ts = (created_at + timedelta(days=retention_days)).timestamp()
        archive_id = uuid4().hex
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM filtered_messages WHERE profile_id = ? AND expires_at <= ?",
                (profile_id, created_ts),
            )
            if message.external_id:
                existing = connection.execute(
                    """SELECT archive_id FROM filtered_messages
                       WHERE profile_id = ? AND source = ? AND account_id = ?
                         AND conversation_id = ? AND external_id = ?
                         AND status = 'filtered' AND expires_at > ?""",
                    (
                        profile_id,
                        message.source.value,
                        message.account_id,
                        message.conversation_id,
                        message.external_id,
                        created_ts,
                    ),
                ).fetchone()
                if existing:
                    return str(existing["archive_id"])
            connection.execute(
                """INSERT INTO filtered_messages
                   (archive_id, profile_id, source, account_id, conversation_id,
                    external_id, message_json, decision_json, status, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'filtered', ?, ?)""",
                (
                    archive_id,
                    profile_id,
                    message.source.value,
                    message.account_id,
                    message.conversation_id,
                    message.external_id or "",
                    json.dumps(self._message_to_dict(message), ensure_ascii=False),
                    json.dumps(self._decision_to_dict(decision), ensure_ascii=False),
                    created_ts,
                    expires_ts,
                ),
            )
        return archive_id

    def list_filtered_messages(
        self,
        profile_id: str = "default",
        now: datetime | None = None,
    ) -> list[dict[str, object]]:
        """List unexpired filtered records for one profile, newest first.

        仅列出当前 profile 未过期的过滤信息，供用户自查并选择转待处理。
        """
        now_ts = self._timestamp(now).timestamp()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM filtered_messages WHERE profile_id = ? AND expires_at <= ?",
                (profile_id, now_ts),
            )
            rows = connection.execute(
                     """SELECT * FROM filtered_messages
                         WHERE profile_id = ? AND status = 'filtered'
                   ORDER BY created_at DESC, archive_id""",
                (profile_id,),
            ).fetchall()
        return [self._filtered_message_from_row(row) for row in rows]

    def promote_filtered_message(
        self,
        archive_id: str,
        profile_id: str,
        now: datetime | None = None,
    ) -> str | None:
        """Atomically turn one archived rejection into a pending review item.

        原子地把一条归档消息转入当前 profile 的待处理队列；重复点击返回同一 ID。
        """
        now_ts = self._timestamp(now).timestamp()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            promoted = connection.execute(
                """SELECT question_id FROM pending_questions_by_profile
                   WHERE profile_id = ? AND source_archive_id = ?""",
                (profile_id, archive_id),
            ).fetchone()
            if promoted:
                connection.execute(
                    "DELETE FROM filtered_messages WHERE archive_id = ? AND profile_id = ?",
                    (archive_id, profile_id),
                )
                return str(promoted["question_id"])
            row = connection.execute(
                     """SELECT * FROM filtered_messages
                         WHERE archive_id = ? AND profile_id = ? AND status = 'filtered'""",
                (archive_id, profile_id),
            ).fetchone()
            if not row or row["expires_at"] <= now_ts:
                if row:
                    connection.execute(
                        "DELETE FROM filtered_messages WHERE archive_id = ? AND profile_id = ?",
                        (archive_id, profile_id),
                    )
                return None
            question_id = uuid4().hex
            question_decision = AgentDecision(
                label=DecisionLabel.UNCERTAIN,
                explanation="用户将此前过滤的信息转为待处理事项，请按当前筛选条件重新核验。",
            )
            connection.execute(
                """INSERT INTO pending_questions_by_profile
                   (question_id, profile_id, source_archive_id, message_json,
                    decision_json, answers_json, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, '[]', 'pending', ?, ?)""",
                (
                    question_id,
                    profile_id,
                    archive_id,
                    row["message_json"],
                    json.dumps(self._decision_to_dict(question_decision), ensure_ascii=False),
                    now_ts,
                    now_ts,
                ),
            )
            connection.execute(
                "DELETE FROM filtered_messages WHERE archive_id = ? AND profile_id = ?",
                (archive_id, profile_id),
            )
        return question_id

    @classmethod
    def _filtered_message_from_row(cls, row: sqlite3.Row) -> dict[str, object]:
        """Rebuild the message and decision shown in the filtered archive UI."""
        message_data = json.loads(row["message_json"])
        message = Message(
            source=MessageSource(message_data["source"]),
            account_id=message_data["account_id"],
            conversation_id=message_data["conversation_id"],
            timestamp=datetime.fromisoformat(message_data["timestamp"]),
            sender=message_data["sender"],
            content=message_data["content"],
            external_id=message_data["external_id"],
            source_ref=message_data["source_ref"],
        )
        decision_data = json.loads(row["decision_json"])
        decision = AgentDecision(
            label=DecisionLabel(decision_data["label"]),
            evidence=tuple(decision_data["evidence"]),
            extracted_fields=decision_data["extracted_fields"],
            missing_fields=tuple(decision_data["missing_fields"]),
            explanation=decision_data["explanation"],
        )
        return {
            "archive_id": row["archive_id"],
            "profile_id": row["profile_id"],
            "message": message,
            "decision": decision,
            "status": row["status"],
            "created_at": datetime.fromtimestamp(row["created_at"], timezone.utc),
            "expires_at": datetime.fromtimestamp(row["expires_at"], timezone.utc),
        }

    @staticmethod
    def _message_to_dict(message: Message) -> dict[str, object]:
        """Serialize only the normalized message contract for durable review."""
        return {
            "source": message.source.value,
            "account_id": message.account_id,
            "conversation_id": message.conversation_id,
            "timestamp": message.timestamp.isoformat(),
            "sender": message.sender,
            "content": message.content,
            "external_id": message.external_id,
            "source_ref": message.source_ref,
        }

    @staticmethod
    def _decision_to_dict(decision: AgentDecision) -> dict[str, object]:
        """Serialize decision evidence and unresolved fields as JSON-compatible data."""
        return {
            "label": decision.label.value,
            "evidence": list(decision.evidence),
            "extracted_fields": decision.extracted_fields,
            "missing_fields": list(decision.missing_fields),
            "explanation": decision.explanation,
        }

    @classmethod
    def _question_from_row(cls, row: sqlite3.Row) -> dict[str, object]:
        """Rebuild typed objects from a SQLite row after restart."""
        message_data = json.loads(row["message_json"])
        message = Message(
            source=MessageSource(message_data["source"]),
            account_id=message_data["account_id"],
            conversation_id=message_data["conversation_id"],
            timestamp=datetime.fromisoformat(message_data["timestamp"]),
            sender=message_data["sender"],
            content=message_data["content"],
            external_id=message_data["external_id"],
            source_ref=message_data["source_ref"],
        )
        decision_data = json.loads(row["decision_json"])
        decision = AgentDecision(
            label=DecisionLabel(decision_data["label"]),
            evidence=tuple(decision_data["evidence"]),
            extracted_fields=decision_data["extracted_fields"],
            missing_fields=tuple(decision_data["missing_fields"]),
            explanation=decision_data["explanation"],
        )
        return {
            "question_id": row["question_id"],
            "profile_id": row["profile_id"],
            "source_archive_id": row["source_archive_id"],
            "message": message,
            "decision": decision,
            "answers": json.loads(row["answers_json"]),
            "status": row["status"],
            "created_at": datetime.fromtimestamp(row["created_at"], timezone.utc),
        }