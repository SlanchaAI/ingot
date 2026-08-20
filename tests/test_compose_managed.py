"""The tracked deployment must declare the invariant it claims.

`docker compose up` with no `-f` is the configuration a reader gets, and it must not launch a
writable stack while the README describes controlled activation. These are static reads of the
tracked YAML: they cannot prove the containers behave, which needs a daemon and the compose smoke
script, but they do catch the failure that actually happened — an invariant that lived only in one
machine's untracked overlay."""
import yaml
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVED = "/app/skills"


def _compose(name):
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def _mounts(service, target):
    for mount in service.get("volumes") or []:
        if isinstance(mount, str) and mount.split(":")[1:2] == [target]:
            yield mount


@pytest.fixture(scope="module")
def managed():
    return _compose("docker-compose.yml")


def test_the_default_stack_has_exactly_one_writer_of_the_served_library(managed):
    writers = [name for name, service in managed["services"].items()
               if any(not mount.endswith(":ro") for mount in _mounts(service, SERVED))]

    assert writers == [], f"these services can change what is served without an approval: {writers}"


def test_every_service_that_serves_the_library_mounts_the_vault(managed):
    """A service reading a different directory than the publisher writes serves stale bytes and
    reports no drift, because nothing is comparing the two."""
    sources = {mount.split(":")[0]
               for service in managed["services"].values()
               for mount in _mounts(service, SERVED)}

    assert sources == {"./vault"}


def test_the_publisher_is_in_the_default_stack_and_owns_the_vault(managed):
    publisher = managed["services"]["publisher"]

    assert "profiles" not in publisher, "the one writer must not be opt-in"
    assert publisher["environment"]["INGOT_VAULT_PATH"] == "/app/vault"
    writable = [mount for mount in publisher["volumes"]
                if mount.startswith("./vault:") and not mount.endswith(":ro")]
    assert writable, "the publisher must be able to write the vault"


def test_the_publisher_and_the_console_run_as_the_same_user(managed):
    """Approval writes receipts at mode 0700. A publisher running as a different user sees an
    empty queue and approvals never publish — the exact stall this deployment already hit."""
    assert managed["services"]["publisher"]["user"] == managed["services"]["ui"]["user"]


def test_every_service_that_touches_state_names_where_it_lives(managed):
    """State no longer defaults to a directory beside the code. A service that mounts a state
    volume without naming the paths would write its review queue and receipts inside the image,
    where the next `docker compose build` discards them."""
    for name, service in managed["services"].items():
        mounts = [mount for mount in (service.get("volumes") or []) if isinstance(mount, str)
                  and mount.split(":")[1:2] and mount.split(":")[1].startswith(("/app/skills",
                                                                                "/app/runs",
                                                                                "/app/vault"))]
        if not mounts:
            continue
        environment = service.get("environment") or {}
        assert "INGOT_RUNS" in environment, f"{name} mounts state without naming INGOT_RUNS"
        assert environment.get("INGOT_LIBRARY"), f"{name} mounts state without naming INGOT_LIBRARY"


def test_the_default_backend_is_local(managed):
    backend = managed["services"]["publisher"]["environment"]["INGOT_PUBLISH_BACKEND"]

    assert backend.startswith("${INGOT_PUBLISH_BACKEND:-local}")


def test_the_development_override_is_explicit_about_being_unmanaged():
    dev = _compose("compose.dev.yaml")

    assert dev["services"]["publisher"]["deploy"]["replicas"] == 0
    writable = {name for name, service in dev["services"].items()
                if any(not mount.endswith(":ro") for mount in _mounts(service, SERVED))}
    assert writable, "the development stack is the writable one; that is its whole purpose"
    assert dev["services"]["unmanaged"]["command"] == ["ingot", "status"]
    assert dev["services"]["unmanaged"]["environment"]["INGOT_MODE"] == "dev"
    assert dev["services"]["ui"]["environment"]["INGOT_MODE"] == "dev"


def test_the_forge_override_never_infers_the_repository():
    forge = _compose("compose.forge.yaml")
    environment = forge["services"]["publisher"]["environment"]

    assert environment["INGOT_PUBLISH_BACKEND"] == "forge"
    assert environment["INGOT_FORGE_REPOSITORY"].startswith("${INGOT_FORGE_REPOSITORY:?")
