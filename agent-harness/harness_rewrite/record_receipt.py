"""Read-only receipt for one work record named by its exact record ID.

An unattended check (for example a Relay post-run verify) has no native
session, but it holds the record ID its run reported. The installed program
is the reader identity, as for the account effect service. The receipt names
only the record's WI, kind, author, journal revision and commit instant;
summary, artifacts, the author session and every other WI fact stay behind
native authentication. A record ID is unguessable, so knowing it is the only
capability this reader needs.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path

from .kernel import Kernel
from .model import Json, Query, Rejected, identity, operation_id, writer_identity
from .production_ledger import ProductionLedger

SCHEMA = "harness.record_receipt/v1"
PAGE = 500


def receipt(kernel: Kernel, handle: object, namespace: str, wi: str, record_id: str) -> Json:
    """Locate the committed request that wrote `record_id` in `wi` and its author."""
    identity(wi)
    identity(record_id)
    found: Json | None = None
    after = 0
    while found is None:
        page = kernel.inspect(Query(wi, view="history", after=after, limit=PAGE), handle)
        if page["status"] == "rejected":
            return page
        entries = page["result"]["history"]
        found = next(
            (
                entry
                for entry in entries
                if "WorkRecorded" in entry["event_kinds"]
                and operation_id(namespace, entry["request_id"]) == record_id
            ),
            None,
        )
        if found is None and len(entries) < PAGE:
            raise Rejected("record_absent", "No work record with this ID exists in this WI")
        after = page["result"]["next_cursor"] or after
    records = kernel.inspect(Query(wi, view="records"), handle)
    if records["status"] == "rejected":
        return records
    record = next(
        (
            item
            for item in records["result"]["items"]
            if item.get("kind") == "WorkRecorded" and item.get("record_id") == record_id
        ),
        None,
    )
    if record is None:
        raise Rejected("record_absent", "No work record with this ID exists in this WI")
    return {
        "schema": SCHEMA,
        "status": "found",
        "work_item_id": wi,
        "record_id": record_id,
        "kind": "WorkRecorded",
        "author": record["author"],
        "revision": found["revision"],
        "recorded_at": found["at"],
    }


def main(entry: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Read the receipt of one exact work record")
    parser.add_argument("--wi", required=True)
    parser.add_argument("--record", required=True)
    args = parser.parse_args(argv)
    try:
        from .effect_service import ServiceHost

        host = ServiceHost(entry)
        host.current = replace(
            host.current,
            actor="record-receipt",
            session="record-receipt",
            readable_wis=frozenset({identity(args.wi)}),
            allowed_intents=frozenset(),
            effect_kinds=frozenset(),
            read_only_reason="record_receipt",
        )
        ledger = ProductionLedger(host.policy["production_ledger"], host.policy["namespace"])
        kernel = Kernel(
            None,
            host.authenticate,
            storage=ledger,
            writer_id=writer_identity(host.seal[0], host.policy),
        )
        response = receipt(kernel, host.handle, host.policy["namespace"], args.wi, args.record)
        if response.get("status") == "found":
            response["ledger"] = {
                "identity": host.policy["production_ledger"]["identity"],
                "ref": host.policy["production_ledger"]["ref"],
            }
    except Rejected as exc:
        response = Kernel._error(exc)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        response = Kernel._error(
            Rejected("source_unavailable", "Receipt input unavailable: " + type(exc).__name__)
        )
    print(json.dumps(response, ensure_ascii=False, sort_keys=True))
    return 0 if response.get("status") == "found" else 2
