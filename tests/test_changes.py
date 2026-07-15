from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dnlab_changes", ROOT / "scripts" / "changes.py")
assert SPEC and SPEC.loader
changes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(changes)


def fragment(**overrides):
    value = {
        "id": "20260715-example-change",
        "type": "feature",
        "title": "Example change",
        "description": "Adds an example capability.",
        "components": ["gui"],
        "audience": ["user"],
        "visibility": "public",
        "breaking": False,
        "upgrade_notes": [],
        "references": ["commit:abc1234"],
    }
    value.update(overrides)
    return value


def test_valid_fragment_and_closed_schema():
    assert changes.validate_fragment(fragment()) == []
    errors = changes.validate_fragment(fragment(unexpected=True))
    assert any("unknown fields: unexpected" in error for error in errors)
    errors = changes.validate_fragment(fragment(components=["gui", "gui"]))
    assert any("components must not contain duplicates" in error for error in errors)


def test_published_schema_matches_runtime_contract():
    schema = json.loads((ROOT / "changes" / "schema.json").read_text())
    properties = schema["properties"]
    assert tuple(properties["type"]["enum"]) == changes.CHANGE_TYPES
    assert tuple(properties["visibility"]["enum"]) == changes.VISIBILITIES
    assert tuple(properties["audience"]["items"]["enum"]) == changes.AUDIENCES
    assert properties["id"]["pattern"] == changes.ID_RE.pattern
    assert properties["components"]["items"]["pattern"] == changes.COMPONENT_RE.pattern


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"type": "unknown"}, "type must be one of"),
        ({"visibility": "secret"}, "visibility must be public or internal"),
        ({"breaking": True}, "breaking changes require"),
        ({"components": []}, "components must be a non-empty"),
        ({"audience": ["customer"]}, "invalid audience"),
        ({"type": "internal"}, "internal fragments must use internal visibility"),
    ],
)
def test_invalid_fragment_rules(updates, message):
    assert any(message in error for error in changes.validate_fragment(fragment(**updates)))


def test_new_vd_requires_vendor_and_platform():
    errors = changes.validate_fragment(fragment(type="new-vd"))
    assert any("require vd.vendor" in error for error in errors)
    assert changes.validate_fragment(
        fragment(type="new-vd", vd={"vendor": "Cisco", "platform": "C9800-CL"})
    ) == []


def test_none_requires_reason_and_internal_visibility():
    errors = changes.validate_fragment(fragment(type="none"))
    assert any("none fragments require a reason" in error for error in errors)
    assert changes.validate_fragment(
        fragment(type="none", visibility="internal", reason="CI-only metadata update.")
    ) == []


@pytest.mark.parametrize(
    "extra",
    [
        ["--vendor", "Example", "--platform", "ExampleOS"],
        ["--reason", "Not applicable."],
    ],
)
def test_new_rejects_type_specific_options_on_a_feature(extra):
    args = changes.parser().parse_args(
        [
            "new",
            "--type",
            "feature",
            "--title",
            "Example change",
            "--description",
            "Adds an example capability.",
            "--component",
            "gui",
            "--audience",
            "user",
            *extra,
        ]
    )
    with pytest.raises(changes.ChangeError, match="only valid"):
        changes.command_new(args)


def test_duplicate_ids_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(changes, "ROOT", tmp_path)
    first = tmp_path / "changes" / "pending" / "20260715-example-change.yml"
    second = tmp_path / "changes" / "archive" / "1.0.0" / "20260715-example-change.yml"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    text = yaml.safe_dump(fragment(), sort_keys=False)
    first.write_text(text)
    second.write_text(text)
    with pytest.raises(changes.ChangeError, match="duplicate id"):
        changes.load_fragments([first, second])


def test_multiple_distinct_fragments_are_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(changes, "ROOT", tmp_path)
    directory = tmp_path / "changes" / "pending"
    directory.mkdir(parents=True)
    first = directory / "20260715-example-change.yml"
    second = directory / "20260715-second-change.yml"
    first.write_text(yaml.safe_dump(fragment(), sort_keys=False))
    second.write_text(
        yaml.safe_dump(fragment(id="20260715-second-change", title="Second change"), sort_keys=False)
    )
    assert len(changes.load_fragments([first, second])) == 2


