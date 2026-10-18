from __future__ import annotations

import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fava.core as fava_core
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


def test_agent_reads_spending_only_by_post(fava: Fava) -> None:
    path = "/extension/AgentApi/spending"
    status, body = fava.request("POST", path, {"date": "2026-09-20"})
    assert (status, [txn["narration"] for txn in body["transactions"]]) == (200, ["dinner"])
    request = urllib.request.Request(
        f"http://127.0.0.1:{fava.port}/agent{path}",
        headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
    )
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=30)
    assert error.value.code == 403


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


SPENDING_LEDGER = REPO / "tests" / "fixtures" / "spending-ledger"
SPENDING = "/spending/extension/AgentApi/spending"
WEB_123 = {
    "date": "2026-10-05",
    "flag": "!",
    "payee": "全聯",
    "narration": "採買與晚餐",
    "key": "web-123",
    "expenses": [
        {
            "account": "Expenses:Daily:Groceries",
            "name_zh": "日用/雜貨",
            "number": "300",
            "currency": "TWD",
        },
        {
            "account": "Expenses:Food:Dinner",
            "name_zh": "食物/晚餐",
            "number": "150",
            "currency": "TWD",
        },
    ],
    "funding": [
        {
            "account": "Liabilities:Card:Cathay",
            "name_zh": "國泰卡",
            "number": "-450",
            "currency": "TWD",
        }
    ],
}
REFUND = {
    "date": "2026-10-05",
    "flag": "*",
    "payee": "全聯",
    "narration": "退貨",
    "key": None,
    "expenses": [
        {
            "account": "Expenses:Daily:Groceries",
            "name_zh": "日用/雜貨",
            "number": "-50",
            "currency": "TWD",
        }
    ],
    "funding": [
        {
            "account": "Liabilities:Card:Cathay",
            "name_zh": "國泰卡",
            "number": "50",
            "currency": "TWD",
        }
    ],
}
HOTEL = {
    "date": "2026-10-05",
    "flag": "!",
    "payee": "Hotel",
    "narration": "訂金",
    "key": None,
    "expenses": [
        {"account": "Expenses:Travel", "name_zh": None, "number": "10.50", "currency": "USD"}
    ],
    "funding": [
        {
            "account": "Liabilities:Card:Cathay",
            "name_zh": "國泰卡",
            "number": "-337.050",
            "currency": "TWD",
        }
    ],
}
OCTOBER = {
    "month": "2026-10",
    "total": [{"currency": "TWD", "number": "680"}, {"currency": "USD", "number": "30.50"}],
    "pending": [{"currency": "TWD", "number": "450"}, {"currency": "USD", "number": "10.50"}],
}


@dataclass(frozen=True)
class SpendingApp:
    app: Any
    txns: Path

    def read(self, body: object) -> tuple[int, Any]:
        response = self.app.test_client().post(
            SPENDING, data=json.dumps(body), content_type="application/json"
        )
        return response.status_code, response.get_json()

    def day(self, date: str) -> Any:
        status, body = self.read({"date": date})
        assert status == 200, body
        return body

    def without_ids(self, date: str) -> Any:
        body = self.day(date)
        for txn in body["transactions"]:
            assert txn.pop("id")
        return body


@pytest.fixture
def spending_app(tmp_path: Path) -> SpendingApp:
    shutil.copytree(SPENDING_LEDGER, tmp_path / "ledger")
    app = create_app([str(tmp_path / "ledger" / "main.beancount")], poll_watcher=True)
    return SpendingApp(app, tmp_path / "ledger" / "txns.beancount")


