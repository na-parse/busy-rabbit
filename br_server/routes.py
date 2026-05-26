'''HTTP routes.

A single Flask blueprint exposing the board page, the login flow, and the JSON
API the browser polls. The API mirrors the original Lakebed query/mutation set:

    GET    /api/board                 board snapshot + viewer capabilities
    POST   /api/cards                 create a card            (editor)
    PATCH  /api/cards/<id>            edit title/notes         (editor)
    POST   /api/cards/<id>/move       reorder / change column  (editor)
    DELETE /api/cards/<id>            delete a card            (editor)
    POST   /api/archive-stale         age Done -> Archived     (editor)

Write routes require an editor session; everything else is public read-only.
'''

from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import auth, board, email_send
from .config import Config
from .db import CardStore
from .logging_setup import get_logger

bp = Blueprint('board', __name__)


# =============================================================================
# Context accessors - config and store are attached to the app at create time.
# =============================================================================

def _config() -> Config:
    return current_app.extensions['busy_rabbit']['config']


def _store() -> CardStore:
    return current_app.extensions['busy_rabbit']['store']


def _log():
    return get_logger()


# =============================================================================
# Auth guard
# =============================================================================

def editor_required(view: Callable) -> Callable:
    '''Reject non-editors with 403 on write endpoints.'''

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        config = _config()
        if not auth.is_config_valid(config):
            return jsonify(error='Invalid configuration: no editors set.'), 409
        if not auth.is_editor(config):
            return jsonify(error='Editor access is required.'), 403
        return view(*args, **kwargs)

    return wrapper


# =============================================================================
# Pages
# =============================================================================

@bp.get('/')
def index():
    '''Render the board shell; the browser fetches data from the API.

    In a ``closed`` deployment an unauthenticated visitor is sent into the
    login flow before any board content is rendered.
    '''
    config = _config()
    if config.is_closed and not auth.is_editor(config):
        return redirect(url_for('board.login_page', required='1'))
    return render_template(
        'index.html',
        board_title=config.board.title,
        app_owner=config.board.app_owner,
        statement=config.board.statement,
        is_editor=auth.is_editor(config),
        viewer_name=auth.viewer_name(config),
        config_valid=auth.is_config_valid(config),
    )


# -----------------------------------------------------------------------------
# Login flow - a multi-step page: email -> code, plus a CLI-token bypass.
# -----------------------------------------------------------------------------

def _render_login(step: str, *, email: str = '', error: str = '',
                  info: str = '', required: bool = False):
    '''Render one step of the login page with optional banners.'''
    config = _config()
    return render_template(
        'login.html',
        board_title=config.board.title,
        config_valid=auth.is_config_valid(config),
        step=step,
        email=email,
        error=error,
        info=info,
        required=required,
    )


@bp.get('/login')
def login_page():
    '''Step 1: prompt for an editor email address.'''
    return _render_login(
        'email', required=bool(request.args.get('required'))
    )


@bp.post('/login')
def login_submit():
    '''Issue a login code for the submitted email and advance to step 2.'''
    config = _config()
    email = request.form.get('email', '')
    try:
        code = auth.request_code(_store(), config, email)
    except auth.NotAnEditor:
        _log().warning('Login attempt for non-editor email=%r', email)
        return _render_login(
            'email', email=email,
            error='That email is not an authorized editor.',
        )
    except auth.RateLimited:
        return _render_login(
            'email', email=email,
            error='Too many code requests. Try again later.',
        )

    try:
        email_send.send_login_code(config, email.strip(), code)
    except email_send.SmtpError as exc:
        _log().error('SMTP send failed for %s: %s', email, exc)
        return _render_login(
            'email', email=email,
            error='SMTP is not working right now. Please try again later.',
        )

    auth.set_pending(email)
    _log().info('Login code sent to %s', email)
    return _render_login('verify', email=email.strip().lower(),
                         info='We emailed you a login code.')


@bp.get('/login/verify')
def verify_page():
    '''Step 2: prompt for the emailed code (requires a pending email).'''
    pending = auth.pending_email()
    if not pending:
        return redirect(url_for('board.login_page'))
    return _render_login('verify', email=pending)


@bp.post('/login/verify')
def verify_submit():
    '''Validate the code and start a session on success.'''
    config = _config()
    pending = auth.pending_email()
    if not pending:
        return redirect(url_for('board.login_page'))
    code = request.form.get('code', '')
    if auth.verify_code(_store(), config, pending, code):
        _log().info('Editor login (code): %s', pending)
        return redirect(url_for('board.index'))
    _log().warning('Bad/expired code for %s', pending)
    return _render_login('verify', email=pending,
                         error='Invalid or expired code.')


@bp.post('/login/resend')
def resend_code():
    '''Re-issue a code to the pending email and stay on step 2.'''
    config = _config()
    pending = auth.pending_email()
    if not pending:
        return redirect(url_for('board.login_page'))
    try:
        code = auth.request_code(_store(), config, pending)
        email_send.send_login_code(config, pending, code)
    except auth.RateLimited:
        return _render_login('verify', email=pending,
                             error='Too many code requests. Try again later.')
    except email_send.SmtpError as exc:
        _log().error('SMTP resend failed for %s: %s', pending, exc)
        return _render_login(
            'verify', email=pending,
            error='SMTP is not working right now. Please try again later.',
        )
    except auth.NotAnEditor:
        return redirect(url_for('board.login_page'))
    _log().info('Login code resent to %s', pending)
    return _render_login('verify', email=pending, info='New code sent.')