def test_post_012_backfill_contains_the_five_required_commits():
    required = {
        "d5e9bcec589c60b7a50fb5a979dfe3bc0b74850e",
        "c8c55e73588236b45a1e369b4ccb61ee01b57d79",
        "1c58fed5a58beee3380598b0e6f6f4fc5a37ce78",
        "240047a256fb6836ad3385c40eb3139473ac4ff1",
        "5234c0b9d77f0b12434d6b52ed9f58e5bb548931",
    }
    loaded = changes.load_fragments(changes.fragment_paths())
    referenced = {
        reference.removeprefix("commit:")
        for _, item in loaded
        for reference in item["references"]
        if reference.startswith("commit:")
    }
    assert required <= referenced


def test_preview_golden_order_and_visibility():
    items = [
        fragment(id="20260715-fix-one", type="bugfix", title="Fix one", description="Fixed it."),
        fragment(id="20260715-add-one", title="Add one", description="Added it."),
        fragment(
            id="20260715-internal-one",
            type="internal",
            title="Refactor internals",
            description="No public behavior changed.",
            visibility="internal",
            audience=["developer"],
        ),
    ]
    expected = (
        "# Unreleased\n\n"
        "### Added\n\n"
        "- Add one: Added it.\n\n"
        "### Fixed\n\n"
        "- Fix one: Fixed it.\n"
    )
    assert changes.render_preview(items) == expected
    assert changes.render_preview(reversed(items)) == expected


@pytest.mark.parametrize(
    ("change_type", "category"),
    [
        ("feature", "Added"),
        ("new-vd", "New Virtual Devices"),
        ("change", "Changed"),
        ("deprecation", "Deprecated"),
        ("removal", "Removed"),
        ("bugfix", "Fixed"),
        ("security", "Security"),
    ],
)
def test_every_public_type_maps_to_its_release_section(change_type, category):
    overrides = {"type": change_type}
    if change_type == "new-vd":
        overrides["vd"] = {"vendor": "Example", "platform": "ExampleOS"}
    item = fragment(**overrides)
    assert changes.validate_fragment(item) == []
    rendered = changes.render_preview([item])
    assert f"### {category}\n" in rendered


def test_breaking_change_renders_warning_and_upgrade_notes():
    item = fragment(
        type="change",
        breaking=True,
        upgrade_notes=["Replace old_key with new_key before upgrading."],
    )
    manifest = changes.build_manifest(
        "1.2.3", "2026-07-15", "Breaking release.", [item], False
    )
    rendered = changes.render_release_sections(manifest)
    assert "### Changed\n" in rendered
    assert "### Breaking Changes\n" in rendered
    assert "### Upgrade Notes\n" in rendered
    assert "Replace old_key with new_key before upgrading." in rendered


def test_release_artifact_profiles_are_explicit():
    standard = changes.artifacts_for("1.2.3", False)
    assert [(item["name"], item["path"]) for item in standard] == [
        ("Source archives", "*-1.2.3-source.tar.gz"),
        ("Source checksums", "SHA256SUMS"),
    ]
    with_lxc = changes.artifacts_for("1.2.3", True)
    assert with_lxc[:2] == standard
    assert [(item["name"], item["path"]) for item in with_lxc[2:]] == [
        ("Proxmox LXC template", "dnlab-lxc-proxmox-1.2.3-amd64.tar.zst"),
        ("Proxmox LXC release notes", "LXC-RELEASE-NOTES-1.2.3.md"),
    ]


def test_coverage_requires_an_added_fragment(monkeypatch):
    monkeypatch.setattr(
        changes,
        "_git_lines",
        lambda *args: ["src/gui/app.py"] if "--diff-filter=ACR" not in args else [],
    )
    with pytest.raises(changes.ChangeError, match="change coverage failed"):
        changes.validate_coverage("base")


