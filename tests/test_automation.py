from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml
from conftest import REPO

WORKFLOWS = REPO / ".github" / "workflows"
PUBLISH_MARKERS = ("secrets.DOCKERHUB_", "docker/login-action", "push: true")
AUTOMERGE_ALLOWLIST = {"patch", "minor", "digest", "pin", "pinDigest"}


def _workflows() -> dict[str, Path]:
    return {path.name: path for path in [*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]}


def _triggers(workflow: Path) -> Any:
    document = yaml.safe_load(workflow.read_text("utf-8"))
    # YAML 1.1 reads the bare key `on` as the boolean true.
    return document.get("on", document.get(True))


def test_only_publish_workflow_pushes() -> None:
    workflows = _workflows()
    assert {"ci.yaml", "publish.yaml"} <= set(workflows)

    for name, path in workflows.items():
        text = path.read_text("utf-8")
        assert "pull_request_target" not in text, name
        if name != "publish.yaml":
            assert [marker for marker in PUBLISH_MARKERS if marker in text] == [], name

    publish_text = workflows["publish.yaml"].read_text("utf-8")
    assert [marker for marker in PUBLISH_MARKERS if marker not in publish_text] == []
    assert _triggers(workflows["publish.yaml"]) == {"push": {"branches": ["main"]}}
    publish = yaml.safe_load(publish_text)
    assert publish["concurrency"]["cancel-in-progress"] is True

    assert "pull_request" in _triggers(workflows["ci.yaml"])


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

    npx = shutil.which("npx")
    assert npx, "npx is required to run renovate-config-validator"
    validation = subprocess.run(
        [npx, "--yes", "--package", "renovate", "renovate-config-validator", "--strict",
         str(config_path)],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=900, check=False,
    )  # fmt: skip
    assert validation.returncode == 0, validation.stdout + validation.stderr
