"""Local profile launcher and pre-start setup UI.

在 Agent 启动前管理 profile；只绑定 loopback，用户数据始终保存在应用数据目录。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from .config import Settings, default_data_dir, profile_config_path
from .credentials import (
    CredentialStore,
    model_api_key_account,
    qq_mail_password_account,
)
from .memory import MemoryStore
from .storage import DedupStore


STATIC_DIR = Path(__file__).resolve().parent / "static"
LAUNCHER_IDLE_TIMEOUT_SECONDS = 30 * 60


def _available_port(start: int) -> int:
    """Return a currently free loopback port at or above the requested port."""
    for port in range(start, min(start + 100, 65536)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise OSError("附近没有可用的本地端口，请稍后重试")


class LauncherService:
    """Manage profile files, scoped credentials, and child Agent processes."""

    def __init__(
        self,
        data_dir: Path | None = None,
        credentials: CredentialStore | None = None,
        process_factory=None,
        health_probe: Callable[[str, int], bool] | None = None,
        startup_timeout: float = 20,
    ) -> None:
        self.data_dir = (data_dir or default_data_dir()).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._credentials = credentials
        self._process_factory = process_factory or subprocess.Popen
        self._health_probe = health_probe or self._probe_agent
        self._startup_timeout = startup_timeout
        self._lock = threading.RLock()
        self.last_activity = time.monotonic()
        self._state_path = self.data_dir / ".launcher_processes.state"
        self._children: dict[str, tuple[subprocess.Popen, int]] = {}
        self._process_state = self._load_process_state()

    def record_activity(self) -> None:
        with self._lock:
            self.last_activity = time.monotonic()

    def idle_seconds(self) -> float:
        with self._lock:
            return time.monotonic() - self.last_activity

    def _credential_store(self) -> CredentialStore:
        if self._credentials is None:
            self._credentials = CredentialStore()
        return self._credentials

    def _load_process_state(self) -> dict[str, dict[str, int]]:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(name): value
            for name, value in raw.items()
            if isinstance(value, dict)
            and isinstance(value.get("port"), int)
            and isinstance(value.get("pid"), int)
        }

    def _save_process_state(self) -> None:
        fd, temporary_name = tempfile.mkstemp(
            prefix=".launcher-processes.", suffix=".tmp", dir=self.data_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(self._process_state, stream, indent=2)
                stream.write("\n")
            os.replace(temporary_name, self._state_path)
        except Exception:
            Path(temporary_name).unlink(missing_ok=True)
            raise

    def _config_paths(self) -> list[Path]:
        root = self.data_dir.resolve()
        profile_directory = self.data_dir / "profiles"
        candidates = list(self.data_dir.glob("*.json"))
        if profile_directory.is_dir() and not profile_directory.is_symlink():
            candidates.extend(profile_directory.glob("*.json"))
        return sorted(
            path for path in candidates
            if not path.name.startswith(".")
            and path.is_file()
            and not path.is_symlink()
            and path.resolve().parent in {root, root / "profiles"}
        )

    def _resolve_config(self, config_name: str) -> Path:
        name = str(config_name or "")
        relative = Path(name)
        if (
            not name
            or relative.is_absolute()
            or any(part in {".", ".."} or part.startswith(".") for part in relative.parts)
            or len(relative.parts) not in {1, 2}
            or (len(relative.parts) == 2 and relative.parts[0] != "profiles")
            or relative.suffix.casefold() != ".json"
        ):
            raise ValueError("无效的配置文件名称")
        path = self.data_dir / relative
        if path.is_symlink() or not path.is_file() or path.resolve().parent not in {
            self.data_dir.resolve(),
            (self.data_dir / "profiles").resolve(),
        }:
            raise FileNotFoundError("配置文件不存在")
        return path

    @staticmethod
    def _probe_agent(config_name: str, port: int) -> bool:
        request = Request(f"http://127.0.0.1:{port}/api/settings")
        try:
            with urlopen(request, timeout=0.35) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return payload.get("config_name") == Path(config_name).name
        except (OSError, ValueError, HTTPError, URLError):
            return False

    def _running_port(self, config_name: str, settings: Settings) -> int | None:
        child = self._children.get(config_name)
        if child and child[0].poll() is None:
            return child[1]
        state = self._process_state.get(config_name)
        if state and self._health_probe(config_name, state["port"]):
            return state["port"]
        if self._health_probe(config_name, settings.port):
            return settings.port
        self._process_state.pop(config_name, None)
        return None

    def list_profiles(self) -> list[dict[str, object]]:
        """List direct-child profile configs without revealing full paths or secrets."""
        profiles = []
        for path in self._config_paths():
            try:
                settings = Settings.load(path)
                running_port = self._running_port(path.name, settings)
                profiles.append({
                    "config_name": path.relative_to(self.data_dir).as_posix(),
                    "profile_id": settings.profile_id,
                    "message_type": settings.message_type,
                    "llm_model": settings.llm_model,
                    "mail_configured": bool(
                        settings.mail_username and settings.notification_recipient
                    ),
                    "valid": True,
                    "running": running_port is not None,
                    "url": f"http://127.0.0.1:{running_port}" if running_port else None,
                })
            except Exception as error:
                profiles.append({
                    "config_name": path.relative_to(self.data_dir).as_posix(),
                    "profile_id": path.stem,
                    "valid": False,
                    "running": False,
                    "error": str(error),
                })
        try:
            self._save_process_state()
        except OSError:
            pass
        return profiles

    def _agent_command(self, config_path: Path, port: int) -> list[str]:
        if getattr(sys, "frozen", False):
            return [
                sys.executable,
                "--run-agent",
                "--config",
                str(config_path),
                "--port",
                str(port),
            ]
        return [
            sys.executable,
            "-m",
            "message_filtering_agent.launcher",
            "--run-agent",
            "--config",
            str(config_path),
            "--port",
            str(port),
        ]

    def _launch_config(self, config_path: Path, settings: Settings) -> dict[str, object]:
        existing_port = self._running_port(config_path.name, settings)
        if existing_port is not None:
            return {
                "started": False,
                "running": True,
                "url": f"http://127.0.0.1:{existing_port}",
                "port": existing_port,
            }
        port = _available_port(settings.port)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = self._process_factory(
            self._agent_command(config_path, port),
            cwd=str(self.data_dir if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
        deadline = time.monotonic() + self._startup_timeout
        while time.monotonic() < deadline:
            if self._health_probe(config_path.name, port):
                break
            exit_code = process.poll()
            if exit_code is not None:
                raise RuntimeError(f"Agent 启动失败，进程退出码：{exit_code}")
            threading.Event().wait(0.15)
        else:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                pass
            raise RuntimeError("Agent 未能在限定时间内启动；配置创建将回滚")
        self._children[config_path.name] = (process, port)
        self._process_state[config_path.name] = {"pid": int(process.pid), "port": port}
        try:
            self._save_process_state()
        except OSError:
            # The Agent still starts; profile discovery can recover it from its configured port.
            self._process_state.pop(config_path.name, None)
        return {
            "started": True,
            "running": True,
            "url": f"http://127.0.0.1:{port}",
            "port": port,
        }

    def launch_profile(self, config_name: str) -> dict[str, object]:
        """Launch an existing config without changing it."""
        with self._lock:
            path = self._resolve_config(config_name)
            settings = Settings.load(path)
            return self._launch_config(path, settings)

    def _settings_from_form(self, data: dict[str, object], config_path: Path) -> Settings:
        profile_id = str(data.get("profile_id", "")).strip()
        target_path = profile_config_path(self.data_dir / "settings.json", profile_id)
        if target_path != config_path:
            raise ValueError("配置文件路径与 profile 名称不一致")
        message_type = str(data.get("message_type", "")).strip()
        criteria = str(data.get("criteria", "")).strip()
        model = str(data.get("llm_model", "")).strip()
        username = str(data.get("mail_username", "")).strip()
        recipient = str(data.get("notification_recipient", "")).strip()
        if not message_type or not criteria or not model:
            raise ValueError("请填写信息类型、筛选条件和模型名称")
        if not username or "@" not in username or not recipient or "@" not in recipient:
            raise ValueError("请填写有效的 QQ 邮箱账号和通知收件地址")
        if str(data.get("output_channel", "qq_mail")) != "qq_mail":
            raise ValueError("当前启动器仅支持 QQ 邮箱输出")
        try:
            port = int(data.get("port", 8765))
            dedup_retention_days = int(data.get("dedup_retention_days", 30))
            filtered_retention_days = int(data.get("filtered_retention_days", 30))
            imap_port = int(data.get("mail_imap_port", 993))
            smtp_port = int(data.get("mail_smtp_port", 465))
            poll_interval = int(data.get("poll_interval_seconds", 60))
        except (TypeError, ValueError) as error:
            raise ValueError("端口和轮询间隔必须是整数") from error
        mail_ingestion_enabled = data.get("mail_ingestion_enabled", False)
        if not isinstance(mail_ingestion_enabled, bool):
            raise ValueError("QQ 邮箱收件开关必须是布尔值")
        return Settings(
            data_dir=self.data_dir,
            config_file=config_path,
            profile_id=profile_id,
            profile_name_bound=True,
            port=port,
            dedup_retention_days=dedup_retention_days,
            filtered_retention_days=filtered_retention_days,
            message_type=message_type,
            criteria=criteria,
            llm_model=model,
            llm_base_url=str(data.get("llm_base_url", "")).strip(),
            output_channel="qq_mail",
            mail_username=username,
            notification_recipient=recipient,
            mail_imap_host=str(data.get("mail_imap_host", "imap.qq.com")).strip(),
            mail_imap_port=imap_port,
            mail_folder=str(data.get("mail_folder", "INBOX")).strip() or "INBOX",
            mail_smtp_host=str(data.get("mail_smtp_host", "smtp.qq.com")).strip(),
            mail_smtp_port=smtp_port,
            poll_interval_seconds=poll_interval,
            mail_ingestion_enabled=mail_ingestion_enabled,
        )

    def create_profile(self, data: dict[str, object]) -> dict[str, object]:
        """Create a bound profile, save scoped secrets, then launch its Agent.

        创建阶段只有提交按钮才有副作用；任何失败都会删除本次文件并恢复凭据原值。
        """
        with self._lock:
            profile_id = str(data.get("profile_id", "")).strip()
            config_path = profile_config_path(self.data_dir / "settings.json", profile_id)
            if config_path.name.casefold() == "settings.json":
                raise ValueError("请将 profile 名称设置为 default 以外的名称")
            if config_path.exists():
                raise FileExistsError(f"配置 {config_path.name} 已存在")
            for existing_path in self._config_paths():
                try:
                    existing_settings = Settings.load(existing_path)
                except Exception:
                    continue
                if existing_settings.profile_id == profile_id:
                    raise FileExistsError(
                        f"profile 名称 {profile_id} 已被 {existing_path.name} 使用"
                    )
            settings = self._settings_from_form(data, config_path)
            credentials = self._credential_store()
            model_account = model_api_key_account(settings.llm_base_url)
            mail_account = qq_mail_password_account(settings.mail_username)
            model_before = credentials.get_secret(model_account)
            legacy_model_before = credentials.get_secret("llm_api_key")
            mail_before = credentials.get_secret(mail_account)
            model_secret = str(data.get("llm_api_key", ""))
            mail_secret = str(data.get("qq_mail_app_password", ""))
            existing_model_key = model_before
            if existing_model_key is None and not settings.llm_base_url.strip():
                existing_model_key = legacy_model_before
            if (
                not model_secret
                and existing_model_key is None
            ):
                raise ValueError("请输入模型 API 密钥；同一模型 URL 的已有 profile 可共用已保存密钥")
            existing_mail_password = credentials.get_secret(mail_account)
            if not mail_secret and existing_mail_password is None:
                raise ValueError("请输入 QQ 邮箱授权码，或使用此邮箱已保存的授权码")

            config_created = False
            original_credentials = {
                model_account: model_before,
                mail_account: mail_before,
            }
            if not settings.llm_base_url.strip():
                original_credentials["llm_api_key"] = legacy_model_before
            modified_accounts: list[str] = []
            try:
                settings.save(config_path, overwrite=False)
                config_created = True
                if model_secret:
                    modified_accounts.append(model_account)
                    credentials.set_secret(model_account, model_secret)
                elif model_before is None and legacy_model_before is not None:
                    modified_accounts.extend((model_account, "llm_api_key"))
                    credentials.set_secret(model_account, legacy_model_before)
                    credentials.delete_secret("llm_api_key")
                if mail_secret:
                    modified_accounts.append(mail_account)
                    credentials.set_secret(mail_account, mail_secret)
                launch = self._launch_config(config_path, settings)
            except Exception:
                for account in reversed(modified_accounts):
                    try:
                        previous = original_credentials[account]
                        if previous is None:
                            credentials.delete_secret(account)
                        else:
                            credentials.set_secret(account, previous)
                    except Exception:
                        pass
                if config_created:
                    config_path.unlink(missing_ok=True)
                raise
            return {
                "config_name": config_path.name,
                "profile_id": profile_id,
                **launch,
            }

    def _stop_agent(self, config_name: str, settings: Settings) -> None:
        port = self._running_port(config_name, settings)
        if port is None:
            self._process_state.pop(config_name, None)
            return
        request = Request(
            f"http://127.0.0.1:{port}/api/shutdown",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{port}",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=3) as response:
                response.read()
        except (OSError, HTTPError, URLError) as error:
            raise RuntimeError(f"无法安全关闭正在运行的 Agent：{error}") from error
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            child = self._children.get(config_name)
            if child and child[0].poll() is not None:
                break
            if not self._health_probe(config_name, port):
                break
            threading.Event().wait(0.15)
        if self._health_probe(config_name, port):
            raise RuntimeError("Agent 尚未完成关闭；配置和数据未删除")
        child = self._children.get(config_name)
        if child and child[0].poll() is None:
            try:
                child[0].wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("Agent 进程尚未退出；配置和数据未删除") from error
        self._children.pop(config_name, None)
        self._process_state.pop(config_name, None)
        self._save_process_state()

    def delete_profile(self, config_name: str) -> dict[str, object]:
        """Delete one profile config and all profile-scoped SQLite state."""
        with self._lock:
            config_path = self._resolve_config(config_name)
            settings = Settings.load(config_path)
            for other_path in self._config_paths():
                if other_path == config_path:
                    continue
                try:
                    other_settings = Settings.load(other_path)
                except Exception as error:
                    raise RuntimeError(
                        f"存在无法读取的配置 {other_path.name}；为避免误删共享状态，请先修复或移走它"
                    ) from error
                if (
                    other_settings.profile_id == settings.profile_id
                    and other_settings.database_path.resolve() == settings.database_path.resolve()
                ):
                    raise RuntimeError(
                        f"另一个配置 {other_path.name} 使用相同 profile 和 SQLite 数据；请先处理该配置"
                    )
            self._stop_agent(config_path.name, settings)
            tombstone = config_path.parent / f".{config_path.name}.{os.getpid()}.deleting"
            os.replace(config_path, tombstone)
            try:
                store = DedupStore(settings.database_path)
                MemoryStore(settings.database_path, settings.profile_id)
                store.delete_profile(settings.profile_id)
                tombstone.unlink(missing_ok=True)
            except Exception:
                if tombstone.exists() and not config_path.exists():
                    os.replace(tombstone, config_path)
                raise

            other_settings = []
            unknown_profile_reference = False
            for other_path in self._config_paths():
                try:
                    other_settings.append(Settings.load(other_path))
                except Exception:
                    # Unknown configs might still depend on these shared credentials.
                    unknown_profile_reference = True
                    break
            deleted_credentials = []
            warnings = []
            try:
                if unknown_profile_reference:
                    raise RuntimeError("仍有无法读取的 profile 配置；为保护可能共享的密钥，未清理凭据")
                credentials = self._credential_store()
                model_account = model_api_key_account(settings.llm_base_url)
                if not any(
                    item is not None
                    and model_api_key_account(item.llm_base_url) == model_account
                    for item in other_settings
                ):
                    credentials.delete_secret(model_account)
                    deleted_credentials.append("model URL credential")
                    if not settings.llm_base_url.strip():
                        credentials.delete_secret("llm_api_key")
                mail_account = qq_mail_password_account(settings.mail_username)
                if settings.mail_username and not any(
                    item is not None
                    and qq_mail_password_account(item.mail_username) == mail_account
                    for item in other_settings
                ):
                    credentials.delete_secret(mail_account)
                    deleted_credentials.append("mailbox credential")
                if not other_settings:
                    credentials.delete_secret("qq_mail_app_password")
            except Exception as error:
                warnings.append(f"配置及 profile 数据已删除，但凭据清理失败：{error}")
            self._process_state.pop(config_path.name, None)
            try:
                self._save_process_state()
            except OSError:
                pass
            return {
                "deleted": True,
                "profile_id": settings.profile_id,
                "deleted_credentials": deleted_credentials,
                "warnings": warnings,
            }


def create_launcher_handler(service: LauncherService) -> type[BaseHTTPRequestHandler]:
    """Create the loopback-only configuration launcher HTTP handler."""
    class LauncherHandler(BaseHTTPRequestHandler):
        server_version = "MessageFilteringAgentLauncher/0.1"

        def log_message(self, format: str, *args: object) -> None:
            print(f"[{self.log_date_time_string()}] {self.address_string()} {format % args}")

        def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            self._send_json(status, {"error": message})

        def _record_activity(self) -> None:
            service.record_activity()

        def _local_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            parsed = urlsplit(origin)
            return (
                parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "localhost"}
                and parsed.port == self.server.server_port
            )

        def _read_json(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 1_100_000:
                raise ValueError("请求正文为空或超出大小限制")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("请求正文必须是 JSON 对象")
            return payload

        def do_GET(self) -> None:
            self._record_activity()
            path = urlsplit(self.path).path
            assets = {
                "/assets/style.css": ("style.css", "text/css; charset=utf-8"),
                "/assets/launcher.css": ("launcher.css", "text/css; charset=utf-8"),
                "/assets/launcher.js": ("launcher.js", "text/javascript; charset=utf-8"),
            }
            if path == "/":
                self._serve_static("launcher.html", "text/html; charset=utf-8")
            elif path in assets:
                self._serve_static(*assets[path])
            elif path == "/api/profiles":
                self._send_json(HTTPStatus.OK, {"profiles": service.list_profiles()})
            elif path == "/api/health":
                self._send_json(HTTPStatus.OK, {"ok": True, "mode": "launcher"})
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "接口不存在")

        def do_POST(self) -> None:
            if not self._local_origin():
                self._send_error(HTTPStatus.FORBIDDEN, "仅允许本机页面请求")
                return
            self._record_activity()
            try:
                data = self._read_json()
                path = urlsplit(self.path).path
                if path == "/api/heartbeat":
                    self._send_json(HTTPStatus.OK, {"active": True})
                elif path == "/api/profiles/create":
                    result = service.create_profile(data)
                    self._send_json(HTTPStatus.CREATED, result)
                elif path == "/api/profiles/launch":
                    name = str(data.get("config_name", ""))
                    self._send_json(HTTPStatus.OK, service.launch_profile(name))
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, "接口不存在")
            except (ValueError, FileExistsError, json.JSONDecodeError) as error:
                self._send_error(HTTPStatus.BAD_REQUEST, str(error))
            except Exception as error:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

        def do_DELETE(self) -> None:
            if not self._local_origin():
                self._send_error(HTTPStatus.FORBIDDEN, "仅允许本机页面请求")
                return
            self._record_activity()
            match = re.fullmatch(r"/api/profiles/(.+)", urlsplit(self.path).path)
            if not match:
                self._send_error(HTTPStatus.NOT_FOUND, "接口不存在")
                return
            try:
                self._send_json(
                    HTTPStatus.OK,
                    service.delete_profile(unquote(match.group(1))),
                )
            except FileNotFoundError as error:
                self._send_error(HTTPStatus.NOT_FOUND, str(error))
            except Exception as error:
                self._send_error(HTTPStatus.BAD_REQUEST, str(error))

        def _serve_static(self, filename: str, content_type: str) -> None:
            body = (STATIC_DIR / filename).read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:",
            )
            self.end_headers()
            self.wfile.write(body)

    return LauncherHandler


def _launcher_port(start: int) -> int:
    return _available_port(start)


def _shutdown_when_idle(
    server: ThreadingHTTPServer,
    service: LauncherService,
    stop_event: threading.Event,
    idle_timeout_seconds: float,
) -> None:
    while not stop_event.is_set():
        remaining = idle_timeout_seconds - service.idle_seconds()
        if remaining <= 0:
            server.shutdown()
            return
        if stop_event.wait(min(remaining, 30.0)):
            return


def main(argv: list[str] | None = None) -> None:
    """Run either the pre-start launcher or an Agent child in packaged mode."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--run-agent":
        from .app import main as agent_main

        agent_main(arguments[1:])
        return

    parser = argparse.ArgumentParser(description="MessageFilteringAgent 便携启动器")
    parser.add_argument("--port", type=int, default=8764, help="本地启动器端口")
    parser.add_argument("--data-dir", type=Path, help="覆盖本地 profile 数据目录")
    options = parser.parse_args(arguments)
    port = _launcher_port(options.port)
    service = LauncherService(options.data_dir)
    server = ThreadingHTTPServer(
        ("127.0.0.1", port),
        create_launcher_handler(service),
    )
    server.daemon_threads = True
    stop_idle_watch = threading.Event()
    idle_watch = threading.Thread(
        target=_shutdown_when_idle,
        args=(server, service, stop_idle_watch, LAUNCHER_IDLE_TIMEOUT_SECONDS),
        name="launcher-idle-shutdown",
        daemon=True,
    )
    idle_watch.start()
    url = f"http://127.0.0.1:{port}"
    print(f"本地启动器已启动：{url}")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("正在关闭启动器；已启动的 Agent 继续运行。")
    finally:
        stop_idle_watch.set()
        idle_watch.join(timeout=1)
        server.server_close()


if __name__ == "__main__":
    main()