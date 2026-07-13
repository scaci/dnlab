from __future__ import annotations

from pathlib import Path


def test_dnlab_runtime_never_invokes_containerlab_save_snapshot_or_restore():
    root = Path(__file__).resolve().parents[3]
    forbidden = [
        "containerlab " + command
        for command in ("save", "snapshot", "restore")
    ]
    scanned_roots = [
        root / "src" / "multinode" / "dnlab_multinode",
        root / "src" / "gui" / "app",
    ]
    offenders: list[str] = []

    for scanned_root in scanned_roots:
        for path in scanned_root.rglob("*"):
            if path.suffix not in {".py", ".sh", ".js"} or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for needle in forbidden:
                if needle in text:
                    offenders.append(f"{path.relative_to(root)}: {needle}")

    assert offenders == []
