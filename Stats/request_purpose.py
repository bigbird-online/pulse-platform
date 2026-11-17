"""Enroll the selected, already-installed native purpose reader and its Hook.

Does not trust a Hook, change sandbox settings, replace lifecycle hooks, restart
Codex, collect usage, or edit a ledger. Native trust/real invocation is a separate
readback; configuration alone is not successful consumer activation.
"""
from __future__ import annotations
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import tempfile

LABEL='Recording native request cost purpose'

def sha(raw):
    return hashlib.sha256(raw).hexdigest()

def _bytes(path):
    if path.is_symlink() or path.resolve()!=path:
        raise ValueError('indirect native configuration path')
    return path.read_bytes() if path.exists() else None

def _atomic(path, raw, mode=0o600):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.request-purpose-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as out:
            os.fchmod(out.fileno(),mode);out.write(raw);out.flush();os.fsync(out.fileno())
        os.replace(temp,path)
    finally:
        Path(temp).unlink(missing_ok=True)

def plan(registry,hooks,*,home,runtime,python,site):
    """Preserve every other source and hook; replace only our own exact slot."""
    if (not home.is_absolute() or not runtime.is_absolute() or not python.is_absolute()
            or registry.get('schema')!='computecost_source_registry/v1'
            or not isinstance(registry.get('sources'),list)):
        raise ValueError('explicit reader and source registry required')
    result=deepcopy(registry);config=deepcopy(hooks)
    sources=[s for s in result['sources'] if s.get('kind')=='codex' and s.get('node')==site]
    if len(sources)!=1:
        raise ValueError('native request source is not unique for this site')
    if sources[0].get('session_wi_mcp_since') is not None:
        raise ValueError('native MCP source cannot reenroll the retired script Hook')
    spool=home/'.local/state/pulse/compute-cost/native-requests'
    sources[0]['request_purpose_directory']=str(spool)
    event=config.setdefault('hooks',{}).setdefault('UserPromptSubmit',[])
    if not isinstance(event,list):raise ValueError('invalid existing Hook configuration')
    handler={'type':'command','command':shlex.join([str(python),str(runtime/'ComputeCost/runtime/codex-request-purpose.py'),'hook']),
             'timeout':5,'statusMessage':LABEL,'additionalContextLimit':700}
    slots=[]
    for group in event:
        if not isinstance(group,dict) or not isinstance(group.get('hooks'),list):
            raise ValueError('invalid native Hook matcher group')
        for index,old in enumerate(group['hooks']):
            if old.get('statusMessage')==LABEL:
                if (old.get('type')!='command' or 'codex-request-purpose.py' not in old.get('command','')
                        or len(group)!=1):
                    raise ValueError('native purpose Hook slot was independently changed')
                slots.append((group,index))
    if len(slots)>1:raise ValueError('duplicate native purpose Hooks')
    if slots:slots[0][0]['hooks'][slots[0][1]]=handler
    else:event.append({'hooks':[handler]})
    return result,config

