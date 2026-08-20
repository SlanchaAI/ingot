"""Where each served skill came from.

A merged library hides its own history. Once several roots are mounted together the UI shows
one flat list, so a skill written here looks exactly like one pulled from a third-party repo.
That matters for two questions an operator actually asks: what have I contributed, and whose
licence governs this text.

Four provenances:

- ``authored``  — written in this library.
- ``vendored``  — copied into this library from an upstream repo. The library records these in
  a ``VENDORED.md`` ledger beside the skills; a vendored skill lives in the same root as an
  authored one, so the root alone cannot tell them apart.
- ``fetched``   — pulled by ``scripts/fetch_skills.sh`` into the local ``skills/`` directory.
- ``external``  — served from any other configured root.

The ledger is the only source of truth for ``vendored``. A skill copied in but never recorded
reads as ``authored``, which overstates authorship — the fix is to record it, not to guess here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from ingot import paths

AUTHORED = "authored"
VENDORED = "vendored"
FETCHED = "fetched"
EXTERNAL = "external"

LEDGER_NAME = "VENDORED.md"

# `## threejs-{animation,fundamentals}` and `## a, b` both name several skills in one heading.
_BRACE = re.compile(r"^(?P<stem>[^{]*)\{(?P<options>[^}]*)\}(?P<tail>.*)$")


def _split_top_level(heading: str) -> list[str]:
    """Split on commas that separate entries, not on commas inside a brace group.

    `threejs-{a,b}, other` is two entries, not three: the first two commas belong to the
    brace group. Splitting the raw string first would tear `threejs-{a` off `b}`.
    """
    parts, depth, current = [], 0, []
    for ch in heading:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _expand(heading: str) -> list[str]:
    """Skill names named by one ledger heading.

    Handles both shapes the ledger uses: brace expansion (`threejs-{a,b}` -> `threejs-a`,
    `threejs-b`) and a plain comma-separated list. Anything else is one name.
    """
    names: list[str] = []
    for part in _split_top_level(heading):
        match = _BRACE.match(part)
        if match:
            stem, tail = match.group("stem").strip(), match.group("tail").strip()
            names.extend(f"{stem}{opt.strip()}{tail}"
                         for opt in match.group("options").split(",") if opt.strip())
        else:
            names.append(part)
    return names


def vendored_names(root: Path) -> set[str]:
    """Skill names the root's ``VENDORED.md`` declares as copied in from upstream.

    Returns an empty set when the root publishes no ledger, which is the common case for a
    fetched or external root.
    """
    ledger = root / LEDGER_NAME
    try:
        text = ledger.read_text(encoding="utf-8", errors="ignore")
    except (OSError, ValueError):
        return set()
    names: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("## "):
            continue
        heading = line[3:].strip()
        # Prose sections ("Local divergences from upstream") are not skill names. A real skill
        # slug has no spaces once the comma/brace forms are expanded.
        for name in _expand(heading):
            if name and " " not in name:
                names.add(name)
    return names


def local_root() -> Path:
    """The repository's own ``skills/`` directory, where fetch_skills.sh writes."""
    return paths.library()


def classify(name: str, skill_dir: str | Path, *,
             ledgers: dict[Path, set[str]] | None = None) -> str:
    """Provenance of one skill.

    ``skill_dir`` is ``Skill.root``, which is the skill's OWN directory
    (``/srv/skills/dotfiles/game-dev``), not the library root. The ledger lives one level up,
    beside its sibling skills, so the library root is the parent.

    ``ledgers`` caches each library root's parsed ledger, so a whole inventory costs one read
    per root rather than one per skill.
    """
    library = Path(skill_dir).parent
    if ledgers is None:
        ledgers = {}
    if library not in ledgers:
        ledgers[library] = vendored_names(library)
    if name in ledgers[library]:
        return VENDORED
    try:
        if library.resolve() == local_root().resolve():
            return FETCHED
    except OSError:
        pass
    return AUTHORED if (library / LEDGER_NAME).exists() else EXTERNAL


def label(provenance: str) -> str:
    """Human-facing group name."""
    return {
        AUTHORED: "Authored here",
        VENDORED: "Vendored in",
        FETCHED: "Fetched",
        EXTERNAL: "External root",
    }.get(provenance, provenance)
