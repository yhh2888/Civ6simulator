"""Decode map tiles, districts, cities and buildings from a decompressed body.

What is decoded and how it was verified (Gathering Storm saves, 2026-11):

* Tile records (55 bytes + optional blocks) — layout from Civ6EGRM.
  Terrain/feature/resource/improvement hashes resolve to known type names.
* River edges: bit i = NE, E, SE, SW, W, NW (y grows northwards). Verified by
  neighbour consistency: every river edge is mirrored by the adjacent tile.
* Tile ownership block (17 bytes): city index (u16 @0), district id
  (u16 @8, 0xFFFF = none), owning player (u8 @12), wonder building hash (@13).
* District records: ``0F 00 00 00 | id u16 | u16 | x i32 | y i32 |
  city u16 | u16 | DISTRICT hash`` — kept only when the tile ownership block
  agrees (same district id and city on that tile).
* City objects: length-prefixed name followed by 8 zero bytes, 04 00 00 00,
  the district count and the district id pairs. The first id is the city
  centre district record.
* Built buildings: a per-city table of (BUILDING hash, u16 plot index). Plots
  were verified to be the city centre or the matching district tile.
* District record extras (offsets after the type hash): locked-in production
  cost (u32 @ +20) and a completed flag (u8 @ +24; 0 = placed but still under
  construction). Not meaningful for the city centre.
* Citizens: right after the city marker come two 37-entry u32 arrays over the
  plots within 3 tiles, in spiral order (centre, then each ring starting at the
  north-east neighbour scaled by the ring and walking clockwise: SE, SW, W,
  NW, NE, E). Array 1 = citizens on the plot (tile workers; specialists on a
  district plot, up to 3; index 0 is the free city centre). Array 2 = citizens
  on plots the player locked by hand. Verified by a two-save experiment (one
  citizen moved = one entry leaves array 1, one enters array 2) and on five
  saves (~1,300 worked plots, all owned by their city). Population = all
  citizens in both arrays, centre excluded.
* Current production: ``2C0F4A46 | kind u32 (0 unit, 1 building/wonder,
  2 district, 3 project) | 0 | type hash | 0 | x i32 | y i32`` (-9999 = no
  plot). Verified by switching production between two saves. Progress of an
  item = u32 after the first later occurrence of its hash in the city object,
  divided by 256 (fixed point); agrees with the per-kind progress tables in
  284 of 284 cities and matched a district the player confirmed in game.
  Only one entry was ever seen; longer queues are not decoded.

* Player state (per player, in player-index order): technologies and civics
  are three tables each — ``count | (hash, u8 done)``, ``count | (hash, u8
  boosted)``, ``count | (hash, u32 progress x 256)``. Policies: five
  140-entry ``(hash, u8)`` tables, four (key, government) pairs just before them (one per government tier;
  the current one is the tier whose slot counts match the slot list), then
  the slot list ``n | n | (policy hash, slot code)``; slot codes 0/1/2/4 =
  economic/military/diplomatic/wildcard (matched the government's slot
  counts). Each block is checked against the player's own city objects
  (it must lie between that player's cities and the next player's).

Not decoded: food storage, governors, religion, trade routes, units.
"""

from __future__ import annotations

import re
import struct
from collections import Counter
from dataclasses import dataclass, field

from app.services.civ6save.container import SaveContainer, SaveFormatError, read_container
from app.services.civ6save.hashes import NO_TYPE, TypeNames, type_hash

# Map sizes known from Civ6EGRM (tile count → width, height).
MAP_SIZES = {
    1144: (44, 26), 2280: (60, 38), 3404: (74, 46), 3696: (84, 44), 4536: (84, 54),
    4800: (96, 50), 5760: (96, 60), 5767: (79, 73), 6048: (108, 56), 6656: (104, 64),
    6996: (106, 66), 7344: (108, 68), 7440: (120, 62), 10360: (140, 74),
    16920: (180, 94), 20800: (200, 104), 26680: (230, 116),
}
MAP_START_KEYS = (
    b"\x0E\x00\x00\x00\x0F\x00\x00\x00\x06\x00\x00\x00",
    b"\x16\x00\x00\x00\x17\x00\x00\x00\x06\x00\x00\x00",
    b"\x00\x00\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\x06\x00\x00\x00",
)
TILE_BASE = 55
RIVER_DIRECTIONS = ("NE", "E", "SE", "SW", "W", "NW")
FREE_CITIES_PLAYER = 62
BARBARIAN_PLAYER = 63
DISTRICT_RECORD_TAG = b"\x0F\x00\x00\x00"
CITY_MARKER = b"\x02\x00\x00\x00\x25\x00\x00\x00"
CITY_PLOTS = 37
PRODUCTION_MARKER = bytes.fromhex("464A0F2C")
PRODUCTION_KINDS = {0: "unit", 1: "building", 2: "district", 3: "project"}
NO_PLOT = -9999
FIXED_POINT = 256.0
SPIRAL_START = "NE"
SPIRAL_WALK = ("SE", "SW", "W", "NW", "NE", "E")


def _u8(b: bytes, o: int) -> int:
    return b[o]


def _u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def _i16(b: bytes, o: int) -> int:
    return struct.unpack_from("<h", b, o)[0]


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def neighbor(x: int, y: int, direction: str, width: int) -> tuple[int, int]:
    """Odd rows shifted east, y grows northwards (game coordinates)."""
    odd = y & 1
    dx, dy = {
        "E": (1, 0), "W": (-1, 0),
        "NE": (1 if odd else 0, 1), "NW": (0 if odd else -1, 1),
        "SE": (1 if odd else 0, -1), "SW": (0 if odd else -1, -1),
    }[direction]
    return (x + dx) % width, y + dy


def city_plots(cx: int, cy: int, width: int) -> list[tuple[int, int]]:
    """The 37 plots within 3 tiles in the order the save stores them.

    y may fall outside the map for cities near the poles; callers check it.
    """
    plots = [(cx, cy)]
    for ring in range(1, 4):
        x, y = cx, cy
        for _ in range(ring):
            x, y = neighbor(x, y, SPIRAL_START, width)
        for direction in SPIRAL_WALK:
            for _ in range(ring):
                plots.append((x, y))
                x, y = neighbor(x, y, direction, width)
    return plots


def hex_distance(x1: int, y1: int, x2: int, y2: int) -> int:
    q1 = x1 - (y1 - (y1 & 1)) // 2
    q2 = x2 - (y2 - (y2 & 1)) // 2
    return max(abs(q1 - q2), abs(y1 - y2), abs(q1 + y1 - q2 - y2))


