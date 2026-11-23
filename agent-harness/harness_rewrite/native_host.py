"""Pi Host admission from real process ancestry and native session evidence.

The installed policy selects allowed session and artifact roots. Neither actor,
session, generation nor a human-confirmed flag is accepted from a command file.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import shlex
import sys
import ctypes
from typing import Any, Callable

from .runtime_presence import capture_native, process_stamp, procfs_absent
from .access import agent_workspaces
from .domain import INTENTS
from .delivery import WORKER_INTENTS
from .model import (
    ArtifactInput,
    HostContext,
    Json,
    Rejected,
    RepositoryBinding,
    PublicationTarget,
    SourceConsumer,
    SourceInput,
    digest,
    encoded,
    native_session_ref,
)


def contained(path: Path, root: Path | None) -> bool:
    return root is not None and (path == root or root in path.parents)


def session_root(profile: Json) -> Path | None:
    """The profile's resolved session root; a root that does not exist contains no log,
    so another profile's missing root never refuses this session."""
    try:
        return Path(profile["session_root"]).resolve(strict=True)
    except FileNotFoundError:
        return None


def pidpath(pid: int) -> Path:
    """macOS kernel executable path of one live process (libproc proc_pidpath)."""
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    library.proc_pidpath.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4096)
    if library.proc_pidpath(pid, buffer, len(buffer)) <= 0:
        raise Rejected("source_unavailable", "Native process executable is unavailable")
    return Path(os.fsdecode(buffer.value)).resolve(strict=True)


def native_executable(pid: int, proc: Path) -> Path:
    if procfs_absent(proc):
        return pidpath(pid)
    return (proc / str(pid) / "exe").resolve(strict=True)


