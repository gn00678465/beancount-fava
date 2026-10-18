from __future__ import annotations

import datetime
import errno
import logging
import os
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from beancount.core.data import Directive, Transaction
from beancount.loader import LoadError
from fava.beans.funcs import hash_entry
from fava.beans.load import load_uncached
from fava.ext import FavaExtensionBase, extension_endpoint
from flask import Response, jsonify, request

from beancount_agent_api import mcp
from beancount_agent_api.core import (
    Accounts,
    ApproveItem,
    Rejection,
    build_transaction,
    flag_offset,
    parse_add_request,
    parse_approve_request,
    parse_spending_request,
    spending,
)

if TYPE_CHECKING:
    from fava.core import FavaLedger

log = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Taipei")
LOCK_TIMEOUT = 10
LOAD_ATTEMPTS = 3
# beancount's loader reports a missing main file or include as an error and loads the rest.
MISSING_FILE_RE = re.compile(r'File (glob )?".*" (does not exist|does not match any files)')

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
        try:
            loaded = self._load_files()
        except OSError as error:
            unreadable = Rejection(
                "",
                "ledger_unreadable",
                f"A ledger file could not be read ({error}); no item was processed.",
            )
            return _error(unreadable, 503)
        if loaded is None:
            busy = Rejection("", "busy", "The ledger files kept changing; retry the request.")
            return _error(busy, 503)
        entries, files = loaded
        # The id hashes the whole entry, metadata and position included, so only the version
        # the user saw matches. Approvals in this request replace their entries here, so a
        # repeated id or key then sees the approved entry.
        current = {hash_entry(e): e for e in entries if isinstance(e, Transaction)}
        results = []
        approved_at: dict[int, tuple[str, int]] = {}
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
                outcome, message = self._write_flag(entry, files)
                if outcome == "approved":
                    approved = entry._replace(flag="*")
                    del current[item.id]
                    current[hash_entry(approved)] = approved
                    result.update(result="approved", new_id=None)
                    approved_at[len(results)] = (entry.meta["filename"], entry.meta["lineno"])
                else:
                    result["result"] = outcome
                    if message is not None:
                        result["message"] = message
            results.append(result)
        if approved_at:
            new_ids = self._approved_ids(files, set(approved_at.values()))
            for index, place in approved_at.items():
                results[index]["new_id"] = new_ids.get(place)
        reload_error = None
        if any(result["result"] in ("approved", "error") for result in results):
            try:
                ledger.changed()
            except OSError as error:
                # fava's watcher may have marked the change as seen before the load failed.
                reload_error = (
                    f"fava could not reload the ledger ({error}); fava may show the ledger "
                    "from before this request until a ledger file changes again or fava "
                    "restarts."
                )
        ledger_errors = None if reload_error else len(ledger.errors)
        body = {"results": results, "ledger_errors": ledger_errors, "reload_error": reload_error}
        return _json(body, 200)

    def _approved_ids(
        self, written: dict[str, bytes], places: set[tuple[str, int]]
    ) -> dict[tuple[str, int], str]:
        """The id of each approved entry, from a load of the files as this request wrote them.

        A plugin can derive data from the flag, so the id comes from a real load, not from
        the entry with its flag replaced. A place is left out when its file no longer holds
        what this request wrote, or when it does not hold exactly one transaction.
        """
        try:
            loaded = self._load_files()
        except OSError:
            return {}
        if loaded is None:
            return {}
        entries, files = loaded
        same = {path for path, data in files.items() if written.get(path) == data}
        found: dict[tuple[str, int], list[Transaction]] = {}
        for entry in entries:
            if isinstance(entry, Transaction):
                place = (entry.meta["filename"], entry.meta["lineno"])
                if place in places and place[0] in same:
                    found.setdefault(place, []).append(entry)
        return {place: hash_entry(txns[0]) for place, txns in found.items() if len(txns) == 1}

    def _load_files(self) -> tuple[list[Directive], dict[str, bytes]] | None:
        """Load the ledger from its files, with the bytes that load parsed.

        fava's own ledger can lag the files while another request reloads it, so approve
        loads by itself. Equal bytes alone do not tie the entries to them: a file can change
        and change back during the load. Any write moves the file's ctime, so an unchanged
        inode, size, mtime, and ctime as well rule that out, as far as the file system's
        timestamps resolve two writes.

        Raises OSError when a file cannot be read, or when the ledger still declares a file
        that does not exist. A file whose include was removed is simply no longer loaded.
        """
        paths = list(self.ledger.options["include"])
        for _ in range(LOAD_ATTEMPTS):
            before = _read_files(paths)
            entries, errors, options = load_uncached(
                self.ledger.beancount_file_path, is_encrypted=False
            )
            missing = [
                error.message
                for error in errors
                if isinstance(error, LoadError) and MISSING_FILE_RE.fullmatch(error.message)
            ]
            if missing:
                # The loaded entries lack a whole file, so no id or key lookup can be trusted.
                raise FileNotFoundError(errno.ENOENT, "; ".join(missing))
            paths = list(options["include"])
            after = _read_files(paths)
            if before is not None and before == after:
                return entries, {path: data for path, (data, _) in after.items()}
        return None

    def _write_flag(self, entry: Transaction, files: dict[str, bytes]) -> tuple[str, str | None]:
        """Change the entry's `!` to `*` in place, and nothing else in its file.

        Returns the item's result and, for "error", a message. An editor that saves between
        the last check and the write is not locked out; reading the file back catches it.
        """
        path, lineno = entry.meta["filename"], entry.meta["lineno"]
        data = files[path]
        offset = flag_offset(data, lineno)
        if offset is None:
            return "stale", None
        approved = data[:offset] + b"*" + data[offset + 1 :]
        where = f"{path} at byte {offset}"
        unknown = "the entry may or may not be approved; reload and check it in fava"
        # fava writes whole files under this lock; holding it keeps a fava edit from writing
        # back a copy read before this change.
        with self.ledger.file._lock:
            try:
                with open(path, "r+b") as file:
                    if file.read() != data:
                        return "stale", None
                    file.seek(offset)
                    file.write(b"*")
            except FileNotFoundError:
                return "stale", None
            except OSError as error:
                _notify(self.ledger, path)
                return "error", f"Writing * to {where} failed ({error}); {unknown}."
            _notify(self.ledger, path)
            try:
                with open(path, "rb") as file:
                    written = file.read()
            except OSError as error:
                return "error", f"Reading {where} back failed ({error}); {unknown}."
        if written != approved:
            return "error", (
                f"{where} did not read back as approved: another program wrote the file while "
                "the flag was written. That byte may now be a stray * in the other program's "
                "text, and it is not restored; check the file in fava."
            )
        files[path] = approved
        return "approved", None

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


def _notify(ledger: FavaLedger, path: str) -> None:
    # fava's watcher also finds the write by itself; the notice only records it at once.
    try:
        ledger.watcher.notify(Path(path))
    except OSError as error:
        log.warning("cannot notify fava's watcher of %s: %s", path, error)


def _read_files(paths: list[str]) -> dict[str, tuple[bytes, tuple[int, ...]]] | None:
    """Each file's bytes and the stat fields that change on any write to it."""
    files = {}
    try:
        for path in paths:
            with Path(path).open("rb") as file:
                stat = os.fstat(file.fileno())
                stamp = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                files[path] = (file.read(), stamp)
    except FileNotFoundError:
        return None
    return files


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
