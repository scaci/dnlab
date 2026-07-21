/**
 * ConsolePanel – opens a node console in a browser window
 * dedicated window, no longer as an embedded popup.
 *
 * The window (`console.html`) is self-contained: xterm + WebSocket + cookie
 * di sessione ereditato dal tab principale. Questo modulo resta solo come
 * punto di ingresso (API compatibility con app.js / context_menu).
 *
 * API:
 *   ConsolePanel.init(_, _)             — noop, firma compat
 *   ConsolePanel.open(labId, nodeName)  — WindowManager.open('/console.html?…')
 *   ConsolePanel.openAll(labId)         — aggregated snapshot window
 *   ConsolePanel.close(nodeName)        — noop, the user closes the window
 */
const ConsolePanel = (() => {
  function init(_tabBarId, _termAreaId) {}

  function open(labId, nodeName) {
    if (!labId || !nodeName) return null;
    const url = `/console.html?lab=${encodeURIComponent(labId)}&node=${encodeURIComponent(nodeName)}`;
    return WindowManager.open(url, '_blank', { width: 1100, height: 720 });
  }

  function openAll(labId) {
    if (!labId) return null;
    const url = `/consoles.html?lab=${encodeURIComponent(labId)}`;
    return WindowManager.open(url, '_blank', { width: 1280, height: 820 });
  }

  function close(_nodeName) {}

  return { init, open, openAll, close };
})();