@bp.get('/login/token')
def token_page():
    '''Alternate step: prompt for an email + CLI prevalidation token.'''
    return _render_login('token')


@bp.post('/login/token')
def token_submit():
    '''Validate a prevalidation token and start a session on success.'''
    config = _config()
    email = request.form.get('email', '')
    token = request.form.get('token', '')
    if auth.verify_token(_store(), config, email, token):
        _log().info('Editor login (token): %s', email.strip().lower())
        return redirect(url_for('board.index'))
    _log().warning('Bad/expired token for email=%r', email)
    return _render_login('token', email=email,
                         error='Invalid or expired token for that email.')


@bp.post('/logout')
def logout():
    '''End the editor session and return to the board.'''
    auth.logout()
    return redirect(url_for('board.index'))


# =============================================================================
# JSON API
# =============================================================================

@bp.get('/api/board')
def api_board():
    '''Board snapshot: every card plus the viewer's capabilities.

    A closed deployment withholds the board from unauthenticated callers.
    '''
    config = _config()
    if config.is_closed and not auth.is_editor(config):
        return jsonify(error='Login required for access.'), 401
    editor_count = len(config.board.editors)
    return jsonify(
        cards=_store().all_cards(),
        isEditor=auth.is_editor(config),
        editorCount=editor_count,
        configValid=auth.is_config_valid(config),
        showOwners=board.show_owners(editor_count),
        viewerName=auth.viewer_name(config),
        archiveAfterDays=config.board.archive_after_days,
        statuses=list(board.STATUSES),
        statusLabels=board.STATUS_LABELS,
        statusAccents=board.STATUS_ACCENTS,
    )


@bp.post('/api/cards')
@editor_required
def api_create_card():
    '''Create a card in a column. Empty titles are ignored, as in the original.'''
    config = _config()
    data = request.get_json(silent=True) or {}
    title = board.clean_title(data.get('title', ''))
    if not title:
        return jsonify(error='Title is required.'), 400
    status = data.get('status', 'todo')
    column = status if board.is_column_status(status) else 'todo'

    store = _store()
    # New cards sort after everything else in their column.
    existing = store.cards_with_status(column)
    last = max(existing, key=board.position_value, default=None)
    position = board.midpoint_position(last, None)

    editor = auth.current_editor(config) or ''
    card = store.create_card(
        status=column,
        title=title,
        position=position,
        owner_id=editor,
        owner_name=config.owner_name_for(editor),
        notes=data.get('notes', ''),
    )
    _log().info('Card created %s in %s by %s', card['id'], column, editor)
    return jsonify(card=card), 201


@bp.patch('/api/cards/<card_id>')
@editor_required
def api_update_card(card_id: str):
    '''Edit a card's title and notes.'''
    data = request.get_json(silent=True) or {}
    title = board.clean_title(data.get('title', ''))
    if not title:
        return jsonify(error='Title is required.'), 400
    card = _store().update_card(card_id, title, data.get('notes', ''))
    if card is None:
        return jsonify(error='Card not found.'), 404
    _log().info('Card updated %s', card_id)
    return jsonify(card=card)


@bp.post('/api/cards/<card_id>/move')
@editor_required
def api_move_card(card_id: str):
    '''Reorder within a column or move between columns.'''
    data = request.get_json(silent=True) or {}
    existing = _store().get_card(card_id)
    if existing is None:
        return jsonify(error='Card not found.'), 404
    status = data.get('status', 'todo')
    column = status if board.is_column_status(status) else 'todo'
    position = str(data.get('position', existing['position']))
    # Positions sort numerically; reject anything that would not parse, rather
    # than letting it silently collapse to 0 and scramble the column order.
    try:
        float(position)
    except (TypeError, ValueError):
        return jsonify(error='Invalid position.'), 400
    status_changed = existing['status'] != column
    card = _store().move_card(card_id, column, position, status_changed)
    _log().info(
        'Card moved %s -> %s (status_changed=%s)',
        card_id, column, status_changed,
    )
    return jsonify(card=card)


@bp.delete('/api/cards/<card_id>')
@editor_required
def api_delete_card(card_id: str):
    '''Delete a card.'''
    _store().delete_card(card_id)
    _log().info('Card deleted %s', card_id)
    return jsonify(ok=True)


@bp.post('/api/archive-stale')
@editor_required
def api_archive_stale():
    '''Persist Done -> Archived for cards past the ageing window.'''
    config = _config()
    store = _store()
    archived = 0
    for card in store.cards_with_status('done'):
        if board.effective_status(
            card, archive_after_days=config.board.archive_after_days
        ) == 'archived':
            store.set_status(card['id'], 'archived')
            archived += 1
    if archived:
        _log().info('Archived %d stale card(s)', archived)
    return jsonify(archived=archived)
