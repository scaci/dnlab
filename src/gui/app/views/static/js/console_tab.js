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

  const session = new ConsoleSession({
    labId,
    nodeName,
    container: document.getElementById('term'),
    onStatus: (status, extra) => {
      const label = extra?.detail || status;
      _setStatus(status === 'connected' ? 'ok' : status === 'connecting' ? '' : 'err', label);
    },
  });
  session.connect();

  // Resizing: xterm needs to be re-fitted when the viewport changes.
  window.addEventListener('resize', () => {
    session.fit();
  });
  window.addEventListener('pagehide', () => session.dispose(), { once: true });
  window.addEventListener('beforeunload', () => session.dispose(), { once: true });

  function _setStatus(cls, text) {
    hdrStat.className = `status ${cls || ''}`;
    hdrStat.textContent = text;
  }
})();