class Background:
    """Runs one work on a daemon thread.

    A ThreadPoolExecutor joins its threads on exit and at interpreter shutdown, so a stuck
    work would hang the run past any result() timeout. A daemon thread holds up neither.

    A work outlives its test only after result() has failed that test. If it resumes later, it
    runs through the class patches of later tests, so failures after the first one in such a
    run can be consequences of it.
    """

    def __init__(self, work: Callable[[], Any]) -> None:
        self._outcome: tuple[bool, Any] | None = None
        self._thread = threading.Thread(target=self._run, args=(work,), daemon=True)
        self._thread.start()

    def _run(self, work: Callable[[], Any]) -> None:
        try:
            self._outcome = (True, work())
        except BaseException as error:  # noqa: BLE001  # re-raised in result()
            self._outcome = (False, error)

    def result(self, deadline: float) -> Any:
        self._thread.join(max(0.0, deadline - time.monotonic()))
        if self._thread.is_alive() or self._outcome is None:
            pytest.fail("a background work did not finish before the deadline")
        ok, value = self._outcome
        if not ok:
            raise value
        return value


def run_together(*works: Callable[[], Any], timeout: float) -> list[Any]:
    started = [Background(work) for work in works]
    deadline = time.monotonic() + timeout
    return [work.result(deadline) for work in started]


def test_spending_lists_the_day_and_sums_the_month(spending_app: SpendingApp) -> None:
    # The cash withdrawal on 2026-10-05 has no expense posting, so it is not listed.
    assert spending_app.without_ids("2026-10-05") == {
        "date": "2026-10-05",
        "transactions": [WEB_123, REFUND, HOTEL],
        "month": OCTOBER,
        "ledger_errors": 0,
    }
    assert spending_app.without_ids("2026-10-31") == {
        "date": "2026-10-31",
        "transactions": [
            {
                "date": "2026-10-31",
                "flag": "*",
                "payee": None,
                "narration": "月底晚餐",
                "key": None,
                "expenses": [
                    {
                        "account": "Expenses:Food:Dinner",
                        "name_zh": "食物/晚餐",
                        "number": "200",
                        "currency": "TWD",
                    }
                ],
                "funding": [
                    {
                        "account": "Assets:TW:Cash",
                        "name_zh": "錢包",
                        "number": "-200",
                        "currency": "TWD",
                    }
                ],
            }
        ],
        "month": OCTOBER,
        "ledger_errors": 0,
    }
    september = spending_app.without_ids("2026-09-30")
    assert [txn["narration"] for txn in september["transactions"]] == ["月底宵夜"]
    assert september["month"] == {
        "month": "2026-09",
        "total": [{"currency": "TWD", "number": "100"}],
        "pending": [],
    }
    november = spending_app.without_ids("2026-11-02")
    assert (november["transactions"], november["month"]["total"]) == (
        [],
        [{"currency": "TWD", "number": "999"}],
    )


def test_spending_id_names_the_entry_in_fava(spending_app: SpendingApp) -> None:
    ids = [txn["id"] for txn in spending_app.day("2026-10-05")["transactions"]]
    first_lines = []
    for id_ in ids:
        response = spending_app.app.test_client().get(
            "/spending/api/source_slice", query_string={"entry_hash": id_}
        )
        first_lines.append(response.get_json()["data"]["slice"].splitlines()[0])
    assert first_lines == [
        '2026-10-05 ! "全聯" "採買與晚餐" ^ik-web-123',
        '2026-10-05 * "全聯" "退貨"',
        '2026-10-05 ! "Hotel" "訂金"',
    ]


@pytest.mark.parametrize(
    ("body", "field", "code"),
    [
        ({"date": "2026-10-01 OR TRUE"}, "date", "invalid_date"),
        ({"date": "20261001"}, "date", "invalid_date"),
        ({"date": "2026-02-30"}, "date", "invalid_date"),
        ({"date": "9999-12-15"}, "date", "invalid_date"),
        ({"date": 20261001}, "date", "invalid_date"),
        ({"date": "2026-10-01", "to": "2026-10-31"}, "to", "unknown_field"),
        ({}, "date", "missing"),
        (["2026-10-01"], "", "invalid_body"),
    ],
)
def test_spending_rejections(
    spending_app: SpendingApp, body: object, field: str, code: str
) -> None:
    status, response = spending_app.read(body)
    assert (status, response["error"]["field"], response["error"]["code"]) == (422, field, code)