def procargs(pid: int) -> bytes:
    """macOS KERN_PROCARGS2 of one same-user process, bounded by kern.argmax."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.sysctl.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    libc.sysctl.restype = ctypes.c_int
    argmax = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(argmax))
    if (
        libc.sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(argmax), size, None, 0) != 0
        or argmax.value <= 0
    ):
        raise OSError("kern.argmax is unavailable")
    size = ctypes.c_size_t(argmax.value)
    buffer = ctypes.create_string_buffer(argmax.value)
    if libc.sysctl((ctypes.c_int * 3)(1, 49, pid), 3, buffer, size, None, 0) != 0:
        raise OSError(ctypes.get_errno(), "Process arguments are unavailable")
    return buffer.raw[: size.value]


ENVIRONMENT_KEYS = frozenset(
    {
        "PULSE_PI_TENANT_ID",
        "SCL_RELAY_HOST_CONTEXT",
        "SCL_SESSION_REF",
        "HARNESS_REWRITE_DISPATCH",
        "HARNESS_REWRITE_ATTEMPT",
    }
)


def procargs_argv(raw: bytes) -> list[str]:
    """argv with its kernel boundaries from KERN_PROCARGS2 bytes.

    Layout: argc, exec path, NUL padding, then argc NUL-terminated strings.
    Fewer complete strings than argc is a truncated or rewritten record.
    """
    if len(raw) < 4:
        raise ValueError("Process arguments are truncated")
    argc = int.from_bytes(raw[:4], sys.byteorder)
    rest = raw[4:]
    end = rest.find(b"\0")
    if not 1 <= argc <= 65536 or end < 0:
        raise ValueError("Process arguments are malformed")
    while end < len(rest) and rest[end] == 0:
        end += 1
    parts = rest[end:].split(b"\0")
    if len(parts) <= argc:
        raise ValueError("Process arguments are truncated")
    return [part.decode() for part in parts[:argc]]


def procargs_fields(raw: bytes) -> tuple[str, dict[str, str]]:
    """Process title and selected environment from KERN_PROCARGS2 bytes.

    Layout: argc, exec path, NUL padding, argv, environment, then Apple's exec
    strings. Node's process.title overwrites argv[0] and blanks the rest of the
    argv area, so argc no longer counts strings; the non-empty strings after the
    title are read and only the selected keys are kept. A repeated key is
    ambiguous and rejected.
    """
    if len(raw) < 4:
        raise ValueError("Process arguments are truncated")
    strings = [x for x in raw[4:].split(b"\0")[1:] if x]
    if not strings:
        raise ValueError("Process title is unavailable")
    environment: dict[str, str] = {}
    for row in strings[1:]:
        key, separator, value = row.partition(b"=")
        name = key.decode(errors="replace")
        if not separator or name not in ENVIRONMENT_KEYS:
            continue
        if name in environment:
            raise ValueError("Process environment repeats " + name)
        environment[name] = value.decode()
    return strings[0].decode(), environment


def process_chain(pid: int, proc: Path = Path("/proc")) -> list[Json]:
    result: list[Json] = []
    if procfs_absent(proc):
        # macOS native desktop/CLI hosts have no procfs. Read only this ancestry.
        seen: set[int] = set()
        while pid > 1 and pid not in seen and len(result) < 64:
            seen.add(pid)
            observed = (
                subprocess.run(
                    ["ps", "-p", str(pid), "-o", "ppid=,uid=,comm="],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=True,
                )
                .stdout.strip()
                .split(None, 2)
            )
            if len(observed) != 3:
                break
            parent, uid, _title = observed
            # process.title changes ps comm/args. The kernel's executable path
            # remains the admitted binary, including native CC and Pi on macOS.
            executable = str(pidpath(pid))
            arguments = subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout
            sessions = re.findall(
                r"(?:^|\s)(--session-id|--resume)\s+([A-Za-z0-9-]{1,128})(?=\s|$)", arguments
            )
            if "--conversation" in arguments:
                # Flat ps text cannot tell an option from the same words inside
                # a prompt argument; only the kernel's argv boundaries can. An
                # unreadable, repeated or malformed option names no conversation.
                from .session_sources import conversation_option

                try:
                    conversation = conversation_option(procargs_argv(procargs(pid)))
                except (OSError, ValueError, UnicodeError):
                    conversation = None
                if conversation is not None:
                    sessions.append(("--conversation", conversation))
            result.append(
                {
                    "pid": pid,
                    "uid": int(uid),
                    "argv": [
                        executable,
                        *[
                            a
                            for a in shlex.split(arguments)[1:8]
                            if a.startswith("/")
                            or a in {"-I", "-B", "native-run", "pi", "entry"}
                            # An external entry launcher names its binding.
                            or re.fullmatch(r"[a-f0-9]{32}", a)
                        ],
                        *[part for pair in sessions for part in pair],
                    ],
                    "executable": executable,
                }
            )
            pid = int(parent)
        return result
    seen = set()
    while pid > 1 and pid not in seen and len(result) < 64:
        seen.add(pid)
        directory = proc / str(pid)
        status = dict(
            line.split(":", 1)
            for line in (directory / "status").read_text().splitlines()
            if ":" in line
        )
        argv = [x.decode() for x in (directory / "cmdline").read_bytes().split(b"\0") if x]
        result.append(
            {
                "pid": pid,
                "uid": int(status["Uid"].split()[0]),
                "argv": argv,
                "executable": str((directory / "exe").resolve())
                if (directory / "exe").exists()
                else None,
            }
        )
        pid = int(status["PPid"].strip())
    return result


def option(argv: list[str], name: str) -> str | None:
    try:
        return argv[argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


def process_environment(pid: int, proc: Path) -> dict[str, str]:
    keys = ENVIRONMENT_KEYS
    if procfs_absent(proc):
        return {key: value for key, value in os.environ.items() if key in keys}
    return {
        key.decode(): value.decode()
        for row in (proc / str(pid) / "environ").read_bytes().split(b"\0")
        if b"=" in row
        for key, value in [row.split(b"=", 1)]
        if key.decode() in keys
    }


def pi_process(
    row: Json, proc: Path, macos: bool, titles: frozenset[str] | None = None
) -> tuple[str, dict[str, str]] | None:
    """The title and environment of this account's renamed Pi process, if it is one.

    `titles` narrows which renamed processes count; on Linux the environment is
    read only for a process that already has one of them.
    """
    from .native_compute import TITLE

    titles = titles or frozenset({"pi", TITLE})
    if macos:
        if row["uid"] != os.getuid():
            return None
        try:
            title, environment = procargs_fields(procargs(row["pid"]))
        except (OSError, ValueError, UnicodeError):
            return None
    else:
        directory = proc / str(row["pid"])
        if not (directory / "comm").is_file():
            return None
        title = (directory / "comm").read_text().strip()
        if row["uid"] != os.getuid() or title not in titles:
            return None
        environment = process_environment(row["pid"], proc)
    return (title, environment) if title in titles else None


def renamed_native(
    chain: list[Json], policy: Json, proc: Path, *, dispatch_only: bool = False
) -> tuple[Json, Path] | None:
    """Pi sets process.title='pi' (the SDK runner TITLE), erasing argv after startup.

    Linux reads comm, environ and exe from procfs. macOS has no procfs: the
    title and environment come from KERN_PROCARGS2 and the executable from
    proc_pidpath; every check below is the same on both. `dispatch_only`
    considers only the SDK runner of a kernel dispatch (title harness-pi-v1),
    whose launch configuration, spawn receipt and session log are all proven;
    an older Pi CLI dispatch without that configuration never qualifies.
    """
    from .native_compute import (
        sdk_entries,
        TITLE,
        launch_config,
        session_proof,
        attempt_root,
        continuation_config,
    )

    macos = procfs_absent(proc)
    for row in chain:
        named = pi_process(row, proc, macos)
        if named is None:
            continue
        title, environment = named
        if dispatch_only and title != TITLE:
            continue
        sdk_config = None
        executable = native_executable(row["pid"], proc)
        if "HARNESS_REWRITE_DISPATCH" in environment:
            effect_id = environment["HARNESS_REWRITE_DISPATCH"]
            from .model import identity

            identity(effect_id)
            if executable != Path(policy["node"]).resolve(strict=True):
                continue
            root = Path(policy["dispatch_root"]) / effect_id
            attempt = environment.get("HARNESS_REWRITE_ATTEMPT")
            target = attempt_root(root, attempt)
            receipt_path = target / "spawn.json"
            if root.is_symlink() or receipt_path.is_symlink():
                raise Rejected("authority_denied", "Native dispatch proof redirects")
            receipt = json.loads(receipt_path.read_text())
            if (
                receipt["process"] != process_stamp(row["pid"], proc)
                or receipt["effect_id"] != effect_id
            ):
                raise Rejected(
                    "authority_denied",
                    "Native dispatch process differs from original launch",
                )
            config_path = root / "launch-config.json"
            if title == "pi" and config_path.exists():
                if json.loads(config_path.read_bytes()).get("runner"):
                    raise Rejected("authority_denied", "SDK runner cannot borrow CLI admission")
            if title == TITLE:
                try:
                    sdk_config = launch_config(root)
                    active_config = continuation_config(root, attempt) if attempt else sdk_config
                    if active_config["native_session_id"] != sdk_config["native_session_id"]:
                        raise ValueError("Continuation Session differs")
                    if (
                        sdk_config["effect_id"] != effect_id
                        or sdk_config["native_session_id"] != receipt["native_session_id"]
                        or sdk_config["argv"][0] != policy["node"]
                        or sdk_config["sdk_entry"] != str(sdk_entries(Path(policy["pi_binary"]))[0])
                        or sdk_config["http_entry"]
                        != str(sdk_entries(Path(policy["pi_binary"]))[1])
                        or sdk_config["agent_dir"] != policy["agent_config_dir"]
                    ):
                        raise ValueError("SDK original configuration differs")
                except (ValueError, OSError, KeyError) as exc:
                    raise Rejected("authority_denied", "SDK dispatch evidence differs") from exc
            files = list((root / "session").glob("*_" + receipt["native_session_id"] + ".jsonl"))
        elif dispatch_only:
            continue
        elif title == "pi" and "SCL_RELAY_HOST_CONTEXT" in environment:
            context = json.loads(environment["SCL_RELAY_HOST_CONTEXT"])
            parent = next((p for p in chain if p["pid"] == context.get("pid")), None)
            if (
                executable != Path(policy["node"]).resolve(strict=True)
                or parent is None
                or parent["uid"] != row["uid"]
                or native_executable(parent["pid"], proc)
                != Path(policy["producer_node"]).resolve(strict=True)
                or len(parent["argv"]) < 2
                or Path(parent["argv"][1]).resolve() != Path(policy["relay_entry"]).resolve()
                or context.get("schema_version") != "scl_relay_host_context/v1"
                or context.get("session_ref") != environment.get("SCL_SESSION_REF")
            ):
                raise Rejected(
                    "authority_denied",
                    "Native Pi has no matching admitted Relay ancestor",
                )
            files = []
            for profile in policy["profiles"]:
                if profile["role"] != "producer":
                    continue
                for path in Path(profile["session_root"]).rglob("*.jsonl"):
                    if path.is_symlink():
                        continue
                    with path.open() as stream:
                        header = json.loads(stream.readline())
                    if header.get("type") != "session" or not isinstance(header.get("id"), str):
                        continue
                    ref = (
                        "session_v1_"
                        + hashlib.sha256(("pulse-session-v1\0" + header["id"]).encode()).hexdigest()
                    )
                    if ref == context["session_ref"]:
                        files.append(path)
        else:
            continue
        if len(files) != 1:
            raise Rejected(
                "source_unavailable",
                "Native session must have one matching original log",
            )
        if sdk_config is not None:
            try:
                session_proof(root, files[0], sdk_config)
            except (OSError, ValueError, KeyError) as exc:
                raise Rejected("authority_denied", "SDK session evidence differs") from exc
        return row, files[0].resolve(strict=True)
    return None


def dispatch_runner(chain: list[Json], policy: Json, proc: Path) -> int | None:
    """Ancestry distance of the nearest SDK runner naming a kernel dispatch.

    Only names the candidate; `renamed_native` proves it before it counts. Only
    processes titled harness-pi-v1 are read further: another process in the
    ancestry (an older Pi, any other program) is never read here, so its
    unreadable environment cannot refuse a source nearer than it.
    """
    from .native_compute import TITLE

    if "node" not in policy:
        return None
    node = Path(policy["node"]).resolve()
    macos = procfs_absent(proc)
    for index, row in enumerate(chain):
        try:
            named = pi_process(row, proc, macos, frozenset({TITLE}))
        except OSError:
            continue  # A process whose title or environment is unreadable names no dispatch.
        if named is None or "HARNESS_REWRITE_DISPATCH" not in named[1]:
            continue
        try:
            if native_executable(row["pid"], proc) == node:
                return index
        except (OSError, ValueError):
            continue
    return None


def nearer_dispatch(
    chain: list[Json],
    policy: Json,
    proc: Path,
    locate: Callable[[list[Json], int | None], Json | None],
) -> tuple[Json | None, tuple[Json, Path] | None]:
    """The nearest session source, or the kernel-dispatched verifier nearer than it.

    The principal is the nearest admitted process in the ancestry. A verifier
    the kernel dispatched from inside an Agent's command stays that Agent's
    descendant until the command returns. The nearest SDK runner is found
    first; only processes nearer than it are candidate sources (`locate` with
    `nearer_than`), still proven against the whole chain, so a candidate that
    fails there refuses this invocation. Sources above the runner are never
    read: neither the Agent's session above it nor its failure (an entry
    binding revoked after the dispatch, a session root no longer available)
    stands in for the dispatch proof below it. The runner itself is proven in
    full (`renamed_native`: spawn receipt, launch configuration and session
    log), so no producer process becomes a verifier by environment alone.
    Returns (source, None) or (None, dispatched).
    """
    runner = dispatch_runner(chain, policy, proc)
    binary = str(Path(policy["pi_binary"]).resolve())
    if runner is None or any(
        row["uid"] == os.getuid()
        and any(str(Path(a).resolve()) == binary for a in row["argv"][:3] if a.startswith("/"))
        for row in chain[:runner]
    ):
        return locate(chain, None), None  # No runner, or a nearer Pi CLI session is the principal.
    source = locate(chain, runner)
    if source is not None:
        return source, None
    dispatched = renamed_native(chain[runner : runner + 1], policy, proc, dispatch_only=True)
    if dispatched is None:
        raise Rejected("authority_denied", "SDK dispatch evidence differs")
    return None, dispatched


class NativeHost:
    def __init__(
        self,
        policy: Path,
        *,
        proc: Path = Path("/proc"),
        pid: int | None = None,
        pre_model: bool = False,
    ):
        if (
            policy.is_symlink()
            or policy.stat().st_uid != os.getuid()
            or policy.stat().st_mode & 0o022
        ):
            raise Rejected(
                "authority_denied",
                "Host policy must be owned and not writable by other users",
            )
        self.policy = json.loads(policy.read_text())
        from .policy import execution, verification_compute

        verification_compute(self.policy)
        self.execution = execution(self.policy)
        self.proc = proc
        chain = process_chain(pid or os.getpid(), proc)
        binary = str(Path(self.policy["pi_binary"]).resolve())
        native = next(
            (
                row
                for row in chain
                if row["uid"] == os.getuid()
                and any(
                    str(Path(arg).resolve()) == binary
                    for arg in row["argv"][:3]
                    if arg.startswith("/")
                )
            ),
            None,
        )
        from . import external_entry, session_sources

        def locate(rows: list[Json], nearer_than: int | None) -> Json | None:
            # An owner-confirmed external entry launcher is its own source (AH-084).
            return external_entry.locate(
                self.policy, rows, policy.parent / "harness.pyz", nearer_than=nearer_than
            ) or session_sources.locate(
                self.policy,
                rows,
                proc,
                entry=policy.parent / "harness.pyz",
                pre_model=pre_model,
                nearer_than=nearer_than,
            )

        extra, dispatched = nearer_dispatch(chain, self.policy, proc, locate)
        if dispatched is not None:
            native = None
        if extra is not None:
            native = None
        self.runtime = extra["runtime"] if extra else "pi"
        # The entry is a session attribute recorded with its sources; it is not
        # part of the principal and grants nothing (AH-082).
        self.entry: str = extra["entry"] if extra else "cli"
        located = (
            (extra["process"], extra["path"])
            if extra
            else dispatched
            if dispatched is not None
            else None
            if native is not None
            else renamed_native(chain, self.policy, proc)
        )
        if native is None and located is not None:
            native, located_session = located
        else:
            located_session = None
        if native is None:
            raise Rejected(
                "authority_denied",
                "No admitted native process is in this invocation ancestry",
            )
        argv = native["argv"]
        path_arg = option(argv, "--session")
        session_file: Path | None
        if extra is not None and extra["path"] is None:
            # Relay-proven session before its first log row (slash input only).
            session_file = None
            matches = [extra["profile"]]
        else:
            if located_session is not None:
                session_file = located_session
            elif path_arg:
                session_file = Path(path_arg).resolve(strict=True)
            else:
                directory, session = (
                    option(argv, "--session-dir"),
                    option(argv, "--session-id"),
                )
                if not directory or not session or not re.fullmatch(r"[A-Za-z0-9-]+", session):
                    raise Rejected(
                        "source_unavailable", "Native Pi session location is unavailable"
                    )
                files = list(Path(directory).glob("*_" + session + ".jsonl"))
                if len(files) != 1:
                    raise Rejected(
                        "source_unavailable",
                        "Native session must have exactly one original log",
                    )
                session_file = files[0].resolve(strict=True)
            if extra is not None and extra["runtime"] == "external":
                matches = [extra["profile"]]
            elif dispatched is not None:
                # A proven dispatch session lies in the dispatch root and belongs
                # to a verifier profile; no producer profile is read for it.
                matches = [
                    p
                    for p in self.policy["profiles"]
                    if p["role"] == "verifier"
                    and p.get("runtime", "pi") == "pi"
                    and not p.get("standalone")
                    and contained(
                        session_file, Path(self.policy["dispatch_root"]).resolve(strict=True)
                    )
                    and contained(session_file, session_root(p))
                ]
            else:
                matches = [
                    p
                    for p in self.policy["profiles"]
                    if p.get("runtime", "pi") == self.runtime
                    and (extra is None or p == extra["profile"])
                    and contained(session_file, session_root(p))
                    and (extra is not None or not p.get("standalone"))
                ]
        if len(matches) != 1:
            raise Rejected("authority_denied", "Native session has no unique admitted profile")
        self.profile = matches[0]
        if self.profile["role"] not in {"producer", "verifier"}:
            raise Rejected("authority_denied", "Unknown native role")
        self.session_file = session_file
        self.relay_command_input: str | None = None
        self.relay_switch_phase: str | None = None
        # An Agent's own switch notice, set only by relay_channel (agent_switch.py).
        self.relay_agent_switch: Json | None = None
        native_extent: int | None = None
        if session_file is None:
            assert extra is not None
            header = extra["header"]
        else:
            with session_file.open() as stream:
                header = extra["header"] if extra else json.loads(stream.readline())
                # Reuse the authenticated descriptor's fixed extent, not a usage
                # collector. It is an upper bound, possibly including a partial
                # last row; capture still requires complete records inside it.
                try:
                    native_extent = os.fstat(stream.fileno()).st_size
                except OSError:
                    native_extent = None
        if (
            header.get("type") != "session"
            or not isinstance(header.get("id"), str)
            or option(argv, "--session-id") not in (None, header["id"])
        ):
            raise Rejected("source_invalid", "Native process and session header disagree")
        self.native_header = header
        self.native_pid: int | None = native["pid"]
        self.native_stamp: Json | None
        try:
            self.native_stamp = process_stamp(native["pid"], proc)
        except (OSError, ValueError, IndexError, UnicodeError):
            self.native_stamp = None
        self.environment = {
            k: v
            for k, v in (
                extra["environment"] if extra else process_environment(native["pid"], proc)
            ).items()
            if k == "PULSE_PI_TENANT_ID"
        }
        self.bind_context(native_extent)

    def native_log(self) -> Path:
        """The original log; absent only for Relay slash input before the first model turn."""
        if self.session_file is None:
            raise Rejected(
                "source_unavailable", "Native session has no original log before its first model turn"
            )
        return self.session_file

    def bind_context(self, native_extent: int | None) -> None:
        """One policy-to-context mapping for live and Host-attested channel sources."""
        allowed = INTENTS - WORKER_INTENTS
        if self.profile["role"] == "verifier":
            allowed = frozenset(
                {"acknowledge_verification", "record_verification", "record_quality"}
            )
        audit = self.execution["mode"] == "audit"
        if audit:
            allowed = frozenset()
        session_ref = "session." + digest(
            [
                self.policy["namespace"],
                self.profile["actor"],
                self.native_header["id"],
                self.policy["surface"],
            ]
        )
        from .notice_transport import configured_routes

        self.base = HostContext(
            self.policy["namespace"],
            self.profile["actor"],
            session_ref,
            self.policy["surface"],
            0,
            frozenset({"*"}),
            allowed,
            native_cursor=native_extent,
            read_only_reason="policy_audit" if audit else None,
            notification_routes=configured_routes(self.policy)
            if self.profile["role"] == "producer" and not audit
            else (),
            repositories=tuple(RepositoryBinding(**r) for r in self.policy.get("repositories", []))
            if self.profile["role"] == "producer" and not audit
            else (),
            publication_targets=tuple(
                PublicationTarget(**t) for t in self.policy.get("publication_targets", [])
            )
            if self.profile["role"] == "producer" and not audit
            else (),
            source_consumers=tuple(
                SourceConsumer(**c) for c in self.policy.get("source_consumers", [])
            )
            if self.profile["role"] == "producer" and not audit
            else (),
            verification_actor=self.policy.get("verification_actor")
            if self.profile["role"] == "producer" and not audit
            else None,
            entry=self.entry,
            merge_repositories=frozenset(
                self.policy.get("pull_request_gate", {}).get("repositories", [])
            )
            if self.profile["role"] == "producer" and not audit
            else frozenset(),
            agent_workspaces=agent_workspaces(self.policy)
            if self.profile["role"] == "producer"
            else None,
        )
        self.current = self.base
        self.handle = object()

    def authenticate(self, handle: object) -> HostContext:
        if handle is not self.handle:
            raise PermissionError("Unknown invocation handle")
        return replace(
            self.current,
            runtime_observations=(
                capture_native(
                    self.native_pid, self.native_stamp, self.base.principal(), self.proc
                ),
            ) if self.native_pid is not None else (),
        )

    def artifact(
        self,
        path: str,
        logical: str,
        version: str,
        access: str = "team",
        *,
        workspace_roots: tuple[Path, ...] = (),
    ) -> ArtifactInput:
        candidate = Path(path).resolve(strict=True)
        if not any(
            contained(candidate, Path(root).resolve(strict=True))
            for root in (*self.profile["artifact_roots"], *workspace_roots)
        ):
            raise Rejected("authority_denied", "Artifact is outside the native profile scope")
        if not candidate.is_file() or candidate.stat().st_size > 64 * 1024 * 1024:
            raise Rejected("source_invalid", "Expected a bounded regular artifact")
        return ArtifactInput(logical, version, candidate.read_bytes(), access)

    def human_input(self, ref: str, *, latest: bool = False) -> bytes:
        """Read authenticated ingress already persisted in this native session."""
        if self.runtime == "external":
            # Its own text is never human proof; only the owner's platform confirmation.
            from .external_entry import human_input

            return human_input(self.policy, self.native_header["id"], ref)
        if not re.fullmatch(r"[a-f0-9]{64}", ref):
            raise Rejected("source_invalid", "Expected the original Relay human input reference")
        from .session_sources import original_user, rows, user_text

        if (
            self.runtime in {"cc", "codex", "agy"} or self.profile.get("standalone")
        ) and not self.environment.get("PULSE_PI_TENANT_ID"):
            return original_user(
                self.native_log(), self.runtime, self.native_header["id"], ref, latest=latest
            )[1]
        record_path = Path(self.policy["human_inputs"]) / (ref + ".json")
        if record_path.is_symlink():
            raise Rejected("source_invalid", "Human provenance cannot redirect")
        try:
            raw = record_path.read_bytes()
        except FileNotFoundError as exc:
            raise Rejected(
                "human_input_unavailable",
                "Original channel input is not in the installed Host custody; "
                "retain and retry the same request after the Host path is repaired",
            ) from exc
        record = json.loads(raw)
        source = record["source"]
        if (
            digest(
                [
                    source["channel"],
                    source["eventId"],
                    source["humanRef"],
                    record["poolKey"],
                ]
            )
            != ref
        ):
            raise Rejected(
                "source_invalid",
                "Human source reference does not match original ingress identity",
            )
        if (
            record["poolKey"] != self.environment.get("PULSE_PI_TENANT_ID")
            or source["channel"] not in ("telegram", "feishu")
            or not source["eventId"]
            or not source["humanRef"]
        ):
            raise Rejected(
                "source_invalid",
                "Human input belongs to a different native conversation",
            )
        if self.relay_command_input == ref:
            # Only relay_channel sets this after authenticating the installed
            # Relay and its actual native child. Slash control precedes model
            # dispatch and does not require a fabricated native transcript row.
            return raw
        # Relay captures and annotates actual user ingress before dispatch. Agent
        # text and tool output cannot serve as the native human source.
        found = False
        latest_user = ""
        for _, row in rows(self.native_log()):
            message = user_text(row, self.runtime)
            if message is not None:
                latest_user = message
                if f"Host human input reference: {ref}." in latest_user:
                    found = True
        if not found:
            raise Rejected("source_invalid", "Human source has not entered this native session")
        if latest and (
            f"Host human input reference: {ref}." not in latest_user
            or source["message"] not in latest_user
        ):
            raise Rejected(
                "source_invalid",
                "Command is not the latest native human input: use the reference of the "
                "newest human message in this conversation, or ask the human to confirm again",
            )
        return raw

    def human_source(self, ref: str, intent: str, data: Json, wi: str | None) -> SourceInput:
        raw = self.human_input(ref)
        if self.runtime == "external":
            from .external_entry import confirmed_command

            confirmed_command(self.policy, self.native_header["id"], ref, intent, wi, data)
        return SourceInput(
            ref,
            "native_human",
            raw,
            intent,
            digest({"intent": intent, "work_item_id": wi, "input": data}),
        )

    def prepare(
        self,
        intent: str,
        data: Json,
        wi: str | None,
        generation: int,
        artifacts: tuple[ArtifactInput, ...],
        human_ref: str | None = None,
        git_source: Json | None = None,
    ) -> object:
        if self.execution["mode"] == "audit":
            raise Rejected("policy_read_only", "Installed audit policy permits observations only")
        if intent == "record_usage" and human_ref:
            raise Rejected("invalid_input", "Native usage cannot be supplied as human approval")
        from .native_usage import cursor

        raw = encoded(
            {
                "native_header": self.native_header,
                "entry": self.entry,
                "command": {"intent": intent, "work_item_id": wi, "input": data},
            }
        )
        source = SourceInput(
            "native." + digest(json.loads(raw)),
            "independent_result"
            if intent in {"record_verification", "record_quality"}
            else "native_agent",
            raw,
            intent if intent in {"record_verification", "record_quality"} else None,
            digest({"intent": intent, "work_item_id": wi, "input": data})
            if intent in {"record_verification", "record_quality"}
            else None,
        )
        if intent == "record_task_result":
            source = replace(source, reference=data["observation_ref"])
        if intent in {"grant_access", "revoke_access"}:
            # Access follows the human's latest word in this same conversation.
            # A standing read request may instead match the family rule, and a
            # revocation only narrows access; the kernel decides both.
            if not human_ref and not (
                intent == "revoke_access" or isinstance(data, dict) and data.get("standing") is True
            ):
                raise Rejected(
                    "human_decision_required",
                    "Session access needs the human's approval in this conversation",
                )
            if human_ref:
                self.human_input(human_ref, latest=True)
        if intent == "grant_merge_authority":
            # A standing merge authority follows the human's latest word, so an
            # earlier approval cannot outlive a later revocation.
            if not human_ref:
                raise Rejected(
                    "human_decision_required",
                    "A standing merge authority needs the human's approval in this conversation",
                )
            self.human_input(human_ref, latest=True)
        if human_ref:
            # A WorkItem the Agent creates from the owner's discussion carries
            # that input; creation is the only human-sourced intent without a WI.
            if self.profile["role"] != "producer":
                raise Rejected(
                    "authority_denied",
                    "This native profile cannot consume a human decision",
                )
            if wi is None and intent not in {"create_draft", "create_lightweight"}:
                raise Rejected(
                    "authority_denied",
                    f"{intent} without a work_item_id takes no human_input_ref; omit it"
                    + (" (a child inherits its parent's decisions)" if intent == "create_child" else ""),
                )
            source = self.human_source(human_ref, intent, data, wi)
        usage_snapshot = None
        if intent in {"record_usage", "open_usage_interval"} and self.session_file is None:
            raise Rejected(
                "measurement_source_unavailable",
                "Native session has no original log before its first model turn",
            )
        if intent in {"record_usage", "open_usage_interval"} and self.runtime != "pi":
            raise Rejected(
                "measurement_source_unavailable",
                "Use the native runtime observation source; business tools remain available",
            )
        if intent == "record_usage":
            from . import native_usage
            from .model import fields

            fields(data, {"interval_id", "task_attempt", "start_byte", "end_byte"})
            if human_ref or self.profile["role"] != "producer" or wi is None:
                raise Rejected(
                    "authority_denied",
                    "Native usage is an observation by its actual executor",
                )
            captured, usage_snapshot = native_usage.capture(
                self.native_log(),
                self.native_header,
                data["start_byte"],
                data["end_byte"],
            )
            artifacts += (captured,)
            source = SourceInput(
                "native-usage." + digest([self.base.principal(), usage_snapshot]),
                "native_usage",
                encoded(usage_snapshot),
                intent,
                digest({"intent": intent, "work_item_id": wi, "input": data}),
            )
        session_compute = review = pull_request = None
        from .pull_requests import COMPUTE_INTENTS

        if intent in COMPUTE_INTENTS and self.profile["role"] == "producer":
            # Model and effort come from this session's own record, never input.
            from . import session_compute as observed

            session_compute = self.subagent_sessions(
                observed.observe(self.runtime, self.session_file)
            )
            if intent == "submit_pull_request" and isinstance(data, dict) and isinstance(
                data.get("head_sha"), str
            ):
                # Confirm commit candidates against the repository itself.
                session_compute = observed.verify_commits(
                    session_compute, data["head_sha"], self.session_file)
            if intent == "record_pull_request_review":
                review = observed.review_profile(self.policy, self.runtime)
            gate = self.policy.get("pull_request_gate")
            if intent == "submit_pull_request" and gate and isinstance(data, dict):
                from .github_merge import PullRequestMerge

                repo, number = data.get("repository"), data.get("number")
                if isinstance(repo, str) and repo in gate["repositories"] and type(number) is int:
                    pull_request = PullRequestMerge(gate, self.policy["python"]).observe(
                        repo, number
                    )
                if pull_request is not None:
                    pull_request = {**pull_request, "authors": self.pull_request_authors(
                        pull_request["commits"], observed
                    )}
        self.current = replace(
            self.base,
            generation=generation,
            artifacts=artifacts,
            source=source,
            git_source=git_source,
            usage_snapshot=usage_snapshot,
            session_compute=session_compute,
            review_profile=review,
            pull_request_observation=pull_request,
            # Only explicit interval opening requires a complete-row cursor.
            # Business preparation never scans or waits for metering; a missing
            # ending observation cannot veto an otherwise authorized decision.
            native_cursor=cursor(self.native_log())
            if intent == "open_usage_interval"
            else self.base.native_cursor,
        )
        return self.handle

    def subagent_sessions(self, compute: Json | None) -> Json | None:
        """Give each proven sub-agent of this session its kernel session reference."""
        if compute is None or not compute.get("subagents"):
            return compute
        return {**compute, "subagents": [
            {**child, "session": native_session_ref(
                self.policy["namespace"], self.profile["actor"], child["native_id"],
                self.policy["surface"],
            )}
            for child in compute["subagents"]
        ]}

    def pull_request_authors(self, commits: list[str], observed: Any) -> list[Json]:
        """The producing session and model of every commit the PR introduces.

        Searches this Agent's own CC, Codex, Pi and agy session roots from the installed
        policy (a profile without `runtime` is Pi); each candidate passes the same
        repository and reflog check.
        """
        from datetime import datetime, timezone

        actor = self.profile["actor"]
        roots = list(dict.fromkeys(
            (str(p.get("runtime", "pi")), Path(p["session_root"]))
            for p in self.policy["profiles"]
            if p["actor"] == actor and p["role"] == "producer"
            and p.get("runtime", "pi") in observed.RUNTIMES
        ))
        found = observed.find_authors(
            roots, commits, datetime.now(timezone.utc).timestamp() - 60 * 24 * 3600
        )
        return [
            {
                "sha": sha,
                "session": native_session_ref(
                    self.policy["namespace"], actor, entry["native_id"], self.policy["surface"]
                ),
                "model": entry["model"],
                "declared_wis": entry["declared_wis"],
                **({"session_declared_wis": entry["session_declared_wis"]}
                   if "session_declared_wis" in entry else {}),
                "wi_marker": entry["wi_marker"],
                **({"request_declared_wis": entry["request_declared_wis"]}
                   if "request_declared_wis" in entry else {}),
                **{k: entry[k] for k in observed.DISPATCH if k in entry},
            }
            for sha, entries in found.items()
            for entry in entries
        ]

    def worker(self, artifacts: tuple[ArtifactInput, ...]) -> object:
        self.current = replace(
            self.base,
            actor="delivery-worker",
            session="delivery-service",
            allowed_intents=WORKER_INTENTS if self.execution["mode"] != "audit" else frozenset(),
            artifacts=artifacts,
            effect_kinds=frozenset(
                {
                    "deliver_artifact",
                    "notification_send",
                    "verification_dispatch",
                    "workspace_allocate",
                    "workspace_revise",
                    "workspace_retire",
                    "publish_source",
                    "adopt_source",
                }
                # Kernel merge only where the operator admitted GitHub access.
                | ({"merge_pull_request"} if "pull_request_gate" in self.policy else set())
            )
            if self.execution["mode"] != "audit"
            else frozenset(),
            verification_actor=None,
        )
        return self.handle
