'''Guided configuration wizard.

An interactive setup flow (``busy_rabbit config setup``) so an app owner can
pre-configure a deployment by answering prompts instead of hand-editing
``config.toml``. It loads any existing config as the defaults - so it doubles as
an editor - validates each answer inline, writes a fully-commented file (backing
up the previous one), and offers a live SMTP test.

The TOML writer here is intentionally small and schema-specific: ``tomllib`` is
read-only, and our config shape is fixed, so a tailored serializer is less debt
than taking on a third-party writer dependency.
'''

from __future__ import annotations

import getpass
import secrets
import shutil
import sys
from pathlib import Path

from .config import (
    DEFAULT_CONFIG_PATH,
    MODES,
    BoardConfig,
    Config,
    ConfigError,
    DatabaseConfig,
    Editor,
    LoggingConfig,
    SecurityConfig,
    ServerConfig,
    SmtpConfig,
    is_valid_email,
    load_config,
    validate_config,
)
from .email_send import SmtpError, send_test

# Placeholder secrets from the shipped template / dataclass defaults; if the
# stored key is one of these, the wizard pushes the owner to generate a real one.
_PLACEHOLDER_SECRETS = {
    'change-me-please',
    'change-me-to-a-long-random-string',
    '',
}


# =============================================================================
# Prompt helpers
# =============================================================================

def _ask(prompt: str, default: str = '',
         validate=None) -> str:
    '''Prompt for a string, showing ``default`` and re-asking on validation.'''
    while True:
        suffix = f' [{default}]' if default else ''
        value = input(f'{prompt}{suffix}: ').strip() or default
        if validate is not None:
            error = validate(value)
            if error:
                print(f'  ! {error}')
                continue
        return value


def _ask_int(prompt: str, default: int) -> int:
    '''Prompt for a whole number, defaulting and re-asking on bad input.'''
    while True:
        raw = input(f'{prompt} [{default}]: ').strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print('  ! Enter a whole number.')


def _ask_bool(prompt: str, default: bool = False) -> bool:
    '''Prompt for a yes/no answer.'''
    hint = 'Y/n' if default else 'y/N'
    while True:
        raw = input(f'{prompt} [{hint}]: ').strip().lower()
        if not raw:
            return default
        if raw in ('y', 'yes'):
            return True
        if raw in ('n', 'no'):
            return False
        print('  ! Please answer y or n.')


def _ask_choice(prompt: str, options: tuple[str, ...], default: str) -> str:
    '''Prompt for one of a fixed set of options.'''
    while True:
        raw = input(
            f'{prompt} ({"/".join(options)}) [{default}]: '
        ).strip().lower()
        value = raw or default
        if value in options:
            return value
        print(f'  ! Choose one of: {", ".join(options)}.')


def _email_error(value: str) -> str | None:
    return None if is_valid_email(value) else 'Not a valid email address.'


def _heading(text: str) -> None:
    print(f'\n=== {text} ===')


# =============================================================================
# Section prompts
# =============================================================================

def _prompt_server(existing: ServerConfig) -> ServerConfig:
    host = _ask('Bind host (0.0.0.0 to expose on the LAN)', existing.host)
    port = _ask_int('Port', existing.port)
    mode = _ask_choice(
        'Access mode (public = open viewing, closed = login required)',
        MODES, existing.mode,
    )
    use_https = _ask_bool(
        'Serve over HTTPS with a self-signed certificate '
        '(no for reverse-proxy setups)',
        existing.use_https,
    )
    return ServerConfig(
        host=host, port=port, debug=existing.debug, mode=mode,
        use_https=use_https,
    )


def _prompt_security(existing: SecurityConfig) -> SecurityConfig:
    print('\nSecurity key (signs session cookies):')
    needs_key = existing.secret_key in _PLACEHOLDER_SECRETS
    if needs_key:
        print('No real secret_key set; generating a strong one.')
        generate = True
    else:
        generate = _ask_bool('Replace the existing secret_key?', False)
    secret = secrets.token_urlsafe(48) if generate else existing.secret_key
    return SecurityConfig(secret_key=secret)


