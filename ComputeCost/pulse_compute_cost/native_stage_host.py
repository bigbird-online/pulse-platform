"""Use the installed native Host in process, never a caller's identity JSON.

This measurement adapter never changes SCL execution admission. Saved usage is
read through the installed read-only CLI, not a second controller. Its destination
is the existing source registry in the admitted native parent's launch environment.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile

from .receipts import ReceiptError, SUPPORTED_RECEIPT_FEATURES
from .stage_record import StageSourceError, record_stage, source_projection, validate_range


# Fixed causes from the installed capture and source projection. Anything
# else is reported as unclassified rather than echoed.
CAPTURE_CAUSES = frozenset({
    "invalid_range", "source_missing", "path_mismatch", "owner_mismatch", "header_invalid",
    "header_identity_mismatch", "range_not_row_boundary", "incomplete_tail", "record_invalid",
    "session_header_in_range", "activity_invalid", "activity_count_out_of_bounds",
})


PROCFS = Path("/proc")
REGISTRY_ENTRY = b"PULSE_COMPUTECOST_SOURCES="


def _darwin_procargs(pid: int) -> bytes:
    """The kernel's KERN_PROCARGS2 copy of one same-account process's exec block."""
    import ctypes
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    libc.sysctl.restype = ctypes.c_int
    def call(*mib, buffer=None, size=0):
        names = (ctypes.c_int * len(mib))(*mib)
        length = ctypes.c_size_t(size)
        if libc.sysctl(names, len(mib), buffer, ctypes.byref(length), None, 0) != 0:
            raise OSError(ctypes.get_errno(), "sysctl")
        return length.value
    limit = ctypes.c_int(0)
    call(1, 8, buffer=ctypes.byref(limit), size=ctypes.sizeof(limit))  # CTL_KERN, KERN_ARGMAX
    buffer = ctypes.create_string_buffer(limit.value)
    used = call(1, 49, pid, buffer=buffer, size=limit.value)  # KERN_PROCARGS2
    return buffer.raw[:used]


def darwin_environment(raw: bytes) -> list[bytes]:
    """Launch environment from KERN_PROCARGS2: argc, exec path, argv, then env.

    Exactly argc argument strings are skipped, so an argument that looks like
    an assignment is never read as environment. Parsing stops at the first
    empty string, before the kernel's private apple[] strings.
    """
    if len(raw) < 4:
        raise ValueError("procargs")
    argc = int.from_bytes(raw[:4], sys.byteorder)
    end = raw.find(b"\0", 4)
    if end < 0:
        raise ValueError("procargs")
    position = end
    while position < len(raw) and raw[position] == 0:
        position += 1
    strings = raw[position:].split(b"\0")
    if len(strings) <= argc:
        raise ValueError("procargs")
    environment = []
    for entry in strings[argc:]:
        if not entry:
            break
        environment.append(entry)
    return environment


def launch_environment(pid: int, proc: Path) -> list[bytes]:
    """The admitted native process's own launch environment, never the caller's.

    Linux reads procfs. macOS has none; it reads the same exec-time block
    through sysctl KERN_PROCARGS2, which the kernel serves only to the same
    account (or root). Both fail closed when the process is unreadable.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise ValueError("native process")
    if proc == PROCFS and not proc.is_dir():
        return darwin_environment(_darwin_procargs(pid))
    return (proc / str(pid) / "environ").read_bytes().split(b"\0")


def capture_failure(cause) -> str:
    reason = cause if isinstance(cause, str) and cause in CAPTURE_CAUSES else "unclassified"
    return f"native stage source capture failed (reason={reason}); retain the original range"


