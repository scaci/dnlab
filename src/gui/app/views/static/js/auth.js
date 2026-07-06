/**
 * AuthGate — login overlay + whoami gate.
 *
 * The backend proxy can't know whether a user is logged in (the auth
 * cookie is opaque), so the SPA runs an identity probe on boot. If
 * whoami returns 401 we show a modal overlay that only clears when
 * login succeeds. A 401 on any subsequent API call re-raises the
 * overlay via the `auth:unauthorized` DOM event.
 *
 * Two backends are special-cased in the UI:
 *   - local_db: show username/password form.
 *   - basic_auth: skip form, show an "already authenticated by proxy"
 *     banner. (The proxy prompted the user and injected X-Remote-User.)
 */
const AuthGate = (() => {
  let _user = null;
  let _overlayEl = null;
  // Overlay in progress: ensures _showOverlay is idempotent, preventing duplicate overlays
  // from initial probe and synchronous auth:unauthorized events.
  let _overlayPromise = null;

  /**
   * Resolve the current user, blocking (= returning a pending promise)
   * until authentication succeeds. Called once at app start.
   */
  async function ensureAuthenticated() {
    try {
      _user = await API.Auth.whoami();
    } catch (e) {
      // Any whoami failure — 401 or transport error — shows the form.
      _user = await _showOverlay();
    }
    // Reactive 401 handler wired **after** the initial probe: if the probe fails,
    // the synchronous dispatch of `auth:unauthorized` from api.js would occur
    // before the catch above reaches the form, potentially creating a second overlay
    // on top of the first one.
    window.addEventListener('auth:unauthorized', _onUnauthorized);
    return _user;
  }

  function renderUserBadge(me) {
    const holder = document.getElementById('user-badge');
    if (!holder) return;
    const role = me.role || '?';
    holder.innerHTML = `
      <span class="user-badge-name">${_esc(me.username)}</span>
      <span class="user-badge-role role-${_esc(role)}">${_esc(role)}</span>
    `;
    holder.title = `Backend: ${me.backend || '?'}`;
  }

  async function logout() {
    try {
      await API.Auth.logout();
    } catch (_) {}
    _user = null;
    // Full reload is the cleanest way: every open WebSocket gets
    // closed by the browser and module state is wiped.
    window.location.reload();
  }

  function currentUser() { return _user; }

  // ── Overlay ───────────────────────────────────────────────────────────

  function _onUnauthorized() {
    // Lost session mid-flight. Show the overlay; do not reload — the
    // user may have unsaved canvas state. _showOverlay() is idempotent,
    // so if an overlay is already in flight (e.g. initial login), attach to
    // the same promise instead of creating a duplicate.
    _showOverlay().then(u => {
      _user = u;
      renderUserBadge(u);
    });
  }

  function _showOverlay() {
    if (_overlayPromise) return _overlayPromise;
    _overlayPromise = new Promise((resolve) => {
      const ov = document.createElement('div');
      ov.id = 'login-overlay';
      ov.className = 'login-overlay';
      ov.innerHTML = `
        <div class="login-card">
          <div class="login-header">
            <img class="login-brand" src="/img/brand/06-mark-dark.svg" alt="dNLab">
            <h1>dNLab GUI</h1>
            <p class="login-sub">Operator access</p>
          </div>
          <form id="login-form" class="login-form">
            <label>Username
              <input id="login-username" type="text" autocomplete="username"
                     autocapitalize="none" autocorrect="off" spellcheck="false"
                     required>
            </label>
            <label>Password
              <input id="login-password" type="password"
                     autocomplete="current-password" required>
            </label>
            <div id="login-error" class="login-error" hidden></div>
            <button type="submit" class="btn btn-primary btn-block">Sign in</button>
          </form>
        </div>
      `;
      document.body.appendChild(ov);
      _overlayEl = ov;

      const form = ov.querySelector('#login-form');
      const errEl = ov.querySelector('#login-error');
      const userEl = ov.querySelector('#login-username');
      const passEl = ov.querySelector('#login-password');
      setTimeout(() => userEl.focus(), 40);

      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        errEl.hidden = true;
        const submitBtn = form.querySelector('button[type="submit"]');
        submitBtn.disabled = true;
        try {
          const me = await API.Auth.login(userEl.value.trim(), passEl.value);
          _removeOverlay();
          resolve(me);
        } catch (err) {
          const msg = (err.message || '').startsWith('401')
            ? 'Invalid credentials'
            : `Login failed: ${err.message || err}`;
          errEl.textContent = msg;
          errEl.hidden = false;
          passEl.value = '';
          passEl.focus();
        } finally {
          submitBtn.disabled = false;
        }
      });
    });
    return _overlayPromise;
  }

  function _removeOverlay() {
    if (_overlayEl) {
      _overlayEl.remove();
      _overlayEl = null;
    }
    _overlayPromise = null;
  }

  function _esc(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  return { ensureAuthenticated, renderUserBadge, logout, currentUser };
})();
