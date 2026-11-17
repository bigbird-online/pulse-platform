"""Deterministic, disposable HTML project page for one WorkItem revision.

The page is an output only: it is rendered from a kernel response with no model
call, and no decision reads it back. The sidecar `<wi>.state.json` keeps the
last rendered snapshot so the next revision can show what changed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import html
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import urlsplit

from .model import Json, identity

TEMPLATE = "project_page.html"
SCHEMA = "harness.project-page/v1"
RECORD_LIMIT = 30
NEXT_LIMIT = 3
# Agent-written narrative (record_project_narrative) renders right after this marker.
NARRATIVE_SLOT = "<!-- slot:narrative -->"
URL = re.compile(r"https?://[^\s<>\"']+")
UTC8 = timezone(timedelta(hours=8))
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# Sibling pages are named <wi>.html in the same directory.
PAGE_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}")
SLOT = re.compile(r"\{\{(title|body|state)\}\}")
# These intents are owed by a particular caller or already shown as PR/node/collaboration steps.
COVERED_INTENTS = frozenset(
    {
        "wait",
        "inspect",
        "claim_task",
        "respond_collaboration",
        "settle_collaboration",
    }
)
# Covered only when a pull request step below already names the action.
PR_INTENTS = frozenset(
    {"record_pull_request_review", "confirm_pull_request_merge", "retry_effect",
     "submit_pull_request"}
)
STAGE_PILL = {
    "finished": "done",
    "completed": "done",
    "merged": "done",
    "verified": "done",
    "running": "now",
    "awaiting_review": "now",
    "merge_pending": "now",
    "ready": "next",
    "waiting": "next",
    "failed": "warn",
    "checks_failed": "warn",
    "merge_failed": "warn",
    "human_confirmation_required": "warn",
    "changes_requested": "warn",
}


def template() -> str:
    # importlib.resources reads package data from a checkout or the zipapp bundle.
    return files("harness_rewrite").joinpath(TEMPLATE).read_text(encoding="utf-8")


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def clip(value: object, limit: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def link(url: str, text: str) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:  # e.g. a malformed IPv6 host: show the text, never fail the page.
        return esc(text)
    if parts.scheme not in {"https", "http"} or not parts.netloc:
        return esc(text)
    return f'<a href="{esc(url)}" rel="noopener noreferrer">{esc(text)}</a>'


def page_link(wi: object) -> str:
    """Another WorkItem's page beside this one; an unexpected identity stays text."""
    text = "" if wi is None else str(wi)
    if not PAGE_NAME.fullmatch(text):
        return f"<code>{esc(text)}</code>"
    return f'<a href="{esc(text)}.html"><code>{esc(text)}</code></a>'


def pill(status: object) -> str:
    value = str(status)
    kind = STAGE_PILL.get(value, "")
    return f'<span class="pill {kind}">{esc(value)}</span>' if kind else (
        f'<span class="pill">{esc(value)}</span>'
    )


def local_time(value: str) -> str:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise ValueError("Timezone required")
    except (ValueError, AttributeError):
        return str(value)
    return moment.astimezone(UTC8).strftime("%Y-%m-%d %H:%M UTC+8")


def table(header: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in header)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f'<div class="table-scroll"><table><tr>{head}</tr>{body}</table></div>'


def node_outcomes(view: Json) -> dict[str, tuple[str, int]]:
    """Latest settled outcome and attempt count per node, from public facts."""
    flow = view["workflow"]
    outcomes: dict[str, tuple[str, int]] = {}
    counts: dict[str, int] = {}
    for attempt in flow.get("attempts") or []:
        node_id = attempt["node_id"]
        counts[node_id] = counts.get(node_id, 0) + 1
        result = attempt.get("result")
        if attempt.get("status") == "finished" and isinstance(result, dict):
            outcomes[node_id] = (str(result.get("outcome")), counts[node_id])
    for node_id, value in (flow.get("control_results") or {}).items():
        if isinstance(value, dict):
            outcomes[node_id] = (str(value.get("outcome")), counts.get(node_id, 0))
    return {
        k: (outcomes.get(k, ("", 0))[0], counts.get(k, 0))
        for k in set(outcomes) | set(counts)
    }


