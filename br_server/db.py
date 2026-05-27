'''SQLite data access.

A thin layer over ``sqlite3`` providing the card store the board needs. The
schema mirrors the original Lakebed ``cards`` table: a text UUID primary key,
the card fields, and created/updated/status-changed timestamps. JSON-friendly
``camelCase`` keys are preserved on the way out so the client code reads the
same shape the original Preact app expected.
'''

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .board import clean_notes, clean_title, now_iso

# =============================================================================
# Schema + migrations
# -----------------------------------------------------------------------------
# SCHEMA_VERSION is stamped into the SQLite ``user_version`` pragma. The schema
# is defined as a *ladder* of migrations: ``_MIGRATIONS[n]`` upgrades a database
# from version ``n-1`` to ``n``. A fresh database is simply version 0 walked up
# to the latest, so creating and upgrading share one path. To evolve the schema:
#   1. write a ``_migrate_to_vN(conn)`` that applies only the version N changes,
#   2. register it in ``_MIGRATIONS`` under key N,
#   3. bump ``SCHEMA_VERSION`` to N.
# A database newer than this build (version > SCHEMA_VERSION) is refused, since
# the code cannot safely downgrade a schema it does not know.
# =============================================================================

SCHEMA_VERSION = 1


class IncompatibleDatabase(RuntimeError):
    '''Raised when a database is newer than this build can handle.'''


# Initial schema. This is migration #1: applied to a brand-new (version 0)
# database. ``IF NOT EXISTS`` keeps it a no-op on any pre-versioning database
# that already carried these tables.
_SCHEMA_V1 = '''
CREATE TABLE IF NOT EXISTS cards (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL DEFAULT '',
    notes            TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'todo',
    position         TEXT NOT NULL DEFAULT '0',
    owner_id         TEXT NOT NULL DEFAULT '',
    owner_name       TEXT NOT NULL DEFAULT '',
    status_changed_at TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL DEFAULT '',
    updated_at       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cards_position ON cards(position);
CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status);

CREATE TABLE IF NOT EXISTS auth_codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT NOT NULL,
    code_hash  TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_codes_email ON auth_codes(email);

CREATE TABLE IF NOT EXISTS auth_tokens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email      TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_email ON auth_tokens(email);
'''


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    '''Create the initial schema (cards + auth tables and their indexes).'''
    conn.executescript(_SCHEMA_V1)


# Ordered ladder of upgrade steps, keyed by the version each step produces.
# Keys must be the contiguous range ``1..SCHEMA_VERSION``.
_MIGRATIONS = {
    1: _migrate_to_v1,
}

# Map between the snake_case DB columns and the camelCase API/JSON keys.
_COLUMN_TO_KEY = {
    'id': 'id',
    'title': 'title',
    'notes': 'notes',
    'status': 'status',
    'position': 'position',
    'owner_id': 'ownerId',
    'owner_name': 'ownerName',
    'status_changed_at': 'statusChangedAt',
    'created_at': 'createdAt',
    'updated_at': 'updatedAt',
}


# =============================================================================
# Store
# =============================================================================

