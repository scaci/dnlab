/**
 * Canvas module – wraps Cytoscape.js for network topology editing.
 *
 * Public interface:
 *   Canvas.init(containerId)
 *   Canvas.loadTopology(topoData)
 *   Canvas.getTopologyData()          → {name, nodes[], links[]}
 *   Canvas.addNode(nodeData)
 *   Canvas.getSelected()                → {nodes:[], edges:[]}
 *   Canvas.setMode(mode)              → 'select' | 'link'
 *   Canvas.on(event, callback)
 *   Canvas.fit()
 *   Canvas.clear()
 */
const Canvas = (() => {
  let cy = null;
  let mode = 'select';          // 'select' | 'link'
  let linkSource = null;
  const listeners = {};
  // (kind, linuxIface) -> vendorIface. Default is passthrough until
  // the caller installs a real resolver (see setInterfaceResolver).
  let _ifaceResolver = (_kind, linux) => linux;

  // ── Mgmt cloud (node fittizio) ─────────────────────────────────────
  // id e classi condivisi: il cloud non viene mai serializzato nel
  // topology YAML come node, e i suoi edge sono solo decorativi.
  const MGMT_ID       = '__mgmt__';
  const MGMT_NODE_CLS = 'mgmt-node';
  const MGMT_EDGE_CLS = 'mgmt-edge';
  let _mgmtVisible = true;
  let _realNetInfoVisible = localStorage.getItem('dnlab-realnet-info-visible') === '1';
  let _realNetHoverTip = null;
  let _realNetRemoteAs = '';
  let _rabbitDotLayer = null;
  const _rabbitDots = new Map();
  const _rabbitCompletedSeenAt = new Map();
  const FOLLOW_RABBIT_AFTERGLOW_MS = 30000;
  const FOLLOW_RABBIT_IDLE_TTL_MS = 10000;
  const EDGE_BEZIER_STEP_PX = 40;
  const _isMgmtId = id => id === MGMT_ID;
  const _isMgmtEdge = e => e.hasClass && e.hasClass(MGMT_EDGE_CLS);
  const _isMgmtEdgeLike = e =>
    _isMgmtEdge(e) || _isMgmtId(e.data('source')) || _isMgmtId(e.data('target'));

  // ── Theme-aware Cytoscape styles ─────────────────────────────────────
  const THEMES = {
    dark: {
      nodeColor: '#e0e0e0', nodeBorder: '#555', textBg: '#1a1a2e',
      edgeColor: '#888', edgeLabelColor: '#aaa', selectColor: '#00d4ff',
    },
    light: {
      nodeColor: '#1f2937', nodeBorder: '#bbb', textBg: '#ffffff',
      edgeColor: '#999', edgeLabelColor: '#555', selectColor: '#0077b6',
    },
  };

  const NODE_ICON_SIZE = 56;
  const NODE_GLYPH_SIZE = 48;
  const NODE_GLYPH_OFFSET = (NODE_ICON_SIZE - NODE_GLYPH_SIZE) / 2;
  const NODE_ICON_CACHE = new Map();

  function _fallbackNodeIcon(color) {
    const svg = `<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE svg>
<svg xmlns="http://www.w3.org/2000/svg" width="${NODE_ICON_SIZE}" height="${NODE_ICON_SIZE}" viewBox="0 0 ${NODE_ICON_SIZE} ${NODE_ICON_SIZE}">
  <circle cx="28" cy="28" r="27" fill="${color}"/>
</svg>`;
    return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
  }

  async function _loadNodeIcon(iconUrl, color) {
    const key = `${iconUrl}:${color}`;
    if (NODE_ICON_CACHE.has(key)) return NODE_ICON_CACHE.get(key);

    const res = await fetch(iconUrl);
    if (!res.ok) throw new Error(`Unable to load icon ${iconUrl}: ${res.status}`);

    const rawSvg = await res.text();
    const uri = _composeNodeIcon(rawSvg, color);
    NODE_ICON_CACHE.set(key, uri);
    return uri;
  }

  function _composeNodeIcon(rawSvg, color) {
    const parser = new DOMParser();
    const doc = parser.parseFromString(rawSvg, 'image/svg+xml');
    const sourceSvg = doc.documentElement;
    const viewBox = _parseViewBox(sourceSvg.getAttribute('viewBox'));
    const scale = NODE_GLYPH_SIZE / Math.max(viewBox.width, viewBox.height);
    const x = NODE_GLYPH_OFFSET + ((NODE_GLYPH_SIZE - viewBox.width * scale) / 2);
    const y = NODE_GLYPH_OFFSET + ((NODE_GLYPH_SIZE - viewBox.height * scale) / 2);

    const svg = `<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE svg>
<svg xmlns="http://www.w3.org/2000/svg" width="${NODE_ICON_SIZE}" height="${NODE_ICON_SIZE}" viewBox="0 0 ${NODE_ICON_SIZE} ${NODE_ICON_SIZE}">
  <circle cx="28" cy="28" r="27" fill="${color}"/>
  <g transform="translate(${x} ${y}) scale(${scale}) translate(${-viewBox.x} ${-viewBox.y})"
     fill="none"
     stroke="#ffffff"
     stroke-width="3"
     stroke-linecap="round"
     stroke-linejoin="round">
    ${sourceSvg.innerHTML}
  </g>
</svg>`;
    return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
  }

  function _parseViewBox(viewBox) {
    const values = (viewBox || '0 0 64 64').trim().split(/[\s,]+/).map(Number);
    if (values.length !== 4 || values.some(Number.isNaN) || values[2] <= 0 || values[3] <= 0) {
      return { x: 0, y: 0, width: 64, height: 64 };
    }
    return { x: values[0], y: values[1], width: values[2], height: values[3] };
  }

  async function _hydrateNodeIcon(node) {
    const kind = node.data('kind') || 'linux';
    const color = node.data('color') || _kindColor(kind);
    const iconUrl = kind === '_real_net'
      ? (DeviceCatalog.icon('cloud') || 'img/devices/cloud.svg')
      : DeviceCatalog.kindIcon(kind);

    try {
      const icon = await _loadNodeIcon(iconUrl, color);
      if (node.removed() || node.data('kind') !== kind || node.data('color') !== color) return;
      node.data('icon', icon);
    } catch (err) {
      console.warn(err);
    }
  }

  function _buildStylesheet(theme = 'dark') {
    const t = THEMES[theme] || THEMES.dark;
    return [
      {
        selector: 'node',
        style: {
          'background-color': 'data(color)',
          'background-opacity': 0,
          'label': 'data(label)',
          'text-valign': 'bottom',
          'text-halign': 'center',
          'font-size': '11px',
          'color': t.nodeColor,
          'text-wrap': 'wrap',
          'text-max-width': '160px',
          'text-margin-y': '4px',
          'width': `${NODE_ICON_SIZE}px`,
          'height': `${NODE_ICON_SIZE}px`,
          'border-width': '2px',
          'border-color': t.nodeBorder,
          'background-image': 'data(icon)',
          'background-fit': 'contain',
          'text-background-color': t.textBg,
          'text-background-opacity': 0.7,
          'text-background-padding': '2px',
          'text-background-shape': 'roundrectangle',
          'background-width-relative-to': 'inner',
          'background-height-relative-to': 'inner',
          'background-width': '100%',
          'background-height': '100%',
          'background-position-x': '50%',
          'background-position-y': '50%',
          'background-repeat': 'no-repeat',
          'background-clip': 'node',            // Vincola l'image ai confini del node (evita il drifting)
          'background-image-containment': 'inside',
        },
      },
      {
        selector: 'node:selected',
        style: {
          'border-color': t.selectColor,
          'border-width': '3px',
        },
      },
      {
        selector: 'node.node-stopped',
        style: {
          'opacity': 0.45,
          'border-style': 'dashed',
        },
      },
      {
        selector: 'node.node-runtime-error',
        style: {
          'border-color': '#ef4444',
          'border-width': '3px',
        },
      },
      {
        selector: 'node.node-runtime-busy',
        style: {
          'border-color': '#f59e0b',
          'border-width': '3px',
        },
      },
      {
        selector: 'node.link-source',
        style: { 'border-color': '#ff9900', 'border-width': '3px' },
      },
      {
        selector: 'node.link-hover',
        style: { 'border-color': '#00ff88', 'border-width': '3px' },
      },
      {
        selector: 'edge',
        style: {
          'width': 2,
          'line-color': t.edgeColor,
          'target-arrow-shape': 'none',
          'curve-style': 'bezier',
          'control-point-step-size': `${EDGE_BEZIER_STEP_PX}px`,
          'label': 'data(label)',
          'font-size': '9px',
          'color': t.edgeLabelColor,
          'text-background-color': t.textBg,
          'text-background-opacity': 0.7,
          'text-background-padding': '2px',
          'text-rotation': 'autorotate',
        },
      },
      {
        selector: 'edge:selected',
        style: { 'line-color': t.selectColor, 'width': 3 },
      },
      {
        selector: 'edge.edge-hover',
        style: { 'line-color': t.selectColor, 'width': 3 },
      },
      {
        selector: 'edge.capture-active',
        style: {
          'line-color': '#22c55e',
          'width': 5,
          'line-style': 'dashed',
          'color': '#22c55e',
          'font-weight': '700',
          'text-background-opacity': 0.95,
          'text-background-color': t.textBg,
          'source-label': 'data(capture_source_badge)',
          'target-label': 'data(capture_target_badge)',
          'source-text-offset': 34,
          'target-text-offset': 34,
          'source-text-margin-y': -8,
          'target-text-margin-y': -8,
          'source-text-background-color': '#052e16',
          'target-text-background-color': '#052e16',
          'source-text-background-opacity': 1,
          'target-text-background-opacity': 1,
          'source-text-background-padding': '3px',
          'target-text-background-padding': '3px',
          'source-text-background-shape': 'roundrectangle',
          'target-text-background-shape': 'roundrectangle',
          'source-text-rotation': 'none',
          'target-text-rotation': 'none',
          'source-text-margin-x': 0,
          'target-text-margin-x': 0,
          'z-index': 20,
        },
      },
      {
        selector: 'edge.rabbit-active',
        style: {
          'line-color': '#facc15',
          'width': 6,
          'line-style': 'solid',
          'z-index': 30,
          'shadow-blur': 18,
          'shadow-color': '#facc15',
          'shadow-opacity': 0.9,
        },
      },
      {
        selector: 'edge.rabbit-pulse',
        style: {
          'line-color': '#ffffff',
          'width': 8,
        },
      },
      {
        // A TTL layer with more than one link: ECMP / load-shared hop.
        selector: 'edge.rabbit-ecmp',
        style: {
          'line-color': '#f59e0b',
          'line-style': 'dashed',
          'width': 4,
        },
      },
      {
        // A gate failed for this segment (e.g. a missing capture layer).
        selector: 'edge.rabbit-unresolved',
        style: {
          'line-color': '#ef4444',
          'line-style': 'dashed',
          'width': 4,
        },
      },
      // ── Mgmt "cloud" virtuale ──────────────────────────────────────
      // It is not a real device: it is not serialized in the topology YAML
      // come node e non partecipa al routing. Solo rendering.
      {
        selector: 'node.mgmt-node',
        style: {
          'background-opacity': 0.9,
          'background-color': '#6b7a90',
          'border-color':      '#8699b0',
          'border-width':      '2px',
          'border-style':      'dashed',
          'width':             `${NODE_ICON_SIZE}px`,
          'height':            `${NODE_ICON_SIZE}px`,
          'font-size':         '10px',
          'font-style':        'italic',
          'text-wrap':         'wrap',
          'text-max-width':    '280px',
          'text-margin-y':     '8px',
          'text-background-padding': '3px',
        },
      },
      // Edge fittizi node↔mgmt-cloud: tratteggiati, senza freccia,
      // subtle color so it does not compete with real data links.
      {
        selector: 'edge.mgmt-edge',
        style: {
          'width': 1,
          'line-color':    '#6b7a90',
          'line-style':    'dashed',
          'line-opacity':  0.6,
          'curve-style':   'bezier',
          'target-arrow-shape': 'none',
          'label':         'data(label)',
          'color':         '#94a3b8',
          'font-size':     '8px',
          'font-style':    'italic',
          'text-opacity':  0.85,
          'events':        'no',   // no tap / hover — is decorative
        },
      },
    ];
  }

  // ── Init ─────────────────────────────────────────────────────────────
  function init(containerId) {
    const savedTheme = localStorage.getItem('dnlab-theme') || 'dark';
    cy = cytoscape({
      container: document.getElementById(containerId),
      pixelRatio: 1,           // Forza il rapporto 1:1 invece di 'auto' (evita drift su schermi HiDPI)
      motionBlur: false,           // Rimuove l'effetto scia che sposta le icone
      textureOnViewport: false,    // OBBLIGATORIO: se true, sposta le images durante lo zoom
      wheelSensitivity: 0.2,       // Makes zoom smoother for calculations
      style: _buildStylesheet(savedTheme),
      layout: { name: 'preset' },
      minZoom: 0.2,
      maxZoom: 4,
      wheelSensitivity: 0.3,
    });

    _bindEvents();
    _ensureRabbitDotLayer();
    return cy;
  }

  function setTheme(theme) {
    if (!cy) return;
    cy.style().fromJson(_buildStylesheet(theme)).update();
    if (_realNetHoverTip) _setRealNetTipTheme();
  }

  // ── Event binding ────────────────────────────────────────────────────
  function _bindEvents() {
    // Double-click on canvas → place node if a device type is queued
    cy.on('dbltap', (evt) => {
      if (evt.target === cy) {
        _emit('canvas-dblclick', { x: evt.position.x, y: evt.position.y });
      }
    });

    // Single click on node → link drawing (select mode just selects)
    cy.on('tap', 'node', (evt) => {
      const node = evt.target;
      // The mgmt cloud has its dedicated handler (opens the modal).
      if (_isMgmtId(node.data('id'))) {
        _emit('mgmt-click', {});
        return;
      }
      if (mode === 'link') {
        _handleLinkTap(node);
      } else {
        _emit('node-select', node.data());
      }
    });

    // Right-click on node → context menu
    cy.on('cxttap', 'node', (evt) => {
      const node        = evt.target;
      // Mgmt cloud: no device menu, only opens the modal.
      if (_isMgmtId(node.data('id'))) {
        _emit('mgmt-click', {});
        return;
      }
      const oe          = evt.originalEvent;
      const screenX     = oe ? oe.clientX : (() => {
        const rp   = node.renderedPosition();
        const rect = cy.container().getBoundingClientRect();
        return rect.left + rp.x;
      })();
      const screenY     = oe ? oe.clientY : (() => {
        const rp   = node.renderedPosition();
        const rect = cy.container().getBoundingClientRect();
        return rect.top + rp.y;
      })();
      _emit('node-rightclick', { data: node.data(), screenX, screenY });
    });

    // Right-click on edge → context menu
    cy.on('cxttap', 'edge', (evt) => {
      const edge = evt.target;
      const oe   = evt.originalEvent;
      const screenX = oe ? oe.clientX : 0;
      const screenY = oe ? oe.clientY : 0;
      _emit('edge-rightclick', { data: edge.data(), screenX, screenY });
    });

    // Prevent native browser context menu on the canvas
    cy.container().addEventListener('contextmenu', e => e.preventDefault());

    cy.on('tap', 'edge', (evt) => {
      if (mode === 'select') _emit('edge-select', evt.target.data());
    });

    cy.on('tap', (evt) => {
      if (evt.target === cy) {
        if (mode === 'link' && linkSource) {
          _cancelLink();
        }
        _emit('deselect', null);
      }
    });

    // Drag node → persist position
    cy.on('dragfree', 'node', (evt) => {
      const n = evt.target;
      _emit('node-move', { id: n.data('id'), position: n.position() });
    });

    // Hover effects in link mode + RealNet quick info balloon.
    cy.on('mouseover', 'node', (evt) => {
      if (mode === 'link' && linkSource && evt.target !== linkSource) {
        evt.target.addClass('link-hover');
      }
      _showRealNetHover(evt.target);
    });
    cy.on('mousemove', 'node', (evt) => {
      _moveRealNetHover(evt.target);
    });
    cy.on('mouseout', 'node', (evt) => {
      evt.target.removeClass('link-hover');
      _hideRealNetHover();
    });

    // Edge hover effect
    cy.on('mouseover', 'edge', (evt) => {
      evt.target.addClass('edge-hover');
    });
    cy.on('mouseout', 'edge', (evt) => {
      evt.target.removeClass('edge-hover');
    });
  }

  // ── Link drawing mode ────────────────────────────────────────────────
  function _handleLinkTap(node) {
    if (!linkSource) {
      linkSource = node;
      node.addClass('link-source');
    } else if (linkSource.id() !== node.id()) {
      _emit('link-draw', {
        source: linkSource.data('id'),
        target: node.data('id'),
      });
      _cancelLink();
    } else {
      _cancelLink();
    }
  }

  function _cancelLink() {
    if (linkSource) linkSource.removeClass('link-source');
    linkSource = null;
  }

  // ── Public API ───────────────────────────────────────────────────────
  function loadTopology(topoData) {
    _clearRabbitDots();
    _stopRabbitPulse();
    cy.elements().remove();
    const { nodes = [], links = [] } = topoData;

    nodes.forEach(n => {
      const node = cy.add({
        group: 'nodes',
        data: _nodeData(n),
        position: { x: n.position?.x ?? 100, y: n.position?.y ?? 100 },
      });
      _applyNodeLabelWidth(node);
      _hydrateNodeIcon(node);
    });

    links.forEach(lk => {
      if (cy.getElementById(lk.source).length && cy.getElementById(lk.target).length) {
        cy.add({
          group: 'edges',
          data: _edgeData(lk),
        });
      }
    });
    // Il cloud viene ri-added separatamente dal caller via setMgmt()
    // — qui puliamo edge mgmt rimasti orfani da un topology precedente.
    _refreshMgmtEdges();
  }

  function getTopologyData() {
    // Il cloud mgmt e i suoi edge fittizi non fanno parte della topology
    // persistita: sono rendering-only.
    const nodes = cy.nodes()
      .filter(n => !_isMgmtId(n.data('id')))
      .map(n => ({
        name:     n.data('id'),
        kind:     n.data('kind'),
        image:    n.data('image'),
        position: { x: Math.round(n.position().x), y: Math.round(n.position().y) },
        extra:    n.data('extra') || {},
      }));
    const links = cy.edges()
      .filter(e => !_isMgmtEdgeLike(e))
      .map(e => ({
        source:       e.data('source'),
        source_iface: e.data('source_iface') || '',
        target:       e.data('target'),
        target_iface: e.data('target_iface') || '',
      }));
    return { nodes, links };
  }

  function addNode(nodeData) {
    const pos = nodeData.position || { x: 200 + Math.random() * 300, y: 150 + Math.random() * 200 };
    if (cy.getElementById(nodeData.name).length) return;
    const node = cy.add({
      group: 'nodes',
      data: _nodeData(nodeData),
      position: pos,
    });
    _applyNodeLabelWidth(node);
    _hydrateNodeIcon(node);
    _refreshMgmtEdges();
  }

  function updateNode(name, updates) {
    const node = cy.getElementById(name);
    if (!node.length) return;
    if (updates.position) node.position(updates.position);
    if (updates.kind  !== undefined) node.data('kind',  updates.kind);
    if (updates.image !== undefined) node.data('image', updates.image);
    if (updates.mgmt_ipv4 !== undefined) node.data('mgmt_ipv4', updates.mgmt_ipv4 || '');
    if (updates.mgmt_ipv6 !== undefined) node.data('mgmt_ipv6', updates.mgmt_ipv6 || '');
    if (updates.extra !== undefined) {
      // Merge (non overwrite): keep any existing keys.
      const prev = node.data('extra') || {};
      const next = { ...prev };
      for (const [key, value] of Object.entries(updates.extra || {})) {
        if (value === null || value === undefined) delete next[key];
        else next[key] = value;
      }
      node.data('extra', next);
      const mgmt = _nodeMgmtIps({ extra: updates.extra });
      node.data('mgmt_ipv4', mgmt.ipv4);
      node.data('mgmt_ipv6', mgmt.ipv6);
    }
    if (updates.webui_state !== undefined) {
      node.data('webui_state', Array.isArray(updates.webui_state) ? updates.webui_state : []);
    }
    if (updates.webui_runtime !== undefined) {
      node.data('webui_runtime', Array.isArray(updates.webui_runtime) ? updates.webui_runtime : []);
    }
    if (updates.runtime_host !== undefined) node.data('runtime_host', updates.runtime_host || '');
    if (updates.runtime_state !== undefined) node.data('runtime_state', updates.runtime_state || '');
    if (updates.runtime_container !== undefined) node.data('runtime_container', updates.runtime_container || '');
    if (updates.runtime_topology_file !== undefined) node.data('runtime_topology_file', updates.runtime_topology_file || '');
    if (updates.runtime_last_error !== undefined) node.data('runtime_last_error', updates.runtime_last_error || '');
    if (updates.scheduled_host !== undefined) node.data('scheduled_host', updates.scheduled_host || '');
    if (updates.placement_mismatch !== undefined) node.data('placement_mismatch', !!updates.placement_mismatch);
    if (updates.duplicate_hosts !== undefined) {
      node.data('duplicate_hosts', Array.isArray(updates.duplicate_hosts) ? updates.duplicate_hosts : []);
    }
    if (updates.node_overrides_state !== undefined) {
      node.data('node_overrides_state', updates.node_overrides_state || null);
    }
    if (updates.node_features_state !== undefined) {
      node.data('node_features_state', updates.node_features_state || null);
    }
    if (updates.kind !== undefined) {
      const kind = updates.kind || 'linux';
      const color = _kindColor(kind);
      node.data('color', color);
      node.data('icon', _fallbackNodeIcon(color));
      _hydrateNodeIcon(node);
    }
    _refreshNodeLabel(node);
  }

  // Set webui_runtime on all nodes at once (chiamato
  // dall'app.js dopo ogni status-live). Does silently nothing for nodes
  // assenti (potrebbero essere stati rimossi dalla topology nel frattempo).
  function setWebUIRuntime(byNode) {
    if (!cy || !byNode) return;
    for (const [name, allocs] of Object.entries(byNode)) {
      const node = cy.getElementById(name);
      if (!node.length) continue;
      node.data('webui_runtime', Array.isArray(allocs) ? allocs : []);
    }
  }

  // Set live mgmt IPs discovered after deploy. These are display-only:
  // explicit node mgmt-ipv4/mgmt-ipv6 remains the source of truth if present.
  function setMgmtRuntime(byNode) {
    if (!cy) return;
    cy.nodes().forEach(node => {
      const id = node.data('id');
      if (_isMgmtId(id)) return;
      const info = (byNode && byNode[id]) || {};
      node.data('runtime_mgmt_ipv4', info.mgmt_ipv4 || info.ipv4_address || info.ipv4 || '');
      node.data('runtime_mgmt_ipv6', info.mgmt_ipv6 || info.ipv6_address || info.ipv6 || '');
      _refreshNodeLabel(node);
    });
  }

  function setPlacementRuntime(byNode) {
    if (!cy) return;
    cy.nodes().forEach(node => {
      const id = node.data('id');
      if (_isMgmtId(id)) return;
      const info = (byNode && byNode[id]) || {};
      node.data('runtime_host', info.host || '');
      node.data('scheduled_host', info.scheduled_host || '');
      node.data('placement_mismatch', !!info.placement_mismatch);
      node.data('duplicate_hosts', Array.isArray(info.duplicate_hosts) ? info.duplicate_hosts : []);
    });
  }

  function setNodeRuntime(byNode) {
    if (!cy) return;
    cy.nodes().forEach(node => {
      const id = node.data('id');
      if (_isMgmtId(id)) return;
      const info = (byNode && byNode[id]) || {};
      node.data('runtime_state', info.state || '');
      node.data('runtime_container', info.container || '');
      node.data('runtime_topology_file', info.topology_file || '');
      node.data('runtime_last_error', info.last_error || '');
      node.toggleClass('node-stopped', info.state === 'stopped');
      node.toggleClass('node-runtime-error', info.state === 'error');
      node.toggleClass('node-runtime-busy', info.state === 'starting' || info.state === 'stopping');
    });
  }

  function removeNode(name) {
    cy.getElementById(name).remove();
    _refreshMgmtEdges();
  }

  function renameNode(oldName, newName) {
    const node = cy.getElementById(oldName);
    if (!node.length) return;
    const pos = node.position();
    const data = node.data();
    // Collect connected edges before removing
    const edges = node.connectedEdges()
      .filter(e => !_isMgmtEdgeLike(e))
      .map(e => ({
        source: e.data('source') === oldName ? newName : e.data('source'),
        target: e.data('target') === oldName ? newName : e.data('target'),
        source_iface: e.data('source_iface') || '',
        target_iface: e.data('target_iface') || '',
      }));
    // Remove old node (and its edges)
    node.remove();
    // Re-add with new name
    cy.add({
      group: 'nodes',
      data: { ...data, id: newName },
      position: pos,
    });
    _refreshNodeLabel(cy.getElementById(newName));
    // Re-add edges
    edges.forEach(e => {
      addEdge(e.source, e.target, e.source_iface, e.target_iface);
    });
    _refreshMgmtEdges();
  }

  /**
   * Build a unique edge ID from source, target, and interface names.
   * This allows multiple links between the same pair of nodes.
   */
  function _edgeId(source, target, sourceIface, targetIface) {
    const parts = [source, target];
    if (sourceIface || targetIface) {
      parts.push(sourceIface || '_', targetIface || '_');
    }
    return parts.join('__');
  }

  function addEdge(source, target, sourceIface = '', targetIface = '') {
    const edgeId = _edgeId(source, target, sourceIface, targetIface);
    if (cy.getElementById(edgeId).length) return;
    cy.add({
      group: 'edges',
      data: {
        id: edgeId,
        source,
        target,
        source_iface: sourceIface,
        target_iface: targetIface,
        source_kind: cy.getElementById(source).data('kind') || '',
        target_kind: cy.getElementById(target).data('kind') || '',
        label: _edgeLabel(source, target, sourceIface, targetIface),
      },
    });
  }

  function removeEdge(source, target, sourceIface, targetIface) {
    if (sourceIface !== undefined && targetIface !== undefined) {
      // Remove specific edge by ID
      const edgeId = _edgeId(source, target, sourceIface, targetIface);
      cy.getElementById(edgeId).remove();
    } else {
      // Remove all edges between source and target
      cy.edges(`[source="${source}"][target="${target}"]`).remove();
      cy.edges(`[source="${target}"][target="${source}"]`).remove();
    }
  }

  function removeEdgeById(edgeId) {
    cy.getElementById(edgeId).remove();
  }

  function getSelected() {
    return {
      nodes: cy.nodes(':selected').map(n => n.data('id')),
      edges: cy.edges(':selected').map(e => ({
        id: e.id(),
        source: e.data('source'),
        target: e.data('target'),
        source_iface: e.data('source_iface') || '',
        target_iface: e.data('target_iface') || '',
      })),
    };
  }

  function setActiveCaptures(captures) {
    if (!cy) return;
    cy.edges('.capture-active').forEach(edge => {
      edge.removeClass('capture-active');
      const base = edge.data('capture_base_label');
      if (base !== undefined) edge.data('label', base);
      edge.removeData('capture_base_label');
      edge.removeData('capture_source_badge');
      edge.removeData('capture_target_badge');
      edge.removeData('capture_sessions');
    });
    (captures || []).forEach(capture => {
      const target = capture.target || capture;
      const edge = _edgeForCaptureTarget(target);
      if (!edge || !edge.length) return;
      if (edge.data('capture_base_label') === undefined) {
        edge.data('capture_base_label', edge.data('label') || '');
      }
      const base = edge.data('capture_base_label') || '';
      edge.data('label', base || '');
      const sessions = edge.data('capture_sessions') || [];
      if (capture.session_id && !sessions.includes(capture.session_id)) {
        sessions.push(capture.session_id);
      }
      edge.data('capture_sessions', sessions);
      _applyCaptureBadge(edge, target);
      edge.addClass('capture-active');
    });
  }

  function setFollowRabbitSessions(sessions) {
    if (!cy) return;
    let animated = 0;
    const now = Date.now();
    const desiredEdges = new Map();
    const desiredDots = new Set();
    const visibleSessionIds = new Set();
    (sessions || []).forEach(session => {
      if (session.session_id) visibleSessionIds.add(session.session_id);
      const animate = _rabbitShouldAnimate(session, now);
      const recon = session.reconstruction;
      const hasRecon = recon
        && ((recon.forward && recon.forward.layers && recon.forward.layers.length)
          || (recon.backward && recon.backward.layers && recon.backward.layers.length));
      if (hasRecon) {
        // Orientation is observed: it comes from the backend TTL-ordered DAG.
        animated += _renderRabbitReconstruction(recon, { animate, now, desiredEdges, desiredDots });
      } else if (!session.status || session.status === 'running') {
        // Fallback for an older backend without reconstruction: best-effort BFS.
        animated += _renderRabbitHits(session, { animate, now, desiredEdges, desiredDots });
      }
    });
    _rabbitCompletedSeenAt.forEach((_seenAt, sessionId) => {
      if (!visibleSessionIds.has(sessionId)) _rabbitCompletedSeenAt.delete(sessionId);
    });
    _applyRabbitEdgeState(desiredEdges);
    _pruneRabbitDots(desiredDots);
    if (animated) _startRabbitPulse();
    else _stopRabbitPulse();
  }

  function _rabbitShouldAnimate(session, now) {
    if (!session || !session.status || session.status === 'running') {
      if (session?.session_id) _rabbitCompletedSeenAt.delete(session.session_id);
      return true;
    }
    const completedAt = Number(session.completed_at || 0);
    if (completedAt > 0) {
      return now - (completedAt * 1000) <= FOLLOW_RABBIT_AFTERGLOW_MS;
    }
    if (!session.session_id) return false;
    if (!_rabbitCompletedSeenAt.has(session.session_id)) {
      _rabbitCompletedSeenAt.set(session.session_id, now);
    }
    return now - _rabbitCompletedSeenAt.get(session.session_id) <= FOLLOW_RABBIT_AFTERGLOW_MS;
  }

  // Render the oriented per-TTL-layer DAG: each edge is drawn src_node -> dst_node
  // exactly as the TTL ordering deduced it, so no GUI-side direction guessing.
  function _renderRabbitReconstruction(recon, opts = {}) {
    const animate = opts.animate !== false;
    const now = opts.now || Date.now();
    const desiredEdges = opts.desiredEdges || new Map();
    const desiredDots = opts.desiredDots || new Set();
    let n = 0;
    [['forward', recon.forward], ['return', recon.backward]].forEach(([variant, leg]) => {
      if (!leg || !leg.layers) return;
      leg.layers.forEach(layer => {
        (layer.edges || []).forEach(e => {
          if (!_rabbitEdgeIsFresh(e, now)) return;
          const from = _rabbitCanvasNode(e.src_node);
          const to = _rabbitCanvasNode(e.dst_node);
          if (!from || !to) return;
          _edgesForRabbitReconstructionEdge(e, from, to).forEach(edge => {
            const classes = ['rabbit-active'];
            if (layer.state === 'unresolved') {
              classes.push('rabbit-unresolved');
            } else if (layer.state === 'ecmp') {
              classes.push('rabbit-ecmp');
            }
            _markRabbitEdge(desiredEdges, edge, classes);
            if (animate) {
              _addRabbitDot(edge, { from, to }, variant, desiredDots);
              n++;
            }
          });
        });
      });
    });
    return n;
  }

  function _renderRabbitHits(session, opts = {}) {
    const animate = opts.animate !== false;
    const now = opts.now || Date.now();
    const desiredEdges = opts.desiredEdges || new Map();
    const desiredDots = opts.desiredDots || new Set();
    const distances = _rabbitDistances(session.source_node, session.hits || []);
    let n = 0;
    (session.hits || []).forEach(hit => {
      if (!_rabbitEdgeIsFresh(hit, now, 'observed_at')) return;
      // Forward orientation runs away from the source; the return leg is the
      // same edge travelled the other way, so swap from/to.
      const forward = _rabbitDirection(hit, distances, session.source_node);
      const variant = hit.direction === 'return' ? 'return' : 'forward';
      const direction = (variant === 'return' && forward)
        ? { from: forward.to, to: forward.from }
        : forward;
      _edgesForRabbitHit(hit).forEach(edge => {
        _markRabbitEdge(desiredEdges, edge, ['rabbit-active']);
        if (animate) {
          _addRabbitDot(edge, direction, variant, desiredDots);
          n++;
        }
      });
    });
    return n;
  }

  function _markRabbitEdge(desiredEdges, edge, classes) {
    if (!edge || !edge.length) return;
    const id = edge.id();
    if (!desiredEdges.has(id)) desiredEdges.set(id, new Set());
    classes.forEach(cls => desiredEdges.get(id).add(cls));
  }

  function _applyRabbitEdgeState(desiredEdges) {
    const rabbitClasses = ['rabbit-active', 'rabbit-ecmp', 'rabbit-unresolved'];
    const keepIds = new Set(desiredEdges.keys());
    cy.edges('.rabbit-active, .rabbit-pulse, .rabbit-ecmp, .rabbit-unresolved').forEach(edge => {
      if (keepIds.has(edge.id())) return;
      edge.removeClass('rabbit-active rabbit-pulse rabbit-ecmp rabbit-unresolved');
    });
    desiredEdges.forEach((classes, edgeId) => {
      const edge = cy.getElementById(edgeId);
      if (!edge.length) return;
      rabbitClasses.forEach(cls => edge.toggleClass(cls, classes.has(cls)));
    });
  }

  function _rabbitEdgeIsFresh(item, now, field = 'last_packet_at') {
    const raw = Number(item?.[field] || 0);
    if (!raw) return true;
    return now - (raw * 1000) <= FOLLOW_RABBIT_IDLE_TTL_MS;
  }

  // A reconstruction node is a VD name, or the pseudo-node "realnet:<name>" whose
  // canvas id is just "<name>". Returns '' when the node is not on the canvas.
  function _rabbitCanvasNode(name) {
    if (!name) return '';
    const id = name.startsWith('realnet:') ? name.slice('realnet:'.length) : name;
    return cy.getElementById(id).length ? id : '';
  }

  function _edgesBetweenNodes(a, b) {
    if (!a || !b) return [];
    return cy.edges().filter(e => {
      const s = e.data('source');
      const t = e.data('target');
      return (s === a && t === b) || (s === b && t === a);
    });
  }

  function _edgesForRabbitReconstructionEdge(item, from, to) {
    const out = [];
    const srcIface = item.source_iface || '';
    const dstIface = item.target_iface || '';
    if (srcIface || dstIface) {
      const exact = cy.getElementById(_edgeId(from, to, srcIface, dstIface));
      if (exact.length) out.push(exact);
      const rev = cy.getElementById(_edgeId(to, from, dstIface, srcIface));
      if (rev.length) out.push(rev);
      if (out.length) return out;
    }

    const epA = item.endpoint_a || {};
    const epB = item.endpoint_b || {};
    const nodeA = _rabbitCanvasNode(epA.node || '');
    const nodeB = _rabbitCanvasNode(epB.node || '');
    const ifaceA = epA.iface || '';
    const ifaceB = epB.iface || '';
    if (nodeA && nodeB && (ifaceA || ifaceB)) {
      const exact = cy.getElementById(_edgeId(nodeA, nodeB, ifaceA, ifaceB));
      if (exact.length) out.push(exact);
      const rev = cy.getElementById(_edgeId(nodeB, nodeA, ifaceB, ifaceA));
      if (rev.length) out.push(rev);
      if (out.length) return out;
    }

    return _edgesBetweenNodes(from, to);
  }

  function setMode(newMode) {
    mode = newMode;
    _cancelLink();
    cy.autoungrabify(newMode === 'link');
  }

  function fit() {
    cy.fit(undefined, 40);
  }

  function clear() {
    _clearRabbitDots();
    _stopRabbitPulse();
    cy.elements().remove();
  }

  /**
   * Convert screen coordinates (clientX/Y from a mouse/drag event)
   * to Cytoscape model coordinates, accounting for current pan and zoom.
   */
  function projectPosition(clientX, clientY) {
    const pan  = cy.pan();
    const zoom = cy.zoom();
    const rect = cy.container().getBoundingClientRect();
    return {
      x: (clientX - rect.left - pan.x) / zoom,
      y: (clientY - rect.top  - pan.y) / zoom,
    };
  }

  function on(event, cb) {
    if (!listeners[event]) listeners[event] = [];
    listeners[event].push(cb);
  }

  function _emit(event, data) {
    (listeners[event] || []).forEach(cb => cb(data));
  }

  function _edgesForRabbitHit(hit) {
    const out = [];
    if (!hit) return out;
    const epA = hit.endpoint_a || {};
    const epB = hit.endpoint_b || {};
    const nodeA = epA.node || '';
    const nodeB = epB.node || '';
    const ifaceA = epA.iface || '';
    const ifaceB = epB.iface || '';
    if (nodeA && nodeB) {
      const exact = cy.getElementById(_edgeId(nodeA, nodeB, ifaceA, ifaceB));
      if (exact.length) out.push(exact);
      const rev = cy.getElementById(_edgeId(nodeB, nodeA, ifaceB, ifaceA));
      if (rev.length) out.push(rev);
    } else if (hit.link_type === 'mgmt') {
      cy.edges(`.${MGMT_EDGE_CLS}`).forEach(e => out.push(e));
    } else if (hit.link_type === 'real_net') {
      cy.edges().forEach(e => {
        if (cy.getElementById(e.data('source')).data('kind') === '_real_net'
          || cy.getElementById(e.data('target')).data('kind') === '_real_net') out.push(e);
      });
    }
    return out;
  }

  function _rabbitHitEndpoints(hit) {
    const epA = hit?.endpoint_a || {};
    const epB = hit?.endpoint_b || {};
    const nodeA = epA.node || '';
    const nodeB = epB.node || '';
    if (!nodeA || !nodeB) return null;
    return { nodeA, nodeB };
  }

  function _rabbitDistances(sourceNode, hits) {
    const start = sourceNode || '';
    const distances = new Map();
    if (!start) return distances;
    const graph = new Map();
    (hits || []).forEach(hit => {
      const ep = _rabbitHitEndpoints(hit);
      if (!ep) return;
      if (!graph.has(ep.nodeA)) graph.set(ep.nodeA, new Set());
      if (!graph.has(ep.nodeB)) graph.set(ep.nodeB, new Set());
      graph.get(ep.nodeA).add(ep.nodeB);
      graph.get(ep.nodeB).add(ep.nodeA);
    });
    const queue = [start];
    distances.set(start, 0);
    while (queue.length) {
      const node = queue.shift();
      const nextDistance = distances.get(node) + 1;
      (graph.get(node) || []).forEach(next => {
        if (distances.has(next)) return;
        distances.set(next, nextDistance);
        queue.push(next);
      });
    }
    return distances;
  }

  function _rabbitDirection(hit, distances, sourceNode) {
    const ep = _rabbitHitEndpoints(hit);
    if (!ep) return null;
    if (ep.nodeA === sourceNode) return { from: ep.nodeA, to: ep.nodeB };
    if (ep.nodeB === sourceNode) return { from: ep.nodeB, to: ep.nodeA };
    const distA = distances.has(ep.nodeA) ? distances.get(ep.nodeA) : Infinity;
    const distB = distances.has(ep.nodeB) ? distances.get(ep.nodeB) : Infinity;
    if (distA < distB) return { from: ep.nodeA, to: ep.nodeB };
    if (distB < distA) return { from: ep.nodeB, to: ep.nodeA };
    return { from: ep.nodeA, to: ep.nodeB };
  }

  function _startRabbitPulse() {
    if (cy.data('rabbitPulseTimer')) return;
    const timer = setInterval(() => {
      if (!cy) return;
      cy.edges('.rabbit-active').forEach(edge => edge.toggleClass('rabbit-pulse'));
      _rabbitDots.forEach(dot => _advanceRabbitDot(dot));
      if (!cy.edges('.rabbit-active').length && !_rabbitDots.size) _stopRabbitPulse();
    }, 80);
    cy.data('rabbitPulseTimer', timer);
  }

  function _stopRabbitPulse() {
    if (!cy) return;
    const timer = cy.data('rabbitPulseTimer');
    if (timer) clearInterval(timer);
    cy.removeData('rabbitPulseTimer');
    cy.edges('.rabbit-pulse').removeClass('rabbit-pulse');
  }

  function _ensureRabbitDotLayer() {
    if (!cy || _rabbitDotLayer) return;
    const container = cy.container();
    if (!container) return;
    _rabbitDotLayer = document.createElement('div');
    _rabbitDotLayer.className = 'rabbit-dot-layer';
    container.appendChild(_rabbitDotLayer);
  }

  function _clearRabbitDots() {
    _rabbitDots.forEach(dot => dot.el.remove());
    _rabbitDots.clear();
  }

  function _addRabbitDot(edge, direction = null, variant = 'forward', desiredDots = null) {
    _ensureRabbitDotLayer();
    // Key by edge + variant so the forward and return dots coexist on one edge.
    const key = _rabbitDotKey(edge, variant);
    if (desiredDots) desiredDots.add(key);
    if (!_rabbitDotLayer) return;
    if (_rabbitDots.has(key)) {
      const dot = _rabbitDots.get(key);
      dot.direction = direction;
      return;
    }
    const el = document.createElement('div');
    el.className = `rabbit-dot rabbit-dot--${variant}`;
    _rabbitDotLayer.appendChild(el);
    const dot = { key, edgeId: edge.id(), el, t: 0, direction };
    _rabbitDots.set(key, dot);
    _advanceRabbitDot(dot);
  }

  function _rabbitDotKey(edge, variant) {
    return `${edge.id()}:${variant}`;
  }

  function _pruneRabbitDots(desiredDots) {
    _rabbitDots.forEach((dot, key) => {
      if (desiredDots.has(key)) return;
      dot.el.remove();
      _rabbitDots.delete(key);
    });
  }

  function _advanceRabbitDot(dot) {
    const edge = cy.getElementById(dot.edgeId);
    if (!edge.length) {
      dot.el.remove();
      _rabbitDots.delete(dot.key);
      return;
    }
    const src = cy.getElementById(edge.data('source'));
    const dst = cy.getElementById(edge.data('target'));
    if (!src.length || !dst.length) {
      dot.el.remove();
      _rabbitDots.delete(dot.key);
      return;
    }
    const t = ((Number(dot.t) || 0) + 0.025) % 1;
    dot.t = t;
    const edgeSource = edge.data('source');
    const from = dot.direction?.from || edgeSource;
    const reverse = from && from !== edgeSource;
    const p = _rabbitEdgePoint(edge, t, reverse);
    const x = p.x;
    const y = p.y;
    dot.el.style.transform = `translate(${x}px, ${y}px) translate(-50%, -50%)`;
  }

  function _rabbitEdgePoint(edge, t, reverse = false) {
    const src = cy.getElementById(edge.data('source'));
    const dst = cy.getElementById(edge.data('target'));
    const pan = cy.pan();
    const zoom = cy.zoom();
    const a = src.position();
    const b = dst.position();
    const p0 = { x: a.x * zoom + pan.x, y: a.y * zoom + pan.y };
    const p2 = { x: b.x * zoom + pan.x, y: b.y * zoom + pan.y };
    const pathT = reverse ? 1 - t : t;
    const control = _rabbitEdgeControlPoint(edge, p0, p2);
    if (!control) {
      return {
        x: p0.x + (p2.x - p0.x) * pathT,
        y: p0.y + (p2.y - p0.y) * pathT,
      };
    }
    const u = 1 - pathT;
    return {
      x: u * u * p0.x + 2 * u * pathT * control.x + pathT * pathT * p2.x,
      y: u * u * p0.y + 2 * u * pathT * control.y + pathT * pathT * p2.y,
    };
  }

  function _rabbitEdgeControlPoint(edge, p0, p2) {
    const siblings = _parallelEdges(edge);
    if (siblings.length < 2) return null;
    const idx = siblings.findIndex(e => e.id() === edge.id());
    if (idx < 0) return null;
    const dx = p2.x - p0.x;
    const dy = p2.y - p0.y;
    const len = Math.hypot(dx, dy);
    if (!len) return null;
    const offset = (idx - (siblings.length - 1) / 2) * EDGE_BEZIER_STEP_PX;
    const nx = -dy / len;
    const ny = dx / len;
    return {
      x: (p0.x + p2.x) / 2 + nx * offset,
      y: (p0.y + p2.y) / 2 + ny * offset,
    };
  }

  function _parallelEdges(edge) {
    const a = edge.data('source');
    const b = edge.data('target');
    return cy.edges().filter(e => {
      const s = e.data('source');
      const t = e.data('target');
      return (s === a && t === b) || (s === b && t === a);
    }).toArray().sort((x, y) => String(x.id()).localeCompare(String(y.id())));
  }

  // ── Data helpers ─────────────────────────────────────────────────────
  function _nodeData(n) {
    const kind = n.kind || 'linux';
    const color = _kindColor(kind);
    const mgmt = _nodeMgmtIps(n);
    return {
      id:    n.name,
      label: _nodeLabel(n.name, kind, mgmt.ipv4, mgmt.ipv6),
      kind,
      image: n.image || '',
      icon:  _fallbackNodeIcon(color),
      color,
      mgmt_ipv4: mgmt.ipv4,
      mgmt_ipv6: mgmt.ipv6,
      runtime_mgmt_ipv4: '',
      runtime_mgmt_ipv6: '',
      runtime_state: n.runtime_state || '',
      runtime_container: n.runtime_container || '',
      runtime_topology_file: n.runtime_topology_file || '',
      runtime_last_error: n.runtime_last_error || '',
      runtime_host: n.runtime_host || '',
      scheduled_host: n.scheduled_host || '',
      placement_mismatch: !!n.placement_mismatch,
      duplicate_hosts: Array.isArray(n.duplicate_hosts) ? n.duplicate_hosts : [],
      extra: n.extra || {},
      // Webui state (sidecar GUI, source-of-truth dei desiderata)
      // e webui runtime (allocazioni host_port dal multinode al
      // deploy time) — popolati dall'app.js dopo loadTopology e ad
      // ogni status-live refresh. No aggancio nello stylesheet
      // Cytoscape: lette dal context-menu e dal Properties panel.
      webui_state:   Array.isArray(n.webui_state)   ? n.webui_state   : [],
      webui_runtime: Array.isArray(n.webui_runtime) ? n.webui_runtime : [],
      node_overrides_state: n.node_overrides_state || null,
      node_features_state: n.node_features_state || null,
    };
  }

  function _nodeMgmtIps(n) {
    const extra = (n && n.extra) || {};
    return {
      ipv4: (n && n.mgmt_ipv4) || extra['mgmt-ipv4'] || '',
      ipv6: (n && n.mgmt_ipv6) || extra['mgmt-ipv6'] || '',
    };
  }

  function _nodeLabel(name, kind, mgmtIpv4 = '', mgmtIpv6 = '') {
    if (kind === '_real_net') {
      const node = cy && cy.getElementById(name);
      if (_realNetInfoVisible && node && node.length) {
        return _realNetLabel(node.data(), false);
      }
      return name;
    }
    if (!_mgmtVisible) return name;
    const lines = [name];
    if (mgmtIpv4) lines.push(mgmtIpv4);
    if (mgmtIpv6) lines.push(mgmtIpv6);
    return lines.join('\n');
  }

  function _realNetLabel(nodeData, withHint = false) {
    const extra = (nodeData && nodeData.extra) || {};
    const lines = [nodeData.id || nodeData.name || 'real_net'];
    if (extra.network) lines.push(`net ${extra.network}`);
    if (extra.ipv4) lines.push(`gw  ${extra.ipv4}`);
    if (extra.bgp && extra.bgp_as) lines.push(`local AS  ${extra.bgp_as}`);
    if (extra.bgp && _realNetRemoteAs) lines.push(`remote AS ${_realNetRemoteAs}`);
    if (withHint) lines.push('(N) to show');
    return lines.join('\n');
  }

  function _isRealNetNode(node) {
    return node && node.length && (node.data('kind') || '') === '_real_net';
  }

  function _showRealNetHover(node) {
    if (_realNetInfoVisible) return;
    if (!_isRealNetNode(node)) return;
    if (!_realNetHoverTip) {
      _realNetHoverTip = document.createElement('div');
      _realNetHoverTip.className = 'canvas-realnet-tip';
      document.body.appendChild(_realNetHoverTip);
    }
    _setRealNetTipTheme();
    _realNetHoverTip.textContent = _realNetLabel(node.data(), true);
    _realNetHoverTip.hidden = false;
    _moveRealNetHover(node);
  }

  function _moveRealNetHover(node) {
    if (!_realNetHoverTip || _realNetHoverTip.hidden || !_isRealNetNode(node)) return;
    const rect = cy.container().getBoundingClientRect();
    const pos = node.renderedPosition();
    _realNetHoverTip.style.left = `${Math.round(rect.left + pos.x + 18)}px`;
    _realNetHoverTip.style.top = `${Math.round(rect.top + pos.y - 18)}px`;
  }

  function _hideRealNetHover() {
    if (_realNetHoverTip) _realNetHoverTip.hidden = true;
  }

  function _setRealNetTipTheme() {
    if (!_realNetHoverTip) return;
    const theme = document.documentElement.getAttribute('data-theme') || localStorage.getItem('dnlab-theme') || 'dark';
    _realNetHoverTip.classList.toggle('canvas-realnet-tip-light', theme === 'dark');
    _realNetHoverTip.classList.toggle('canvas-realnet-tip-dark', theme !== 'dark');
  }

  function _refreshNodeLabel(node) {
    if (!node || !node.length || _isMgmtId(node.data('id'))) return;
    node.data('label', _nodeLabel(
      node.data('id'),
      node.data('kind') || '',
      node.data('mgmt_ipv4') || node.data('runtime_mgmt_ipv4') || '',
      node.data('mgmt_ipv6') || node.data('runtime_mgmt_ipv6') || '',
    ));
    _applyNodeLabelWidth(node);
  }

  function _refreshNodeLabels() {
    if (!cy) return;
    cy.nodes().forEach(n => _refreshNodeLabel(n));
  }

  function _applyNodeLabelWidth(node) {
    if (!node || !node.length || _isMgmtId(node.data('id'))) return;
    node.style('text-max-width', `${_nodeLabelWidth(node.data('label'))}px`);
  }

  function _nodeLabelWidth(label) {
    const lines = String(label || '').split('\n');
    const maxLen = Math.max(...lines.map(line => line.length), 0);
    return Math.max(120, Math.min(420, Math.ceil(maxLen * 6.2) + 24));
  }

  function _edgeData(lk) {
    return {
      id:           _edgeId(lk.source, lk.target, lk.source_iface, lk.target_iface),
      source:       lk.source,
      target:       lk.target,
      source_iface: lk.source_iface || '',
      target_iface: lk.target_iface || '',
      source_kind:  cy ? (cy.getElementById(lk.source).data('kind') || '') : '',
      target_kind:  cy ? (cy.getElementById(lk.target).data('kind') || '') : '',
      label: _edgeLabel(lk.source, lk.target, lk.source_iface || '', lk.target_iface || ''),
    };
  }

  function _edgeForCaptureTarget(target) {
    if (!target) return null;
    if (target.kind === 'mgmt' && target.node) {
      return cy.getElementById(`__mgmt__${target.node}`);
    }
    const link = target.link || {};
    if (!link.source || !link.target) return null;
    const edgeId = _edgeId(
      link.source,
      link.target,
      link.source_iface || '',
      link.target_iface || '',
    );
    const edge = cy.getElementById(edgeId);
    if (edge.length) return edge;
    return cy.getElementById(_edgeId(
      link.target,
      link.source,
      link.target_iface || '',
      link.source_iface || '',
    ));
  }

  function _applyCaptureBadge(edge, target) {
    const badge = _captureBadge(target);
    const link = target.link || {};
    if (target.kind === 'mgmt') {
      edge.data('capture_source_badge', badge);
      return;
    }
    if (target.node === link.target && target.iface === link.target_iface) {
      edge.data('capture_target_badge', badge);
      return;
    }
    edge.data('capture_source_badge', badge);
  }

  function _captureBadge(target) {
    const node = target.node || 'VD';
    const iface = target.iface || '-';
    return `sniffing ${node} ${iface}`;
  }

  function _edgeLabel(source, target, sIf, tIf) {
    const srcKind = cy ? (cy.getElementById(source).data('kind') || '') : '';
    const tgtKind = cy ? (cy.getElementById(target).data('kind') || '') : '';
    if (srcKind === '_real_net') return _ifaceResolver(tgtKind, tIf);
    if (tgtKind === '_real_net') return _ifaceResolver(srcKind, sIf);
    return [_ifaceResolver(srcKind, sIf), _ifaceResolver(tgtKind, tIf)]
      .filter(Boolean).join(' – ');
  }

  function setInterfaceResolver(fn) {
    _ifaceResolver = typeof fn === 'function' ? fn : ((_, l) => l);
    // Relabel existing edges so a late-arriving resolver is reflected.
    if (!cy) return;
    cy.edges().forEach(e => {
      e.data('label', _edgeLabel(
        e.data('source'), e.data('target'),
        e.data('source_iface') || '', e.data('target_iface') || '',
      ));
    });
  }

  // Delega a DeviceCatalog (config/devices.json) — niente hardcoded qui.
  function _kindColor(kind = '') {
    if (kind === '_real_net') return DeviceCatalog.vendorColor('generic');
    return DeviceCatalog.kindColor(kind);
  }

  // ── Mgmt cloud: API pubblica ──────────────────────────────────────
  function setMgmt(cfg) {
    if (!cy) return;
    const pos = (cfg && cfg.pos) || _defaultMgmtPos();
    const subnet    = (cfg && cfg.subnet)    || '';
    const gw        = (cfg && cfg.gw)        || '';
    const subnet_v6 = (cfg && cfg.subnet_v6) || '';
    const gw_v6     = (cfg && cfg.gw_v6)     || '';
    const existing = cy.getElementById(MGMT_ID);
    const labelBase = 'mgmt';
    const labelLines = [labelBase];
    labelLines.push(`net4  ${subnet} - gw4  ${gw}`);
    labelLines.push(`net6  ${subnet_v6} - gw6  ${gw_v6}`);
    const label = labelLines.join('\n');
    const labelWidth = _mgmtLabelWidth(labelLines);

    if (existing.length) {
      existing.data('label', label);
      existing.data('subnet', subnet);
      existing.data('gw', gw);
      existing.data('subnet_v6', subnet_v6);
      existing.data('gw_v6', gw_v6);
      existing.style('text-max-width', `${labelWidth}px`);
      if (cfg && cfg.pos) existing.position(pos);
    } else {
      const iconUrl = DeviceCatalog.icon('oob') || 'img/devices/oob.svg';
      const node = cy.add({
        group: 'nodes',
        data: {
          id:     MGMT_ID,
          label,
          kind:   '_mgmt',
          subnet, gw, subnet_v6, gw_v6,
          icon:   _fallbackCloudIcon(),
          color:  '#6b7a90',
        },
        classes: MGMT_NODE_CLS,
        position: pos,
      });
      node.style('text-max-width', `${labelWidth}px`);
      // Hydrate using the dedicated OOB icon so it matches the sidebar card.
      _hydrateCloudIcon(cy.getElementById(MGMT_ID), iconUrl);
    }
    _applyMgmtVisibility();
    _refreshMgmtEdges();
  }

  function _mgmtLabelWidth(lines) {
    const maxLen = Math.max(...(lines || []).map(line => String(line || '').length), 0);
    return Math.max(180, Math.min(520, Math.ceil(maxLen * 5.8) + 28));
  }

  function clearMgmt() {
    if (!cy) return;
    const node = cy.getElementById(MGMT_ID);
    if (node.length) node.remove();
    cy.edges(`.${MGMT_EDGE_CLS}`).remove();
  }

  function hasMgmt() {
    return !!(cy && cy.getElementById(MGMT_ID).length);
  }

  function getMgmtPosition() {
    if (!cy) return null;
    const node = cy.getElementById(MGMT_ID);
    if (!node.length) return null;
    const p = node.position();
    return { x: Math.round(p.x), y: Math.round(p.y) };
  }

  function setMgmtVisible(v) {
    _mgmtVisible = !!v;
    _applyMgmtVisibility();
    _refreshNodeLabels();
  }

  function setRealNetInfoVisible(v) {
    _realNetInfoVisible = !!v;
    localStorage.setItem('dnlab-realnet-info-visible', _realNetInfoVisible ? '1' : '0');
    _refreshNodeLabels();
  }

  function toggleRealNetInfoVisible() {
    setRealNetInfoVisible(!_realNetInfoVisible);
  }

  function setRealNetRemoteAs(asn) {
    _realNetRemoteAs = asn ? String(asn) : '';
    _refreshNodeLabels();
  }

  function isMgmtId(id) { return _isMgmtId(id); }

  // ── Mgmt cloud: interni ───────────────────────────────────────────
  function _defaultMgmtPos() {
    // Space in the top-left of the canvas, outside other nodes.
    const ext = cy && cy.extent ? cy.extent() : { x1: 0, y1: 0, x2: 600, y2: 400 };
    return { x: ext.x1 + 80, y: ext.y1 + 80 };
  }

  function _fallbackCloudIcon() {
    const svg = `<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE svg>
<svg xmlns="http://www.w3.org/2000/svg" width="${NODE_ICON_SIZE}" height="${NODE_ICON_SIZE}" viewBox="0 0 ${NODE_ICON_SIZE} ${NODE_ICON_SIZE}">
  <circle cx="28" cy="28" r="27" fill="#6b7a90"/>
</svg>`;
    return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
  }

  async function _hydrateCloudIcon(node, iconUrl) {
    try {
      const icon = await _loadNodeIcon(iconUrl, '#6b7a90');
      if (!node.removed()) node.data('icon', icon);
    } catch (err) {
      console.warn(err);
    }
  }

  function _refreshMgmtEdges() {
    if (!cy) return;
    // Drop all mgmt edges, including legacy/stray ones that lost the class.
    cy.edges().filter(e => _isMgmtEdgeLike(e)).remove();
    const mgmt = cy.getElementById(MGMT_ID);
    if (!mgmt.length) return;
    cy.nodes().forEach(n => {
      const id = n.data('id');
      if (_isMgmtId(id)) return;
      const kind = n.data('kind') || 'linux';
      if (kind === '_real_net') return;
      const iface = DeviceCatalog.kindMgmtIface(kind);
      if (!iface) return;   // es. bridge: no mgmt
      cy.add({
        group: 'edges',
        data: {
          id:     `__mgmt__${id}`,
          source: id,
          target: MGMT_ID,
          label:  iface,
        },
        classes: MGMT_EDGE_CLS,
      });
    });
    _applyMgmtVisibility();
  }

  function _applyMgmtVisibility() {
    if (!cy) return;
    const node = cy.getElementById(MGMT_ID);
    const display = _mgmtVisible ? 'element' : 'none';
    if (node.length) node.style('display', display);
    cy.edges(`.${MGMT_EDGE_CLS}`).style('display', display);
  }

  return {
    init, loadTopology, getTopologyData,
    addNode, updateNode, removeNode, renameNode,
    addEdge, removeEdge, removeEdgeById, getSelected,
    setActiveCaptures, setFollowRabbitSessions,
    setMode, setTheme, setInterfaceResolver,
    fit, clear, on, projectPosition,
    setMgmt, clearMgmt, hasMgmt, getMgmtPosition, setMgmtVisible, isMgmtId,
    setRealNetInfoVisible, toggleRealNetInfoVisible, setRealNetRemoteAs,
    setWebUIRuntime, setMgmtRuntime, setPlacementRuntime, setNodeRuntime,
    MGMT_ID,
  };
})();
