/**
 * JumphostBox — fixed toolbar panel that shows SSH access for the
 * currently open lab bastion when the lab is deployed.
 *
 * The state comes from the `status-live` endpoint (`infra.jumphost` field),
 * with ``lab_id`` added by the shell for password lookup:
 *   {
 *     container:   "dnlab-<lab>-jumphost",
 *     host:        "master",
 *     mgmt_ip:     "192.168.200.X",
 *     ext_ip:      "192.168.100.254",  ← IP on the shared jumphost network
 *     ssh_port:    2201,                ← published port on the master (0 if inactive)
 *     ssh_bind_ip: "0.0.0.0",
 *     running:     true,
 *     lab_id:      "<uuid>"
 *   }
 *
 * The box exposes:
 *   • the masked SSH command in a clickable <code> + copy button,
 *   • a clickable "passwd" span that fetches+copies the password.
 *
 * If ``ssh_port`` is set, prefer ``ssh-keygen -R "[host]:<port>"`` followed by
 * ``ssh -p <port>
 * labuser@<gui-host>`` because it is reachable from the user's PC. Fall
 * back to the old ``ssh labuser@<ext_ip>`` master-local path if the port
 * is missing.
 */
const JumphostBox = (() => {
  let _rootEl = null;
  let _info = null;

  function init(elId = 'jumphost-box') {
    _rootEl = document.getElementById(elId);
    if (!_rootEl) return;
    _rootEl.style.display = 'none';
  }

  /**
   * Receive the `status.infra.jumphost` blob (or null) and update the DOM.
   */
  function update(info) {
    _info = info || null;
    if (!_rootEl) return;
    if (!info || !info.container) {
      _rootEl.style.display = 'none';
      _rootEl.innerHTML = '';
      return;
    }

    const ssh = _sshCommand(info);
    const running = info.running !== false;
    const dotClass = running ? 'jh-dot-ok' : 'jh-dot-err';
    const statusLabel = running ? 'up' : 'down';

    _rootEl.style.display = 'inline-flex';
    _rootEl.innerHTML = `
      <span class="jh-dot ${dotClass}" title="bastion ${statusLabel}"></span>
      <span class="jh-label">BASTION</span>
      <code class="jh-cmd" title="Copy SSH command">JUMPHOST</code>
      <button class="jh-copy" title="Copy SSH command">📋</button>
      <span class="jh-pwd" title="Copy the bastion password to clipboard">passwd</span>
    `;
    _rootEl.querySelector('.jh-copy').addEventListener('click', (e) => {
      e.stopPropagation();
      _copySshCommand(ssh);
    });
    _rootEl.querySelector('.jh-cmd').addEventListener('click', (e) => {
      e.stopPropagation();
      _copySshCommand(ssh);
    });
    _rootEl.querySelector('.jh-pwd').addEventListener('click', (e) => {
      e.stopPropagation();
      _copyPassword();
    });
  }

  /**
   * Hide the box, used on destroy or topology changes.
   */
  function clear() { update(null); }

  function getInfo() { return _info; }

  // ── Helpers ─────────────────────────────────────────────────────────

  function _sshCommand(info) {
    // If the published port on the master is known, prefer it because
    // directly reachable from the user PC through the GUI host.
    if (info.ssh_port && info.ssh_port > 0) {
      const host = window.location.hostname;
      return `ssh-keygen -R "[${host}]:${info.ssh_port}"\nssh -p ${info.ssh_port} labuser@${host}`;
    }
    const ip = (info.ext_ip || info.mgmt_ip || '').split('/')[0];
    return ip ? `ssh-keygen -R ${ip}\nssh labuser@${ip}` : `docker exec -it ${info.container} bash`;
  }

  function _copy(text, label, options = {}) {
    // Prefer the modern API; fall back to a temporary textarea
    // (also works in non-HTTPS contexts where `clipboard` is blocked).
    const msg = label || `Copied: ${text}`;
    const done = (ok) => {
      if (typeof showToast === 'function') {
        if (ok) {
          showToast(msg, 'success');
        } else if (!options.silentFailure) {
          showToast('Copy failed', 'error');
        }
      }
      return ok;
    };
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text)
        .then(() => done(true), () => _fallbackCopy(text, done));
    }
    return Promise.resolve(_fallbackCopy(text, done));
  }

  async function _copySshCommand(ssh) {
    const ok = await _copy(ssh, 'SSH command copied', { silentFailure: true });
    if (!ok) _showCommandModal(ssh);
  }

  async function _copyPassword() {
    if (!_info || !_info.lab_id) return;
    try {
      const res = await API.Labs.jumphostPassword(_info.lab_id);
      const pw  = res && res.password;
      if (!pw) throw new Error('password unavailable');
      const ok = await _copy(pw, 'Bastion password copied', { silentFailure: true });
      if (!ok) _showPasswordModal(pw);
    } catch (err) {
      if (typeof showToast === 'function') {
        showToast(`Password retrieval error: ${err.message || err}`, 'error');
      }
    }
  }

  function _fallbackCopy(text, done) {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.top = '0';
      ta.style.left = '0';
      ta.style.width = '1px';
      ta.style.height = '1px';
      ta.style.opacity = '0.01';
      document.body.appendChild(ta);
      ta.focus({ preventScroll: true });
      ta.select();
      ta.setSelectionRange(0, ta.value.length);
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return done(ok);
    } catch (_) { return done(false); }
  }

  function _showPasswordModal(password) {
    if (typeof showModal !== 'function') return;
    const body = document.createElement('div');
    body.className = 'jh-password-modal';
    body.innerHTML = `
      <label>Bastion password
        <input class="props-input jh-password-value" type="text" readonly value="${_escape(password)}">
      </label>
      <div class="admin-form-actions">
        <button class="btn btn-primary btn-sm jh-password-inline-copy" type="button">Copy</button>
      </div>
    `;
    showModal('Bastion password', body, []);
    const input = body.querySelector('.jh-password-value');
    if (input) input.select();
    body.querySelector('.jh-password-inline-copy')?.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (input) {
        input.focus({ preventScroll: true });
        input.select();
        input.setSelectionRange(0, input.value.length);
      }
      _copyVisiblePassword(password, input).then((ok) => {
        if (ok && typeof hideModal === 'function') hideModal();
      });
    });
  }

  function _showCommandModal(command) {
    if (typeof showModal !== 'function') return;
    const body = document.createElement('div');
    body.className = 'jh-command-modal';
    body.innerHTML = `
      <label>SSH command
        <textarea class="props-input jh-command-value" rows="3" readonly>${_escape(command)}</textarea>
      </label>
      <div class="admin-form-actions">
        <button class="btn btn-primary btn-sm jh-command-inline-copy" type="button">Copy</button>
      </div>
    `;
    showModal('SSH command', body, []);
    const input = body.querySelector('.jh-command-value');
    if (input) input.select();
    body.querySelector('.jh-command-inline-copy')?.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (input) {
        input.focus({ preventScroll: true });
        input.select();
        input.setSelectionRange(0, input.value.length);
      }
      _copyVisibleCommand(command, input).then((ok) => {
        if (ok && typeof hideModal === 'function') hideModal();
      });
    });
  }

  function _copyVisibleCommand(command, input) {
    const done = (ok) => {
      if (typeof showToast === 'function') {
        showToast(ok ? 'SSH command copied' : 'Copy failed', ok ? 'success' : 'error');
      }
      return ok;
    };

    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(command)
        .then(() => done(true), () => Promise.resolve(_copySelectedInput(input, done)));
    }
    return Promise.resolve(_copySelectedInput(input, done));
  }

  function _copyVisiblePassword(password, input) {
    const done = (ok) => {
      if (typeof showToast === 'function') {
        showToast(ok ? 'Bastion password copied' : 'Copy failed', ok ? 'success' : 'error');
      }
      return ok;
    };

    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(password)
        .then(() => done(true), () => Promise.resolve(_copySelectedInput(input, done)));
    }
    return Promise.resolve(_copySelectedInput(input, done));
  }

  function _copySelectedInput(input, done) {
    try {
      if (!input) return done(false);
      input.focus({ preventScroll: true });
      input.select();
      input.setSelectionRange(0, input.value.length);
      return done(document.execCommand('copy'));
    } catch (_) {
      return done(false);
    }
  }

  function _escape(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  return { init, update, clear, getInfo };
})();
