from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import AGENT_TOKEN, REPO, docker, ledger_volume_of, running_fava
from fava.application import create_app
from fava.core import FavaLedger

from beancount_agent_api.core import FIELDS

AGENT_LEDGER = REPO / "tests" / "fixtures" / "agent-ledger"
TXNS = AGENT_LEDGER / "txns" / "2026.beancount"
DINNER = {"date": "2026-09-24", "source": "錢包", "target": "晚餐", "amount": "190", "key": "d1"}
DINNER_ENTRY = (
    "2026-09-24 ! ^ik-d1\n"
    "  Expenses:Food:Dinner                                  190 TWD\n"
    "  Assets:TW:Cash                                       -190 TWD\n"
)
MCP = "/extension/AgentApi/mcp"
VERSION = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
}
SERVER_META = {
    "io.modelcontextprotocol/serverInfo": {"name": "beancount-agent-api", "version": "0"}
}
BALANCES = "SELECT account, sum(position) GROUP BY account"


def rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {"_meta": META, **(params or {})},
    }


def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return rpc("tools/call", {"name": name, "arguments": arguments})


def mcp_headers(body: dict[str, Any]) -> dict[str, str]:
    headers = {"MCP-Protocol-Version": VERSION, "Mcp-Method": body["method"]}
    if body["method"] == "tools/call":
        headers["Mcp-Name"] = body["params"]["name"]
    return headers


@dataclass(frozen=True)
class Fava:
    container: str
    port: int

    def request(
        self, method: str, path: str, body: object = None, headers: dict[str, str] | None = None
    ) -> tuple[int, Any]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/agent{path}",
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {AGENT_TOKEN}",
                "Content-Type": "application/json",
                **(headers or {}),
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def add(self, body: object) -> tuple[int, Any]:
        return self.request("POST", "/extension/AgentApi/transactions", body)

    def mcp(self, method: str, params: dict[str, Any] | None = None) -> tuple[int, Any]:
        body = rpc(method, params)
        return self.request("POST", MCP, body, mcp_headers(body))

    def txns(self) -> str:
        result = docker("exec", self.container, "cat", "/ledger/txns/2026.beancount", check=True)
        return result.stdout


@pytest.fixture
def fava(image: str, tmp_path: Path) -> Iterator[Fava]:
    with (
        ledger_volume_of(image, AGENT_LEDGER) as volume,
        running_fava(image, volume, "/ledger/main.beancount", tmp_path) as (container, port),
    ):
        yield Fava(container, port)


def test_extension_loads_from_the_installed_package(fava: Fava) -> None:
    status, body = fava.request("GET", "/api/errors")
    assert (status, body["data"]) == (200, [])


def test_add_appends_once_per_key(fava: Fava) -> None:
    original = TXNS.read_text(encoding="utf-8")

    assert fava.add(DINNER) == (
        201,
        {
            "created": True,
            "link": "ik-d1",
            "entry": DINNER_ENTRY,
            "errors": {"before": 0, "after": 0},
        },
    )
    assert fava.txns() == original + "\n" + DINNER_ENTRY

    assert fava.add(DINNER) == (200, {"created": False, "link": "ik-d1", "entry": DINNER_ENTRY})
    assert fava.txns() == original + "\n" + DINNER_ENTRY


def test_dry_run_shows_the_entry_without_writing(fava: Fava) -> None:
    original = TXNS.read_text(encoding="utf-8")

    assert fava.add({**DINNER, "dry_run": True}) == (
        200,
        {"created": False, "dry_run": True, "link": "ik-d1", "entry": DINNER_ENTRY},
    )
    assert fava.txns() == original

    status, body = fava.add(DINNER)
    assert (status, body["entry"]) == (201, DINNER_ENTRY)


def test_time_is_written_as_local_metadata(fava: Fava) -> None:
    breakfast = {
        "time": "2026-09-24T23:00:00Z",
        "source": "錢包",
        "target": "食物/晚餐",
        "amount": "80",
        "narration": "早餐",
        "key": "b1",
    }
    status, _ = fava.add(breakfast)
    assert status == 201
    assert fava.txns().endswith(
        '\n2026-09-25 ! "早餐" ^ik-b1\n'
        '  time: "07:00:00"\n'
        "  Expenses:Food:Dinner                                   80 TWD\n"
        "  Assets:TW:Cash                                        -80 TWD\n"
    )


