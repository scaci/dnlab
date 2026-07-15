# Contributing to dNLab

Thank you for contributing to dNLab.

## License

dNLab is licensed under the GNU Affero General Public License v3.0 or later
(`AGPL-3.0-or-later`). By contributing to this project, you agree that your
contribution is submitted under `AGPL-3.0-or-later`.

Contributors keep the copyright in their contributions. The project does not
require copyright assignment.

Unless a separate written agreement says otherwise, external contributions are
accepted only under `AGPL-3.0-or-later`.

## Developer Certificate of Origin

All contributions must comply with the Developer Certificate of Origin 1.1
(`DCO`). The DCO is a lightweight certification that you have the right to
submit your contribution under the project's license.

The full DCO text is available in [DCO](../DCO).

## Sign-Off Requirement

Every commit must include a `Signed-off-by` line using your real name and email
address:

```text
Signed-off-by: Your Name <you@example.com>
```

The easiest way to add this line is to commit with `-s`:

```bash
git commit -s
```

Pull requests with unsigned commits may be blocked until the sign-off is added.

## Structured change records

Every logical change must include one YAML fragment in `changes/pending/`.
Fragments are version-independent: the release version is assigned only when
the fragment is consumed. A pull request can contain multiple fragments. If a
change has no product or release-note impact, use a motivated `none` fragment
instead of silently skipping the record.

Install the pinned change-tool dependency once in the development environment:

```bash
python3 -m pip install -r changes/requirements.txt
```

This one-time installation provides the pinned YAML parser used by the change
tool. Then create fragments with the repository command rather than copying
identifiers:

```bash
python3 scripts/changes.py new \
  --type bugfix \
  --title "Keep stopped node state" \
  --description "Stopped per-VD nodes remain stopped during reconciliation." \
  --component multinode \
  --audience operator \
  --reference issue:123
```

Use one of these types:

- `bugfix`, `feature`, `change`, `deprecation`, `removal`, or `security` for
  public product changes;
- `new-vd` for a new virtual device, with both `--vendor` and `--platform`;
- `internal` for implementation changes that should not appear in the public
  changelog;
- `none` for a motivated exemption, with `--reason`.

`visibility: internal` controls changelog rendering only. It is not an access
control mechanism: never place confidential data or private tracker details in
a fragment committed to this repository.

### Examples

New user-facing function:

```bash
python3 scripts/changes.py new \
  --type feature \
  --title "Restart individual nodes" \
  --description "Operators can restart one per-VD node without redeploying its lab." \
  --component gui --component multinode \
  --audience user --audience operator
```

New virtual device:

```bash
python3 scripts/changes.py new \
  --type new-vd \
  --title "Add ExampleOS router" \
  --description "The device catalog and image builder support ExampleOS routers." \
  --component gui --component image-build \
  --audience admin --audience user \
  --vendor Example --platform ExampleOS
```

Breaking feature change:

```bash
python3 scripts/changes.py new \
  --type change \
  --title "Rename the runtime key" \
  --description "Runtime configuration now uses the new key name." \
  --component multinode --audience admin \
  --breaking --upgrade-note "Replace old_key with new_key before upgrading."
```

Motivated exemption:

```bash
python3 scripts/changes.py new \
  --type none \
  --title "Refresh CI comments" \
  --description "Only comments in the CI workflow changed." \
  --component release --audience developer \
  --reason "No runtime, packaging, or documentation behavior changed."
```

Validate and preview all pending public changes before opening a pull request:

```bash
python3 scripts/changes.py validate
python3 scripts/changes.py preview
```

The CI coverage gate uses the actual Git diff and detects added, modified, and
removed code. It proves that a pull request with product changes introduces at
least one valid fragment, or a motivated `none` exemption; release pull
requests may satisfy the same rule by moving validated fragments into a version
archive.

No automated diff analysis can prove that one fragment exists for every
*logical* change or that its wording accurately describes behavior. The author
and reviewer must therefore compare the logical changes listed in the pull
request with the fragment IDs and changelog preview using the pull-request
checklist. CI enforces presence and structure; review enforces completeness and
meaning.

### Preparing a release

The release command validates and archives every pending fragment, creates
`docs/releases/X.Y.Z.yml`, regenerates `CHANGELOG.md`, and writes immutable
release-note assets under the ignored `dist/release/X.Y.Z/` directory:

```bash
python3 scripts/changes.py release \
  --version X.Y.Z \
  --date YYYY-MM-DD \
  --summary "Short release summary."
```

Add `--with-lxc` only when the release publishes the Proxmox LXC template and
its release notes. Start the first run from a clean worktree so only reviewed,
committed fragments enter the release. Review the generated text, commit the
manifest, archived fragments and changelog, then create tag `vX.Y.Z`. Re-running
the same command after archiving is idempotent as long as no new pending
fragments exist.

## Commercial Relicensing

The DCO does not grant dNLab maintainers a separate right to relicense external
contributions under commercial terms. Any commercial licensing arrangement that
includes code contributed by third parties requires a separate written agreement
covering that code.