def node_stages(view: Json) -> dict[str, str]:
    flow = view["workflow"]
    graph = flow.get("graph")
    if not graph:
        return {}
    outcomes = node_outcomes(view)
    stages: dict[str, str] = {}
    for node in graph["nodes"]:
        stage = flow["node_stages"].get(node["node_id"], "waiting")
        outcome = outcomes.get(node["node_id"], ("", 0))[0]
        if stage == "finished" and outcome not in node["completion_rule"]["success_outcomes"]:
            stage = "failed"
        stages[node["node_id"]] = stage
    return stages


def page_state(response: Json, as_of: str) -> Json:
    view = response["result"]["current_observation"]
    return {
        "schema": SCHEMA,
        "work_item_id": response["work_item"]["id"],
        "revision": response["work_item"]["local_revision"],
        "as_of": as_of,
        "business_progress": view["business_progress"],
        "execution_progress": view["workflow"]["execution_progress"],
        "nodes": node_stages(view),
        "pull_requests": {pr["key"]: pr["status"] for pr in view.get("pull_requests") or []},
        "children": {c["id"]: c["business_progress"] for c in view.get("children") or []},
        "narrative": narrative_marker(view, as_of),
    }


def narrative_marker(view: Json, as_of: str) -> Json | None:
    """What the page shows about the narrative's age, to re-render when it expires."""
    staleness = narrative_staleness(view, as_of)
    if staleness is None:
        return None
    return {"recorded_at": view["narrative"].get("recorded_at"), "stale": any(staleness)}


def expired_unmarked(current: Json, as_of: str) -> bool:
    """A page whose shown narrative has since passed STALE_AFTER without being marked."""
    from .narrative import STALE_AFTER

    marker = current.get("narrative")
    if not isinstance(marker, dict) or marker.get("stale") is not False:
        return False
    try:
        return datetime.fromisoformat(as_of.replace("Z", "+00:00")) - datetime.fromisoformat(
            str(marker["recorded_at"]).replace("Z", "+00:00")) > STALE_AFTER
    except (KeyError, ValueError, TypeError):
        return True


def changes(baseline: Json | None, current: Json) -> list[str]:
    if baseline is None:
        return []
    lines = []
    for key, label in (("business_progress", "业务进度"), ("execution_progress", "执行进度")):
        if baseline.get(key) != current[key]:
            lines.append(f"{label}：{baseline.get(key)} → {current[key]}")
    for key, label in (("nodes", "节点"), ("pull_requests", "PR"), ("children", "子 WI")):
        before, after = baseline.get(key) or {}, current.get(key) or {}
        for name in sorted(set(before) | set(after)):
            if before.get(name) != after.get(name):
                lines.append(
                    f"{label} {name}：{before.get(name, '新增')} → {after.get(name, '移除')}"
                )
    return lines


def acceptance(view: Json) -> list[tuple[str, str, str]]:
    """Current acceptance only: still-valid control results and the active candidate.

    Rework drops affected control results and the active candidate, so an older
    candidate's passed checks never vouch for changed work.
    """
    status: dict[str, str] = {}
    graph = view["workflow"].get("graph") or {}
    nodes = {n["node_id"]: n for n in graph.get("nodes", [])}
    for node_id, value in (view["workflow"].get("control_results") or {}).items():
        node = nodes.get(node_id, {})
        if node.get("kind") == "verification" and (value or {}).get("outcome") == "accepted":
            for ref in node.get("acceptance_refs") or graph.get("acceptance_refs") or []:
                status[ref] = "verified"
    candidates = view.get("candidates") or {}
    active = candidates.get("active")
    for item in candidates.get("items") or []:
        if active is None or item.get("id") != active:
            continue
        for check in ((item.get("verification") or {}).get("checks")) or []:
            verdict = check.get("verdict")
            status[check.get("criterion")] = {"passed": "verified", "failed": "failed"}.get(
                verdict, "unverified"
            )
    return [
        (f"AC-{i + 1}", str(text), status.get(f"AC-{i + 1}", "unverified"))
        for i, text in enumerate(view["definition"].get("acceptance") or [])
    ]


