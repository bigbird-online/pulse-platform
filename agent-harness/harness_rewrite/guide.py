"""Per-intent input guides, read with `inspect --view guide --intent NAME`.

A caller that cannot read the skill or OPERATIONS.md (a tenant, a remote node, a
tighter sandbox) learns an intent's input here. The top-level field sets below are
the sets the kernel's own `fields` check uses (`shape`), so a guide cannot drift
from validation; tests execute every example envelope in a test kernel.
Placeholders in examples are written <LIKE_THIS>.
"""

from __future__ import annotations

from typing import Any

Json = dict[str, Any]

READ = "pulse_work_item action=inspect view=guide intent={intent} (CLI: inspect --view guide --intent {intent})"
HUMAN_REF = (
    "required: top-level human_input_ref, the 64-hex reference of the human's latest message "
    "approving exactly this input (identity action returns latest_human_input_ref); never inside input"
)
NO_HUMAN = "not used: the native session itself is the source"
NO_FILES = "not used"
CREATE_FIELDS = {
    "summary": "text: one plain line of at most 120 characters, what this WI does and why, no IDs or dates",
    "why": "text: why the work is needed",
    "what": "text: what will be produced",
    "acceptance": "[text]: nonempty list of acceptance criteria, numbered AC-1, AC-2, … in order",
    "resources": '[scope]: unique logical scopes such as "repo" or "repo/docs", never absolute paths',
}
CREATE_REJECTIONS = [
    {"reason": 'A WI needs "summary"', "fix": "add summary: one plain line, at most 120 characters"},
    {"reason": "Acceptance criteria are required", "fix": "acceptance is a nonempty list of nonempty texts"},
    {"reason": "Resources must be unique logical resource paths", "fix": 'use relative scopes such as "repo", no leading /'},
]
SHAPE_REJECTIONS = [
    {"reason": "Required business input is missing: X", "fix": "add field X inside input"},
    {"reason": "Unsupported business input field: X", "fix": "remove X, or move it to the envelope if it is human_input_ref/files"},
    {"reason": "human_input_ref goes at the top level of execute, not in input", "fix": "pass human_input_ref beside input"},
]
HUMAN_REJECTION = {
    "reason": "An authenticated decision for this content is required",
    "fix": "the human must approve this exact input in this conversation; pass that message's reference as "
           "top-level human_input_ref, and change nothing in input after the approval",
}
ARTIFACT_FILES = (
    'files {name: {path: absolute file under your admitted roots, logical: "repo/<path>", version: label, '
    'access?: "team"|"private"}}; reference each in input as {"file": name}; the core captures the bytes '
    "and fills sha256/size, never write hashes yourself"
)
FILE_REJECTION = {
    "reason": "files.NAME must be {path, logical, version[, access]} strings",
    "fix": 'files is an object of rows, not a list of paths; input refers to a row as {"file": "NAME"}',
}

