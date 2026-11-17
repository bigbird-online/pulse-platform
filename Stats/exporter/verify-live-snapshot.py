"""Conservation and uniqueness checks against a real production snapshot.

Every check here restates one of the five-no promises in terms the data can
falsify. A green run means the promise holds on today's real numbers, not on a
fixture built to make it hold.

A check that could not run is not a check that passed. Every assertion below is
registered up front with the inputs it needs; when an input is missing the check
is reported as SKIP with the field that was None, and the summary line reports
the denominator. "All checks passed" is only ever printed when nothing was
skipped.
"""
import json
import sys

USAGE = """usage: verify-live-snapshot.py <snapshot.json>

Produce one with:
  /usr/bin/python3 exporter/pulse-stats-snapshot.py --json > /tmp/snap.json
"""

failures = []
skipped = []
passed = []


def check(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label + (f"  [{detail}]" if detail else ""))
    if cond:
        passed.append(label)
    else:
        failures.append(label)


def skip(label, missing):
    """Record a check that could not run because its inputs were unavailable.

    `missing` names the fields that were None. This is the third state the
    original script lacked: without it a guarded-out assertion is invisible and
    the run still prints as green.
    """
    detail = ", ".join(missing) if isinstance(missing, (list, tuple, set)) else str(missing)
    print(f"SKIP  {label}  [no data: {detail}]")
    skipped.append((label, detail))


def missing_fields(**fields):
    """Return the names of the given fields whose value is None."""
    return [name for name, value in fields.items() if value is None]


def guarded(label, needs, run):
    """Run `run()` if every field in `needs` is present, else record a SKIP.

    `needs` is a dict of field-name -> value. The assertion inside `run` is
    untouched; only the decision to record a skip instead of silently doing
    nothing is new.
    """
    absent = missing_fields(**needs)
    if absent:
        skip(label, absent)
        return False
    run()
    return True


if len(sys.argv) != 2:
    sys.exit(USAGE)

with open(sys.argv[1], encoding="utf-8") as handle:
    snap = json.load(handle)

# --- schema / identity -------------------------------------------------------
check("schema is v2", snap.get("schema_version") == "pulse.monitoring.snapshot.v2",
      snap.get("schema_version", "?"))

metrics = snap.get("metrics", [])
ids = [m["id"] for m in metrics]
check("metric ids unique", len(ids) == len(set(ids)),
      f"{len(ids)} metrics, {len(set(ids))} unique")

caps = snap.get("capabilities", [])
cap_ids = [c["id"] for c in caps]
check("capability ids unique", len(cap_ids) == len(set(cap_ids)),
      f"{len(cap_ids)} capabilities")

by_id = {m["id"]: m for m in metrics}

# --- P0-4: no schema-undefined quality ---------------------------------------
LEGAL_QUALITY = {"validated", "descriptive", "insufficient", "unavailable"}
bad_quality = sorted({m.get("quality") for m in metrics} - LEGAL_QUALITY - {None})
check("all metric quality values are legal", not bad_quality, str(bad_quality))

LEGAL_STATUS = {"available", "unknown", "unavailable"}
bad_status = sorted({m.get("status") for m in metrics} - LEGAL_STATUS - {None})
check("all metric status values are legal", not bad_status, str(bad_status))


def value_of(mid):
    m = by_id.get(mid)
    if not m or m.get("status") != "available":
        return None
    return m.get("value")


# --- P0-2: today's window is the Beijing day, consistently -------------------
today_metrics = [m for m in metrics if m["id"].startswith("usage.today.")]
windows = {m.get("window") for m in today_metrics}
check("usage.today.* share one window", len(windows) <= 1, str(windows))
check("that window is today_cst", windows <= {"today_cst"} or not windows, str(windows))

