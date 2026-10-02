"""Exact integer intervals and finite/cofinite typed symbolic domains."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

type Scalar = str | bool | int

_BOUNDS = {
    "integer": (-(2**128), 2**128 - 1),
    "ipv4": (0, 2**32 - 1),
    "ipv6": (0, 2**128 - 1),
}


@dataclass(frozen=True)
class Domain:
    kind: str
    intervals: tuple[tuple[int, int], ...] = ()
    symbols: tuple[str | bool, ...] = ()
    excluded: bool = False

    def __post_init__(self) -> None:
        if type(self.excluded) is not bool:
            raise ValueError("domain complement flag must be boolean")
        if self.kind in _BOUNDS:
            if self.symbols or self.excluded:
                raise ValueError("numeric domains use intervals only")
            low, high = _BOUNDS[self.kind]
            normalized: list[tuple[int, int]] = []
            for start, end in sorted(self.intervals):
                if (
                    type(start) is not int
                    or type(end) is not int
                    or not low <= start <= end <= high
                ):
                    raise ValueError("invalid typed interval")
                if normalized and start <= normalized[-1][1] + 1:
                    normalized[-1] = (normalized[-1][0], max(end, normalized[-1][1]))
                else:
                    normalized.append((start, end))
            object.__setattr__(self, "intervals", tuple(normalized))
        elif self.kind in ("string", "boolean"):
            expected = str if self.kind == "string" else bool
            if self.intervals or any(type(v) is not expected for v in self.symbols):
                raise ValueError("symbol types do not match the domain")
            symbols = set(self.symbols)
            if self.kind == "boolean" and self.excluded:
                symbols = set[bool | str]((True, False)) - symbols
                object.__setattr__(self, "excluded", False)
            object.__setattr__(self, "symbols", tuple(sorted(symbols)))
        else:
            raise ValueError("unsupported domain kind")

    @classmethod
    def value(cls, value: Scalar) -> Domain:
        if type(value) is int:
            return cls("integer", ((value, value),))
        if type(value) is bool:
            return cls("boolean", symbols=(value,))
        if isinstance(value, str):
            return cls("string", symbols=(value,))
        raise ValueError("unsupported scalar")

    @classmethod
    def range(cls, start: int, end: int) -> Domain:
        return cls("integer", ((start, end),))

    @classmethod
    def ip(cls, prefix: str) -> Domain:
        network = ipaddress.ip_network(prefix, strict=False)
        return cls(
            f"ipv{network.version}",
            ((int(network.network_address), int(network.broadcast_address)),),
        )

    @classmethod
    def universe(cls, kind: str) -> Domain:
        if kind in _BOUNDS:
            return cls(kind, (_BOUNDS[kind],))
        return cls(kind, excluded=True)

    @property
    def empty(self) -> bool:
        return (
            not self.intervals if self.kind in _BOUNDS else not self.symbols and not self.excluded
        )

    @property
    def singleton(self) -> bool:
        if self.kind in _BOUNDS:
            return len(self.intervals) == 1 and self.intervals[0][0] == self.intervals[0][1]
        return len(self.symbols) == 1 and not self.excluded

    def intersect(self, other: Domain) -> Domain:
        if self.kind != other.kind:
            raise ValueError("domain kinds differ; boolean True is not integer 1")
        if self.kind in _BOUNDS:
            parts: list[tuple[int, int]] = []
            left_index = right_index = 0
            while left_index < len(self.intervals) and right_index < len(other.intervals):
                a, b = self.intervals[left_index]
                c, d = other.intervals[right_index]
                if max(a, c) <= min(b, d):
                    parts.append((max(a, c), min(b, d)))
                if b <= d:
                    left_index += 1
                else:
                    right_index += 1
            return Domain(self.kind, tuple(parts))
        left, right = set(self.symbols), set(other.symbols)
        if self.excluded and other.excluded:
            return Domain(self.kind, symbols=tuple(left | right), excluded=True)
        if self.excluded:
            return Domain(self.kind, symbols=tuple(right - left))
        return Domain(self.kind, symbols=tuple(left - right if other.excluded else left & right))

    def complement(self) -> Domain:
        if self.kind not in _BOUNDS:
            return Domain(self.kind, symbols=self.symbols, excluded=not self.excluded)
        low, high = _BOUNDS[self.kind]
        parts: list[tuple[int, int]] = []
        cursor = low
        for start, end in self.intervals:
            if cursor < start:
                parts.append((cursor, start - 1))
            cursor = end + 1
        if cursor <= high:
            parts.append((cursor, high))
        return Domain(self.kind, tuple(parts))

    def subtract(self, other: Domain) -> Domain:
        return self.intersect(other.complement())

    def witness(self) -> Scalar:
        if self.empty:
            raise ValueError("empty domain has no witness")
        if self.kind in _BOUNDS:
            value = self.intervals[0][0]
            if self.kind == "ipv4":
                return str(ipaddress.IPv4Address(value))
            if self.kind == "ipv6":
                return str(ipaddress.IPv6Address(value))
            return value
        if self.excluded:
            value_string = "other"
            while value_string in self.symbols:
                value_string += "_"
            return value_string
        return self.symbols[0]


@dataclass(frozen=True)
class Space:
    fields: tuple[tuple[str, Domain], ...]

    def __post_init__(self) -> None:
        if len({key for key, _ in self.fields}) != len(self.fields):
            raise ValueError("duplicate packet field")
        if any(
            not key or not isinstance(value, Domain) or value.empty for key, value in self.fields
        ):
            raise ValueError("packet fields need nonempty typed domains")
        object.__setattr__(self, "fields", tuple(sorted(self.fields)))

    @classmethod
    def of(cls, **fields: Scalar | Domain) -> Space:
        return cls(
            tuple(
                (key, value if isinstance(value, Domain) else Domain.value(value))
                for key, value in fields.items()
            )
        )

    def get(self, name: str) -> Domain | None:
        return dict(self.fields).get(name)

    def with_field(self, name: str, domain: Domain) -> Space:
        return Space(tuple({**dict(self.fields), name: domain}.items()))

    def witness(self) -> dict[str, Scalar]:
        return {key: value.witness() for key, value in self.fields}
