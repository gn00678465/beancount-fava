from __future__ import annotations

import datetime
import threading
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from beancount.core.data import Directive, Transaction
from fava.ext import FavaExtensionBase, extension_endpoint
from flask import Response, jsonify, request

from beancount_agent_api.core import (
    Accounts,
    Rejection,
    build_transaction,
    parse_add_request,
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
        if not _write_lock.acquire(timeout=LOCK_TIMEOUT):
            busy = Rejection("", "busy", "Another write is still running; retry with the same key.")
            return _error(busy, 503)
        try:
            return self._add()
        finally:
            _write_lock.release()

    def _add(self) -> Response:
        ledger = self.ledger
        ledger.changed()
        accounts = Accounts.from_directives(
            ledger.all_entries_by_type.Open,
            ledger.all_entries_by_type.Close,
            ledger.options["operating_currency"],
        )
        today = datetime.datetime.now(TZ).date()
        req = parse_add_request(request.get_json(silent=True), accounts, TZ, today)
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