def test_coverage_accepts_and_validates_fragment(tmp_path, monkeypatch):
    monkeypatch.setattr(changes, "ROOT", tmp_path)
    path = tmp_path / "changes" / "pending" / "20260715-example-change.yml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(fragment(), sort_keys=False))

    def git_lines(*args):
        if "--diff-filter=ACR" in args:
            return ["changes/pending/20260715-example-change.yml"]
        return ["src/gui/app.py", "changes/pending/20260715-example-change.yml"]

    monkeypatch.setattr(changes, "_git_lines", git_lines)
    changes.validate_coverage("base")


def test_coverage_accepts_multiple_fragments(tmp_path, monkeypatch):
    monkeypatch.setattr(changes, "ROOT", tmp_path)
    directory = tmp_path / "changes" / "pending"
    directory.mkdir(parents=True)
    names = ["20260715-example-change.yml", "20260715-second-change.yml"]
    (directory / names[0]).write_text(yaml.safe_dump(fragment(), sort_keys=False))
    (directory / names[1]).write_text(
        yaml.safe_dump(fragment(id="20260715-second-change", title="Second change"), sort_keys=False)
    )

    def git_lines(*args):
        if "--diff-filter=ACR" in args:
            return [f"changes/pending/{name}" for name in names]
        return ["src/gui/app.py", *(f"changes/pending/{name}" for name in names)]

    monkeypatch.setattr(changes, "_git_lines", git_lines)
    changes.validate_coverage("base")


def test_coverage_accepts_motivated_none_exemption(tmp_path, monkeypatch):
    monkeypatch.setattr(changes, "ROOT", tmp_path)
    relative = "changes/pending/20260715-ci-exemption.yml"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            fragment(
                id="20260715-ci-exemption",
                type="none",
                title="Refresh CI comments",
                description="Only workflow comments changed.",
                visibility="internal",
                audience=["developer"],
                reason="No runtime, packaging, or documentation behavior changed.",
            ),
            sort_keys=False,
        )
    )
    monkeypatch.setattr(
        changes,
        "_git_lines",
        lambda *args: [relative]
        if "--diff-filter=ACR" in args
        else [".github/workflows/ci.yml", relative],
    )
    changes.validate_coverage("base")


def test_coverage_rejects_an_invalid_added_fragment(tmp_path, monkeypatch):
    monkeypatch.setattr(changes, "ROOT", tmp_path)
    relative = "changes/pending/20260715-invalid-change.yml"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    invalid = fragment(id="20260715-invalid-change")
    del invalid["description"]
    path.write_text(yaml.safe_dump(invalid, sort_keys=False))
    monkeypatch.setattr(
        changes,
        "_git_lines",
        lambda *args: [relative]
        if "--diff-filter=ACR" in args
        else ["src/gui/app.py", relative],
    )
    with pytest.raises(changes.ChangeError, match="description must be a non-empty string"):
        changes.validate_coverage("base")


def test_coverage_accepts_a_fragment_renamed_into_a_release_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(changes, "ROOT", tmp_path)
    relative = "changes/archive/1.2.3/20260715-example-change.yml"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(fragment(), sort_keys=False))

    def git_lines(*args):
        if "--diff-filter=ACR" in args:
            assert "--find-renames" in args
            return [relative]
        return ["docs/releases/1.2.3.yml", relative]

    monkeypatch.setattr(changes, "_git_lines", git_lines)
    changes.validate_coverage("base")


def test_first_release_requires_a_clean_git_worktree(tmp_path, monkeypatch):
    monkeypatch.setattr(changes, "ROOT", tmp_path)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(
        changes,
        "_git_lines",
        lambda *args: [" M src/gui/app.py", "?? local-note.txt"],
    )
    with pytest.raises(changes.ChangeError, match="require a clean git worktree") as error:
        changes.ensure_clean_release_worktree()
    assert "src/gui/app.py" in str(error.value)
    assert "local-note.txt" in str(error.value)