def next_steps(response: Json) -> list[tuple[str, str, str]]:
    """Who · action · why, derived from kernel facts only."""
    view = response["result"]["current_observation"]
    owner = view["definition"].get("owner") or "owner"
    steps: list[tuple[str, str, str]] = []
    for pr in view.get("pull_requests") or []:
        name = f"PR {pr['repository']}#{pr['number']}"
        last = (pr.get("rounds") or [{}])[-1]
        review = last.get("review") or {}
        blocked = (last.get("review_dispatch") or {}).get("blocked")
        if pr["status"] == "human_confirmation_required":
            steps.append(("人类", f"确认 {name} 是否合并", "审核轮次已用完或未请求审核"))
        elif pr["status"] == "changes_requested":
            steps.append((owner, f"修改 {name} 后用新提交重新登记",
                          f"第 {review.get('round', '?')} 轮审核 {review.get('score', '?')} 分未通过"))
        elif pr["status"] == "checks_failed":
            failed = ", ".join(str(c) for c in (pr.get("merge_blocked") or {}).get(
                "failed_checks", []))
            steps.append((owner, f"修复 {name} 的失败检查", failed or "检查失败"))
        elif pr["status"] == "merge_failed":
            steps.append((owner, f"排查 {name} 合并失败", str((pr.get("merge_blocked") or {}).get(
                "reason") or "GitHub 拒绝合并")))
        elif (pr.get("merge_blocked") or {}).get("reason") == "merge_retry_exhausted":
            steps.append((owner, f"排查 {name} 合并受阻后 retry_effect", "合并重试已用完"))
        elif pr["status"] == "awaiting_review" and blocked:
            steps.append((owner, f"恢复 {name} 的审核派发（retry_effect）",
                          str(blocked.get("reason") if isinstance(blocked, dict) else blocked)))
        elif pr["status"] == "awaiting_review":
            steps.append(("审核者", f"审核 {name}", "等待异构审核"))
    pr_steps = bool(steps)
    for effect in response.get("effects") or []:
        retry = effect.get("retry") or {}
        exhausted = effect.get("status") == "observed_absence" and retry.get(
            "attempts_remaining") == 0
        if effect.get("required") and (effect.get("status") == "observed_failure" or exhausted):
            steps.append((owner, f"处理失败效果 {effect.get('effect_kind')}",
                          clip(effect.get("effect_id"), 24)))
    action = response.get("next_action") or {}
    if action.get("intent") and action["intent"] not in COVERED_INTENTS and not (
        pr_steps and action["intent"] in PR_INTENTS
    ):
        steps.append((owner, str(action["intent"]), str(action.get("reason") or "")))
    for contract in view.get("collaborations") or []:
        if contract.get("withdrawn"):
            continue
        for actor, answer in sorted((contract.get("responses") or {}).items()):
            if (answer or {}).get("status") == "pending":
                steps.append((actor, f"回应协作 {clip(contract['contract_id'], 14)}",
                              clip(contract.get("purpose"), 80)))
    graph = view["workflow"].get("graph") or {}
    stages = node_stages(view)
    for node in graph.get("nodes", []):
        if stages.get(node["node_id"]) == "ready":
            steps.append((str(node.get("executor_role")), f"领取节点 {node['node_id']}",
                          clip(node.get("purpose"), 80)))
    return steps


def header(response: Json) -> str:
    view = response["result"]["current_observation"]
    wi = response["work_item"]["id"]
    display = view.get("summary") or clip(view.get("title"), 120)
    affiliation = view.get("affiliation") or {}
    reference = affiliation.get("reference") or {}
    belongs = " ".join(
        str(v) for v in (reference.get("type"), reference.get("ref"), affiliation.get("work_class"))
        if v
    ) or str(affiliation.get("state") or "无")
    parts = [
        f"<code>{esc(wi)}</code>",
        *([f"父 WI {page_link(view['parent'])}"] if view.get("parent") else []),
        f"负责人 {esc(view['definition'].get('owner'))}",
        f"档位 {esc(view['profile'])}",
        f"归属 {esc(belongs)}",
        f"业务进度 {esc(view['business_progress'])}",
    ]
    return f'<header><h1>{esc(display)}</h1><div class="sub">{" &middot; ".join(parts)}</div></header>'


