/**
 * UsersModal — admin-only user management dashboard.
 *
 * Reached from the "👥 Users" toolbar button (shown only when the
 * logged-in user has role=admin). The FastAPI endpoints behind this
 * are gated by `require_role(Role.admin)`; a non-admin hitting /api/users/
 * directly gets 403 regardless of whether the button renders.
 *
 * Flow:
 *   - Shows a table: id / username / role / backend / active / last login
 *   - Per-row actions (as icon buttons):
 *       • cambia ruolo   (role select + save)
 *       • reset password (prompt for new pw)
 *       • toggle attivo  (enable/disable)
 *       • elimina        (with confirm)
 *   - Header "+ Add user" button opens an inline form that POSTs and
 *     re-renders the table.
 *
 * Safety rails are enforced server-side — this UI just surfaces the
 * resulting 400/409 messages as toasts. No client-side sneaky checks.
 */
const UsersModal = (() => {
  const ROLES = ['admin', 'graduate', 'assistant', 'student', 'rookie'];
  let _body = null;

  async function show() {
    _body = document.createElement('div');
    _body.className = 'users-modal';
    _body.innerHTML = `
      <div class="users-loading">
        <div class="spinner"></div><span>Loading users…</span>
      </div>
    `;
    showModal('User management', _body, [
      { label: 'Close', class: 'btn-secondary' },
    ]);
    await _render();
  }

  async function _render() {
    let users;
    try {
      users = await API.Users.list();
    } catch (e) {
      _body.innerHTML = `<div class="plan-error">
        <strong>Cannot load the user list:</strong>
        <pre>${_esc(e.message || e)}</pre>
      </div>`;
      return;
    }

    const me = AuthGate.currentUser();
    const myUsername = me?.username;

    const header = `
      <div class="users-header">
        <button id="users-add-btn" class="btn btn-primary btn-sm">
          ＋ Add user
        </button>
        <div id="users-add-form" class="users-add-form" hidden></div>
      </div>
    `;

    const tableRows = users.map(u => _renderRow(u, myUsername)).join('');
    const table = `
      <table class="users-table">
        <thead><tr>
          <th>id</th><th>username</th><th>ruolo</th>
          <th>backend</th><th>attivo</th><th>ultimo login</th>
          <th class="users-actions-col">azioni</th>
        </tr></thead>
        <tbody>${tableRows}</tbody>
      </table>
    `;

    _body.innerHTML = header + table;
    _body.querySelector('#users-add-btn').addEventListener('click', _toggleAddForm);
    _wireRowActions();
  }

  function _renderRow(u, myUsername) {
    const isSelf = u.username === myUsername;
    const isFederated = u.backend !== 'local_db';
    const rolePill = `<span class="role-pill role-${_esc(u.role)}">${_esc(u.role)}</span>`;
    const activePill = u.is_active
      ? '<span class="users-pill users-pill-ok">yes</span>'
      : '<span class="users-pill users-pill-off">no</span>';
    const last = u.last_login_at ? _fmtRelative(u.last_login_at) : '—';
    const selfTag = isSelf ? ' <span class="users-self">(tu)</span>' : '';

    // Federated users can only have role / is_active edited — password
    // belongs to the upstream directory; disable reset-pw for them.
    const pwDisabled = isFederated ? 'disabled title="password managed by an external backend"' : '';
    const deleteDisabled = isSelf ? 'disabled title="non puoi eliminare te stesso"' : '';

    return `
      <tr data-user-id="${u.id}" data-username="${_esc(u.username)}"
          data-role="${_esc(u.role)}" data-backend="${_esc(u.backend)}"
          data-is-active="${u.is_active ? '1' : '0'}">
        <td class="users-num">${u.id}</td>
        <td class="users-uname">${_esc(u.username)}${selfTag}</td>
        <td>${rolePill}</td>
        <td><code>${_esc(u.backend)}</code></td>
        <td>${activePill}</td>
        <td class="users-dim">${last}</td>
        <td class="users-actions">
          <button class="btn btn-xs users-act-role"   title="Cambia ruolo">👤</button>
          <button class="btn btn-xs users-act-pw"     title="Reset password" ${pwDisabled}>🔑</button>
          <button class="btn btn-xs users-act-toggle" title="${u.is_active ? 'Disabilita' : 'Abilita'}">
            ${u.is_active ? '⏸' : '▶'}
          </button>
          <button class="btn btn-xs btn-danger users-act-del" title="Delete" ${deleteDisabled}>🗑</button>
        </td>
      </tr>
    `;
  }

  // ── Add form ──────────────────────────────────────────────────────────

  function _toggleAddForm() {
    const form = _body.querySelector('#users-add-form');
    if (!form.hidden) { form.hidden = true; form.innerHTML = ''; return; }
    form.innerHTML = `
      <div class="users-form-row">
        <label>Username <input id="newu-name" type="text" autocomplete="off" spellcheck="false" required></label>
        <label>Password <input id="newu-pw" type="password" minlength="8" required></label>
        <label>Ruolo
          <select id="newu-role">
            ${ROLES.map(r => `<option value="${r}" ${r === 'student' ? 'selected' : ''}>${r}</option>`).join('')}
          </select>
        </label>
        <label>Email <input id="newu-email" type="email" autocomplete="off"></label>
        <button id="newu-save" class="btn btn-primary btn-sm">Create</button>
      </div>
    `;
    form.hidden = false;
    form.querySelector('#newu-save').addEventListener('click', _submitAdd);
    form.querySelector('#newu-name').focus();
  }

  async function _submitAdd() {
    const username = _body.querySelector('#newu-name').value.trim();
    const password = _body.querySelector('#newu-pw').value;
    const role = _body.querySelector('#newu-role').value;
    const email = _body.querySelector('#newu-email').value.trim();
    if (!username || !password || password.length < 8) {
      showToast('Username and password (≥8 chars) required', 'warn');
      return;
    }
    try {
      await API.Users.create({ username, password, role, email: email || null });
      showToast(`User ${username} created`, 'success');
      await _render();
    } catch (e) {
      showToast(_apiErr('Creation failed', e), 'error');
    }
  }

  // ── Row actions ───────────────────────────────────────────────────────

  function _wireRowActions() {
    _body.querySelectorAll('tr[data-user-id]').forEach(tr => {
      const id = Number(tr.dataset.userId);
      const username = tr.dataset.username;
      const role = tr.dataset.role;
      const isActive = tr.dataset.isActive === '1';
      const backend = tr.dataset.backend;

      tr.querySelector('.users-act-role').addEventListener('click', () => _editRole(id, username, role));
      tr.querySelector('.users-act-pw').addEventListener('click',  () => _resetPw(id, username, backend));
      tr.querySelector('.users-act-toggle').addEventListener('click', () => _toggleActive(id, username, isActive));
      tr.querySelector('.users-act-del').addEventListener('click', () => _deleteUser(id, username));
    });
  }

  async function _editRole(id, username, currentRole) {
    const form = document.createElement('div');
    form.innerHTML = `
      <p>User: <strong>${_esc(username)}</strong></p>
      <label>New ruolo
        <select id="edit-role">
          ${ROLES.map(r => `<option value="${r}" ${r === currentRole ? 'selected' : ''}>${r}</option>`).join('')}
        </select>
      </label>
    `;
    showModal('Cambia ruolo', form, [
      { label: 'Cancel' },
      { label: 'Save', class: 'btn-primary', action: async () => {
          const newRole = form.querySelector('#edit-role').value;
          if (newRole === currentRole) { await show(); return; }
          try {
            await API.Users.patch(id, { role: newRole });
            showToast(`${username}: ruolo → ${newRole}`, 'success');
          } catch (e) {
            showToast(_apiErr('Role change failed', e), 'error');
          }
          await show();
      }},
    ]);
  }

  async function _resetPw(id, username, backend) {
    if (backend !== 'local_db') {
      showToast('Password managed by the external backend', 'warn');
      return;
    }
    const form = document.createElement('div');
    form.innerHTML = `
      <p>Reset password di <strong>${_esc(username)}</strong></p>
      <label>New password <input id="pw-new" type="password" minlength="8" required></label>
      <p class="users-hint">Minimum 8 characters. The user will not receive notifications; communicate it manually.</p>
    `;
    showModal('Reset password', form, [
      { label: 'Cancel' },
      { label: 'Imposta', class: 'btn-primary', action: async () => {
          const pw = form.querySelector('#pw-new').value;
          if (!pw || pw.length < 8) {
            showToast('Password too short', 'warn');
            return show();
          }
          try {
            await API.Users.resetPassword(id, pw);
            showToast(`Password di ${username} updated`, 'success');
          } catch (e) {
            showToast(_apiErr('Password reset failed', e), 'error');
          }
          await show();
      }},
    ]);
  }

  async function _toggleActive(id, username, isActive) {
    try {
      await API.Users.patch(id, { is_active: !isActive });
      showToast(`${username}: ${isActive ? 'disabilitato' : 'abilitato'}`, 'success');
    } catch (e) {
      showToast(_apiErr('Operazione failed', e), 'error');
    }
    await _render();
  }

  async function _deleteUser(id, username) {
    if (!confirm(`Deletere permanently l'user "${username}"?\nI suoi lab NON vengono eliminati, verranno solo scollegati.`)) {
      return;
    }
    try {
      await API.Users.remove(id);
      showToast(`User ${username} deleted`, 'success');
    } catch (e) {
      showToast(_apiErr('Deletion failed', e), 'error');
    }
    await _render();
  }

  // ── Helpers ───────────────────────────────────────────────────────────

  function _apiErr(prefix, e) {
    // Error text from api.js looks like "400: ..." — peel to show just the detail.
    const raw = String(e?.message || e || '');
    const m = raw.match(/^\d+:\s*(.*)$/s);
    let detail = m ? m[1] : raw;
    // FastAPI body is JSON like {"detail":"..."}; surface just detail if parseable.
    try {
      const obj = JSON.parse(detail);
      if (obj && obj.detail) detail = obj.detail;
    } catch (_) {}
    return `${prefix}: ${detail}`;
  }

  function _fmtRelative(iso) {
    const t = Date.parse(iso);
    if (isNaN(t)) return _esc(iso);
    const diff = (Date.now() - t) / 1000;
    if (diff < 0) return 'in futuro';
    if (diff < 60) return `${Math.floor(diff)}s fa`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m fa`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h fa`;
    return `${Math.floor(diff / 86400)}g fa`;
  }

  function _esc(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  return { show };
})();
