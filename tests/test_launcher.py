"""Offline tests for launcher profile and credential transactions."""

from __future__ import annotations

import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from message_filtering_agent.config import Settings
from message_filtering_agent.credentials import (
    model_api_key_account,
    qq_mail_password_account,
)
from message_filtering_agent.launcher import (
    LauncherService,
    _shutdown_when_idle,
    create_launcher_handler,
)
from message_filtering_agent.memory import MemoryStore
from message_filtering_agent.storage import DedupStore


class FakeCredentials:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.fail_on: str | None = None

    def get_secret(self, account: str) -> str | None:
        return self.values.get(account)

    def set_secret(self, account: str, secret: str) -> None:
        if account == self.fail_on:
            self.fail_on = None
            raise RuntimeError("simulated credential write failure")
        self.values[account] = secret

    def delete_secret(self, account: str) -> None:
        self.values.pop(account, None)

    def get_model_api_key(self, base_url: str) -> str | None:
        value = self.get_secret(model_api_key_account(base_url))
        if value is None and not base_url.strip():
            return self.get_secret("llm_api_key")
        return value

    def get_qq_mail_app_password(self, username: str) -> str | None:
        return self.get_secret(qq_mail_password_account(username)) or self.get_secret(
            "qq_mail_app_password"
        )


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.exit_code: int | None = None

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.exit_code = 1

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code or 0


class LauncherServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.credentials = FakeCredentials()
        self.processes: list[tuple[list[str], dict[str, object]]] = []
        self.agent_ready = False

        def start_process(command, **kwargs):
            self.processes.append((command, kwargs))
            self.agent_ready = True
            return FakeProcess(1000 + len(self.processes))

        self.service = LauncherService(
            self.root,
            credentials=self.credentials,
            process_factory=start_process,
            health_probe=lambda _name, _port: self.agent_ready,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def payload(profile_id: str, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "profile_id": profile_id,
            "message_type": "招聘信息",
            "criteria": "工作地点为上海",
            "llm_model": "test-model",
            "llm_base_url": "https://model.example.test/v1",
            "mail_username": "sender@qq.example",
            "notification_recipient": "recipient@example.test",
            "llm_api_key": "model-secret",
            "qq_mail_app_password": "mail-secret",
        }
        values.update(overrides)
        return values

    def test_create_profile_saves_secrets_outside_config_and_launches_agent(self) -> None:
        result = self.service.create_profile(self.payload("research"))

        config_path = self.root / "research.json"
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(result["config_name"], "research.json")
        self.assertTrue(result["started"])
        self.assertTrue(payload["profile_name_bound"])
        self.assertEqual(Path(payload["data_dir"]).resolve(), self.root.resolve())
        self.assertNotIn("llm_api_key", payload)
        self.assertNotIn("qq_mail_app_password", payload)
        self.assertEqual(
            self.credentials.get_secret(model_api_key_account("https://model.example.test/v1")),
            "model-secret",
        )
        self.assertEqual(
            self.credentials.get_secret(qq_mail_password_account("sender@qq.example")),
            "mail-secret",
        )
        command = self.processes[0][0]
        self.assertIn("--run-agent", command)
        self.assertIn("--config", command)
        config_argument = command[command.index("--config") + 1]
        self.assertEqual(Path(config_argument).resolve(), config_path.resolve())

    def test_failed_credential_write_rolls_back_only_new_config_and_changes(self) -> None:
        model_account = model_api_key_account("https://model.example.test/v1")
        mail_account = qq_mail_password_account("sender@qq.example")
        self.credentials.values[model_account] = "old-model-secret"
        self.credentials.values[mail_account] = "old-mail-secret"
        self.credentials.fail_on = mail_account

        with self.assertRaisesRegex(RuntimeError, "simulated credential write failure"):
            self.service.create_profile(self.payload("research"))

        self.assertFalse((self.root / "research.json").exists())
        self.assertEqual(self.credentials.values[model_account], "old-model-secret")
        self.assertEqual(self.credentials.values[mail_account], "old-mail-secret")
        self.assertEqual(self.processes, [])

    def test_agent_startup_timeout_rolls_back_profile_and_credentials(self) -> None:
        account = model_api_key_account("https://model.example.test/v1")
        self.credentials.values[account] = "existing-model-secret"
        self.service._health_probe = lambda _name, _port: False
        self.service._startup_timeout = 0.01

        with self.assertRaisesRegex(RuntimeError, "未能在限定时间内启动"):
            self.service.create_profile(
                self.payload("timeout", llm_api_key="replacement-model-secret")
            )

        self.assertFalse((self.root / "timeout.json").exists())
        self.assertEqual(self.credentials.values[account], "existing-model-secret")
        self.assertNotIn(
            qq_mail_password_account("sender@qq.example"),
            self.credentials.values,
        )

    def test_failed_default_profile_creation_restores_legacy_model_key(self) -> None:
        model_account = model_api_key_account("")
        mail_account = qq_mail_password_account("sender@qq.example")
        self.credentials.values["llm_api_key"] = "legacy-global-key"
        self.credentials.fail_on = mail_account

        with self.assertRaisesRegex(RuntimeError, "simulated credential write failure"):
            self.service.create_profile(
                self.payload("default", llm_base_url="", llm_api_key="")
            )

        self.assertFalse((self.root / "default.json").exists())
        self.assertEqual(self.credentials.values.get("llm_api_key"), "legacy-global-key")
        self.assertNotIn(model_account, self.credentials.values)
        self.assertNotIn(mail_account, self.credentials.values)


class LauncherHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.credentials = FakeCredentials()
        self.processes: list[tuple[list[str], dict[str, object]]] = []
        self.agent_ready = False

        def start_process(command, **kwargs):
            self.processes.append((command, kwargs))
            self.agent_ready = True
            return FakeProcess(2345)

        self.service = LauncherService(
            self.root,
            credentials=self.credentials,
            process_factory=start_process,
            health_probe=lambda _name, _port: self.agent_ready,
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), create_launcher_handler(self.service)
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = HTTPConnection("127.0.0.1", self.server.server_port)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def test_launcher_page_and_profile_creation_api(self) -> None:
        self.connection.request("GET", "/")
        response = self.connection.getresponse()
        html = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("现有配置", html)
        self.assertIn("创建并启动", html)
        self.assertIn("30 分钟无操作会自动关闭", html)

        self.connection.request(
            "POST",
            "/api/profiles/create",
            body=json.dumps(LauncherServiceTests.payload("http-test")),
            headers={"Content-Type": "application/json", "Origin": self.origin},
        )
        response = self.connection.getresponse()
        payload = json.loads(response.read())
        self.assertEqual(response.status, 201)
        self.assertEqual(payload["config_name"], "http-test.json")
        self.assertNotIn("model-secret", json.dumps(payload))

        self.connection.request("GET", "/api/profiles")
        response = self.connection.getresponse()
        profiles = json.loads(response.read())["profiles"]
        self.assertEqual(response.status, 200)
        self.assertEqual([item["profile_id"] for item in profiles], ["http-test"])

    def test_activity_heartbeat_refreshes_launcher_idle_timer(self) -> None:
        previous_activity = self.service.last_activity
        self.connection.request(
            "POST",
            "/api/heartbeat",
            body="{}",
            headers={"Content-Type": "application/json", "Origin": self.origin},
        )
        response = self.connection.getresponse()
        payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(payload, {"active": True})
        self.assertGreaterEqual(self.service.last_activity, previous_activity)

    def test_idle_monitor_shuts_down_the_launcher_server(self) -> None:
        self.service.last_activity = time.monotonic() - 1
        stop_idle_watch = threading.Event()
        idle_watch = threading.Thread(
            target=_shutdown_when_idle,
            args=(self.server, self.service, stop_idle_watch, 0.01),
            daemon=True,
        )
        idle_watch.start()
        self.thread.join(timeout=1)
        stop_idle_watch.set()
        idle_watch.join(timeout=1)
        self.assertFalse(self.thread.is_alive())

    def test_launcher_rejects_foreign_write_origin(self) -> None:
        self.connection.request(
            "POST",
            "/api/profiles/create",
            body=json.dumps(LauncherServiceTests.payload("blocked")),
            headers={
                "Content-Type": "application/json",
                "Origin": "https://attacker.invalid",
            },
        )
        response = self.connection.getresponse()
        payload = json.loads(response.read())
        self.assertEqual(response.status, 403)
        self.assertFalse((self.root / "blocked.json").exists())
        self.assertIn("本机", payload["error"])

    def test_delete_profile_removes_profile_state_but_keeps_shared_credentials(self) -> None:
        first_path = self.root / "first.json"
        second_path = self.root / "second.json"
        for path, profile_id in ((first_path, "first"), (second_path, "second")):
            Settings(
                data_dir=self.root,
                config_file=path,
                profile_id=profile_id,
                profile_name_bound=True,
                llm_base_url="https://shared.example.test/v1",
                mail_username="same@qq.example",
            ).save(path, overwrite=False)

        database_path = self.root / "agent.sqlite3"
        storage = DedupStore(database_path)
        MemoryStore(database_path, "first").add_entry("first memory", "补充")
        MemoryStore(database_path, "second").add_entry("second memory", "补充")
        self.credentials.values[model_api_key_account("https://shared.example.test/v1")] = "shared-model"
        self.credentials.values[qq_mail_password_account("same@qq.example")] = "shared-mail"

        with patch.object(self.service, "_stop_agent"):
            result = self.service.delete_profile("first.json")

        self.assertFalse(first_path.exists())
        self.assertTrue(second_path.exists())
        self.assertEqual(MemoryStore(database_path, "first").list_entries(), [])
        self.assertEqual(len(MemoryStore(database_path, "second").list_entries()), 1)
        self.assertEqual(
            self.credentials.get_secret(model_api_key_account("https://shared.example.test/v1")),
            "shared-model",
        )
        self.assertEqual(
            self.credentials.get_secret(qq_mail_password_account("same@qq.example")),
            "shared-mail",
        )
        self.assertTrue(result["deleted"])

    def test_existing_config_is_never_overwritten(self) -> None:
        existing = self.root / "research.json"
        existing.write_text("preserve this", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            self.service.create_profile(LauncherServiceTests.payload("research"))

        self.assertEqual(existing.read_text(encoding="utf-8"), "preserve this")
        self.assertEqual(self.processes, [])

    def test_custom_model_url_cannot_reuse_legacy_global_key(self) -> None:
        self.credentials.values["llm_api_key"] = "legacy-key"

        with self.assertRaisesRegex(ValueError, "模型 API 密钥"):
            self.service.create_profile(
                LauncherServiceTests.payload(
                    "custom-endpoint",
                    llm_base_url="https://custom.example.test/v1",
                    llm_api_key="",
                )
            )

        self.assertFalse((self.root / "custom-endpoint.json").exists())
        self.assertEqual(self.credentials.values["llm_api_key"], "legacy-key")

    def test_profile_name_is_unique_across_root_and_profiles_directory(self) -> None:
        profile_directory = self.root / "profiles"
        profile_directory.mkdir()
        existing = profile_directory / "same-name.json"
        Settings(
            data_dir=self.root,
            config_file=existing,
            profile_id="same-name",
            profile_name_bound=True,
        ).save(existing, overwrite=False)

        with self.assertRaisesRegex(FileExistsError, "已被 same-name.json 使用"):
            self.service.create_profile(LauncherServiceTests.payload("same-name"))

        self.assertFalse((self.root / "same-name.json").exists())

    def test_launcher_lists_profiles_subdirectory_configs(self) -> None:
        profile_directory = self.root / "profiles"
        profile_directory.mkdir()
        path = profile_directory / "finance.json"
        Settings(
            data_dir=self.root,
            config_file=path,
            profile_id="finance",
            profile_name_bound=True,
        ).save(path, overwrite=False)

        profiles = self.service.list_profiles()

        self.assertEqual([item["config_name"] for item in profiles], ["profiles/finance.json"])


if __name__ == "__main__":
    unittest.main()