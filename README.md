# busy-rabbit

A self-contained, self-hostable Kanban / to-do work tracker. It is a Python +
Flask + SQLite re-implementation of the original Lakebed `rabbit-trails`
capsule, with **no Node, no build step, and no external services** so it can be
moved around and hosted locally for compliance.

## Quickstart

Requires Python 3.11+ (uses the stdlib `tomllib`). From the repo root:

```sh
# 1. Install dependencies
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Create your config (guided wizard — writes config.toml)
./busy_rabbit config setup

# 3. Initialise the database
./busy_rabbit db init

# 4. Start the server
./busy_rabbit serve
```

Open the printed URL. To unlock editing, click **Sign in** — see
[Signing in](#signing-in) below. The fastest first-run path needs no SMTP:

```sh
./busy_rabbit auth token you@example.com   # you@... must be a configured editor
```

then visit `/login/token` and enter that email + token.

Prefer to hand-edit instead of the wizard? Copy the documented template and edit
it: `cp config.example.toml config.toml`.

## Signing in

The board is viewable without signing in (in `public` mode); **editing always
requires an editor session**. There is no password — identity is your email,
which must be listed under `[[board.editors]]`. Two ways to sign in:

| Method | How | Needs SMTP? |
| --- | --- | --- |
| **Email code** | Enter your email → receive a 6-digit code → enter it. | Yes |
| **CLI token** | Run `busy_rabbit auth token <email>`, then sign in at `/login/token` with the email + token. | No |

Sessions last 30 days. Editors are re-checked against `config.toml` on every
request, so removing an editor revokes access immediately.

## Features

- Four working columns — **Pending 3rd Party**, **To Do**, **In Progress**,
  **Done** — plus a derived **Archived** drawer for Done cards older than the
  configured window (default 14 days).
- Drag-and-drop reordering within a column and moves between columns, using
  midpoint positioning so existing cards never need rewriting.
- Inline card create / edit / delete, with a free-form **notes** body shown
  beneath the title in a softer tone.
- GitHub-style light and dark themes (remembered per browser).
- Email-based editor auth (emailed one-time code or CLI prevalidation token);
  no passwords, no external identity provider.
- Two access modes: `public` (anyone may view, read-only) or `closed`
  (sign-in required for any access).
- Owner names shown on cards when more than one editor is configured.
- Rotating logs under `./logs`.

## Layout

| Path | Purpose |
| --- | --- |
| `busy_rabbit` | CLI shim: serve, db operations, auth, config, logs |
| `br_server/` | Flask application package |
| `br_server/board.py` | Pure board model (statuses, archiving, ordering) |
| `br_server/db.py` | SQLite card store |
| `br_server/auth.py` | Email-based editor sessions (codes + CLI tokens) |
| `br_server/email_send.py` | SMTP delivery of login codes |
| `br_server/routes.py` | Page + JSON API routes |
| `br_server/wizard.py` | Guided `config setup` wizard |
| `br_server/templates/` | Jinja2 templates |
| `br_server/static/` | `br.js` client + `styles.css` |
| `config.toml` | Local configuration (gitignored) |
| `config.example.toml` | Documented template to copy |
| `data/` | SQLite database (gitignored) |
| `logs/` | Rotating logs (gitignored) |

## CLI

All commands accept a global `--config PATH` to use an alternate config file
(default: `config.toml` at the repo root). Run `./busy_rabbit <command> --help`
for any command's own help.

```sh
./busy_rabbit --config PATH <command>   # use an alternate config file
```

| Command | Options | Description |
| --- | --- | --- |
| `serve` | `--host H`, `--port P`, `--debug` / `--no-debug` | Start the web server. Options override `[server]` from config. |
| `db init` | — | Create the database schema if needed. |
| `db list` | — | List cards in a compact table. |
| `db dump` | `-o`, `--output FILE` | Dump all cards as JSON (to a file, else stdout). |
| `db stats` | — | Show card counts grouped by effective status. |
| `auth token` | `<email>` (required) | Mint a 24h prevalidation token for a configured editor; sign in at `/login/token`. |
| `config setup` | — | Interactive guided wizard; writes a commented `config.toml` (backs up any existing file). |
| `config show` | — | Print the resolved config (secrets redacted) and report validation errors. |
| `logs tail` | `-n`, `--lines N` (default 40) | Print the tail of `logs/busy_rabbit.log`. |

Ctrl+C exits any command cleanly (no traceback).

## Configuration

All settings live in `config.toml` (see `config.example.toml` for the full,
documented template, or run `./busy_rabbit config setup`). Key points:

- `[[board.editors]]` must list at least one entry, each with a valid `email`;
  otherwise the board stays read-only (an in-app banner explains this). An
  optional `nickname` becomes the card owner label (else the email local part).
- `[server] mode` is `public` (open read-only viewing) or `closed` (sign-in
  required for any access).
- `[security] secret_key` signs session cookies — set it to a long random
  string for any real use. The wizard generates one for you.
- `[smtp]` delivers one-time login codes and is required for the email sign-in
  flow; the CLI token flow works without it. Auth is used only when **both**
  `username` and `password` are set.
- Set `[server] host = "0.0.0.0"` to expose the app on your network.
- Paths in `[database]` and `[logging]` are relative to the repo root.
