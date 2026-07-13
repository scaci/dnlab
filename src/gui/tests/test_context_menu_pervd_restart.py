from pathlib import Path


STATIC_JS = Path(__file__).parents[1] / "app" / "views" / "static" / "js"


def test_restart_is_exposed_only_for_managed_per_vd_nodes():
    menu = (STATIC_JS / "context_menu.js").read_text(encoding="utf-8")
    app = (STATIC_JS / "app.js").read_text(encoding="utf-8")
    api = (STATIC_JS / "api.js").read_text(encoding="utf-8")

    assert "const canRestart = isLabRunning && isPerVdRuntime" in menu
    assert "data-action=\"restart-vd\"" in menu
    assert "ContextMenu.on('restart-vd'" in app
    assert "restartNode(currentLabId, nodeName)" in app
    assert "/restart`" in api
