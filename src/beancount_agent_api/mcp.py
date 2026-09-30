from __future__ import annotations

import base64
import json
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from importlib.metadata import version
from typing import TYPE_CHECKING

from fava.context import g
from fava.helpers import FavaAPIError
from fava.internal_api import get_ledger_data
from fava.json_api import json_err, json_success
from flask import Response, request

from beancount_agent_api.core import REQUIRED

if TYPE_CHECKING:
    from beancount_agent_api.extension import AgentApi

SUPPORTED = ("2026-07-28",)
PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO = {"name": "beancount-agent-api", "version": version("beancount-fava-image")}
CACHE = {"ttlMs": 3600000, "cacheScope": "private"}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022
HTTP_STATUS = {
    PARSE_ERROR: 400,
    INVALID_REQUEST: 400,
    METHOD_NOT_FOUND: 404,
    INVALID_PARAMS: 400,
    HEADER_MISMATCH: 400,
    UNSUPPORTED_PROTOCOL_VERSION: 400,
}

B64_SENTINEL = re.compile(r"=\?base64\?(.*)\?=")
HEADER_SAFE = re.compile(r"[\x20-\x7E]*")


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: object = None, status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data
        self.status = status or HTTP_STATUS[code]

    def body(self, id_: str | int | None) -> dict[str, object]:
        error: dict[str, object] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error["data"] = self.data
        # MCP types the id as string | number, so an unread id is omitted rather than null.
        return {"jsonrpc": "2.0", **({} if id_ is None else {"id": id_}), "error": error}


@dataclass
class RequestTrace:
    method: str | None = None
    tool: str | None = None
    id: str | int | None = None
    note: str = ""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: Mapping[str, object]
    run: Callable[[AgentApi, Mapping[str, object]], Response]


def _list_accounts(ext: AgentApi, arguments: Mapping[str, object]) -> Response:
    return json_success(get_ledger_data())


def _query(ext: AgentApi, arguments: Mapping[str, object]) -> Response:
    query_string = arguments.get("query_string")
    if not isinstance(query_string, str):
        return json_err("Send 'query_string' as a string.", HTTPStatus.BAD_REQUEST)
    try:
        result = g.ledger.query_shell.execute_query_serialised(
            g.filtered.entries_with_all_prices, query_string
        )
    except FavaAPIError as error:
        return json_err(error.message, HTTPStatus.INTERNAL_SERVER_ERROR)
    return json_success(result)


def _add_transaction(ext: AgentApi, arguments: Mapping[str, object]) -> Response:
    return ext.add(arguments)


def _string(description: str) -> dict[str, str]:
    return {"type": "string", "description": description}


TOOLS = (
    Tool(
        "list_accounts",
        "List the ledger's accounts. data.accounts holds the account names; "
        "data.account_details[<account>].meta['name-zh'] holds the Chinese alias that "
        "add_transaction accepts as source or target. Same data as GET /api/ledger_data.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        _list_accounts,
    ),
    Tool(
        "query",
        "Run a Beancount Query Language (BQL) query against the ledger, such as "
        '"SELECT account, sum(position) GROUP BY account". Same data as GET /api/query.',
        {
            "type": "object",
            "properties": {"query_string": _string("The BQL query to run.")},
            "required": ["query_string"],
            "additionalProperties": False,
        },
        _query,
    ),
    Tool(
        "add_transaction",
        "Add one transaction that moves amount from source to target. The entry is written "
        "with the ! flag and a person approves it in fava. Send exactly one of date or time.",
        {
            "type": "object",
            "properties": {
                "date": _string("Transaction date as YYYY-MM-DD."),
                "time": _string(
                    "Moment of the transaction as RFC 3339 with an offset, such as "
                    "2026-09-24T19:00:00+08:00; the date is taken in Asia/Taipei."
                ),
                "source": _string(
                    "Account the money leaves: the full account name, the full name-zh, "
                    "or the last segment of name-zh."
                ),
                "target": _string("Account the money goes to, in the same forms as source."),
                "amount": _string(
                    'Positive decimal string with at most 2 decimals, such as "190".'
                ),
                "currency": _string(
                    "Currency code such as TWD; defaults to the ledger's operating currency."
                ),
                "narration": _string("Free text without line breaks or control characters."),
                "key": _string(
                    "Idempotency key: generate one per transaction and reuse it on retry, so "
                    "a retry never writes twice. ASCII letters, digits, and - _ / . only."
                ),
                "dry_run": {
                    "type": "boolean",
                    "description": "When true, return the entry text without writing it.",
                },
            },
            "required": list(REQUIRED),
            "additionalProperties": False,
        },
        _add_transaction,
    ),
)


def _discover(
    ext: AgentApi, params: Mapping[str, object], trace: RequestTrace
) -> dict[str, object]:
    return {"supportedVersions": list(SUPPORTED), "capabilities": {"tools": {}}, **CACHE}


def _tools_list(
    ext: AgentApi, params: Mapping[str, object], trace: RequestTrace
) -> dict[str, object]:
    tools = [
        {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema}
        for tool in TOOLS
    ]
    return {"tools": tools, **CACHE}


