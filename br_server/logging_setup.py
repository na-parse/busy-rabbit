'''Logging configuration.

The app maintains rotating logs under ``./logs`` (configurable). Two handlers
are attached: a rotating file handler for ``busy_rabbit.log`` and a console
handler so ``busy_rabbit serve`` still shows activity in the terminal.
'''

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import Config

LOGGER_NAME = 'busy_rabbit'
_FORMAT = '%(asctime)s %(levelname)-7s [%(name)s] %(message)s'
_configured = False


def configure_logging(config: Config) -> logging.Logger:
    '''Set up the ``busy_rabbit`` logger from config; idempotent per process.'''
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if _configured:
        return logger

    log_dir = config.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, config.logging.level.upper(), logging.INFO)
    logger.setLevel(level)
    formatter = logging.Formatter(_FORMAT)

    file_handler = RotatingFileHandler(
        log_dir / 'busy_rabbit.log',
        maxBytes=config.logging.max_bytes,
        backupCount=config.logging.backup_count,
        encoding='utf-8',
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    logger.propagate = False
    _configured = True
    return logger


def get_logger() -> logging.Logger:
    '''Return the shared app logger (configure it first via the app factory).'''
    return logging.getLogger(LOGGER_NAME)