def test_spending_sees_an_approval_in_fava(spending_app: SpendingApp) -> None:
    client = spending_app.app.test_client()
    id_ = spending_app.day("2026-10-05")["transactions"][0]["id"]
    source = client.get("/spending/api/source_slice", query_string={"entry_hash": id_})
    data = source.get_json()["data"]
    approved = client.put(
        "/spending/api/source_slice",
        json={
            "entry_hash": id_,
            "source": data["slice"].replace(" ! ", " * ", 1),
            "sha256sum": data["sha256sum"],
        },
    )
    assert approved.status_code == 200

    body = spending_app.without_ids("2026-10-05")
    assert body["transactions"][0] == {**WEB_123, "flag": "*"}
    assert body["month"]["pending"] == [{"currency": "USD", "number": "10.50"}]


def test_spending_sees_a_write_through_the_api(spending_app: SpendingApp) -> None:
    response = spending_app.app.test_client().post(
        "/spending/extension/AgentApi/transactions",
        data=json.dumps(
            {"date": "2026-10-05", "source": "錢包", "target": "晚餐", "amount": "70", "key": "k"}
        ),
        content_type="application/json",
    )
    assert response.status_code == 201

    body = spending_app.without_ids("2026-10-05")
    assert [txn["key"] for txn in body["transactions"]] == ["web-123", None, None, "k"]
    assert body["month"]["pending"][0] == {"currency": "TWD", "number": "520"}


def test_spending_sees_an_edit_to_the_file(spending_app: SpendingApp) -> None:
    assert spending_app.day("2026-10-06")["transactions"] == []
    with spending_app.txns.open("a", encoding="utf-8") as txns:
        txns.write('\n2026-10-06 * "咖啡"\n  Expenses:Food:Dinner  60 TWD\n  Assets:TW:Cash\n')

    body = spending_app.day("2026-10-06")
    assert [txn["narration"] for txn in body["transactions"]] == ["咖啡"]
    assert body["month"]["total"][0] == {"currency": "TWD", "number": "740"}

    with spending_app.txns.open("a", encoding="utf-8") as txns:
        txns.write("\n2026-10-07 * broken\n")
    ledger_errors = spending_app.day("2026-10-06")["ledger_errors"]
    # fava's /api/errors does not reload, so it reads what the request above loaded.
    errors = spending_app.app.test_client().get("/spending/api/errors").get_json()["data"]
    assert ledger_errors == len(errors) > 0


def test_spending_uses_the_ledger_root_for_expenses(tmp_path: Path) -> None:
    ledger = tmp_path / "main.beancount"
    ledger.write_text(
        'option "title" "Spending"\n'
        'option "operating_currency" "TWD"\n'
        'option "name_expenses" "Costs"\n'
        'option "name_income" "CostsBack"\n'
        '2020-01-01 custom "fava-extension" "beancount_agent_api"\n'
        "2026-01-01 open Assets:Cash TWD\n"
        "2026-01-01 open Costs:Food TWD\n"
        "2026-01-01 open CostsBack:Refund TWD\n"
        '2026-10-05 ! "午餐"\n  Costs:Food  100 TWD\n  Assets:Cash\n'
        '2026-10-05 * "退款"\n  CostsBack:Refund  -30 TWD\n  Assets:Cash\n',
        encoding="utf-8",
    )
    app = SpendingApp(create_app([str(ledger)], poll_watcher=True), ledger)
    # CostsBack shares the text prefix but is another root, so the refund is not listed.
    body = app.without_ids("2026-10-05")
    hundred = [{"currency": "TWD", "number": "100"}]
    assert body == {
        "date": "2026-10-05",
        "transactions": [
            {
                "date": "2026-10-05",
                "flag": "!",
                "payee": None,
                "narration": "午餐",
                "key": None,
                "expenses": [
                    {"account": "Costs:Food", "name_zh": None, "number": "100", "currency": "TWD"}
                ],
                "funding": [
                    {"account": "Assets:Cash", "name_zh": None, "number": "-100", "currency": "TWD"}
                ],
            }
        ],
        "month": {"month": "2026-10", "total": hundred, "pending": hundred},
        "ledger_errors": 0,
    }


