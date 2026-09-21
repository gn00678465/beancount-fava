from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from typing import Any

import yaml
from conftest import IMAGE_TAG, REPO

WORKFLOWS = REPO / ".github" / "workflows"
AUTOMERGE_ALLOWLIST = {"patch", "minor", "digest", "pin", "pinDigest"}


def _workflows() -> dict[str, dict[str, Any]]:
    paths = [*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]
    return {path.name: yaml.safe_load(path.read_text("utf-8")) for path in paths}


def _triggers(workflow: dict[str, Any]) -> Any:
    # YAML 1.1 reads the bare key `on` as the boolean true.
    return workflow.get("on", workflow.get(True))


def _steps(workflow: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for job in workflow["jobs"].values():
        yield from job.get("steps", [])


def _publish_capabilities(workflow: dict[str, Any]) -> list[str]:
    """Everything in a workflow that could push an image or read a secret."""
    found = []
    if "secrets" in json.dumps(workflow):
        found.append("secrets reference")
    for step in _steps(workflow):
        if "docker/login-action" in step.get("uses", ""):
            found.append("docker/login-action")
        if step.get("with", {}).get("push", False) is not False:
            found.append("push input")
        if "docker push" in step.get("run", ""):
            found.append("docker push")
    return found


def _run_lines(workflow: dict[str, Any]) -> str:
    return "\n".join(step.get("run", "") for step in _steps(workflow))


def test_only_publish_workflow_pushes() -> None:
    workflows = _workflows()
    assert {"ci.yaml", "publish.yaml"} <= set(workflows)

    for name, workflow in workflows.items():
        assert "pull_request_target" not in json.dumps(_triggers(workflow)), name
        # `bash` adds pipefail; the default shell lets a failed pipe stage pass.
        assert workflow["defaults"]["run"]["shell"] == "bash", name
        if name != "publish.yaml":
            assert _publish_capabilities(workflow) == [], name

    publish = workflows["publish.yaml"]
    assert {"docker/login-action", "push input"} <= set(_publish_capabilities(publish))
    assert _triggers(publish) == {"push": {"branches": ["main"]}}
    # Runs queue instead of cancelling: a cancel mid-push leaves tags inconsistent.
    assert publish["concurrency"]["group"]
    assert publish["concurrency"]["cancel-in-progress"] is False
    assert IMAGE_TAG in _run_lines(publish)

    ci = workflows["ci.yaml"]
    assert "pull_request" in _triggers(ci)
    ci_commands = _run_lines(ci)
    assert "uv run pytest" in ci_commands
    assert "pip-audit" in ci_commands


def test_renovate_automerge_allowlist() -> None:
    config_path = REPO / "renovate.json"
    config = json.loads(config_path.read_text("utf-8"))
    assert config.get("automerge") is False
    assert config.get("platformAutomerge") is False

    automerged: set[str] = set()
    for rule in config.get("packageRules", []):
        if rule.get("automerge"):
            # A rule without update types would also automerge major updates.
            assert rule.get("matchUpdateTypes"), rule
            automerged |= set(rule["matchUpdateTypes"])
    assert automerged == AUTOMERGE_ALLOWLIST

    # Transitive dependencies (e.g. a fixed diskcache) only arrive this way.
    maintenance = config.get("lockFileMaintenance", {})
    assert maintenance.get("enabled") is True
    assert not maintenance.get("automerge")

    # A new Python minor must be a manual decision, not an automerged "minor".
    python_rules = [
        rule for rule in config.get("packageRules", [])
        if rule.get("matchPackageNames") == ["python"] and rule.get("allowedVersions")
    ]  # fmt: skip
    assert len(python_rules) == 1

    npx = shutil.which("npx")
    assert npx, "npx is required to run renovate-config-validator"
    validation = subprocess.run(
        [npx, "--yes", "--package", "renovate", "renovate-config-validator", "--strict",
         str(config_path)],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=900, check=False,
    )  # fmt: skip
    assert validation.returncode == 0, validation.stdout + validation.stderr
