from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURE_LEDGER = REPO / "tests" / "fixtures" / "ledger"
LEDGER_FILE = "/ledger/main.beancount"
# publish.yaml reads versions from this tag after the suite built and tested it.
IMAGE_TAG = "beancount-fava:test"


def docker(
    *args: str, check: bool = False, timeout: int = 1800
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
        timeout=timeout,
    )


def build_image(tag: str, context: Path, platform: str) -> subprocess.CompletedProcess[str]:
    return docker("build", "--platform", platform, "-t", tag, str(context))


def _built(tag: str, platform: str) -> str:
    result = build_image(tag, REPO, platform)
    assert result.returncode == 0, result.stderr
    return tag


@pytest.fixture(scope="session")
def image() -> str:
    return _built(IMAGE_TAG, "linux/amd64")


@pytest.fixture(scope="session")
def image_arm64() -> str:
    return _built("beancount-fava:test-arm64", "linux/arm64")


def _ledger_volume(image_tag: str, platform: str) -> Iterator[str]:
    """A named volume holding the fixture ledger, owned by uid 1000.

    Bind mounts ignore ownership on Docker Desktop, so they cannot show whether
    the image's non-root user can write the ledger.
    """
    name = f"bf-test-{uuid.uuid4().hex[:12]}"
    docker("volume", "create", name, check=True)
    try:
        docker(
            "create", "--platform", platform, "--name", name, "-v", f"{name}:/ledger",
            image_tag, "true", check=True,
        )  # fmt: skip
        docker("cp", f"{FIXTURE_LEDGER}/.", f"{name}:/ledger/", check=True)
        docker("rm", "-f", name, check=True)
        docker(
            "run", "--rm", "--platform", platform, "-u", "0", "--entrypoint", "chown",
            "-v", f"{name}:/ledger", image_tag, "-R", "1000:1000", "/ledger", check=True,
        )  # fmt: skip
        yield name
    finally:
        docker("rm", "-f", name)
        docker("volume", "rm", "-f", name)


@pytest.fixture
def ledger_volume(image: str) -> Iterator[str]:
    yield from _ledger_volume(image, "linux/amd64")


@pytest.fixture
def ledger_volume_arm64(image_arm64: str) -> Iterator[str]:
    yield from _ledger_volume(image_arm64, "linux/arm64")


@pytest.fixture
def fava_container(image: str, ledger_volume: str) -> Iterator[str]:
    """A detached container running the image's default command on a random host port."""
    name = f"bf-fava-{uuid.uuid4().hex[:12]}"
    docker(
        "run", "-d", "--name", name, "-p", "127.0.0.1::5000",
        "-v", f"{ledger_volume}:/ledger", "-e", f"BEANCOUNT_FILE={LEDGER_FILE}",
        image, check=True,
    )  # fmt: skip
    try:
        yield name
    finally:
        docker("rm", "-f", name)


def host_port(container: str) -> int:
    result = docker("port", container, "5000/tcp", check=True)
    return int(result.stdout.strip().splitlines()[0].rsplit(":", 1)[1])
