"""Synthetic .Civ6Save builder for decoder/import tests.

Writes the same byte layout the decoder reads from real Gathering Storm saves
(tile records, district records, city objects, building tables, chunked zlib
body), on the 44 x 26 "Duel" map size. Real saves are not shipped with the
project; tests that need them read ``CIV6_SAVE_DIR`` and are skipped otherwise.
"""

from __future__ import annotations

import struct
import zlib

from app.services.civ6save.decoder import city_plots
from app.services.civ6save.hashes import type_hash

WIDTH, HEIGHT = 44, 26
MAP_KEY = b"\x0E\x00\x00\x00\x0F\x00\x00\x00\x06\x00\x00\x00"
SLOT_CIV_KEY = bytes.fromhex("31EB8862")
SLOT_LEADER_KEY = bytes.fromhex("5F5ECDE8")
SLOT_INDEX_KEY = bytes.fromhex("58BA7F4C02")


def h(name: str | None) -> int:
    return 0xFFFFFFFF if name is None else type_hash(name)


def tile_record(terrain="TERRAIN_GRASS", feature=None, resource=None, improvement=None, *, road=255,
                appeal=0, river=0, cliff=0, owner=None) -> bytes:
    """owner = (player, city_index, district_id or None, wonder or None)."""
    rec = bytearray(55)
    struct.pack_into("<I", rec, 12, h(terrain))
    struct.pack_into("<I", rec, 16, h(feature))
    struct.pack_into("<I", rec, 27, h(resource))
    struct.pack_into("<I", rec, 33, h(improvement))
    rec[38] = road
    struct.pack_into("<h", rec, 40, appeal)
    rec[46] = river
    rec[47] = cliff
    rec[49] = 64 if owner else 32
    struct.pack_into("<I", rec, 51, 0)
    if owner:
        player, city_index, district_id, wonder = owner
        own = bytearray(17)
        struct.pack_into("<HH", own, 0, city_index, city_index + 1)
        struct.pack_into("<HH", own, 4, city_index, city_index + 1)
        struct.pack_into("<H", own, 8, 0xFFFF if district_id is None else district_id)
        struct.pack_into("<H", own, 10, 0xFFFF if district_id is None else district_id + 1)
        own[12] = player
        struct.pack_into("<I", own, 13, h(wonder))
        rec += own
    return bytes(rec)


def district_record(district_id: int, x: int, y: int, city_index: int, district_type: str,
                    completed: bool = True, cost: int = 0) -> bytes:
    after = bytearray(64)
    struct.pack_into("<I", after, 16, cost)   # hash + 20: locked cost
    after[20] = 1 if completed else 0          # hash + 24: completed flag
    return (b"\x16\x00\x00\x00" + b"\x0F\x00\x00\x00" + struct.pack("<HH", district_id, district_id + 1)
            + struct.pack("<ii", x, y) + struct.pack("<HH", city_index, city_index + 1)
            + struct.pack("<I", h(district_type)) + bytes(after))


BUILDING_ORDER = [
    "BUILDING_MONUMENT", "BUILDING_PALACE", "BUILDING_BARRACKS", "BUILDING_GRANARY", "BUILDING_LIBRARY",
    "BUILDING_SHRINE", "BUILDING_WALLS", "BUILDING_WATER_MILL", "BUILDING_ARENA", "BUILDING_LIGHTHOUSE",
    "BUILDING_MARKET", "BUILDING_STABLE", "BUILDING_TEMPLE", "BUILDING_AMPHITHEATER", "BUILDING_CASTLE",
    "BUILDING_UNIVERSITY", "BUILDING_WORKSHOP", "BUILDING_ARMORY", "BUILDING_SHIPYARD", "BUILDING_BANK",
    "BUILDING_PYRAMIDS", "BUILDING_ORACLE", "BUILDING_STONEHENGE", "BUILDING_HANGING_GARDENS",
]