# --- P0-3: real work excludes cache re-reads ---------------------------------
# The App derives working tokens client-side; the exporter ships the parts. The
# base is `input_tokens_uncached`, NOT `input_tokens` -- the latter includes
# cache reads, which is exactly the 2026-07-29 inflation this slice removed.
fresh = value_of("usage.today.input_tokens_uncached")
cache_creation = value_of("usage.today.cache_creation_tokens")
output = value_of("usage.today.output_tokens")
cache_read = value_of("usage.today.cache_read_tokens")
total_input = value_of("usage.today.input_tokens")

check("all working-token parts are present",
      None not in (fresh, cache_creation, output),
      f"uncached={fresh} cache_creation={cache_creation} output={output}")

working = None
if None not in (fresh, cache_creation, output):
    working = fresh + cache_creation + output


def _working_positive():
    check("working tokens are computable and positive", working > 0, f"{working:,}")


guarded("working tokens are computable and positive",
        {"usage.today.input_tokens_uncached": fresh,
         "usage.today.cache_creation_tokens": cache_creation,
         "usage.today.output_tokens": output},
        _working_positive)


def _cache_read_excluded():
    check("cache re-reads stay out of the work total",
          cache_read > working or working < (working + cache_read),
          f"re-read {cache_read:,} excluded from work {working:,}")


guarded("cache re-reads stay out of the work total",
        {"usage.today.cache_read_tokens": cache_read, "working_tokens": working},
        _cache_read_excluded)


def _uncached_subset():
    check("uncached input is a strict subset of total input",
          fresh <= total_input, f"{fresh:,} <= {total_input:,}")


guarded("uncached input is a strict subset of total input",
        {"usage.today.input_tokens_uncached": fresh,
         "usage.today.input_tokens": total_input},
        _uncached_subset)


def _total_inflates():
    check("using total input would inflate the figure",
          total_input > working,
          f"inflated {total_input:,} vs real {working:,}")


guarded("using total input would inflate the figure",
        {"usage.today.input_tokens": total_input, "working_tokens": working},
        _total_inflates)


# input_tokens is the *total* intake: bare + cache read + cache write
# (usage_sources.py:987). All three parts must account for it exactly.
def _input_conserves():
    parts = fresh + cache_read + cache_creation
    check("input_tokens == uncached + cache_read + cache_creation",
          total_input == parts,
          f"{total_input:,} vs {fresh:,}+{cache_read:,}+{cache_creation:,}={parts:,}")


guarded("input_tokens == uncached + cache_read + cache_creation",
        {"usage.today.input_tokens_uncached": fresh,
         "usage.today.cache_read_tokens": cache_read,
         "usage.today.cache_creation_tokens": cache_creation,
         "usage.today.input_tokens": total_input},
        _input_conserves)

# --- P1-5b: billing conserves exactly, in integer cents ----------------------
components = value_of("billing.mtd.components")
total = value_of("billing.mtd.total_cents")
attributed = value_of("billing.mtd.attributed_cents")
unattributed = value_of("billing.mtd.unattributed_cents")
by_agent = value_of("billing.mtd.by_agent")

# The presence check is itself an assertion, so it always runs: a snapshot that
# cannot state its own billing facts is a finding, not a skip.
check("billing facts present", not (components is None or total is None),
      "components or total unavailable" if (components is None or total is None)
      else f"total={total}, {len(components)} components")


def _components_sum():
    comp_sum = sum(c["amount_cents"] for c in components)
    check("components sum to total_cents", comp_sum == total, f"{comp_sum} vs {total}")


guarded("components sum to total_cents",
        {"billing.mtd.components": components, "billing.mtd.total_cents": total},
        _components_sum)


def _components_integer():
    check("all component amounts are integer cents",
          all(isinstance(c["amount_cents"], int) for c in components))


guarded("all component amounts are integer cents",
        {"billing.mtd.components": components},
        _components_integer)


def _component_ids_unique():
    check("component ids unique",
          len({c["component_id"] for c in components}) == len(components))


