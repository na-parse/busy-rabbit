'''Editor authentication.

Email-based, self-contained auth suited to internal / compliance hosting. There
are no passwords: an editor proves ownership of a configured email address by
entering a one-time code mailed to them, or by presenting a prevalidation token
minted on the CLI (an SMTP-free path).

State lives in two places:

* the signed Flask session holds the validated editor email and the moment it
  was validated, giving a 30-day rolling login without any server-side session
  table;
* short-lived codes and tokens are persisted (hashed) in SQLite via
  :class:`~br_server.db.CardStore`, so expiry, single-use-of-newest, and
  generation rate limits survive a restart.

Editors are re-checked against the live config on every request, so removing
someone from ``config.toml`` revokes their access immediately.
'''

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from flask import session

from .config import Config
from .db import CardStore

# =============================================================================
# Policy constants
# =============================================================================

CODE_TTL = timedelta(minutes=15)       # how long an emailed code is accepted
TOKEN_TTL = timedelta(hours=24)        # how long a CLI token is accepted
SESSION_TTL = timedelta(days=30)       # how long a validated session lasts
RATE_WINDOW = timedelta(hours=1)       # window for code-generation throttling
RATE_MAX = 5                           # max codes per email per RATE_WINDOW
CODE_DIGITS = 6                        # length of the numeric email code
TOKEN_BYTES = 24                       # entropy of a CLI prevalidation token

# Session keys.
EMAIL_KEY = 'editor_email'
VALIDATED_KEY = 'validated_at'
PENDING_KEY = 'pending_email'


# =============================================================================
# Errors
# =============================================================================

class AuthError(Exception):
    '''Base class for surfaced authentication problems.'''


class NotAnEditor(AuthError):
    '''The supplied email is not a configured editor.'''


class RateLimited(AuthError):
    '''Too many codes requested for an email within the rate window.'''


# =============================================================================
# Time / secret helpers
# =============================================================================

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    '''Format a UTC datetime to match :func:`board.now_iso` (sortable).'''
    return moment.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _hash(secret: str) -> str:
    '''Stable hash for at-rest storage of codes and tokens.'''
    return hashlib.sha256(secret.encode('utf-8')).hexdigest()


def _fresh(created_at: str, ttl: timedelta) -> bool:
    '''Whether a row created at ``created_at`` is still within ``ttl``.'''
    return created_at >= _iso(_now() - ttl)


def _norm(email: str) -> str:
    return (email or '').strip().lower()


# =============================================================================
# Config-derived predicates
# =============================================================================

def is_config_valid(config: Config) -> bool:
    '''A board is writable only when at least one editor is configured.'''
    return bool(config.board.editors)


# =============================================================================
# Login code flow (email)
# =============================================================================

def request_code(store: CardStore, config: Config, email: str) -> str:
    '''Issue a login code for ``email`` and return it (the caller mails it).

    Raises :class:`NotAnEditor` if the email is not authorised, or
    :class:`RateLimited` if too many codes were requested recently. Issuing a
    new code implicitly retires earlier ones (only the newest is verifiable).
    '''
    email = _norm(email)
    if not config.is_editor_email(email):
        raise NotAnEditor(email)

    since = _iso(_now() - RATE_WINDOW)
    if store.count_auth_codes_since(email, since) >= RATE_MAX:
        raise RateLimited(email)

    code = ''.join(secrets.choice('0123456789') for _ in range(CODE_DIGITS))
    store.add_auth_code(email, _hash(code), prune_before=since)
    return code


def verify_code(store: CardStore, config: Config, email: str,
                code: str) -> bool:
    '''Validate a login code; on success start a 30-day editor session.'''
    email = _norm(email)
    if not config.is_editor_email(email):
        return False
    row = store.newest_auth_code(email)
    if not row or not _fresh(row['created_at'], CODE_TTL):
        return False
    if not hmac.compare_digest(row['hash'], _hash(code or '')):
        return False
    _establish_session(email)
    return True


# =============================================================================
# Prevalidation token flow (CLI, SMTP-free)
# =============================================================================

def generate_token(store: CardStore, config: Config, email: str) -> str:
    '''Mint a CLI prevalidation token for ``email`` and return it.

    Raises :class:`NotAnEditor` if the email is not authorised. Only the newest
    token for an email is verifiable.
    '''
    email = _norm(email)
    if not config.is_editor_email(email):
        raise NotAnEditor(email)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    store.add_auth_token(
        email, _hash(token), prune_before=_iso(_now() - TOKEN_TTL)
    )
    return token


def verify_token(store: CardStore, config: Config, email: str,
                 token: str) -> bool:
    '''Validate a prevalidation token; on success start an editor session.'''
    email = _norm(email)
    if not config.is_editor_email(email):
        return False
    row = store.newest_auth_token(email)
    if not row or not _fresh(row['created_at'], TOKEN_TTL):
        return False
    if not hmac.compare_digest(row['hash'], _hash(token or '')):
        return False
    _establish_session(email)
    return True


# =============================================================================
# Session management
# =============================================================================

def _establish_session(email: str) -> None:
    '''Record a validated editor identity in the signed session.'''
    session.permanent = True
    session[EMAIL_KEY] = email
    session[VALIDATED_KEY] = _iso(_now())
    session.pop(PENDING_KEY, None)


def logout() -> None:
    '''Drop any editor identity from the session.'''
    session.pop(EMAIL_KEY, None)
    session.pop(VALIDATED_KEY, None)
    session.pop(PENDING_KEY, None)


def current_editor(config: Config) -> str | None:
    '''The logged-in editor's email if the session is still valid, else None.

    Validity requires a non-expired session *and* the email still appearing in
    the live config, so config edits revoke access on the next request.
    '''
    email = session.get(EMAIL_KEY)
    validated = session.get(VALIDATED_KEY)
    if not email or not validated:
        return None
    if not _fresh(validated, SESSION_TTL):
        return None
    if not config.is_editor_email(email):
        return None
    return email


def is_editor(config: Config) -> bool:
    '''Whether the current session holds a valid editor identity.'''
    return current_editor(config) is not None


def viewer_name(config: Config) -> str:
    '''Display name for the current viewer (owner label, or "Guest").'''
    email = current_editor(config)
    return config.owner_name_for(email) if email else 'Guest'


# =============================================================================
# Pending-login bookkeeping (between the email and verification steps)
# =============================================================================

def set_pending(email: str) -> None:
    '''Remember which email is mid-login so the verify step knows the target.'''
    session[PENDING_KEY] = _norm(email)


def pending_email() -> str | None:
    '''The email awaiting code verification, if any.'''
    return session.get(PENDING_KEY)