def banner(response: Json, state: Json) -> str:
    view = response["result"]["current_observation"]
    counts = {k: 0 for k in ("finished", "running", "waiting", "failed")}
    for stage in state["nodes"].values():
        key = {"ready": "waiting"}.get(stage, stage)
        if key in counts:
            counts[key] += 1
    statuses = list(state["pull_requests"].values())
    merged = statuses.count("merged")
    open_prs = sum(s not in {"merged", "withdrawn", "cancelled"} for s in statuses)
    facts = [f"执行进度 {esc(state['execution_progress'])}"]
    if view["workflow"].get("graph"):
        facts.append(
            f"节点 完成 {counts['finished']} / 进行 {counts['running']} / "
            f"等待 {counts['waiting']} / 失败 {counts['failed']}"
        )
    facts.append(f"PR 已合并 {merged} / 未结 {open_prs}")
    return (
        f'<div class="status"><span class="badge">{esc(str(state["business_progress"]).upper())}'
        f'</span><span class="meta" style="float:right">截至 {esc(local_time(state["as_of"]))}'
        f' &middot; 第 {esc(state["revision"])} 版</span><p>{" &middot; ".join(facts)}</p></div>'
    )


def linked(text: str) -> str:
    """Escaped text; only http(s) URLs become links, any other scheme stays text."""
    out, last = [], 0
    for match in URL.finditer(text):
        out.append(esc(text[last:match.start()]))
        out.append(link(match[0], match[0]))
        last = match.end()
    return "".join(out) + esc(text[last:])


def narrative_staleness(view: Json, as_of: str) -> tuple[int, bool] | None:
    """(newer progress records, older than STALE_AFTER) of the narrative; None without one."""
    from .narrative import STALE_AFTER

    record = view.get("narrative")
    if not isinstance(record, dict) or not isinstance(record.get("narrative"), dict):
        return None
    newer = len(view.get("records") or []) - int(record.get("after_records") or 0)
    try:
        aged = datetime.fromisoformat(as_of.replace("Z", "+00:00")) - datetime.fromisoformat(
            str(record["recorded_at"]).replace("Z", "+00:00")) > STALE_AFTER
    except (KeyError, ValueError, TypeError):
        aged = True
    return newer, aged


def narrative_section(view: Json, as_of: str) -> str:
    """The owning Agent's latest judgement, marked when newer facts may outdate it."""
    staleness = narrative_staleness(view, as_of)
    if staleness is None:
        return ""
    record = view["narrative"]
    part = record["narrative"]
    newer, aged = staleness
    note = f'{esc(record.get("author"))} 记于 {esc(local_time(str(record.get("recorded_at"))))}'
    if newer > 0 or aged:
        reasons = ([f"之后又有 {newer} 条进展记录"] if newer > 0 else []) + (
            ["已超过 3 天"] if aged else [])
        note += f' &middot; <span class="pill warn">可能已过时</span> {esc("，".join(reasons))}'
    body = []
    if part.get("banner"):
        body.append(f"<p><strong>{linked(part['banner'])}</strong></p>")
    for key, title in (("next_steps", "下一步判断"), ("risks", "风险"), ("decisions", "决策")):
        if part.get(key):
            items = "".join(f"<li>{linked(item)}</li>" for item in part[key])
            body.append(f"<p>{title}</p><ul>{items}</ul>")
    if part.get("background"):
        paragraphs = "".join(f"<p>{linked(line)}</p>" for line in part["background"].split("\n") if line.strip())
        body.append(f"<details><summary>背景</summary>{paragraphs}</details>")
    return f'<div class="callout"><p class="meta">Agent 判断 &middot; {note}</p>{"".join(body)}</div>'


def next_strip(response: Json) -> str:
    steps = next_steps(response)
    if not steps:
        return ('<details class="next" open><summary>下一步 <span class="meta">&middot; 无待办'
                '</span></summary><p class="none">内核事实中没有待处理的下一步。</p></details>')
    shown = steps[:NEXT_LIMIT]
    count = f"{len(steps)} 项" + (f"，列前 {NEXT_LIMIT}" if len(steps) > NEXT_LIMIT else "")
    items = "".join(
        f'<li><span class="who">{esc(who)}</span> — {esc(what)}'
        f' <span class="meta">— {esc(why)}</span></li>'
        for who, what, why in shown
    )
    return (f'<details class="next" open><summary>下一步 <span class="meta">&middot; {esc(count)}'
            f'</span></summary><ol>{items}</ol></details>')


