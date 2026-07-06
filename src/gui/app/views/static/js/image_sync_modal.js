/**
 * ImageSyncModal — dashboard operativa dell'image-sync daemon.
 *
 * Poll `GET /api/image-sync/status` ogni 3s while the modal is open
 * e renderizza:
 *
 *   • KPI header: daemon status, last reconcile, interval, duration
 *   • filtri include/exclude
 *   • tabella worker (reachable, #images, missing, extra, last_sync, error)
 *   • list of images on the master (collapsible)
 *   • "Reconcile now" button → POST /api/image-sync/reconcile (SIGUSR1)
 *
 * Polling stops when the modal is closed o se il daemon
 * risulta irraggiungibile (available=false), for non spammare 503.
 */
const ImageSyncModal = (() => {
  let _pollTimer = null;
  let _body = null;
  let _lastUpdatedAt = null;     // used to highlight "just updated"
  let _expectingReconcile = false; // flag dopo trigger: attende bump di updated_at

  const POLL_INTERVAL_MS = 3000;

  async function show() {
    _body = document.createElement('div');
    _body.className = 'isync-modal';
    _body.innerHTML = `
      <div class="plan-loading">
        <div class="spinner"></div>
        <span>Loading status image-sync…</span>
      </div>
    `;
    showModal('Image sync', _body, [
      { label: 'Close', class: 'btn-secondary', action: () => _stopPolling() },
    ]);

    // Intercetta chiusura da overlay/esc: il bottone Close gestisce
    // the explicit case, but the user can click outside or press Esc.
    const overlay = document.getElementById('modal-overlay');
    const closeWatcher = () => {
      if (!overlay.classList.contains('active')) {
        _stopPolling();
        overlay.removeEventListener('transitionend', closeWatcher);
      }
    };
    overlay.addEventListener('transitionend', closeWatcher);

    await _refresh();
    _startPolling();
  }

  // ── Polling ────────────────────────────────────────────────────────

  function _startPolling() {
    _stopPolling();
    _pollTimer = setInterval(_refresh, POLL_INTERVAL_MS);
  }

  function _stopPolling() {
    if (_pollTimer) {
      clearInterval(_pollTimer);
      _pollTimer = null;
    }
  }

  async function _refresh() {
    let resp;
    try {
      resp = await API.Multinode.imageSyncStatus();
    } catch (e) {
      _renderError(e.message || String(e));
      _stopPolling();
      return;
    }

    if (!resp || !resp.available) {
      _renderUnavailable();
      _stopPolling();
      return;
    }

    const state = resp.state || {};
    // Se abbiamo appena triggerato un reconcile, riconosci quando
    // updated_at cambia e dai feedback.
    if (_expectingReconcile && state.updated_at && state.updated_at !== _lastUpdatedAt) {
      _expectingReconcile = false;
      if (typeof showToast === 'function') {
        showToast('Reconcile completato', 'success');
      }
    }
    _lastUpdatedAt = state.updated_at || null;
    _render(state);
  }

  // ── Rendering ──────────────────────────────────────────────────────

  function _renderError(msg) {
    if (!_body) return;
    _body.innerHTML = `
      <div class="plan-error">
        <strong>Cannot read daemon status:</strong>
        <pre>${_escape(msg)}</pre>
        <p class="plan-hint">
          Check that the service <code>dnlab-image-sync.service</code>
          sia attivo: <code>systemctl status dnlab-image-sync</code>.
        </p>
      </div>
    `;
  }

  function _renderUnavailable() {
    if (!_body) return;
    _body.innerHTML = `
      <div class="plan-warn">
        <strong>⚠ Image-sync daemon non raggiungibile</strong>
        <p class="plan-hint">
          The status file does not exist yet. Start the service with:
          <code>systemctl start dnlab-image-sync.service</code>,
          or wait for the first reconcile cycle (it may take
          qualche minuto dopo lo start).
        </p>
      </div>
    `;
  }

  function _render(state) {
    if (!_body) return;

    const master = state.master || {};
    const workers = state.workers || {};
    const filter = state.filter || {};
    const interval = state.interval_seconds || 0;
    const lastMs = state.last_reconcile_duration_ms || 0;
    const count = state.reconcile_count || 0;
    const updatedAt = state.updated_at || null;

    const workerCount = Object.keys(workers).length;
    const masterImgCount = Object.keys(master.images || {}).length;
    const unreachable = Object.values(workers).filter(w => !w.reachable).length;
    const totalMissing = Object.values(workers)
      .reduce((sum, w) => sum + ((w.missing || []).length), 0);

    const daemonPill = unreachable === 0
      ? '<span class="isync-pill isync-pill-ok">healthy</span>'
      : `<span class="isync-pill isync-pill-warn">${unreachable} unreachable</span>`;

    const kpiHTML = `
      <div class="plan-summary">
        <div class="plan-kpi">
          <div class="plan-kpi-num">${masterImgCount}</div>
          <div class="plan-kpi-lbl">master img</div>
        </div>
        <div class="plan-kpi">
          <div class="plan-kpi-num">${workerCount}</div>
          <div class="plan-kpi-lbl">worker</div>
        </div>
        <div class="plan-kpi">
          <div class="plan-kpi-num">${totalMissing}</div>
          <div class="plan-kpi-lbl">missing total</div>
        </div>
        <div class="plan-kpi">
          <div class="plan-kpi-num">${count}</div>
          <div class="plan-kpi-lbl">reconcile cnt</div>
        </div>
      </div>
    `;

    const headerHTML = `
      <div class="isync-header">
        <div class="isync-header-left">
          ${daemonPill}
          <span class="isync-meta">
            ultimo run: <strong>${_fmtRelative(updatedAt)}</strong>
            <span class="isync-dim">· durata ${lastMs}ms · intervallo ${interval}s</span>
          </span>
        </div>
        <button id="isync-btn-reconcile" class="btn btn-primary btn-sm">
          🔄 Reconcile now
        </button>
      </div>
    `;

    const incl = (filter.include || []).length
      ? (filter.include || []).map(p => `<code>${_escape(p)}</code>`).join(' ')
      : '<span class="isync-dim">(noo — tutte le images)</span>';
    const excl = (filter.exclude || []).length
      ? (filter.exclude || []).map(p => `<code>${_escape(p)}</code>`).join(' ')
      : '<span class="isync-dim">(noo)</span>';

    const filterHTML = `
      <div class="plan-section">
        <h4>Filtri</h4>
        <div class="isync-filter">
          <div><span class="isync-filter-lbl">include:</span> ${incl}</div>
          <div><span class="isync-filter-lbl">exclude:</span> ${excl}</div>
        </div>
      </div>
    `;

    const workerRows = Object.entries(workers).map(([name, w]) => {
      const reachable = w.reachable;
      const imgCount = Object.keys(w.images || {}).length;
      const miss = (w.missing || []).length;
      const extra = (w.extra || []).length;
      const err = w.last_error || '';
      const lastSync = _fmtRelative(w.last_sync_at);
      const missCell = miss > 0
        ? `<span class="isync-bad">${miss}</span>` : '0';
      const extraCell = extra > 0
        ? `<span class="isync-warn">${extra}</span>` : '0';
      const dot = reachable
        ? '<span class="isync-dot isync-dot-ok"></span>'
        : '<span class="isync-dot isync-dot-err"></span>';
      return `
        <tr>
          <td>${dot}${_escape(name)}</td>
          <td class="isync-host">${_escape(w.host || '—')}</td>
          <td class="isync-num">${imgCount}</td>
          <td class="isync-num">${missCell}</td>
          <td class="isync-num">${extraCell}</td>
          <td>${lastSync}</td>
          <td class="isync-err" title="${_escape(err)}">${_escape(_truncate(err, 40))}</td>
        </tr>
      `;
    }).join('');

    const workersHTML = `
      <div class="plan-section">
        <h4>Worker</h4>
        ${workerCount === 0
          ? '<p class="plan-empty">No worker configurato.</p>'
          : `<table class="isync-table">
              <thead><tr>
                <th>Nome</th><th>Host</th>
                <th class="isync-num">Img</th>
                <th class="isync-num">Missing</th>
                <th class="isync-num">Extra</th>
                <th>Ultimo sync</th>
                <th>Error</th>
              </tr></thead>
              <tbody>${workerRows}</tbody>
            </table>`
        }
      </div>
    `;

    const masterImages = Object.keys(master.images || {}).sort();
    const masterHTML = `
      <div class="plan-section">
        <h4>Images sul master (${_escape(master.host || '—')})</h4>
        ${masterImages.length === 0
          ? '<p class="plan-empty">Noa image matcha i filtri.</p>'
          : `<details class="isync-master">
              <summary>${masterImages.length} images</summary>
              <ul class="isync-img-list">
                ${masterImages.map(n => `<li><code>${_escape(n)}</code></li>`).join('')}
              </ul>
            </details>`
        }
      </div>
    `;

    _body.innerHTML = headerHTML + kpiHTML + filterHTML + workersHTML + masterHTML;

    // Wire-up Reconcile button
    const btn = _body.querySelector('#isync-btn-reconcile');
    if (btn) btn.addEventListener('click', _triggerReconcile);
  }

  // ── Reconcile trigger ──────────────────────────────────────────────

  async function _triggerReconcile() {
    const btn = _body?.querySelector('#isync-btn-reconcile');
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-inline"></span> Triggering…';
    }
    try {
      await API.Multinode.imageSyncReconcile();
      _expectingReconcile = true;
      if (typeof showToast === 'function') {
        showToast('Reconcile requested — attendo completamento…', 'info');
      }
      // immediate refresh to remove "stale" state
      await _refresh();
    } catch (e) {
      if (typeof showToast === 'function') {
        showToast('Trigger failed: ' + (e.message || e), 'error');
      }
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = '🔄 Reconcile now';
      }
    }
  }

  // ── Helpers ────────────────────────────────────────────────────────

  function _fmtRelative(iso) {
    if (!iso) return '—';
    const t = Date.parse(iso);
    if (isNaN(t)) return _escape(iso);
    const diff = (Date.now() - t) / 1000;
    if (diff < 0) return 'in futuro';
    if (diff < 60) return `${Math.floor(diff)}s fa`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m fa`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h fa`;
    return `${Math.floor(diff / 86400)}g fa`;
  }

  function _truncate(s, n) {
    s = String(s || '');
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  }

  function _escape(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  return { show };
})();