def test_release_worktree_check_is_skipped_outside_git(tmp_path, monkeypatch):
    monkeypatch.setattr(changes, "ROOT", tmp_path)
    monkeypatch.setattr(
        changes,
        "_git_lines",
        lambda *args: pytest.fail("git must not be called outside a repository"),
    )
    changes.ensure_clean_release_worktree()


def configure_temp_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(changes, "ROOT", tmp_path)
    monkeypatch.setattr(changes, "PENDING", tmp_path / "changes" / "pending")
    monkeypatch.setattr(changes, "ARCHIVE", tmp_path / "changes" / "archive")
    monkeypatch.setattr(changes, "RELEASES", tmp_path / "docs" / "releases")
    monkeypatch.setattr(changes, "CHANGELOG", tmp_path / "CHANGELOG.md")
    monkeypatch.setattr(changes, "DIST_RELEASES", tmp_path / "dist" / "release")
    changes.PENDING.mkdir(parents=True)
    changes.ARCHIVE.mkdir(parents=True)
    changes.RELEASES.mkdir(parents=True)


def test_new_id_is_unique_across_pending_and_archive(tmp_path, monkeypatch, capsys):
    configure_temp_repo(tmp_path, monkeypatch)

    class FrozenDateTime(changes.dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 15, 12, 34, 56, tzinfo=tz)

    monkeypatch.setattr(changes.dt, "datetime", FrozenDateTime)
    archived = changes.ARCHIVE / "1.2.3" / "20260715-123456-example-change.yml"
    archived.parent.mkdir(parents=True)
    archived.write_text(
        yaml.safe_dump(fragment(id="20260715-123456-example-change"), sort_keys=False)
    )
    args = changes.parser().parse_args(
        [
            "new",
            "--type",
            "feature",
            "--title",
            "Example change",
            "--description",
            "Adds an example capability.",
            "--component",
            "gui",
            "--audience",
            "user",
        ]
    )
    changes.command_new(args)
    created = changes.PENDING / "20260715-123456-example-change-2.yml"
    assert created.exists()
    assert capsys.readouterr().out.strip().endswith(created.name)


def test_release_cycle_and_idempotency(tmp_path, monkeypatch):
    configure_temp_repo(tmp_path, monkeypatch)
    source = changes.PENDING / "20260715-example-change.yml"
    source.write_text(yaml.safe_dump(fragment(), sort_keys=False))
    args = type(
        "Args",
        (),
        {
            "version": "1.2.3",
            "date": "2026-07-15",
            "summary": "Example release.",
            "with_lxc": True,
        },
    )()
    changes.command_release(args)
    first_changelog = changes.CHANGELOG.read_text()
    assert not source.exists()
    assert (changes.ARCHIVE / "1.2.3" / source.name).exists()
    assert (changes.RELEASES / "1.2.3.yml").exists()
    asset_dir = changes.DIST_RELEASES / "1.2.3"
    assert (asset_dir / "CHANGELOG-1.2.3.md").exists()
    assert (asset_dir / "RELEASE_NOTES-1.2.3.md").exists()
    release_manifest = json.loads((asset_dir / "RELEASE-MANIFEST-1.2.3.json").read_text())
    assert release_manifest["changes"] == ["20260715-example-change"]
    assert "dnlab-lxc-proxmox-1.2.3-amd64.tar.zst" in (
        changes.RELEASES / "1.2.3.yml"
    ).read_text()
    changes.command_release(args)
    assert changes.CHANGELOG.read_text() == first_changelog

    args.summary = "A different release."
    with pytest.raises(changes.ChangeError, match="release metadata is immutable"):
        changes.command_release(args)


def test_release_rejects_empty_summary_before_consuming_fragments(tmp_path, monkeypatch):
    configure_temp_repo(tmp_path, monkeypatch)
    pending = changes.PENDING / "20260715-example-change.yml"
    pending.write_text(yaml.safe_dump(fragment(), sort_keys=False))
    args = type(
        "Args",
        (),
        {"version": "1.2.3", "date": "2026-07-15", "summary": "  ", "with_lxc": False},
    )()
    with pytest.raises(changes.ChangeError, match="summary must be a non-empty string"):
        changes.command_release(args)
    assert pending.exists()
    assert not (changes.ARCHIVE / "1.2.3" / pending.name).exists()