# SMTP transport options, in menu order: TLS mode, human label, default port.
# Auth is mandatory for the encrypted modes and skipped for plain.
_TLS_MENU: tuple[tuple[str, str, int], ...] = (
    ('none', 'Plain (no encryption)', 25),
    ('starttls', 'STARTTLS (upgrade to TLS)', 587),
    ('implicit', 'Implicit TLS / SMTPS', 465),
)


def _prompt_transport(existing_tls: str) -> tuple[str, int]:
    '''Pick a transport from a numbered menu; return (tls mode, default port).'''
    default_index = next(
        (i for i, opt in enumerate(_TLS_MENU) if opt[0] == existing_tls), 1
    )
    print('Transport security:')
    for i, (_, label, port) in enumerate(_TLS_MENU, start=1):
        print(f'  {i}. {label} (default port {port})')
    choices = tuple(str(i) for i in range(1, len(_TLS_MENU) + 1))
    pick = _ask_choice('Select', choices, str(default_index + 1))
    tls, _, port = _TLS_MENU[int(pick) - 1]
    return tls, port


def _prompt_password(existing: str) -> str:
    '''Prompt (hidden) for a required SMTP password, allowing keep-on-blank.'''
    while True:
        entered = getpass.getpass('SMTP password (hidden): ')
        if entered:
            return entered
        if existing:
            print('  (keeping existing password)')
            return existing
        print('  ! Password is required for this transport.')


def _prompt_smtp(existing: SmtpConfig) -> SmtpConfig:
    if not _ask_bool(
        'Configure SMTP now? (needed for the email login flow)',
        bool(existing.relay),
    ):
        print('  Skipping SMTP. Email login will be unavailable; CLI '
              'prevalidation tokens still work.')
        return SmtpConfig()
    relay = _ask('SMTP relay host', existing.relay)
    tls, default_port = _prompt_transport(existing.tls)
    port = _ask_int('SMTP port', default_port)
    # Plain SMTP carries no auth; the encrypted transports require it.
    if tls == 'none':
        print('  Plain transport: sending without authentication.')
        username = password = ''
    else:
        username = _ask('SMTP username',
                        validate=lambda v: None if v else 'Username required.')
        password = _prompt_password(existing.password)
    from_addr = _ask('From address (blank = auto)', existing.from_addr)
    return SmtpConfig(
        relay=relay, port=port, tls=tls,
        username=username, password=password, from_addr=from_addr,
    )


def _prompt_editors(existing: list[Editor]) -> list[Editor]:
    print('\nEditors (authorised by email):')
    editors: list[Editor] = []
    if existing:
        print('Current editors:')
        for editor in existing:
            label = f' ({editor.nickname})' if editor.nickname else ''
            print(f'  - {editor.email}{label}')
        if _ask_bool('Keep these editors?', True):
            editors = list(existing)
    while not editors or _ask_bool('Add another editor?', False):
        email = _ask('Editor email', validate=_email_error)
        nickname = _ask('Nickname (optional, blank = use email name)')
        editors.append(Editor(email=email, nickname=nickname))
    return editors


def _prompt_board(existing: BoardConfig) -> BoardConfig:
    title = _ask('Board title', existing.title)
    app_owner = _ask('App owner (who runs this; optional)', existing.app_owner)
    statement = _ask('Tagline (optional)', existing.statement)
    archive = _ask_int('Archive Done cards after N days',
                       existing.archive_after_days)
    editors = _prompt_editors(existing.editors)
    return BoardConfig(
        title=title, archive_after_days=archive,
        app_owner=app_owner, statement=statement, editors=editors,
    )


# =============================================================================
# TOML serialisation
# =============================================================================

