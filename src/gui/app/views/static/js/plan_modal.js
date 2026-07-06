/**
 * PlanModal — pre-deploy confirmation modal.
 *
 * Shows the next-deploy scheduling plan that `dnlab_multinode.PlanController`
 * would compute if deployment started now: VD→host assignments, cross-host
 * links with VxLAN IDs, and warnings about missing images on workers
 * (data read from the image-sync daemon state).
 *
 * Usage:
 *   PlanModal.show(labId, displayName, { onConfirm, onCancel })
 *
 * The promise is resolved/rejected via callbacks, not directly, because
 * `showModal()` returns nothing.
 */
const PlanModal = (() => {

  /**
   * Open the modal. Runs plan + image-sync in parallel, then renders.
   * If planning fails (e.g. invalid topology, missing hosts.yml),
   * shows a blocking error without a "Deploy" button.
   */
  async function show(labId, displayName, { onConfirm, onCancel } = {}) {
    // Initial "loading" state — rendering still happens so
    // the user sees the modal immediately even if the planner is slow.
    const body = document.createElement('div');
    body.className = 'plan-modal';
    body.innerHTML = `
      <div class="plan-loading">
        <div class="spinner"></div>
        <span>Calculating next deploy plan…</span>
      </div>
    `;
    showModal(`Next deploy plan — ${displayName}`, body, [
      { label: 'Cancel', class: 'btn-secondary', action: () => onCancel && onCancel() },
    ]);

    let plan = null;
    let syncState = null;
    let planErr = null;
    try {
      [plan, syncState] = await Promise.all([
        API.Labs.plan(labId),
        API.Multinode.imageSyncStatus().catch(() => ({ available: false })),
      ]);
    } catch (e) {
      planErr = e;
    }

    if (planErr) {
      _renderError(body, planErr);
      return;
    }

    _renderPlan(body, displayName, plan, syncState, onConfirm, onCancel);
  }

  // ── Renderers ──────────────────────────────────────────────────────

  function _renderError(body, err) {
    body.innerHTML = `
      <div class="plan-error">
        <strong>Cannot calculate the plan:</strong>
        <pre>${_escape(err.message || String(err))}</pre>
        <p class="plan-hint">
          “Verify that the <code>hosts.yml</code> file is reachable
          and that the topology has been saved correctly.”
        </p>
      </div>
    `;
  }

  function _renderPlan(body, displayName, plan, syncState, onConfirm, onCancel) {
    const assignments = plan.assignments || {};
    const crossLinks = plan.cross_host_links || [];
    const totalVDs = Object.values(assignments)
      .reduce((sum, a) => sum + (a.vd_names || []).length, 0);
    const hostCount = Object.keys(assignments).length;

    const hostRows = Object.entries(assignments).map(([hostName, a]) => {
      const vds = a.vd_names || [];
      const cpu = a.cpu_used || 0;
      const ram = Math.round((a.ram_mb_used || 0) / 1024 * 10) / 10;
      return `
        <div class="plan-host-row">
          <div class="plan-host-name">
            <span class="plan-host-dot"></span>${_escape(hostName)}
            <span class="plan-host-ip">${_escape(a.host_ip || '')}</span>
          </div>
          <div class="plan-host-meta">
            ${vds.length} VD · ${cpu} vCPU · ${ram} GB
          </div>
          <div class="plan-host-vds">${vds.map(v => `<span class="plan-vd-chip">${_escape(v)}</span>`).join('')}</div>
        </div>
      `;
    }).join('');

    const crossBlock = crossLinks.length === 0
      ? `<p class="plan-empty">No cross-host links (all nodes on the same host).</p>`
      : `<table class="plan-links">
          <thead><tr><th>VxLAN</th><th>Source</th><th>Target</th></tr></thead>
          <tbody>
            ${crossLinks.map(l => `
              <tr>
                <td class="plan-vxid">${l.vxlan_id}</td>
                <td>${_escape(l.source_node)}:${_escape(l.source_iface)} <span class="plan-host-badge">${_escape(l.source_host)}</span></td>
                <td>${_escape(l.target_node)}:${_escape(l.target_iface)} <span class="plan-host-badge">${_escape(l.target_host)}</span></td>
              </tr>
            `).join('')}
          </tbody>
        </table>`;

    const imgWarn = _imageWarnings(plan, syncState);

    body.innerHTML = `
      <div class="plan-summary">
        <div class="plan-kpi">
          <div class="plan-kpi-num">${totalVDs}</div>
          <div class="plan-kpi-lbl">VDs</div>
        </div>
        <div class="plan-kpi">
          <div class="plan-kpi-num">${hostCount}</div>
          <div class="plan-kpi-lbl">host</div>
        </div>
        <div class="plan-kpi">
          <div class="plan-kpi-num">${crossLinks.length}</div>
          <div class="plan-kpi-lbl">link cross-host</div>
        </div>
        <div class="plan-kpi">
          <div class="plan-kpi-num">#${plan.vrf_table_id || '–'}</div>
          <div class="plan-kpi-lbl">VRF table</div>
        </div>
      </div>

      ${imgWarn ? `<div class="plan-section plan-warn-box">${imgWarn}</div>` : ''}

      <div class="plan-section">
        <h4>Next deploy assignments by host</h4>
        <div class="plan-hosts">${hostRows || '<p class="plan-empty">No host assegnato.</p>'}</div>
      </div>

      <div class="plan-section">
        <h4>Link cross-host (VxLAN dataplane)</h4>
        ${crossBlock}
      </div>
    `;

    // Rebuild the modal buttons to add "Deploy".
    // The `showModal()` API has no clean way to reassign buttons after
    // creation, so this directly manipulates the footer.
    const footer = document.getElementById('modal-footer');
    if (footer) {
      footer.innerHTML = '';
      const addBtn = (label, cls, handler) => {
        const b = document.createElement('button');
        b.className = `btn ${cls}`;
        b.textContent = label;
        b.onclick = () => { hideModal(); handler && handler(); };
        footer.appendChild(b);
      };
      addBtn('Cancel', 'btn-secondary', () => onCancel && onCancel());
      addBtn('▶ Deploy', 'btn-start', () => onConfirm && onConfirm(plan));
    }
  }

  // ── Helpers ────────────────────────────────────────────────────────

  /**
   * Builds a warning for images not yet present on all
   * assigned hosts. Returns HTML or an empty string if everything is OK.
   */
  function _imageWarnings(plan, syncState) {
    if (!syncState || !syncState.available || !syncState.state) return '';
    const workers = (syncState.state && syncState.state.workers) || {};
    const missingByHost = {};
    for (const [hostName, worker] of Object.entries(workers)) {
      const miss = worker && worker.missing ? worker.missing : [];
      if (miss.length) missingByHost[hostName] = miss;
    }
    if (Object.keys(missingByHost).length === 0) return '';

    const rows = Object.entries(missingByHost).map(([h, imgs]) => `
      <li><strong>${_escape(h)}</strong>: ${imgs.map(i => `<code>${_escape(i)}</code>`).join(', ')}</li>
    `).join('');
    return `
      <div class="plan-warn">
        <strong>⚠ Images not synchronized:</strong>
        <ul>${rows}</ul>
        <p class="plan-hint">
          The deployment will be refused until the image-sync daemon completes replication. Wait a few minutes or run
          manualmente <code>dnlab-image-sync sync --all</code>.
        </p>
      </div>
    `;
  }

  function _escape(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  return { show };
})();