def test_release_summary_is_canonical_across_idempotent_runs(tmp_path, monkeypatch):
    configure_temp_repo(tmp_path, monkeypatch)
    pending = changes.PENDING / "20260715-example-change.yml"
    pending.write_text(yaml.safe_dump(fragment(), sort_keys=False))
    args = type(
        "Args",
        (),
        {
            "version": "1.2.3",
            "date": "2026-07-15",
            "summary": "  Example   release.  ",
            "with_lxc": False,
        },
    )()
    changes.command_release(args)
    manifest_path = changes.RELEASES / "1.2.3.yml"
    first = manifest_path.read_bytes()
    assert yaml.safe_load(first)["summary"] == "Example release."
    args.summary = "Example release."
    changes.command_release(args)
    assert manifest_path.read_bytes() == first


def test_release_date_cannot_precede_existing_releases(tmp_path, monkeypatch):
    configure_temp_repo(tmp_path, monkeypatch)
    existing = {
        "version": "1.2.2",
        "date": "2026-07-16",
        "tag": "v1.2.2",
        "summary": "Existing release.",
        "entries": [],
        "breaking_changes": [],
        "upgrade_notes": [],
        "artifacts": changes.artifacts_for("1.2.2", False),
        "source_notes": ["CHANGELOG.md"],
        "internal_refs": [],
    }
    (changes.RELEASES / "1.2.2.yml").write_text(yaml.safe_dump(existing, sort_keys=False))
    pending = changes.PENDING / "20260715-example-change.yml"
    pending.write_text(yaml.safe_dump(fragment(), sort_keys=False))
    args = type(
        "Args",
        (),
        {
            "version": "1.2.3",
            "date": "2026-07-15",
            "summary": "New release.",
            "with_lxc": False,
        },
    )()
    with pytest.raises(changes.ChangeError, match="must not precede"):
        changes.command_release(args)
    assert pending.exists()


def test_release_resumes_after_archive_copy(tmp_path, monkeypatch):
    configure_temp_repo(tmp_path, monkeypatch)
    pending = changes.PENDING / "20260715-example-change.yml"
    archive = changes.ARCHIVE / "1.2.3" / pending.name
    archive.parent.mkdir(parents=True)
    content = yaml.safe_dump(fragment(), sort_keys=False)
    pending.write_text(content)
    archive.write_text(content)
    args = type(
        "Args",
        (),
        {
            "version": "1.2.3",
            "date": "2026-07-15",
            "summary": "Resumed release.",
            "with_lxc": False,
        },
    )()
    changes.command_release(args)
    assert not pending.exists()
    assert archive.exists()
    changes.validate_tag("v1.2.3")


def test_release_resumes_from_a_partial_multi_fragment_archive(tmp_path, monkeypatch):
    configure_temp_repo(tmp_path, monkeypatch)
    first = changes.PENDING / "20260715-example-change.yml"
    second = changes.PENDING / "20260715-second-change.yml"
    first.write_text(yaml.safe_dump(fragment(), sort_keys=False))
    second.write_text(
        yaml.safe_dump(fragment(id="20260715-second-change", title="Second change"), sort_keys=False)
    )
    args = type(
        "Args",
        (),
        {
            "version": "1.2.3",
            "date": "2026-07-15",
            "summary": "Resumed multi-change release.",
            "with_lxc": False,
        },
    )()
    archive_dir = changes.ARCHIVE / "1.2.3"
    archive_dir.mkdir(parents=True)
    (archive_dir / first.name).write_bytes(first.read_bytes())
    marker = archive_dir / ".release-transaction.json"
    marker.write_text(
        json.dumps(changes._release_transaction_payload(args, [first, second]), sort_keys=True)
    )

    changes.command_release(args)

    assert not first.exists()
    assert not second.exists()
    assert not marker.exists()
    assert (archive_dir / first.name).exists()
    assert (archive_dir / second.name).exists()
    changes.validate_tag("v1.2.3")