class CardStore:
    '''Card persistence backed by a single SQLite file.'''

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    # -------------------------------------------------------------------------
    # Connection / setup
    # -------------------------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        '''Open a connection with row access by name and FK enforcement.'''
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        return conn

    def init_db(self) -> None:
        '''Create or upgrade the schema to the current :data:`SCHEMA_VERSION`.

        Walks the :data:`_MIGRATIONS` ladder from the database's current
        ``user_version`` up to :data:`SCHEMA_VERSION`, stamping the version
        after each step so an interrupted run resumes where it left off. A
        fresh (version 0) database runs every step; an up-to-date one runs
        none. A database newer than this build raises
        :class:`IncompatibleDatabase`.
        '''
        with self.connect() as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version > SCHEMA_VERSION:
                raise IncompatibleDatabase(
                    f'Database at {self.db_path} has schema version {version}, '
                    f'newer than this build (version {SCHEMA_VERSION}). Upgrade '
                    f'busy-rabbit; this build cannot downgrade the database.'
                )
            for target in range(version + 1, SCHEMA_VERSION + 1):
                # Bind each step to its version stamp in one transaction so a
                # crash never leaves a half-applied step claiming completion.
                # (Migrations should use ``conn.execute``; ``executescript``
                # auto-commits and so escapes this guarantee.)
                with conn:
                    _MIGRATIONS[target](conn)
                    conn.execute(f'PRAGMA user_version = {target}')

    # -------------------------------------------------------------------------
    # Reads
    # -------------------------------------------------------------------------

    def all_cards(self) -> list[dict[str, Any]]:
        '''Every card, ordered by numeric position ascending.'''
        with self.connect() as conn:
            rows = conn.execute(
                'SELECT * FROM cards '
                'ORDER BY CAST(position AS REAL) ASC, created_at ASC'
            ).fetchall()
        return [self._row_to_card(row) for row in rows]

    def get_card(self, card_id: str) -> dict[str, Any] | None:
        '''A single card by id, or ``None`` if it does not exist.'''
        with self.connect() as conn:
            row = conn.execute(
                'SELECT * FROM cards WHERE id = ?', (card_id,)
            ).fetchone()
        return self._row_to_card(row) if row else None

    def cards_with_status(self, status: str) -> list[dict[str, Any]]:
        '''All cards currently in the given stored ``status``.'''
        with self.connect() as conn:
            rows = conn.execute(
                'SELECT * FROM cards WHERE status = ?', (status,)
            ).fetchall()
        return [self._row_to_card(row) for row in rows]

    def count(self) -> int:
        '''Total number of cards.'''
        with self.connect() as conn:
            return conn.execute('SELECT COUNT(*) FROM cards').fetchone()[0]

    # -------------------------------------------------------------------------
    # Writes
    # -------------------------------------------------------------------------

    def create_card(
        self,
        status: str,
        title: str,
        position: str,
        owner_id: str,
        owner_name: str,
        notes: str = '',
    ) -> dict[str, Any]:
        '''Insert a new card and return it.'''
        card_id = uuid.uuid4().hex
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                'INSERT INTO cards (id, title, notes, status, position, '
                'owner_id, owner_name, status_changed_at, created_at, '
                'updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    card_id,
                    clean_title(title),
                    clean_notes(notes),
                    status,
                    position,
                    owner_id,
                    owner_name,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
        return self.get_card(card_id)  # type: ignore[return-value]

    def update_card(
        self, card_id: str, title: str, notes: str = ''
    ) -> dict[str, Any] | None:
        '''Update a card's title and notes.'''
        with self.connect() as conn:
            conn.execute(
                'UPDATE cards SET title = ?, notes = ?, updated_at = ? '
                'WHERE id = ?',
                (clean_title(title), clean_notes(notes), now_iso(), card_id),
            )
        return self.get_card(card_id)

    def move_card(
        self, card_id: str, status: str, position: str, status_changed: bool
    ) -> dict[str, Any] | None:
        '''Reposition a card; bump ``status_changed_at`` only on a real move.'''
        timestamp = now_iso()
        with self.connect() as conn:
            if status_changed:
                conn.execute(
                    'UPDATE cards SET status = ?, position = ?, '
                    'status_changed_at = ?, updated_at = ? WHERE id = ?',
                    (status, position, timestamp, timestamp, card_id),
                )
            else:
                conn.execute(
                    'UPDATE cards SET position = ?, updated_at = ? '
                    'WHERE id = ?',
                    (position, timestamp, card_id),
                )
        return self.get_card(card_id)

    def set_status(self, card_id: str, status: str) -> None:
        '''Force a card's stored status (used by archive sweeps).'''
        with self.connect() as conn:
            conn.execute(
                'UPDATE cards SET status = ?, updated_at = ? WHERE id = ?',
                (status, now_iso(), card_id),
            )

    def delete_card(self, card_id: str) -> None:
        '''Remove a card.'''
        with self.connect() as conn:
            conn.execute('DELETE FROM cards WHERE id = ?', (card_id,))

    # -------------------------------------------------------------------------
    # Auth secrets - login codes and CLI prevalidation tokens
    # -------------------------------------------------------------------------
    # Codes/tokens are stored as hashes. For both, only the newest unexpired
    # row for an email is honoured at verify time, so issuing a new secret
    # implicitly retires older ones. Rows past the rate/expiry window are
    # pruned on insert to keep the tables small.

    def add_auth_code(self, email: str, code_hash: str, prune_before: str) -> None:
        '''Record a freshly issued login code; prune codes older than a cutoff.'''
        with self.connect() as conn:
            conn.execute('DELETE FROM auth_codes WHERE created_at < ?',
                         (prune_before,))
            conn.execute(
                'INSERT INTO auth_codes (email, code_hash, created_at) '
                'VALUES (?, ?, ?)',
                (email, code_hash, now_iso()),
            )

    def newest_auth_code(self, email: str) -> dict[str, str] | None:
        '''The most recently issued code row for an email, or ``None``.'''
        with self.connect() as conn:
            row = conn.execute(
                'SELECT code_hash, created_at FROM auth_codes '
                'WHERE email = ? ORDER BY id DESC LIMIT 1',
                (email,),
            ).fetchone()
        return {'hash': row['code_hash'], 'created_at': row['created_at']} \
            if row else None

    def delete_auth_codes(self, email: str) -> None:
        '''Drop every login code for an email (consumed on successful login).'''
        with self.connect() as conn:
            conn.execute('DELETE FROM auth_codes WHERE email = ?', (email,))

    def count_auth_codes_since(self, email: str, since: str) -> int:
        '''Codes issued to an email at or after ``since`` (rate limiting).'''
        with self.connect() as conn:
            return conn.execute(
                'SELECT COUNT(*) FROM auth_codes '
                'WHERE email = ? AND created_at >= ?',
                (email, since),
            ).fetchone()[0]

    def add_auth_token(self, email: str, token_hash: str,
                       prune_before: str) -> None:
        '''Record a CLI prevalidation token; prune tokens past the cutoff.'''
        with self.connect() as conn:
            conn.execute('DELETE FROM auth_tokens WHERE created_at < ?',
                         (prune_before,))
            conn.execute(
                'INSERT INTO auth_tokens (email, token_hash, created_at) '
                'VALUES (?, ?, ?)',
                (email, token_hash, now_iso()),
            )

    def newest_auth_token(self, email: str) -> dict[str, str] | None:
        '''The most recently issued token row for an email, or ``None``.'''
        with self.connect() as conn:
            row = conn.execute(
                'SELECT token_hash, created_at FROM auth_tokens '
                'WHERE email = ? ORDER BY id DESC LIMIT 1',
                (email,),
            ).fetchone()
        return {'hash': row['token_hash'], 'created_at': row['created_at']} \
            if row else None

    def delete_auth_tokens(self, email: str) -> None:
        '''Drop every CLI token for an email (consumed on successful login).'''
        with self.connect() as conn:
            conn.execute('DELETE FROM auth_tokens WHERE email = ?', (email,))

    # -------------------------------------------------------------------------
    # Serialisation
    # -------------------------------------------------------------------------

    @staticmethod
    def _row_to_card(row: sqlite3.Row) -> dict[str, Any]:
        '''Convert a DB row into the camelCase card dict the client expects.'''
        return {
            _COLUMN_TO_KEY[column]: row[column] for column in _COLUMN_TO_KEY
        }
