'''Outbound email for login codes.

A thin wrapper over the stdlib ``smtplib`` so busy-rabbit can deliver a
one-time login code without any third-party dependency. The transport security
mode and optional authentication come from the ``[smtp]`` config section.

Failures are surfaced as :class:`SmtpError` so the login flow can tell the user
plainly that mail is not working, per the design's no-obfuscation stance.
'''

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from .config import TLS_IMPLICIT, TLS_STARTTLS, Config

# How long a code stays valid, surfaced in the email body. Kept here as plain
# text only; the authoritative TTL lives in auth.py.
_CODE_VALID_TEXT = '15 minutes'


class SmtpError(Exception):
    '''Raised when a login code could not be sent.'''


def send_login_code(config: Config, to_email: str, code: str) -> None:
    '''Email ``code`` to ``to_email``; raise :class:`SmtpError` on any failure.

    Honours the three ``[smtp].tls`` modes (starttls / implicit / none) and
    authenticates only when both username and password are configured.
    '''
    smtp = config.smtp
    if not smtp.relay:
        raise SmtpError('No SMTP relay configured.')

    message = _build_message(config, to_email, code)
    try:
        with _connect(smtp.relay, smtp.port, smtp.tls) as server:
            if smtp.tls == TLS_STARTTLS:
                server.starttls(context=ssl.create_default_context())
            if smtp.use_auth:
                server.login(smtp.username, smtp.password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise SmtpError(str(exc)) from exc


def send_test(config: Config, to_email: str) -> None:
    '''Send a one-off "SMTP works" probe; raise :class:`SmtpError` on failure.

    Used by the setup wizard to confirm mail delivery before deployment.
    '''
    smtp = config.smtp
    if not smtp.relay:
        raise SmtpError('No SMTP relay configured.')
    message = EmailMessage()
    message['Subject'] = f'{config.board.title}: SMTP test'
    message['From'] = smtp.sender()
    message['To'] = to_email
    message.set_content(
        f'This is a test message from {config.board.title}. If you received '
        f'it, SMTP delivery is working.\n'
    )
    try:
        with _connect(smtp.relay, smtp.port, smtp.tls) as server:
            if smtp.tls == TLS_STARTTLS:
                server.starttls(context=ssl.create_default_context())
            if smtp.use_auth:
                server.login(smtp.username, smtp.password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise SmtpError(str(exc)) from exc


def _connect(relay: str, port: int, tls: str) -> smtplib.SMTP:
    '''Open the right SMTP connection class for the configured TLS mode.'''
    if tls == TLS_IMPLICIT:
        return smtplib.SMTP_SSL(
            relay, port, context=ssl.create_default_context(), timeout=15
        )
    return smtplib.SMTP(relay, port, timeout=15)


def _build_message(config: Config, to_email: str, code: str) -> EmailMessage:
    '''Compose the plain-text login-code email.'''
    message = EmailMessage()
    message['Subject'] = f'{config.board.title} login code: {code}'
    message['From'] = config.smtp.sender()
    message['To'] = to_email
    message.set_content(
        f'Your {config.board.title} login code is:\n\n'
        f'    {code}\n\n'
        f'It is valid for {_CODE_VALID_TEXT}. If you did not request it, you '
        f'can ignore this email.\n'
    )
    return message