def test_rejection_lists_candidates(fava: Fava) -> None:
    original = TXNS.read_text(encoding="utf-8")
    assert fava.add({**DINNER, "target": "午餐"}) == (
        422,
        {
            "error": {
                "field": "target",
                "code": "ambiguous_account",
                "message": "'午餐' matches more than one account; send one of the candidates.",
                "candidates": ["Expenses:Food:Lunch", "Expenses:Work:Lunch"],
            }
        },
    )
    assert fava.add({**DINNER, "postings": []})[1]["error"]["code"] == "unknown_field"
    assert fava.txns() == original


def test_concurrent_same_key_writes_once(fava: Fava) -> None:
    body = {**DINNER, "key": "c1"}
    statuses: list[int] = []
    barrier = threading.Barrier(5)

    def send() -> None:
        barrier.wait()
        statuses.append(fava.add(body)[0])

    threads = [threading.Thread(target=send) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [200, 200, 200, 200, 201]
    assert fava.txns().count("^ik-c1") == 1


def test_mcp_over_http(fava: Fava) -> None:
    original = TXNS.read_text(encoding="utf-8")

    status, body = fava.mcp("server/discover")
    assert (status, body["result"]["supportedVersions"]) == (200, ["2026-07-28"])

    status, body = fava.mcp("tools/list")
    names = [tool["name"] for tool in body["result"]["tools"]]
    assert (status, names) == (200, ["list_accounts", "query", "add_transaction"])

    status, body = fava.mcp("tools/call", {"name": "add_transaction", "arguments": DINNER})
    assert (status, body["result"]["isError"]) == (200, False)
    assert body["result"]["structuredContent"]["entry"] == DINNER_ENTRY
    assert fava.txns() == original + "\n" + DINNER_ENTRY


@dataclass(frozen=True)
class InProcess:
    app: Any
    txns: Path

    def add(self, body: object) -> tuple[int, Any]:
        # Flask's json= sorts object keys; a real client keeps its own key order.
        response = self.app.test_client().post(
            "/agent/extension/AgentApi/transactions",
            data=json.dumps(body),
            content_type="application/json",
        )
        return response.status_code, response.get_json()

    def get(self, path: str, **query: str) -> Any:
        return self.app.test_client().get(f"/agent{path}", query_string=query)

    def mcp(self, body: Any, headers: dict[str, str] | None = None, method: str = "POST") -> Any:
        data = body if isinstance(body, str) else json.dumps(body)
        return self.app.test_client().open(
            f"/agent{MCP}",
            method=method,
            data=data,
            content_type="application/json",
            headers=mcp_headers(body) if headers is None else headers,
        )

    def result(self, body: dict[str, Any]) -> Any:
        response = self.mcp(body)
        assert response.status_code == 200
        return response.get_json()["result"]


@pytest.fixture
def in_process(tmp_path: Path) -> InProcess:
    shutil.copytree(AGENT_LEDGER, tmp_path / "ledger")
    app = create_app([str(tmp_path / "ledger" / "main.beancount")], poll_watcher=True)
    return InProcess(app, tmp_path / "ledger" / "txns" / "2026.beancount")


def test_retry_before_the_reload_lands(in_process: InProcess, monkeypatch: Any) -> None:
    # Another request thread has claimed every reload and is still loading.
    monkeypatch.setattr(FavaLedger, "changed", lambda _: False)

    assert in_process.add(DINNER)[0] == 201
    assert in_process.add(DINNER) == (
        200,
        {"created": False, "link": "ik-d1", "entry": DINNER_ENTRY},
    )
    assert in_process.txns.read_text(encoding="utf-8").count("^ik-d1") == 1


def test_key_reused_for_another_transaction(in_process: InProcess) -> None:
    assert in_process.add(DINNER)[0] == 201
    status, body = in_process.add({**DINNER, "amount": "200"})
    assert (status, body["error"]["code"], body["entry"]) == (409, "key_conflict", DINNER_ENTRY)
    assert in_process.txns.read_text(encoding="utf-8").count("^ik-d1") == 1


def test_retry_after_the_entry_was_approved(in_process: InProcess) -> None:
    assert in_process.add(DINNER)[0] == 201
    text = in_process.txns.read_text(encoding="utf-8")
    in_process.txns.write_text(text.replace("2026-09-24 ! ^ik-d1", "2026-09-24 * ^ik-d1"))

    status, body = in_process.add(DINNER)
    assert (status, body["entry"]) == (200, DINNER_ENTRY.replace(" ! ", " * "))


REPAID = {
    **DINNER,
    "narration": "手機",
    "payee": "小明",
    "tags": ["reimburse", "family"],
    "meta": {"via": "line-pay", "note": "還代墊"},
    "key": "r1",
}
REPAID_ENTRY = (
    '2026-09-24 ! "小明" "手機" #family #reimburse ^ik-r1\n'
    '  note: "還代墊"\n'
    '  via: "line-pay"\n'
    "  Expenses:Food:Dinner                                  190 TWD\n"
    "  Assets:TW:Cash                                       -190 TWD\n"
)


def test_payee_tags_and_meta_retry_in_any_order(in_process: InProcess) -> None:
    original = in_process.txns.read_text(encoding="utf-8")
    assert in_process.add(REPAID) == (
        201,
        {
            "created": True,
            "link": "ik-r1",
            "entry": REPAID_ENTRY,
            "errors": {"before": 0, "after": 0},
        },
    )
    assert in_process.txns.read_text(encoding="utf-8") == original + "\n" + REPAID_ENTRY

    # A new app has only the file, so the retry compares against the parsed entry.
    ledger = in_process.txns.parent.parent / "main.beancount"
    restarted = InProcess(create_app([str(ledger)], poll_watcher=True), in_process.txns)
    reordered = {
        **REPAID,
        "tags": ["family", "reimburse", "family"],
        "meta": {"note": "還代墊", "via": "line-pay"},
    }
    assert restarted.add(reordered) == (
        200,
        {"created": False, "link": "ik-r1", "entry": REPAID_ENTRY},
    )

    status, body = restarted.add({**REPAID, "tags": ["reimburse"]})
    assert (status, body["error"]["code"], body["entry"]) == (409, "key_conflict", REPAID_ENTRY)
    assert in_process.txns.read_text(encoding="utf-8").count("^ik-r1") == 1


def test_meta_and_time_share_one_order(in_process: InProcess) -> None:
    timed = {k: v for k, v in REPAID.items() if k != "date"}
    status, body = in_process.add(
        {**timed, "time": "2026-09-24T23:00:00Z", "tags": [], "key": "r2"}
    )
    assert (status, body["entry"]) == (
        201,
        (
            '2026-09-25 ! "小明" "手機" ^ik-r2\n'
            '  note: "還代墊"\n'
            '  time: "07:00:00"\n'
            '  via: "line-pay"\n'
            "  Expenses:Food:Dinner                                  190 TWD\n"
            "  Assets:TW:Cash                                       -190 TWD\n"
        ),
    )


def test_link_on_a_note_is_not_a_transaction(in_process: InProcess) -> None:
    with in_process.txns.open("a", encoding="utf-8") as txns:
        txns.write('\n2026-09-01 note Assets:TW:Cash "wallet" ^ik-d1\n')

    assert in_process.add(DINNER)[0] == 201


def test_mcp_discover(in_process: InProcess) -> None:
    assert in_process.result(rpc("server/discover")) == {
        "supportedVersions": ["2026-07-28"],
        "capabilities": {"tools": {}},
        "ttlMs": 3600000,
        "cacheScope": "private",
        "resultType": "complete",
        "_meta": SERVER_META,
    }


def test_mcp_tools_list(in_process: InProcess) -> None:
    result = in_process.result(rpc("tools/list"))
    tools = {tool["name"]: tool for tool in result["tools"]}
    assert list(tools) == ["list_accounts", "query", "add_transaction"]
    assert (result["resultType"], result["ttlMs"], result["cacheScope"]) == (
        "complete",
        3600000,
        "private",
    )
    assert result["_meta"] == SERVER_META
    assert tools["add_transaction"]["inputSchema"]["required"] == [
        "source",
        "target",
        "amount",
        "key",
    ]
    assert tools["query"]["inputSchema"]["required"] == ["query_string"]


def test_mcp_list_accounts_matches_rest(in_process: InProcess) -> None:
    rest = in_process.get("/api/ledger_data")
    result = in_process.result(call("list_accounts", {}))
    assert result["isError"] is False
    assert result["structuredContent"] == rest.get_json()
    assert result["content"] == [{"type": "text", "text": rest.get_data(as_text=True)}]
    assert result["structuredContent"]["data"]["account_details"]["Assets:TW:Cash"]["meta"] == {
        "filename": str(in_process.txns.parent.parent / "main.beancount"),
        "lineno": 11,
        "name-zh": "錢包",
    }


def test_mcp_query_matches_rest(in_process: InProcess) -> None:
    rest = in_process.get("/api/query", query_string=BALANCES)
    result = in_process.result(call("query", {"query_string": BALANCES}))
    assert result["isError"] is False
    assert result["structuredContent"] == rest.get_json()
    assert result["content"] == [{"type": "text", "text": rest.get_data(as_text=True)}]
    assert result["structuredContent"]["data"]["rows"][0] == ["Assets:TW:Cash", {"TWD": 4850}]

    result = in_process.result(call("query", {"query_string": "SELEC"}))
    assert (result["isError"], result["structuredContent"]) == (
        True,
        {"error": "Query parse error: syntax error."},
    )
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]

    result = in_process.result(call("query", {}))
    assert (result["isError"], result["structuredContent"]) == (
        True,
        {"error": "Send 'query_string' as a string."},
    )


