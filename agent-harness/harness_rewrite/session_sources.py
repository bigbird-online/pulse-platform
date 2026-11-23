"""Runtime-specific source readers; no lifecycle state or authorization decisions.

Only operator-pinned native ancestors and their original session logs qualify.
Commands cannot choose a transcript, runtime, actor, or authorization context.
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Iterator, cast

from .model import Json, Rejected, digest, encoded

RUNTIMES = {"pi", "cc", "codex", "agy"}
# Antigravity CLI keeps one directory per conversation under its brain root; the
# directory name is the conversation ID, the transcript names no session itself.
AGY_TRANSCRIPT = Path(".system_generated/logs/transcript_full.jsonl")
MAX_ROW = 8 * 1024 * 1024
# A Relay scheduler run: its proof supplies a CLI session's native ID only.
SCHEDULER = "scheduler"
SESSION_KEYS = {"CODEX_THREAD_ID", "CODEX_SESSION_ID", "PULSE_PI_TENANT_ID"}


def program(profile: Json, value: object) -> bool:
    """Whether one process path is the profile's installed native program.

    An executable_root admits every protected program file directly inside the
    provider's version directory, so a runtime upgrade keeps its identity.
    """
    if not isinstance(value, str) or not value.startswith("/"):
        return False
    path = Path(value).resolve()
    if profile.get("executable"):
        return path == Path(profile["executable"]).resolve()
    if path.parent != Path(profile["executable_root"]) or not path.is_file():
        return False
    status = path.stat()
    return status.st_uid == os.getuid() and not status.st_mode & 0o022


def rows(path: Path) -> Iterator[tuple[int, Json]]:
    """Bound each row without retaining tool payloads or copying the session."""
    with path.open("rb") as stream:
        extent = os.fstat(stream.fileno()).st_size
        while stream.tell() < extent:
            offset = stream.tell()
            raw = stream.readline(min(MAX_ROW + 1, extent - offset))
            if not raw.endswith(b"\n"):
                # Large tool rows are never human proof. Drain bounded chunks.
                while raw and not raw.endswith(b"\n") and stream.tell() < extent:
                    raw = stream.readline(min(65536, extent - stream.tell()))
                continue
            if len(raw) > MAX_ROW:
                continue
            # Native writers may leave empty separators between complete records.
            # They contain no evidence; retain physical offsets and original bytes.
            # Do not infer that malformed nonempty records are harmless metadata.
            if not raw.strip(b" \t\r\n"):
                continue
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise Rejected(
                    "source_invalid", "Native session contains an invalid complete row"
                ) from exc
            if not isinstance(row, dict):
                raise Rejected("source_invalid", "Invalid native session record")
            yield offset, row


def user_text(row: Json, runtime: str) -> str | None:
    if runtime == "agy":
        # Only the user's own explicit input; system notices are never consent.
        if row.get("type") != "USER_INPUT" or row.get("source") != "USER_EXPLICIT":
            return None
        content = row.get("content")
        if not isinstance(content, str):
            return None
        request = re.search(r"<USER_REQUEST>\n?(.*?)\n?</USER_REQUEST>", content, re.S)
        return request.group(1) if request else content
    if runtime == "codex":
        # response_item is the original native input. event_msg duplicates it.
        if row.get("type") != "response_item":
            return None
        message = row.get("payload", {})
    else:
        if row.get("type") not in ({"user"} if runtime == "cc" else {"message"}):
            return None
        if runtime == "cc" and (row.get("isMeta") or row.get("isSidechain")):
            return None
        message = row.get("message", {})
    if message.get("role") != "user":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    # Tool results in CC are also role=user. They never supply human consent.
    texts = [
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and part.get("type") in {"text", "input_text"}
    ]
    return "\n".join(texts) if texts else None


def original_user(
    path: Path, runtime: str, native_id: str, ref: str | None, *, latest: bool = False
) -> tuple[str, bytes]:
    selected = None
    last = None
    for offset, row in rows(path):
        text = user_text(row, runtime)
        if text is None:
            continue
        current = digest([runtime, native_id, offset, row])
        last = current
        if ref is None or ref == current:
            selected = (
                current,
                encoded(
                    {
                        "source": {
                            "channel": runtime,
                            "eventId": current,
                            "humanRef": "native-user",
                            "message": text,
                        },
                        "native_session_id": native_id,
                        "offset": offset,
                        "original": row,
                    }
                ),
            )
    if selected is None or (latest and selected[0] != last):
        raise Rejected("source_invalid", "Original native human input is absent or superseded")
    return selected


def _option(argv: list[str], name: str) -> str | None:
    try:
        return argv[argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


def conversation_option(argv: list[str]) -> str | None:
    """agy's one --conversation value in a bounded argv (argv[0] is the program).

    Absent, repeated or malformed is no identity: the session is not guessed.
    """
    values = []
    for index, arg in enumerate(argv[1:], 1):
        if arg == "--conversation":
            values.append(argv[index + 1] if index + 1 < len(argv) else "")
        elif arg.startswith("--conversation="):
            values.append(arg.partition("=")[2])
    if len(values) != 1 or re.fullmatch(r"[A-Za-z0-9-]{1,128}", values[0]) is None:
        return None
    return values[0]


def process_cwd(pid: int, proc: Path) -> Path | None:
    """The working directory of one native process, read-only; None if unreadable."""
    from .runtime_presence import procfs_absent

    if procfs_absent(proc):
        try:
            listed = subprocess.run(
                ["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        found = [line[1:] for line in listed.splitlines() if line.startswith("n/")]
        return Path(found[0]) if len(found) == 1 else None
    try:
        return (proc / str(pid) / "cwd").resolve(strict=True)
    except OSError:
        return None


def process_identity(pid: int, proc: Path) -> Json:
    """Identity in the format Relay writes for its source proofs.

    Linux: the full process stamp. macOS: Relay's `nativeProcessStamp` stores
    only {pid, started}, so the boot part of the stamp is left out here.
    """
    from .runtime_presence import process_started, process_stamp, procfs_absent

    if procfs_absent(proc):
        return {"pid": pid, "started": process_started(pid)}
    return process_stamp(pid, proc)


def ps_environment(raw: str) -> tuple[dict[str, str], bool]:
    """Session keys from `ps eww` output, and whether it showed an environment.

    macOS shows no environment of a platform program such as /bin/zsh to a
    non-root reader, so a shell that forks the caller hides these keys.
    """
    found = dict(re.findall(r"(?:^|\s)([A-Z_]+)=([^\s]+)", raw))
    return {k: v for k, v in found.items() if k in SESSION_KEYS}, "PATH" in found


def inherited_environment(pid: int, proc: Path) -> tuple[dict[str, str], bool]:
    """Session keys from one process environment, and whether it was readable."""
    if proc == Path("/proc") and not proc.exists():
        # Read only the native process's direct child, before caller-created
        # wrappers can replace their own environment. Never return raw env.
        return ps_environment(
            subprocess.run(
                ["ps", "eww", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout
        )
    environment_bytes = (proc / str(pid) / "environ").read_bytes()
    return {
        k.decode(): v.decode()
        for row in environment_bytes.split(b"\0")
        if b"=" in row
        for k, v in [row.split(b"=", 1)]
        if k.decode() in SESSION_KEYS
    }, True


def _proof_record(root: Path, holder: Json, proc: Path) -> Json | None:
    """The source proof Relay wrote for this live process, if any.

    Relay names a proof by PID only. A record whose process birth differs from
    the live process was left by an earlier process with the same PID: it
    proves nothing about this process, so it counts as no record and grants
    nothing. A record of this very process is checked in full by the caller.
    """
    path = root / (str(holder["pid"]) + ".json")
    if not path.exists():
        return None
    if (
        path.resolve() != path
        or path.stat().st_uid != os.getuid()
        or path.stat().st_mode & 0o077
        or path.stat().st_size > 4096
    ):
        raise Rejected("authority_denied", "Native process source proof is not protected")
    record = cast(Json, json.loads(path.read_bytes()))
    if record.get("process") != process_identity(holder["pid"], proc):
        return None
    return record


def _check_relay_record(
    policy: Json, record: Json, runtime: str, holder: Json, native: Json, chain: list[Json]
) -> None:
    parent = next((p for p in chain if p["pid"] == record.get("relay_pid")), None)
    # The record already names this live process (_proof_record). The failed
    # condition is named (never a path or process detail) so that a denial can
    # be told apart afterwards: another runtime, a Relay that is no longer an
    # ancestor, or a Relay other than the installed entry.
    if record.get("schema") != "pulse.native-session-source/v1" or record.get("runtime") != runtime:
        failed = "proof_kind"
    elif holder["uid"] != native["uid"]:
        failed = "owner"
    elif parent is None:
        failed = "relay_not_ancestor"
    elif parent["uid"] != native["uid"]:
        failed = "relay_owner"
    elif not any(
        Path(arg).resolve() == Path(policy["relay_entry"]).resolve()
        for arg in parent["argv"][:3]
        if arg.startswith("/")
    ):
        failed = "relay_entry"
    else:
        return
    raise Rejected(
        "authority_denied",
        "Native source proof differs from the installed Relay ancestry: " + failed,
    )


def relay_identity(
    policy: Json, profile: Json, native: Json, chain: list[Json], proc: Path
) -> Json | None:
    """The Relay conversation proof of this native process; scheduler runs claim no tenant.

    Relay starts Codex through its npm launcher and records that process, so
    a Codex proof may name the native runtime's direct parent, exactly as a
    scheduler proof may. Other runtimes are started directly.
    """
    root = profile.get("binding_root")
    if not root:
        return None
    runtime = profile.get("runtime", "pi")
    index = chain.index(native)
    holders = chain[index : index + 2] if runtime == "codex" else [native]
    found = []
    for holder in holders:
        record = _proof_record(Path(root), holder, proc)
        if record is None or record.get("entry") == SCHEDULER:
            continue
        _check_relay_record(policy, record, runtime, holder, native, chain)
        found.append(record)
    if len(found) > 1:
        raise Rejected("authority_denied", "Native conversation source proof is ambiguous")
    return found[0] if found else None


def relay_proof(
    policy: Json, runtime: str, native: Json, chain: list[Json], proc: Path
) -> Json | None:
    """The Relay proof of this native process under any same-runtime proof root."""
    for profile in policy["profiles"]:
        if profile.get("runtime", "pi") == runtime and profile.get("binding_root"):
            proof = relay_identity(policy, profile, native, chain, proc)
            if proof is not None:
                return proof
    return None


def scheduler_proof(
    policy: Json, runtime: str, native: Json, chain: list[Json], proc: Path
) -> Json | None:
    """The Relay scheduler proof of this native process or of its launcher.

    The scheduler names the process it started: the runtime itself, or the
    launcher (Codex npm shim, CC PTY bridge) whose direct child it is. A
    runtime nested deeper has another parent, so it never inherits the proof.
    """
    index = chain.index(native)
    roots = {
        p["binding_root"]
        for p in policy["profiles"]
        if p.get("runtime", "pi") == runtime and p.get("binding_root") and not p.get("standalone")
    }
    found = []
    for root in sorted(roots):
        for holder in chain[index : index + 2]:
            record = _proof_record(Path(root), holder, proc)
            if record is None or record.get("entry") != SCHEDULER:
                continue
            _check_relay_record(policy, record, runtime, holder, native, chain)
            found.append(record)
    if len(found) > 1:
        raise Rejected("authority_denied", "Native scheduler source proof is ambiguous")
    return found[0] if found else None


def tenant_directory(tenant: str) -> str:
    """Relay's per-tenant Pi session directory name (safeTenantDirectory)."""
    prefix = re.sub(r"[^A-Za-z0-9_.-]", "_", tenant)[:48] or "tenant"
    return prefix + "-" + hashlib.sha256(tenant.encode()).hexdigest()[:12]


