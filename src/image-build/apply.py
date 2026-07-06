#!/usr/bin/env python3
"""Apply dnlab patches to an upstream vrnetlab image.

Usage:
    apply.py <kind> <upstream_image> [--tag-suffix=-dnlab] [--dry-run]

Example:
    apply.py cisco_n9kv vrnetlab/cisco_n9kv_v2:9300-10.5.5.M

Produces ``vrnetlab/cisco_n9kv_v2:9300-10.5.5.M-dnlab``.

Strategy (Level 1 — convention-based wrapper):
  1. Spawn a throwaway container from the upstream image
  2. Extract the target files (launch.py, vrnetlab.py) into a build dir
  3. Apply kind-specific text transforms from ``patches/<kind>.py``
  4. Build a new image ``FROM <upstream>`` that ``COPY``s the patched
     files back to ``/``

Idempotent: re-running on an already-patched upstream is detected via
the provenance marker injected by ``patches/_common.py`` and silently
succeeds without modifying the file.
"""

from __future__ import annotations

import argparse
import importlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


TAG_SUFFIX_DEFAULT = "-dnlab"


def _run(cmd: list[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(cmd)}", file=sys.stderr)
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def _image_exists_locally(image: str) -> bool:
    res = _run(["docker", "image", "inspect", image], check=False, capture=True)
    return res.returncode == 0


def _extract_files(image: str, files: list[str], dest: Path) -> None:
    """Copy ``files`` from a throwaway container of ``image`` into ``dest``."""
    res = _run(["docker", "create", image], capture=True)
    container_id = res.stdout.strip()
    try:
        for src in files:
            out = dest / Path(src).name
            _run(["docker", "cp", f"{container_id}:{src}", str(out)])
    finally:
        _run(["docker", "rm", "-f", container_id], check=False, capture=True)


def _build_patched(
    upstream: str,
    tag: str,
    build_dir: Path,
    patched_files: dict[str, Path],
) -> None:
    dockerfile = build_dir / "Dockerfile"
    lines = [f"FROM {upstream}", ""]
    for target_path, local_file in patched_files.items():
        lines.append(f"COPY {local_file.name} {target_path}")
    dockerfile.write_text("\n".join(lines) + "\n")
    _run(["docker", "build", "-t", tag, str(build_dir)])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("kind", help="vrnetlab kind, e.g. cisco_n9kv")
    ap.add_argument("image", help="upstream image ref, e.g. vrnetlab/cisco_n9kv_v2:9300-10.5.5.M")
    ap.add_argument("--tag-suffix", default=TAG_SUFFIX_DEFAULT)
    ap.add_argument("--dry-run", action="store_true",
                    help="Extract + patch files and print the resulting diff; do not build.")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).parent))
    try:
        mod = importlib.import_module(f"patches.{args.kind}")
    except ModuleNotFoundError:
        print(f"error: no patcher module for kind '{args.kind}' "
              f"(expected patches/{args.kind}.py)", file=sys.stderr)
        return 2

    if not _image_exists_locally(args.image):
        print(f"error: image {args.image!r} not present locally — pull it first.",
              file=sys.stderr)
        return 2

    out_tag = f"{args.image}{args.tag_suffix}"
    print(f"[{mod.KIND}] upstream={args.image} -> {out_tag}", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="dnlab-image-build-") as td:
        build_dir = Path(td)
        _extract_files(args.image, mod.FILES, build_dir)

        patched: dict[str, Path] = {}
        for target in mod.FILES:
            local = build_dir / Path(target).name
            original = local.read_text()
            new_text, notes = mod.apply(target, original)
            for n in notes:
                print(f"  {n}", file=sys.stderr)
            if new_text != original:
                local.write_text(new_text)
            patched[target] = local

        if args.dry_run:
            print("--- dry run: skipping docker build ---", file=sys.stderr)
            for target, local in patched.items():
                print(f"# {target}\n{local.read_text()}\n")
            return 0

        _build_patched(args.image, out_tag, build_dir, patched)

    print(f"built {out_tag}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
