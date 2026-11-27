"""Outer .Civ6Save container: readable header + zlib-compressed game body."""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass, field

MAX_SAVE_BYTES = 96 * 1024 * 1024
MAX_BODY_BYTES = 320 * 1024 * 1024
CHUNK = 64 * 1024

SPEED_IDS = {
    "GAMESPEED_QUICK": "QUICK",
    "GAMESPEED_ONLINE": "ONLINE",
    "GAMESPEED_STANDARD": "STANDARD",
    "GAMESPEED_EPIC": "EPIC",
    "GAMESPEED_MARATHON": "MARATHON",
}


class SaveFormatError(ValueError):
    """The bytes are not a readable Civ VI save (or a layout we do not know)."""


@dataclass
class SaveHeader:
    game_speed: str | None = None
    map_size: str | None = None
    ruleset: str | None = None
    difficulty: str | None = None
    map_script: str | None = None
    game_modes: list[str] = field(default_factory=list)
    # Setup slots: player index → civilization / leader type ids.
    slots: dict[int, dict] = field(default_factory=dict)


@dataclass
class SaveContainer:
    header: SaveHeader
    body: bytes
    body_offset: int


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _first(pattern: bytes, data: bytes) -> str | None:
    match = re.search(pattern, data)
    return match.group().decode("ascii") if match else None


def read_header(raw: bytes) -> SaveHeader:
    head = raw[: 1024 * 1024]
    header = SaveHeader(
        game_speed=_first(rb"GAMESPEED_[A-Z]+(?!_NAME)\b", head),
        map_size=_first(rb"MAPSIZE_[A-Z]+(?!_NAME)\b", head),
        ruleset=_first(rb"RULESET_[A-Z0-9_]+", head),
        difficulty=_first(rb"DIFFICULTY_[A-Z]+(?!_NAME)\b", head),
        map_script=_first(rb"[A-Za-z0-9_]+\.lua", head),
    )
    header.game_modes = sorted(
        {m.group(1).decode("ascii") for m in re.finditer(rb"(GAMEMODE_[A-Z_]+?)_NAME", head)}
    )
    header.slots = read_player_slots(head)
    return header


def read_player_slots(head: bytes) -> dict[int, dict]:
    """Player slots from the setup parameters in the readable header.

    Layout follows Civ6EGRM: an ``LOC_CIVILIZATION_*_ADJECTIVE`` string entry
    (key 31 EB 88 62) whose player index precedes the nearest earlier
    58 BA 7F 4C 02 marker, followed by the leader entry (key 5F 5E CD E8).
    """
    civ_key = bytes.fromhex("31EB8862")
    leader_key = bytes.fromhex("5F5ECDE8")
    index_key = bytes.fromhex("58BA7F4C02")
    slots: dict[int, dict] = {}
    position = head.find(civ_key)
    while position >= 0:
        try:
            length = _u16(head, position + 8)
            text = head[position + 16 : position + 16 + length].split(b"\0")[0].decode("ascii")
            marker = head.rfind(index_key, 0, position)
            index = _u16(head, marker - 4) if marker >= 4 else -1
            leader_at = head.find(leader_key, position)
            leader = None
            if 0 <= leader_at < position + 4096:
                leader_length = _u16(head, leader_at + 8)
                leader = head[leader_at + 16 : leader_at + 16 + leader_length].split(b"\0")[0].decode("ascii")
        except (struct.error, UnicodeDecodeError):
            text, index, leader = "", -1, None
        if text.startswith("LOC_CIVILIZATION_") and text.endswith("_ADJECTIVE") and 0 <= index < 64:
            civ = "CIVILIZATION_" + text[len("LOC_CIVILIZATION_") : -len("_ADJECTIVE")]
            slots.setdefault(
                index,
                {
                    "civilization": civ,
                    "leader": leader,
                    "minor": bool(leader and leader.startswith("LEADER_MINOR_CIV_")),
                    "spectator": civ == "CIVILIZATION_SPECTATOR",
                },
            )
        position = head.find(civ_key, position + 8)
    return slots


def _inflate_chunked(raw: bytes, start: int, limit: int) -> bytes:
    """Inflate 64 KiB chunks that are each followed by a 4-byte length word."""
    inflater = zlib.decompressobj()
    out = bytearray()
    position = start
    while position < len(raw) and not inflater.eof:
        chunk = raw[position : position + CHUNK]
        try:
            out += inflater.decompress(chunk, max(1, limit - len(out)))
        except zlib.error:
            # The game ends the stream with a sync flush (00 00 FF FF) and no
            # final block, so inflating past it fails. Keep what we have.
            if not out:
                raise
            break
        if inflater.unconsumed_tail or len(out) >= limit:
            raise SaveFormatError("세이브 본문이 너무 큽니다 (압축 해제 한도 초과).")
        position += CHUNK + 4
    return bytes(out)


def decompress_body(raw: bytes) -> tuple[bytes, int]:
    candidates: list[int] = []
    mod_index = raw.rfind(b"MOD_TITLE")
    first = raw.find(b"\x78\x9c", mod_index if mod_index >= 0 else 0)
    if first >= 0:
        candidates.append(first)
    # Fallback: every zlib header in the file, largest stream wins.
    candidates += [m.start() for m in re.finditer(b"\x78\x9c", raw) if m.start() != first]
    best: tuple[int, bytes, int] = (0, b"", -1)
    for start in candidates:
        try:
            probe = zlib.decompressobj().decompress(raw[start : start + 4096], 1 << 16)
        except zlib.error:
            continue
        if len(probe) < 1024:
            continue
        try:
            body = _inflate_chunked(raw, start, MAX_BODY_BYTES)
        except zlib.error:
            continue
        if len(body) > best[0]:
            best = (len(body), body, start)
        if start == first and len(body) > 1_000_000:
            break
    if best[0] < 100_000:
        raise SaveFormatError("세이브 본문(압축 데이터)을 찾지 못했습니다.")
    return best[1], best[2]


def read_container(raw: bytes) -> SaveContainer:
    if len(raw) > MAX_SAVE_BYTES:
        raise SaveFormatError("세이브 파일이 너무 큽니다.")
    if raw[:4] != b"CIV6":
        raise SaveFormatError("Civ6Save 파일이 아닙니다 (CIV6 헤더 없음).")
    header = read_header(raw)
    body, offset = decompress_body(raw)
    return SaveContainer(header=header, body=body, body_offset=offset)


def speed_id(header: SaveHeader) -> str | None:
    return SPEED_IDS.get(header.game_speed or "")