def overview(response: Json, delta: list[str], baseline: Json | None) -> str:
    view = response["result"]["current_observation"]
    definition = view["definition"]
    rows = [[esc(ref), esc(text), pill(status)] for ref, text, status in acceptance(view)]
    since = (
        "<p class=\"meta\">首次生成，无上一版可比。</p>" if baseline is None
        else "<p class=\"meta\">无状态变化。</p>" if not delta
        else "<ul>" + "".join(f"<li>{esc(line)}</li>" for line in delta) + "</ul>"
    )
    since_title = "自上一版以来" if baseline is None else f"自第 {esc(baseline['revision'])} 版以来"
    return (
        f'<section class="pane active" id="over"><div class="callout">{esc(definition.get("why"))}'
        f"</div><h2>做什么</h2><p>{esc(definition.get('what'))}</p>"
        f"<h2>验收标准</h2>{table(['编号', '标准', '状态'], rows)}"
        f"{child_items(view)}"
        f"<h2>{since_title}</h2>{since}</section>"
    )


def workstreams(view: Json, state: Json) -> str:
    flow = view["workflow"]
    graph = flow.get("graph")
    if not graph:
        return '<section class="pane" id="work"><p class="meta">尚无已采用计划。</p></section>'
    outcomes = node_outcomes(view)
    rows = []
    for node in graph["nodes"]:
        node_id = node["node_id"]
        after = [
            f"after {e['from']}" + ("" if e["condition"] in {"*", "success"} else f" ({e['condition']})")
            for e in graph["edges"] if e["to"] == node_id
        ]
        outcome, attempts = outcomes.get(node_id, ("", 0))
        rows.append([
            f"<code>{esc(node_id)}</code>",
            esc(clip(node.get("purpose"), 400)),
            esc(node.get("executor_role")),
            esc(", ".join(after) or "—"),
            pill(state["nodes"].get(node_id, "waiting")),
            esc(f"{attempts} 次" + (f" · {outcome}" if outcome else "")),
        ])
    plan = flow.get("adoption") or {}
    detail = (f"<details><summary>已采用计划</summary><p>{esc(plan.get('plan_text'))}</p>"
              f"<p class=\"meta\">{esc(plan.get('adopted_by'))} · "
              f"{esc(local_time(str(plan.get('adopted_at'))))}</p></details>") if plan else ""
    return (
        '<section class="pane" id="work"><h2>计划节点</h2>'
        + table(["节点", "目的", "执行角色", "依赖", "阶段", "尝试/结果"], rows)
        + detail + "</section>"
    )


def pull_requests(view: Json) -> str:
    rows = []
    for pr in view.get("pull_requests") or []:
        number, repository = pr["number"], str(pr["repository"])
        label = f"#{number}"
        cell = (
            link(f"https://github.com/{repository}/pull/{number}", label)
            if type(number) is int and REPOSITORY.fullmatch(repository) else esc(label)
        )
        latest = pr["rounds"][-1] if pr.get("rounds") else {}
        review = latest.get("review") or {}
        merge = ((latest.get("merge") or {}).get("observation")) or {}
        rows.append([
            cell, esc(repository), pill(pr["status"]),
            esc(f"{pr.get('reviewed_rounds', 0)}/{len(pr.get('rounds') or [])}"),
            esc(review.get("score", "—")),
            f"<code>{esc(merge['merge_commit'][:12])}</code>" if merge.get("merge_commit") else "—",
        ])
    body = table(["PR", "仓库", "状态", "审核轮次", "最新评分", "合并提交"], rows) if rows else (
        '<p class="meta">尚无登记的 PR。</p>')
    return f'<section class="pane" id="pr"><h2>PR</h2>{body}</section>'


