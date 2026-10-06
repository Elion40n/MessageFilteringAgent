"""Profile-scoped feedback memory and imported-file deduplication.

按筛选配置隔离人工反馈记忆与导入文件指纹。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Iterator
from uuid import uuid4


class MemoryStore:
    """Persist editable feedback memories and exact file fingerprints per profile.

    按 profile 隔离可编辑的反馈记忆和导入文件内容指纹。
    """

    def __init__(self, database_path: Path, profile_id: str) -> None:
        """Create the profile-bound tables used by memory and file claims.

        初始化 profile 级记忆与文件领取表。
        """
        if not profile_id.strip():
            raise ValueError("profile_id must not be empty")
        self.database_path = database_path
        self.profile_id = profile_id
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_entries (
                    memory_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    source TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memory_entries_profile
                    ON memory_entries(profile_id, enabled, created_at);
                CREATE TABLE IF NOT EXISTS imported_file_fingerprints (
                    profile_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    claim_token TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(profile_id, fingerprint)
                );
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Commit or roll back one SQLite transaction, then close its handle.

        提交或回滚单个 SQLite 事务，并始终关闭连接句柄。
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
    def _fingerprint(path: Path) -> tuple[str, int]:
        """Hash a file incrementally and return its digest and byte size.

        分块计算文件摘要，返回 SHA-256 指纹和字节数。
        """
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            # Stream large exports in bounded chunks instead of loading them into memory.
            # 分块读取大型导出文件，避免一次性载入内存。
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        return digest.hexdigest(), size

    def list_entries(self) -> list[dict[str, object]]:
        """List editable memories owned by this profile.

        列出当前 profile 拥有的可编辑记忆。
        """
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM memory_entries WHERE profile_id = ?
                   ORDER BY updated_at DESC, memory_id""",
                (self.profile_id,),
            ).fetchall()
        return [self._entry_payload(row) for row in rows]

    def active_context(self) -> list[str]:
        """Return enabled memories as supplemental classifier context.

        将已启用记忆整理为供分类器补充参考的上下文。
        """
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT decision, content FROM memory_entries
                   WHERE profile_id = ? AND enabled = 1
                   ORDER BY created_at""",
                (self.profile_id,),
            ).fetchall()
        return [f"人工判断：{row['decision']}；经验：{row['content']}" for row in rows]

    def add_entry(
        self,
        content: str,
        decision: str = "补充",
        source: str = "manual",
        tags: list[str] | None = None,
        enabled: bool = True,
    ) -> dict[str, object]:
        """Validate and persist one new feedback-memory record.

        校验并保存一条新的反馈记忆。
        """
        content = content.strip()
        if not content:
            raise ValueError("记忆内容不能为空")
        if decision not in {"满足", "不满足", "补充"}:
            raise ValueError("记忆判断必须是满足、不满足或补充")
        now = datetime.now(timezone.utc).timestamp()
        memory_id = uuid4().hex
        with self._lock, self._connection() as connection:
            connection.execute(
                """INSERT INTO memory_entries
                   (memory_id, profile_id, content, decision, source, tags_json,
                    enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    memory_id,
                    self.profile_id,
                    content,
                    decision,
                    source.strip(),
                    json.dumps(tags or [], ensure_ascii=False),
                    int(enabled),
                    now,
                    now,
                ),
            )
        entry = self.get_entry(memory_id)
        assert entry is not None
        return entry

    def get_entry(self, memory_id: str) -> dict[str, object] | None:
        """Fetch one memory only if it belongs to this profile.

        仅在记忆属于当前 profile 时返回该条记录。
        """
        with self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM memory_entries
                   WHERE profile_id = ? AND memory_id = ?""",
                (self.profile_id, memory_id),
            ).fetchone()
        return self._entry_payload(row) if row else None

    def update_entry(self, memory_id: str, changes: dict[str, object]) -> bool:
        """Update allowlisted fields without crossing the profile boundary.

        仅更新允许字段，并确保不会跨 profile 修改记忆。
        """
        allowed = {"content", "decision", "source", "tags", "enabled"}
        unknown = changes.keys() - allowed
        if unknown:
            raise ValueError("包含不支持的记忆字段")
        current = self.get_entry(memory_id)
        if current is None:
            return False
        content = str(changes.get("content", current["content"])).strip()
        decision = str(changes.get("decision", current["decision"]))
        if not content:
            raise ValueError("记忆内容不能为空")
        if decision not in {"满足", "不满足", "补充"}:
            raise ValueError("记忆判断必须是满足、不满足或补充")
        tags = changes.get("tags", current["tags"])
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise ValueError("记忆标签必须是字符串列表")
        enabled = changes.get("enabled", current["enabled"])
        if not isinstance(enabled, bool):
            raise ValueError("记忆启用状态必须是布尔值")
        source = str(changes.get("source", current["source"])).strip()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """UPDATE memory_entries SET content = ?, decision = ?, source = ?,
                   tags_json = ?, enabled = ?, updated_at = ?
                   WHERE profile_id = ? AND memory_id = ?""",
                (
                    content,
                    decision,
                    source,
                    json.dumps(tags, ensure_ascii=False),
                    int(enabled),
                    datetime.now(timezone.utc).timestamp(),
                    self.profile_id,
                    memory_id,
                ),
            )
        return cursor.rowcount == 1

    def delete_entry(self, memory_id: str) -> bool:
        """Delete one memory owned by this profile.

        删除一条属于当前 profile 的记忆。
        """
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM memory_entries WHERE profile_id = ? AND memory_id = ?",
                (self.profile_id, memory_id),
            )
        return cursor.rowcount == 1

    def claim_file(self, path: Path) -> str | None:
        """Claim a file by exact content; return None for complete or fresh claims.

        按文件内容领取待处理文件；已完成或租约尚新的重复文件返回 None。
        """
        fingerprint, size = self._fingerprint(path)
        now = datetime.now(timezone.utc).timestamp()
        token = uuid4().hex
        # A crashed process leaves a claim that becomes retryable after one hour.
        # 进程异常终止后会遗留领取记录；超过一小时才允许重新领取。
        stale_before = (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()
        with self._lock, self._connection() as connection:
            # Serialize the fingerprint check and claim so concurrent pollers cannot both process it.
            # 将指纹检查和领取放在同一写事务中，避免并发轮询重复处理同一文件。
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT status, updated_at FROM imported_file_fingerprints
                   WHERE profile_id = ? AND fingerprint = ?""",
                (self.profile_id, fingerprint),
            ).fetchone()
            if row and (row["status"] == "complete" or row["updated_at"] >= stale_before):
                return None
            connection.execute(
                """INSERT INTO imported_file_fingerprints
                   (profile_id, fingerprint, file_name, file_size, status, claim_token, updated_at)
                   VALUES (?, ?, ?, ?, 'claimed', ?, ?)
                   ON CONFLICT(profile_id, fingerprint) DO UPDATE SET
                     file_name = excluded.file_name, file_size = excluded.file_size,
                     status = 'claimed', claim_token = excluded.claim_token,
                     updated_at = excluded.updated_at""",
                (self.profile_id, fingerprint, path.name, size, token, now),
            )
        return token

    def complete_file(self, path: Path, claim_token: str) -> bool:
        """Mark this exact-content claim complete if the token still matches.

        仅当领取令牌仍匹配时，才将该内容指纹标记为完成。
        """
        fingerprint, _ = self._fingerprint(path)
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """UPDATE imported_file_fingerprints SET status = 'complete', updated_at = ?
                   WHERE profile_id = ? AND fingerprint = ? AND claim_token = ?""",
                (
                    datetime.now(timezone.utc).timestamp(),
                    self.profile_id,
                    fingerprint,
                    claim_token,
                ),
            )
        return cursor.rowcount == 1

    def release_file(self, path: Path, claim_token: str) -> None:
        """Release a failed claim so the next poll can retry the file.

        释放处理失败的文件占用，使后续轮询可以重试。
        """
        fingerprint, _ = self._fingerprint(path)
        with self._lock, self._connection() as connection:
            connection.execute(
                """DELETE FROM imported_file_fingerprints
                   WHERE profile_id = ? AND fingerprint = ? AND claim_token = ?""",
                (self.profile_id, fingerprint, claim_token),
            )

    @staticmethod
    def _entry_payload(row: sqlite3.Row) -> dict[str, object]:
        """Convert a SQLite row into the local UI's JSON-safe shape.

        将 SQLite 行转换为可供本机界面使用的 JSON 数据。
        """
        return {
            "memory_id": row["memory_id"],
            "content": row["content"],
            "decision": row["decision"],
            "source": row["source"],
            "tags": json.loads(row["tags_json"]),
            "enabled": bool(row["enabled"]),
            "created_at": datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat(),
            "updated_at": datetime.fromtimestamp(row["updated_at"], timezone.utc).isoformat(),
        }