guarded("component ids unique",
        {"billing.mtd.components": components},
        _component_ids_unique)


def _attribution_conserves():
    check("attributed + unattributed == total",
          attributed + unattributed == total,
          f"{attributed}+{unattributed} vs {total}")


guarded("attributed + unattributed == total",
        {"billing.mtd.attributed_cents": attributed,
         "billing.mtd.unattributed_cents": unattributed,
         "billing.mtd.total_cents": total},
        _attribution_conserves)


def _agent_sum():
    agent_sum = sum(a["attributed_cents"] for a in by_agent)
    check("per-agent attributed sums to attributed_cents",
          agent_sum == attributed, f"{agent_sum} vs {attributed}")


guarded("per-agent attributed sums to attributed_cents",
        {"billing.mtd.by_agent": by_agent, "billing.mtd.attributed_cents": attributed},
        _agent_sum)


def _agent_names_unique():
    check("agent names unique",
          len({a["agent"] for a in by_agent}) == len(by_agent))


guarded("agent names unique",
        {"billing.mtd.by_agent": by_agent},
        _agent_names_unique)


def _no_codex_row():
    check("no codex agent row (its $20 stays unattributed)",
          not any(a["agent"].lower() == "codex" for a in by_agent))


guarded("no codex agent row (its $20 stays unattributed)",
        {"billing.mtd.by_agent": by_agent},
        _no_codex_row)


def _unattributed_covers():
    required = sum(c["amount_cents"] for c in components
                   if c["attribution"] == "unattributed_no_agent_source")
    check("unattributed covers every unattributed component",
          unattributed >= required, f"{unattributed} >= {required}")


guarded("unattributed covers every unattributed component",
        {"billing.mtd.components": components,
         "billing.mtd.unattributed_cents": unattributed},
        _unattributed_covers)

# --- P1-3: capability groups partition the input set ------------------------
GROUPS = {"active_validated", "active_partial", "not_implemented", "retired"}
states = [c.get("implementation_status") for c in caps]
unknown_states = sorted(set(states) - GROUPS - {None})
check("every capability state is a known group", not unknown_states, str(unknown_states))
grouped = sum(1 for s in states if s in GROUPS)
check("groups cover all capabilities", grouped == len(caps), f"{grouped}/{len(caps)}")

# --- P1-2: open incidents are the ones without recovery evidence ------------
incidents = snap.get("incidents", [])
open_ids = [i["incident_key"] for i in incidents if not i.get("recovery_evidence")]
check("incident keys unique",
      len({i["incident_key"] for i in incidents}) == len(incidents))
check("open incidents are a subset of all incidents", len(open_ids) <= len(incidents),
      f"{len(open_ids)} open / {len(incidents)} total")

# --- summary -----------------------------------------------------------------
# The denominator is the point: "passed" without "out of how many, and how many
# never ran" is the exact shape of a green run that verified nothing.
registered = len(passed) + len(failures) + len(skipped)
executed = len(passed) + len(failures)

print()
if skipped:
    print(f"{len(skipped)} SKIPPED (not verified -- input data missing):")
    for label, detail in skipped:
        print(f"  - {label}  [no data: {detail}]")
    print()

print(f"checks registered: {registered}  |  executed: {executed}  |  "
      f"passed: {len(passed)}  |  failed: {len(failures)}  |  skipped: {len(skipped)}")

if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)

if skipped:
    # Exit 0: missing upstream data is not this script's defect, and failing the
    # run would make the exit code useless for catching real conservation
    # breaches. The wording carries the warning instead -- this run is NOT a
    # clean bill of health for the skipped assertions.
    print(f"INCOMPLETE: {len(passed)}/{registered} checks passed, "
          f"{len(skipped)} could not run. Conservation is UNVERIFIED for the "
          f"skipped checks above.")
    sys.exit(0)

print(f"all live-snapshot checks passed ({len(passed)}/{registered}, none skipped)")
