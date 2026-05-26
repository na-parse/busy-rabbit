/* =============================================================================
   busy-rabbit client

   Vanilla JS port of the original Preact board. It fetches /api/board, renders
   the columns, wires drag-and-drop + inline editing, and polls for changes so
   multiple tabs stay roughly in sync. No framework or build step: the DOM is
   rebuilt from the latest board snapshot on each render, with transient UI
   state (which card is being edited, which column is adding) tracked locally.
   ============================================================================= */

(function () {
  'use strict';

  // ===========================================================================
  // Constants mirrored from the server board model
  // ===========================================================================

  var THEME_KEY = 'busy-rabbit-theme';
  var THEMES = ['github-dark', 'github-light'];
  var POLL_MS = 4000;

  // ===========================================================================
  // Local UI state (not persisted; rebuilt board data lives in `state.board`)
  // ===========================================================================

  var state = {
    board: null,
    editingId: null, // card id with an open inline editor
    addingStatus: null, // column status with an open add form
    drag: null, // { id } currently dragged card
    dropBeforeId: null, // card id the drop indicator sits above
    suspendPoll: false // pause polling while the user is mid-edit
  };

  var els = {
    columns: document.getElementById('columns'),
    archived: document.getElementById('archived'),
    readonly: document.getElementById('readonly-badge'),
    themeToggle: document.getElementById('theme-toggle')
  };

  // ===========================================================================
  // Time + ordering helpers (ported from shared/board.ts)
  // ===========================================================================

  function parseMs(iso) {
    var t = Date.parse(iso);
    return isFinite(t) ? t : null;
  }

  function archiveAfterMs() {
    var days = (state.board && state.board.archiveAfterDays) || 14;
    return days * 24 * 60 * 60 * 1000;
  }

  function effectiveStatus(card) {
    if (card.status === 'done') {
      var since = parseMs(card.statusChangedAt);
      if (since !== null && Date.now() - since > archiveAfterMs()) {
        return 'archived';
      }
    }
    return card.status || 'todo';
  }

  function daysUntilArchive(card) {
    var since = parseMs(card.statusChangedAt);
    var days = (state.board && state.board.archiveAfterDays) || 14;
    if (since === null) return days;
    var remaining = archiveAfterMs() - (Date.now() - since);
    return Math.max(0, Math.ceil(remaining / (24 * 60 * 60 * 1000)));
  }

  function positionValue(card) {
    var n = Number(card.position);
    return isFinite(n) ? n : 0;
  }

  function midpointPosition(before, after) {
    var b = before ? positionValue(before) : null;
    var a = after ? positionValue(after) : null;
    if (b === null && a === null) return '1';
    if (b === null) return String(a - 1);
    if (a === null) return String(b + 1);
    return String((a + b) / 2);
  }

  function cleanTitle(value) {
    return value.trim().replace(/\s+/g, ' ').slice(0, 120);
  }

  function formatDate(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleDateString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric'
    });
  }

  function relativeTime(iso) {
    var then = parseMs(iso);
    if (then === null) return '';
    var diff = Date.now() - then;
    var minute = 60000, hour = 60 * minute, day = 24 * hour;
    if (diff < minute) return 'just now';
    if (diff < hour) return Math.round(diff / minute) + 'm ago';
    if (diff < day) return Math.round(diff / hour) + 'h ago';
    var d = Math.round(diff / day);
    if (d < 30) return d + 'd ago';
    return formatDate(iso);
  }

  // ===========================================================================
  // Tiny DOM builder - el('div', { class: 'x' }, [children])
  // ===========================================================================

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === null || value === undefined || value === false) return;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = value;
        else if (key.slice(0, 2) === 'on') {
          node.addEventListener(key.slice(2).toLowerCase(), value);
        } else if (value === true) node.setAttribute(key, '');
        else node.setAttribute(key, value);
      });
    }
    (children || []).forEach(function (child) {
      if (child === null || child === undefined) return;
      node.appendChild(
        typeof child === 'string' ? document.createTextNode(child) : child
      );
    });
    return node;
  }

  // ===========================================================================
  // API
  // ===========================================================================

  function api(method, url, body) {
    return fetch(url, {
      method: method,
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined
    }).then(function (res) {
      if (!res.ok) return res.json().then(function (e) { throw e; });
      return res.status === 204 ? null : res.json();
    });
  }

  function refresh() {
    return api('GET', '/api/board').then(function (data) {
      state.board = data;
      render();
    });
  }

  function mutate(promise) {
    // Optimistically re-fetch after any successful mutation.
    return promise.then(refresh).catch(function (err) {
      console.error(err);
      alert((err && err.error) || 'Action failed.');
    });
  }

  // ===========================================================================
  // Card mutations
  // ===========================================================================

  function createCard(status, title) {
    return mutate(api('POST', '/api/cards', { status: status, title: title }));
  }
  function updateCard(id, title, notes) {
    return mutate(
      api('PATCH', '/api/cards/' + id, { title: title, notes: notes || '' })
    );
  }
  function moveCard(id, status, position) {
    return mutate(
      api('POST', '/api/cards/' + id + '/move', { status: status, position: position })
    );
  }
  function deleteCard(id) {
    return mutate(api('DELETE', '/api/cards/' + id));
  }
  function archiveStale() {
    return api('POST', '/api/archive-stale').then(function (r) {
      if (r && r.archived > 0) refresh();
    });
  }

  // ===========================================================================
  // Grouping
  // ===========================================================================

  function groupCards() {
    var buckets = { deferred: [], todo: [], in_progress: [], done: [], archived: [] };
    (state.board.cards || []).forEach(function (card) {
      buckets[effectiveStatus(card)].push(card);
    });
    Object.keys(buckets).forEach(function (key) {
      buckets[key].sort(function (a, b) {
        return positionValue(a) - positionValue(b);
      });
    });
    return buckets;
  }

  // ===========================================================================
  // Drag + drop
  // ===========================================================================

  function clearDrag() {
    state.drag = null;
    state.dropBeforeId = null;
  }

  // Open the inline editor for a card; pause polling so it cannot clobber the
  // open form, and close any add box.
  function openEdit(id) {
    state.addingStatus = null;
    state.editingId = id;
    state.suspendPoll = true;
    render();
  }

  // Drop-indicator helpers manipulate live DOM directly (no re-render) so the
  // in-flight drag is never interrupted.
  function clearDropIndicators() {
    var marked = els.columns.querySelectorAll('.drop-before');
    for (var i = 0; i < marked.length; i++) {
      marked[i].classList.remove('drop-before');
    }
  }

  function markDropBefore(node) {
    clearDropIndicators();
    node.classList.add('drop-before');
  }

  function dropBefore(column, targetId, grouped) {
    if (!state.drag) return;
    var list = grouped[column].filter(function (c) { return c.id !== state.drag.id; });
    var index = list.findIndex(function (c) { return c.id === targetId; });
    if (index === -1) { dropAtEnd(column, grouped); return; }
    var position = midpointPosition(list[index - 1], list[index]);
    var id = state.drag.id;
    clearDrag();
    moveCard(id, column, position);
  }

  function dropAtEnd(column, grouped) {
    if (!state.drag) return;
    var list = grouped[column].filter(function (c) { return c.id !== state.drag.id; });
    var position = midpointPosition(list[list.length - 1], undefined);
    var id = state.drag.id;
    clearDrag();
    moveCard(id, column, position);
  }

  // ===========================================================================
  // Card rendering
  // ===========================================================================

  function avatar(name) {
    var initial = (name || '').trim().slice(0, 1).toUpperCase() || '?';
    return el('span', { class: 'avatar', 'aria-hidden': 'true' }, [initial]);
  }

  function cardView(card, column) {
    var isEditor = state.board.isEditor;
    var withOwners = state.board.showOwners;
    var archiving = column === 'done' ? daysUntilArchive(card) : null;

    var meta = [
      el('span', { title: 'Created ' + formatDate(card.createdAt) }, [
        'created ' + relativeTime(card.createdAt)
      ]),
      el('span', { title: 'In this column since ' + formatDate(card.statusChangedAt) }, [
        (column === 'archived' ? 'archived ' : 'moved ') +
          relativeTime(card.statusChangedAt)
      ])
    ];
    if (archiving !== null) {
      meta.push(el('span', { class: 'archive-soon' }, ['archives in ' + archiving + 'd']));
    }

    var children = [
      el('h3', {}, [card.title]),
      // Notes are the card body, set off from the title by a rule and shown in
      // a softer tone.
      card.notes
        ? el('div', { class: 'card-notes' }, [el('p', {}, [card.notes])])
        : null,
      el('div', { class: 'card-meta' }, meta)
    ];
    if (withOwners && card.ownerName) {
      children.push(
        el('div', { class: 'card-owner' }, [avatar(card.ownerName), card.ownerName])
      );
    }

    var classes = ['card'];
    if (isEditor) classes.push('editable');
    if (state.drag && state.drag.id === card.id) classes.push('dragging');
    if (state.dropBeforeId === card.id) classes.push('drop-before');

    // draggable is an enumerated attribute: it must be the literal string
    // "true", not an empty/boolean value, or the browser will not drag it.
    var node = el('article', {
      class: classes.join(' '),
      draggable: isEditor ? 'true' : null
    }, children);

    if (isEditor) {
      node.addEventListener('click', function () { openEdit(card.id); });
      // IMPORTANT: never call render() while a drag is in flight. render()
      // rebuilds the column DOM via innerHTML, which destroys the node being
      // dragged and cancels the drag. Instead we toggle classes directly on
      // the live nodes and only re-render once the drag has finished.
      node.addEventListener('dragstart', function (e) {
        state.drag = { id: card.id };
        if (e.dataTransfer) {
          e.dataTransfer.effectAllowed = 'move';
          // Firefox requires data to be set for a drag to begin.
          try { e.dataTransfer.setData('text/plain', card.id); } catch (_) {}
        }
        // Defer so the drag image captures the card before it dims.
        setTimeout(function () { node.classList.add('dragging'); }, 0);
      });
      node.addEventListener('dragend', function () {
        clearDropIndicators();
        clearDrag();
        render();
      });
      node.addEventListener('dragover', function (e) {
        if (!state.drag) return;
        e.preventDefault();
        if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
        markDropBefore(node);
        state.dropBeforeId = card.id;
      });
      node.addEventListener('drop', function (e) {
        if (!state.drag) return;
        e.preventDefault();
        e.stopPropagation();
        dropBefore(column, card.id, groupCards());
      });
    }
    return node;
  }

  function cardEditor(card) {
    var titleInput = el('input', { value: card.title, placeholder: 'Title', autofocus: true });
    var notesInput = el('textarea', { rows: '4', class: 'notes-field', placeholder: 'Notes' });
    notesInput.value = card.notes || '';

    function save() {
      if (!cleanTitle(titleInput.value)) return;
      state.editingId = null;
      state.suspendPoll = false;
      updateCard(card.id, titleInput.value, notesInput.value);
    }
    function cancel() {
      state.editingId = null;
      state.suspendPoll = false;
      render();
    }

    return el('div', { class: 'editor-box' }, [
      titleInput,
      notesInput,
      el('div', { class: 'editor-actions' }, [
        el('button', {
          type: 'button', class: 'btn-danger',
          onClick: function () {
            state.editingId = null;
            state.suspendPoll = false;
            deleteCard(card.id);
          }
        }, ['Delete']),
        el('div', { class: 'right' }, [
          el('button', { type: 'button', class: 'btn-cancel', onClick: cancel }, ['Cancel']),
          el('button', { type: 'button', class: 'btn-save', onClick: save }, ['Save'])
        ])
      ])
    ]);
  }

  function addBox(status) {
    var input = el('textarea', { rows: '2', placeholder: 'Card title, then Enter' });
    var box = el('div', { class: 'add-box' }, [input]);

    // Close the form. `save` true -> create a card from the typed title (used
    // by Enter and clicking elsewhere, so work is not lost); false -> discard
    // (used by Escape).
    function close(save) {
      teardown();
      state.addingStatus = null;
      state.suspendPoll = false;
      var clean = save ? cleanTitle(input.value) : '';
      if (clean) {
        createCard(status, clean);
      } else {
        render();
      }
    }

    // Clicking anywhere outside the form closes it.
    function onDocPointerDown(e) {
      if (!box.contains(e.target)) close(true);
    }
    // Esc closes it from anywhere, discarding the entry.
    function onDocKeyDown(e) {
      if (e.key === 'Escape') close(false);
    }
    function teardown() {
      document.removeEventListener('pointerdown', onDocPointerDown, true);
      document.removeEventListener('keydown', onDocKeyDown, true);
    }

    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); close(true); }
    });

    // Defer focus + document listeners to the next tick so the very click that
    // opened this form (on the "+") is not itself treated as an outside click.
    // (A dynamically inserted element's `autofocus` attribute does nothing, so
    // focus must be set programmatically.)
    setTimeout(function () {
      input.focus();
      document.addEventListener('pointerdown', onDocPointerDown, true);
      document.addEventListener('keydown', onDocKeyDown, true);
    }, 0);

    return box;
  }

  // ===========================================================================
  // Column rendering
  // ===========================================================================

  function column(status, cards) {
    var isEditor = state.board.isEditor;
    var labels = state.board.statusLabels;
    var accents = state.board.statusAccents;

    var header = el('header', { class: 'column-header' }, [
      el('div', { class: 'column-title' }, [
        el('span', { class: 'dot', style: 'background-color:' + accents[status] }),
        el('h2', {}, [labels[status]]),
        el('span', { class: 'count' }, [String(cards.length)])
      ]),
      isEditor
        ? el('button', {
            type: 'button', class: 'add-btn',
            'aria-label': 'Add card to ' + labels[status],
            onClick: function () {
              state.editingId = null;
              state.addingStatus = status;
              state.suspendPoll = true;
              render();
            }
          }, ['+'])
        : null
    ]);

    var body = el('div', { class: 'column-body' }, []);
    if (state.addingStatus === status) body.appendChild(addBox(status));
    cards.forEach(function (card) {
      body.appendChild(
        state.editingId === card.id ? cardEditor(card) : cardView(card, status)
      );
    });
    if (cards.length === 0 && state.addingStatus !== status) {
      body.appendChild(el('p', { class: 'empty' }, ['Nothing here']));
    }

    if (isEditor) {
      body.addEventListener('dragover', function (e) {
        if (!state.drag) return;
        e.preventDefault();
      });
      body.addEventListener('drop', function (e) {
        if (!state.drag) return;
        e.preventDefault();
        dropAtEnd(status, groupCards());
      });
    }

    return el('section', { class: 'column' }, [header, body]);
  }

  // ===========================================================================
  // Archived drawer
  // ===========================================================================

  var archivedOpen = false;

  function renderArchived(cards) {
    if (!cards.length) { els.archived.hidden = true; els.archived.innerHTML = ''; return; }
    els.archived.hidden = false;
    els.archived.innerHTML = '';

    var labels = state.board.statusLabels;
    var accents = state.board.statusAccents;
    var toggle = el('button', { type: 'button', class: 'archived-toggle' }, [
      el('span', { class: 'dot', style: 'background-color:' + accents.archived }),
      labels.archived,
      el('span', { class: 'count' }, [String(cards.length)]),
      el('span', {}, [archivedOpen ? '▾' : '▸'])
    ]);
    toggle.addEventListener('click', function () { archivedOpen = !archivedOpen; render(); });
    els.archived.appendChild(toggle);

    if (archivedOpen) {
      var grid = el('div', { class: 'archived-grid' }, []);
      cards.forEach(function (card) { grid.appendChild(cardView(card, 'archived')); });
      els.archived.appendChild(grid);
    }
  }

  // ===========================================================================
  // Render
  // ===========================================================================

  function render() {
    if (!state.board) return;
    els.columns.setAttribute('aria-busy', 'false');
    els.columns.innerHTML = '';
    var grouped = groupCards();
    state.board.statuses.forEach(function (status) {
      els.columns.appendChild(column(status, grouped[status]));
    });
    renderArchived(grouped.archived);

    if (els.readonly) els.readonly.hidden = !!state.board.isEditor;
  }

  // ===========================================================================
  // Theme
  // ===========================================================================

  function initialTheme() {
    var saved = localStorage.getItem(THEME_KEY);
    if (saved && THEMES.indexOf(saved) !== -1) return saved;
    if (window.matchMedia && matchMedia('(prefers-color-scheme: light)').matches) {
      return 'github-light';
    }
    return 'github-dark';
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem(THEME_KEY, theme);
    if (els.themeToggle) {
      els.themeToggle.textContent = theme === 'github-light' ? '☀ light' : '☾ dark';
    }
  }

  // ===========================================================================
  // Boot
  // ===========================================================================

  function boot() {
    var theme = initialTheme();
    applyTheme(theme);
    if (els.themeToggle) {
      els.themeToggle.addEventListener('click', function () {
        var current = document.documentElement.getAttribute('data-theme');
        var next = THEMES[(THEMES.indexOf(current) + 1) % THEMES.length];
        applyTheme(next);
      });
    }

    refresh().then(function () {
      // Editors persist Done -> Archived ageing once on load.
      if (state.board && state.board.isEditor) archiveStale();
    });

    // Poll for changes from other tabs/users, but never clobber an open editor.
    setInterval(function () {
      if (state.suspendPoll || state.editingId || state.addingStatus || state.drag) return;
      refresh();
    }, POLL_MS);
  }

  boot();
})();
