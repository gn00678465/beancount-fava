from __future__ import annotations

import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
# publish.yaml reads the versions for its tags from this image after these tests passed.
IMAGE_TAG = "beancount-fava:test"
AGENT_TOKEN = "image-test-token"


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


@contextmanager
def ledger_volume_of(image: str, ledger_dir: Path) -> Iterator[str]:
    """A named volume holding a copy of `ledger_dir`, owned by uid 1000.

    Bind mounts ignore ownership on Docker Desktop, so they cannot show whether
    the image's non-root user can write the ledger.
    """
    name = f"bf-test-{uuid.uuid4().hex[:12]}"
    docker("volume", "create", name, check=True)
    try:
        docker("create", "--name", name, "-v", f"{name}:/ledger", image, "true", check=True)
        docker("cp", f"{ledger_dir}/.", f"{name}:/ledger/", check=True)
        docker("rm", "-f", name, check=True)
        docker(
            "run", "--rm", "-u", "0", "--entrypoint", "chown", "-v", f"{name}:/ledger",
            image, "-R", "1000:1000", "/ledger", check=True,
        )  # fmt: skip
        yield name
    finally:
        docker("rm", "-f", name)
        docker("volume", "rm", "-f", name)


@contextmanager
def running_fava(
    image: str, volume: str, ledger_file: str, tmp_path: Path, *options: str
) -> Iterator[tuple[str, int]]:
    name = f"bf-fava-{uuid.uuid4().hex[:12]}"
    token_file = tmp_path / "agent-token"
    token_file.write_text(f"{AGENT_TOKEN}\n")
    token_file.chmod(0o644)
    docker(
        "create", "--name", name, "-p", "127.0.0.1::5000", "-v", f"{volume}:/ledger",
        "-e", f"BEANCOUNT_FILE={ledger_file}", "-e", "AGENT_API_TOKEN_FILE=/run/agent-token",
        *options, image, check=True,
    )  # fmt: skip
    try:
        docker("cp", str(token_file), f"{name}:/run/agent-token", check=True)
        docker("start", name, check=True)
        mapping = docker("port", name, "5000/tcp", check=True).stdout
        port = int(mapping.strip().splitlines()[0].rsplit(":", 1)[1])
        deadline = time.monotonic() + 30
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).close()
                break
            except urllib.error.HTTPError:
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError) as error:
                if time.monotonic() > deadline:
                    pytest.fail(f"fava did not answer within 30s: {error}")
                time.sleep(0.5)
        yield name, port
    finally:
        docker("rm", "-f", name)