def locate(
    policy: Json,
    chain: list[Json],
    proc: Path,
    *,
    entry: Path | None = None,
    pre_model: bool = False,
    nearer_than: int | None = None,
) -> Json | None:
    """Authenticate CC/Codex source using installed executable and source roots.

    With pre_model, a Relay-proven session whose runtime has not yet written its
    first log row is located without a path. Only Relay slash input uses this.
    With `nearer_than`, only the first that many processes can be the native
    runtime; its Relay, scheduler and launcher proofs still read the whole chain.
    """
    found = []
    pending = []
    for profile in policy["profiles"]:
        runtime = profile.get("runtime", "pi")
        if runtime == "pi" and not profile.get("binding_root"):
            continue
        if runtime not in RUNTIMES or (
            runtime != "pi" and not (profile.get("executable") or profile.get("executable_root"))
        ):
            raise Rejected("authority_denied", "Native runtime profile is incomplete")
        installed = {"executable": policy["producer_node"]} if runtime == "pi" else profile
        native = next(
            (
                row
                for row in chain[:nearer_than]
                if row["uid"] == os.getuid()
                and any(
                    program(installed, value)
                    for value in [row.get("executable"), *row["argv"][:3]]
                )
            ),
            None,
        )
        if native is None:
            continue
        # A scheduler proof can name a Codex launcher that runs on the Pi node;
        # it belongs to the CLI profile of the runtime below, never to Pi.
        if runtime == "pi":
            record = _proof_record(Path(profile["binding_root"]), native, proc)
            if record is None or record.get("entry") == SCHEDULER:
                continue
        if profile.get("standalone"):
            if entry is None:
                continue
            from .standalone import identity

            proof = identity(policy, profile, native, chain, proc, entry)
            if proof is None:
                continue
        else:
            proof = relay_identity(policy, profile, native, chain, proc)
        tenants = profile.get("relay_tenants")
        if tenants and (proof is None or proof.get("tenant") not in tenants):
            continue
        # A profile without tenants yields to the Relay profile that claims this
        # process's tenant, even when it has no proof root of its own.
        claim = (
            proof
            if proof is not None or tenants or profile.get("standalone")
            else relay_proof(policy, runtime, native, chain, proc)
        )
        if (
            not tenants
            and claim is not None
            and any(
                p.get("runtime", "pi") == runtime
                and p.get("relay_tenants")
                and claim.get("tenant") in p["relay_tenants"]
                for p in policy["profiles"]
            )
        ):
            continue
        # A CLI profile (no proof root, not standalone) admits a Relay
        # scheduler run. Its proof replaces only the inherited session ID; the
        # registered workspace still selects the Agent, and no tenant applies.
        scheduled = (
            scheduler_proof(policy, runtime, native, chain, proc)
            if runtime != "pi" and not profile.get("binding_root") and not profile.get("standalone")
            else None
        )
        index = chain.index(native)
        reader = chain[max(0, index - 1)]["pid"]
        environment, visible = ({}, True) if scheduled else inherited_environment(reader, proc)
        native_id = (
            proof["session_id"]
            if proof
            else scheduled["session_id"]
            if scheduled
            else (_option(native["argv"], "--session-id") or _option(native["argv"], "--resume"))
            if runtime == "cc"
            else conversation_option(native["argv"])
            if runtime == "agy"
            else environment.get("CODEX_THREAD_ID") or environment.get("CODEX_SESSION_ID")
        )
        if runtime == "agy" and not proof and profile.get("workspace_roots"):
            # An agy transcript records no working directory. Without a Relay
            # conversation proof (tenant), the process's own cwd selects the
            # Agent, as a CC or Codex header cwd does.
            cwd = process_cwd(native["pid"], proc)
            if cwd is None or not any(
                cwd == Path(p) or Path(p) in cwd.parents for p in profile["workspace_roots"]
            ):
                continue
        if proof:
            native_id = proof["session_id"]
            if profile.get("standalone"):
                environment.pop("PULSE_PI_TENANT_ID", None)
            else:
                environment["PULSE_PI_TENANT_ID"] = proof["tenant"]
        if not native_id or re.fullmatch(r"[A-Za-z0-9-]{1,128}", native_id) is None:
            # Locating facts only: no environment values or session IDs.
            raise Rejected(
                "source_unavailable",
                "Native Host session identity is missing (runtime=%s, native_pid=%s, "
                "relay_proof=%s, scheduler_proof=%s, environment_pid=%s, environment=%s)"
                % (
                    runtime,
                    native["pid"],
                    "present" if proof else "absent",
                    "present" if scheduled else "absent",
                    reader,
                    "readable" if visible else "unreadable",
                ),
            )
        candidate = {
            "process": native,
            "profile": profile,
            "header": {"type": "session", "id": native_id},
            "runtime": runtime,
            "environment": environment,
            # Only Relay knows its conversation entry; a proof written before
            # entries were recorded names the Relay without one.
            "entry": proof.get("entry", "relay")
            if proof and not profile.get("standalone")
            else SCHEDULER
            if scheduled
            else "cli",
        }
        for path in source_files(profile, native_id):
            found.append({**candidate, "path": path})
        if (
            pre_model
            and proof is not None
            and not profile.get("standalone")
            and (
                tenants
                or (
                    runtime == "pi"
                    and Path(profile["session_root"]).name == tenant_directory(proof["tenant"])
                )
            )
        ):
            pending.append({**candidate, "path": None})
    if not found and pre_model:
        found = pending
    if len(found) > 1:
        raise Rejected("authority_denied", "Native Host profile is ambiguous")
    return found[0] if found else None


