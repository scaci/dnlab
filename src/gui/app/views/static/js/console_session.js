/**
 * ConsoleSession - reusable xterm/WebSocket lifecycle for VD consoles.
 *
 * A session owns exactly one terminal and one downstream WebSocket.  It is
 * used by both the standalone console window and the aggregated consoles
 * view.  Manual reconnects can replace the terminal so the relay replay is
 * not appended to output already shown before the disconnect.
 */
class ConsoleSession {
  constructor({ labId, nodeName, container, onStatus = null }) {
    this.labId = labId;
    this.nodeName = nodeName;
    this.container = container;
    this.onStatus = onStatus;
    this.terminal = null;
    this.fitAddon = null;
    this.socket = null;
    this._generation = 0;
    this._disposed = false;
    this._inputSubscription = null;
  }

  connect({ resetTerminal = false } = {}) {
    if (this._disposed) return;
    this._closeSocket('reconnecting');
    if (resetTerminal || !this.terminal) this._createTerminal();

    const generation = ++this._generation;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/ws/console/${encodeURIComponent(this.labId)}/${encodeURIComponent(this.nodeName)}`;
    const socket = new WebSocket(url);
    this.socket = socket;
    socket.binaryType = 'arraybuffer';
    let socketErrored = false;
    this._setStatus('connecting');

    socket.onopen = () => {
      if (!this._isCurrent(socket, generation)) return;
      this._setStatus('connected');
      this.terminal?.write(`\x1b[32mConnected to ${this.nodeName}\x1b[0m\r\n`);
    };
    socket.onmessage = (event) => {
      if (!this._isCurrent(socket, generation)) return;
      const data = event.data instanceof ArrayBuffer
        ? new Uint8Array(event.data)
        : event.data;
      this.terminal?.write(data);
    };
    socket.onerror = () => {
      if (!this._isCurrent(socket, generation)) return;
      socketErrored = true;
      this._setStatus('error');
    };
    socket.onclose = (event) => {
      if (!this._isCurrent(socket, generation)) return;
      this.socket = null;
      const authFail = event.code === 4401;
      this._setStatus(authFail || socketErrored ? 'error' : 'closed', {
        detail: authFail ? 'unauthorized' : '',
      });
      const message = authFail
        ? 'Session expired — reload the page after logging in'
        : 'Connection closed';
      this.terminal?.write(`\r\n\x1b[31m[${message}]\x1b[0m\r\n`);
    };
  }

  fit() {
    try { this.fitAddon?.fit(); } catch (_) {}
  }

  close(reason = 'console closed') {
    this._generation += 1;
    this._closeSocket(reason);
    this._setStatus('closed');
  }

  dispose() {
    if (this._disposed) return;
    this.close('console closed');
    this._disposed = true;
    try { this._inputSubscription?.dispose(); } catch (_) {}
    this._inputSubscription = null;
    try { this.terminal?.dispose(); } catch (_) {}
    this.terminal = null;
    this.fitAddon = null;
    if (this.container) this.container.replaceChildren();
  }

  _createTerminal() {
    try { this._inputSubscription?.dispose(); } catch (_) {}
    try { this.terminal?.dispose(); } catch (_) {}
    if (this.container) this.container.replaceChildren();

    this.terminal = new Terminal({
      cursorBlink: true,
      fontSize: 14,
      fontFamily: "'Fira Mono', 'Cascadia Code', monospace",
      theme: {
        background: '#0d0d1a',
        foreground: '#e0e0e0',
        cursor: '#00d4ff',
      },
    });
    this.fitAddon = new FitAddon.FitAddon();
    this.terminal.loadAddon(this.fitAddon);
    this.terminal.open(this.container);
    this._inputSubscription = this.terminal.onData((data) => {
      if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(data);
    });
    this.fit();
  }

  _closeSocket(reason) {
    const socket = this.socket;
    this.socket = null;
    if (!socket) return;
    socket.onopen = null;
    socket.onmessage = null;
    socket.onerror = null;
    socket.onclose = null;
    try {
      if (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING) {
        socket.close(1000, reason);
      }
    } catch (_) {}
  }

  _isCurrent(socket, generation) {
    return !this._disposed && this.socket === socket && this._generation === generation;
  }

  _setStatus(status, extra = {}) {
    if (typeof this.onStatus === 'function') {
      this.onStatus(status, extra);
    }
  }
}
