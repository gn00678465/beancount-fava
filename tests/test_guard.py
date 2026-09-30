from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from werkzeug.test import Client

from beancount_agent_api.guard import ConfigError, guard, load_credentials

AUD = "test-aud"
TOKEN = "agent-secret"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(*keys: tuple[str, rsa.RSAPrivateKey]) -> dict[str, Any]:
    entries = []
    for kid, key in keys:
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
        entries.append({**jwk, "kid": kid, "alg": "RS256", "use": "sig"})
    return {"keys": entries}


@pytest.fixture(scope="module")
def team_domain() -> Iterator[str]:
    body = json.dumps(_jwks(("k1", KEY))).encode()

    class Certs(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            responses = {
                "/cdn-cgi/access/certs": body,
                "/html/cdn-cgi/access/certs": b"<html>maintenance</html>",
            }
            if self.path not in responses:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(responses[self.path])

        def log_message(self, *_: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Certs)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "token"
    path.write_text(f"{TOKEN}\n")
    return path


def _stub_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"fava"]


def _client(env: dict[str, str]) -> Client:
    return Client(guard(_stub_app, load_credentials(env)))


@pytest.fixture
def client(team_domain: str, token_file: Path) -> Client:
    return _client(
        {
            "AGENT_API_TOKEN_FILE": str(token_file),
            "CF_ACCESS_TEAM_DOMAIN": team_domain,
            "CF_ACCESS_AUD": AUD,
        }
    )


def _sign(
    team_domain: str,
    *,
    key: rsa.RSAPrivateKey = KEY,
    kid: str = "k1",
    **claims: Any,
) -> str:
    payload = {"aud": AUD, "iss": team_domain, "exp": int(time.time()) + 300, **claims}
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})


AGENT = {"Authorization": f"Bearer {TOKEN}"}

POLICY_CASES = [
    (None, "GET", "/agent/api/ledger_data", 401),
    ("wrong-token", "GET", "/agent/api/ledger_data", 401),
    ("agent", "GET", "/agent/api/ledger_data", 200),
    ("agent", "GET", "/agent/api/errors", 200),
    ("agent", "PUT", "/agent/api/source", 403),
    ("agent", "PUT", "/agent/api/add_entries", 403),
    ("agent", "DELETE", "/agent/api/source_slice", 403),
    ("agent", "POST", "/agent/api/ledger_data", 403),
    ("agent", "GET", "/agent/api/nested/path", 403),
    ("agent", "POST", "/agent/extension/AgentApi/transactions", 200),
    ("agent", "GET", "/agent/extension/AgentApi/transactions", 403),
    ("agent", "POST", "/agent/extension/OtherExt/transactions", 403),
    ("agent", "POST", "/agent/extension/AgentApi/a/b", 403),
    ("agent", "GET", "/", 403),
    ("agent", "GET", "/agent/income_statement/", 403),
    ("agent", "GET", "/agent/editor/", 403),
    ("agent", "GET", "/static/app.js", 403),
    ("browser", "GET", "/", 200),
    ("browser", "GET", "/agent/income_statement/", 200),
    ("browser", "PUT", "/agent/api/source", 200),
    ("browser", "DELETE", "/agent/api/source_slice", 200),
    ("browser", "GET", "/static/app.js", 200),
]


@pytest.mark.parametrize(("credential", "method", "path", "status"), POLICY_CASES)
def test_policy_table(
    client: Client, team_domain: str, credential: str | None, method: str, path: str, status: int
) -> None:
    headers = {}
    if credential == "agent":
        headers = AGENT
    elif credential == "browser":
        headers = {"Cf-Access-Jwt-Assertion": _sign(team_domain)}
    elif credential is not None:
        headers = {"Authorization": f"Bearer {credential}"}
    assert client.open(path, method=method, headers=headers).status_code == status


def test_unauthenticated_response_names_the_scheme(client: Client) -> None:
    response = client.get("/agent/api/errors")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="fava"'
    assert response.get_data() == b"401 Unauthorized"


