/**
 * Sidebar module – device palette loaded from Docker images.
 *
 * Usage:
 *   Sidebar.init(containerId)
 *   Sidebar.load()         → fetches images from backend
 *   Sidebar.on('place', cb)  → cb({ kind, image, vendor })
 */
const Sidebar = (() => {
  let _container = null;
  let _pendingDevice = null;
  let _syncState = null;       // from the image-sync daemon
  let _syncTimer = null;
  const SYNC_POLL_MS = 30_000;
  const listeners = {};

  function init(containerId) {
    _container = document.getElementById(containerId);
    _renderSkeleton();
  }

  async function load() {
    try {
      // Fetch sync state in parallel with images: badges are computed during
      // render.
      const [images, sync] = await Promise.all([
        API.Docker.networkImages(),
        API.Multinode.imageSyncStatus().catch(() => ({ available: false })),
      ]);
      _syncState = sync && sync.available ? sync.state : null;
      _render(images);
      _startSyncPolling();
    } catch (e) {
      console.error('Sidebar load error:', e);
      _container.innerHTML = `<p class="sidebar-error">Error loading images: ${e.message}</p>`;
    }
  }

  function _startSyncPolling() {
    if (_syncTimer) return;
    _syncTimer = setInterval(async () => {
      try {
        const sync = await API.Multinode.imageSyncStatus();
        _syncState = sync && sync.available ? sync.state : null;
        _refreshBadges();
      } catch (_) { /* ignore transient errors */ }
    }, SYNC_POLL_MS);
  }

  // Treat the published state as stale if the daemon has not reconciled for
  // more than 3x the configured interval (default 5 min -> 15 min threshold).
  // A stale state file is indistinguishable from "all green" as data, but the
  // daemon is probably dead.
  function _syncIsStale(state) {
    if (!state || !state.updated_at) return true;
    const interval = (state.interval_seconds || 300) * 1000;
    const age = Date.now() - new Date(state.updated_at).getTime();
    return age > interval * 3;
  }

  // Classify the sync status of a single image across workers.
  // Return { cls, title } — cls ∈ {'ok','pending','error','stale','none'}.
  function _imageSyncStatus(fullName) {
    if (!_syncState) return { cls: 'none', title: 'image-sync unavailable' };
    if (_syncIsStale(_syncState)) {
      const ts = _syncState.updated_at || '?';
      return {
        cls: 'stale',
        title: `image-sync daemon stopped (last reconcile ${ts}) — data is unreliable`,
      };
    }
    const workers = _syncState.workers || {};
    const names = Object.keys(workers);
    if (names.length === 0) return { cls: 'none', title: 'no worker' };
    const unreachable = [];
    const missing = [];
    const errord = [];
    for (const n of names) {
      const w = workers[n];
      if (!w.reachable) { unreachable.push(n); continue; }
      if (w.last_error) errord.push(n);
      if ((w.missing || []).includes(fullName)) missing.push(n);
    }
    if (unreachable.length || errord.length) {
      return {
        cls: 'error',
        title: `KO on ${[...unreachable, ...errord].join(', ')}`,
      };
    }
    if (missing.length) {
      return { cls: 'pending', title: `missing on ${missing.join(', ')}` };
    }
    return { cls: 'ok', title: `sync OK on ${names.join(', ')}` };
  }

  function _refreshBadges() {
    document.querySelectorAll('.device-card[data-image]').forEach(card => {
      const badge = card.querySelector('.sync-badge');
      if (!badge) return;
      const s = _imageSyncStatus(card.dataset.image);
      badge.className = `sync-badge sync-${s.cls}`;
      badge.title = s.title;
    });
  }

  function getPendingDevice() { return _pendingDevice; }
  function clearPending()     { _pendingDevice = null; }

  // ── Rendering ────────────────────────────────────────────────────────
  function _renderSkeleton() {
    _container.innerHTML = `
      <div class="sidebar-search-wrap">
        <input id="sidebar-search" class="sidebar-search" type="text" placeholder="Filter devices…">
      </div>
      <div id="sidebar-list" class="sidebar-list"></div>
    `;
    document.getElementById('sidebar-search').addEventListener('input', e => {
      _filterItems(e.target.value.toLowerCase());
    });
  }

  function _render(images) {
    const list = document.getElementById('sidebar-list');
    list.innerHTML = '';

    // ── Infrastructure group at the top ─────────────────────────────
    // Non-device objects that the GUI can place on the canvas (currently only
    // the mgmt network OOB object).
    list.appendChild(_makeInfrastructureSection());

    // Group by vendor
    const groups = {};
    images.forEach(img => {
      const v = img.vendor || 'generic';
      if (!groups[v]) groups[v] = [];
      groups[v].push(img);
    });

    for (const [vendor, items] of Object.entries(groups)) {
      const section = document.createElement('div');
      section.className = 'sidebar-group';
      section.innerHTML = `<div class="sidebar-group-header">${_vendorTitle(vendor)}</div>`;

      items.forEach(img => {
        const card = _makeCard(img);
        section.appendChild(card);
      });

      list.appendChild(section);
    }
  }

  function _makeInfrastructureSection() {
    const section = document.createElement('div');
    section.className = 'sidebar-group sidebar-group-infra';
    section.innerHTML = `<div class="sidebar-group-header">Infrastructure</div>`;
    section.appendChild(_makeMgmtCard());
    section.appendChild(_makeRealNetCard());
    return section;
  }

  function _makeMgmtCard() {
    const card = document.createElement('div');
    card.className = 'device-card device-card-infra';
    card.dataset.kind = '_mgmt';
    card.style.borderLeftColor = DeviceCatalog.vendorColor('generic');
    card.innerHTML = `
      <div class="device-icon" style="background:${DeviceCatalog.vendorColor('generic')}">
        <img src="${DeviceCatalog.icon('oob') || 'img/devices/oob.svg'}" class="kind-icon" alt="">
      </div>
      <div class="device-info">
        <div class="device-kind">Mgmt Network</div>
        <div class="device-image">subnet + gateway</div>
      </div>
    `;
    const select = () => {
      document.querySelectorAll('.device-card').forEach(c => c.classList.remove('selected'));
      card.classList.add('selected');
      _pendingDevice = { kind: '_mgmt', image: '', vendor: 'generic' };
      _emit('device-selected', _pendingDevice);
      showToast('Click/drag on the canvas to place the mgmt network', 'info');
    };
    card.addEventListener('click', select);
    card.draggable = true;
    card.addEventListener('dragstart', e => {
      _pendingDevice = { kind: '_mgmt', image: '', vendor: 'generic' };
      e.dataTransfer.setData('text/plain', JSON.stringify(_pendingDevice));
    });
    return card;
  }

  function _makeRealNetCard() {
    const card = document.createElement('div');
    card.className = 'device-card device-card-infra';
    card.dataset.kind = '_real_net';
    card.style.borderLeftColor = DeviceCatalog.vendorColor('generic');
    card.innerHTML = `
      <div class="device-icon" style="background:${DeviceCatalog.vendorColor('generic')}">
        <img src="${DeviceCatalog.icon('cloud') || 'img/devices/cloud.svg'}" class="kind-icon" alt="">
      </div>
      <div class="device-info">
        <div class="device-kind">Real Network</div>
        <div class="device-image">NAT / BGP gateway</div>
      </div>
    `;
    const select = () => {
      document.querySelectorAll('.device-card').forEach(c => c.classList.remove('selected'));
      card.classList.add('selected');
      _pendingDevice = { kind: '_real_net', image: '', vendor: 'generic' };
      _emit('device-selected', _pendingDevice);
      showToast('Click/drag on the canvas to place a real network', 'info');
    };
    card.addEventListener('click', select);
    card.draggable = true;
    card.addEventListener('dragstart', e => {
      _pendingDevice = { kind: '_real_net', image: '', vendor: 'generic' };
      e.dataTransfer.setData('text/plain', JSON.stringify(_pendingDevice));
    });
    return card;
  }

  function _makeCard(img) {
    const card = document.createElement('div');
    card.className = 'device-card';
    card.dataset.kind   = img.kind;
    card.dataset.image  = img.full_name || `${img.repository}:${img.tag}`;
    card.dataset.vendor = img.vendor;
    card.style.borderLeftColor = vendorColor(img.vendor);

    const sync = _imageSyncStatus(card.dataset.image);
    card.innerHTML = `
      <div class="device-icon" style="background:${vendorColor(img.vendor)}">

        <img src="${_kindIcon(img.kind)}"
             class="kind-icon">

      </div>
      <div class="device-info">
        <div class="device-kind">${kindLabel(img.kind)}</div>
        <div class="device-image">${img.tag}</div>
      </div>
      <span class="sync-badge sync-${sync.cls}" title="${sync.title}"></span>
    `;

    card.addEventListener('click', () => {
      document.querySelectorAll('.device-card').forEach(c => c.classList.remove('selected'));
      card.classList.add('selected');
      _pendingDevice = {
        kind:   img.kind,
        image:  img.full_name || `${img.repository}:${img.tag}`,
        vendor: img.vendor,
      };
      _emit('device-selected', _pendingDevice);
      showToast(`Click on canvas to place: ${kindLabel(img.kind)}`, 'info');
    });

    // HTML5 drag-and-drop
    card.draggable = true;
    card.addEventListener('dragstart', e => {
      _pendingDevice = {
        kind:   img.kind,
        image:  img.full_name || `${img.repository}:${img.tag}`,
        vendor: img.vendor,
      };
      e.dataTransfer.setData('text/plain', JSON.stringify(_pendingDevice));
    });

    return card;
  }

  function _filterItems(query) {
    document.querySelectorAll('.device-card').forEach(card => {
      const text = (card.dataset.kind + card.dataset.image).toLowerCase();
      card.style.display = text.includes(query) ? '' : 'none';
    });
    document.querySelectorAll('.sidebar-group').forEach(g => {
      const visible = [...g.querySelectorAll('.device-card')].some(c => c.style.display !== 'none');
      g.style.display = visible ? '' : 'none';
    });
  }

  // Titoli vendor dal DeviceCatalog (config/devices.json).
  function _vendorTitle(vendor) { return DeviceCatalog.vendorTitle(vendor); }

  function _vendorInitial(vendor) {
    return (vendor || '?')[0].toUpperCase();
  }

  function on(event, cb) {
    if (!listeners[event]) listeners[event] = [];
    listeners[event].push(cb);
  }

  function _emit(event, data) {
    (listeners[event] || []).forEach(cb => cb(data));
  }

  return { init, load, getPendingDevice, clearPending, on };
})();
