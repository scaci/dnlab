from pathlib import Path
import json
import subprocess


STATIC_DIR = Path(__file__).parents[1] / "app" / "views" / "static"


def test_canvas_topology_export_preserves_node_feature_state():
    canvas = (STATIC_DIR / "js" / "canvas.js").read_text()

    get_topology = canvas.split("function getTopologyData()", 1)[1].split(
        "function addNode", 1
    )[0]
    assert "node_features_state: n.data('node_features_state') || null" in get_topology


def test_local_javascript_assets_are_cache_busted():
    index = (STATIC_DIR / "index.html").read_text()

    local_scripts = [
        line.strip() for line in index.splitlines() if '<script src="/js/' in line
    ]
    assert local_scripts
    assert all("?v=" in script for script in local_scripts)


def test_topology_api_feature_state_wins_over_stale_canvas_state():
    module = STATIC_DIR / "js" / "node_feature_state.js"
    script = f"""
const resolver = require({json.dumps(str(module))});
const api = {{ Labs: {{ getTopology: async () => ({{
  nodes: [{{name: 'frr2', kind: 'frr', image: 'vrnetlab/dnlab_frr:test'}}],
  gui_node_features_state: {{frr2: {{frr_daemons: {{bgpd: true}}}}}},
}}) }} }};
resolver.resolve(api, 'lab-id', {{
  id: 'frr2', kind: 'frr', node_features_state: {{frr_daemons: {{bgpd: false}}}},
}}).then(result => {{
  if (result.node_features_state.frr_daemons.bgpd !== true) process.exit(1);
}}).catch(() => process.exit(2));
"""
    subprocess.run(["node", "-e", script], check=True)


def test_device_catalog_bypasses_browser_cache():
    catalog = (STATIC_DIR / "js" / "device_catalog.js").read_text()

    assert "cache: 'no-store'" in catalog
