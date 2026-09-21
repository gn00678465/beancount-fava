"""Checks that run against the built image; CI and publish both depend on them."""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURE_LEDGER = REPO / "tests" / "fixtures" / "ledger"
LEDGER_FILE = "/ledger/main.beancount"
# publish.yaml reads the versions for its tags from this image after these tests passed.
IMAGE_TAG = "beancount-fava:test"

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


def docker(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
        timeout=1800,
    )


@pytest.fixture(scope="session")
def image() -> str:
    result = docker("build", "-t", IMAGE_TAG, str(REPO))
    assert result.returncode == 0, result.stderr
    return IMAGE_TAG


@pytest.fixture
def ledger_volume(image: str) -> Iterator[str]:
    """A named volume holding the fixture ledger, owned by uid 1000.

    Bind mounts ignore ownership on Docker Desktop, so they cannot show whether
    the image's non-root user can write the ledger.
    """
    name = f"bf-test-{uuid.uuid4().hex[:12]}"
    docker("volume", "create", name, check=True)
    try:
        docker("create", "--name", name, "-v", f"{name}:/ledger", image, "true", check=True)
        docker("cp", f"{FIXTURE_LEDGER}/.", f"{name}:/ledger/", check=True)
        docker("rm", "-f", name, check=True)
        docker(
            "run", "--rm", "-u", "0", "--entrypoint", "chown", "-v", f"{name}:/ledger",
            image, "-R", "1000:1000", "/ledger", check=True,
        )  # fmt: skip
        yield name
    finally:
        docker("rm", "-f", name)
        docker("volume", "rm", "-f", name)


@pytest.fixture
def fava_port(image: str, ledger_volume: str) -> Iterator[tuple[str, int]]:
    """The default command on a random host port; yields (container, port) once fava answers."""
    name = f"bf-fava-{uuid.uuid4().hex[:12]}"
    docker(
        "run", "-d", "--name", name, "-p", "127.0.0.1::5000", "-v", f"{ledger_volume}:/ledger",
        "-e", f"BEANCOUNT_FILE={LEDGER_FILE}", image, check=True,
    )  # fmt: skip
    try:
        mapping = docker("port", name, "5000/tcp", check=True).stdout
        port = int(mapping.strip().splitlines()[0].rsplit(":", 1)[1])
        deadline = time.monotonic() + 30
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).close()
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError) as error:
                if time.monotonic() > deadline:
                    pytest.fail(f"fava did not answer within 30s: {error}")
                time.sleep(0.5)
        yield name, port
    finally:
        docker("rm", "-f", name)


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


def test_python_packages_equal_lock(image: str) -> None:
    result = docker("run", "--rm", image, "/opt/venv/bin/python", "-c", LIST_DISTRIBUTIONS)
    assert result.returncode == 0, result.stderr
    installed = {(_normalize(name), version) for name, version in json.loads(result.stdout)}
    assert installed == _locked_packages()
    assert dict(installed)["beancount"].split(".")[0] == "3"

    # The base image's own interpreter must not carry packages either.
    system = docker("run", "--rm", image, "/usr/local/bin/python3", "-c", LIST_DISTRIBUTIONS)
    assert system.returncode == 0, system.stderr
    assert json.loads(system.stdout) == []


def test_command_line_tools(image: str, ledger_volume: str) -> None:
    def run(*command: str) -> subprocess.CompletedProcess[str]:
        return docker("run", "--rm", "-v", f"{ledger_volume}:/ledger", image, *command)

    for command in (["bean-price", "--help"], ["bean-format", "--help"], ["git", "--version"]):
        assert run(*command).returncode == 0, command
    assert run("bean-check", LEDGER_FILE).returncode == 0
    query = run("bean-query", LEDGER_FILE, "SELECT account, sum(position) GROUP BY account")
    assert "Expenses:Food" in query.stdout, query.stderr

    unbalanced = run("bean-check", "/ledger/unbalanced.beancount")
    assert unbalanced.returncode == 1
    assert "does not balance" in unbalanced.stdout + unbalanced.stderr


def test_runs_as_uid_1000_with_writable_ledger_dir(image: str) -> None:
    assert docker("run", "--rm", image, "id", "-u").stdout.strip() == "1000"
    listing = docker("run", "--rm", image, "ls", "-A", "/ledger")
    assert (listing.returncode, listing.stdout.strip()) == (0, "")
    assert docker("run", "--rm", image, "touch", "/ledger/x").returncode == 0


def test_fava_serves_and_writes_the_mounted_ledger(
    image: str, fava_port: tuple[str, int], ledger_volume: str
) -> None:
    _, port = fava_port
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
        assert response.status == 200
        assert urllib.parse.urlparse(response.url).path.startswith("/spec-ledger/")

    entry = {
        "t": "Transaction",
        "date": "2024-02-01",
        "flag": "*",
        "payee": "",
        "narration": "write-probe",
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
    assert "write-probe" in stored.stdout


def test_missing_ledger_file_fails_at_start(image: str) -> None:
    # fava itself starts anyway and serves a blank page: it sends `filename: null`,
    # which its own frontend rejects ("Validation of object failed at key options").
    name = f"bf-missing-{uuid.uuid4().hex[:12]}"
    docker(
        "run", "-d", "--name", name, "-e", "BEANCOUNT_FILE=/ledger/missing.beancount", image,
        check=True,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 20
        while docker("inspect", "--format", "{{.State.Running}}", name).stdout.strip() == "true":
            assert time.monotonic() < deadline, "container is still running with a missing ledger"
            time.sleep(0.5)
        exit_code = docker("inspect", "--format", "{{.State.ExitCode}}", name).stdout.strip()
        assert exit_code == "1"
        assert "/ledger/missing.beancount" in docker("logs", name).stderr
    finally:
        docker("rm", "-f", name)


def test_stops_on_sigterm(fava_port: tuple[str, int]) -> None:
    container, _ = fava_port
    started = time.monotonic()
    docker("stop", container, check=True)
    elapsed = time.monotonic() - started
    exit_code = docker("inspect", "--format", "{{.State.ExitCode}}", container, check=True)
    assert exit_code.stdout.strip() == "143"
    assert elapsed < 5