# Nested plan shapes; workflow.validate_graph uses these same sets.
GRAPH = {
    "required": {
        "work_item_id": "this WI's ID",
        "graph_id": "identifier, e.g. main; a revision keeps it",
        "revision": "int: 1 for adopt_plan, previous_revision + 1 for revise_plan",
        "definition_revision": "int: the WI's current definition revision (1 unless amended)",
        "objective": "text",
        "acceptance_refs": '["AC-1", …]: existing criteria this plan covers',
        "confirmation_ref": "the same 64-hex as top-level human_input_ref",
        "nodes": "[node]: 1 to 512",
        "edges": "[edge]: {from, to, condition}; [] for none",
    },
    "optional": {},
}
NODE = {
    "required": {
        "node_id": "identifier, unique in the graph",
        "kind": "work | decision | wait | verification | review | delivery",
        "purpose": "text",
        "executor_role": "work/decision/wait: producer|collaborator; verification: verifier; review: human; delivery: delivery",
        "input_refs": "[]: captured artifact references (usually empty)",
        "output_contract": '{artifacts: [identifier], outcomes: [identifier, …]} e.g. {"artifacts":["report"],"outcomes":["success","failure"]}',
        "resource_scope": "[scope]: within the WI's resources",
        "required": "bool",
        "completion_rule": '{join: "all"|"selected", success_outcomes: subset of outcomes}',
    },
    "optional": {
        "acceptance_refs": "verification nodes only: the criteria they verify",
        "deadline": "RFC3339 instant with timezone",
    },
}
EDGE = {
    "required": {"from": "node_id", "to": "node_id", "condition": 'an outcome of "from", or "*"'},
    "optional": {},
}
PLAN_EXAMPLE = {
    "work_item_id": "<WI>",
    "graph_id": "main",
    "revision": 1,
    "definition_revision": 1,
    "objective": "Produce the agreed report",
    "acceptance_refs": ["AC-1"],
    "confirmation_ref": "<HUMAN_REF>",
    "nodes": [
        {
            "node_id": "write",
            "kind": "work",
            "purpose": "Write and check the report",
            "executor_role": "producer",
            "input_refs": [],
            "output_contract": {"artifacts": ["report"], "outcomes": ["success", "failure"]},
            "resource_scope": ["repo"],
            "required": True,
            "completion_rule": {"join": "all", "success_outcomes": ["success"]},
        }
    ],
    "edges": [],
}
PLAN_NESTED = {"graph": GRAPH, "node": NODE, "edge": EDGE}
PLAN_REJECTIONS = [
    HUMAN_REJECTION,
    {"reason": "Plan confirmation must refer to the admitted human input",
     "fix": "graph.confirmation_ref equals top-level human_input_ref"},
    {"reason": "Plan refers to a different WI, definition or revision",
     "fix": "graph.work_item_id is this WI, revision is the next plan revision, definition_revision the current one"},
    {"reason": "Plan exceeds the declared resource scope", "fix": "each node resource_scope lies within the WI's resources"},
    {"reason": "Plan must reference existing acceptance criteria", "fix": "acceptance_refs are AC-n of this WI"},
    {"reason": "Expected nonempty text",
     "fix": 'plan_text is an inline string; plan_text and graph never go through files or {"file": name}'},
]

