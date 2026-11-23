"""Connect the disposable calculation view to the installed core's Git facts.

The registry selects a reviewed installation, never another WI authority. The
native stage producer additionally authenticates its real Pi ancestry. Operator
collectors can append observations but cannot grant or change execution rights.
"""
from __future__ import annotations

from hashlib import sha256
import fcntl
import os
import tempfile
import importlib
import json
from pathlib import Path
import sys

from .ledger import UsageLedger, LedgerError, SnapshotChanged
from .receipts import HEAD_NAMING_PRODUCERS, Receipt, ReceiptError


class InstalledAuthority:
    """Expose only append/read and machine-safe failures, not core internals."""
    def __init__(self, store):
        self._store = store
        # (stream entries, chunk paths) of the last complete verified read.
        self._verified = None
        self._last_tree = None

    def _module(self):
        return sys.modules[type(self._store).__module__]

    def _stream(self, tree):
        prefix = self._module().PREFIX
        return {p: oid for p, oid in tree.items() if p.startswith(prefix)}

    def _token(self, tree):
        """Identity of exactly the Git objects a verified read consumed.

        The stream wrappers name their chunk objects, so equal wrapper ids
        fix the chunk path set; equal chunk ids then fix every decoded byte.
        Other core objects (WorkItem decisions) were never read and cannot
        change the decoded facts, so they do not invalidate the cache.
        """
        stream = self._stream(tree)
        if self._verified is None or self._verified[0] != stream:
            return None
        chunks = self._verified[1]
        return self._module().digest({**stream, **{p: tree.get(p, 'missing') for p in chunks}})

    def _tree(self, revision):
        """One `ls-tree` per revision: scan, readback and checkpoint share it."""
        if self._last_tree is None or self._last_tree[0] != revision:
            self._last_tree = (revision, self._store.ledger.tree(revision))
        return self._last_tree[1]

    def _verified_read(self, revision, tree, after=None):
        store = self._store
        if after is None:
            yield from store.iter_batches(revision=revision)
        else:
            yield from store.iter_batches(revision=revision, after=after)
        if store.revision != revision:
            return
        stream, prefix = self._stream(tree), self._module().PREFIX
        # iter_batches records HEAD, every wrapper and every chunk it read.
        chunks = frozenset(p for p in store.ledger.dependencies if not p.startswith(prefix))
        if any(p not in tree for p in chunks):
            return
        self._verified = (stream, chunks)

    def verified_signature(self):
        """scan() identity after a complete read, or None."""
        store = self._store
        if self._verified is None or store.revision is None:
            return None
        return self._token(self._tree(store.revision))

    def checkpoint_binding(self):
        """(ledger binding, core validation source) a persisted prefix depends on.

        Raises when the installed core cannot resume a verified prefix, so an
        older core always reads its whole stream.
        """
        store, module = self._store, self._module()
        if not hasattr(store, 'prefix_identity'):
            raise ReceiptError('installed core cannot resume a verified prefix')
        source = module.__loader__.get_data(module.__file__)
        config = store.ledger.config
        return ({'namespace': store.ledger.namespace, 'identity': config['identity'],
                 'ref': config['ref']}, source)

    def checkpoint_prefix(self):
        """The verified prefix of the last complete read or confirmed own append."""
        store = self._store
        if self._verified is None or store.revision is None or store.sequence < 1:
            return None
        chunks = sorted(self._verified[1])
        tree = self._tree(store.revision)
        if self._stream(tree) != self._verified[0]:
            return None
        return {'sequence': store.sequence, 'watermark': store.watermark,
                'objects': store.prefix_identity(tree, store.sequence, chunks), 'chunks': chunks}

    def scan_after(self, prefix):
        """(identity, batches, resumed): resume after `prefix` iff Git still holds it.

        `resumed` only says the object identity matched; the core still chains
        the next batch (or head) to the prefix digest and the caller restarts
        from a complete read on any failure.
        """
        try:
            store = self._store
            store.guard()
            revision = store.ledger.refresh()
            tree = self._tree(revision)
            token = self._token(tree)
            if token is None:
                token = self._module().digest({'unverified': revision})
            resumed = False
            if hasattr(store, 'prefix_identity'):
                try:
                    resumed = (isinstance(prefix, dict) and isinstance(prefix.get('sequence'), int)
                               and store.prefix_identity(tree, prefix['sequence'], prefix['chunks'])
                               == prefix['objects']
                               and self._head_covers(tree, prefix['sequence']))
                except Exception:
                    resumed = False
            return token, self._verified_read(revision, tree, prefix if resumed else None), resumed
        except Exception as exc:
            raise ReceiptError('production observations unavailable; preserve source facts and retry') from exc

    def _head_covers(self, tree, sequence):
        """The current head exists and still reaches the checkpoint's last batch.

        Unchanged historical objects alone do not prove the stream still holds
        them: a removed or rewound head means the complete read decides.
        """
        head = self._module().HEAD
        if head not in tree:
            return False
        value = json.loads(self._store.ledger.blobs({head: tree[head]})[head])
        return isinstance(value, dict) and type(value.get('sequence')) is int and value['sequence'] >= sequence

    def resume_confirmed(self, sequence):
        """The core's last read really continued after `sequence`."""
        return getattr(self._store, 'resumed', 0) == sequence and getattr(self._store, 'sequence', 0) >= sequence

    def read(self):
        try:
            return self._store.read()
        except Exception as exc:
            raise ReceiptError('production observations unavailable; preserve source facts and retry') from exc

    def iter_batches(self):
        try:
            yield from self._store.iter_batches()
        except Exception as exc:
            raise ReceiptError('production observations unavailable; preserve source facts and retry') from exc

    def scan(self):
        """(identity, batches). An unverified identity never equals a cached one."""
        try:
            store = self._store
            store.guard()
            revision = store.ledger.refresh()
            tree = self._tree(revision)
            token = self._token(tree)
            if token is None:
                token = self._module().digest({'unverified': revision})
            return token, self._verified_read(revision, tree)
        except Exception as exc:
            raise ReceiptError('production observations unavailable; preserve source facts and retry') from exc

    def appended_signature(self, batch):
        """scan() identity of the revision this process published, iff its head is batch.

        Uses the store's confirmed publication only; a reply-loss readback or
        any other head returns None and the caller verifies the stream again.
        The identity is computed exactly as scan() does, so a later scan that
        observes another append differs and is read cold.
        """
        store = self._store
        module = self._module()
        if (self._verified is None or store.revision is None or store.sequence != batch['sequence']
                or store.watermark != batch['sha256']):
            return None
        tree = self._tree(store.revision)
        wrapper = f"{module.PREFIX}{batch['sequence']:020d}.json"
        if module.HEAD not in tree or wrapper not in tree:
            return None
        read = store.ledger.blobs({module.HEAD: tree[module.HEAD], wrapper: tree[wrapper]})
        head = json.loads(read[module.HEAD])
        if head.get('sequence') != batch['sequence'] or head.get('sha256') != batch['sha256']:
            return None
        stream, (previous, chunks) = self._stream(tree), self._verified
        # Exactly the previous verified stream plus this batch and its new head.
        if ({p: o for p, o in stream.items() if p not in (module.HEAD, wrapper)}
                != {p: o for p, o in previous.items() if p != module.HEAD}):
            return None
        record = json.loads(read[wrapper])
        if 'record' in record:
            if record['record'] != batch:
                return None
            added = frozenset()
        else:
            refs = [module.object_path(sha) for sha in record['object']['chunks']]
            if any(p not in tree for p in refs):
                return None
            parts = store.ledger.blobs({p: tree[p] for p in refs})
            if json.loads(b''.join(parts[p] for p in refs)) != batch:
                return None
            added = frozenset(refs)
        self._verified = (stream, chunks | added)
        return self._token(tree)

    def validate_batch(self, batch, sequence, previous):
        self._store.validate(batch, sequence, previous)

    def append_many(self, batches):
        try:
            self._store.append_many(batches)
        except Exception as exc:
            raise ReceiptError('original adoption unconfirmed; resume the same retained sources') from exc

    def append(self, batch):
        try:
            self._store.append(batch)
        except Exception as exc:
            if getattr(exc, 'code', None) == 'ledger_conflict':
                # The core confirmed its own commit is absent and an observed
                # dependency (another node's append) changed. Nothing landed.
                raise SnapshotChanged('production observations changed; recollect') from exc
            # A transport may have lost a successful push reply. Read the same
            # immutable transaction before reporting an ambiguous result.
            try:
                current = self._store.read()
                if len(current) >= batch['sequence'] and current[batch['sequence']-1] == batch:
                    return
            except Exception:
                pass
            raise ReceiptError('production observation save unconfirmed; retry the same source facts') from exc


