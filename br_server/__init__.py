'''busy-rabbit server package.

A self-contained Flask + SQLite re-implementation of the original Lakebed
``rabbit-trails`` Kanban board. The public entry point is :func:`create_app`,
used by both the CLI (``busy_rabbit serve``) and any WSGI host.
'''

from __future__ import annotations

from pathlib import Path

from flask import Flask

from .auth import SESSION_TTL
from .config import (
    Config,
    config_has_secret,
    load_config,
    load_or_create_secret,
    validate_config,
)
from .db import CardStore
from .logging_setup import configure_logging

__all__ = ['create_app', 'load_config', 'Config', 'CardStore']


def create_app(
    config: Config | None = None,
    config_path: Path | str | None = None,
) -> Flask:
    '''Build and configure the Flask application.

    Args:
        config: A pre-loaded :class:`Config`. Takes precedence over
            ``config_path``.
        config_path: Path to a ``config.toml`` to load when ``config`` is not
            given. Falls back to the repo-default location.
    '''
    if config is None:
        config = load_config(config_path)

    logger = configure_logging(config)

    # Fail fast and loudly on misconfiguration rather than serving a broken
    # auth flow (bad emails, inconsistent SMTP, unknown mode, no editors).
    validate_config(config)

    app = Flask(__name__)
    # Session secret is auto-managed in a file beside the DB, not configured.
    secret_existed = config.secret_path.exists()
    app.secret_key = load_or_create_secret(config.secret_path)
    app.permanent_session_lifetime = SESSION_TTL
    if not secret_existed:
        logger.info('Generated a new session secret at %s', config.secret_path)
    if config_has_secret(config.source_path):
        logger.warning(
            'Obsolete [security] secret_key found in %s; it is ignored and '
            'safe to delete. The session secret is managed at %s.',
            config.source_path, config.secret_path,
        )

    store = CardStore(config.db_path)
    store.init_db()

    # Stash shared singletons where the blueprint can reach them.
    app.extensions['busy_rabbit'] = {'config': config, 'store': store}

    from .routes import bp

    app.register_blueprint(bp)

    logger.info(
        'busy-rabbit ready (db=%s, mode=%s, editors=%d)',
        config.db_path,
        config.server.mode,
        len(config.board.editors),
    )
    return app
