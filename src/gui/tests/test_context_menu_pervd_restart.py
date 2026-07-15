from pathlib import Path


STATIC_JS = Path(__file__).parents[1] / "app" / "views" / "static" / "js"
STATIC_ROOT = STATIC_JS.parent


def test_restart_is_exposed_only_for_managed_per_vd_nodes():
    menu = (STATIC_JS / "context_menu.js").read_text(encoding="utf-8")
    app = (STATIC_JS / "app.js").read_text(encoding="utf-8")
    api = (STATIC_JS / "api.js").read_text(encoding="utf-8")

    assert "const canRestart = isLabRunning && isPerVdRuntime" in menu
    assert "data-action=\"restart-vd\"" in menu
    assert "ContextMenu.on('restart-vd'" in app
    assert "restartNode(currentLabId, nodeName)" in app
    assert "/restart`" in api


def test_context_menu_uses_backend_hot_add_capability():
    menu = (STATIC_JS / "context_menu.js").read_text(encoding="utf-8")
    canvas = (STATIC_JS / "canvas.js").read_text(encoding="utf-8")

    assert "!!nodeData.can_start" in menu
    assert "node.data('can_start', !!info.can_start)" in canvas


def test_start_is_optimistic_stop_is_force_capable_and_gear_is_overlaid():
    menu = (STATIC_JS / "context_menu.js").read_text(encoding="utf-8")
    canvas = (STATIC_JS / "canvas.js").read_text(encoding="utf-8")
    app = (STATIC_JS / "app.js").read_text(encoding="utf-8")
    css = (STATIC_ROOT / "css" / "canvas.css").read_text(encoding="utf-8")
    gear = STATIC_ROOT / "img" / "status" / "runtime-gear.svg"

    assert "!!nodeData.can_stop" in menu
    assert "Canvas.setNodeOperation(nodeName, 'starting', true)" in app
    assert "nodeName, activeStart ? 'cancelling' : 'stopping', !!activeStart" in app
    assert "Canvas.hasActiveNodeOperations() ? 5000 : 15000" in app
    assert "runtime-gear-layer" in canvas
    assert 'src="/img/status/runtime-gear.svg"' in canvas
    assert 'src="/static/img/status/runtime-gear.svg"' not in canvas
    assert "node.renderedPosition()" in canvas
    assert "@keyframes runtime-gear-spin" in css
    assert "transform-origin: 50% 50%" in css
    assert gear.is_file()
    assert 'transform="translate(0 2.29)"' in gear.read_text(encoding="utf-8")


def test_link_addition_reconciles_live_and_keeps_desired_state_errors():
    app = (STATIC_JS / "app.js").read_text(encoding="utf-8")
    api = (STATIC_JS / "api.js").read_text(encoding="utf-8")

    assert "reconcileLink: (id, link)" in api
    assert "await _applyLiveLink(link)" in app
    assert "Link saved but runtime activation failed" in app