def test_mcp_add_transaction_writes_the_entry(in_process: InProcess) -> None:
    original = in_process.txns.read_text(encoding="utf-8")

    result = in_process.result(call("add_transaction", DINNER))
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "created": True,
        "link": "ik-d1",
        "entry": DINNER_ENTRY,
        "errors": {"before": 0, "after": 0},
    }
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert in_process.txns.read_text(encoding="utf-8") == original + "\n" + DINNER_ENTRY


def test_mcp_add_transaction_with_payee_tags_meta(in_process: InProcess) -> None:
    result = in_process.result(call("add_transaction", REPAID))
    assert (result["isError"], result["structuredContent"]) == (
        False,
        {
            "created": True,
            "link": "ik-r1",
            "entry": REPAID_ENTRY,
            "errors": {"before": 0, "after": 0},
        },
    )


def test_mcp_schema_lists_every_request_field(in_process: InProcess) -> None:
    tools = {tool["name"]: tool for tool in in_process.result(rpc("tools/list"))["tools"]}
    assert set(tools["add_transaction"]["inputSchema"]["properties"]) == set(FIELDS)


def test_mcp_rejection_is_a_tool_error(in_process: InProcess) -> None:
    original = in_process.txns.read_text(encoding="utf-8")
    ambiguous = {**DINNER, "target": "午餐"}

    result = in_process.result(call("add_transaction", ambiguous))
    assert result["isError"] is True
    assert result["structuredContent"] == {
        "error": {
            "field": "target",
            "code": "ambiguous_account",
            "message": "'午餐' matches more than one account; send one of the candidates.",
            "candidates": ["Expenses:Food:Lunch", "Expenses:Work:Lunch"],
        }
    }
    assert result["structuredContent"] == in_process.add(ambiguous)[1]
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert in_process.txns.read_text(encoding="utf-8") == original


