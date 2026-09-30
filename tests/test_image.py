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
from dataclasses import dataclass
from pathlib import Path

import jwt
import pytest
from conftest import AGENT_TOKEN, REPO, docker, ledger_volume_of, running_fava
from cryptography.hazmat.primitives.asymmetric import rsa
from playwright.sync_api import Browser, Error, expect, sync_playwright

FIXTURE_LEDGER = REPO / "tests" / "fixtures" / "ledger"
LEDGER_FILE = "/ledger/main.beancount"
ACCESS_AUD = "image-test-aud"

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


@pytest.fixture
def ledger_volume(image: str) -> Iterator[str]:
    with ledger_volume_of(image, FIXTURE_LEDGER) as name:
        yield name


@dataclass(frozen=True)
class Access:
    network: str
    team_domain: str
    key: rsa.RSAPrivateKey

    def sign(self) -> str:
        claims = {"aud": ACCESS_AUD, "iss": self.team_domain, "exp": int(time.time()) + 3600}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "test"})


@pytest.fixture(scope="session")
def access(image: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Access]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    srv = tmp_path_factory.mktemp("jwks") / "srv"
    certs = srv / "cdn-cgi" / "access" / "certs"
    certs.parent.mkdir(parents=True)
    certs.write_text(json.dumps({"keys": [{**jwk, "kid": "test", "alg": "RS256"}]}))
    for path in (srv, srv / "cdn-cgi", certs.parent):
        path.chmod(0o755)
    certs.chmod(0o644)

    name = f"bf-jwks-{uuid.uuid4().hex[:12]}"
    docker("network", "create", name, check=True)
    try:
        docker(
            "create", "--name", name, "--network", name, image,
            "python", "-u", "-m", "http.server", "8000", "-d", "/tmp/jwks", check=True,
        )  # fmt: skip
        docker("cp", f"{srv}/.", f"{name}:/tmp/jwks", check=True)
        docker("start", name, check=True)
        deadline = time.monotonic() + 20
        while "Serving HTTP" not in docker("logs", name).stdout:
            assert time.monotonic() < deadline, "the JWKS stub did not start"
            time.sleep(0.2)
        yield Access(network=name, team_domain=f"http://{name}:8000", key=key)
    finally:
        docker("rm", "-f", name)
        docker("network", "rm", name)


@pytest.fixture
def fava_port(
    image: str, ledger_volume: str, access: Access, tmp_path: Path
) -> Iterator[tuple[str, int]]:
    with running_fava(
        image, ledger_volume, LEDGER_FILE, tmp_path,
        "--network", access.network,
        "-e", f"CF_ACCESS_TEAM_DOMAIN={access.team_domain}", "-e", f"CF_ACCESS_AUD={ACCESS_AUD}",
    ) as container:  # fmt: skip
        yield container


def status(
    url: str, headers: dict[str, str], method: str = "GET", data: bytes | None = None
) -> int:
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    """An installed Chrome or Edge; GitHub's ubuntu runners ship Chrome."""
    with sync_playwright() as playwright:
        for channel in ("chrome", "msedge"):
            try:
                instance = playwright.chromium.launch(channel=channel)
            except Error:
                continue
            yield instance
            instance.close()
            return
        pytest.fail("the UI test needs an installed Chrome or Edge")


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
    assert installed == _locked_packages() | {("beancount-fava-image", "0")}
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
    image: str, fava_port: tuple[str, int], ledger_volume: str, access: Access
) -> None:
    _, port = fava_port
    browser_headers = {"Cf-Access-Jwt-Assertion": access.sign()}
    request = urllib.request.Request(f"http://127.0.0.1:{port}/", headers=browser_headers)
    with urllib.request.urlopen(request, timeout=5) as response:
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
    add_entries = f"http://127.0.0.1:{port}/spec-ledger/api/add_entries"
    body = json.dumps({"entries": [entry]}).encode()
    json_headers = {"Content-Type": "application/json"}
    agent_headers = {"Authorization": f"Bearer {AGENT_TOKEN}"}
    assert status(add_entries, agent_headers | json_headers, "PUT", body) == 403
    assert status(add_entries, browser_headers | json_headers, "PUT", body) == 200

    stored = docker("run", "--rm", "-v", f"{ledger_volume}:/ledger", image, "cat", LEDGER_FILE)
    assert stored.stdout.count("write-probe") == 1