def _q(value: str) -> str:
    '''Render a string as a TOML basic string (escaping \\ and ").'''
    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def dump_toml(config: Config) -> str:
    '''Serialise a :class:`Config` to a fully-commented ``config.toml`` body.'''
    s, b, m, sm, d, lg = (
        config.server, config.board, config.smtp,
        config.security, config.database, config.logging,
    )
    lines: list[str] = [
        '# busy-rabbit configuration',
        '# Generated by `busy_rabbit config setup`. Safe to hand-edit.',
        '# Relative paths resolve against the repository root.',
        '',
        '[server]',
        f'host = {_q(s.host)}   # bind address; "0.0.0.0" exposes on the LAN',
        f'port = {s.port}',
        f'debug = {str(s.debug).lower()}',
        '# Access mode: "public" (open viewing) or "closed" (login required).',
        f'mode = {_q(s.mode)}',
        '# Serve directly over HTTPS with a self-signed certificate, generated',
        '# beside the database on first start. Leave false to use a reverse',
        '# proxy for trusted/public HTTPS.',
        f'use_https = {str(s.use_https).lower()}',
        '',
        '[security]',
        '# Signs session cookies; sessions last 30 days. Keep this secret.',
        f'secret_key = {_q(sm.secret_key)}',
        '',
        '[smtp]',
        '# Delivers one-time login codes. Leave relay empty to disable email',
        '# login (CLI prevalidation tokens still work).',
        f'relay = {_q(m.relay)}',
        f'port = {m.port}',
        '# Transport security: "starttls", "implicit" (SMTPS), or "none".',
        f'tls = {_q(m.tls)}',
        '# Auth is used only when BOTH username and password are set.',
        f'username = {_q(m.username)}',
        f'password = {_q(m.password)}',
        '# Envelope From; blank = username (if an address) or busy-rabbit@relay.',
        f'from_addr = {_q(m.from_addr)}',
        '',
        '[board]',
        f'title = {_q(b.title)}',
        '# Optional title-bar branding.',
        f'app_owner = {_q(b.app_owner)}',
        f'statement = {_q(b.statement)}',
        f'archive_after_days = {b.archive_after_days}',
        '',
        '# Authorised editors. Each needs a valid email; nickname is optional',
        '# (used as the card owner label, else the email local part).',
    ]
    for editor in b.editors:
        lines.append('[[board.editors]]')
        lines.append(f'email = {_q(editor.email)}')
        if editor.nickname:
            lines.append(f'nickname = {_q(editor.nickname)}')
        lines.append('')
    lines += [
        '[database]',
        f'path = {_q(d.path)}',
        '',
        '[logging]',
        f'level = {_q(lg.level)}   # DEBUG, INFO, WARNING, ERROR',
        f'dir = {_q(lg.dir)}',
        f'max_bytes = {lg.max_bytes}',
        f'backup_count = {lg.backup_count}',
        '',
    ]
    return '\n'.join(lines)


# =============================================================================
# Orchestration
# =============================================================================

# The setup flow is organised into three master sections. When editing a live
# config each section first prints its current settings, then asks whether to
# update them - so an operator can skip straight to the part they care about.

def _show_server(server: ServerConfig, security: SecurityConfig) -> None:
    key = 'NOT set' if security.secret_key in _PLACEHOLDER_SECRETS else 'set'
    print(f'  Bind:        {server.host}:{server.port}')
    print(f'  Access mode: {server.mode}')
    print(f'  HTTPS:       {"on (self-signed)" if server.use_https else "off"}')
    print(f'  Secret key:  {key}')


def _show_mailer(smtp: SmtpConfig) -> None:
    if not smtp.relay:
        print('  Email login disabled (no relay set).')
        return
    print(f'  Relay:       {smtp.relay}:{smtp.port}')
    print(f'  Transport:   {smtp.tls}')
    print(f'  Auth:        {smtp.username or "(none)"}')
    print(f'  From:        {smtp.from_addr or "(auto)"}')


def _show_board(board: BoardConfig) -> None:
    print(f'  Title:       {board.title}')
    print(f'  App owner:   {board.app_owner or "(none)"}')
    print(f'  Tagline:     {board.statement or "(none)"}')
    print(f'  Archive after: {board.archive_after_days} days')
    print(f'  Editors ({len(board.editors)}):')
    for editor in board.editors:
        label = f' ({editor.nickname})' if editor.nickname else ''
        print(f'    - {editor.email}{label}')


