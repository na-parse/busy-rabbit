'''Shared board model.

Pure board logic ported from the original ``shared/board.ts``. No Flask, no
sqlite, no I/O here - just the rules for statuses, archive ageing, ordering,
text cleaning, and the editor-configuration semantics. Kept dependency-free so
it can be unit tested and reused from the CLI as easily as from the web app.
'''

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

# =============================================================================
# Statuses - the four working columns plus the derived 'archived' state.
# =============================================================================

STATUSES: tuple[str, ...] = ('deferred', 'todo', 'in_progress', 'scheduled', 'done')

# 'archived' is a derived state, never a column the user drops into directly.
ALL_STATUSES: tuple[str, ...] = STATUSES + ('archived',)

STATUS_LABELS: dict[str, str] = {
    'deferred': 'Pending 3rd Party',
    'todo': 'To Do',
    'in_progress': 'In Progress',
    'scheduled': 'Scheduled',
    'done': 'Done',
    'archived': 'Archived',
}

# Accent colours per column. Chosen to read acceptably on both the
# github-light and github-dark palettes.
STATUS_ACCENTS: dict[str, str] = {
    'deferred': '#6e7781',
    'todo': '#0969da',
    'in_progress': '#9a6700',
    'scheduled': '#0e7490',
    'done': '#1a7f37',
    'archived': '#8250df',
}


def is_column_status(value: str) -> bool:
    '''Return ``True`` if ``value`` names one of the four real columns.'''
    return value in STATUSES


# =============================================================================
# Time helpers - timestamps are stored as ISO-8601 UTC strings, matching the
# original ``new Date().toISOString()`` format so existing data is portable.
# =============================================================================

def now_iso() -> str:
    '''Current UTC time as an ISO-8601 string with a trailing ``Z``.'''
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec='milliseconds')
        .replace('+00:00', 'Z')
    )


def _parse_ms(iso: str) -> float | None:
    '''Parse an ISO-8601 string to epoch milliseconds, or ``None``.'''
    if not iso:
        return None
    text = iso.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


def _now_ms() -> float:
    return datetime.now(timezone.utc).timestamp() * 1000.0


# =============================================================================
# Archive ageing - Done cards older than the window are shown as archived.
# =============================================================================

ARCHIVE_AFTER_DAYS = 14


def _archive_after_ms(days: int = ARCHIVE_AFTER_DAYS) -> float:
    return days * 24 * 60 * 60 * 1000


def effective_status(
    card: dict[str, Any],
    now_ms: float | None = None,
    archive_after_days: int = ARCHIVE_AFTER_DAYS,
) -> str:
    '''Archive-aware status: a Done card past the window reads as archived.'''
    if now_ms is None:
        now_ms = _now_ms()
    if card.get('status') == 'done':
        since = _parse_ms(card.get('statusChangedAt', ''))
        if since is not None and now_ms - since > _archive_after_ms(
            archive_after_days
        ):
            return 'archived'
    return card.get('status') or 'todo'


def days_until_archive(
    card: dict[str, Any],
    now_ms: float | None = None,
    archive_after_days: int = ARCHIVE_AFTER_DAYS,
) -> int:
    '''Whole days remaining before a Done card ages into the archive.'''
    if now_ms is None:
        now_ms = _now_ms()
    since = _parse_ms(card.get('statusChangedAt', ''))
    if since is None:
        return archive_after_days
    remaining = _archive_after_ms(archive_after_days) - (now_ms - since)
    if remaining <= 0:
        return 0
    return max(0, math.ceil(remaining / (24 * 60 * 60 * 1000)))


# =============================================================================
# Editor configuration
# -----------------------------------------------------------------------------
# At least one editor must be configured; zero is an invalid configuration.
# With a single editor the board is a solo space, so owner names are hidden;
# with two or more it is collaborative and owners are shown.
#   0  -> invalid configuration
#   1  -> valid, owners hidden
#  >1  -> valid, owners shown
# =============================================================================

def is_valid_config(editor_count: int) -> bool:
    '''A board needs at least one editor to be writable.'''
    return editor_count >= 1


def show_owners(editor_count: int) -> bool:
    '''Owner names are only meaningful with more than one editor.'''
    return editor_count > 1


# =============================================================================
# Text cleaning
# =============================================================================

_WHITESPACE = re.compile(r'\s+')


def clean_title(value: str) -> str:
    '''Collapse whitespace and cap a card title at 120 characters.'''
    return _WHITESPACE.sub(' ', (value or '').strip())[:120]


def clean_notes(value: str) -> str:
    '''Trim and cap a card's notes at 2000 characters.

    Notes are the card's free-form body text shown beneath the title; internal
    whitespace and line breaks are preserved, only the ends are trimmed.
    '''
    return (value or '').strip()[:2000]


# =============================================================================
# Ordering - positions are numeric values stored as strings. A new card sits
# after the current maximum; a reorder drops a card at the midpoint of its new
# neighbours, so existing rows never need rewriting.
# =============================================================================

def position_value(card: dict[str, Any]) -> float:
    '''Numeric position of a card, defaulting to 0 when unparseable.'''
    try:
        return float(card.get('position', '0'))
    except (TypeError, ValueError):
        return 0.0


def midpoint_position(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> str:
    '''Position string that sorts between ``before`` and ``after``.'''
    b = position_value(before) if before else None
    a = position_value(after) if after else None
    if b is None and a is None:
        return '1'
    if b is None:
        return _fmt(a - 1)  # type: ignore[operator]
    if a is None:
        return _fmt(b + 1)
    return _fmt((a + b) / 2)


def _fmt(value: float) -> str:
    '''Render a position without a trailing ``.0`` for whole numbers.'''
    if value == int(value):
        return str(int(value))
    return repr(value)