def _tools_call(
    ext: AgentApi, params: Mapping[str, object], trace: RequestTrace
) -> dict[str, object]:
    name = params.get("name")
    tool = next((tool for tool in TOOLS if tool.name == name), None)
    if tool is None:
        raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
    trace.tool = tool.name
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        raise RpcError(INVALID_PARAMS, "Send 'arguments' as an object.")
    response = tool.run(ext, arguments)
    is_error = response.status_code >= 400
    if is_error:
        trace.note = "isError"
    return {
        "content": [{"type": "text", "text": response.get_data(as_text=True)}],
        "structuredContent": response.get_json(),
        "isError": is_error,
    }


METHODS = {
    "server/discover": _discover,
    "tools/list": _tools_list,
    "tools/call": _tools_call,
}


def handle(ext: AgentApi) -> Response:
    trace = RequestTrace()
    try:
        response = _handle(ext, trace)
    except RpcError as error:
        response = _json(error.body(trace.id), error.status)
        trace.note = str(error.code)
    line = f"mcp {trace.method or '-'} {trace.tool or '-'} {response.status_code} {trace.note}"
    print(line.rstrip(), file=sys.stderr, flush=True)
    return response


def _handle(ext: AgentApi, trace: RequestTrace) -> Response:
    origin = request.headers.get("Origin")
    if origin is not None:
        raise RpcError(INVALID_REQUEST, f"Origin {origin} is not allowed.", status=403)
    if request.method != "POST":
        return Response(status=405, headers={"Allow": "POST"})
    try:
        body = json.loads(request.get_data())
    # A deeply nested body raises RecursionError, not a JSON error.
    except (ValueError, RecursionError) as error:
        raise RpcError(PARSE_ERROR, "Parse error") from error
    if not isinstance(body, dict):
        raise RpcError(
            INVALID_REQUEST, "Send one JSON-RPC request object; batches are not supported."
        )
    method = body.get("method")
    if "id" not in body:
        if isinstance(method, str) and body.get("jsonrpc") == "2.0":
            return Response(status=202)
        raise RpcError(INVALID_REQUEST, "Invalid Request")
    id_ = body["id"]
    if isinstance(id_, bool) or not isinstance(id_, str | int):
        raise RpcError(INVALID_REQUEST, "Send 'id' as a string or an integer.")
    trace.id = id_
    params = body.get("params", {})
    if body.get("jsonrpc") != "2.0" or not isinstance(method, str) or not isinstance(params, dict):
        raise RpcError(INVALID_REQUEST, "Invalid Request")
    handler = METHODS.get(method)
    # Before the _meta and header checks, so a legacy `initialize` learns the supported version.
    if handler is None:
        raise RpcError(
            METHOD_NOT_FOUND,
            f"Method not found: {method}. This server speaks MCP {SUPPORTED[0]} only.",
        )
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        raise RpcError(
            INVALID_PARAMS,
            f"params._meta must be an object carrying {PROTOCOL_VERSION_KEY} "
            f"and {CLIENT_CAPABILITIES_KEY}.",
        )
    if missing := [
        key for key in (PROTOCOL_VERSION_KEY, CLIENT_CAPABILITIES_KEY) if key not in meta
    ]:
        raise RpcError(INVALID_PARAMS, f"params._meta is missing {', '.join(missing)}.")
    trace.method = method
    requested = meta[PROTOCOL_VERSION_KEY]
    _check_headers(method, params, requested)
    if requested not in SUPPORTED:
        raise RpcError(
            UNSUPPORTED_PROTOCOL_VERSION,
            f"Protocol version {requested} is not supported.",
            {"supported": list(SUPPORTED), "requested": requested},
        )
    result = handler(ext, params, trace)
    result.update(resultType="complete", _meta={"io.modelcontextprotocol/serverInfo": SERVER_INFO})
    return _json({"jsonrpc": "2.0", "id": id_, "result": result}, 200)


def _check_headers(method: str, params: Mapping[str, object], requested: object) -> None:
    headers = request.headers
    expected = {"MCP-Protocol-Version": requested, "Mcp-Method": method}
    if method == "tools/call":
        expected["Mcp-Name"] = params.get("name")
    for name, body_value in expected.items():
        if name not in headers or _decode(name, headers[name]) != body_value:
            raise RpcError(
                HEADER_MISMATCH, f"Header mismatch: {name} does not match the request body."
            )


def _decode(name: str, value: str) -> str:
    sentinel = B64_SENTINEL.fullmatch(value) if name == "Mcp-Name" else None
    if sentinel is None:
        if HEADER_SAFE.fullmatch(value) is None:
            raise RpcError(HEADER_MISMATCH, f"Header mismatch: {name} has invalid characters.")
        return value
    payload = sentinel.group(1)
    try:
        decoded = base64.b64decode(payload, validate=True)
        # validate=True still accepts non-canonical trailing bits; a spec client never emits them.
        if base64.b64encode(decoded).decode() != payload:
            raise ValueError
        return decoded.decode("utf-8")
    except ValueError as error:
        raise RpcError(HEADER_MISMATCH, f"Header mismatch: {name} is malformed base64.") from error


def _json(body: Mapping[str, object], status: int) -> Response:
    # ASCII escapes, as fava's jsonify does: a lone surrogate in an echoed string cannot be UTF-8.
    return Response(json.dumps(body), status, mimetype="application/json")