def install(home,runtime,python,revision,*,expected_registry_sha256,expected_hooks_sha256,site='ownermac'):
    """CAS installation under one local lock, with byte-exact owned rollback."""
    home=Path(home).resolve();runtime=Path(runtime).resolve();python=Path(python)
    current=home/'.local/share/pulse/stats/current'
    marker=runtime/'.stats-source-revision'
    if (current.resolve()!=runtime or marker.read_text().strip()!=revision
            or not (runtime/'ComputeCost/pulse_compute_cost/native_request_purpose.py').is_file()
            or not (runtime/'ComputeCost/runtime/codex-request-purpose.py').is_file()
            or not python.is_file()):
        raise ValueError('the compatible exact native reader is not active')
    state=home/'.local/state/pulse/stats';state.mkdir(mode=0o700,parents=True,exist_ok=True)
    registry=home/'.local/state/pulse/compute-cost/sources.json';hooks=home/'.codex/hooks.json'
    lock=state/'.request-purpose-install.lock'
    if lock.is_symlink():raise ValueError('unsafe installation lock')
    with lock.open('a+b') as handle:
        fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        original=[_bytes(registry),_bytes(hooks)]
        if original[0] is None:raise ValueError('no enrolled source registry')
        if [sha(raw) if raw is not None else None for raw in original] != [expected_registry_sha256,expected_hooks_sha256]:
            raise ValueError('native configuration changed after review')
        updated=plan(json.loads(original[0]),json.loads(original[1]) if original[1] is not None else {},
                     home=home,runtime=runtime,python=python,site=site)
        after=[(json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode() for value in updated]
        modes=[p.stat().st_mode & 0o777 if p.exists() else 0o600 for p in (registry,hooks)]
        spool=home/'.local/state/pulse/compute-cost/native-requests'
        if spool.is_symlink() or spool.resolve()!=spool:raise ValueError('unsafe native request spool')
        spool.mkdir(mode=0o700,parents=True,exist_ok=True)
        if spool.stat().st_uid!=os.getuid() or spool.stat().st_mode & 0o077:
            raise ValueError('native request spool is not private')
        backup=state/('request-purpose-before-'+revision+'.json')
        if not backup.exists():
            _atomic(backup,json.dumps({'source_revision':revision,'registry':json.loads(original[0]),
                    'hooks':json.loads(original[1]) if original[1] else None},ensure_ascii=False).encode())
        written=[]
        try:
            for index,path in enumerate((registry,hooks)):
                if _bytes(path)!=original[index] or current.resolve()!=runtime or marker.read_text().strip()!=revision:
                    raise ValueError('reader or native configuration changed during adoption')
                if original[index]!=after[index]:
                    _atomic(path,after[index],modes[index]);written.append((index,path))
            if any(_bytes(path)!=raw for path,raw in zip((registry,hooks),after)) or current.resolve()!=runtime:
                raise ValueError('native enrollment readback differs')
        except Exception:
            for index,path in reversed(written):
                if _bytes(path)!=after[index]:
                    raise RuntimeError('rollback pending: native configuration has another writer') from None
                if original[index] is None:path.unlink()
                else:_atomic(path,original[index],modes[index])
            raise
        return {'source_revision':revision,'status':'configured_pending_native_trust_and_invocation',
                'source_registry_sha256':sha(after[0]),'hooks_sha256':sha(after[1]),
                'native_trust_changed':False,'existing_hooks_preserved':True,'production_ledger_changed':False}


def mcp_instructions(python, script):
    """Read the actual server's short rule through standard MCP initialization."""
    import subprocess

    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}
    completed = subprocess.run([str(python), "-I", "-B", str(script)], input=json.dumps(request) + "\n", text=True, capture_output=True, check=True, timeout=15)
    message = json.loads(completed.stdout)
    result = message.get("result", {})
    rule = result.get("instructions")
    if (message.get("jsonrpc") != "2.0" or message.get("id") != 1
            or result.get("serverInfo", {}).get("name") != "pulse-request-purpose"
            or not isinstance(rule, str) or not 0 < len(rule) <= 512 or "\0" in rule):
        raise ValueError("native MCP instruction is unavailable")
    return rule