def installed_store(entrypoint, *, guard=lambda: None):
    try:
        selected = Path(entrypoint)
        if not selected.is_absolute() or selected.name != 'harness.pyz' or selected.parent.name != 'active':
            raise ValueError('entry')
        entry = selected.resolve(strict=True)
        root = selected.parent.parent.resolve(strict=True)
        if entry.parent.parent != root / 'releases':
            raise ValueError('release')
        receipt = json.loads((entry.parent / 'installation.json').read_bytes())
        if sha256(entry.read_bytes()).hexdigest() != receipt['bundle_sha256']:
            raise ValueError('sealed program')
        prefix = str(entry) + '/'
        for name, module in tuple(sys.modules.items()):
            if name == 'harness_rewrite' or name.startswith('harness_rewrite.'):
                if not str(getattr(module, '__file__', '')).startswith(prefix):
                    raise ValueError('foreign core')
        sys.path.insert(0, str(entry))
        try:
            policy_module = importlib.import_module('harness_rewrite.policy')
            production = importlib.import_module('harness_rewrite.production_ledger')
            observations = importlib.import_module('harness_rewrite.observations')
            installation = importlib.import_module('harness_rewrite.production_install')
        finally:
            sys.path.remove(str(entry))
        policy = policy_module.load(entry.parent / 'host-policy.json')
        def active():
            guard()
            installation.require_active_program(entry, policy)
        active()
        store = observations.ObservationStore(production.ProductionLedger(
            policy['production_ledger'], policy['namespace']), guard=active)
        return InstalledAuthority(store)
    except Exception as exc:
        raise ReceiptError('installed observation authority is unavailable; no local fact fallback') from exc