def old_session_ref(runtime: str, native_id: str) -> str:
    prefix = "pulse-session-v1\0" if runtime in {"pi", "cc", "agy"} else "scl-codex-session-v1\0"
    return "session_v1_" + hashlib.sha256((prefix + native_id).encode()).hexdigest()


def codex_files(root: Path, native_id: str) -> list[Path]:
    """Resolve a logical desktop thread's current physical rollout, read-only.

    Codex resumes may append a rollout UUID to the filename without changing
    session_meta.id. Its own account-owned index locates the current original;
    it supplies no WI, grant, actor or human decision. The caller still checks
    the original header, workspace and delegation boundary below. Never guess
    between different originals by mtime, size or an older matching filename.
    """
    databases = [
        path for path in root.parent.glob("state_*.sqlite")
        if re.fullmatch(r"state_[0-9]+\.sqlite", path.name)
    ]
    if databases:
        database = max(databases, key=lambda p: int(p.stem.split("_")[1]))
        if (
            database.resolve() != database
            or database.stat().st_uid != os.getuid()
            or database.stat().st_mode & 0o022
        ):
            raise Rejected("authority_denied", "Native Codex locator is not account protected")
        try:
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
                found = conn.execute(
                    "SELECT rollout_path FROM threads WHERE id=?", (native_id,)
                ).fetchall()
        except sqlite3.Error as exc:
            raise Rejected("source_unavailable", "Native Codex current rollout locator is unavailable") from exc
        if len(found) > 1:
            raise Rejected("source_invalid", "Native Codex current rollout locator is ambiguous")
        if found:
            if not isinstance(found[0][0], str):
                raise Rejected("source_invalid", "Native Codex current rollout path is invalid")
            selected = Path(found[0][0])
            if (
                not selected.is_absolute()
                or root not in selected.parents
                or selected.suffix != ".jsonl"
            ):
                raise Rejected("authority_denied", "Native Codex current rollout is outside its source root")
            if not selected.exists():
                raise Rejected("source_unavailable", "Native Codex current rollout original is unavailable")
            return [selected]
    files = [
        p for p in root.rglob("*" + native_id + "*.jsonl")
        if re.search(re.escape(native_id) + r"(?:_[A-Za-z0-9-]{1,128})?\.jsonl$", p.name)
    ]
    if len(files) > 1:
        raise Rejected("source_unavailable", "Native Codex current rollout requires its native locator")
    return files