def pr_text(pr: Json) -> str:
    number, repository = pr.get("number"), str(pr.get("repository"))
    label = f"{repository}#{number}"
    if type(number) is int and REPOSITORY.fullmatch(repository):
        label = link(f"https://github.com/{repository}/pull/{number}", label)
    else:
        label = esc(label)
    return f"{label} {pill(pr.get('status'))}"


def child_items(view: Json) -> str:
    """A parent's children as the kernel read them; their cost lives in Stats."""
    rows = []
    for child in view.get("children") or []:
        prs = child.get("pull_requests") or []
        rows.append([
            page_link(child.get("id")),
            esc(clip(child.get("summary"), 200)),
            pill(child.get("business_progress")),
            esc(f"{len(prs)} 个") + ("<br>" + "<br>".join(pr_text(pr) for pr in prs) if prs else ""),
        ])
    if not rows:
        return ""
    return ("<h2>子 WI</h2>" + table(["子 WI", "简介", "状态", "PR"], rows)
            + '<p class="meta">子 WI 的成本见 Stats「产出与成本」。</p>')


def collaborations(view: Json) -> str:
    rows = []
    for contract in view.get("collaborations") or []:
        answers = ", ".join(
            f"{actor}: {(answer or {}).get('status')}"
            for actor, answer in sorted((contract.get("responses") or {}).items())
        )
        rows.append([
            f"<code>{esc(clip(contract['contract_id'], 14))}</code>",
            esc(contract.get("kind")) + (" · 已撤回" if contract.get("withdrawn") else ""),
            esc(contract.get("offered_by")),
            esc(answers or "—"),
            esc(clip(contract.get("purpose"), 400)),
        ])
    body = table(["合同", "类型", "发起", "回应", "目的"], rows) if rows else (
        '<p class="meta">无协作合同。</p>')
    return f'<section class="pane" id="collab"><h2>协作</h2>{body}</section>'


def timeline(view: Json) -> str:
    records = list(reversed(view.get("records") or []))
    items = []
    for record in records[:RECORD_LIMIT]:
        body = record.get("record") if isinstance(record.get("record"), dict) else {}
        summary = body.get("summary") or record.get("checkpoint") or ""
        items.append(
            f"<li>{pill(record.get('kind'))} <span class=\"meta\">{esc(record.get('author'))}"
            f"</span><br>{esc(clip(summary, 600))}</li>"
        )
    more = (f'<p class="meta">另有 {len(records) - RECORD_LIMIT} 条较早记录未显示。</p>'
            if len(records) > RECORD_LIMIT else "")
    notices = view.get("notices") or {}
    notice = (f"<h2>通知</h2><p>共 {esc(notices.get('count', 0))} · 未结 {esc(notices.get('open', 0))}"
              f" · 失败 {esc(notices.get('failed', 0))} · 积压 {esc(notices.get('backlog', 0))}</p>")
    listing = f"<ol>{''.join(items)}</ol>" if items else '<p class="meta">尚无记录。</p>'
    return f'<section class="pane" id="log"><h2>记录（新到旧）</h2>{listing}{more}{notice}</section>'


def render(response: Json, *, as_of: str, baseline: Json | None) -> tuple[str, Json]:
    """Pure function of one kernel response, its revision time and the previous snapshot."""
    view = response["result"]["current_observation"]
    state = page_state(response, as_of)
    delta = changes(baseline, state)
    state["since_revision"] = None if baseline is None else baseline["revision"]
    state["changes"] = delta
    light = view["profile"] == "lightweight"
    tabs = [("over", "概览")]
    panes = [overview(response, delta, baseline)]
    if not light:
        tabs.append(("work", "计划节点"))
        panes.append(workstreams(view, state))
    tabs.append(("pr", "PR"))
    panes.append(pull_requests(view))
    if not light:
        tabs.append(("collab", "协作"))
        panes.append(collaborations(view))
    tabs.append(("log", "记录"))
    panes.append(timeline(view))
    bar = "".join(
        f'<button class="tab{" active" if i == 0 else ""}" data-pane="{key}">'
        f"{i + 1} &middot; {label}</button>"
        for i, (key, label) in enumerate(tabs)
    )
    body = (
        header(response) + banner(response, state) + next_strip(response) + NARRATIVE_SLOT
        + narrative_section(response["result"]["current_observation"], as_of)
        + f'<div class="tabbar">{bar}</div><main>{"".join(panes)}</main>'
    )
    title = f"{view.get('summary') or clip(view.get('title'), 80)} — {response['work_item']['id']}"
    block = json.dumps(state, ensure_ascii=False, sort_keys=True).replace("<", "\\u003c")
    slots = {"title": esc(title), "body": body, "state": block}
    # One pass over the template only: inserted text is never scanned for slots again.
    return SLOT.sub(lambda m: slots[m.group(1)], template()), state


