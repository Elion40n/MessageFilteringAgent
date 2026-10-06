"""Access secrets through the operating system credential store.

仅通过操作系统凭据后端读取密钥；明文密钥不进入设置 JSON 或交接文件。
"""

import hashlib
from urllib.parse import urlsplit, urlunsplit


def normalize_model_url(base_url: str) -> str:
    """Normalize equivalent endpoint spellings before sharing a model key.

    清理空白、主机大小写和尾斜杠，使同一模型端点使用同一凭据槽。
    """
    value = base_url.strip()
    if not value:
        return "default"
    parts = urlsplit(value)
    scheme = parts.scheme.casefold()
    hostname = (parts.hostname or "").casefold()
    if not scheme or not hostname:
        return value.rstrip("/")
    try:
        port = parts.port
    except ValueError:
        return value.rstrip("/")
    if port and not (scheme == "https" and port == 443) and not (scheme == "http" and port == 80):
        hostname = f"{hostname}:{port}"
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, hostname, path, parts.query, parts.fragment))


def model_api_key_account(base_url: str) -> str:
    """Return an opaque credential account scoped to one model endpoint."""
    digest = hashlib.sha256(normalize_model_url(base_url).encode("utf-8")).hexdigest()
    return f"llm_api_key_url:{digest}"


def qq_mail_password_account(username: str) -> str:
    """Return an opaque credential account scoped to one QQ mailbox."""
    normalized = username.strip().casefold()
    if not normalized:
        return "qq_mail_app_password"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"qq_mail_app_password_user:{digest}"


class CredentialStoreUnavailable(RuntimeError):
    """Raised when the system keyring backend is unavailable.

    系统凭据库不可用时用明确错误阻止静默退回明文存储。
    """


class CredentialStore:
    """Small adapter around the optional keyring dependency.

    将具体凭据后端隔离，未安装依赖时仅在用户启用密钥功能时失败。
    """

    SERVICE_NAME = "MessageFilteringAgent"

    def __init__(self) -> None:
        try:
            import keyring
        except ImportError as error:
            raise CredentialStoreUnavailable(
                "Install project dependencies to use the OS credential store."
            ) from error
        self._keyring = keyring

    def set_secret(self, account: str, secret: str) -> None:
        """Save a secret under the app namespace and account key."""
        self._keyring.set_password(self.SERVICE_NAME, account, secret)

    def get_secret(self, account: str) -> str | None:
        """Return the secret, or None when it has not been configured."""
        return self._keyring.get_password(self.SERVICE_NAME, account)

    def get_model_api_key(self, base_url: str) -> str | None:
        """Read an endpoint-scoped key and migrate the legacy key to default only.

        旧全局 key 没有 endpoint 元数据，因此只安全归属默认 URL；自定义 endpoint
        必须显式配置自己的 key，避免读取时被错误分配到某个 URL。
        """
        account = model_api_key_account(base_url)
        secret = self.get_secret(account)
        if secret is not None:
            return secret
        if base_url.strip():
            return None
        legacy = self.get_secret("llm_api_key")
        if legacy is None:
            return None
        self.set_secret(account, legacy)
        self.delete_secret("llm_api_key")
        return legacy

    def get_qq_mail_app_password(self, username: str) -> str | None:
        """Read a mailbox-scoped credential and migrate the old global key once."""
        account = qq_mail_password_account(username)
        secret = self.get_secret(account)
        if secret is not None:
            return secret
        legacy = self.get_secret("qq_mail_app_password")
        if legacy is None:
            return None
        if username.strip():
            self.set_secret(account, legacy)
            self.delete_secret("qq_mail_app_password")
        return legacy

    def delete_secret(self, account: str) -> None:
        """Delete a configured secret; deleting an absent key is harmless."""
        try:
            self._keyring.delete_password(self.SERVICE_NAME, account)
        except self._keyring.errors.PasswordDeleteError:
            # Removing an already absent credential is an idempotent cleanup operation.
            # 凭据本就不存在时视为清理成功，保持删除操作可重复执行。
            pass