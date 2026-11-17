"""Fixed SDK runner bytes and original dispatch selection proof."""

from __future__ import annotations

import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path

from .model import Json
from .runtime_presence import PROC, procfs_absent
from .policy import verification_compute

RUNNER = "verifier-runner.mjs"
LAUNCH_FENCE = "harness.verifier_launch_fence/v1"
TITLE = "harness-pi-v1"


def sdk_entries(pi_binary: Path) -> tuple[Path, Path]:
    """Resolve only modules belonging to the already admitted Pi installation."""
    directory = pi_binary.resolve().parent
    distribution = directory.parent if directory.name == "bundle" else directory
    return directory / "index.js", distribution / "core/http-dispatcher.js"


def runner_bytes() -> bytes:
    return files("harness_rewrite").joinpath(RUNNER).read_bytes()


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def launch_config(root: Path) -> Json:
    for name in ("launch-config.json", RUNNER, "candidate.json", "launch.json"):
        if (root / name).resolve(strict=True) != root / name:
            raise ValueError("SDK dispatch proof redirects")
    config = json.loads((root / "launch-config.json").read_bytes())
    if config.get("runner") != "pi-sdk/v1":
        raise ValueError("Unsupported SDK dispatch")
    expected = (root / RUNNER).read_bytes()
    # Reviewed previous runner remains valid evidence for in-flight assignments.
    # It is never copied/modified or launched as the new continuation adapter.
    admitted = {checksum(runner_bytes()), "7e63d225ff8018d684d383b804f9163510155018d7cbb686baafe1d99b54c171",
                "3986213b8219da6670b4449a5d82d606c6b6878178522d5565670e538f090552"}
    if (
        checksum(expected) not in admitted
        or config["runner_sha256"] != checksum(expected)
        or config["argv"] != [config["argv"][0], str(root / RUNNER)]
        or not Path(config["argv"][0]).is_absolute()
    ):
        raise ValueError("SDK runner differs from installed resource")
    selection = verification_compute({"verification_compute": config["selection"]})
    if "bash" not in selection["tools"]:
        raise ValueError("Verifier tools cannot submit native results")
    return dict(config)


def session_proof(root: Path, path: Path, config: Json) -> None:
    if path.resolve(strict=True) != path or path.parent != root / "session":
        raise ValueError("SDK session redirects")
    with path.open() as stream:
        header = json.loads(stream.readline())
        if (
            header.get("type") != "session"
            or header.get("id") != config["native_session_id"]
        ):
            raise ValueError("SDK session differs from launch")
        # SDK records configuration before its first prompt. Do not inspect
        # unrelated session content or accept later model changes as this proof.
        rows = []
        for _ in range(8):
            line = stream.readline()
            if not line:
                break
            rows.append(json.loads(line))
    selection = config["selection"]
    proof = {
        "selection": selection,
        "runner_sha256": config["runner_sha256"],
        "config_sha256": checksum((root / "launch-config.json").read_bytes()),
    }
    if not (
        any(
            r.get("type") == "model_change"
            and r.get("provider") == selection["provider"]
            and r.get("modelId") == selection["model"]
            for r in rows
        )
        and any(
            r.get("type") == "thinking_level_change"
            and r.get("thinkingLevel") == selection["thinking"]
            for r in rows
        )
        and any(
            r.get("type") == "custom"
            and r.get("customType") == "harness.verification-compute/v1"
            and r.get("data") == proof
            for r in rows
        )
    ):
        raise ValueError("SDK native computation evidence differs")


def attempt_root(root: Path, attempt: str | None) -> Path:
    import re
    if attempt is None:
        return root
    if not re.fullmatch(r"[0-9]{6}", attempt) or int(attempt) < 1:
        raise ValueError("Invalid native continuation attempt")
    target = root / "attempts" / attempt
    if target.resolve(strict=True) != target:
        raise ValueError("Native continuation redirects")
    return target


def write_exclusive(path: Path, data: bytes) -> None:
    """Create `path` holding `data` or nothing: write a private temporary file,
    fsync it, then hard-link it into place. The link fails with FileExistsError
    when `path` exists, the same exclusivity as O_EXCL, and the name never
    appears empty or partly written."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink()
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def empty_proof_unspawned(proc: Path = PROC) -> bool:
    """Whether an empty spawn.json proves that its wrapper never exec'd.

    Wrappers since write_exclusive never leave an empty proof; only an older
    wrapper did, between its O_EXCL create and its write. On a host without
    procfs (macOS) that older wrapper's process stamp always raised before the
    write, so it could never reach exec, however long it pauses: the empty file
    is proof. With procfs the older wrapper may only be paused and could still
    write and exec, so the empty file proves nothing there.
    """
    return procfs_absent(proc)


def unspawned(spawn: Path, proc: Path = PROC) -> bool:
    """Whether a present spawn.json proves its native process never started:
    a recovery no-spawn fence, or an empty proof where empty_proof_unspawned."""
    raw = spawn.read_bytes()
    return (empty_proof_unspawned(proc) if not raw
            else json.loads(raw).get("schema") == LAUNCH_FENCE)


def continuation_config(root: Path, attempt: str) -> Json:
    original = launch_config(root)
    target = attempt_root(root, attempt)
    for name in ("launch-config.json", RUNNER, "launch.json"):
        if (target / name).resolve(strict=True) != target / name:
            raise ValueError("Continuation proof redirects")
    config = json.loads((target / "launch-config.json").read_bytes())
    expected = {**original, "argv": [original["argv"][0], str(target / RUNNER)],
                "runner_sha256": checksum(runner_bytes()),
                "original_config_sha256": checksum((root / "launch-config.json").read_bytes()),
                "session_file": config.get("session_file"), "attempt": attempt}
    if config != expected or (target / RUNNER).read_bytes() != runner_bytes():
        raise ValueError("Continuation differs from the fixed assignment")
    if config['session_file'] is None:
        predecessors = [root, *(p for p in sorted((root / 'attempts').iterdir()) if p.name < attempt)]
        if (list((root / 'session').glob('*_' + original['native_session_id'] + '.jsonl'))
                or not all(unspawned(p / 'spawn.json') for p in predecessors)):
            raise ValueError('First native execution is not proven absent')
    else:
        session_proof(root, Path(config["session_file"]), original)
    return dict(config)