def test_spending_during_another_reload_is_old_until_the_next_read(
    spending_app: SpendingApp, monkeypatch: Any
) -> None:
    assert spending_app.day("2026-10-06")["month"]["total"][0]["number"] == "680"
    with spending_app.txns.open("a", encoding="utf-8") as txns:
        txns.write('\n2026-10-06 * "咖啡"\n  Expenses:Food:Dinner  60 TWD\n  Assets:TW:Cash\n')

    # Hold another request inside load_file, after fava's watcher has handed it the change.
    loading, release = threading.Event(), threading.Event()
    load = fava_core.load_uncached

    def held_load(*args: Any, **kwargs: Any) -> Any:
        loading.set()
        assert release.wait(30)
        return load(*args, **kwargs)

    monkeypatch.setattr(fava_core, "load_uncached", held_load)
    other = Background(lambda: spending_app.app.test_client().get("/spending/income_statement/"))
    try:
        assert loading.wait(30)
        during = spending_app.day("2026-10-06")
    finally:
        release.set()
    assert other.result(time.monotonic() + 30).status_code == 200

    assert (during["transactions"], during["month"]["total"][0]["number"]) == ([], "680")
    after = spending_app.day("2026-10-06")
    assert (
        [txn["narration"] for txn in after["transactions"]],
        after["month"]["total"][0]["number"],
    ) == (["咖啡"], "740")


def test_spending_relies_on_fava_to_reload(spending_app: SpendingApp, monkeypatch: Any) -> None:
    spending_app.day("2026-10-05")
    calls = []
    changed = FavaLedger.changed

    def counted(ledger: FavaLedger) -> bool:
        calls.append(ledger)
        return changed(ledger)

    monkeypatch.setattr(FavaLedger, "changed", counted)
    spending_app.day("2026-10-05")
    # One call is fava's before_request; a second would mean the endpoint reloads by itself.
    assert len(calls) == 1


def test_spending_ignores_filters_of_concurrent_queries(spending_app: SpendingApp) -> None:
    query = {"query_string": "SELECT date WHERE date = 2026-10-05"}
    expected = spending_app.day("2026-10-05")
    client = spending_app.app.test_client()
    rows = client.get("/spending/api/query", query_string=query).json["data"]["rows"]
    rounds = 200
    barrier = threading.Barrier(3, timeout=30)

    def filtered_query() -> tuple[int, int]:
        client = spending_app.app.test_client()
        barrier.wait()
        for _ in range(rounds):
            client.get("/spending/api/query", query_string={**query, "time": "2019"})
        return rounds, 0

    def unfiltered_query() -> tuple[int, int]:
        # The control: fava's own query endpoint reads the other request's filter.
        client = spending_app.app.test_client()
        barrier.wait()
        done = wrong = 0
        for _ in range(rounds):
            if client.get("/spending/api/query", query_string=query).json["data"]["rows"] != rows:
                wrong += 1
            done += 1
        return done, wrong

    def read_spending() -> tuple[int, int]:
        barrier.wait()
        done = wrong = 0
        for _ in range(rounds):
            if spending_app.read({"date": "2026-10-05"}) != (200, expected):
                wrong += 1
            done += 1
        return done, wrong

    (_, _), (query_done, query_wrong), (spending_done, spending_wrong) = run_together(
        filtered_query, unfiltered_query, read_spending, timeout=120
    )

    assert (query_done, spending_done) == (rounds, rounds)
    assert spending_wrong == 0
    assert query_wrong > 0