def building_table(values: dict[str, int]) -> bytes:
    out = struct.pack("<I", len(BUILDING_ORDER))
    for name in BUILDING_ORDER:
        out += struct.pack("<IH", h(name), values.get(name, 0xFFFF))
    return out


PRODUCTION_KIND = {"unit": 0, "building": 1, "district": 2, "project": 3}


def citizen_arrays(centre: tuple[int, int], working: dict, locked: dict) -> bytes:
    """Two 37-entry arrays in the save's spiral order (centre counts as 1)."""
    plots = city_plots(centre[0], centre[1], WIDTH)
    a1 = [0] * 37
    a2 = [0] * 37
    a1[0] = 1
    for (x, y), count in working.items():
        a1[plots.index((x, y))] = count
    for (x, y), count in locked.items():
        a2[plots.index((x, y))] = count
    # The city marker 02 00 00 00 | 25 00 00 00 doubles as the first count.
    return (b"\x02\x00\x00\x00" + struct.pack("<I", 37) + struct.pack("<37I", *a1)
            + struct.pack("<I", 37) + struct.pack("<37I", *a2))


def production_entry(kind: str, item: str, x: int | None = None, y: int | None = None) -> bytes:
    marker = bytes.fromhex("464A0F2C")
    return (struct.pack("<I", 1) + marker + struct.pack("<I", 2) + marker
            + struct.pack("<IIIIii", PRODUCTION_KIND[kind], 0, h(item), 0,
                          -9999 if x is None else x, -9999 if y is None else y)
            + struct.pack("<I", 1))


def city_object(name: str, district_ids: list[int], built: dict[str, int], *, centre=None, working=None,
                locked=None, production=None, progress=None) -> bytes:
    encoded = name.encode("utf-8")
    head = struct.pack("<I", len(encoded)) + encoded + b"\x00" * 8 + b"\x04\x00\x00\x00"
    head += struct.pack("<I", len(district_ids)) + b"".join(struct.pack("<HH", i, i + 1) for i in district_ids)
    block = bytearray(head + b"\x00" * 16)
    if centre is not None and working is not None:
        block += citizen_arrays(centre, working, locked or {})
    block += b"\x00" * (2400 - len(block))
    if production:
        block += production_entry(*production)
        block += b"\x00" * 64
        # Progress table: (hash, value x 256) pairs after the production entry.
        for item, amount in (progress or {}).items():
            block += struct.pack("<II", h(item), int(round(amount * 256)))
    block += b"\x00" * (4500 - len(block))
    block += building_table({})          # 1st stride-6 table: in-progress plots (empty)
    block += b"\x00" * 300
    block += building_table(built)       # 2nd stride-6 table: built buildings → plot
    block += b"\x00" * 2000
    return bytes(block)


def _flag_table(names: list[str], marked: set[str]) -> bytes:
    return struct.pack("<I", len(names)) + b"".join(struct.pack("<IB", h(n), 1 if n in marked else 0) for n in names)


def tech_triple(names: list[str], done: set[str], boosted: set[str], progress: dict[str, float]) -> bytes:
    """(researched, boosted, progress × 256) tables, as stored per player."""
    third = struct.pack("<I", len(names)) + b"".join(
        struct.pack("<II", h(n), int(round(progress.get(n, 0.0) * 256))) for n in names)
    return _flag_table(names, done) + _flag_table(names, boosted) + third


GOVERNMENT_KEYS = (0xB0ABCBE5, 0x29A29A5F, 0x5EA5AAC9, 0xC0C13F6A)


def policy_block(policy_names: list[str], tiers: list[str | None], slots: list[tuple[str | None, int]],
                 active: set[str] = frozenset()) -> bytes:
    """(key, government) pairs per tier, five 140-style policy subtables, then
    the slot list (policy hash or 0xFFFFFFFF, slot code)."""
    head = struct.pack("<IIII", 0, h("GOVERNMENT_SYNTHETIC_TECHNOCRACY"), 0, 4)
    for key, government in zip(GOVERNMENT_KEYS, list(tiers) + [None] * (4 - len(tiers))):
        head += struct.pack("<II", key, h(government) if government else 0xFFFFFFFF)
    tables = b"".join(_flag_table(policy_names, active if i == 3 else set()) for i in range(5))
    slot_list = struct.pack("<II", len(slots), len(slots)) + b"".join(
        struct.pack("<II", h(policy) if policy else 0xFFFFFFFF, code) for policy, code in slots)
    return head + tables + slot_list