def install_mcp(
    home,
    runtime,
    python,
    revision,
    *,
    expected_config_sha256,
    expected_tool_revision=None,
):
    """Configure the stateless tool and its short native rule; accounting activation is separate.

    Accept a staged immutable release, not a mutable worktree. Do not change
    native trust, lifecycle hooks, the publisher, source registry or usage.
    """
    import re
    import tomllib

    home, runtime, python = Path(home), Path(runtime), Path(python)
    if (
        not re.fullmatch(r"[0-9a-f]{40}", revision)
        or runtime != home / ".local/share/pulse/stats/releases" / revision
        or runtime.is_symlink()
        or runtime.resolve() != runtime
        or python != home / ".local/share/coevolution/python-env/bin/python"
        or not python.is_file()
    ):
        raise ValueError("exact staged source and canonical Python required")
    if (runtime / ".stats-source-revision").read_text().strip() != revision:
        raise ValueError("staged source marker mismatch")
    inventory = json.loads((runtime / ".stats-files.json").read_text())
    files = {
        name: hashed
        for name, hashed in inventory.items()
        if name.startswith("ComputeCost/")
    }
    required = {
        "ComputeCost/runtime/codex-mcp-request-purpose.py",
        "ComputeCost/pulse_compute_cost/mcp_request_purpose.py",
    }
    if not required <= set(files):
        raise ValueError("staged native MCP tool is incomplete")
    for name, hashed in files.items():
        path = runtime / name
        if (
            ".." in Path(name).parts
            or path.is_symlink()
            or path.resolve() != path
            or sha(path.read_bytes()) != hashed
        ):
            raise ValueError("staged native MCP tool integrity differs")
    path = home / ".codex/config.toml"
    state = home / ".local/state/pulse/stats"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = state / ".request-purpose-install.lock"
    if lock.is_symlink():
        raise ValueError("unsafe installation lock")
    with lock.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = _bytes(path)
        if original is None or sha(original) != expected_config_sha256:
            raise ValueError("native MCP config changed after review")
        config = tomllib.loads(original.decode())
        name = "pulse-request-purpose"
        # Codex starts the tool by path in every new session. A pinned release
        # path dies when that release is retired; the selector always names
        # the active release, so later deploys need no reinstallation.
        selector = home / ".local/share/pulse/stats/current"
        if not selector.is_symlink() or selector.resolve() != runtime:
            raise ValueError("staged release must be the selected runtime")
        desired = {
            "command": str(python),
            "args": [str(selector / "ComputeCost/runtime/codex-mcp-request-purpose.py")],
        }
        existing = config.get("mcp_servers", {}).get(name)
        changed = existing != desired
        rule = mcp_instructions(python, runtime / "ComputeCost/runtime/codex-mcp-request-purpose.py")
        instruction_changed = "developer_instructions" not in config
        if not instruction_changed and config["developer_instructions"] != rule:
            raise ValueError("existing native instructions require an explicit merge; not overwritten")

        def block_for(slot):
            return (
                "\n[mcp_servers.pulse-request-purpose]\ncommand = "
                + json.dumps(slot["command"])
                + "\nargs = "
                + json.dumps(slot["args"])
                + "\n"
            ).encode()

        prior_block = None
        if existing is not None and changed:
            if not isinstance(expected_tool_revision, str) or not re.fullmatch(
                r"[0-9a-f]{40}", expected_tool_revision
            ):
                raise ValueError(
                    "native MCP slot is already owned or configured differently"
                )
            prior = {
                "command": str(python),
                "args": [
                    str(
                        runtime.parent
                        / expected_tool_revision
                        / "ComputeCost/runtime/codex-mcp-request-purpose.py"
                    )
                ],
            }
            prior_block = block_for(prior)
            if existing != prior or original.count(prior_block) != 1:
                raise ValueError(
                    "native MCP slot is already owned or configured differently"
                )
        block = block_for(desired)
        after = (
            original.replace(prior_block, block, 1)
            if prior_block is not None
            else original + block
            if changed
            else original
        )
        if instruction_changed:
            after = ("developer_instructions = " + json.dumps(rule, ensure_ascii=False) + "\n").encode() + after
        parsed = tomllib.loads(after.decode())
        parsed["mcp_servers"].pop(name)
        before = deepcopy(config)
        if existing is not None:
            before["mcp_servers"].pop(name)
        if not parsed["mcp_servers"] and "mcp_servers" not in before:
            parsed.pop("mcp_servers")
        if parsed.pop("developer_instructions", None) != rule:
            raise ValueError("native MCP instruction readback differs")
        before.pop("developer_instructions", None)
        if parsed != before:
            raise ValueError("native MCP installation would alter another setting")
        if _bytes(path) != original:
            raise ValueError("native MCP config changed during installation")
        if changed or instruction_changed:
            _atomic(path, after, path.stat().st_mode & 0o777)
        if _bytes(path) != after:
            raise RuntimeError(
                "native MCP config readback changed; not overwriting another writer"
            )
        return {
            "status": "configured_pending_native_invocation",
            "changed": changed or instruction_changed,
            "runtime_instruction_configured": True,
            "source_revision": revision,
            "configuration_sha256": sha(after),
            "hooks_changed": False,
            "source_registry_changed": False,
            "native_trust_changed": False,
            "production_ledger_changed": False,
        }
