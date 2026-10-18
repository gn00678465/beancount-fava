from __future__ import annotations

import datetime
import threading
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from beancount.core.data import Directive, Transaction
from fava.beans.funcs import hash_entry
from fava.core.file import get_entry_slice
from fava.ext import FavaExtensionBase, extension_endpoint
from fava.helpers import FavaAPIError
from flask import Response, jsonify, request

from beancount_agent_api import mcp
from beancount_agent_api.core import (
    Accounts,
    ApproveItem,
    Rejection,
    approved_slice,
    build_transaction,
    parse_add_request,
    parse_approve_request,
    parse_spending_request,
    spending,
)

if TYPE_CHECKING:
    from fava.core import FavaLedger

TZ = ZoneInfo("Asia/Taipei")
LOCK_TIMEOUT = 10

# fava's FileModule lock covers only the file write. The link lookup and the insert must share
# one critical section, or two requests with the same key both miss the link and both write.
_write_lock = threading.Lock()


class AgentApi(FavaExtensionBase):
    def __init__(self, ledger: FavaLedger, config: str | None = None) -> None:
        super().__init__(ledger, config)
        # Any request thread can claim the reload that makes a write visible. Until that
        # thread finishes loading, the ledger lacks the new link, so a retry must also find
        # the entries this process wrote here.
        self._written: dict[str, Transaction] = {}

    @extension_endpoint("transactions", ["POST"])
    def transactions(self) -> Response:
        return self.add(request.get_json(silent=True))

    @extension_endpoint("spending", ["POST"])
    def spending_endpoint(self) -> Response:
        # fava reloads the ledger before every request outside its json_api blueprint, so this
        # read sees writes from fava, this API, and editors without calling ledger.changed().
        date = parse_spending_request(request.get_json(silent=True))
        if isinstance(date, Rejection):
            return _error(date, 422)
        ledger = self.ledger
        # Not fava's query shell: it keeps one connection per ledger, and a filtered query in
        # another request swaps the entries under this one.
        body = spending(
            ledger.all_entries_by_type.Transaction,
            ledger.all_entries_by_type.Open,
            ledger.options["name_expenses"],
            date,
        )
        return _json({**body, "ledger_errors": len(ledger.errors)}, 200)

    @extension_endpoint("approve", ["POST"])
    def approve_endpoint(self) -> Response:
        items = parse_approve_request(request.get_json(silent=True))
        if isinstance(items, Rejection):
            return _error(items, 422)
        if not _write_lock.acquire(timeout=LOCK_TIMEOUT):
            busy = Rejection("", "busy", "Another write is still running; retry the request.")
            return _error(busy, 503)
        try:
            return self._approve(items)
        finally:
            _write_lock.release()

    def _approve(self, items: list[ApproveItem]) -> Response:
        ledger = self.ledger
        ledger.changed()
        # The ledger reloads once after the last item, so earlier approvals in this request
        # replace their entries here; a repeated id or key then sees the approved entry.
        current = {hash_entry(txn): txn for txn in ledger.all_entries_by_type.Transaction}
        results = []
        for item in items:
            result: dict[str, object] = {"id": item.id, "key": item.key}
            entry = current.get(item.id)
            if entry is None and item.key is not None:
                link = f"ik-{item.key}"
                entry = next((txn for txn in current.values() if link in txn.links), None)
                if entry is None:
                    result["result"] = "not_found"
                elif entry.flag == "!":
                    # The key names a pending entry, but not the version the user saw.
                    result["result"] = "stale"
                    entry = None
            if entry is None:
                result.setdefault("result", "stale")
            elif entry.flag == "*":
                result["result"] = "already"
            elif entry.flag != "!":
                result["result"] = "not_pending"
            else:
                new_id = self._approve_entry(entry)
                if new_id is None:
                    result["result"] = "stale"
                else:
                    del current[item.id]
                    current[new_id] = entry._replace(flag="*")
                    result.update(result="approved", new_id=new_id)
            results.append(result)
        if any(result["result"] == "approved" for result in results):
            ledger.changed()
        return _json({"results": results, "ledger_errors": len(ledger.errors)}, 200)

    def _approve_entry(self, entry: Transaction) -> str | None:
        try:
            source, sha256sum = get_entry_slice(entry)
            approved = approved_slice(source, entry)
            if approved is None:
                return None
            self.ledger.file.save_entry_slice(hash_entry(entry), approved, sha256sum)
        except FavaAPIError:
            return None
        return hash_entry(entry._replace(flag="*"))

    @extension_endpoint("mcp", ["GET", "POST", "PUT", "DELETE"])
    def mcp_endpoint(self) -> Response:
        return mcp.handle(self)

    def add(self, raw: object) -> Response:
        if not _write_lock.acquire(timeout=LOCK_TIMEOUT):
            busy = Rejection("", "busy", "Another write is still running; retry with the same key.")
            return _error(busy, 503)
        try:
            return self._add(raw)
        finally:
            _write_lock.release()

    def _add(self, raw: object) -> Response:
        ledger = self.ledger
        ledger.changed()
        accounts = Accounts.from_directives(
            ledger.all_entries_by_type.Open,
            ledger.all_entries_by_type.Close,
            ledger.options["operating_currency"],
        )
        today = datetime.datetime.now(TZ).date()
        req = parse_add_request(raw, accounts, TZ, today)
        if isinstance(req, Rejection):
            return _error(req, 422)
        txn = build_transaction(req)
        text = self._render(txn)
        existing = self._existing(req.link)
        if existing is not None:
            # Approving the entry in fava changes only its flag; a retry still matches it.
            if self._render(existing._replace(flag=txn.flag)) != text:
                conflict = Rejection(
                    "key", "key_conflict", f"{req.link} already marks a different transaction."
                )
                return _error(conflict, 409, entry=self._render(existing))
            return _json({"created": False, "link": req.link, "entry": self._render(existing)}, 200)
        if req.dry_run:
            return _json({"created": False, "dry_run": True, "link": req.link, "entry": text}, 200)
        before = len(ledger.errors)
        ledger.file.insert_entries([txn])
        self._written[req.link] = txn
        ledger.changed()
        errors = {"before": before, "after": len(ledger.errors)}
        return _json({"created": True, "link": req.link, "entry": text, "errors": errors}, 201)

    def _existing(self, link: str) -> Transaction | None:
        loaded = (txn for txn in self.ledger.all_entries_by_type.Transaction if link in txn.links)
        return next(loaded, self._written.get(link))

    def _render(self, entry: Directive) -> str:
        # fava.beans.str imports fava.core, which imports fava.beans.str back; importing it at
        # module load, before anything has imported fava.core, raises a circular ImportError.
        from fava.beans.str import to_string

        options = self.ledger.fava_options
        return to_string(entry, options.currency_column, options.indent)


def _error(rejection: Rejection, status: int, **extra: object) -> Response:
    error = {
        "field": rejection.field,
        "code": rejection.code,
        "message": rejection.message,
        "candidates": list(rejection.candidates),
    }
    return _json({"error": error, **extra}, status)


def _json(body: dict[str, object], status: int) -> Response:
    response = jsonify(body)
    response.status_code = status
    return response