def plot(x: int, y: int) -> int:
    return y * WIDTH + x


def default_layout() -> dict:
    """Two players. Player 0 'Testopolis' at (10, 10) with a campus at (11, 10)
    and the Pyramids at (9, 10); player 1 'Otherton' at (30, 12)."""
    tiles = {}
    own0 = [(10, 10), (11, 10), (9, 10), (10, 11), (11, 11), (10, 9), (11, 9)]
    for x, y in own0:
        tiles[(x, y)] = {"owner": (0, 0, None, None)}
    tiles[(10, 10)]["owner"] = (0, 0, 0, None)
    tiles[(11, 10)].update({"owner": (0, 0, 1, None)})
    tiles[(9, 10)].update({"owner": (0, 0, 2, "BUILDING_PYRAMIDS"), "terrain": "TERRAIN_DESERT"})
    tiles[(10, 11)].update({"resource": "RESOURCE_WHEAT", "improvement": "IMPROVEMENT_FARM"})
    tiles[(10, 9)]["owner"] = (0, 0, 3, None)  # theater placed, still under construction
    tiles[(11, 9)].update({"terrain": "TERRAIN_PLAINS_HILLS", "feature": "FEATURE_FOREST"})
    # River between (10,9) [its E edge] and (11,9) [its W edge]: bits NE=0,E=1,SE=2,SW=3,W=4,NW=5
    tiles[(10, 9)]["river"] = 1 << 1
    tiles[(11, 9)]["river"] = 1 << 4
    for x, y in [(30, 12), (31, 12), (29, 12)]:
        tiles[(x, y)] = {"owner": (1, 0, None, None)}
    tiles[(30, 12)]["owner"] = (1, 0, 0, None)
    cities = [
        {"player": 0, "name": "LOC_CITY_NAME_TESTOPOLIS",
         "districts": [(0, 10, 10, "DISTRICT_CITY_CENTER", True, 0), (1, 11, 10, "DISTRICT_CAMPUS", True, 54),
                       (2, 9, 10, "DISTRICT_WONDER", True, 0), (3, 10, 9, "DISTRICT_THEATER", False, 60)],
         "built": {"BUILDING_PALACE": plot(10, 10), "BUILDING_LIBRARY": plot(11, 10), "BUILDING_PYRAMIDS": plot(9, 10)},
         # citizens: farm worked, (11,11) locked by hand, 2 specialists in the campus → population 4
         "working": {(10, 11): 1, (11, 10): 2}, "locked": {(11, 11): 1},
         "production": ("building", "BUILDING_GRANARY"),
         "progress": {"BUILDING_GRANARY": 12.5, "DISTRICT_THEATER": 20.0}},
        # No citizen arrays: the importer must fall back to automatic placement.
        {"player": 1, "name": "Otherton", "districts": [(0, 30, 12, "DISTRICT_CITY_CENTER", True, 0)],
         "built": {"BUILDING_PALACE": plot(30, 12)}},
    ]
    return {"tiles": tiles, "cities": cities}


