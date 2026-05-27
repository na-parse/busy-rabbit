'''Demo data seeding.

Builds a representative set of cards that exercises every part of the board
design — all five working columns, the derived ``archived`` state, the Done
archive countdown, per-owner avatars (when more than one editor is
configured), and a mix of cards with and without notes — so a fresh instance
can be shown off without hand-entering data. Used by ``busy_rabbit db demo``.
'''

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import Editor
from .db import CardStore

# =============================================================================
# Fixtures
# -----------------------------------------------------------------------------
# Each fixture is ``(status, title, notes, owner, age_days)``:
#   owner    - index into the configured editor list (wrapped modulo its
#              length), so the demo cast spreads across whoever is configured.
#   age_days - how long ago the card entered its current status. It backdates
#              both ``created_at`` and ``status_changed_at``, driving the
#              timing popover for every column and, for Done cards, the archive
#              countdown: anything older than ``archive_after_days`` (default
#              14) renders in the derived ``archived`` state instead.
# The set deliberately mixes noted and bare cards, spreads ownership, and
# places two Done cards past the archive window so the archived shelf is
# populated out of the box.
# =============================================================================

DEMO_CARDS: tuple[tuple[str, str, str, int, float], ...] = (
    # --- deferred: Pending 3rd Party ------------------------------------
    ('deferred', 'Awaiting CA-signed TLS certificate',
     'Security team is issuing the cert; until then we run self-signed '
     'behind the reverse proxy.', 0, 5),
    ('deferred', 'SMTP relay allowlist request', '', 1, 2),

    # --- todo -----------------------------------------------------------
    ('todo', 'Write the operator runbook',
     'Cover serve, db init/demo, auth token, and config setup with copy-paste '
     'examples.', 0, 1),
    ('todo', 'Add CSV export of the board', '', 1, 3),
    ('todo', 'Quarterly editor list audit', '', 0, 0),
    ('todo', 'Document SQLite WAL backup and restore',
     'Note that -wal/-shm sidecars must be copied with the main db file.', 1, 4),
    ('todo', 'Add card search and filter', '', 0, 2),
    ('todo', 'Review medium-theme contrast on small screens', '', 1, 6),

    # --- in_progress ----------------------------------------------------
    ('in_progress', 'Harden the session cookie against CSRF',
     'SameSite=Lax plus an origin check on state-changing requests.', 0, 1),
    ('in_progress', 'Plan the v2 schema migration',
     'Adding a labels table; extend the _MIGRATIONS ladder to version 2.', 1, 2),
    ('in_progress', 'Tune owner avatar colours', '', 0, 0),

    # --- scheduled ------------------------------------------------------
    ('scheduled', 'Quarterly dependency bump',
     'Pin and refresh requirements.txt, then smoke-test serve + login.', 1, 3),
    ('scheduled', 'Rotate the session signing secret', '', 0, 1),
    ('scheduled', 'Staging deploy dry-run', '', 1, 2),

    # --- done: recent, archive countdown still visible ------------------
    ('done', 'Reframe the README as a deployment guide', '', 0, 1),
    ('done', 'Add the Scheduled column and medium theme',
     'New working column plus a third palette between light and dark.', 1, 3),
    ('done', 'Card timing popover and coloured avatars', '', 0, 7),
    ('done', 'Initial Flask + SQLite port of rabbit-trails',
     'Self-contained rewrite: no Node, no build step.', 1, 12),

    # --- done: older than the window, shown as archived -----------------
    ('done', 'Choose the stack: Flask over Node', '', 0, 21),
    ('done', 'Project kickoff and scope', '', 1, 30),
)


# =============================================================================
# Seeding
# =============================================================================

def _iso_days_ago(days: float) -> str:
    '''ISO-8601 UTC timestamp ``days`` in the past, with a trailing ``Z``.'''
    moment = datetime.now(timezone.utc) - timedelta(days=days)
    return moment.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def seed_demo(store: CardStore, editors: list[Editor]) -> int:
    '''Insert the demo fixtures into ``store`` and return how many were added.

    Cards are positioned in fixture order and assigned to the configured
    ``editors`` round-robin. ``editors`` must be non-empty.
    '''
    if not editors:
        raise ValueError('seed_demo requires at least one editor')
    for position, fixture in enumerate(DEMO_CARDS, start=1):
        status, title, notes, owner_idx, age_days = fixture
        editor = editors[owner_idx % len(editors)]
        timestamp = _iso_days_ago(age_days)
        store.create_card(
            status=status,
            title=title,
            position=str(position),
            owner_id=editor.email,
            owner_name=editor.owner_name,
            notes=notes,
            created_at=timestamp,
            status_changed_at=timestamp,
        )
    return len(DEMO_CARDS)