class NativeStageHost:
    def __init__(self, host, capture, *, guard=lambda: None, inspect=None, authority_entry=None):
        self.host, self._capture, self._guard = host, capture, guard
        self._inspect = inspect
        self._authority_entry = authority_entry

    def capture(self, source_range):
        start, end = validate_range(source_range)
        self._guard()
        try:
            artifact, _ = self._capture(self.host.session_file, self.host.native_header, start, end)
            source = source_projection(self.host.session_file, self.host.native_header, artifact.content,
                start=start, end=end, principal=self.host.base.principal(),
                observed_at=datetime.now(timezone.utc).isoformat())
        except ReceiptError as exc:
            if not isinstance(exc, StageSourceError):
                raise
            raise ReceiptError(capture_failure(exc.cause)) from exc
        except Exception as exc:
            # No raw native messages, paths or private policy escape on failure;
            # only the installed capture's fixed cause name is reported.
            raise ReceiptError(capture_failure(getattr(exc, "cause", None))) from exc
        self._guard()
        return source

    def registry(self):
        self._guard()
        # Invocation environment is model-modifiable; the admitted Pi parent's
        # launch environment supplies this operator binding. No path CLI flag.
        try:
            entries = launch_environment(self.host.native_pid, self.host.proc)
            values = [entry[len(REGISTRY_ENTRY):].decode() for entry in entries
                      if entry.startswith(REGISTRY_ENTRY)]
            if len(values) != 1 or not values[0]:
                raise ValueError("unregistered")
            path = Path(values[0])
            info = path.lstat()
            if (not path.is_absolute() or path.resolve() != path or path.is_symlink() or not path.is_file()
                    or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 1024 * 1024):
                raise ValueError("private registry")
            registry = json.loads(path.read_bytes())
            if registry.get("schema") != "computecost_source_registry/v1" or not isinstance(registry.get("sources"), list):
                raise ValueError("registry")
            features = registry.get("required_receipt_features")
            if (not isinstance(features, list) or "stage_semantics.v1" not in features
                    or any(not isinstance(f, str) or f not in SUPPORTED_RECEIPT_FEATURES for f in features)):
                raise ValueError("stage readers not admitted")
            from .source_registration import stage_ledger_root
            stage_ledger_root(registry)
            if self._authority_entry is not None and registry.get("observation_core_entrypoint") != self._authority_entry:
                raise ValueError("production observation authority differs")
        except (OSError, ValueError, TypeError, UnicodeError, AttributeError) as exc:
            raise ReceiptError("native stage ledger is not safely registered by the Host") from exc
        self._guard()
        return registry, path

    def ledger(self):
        registry, _ = self.registry()
        if self._authority_entry is not None and registry.get("observation_core_entrypoint") != self._authority_entry:
            raise ReceiptError("native stage requires the same installed production observation authority")
        from .observation_authority import registered_ledger
        return registered_ledger(registry, guard=self._guard)

    def import_usage(self, wi, record_id):
        from .native_scl_usage import validate_selection, saved_record, import_saved_usage
        validate_selection(wi, record_id)
        if (self.host.profile['role'] != 'producer' or self.host.execution['mode'] == 'audit'
                or self._inspect is None):
            raise ReceiptError('saved usage import is unavailable for this native profile')
        ledger = self.ledger()  # Missing binding fails before private original reads.
        self._guard()
        query = self._inspect(wi, 'measurements')
        row = saved_record(query, wi, record_id)
        original = self._inspect(wi, 'artifact', row['evidence'])
        try:
            item = original['result']['artifact']
            if (original['status'] != 'read_only' or original.get('error') is not None
                    or original['work_item']['id'] != wi or item['reference'] != row['evidence']):
                raise ValueError('artifact')
            raw = base64.b64decode(item['base64'], validate=True)
            source_proof = None
            if 'native_usage_sources' in item:
                proofs = item['native_usage_sources']
                if not isinstance(proofs, list) or any(not isinstance(p, dict) for p in proofs):
                    raise ValueError('sources')
                source_proof, = [p for p in proofs if p.get('record_id') == record_id]
        except (KeyError, ValueError, TypeError) as exc:
            raise ReceiptError('saved native usage original is unavailable') from exc
        self._guard()
        if self.ledger().root != ledger.root:
            raise ReceiptError('native stage destination changed; preserve the original command')
        return import_saved_usage(ledger, query, wi, record_id, raw,
            path=self.host.session_file, header=self.host.native_header,
            principal=self.host.base.principal(), observed_at=datetime.now(timezone.utc).isoformat(),
            source_proof=source_proof)

    def import_assessments(self, wi, candidate_id):
        from .native_output_assessment import validate_selection, import_assessments
        validate_selection(wi, candidate_id)
        if (self.host.profile['role'] != 'producer' or self.host.execution['mode'] == 'audit'
                or self._inspect is None):
            raise ReceiptError('assessment import is unavailable for this native profile')
        ledger = self.ledger()
        self._guard()
        query = self._inspect(wi, 'measurements')
        self._guard()
        if self.ledger().root != ledger.root:
            raise ReceiptError('native stage destination changed; preserve the original command')
        return import_assessments(ledger, query, wi, candidate_id,
            observed_at=datetime.now(timezone.utc).isoformat())

    def record(self, command):
        if self.host.profile["role"] != "producer" or self.host.execution["mode"] == "audit":
            raise ReceiptError("stage saving is unavailable for this native profile")
        if not isinstance(command, dict) or "source" not in command:
            raise ReceiptError("stage command requires a bounded source")
        ledger = self.ledger()
        source = self.capture(command["source"])
        self._guard()
        if self.ledger().root != ledger.root:
            raise ReceiptError("native stage destination changed; preserve the original command")
        return record_stage(ledger, source, command)


