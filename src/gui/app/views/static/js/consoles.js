/** Aggregated, snapshot-based console window. */
(() => {
  const params = new URLSearchParams(location.search);
  const labId = params.get('lab') || '';
  const labLabel = document.getElementById('consoles-lab');
  const summary = document.getElementById('consoles-summary');
  const tabBar = document.getElementById('console-tabs');
  const panes = document.getElementById('console-panes');
  const message = document.getElementById('consoles-message');
  const entries = new Map();
  let activeNode = null;

  if (!labId) {
    _showMessage('Missing lab parameter.');
    summary.textContent = 'error';
    return;
  }

  window.addEventListener('resize', () => _activeEntry()?.session.fit());
  window.addEventListener('pagehide', _disposeAll, { once: true });
  window.addEventListener('beforeunload', _disposeAll, { once: true });
  _loadSnapshot();

  async function _loadSnapshot() {
    try {
      const lab = await API.Labs.status(labId);
      const names = [...new Set((lab.containers || [])
        .filter(container => container.state === 'running' && container.node_name)
        .map(container => container.node_name))]
        .sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }));

      labLabel.textContent = lab.name || `lab ${labId.slice(0, 8)}…`;
      document.title = `Consoles · ${lab.name || 'dNLab'}`;
      if (!names.length) {
        summary.textContent = '0 live VDs';
        _showMessage('The runtime snapshot contains no live VD consoles.');
        _notifyOpener('Runtime status is stale or contains no live VD consoles', 'warn');
        return;
      }

      message.hidden = true;
      names.forEach(name => _addConsole(name)); // Eager: every session connects now.
      _activate(names[0]);
      _updateSummary();
    } catch (error) {
      summary.textContent = 'error';
      _showMessage(`Unable to load live VDs: ${error.message}`);
      _notifyOpener('Runtime status is stale or unavailable', 'warn');
    }
  }

  function _addConsole(nodeName) {
    const tab = document.createElement('button');
    tab.type = 'button';
    tab.className = 'console-tab';
    tab.dataset.node = nodeName;
    tab.dataset.status = 'connecting';
    tab.innerHTML = `
      <span class="console-tab-name"></span>
      <span class="console-tab-status">connecting</span>
      <span class="console-tab-close" role="button" aria-label="Close console" title="Close console">×</span>`;
    tab.querySelector('.console-tab-name').textContent = nodeName;

    const pane = document.createElement('section');
    pane.className = 'console-pane';
    pane.dataset.node = nodeName;
    const terminalHost = document.createElement('div');
    terminalHost.className = 'console-terminal';
    const reconnect = document.createElement('button');
    reconnect.type = 'button';
    reconnect.className = 'console-reconnect';
    reconnect.textContent = 'Reconnect';
    reconnect.hidden = true;
    pane.append(terminalHost, reconnect);
    tabBar.appendChild(tab);
    panes.appendChild(pane);

    const session = new ConsoleSession({
      labId,
      nodeName,
      container: terminalHost,
      onStatus: (status) => {
        tab.dataset.status = status;
        tab.querySelector('.console-tab-status').textContent = status;
        reconnect.hidden = status !== 'closed' && status !== 'error';
        _updateSummary();
      },
    });
    const entry = { nodeName, tab, pane, reconnect, session };
    entries.set(nodeName, entry);

    tab.addEventListener('click', (event) => {
      if (event.target.closest('.console-tab-close')) {
        event.stopPropagation();
        _removeConsole(nodeName);
        return;
      }
      _activate(nodeName);
    });
    reconnect.addEventListener('click', () => {
      reconnect.hidden = true;
      session.connect({ resetTerminal: true });
      if (activeNode === nodeName) requestAnimationFrame(() => session.fit());
    });

    session.connect();
  }

  function _activate(nodeName) {
    const entry = entries.get(nodeName);
    if (!entry) return;
    activeNode = nodeName;
    entries.forEach(item => {
      const active = item === entry;
      item.tab.classList.toggle('active', active);
      item.tab.setAttribute('aria-selected', active ? 'true' : 'false');
      item.pane.classList.toggle('active', active);
    });
    requestAnimationFrame(() => entry.session.fit());
  }

  function _removeConsole(nodeName) {
    const entry = entries.get(nodeName);
    if (!entry) return;
    const ordered = [...entries.keys()];
    const index = ordered.indexOf(nodeName);
    entry.session.dispose();
    entry.tab.remove();
    entry.pane.remove();
    entries.delete(nodeName);

    if (activeNode === nodeName) {
      activeNode = null;
      const next = ordered[index + 1] || ordered[index - 1];
      if (next && entries.has(next)) _activate(next);
    }
    if (!entries.size) _showMessage('All console tabs have been closed.');
    _updateSummary();
  }

  function _activeEntry() {
    return activeNode ? entries.get(activeNode) : null;
  }

  function _updateSummary() {
    const connected = [...entries.values()]
      .filter(entry => entry.tab.dataset.status === 'connected').length;
    summary.textContent = `${connected}/${entries.size} connected`;
  }

  function _showMessage(text) {
    message.hidden = false;
    message.textContent = text;
  }

  function _disposeAll() {
    entries.forEach(entry => entry.session.dispose());
    entries.clear();
  }

  function _notifyOpener(text, level) {
    try {
      if (window.opener && typeof window.opener.showToast === 'function') {
        window.opener.showToast(text, level);
      }
    } catch (_) {}
  }
})();
