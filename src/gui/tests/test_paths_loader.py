from app.services import paths as paths_module


def test_load_accepts_lab_cleanup_state_without_unknown_key_warning(tmp_path, monkeypatch, caplog):
    paths_file = tmp_path / "paths.yml"
    paths_file.write_text(
        "lab_cleanup_state: /var/lib/dnlab-lab-cleanup/state.json\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DNLAB_PATHS_FILE", str(paths_file))

    loaded = paths_module._load()

    assert loaded.lab_cleanup_state == "/var/lib/dnlab-lab-cleanup/state.json"
    assert "Ignoring unknown keys" not in caplog.text