def installed_host():
    """Admit the exact installed package before importing its authentication code.

    PULSE_WORK_ITEM_CORE_HOST is the existing operator-selected public entry.
    NativeHost then checks real process ancestry/session/profile. No business
    command may substitute a policy, session path, identity or store.
    """
    try:
        config = json.loads(os.environ["PULSE_WORK_ITEM_CORE_HOST"])
        selected = Path(config["entrypoint"])
        if not selected.is_absolute() or selected.name != "harness.pyz" or selected.parent.name != "active":
            raise ValueError("entry")
        entry = selected.resolve(strict=True)
        root = selected.parent.parent.resolve(strict=True)
        if entry.parent.parent != root / "releases":
            raise ValueError("release")
        receipt_path = entry.parent / "installation.json"
        receipt = json.loads(receipt_path.read_bytes())
        expected = receipt["bundle_sha256"]
        if not isinstance(expected, str) or expected != sha256(entry.read_bytes()).hexdigest():
            raise ValueError("program digest")
        prefix = str(entry) + "/"
        for name, module in tuple(sys.modules.items()):
            if name == "harness_rewrite" or name.startswith("harness_rewrite."):
                if not str(getattr(module, "__file__", "")).startswith(prefix):
                    raise ValueError("foreign native modules")
        sys.path.insert(0, str(entry))
        try:
            native = importlib.import_module("harness_rewrite.native_host")
            usage = importlib.import_module("harness_rewrite.native_usage")
            installation = importlib.import_module("harness_rewrite.production_install")
        finally:
            sys.path.remove(str(entry))
        host = native.NativeHost(entry.parent / "host-policy.json")
        if "production_ledger" not in host.policy:
            raise ValueError("not production installation")
        def guard():
            try:
                installation.require_active_program(entry, host.policy)
            except Exception as exc:
                raise ReceiptError("native program changed; retry through the current installed entry") from exc
        guard()
        def inspect(wi, view, artifact_ref=None):
            # Only fixed installed read operations. No raw output/error is sent
            # to the model, persisted, or accepted as a business input file.
            guard()
            try:
                with tempfile.TemporaryDirectory(prefix='computecost-native-read-') as folder:
                    args = [config['python'], '-I', '-B', str(selected), 'inspect', '--wi', wi, '--view', view]
                    if artifact_ref is not None:
                        ref = Path(folder) / 'reference.json'
                        ref.write_text(json.dumps(artifact_ref), encoding='utf-8')
                        ref.chmod(0o600)
                        args += ['--artifact-ref', str(ref)]
                    result = subprocess.run(args, capture_output=True, timeout=120)
                if result.returncode or len(result.stdout) > 64 * 1024 * 1024:
                    raise ValueError('read')
                response = json.loads(result.stdout)
                if response.get('status') != 'read_only' or response.get('error') is not None:
                    raise ValueError('read')
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                raise ReceiptError('installed native usage read failed; retain the original record selection') from exc
            guard()
            return response
        return NativeStageHost(host, usage.capture, guard=guard, inspect=inspect, authority_entry=str(selected))
    except ReceiptError:
        raise
    except Exception as exc:
        raise ReceiptError("installed native stage Host is unavailable or not admitted") from exc
