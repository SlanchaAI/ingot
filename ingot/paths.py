"""Where mutable state lives. The one place that answers, so no module derives it from `__file__`.

State that was package-relative worked for exactly two deployments — a container and a checkout —
and broke everywhere else: a read-only or system Python, an upgrade that replaces the package
directory, a recreated environment, two deployments sharing one installation, and any backup that
expects application code and controlled state to be separable. A release controller that keeps its
own review queue inside `site-packages` cannot claim to control anything.

Every function reads the environment when called. Modules that still expose a module-level constant
derive it from here, so the default is right for an installation; the resolution rule lives here.
"""
from __future__ import annotations

import os
from pathlib import Path

HOME = "INGOT_HOME"
LIBRARY = "INGOT_LIBRARY"
RUNS = "INGOT_RUNS"
TASKS = "INGOT_TASKS"
VAULT = "INGOT_VAULT_PATH"
# Names that predate INGOT_HOME. Honoured so an existing deployment keeps working, and reported by
# `ingot status` so it is visible rather than load-bearing and forgotten.
LEGACY = {LIBRARY: "SKILLS_DIR", VAULT: "VAULT_DIR"}

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, env: dict | None = None) -> str:
    env = os.environ if env is None else env
    value = env.get(name) or env.get(LEGACY.get(name, ""), "")
    return value.strip()


def home(env: dict | None = None) -> Path:
    """The root of everything mutable.

    XDG on Unix by default, so a plain `pip install ingot` puts state somewhere a package upgrade
    cannot take with it. Containers and managed deployments override it, or override each path
    below individually."""
    env = os.environ if env is None else env
    explicit = _env(HOME, env)
    if explicit:
        return Path(explicit).expanduser()
    state = (env.get("XDG_STATE_HOME") or "").strip()
    if state:
        return Path(state).expanduser() / "ingot"
    return Path.home() / ".local" / "state" / "ingot"


def _under(name: str, default: str, env: dict | None = None) -> Path:
    explicit = _env(name, env)
    return Path(explicit).expanduser() if explicit else home(env) / default


def library(env: dict | None = None) -> Path:
    """The served skill library."""
    return _under(LIBRARY, "library", env)


def runs(env: dict | None = None) -> Path:
    """Review queue, publication receipts, evidence, snapshots, and audit trails."""
    return _under(RUNS, "runs", env)


def tasks(env: dict | None = None) -> Path:
    """Held-out evaluation task sets."""
    return _under(TASKS, "tasks", env)


def vault(env: dict | None = None) -> Path:
    """The Git repository the publisher owns.

    Defaults to the library, because in the local backend the served checkout *is* the vault. A
    deployment that wants them separate says so."""
    explicit = _env(VAULT, env)
    return Path(explicit).expanduser() if explicit else library(env)


def _source(name: str, env: dict | None = None) -> str:
    """Which setting decided a path, for a report that has to be checkable rather than believed."""
    env = os.environ if env is None else env
    if env.get(name):
        return name
    legacy = LEGACY.get(name)
    if legacy and env.get(legacy):
        return f"{legacy} (deprecated; prefer {name})"
    if env.get(HOME):
        return HOME
    if name == VAULT:
        return _source(LIBRARY, env)
    return "XDG_STATE_HOME" if env.get("XDG_STATE_HOME") else "default"


def resolved(env: dict | None = None) -> list[dict]:
    """Every path this process would use, where it came from, and whether it can be written."""
    entries = [("home", home(env), HOME), ("library", library(env), LIBRARY),
               ("runs", runs(env), RUNS), ("tasks", tasks(env), TASKS),
               ("vault", vault(env), VAULT)]
    return [{"name": name, "path": str(path), "source": _source(setting, env),
             "exists": path.exists(),
             # An absent path is not a problem: it is created on first write. An absent path whose
             # parent cannot be written is, and reporting only `exists` would hide it.
             "writable": os.access(path if path.exists() else _nearest(path), os.W_OK | os.X_OK)}
            for name, path, setting in entries]


def _nearest(path: Path) -> Path:
    for candidate in path.parents:
        if candidate.exists():
            return candidate
    return path


def legacy_state() -> list[str]:
    """Package-relative state directories left by a version that kept them next to the code.

    Reported, never migrated. Moving a review queue or a receipt store on someone's behalf is a
    change to controlled state made by a process that was not asked to make it."""
    found = []
    for name in ("runs", "skills"):
        directory = PACKAGE_ROOT / name
        if not directory.is_dir():
            continue
        contents = [item for item in directory.iterdir() if item.name != ".gitkeep"]
        if contents:
            found.append(str(directory))
    return found
