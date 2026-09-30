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

AGENT_LEDGER = REPO / "tests" / "fixtures" / "agent-ledger"
TXNS = AGENT_LEDGER / "txns" / "2026.beancount"
DINNER = {"date": "2026-09-24", "source": "錢包", "target": "晚餐", "amount": "190", "key": "d1"}
DINNER_ENTRY = (
    "2026-09-24 ! ^ik-d1\n"
    "  Expenses:Food:Dinner                                  190 TWD\n"
    "  Assets:TW:Cash                                       -190 TWD\n"
)


@dataclass(frozen=True)
class Fava:
    container: str
    port: int

    def request(self, method: str, path: str, body: object = None) -> tuple[int, Any]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/agent{path}",
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {AGENT_TOKEN}", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def add(self, body: object) -> tuple[int, Any]:
        return self.request("POST", "/extension/AgentApi/transactions", body)

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


@dataclass(frozen=True)
class InProcess:
    app: Any
    txns: Path

    def add(self, body: object) -> tuple[int, Any]:
        response = self.app.test_client().post("/agent/extension/AgentApi/transactions", json=body)
        return response.status_code, response.get_json()


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


def test_link_on_a_note_is_not_a_transaction(in_process: InProcess) -> None:
    with in_process.txns.open("a", encoding="utf-8") as txns:
        txns.write('\n2026-09-01 note Assets:TW:Cash "wallet" ^ik-d1\n')

    assert in_process.add(DINNER)[0] == 201
