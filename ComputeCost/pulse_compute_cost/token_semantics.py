"""Input-token class semantics shared by event and receipt consumers.

Native `input` can mean either total input (including cache) or only the
uncached class. Keep unknown counts unknown; a caller must choose its native
meaning explicitly before it can use a canonical quantity.
"""

from __future__ import annotations

from dataclasses import dataclass


class TokenSemanticsError(ValueError):
    pass


def _count(value: int | None, field: str) -> int | None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise TokenSemanticsError(f"{field} must be a non-negative integer or null")
    return value


@dataclass(frozen=True)
class InputTokenClasses:
    total_input: int | None
    uncached_input: int | None
    cache_read: int | None
    cache_write: int | None

    @classmethod
    def from_disjoint(
        cls, uncached_input: int | None, cache_read: int | None, cache_write: int | None,
    ) -> InputTokenClasses:
        uncached = _count(uncached_input, "uncached_input")
        read = _count(cache_read, "cache_read")
        write = _count(cache_write, "cache_write")
        total = (uncached + read + write
                 if uncached is not None and read is not None and write is not None else None)
        return cls(total, uncached, read, write)

    @classmethod
    def from_inclusive(
        cls, total_input: int | None, cache_read: int | None, cache_write: int | None,
    ) -> InputTokenClasses:
        total = _count(total_input, "total_input")
        read = _count(cache_read, "cache_read")
        write = _count(cache_write, "cache_write")
        if total is not None:
            known_cache = (read or 0) + (write or 0)
            if known_cache > total:
                raise TokenSemanticsError("cache classes exceed inclusive input")
        if total is not None and read is not None and write is not None:
            uncached = total - read - write
        else:
            uncached = None
        return cls(total, uncached, read, write)

    def require_total(self) -> int:
        if self.total_input is None:
            raise TokenSemanticsError("total input is unknown")
        return self.total_input

    def require_uncached(self) -> int:
        if self.uncached_input is None:
            raise TokenSemanticsError("uncached input is unknown")
        return self.uncached_input
