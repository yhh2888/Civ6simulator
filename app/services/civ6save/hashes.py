"""Type-name hashes used inside Civ VI saves.

Civ VI stores database types (TERRAIN_GRASS, BUILDING_LIBRARY, ...) as a 32-bit
hash. The hash is the bitwise-inverted CRC-32 of the ASCII type name, i.e.
CRC-32 without the final XOR. Verified for terrains, features, resources,
improvements, districts and buildings in real saves.
"""

from __future__ import annotations

import zlib
from pathlib import Path
from typing import Iterable

NO_TYPE = 0xFFFFFFFF


def type_hash(name: str) -> int:
    return zlib.crc32(name.encode("ascii")) ^ 0xFFFFFFFF


def load_name_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line)
    return names


class TypeNames:
    """Reverse lookup hash → type name, with per-prefix hash sets."""

    def __init__(self, names: Iterable[str]):
        self.by_hash: dict[int, str] = {}
        for name in names:
            if isinstance(name, str) and name and name.isascii():
                self.by_hash.setdefault(type_hash(name), name)
        self._prefix_cache: dict[str, dict[int, str]] = {}

    def __len__(self) -> int:
        return len(self.by_hash)

    def name(self, value: int) -> str | None:
        """Type name for a hash; ``None`` for the empty marker 0xFFFFFFFF."""
        if value == NO_TYPE:
            return None
        return self.by_hash.get(value)

    def name_or_unknown(self, value: int) -> str | None:
        if value == NO_TYPE:
            return None
        return self.by_hash.get(value) or f"UNKNOWN_{value:08X}"

    def with_prefix(self, prefix: str) -> dict[int, str]:
        cached = self._prefix_cache.get(prefix)
        if cached is None:
            cached = {h: n for h, n in self.by_hash.items() if n.startswith(prefix)}
            self._prefix_cache[prefix] = cached
        return cached