def test_mcp_shares_the_key_with_rest(in_process: InProcess) -> None:
    shared = {**DINNER, "key": "shared"}
    assert in_process.add(shared)[0] == 201

    result = in_process.result(call("add_transaction", shared))
    assert result["structuredContent"]["created"] is False
    assert in_process.txns.read_text(encoding="utf-8").count("^ik-shared") == 1


def test_mcp_names_the_supported_version_for_legacy_clients(in_process: InProcess) -> None:
    response = in_process.mcp(rpc("initialize"))
    assert (response.status_code, response.get_json()) == (
        404,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32601,
                "message": "Method not found: initialize. This server speaks MCP 2026-07-28 only.",
            },
        },
    )


def test_mcp_decodes_the_base64_name_sentinel(in_process: InProcess) -> None:
    body = call("list_accounts", {})
    headers = {**mcp_headers(body), "Mcp-Name": "=?base64?bGlzdF9hY2NvdW50cw==?="}
    response = in_process.mcp(body, headers)
    assert (response.status_code, response.get_json()["result"]["isError"]) == (200, False)


LIST_HEADERS = {"MCP-Protocol-Version": VERSION, "Mcp-Method": "tools/list"}
LEGACY_META = {**META, "io.modelcontextprotocol/protocolVersion": "2025-11-25"}
LADDER_CASES = [
    ("POST", rpc("tools/list"), {"Mcp-Method": "tools/list"}, 400, -32020, None),
    ("POST", rpc("tools/list"), {**LIST_HEADERS, "Mcp-Method": "tools/call"}, 400, -32020, None),
    (
        "POST",
        call("query", {"query_string": BALANCES}),
        {**LIST_HEADERS, "Mcp-Method": "tools/call", "Mcp-Name": "list_accounts"},
        400,
        -32020,
        None,
    ),
    (
        "POST",
        {**rpc("tools/list"), "params": {"_meta": LEGACY_META}},
        {**LIST_HEADERS, "MCP-Protocol-Version": "2025-11-25"},
        400,
        -32022,
        {"supported": ["2026-07-28"], "requested": "2025-11-25"},
    ),
    ("POST", {**rpc("tools/list"), "params": {}}, None, 400, -32602, None),
    ("POST", rpc("initialize"), None, 404, -32601, None),
    ("POST", rpc("resources/list"), None, 404, -32601, None),
    ("POST", [rpc("tools/list")], LIST_HEADERS, 400, -32600, None),
    ("POST", "{not json", LIST_HEADERS, 400, -32700, None),
    pytest.param("POST", "[" * 1000000, LIST_HEADERS, 400, -32700, None, id="deep-nesting"),
    ("POST", call("nope", {}), None, 400, -32602, None),
    ("POST", {"jsonrpc": "2.0", "method": "notifications/initialized"}, {}, 202, None, None),
    ("GET", rpc("tools/list"), None, 405, None, None),
    ("DELETE", rpc("tools/list"), None, 405, None, None),
    ("POST", rpc("tools/list"), {**LIST_HEADERS, "Origin": "http://evil"}, 403, -32600, None),
]


