'''busy-rabbit command-line interface.

Implements the operator commands exposed by the ``busy_rabbit`` shim: starting
the server, initialising and inspecting the SQLite database, minting auth
tokens, checking configuration, and tailing logs. The thin shim only puts the
repo root on ``sys.path`` and calls :func:`main` here, so the actual logic
lives inside the package with normal top-of-file imports.
'''

from __future__ import annotations

import argparse
import json
import socket
import sys
from collections import Counter
from pathlib import Path

from . import auth, create_app, load_config
from .board import STATUS_LABELS, effective_status
from .certs import create_self_signed_cert, is_ssl_configured
from .config import ConfigError, validate_config
from .db import SCHEMA_VERSION, CardStore
from .logging_setup import configure_logging


# =============================================================================
# Helpers
# =============================================================================

def _load(args: argparse.Namespace):
    '''Load config from the optional ``--config`` path.'''
    return load_config(getattr(args, 'config', None))


def _store(config) -> CardStore:
    return CardStore(config.db_path)


# =============================================================================
# Commands - serve
# =============================================================================

def cmd_serve(args: argparse.Namespace) -> int:
    '''Start the Flask development server.'''
    config = _load(args)
    app = create_app(config=config)
    host = args.host or config.server.host
    port = args.port or config.server.port
    debug = config.server.debug if args.debug is None else args.debug
    logger = configure_logging(config)

    ssl_context = _prepare_ssl(config, logger)
    if ssl_context is False:
        return 1
    scheme = 'https' if ssl_context else 'http'

    logger.info('Serving on %s://%s:%d (debug=%s)', scheme, host, port, debug)
    print(f'Serving busy-rabbit on {scheme}://{host}:{port}')
    if not ssl_context:
        print('HTTP mode; run behind a reverse proxy if public HTTPS is needed.')
    # use_reloader is left off so logs/handlers are not duplicated.
    app.run(host=host, port=port, debug=debug, use_reloader=False,
            ssl_context=ssl_context)
    return 0


def _prepare_ssl(config, logger):
    '''Resolve the ssl_context for ``app.run``.

    Returns ``None`` for plain HTTP, an ``(cert, key)`` path tuple for HTTPS,
    or ``False`` on a fatal error (caller should abort). Behaviour follows
    ``[server].use_https``:
      - off: HTTP; any cert files are ignored.
      - on + no cert files: auto-generate a self-signed pair, then serve.
      - on + valid pair present: reuse it.
      - on + files present but invalid: refuse to start.
    '''
    if not config.server.use_https:
        return None

    cert = config.cert_path
    key = config.key_path
    if not (cert.exists() or key.exists()):
        logger.info('use_https on with no cert files; generating self-signed pair')
        print('use_https is enabled; generating self-signed certificate...')
        try:
            create_self_signed_cert(cert, key, socket.gethostname())
        except Exception as exc:
            logger.error('certificate generation failed: %s', exc)
            print(f'Failed to generate self-signed certificate: {exc}',
                  file=sys.stderr)
            return False
        print(f'  cert: {cert}')
        print(f'  key : {key}')

    if not is_ssl_configured(cert, key):
        logger.error('use_https on but cert files are missing or invalid')
        print('use_https is enabled but the certificate files are missing or '
              'invalid:', file=sys.stderr)
        print(f'  cert: {cert} ({"present" if cert.exists() else "missing"})',
              file=sys.stderr)
        print(f'  key : {key} ({"present" if key.exists() else "missing"})',
              file=sys.stderr)
        print('Delete both files to have a fresh pair generated, or set '
              'use_https = false to serve over HTTP.', file=sys.stderr)
        return False

    return (str(cert), str(key))


# =============================================================================
# Commands - db
# =============================================================================

def cmd_db_init(args: argparse.Namespace) -> int:
    '''Create the database schema, or migrate an existing one to the latest.'''
    config = _load(args)
    store = _store(config)
    store.init_db()
    print(f'Database at {config.db_path} ready (schema v{SCHEMA_VERSION}).')
    return 0