@dataclass
class SaveTile:
    index: int
    x: int
    y: int
    terrain: str
    feature: str | None
    resource: str | None
    improvement: str | None
    road: int
    appeal: int
    river_edges: list[str]
    cliff_edges: list[str]
    player: int | None = None
    city_index: int | None = None
    district_id: int | None = None
    wonder: str | None = None
    offset: int = 0  # record offset in the body (debugging, yield overlays)
    extra: int = 0   # bytes after the 55-byte base record
    # byte 48 bit 0: the improvement is pillaged. On a turn-90 save the bit is
    # set on 15 tiles, all with an improvement (never on empty plots or
    # districts), including the truffle camp the game shows at 3 gold instead of 5.
    pillaged: bool = False


@dataclass
class SaveDistrict:
    id: int
    type: str
    x: int
    y: int
    city_index: int
    player: int | None
    verified: bool
    offset: int
    completed: bool = True
    cost: int | None = None


@dataclass
class SaveCity:
    name: str
    raw_name: str
    player: int
    city_index: int
    x: int
    y: int
    population: int | None
    districts: list[SaveDistrict]
    buildings: list[dict]
    owned_tiles: int
    verified: bool
    offset: int
    # {"tiles": [{x, y, count, locked}], "specialists": [{x, y, count, district}],
    #  "verified": bool} or None when the arrays were not found.
    citizens: dict | None = None
    # {"kind", "type", "x", "y", "progress"} or None (nothing in production).
    production: dict | None = None
    # Placed but unfinished districts / wonders: [{"category", "type", "x", "y", "progress", "cost"}].
    construction: list[dict] = field(default_factory=list)


@dataclass
class SaveData:
    width: int
    height: int
    tiles: list[SaveTile]
    districts: list[SaveDistrict]
    cities: list[SaveCity]
    header: dict
    turn_estimate: int | None
    players: dict[int, dict]
    warnings: list[str] = field(default_factory=list)
    unknown_types: dict[str, int] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    # player index → {"technologies", "boosted_technologies", "tech_progress",
    #  "civics", "inspired_civics", "civic_progress", "government_candidates"
    #  (one per tier, None = empty), "policies": [{"policy", "slot"}], "verified"}
    player_states: dict[int, dict] = field(default_factory=dict)
    # [{"origin": (x, y), "origin_player", "destination": (x, y),
    #   "destination_player", "started_turn"}]
    trade_routes: list[dict] = field(default_factory=list)
    # [{"type", "owner", "city_index", "turns", "promotions"}] (named records only)
    governors: list[dict] = field(default_factory=list)
    # [{"resolution", "target_kind", "target", "votes": [1|2, ...], "effect"}]
    congress: list[dict] = field(default_factory=list)
    # great works by city: (player, city_index) → [GREATWORK_...]
    great_works: dict = field(default_factory=dict)
    # (x, y) → {"YIELD_FOOD": 1, ...}: permanent plot yields added by
    # natural events (flood, volcano, storm fertility)
    plot_yields: dict = field(default_factory=dict)

    def tile(self, x: int, y: int) -> SaveTile:
        return self.tiles[y * self.width + x]


# --------------------------------------------------------------------- tiles
def _find_map_start(body: bytes) -> tuple[int, int]:
    for key in MAP_START_KEYS:
        position = body.find(key)
        while position >= 0:
            count = _u32(body, position + len(key))
            if count in MAP_SIZES:
                return position + len(key) + 4, count
            position = body.find(key, position + 1)
    raise SaveFormatError("맵 타일 블록을 찾지 못했습니다 (지원하지 않는 맵 크기이거나 세이브 구조가 다릅니다).")


def _edges(bits: int) -> list[str]:
    return [RIVER_DIRECTIONS[i] for i in range(6) if bits >> i & 1]


