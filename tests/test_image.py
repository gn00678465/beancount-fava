from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from conftest import LEDGER_FILE, REPO, build_image, docker, host_port

# The image is CPython on Linux. A marker not listed here fails the test, so a
# new platform-specific dependency gets a decision instead of a silent skip.
MARKER_APPLIES_IN_IMAGE = {
    "implementation_name != 'PyPy'": True,
    "sys_platform != 'win32'": True,
    "sys_platform == 'win32'": False,
}

LIST_DISTRIBUTIONS = (
    "import importlib.metadata as m, json;"
    "print(json.dumps([[d.metadata['Name'], d.version] for d in m.distributions()]))"
)


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked_packages() -> set[tuple[str, str]]:
    export = subprocess.run(
        ["uv", "export", "--locked", "--no-dev", "--no-hashes", "--no-emit-project",
         "--format", "requirements-txt"],
        cwd=REPO, capture_output=True, text=True, check=True,
    )  # fmt: skip
    packages = set()
    for line in export.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement, _, marker = (part.strip() for part in line.partition(";"))
        if marker:
            assert marker in MARKER_APPLIES_IN_IMAGE, f"unknown marker: {line}"
            if not MARKER_APPLIES_IN_IMAGE[marker]:
                continue
        name, _, version = requirement.partition("==")
        packages.add((_normalize(name), version))
    return packages


def test_installed_packages_equal_lock(image: str) -> None:
    result = docker("run", "--rm", image, "/opt/venv/bin/python", "-c", LIST_DISTRIBUTIONS)
    assert result.returncode == 0, result.stderr
    installed = {(_normalize(name), version) for name, version in json.loads(result.stdout)}

    assert installed == _locked_packages()
    versions = dict(installed)
    assert versions["beancount"].split(".")[0] == "3"
    assert "pytest" not in versions

    # The base image's own interpreter must not carry packages either.
    system = docker("run", "--rm", image, "/usr/local/bin/python3", "-c", LIST_DISTRIBUTIONS)
    assert system.returncode == 0, system.stderr
    assert json.loads(system.stdout) == []


def test_build_fails_on_lock_drift(tmp_path: Path) -> None:
    context = tmp_path / "context"
    context.mkdir()
    for name in ("Dockerfile", "pyproject.toml", "uv.lock"):
        shutil.copy(REPO / name, context / name)
    pyproject = context / "pyproject.toml"
    drifted, replaced = re.subn(r"fava==[0-9.]+", "fava==1.30.15", pyproject.read_text("utf-8"))
    assert replaced == 1, "pyproject.toml has no exact fava pin to drift"
    pyproject.write_text(drifted, "utf-8")

    result = build_image("beancount-fava:test-drift", context, "linux/amd64")
    try:
        assert result.returncode != 0
        assert "uv.lock" in result.stdout + result.stderr
    finally:
        docker("rmi", "-f", "beancount-fava:test-drift")


@pytest.mark.parametrize(
    "command",
    [["bean-query", "--help"], ["bean-price", "--help"], ["bean-format", "--help"],
     ["git", "--version"]],
    ids=lambda command: command[0],
)  # fmt: skip
def test_cli_tools_available(image: str, command: list[str]) -> None:
    result = docker("run", "--rm", image, *command)
    assert result.returncode == 0, result.stderr