def source_files(profile: Json, native_id: str) -> Iterator[Path]:
    runtime = profile.get("runtime", "pi")
    root = Path(profile["session_root"])
    if root.resolve(strict=True) != root:
        raise Rejected("authority_denied", "Native source root redirects")
    if runtime == "agy":
        # The conversation directory is the identity; never search other names.
        if re.fullmatch(r"[A-Za-z0-9-]{1,128}", native_id) is None:
            raise Rejected("source_invalid", "Native agy conversation identity is invalid")
        conversation = root / native_id
        if conversation.is_symlink() or (conversation.exists() and conversation.resolve() != conversation):
            raise Rejected("authority_denied", "Native agy conversation redirects")
        transcript = conversation / AGY_TRANSCRIPT
        files = [transcript] if transcript.exists() or transcript.is_symlink() else []
    else:
        files = codex_files(root, native_id) if runtime == "codex" else list(
            root.rglob(("*" if runtime == "pi" else "") + native_id + ".jsonl")
        )
    for path in files:
        if path.is_symlink() or path.resolve() != path or path.stat().st_uid != os.getuid():
            raise Rejected(
                "authority_denied", "Native source is redirected or owned by another account"
            )
        first = next(rows(path), None)
        if first is None:
            continue
        row = first[1]
        if runtime == "agy":
            # Every agy row is one step; the first carries its index and type.
            if not isinstance(row.get("step_index"), int) or not isinstance(row.get("type"), str):
                raise Rejected("source_invalid", "Native agy transcript differs from its step format")
        elif runtime == "pi":
            if row.get("type") != "session" or row.get("id") != native_id:
                raise Rejected("source_invalid", "Native Pi header differs from Host identity")
            scopes = [Path(p) for p in profile.get("workspace_roots", [])]
            cwd = Path(str(row.get("cwd", "")))
            if scopes and (
                not cwd.is_absolute() or not any(cwd == p or p in cwd.parents for p in scopes)
            ):
                continue
        elif runtime == "codex":
            meta = row.get("payload", {})
            if row.get("type") != "session_meta" or meta.get("id") != native_id:
                raise Rejected("source_invalid", "Native Codex header differs from Host identity")
            cwd = Path(str(meta.get("cwd", "")))
            current = profile.get("workspace_roots") or profile["artifact_roots"]
            # Original roots admit a historical header in the registered session
            # directory. Artifact and current working-directory scopes stay separate.
            scopes = [Path(p) for p in (*current, *profile.get("original_workspace_roots", []))]
            if not cwd.is_absolute() or ".." in cwd.parts or not any(cwd == p or p in cwd.parents for p in scopes):
                continue
            if meta.get("forked_from_id") or isinstance(meta.get("source"), dict):
                raise Rejected(
                    "authority_denied",
                    "Delegated sessions require a separately admitted profile",
                )
        else:
            # CC may begin with file-history-snapshot, so inspect identities
            # from the first matching native message instead of its first row.
            identified = next(
                (
                    r
                    for _, r in rows(path)
                    if r.get("type") in {"user", "assistant"}
                    and r.get("sessionId")
                    and not r.get("isSidechain")
                ),
                None,
            )
            if identified is None or identified["sessionId"] != native_id:
                raise Rejected("source_invalid", "Native CC header differs from process identity")
            if profile.get("workspace_roots"):
                cwd = Path(str(identified.get("cwd", "")))
                if not cwd.is_absolute() or not any(
                    cwd == Path(p) or Path(p) in cwd.parents for p in profile["workspace_roots"]
                ):
                    continue
        yield path
