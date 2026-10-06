"""Reserved local HTTP client for future SIWX chat export support.

未来接入时通过 SIWX loopback Web API 请求导出；当前版本不会启用此客户端。
本模块不负责微信解密或账号登录。
"""

from __future__ import annotations

from pathlib import Path
import json
import time
from threading import Event
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class SIWXExportError(RuntimeError):
    """Report an unavailable SIWX service or failed export job."""


class SIWXExportClient:
    """Start and await one SIWX JSON chat export through its local API."""

    def __init__(
        self,
        base_url: str,
        export_root: Path,
        request_timeout: float = 10,
        poll_interval: float = 1,
        task_timeout: float = 6 * 60 * 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.export_root = export_root.expanduser().resolve()
        self.request_timeout = request_timeout
        self.poll_interval = poll_interval
        self.task_timeout = task_timeout

    def _request(self, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="GET" if payload is None else "POST",
        )
        try:
            with urlopen(request, timeout=self.request_timeout) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise SIWXExportError(
                f"SIWX API 返回 HTTP {error.code}: {detail or error.reason}"
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise SIWXExportError(f"无法连接 SIWX 服务 {self.base_url}: {error}") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SIWXExportError(f"SIWX API 返回了无效 JSON: {error}") from error
        if not isinstance(decoded, dict):
            raise SIWXExportError("SIWX API 响应格式无效")
        return decoded

    def _run_job(
        self,
        mode: str,
        stop_event: Event,
        export_options: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        payload: dict[str, object] = {"mode": mode}
        if export_options is not None:
            payload["export_opts"] = export_options
        self._request("/api/run", payload)
        deadline = time.monotonic() + self.task_timeout
        while not stop_event.is_set():
            if time.monotonic() >= deadline:
                raise SIWXExportError(f"SIWX {mode} 任务等待超时")
            job = self._request("/api/job")
            if not job.get("running"):
                if job.get("mode") != mode:
                    raise SIWXExportError(f"SIWX 返回的任务不是 {mode} 任务")
                if not job.get("ok"):
                    logs = job.get("logs") or []
                    last_log = logs[-1] if logs else "无任务日志"
                    raise SIWXExportError(f"SIWX {mode} 任务失败：{last_log}")
                return job
            stop_event.wait(self.poll_interval)
        return None

    def export_chat_json(
        self,
        account: str,
        chats: list[str],
        start_date: str | None,
        end_date: str,
        stop_event: Event,
    ) -> list[Path] | None:
        """Sync SIWX, export selected chats, and return their JSON files.

        先增量解密，再导出指定会话；日期范围使用 SIWX 本机日期且结束日包含全天。
        """
        selected_chats = list(dict.fromkeys(chat.strip() for chat in chats if chat.strip()))
        if not account.strip() or not selected_chats:
            raise SIWXExportError("自动导出需要 SIWX 账号和至少一个会话")
        sync_job = self._run_job("sync", stop_event)
        if sync_job is None or stop_event.is_set():
            return None
        export_options: dict[str, object] = {
            "account": account.strip(),
            "chats": [{"chat": chat, "display": chat} for chat in selected_chats],
            "format": "json",
            "pack": "folder",
            "start": start_date,
            "end": end_date,
            "messages": True,
            "media": False,
            "voice": False,
            "avatars": False,
        }
        job = self._run_job("export", stop_event, export_options)
        if job is None or stop_event.is_set():
            return None
        report = job.get("report")
        sessions = report.get("sessions") if isinstance(report, dict) else None
        if not isinstance(sessions, list) or not sessions:
            raise SIWXExportError("SIWX 导出完成，但没有返回会话文件列表")
        paths: list[Path] = []
        returned_chats: set[str] = set()
        for session in sessions:
            if not isinstance(session, dict) or session.get("error"):
                raise SIWXExportError(
                    f"SIWX 会话导出失败：{session.get('error', '无效结果')}"
                    if isinstance(session, dict)
                    else "SIWX 返回了无效的会话导出结果"
                )
            raw_path = session.get("file")
            if not raw_path:
                raise SIWXExportError("SIWX 没有返回会话 JSON 文件路径")
            path = Path(str(raw_path)).expanduser()
            try:
                resolved_path = path.resolve(strict=True)
                resolved_path.relative_to(self.export_root)
            except (OSError, ValueError) as error:
                raise SIWXExportError(
                    f"SIWX 导出文件不在配置目录内或不存在：{path}"
                ) from error
            if resolved_path.suffix.lower() != ".json" or not resolved_path.is_file():
                raise SIWXExportError(f"SIWX 导出 JSON 不存在：{path}")
            paths.append(resolved_path)
            if session.get("chat"):
                returned_chats.add(str(session["chat"]))
        missing_chats = set(selected_chats) - returned_chats
        if missing_chats:
            raise SIWXExportError(
                "SIWX 未返回这些会话的导出结果：" + ", ".join(sorted(missing_chats))
            )
        return paths