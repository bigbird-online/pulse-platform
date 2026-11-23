"""Session packages: the facts one judgement must read together.

Facts are edges and the references below are nodes; connected nodes form one
session package (a native session with its subagents, Host executions and the
claims naming them). A fact with no node belongs to the public package.

| kind | nodes |
|---|---|
| usage | itself, `native_scope_ref`, `execution_ref`, each `covers` |
| execution | itself, `parent_ref` |
| attribution | itself, `target_ref`, `execution_ref`, a subcall's `parent_scope_ref`, an actor claim's authenticated input |
| human | itself, `execution_ref` |
| stage_link | itself, `target_refs` |
| coverage of a Pi inventory | itself, the native requests in its evidence |

An authenticated input is the `(node, entrypoint, input_ref)` of an actor
claim: `host_native_input` joins the Host execution and the Pi requests that
carry the same one, and nothing else need connect the two.

`coverage_ref` and `assignment_ref` are not nodes: they gather facts by
source or by WI and would join unrelated sessions. Every other kind is public.
What a judgement compares through `assignment_ref` is the binding alias table
(`pi_binding_alias`), which names assignments and no session: its claims are
shared facts, public and read with every package (`is_shared`).
`LINK_LOGIC` binds a fact index to this file, so changing a key rebuilds it.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from .receipts import FIELDS, Receipt, canonical

LINKED_KINDS = frozenset({"usage", "execution", "attribution", "human", "stage_link"})
INVENTORY_PRODUCERS = frozenset({"pi_native_inventory.v1", "pi_parent_history.v1"})
PUBLIC_KINDS = frozenset(FIELDS) - LINKED_KINDS
SHARED_PRODUCERS = frozenset({"pi_canonical_binding_alias.v1"})
LINK_LOGIC = sha256(Path(__file__).read_bytes()).hexdigest()


def node(ref: str) -> bytes:
    """A `ccf1_` reference as its 32 digest bytes."""
    return bytes.fromhex(ref[5:])


def is_shared(row: Receipt) -> bool:
    """A public fact every package's judgement reads beside its own facts."""
    return row.kind == "attribution" and row.producer in SHARED_PRODUCERS


def input_node(context: Mapping[str, Any]) -> bytes | None:
    """The node of an actor context's authenticated input, if it names one."""
    key = [context.get("node"), context.get("entrypoint"), context.get("input_ref")]
    return sha256(canonical(["authenticated_input", *key])).digest() if all(key) else None


def link_nodes(row: Receipt) -> tuple[bytes, ...]:
    """The sorted, distinct nodes this fact connects; empty for a public fact."""
    extra: list[bytes] = []
    if is_shared(row):
        return ()
    if row.kind == "coverage":
        if row.producer not in INVENTORY_PRODUCERS or not row.evidence:
            return ()
        refs = [row.entity_ref, *row.evidence]
    elif row.kind not in LINKED_KINDS:
        return ()
    else:
        p = row.payload
        refs = [row.entity_ref]
        if row.kind == "usage":
            refs += [p.get("native_scope_ref"), p.get("execution_ref"), *p["covers"]]
        elif row.kind == "execution":
            refs.append(p["parent_ref"])
        elif row.kind == "attribution":
            refs += [p["target_ref"], p.get("execution_ref")]
            subcall = (p.get("context") or {}).get("subcall")
            if subcall is not None:
                refs.append(subcall["parent_scope_ref"])
            if p.get("scope") == "actor":
                extra = [n for n in [input_node(p.get("context") or {})] if n]
        elif row.kind == "human":
            refs.append(p["execution_ref"])
        else:
            refs += p["target_refs"]
    return tuple(sorted({*extra, *(node(ref) for ref in refs if ref is not None)}))