def atomic(root: Path, target: Path, content: bytes) -> None:
    if target.is_symlink():
        raise OSError("Page target cannot redirect writes")
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".generation-", delete=False) as out:
            temporary = out.name
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def valid_state(value: Any, wi: str) -> bool:
    return (
        isinstance(value, dict)
        and value.get("work_item_id") == wi
        and type(value.get("revision")) is int
    )


def previous(path: Path, wi: str) -> Json | None:
    """The sidecar, or None when missing or damaged in any way: views are disposable."""
    try:
        saved = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if (
        not isinstance(saved, dict)
        or saved.get("schema") != SCHEMA
        or not valid_state(saved.get("current"), wi)
        or not (saved.get("baseline") is None or valid_state(saved["baseline"], wi))
    ):
        return None
    return dict(saved)


STATE_BLOCK = re.compile(
    r'<script type="application/json" id="artifact-state">(.*?)</script>', re.S
)


def shown(page: Path, wi: str) -> Json | None:
    """The state embedded in the page itself; the page is what readers see."""
    try:
        match = STATE_BLOCK.search(page.read_text(encoding="utf-8"))
        state = json.loads(match[1]) if match else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(state, dict) or not valid_state(state, wi) or state.get("schema") != SCHEMA:
        return None
    return dict(state)


def write(root: Path, response: Json, *, as_of: str, refresh: bool = True) -> Json | None:
    """Render under a publication lock the caller already holds.

    A page never moves backwards: an older revision does not replace a newer one.
    With refresh=False an existing page for the same revision is kept as is.
    """
    work_item = response.get("work_item") or {}
    view = (response.get("result") or {}).get("current_observation")
    if view is None or not isinstance(work_item.get("local_revision"), int):
        return None
    wi = identity(work_item["id"])
    revision = work_item["local_revision"]
    page, sidecar = root / (wi + ".html"), root / (wi + ".state.json")
    # The page carries its own revision, so a failed sidecar write can neither
    # misreport the page nor let a late older commit move it backwards.
    current = shown(page, wi)
    if current is not None and (
        current["revision"] > revision
        or current["revision"] == revision and not refresh and not expired_unmarked(current, as_of)
    ):
        return {"path": str(page), "revision": current["revision"]}
    saved = previous(sidecar, wi)
    baseline: Json | None
    if current is not None and current["revision"] < revision:
        baseline = {k: v for k, v in current.items() if k not in ("changes", "since_revision")}
    elif saved is not None and saved["current"]["revision"] == revision:
        baseline = saved.get("baseline")  # Re-rendering a revision keeps its delta.
    else:
        baseline = None
    content, state = render(response, as_of=as_of, baseline=baseline)
    atomic(root, page, content.encode("utf-8"))
    atomic(root, sidecar, json.dumps(
        {"schema": SCHEMA, "current": {k: v for k, v in state.items() if k != "changes"},
         "baseline": baseline}, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return {"path": str(page), "revision": revision}


def lock(root: Path) -> Any:
    if root.resolve() != root:
        raise OSError("Page root cannot redirect writes")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / ".publication.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    stream = os.fdopen(fd, "rb")
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    return stream


def publish(root: Path, response: Json, *, as_of: str) -> Json | None:
    """Post-commit entry: take the shared view lock, then write the page."""
    root = root.absolute()
    with lock(root):
        return write(root, response, as_of=as_of)


def located(root: Path, wi: str) -> Json | None:
    """Where the current page is, read from the page itself; None when absent or damaged."""
    root = root.absolute()
    page = root / (identity(wi) + ".html")
    current = shown(page, wi)
    return None if current is None else {"path": str(page), "revision": current["revision"]}