GUIDES: dict[str, Json] = {
    "create_lightweight": {
        "purpose": "Create a lightweight WI: start_work {} then record_work, complete_lightweight.",
        "work_item_id": "omit: the result returns the new WI ID",
        "human_input_ref": NO_HUMAN, "files": NO_FILES,
        "required": CREATE_FIELDS, "optional": {},
        "host_filled": {"owner": "omit: the CLI fills your own actor"},
        "example": {"intent": "create_lightweight", "input": {
            "summary": "Fix the report export so tenants can read it",
            "why": "Tenants cannot open the exported report", "what": "A working export and its test",
            "acceptance": ["The export opens for a tenant"], "resources": ["repo"]}},
        "rejections": CREATE_REJECTIONS,
    },
    "create_draft": {
        "purpose": "Create a standard WI draft: adopt_plan (human-confirmed), then start_work {graph_revision: 1}.",
        "work_item_id": "omit: the result returns the new WI ID",
        "human_input_ref": NO_HUMAN, "files": NO_FILES,
        "required": CREATE_FIELDS, "optional": {},
        "host_filled": {"owner": "omit: the CLI fills your own actor"},
        "example": {"intent": "create_draft", "input": {
            "summary": "Write the quarterly report the team agreed on",
            "why": "The team needs the agreed report", "what": "The report",
            "acceptance": ["The report is published"], "resources": ["repo"]}},
        "rejections": CREATE_REJECTIONS,
    },
    "create_child": {
        "purpose": "Create a lightweight child of the WI this session holds; it is active at once (no start_work).",
        "work_item_id": "omit: the result returns the child WI ID; dispatch a sub-agent whose prompt starts with [WI: <child>]",
        "human_input_ref": NO_HUMAN, "files": NO_FILES,
        "required": {"parent": "the active WI this session holds (not itself a child)", **CREATE_FIELDS},
        "optional": {},
        "example": {"intent": "create_child", "input": {
            "parent": "<PARENT_WI>", "summary": "Add the missing export test",
            "why": "The export has no test", "what": "One regression test",
            "acceptance": ["The test fails before the fix"], "resources": ["repo"]}},
        "rejections": CREATE_REJECTIONS + [
            {"reason": "Unsupported business input field: owner", "fix": "a child takes its parent's owner; omit owner"},
            {"reason": "one level", "fix": "a child cannot have children; create it under the parent WI"}],
    },
    "start_work": {
        "purpose": "The owner starts a draft and becomes its holder.",
        "work_item_id": "required: the draft WI",
        "human_input_ref": NO_HUMAN, "files": NO_FILES,
        "required": {}, "optional": {"graph_revision": "int: standard WI only, the adopted plan revision (1)"},
        "example": {"intent": "start_work", "work_item_id": "<WI>", "input": {}},
        "rejections": [
            {"reason": "Standard work requires the current adopted plan",
             "fix": "adopt_plan first, then pass graph_revision: <adopted revision>"},
            {"reason": "Only the designated owner may start a draft", "fix": "start it from the owner Agent"}],
    },
    "adopt_plan": {
        "purpose": "Adopt the first human-confirmed plan of a standard WI.",
        "work_item_id": "required: the standard WI (draft or active)",
        "human_input_ref": HUMAN_REF + "; graph.confirmation_ref is the same value",
        "files": 'not used: plan_text and graph go inline, never as files or {"file": name}',
        "required": {"plan_text": "text: the plan itself as text", "graph": "object: see nested.graph"},
        "optional": {"resources": "[scope]: only an original historical WI without declared scope"},
        "nested": PLAN_NESTED,
        "example": {"intent": "adopt_plan", "work_item_id": "<WI>", "human_input_ref": "<HUMAN_REF>",
                    "input": {"plan_text": "Write the report, check it, publish it.", "graph": PLAN_EXAMPLE}},
        "rejections": PLAN_REJECTIONS + [
            {"reason": "Use an explicit plan revision to replace an adopted plan", "fix": "use revise_plan"}],
    },
    "revise_plan": {
        "purpose": "Replace the adopted plan with a human-confirmed revision.",
        "work_item_id": "required: the standard WI",
        "human_input_ref": HUMAN_REF + "; graph.confirmation_ref is the same value",
        "files": 'not used: plan_text and graph go inline, never as files or {"file": name}',
        "required": {
            "plan_text": "text", "graph": "object: see nested.graph, same graph_id, revision previous_revision + 1",
            "previous_revision": "int: the current plan revision",
            "reuse_results": "[node_id]: completed nodes whose results stay valid; [] for none",
            "reason": "text"},
        "optional": {},
        "nested": PLAN_NESTED,
        "example": {"intent": "revise_plan", "work_item_id": "<WI>", "human_input_ref": "<HUMAN_REF>",
                    "input": {"plan_text": "Write the report again with the new data.",
                              "graph": {**PLAN_EXAMPLE, "revision": 2},
                              "previous_revision": 1, "reuse_results": [], "reason": "New data arrived"}},
        "rejections": PLAN_REJECTIONS + [
            {"reason": "Previous plan revision is no longer current", "fix": "read view plan for the current revision"},
            {"reason": "Plan revision must preserve graph identity", "fix": "keep graph_id"}],
    },
    "checkpoint": {
        "purpose": "Record progress without result files.",
        "work_item_id": "required: a WI you hold (or the same Agent's WI)",
        "human_input_ref": NO_HUMAN, "files": NO_FILES,
        "required": {"summary": "text: known progress and remaining risk", "resources": "[scope] within the WI's resources"},
        "optional": {},
        "example": {"intent": "checkpoint", "work_item_id": "<WI>",
                    "input": {"summary": "Export fixed; test still to write", "resources": ["repo"]}},
        "rejections": [{"reason": "No current execution grant for this scope", "fix": "resources lie within your grant; read capability local_work"}],
    },
    "request_rollover": {
        "purpose": "Move the WI you hold to a fresh session of yourself (context nearly full, many "
                   "compactions). After this turn ends the Host fences this session, starts the "
                   "successor and hands it the WI; execution stays with you. End the turn at once.",
        "work_item_id": "required: the WI this session holds (not a child)",
        "human_input_ref": NO_HUMAN, "files": NO_FILES,
        "required": {"summary": "text: one line for the successor; the checkpoint holds the frontier"},
        "optional": {},
        "example": {"intent": "request_rollover", "work_item_id": "<WI>",
                    "input": {"summary": "Continue from the latest checkpoint"}},
        "rejections": [
            {"reason": "latest record must be a checkpoint by this session",
             "fix": "checkpoint {summary: done, pending, next action, resources} first, record nothing after it"},
            {"reason": "attempt_started or unknown", "fix": "resume the WI until its effects settle"},
            {"reason": "Current execution holder is required", "fix": "only the holder session rolls over"},
            {"reason": "Relay conversation", "fix": "outside Relay the human uses /new and /wi continue"}],
    },
    "request_switch": {
        "purpose": "Move this Relay conversation to another WI of yours, as /wi switch does, without a "
                   "human command. After this turn ends the Host leaves this session (the WI stays "
                   "held here), opens the target's original session or a new one and hands the target "
                   "to you there. End the turn at once. A target that is this WI or its child answers "
                   "no_switch: keep working here.",
        "work_item_id": "required: the WI this session holds (not a child)",
        "human_input_ref": NO_HUMAN, "files": NO_FILES,
        "required": {"target": "the WI to switch to: a root WI you hold in another session, or your own draft",
                     "summary": "text: one line on why; the checkpoint holds the frontier"},
        "optional": {},
        "example": {"intent": "request_switch", "work_item_id": "<WI>",
                    "input": {"target": "<other WI>", "summary": "Review returned; continue the other WI"}},
        "rejections": [
            {"reason": "latest record must be a checkpoint by this session",
             "fix": "checkpoint {summary: done, pending, next action, resources} first, record nothing after it"},
            {"reason": "attempt_started or unknown", "fix": "resume the WI until its effects settle"},
            {"reason": "Current execution holder is required", "fix": "only the holder session switches"},
            {"reason": "is a child of", "fix": "switch to its parent WI"},
            {"reason": "Another Agent", "fix": "another Agent's work is offered by collaboration, not switched to"},
            {"reason": "No session holds", "fix": "ask the human for /wi continue <target>"},
            {"reason": "only active work or your draft", "fix": "ended or stopped work needs the human's reopen"},
            {"reason": "Relay conversation", "fix": "outside Relay the human uses /new and /wi continue"}],
    },
    "record_work": {
        "purpose": "Record a work result with its files; complete_lightweight needs these record_ids.",
        "work_item_id": "required: a WI you hold (or the same Agent's WI)",
        "human_input_ref": NO_HUMAN, "files": "required: " + ARTIFACT_FILES,
        "required": {"summary": "text", "resources": "[scope] within the WI's resources",
                     "artifacts": '[{"file": name}]: nonempty, each naming a files row'},
        "optional": {},
        "example": {"intent": "record_work", "work_item_id": "<WI>",
                    "input": {"summary": "Fixed the export and added its test", "resources": ["repo"],
                              "artifacts": [{"file": "result"}]},
                    "files": {"result": {"path": "<ABSOLUTE_FILE>", "logical": "repo/evidence/result.md", "version": "v1"}}},
        "rejections": [
            FILE_REJECTION,
            {"reason": "Work results require versioned artifact references", "fix": "artifacts is a nonempty list"},
            {"reason": "Work record requires matching captured artifact bytes",
             "fix": 'use {"file": name} placeholders with files, never hand-written references'}],
    },
    "complete_lightweight": {
        "purpose": "Close a lightweight WI after the human accepts it.",
        "work_item_id": "required: the lightweight WI you hold",
        "human_input_ref": HUMAN_REF + " (the acceptance message)", "files": NO_FILES,
        "required": {"record_ids": "[record_id]: exactly every WorkRecorded record_id (not checkpoints)",
                     "acceptance_refs": '["AC-1", … "AC-n"]: every criterion, in definition order'},
        "optional": {},
        "example": {"intent": "complete_lightweight", "work_item_id": "<WI>", "human_input_ref": "<HUMAN_REF>",
                    "input": {"record_ids": ["<RECORD_ID>"], "acceptance_refs": ["AC-1"]}},
        "rejections": [HUMAN_REJECTION, {"reason": "Fixed work records and all criteria are required",
                                         "fix": "the reason lists the exact record_ids and acceptance_refs"}],
    },
    "submit_pull_request": {
        "purpose": "Register an open PR to main for review and kernel merge; never merge it yourself.",
        "work_item_id": "required: a WI your Agent holds",
        "human_input_ref": NO_HUMAN, "files": NO_FILES,
        "required": {"repository": "owner/name, exact case", "number": "int: PR number",
                     "head_sha": "40-hex head commit, made by this session or a sub-agent it spawned"},
        "optional": {"immediately": "bool: dispatch review now (default false)"},
        "example": {"intent": "submit_pull_request", "work_item_id": "<WI>",
                    "input": {"repository": "<OWNER/REPO>", "number": 7, "head_sha": "<HEAD_SHA>", "immediately": True}},
        "rejections": [
            {"reason": "Pull request number must be a positive integer", "fix": "number is an integer, not a string"},
            {"reason": "Repository is not admitted for kernel merge", "fix": "use the exact admitted repository name"},
            {"reason": "The pull request head or base differs on GitHub", "fix": "push first; head_sha is the PR head, base main"}],
    },
    "offer_collaboration": {
        "purpose": "Ask another Agent (or one exact session) to do bounded work on this WI.",
        "work_item_id": "required: the WI you hold, which the contract serves",
        "human_input_ref": NO_HUMAN, "files": NO_FILES,
        "required": {
            "kind": "request | delegate | responsibility",
            "recipients": "[{principal: {actor} or {actor, session, surface}, resources: [scope], target_work_item_id?: the recipient's own WI}]",
            "purpose": "text", "required": "bool: whether completion waits for it",
            "main_only": "bool: true (default choice) sends it to the recipient's main session"},
        "optional": {"deadline": "RFC3339 instant with timezone"},
        "example": {"intent": "offer_collaboration", "work_item_id": "<WI>", "input": {
            "kind": "request", "purpose": "Review the export fix", "required": False, "main_only": True,
            "recipients": [{"principal": {"actor": "<AGENT>"}, "resources": ["repo"],
                            "target_work_item_id": "<TARGET_WI>"}]}},
        "rejections": [
            {"reason": "Declare main_only", "fix": "add main_only: true or false"},
            {"reason": "Contract exceeds current resource scope", "fix": "recipient resources lie within this WI's resources"},
            {"reason": "target_work_item_id", "fix": "a main_only Agent recipient names the WI it handles the contract on; the refusal lists candidates"}],
    },
    "respond_collaboration": {
        "purpose": "Answer a contract offered to you: accept or reject, then return or complete.",
        "work_item_id": "required: the offering WI (the one carrying the contract)",
        "human_input_ref": NO_HUMAN, "files": "complete only: " + ARTIFACT_FILES,
        "required": {"contract_id": "the offered contract_id", "decision": "accept | reject | return | complete",
                     "reason": "text"},
        "optional": {"evidence": '[{"file": name}]: complete only, the delivered result files'},
        "example": {"intent": "respond_collaboration", "work_item_id": "<OFFERING_WI>",
                    "input": {"contract_id": "<CONTRACT_ID>", "decision": "accept", "reason": "I will review it today"}},
        "rejections": [
            {"reason": "Only an accepted responsibility may be returned/completed", "fix": "answer accept first"},
            {"reason": "Evidence belongs to the actual completion decision", "fix": "evidence only with decision complete"},
            FILE_REJECTION],
    },
    "review_child": {
        "purpose": "The parent's holder approves (closes) or returns a child WI.",
        "work_item_id": "required: the child WI",
        "human_input_ref": NO_HUMAN, "files": NO_FILES,
        "required": {"decision": "approved | returned", "reason": "text"},
        "optional": {},
        "example": {"intent": "review_child", "work_item_id": "<CHILD_WI>",
                    "input": {"decision": "returned", "reason": "The test does not cover the tenant case"}},
        "rejections": [{"reason": "Only a child work item is reviewed", "fix": "work_item_id is the child, not the parent"}],
    },
    "allocate_workspaces": {
        "purpose": "Ask the core for managed Git workspaces on the WI you hold.",
        "work_item_id": "required: a WI you hold",
        "human_input_ref": NO_HUMAN, "files": NO_FILES,
        "required": {"workspaces": "[{repo_id, base_commit: 40/64-hex, resources: [scope], sparse: [relative path] or [] for full, purpose}]"},
        "optional": {},
        "example": {"intent": "allocate_workspaces", "work_item_id": "<WI>", "input": {"workspaces": [{
            "repo_id": "<REPO_ID>", "base_commit": "<BASE_COMMIT>", "resources": ["repo"],
            "sparse": ["src", ".pre-commit-config.yaml"], "purpose": "Fix the export"}]}},
        "rejections": [
            {"reason": "Repository is not admitted by this Host", "fix": "use a repo_id from inspect view=policy"},
            {"reason": "Workspace requires an exact commit identity", "fix": "base_commit is a full 40-hex sha"},
            {"reason": "Sparse paths must be literal relative paths", "fix": "no globs, no leading /"}],
    },
    "grant_access": {
        "purpose": "Open host paths to this session (or, standing, to your Agent read-only).",
        "work_item_id": "required: the WI you hold",
        "human_input_ref": HUMAN_REF + "; a standing read of a family workspace needs none",
        "files": NO_FILES,
        "required": {"paths": "[absolute normalized path]: 1 to 32", "mode": "read | write", "reason": "text"},
        "optional": {"standing": "bool: a read-only grant bound to your Agent"},
        "example": {"intent": "grant_access", "work_item_id": "<WI>", "human_input_ref": "<HUMAN_REF>",
                    "input": {"paths": ["/<ABSOLUTE_DIR>"], "mode": "read", "reason": "Read the shared notes"}},
        "rejections": [
            HUMAN_REJECTION,
            {"reason": "Access paths must be normalized absolute host paths", "fix": "absolute, no trailing / or .."},
            {"reason": "A standing grant is read-only", "fix": "standing only with mode read"}],
    },
}


