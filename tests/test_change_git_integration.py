from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "dnlab_changes_git_integration", PROJECT_ROOT / "scripts" / "changes.py"
)
assert SPEC and SPEC.loader
changes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(changes)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def initialize_repository(repo: Path) -> str:
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Change Integration Test")
    git(repo, "config", "user.email", "change-test@example.invalid")
    source = repo / "src" / "app.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    git(repo, "add", "src/app.py")
    git(repo, "commit", "-q", "-m", "baseline")
    return git(repo, "rev-parse", "HEAD")


def commit_product_operation(repo: Path, operation: str) -> str:
    source = repo / "src" / "app.py"
    if operation == "add":
        changed = repo / "src" / "added.py"
        changed.write_text("ADDED = True\n", encoding="utf-8")
    elif operation == "modify":
        source.write_text("VALUE = 2\n", encoding="utf-8")
        changed = source
    elif operation == "remove":
        source.unlink()
        changed = source
    else:  # pragma: no cover - the parameter list is closed below
        raise AssertionError(operation)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", f"{operation} product code")
    return str(changed.relative_to(repo))


def commit_fragment(repo: Path, operation: str) -> Path:
    fragment_id = f"20260715-integration-{operation}"
    path = repo / "changes" / "pending" / f"{fragment_id}.yml"
    path.parent.mkdir(parents=True)
    data = {
        "id": fragment_id,
        "type": "change",
        "title": f"Track code {operation}",
        "description": f"Records the integration-test code {operation} operation.",
        "components": ["gui"],
        "audience": ["developer"],
        "visibility": "internal",
        "breaking": False,
        "upgrade_notes": [],
        "references": [],
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    git(repo, "add", str(path.relative_to(repo)))
    git(repo, "commit", "-q", "-m", f"record {operation} change")
    return path


@pytest.mark.parametrize("operation", ["add", "modify", "remove"])
def test_real_git_code_operations_require_and_accept_a_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    base = initialize_repository(tmp_path)
    changed_path = commit_product_operation(tmp_path, operation)
    monkeypatch.setattr(changes, "ROOT", tmp_path)

    with pytest.raises(changes.ChangeError, match="change coverage failed"):
        changes.validate_coverage(base)

    fragment_path = commit_fragment(tmp_path, operation)
    changes.validate_coverage(base)

    changed = git(tmp_path, "diff", "--name-only", f"{base}...HEAD").splitlines()
    assert changed_path in changed
    assert str(fragment_path.relative_to(tmp_path)) in changed