def test_bean_check_valid_ledger(image: str, ledger_volume: str) -> None:
    result = docker(
        "run", "--rm", "-v", f"{ledger_volume}:/ledger", image, "bean-check", LEDGER_FILE
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_bean_check_unbalanced_ledger(image: str, ledger_volume: str) -> None:
    result = docker(
        "run", "--rm", "-v", f"{ledger_volume}:/ledger", image,
        "bean-check", "/ledger/unbalanced.beancount",
    )  # fmt: skip
    assert result.returncode == 1
    assert "does not balance" in result.stdout + result.stderr


def test_runs_as_non_root(image: str) -> None:
    result = docker("run", "--rm", image, "id", "-u")
    assert result.stdout.strip() == "1000"


def test_ledger_dir_empty_and_writable(image: str) -> None:
    listing = docker("run", "--rm", image, "ls", "-A", "/ledger")
    assert listing.returncode == 0, listing.stderr
    assert listing.stdout.strip() == ""
    touch = docker("run", "--rm", image, "touch", "/ledger/x")
    assert touch.returncode == 0, touch.stderr


def _wait_for_fava(port: int, deadline_seconds: float = 30) -> tuple[int, str]:
    """GET / following redirects; returns (status, final path)."""
    deadline = time.monotonic() + deadline_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                return response.status, urllib.parse.urlparse(response.url).path
        except (urllib.error.URLError, ConnectionError, TimeoutError) as error:
            last_error = error
            time.sleep(0.5)
    pytest.fail(f"fava did not answer within {deadline_seconds}s: {last_error}")


def test_fava_serves_on_published_port(fava_container: str) -> None:
    status, path = _wait_for_fava(host_port(fava_container))
    assert status == 200
    assert path.startswith("/spec-ledger/")


def test_fava_writes_mounted_ledger(image: str, fava_container: str, ledger_volume: str) -> None:
    port = host_port(fava_container)
    _wait_for_fava(port)
    entry = {
        "t": "Transaction",
        "date": "2024-02-01",
        "flag": "*",
        "payee": "",
        "narration": "spec-write-probe",
        "tags": [],
        "links": [],
        "meta": {},
        "postings": [
            {"account": "Expenses:Food", "amount": "50 TWD"},
            {"account": "Assets:Cash", "amount": "-50 TWD"},
        ],
    }
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/spec-ledger/api/add_entries",
        data=json.dumps({"entries": [entry]}).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200

    stored = docker("run", "--rm", "-v", f"{ledger_volume}:/ledger", image, "cat", LEDGER_FILE)
    assert "spec-write-probe" in stored.stdout


def test_fava_without_ledger_fails(image: str) -> None:
    result = docker("run", "--rm", image, timeout=60)
    assert result.returncode == 2
    assert "No file specified" in result.stderr


def test_stops_on_sigterm(fava_container: str) -> None:
    _wait_for_fava(host_port(fava_container))
    started = time.monotonic()
    docker("stop", fava_container, check=True)
    elapsed = time.monotonic() - started
    exit_code = docker("inspect", "--format", "{{.State.ExitCode}}", fava_container, check=True)

    assert exit_code.stdout.strip() == "143"
    assert elapsed < 5


def test_arm64_smoke(image_arm64: str, ledger_volume_arm64: str) -> None:
    machine = docker(
        "run", "--rm", "--platform", "linux/arm64", image_arm64,
        "python", "-c", "import platform; print(platform.machine())",
    )  # fmt: skip
    assert machine.stdout.strip() == "aarch64"
    check = docker(
        "run", "--rm", "--platform", "linux/arm64", "-v", f"{ledger_volume_arm64}:/ledger",
        image_arm64, "bean-check", LEDGER_FILE,
    )  # fmt: skip
    assert check.returncode == 0, check.stdout + check.stderr


def test_base_images_digest_pinned() -> None:
    dockerfile = (REPO / "Dockerfile").read_text("utf-8")
    stages = set(re.findall(r"^FROM\s+\S+\s+AS\s+(\S+)", dockerfile, re.MULTILINE | re.IGNORECASE))
    references = re.findall(r"^FROM\s+(\S+)", dockerfile, re.MULTILINE | re.IGNORECASE)
    references += re.findall(r"^COPY\s+--from=(\S+)", dockerfile, re.MULTILINE | re.IGNORECASE)
    external = [reference for reference in references if reference not in stages]

    assert external
    assert [reference for reference in external if "@sha256:" not in reference] == []
