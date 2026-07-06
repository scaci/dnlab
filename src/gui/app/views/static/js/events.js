/**
 * EventsPanel – docked footer that streams orchestrator progress events.
 *
 * Subscribes to /ws/events/{lab} for the currently-open topology.
 * Displays a running log of BusEvent dicts ({phase, status, host, detail,
 * elapsed_ms, data}) and exposes the latest status so the toolbar badge
 * can reflect the orchestrator's view.
 *
 * Only one subscription is active at a time — switching topology tears
 * down the previous socket before opening the next.
 */
const EventsPanel = (() => {
  let _ws = null;
  let _lab = null;
  let _rootEl = null;
  let _bodyEl = null;
  let _statusEl = null;
  let _toggleEl = null;
  let _clearEl = null;
  let _collapsed = false;
  let _autoScroll = true;
  let _latestPhase = null;
  let _statusListener = null;
  let _intentionalClose = false;
  let _reconnectAttempt = 0;
  let _reconnectTimer = null;
  const _lastSigByKey = new Map();

  function init(rootId = 'events-footer') {
    _rootEl = document.getElementById(rootId);
    if (!_rootEl) return;
    _rootEl.innerHTML = `
      <div class="events-header">
        <span class="events-title">⚡ Lab events</span>
        <span class="events-latest" id="events-latest">—</span>
        <span class="events-spacer"></span>
        <button class="events-btn" id="events-clear" title="Clear Log">🧹</button>
        <button class="events-btn" id="events-toggle" title="Expand / Collapse">▾</button>
      </div>
      <div class="events-body" id="events-body"></div>
    `;
    _bodyEl = _rootEl.querySelector('#events-body');
    _statusEl = _rootEl.querySelector('#events-latest');
    _toggleEl = _rootEl.querySelector('#events-toggle');
    _clearEl = _rootEl.querySelector('#events-clear');

    _toggleEl.addEventListener('click', _toggleCollapsed);
    _rootEl.querySelector('.events-header').addEventListener('click', (e) => {
      // Clicking the header (but not the buttons) toggles as well.
      if (e.target === _toggleEl || e.target === _clearEl) return;
      _toggleCollapsed();
    });
    _clearEl.addEventListener('click', (e) => {
      e.stopPropagation();
      _bodyEl.innerHTML = '';
      _lastSigByKey.clear();
    });

    _bodyEl.addEventListener('scroll', () => {
      const atBottom = _bodyEl.scrollHeight - _bodyEl.scrollTop - _bodyEl.clientHeight < 8;
      _autoScroll = atBottom;
    });

    // Start collapsed until a topology is opened.
    _setCollapsed(true);
  }

  function setLab(labId) {
    if (labId === _lab) return;
    _close();
    _lab = labId;
    _lastSigByKey.clear();
    if (!labId) {
      _setCollapsed(true);
      return;
    }
    _setCollapsed(false);
    _appendLine(`— connection to the lab —`, 'events-sys');
    _intentionalClose = false;
    _openWs(labId);
  }

  function _openWs(labId) {
    const shortId = String(labId).slice(0, 8);
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws/events/${labId}`);
    _ws = ws;

    ws.onopen = () => {
      if (_reconnectAttempt > 0) {
        _appendLine(`[WebSocket connected (lab=${shortId})]`, 'events-sys events-ok');
      }
      _reconnectAttempt = 0;
    };

    ws.onmessage = (ev) => {
      let data;
      try { data = JSON.parse(ev.data); } catch (_) { return; }
      // Per-slot deduplication: the poller republishes the same events on every
      // cycle. Key = phase|host. We render only if the new
      // signature (status|detail) differs from the last one for that slot.
      const key = `${data.phase}|${data.host || ''}`;
      const sig = `${data.status}|${data.detail || ''}`;
      if (_lastSigByKey.get(key) !== sig) {
        _renderEvent(data);
        _lastSigByKey.set(key, sig);
      }
      _latestPhase = data;
      _updateLatest(data);
      if (_statusListener) _statusListener(data);
    };

    ws.onclose = () => {
      if (_intentionalClose) return;
      const delay = Math.min(1000 * 2 ** _reconnectAttempt, 30000);
      const cls = _reconnectAttempt >= 3 ? 'events-sys events-warn' : 'events-sys';
      _appendLine(`[WebSocket closed (lab=${shortId}), retrying in ${Math.round(delay / 1000)}s]`, cls);
      _reconnectTimer = setTimeout(() => {
        _reconnectTimer = null;
        _openWs(labId);
      }, delay);
      _reconnectAttempt++;
    };

    ws.onerror = () => _appendLine(`[Error WebSocket (lab=${shortId})]`, 'events-sys events-err');
  }

  function onStatus(cb) { _statusListener = cb; }

  function latest() { return _latestPhase; }

  // ── internals ──────────────────────────────────────────────────────
  function _close() {
    _intentionalClose = true;
    if (_reconnectTimer !== null) {
      clearTimeout(_reconnectTimer);
      _reconnectTimer = null;
    }
    _reconnectAttempt = 0;
    if (_ws) {
    // Detach handlers before closing: the intentional shutdown
    // is silent (no "[WebSocket closed]" while we're already
    // showing "— connecting to the lab —" for the new lab).
      _ws.onopen = _ws.onmessage = _ws.onclose = _ws.onerror = null;
      try { _ws.close(); } catch (_) {}
      _ws = null;
    }
  }

  function _toggleCollapsed() { _setCollapsed(!_collapsed); }

  function _setCollapsed(v) {
    _collapsed = v;
    if (!_rootEl) return;
    _rootEl.classList.toggle('collapsed', v);
    if (_toggleEl) _toggleEl.textContent = v ? '▴' : '▾';
  }

  function _renderEvent(evt) {
    const cls = _statusClass(evt.status);
    const elapsed = evt.elapsed_ms ? `${(evt.elapsed_ms / 1000).toFixed(1)}s` : '';
    const host = evt.host ? ` @${evt.host}` : '';
    const detail = evt.detail ? ` — ${evt.detail}` : '';
    const line = `[${evt.phase}/${evt.status}]${host}${detail}${elapsed ? `  (${elapsed})` : ''}`;
    _appendLine(line, cls);
  }

  function _appendLine(text, extraClass = '') {
    if (!_bodyEl) return;
    const el = document.createElement('div');
    el.className = `events-line ${extraClass}`;
    el.textContent = text;
    _bodyEl.appendChild(el);
    // Cap buffer to 500 lines to match server-side ring buffer.
    while (_bodyEl.childNodes.length > 500) _bodyEl.removeChild(_bodyEl.firstChild);
    if (_autoScroll) _bodyEl.scrollTop = _bodyEl.scrollHeight;
  }

  function _updateLatest(evt) {
    if (!_statusEl) return;
    const host = evt.host ? `@${evt.host}` : '';
    _statusEl.textContent = `${evt.phase}/${evt.status} ${host} ${evt.detail || ''}`.trim();
    _statusEl.className = `events-latest ${_statusClass(evt.status)}`;
  }

  function _statusClass(status) {
    switch (status) {
      case 'ok':
      case 'done':
      case 'success':   return 'events-ok';
      case 'error':
      case 'failed':    return 'events-err';
      case 'warn':
      case 'warning':   return 'events-warn';
      case 'started':
      case 'running':
      case 'progress':  return 'events-running';
      default:          return '';
    }
  }

  return { init, setLab, onStatus, latest };
})();
