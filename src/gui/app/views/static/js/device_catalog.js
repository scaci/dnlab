/**
 * DeviceCatalog – single client-side source of truth for device kind / vendor /
 * and graphical representation.
 *
 * Loaded at startup from `/config/devices.json`; if the fetch fails,
 * a minimal built-in fallback (router/generic) is used so the GUI remains
 * usable.
 *
 * Adding new devices must be done **only** in that JSON — no JS module needs
 * to be touched, as long as the vendor and type are already known (or are
 * added to the JSON as well, along with the corresponding SVG).
 *
 *
 * API:
 *   await DeviceCatalog.load()            — call once during bootstrap
 *   DeviceCatalog.kindLabel(kind)         — "vMX", "XRv9k", …
 *   DeviceCatalog.kindIcon(kind)          — SVG URL
 *   DeviceCatalog.kindVendor(kind)        — "cisco" | "juniper" | …
 *   DeviceCatalog.kindColor(kind)         — vendor hex color
 *   DeviceCatalog.vendorColor(vendor)
 *   DeviceCatalog.vendorTitle(vendor)
 */
const DeviceCatalog = (() => {
  const FALLBACK = {
    defaults: { type: 'router', vendor: 'generic' },
    vendors: {
      generic: { title: 'Other', color: '#888888' },
    },
    icons: {
      router: 'img/devices/router.svg',
      cloud: 'img/devices/cloud.svg',
      oob: 'img/devices/oob.svg',
    },
    overrides: {},
    node_features: {},
    kinds: {},
  };

  let _cfg = FALLBACK;
  let _loadPromise = null;

  function load() {
    if (_loadPromise) return _loadPromise;
    _loadPromise = fetch('config/devices.json', { credentials: 'same-origin' })
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(cfg => {
        _cfg = _mergeWithFallback(cfg);
      })
      .catch(err => {
        console.warn('DeviceCatalog: unable to load config/devices.json — using built-in fallback', err);
        _cfg = FALLBACK;
      });
    return _loadPromise;
  }

  function _mergeWithFallback(cfg) {
    return {
      defaults: { ...FALLBACK.defaults, ...(cfg.defaults || {}) },
      vendors:  { ...FALLBACK.vendors,  ...(cfg.vendors  || {}) },
      icons:    { ...FALLBACK.icons,    ...(cfg.icons    || {}) },
      overrides: { ...(cfg.overrides || {}) },
      node_features: { ...(cfg.node_features || {}) },
      kinds:    { ...(cfg.kinds || {}) },
    };
  }

  function _kindEntry(kind) {
    return (kind && _cfg.kinds[kind]) || null;
  }

  function kindLabel(kind) {
    const e = _kindEntry(kind);
    return (e && e.label) || kind || '';
  }

  function kindVendor(kind) {
    const e = _kindEntry(kind);
    return (e && e.vendor) || _cfg.defaults.vendor;
  }

  function kindType(kind) {
    const e = _kindEntry(kind);
    return (e && e.type) || _cfg.defaults.type;
  }

  function kindIcon(kind) {
    const type = kindType(kind);
    return _cfg.icons[type] || _cfg.icons[_cfg.defaults.type] || '';
  }

  function icon(name) {
    return _cfg.icons[name] || '';
  }

  function kindMgmtIface(kind) {
    if (kind === '_real_net' || kind === '_mgmt') return null;
    const e = _kindEntry(kind);
    if (!e) return _cfg.defaults.mgmt_iface || 'eth0';
    // null in config -> device without a mgmt iface (for example bridge): return null.
    return ('mgmt_iface' in e) ? e.mgmt_iface : (_cfg.defaults.mgmt_iface || 'eth0');
  }

  // List [{scheme, port, path?, label?}] of Web UI entries for the kind.
  // Returns an empty array if not configured — the frontend still shows
  // the button when the user has added custom ports via Properties
  // (node.extra.webui_ports).
  function kindWebUI(kind) {
    const e = _kindEntry(kind);
    if (!e || !Array.isArray(e.webui)) return [];
    return e.webui.map(w => ({
      scheme: w.scheme || 'https',
      port:   w.port,
      path:   w.path || '/',
      label:  w.label || `${w.scheme || 'https'}:${w.port}`,
    }));
  }

  function kindDefaultEnv(kind) {
    const e = _kindEntry(kind);
    if (!e || !e.env || typeof e.env !== 'object' || Array.isArray(e.env)) return {};
    return { ...e.env };
  }

  function kindOverrides(kind) {
    const e = _kindEntry(kind);
    const key = e && e.override;
    if (!key) return null;
    const cfg = _cfg.overrides[key];
    return cfg ? { key, ...cfg } : null;
  }

  function kindNodeFeatures(kind) {
    const e = _kindEntry(kind);
    const keys = e && e.node_features;
    const enabled = Array.isArray(keys) ? keys : (keys ? [keys] : []);
    return enabled
      .map(key => {
        const cfg = _cfg.node_features[key];
        return cfg ? { key, ...cfg } : null;
      })
      .filter(Boolean);
  }

  function kindColor(kind) {
    return vendorColor(kindVendor(kind));
  }

  function vendorColor(vendor) {
    const v = _cfg.vendors[vendor] || _cfg.vendors[_cfg.defaults.vendor];
    return (v && v.color) || '#888888';
  }

  function vendorTitle(vendor) {
    const v = _cfg.vendors[vendor];
    return (v && v.title) || vendor || '?';
  }

  return {
    load,
    kindLabel, kindVendor, kindType, kindIcon, kindColor, kindMgmtIface, kindWebUI, kindOverrides,
    kindNodeFeatures,
    kindDefaultEnv, vendorColor, vendorTitle, icon,
  };
})();