def _parse_tiles(body: bytes, names: TypeNames) -> tuple[int, int, list[SaveTile], int]:
    start, count = _find_map_start(body)
    width, height = MAP_SIZES[count]
    terrains = set(names.with_prefix("TERRAIN_"))
    features = set(names.with_prefix("FEATURE_")) | {NO_TYPE}
    extra20_improvements = {type_hash("IMPROVEMENT_SKI_RESORT"), type_hash("IMPROVEMENT_MOUNTAIN_TUNNEL")}
    extra20_features = {type_hash("FEATURE_SUK_GALAPAGOS"), type_hash("FEATURE_GALAPAGOS"), type_hash("FEATURE_PAMUKKALE")}
    barricade = 1765528555  # observed improvement hash with a 20-byte block (Civ6EGRM)
    terrain_tags = [struct.pack("<I", h) for h in terrains]

    def valid_at(offset: int) -> bool:
        return (
            offset + 20 <= len(body)
            and _u32(body, offset + 12) in terrains
            and _u32(body, offset + 16) in features
        )

    tiles: list[SaveTile] = []
    resyncs = 0
    m = start
    for i in range(count):
        if not valid_at(m):
            raise SaveFormatError(f"타일 {i}번 레코드를 해독하지 못했습니다.")
        own_flag = _u8(body, m + 49)
        overlays = _u32(body, m + 51)
        improvement = _u32(body, m + 33)
        feature = _u32(body, m + 16)
        extra = {0: 0, 1: 24, 2: 44, 3: 64}.get(overlays, 0)
        if own_flag >= 64:
            extra += 17
        if improvement in extra20_improvements or feature in extra20_features or improvement == barricade:
            extra += 20
        if i < count - 1 and not valid_at(m + TILE_BASE + extra):
            for alt in (extra - 20, extra + 20, extra + 17, extra - 17):
                if alt >= 0 and valid_at(m + TILE_BASE + alt):
                    extra = alt
                    break
            else:
                # Resynchronise on the nearest following terrain hash.
                window = body[m + TILE_BASE : m + TILE_BASE + 600]
                hits = [window.find(tag) for tag in terrain_tags]
                hits = [h - 12 for h in hits if h >= 12]
                hits = [h for h in sorted(hits) if valid_at(m + TILE_BASE + h)]
                if not hits:
                    raise SaveFormatError(f"타일 {i}번 다음 레코드 위치를 찾지 못했습니다.")
                extra = hits[0]
                resyncs += 1
        block = body[m + TILE_BASE : m + TILE_BASE + extra]
        player = city_index = district_id = wonder = None
        if own_flag >= 64 and len(block) >= 17:
            own = block[-17:]
            player = own[12]
            city_index = _u16(own, 0)
            raw_district = _u16(own, 8)
            district_id = None if raw_district == 0xFFFF else raw_district
            wonder = names.name_or_unknown(_u32(own, 13))
        tiles.append(
            SaveTile(
                offset=m,
                extra=extra,
                index=i,
                x=i % width,
                y=i // width,
                terrain=names.name_or_unknown(_u32(body, m + 12)) or "TERRAIN_OCEAN",
                feature=names.name_or_unknown(feature),
                resource=names.name_or_unknown(_u32(body, m + 27)),
                improvement=names.name_or_unknown(improvement),
                road=_u8(body, m + 38),
                appeal=_i16(body, m + 40),
                river_edges=_edges(_u8(body, m + 46)),
                cliff_edges=_edges(_u8(body, m + 47)),
                player=player,
                city_index=city_index,
                district_id=district_id,
                wonder=wonder,
                pillaged=bool(improvement not in (0, 0xFFFFFFFF) and _u8(body, m + 48) & 1),
            )
        )
        m += TILE_BASE + extra
    if resyncs > max(5, count // 50):
        raise SaveFormatError(f"타일 레코드 정렬이 {resyncs}번 어긋났습니다. 이 세이브 구조는 지원하지 않습니다.")
    return width, height, tiles, resyncs


# ----------------------------------------------------------------- districts
def _parse_districts(body: bytes, names: TypeNames, width: int, height: int, tiles: list[SaveTile]) -> list[SaveDistrict]:
    hashes = {struct.pack("<I", h): n for h, n in names.with_prefix("DISTRICT_").items()}
    if not hashes:
        return []
    pattern = re.compile(b"|".join(re.escape(k) for k in hashes))
    found: dict[tuple, SaveDistrict] = {}
    for match in pattern.finditer(body):
        p = match.start()
        if p < 20 or body[p - 20 : p - 16] != DISTRICT_RECORD_TAG:
            continue
        district_id = _u16(body, p - 16)
        x, y = struct.unpack_from("<ii", body, p - 12)
        if not (0 <= x < width and 0 <= y < height):
            continue
        city_index = _u16(body, p - 4)
        tile = tiles[y * width + x]
        verified = tile.player is not None and tile.district_id == district_id and tile.city_index == city_index
        key = (tile.player, district_id, x, y)
        if key in found or not verified:
            # Unverified matches are byte patterns that only look like a
            # district record (or stale history entries); they are dropped.
            continue
        found[key] = SaveDistrict(
            id=district_id, type=hashes[match.group()], x=x, y=y, city_index=city_index,
            player=tile.player, verified=verified, offset=p,
            completed=body[p + 24] != 0 if p + 25 <= len(body) else True,
            cost=_u32(body, p + 20) if p + 24 <= len(body) else None,
        )
    return list(found.values())


# -------------------------------------------------------------------- cities
CITY_SUFFIX = b"\x00" * 8 + b"\x04\x00\x00\x00"


def pretty_city_name(raw: str) -> str:
    for prefix in ("LOC_CITY_NAME_", "LOC_CITY_"):
        if raw.startswith(prefix):
            return " ".join(part.capitalize() for part in raw[len(prefix):].split("_") if part)
    return raw


def _city_objects(body: bytes) -> list[dict]:
    """City objects: ``len u32 | name | 8 x 00 | 04 00 00 00 | n | n id pairs``."""
    objects = []
    q = body.find(CITY_SUFFIX)
    while q >= 0:
        s = q + len(CITY_SUFFIX)
        found = None
        for length in range(1, 81):
            p = q - length
            if p < 4:
                break
            if _u32(body, p - 4) == length:
                raw = body[p:q]
                if all(c >= 0x20 for c in raw):
                    found = (p, raw)
                break
        if found and s + 4 <= len(body):
            p, raw = found
            n = _u32(body, s)
            if 0 < n < 64 and s + 4 + 4 * n <= len(body):
                ids = [_u16(body, s + 4 + 4 * k) for k in range(n)]
                if len(set(ids)) == n:
                    try:
                        name = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        name = None
                    if name:
                        objects.append({"raw_name": name, "offset": p, "district_ids": ids,
                                        **_citizen_arrays(body, s)})
        q = body.find(CITY_SUFFIX, q + 1)
    return objects


def _citizen_arrays(body: bytes, start: int) -> dict:
    marker = body.find(CITY_MARKER, start, start + 600)
    if marker < 0:
        return {"working": None, "locked": None}
    o = marker + 4
    arrays = []
    for _ in range(2):
        if o + 4 + 4 * CITY_PLOTS > len(body) or _u32(body, o) != CITY_PLOTS:
            return {"working": None, "locked": None}
        values = list(struct.unpack_from(f"<{CITY_PLOTS}I", body, o + 4))
        if any(v > 8 for v in values):
            return {"working": None, "locked": None}
        arrays.append(values)
        o += 4 + 4 * CITY_PLOTS
    return {"working": arrays[0], "locked": arrays[1]}


def _progress(body: bytes, after: int, end: int, value_hash: int) -> float | None:
    """Production progress of an item: first later occurrence of its hash."""
    tag = struct.pack("<I", value_hash)
    position = body.find(tag, after, end)
    if position < 0 or position + 8 > end:
        return None
    raw = _u32(body, position + 4)
    if raw > 100_000 * FIXED_POINT:
        return None
    return raw / FIXED_POINT


def _production(body: bytes, names: TypeNames, start: int, end: int) -> tuple[dict | None, int | None]:
    """Current production entry of a city object (or None) and its offset."""
    q = body.find(PRODUCTION_MARKER, start, end)
    if q < 0 or body[q + 8 : q + 12] != PRODUCTION_MARKER:
        return None, None
    kind, _zero, value_hash, _zero2, x, y = struct.unpack_from("<IIIIii", body, q + 12)
    if kind not in PRODUCTION_KINDS or value_hash in (0, 0xFFFFFFFF):
        return None, q
    placed = kind in (1, 2) and x != NO_PLOT and y != NO_PLOT
    entry = {
        "kind": PRODUCTION_KINDS[kind],
        "type": names.name_or_unknown(value_hash),
        "x": x if placed else None,
        "y": y if placed else None,
        "progress": _progress(body, q + 36, end, value_hash),
        "queue_length": _u32(body, q - 4),
    }
    return entry, q


def _building_damage_flags(body: bytes, start: int, end: int, buildings: dict[bytes, str]) -> dict[str, int]:
    """Per-building u8 after each hash in the city's stride-5 building table.
    1 = pillaged (needs repair): on a turn-90 save only Mediolanum's water
    mill, which the game shows as needing repair, has it set."""
    o = start
    while o + 14 <= end:
        if body[o:o + 4] in buildings and body[o + 5:o + 9] in buildings and body[o + 10:o + 14] in buildings:
            while o - 5 >= start and body[o - 5:o - 1] in buildings:
                o -= 5
            flags = {}
            while o + 5 <= end and body[o:o + 4] in buildings:
                flags[buildings[body[o:o + 4]]] = body[o + 4]
                o += 5
            if len(flags) >= 10 and all(v in (0, 1) for v in flags.values()):
                return flags
        o += 1
    return {}


def _building_tables(body: bytes, start: int, end: int, buildings: dict[bytes, str]) -> list[list[tuple[str, int]]]:
    """All (count-prefixed or not) stride-6 BUILDING tables in [start, end)."""
    tables = []
    if not buildings:
        return tables
    pattern = re.compile(b"|".join(re.escape(k) for k in buildings))
    position = start
    while True:
        match = pattern.search(body, position, end)
        if match is None:
            break
        p = match.start()
        rows = []
        o = p
        while o + 6 <= end and body[o : o + 4] in buildings:
            rows.append((buildings[body[o : o + 4]], _u16(body, o + 4)))
            o += 6
        if len(rows) >= 20:
            tables.append(rows)
            position = o
        else:
            position = p + 1
    return tables


def _parse_cities(body: bytes, names: TypeNames, width: int, height: int, tiles: list[SaveTile],
                  districts: list[SaveDistrict], warnings: list[str]) -> list[SaveCity]:
    centres: dict[int, list[SaveDistrict]] = {}
    for district in districts:
        if district.type == "DISTRICT_CITY_CENTER":
            centres.setdefault(district.id, []).append(district)
    buildings = {struct.pack("<I", h): n for h, n in names.with_prefix("BUILDING_").items()}
    objects = _city_objects(body)
    objects.sort(key=lambda o: o["offset"])
    cities: list[SaveCity] = []
    used: set[tuple] = set()
    for number, obj in enumerate(objects):
        candidates = [d for d in centres.get(obj["district_ids"][0], [])
                      if d.offset > obj["offset"] and (d.player, d.id, d.x, d.y) not in used]
        if not candidates:
            continue
        centre = min(candidates, key=lambda d: d.offset)
        centre_tile = tiles[centre.y * width + centre.x]
        if centre.player is None or centre_tile.city_index != centre.city_index:
            continue
        used.add((centre.player, centre.id, centre.x, centre.y))
        own_districts = [d for d in districts if d.player == centre.player and d.city_index == centre.city_index]
        end = objects[number + 1]["offset"] if number + 1 < len(objects) else obj["offset"] + 24000
        end = min(end, obj["offset"] + 24000)
        # The second stride-6 building table holds the built buildings with
        # their plot index (0xFFFF = not built). Validate by plot distance.
        built: list[dict] = []
        tables = _building_tables(body, obj["offset"] + 4000, end, buildings)
        chosen: list[tuple[str, int]] | None = None
        if len(tables) >= 2:
            present = [(n, v) for n, v in tables[1] if v != 0xFFFF]
            if all(v < width * height and hex_distance(v % width, v // width, centre.x, centre.y) <= 3
                   for _, v in present):
                chosen = present
        if chosen is None:
            warnings.append(f"{pretty_city_name(obj['raw_name'])}: 건물 목록을 확인하지 못해 비워 두었습니다.")
            chosen = []
        damaged = _building_damage_flags(body, obj["offset"] + 4000, end, buildings)
        for name, plot in chosen:
            built.append({"type": name, "x": plot % width, "y": plot // width,
                          **({"pillaged": True} if damaged.get(name) else {})})
        owned = sum(1 for t in tiles if t.player == centre.player and t.city_index == centre.city_index)
        citizens, population = _citizens(obj, centre, own_districts, tiles, width, height)
        production, production_at = _production(body, names, obj["offset"], end)
        construction = []
        if production_at is not None:
            built_types = {b["type"] for b in built}
            for district in own_districts:
                if district.completed or district.type == "DISTRICT_CITY_CENTER":
                    continue
                if district.type == "DISTRICT_WONDER":
                    tile = tiles[district.y * width + district.x]
                    wonder = tile.wonder
                    if not wonder or wonder in built_types:
                        continue
                    construction.append({
                        "category": "wonder", "type": wonder, "x": district.x, "y": district.y,
                        "progress": _progress(body, production_at + 36, end, type_hash(wonder))
                        if not wonder.startswith("UNKNOWN_") else None,
                        "cost": None,
                    })
                else:
                    construction.append({
                        "category": "district", "type": district.type, "x": district.x, "y": district.y,
                        "progress": _progress(body, production_at + 36, end, type_hash(district.type)),
                        "cost": district.cost,
                    })
        cities.append(
            SaveCity(
                name=pretty_city_name(obj["raw_name"]), raw_name=obj["raw_name"], player=centre.player,
                city_index=centre.city_index, x=centre.x, y=centre.y, population=population,
                districts=own_districts, buildings=built, owned_tiles=owned, verified=centre.verified,
                offset=obj["offset"], citizens=citizens, production=production, construction=construction,
            )
        )
    return cities


def _citizens(obj: dict, centre: SaveDistrict, districts: list[SaveDistrict], tiles: list[SaveTile],
              width: int, height: int) -> tuple[dict | None, int | None]:
    working, locked = obj.get("working"), obj.get("locked")
    if working is None or locked is None:
        return None, None
    district_at = {(d.x, d.y): d.type for d in districts}
    worked_tiles, specialists = [], []
    verified = True
    population = 0
    for index, (x, y) in enumerate(city_plots(centre.x, centre.y, width)):
        count, held = working[index], locked[index]
        if index == 0 or not (count or held):
            continue
        population += count + held
        if not 0 <= y < height:
            verified = False
            continue
        tile = tiles[y * width + x]
        if tile.player != centre.player or tile.city_index != centre.city_index:
            verified = False
        district = district_at.get((x, y))
        if district and district != "DISTRICT_CITY_CENTER":
            specialists.append({"x": x, "y": y, "count": count + held, "district": district, "locked": bool(held)})
        else:
            worked_tiles.append({"x": x, "y": y, "count": count + held, "locked": bool(held)})
    return {"tiles": worked_tiles, "specialists": specialists, "verified": verified}, (population or None)


def _turn_estimate(body: bytes) -> int | None:
    """Per-player 'turns played' list near the start of the body (lucien analysis).

    Several byte alignments can look like a list of small numbers; the real
    one is the run with the smallest values.
    """
    runs = []
    for start in range(100, 400):
        values = []
        o = start
        while o + 4 <= len(body) and len(values) < 70:
            v = _u32(body, o)
            if not 0 < v < 5000:
                break
            values.append(v)
            o += 4
        if len(values) >= 8 and Counter(values).most_common(1)[0][1] >= len(values) // 2:
            runs.append(max(values))
    return min(runs) if runs else None


# -------------------------------------------------------------- player state
SLOT_CODES = {0: "SLOT_ECONOMIC", 1: "SLOT_MILITARY", 2: "SLOT_DIPLOMATIC", 4: "SLOT_WILDCARD"}
POLICY_SUBTABLES = 5


# Types every ruleset has; each per-player table lists every type of its kind,
# so finding one of these locates all tables quickly (memmem instead of a
# regex over hundreds of alternatives).
TABLE_ANCHORS = {
    "TECH_": ("TECH_POTTERY", "TECH_MINING", "TECH_WRITING", "TECH_SAILING"),
    "CIVIC_": ("CIVIC_CODE_OF_LAWS", "CIVIC_CRAFTSMANSHIP", "CIVIC_FOREIGN_TRADE"),
    "POLICY_": ("POLICY_DISCIPLINE", "POLICY_URBAN_PLANNING", "POLICY_GOD_KING", "POLICY_SURVEY"),
}


def _table_at(body: bytes, hashes: set[bytes], stride: int, p: int, min_count: int) -> int | None:
    n = _u32(body, p)
    if not min_count <= n < 1000 or p + 4 + stride * n > len(body):
        return None
    good = sum(1 for j in range(n) if body[p + 4 + stride * j : p + 8 + stride * j] in hashes)
    return n if good >= n * 0.9 else None


def _hash_tables(body: bytes, hashes: set[bytes], stride: int, min_count: int = 20,
                 anchors: tuple[bytes, ...] = ()) -> list[tuple[int, int]]:
    """Count-prefixed tables of (type hash, value) rows: [(offset of count, n)]."""
    if not hashes:
        return []
    starts: set[int] = set()
    anchors = tuple(a for a in anchors if a in hashes)
    if anchors:
        for anchor in anchors:
            q = body.find(anchor)
            while q >= 0:
                r = q
                # walk back to the first row; the count sits just before it
                while r - stride >= 4 and body[r - stride : r - stride + 4] in hashes:
                    r -= stride
                starts.add(r - 4)
                q = body.find(anchor, q + 1)
    else:
        pattern = re.compile(b"|".join(re.escape(h) for h in hashes))
        starts = {m.start() - 4 for m in pattern.finditer(body)}
    found = []
    last_end = -1
    for p in sorted(starts):
        if p < 0 or p < last_end:
            continue
        n = _table_at(body, hashes, stride, p, min_count)
        if n is not None:
            found.append((p, n))
            last_end = p + 4 + stride * n
    if not found and anchors:
        return _hash_tables(body, hashes, stride, min_count)  # anchors absent: full scan
    return found


def _anchors(prefix: str) -> tuple[bytes, ...]:
    return tuple(struct.pack("<I", type_hash(name)) for name in TABLE_ANCHORS.get(prefix, ()))


def _read_table(body: bytes, names: TypeNames, offset: int, stride: int) -> list[tuple[str | None, int]]:
    n = _u32(body, offset)
    rows = []
    for j in range(n):
        o = offset + 4 + stride * j
        value = body[o + 4] if stride == 5 else _u32(body, o + 4)
        rows.append((names.name(_u32(body, o)), value))
    return rows


def _triples(body: bytes, names: TypeNames, prefix: str) -> list[dict]:
    """(done, boosted, progress) table triples in file order."""
    hashes = {struct.pack("<I", h) for h in names.with_prefix(prefix)}
    singles = _hash_tables(body, hashes, 5, anchors=_anchors(prefix))
    triples = []
    for i, (offset, n) in enumerate(singles):
        second = offset + 4 + 5 * n
        third = second + 4 + 5 * n
        if (i + 1 < len(singles) and singles[i + 1] == (second, n) and third + 4 + 8 * n <= len(body)
                and _u32(body, third) == n):
            done = [t for t, v in _read_table(body, names, offset, 5) if t and v]
            boosted = [t for t, v in _read_table(body, names, second, 5) if t and v]
            progress = {t: round(v / FIXED_POINT, 4) for t, v in _read_table(body, names, third, 8) if t and v}
            triples.append({"offset": offset, "done": done, "boosted": boosted, "progress": progress})
    # a table can only start one triple
    result, used = [], set()
    for triple in triples:
        if triple["offset"] in used:
            continue
        used.update({triple["offset"]})
        result.append(triple)
    filtered = []
    for triple in result:
        if filtered and triple["offset"] < filtered[-1]["offset"] + 10:
            continue
        filtered.append(triple)
    # drop the "boosted" table of a triple that was also seen as a start
    starts = {t["offset"] for t in filtered}
    return [t for t in filtered if not any(t["offset"] == s + 4 + 5 * len(_read_table(body, names, s, 5)) for s in starts)]


def _policy_blocks(body: bytes, names: TypeNames) -> list[dict]:
    hashes = {struct.pack("<I", h) for h in names.with_prefix("POLICY_")}
    tables = _hash_tables(body, hashes, 5, anchors=_anchors("POLICY_"))
    governments = names.with_prefix("GOVERNMENT_")
    blocks = []
    i = 0
    while i < len(tables):
        offset, n = tables[i]
        chain = [tables[i]]
        while len(chain) < POLICY_SUBTABLES and i + len(chain) < len(tables):
            nxt = tables[i + len(chain)]
            if nxt[0] != chain[-1][0] + 4 + 5 * chain[-1][1] or nxt[1] != n:
                break
            chain.append(nxt)
        if len(chain) < POLICY_SUBTABLES:
            i += 1
            continue
        end = chain[-1][0] + 4 + 5 * n
        # Four (key, government) pairs precede the tables, one per government
        # tier; the importer picks the one whose slots match the slot list.
        candidates = []
        for back in range(28, 0, -8):
            value = _u32(body, offset - back)
            candidates.append(governments.get(value))
        slots, layout = [], []
        count = _u32(body, end)
        if 0 < count <= 16 and _u32(body, end + 4) == count:
            for j in range(count):
                value_hash, code = struct.unpack_from("<II", body, end + 8 + 8 * j)
                layout.append(SLOT_CODES.get(code, f"SLOT_{code}"))
                policy = names.name(value_hash)
                if policy:
                    slots.append({"policy": policy, "slot": SLOT_CODES.get(code), "slot_code": code})
        blocks.append({"offset": offset, "government_candidates": candidates, "policies": slots,
                       "slot_layout": layout})
        i += POLICY_SUBTABLES
    return blocks


ENVOY_ARRAY_PLAYERS = 64


def _envoy_arrays(body: bytes, majors: set[int]) -> list[tuple[int, dict[int, int]]]:
    """Envoys each major player has sent: ``u32 64`` then 64 × u32 tokens in
    1/256 fixed point, indexed by player slot. Only arrays whose non-zero
    entries are all non-major slots (city-states) qualify.
    Verified against the in-game envoy screen (Vilnius 3, Auckland 4, Kabul 3,
    Preslav 4, Bologna 9)."""
    count = struct.pack("<I", ENVOY_ARRAY_PLAYERS)
    found = []
    q = body.find(count)
    while q >= 0:
        p = q + 4
        if p + 4 * ENVOY_ARRAY_PLAYERS <= len(body):
            values = struct.unpack_from(f"<{ENVOY_ARRAY_PLAYERS}I", body, p)
            if any(values) and all(v % 256 == 0 and v // 256 <= 99 for v in values):
                tokens = {i: v // 256 for i, v in enumerate(values) if v}
                if not majors.intersection(tokens):
                    found.append((q, tokens))
        q = body.find(count, q + 1)
    return found


def _player_states(body: bytes, names: TypeNames, cities: list[SaveCity], warnings: list[str],
                   slots: dict[int, dict] | None = None) -> dict[int, dict]:
    techs = _triples(body, names, "TECH_")
    civics = _triples(body, names, "CIVIC_")
    policies = _policy_blocks(body, names)
    first_city: dict[int, int] = {}
    for city in sorted(cities, key=lambda c: c.offset):
        first_city.setdefault(city.player, city.offset)
    bounds = sorted(first_city.items())

    def plausible(player: int, offset: int) -> bool:
        start = first_city.get(player)
        if start is None:
            return True  # no city to check against
        later = [o for p, o in bounds if p > player]
        return start < offset and (not later or offset < min(later))

    states: dict[int, dict] = {}
    for player in range(max(len(techs), len(civics), len(policies))):
        state: dict = {"verified": True}
        if player < len(techs):
            state.update(technologies=techs[player]["done"], boosted_technologies=techs[player]["boosted"],
                         tech_progress=techs[player]["progress"])
            state["verified"] &= plausible(player, techs[player]["offset"])
        if player < len(civics):
            state.update(civics=civics[player]["done"], inspired_civics=civics[player]["boosted"],
                         civic_progress=civics[player]["progress"])
            state["verified"] &= plausible(player, civics[player]["offset"])
        if player < len(policies):
            state.update(government_candidates=policies[player]["government_candidates"],
                         policies=policies[player]["policies"], slot_layout=policies[player]["slot_layout"])
            state["verified"] &= plausible(player, policies[player]["offset"])
        states[player] = state
    # Envoys: one array per major, in player order inside the player's block.
    # Majors from the setup slots (leaders not LEADER_MINOR_CIV_*).
    majors = {p for p, slot in (slots or {}).items() if not slot.get("minor")}
    arrays = _envoy_arrays(body, majors) if majors else []
    # Only arrays inside a major's block count; city-states keep similar
    # arrays in their own blocks (a turn-90 save has one in Kabul's block with
    # Auckland 7 / Kabul 11 that no major sent), which would otherwise wrongly
    # outbid the real suzerain.
    owner_at = _block_owner(cities)
    arrays = [(offset, tokens) for offset, tokens in arrays if owner_at(offset) in majors]
    for player in list(states):
        if player not in majors:
            continue
        mine = [tokens for offset, tokens in arrays if plausible(player, offset)
                and (first_city.get(player) is not None) and player not in tokens]
        if mine:
            states[player]["envoys"] = mine[0]
    # Beliefs named inside each major's block (the pantheon is among them;
    # the importer keeps the ones whose class is BELIEF_CLASS_PANTHEON).
    belief_hashes = [(struct.pack("<I", h), name) for h, name in names.with_prefix("BELIEF_").items()]
    starts = sorted(first_city.items(), key=lambda item: item[1])
    for player in list(states):
        start = first_city.get(player)
        if player not in majors or start is None:
            continue
        end = min([o for _, o in starts if o > start] or [len(body)])
        found = sorted((body.find(h, start, end), name) for h, name in belief_hashes if body.find(h, start, end) >= 0)
        states[player]["beliefs_in_block"] = [name for _, name in found]
    others_max: dict[int, dict[int, int]] = {}
    for player in states:
        best: dict[int, int] = {}
        for offset, tokens in arrays:
            if states[player].get("envoys") is tokens:
                continue
            for cs, n in tokens.items():
                best[cs] = max(best.get(cs, 0), n)
        others_max[player] = best
    for player, state in states.items():
        if "envoys" in state:
            # Suzerain: at least 3 envoys and strictly more than any other major.
            state["suzerain_of"] = sorted(cs for cs, n in state["envoys"].items()
                                          if n >= 3 and n > others_max[player].get(cs, 0))
    return states


def _block_owner(cities: list[SaveCity]):
    """offset → player whose block holds it (first city of the owner ≤ offset <
    next player's first city)."""
    first_city: dict[int, int] = {}
    for city in sorted(cities, key=lambda c: c.offset):
        first_city.setdefault(city.player, city.offset)
    starts = sorted((offset, player) for player, offset in first_city.items())

    def owner_at(offset: int) -> int | None:
        owner = None
        for start, player in starts:
            if start <= offset:
                owner = player
        return owner

    return owner_at


GOVERNOR_NAME = re.compile(rb"LOC_GOVERNOR_[A-Z_]+_NAME")


RECORD_SPAN = 160


def _governors(body: bytes, names: TypeNames, cities: list[SaveCity]) -> list[dict]:
    """Appointed governors.

    Each appointed governor is stored as GOVERNOR_ hash, u32 length, its
    LOC_..._NAME string, then a record whose first bytes are
    00, u16 ?, u16 city index, u16 city id; the same (index, id) pair appears
    again near the end followed by u16 turns spent in the city (5 = the
    GOVERNOR_BASE_TURNS_TO_ESTABLISH value, so established). The promotion list
    follows: 5-byte entries (promotion hash, u8 has-it flag).
    Checked on a turn-90 save: Liang in Puteoli (5), Magnus in Aigai (5),
    Pingala in Pella with 3 turns (game: establishing, 2 turns left).
    Only governors stored with a name string are found. AI governors may
    have no promotion list (Kongo's Moksha in Mbanza Kongo, turn 31: base
    title only, 5 turns, established).
    """
    owner_at = _block_owner(cities)
    found = []
    for match in GOVERNOR_NAME.finditer(body):
        start = match.start() - 8
        if start < 0:
            continue
        governor = names.name(_u32(body, start))
        if not governor or not governor.startswith("GOVERNOR_") or governor.startswith("GOVERNOR_PROMOTION"):
            continue
        if _u32(body, match.start() - 4) != match.end() - match.start():
            continue
        # One byte (0 on the player's own governors, 5 on an AI's established
        # Moksha) separates the name from the record.
        record_start = match.end() + 1
        o = record_start
        while o < record_start + RECORD_SPAN and o + 4 <= len(body):
            name = names.name(_u32(body, o))
            if name and name.startswith("GOVERNOR_PROMOTION"):
                break
            o += 1
        # AI governors can have no promotion list: the record then ends where
        # the (index, id, turns) tail does, within RECORD_SPAN bytes.
        record = body[record_start:o]
        if len(record) < 16:
            continue
        city_index, city_id = struct.unpack_from("<HH", record, 3)
        pair = struct.pack("<HH", city_index, city_id)
        turns = None
        tail = record.rfind(pair)
        if tail > 6 and tail + 6 <= len(record):
            turns = _u16(record, tail + 4)
        promotions = []
        while o + 5 <= len(body):
            name = names.name(_u32(body, o))
            if not name or not name.startswith("GOVERNOR_PROMOTION"):
                break
            if body[o + 4]:
                promotions.append(name)
            o += 5
        found.append({"type": governor, "owner": owner_at(start),
                      "city_index": None if city_index == 0xFFFF else city_index,
                      "turns": turns, "promotions": promotions, "offset": start})
    return found


def _congress(body: bytes, names: TypeNames) -> list[dict]:
    """World Congress resolutions of the last session (heuristic).

    The session stores, per resolution that was voted on: resolution hash,
    its TargetKind hash (PLAYER, RESOURCE, ...), i32 chosen target, then one
    i32 per voter with the effect voted for (1 = A, 2 = B), padded with -1.
    The effect that got more votes is taken as the result. Checked on a
    turn-90 save: Trade Treaty with target player 0 and votes 7×A / 3×B, which
    matches the +4 gold on routes into that player's cities in game.
    """
    resolutions = {name: value for value, name in names.with_prefix("WC_RES_").items()}
    kinds = {}
    for kind in ("PLAYER", "RESOURCE", "YIELD", "DISTRICT", "BUILDING", "PROJECT", "RELIGION", "FEATURE",
                 "GOVERNMENT", "GREATPERSONCLASS", "GREATWORKOBJECT", "MINORCIVBONUS", "UNITOPERATION",
                 "UNITPROMOTIONCLASS"):
        kinds[type_hash(kind)] = kind
    found = {}
    for name, value in resolutions.items():
        marker = struct.pack("<I", value)
        q = body.find(marker)
        while q >= 0:
            if q + 16 <= len(body) and _u32(body, q + 4) in kinds:
                target = struct.unpack_from("<i", body, q + 8)[0]
                votes = []
                o = q + 12
                while o + 4 <= len(body) and len(votes) < 64:
                    v = struct.unpack_from("<i", body, o)[0]
                    if v not in (1, 2):
                        break
                    votes.append(v)
                    o += 4
                if votes:
                    a, b = votes.count(1), votes.count(2)
                    found[name] = {"resolution": name, "target_kind": kinds[_u32(body, q + 4)], "target": target,
                                   "votes": votes, "effect": 1 if a > b else 2 if b > a else None,
                                   "offset": q}
            q = body.find(marker, q + 1)
    return sorted(found.values(), key=lambda item: item["offset"])


PLOT_YIELD_ORDER = ("YIELD_FOOD", "YIELD_PRODUCTION", "YIELD_GOLD", "YIELD_SCIENCE", "YIELD_CULTURE", "YIELD_FAITH")


def _great_work_list(body: bytes, names: TypeNames) -> list[str]:
    """Every great work created so far, in creation order (its index is what
    city slots refer to): u32 count, then per work u32 6, GREATWORK hash,
    u32 player, u32 turn, u32 length + creator name."""
    works = {struct.pack("<I", h): n for h, n in names.with_prefix("GREATWORK_").items()}
    if not works:
        return []
    pattern = re.compile(b"|".join(re.escape(k) for k in works))
    for match in pattern.finditer(body):
        start = match.start() - 8
        if start < 0:
            continue
        count = _u32(body, start)
        if not 1 <= count <= 2000 or _u32(body, start + 4) != 6:
            continue
        o, found = start + 4, []
        for _ in range(count):
            if o + 20 > len(body) or _u32(body, o) != 6 or body[o + 4:o + 8] not in works:
                found = None
                break
            found.append(works[body[o + 4:o + 8]])
            length = _u32(body, o + 16)
            if length > 200:
                found = None
                break
            o += 20 + length
        if found:
            return found
    return []


def _city_great_works(body: bytes, start: int, end: int, built: set[str], work_list: list[str],
                      buildings: dict[bytes, str]) -> list[str]:
    """Great works in a city's buildings: u32 building count, then per building
    its hash, u32 slot count and per slot three u32 (the last is the index into
    the great work list, 0xFFFFFFFF for an empty slot). Checked on a turn-90 save: Pella palace → Rumi's
    Divani (+4 culture), Mohenjo-daro amphitheater → Sun Tzu and Chaucer."""
    for match in re.finditer(b"|".join(re.escape(k) for k in buildings), body[start:end]):
        p = start + match.start()
        if buildings.get(body[p:p + 4]) not in built or p < start + 4:
            continue
        count = _u32(body, p - 4)
        if not 1 <= count <= 12:
            continue
        o, result = p, []
        for _ in range(count):
            name = buildings.get(body[o:o + 4])
            n = _u32(body, o + 4) if o + 8 <= end else 99
            if name not in built or not 0 <= n <= 8 or o + 8 + 12 * n > end:
                result = None
                break
            for k in range(n):
                index = _u32(body, o + 8 + 12 * k + 8)
                if index == 0xFFFFFFFF:
                    continue  # empty slot
                if index >= len(work_list):
                    result = None
                    break
                result.append(work_list[index])
            if result is None:
                break
            o += 8 + 12 * n
        if result is not None and o > p:
            return result
    return []


def _great_works_by_city(body: bytes, names: TypeNames, cities: list[SaveCity]) -> dict:
    work_list = _great_work_list(body, names)
    if not work_list:
        return {}
    buildings = {struct.pack("<I", h): n for h, n in names.with_prefix("BUILDING_").items()}
    ordered = sorted(cities, key=lambda c: c.offset)
    result = {}
    for number, city in enumerate(ordered):
        end = ordered[number + 1].offset if number + 1 < len(ordered) else city.offset + 30000
        end = min(end, city.offset + 30000)
        built = {b["type"] for b in city.buildings}
        works = _city_great_works(body, city.offset, end, built, work_list, buildings) if built else []
        if works:
            result[(city.player, city.city_index)] = works
    return result


def _plot_yields(body: bytes, width: int, height: int, start: int = 0) -> dict:
    """Permanent yield changes on plots (natural-event fertility).

    The map section stores a yield template — u32 6 then six (yield hash,
    i32 0) pairs — followed by u32 plot count, one i32 per plot (−1 or an
    index into the list below), u32 entry count and the entries, each again
    u32 6 + six (yield hash, i32 amount). Checked on two saves one turn apart:
    after a major flood of the Gambia the two floodplains next to the oil
    east of Niani read +1 food and +1 food +1 production, the values the game
    shows (2 → 3 food, 2 → 3 food 1 production).
    """
    plots = width * height
    template = struct.pack("<I", 6) + b"".join(struct.pack("<Ii", type_hash(y), 0) for y in PLOT_YIELD_ORDER)
    marker = template + struct.pack("<I", plots)
    q = body.find(marker, start)
    if q < 0:
        return {}
    o = q + len(marker)
    if o + plots * 4 + 4 > len(body):
        return {}
    slots = struct.unpack_from(f"<{plots}i", body, o)
    o += plots * 4
    count = _u32(body, o)
    o += 4
    if count > plots:
        return {}
    names = {type_hash(y): y for y in PLOT_YIELD_ORDER}
    entries = []
    for _ in range(count):
        n = _u32(body, o)
        o += 4
        if n > 16 or o + n * 8 > len(body):
            return {}
        values = {}
        for k in range(n):
            h, amount = struct.unpack_from("<Ii", body, o + k * 8)
            if h in names and amount:
                values[names[h]] = amount
        o += n * 8
        entries.append(values)
    result = {}
    for index, slot in enumerate(slots):
        if 0 <= slot < len(entries) and entries[slot]:
            result[(index % width, index // width)] = dict(entries[slot])
    return result


def _trade_routes(body: bytes, cities: list[SaveCity]) -> list[dict]:
    """Traders' MAKE_TRADE_ROUTE operations.

    Layout after the UNITOPERATION_MAKE_TRADE_ROUTE hash: +16 i32 turn the
    route started, +36 destination x, y, +44 origin x, y (both city centres).
    Verified against the game: Alexandria→Madrid (turn 82), Pella→Toledo,
    Mohenjo Daro→Madrid.
    """
    centres = {(c.x, c.y): c.player for c in cities}
    # City-states' traders also run routes from major cities' coordinates, so
    # ownership comes from the block, not the origin.
    owner_at = _block_owner(cities)
    marker = struct.pack("<I", type_hash("UNITOPERATION_MAKE_TRADE_ROUTE"))
    routes, seen = [], set()
    q = body.find(marker)
    while q >= 0:
        if q + 52 <= len(body):
            turn = struct.unpack_from("<i", body, q + 16)[0]
            dx, dy, ox, oy = struct.unpack_from("<4i", body, q + 36)
            key = (ox, oy, dx, dy, turn)
            if (ox, oy) in centres and (dx, dy) in centres and (ox, oy) != (dx, dy) and 0 <= turn < 10000 \
                    and key not in seen:
                seen.add(key)
                routes.append({"origin": (ox, oy), "origin_player": centres[(ox, oy)], "destination": (dx, dy),
                               "destination_player": centres[(dx, dy)], "started_turn": turn, "owner": owner_at(q)})
        q = body.find(marker, q + 1)
    return routes


# ---------------------------------------------------------------------- API
def decode_container(container: SaveContainer, names: TypeNames) -> SaveData:
    body = container.body
    warnings: list[str] = []
    width, height, tiles, resyncs = _parse_tiles(body, names)
    if resyncs:
        warnings.append(f"타일 레코드 {resyncs}곳에서 위치 보정을 했습니다 (드문 개선 시설/특수 타일).")
    districts = _parse_districts(body, names, width, height, tiles)
    cities = _parse_cities(body, names, width, height, tiles, districts, warnings)
    player_states = _player_states(body, names, cities, warnings, getattr(container.header, "slots", None))
    centres = sum(1 for d in districts if d.type == "DISTRICT_CITY_CENTER")
    if centres != len(cities):
        warnings.append(f"도심 특수지구 {centres}곳 중 도시 정보와 연결된 곳은 {len(cities)}곳입니다.")
    unknown: Counter = Counter()
    for tile in tiles:
        for value in (tile.terrain, tile.feature, tile.resource, tile.improvement, tile.wonder):
            if isinstance(value, str) and value.startswith("UNKNOWN_"):
                unknown[value] += 1
    for city in cities:
        for building in city.buildings:
            if building["type"].startswith("UNKNOWN_"):
                unknown[building["type"]] += 1
    header = container.header
    players: dict[int, dict] = {}
    for index, slot in header.slots.items():
        players[index] = dict(slot)
    for tile in tiles:
        if tile.player is not None and tile.player not in players:
            players[tile.player] = {
                "civilization": "CIVILIZATION_FREE_CITIES" if tile.player == FREE_CITIES_PLAYER else None,
                "leader": None, "minor": False, "spectator": False,
            }
    for index, info in players.items():
        info["cities"] = sum(1 for c in cities if c.player == index)
        info["tiles"] = sum(1 for t in tiles if t.player == index)
    stats = {
        "tiles": len(tiles),
        "river_tiles": sum(1 for t in tiles if t.river_edges),
        "owned_tiles": sum(1 for t in tiles if t.player is not None),
        "districts": len(districts),
        "districts_verified": sum(1 for d in districts if d.verified),
        "cities": len(cities),
        "cities_verified": sum(1 for c in cities if c.verified),
        "city_centres": sum(1 for d in districts if d.type == "DISTRICT_CITY_CENTER"),
        "resyncs": resyncs,
        "known_type_names": len(names),
    }
    return SaveData(
        width=width, height=height, tiles=tiles, districts=districts, cities=cities,
        header={
            "game_speed": header.game_speed, "map_size": header.map_size, "ruleset": header.ruleset,
            "difficulty": header.difficulty, "map_script": header.map_script, "game_modes": header.game_modes,
        },
        turn_estimate=_turn_estimate(body), players=players, warnings=warnings,
        unknown_types=dict(unknown.most_common()), stats=stats, player_states=player_states,
        trade_routes=_trade_routes(body, cities),
        governors=_governors(body, names, cities),
        congress=_congress(body, names),
        plot_yields=_plot_yields(body, width, height),
        great_works=_great_works_by_city(body, names, cities),
    )


def decode_save(raw: bytes, names: TypeNames) -> SaveData:
    return decode_container(read_container(raw), names)
