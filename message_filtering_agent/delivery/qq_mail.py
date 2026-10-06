"""QQ Mail SMTP delivery with source provenance included in each notice.

QQ 邮箱通知适配器：发送摘要，同时带回原消息来源、时间和定位标识。
"""

from __future__ import annotations

from email.message import EmailMessage
import hashlib
import smtplib
import ssl

from ..config import Settings
from ..credentials import CredentialStore
from ..models import AgentDecision, Message


class QQMailDelivery:
    """Build and send one notification through a configured SMTP profile.

    SMTP 连接只在真正发送符合项时建立，单元测试可独立验证邮件内容。
    """

    def __init__(self, settings: Settings, credentials: CredentialStore) -> None:
        self.settings = settings
        self.credentials = credentials

    def build_message(self, message: Message, decision: AgentDecision) -> EmailMessage:
        """Create a deterministic, provenance-rich MIME message without network I/O.

        先生成稳定 Message-ID；发送结果不确定时，邮件服务器可据此识别重复投递。
        """
        sender = self.settings.mail_username.strip()
        recipient = self.settings.notification_recipient.strip()
        if not sender or not recipient:
            raise ValueError("请先配置 QQ 邮箱账号和通知收件地址")
        identity = "|".join(
            (message.source.value, message.account_id, message.conversation_id,
             message.external_id or message.content)
        )
        message_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        mail = EmailMessage()
        mail["Subject"] = f"筛选命中：{self.settings.message_type}"
        mail["From"] = sender
        mail["To"] = recipient
        mail["Message-ID"] = f"<mfa-{message_id}@message-filtering-agent.local>"
        evidence = "\n".join(f"- {item}" for item in decision.evidence) or "（无）"
        fields = "\n".join(
            f"- {name}: {value}" for name, value in decision.extracted_fields.items()
        ) or "（无）"
        mail.set_content(
            "发现符合条件的信息。\n\n"
            f"来源：{message.source.value}\n"
            f"会话/邮箱：{message.conversation_id}\n"
            f"发送者：{message.sender}\n"
            f"时间：{message.timestamp.isoformat()}\n"
            f"原始定位：{message.source_ref or message.external_id or '手动输入'}\n\n"
            f"信息正文：\n{message.content}\n\n"
            f"判断理由：\n{decision.explanation}\n\n"
            f"提取字段：\n{fields}\n\n"
            f"判断依据：\n{evidence}\n"
        )
        return mail

    def send(self, message: Message, decision: AgentDecision) -> None:
        """Send with implicit TLS on port 465, otherwise negotiate STARTTLS."""
        password = self.credentials.get_qq_mail_app_password(self.settings.mail_username)
        if not password:
            raise ValueError("请先在本地界面保存 QQ 邮箱授权码")
        mail = self.build_message(message, decision)
        context = ssl.create_default_context()
        if self.settings.mail_smtp_port == 465:
            # Port 465 uses TLS from connection establishment; STARTTLS is not used here.
            # 465 端口从建立连接开始即启用 TLS，因此不再发送 STARTTLS 升级命令。
            with smtplib.SMTP_SSL(
                self.settings.mail_smtp_host,
                self.settings.mail_smtp_port,
                context=context,
                timeout=20,
            ) as client:
                client.login(self.settings.mail_username, password)
                client.send_message(mail)
        else:
            # Require TLS before credentials are sent over a non-465 SMTP connection.
            # 非 465 端口必须先升级到 TLS，再传输账号凭据。
            with smtplib.SMTP(
                self.settings.mail_smtp_host,
                self.settings.mail_smtp_port,
                timeout=20,
            ) as client:
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
                client.login(self.settings.mail_username, password)
                client.send_message(mail)