@pytest.mark.parametrize(("method", "body", "headers", "status", "code", "data"), LADDER_CASES)
def test_mcp_error_ladder(
    in_process: InProcess,
    method: str,
    body: Any,
    headers: dict[str, str] | None,
    status: int,
    code: int | None,
    data: Any,
) -> None:
    response = in_process.mcp(body, headers, method)
    assert response.status_code == status
    if code is None:
        assert response.get_data() == b""
    else:
        error = response.get_json()["error"]
        assert (error["code"], error.get("data")) == (code, data)
    if status == 405:
        assert response.headers["Allow"] == "POST"


def test_mcp_malformed_mcp_name_is_a_header_mismatch(in_process: InProcess) -> None:
    body = rpc("tools/call", {"arguments": {}})
    headers = {**LIST_HEADERS, "Mcp-Method": "tools/call"}
    for name in ("=?base64?!!?=", "=?base64?bGlzdF9hY2NvdW50cx==?=", "é"):
        response = in_process.mcp(body, {**headers, "Mcp-Name": name})
        assert (response.status_code, response.get_json()["error"]["code"]) == (400, -32020)


def test_mcp_error_echoes_a_readable_id(in_process: InProcess) -> None:
    response = in_process.mcp({**rpc("tools/list"), "id": 7, "params": None}, LIST_HEADERS)
    assert (response.status_code, response.get_json()["id"]) == (400, 7)


def test_mcp_echoes_a_lone_surrogate_id(in_process: InProcess) -> None:
    response = in_process.mcp(json.dumps({**rpc("tools/list"), "id": "\ud800"}), LIST_HEADERS)
    assert (response.status_code, response.get_json()["id"]) == (200, "\ud800")


def test_mcp_log_names_only_known_methods(in_process: InProcess, capsys: Any) -> None:
    forged = "tools/list\nmcp tools/call add_transaction 200"
    in_process.mcp({**rpc("tools/list"), "method": forged}, LIST_HEADERS)
    in_process.mcp({"jsonrpc": "2.0", "method": forged}, {})
    in_process.result(rpc("tools/list"))
    assert capsys.readouterr().err == "mcp - - 404 -32601\nmcp - - 202\nmcp tools/list - 200\n"
