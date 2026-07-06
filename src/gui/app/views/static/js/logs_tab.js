/**
 * Standalone logs window: aperta da LogsPanel.open().
 * Come console_tab.js ma con un viewer testuale scrollabile invece di
 * xterm — auto-scroll until the user interrupts it by scrolling up.
 */
(() => {
  const params = new URLSearchParams(location.search);
  const labId = params.get('lab') || '';
  const nodeName = params.get('node') || '';

  const hdrNode = document.getElementById('hdr-node');
  const hdrLab  = document.getElementById('hdr-lab');
  const hdrStat = document.getElementById('hdr-status');
  const logEl   = document.getElementById('log');

  hdrNode.textContent = `📋 ${nodeName || '(no node)'}`;
  hdrLab.textContent  = labId ? `lab ${labId.slice(0, 8)}…` : '';
  document.title = `Logs · ${nodeName || 'dNLab'}`;

  if (!labId || !nodeName) {
    _setStatus('err', 'parametri mancanti');
    return;
  }

  let autoScroll = true;
  logEl.addEventListener('scroll', () => {
    const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 8;
    autoScroll = atBottom;
  });

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const wsUrl = `${proto}://${location.host}/ws/logs/${labId}/${encodeURIComponent(nodeName)}`;
  const ws = new WebSocket(wsUrl);

  ws.onopen = () => _setStatus('ok', 'connected');
  ws.onmessage = (ev) => {
    _appendLine(ev.data);
    if (autoScroll) logEl.scrollTop = logEl.scrollHeight;
  };
  ws.onclose = (ev) => {
    const authFail = ev.code === 4401;
    _setStatus('err', authFail ? 'unauthorized' : 'closed');
    _appendLine(
      authFail
        ? '[Session expired — reload the page after logging in]'
        : '[WebSocket closed]',
      'log-line-warn',
    );
  };
  ws.onerror = () => {
    _setStatus('err', 'error');
    _appendLine('[Error WebSocket]', 'log-line-err');
  };

  function _appendLine(text, extraClass = '') {
    const line = document.createElement('div');
    line.className = `log-line ${extraClass}`.trim();
    line.textContent = String(text).replace(/\r?\n$/, '');
    logEl.appendChild(line);
  }

  function _setStatus(cls, text) {
    hdrStat.className = `status ${cls || ''}`;
    hdrStat.textContent = text;
  }
})();
