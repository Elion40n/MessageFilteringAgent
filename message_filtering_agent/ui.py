"""Local-only browser UI and JSON API.

仅服务本机浏览器的页面与 JSON API；此模块不把数据或凭据暴露到远程网络。
"""

from __future__ import annotations

from dataclasses import fields
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .app import Runtime


STATIC_DIR = Path(__file__).parent / "static"
SECRET_NAMES = {"llm_api_key", "qq_mail_app_password"}


def _public_settings(runtime: Runtime) -> dict[str, object]:
    """Expose editable preferences without revealing local paths or secrets.

    返回可编辑的公开设置，但不泄露本地路径或凭据。
    """
    settings = {
        item.name: getattr(runtime.settings, item.name)
        for item in fields(runtime.settings)
        if item.name not in {"data_dir", "config_file"}
    }
    settings["config_name"] = runtime.settings.settings_path.name
    return settings


def _question_payload(question: dict[str, object]) -> dict[str, object]:
    """Convert typed persisted objects into the UI's JSON review shape."""
    message = question["message"]
    decision = question["decision"]
    return {
        "question_id": question["question_id"],
        "source": message.source.value,
        "conversation": message.conversation_id,
        "sender": message.sender,
        "timestamp": message.timestamp.isoformat(),
        "content": message.content,
        "evidence": list(decision.evidence),
        "missing_fields": list(decision.missing_fields),
        "explanation": decision.explanation,
        "answers": question["answers"],
    }


def _filtered_message_payload(item: dict[str, object]) -> dict[str, object]:
    """Convert a profile-scoped filtered archive record to its UI payload.

    将当前 profile 的过滤归档转换为网页列表数据。
    """
    message = item["message"]
    decision = item["decision"]
    return {
        "archive_id": item["archive_id"],
        "profile_id": item["profile_id"],
        "source": message.source.value,
        "conversation": message.conversation_id,
        "sender": message.sender,
        "timestamp": message.timestamp.isoformat(),
        "archived_at": item["created_at"].isoformat(),
        "expires_at": item["expires_at"].isoformat(),
        "source_ref": message.source_ref,
        "content": message.content,
        "decision": decision.label.display_name,
        "evidence": list(decision.evidence),
        "explanation": decision.explanation,
        "status": item["status"],
    }