def registered_ledger(registry, *, guard=lambda: None, read_only=False):
    entry = registry.get('observation_core_entrypoint')
    if entry is None:
        # Historical standalone tools stay readable until their node is adopted.
        return UsageLedger(registry['ledger_root'], read_only=read_only)
    return UsageLedger(registry['ledger_root'], authority=installed_store(entry, guard=guard),
                       read_only=read_only)


class _CoverageHeads:
    """Coverage version graph of the shared stream plus the rows adopted so far."""

    def __init__(self):
        self.entity: dict[str, str] = {}
        self.entities: set[str] = set()
        self.replaced: set[str] = set()
        self.named: dict[str, str] = {}

    def add(self, row: Receipt) -> None:
        self.entity[row.fact_ref] = row.entity_ref
        self.entities.add(row.entity_ref)
        if row.supersedes:
            self.replaced.add(row.supersedes)
        if row.producer in HEAD_NAMING_PRODUCERS:
            self.named.update((ref, row.entity_ref) for ref in row.evidence)

    def extends(self, row: Receipt) -> bool:
        """A new version of a new entity, or the successor of a current head."""
        if row.fact_ref in self.entity:
            return False
        if row.entity_ref not in self.entities:
            return True
        prior = row.supersedes
        return (prior is not None and self.entity.get(prior) == row.entity_ref
                and prior not in self.replaced and self.named.get(prior) != row.entity_ref)