def build_body(layout: dict | None = None, turn: int = 37) -> bytes:
    layout = layout or default_layout()
    body = bytearray(b"\x05\x00\x00\x00\x03\x00\x00\x00" + b"\x00" * 121)
    body += b"".join(struct.pack("<I", turn) for _ in range(12)) + b"\xFF" * 64
    body += b"\x00" * (1024 - len(body))
    body += MAP_KEY + struct.pack("<I", WIDTH * HEIGHT)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            spec = layout["tiles"].get((x, y), {})
            terrain = spec.get("terrain") or ("TERRAIN_OCEAN" if y in (0, HEIGHT - 1) else "TERRAIN_GRASS")
            body += tile_record(terrain, spec.get("feature"), spec.get("resource"), spec.get("improvement"),
                                river=spec.get("river", 0), owner=spec.get("owner"))
    body += b"\x00" * 4096
    # Player blocks: city objects first, then that player's district records.
    for player in sorted({c["player"] for c in layout["cities"]}):
        own = [c for c in layout["cities"] if c["player"] == player]
        for city in own:
            centre = next((d[1], d[2]) for d in city["districts"] if d[3] == "DISTRICT_CITY_CENTER")
            body += city_object(city["name"], [d[0] for d in city["districts"]], city["built"], centre=centre,
                                working=city.get("working"), locked=city.get("locked"),
                                production=city.get("production"), progress=city.get("progress"))
        for city_index, city in enumerate(own):
            for district_id, x, y, district_type, completed, cost in city["districts"]:
                body += district_record(district_id, x, y, city_index, district_type, completed, cost)
        body += b"\x00" * 2048
        body += (layout.get("player_states") or {}).get(player, b"")
        body += b"\x00" * 512
    body += b"\x00" * (140_000 - len(body)) if len(body) < 140_000 else b""
    return bytes(body)


def slot_entry(index: int, civ: str, leader: str) -> bytes:
    adjective = f"LOC_{civ}_ADJECTIVE\x00".encode()
    entry = struct.pack("<H", index) + b"\x00\x00" + SLOT_INDEX_KEY + b"\x00" * 7
    entry += SLOT_CIV_KEY + b"\x00" * 4 + struct.pack("<H", len(adjective)) + b"\x00" * 6 + adjective
    leader_bytes = (leader + "\x00").encode()
    entry += SLOT_LEADER_KEY + b"\x00" * 4 + struct.pack("<H", len(leader_bytes)) + b"\x00" * 6 + leader_bytes
    return entry + b"\x00" * 32


def trade_route_op(origin: tuple[int, int], destination: tuple[int, int], started_turn: int) -> bytes:
    """A trader's UNITOPERATION_MAKE_TRADE_ROUTE record (turn +16, dest +36, origin +44)."""
    record = bytearray(struct.pack("<I", h("UNITOPERATION_MAKE_TRADE_ROUTE")) + b"\x00" * 60)
    struct.pack_into("<i", record, 16, started_turn)
    struct.pack_into("<4i", record, 36, *destination, *origin)
    return bytes(record)


def envoy_array(tokens: dict[int, int]) -> bytes:
    """u32 64 then 64 player slots of envoys in 1/256 fixed point."""
    return struct.pack("<I", 64) + b"".join(struct.pack("<I", tokens.get(i, 0) * 256) for i in range(64))


def build_save(body: bytes | None = None, *, speed: str = "GAMESPEED_STANDARD",
               extra_slots: tuple[tuple[int, str, str], ...] = ()) -> bytes:
    body = body if body is not None else build_body()
    header = bytearray(b"CIV6" + b"\x00" * 60)
    for text in (speed, "MAPSIZE_DUEL", "RULESET_EXPANSION_2", "DIFFICULTY_PRINCE", "Continents.lua"):
        header += text.encode() + b"\x00" * 12
    header += slot_entry(0, "CIVILIZATION_SUMERIA", "LEADER_GILGAMESH")
    header += slot_entry(1, "CIVILIZATION_EGYPT", "LEADER_CLEOPATRA")
    for index, civ, leader in extra_slots:
        header += slot_entry(index, civ, leader)
    header += b"MOD_TITLE" + b"\x00" * 40
    compressed = zlib.compress(body, 6)
    stream = bytearray()
    for start in range(0, len(compressed), 65536):
        stream += compressed[start : start + 65536] + b"\x00\x00\x00\x00"
    return bytes(header + stream + b"\x00\x00\xFF\xFF" + b"\x00" * 16)
