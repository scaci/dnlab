/**
 * Cat9kv vswitch node override plugin.
 */
NodeOverridePlugins.register({
  key: 'cat9kv_vswitch',

  render({ nodeData, override }) {
    const state = cat9kvState(nodeData, override);
    const platformOptions = (override.platforms || []).map(p => `
      <option value="${esc(p.value)}" ${state.platform === p.value ? 'selected' : ''}>
        ${esc(p.label || p.value)}
      </option>
    `).join('');
    const help = (override.platforms || []).map(p => `
      <span class="mgmt-hint"><strong>${esc(p.label || p.value)}</strong>: ${esc(p.help || '')}</span>
    `).join('');

    return `
      <fieldset class="props-fieldset node-override node-override-cat9kv">
        <legend>${esc(override.label || 'Node override')}</legend>
        <label>Platform
          <select name="cat9kv_platform" class="props-input">
            ${platformOptions}
          </select>
        </label>
        <label>Port count
          <input type="number" name="cat9kv_port_count" min="1" max="256"
                 value="${esc(String(state.port_count))}" class="props-input">
        </label>
        <label>Serial number
          <div class="props-inline">
            <input type="text" name="cat9kv_serial_number" maxlength="12"
                   value="${esc(state.serial_number)}" class="props-input">
            <button type="button" id="btn-cat9kv-random-serial" class="btn btn-sm">Random</button>
          </div>
        </label>
        ${help}
      </fieldset>
    `;
  },

  read({ panel }) {
    const platform = (panel.querySelector('[name="cat9kv_platform"]')?.value || 'UADP').toUpperCase();
    const portCount = parseInt(panel.querySelector('[name="cat9kv_port_count"]')?.value || '24', 10);
    const serialInput = panel.querySelector('[name="cat9kv_serial_number"]');
    const serial = cleanCiscoSerial(serialInput?.value) || randomCiscoSerial();
    if (serialInput) serialInput.value = serial;
    return {
      type: 'cat9kv_vswitch',
      platform: ['UADP', 'Q200'].includes(platform) ? platform : 'UADP',
      port_count: Number.isFinite(portCount) ? Math.max(1, Math.min(portCount, 256)) : 24,
      serial_number: serial,
    };
  },

  wire({ panel }) {
    const btn = panel.querySelector('#btn-cat9kv-random-serial');
    if (!btn) return;
    btn.addEventListener('click', () => {
      const input = panel.querySelector('[name="cat9kv_serial_number"]');
      if (input) input.value = randomCiscoSerial();
    });
  },
});

function cat9kvState(nodeData, override) {
  const state = nodeData.node_overrides_state || nodeData.node_overrides || {};
  return {
    platform: ['UADP', 'Q200'].includes((state.platform || '').toUpperCase())
      ? state.platform.toUpperCase()
      : (override.default_platform || 'UADP'),
    port_count: Number(state.port_count || override.default_port_count || 24),
    serial_number: cleanCiscoSerial(state.serial_number) || randomCiscoSerial(),
  };
}

function cleanCiscoSerial(value) {
  return String(value || '').replace(/[^A-Za-z0-9]/g, '').toUpperCase().slice(0, 12);
}

function randomCiscoSerial() {
  const prefixes = ['FOC', 'FDO', 'FXS', 'CAT'];
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
  const prefix = prefixes[Math.floor(Math.random() * prefixes.length)];
  let suffix = '';
  for (let i = 0; i < 8; i += 1) suffix += chars[Math.floor(Math.random() * chars.length)];
  return prefix + suffix;
}

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

