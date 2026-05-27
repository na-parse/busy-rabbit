'''Configuration loading.

Replaces the original ``.env.lakebed.server`` with a repository-local
``config.toml``. The file is read once with the stdlib ``tomllib`` parser and
wrapped in a small typed accessor so the rest of the app never touches raw
dicts or environment variables.
'''

from __future__ import annotations

import os
import re
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Repository root: the directory that contains this package's parent.
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / 'config.toml'

# Deployment access modes (see [server].mode).
MODE_PUBLIC = 'public'
MODE_CLOSED = 'closed'
MODES = (MODE_PUBLIC, MODE_CLOSED)

# SMTP transport security modes (see [smtp].tls).
TLS_STARTTLS = 'starttls'
TLS_IMPLICIT = 'implicit'
TLS_NONE = 'none'
TLS_MODES = (TLS_STARTTLS, TLS_IMPLICIT, TLS_NONE)

# Deliberately loose: enough to catch obvious typos, not RFC-5322 complete.
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def is_valid_email(value: str) -> bool:
    '''Whether ``value`` passes the basic editor-email format check.'''
    return bool(_EMAIL_RE.match(value or ''))


class ConfigError(Exception):
    '''Raised when ``config.toml`` is structurally valid but semantically bad.

    The app refuses to start rather than running in a confusing half-state,
    matching the design goal of surfacing problems quickly.
    '''


# =============================================================================
# Typed config sections
# =============================================================================

@dataclass(frozen=True)
class ServerConfig:
    host: str = '127.0.0.1'
    port: int = 8000
    debug: bool = False
    # Access mode: 'public' lets unauthenticated viewers read the board;
    # 'closed' forces them into the login flow before any board access.
    mode: str = MODE_PUBLIC
    # Serve directly over HTTPS using a self-signed certificate (auto-generated
    # alongside the database on first start). Off by default; for trusted public
    # HTTPS, leave this off and run behind a reverse proxy instead.
    use_https: bool = False


@dataclass(frozen=True)
class SmtpConfig:
    '''Outbound mail settings used to deliver login codes.'''

    relay: str = ''
    port: int = 587
    # Transport security: 'starttls' (upgrade on the wire), 'implicit'
    # (SMTPS / TLS from connect), or 'none' (plain SMTP).
    tls: str = TLS_STARTTLS
    # Auth is used iff both username and password are set; supplying only one
    # is a configuration error.
    username: str = ''
    password: str = ''
    # Envelope From address. Falls back to username (if it looks like an
    # address) or ``busy-rabbit@<relay>`` when blank.
    from_addr: str = ''

    @property
    def use_auth(self) -> bool:
        '''Whether SMTP auth credentials are configured.'''
        return bool(self.username) and bool(self.password)

    def sender(self) -> str:
        '''The From address to stamp on outbound mail.'''
        if self.from_addr:
            return self.from_addr
        if _EMAIL_RE.match(self.username or ''):
            return self.username
        return f'busy-rabbit@{self.relay or "localhost"}'


@dataclass(frozen=True)
class Editor:
    '''A single authorised editor: an email plus an optional display name.'''

    email: str
    nickname: str = ''

    @property
    def owner_name(self) -> str:
        '''Card-owner label: the nickname, else the email's local part.'''
        if self.nickname.strip():
            return self.nickname.strip()
        return self.email.split('@', 1)[0]


@dataclass(frozen=True)
class BoardConfig:
    title: str = 'busy-rabbit'
    archive_after_days: int = 14
    # Per-deployment branding shown in the title bar. app_owner labels who runs
    # this instance (e.g. 'Storage Infrastructure'); statement is a short
    # tagline (e.g. 'keeping all the bits in the right buckets'). Both optional.
    app_owner: str = ''
    statement: str = ''
    # Authorised editors, given as [[board.editors]] tables (email + optional
    # nickname). Count drives owner-display + config validity.
    editors: list[Editor] = field(default_factory=list)


@dataclass(frozen=True)
class DatabaseConfig:
    path: str = 'data/busy_rabbit.db'


@dataclass(frozen=True)
class LoggingConfig:
    level: str = 'INFO'
    dir: str = 'logs'
    max_bytes: int = 1_000_000
    backup_count: int = 5


@dataclass(frozen=True)
class Config:
    '''Top-level configuration, with paths resolved against the repo root.'''

    server: ServerConfig
    smtp: SmtpConfig
    board: BoardConfig
    database: DatabaseConfig
    logging: LoggingConfig
    source_path: Path

    # -------------------------------------------------------------------------
    # Resolved filesystem paths
    # -------------------------------------------------------------------------

    @property
    def db_path(self) -> Path:
        return _resolve(self.database.path)

    @property
    def log_dir(self) -> Path:
        return _resolve(self.logging.dir)

    # TLS material lives beside the database; the location is fixed (not
    # configurable) and only used when server.use_https is enabled.
    @property
    def cert_path(self) -> Path:
        return self.db_path.parent / 'busy-rabbit-cert.pem'

    @property
    def key_path(self) -> Path:
        return self.db_path.parent / 'busy-rabbit-key.pem'

    # Session-signing secret lives beside the database (location fixed, not
    # configurable) and is auto-generated on first start — see
    # :func:`load_or_create_secret`. Operators never set or see it.
    @property
    def secret_path(self) -> Path:
        return self.db_path.parent / 'server.secret'

    # -------------------------------------------------------------------------
    # Editor accessors
    # -------------------------------------------------------------------------

    @property
    def is_closed(self) -> bool:
        '''Whether unauthenticated viewers are locked out of the board.'''
        return self.server.mode == MODE_CLOSED

    @property
    def editor_emails(self) -> list[str]:
        '''Lower-cased editor emails, used for membership checks.'''
        return [e.email.strip().lower() for e in self.board.editors]

    def find_editor(self, email: str) -> Editor | None:
        '''Return the configured editor matching ``email`` (case-insensitive).'''
        wanted = (email or '').strip().lower()
        for editor in self.board.editors:
            if editor.email.strip().lower() == wanted:
                return editor
        return None

    def is_editor_email(self, email: str) -> bool:
        '''Whether ``email`` belongs to a configured editor.'''
        return self.find_editor(email) is not None

    def owner_name_for(self, email: str) -> str:
        '''Card-owner label for an editor email (nickname or local part).'''
        editor = self.find_editor(email)
        return editor.owner_name if editor else (email or '').split('@', 1)[0]


