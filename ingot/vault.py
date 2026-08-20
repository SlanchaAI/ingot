"""Create the local Git vault the publisher owns.

The local backend needs a Git repository before it can publish anything, and a deployment whose
very first start fails because the directory is empty is a deployment nobody gets past. This is the
one bootstrap command, and it is idempotent so the managed compose can call it on every start."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

VAULT_SCHEMA = "ingot/vault/v1"

# Written into the vault, not imported from it: the publisher runs this with the vault as the
# working directory and nothing but the interpreter guaranteed to be there. It refuses the tree
# rather than the change, so a vault a server could not load can never be committed.
VALIDATOR = '''#!/usr/bin/env python3
"""Refuse a vault tree the skill server could not serve.

Every publication runs this against a fresh worktree before the commit is made."""
import json
import re
import sys
from pathlib import Path

import yaml

FRONTMATTER = re.compile(r"\\A---\\r?\\n(.*?)\\r?\\n---\\r?\\n?", re.DOTALL)
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\\Z")

root = Path(__file__).resolve().parent.parent
problems = []

registry = root / "registry.json"
if registry.is_file():
    try:
        if not isinstance(json.loads(registry.read_text(encoding="utf-8")), dict):
            problems.append("registry.json is not a JSON object")
    except ValueError as exc:
        problems.append(f"registry.json is not valid JSON: {exc}")

for skill_md in sorted(root.glob("*/SKILL.md")):
    name = skill_md.parent.name
    where = f"{name}/SKILL.md"
    if not SLUG.fullmatch(name):
        problems.append(f"{name}: directory name is not a slug")
    text = skill_md.read_text(encoding="utf-8")
    match = FRONTMATTER.match(text)
    if not match:
        problems.append(f"{where}: no YAML frontmatter")
        continue
    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        problems.append(f"{where}: frontmatter is not valid YAML: {exc}")
        continue
    if not isinstance(metadata, dict):
        problems.append(f"{where}: frontmatter is not a mapping")
        continue
    if metadata.get("name") != name:
        problems.append(f"{where}: frontmatter name {metadata.get('name')!r} != directory {name!r}")
    if not str(metadata.get("description", "")).strip():
        problems.append(f"{where}: description is empty")

for problem in problems:
    print(f"invalid: {problem}", file=sys.stderr)
sys.exit(1 if problems else 0)
'''


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    if result.returncode:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "failed"
        verb = next((arg for arg in args if not arg.startswith("-") and "=" not in arg), args[0])
        raise ValueError(f"git {verb}: {detail}")
    return result.stdout.strip()


def _is_repository(path: Path) -> bool:
    result = subprocess.run(["git", "-C", str(path), "rev-parse", "--git-dir"],
                            capture_output=True, text=True)
    return result.returncode == 0


def init_vault(path: Path, *, branch: str = "main") -> dict:
    """Create or complete a vault. Idempotent against one that is already valid.

    A non-empty directory that is not a Git repository is refused rather than adopted: pointing the
    publisher at a directory of loose skills would make its first commit look like a publication
    nobody approved."""
    path = Path(path).expanduser()
    existed = path.exists()
    if existed and not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    if existed and not _is_repository(path) and any(path.iterdir()):
        raise ValueError(f"{path} is not empty and is not a Git repository; "
                         f"initialize an empty directory or point at an existing vault")
    path.mkdir(parents=True, exist_ok=True)
    path = path.resolve()

    created = not _is_repository(path)
    if created:
        _git(path, "init", "-b", branch)

    written = []
    for relative, contents in ((Path("registry.json"), "{}\n"),
                               (Path("scripts") / "validate.py", VALIDATOR)):
        target = path / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
        written.append(relative.as_posix())

    head = None
    if written or created:
        _git(path, "add", "-A", ".")
        if _git(path, "status", "--porcelain"):
            _git(path, "-c", "user.name=Ingot Publisher", "-c", "user.email=ingot@local.invalid",
                 "commit", "-m", "Initialize the Ingot skill vault")
    try:
        head = _git(path, "rev-parse", "HEAD")
    except ValueError:
        # An adopted repository with no commits at all: give it one so a publication branch has a
        # base to be cut from.
        _git(path, "-c", "user.name=Ingot Publisher", "-c", "user.email=ingot@local.invalid",
             "commit", "--allow-empty", "-m", "Initialize the Ingot skill vault")
        head = _git(path, "rev-parse", "HEAD")

    return {
        "schema_version": VAULT_SCHEMA,
        "path": str(path),
        "status": "created" if created else ("updated" if written else "unchanged"),
        "branch": _git(path, "symbolic-ref", "--short", "HEAD"),
        "head": head,
        "added": written,
    }
