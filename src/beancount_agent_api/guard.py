from __future__ import annotations

import enum
import hmac
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jwt

log = logging.getLogger(__name__)

WSGIApp = Callable[[dict[str, Any], Callable[..., Any]], Iterable[bytes]]


class Principal(enum.Enum):
    BROWSER = "browser"
    AGENT = "agent"


@dataclass(frozen=True)
class Rule:
    principal: Principal
    method: str | None
    path: re.Pattern[str]


POLICY = (
    Rule(Principal.BROWSER, None, re.compile(r"/.*")),
    Rule(Principal.AGENT, "GET", re.compile(r"/[^/]+/api/[^/]+")),
    Rule(Principal.AGENT, "POST", re.compile(r"/[^/]+/extension/AgentApi/[^/]+")),
    Rule(Principal.AGENT, None, re.compile(r"/[^/]+/extension/AgentApi/mcp")),
)


def allowed(principal: Principal, method: str, path: str) -> bool:
    return any(
        rule.principal is principal
        and rule.method in (None, method)
        and rule.path.fullmatch(path) is not None
        for rule in POLICY
    )


# A browser sends one JWT for its whole Access session, and verifying its RSA signature costs
# more than rendering some fava pages. Trust a verified token no longer than PyJWKClient trusts
# its cached key set, so a key removed from the team's certs stops working just as late.
VERIFIED_FOR = 300
VERIFIED_MAX = 1024


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Access:
    team_domain: str
    aud: str
    jwks: jwt.PyJWKClient
    verified: dict[str, float] = field(default_factory=dict, compare=False)

    def verify(self, token: str) -> bool:
        now = time.time()
        if self.verified.get(token, 0) > now:
            return True
        try:
            key = self.jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.aud,
                issuer=self.team_domain,
                options={"require": ["exp", "aud", "iss"]},
            )
        # PyJWKClient wraps URL, timeout, and HTTP protocol errors only; a body that is not
        # JSON or a connection reset mid-read escapes as ValueError or OSError.
        except (jwt.PyJWKClientConnectionError, ValueError, OSError) as error:
            log.warning("cannot fetch Cloudflare Access keys: %s", error)
            return False
        except jwt.PyJWTError:
            return False
        if len(self.verified) >= VERIFIED_MAX:
            self.verified.clear()
        self.verified[token] = min(claims["exp"], now + VERIFIED_FOR)
        return True


@dataclass(frozen=True)
class Credentials:
    agent_token: str | None
    access: Access | None

    def authenticate(self, environ: Mapping[str, Any]) -> Principal | None:
        scheme, _, token = environ.get("HTTP_AUTHORIZATION", "").partition(" ")
        if (
            self.agent_token is not None
            and scheme.lower() == "bearer"
            and hmac.compare_digest(token.strip().encode(), self.agent_token.encode())
        ):
            return Principal.AGENT
        # Cloudflare documents the header as the token to validate; the CF_Authorization
        # cookie is not guaranteed to reach the origin.
        assertion = environ.get("HTTP_CF_ACCESS_JWT_ASSERTION")
        if self.access is not None and assertion and self.access.verify(assertion):
            return Principal.BROWSER
        return None


def load_credentials(env: Mapping[str, str]) -> Credentials:
    token_file = env.get("AGENT_API_TOKEN_FILE")
    agent_token = None
    if token_file:
        try:
            agent_token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ConfigError(f"AGENT_API_TOKEN_FILE cannot be read: {error}") from error
        if not agent_token:
            raise ConfigError(f"AGENT_API_TOKEN_FILE names an empty file: {token_file}")

    team_domain = env.get("CF_ACCESS_TEAM_DOMAIN", "").rstrip("/")
    aud = env.get("CF_ACCESS_AUD", "")
    access = None
    if team_domain and aud:
        jwks = jwt.PyJWKClient(
            f"{team_domain}/cdn-cgi/access/certs", lifespan=VERIFIED_FOR, timeout=2
        )
        access = Access(team_domain, aud, jwks)
    elif team_domain or aud:
        missing = "CF_ACCESS_AUD" if team_domain else "CF_ACCESS_TEAM_DOMAIN"
        raise ConfigError(f"{missing} is not set; Cloudflare Access needs both variables")

    if agent_token is None and access is None:
        raise ConfigError(
            "no credential is configured: set AGENT_API_TOKEN_FILE for agents, or "
            "CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD for browsers behind Cloudflare Access"
        )
    return Credentials(agent_token, access)


def _deny(
    start_response: Callable[..., Any], status: str, extra: list[tuple[str, str]]
) -> list[bytes]:
    body = status.encode()
    start_response(
        status,
        [("Content-Type", "text/plain"), ("Content-Length", str(len(body))), *extra],
    )
    return [body]


def guard(app: WSGIApp, credentials: Credentials) -> WSGIApp:
    def guarded(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        principal = credentials.authenticate(environ)
        if principal is None:
            return _deny(
                start_response,
                "401 Unauthorized",
                [("WWW-Authenticate", 'Bearer realm="fava"')],
            )
        if not allowed(principal, environ["REQUEST_METHOD"], environ.get("PATH_INFO", "")):
            return _deny(start_response, "403 Forbidden", [])
        return app(environ, start_response)

    return guarded