def shape(intent: str) -> tuple[set[str], set[str]]:
    """The (required, optional) top-level input fields the kernel checks for `intent`."""
    guide = GUIDES[intent]
    return set(guide["required"]) | set(guide.get("host_filled", {})), set(guide["optional"])


def nested(name: str) -> tuple[set[str], set[str]]:
    part = PLAN_NESTED[name]
    return set(part["required"]), set(part["optional"])


def signature(intent: str) -> str:
    """One line: the input object, then the envelope fields that apply."""
    guide = GUIDES[intent]
    names = [*guide["required"], *(name + "?" for name in guide["optional"])]
    head = {key: str(guide[key]).split(":")[0] for key in ("work_item_id", "human_input_ref", "files")}
    return "input {" + ", ".join(names) + "}; " + "; ".join(f"{k}: {v}" for k, v in head.items())


def read(intent: str | None) -> Json:
    """The guide view: one intent in full, or the index of covered intents."""
    common = {
        "envelope": "execute takes only request_id, intent, work_item_id, input, files, sources and "
                    "human_input_ref; business fields go inside input",
        "rejections": SHAPE_REJECTIONS,
    }
    if intent is None:
        return {"schema": "harness.intent_guide/v1", "status": "read_only", **common,
                "result": {"items": [{"intent": name, "input": signature(name)} for name in sorted(GUIDES)]}}
    if intent not in GUIDES:
        from .model import Rejected

        raise Rejected("invalid_input", "No guide for this intent; guides: " + ", ".join(sorted(GUIDES)))
    return {"schema": "harness.intent_guide/v1", "status": "read_only", "intent": intent,
            **common, "guide": GUIDES[intent], "input": signature(intent)}


SHAPE_CODES = frozenset({"invalid_input", "source_invalid"})


def annotate(response: Json, intent: object) -> Json:
    """A shape refusal names the right input and where its full guide is."""
    error = response.get("error")
    if response.get("status") != "rejected" or not isinstance(error, dict) or error.get("code") not in SHAPE_CODES:
        return response
    if isinstance(intent, str) and intent in GUIDES:
        error["input_guide"] = {"intent": intent, "input": signature(intent), "read": READ.format(intent=intent)}
    else:
        error["input_guide"] = {"read": "pulse_work_item action=inspect view=guide lists the intents with guides"}
    return response