def test_partial_release_rejects_a_new_untracked_fragment(tmp_path, monkeypatch):
    configure_temp_repo(tmp_path, monkeypatch)
    original = changes.PENDING / "20260715-example-change.yml"
    original.write_text(yaml.safe_dump(fragment(), sort_keys=False))
    args = type(
        "Args",
        (),
        {
            "version": "1.2.3",
            "date": "2026-07-15",
            "summary": "Interrupted release.",
            "with_lxc": False,
        },
    )()
    archive_dir = changes.ARCHIVE / "1.2.3"
    archive_dir.mkdir(parents=True)
    marker = archive_dir / ".release-transaction.json"
    marker.write_text(
        json.dumps(changes._release_transaction_payload(args, [original]), sort_keys=True)
    )
    extra = changes.PENDING / "20260715-late-change.yml"
    extra.write_text(
        yaml.safe_dump(fragment(id="20260715-late-change", title="Late change"), sort_keys=False)
    )

    with pytest.raises(changes.ChangeError, match="unexpected fragments"):
        changes.command_release(args)
    assert original.exists()
    assert extra.exists()


def test_tag_rejects_manifest_archive_drift(tmp_path, monkeypatch):
    configure_temp_repo(tmp_path, monkeypatch)
    pending = changes.PENDING / "20260715-example-change.yml"
    pending.write_text(yaml.safe_dump(fragment(), sort_keys=False))
    args = type(
        "Args",
        (),
        {
            "version": "1.2.3",
            "date": "2026-07-15",
            "summary": "Example release.",
            "with_lxc": False,
        },
    )()
    changes.command_release(args)
    manifest_path = changes.RELEASES / "1.2.3.yml"
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest["entries"][0]["body"] = "Tampered body."
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    with pytest.raises(changes.ChangeError, match="does not match its archived"):
        changes.validate_tag("v1.2.3")


def test_validate_rejects_a_residual_release_transaction(tmp_path, monkeypatch):
    configure_temp_repo(tmp_path, monkeypatch)
    marker = changes.ARCHIVE / "1.2.3" / ".release-transaction.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"fragments": {}}))
    changes.CHANGELOG.write_text(changes.render_changelog([]))
    with pytest.raises(changes.ChangeError, match="incomplete release transaction"):
        changes.validate_all()


def test_existing_manifests_preserve_public_content():
    manifests = changes.load_manifests()
    rendered = changes.render_changelog(manifests)
    published = (ROOT / "CHANGELOG.md").read_text()
    assert rendered == published
    for path in changes.release_paths():
        manifest = changes.load_yaml(path)
        assert str(manifest["summary"]).split()[0] in rendered
        for entry in manifest.get("entries", []):
            assert entry["title"] in rendered


def test_validate_rejects_manual_changelog_drift(tmp_path, monkeypatch):
    configure_temp_repo(tmp_path, monkeypatch)
    manifest = {
        "version": "1.2.3",
        "date": "2026-07-15",
        "tag": "v1.2.3",
        "summary": "Example release.",
        "entries": [],
        "breaking_changes": [],
        "upgrade_notes": [],
        "artifacts": changes.artifacts_for("1.2.3", False),
        "source_notes": ["CHANGELOG.md"],
        "internal_refs": [],
    }
    (changes.RELEASES / "1.2.3.yml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    changes.CHANGELOG.write_text("# Hand-edited changelog\n")
    with pytest.raises(changes.ChangeError, match="do not edit it manually"):
        changes.validate_all()


def test_legacy_breaking_entry_is_not_rendered_twice():
    manifest = {
        "entries": [{"category": "Breaking", "title": "Rename key", "body": "Use new_key."}],
        "breaking_changes": [{"title": "Key changed", "body": "Replace old_key."}],
        "upgrade_notes": [],
        "artifacts": [],
    }
    rendered = changes.render_release_sections(manifest)
    assert "### Breaking\n" in rendered
    assert "### Breaking Changes" not in rendered
