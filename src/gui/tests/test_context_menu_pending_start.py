from pathlib import Path


def test_pending_runtime_node_can_be_started_from_context_menu():
    source = (
        Path(__file__).parents[1]
        / "app/views/static/js/context_menu.js"
    ).read_text()

    assert "const isPendingRuntime = isLabRunning" in source
    assert "runtimeState === 'missing'" in source
    assert "const isManagedRuntime = hasManagedRuntime || isPendingRuntime" in source
    assert "const isStartable = isPendingRuntime" in source
    assert "Apply lab changes and start VD" in source