# =============================================================================
# Loading
# =============================================================================

def _resolve(value: str) -> Path:
    '''Resolve a possibly-relative config path against the repo root.'''
    path = Path(value)
    return path if path.is_absolute() else (REPO_ROOT / path)


def load_config(path: Path | str | None = None) -> Config:
    '''Read ``config.toml`` and return a fully-populated :class:`Config`.

    Falls back to dataclass defaults for any missing key, so a partial file
    still yields a usable configuration.
    '''
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    data: dict = {}
    if config_path.exists():
        with config_path.open('rb') as handle:
            data = tomllib.load(handle)

    board_fields = _section(data, 'board', BoardConfig)
    board_fields['editors'] = _editors(data)

    return Config(
        server=ServerConfig(**_section(data, 'server', ServerConfig)),
        smtp=SmtpConfig(**_section(data, 'smtp', SmtpConfig)),
        board=BoardConfig(**board_fields),
        database=DatabaseConfig(**_section(data, 'database', DatabaseConfig)),
        logging=LoggingConfig(**_section(data, 'logging', LoggingConfig)),
        source_path=config_path,
    )


def _section(data: dict, name: str, cls: type) -> dict:
    '''Pick only the known fields for ``cls`` from the ``name`` table.'''
    raw = data.get(name, {}) or {}
    allowed = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
    return {key: value for key, value in raw.items() if key in allowed}


def _editors(data: dict) -> list[Editor]:
    '''Build :class:`Editor` records from the ``[[board.editors]]`` tables.'''
    raw = (data.get('board', {}) or {}).get('editors', []) or []
    editors: list[Editor] = []
    for item in raw:
        if isinstance(item, dict):
            editors.append(
                Editor(
                    email=str(item.get('email', '')).strip(),
                    nickname=str(item.get('nickname', '')).strip(),
                )
            )
    return editors


def load_or_create_secret(path: Path) -> str:
    '''Return the persistent session-signing secret, creating it on first use.

    The secret signs Flask session cookies and must stay stable across restarts
    (a changed secret invalidates every login). It lives in its own file rather
    than ``config.toml`` so it is never something an operator has to choose,
    paste, or protect by hand. A fresh install generates a strong random value;
    later starts reuse it. The file is created owner-readable only (mode 0600).
    '''
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Exclusive create wins the common case and any startup race cleanly.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        existing = path.read_text(encoding='utf-8').strip()
        if existing:
            return existing
        # Empty/corrupt (e.g. an interrupted first write): overwrite it.
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC, 0o600)
    secret = secrets.token_urlsafe(48)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(secret)
    return secret


def config_has_secret(path: Path | str | None) -> bool:
    '''Whether ``config.toml`` still carries a now-ignored ``secret_key``.

    Used to nudge operators of older deployments to delete the obsolete
    ``[security] secret_key`` setting. Returns ``False`` for a missing or
    unparseable file rather than raising.
    '''
    if not path or not Path(path).exists():
        return False
    try:
        with Path(path).open('rb') as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return bool((data.get('security') or {}).get('secret_key'))


# =============================================================================
# Validation - run at startup so misconfiguration fails fast and loudly.
# =============================================================================

def validate_config(config: Config) -> None:
    '''Raise :class:`ConfigError` if the configuration cannot be served.

    Checks: a valid access mode, at least one editor, well-formed editor
    emails, a sane SMTP TLS mode, and consistent SMTP credentials.
    '''
    if config.server.mode not in MODES:
        raise ConfigError(
            f'[server] mode must be one of {MODES!r}, '
            f'got {config.server.mode!r}.'
        )

    if not config.board.editors:
        raise ConfigError(
            'No editors configured: add at least one [[board.editors]] '
            'with an email.'
        )
    for editor in config.board.editors:
        if not _EMAIL_RE.match(editor.email):
            raise ConfigError(
                f'Editor email is not a valid address: {editor.email!r}.'
            )

    if config.smtp.tls not in TLS_MODES:
        raise ConfigError(
            f'[smtp] tls must be one of {TLS_MODES!r}, '
            f'got {config.smtp.tls!r}.'
        )
    has_user = bool(config.smtp.username)
    has_pass = bool(config.smtp.password)
    if has_user != has_pass:
        raise ConfigError(
            '[smtp] username and password must be set together (or both '
            'left empty to disable SMTP auth).'
        )
