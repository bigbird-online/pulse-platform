"""Lossless encoding for the bounded Stats cost snapshot."""
from __future__ import annotations

import base64
from collections import Counter
from copy import deepcopy
import gzip
import json
import zlib

from .receipts import canonical

MAX_BYTES = 512 * 1024
# In-memory expansion bound for readers, not a transport limit: the publication
# stays within MAX_BYTES and the App snapshot within 1 MiB (gzip passes through).
MAX_EXPANDED_BYTES = 4 * 1024 * 1024
ROW_LISTS = ('groups', 'outcome_only_groups', 'pull_request_only_groups')
SHARED_FIELDS = ('tenant', 'outcomes', 'key', 'cost', 'usage', 'attribution', 'detail')
COMPRESSED_FIELD = 'compressed_dashboard'
COMPRESSION_METADATA = ('schema_version', 'generated_at', 'expires_at', 'source_revision', 'status', 'sha256')
COMPRESSED_PROBE_SCHEMA = 'pulse_stats_cost_compression/v1'


def rows(document: dict) -> list[dict]:
    return [row for view in document['views'] for group in ROW_LISTS for row in view.get(group, [])]


def compressed_dashboard_bytes(document: dict) -> bytes:
    raw = canonical(document) + b'\n'
    if len(raw) > MAX_EXPANDED_BYTES:
        raise ValueError('expanded cost snapshot exceeds the in-memory expansion limit')
    packed = {name: document[name] for name in COMPRESSION_METADATA}
    packed[COMPRESSED_FIELD] = base64.b64encode(gzip.compress(raw, mtime=0)).decode('ascii')
    return canonical(packed) + b'\n'


def wire_encoding(document: dict) -> str | None:
    if COMPRESSED_FIELD in document:
        return COMPRESSED_FIELD
    return 'row_contexts' if 'row_contexts' in document else None


def dashboard_bytes(document: dict) -> bytes:
    original = canonical(document) + b'\n'
    if not MAX_BYTES < len(original) <= MAX_EXPANDED_BYTES:
        return original
    packed = deepcopy(document)
    groups = rows(packed)
    contexts = {}
    for field in SHARED_FIELDS:
        values = [(row, canonical(row[field])) for row in groups if isinstance(row.get(field), dict)]
        counts = Counter(value for _, value in values)
        indexes = {value: index for index, value in enumerate(
            value for value, count in counts.items() if count > 1)}
        if not indexes:
            continue
        contexts[field] = [json.loads(value) for value in indexes]
        for row, value in values:
            if value in indexes:
                row[field] = indexes[value]
    if contexts:
        packed['row_contexts'] = contexts
    compact = canonical(packed) + b'\n'
    if len(compact) <= MAX_BYTES:
        return compact
    compressed = compressed_dashboard_bytes(document)
    return min((original, compact, compressed), key=len)


def expand_dashboard(document: dict, *, max_encoded_bytes: int = MAX_BYTES) -> dict:
    """`max_encoded_bytes` bounds the carrier: a publication file, or the 1 MiB App snapshot."""
    if COMPRESSED_FIELD in document:
        if set(document) != {*COMPRESSION_METADATA, COMPRESSED_FIELD}:
            raise ValueError('invalid compressed cost snapshot metadata')
        encoded = document[COMPRESSED_FIELD]
        if not isinstance(encoded, str) or len(encoded) > max_encoded_bytes:
            raise ValueError('invalid compressed cost snapshot size')
        try:
            raw = base64.b64decode(encoded, validate=True)
            decoder = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
            expanded = decoder.decompress(raw, MAX_EXPANDED_BYTES + 1)
            if len(expanded) > MAX_EXPANDED_BYTES:
                raise ValueError('expanded cost snapshot exceeds the in-memory expansion limit')
            if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise ValueError('invalid compressed cost snapshot stream')
            value = json.loads(expanded)
        except (zlib.error, RecursionError) as exc:
            raise ValueError('invalid compressed cost snapshot') from exc
        if (not isinstance(value, dict) or wire_encoding(value)
                or any(name not in value or value[name] != document[name] for name in COMPRESSION_METADATA)):
            raise ValueError('compressed cost snapshot metadata differs')
        return value
    if 'row_contexts' not in document:
        return document
    expanded = deepcopy(document)
    contexts = expanded.pop('row_contexts')
    if not isinstance(contexts, dict) or not contexts or set(contexts) - set(SHARED_FIELDS):
        raise ValueError('invalid cost row context')
    groups = rows(expanded)
    size = len(canonical(expanded)) + 1
    for field, values in contexts.items():
        if not isinstance(values, list) or not values or not all(isinstance(value, dict) for value in values):
            raise ValueError('invalid cost row context values')
        lengths = [len(canonical(value)) for value in values]
        used = set()
        for row in groups:
            index = row.get(field)
            if type(index) is not int:
                continue
            if not 0 <= index < len(values):
                raise ValueError('invalid cost row context reference')
            used.add(index)
            size += lengths[index] - len(str(index))
            if size > MAX_EXPANDED_BYTES:
                raise ValueError('expanded cost snapshot exceeds the in-memory expansion limit')
            row[field] = deepcopy(values[index])
        if len(used) != len(values):
            raise ValueError('unreferenced cost row context value')
    return expanded