def test_every_path_needs_a_credential(fava_port: tuple[str, int]) -> None:
    _, port = fava_port
    base = f"http://127.0.0.1:{port}"
    agent_headers = {"Authorization": f"Bearer {AGENT_TOKEN}"}
    assert status(f"{base}/spec-ledger/api/ledger_data", agent_headers) == 200
    for path in ("/", "/spec-ledger/api/ledger_data", "/spec-ledger/api/changed", "/static/app.js"):
        assert status(f"{base}{path}", {}) == 401, path
    assert status(f"{base}/spec-ledger/income_statement/", agent_headers) == 403


def test_ui_opens_navigates_and_adds_an_entry(
    image: str, fava_port: tuple[str, int], ledger_volume: str, browser: Browser, access: Access
) -> None:
    _, port = fava_port
    context = browser.new_context(
        locale="en-US", extra_http_headers={"Cf-Access-Jwt-Assertion": access.sign()}
    )
    page = context.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda error: problems.append(f"pageerror: {error}"))
    page.on(
        "console",
        lambda message: (
            problems.append(f"console: {message.text}") if message.type == "error" else None
        ),
    )

    page.goto(f"http://127.0.0.1:{port}/")
    expect(page.locator("h1")).to_contain_text("Income Statement")
    expect(page.locator("h1")).to_contain_text("Spec Ledger")

    page.locator("aside").get_by_role("link", name="Balance Sheet").click()
    expect(page.locator("h1")).to_contain_text("Balance Sheet")
    page.locator("aside").get_by_role("link", name="Journal").click()
    expect(page.get_by_text("fixture-purchase")).to_be_visible()

    page.locator("aside").get_by_title("Add Journal Entry").click()
    dialog = page.get_by_role("dialog")
    dialog.locator("input[type=date]").fill("2024-03-01")
    dialog.get_by_placeholder("Payee").fill("UI Shop")
    dialog.get_by_placeholder("Narration").fill("ui-added-entry")
    dialog.get_by_placeholder("Account").nth(0).fill("Expenses:Food")
    dialog.get_by_placeholder("Amount").nth(0).fill("30 TWD")
    dialog.get_by_placeholder("Account").nth(1).fill("Assets:Cash")
    dialog.get_by_placeholder("Amount").nth(1).fill("-30 TWD")
    dialog.get_by_role("button", name="Save").click()
    expect(dialog).to_be_hidden()
    expect(page.get_by_text("ui-added-entry")).to_be_visible()

    stored = docker("run", "--rm", "-v", f"{ledger_volume}:/ledger", image, "cat", LEDGER_FILE)
    assert "ui-added-entry" in stored.stdout

    page.locator("aside").get_by_role("link", name="Editor", exact=True).click()
    expect(page.locator(".cm-content")).to_contain_text('option "title" "Spec Ledger"')

    assert problems == []


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


def test_refuses_to_start_without_credentials(image: str, ledger_volume: str) -> None:
    result = docker(
        "run", "--rm", "-v", f"{ledger_volume}:/ledger", "-e", f"BEANCOUNT_FILE={LEDGER_FILE}",
        image,
    )  # fmt: skip
    assert result.returncode == 1
    assert result.stderr.strip() == (
        "beancount-fava-serve: no credential is configured: set AGENT_API_TOKEN_FILE for "
        "agents, or CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD for browsers behind Cloudflare Access"
    )


def test_stops_on_sigterm(fava_port: tuple[str, int]) -> None:
    container, _ = fava_port
    started = time.monotonic()
    docker("stop", container, check=True)
    elapsed = time.monotonic() - started
    exit_code = docker("inspect", "--format", "{{.State.ExitCode}}", container, check=True)
    assert exit_code.stdout.strip() == "143"
    assert elapsed < 5
