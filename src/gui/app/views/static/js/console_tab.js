/**
 * Standalone console window: aperta da ConsolePanel.open().
 * Read lab/node from the query string, open xterm + WebSocket, keep the
 * session for the whole tab lifetime. The session cookie is inherited
 * same-origin, so auth on the WS handshake is transparent.
 */
(() => {
  const params = new URLSearchParams(location.search);
  const labId = params.get('lab') || '';
  const nodeName = params.get('node') || '';

  const hdrNode = document.getElementById('hdr-node');
  const hdrLab  = document.getElementById('hdr-lab');
  const hdrStat = document.getElementById('hdr-status');

  hdrNode.textContent = `⬡ ${nodeName || '(no node)'}`;
  hdrLab.textContent  = labId ? `lab ${labId.slice(0, 8)}…` : '';
  document.title = `Console · ${nodeName || 'dNLab'}`;

  if (!labId || !nodeName) {
    _setStatus('err', 'missing parameters');
    return;
  }

  const term = new Terminal({
    cursorBlink: true,
    fontSize: 14,
    fontFamily: "'Fira Mono', 'Cascadia Code', monospace",
    theme: {
      background: '#0d0d1a',
      foreground: '#e0e0e0',
      cursor:     '#00d4ff',
    },
  });
  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(document.getElementById('term'));
  fitAddon.fit();

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const wsUrl = `${proto}://${location.host}/ws/console/${labId}/${encodeURIComponent(nodeName)}`;
  const ws = new WebSocket(wsUrl);
  ws.binaryType = 'arraybuffer';
  let closing = false;

  ws.onopen = () => {
    _setStatus('ok', 'connected');
    term.write(`\x1b[32mConnected to ${nodeName}\x1b[0m\r\n`);
  };
  ws.onmessage = (ev) => {
    const data = ev.data instanceof ArrayBuffer
      ? new Uint8Array(ev.data)
      : ev.data;
    term.write(data);
  };
  ws.onclose = (ev) => {
    const authFail = ev.code === 4401;
    _setStatus('err', authFail ? 'unauthorized' : 'closed');
    term.write(`\r\n\x1b[31m[${authFail ? 'Session expired — reload the page after logging in' : 'Connection closed'}]\x1b[0m\r\n`);
  };
  ws.onerror = () => {
    _setStatus('err', 'error');
  };

  term.onData(data => {
    if (ws.readyState === WebSocket.OPEN) ws.send(data);
  });

  // Resizing: xterm needs to be re-fitted when the viewport changes.
  window.addEventListener('resize', () => {
    try { fitAddon.fit(); } catch (_) {}
  });
  window.addEventListener('pagehide', _closeConsoleSocket);
  window.addEventListener('beforeunload', _closeConsoleSocket);

  function _setStatus(cls, text) {
    hdrStat.className = `status ${cls || ''}`;
    hdrStat.textContent = text;
  }

  function _closeConsoleSocket() {
    if (closing) return;
    closing = true;
    try {
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
        ws.close(1000, 'console window closed');
      }
    } catch (_) {}
  }
})();