def create_handler(runtime: Runtime) -> type[BaseHTTPRequestHandler]:
    """Bind request handlers to one runtime without global mutable state."""
    class LocalHandler(BaseHTTPRequestHandler):
        server_version = "MessageFilteringAgent/0.1"

        def log_message(self, format: str, *args: object) -> None:
            """Keep standard request logging while making local activity visible."""
            print(f"[{self.log_date_time_string()}] {self.address_string()} {format % args}")

        def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            """Send UTF-8 JSON with cache and content-sniffing protections."""
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

        def _read_json(self) -> dict[str, object]:
            """Read only bounded JSON objects, rejecting empty or oversized bodies."""
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 1_000_000:
                raise ValueError("请求正文为空或超出大小限制")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("请求正文必须是 JSON 对象")
            return data

        def _origin_is_local(self) -> bool:
            """Reject cross-origin browser writes; server binding remains loopback-only.

            Origin 校验用于防止其他网站借浏览器向本地 API 发起写请求；无 Origin 的
            非浏览器本地客户端仍由 loopback 绑定限制在本机。
            """
            origin = self.headers.get("Origin")
            if not origin:
                return True
            parsed = urlsplit(origin)
            return (
                parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "localhost"}
                and parsed.port == self.server.server_port
            )

        def do_GET(self) -> None:
            """Serve bundled assets and read-only status/config/review endpoints.

            提供静态页面，以及状态、当前配置、待确认事项和 profile 记忆读取接口。
            """
            path = urlsplit(self.path).path
            if path == "/":
                self._serve_static("index.html", "text/html; charset=utf-8")
            elif path == "/assets/app.js":
                self._serve_static("app.js", "text/javascript; charset=utf-8")
            elif path == "/assets/style.css":
                self._serve_static("style.css", "text/css; charset=utf-8")
            elif path == "/api/health":
                self._send_json(HTTPStatus.OK, {"ok": True, "mode": "localhost"})
            elif path == "/api/settings":
                self._send_json(HTTPStatus.OK, _public_settings(runtime))
            elif path == "/api/activity":
                self._send_json(
                    HTTPStatus.OK,
                    runtime.storage.get_activity_snapshot(runtime.settings.profile_id),
                )
            elif path == "/api/questions":
                questions = [
                    _question_payload(item)
                    for item in runtime.storage.list_pending_questions(
                        runtime.settings.profile_id
                    )
                ]
                self._send_json(HTTPStatus.OK, {"questions": questions})
            elif path == "/api/filtered-messages":
                messages = [
                    _filtered_message_payload(item)
                    for item in runtime.storage.list_filtered_messages(
                        runtime.settings.profile_id
                    )
                ]
                self._send_json(HTTPStatus.OK, {"messages": messages})
            elif path == "/api/memories":
                self._send_json(
                    HTTPStatus.OK, {"memories": runtime.memory.list_entries()}
                )
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "接口不存在")

        def do_POST(self) -> None:
            """Handle local writes, message processing, secrets, and review answers.

            处理本机配置/记忆写入、消息提交、凭据保存和人工确认。
            """
            if not self._origin_is_local():
                self._send_error(HTTPStatus.FORBIDDEN, "仅允许本机页面请求")
                return
            path = urlsplit(self.path).path
            try:
                data = self._read_json()
                if path == "/api/settings":
                    previous_mode = runtime.settings.wechat_input_mode
                    previous_profile_id = runtime.settings.profile_id
                    previous_config_path = runtime.settings.settings_path
                    updated = runtime.update_settings(data)
                    restart_required = (
                        updated.wechat_input_mode != previous_mode
                        or updated.profile_id != previous_profile_id
                        or updated.settings_path != previous_config_path
                    )
                    payload = _public_settings(runtime)
                    if restart_required:
                        payload["restart_required"] = True
                    self._send_json(HTTPStatus.OK, payload)
                    if restart_required:
                        # shutdown() blocks until serve_forever exits, so never call it
                        # synchronously from this request handler thread.
                        # shutdown() 会等待服务循环退出，因此必须从请求处理线程之外调用。
                        threading.Thread(
                            target=self.server.shutdown,
                            name="agent-config-shutdown",
                            daemon=True,
                        ).start()
                elif path == "/api/shutdown":
                    self._send_json(HTTPStatus.OK, {"stopping": True})
                    threading.Thread(
                        target=self.server.shutdown,
                        name="agent-launcher-shutdown",
                        daemon=True,
                    ).start()
                elif path == "/api/memories":
                    entry = runtime.add_memory(data)
                    self._send_json(HTTPStatus.CREATED, {"memory": entry})
                elif path == "/api/secrets":
                    self._save_secret(data)
                elif path == "/api/messages":
                    transcripts = data.get("transcripts")
                    if transcripts is None:
                        transcript = data.get("content", "")
                        if not isinstance(transcript, str):
                            raise ValueError("content 必须是聊天记录文本")
                        result = runtime.process_manual_transcript(transcript)
                        self._send_json(
                            HTTPStatus.OK,
                            {
                                "segments": result["segments"],
                                "results": [
                                    self._result_payload(item)
                                    for item in result["results"]
                                ],
                            },
                        )
                    else:
                        if not isinstance(transcripts, list) or any(
                            not isinstance(item, str) for item in transcripts
                        ):
                            raise ValueError("transcripts 必须是聊天记录文本列表")
                        groups = runtime.process_manual_transcripts(transcripts)
                        self._send_json(
                            HTTPStatus.OK,
                            {
                                "groups": [
                                    {
                                        **group,
                                        "results": [
                                            self._result_payload(item)
                                            for item in group["results"]
                                        ],
                                    }
                                    for group in groups
                                ],
                            },
                        )
                else:
                    memory_match = re.fullmatch(r"/api/memories/([a-f0-9]+)", path)
                    filtered_match = re.fullmatch(
                        r"/api/filtered-messages/([a-f0-9]+)/promote", path
                    )
                    question_match = re.fullmatch(
                        r"/api/questions/([a-f0-9]+)/answer", path
                    )
                    if memory_match:
                        if not runtime.update_memory(memory_match.group(1), data):
                            self._send_error(HTTPStatus.NOT_FOUND, "记忆不存在")
                            return
                        self._send_json(HTTPStatus.OK, {"updated": True})
                    elif filtered_match:
                        question_id = runtime.storage.promote_filtered_message(
                            filtered_match.group(1), runtime.settings.profile_id
                        )
                        if question_id is None:
                            self._send_error(HTTPStatus.NOT_FOUND, "过滤信息不存在或已过期")
                            return
                        self._send_json(
                            HTTPStatus.OK,
                            {"promoted": True, "question_id": question_id},
                        )
                    elif question_match:
                        runtime.answer_question(
                            question_match.group(1),
                            str(data.get("answer", "")),
                            str(data.get("choice", "")),
                        )
                        self._send_json(HTTPStatus.OK, {"processed": True})
                    else:
                        self._send_error(HTTPStatus.NOT_FOUND, "接口不存在")
            except (ValueError, json.JSONDecodeError) as error:
                self._send_error(HTTPStatus.BAD_REQUEST, str(error))
            except Exception as error:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

        def do_DELETE(self) -> None:
            """Delete one memory after enforcing the local-origin write boundary.

            校验本机来源后删除一条当前 profile 的记忆。
            """
            if not self._origin_is_local():
                self._send_error(HTTPStatus.FORBIDDEN, "仅允许本机页面请求")
                return
            match = re.fullmatch(r"/api/memories/([a-f0-9]+)", urlsplit(self.path).path)
            if not match:
                self._send_error(HTTPStatus.NOT_FOUND, "接口不存在")
                return
            if not runtime.delete_memory(match.group(1)):
                self._send_error(HTTPStatus.NOT_FOUND, "记忆不存在")
                return
            self._send_json(HTTPStatus.OK, {"deleted": True})

        def _save_secret(self, data: dict[str, object]) -> None:
            """Validate a named secret and store it without echoing its value."""
            from .credentials import (
                CredentialStore,
                model_api_key_account,
                qq_mail_password_account,
            )

            name = str(data.get("name", ""))
            secret = str(data.get("secret", ""))
            if name not in SECRET_NAMES:
                self._send_error(HTTPStatus.BAD_REQUEST, "不支持的凭据名称")
                return
            if not secret:
                self._send_error(HTTPStatus.BAD_REQUEST, "密钥不能为空")
                return
            credentials = CredentialStore()
            if name == "llm_api_key":
                account = model_api_key_account(str(data.get("model_url", "")))
            else:
                account = qq_mail_password_account(str(data.get("mail_username", "")))
            credentials.set_secret(account, secret)
            self._send_json(HTTPStatus.OK, {"saved": True})

        def _serve_static(self, filename: str, content_type: str) -> None:
            """Serve only fixed, bundled asset names with a restrictive CSP."""
            body = (STATIC_DIR / filename).read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:",
            )
            self.end_headers()
            self.wfile.write(body)

        @staticmethod
        def _result_payload(result: dict[str, object]) -> dict[str, object]:
            """Keep internal objects out of responses and expose only display fields."""
            decision = result.get("decision")
            if decision is None:
                payload = {
                    "status": result.get("status", "unknown"),
                    "pending_question_id": result.get("pending_question_id"),
                }
                if result.get("error"):
                    payload["error"] = result["error"]
                return payload
            return {
                "status": result.get("status", "unknown"),
                "decision": decision.label.display_name,
                "evidence": list(decision.evidence),
                "extracted_fields": decision.extracted_fields,
                "missing_fields": list(decision.missing_fields),
                "explanation": decision.explanation,
                "pending_question_id": result.get("pending_question_id"),
            }

    return LocalHandler


def serve(runtime: Runtime, port: int) -> None:
    """Run the threaded HTTP server on loopback and close it cleanly."""
    # Binding to 127.0.0.1 prevents LAN/phone access in v1; do not change to 0.0.0.0 casually.
    # 绑定 127.0.0.1 使首版仅本机可访问；不要随意改为 0.0.0.0。
    server = ThreadingHTTPServer(("127.0.0.1", port), create_handler(runtime))
    server.daemon_threads = True
    print(f"本地界面已启动：http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("正在关闭本地界面。")
    finally:
        server.shutdown()
        server.server_close()