def _adopt_original_receipts(registry, *, verify_only=False, prepare_only=False, store=None,
                             original_paths=None):
    """Adopt original facts, preserving local files and the shared history.

    Local transaction numbers are not global identities. Identical wire facts
    already published by another node are reused; distinct observations and
    conflicting assertions retain their original fields for the normal reducer.
    No assertion winner, new attribution or business decision is chosen here.
    A coverage version is a collector's state snapshot: it is adopted only as a
    new entity or as the successor of a current head. A version that would
    fork an entity the stream already tracks stays in its original file.
    """
    if not registry.get('observation_core_entrypoint'):
        raise ReceiptError('explicit installed observation authority is required')
    from datetime import timedelta
    from .events import parse_timestamp, utc_text
    from .receipts import digest, canonical
    root = Path(registry['ledger_root'])
    store = store or installed_store(registry['observation_core_entrypoint'])
    directory = root / 'receipts'
    if directory.is_symlink():
        raise ReceiptError('original receipt directory redirects')
    originals, previous, previous_at = [], None, None
    # Validate all retained originals before publication, retaining only their
    # immutable identities. Loading every raw fact twice made large nodes fail.
    paths = sorted(directory.glob('*.json')) if original_paths is None else original_paths
    for sequence, path in enumerate(paths, 1):
        if path.is_symlink() or path.name != f'{sequence:020d}.json':
            raise ReceiptError('original receipt sequence differs')
        raw = path.read_bytes()
        batch = json.loads(raw)
        at = parse_timestamp(batch['recorded_at'], 'recorded_at')
        if (set(batch) != {'schema', 'sequence', 'recorded_at', 'previous', 'facts', 'sha256'}
                or batch['schema'] != 'computecost_ledger_transaction/v1'
                or batch['sequence'] != sequence or batch['previous'] != previous
                or batch['sha256'] != digest({k: v for k, v in batch.items() if k != 'sha256'})
                or not isinstance(batch['facts'], list) or not batch['facts']
                or previous_at is not None and at <= previous_at):
            raise ReceiptError('original receipt digest/order differs')
        for row in batch['facts']:
            Receipt.from_dict(row)
        store.validate_batch(batch, sequence, previous)
        originals.append((path, sha256(raw).digest(), batch['sha256']))
        previous, previous_at = batch['sha256'], at
    count, head, head_at, known = 0, None, None, set()
    coverage, coverage_retained = _CoverageHeads(), 0
    shared_prefix_matches = True
    for count, batch in enumerate(store.iter_batches(), 1):
        if count <= len(originals) and batch['sha256'] != originals[count - 1][2]:
            shared_prefix_matches = False
        head, head_at = batch['sha256'], parse_timestamp(batch['recorded_at'], 'recorded_at')
        for row in batch['facts']:
            fact = Receipt.from_dict(row)
            known.add(digest(row))
            if fact.kind == 'coverage':
                coverage.add(fact)
    published = count
    pending, size = [], 0
    for path, fingerprint, _ in originals:
        raw = path.read_bytes()
        if sha256(raw).digest() != fingerprint:
            raise ReceiptError('original changed during adoption; retain the published prefix')
        original = json.loads(raw)
        missing = []
        for row in original['facts']:
            identity = digest(row)
            if identity not in known:
                if row['kind'] == 'coverage':
                    fact = Receipt.from_dict(row)
                    if not coverage.extends(fact):
                        coverage_retained += 1
                        continue
                    coverage.add(fact)
                missing.append(row); known.add(identity)
        if shared_prefix_matches and original['sequence'] > published:
            missing = original['facts']  # Preserve the exact first-node history.
            for row in missing:
                if row['kind'] == 'coverage':
                    coverage.add(Receipt.from_dict(row))
        if not missing:
            continue
        if verify_only:
            raise ReceiptError('original adoption is incomplete; resume the same deployment when idle')
        at = parse_timestamp(original['recorded_at'], 'recorded_at')
        if head_at is not None:
            at = max(at, head_at + timedelta(microseconds=1))
        batch = {'schema': original['schema'], 'sequence': count + 1,
                 'recorded_at': utc_text(at), 'previous': head, 'facts': missing}
        batch['sha256'] = digest(batch)
        # Preserve an original transaction byte-for-byte when its entire prefix
        # is already the shared prefix (including its timestamp representation).
        if (original['sequence'] == count + 1 and original['previous'] == head
                and len(missing) == len(original['facts'])
                and (head_at is None or parse_timestamp(original['recorded_at'], 'recorded_at') > head_at)):
            batch = original
        store.validate_batch(batch, count + 1, head)
        count, head, head_at = count + 1, batch['sha256'], at
        if prepare_only:
            continue
        length = len(canonical(batch))
        if pending and size + length > 8 * 1024 * 1024:
            store.append_many(pending)
            pending, size = [], 0
        pending.append(batch)
        size += length
    if pending:
        store.append_many(pending)
    # Publication verifies its exact immutable Git commit. Check the retained
    # local preimages again without fetching and decoding the full Git prefix.
    if any(sha256(p.read_bytes()).digest() != fingerprint for p, fingerprint, _ in originals):
        raise LedgerError('original receipt adoption readback differs')
    if prepare_only:
        return {'status': 'prepared', 'original_transactions': len(originals),
                'published_transactions': published, 'original_head': previous}
    return {'schema': 'computecost_observation_adoption/v1', 'status': 'verified',
            'original_transactions': len(originals), 'watermark': count,
            'source': 'production_git', 'originals_preserved': True,
            'coverage_versions_retained_in_originals': coverage_retained}