def cmd_db_dump(args: argparse.Namespace) -> int:
    '''Dump all cards as JSON to stdout or a file.'''
    config = _load(args)
    cards = _store(config).all_cards()
    payload = json.dumps(cards, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(payload + '\n', encoding='utf-8')
        print(f'Wrote {len(cards)} card(s) to {args.output}')
    else:
        print(payload)
    return 0


def cmd_db_list(args: argparse.Namespace) -> int:
    '''List cards in a compact table.'''
    config = _load(args)
    cards = _store(config).all_cards()
    if not cards:
        print('No cards.')
        return 0
    print(f'{"ID":<10} {"STATUS":<12} {"POS":<8} TITLE')
    print('-' * 64)
    for card in cards:
        eff = effective_status(card, archive_after_days=config.board.archive_after_days)
        title = card['title'][:40]
        print(f'{card["id"][:8]:<10} {eff:<12} {card["position"][:8]:<8} {title}')
    print(f'\n{len(cards)} card(s).')
    return 0


def cmd_db_stats(args: argparse.Namespace) -> int:
    '''Show card counts grouped by effective status.'''
    config = _load(args)
    cards = _store(config).all_cards()
    counts = Counter(
        effective_status(c, archive_after_days=config.board.archive_after_days)
        for c in cards
    )
    print(f'Database: {config.db_path}')
    print(f'Total cards: {len(cards)}\n')
    for status, label in STATUS_LABELS.items():
        print(f'  {label:<20} {counts.get(status, 0)}')
    return 0


# =============================================================================
# Commands - auth
# =============================================================================

def cmd_auth_token(args: argparse.Namespace) -> int:
    '''Mint a CLI prevalidation token for a configured editor email.'''
    config = _load(args)
    store = _store(config)
    store.init_db()  # ensure schema exists / is migrated to the current version
    try:
        token = auth.generate_token(store, config, args.email)
    except auth.NotAnEditor:
        print(f'Error: {args.email} is not a configured editor.')
        return 1
    print(f'token: {token}')
    print('\nValid for 24h. Sign in at /login/token with this email + token.')
    return 0


# =============================================================================
# Commands - config / logs
# =============================================================================

def cmd_config_setup(args: argparse.Namespace) -> int:
    '''Run the interactive guided setup wizard.'''
    from .wizard import run_setup
    return run_setup(getattr(args, 'config', None))


def cmd_config_show(args: argparse.Namespace) -> int:
    '''Print the resolved configuration and report any validation errors.'''
    config = _load(args)
    editors = config.board.editors
    smtp = config.smtp
    print(f'Source:        {config.source_path}')
    print(f'Server:        {config.server.host}:{config.server.port} '
          f'(debug={config.server.debug})')
    print(f'Mode:          {config.server.mode}')
    print(f'HTTPS:         {"on (self-signed)" if config.server.use_https else "off"}')
    print(f'Database:      {config.db_path}')
    print(f'Log dir:       {config.log_dir} (level={config.logging.level})')
    print(f'Board title:   {config.board.title}')
    print(f'App owner:     {config.board.app_owner or "(none)"}')
    print(f'Statement:     {config.board.statement or "(none)"}')
    print(f'Archive after: {config.board.archive_after_days} days')
    print(f'SMTP:          {smtp.relay or "(none)"}:{smtp.port} '
          f'(tls={smtp.tls}, auth={smtp.use_auth})')
    print(f'Editors ({len(editors)}):')
    for editor in editors:
        print(f'  - {editor.email} ({editor.owner_name})')
    try:
        validate_config(config)
        print('Config valid:  True')
    except ConfigError as exc:
        print('Config valid:  False')
        print(f'\nERROR: {exc}')
    return 0


def cmd_logs_tail(args: argparse.Namespace) -> int:
    '''Print the tail of the main log file.'''
    config = _load(args)
    log_file = config.log_dir / 'busy_rabbit.log'
    if not log_file.exists():
        print(f'No log file yet at {log_file}')
        return 0
    lines = log_file.read_text(encoding='utf-8').splitlines()
    for line in lines[-args.lines:]:
        print(line)
    return 0


# =============================================================================
# Argument parser
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    '''Construct the full CLI argument parser.'''
    parser = argparse.ArgumentParser(
        prog='busy_rabbit', description='busy-rabbit control shim.'
    )
    parser.add_argument(
        '--config', metavar='PATH',
        help='Path to config.toml (default: repo-root config.toml).'
    )
    sub = parser.add_subparsers(dest='command', required=True)

    # serve
    p_serve = sub.add_parser('serve', help='Start the web server.')
    p_serve.add_argument('--host', help='Override bind host.')
    p_serve.add_argument('--port', type=int, help='Override bind port.')
    p_serve.add_argument(
        '--debug', action=argparse.BooleanOptionalAction, default=None,
        help='Override debug mode.'
    )
    p_serve.set_defaults(func=cmd_serve)

    # db
    p_db = sub.add_parser('db', help='Database operations.')
    db_sub = p_db.add_subparsers(dest='db_command', required=True)

    db_sub.add_parser('init', help='Create schema.').set_defaults(func=cmd_db_init)

    p_dump = db_sub.add_parser('dump', help='Dump cards as JSON.')
    p_dump.add_argument('-o', '--output', help='Write to file instead of stdout.')
    p_dump.set_defaults(func=cmd_db_dump)

    db_sub.add_parser('list', help='List cards.').set_defaults(func=cmd_db_list)
    db_sub.add_parser('stats', help='Card counts by status.').set_defaults(
        func=cmd_db_stats
    )

    # auth
    p_auth = sub.add_parser('auth', help='Authentication operations.')
    auth_sub = p_auth.add_subparsers(dest='auth_command', required=True)
    p_token = auth_sub.add_parser(
        'token', help='Mint a 24h prevalidation token for an editor email.'
    )
    p_token.add_argument('email', help='Configured editor email address.')
    p_token.set_defaults(func=cmd_auth_token)

    # config
    p_config = sub.add_parser('config', help='Configuration operations.')
    config_sub = p_config.add_subparsers(dest='config_command', required=True)
    config_sub.add_parser(
        'setup', help='Interactive guided configuration wizard.'
    ).set_defaults(func=cmd_config_setup)
    config_sub.add_parser('show', help='Show resolved config.').set_defaults(
        func=cmd_config_show
    )

    # logs
    p_logs = sub.add_parser('logs', help='Log operations.')
    logs_sub = p_logs.add_subparsers(dest='logs_command', required=True)
    p_tail = logs_sub.add_parser('tail', help='Tail the main log.')
    p_tail.add_argument('-n', '--lines', type=int, default=40,
                        help='Number of lines (default: 40).')
    p_tail.set_defaults(func=cmd_logs_tail)

    return parser


def main(argv: list[str] | None = None) -> int:
    '''Parse ``argv`` and dispatch to the selected command.'''
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        # Clean exit on Ctrl+C anywhere in the CLI (interactive prompts, the
        # server loop, etc.) without dumping a traceback. 130 = SIGINT.
        print('\nAborted.', file=sys.stderr)
        return 130
