"""Validated non-secret application settings.

应用的非敏感配置定义与持久化；密钥必须交给系统凭据库保存。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit


def default_data_dir() -> Path:
    """Choose a per-user writable directory for settings and SQLite state.

    优先使用 Windows 用户级数据目录；其他平台退回到用户主目录。
    """
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "MessageFilteringAgent"
    return Path.home() / ".local" / "share" / "message-filtering-agent"


def default_config_path(data_dir: Path | None = None) -> Path:
    """Find the default config, or the only profile config after first-save naming.

    默认 settings.json 不存在时，仅在目录里只有一个 JSON 配置时自动续接。
    """
    directory = data_dir or default_data_dir()
    settings_path = directory / "settings.json"
    if settings_path.exists():
        return settings_path
    profile_files = sorted(
        candidate for candidate in directory.glob("*.json") if candidate.is_file()
    )
    if len(profile_files) == 1:
        return profile_files[0]
    if len(profile_files) > 1:
        raise ValueError("默认配置文件不存在且发现多个 profile 配置，请通过 --config 指定文件")
    return settings_path


def profile_config_path(current_path: Path, profile_id: str) -> Path:
    """Return a safe JSON path whose stem is the selected profile name.

    将 profile 名称作为 JSON 文件名，并拒绝 Windows 不允许的文件名。
    """
    name = profile_id.strip()
    invalid_characters = '<>:"/\\|?*'
    reserved_names = {"CON", "PRN", "AUX", "NUL"}
    reserved_names.update(f"COM{number}" for number in range(1, 10))
    reserved_names.update(f"LPT{number}" for number in range(1, 10))
    if (
        not name
        or len(name) > 120
        or any(ord(character) < 32 or character in invalid_characters for character in name)
        or name.endswith((" ", "."))
        or name.casefold().endswith(".json")
        or name.split(".", 1)[0].upper() in reserved_names
    ):
        raise ValueError("配置名称不能作为 Windows 文件名；请移除非法字符或 .json 后缀")
    return current_path.with_name(f"{name}.json")


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable, validated configuration shared by runtime components.

    不可变设置便于每次界面保存后整体校验，再替换运行时配置。
    """

    data_dir: Path = default_data_dir()
    profile_id: str = "default"
    dedup_retention_days: int = 30
    filtered_retention_days: int = 30
    port: int = 8765
    wechat_input_mode: str = "manual"
    # SIWX fields are retained as a future WeChat input placeholder; the current UI/API cannot activate them.
    siwx_export_dir: str = ""
    siwx_auto_export_enabled: bool = False
    siwx_api_url: str = "http://127.0.0.1:8787"
    siwx_account: str = ""
    siwx_chats: str = ""
    siwx_auto_export_start_date: str = ""
    siwx_auto_export_interval_days: int = 1
    siwx_auto_cleanup_enabled: bool = True
    siwx_auto_cleanup_retention_days: int = 7
    message_type: str = "招聘信息"
    criteria: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str = ""
    output_channel: str = "qq_mail"
    mail_imap_host: str = "imap.qq.com"
    mail_imap_port: int = 993
    mail_folder: str = "INBOX"
    mail_ingestion_enabled: bool = False
    poll_interval_seconds: int = 60
    mail_smtp_host: str = "smtp.qq.com"
    mail_smtp_port: int = 465
    mail_username: str = ""
    notification_recipient: str = ""
    profile_name_bound: bool = False
    config_file: Path | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        """Reject invalid values before they reach network or server code.

        在设置落盘或启动服务前拦截非法范围和未知选项。
        """
        if not self.profile_id.strip():
            raise ValueError("profile_id must not be empty")
        if not 1 <= self.dedup_retention_days <= 3650:
            raise ValueError("dedup_retention_days must be between 1 and 3650")
        if not 1 <= self.filtered_retention_days <= 3650:
            raise ValueError("filtered_retention_days must be between 1 and 3650")
        if not 1024 <= self.port <= 65535:
            raise ValueError("port must be between 1024 and 65535")
        if not self.message_type.strip():
            raise ValueError("message_type must not be empty")
        if self.output_channel not in {"qq_mail", "qq_message"}:
            raise ValueError("output_channel must be qq_mail or qq_message")
        if self.wechat_input_mode not in {"manual", "siwx"}:
            raise ValueError("wechat_input_mode must be manual or siwx")
        if not 1 <= self.siwx_auto_export_interval_days <= 30:
            raise ValueError("siwx_auto_export_interval_days must be between 1 and 30")
        if not 1 <= self.siwx_auto_cleanup_retention_days <= 3650:
            raise ValueError("siwx_auto_cleanup_retention_days must be between 1 and 3650")
        if self.siwx_auto_export_start_date:
            try:
                date.fromisoformat(self.siwx_auto_export_start_date)
            except ValueError as error:
                raise ValueError("SIWX 首次导出开始日期必须使用 YYYY-MM-DD") from error
        if self.siwx_auto_export_enabled:
            if self.wechat_input_mode != "siwx":
                raise ValueError("启用 SIWX 自动导出时，微信数据来源必须选择 SIWX")
            parsed_url = urlsplit(self.siwx_api_url.strip())
            try:
                api_port = parsed_url.port
            except ValueError as error:
                raise ValueError("SIWX 服务地址端口无效") from error
            if (
                parsed_url.scheme != "http"
                or parsed_url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or (api_port is not None and not 1 <= api_port <= 65535)
                or parsed_url.username is not None
                or parsed_url.password is not None
                or parsed_url.path not in {"", "/"}
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise ValueError("SIWX 服务地址必须是本机 HTTP 地址，例如 http://127.0.0.1:8787")
            if not self.siwx_export_dir.strip():
                raise ValueError("自动导出需要填写 SIWX exports 目录")
            if not self.siwx_account.strip():
                raise ValueError("自动导出需要填写 SIWX 账号 wxid")
            if not any(line.strip() for line in self.siwx_chats.splitlines()):
                raise ValueError("自动导出至少需要填写一个 SIWX 会话 username")
        for field_name in ("mail_imap_port", "mail_smtp_port"):
            port = getattr(self, field_name)
            if not 1 <= port <= 65535:
                raise ValueError(f"{field_name} must be between 1 and 65535")
        if not 10 <= self.poll_interval_seconds <= 3600:
            raise ValueError("poll_interval_seconds must be between 10 and 3600")

    @property
    def database_path(self) -> Path:
        """Return the persistent SQLite database path."""
        return self.data_dir / "agent.sqlite3"

    @property
    def settings_path(self) -> Path:
        """Return the non-secret JSON settings path."""
        return self.config_file or self.data_dir / "settings.json"

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        """Load persisted preferences, using defaults on first launch.

        首次运行时不创建空配置文件，直到用户保存设置才写盘。
        """
        settings_path = path or default_config_path()
        default_profile = (
            "default" if settings_path.name.lower() == "settings.json" else settings_path.stem
        )
        if not settings_path.exists():
            return cls(
                data_dir=settings_path.parent,
                config_file=settings_path,
                profile_id=default_profile,
            )
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
        wechat_input_mode = raw.get("wechat_input_mode", "manual")
        siwx_auto_export_enabled = bool(raw.get("siwx_auto_export_enabled", False))
        # SIWX support remains as a future placeholder; old configs must not activate it.
        if wechat_input_mode in {"wxauto", "siwx"}:
            wechat_input_mode = "manual"
            siwx_auto_export_enabled = False
        return cls(
            data_dir=Path(raw.get("data_dir", settings_path.parent)),
            config_file=settings_path,
            profile_id=raw.get("profile_id", default_profile),
            dedup_retention_days=int(raw.get("dedup_retention_days", 30)),
            filtered_retention_days=int(raw.get("filtered_retention_days", 30)),
            port=int(raw.get("port", 8765)),
            wechat_input_mode=wechat_input_mode,
            siwx_export_dir=raw.get("siwx_export_dir", ""),
            siwx_auto_export_enabled=siwx_auto_export_enabled,
            siwx_api_url=raw.get("siwx_api_url", "http://127.0.0.1:8787"),
            siwx_account=raw.get("siwx_account", ""),
            siwx_chats=raw.get("siwx_chats", ""),
            siwx_auto_export_start_date=raw.get("siwx_auto_export_start_date", ""),
            siwx_auto_export_interval_days=int(
                raw.get("siwx_auto_export_interval_days", 1)
            ),
            siwx_auto_cleanup_enabled=bool(raw.get("siwx_auto_cleanup_enabled", True)),
            siwx_auto_cleanup_retention_days=int(
                raw.get("siwx_auto_cleanup_retention_days", 7)
            ),
            message_type=raw.get("message_type", "招聘信息"),
            criteria=raw.get("criteria", ""),
            llm_model=raw.get("llm_model", "gpt-4o-mini"),
            llm_base_url=raw.get("llm_base_url", ""),
            output_channel=raw.get("output_channel", "qq_mail"),
            mail_imap_host=raw.get("mail_imap_host", "imap.qq.com"),
            mail_imap_port=int(raw.get("mail_imap_port", 993)),
            mail_folder=raw.get("mail_folder", "INBOX"),
            mail_ingestion_enabled=bool(raw.get("mail_ingestion_enabled", False)),
            poll_interval_seconds=int(raw.get("poll_interval_seconds", 60)),
            mail_smtp_host=raw.get("mail_smtp_host", "smtp.qq.com"),
            mail_smtp_port=int(raw.get("mail_smtp_port", 465)),
            mail_username=raw.get("mail_username", ""),
            notification_recipient=raw.get("notification_recipient", ""),
            # Existing JSON without the binding marker is a legacy, already-initialized profile.
            # 缺少绑定标记的既有配置按已初始化处理，不再获得首次命名机会。
            profile_name_bound=bool(raw.get("profile_name_bound", True)),
        )

    def save(self, path: Path | None = None, *, overwrite: bool = True) -> Path:
        """Atomically write preferences, optionally refusing to replace a file.

        先写同目录临时文件再替换目标，避免中断留下半份 JSON 配置。
        """
        settings_path = path or self.settings_path
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        payload.pop("config_file", None)
        payload["data_dir"] = str(self.data_dir)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f"{settings_path.name}.", suffix=".tmp", dir=settings_path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
            if overwrite:
                # Same-directory replacement keeps the final rename atomic on supported filesystems.
                # 临时文件与目标位于同一目录，以支持文件系统原子替换。
                os.replace(temporary_name, settings_path)
            else:
                # Hard-link creation is atomic and fails if another file already owns the name.
                # 硬链接原子创建且不会覆盖并发创建的同名文件。
                os.link(temporary_name, settings_path)
                Path(temporary_name).unlink()
        except Exception:
            Path(temporary_name).unlink(missing_ok=True)
            raise
        return settings_path