def verify_original_receipts(registry):
    root = Path(registry['ledger_root'])
    if root.resolve(strict=True) != root or root.stat().st_uid != os.getuid():
        raise ReceiptError('original store is redirected or not account-owned')
    with (root / '.usage-ledger.lock').open('a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return _adopt_original_receipts(registry, verify_only=True)


def preview_original_receipts(registry, store):
    """Read a fixed immutable prefix without holding up the live writer.

    Only enumerate under its lock. Existing receipt bytes are atomically
    published and immutable; adoption already verifies every retained preimage.
    A later append belongs to the next preview/final maintenance snapshot.
    """
    root = Path(registry['ledger_root'])
    if root.resolve(strict=True) != root or root.stat().st_uid != os.getuid():
        raise ReceiptError('original store is redirected or not account-owned')
    with (root / '.usage-ledger.lock').open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReceiptError('original receipt writer is active; defer preparation') from exc
        paths = tuple(sorted((root / 'receipts').glob('*.json')))
    return _adopt_original_receipts(registry, prepare_only=True, store=store, original_paths=paths)


def adopt_original_receipts(registry, *, selection=None):
    """Serialize the retained originals and reader selection with existing writers."""
    root = Path(registry['ledger_root'])
    if root.resolve(strict=True) != root or root.stat().st_uid != os.getuid():
        raise ReceiptError('original store is redirected or not account-owned')
    with (root / '.usage-ledger.lock').open('a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if selection is not None:
            path = Path(selection['path'])
            if path.resolve() != path or path.parent.stat().st_uid != os.getuid() or path.parent.stat().st_mode & 0o077:
                raise ReceiptError('reader selection must remain private and account-owned')
            before = selection['before']
            after = selection['after']
            if json.loads(after) != registry:
                raise ReceiptError('reader selection differs from reviewed registry')
            current = path.read_text() if path.exists() else None
            if current not in (before, after):
                raise ReceiptError('reader selection changed before original adoption')
        result = _adopt_original_receipts(registry)
        if selection is not None and current != after:
            fd, temporary = tempfile.mkstemp(prefix='.observation-reader-', dir=path.parent)
            try:
                with os.fdopen(fd, 'w') as stream:
                    stream.write(after); stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary, path)
                directory = os.open(path.parent, os.O_RDONLY)
                try: os.fsync(directory)
                finally: os.close(directory)
            finally:
                if os.path.exists(temporary): os.unlink(temporary)
        return result


def main():
    import argparse
    import os
    parser = argparse.ArgumentParser(description="Adopt retained receipt facts into the installed production core")
    parser.add_argument('--registry', type=Path, required=True)
    args = parser.parse_args()
    info = args.registry.lstat()
    if (args.registry.is_symlink() or not args.registry.is_file()
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ReceiptError('operator registry must be private and account-owned')
    print(json.dumps(adopt_original_receipts(json.loads(args.registry.read_bytes())), sort_keys=True))


if __name__ == '__main__':
    main()