def _enter_section(name: str, editing: bool, show) -> bool:
    '''Print a section heading; when editing, show the summary and ask to update.

    Returns ``True`` if the section's prompts should run, ``False`` to keep the
    existing settings untouched. A fresh config (``editing`` False) always runs.
    '''
    _heading(name)
    if not editing:
        return True
    show()
    return _ask_bool('Update these settings?', False)


def _summary(config: Config) -> None:
    _heading('Review')
    s, m, b = config.server, config.smtp, config.board
    print(f'Mode:        {s.mode}  (host {s.host}:{s.port}, '
          f'{"https" if s.use_https else "http"})')
    print(f'SMTP:        {m.relay or "(disabled)"}:{m.port} '
          f'(tls={m.tls}, auth={m.use_auth})')
    print(f'Board:       {b.title}')
    print(f'Editors ({len(b.editors)}):')
    for editor in b.editors:
        print(f'  - {editor.email} ({editor.owner_name})')


def _write(config: Config, target: Path) -> None:
    '''Back up an existing config and write the new one.'''
    if target.exists():
        backup = target.with_name(target.name + '.bak')
        shutil.copy2(target, backup)
        print(f'Backed up existing config to {backup}')
    target.write_text(dump_toml(config), encoding='utf-8')
    print(f'Wrote {target}')


def _maybe_test_smtp(config: Config) -> None:
    '''Offer to send a live SMTP probe to one editor.'''
    if not config.smtp.relay or not config.board.editors:
        return
    to = config.board.editors[0].email
    if not _ask_bool(f'Send a test email to {to} now?', False):
        return
    try:
        send_test(config, to)
        print(f'  Sent. Check {to} to confirm delivery.')
    except SmtpError as exc:
        print(f'  ! SMTP error: {exc}')
        print('  Fix [smtp] settings and re-run `busy_rabbit config setup`.')


def run_setup(config_path: str | Path | None = None) -> int:
    '''Drive the interactive setup flow; return a process exit code.'''
    if not sys.stdin.isatty():
        print('config setup needs an interactive terminal.')
        return 1

    target = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    existing = load_config(target)

    editing = target.exists()
    print('busy-rabbit guided setup')
    print('Press Enter to accept the [default] shown for each prompt.')
    if editing:
        print(f'Editing existing config at {target}.')
        print('For each section, review the summary and choose what to update.')

    # Server Settings: bind address, port, access mode, security key.
    if _enter_section('Server Settings', editing,
                      lambda: _show_server(existing.server, existing.security)):
        server = _prompt_server(existing.server)
        security = _prompt_security(existing.security)
    else:
        server, security = existing.server, existing.security

    # Mailer: SMTP delivery for emailed login codes.
    if _enter_section('Mailer', editing, lambda: _show_mailer(existing.smtp)):
        smtp = _prompt_smtp(existing.smtp)
    else:
        smtp = existing.smtp

    # Board: title, app owner, tagline, archive ageing, editors.
    if _enter_section('Board', editing, lambda: _show_board(existing.board)):
        board = _prompt_board(existing.board)
    else:
        board = existing.board

    config = Config(
        server=server,
        security=security,
        smtp=smtp,
        board=board,
        database=DatabaseConfig(path=existing.database.path),
        logging=existing.logging,
        source_path=target,
    )

    _summary(config)
    try:
        validate_config(config)
    except ConfigError as exc:
        # Inline validation should prevent this; guard anyway.
        print(f'\n! Configuration is invalid: {exc}')
        print('Nothing was written.')
        return 1

    if not _ask_bool('\nWrite this configuration?', True):
        print('Cancelled; nothing was written.')
        return 1
    _write(config, target)
    _maybe_test_smtp(config)
    print('\nSetup complete.')
    return 0
