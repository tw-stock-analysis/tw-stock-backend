import asyncio
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import BACKEND_ROOT, Settings
from app.core.passwords import PasswordService
from app.services.accounts import AccountService
from app.services.credentials import IssuedCredential
from app.services.errors import ServiceError

logger = logging.getLogger(__name__)


class RegistrationService:
    def __init__(self, accounts: AccountService, passwords: PasswordService, settings: Settings):
        self.accounts = accounts
        self.passwords = passwords
        self.settings = settings
        self.delivery = asyncio.Semaphore(4)

    def require_delivery(self) -> None:
        if not self.settings.auth_mail_outbox and not (
            self.settings.auth_smtp_host and self.settings.auth_smtp_from
        ):
            raise ServiceError("REGISTRATION_UNAVAILABLE", "目前未開放帳號註冊。", 503)
        if self.delivery.locked():
            raise ServiceError("AUTH_CAPACITY_EXCEEDED", "帳號服務忙碌中，請稍後再試。", 503)

    async def send_code(self, email: str) -> None:
        if self.delivery.locked():
            return
        try:
            async with self.delivery:
                credential = await self.accounts.registration_code(email)
                if credential is not None:
                    await asyncio.to_thread(self._deliver, email, credential)
        except Exception as error:
            # SMTP errors can contain recipient addresses. Never log message contents or tokens.
            logger.warning("Registration delivery failed (%s)", type(error).__name__)

    def _deliver(self, recipient: str, credential: IssuedCredential) -> None:
        settings = self.settings
        message = EmailMessage()
        message["From"] = settings.auth_smtp_from or "registration@localhost"
        message["To"] = recipient
        message["Subject"] = "台股研究室：帳號註冊驗證碼"
        message.set_content(
            "請將以下驗證碼貼到台股研究室的註冊頁，再設定密碼。驗證碼 30 分鐘內有效。\n\n"
            f"{credential.value}\n\n若不是你提出的申請，請忽略這封信。\n",
            cte="quoted-printable",
        )
        if settings.auth_mail_outbox:
            outbox = BACKEND_ROOT / ".auth-mail"
            outbox.mkdir(mode=0o700, exist_ok=True)
            if outbox.is_symlink() or outbox.stat().st_mode & 0o077:
                raise PermissionError("The development mail directory must be private")
            fd = os.open(
                outbox / f"{credential.id}.eml", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(fd, "wb") as file:
                file.write(message.as_bytes())
            return
        with smtplib.SMTP_SSL(
            settings.auth_smtp_host,
            settings.auth_smtp_port,
            timeout=5,
            context=ssl.create_default_context(),
        ) as smtp:
            if settings.auth_smtp_user:
                smtp.login(settings.auth_smtp_user, settings.auth_smtp_password.get_secret_value())
            smtp.send_message(message)

    async def verify(self, token: str, password: str, display_name: str | None) -> None:
        password_hash = await self.passwords.hash(password)
        await self.accounts.verify_email(
            token, password_hash=password_hash, display_name=display_name
        )