def test_bearer_scheme_is_case_insensitive(client: Client) -> None:
    headers = {"Authorization": f"bearer {TOKEN}"}
    assert client.get("/agent/api/errors", headers=headers).status_code == 200


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({}, 200),
        ({"aud": "other-aud"}, 401),
        ({"iss": "https://other.cloudflareaccess.com"}, 401),
        ({"exp": int(time.time()) - 60}, 401),
        ({"key": OTHER_KEY}, 401),
        ({"kid": "unknown"}, 401),
    ],
)
def test_jwt_claims_and_signature(
    client: Client, team_domain: str, change: dict[str, Any], status: int
) -> None:
    token = _sign(team_domain, **change)
    response = client.get("/agent/income_statement/", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == status


def test_verified_jwt_stops_working_at_exp(client: Client, team_domain: str) -> None:
    exp = int(time.time()) + 2
    headers = {"Cf-Access-Jwt-Assertion": _sign(team_domain, exp=exp)}
    assert client.get("/", headers=headers).status_code == 200
    assert client.get("/", headers=headers).status_code == 200
    time.sleep(exp + 0.1 - time.time())
    assert client.get("/", headers=headers).status_code == 401


def test_jwt_without_exp_is_rejected(client: Client, team_domain: str) -> None:
    token = jwt.encode(
        {"aud": AUD, "iss": team_domain}, KEY, algorithm="RS256", headers={"kid": "k1"}
    )
    response = client.get("/", headers={"Cf-Access-Jwt-Assertion": token})
    assert response.status_code == 401


def test_jwt_in_cookie_is_ignored(client: Client, team_domain: str) -> None:
    client.set_cookie("CF_Authorization", _sign(team_domain))
    assert client.get("/").status_code == 401


@pytest.mark.parametrize("certs_origin", ["http://127.0.0.1:9", "{team_domain}/html"])
def test_unusable_key_endpoint_rejects_browsers_but_not_agents(
    token_file: Path, team_domain: str, certs_origin: str
) -> None:
    domain = certs_origin.format(team_domain=team_domain)
    client = _client(
        {
            "AGENT_API_TOKEN_FILE": str(token_file),
            "CF_ACCESS_TEAM_DOMAIN": domain,
            "CF_ACCESS_AUD": AUD,
        }
    )
    token = _sign(domain)
    assert client.get("/", headers={"Cf-Access-Jwt-Assertion": token}).status_code == 401
    assert client.get("/agent/api/errors", headers=AGENT).status_code == 200


def test_token_only_configuration_rejects_jwts(token_file: Path, team_domain: str) -> None:
    client = _client({"AGENT_API_TOKEN_FILE": str(token_file)})
    token = _sign(team_domain)
    assert client.get("/", headers={"Cf-Access-Jwt-Assertion": token}).status_code == 401
    assert client.get("/agent/api/errors", headers=AGENT).status_code == 200


def test_access_only_configuration_rejects_tokens(team_domain: str) -> None:
    client = _client({"CF_ACCESS_TEAM_DOMAIN": team_domain, "CF_ACCESS_AUD": AUD})
    assert client.get("/agent/api/errors", headers=AGENT).status_code == 401
    token = _sign(team_domain)
    assert client.get("/", headers={"Cf-Access-Jwt-Assertion": token}).status_code == 200


@pytest.mark.parametrize(
    ("env", "message"),
    [
        (
            {},
            (
                "no credential is configured: set AGENT_API_TOKEN_FILE for agents, or "
                "CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD for browsers behind Cloudflare Access"
            ),
        ),
        (
            {"CF_ACCESS_TEAM_DOMAIN": "https://team.cloudflareaccess.com"},
            "CF_ACCESS_AUD is not set; Cloudflare Access needs both variables",
        ),
        (
            {"CF_ACCESS_AUD": AUD},
            "CF_ACCESS_TEAM_DOMAIN is not set; Cloudflare Access needs both variables",
        ),
    ],
)
def test_missing_configuration_is_an_error(env: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError) as error:
        load_credentials(env)
    assert str(error.value) == message


def test_empty_token_file_is_an_error(tmp_path: Path) -> None:
    empty = tmp_path / "token"
    empty.write_text(" \n")
    with pytest.raises(ConfigError) as error:
        load_credentials({"AGENT_API_TOKEN_FILE": str(empty)})
    assert str(error.value) == f"AGENT_API_TOKEN_FILE names an empty file: {empty}"


def test_missing_token_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as error:
        load_credentials({"AGENT_API_TOKEN_FILE": str(tmp_path / "absent")})
    assert str(error.value).startswith("AGENT_API_TOKEN_FILE cannot be read: ")
