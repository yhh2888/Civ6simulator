"""Import a Civ VI ``.Civ6Save`` into the active map, cities and game state.

Two steps so the user can choose which player to import:

1. ``preview_save(raw, filename)`` decodes the file, keeps the result in a
   small in-memory cache and returns a summary with a token.
2. ``apply_save_import({"token", "player", "auto_assign"})`` converts the
   decoded save to the app's documents and replaces the active game. Like
   "저장본 불러오기" the change lives in memory until the user presses 저장.
   Citizens come from the save; ``auto_assign=true`` replaces them with the
   balanced optimizer (cities without readable citizen data are always
   filled in that way).

Only the chosen player's cities become simulator cities. Other players'
territory and cities are kept on the map for display (``save_owner`` on tiles,
``save_import.foreign_cities`` on the map).
"""

from __future__ import annotations

import hashlib
import struct
import threading
import time
import zlib
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.dependencies import synchronized_mutation
from app.core.exceptions import ApplicationError
from app.repositories import locations
from app.repositories.json_repository import write_json_transaction
from app.repositories.rules_db_repository import load_rules_db
from app.services.civ6save import SaveFormatError, TypeNames, decode_save
from app.services.civ6save.container import SPEED_IDS
from app.services.civ6save.decoder import FREE_CITIES_PLAYER, SaveData
from app.services.civ6save.hashes import load_name_list
from app.services.empire_service import slot_counts
from app.services.game_data_service import game_rules
from app.services.production_service import prepare_city_production
from app.services.snapshot_service import validate_game_state_snapshot, validate_map_snapshot

EXTRA_NAMES_FILE = Path(__file__).resolve().parent / "civ6save" / "type_names_extra.txt"
CACHE_LIMIT = 2
CACHE_SECONDS = 30 * 60
# Growth costs are verified only for Standard speed (food_service).
VERIFIED_GROWTH_SPEEDS = {"STANDARD"}
CATEGORY_LABELS = {"unit": "유닛", "building": "건물", "wonder": "불가사의", "district": "특수지구", "project": "프로젝트"}

_cache: "OrderedDict[str, tuple[float, str, SaveData]]" = OrderedDict()
_cache_lock = threading.Lock()
_names_cache: dict[str, Any] = {}


def type_names() -> TypeNames:
    """Hash → name table from every available rules database plus the extra list."""
    from app.repositories.locations import RULESET_FILES

    dbs = [load_rules_db()] + [load_rules_db(ruleset) for ruleset in RULESET_FILES]
    key = "|".join(str(db.meta.get("fingerprint") or id(db)) for db in dbs)
    cached = _names_cache.get("names")
    if cached is not None and _names_cache.get("key") == key:
        return cached
    names: list = []
    for db in dbs:
        names += [row.get("Type") for row in db.rows("Types")]
        names += [row.get("BuildingType") for row in db.rows("Buildings")]
        names += [row.get("DistrictType") for row in db.rows("Districts")]
    names += load_name_list(EXTRA_NAMES_FILE)
    result = TypeNames(n for n in names if isinstance(n, str))
    _names_cache.update({"names": result, "key": key})
    return result


def clear_name_cache() -> None:
    _names_cache.clear()


def ruleset_for(save: SaveData) -> str | None:
    """The save's ruleset when this app has a rules database for it."""
    from app.repositories.rules_db_repository import available_rulesets

    ruleset = save.header.get("ruleset")
    return ruleset if available_rulesets().get(ruleset) else None


def _store(token: str, filename: str, save: SaveData) -> None:
    with _cache_lock:
        now = time.time()
        for key in [k for k, (at, _, _) in _cache.items() if now - at > CACHE_SECONDS]:
            _cache.pop(key, None)
        _cache[token] = (now, filename, save)
        _cache.move_to_end(token)
        while len(_cache) > CACHE_LIMIT:
            _cache.popitem(last=False)


def _cached(token: str) -> tuple[str, SaveData]:
    with _cache_lock:
        entry = _cache.get(token)
    if entry is None:
        raise ApplicationError(410, "세이브 미리보기가 만료되었습니다. 파일을 다시 선택하세요.")
    return entry[1], entry[2]


def _player_label(info: dict) -> str:
    civ = (info.get("civilization") or "").replace("CIVILIZATION_", "").replace("_", " ").title()
    leader = (info.get("leader") or "").replace("LEADER_MINOR_CIV_", "").replace("LEADER_", "").replace("_", " ").title()
    if info.get("civilization") == "CIVILIZATION_FREE_CITIES":
        return "자유 도시"
    if info.get("minor"):
        return f"{civ} (도시국가)"
    return f"{civ} · {leader}" if leader else civ or "알 수 없음"


def default_player(save: SaveData) -> int | None:
    majors = [i for i, p in sorted(save.players.items())
              if p.get("cities") and not p.get("minor") and not p.get("spectator") and i < FREE_CITIES_PLAYER]
    if 0 in majors:
        return 0
    return majors[0] if majors else None


def _speed(save: SaveData) -> tuple[str, str | None, str | None]:
    """(speed used by the app, speed in the save, warning).

    The save's own speed is kept: production and research costs use the
    GameSpeeds cost multiplier from the rules data. Growth thresholds are only
    verified for Standard speed, so other speeds show growth as unsupported.
    """
    original = SPEED_IDS.get(save.header.get("game_speed") or "")
    if original is None:
        fallback = game_rules().get("game_speed", "STANDARD")
        return fallback, None, "세이브의 게임 속도를 읽지 못해 기본 속도로 계산합니다."
    if original in VERIFIED_GROWTH_SPEEDS:
        return original, original, None
    return original, original, (
        f"게임 속도 {original}: 생산·연구 비용은 데이터의 속도 배율을 적용하지만, "
        "성장에 필요한 식량은 표준 속도만 검증되어 이 속도에서는 성장 계산이 '미지원'으로 표시됩니다."
    )


def summary(save: SaveData, token: str, filename: str) -> dict:
    speed, original_speed, speed_warning = _speed(save)
    players = []
    for index, info in sorted(save.players.items()):
        if info.get("spectator"):
            continue
        cities = [c for c in save.cities if c.player == index]
        players.append({
            "index": index,
            "label": _player_label(info),
            "civilization": info.get("civilization"),
            "leader": info.get("leader"),
            "minor": bool(info.get("minor")),
            "cities": len(cities),
            "city_names": [c.name for c in cities][:12],
            "tiles": info.get("tiles", 0),
            "importable": bool(cities),
        })
    warnings = list(save.warnings)
    if speed_warning:
        warnings.append(speed_warning)
    return {
        "token": token,
        "filename": filename,
        "width": save.width,
        "height": save.height,
        "header": save.header,
        "turn_estimate": save.turn_estimate,
        "game_speed_used": speed,
        "game_speed_in_save": original_speed,
        "players": players,
        "default_player": default_player(save),
        "stats": save.stats,
        "unknown_types": save.unknown_types,
        "warnings": warnings,
        "decoded": DECODED_FIELDS,
        "not_decoded": NOT_DECODED_FIELDS,
    }


DECODED_FIELDS = [
    "지형·지형 특성·자원·시설·도로 (타입 해시 해독)",
    "강 6면(이웃 타일과 100% 일치 확인)·절벽",
    "타일 소유(플레이어·도시)",
    "도시 이름·위치·소유자",
    "특수지구 종류·위치·완성 여부(짓는 중이면 생산 대기열로)",
    "지어진 건물·불가사의(건물 위치가 도심/특수지구와 일치하는지 확인)",
    "시민 배치·잠금 타일·전문가 수, 인구(두 세이브 비교로 확인)",
    "현재 생산 항목·진행도, 특수지구의 고정 비용",
    "연구한 기술·사회제도, 유레카·영감, 연구 진행도",
    "정부(슬롯 구성과 대조해 확정)·장착 정책",
    "상인의 교역로(출발·도착 도시, 시작 턴), 판테온(종교관), 도시국가에 보낸 사절 수, 종주국(사절 3명 이상이며 다른 문명보다 많을 때로 판정)",
    "자연재해로 늘어난 타일 산출(범람·화산·폭풍 비옥도, 한 턴 차이 세이브로 확인)",
    "시설·건물 약탈 상태(약탈된 시설·건물은 수리 전까지 산출 없음)",
    "도시 건물에 걸린 걸작(걸작 목록 순번으로 연결)",
    "총독 위치·진급·정착 턴, 세계 의회 결의(추정), 게임 난이도",
]
NOT_DECODED_FIELDS = [
    "대기열 2번째 이후 항목 (받은 세이브가 모두 1개라 형식 미확인)",
    "비축 식량",
    "지금 연구 중인 기술·사회제도 (진행도만 있음)",
    "종교 창시 신앙, 쾌적도·충성도, 유닛 (교역로 목적지가 지도에 없는 도시면 제외)",
]


def preview_save(raw: bytes, filename: str | None = None) -> dict:
    if not raw:
        raise ApplicationError(422, "빈 파일입니다.")
    filename = (filename or "save.Civ6Save").replace("\\", "/").rsplit("/", 1)[-1][:120]
    try:
        save = decode_save(raw, type_names())
    except SaveFormatError as error:
        raise ApplicationError(422, f"세이브를 해독하지 못했습니다: {error}") from error
    except (ValueError, IndexError, KeyError, struct.error, zlib.error) as error:  # layout surprises
        raise ApplicationError(422, f"세이브 구조를 해석하지 못했습니다 ({type(error).__name__}).") from error
    token = hashlib.sha1(raw).hexdigest()[:16]
    _store(token, filename, save)
    return summary(save, token, filename)


# ------------------------------------------------------------- conversion
def _wonder_types(db) -> set[str]:
    return {row.get("BuildingType") for row in db.rows("Buildings")
            if str(row.get("IsWonder", "")).lower() in ("true", "1")}


def _player_state_documents(save: SaveData, player: int, db) -> dict:
    """Technologies, civics, research progress, government and policies.

    The save keeps one (key, government) pair per government tier; the
    current government is the one whose slot counts match the decoded slot
    layout. Nothing is applied when the per-player tables failed validation.
    """
    state = save.player_states.get(player) or {}
    result: dict[str, Any] = {"decoded": False, "warnings": []}
    if not state:
        result["warnings"].append("기술·사회제도·정책 표를 찾지 못했습니다.")
        return result
    if not state.get("verified", False):
        result["warnings"].append("기술·사회제도 표 위치를 검증하지 못해 적용하지 않았습니다.")
        return result
    techs = db.index("Technologies", "TechnologyType")
    civics = db.index("Civics", "CivicType")
    done_t = [t for t in state.get("technologies") or [] if t in techs]
    done_c = [c for c in state.get("civics") or [] if c in civics]
    research = {
        "technology": {"current_id": None, "progress": {
            k: round(v, 3) for k, v in (state.get("tech_progress") or {}).items() if k in techs and k not in done_t and v > 0}},
        "civic": {"current_id": None, "progress": {
            k: round(v, 3) for k, v in (state.get("civic_progress") or {}).items() if k in civics and k not in done_c and v > 0}},
    }
    boosted = {
        "technology": [t for t in state.get("boosted_technologies") or [] if t in techs and t not in done_t],
        "civic": [c for c in state.get("inspired_civics") or [] if c in civics and c not in done_c],
    }
    layout = state.get("slot_layout") or []
    wanted: dict[str, int] = {}
    for slot in layout:
        wanted[slot] = wanted.get(slot, 0) + 1
    candidates = [g for g in state.get("government_candidates") or [] if g]
    if "GOVERNMENT_CHIEFDOM" not in candidates:
        candidates.append("GOVERNMENT_CHIEFDOM")
    matching = [g for g in candidates if layout and slot_counts(db, g) == wanted]
    government = matching[0] if len(matching) == 1 else None
    if len(matching) > 1:
        result["warnings"].append("슬롯 구성이 같은 정부가 여럿이라 정부를 확정하지 못했습니다: " + ", ".join(matching))
    policy_slots: dict[str, list[str]] = {}
    policies = db.index("Policies", "PolicyType")
    for item in state.get("policies") or []:
        if item.get("slot") and item.get("policy") in policies:
            policy_slots.setdefault(item["slot"], []).append(item["policy"])
    if government is None:
        if layout and not matching:
            result["warnings"].append(
                "정부를 확정하지 못했습니다 (슬롯 구성 "
                + ", ".join(f"{k}×{v}" for k, v in sorted(wanted.items())) + "). 정책도 적용하지 않았습니다.")
        policy_slots = {}
    city_states: dict[str, dict] = {}
    civilizations = db.index("Civilizations", "CivilizationType")
    suzerain_of = set(state.get("suzerain_of") or [])
    for index, envoys in (state.get("envoys") or {}).items():
        civ = (save.players.get(index) or {}).get("civilization")
        if civ and (civilizations.get(civ) or {}).get("StartingCivilizationLevelType") == "CIVILIZATION_LEVEL_CITY_STATE":
            city_states[civ] = {"envoys": int(envoys), "suzerain": index in suzerain_of}
    beliefs = db.index("Beliefs", "BeliefType")
    pantheons = [b for b in state.get("beliefs_in_block") or []
                 if (beliefs.get(b) or {}).get("BeliefClassType") == "BELIEF_CLASS_PANTHEON"]
    pantheon = pantheons[0] if len(pantheons) == 1 else None
    if len(pantheons) > 1:
        result["warnings"].append("판테온 후보가 여럿이라 정하지 못했습니다: " + ", ".join(pantheons))
    result["pantheon"] = pantheon
    result.update(decoded=True, technologies=sorted(done_t), civics=sorted(done_c), research=research,
                  boosted=boosted, government=government, policy_slots=policy_slots, city_states=city_states)
    return result


def _governor_fields(save: SaveData, player: int, city, db) -> dict:
    """Governor stationed in the city, its promotions and whether it is
    established (turns in the city ≥ GOVERNOR_BASE_TURNS_TO_ESTABLISH)."""
    needed = int(db.param("GOVERNOR_BASE_TURNS_TO_ESTABLISH", 5) or 5) if db.available else 5
    for governor in save.governors:
        if governor.get("owner") not in (None, player) or governor.get("city_index") != city.city_index:
            continue
        if not _in_rules(db, "Governors", "GovernorType", governor["type"]):
            continue
        turns = governor.get("turns")
        promotions = [p for p in governor.get("promotions") or []
                      if _in_rules(db, "GovernorPromotions", "GovernorPromotionType", p)]
        return {"governor": governor["type"], "governor_promotions": promotions,
                "governor_turns": turns,
                "governor_established": bool(isinstance(turns, int) and turns >= needed)}
    return {"governor": None, "governor_promotions": []}


def _incoming_routes(save: SaveData, player: int, city) -> list[dict]:
    """Routes other players' (and city-states') traders run into this city."""
    labels = {(c_.x, c_.y): c_.name for c_ in save.cities}
    routes = []
    for route in save.trade_routes:
        if route["destination"] != (city.x, city.y) or route["destination_player"] != player:
            continue
        owner = route.get("owner")
        if owner == player or route["origin_player"] == player:
            continue  # own traders: counted on the origin city
        ox, oy = route["origin"]
        routes.append({"origin": f"{ox},{oy}", "origin_name": labels.get((ox, oy)),
                       "origin_player": route["origin_player"],
                       "origin_label": _player_label(save.players.get(route["origin_player"], {})),
                       "started_turn": route["started_turn"]})
    return routes[:20]


YIELD_KEY = {"YIELD_FOOD": "Food", "YIELD_PRODUCTION": "Production", "YIELD_GOLD": "Gold",
             "YIELD_SCIENCE": "Science", "YIELD_CULTURE": "Culture", "YIELD_FAITH": "Faith"}


def _event_yields(values: dict | None) -> dict:
    return {YIELD_KEY[k]: v for k, v in (values or {}).items() if k in YIELD_KEY and v}


def _leader_for(db, info: dict) -> str | None:
    """The slot's leader; when the save does not name it (the human slot in
    the saves seen so far), the civilization's only leader if it has one."""
    leader = info.get("leader")
    if _in_rules(db, "Leaders", "LeaderType", leader):
        return leader
    options = [r.get("LeaderType") for r in db.rows("CivilizationLeaders")
               if r.get("CivilizationType") == info.get("civilization") and r.get("LeaderType")]
    return options[0] if len(set(options)) == 1 else None


def _difficulty_documents(save: SaveData, db, previous: dict | None = None) -> dict:
    """Every major civilization starts at the game's difficulty; the save's
    owner (the default player) is human, the others AI. Settings the user
    made earlier for this save (``previous``) are kept."""
    from app.services import app_config_service as cfg
    from app.services.difficulty_service import clean, default_level, levels

    order = levels(db)
    header = save.header if isinstance(save.header, dict) else getattr(save.header, "__dict__", {})
    level = header.get("difficulty") if header.get("difficulty") in order else default_level(db)
    # In a spectator game the owner is the spectator slot: every civilization is AI.
    spectator = any(p.get("spectator") for p in save.players.values())
    human = None if spectator and cfg.get("save_import.json", "spectator_all_ai", True) else default_player(save)
    players = {
        str(index): {"difficulty": level, "human": index == human}
        for index, p in sorted(save.players.items())
        if not p.get("minor") and not p.get("spectator") and index < FREE_CITIES_PLAYER
    }
    result = {"default": level, "players": players}
    if isinstance(previous, dict):
        try:
            merged = clean(db, {"players": {k: v for k, v in (previous.get("players") or {}).items() if k in players}},
                           result)
            result = merged
        except ApplicationError:
            pass
    return result


def _congress_documents(save: SaveData, player: int, db) -> list[dict]:
    """Last World Congress results; PLAYER-target resolutions record whether
    they target this player (only those can be applied)."""
    result = []
    for item in save.congress:
        if not _in_rules(db, "Resolutions", "ResolutionType", item["resolution"]) or item.get("effect") not in (1, 2):
            continue
        entry = {"resolution": item["resolution"], "effect": item["effect"], "target_kind": item["target_kind"],
                 "votes": {"A": item["votes"].count(1), "B": item["votes"].count(2)}, "source": "save"}
        if item["target_kind"] == "PLAYER":
            entry["target_player"] = item["target"]
            entry["targets_me"] = item["target"] == player
        result.append(entry)
    return result


def build_documents(save: SaveData, player: int, filename: str, *, now: str | None = None,
                    difficulties: dict | None = None) -> tuple[dict, dict, dict, dict]:
    """Convert a decoded save to (map, cities, game_state, report)."""
    ruleset = ruleset_for(save)
    db = load_rules_db(ruleset)
    rules = game_rules()
    wonders = _wonder_types(db)
    tile_wonders = {t.wonder for t in save.tiles if t.wonder}
    speed, original_speed, speed_warning = _speed(save)
    info = save.players.get(player, {})
    own_cities = [c for c in save.cities if c.player == player]
    if not own_cities:
        raise ApplicationError(422, "선택한 플레이어에게 도시가 없습니다.")

    def city_id_for(city) -> str:
        digest = hashlib.sha1(f"{filename}|{player}|{city.city_index}|{city.x},{city.y}".encode()).hexdigest()
        return "city-" + digest[:8]

    ids = {(c.player, c.city_index): city_id_for(c) for c in own_cities}
    district_records = {(d.player, d.id): d for d in save.districts}
    centres = {(c.x, c.y) for c in save.cities}
    built_wonders = {(b["x"], b["y"]): b["type"] for c in save.cities for b in c.buildings
                     if b["type"] in wonders or b["type"] in tile_wonders}

    own_centres = {(c.x, c.y) for c in own_cities}
    worked_plots: set[tuple[int, int]] = set()
    for c in own_cities:
        if c.citizens:
            worked_plots |= {(t["x"], t["y"]) for t in c.citizens["tiles"]}
            worked_plots |= {(t["x"], t["y"]) for t in c.citizens["specialists"]}
    tiles = []
    for t in save.tiles:
        district = in_progress = None
        if t.district_id is not None and (t.x, t.y) not in centres:
            record = district_records.get((t.player, t.district_id))
            kind = "DISTRICT_WONDER" if t.wonder else (record.type if record else None)
            finished = (t.x, t.y) in built_wonders if t.wonder else (record is None or record.completed)
            if finished:
                district = kind
            else:
                in_progress = kind
        owner_city_id = ids.get((t.player, t.city_index)) if t.player == player else None
        tiles.append({
            "x": t.x,
            "y": t.y,
            "terrain": t.terrain,
            "feature": t.feature,
            "resource": t.resource,
            "river": bool(t.river_edges),
            "river_wires": None,
            "river_edges": t.river_edges,
            "cliff_edges": t.cliff_edges,
            "improvement": t.improvement,
            "district": district,
            "district_in_progress": in_progress,
            "wonder": t.wonder if (t.x, t.y) in built_wonders else None,
            "wonder_in_progress": t.wonder if t.wonder and (t.x, t.y) not in built_wonders else None,
            "worked": owner_city_id is not None and ((t.x, t.y) in own_centres or (t.x, t.y) in worked_plots),
            "owner_city_id": owner_city_id,
            "hill_override": None,
            "road": None if t.road == 255 else t.road,
            "appeal": t.appeal,
            "save_owner": t.player,
            **({"improvement_pillaged": True} if t.pillaged and t.improvement else {}),
            **({"event_yields": _event_yields(save.plot_yields.get((t.x, t.y)))} if (t.x, t.y) in save.plot_yields else {}),
        })

    population_decoded = 0
    needs_auto_assign: list[str] = []
    production_notes: list[str] = []
    cities: dict[str, dict] = {}
    for c in own_cities:
        city_id = ids[(c.player, c.city_index)]
        built = [b["type"] for b in c.buildings]
        pillaged_buildings = [b["type"] for b in c.buildings if b.get("pillaged")]
        city_wonders = [b for b in built if b in wonders or b in tile_wonders]
        city_buildings = [b for b in built if b not in city_wonders]
        districts = []
        for d in c.districts:
            if d.completed and d.type not in ("DISTRICT_CITY_CENTER", "DISTRICT_WONDER") and d.type not in districts:
                districts.append(d.type)
        population = c.population if isinstance(c.population, int) else None
        if population:
            population_decoded += 1
        population = max(1, min(population or 1, rules["city"]["maximum_population"]))
        specialists, preferences = {}, {}
        if c.citizens:
            for spec in c.citizens["specialists"]:
                specialists[f"{spec['x']},{spec['y']}"] = int(spec["count"])
            for plot in c.citizens["tiles"] + c.citizens["specialists"]:
                if plot["locked"]:
                    preferences[f"{plot['x']},{plot['y']}"] = {"lock": True}
        else:
            needs_auto_assign.append(city_id)
        queue, progress = _production_queue(c, city_id, wonders | tile_wonders)
        if c.production and not queue:
            production_notes.append(f"{c.name}: 생산 중인 {c.production['type']}을(를) 대기열로 옮기지 못했습니다.")
        city = {
            "id": city_id,
            "name": c.name,
            "x": c.x,
            "y": c.y,
            "population": population,
            "civilization": info.get("civilization"),
            "leader": info.get("leader"),
            "buildings": city_buildings,
            "districts": districts,
            "wonders": city_wonders,
            **_governor_fields(save, player, c, db),
            "policies": [],
            "technologies": [],
            "civics": [],
            "religion": None,
            "housing": 2,
            "amenities": 0,
            "notes": "Civ6Save에서 가져옴. 인구·시민 배치·잠금·전문가·현재 생산과 진행도·출발 교역로는 세이브 값입니다. 비축 식량은 세이브에서 읽지 않았습니다.",
            "worked_tiles": [],
            "specialists": specialists,
            "tile_preferences": preferences,
            "citizen_mode": "manual",
            "production_queue": queue,
            "production_progress": progress,
            "trade_routes": [
                {"destination": f"{r['destination'][0]},{r['destination'][1]}", "started_turn": r["started_turn"]}
                for r in save.trade_routes
                if r["origin"] == (c.x, c.y) and r["origin_player"] == player and r.get("owner") in (None, player)
            ][:20],
            "incoming_routes": _incoming_routes(save, player, c),
            "pillaged_buildings": pillaged_buildings,
            "great_works": [w for w in save.great_works.get((c.player, c.city_index), [])
                            if _in_rules(db, "GreatWorks", "GreatWorkType", w)][:40],
            "created_from_map": False,
            "stored_food": 0.0,
            "food_yield": 0.0,
            "import_meta": {
                "source": "civ6save",
                "population": "decoded" if c.population else "default",
                "citizens": "decoded" if c.citizens else "missing",
                "citizens_verified": bool(c.citizens and c.citizens["verified"]),
                "production": c.production,
                "save_player": c.player,
                "save_city_index": c.city_index,
                "raw_name": c.raw_name,
            },
        }
        prepare_city_production(city)
        cities[city_id] = city

    foreign_cities = [
        {"name": c.name, "x": c.x, "y": c.y, "player": c.player,
         "label": _player_label(save.players.get(c.player, {}))}
        for c in save.cities if c.player != player
    ]
    players = {
        str(index): {"label": _player_label(p), "civilization": p.get("civilization"), "leader": p.get("leader"),
                     "minor": bool(p.get("minor")), "cities": p.get("cities", 0)}
        for index, p in sorted(save.players.items()) if not p.get("spectator")
    }
    warnings = list(save.warnings) + production_notes
    if speed_warning:
        warnings.append(speed_warning)
    known = _known_types(db)

    def missing(tile_list) -> list[str]:
        return sorted({
            value for t in tile_list
            for value in (t["terrain"], t["feature"], t["resource"], t["improvement"], t["district"])
            if isinstance(value, str) and value and not value.startswith("UNKNOWN_") and value not in known
        })

    unknown_in_rules = missing(tiles)
    unknown_owned = missing([t for t in tiles if t["owner_city_id"]])
    unknown_buildings = sorted({b for c in cities.values() for b in c["buildings"] + c["wonders"] if b not in known})
    if unknown_owned or unknown_buildings:
        listed = unknown_owned + unknown_buildings
        warnings.append(
            f"내 도시 영토에 현재 규칙 데이터({db.meta.get('ruleset') or 'Data.zip, 기본 게임'})에 없는 타입 {len(listed)}종이 있어 그 부분의 산출은 계산되지 않습니다: "
            + ", ".join(listed[:12]) + (" …" if len(listed) > 12 else "")
        )
    meta = {
        "format": "civ6save-import",
        "format_version": 1,
        "file": filename,
        "imported_at": now or datetime.now(timezone.utc).isoformat(),
        "imported_player": player,
        "imported_label": _player_label(info),
        "header": save.header,
        "turn_estimate": save.turn_estimate,
        "game_speed_in_save": original_speed,
        "players": players,
        "foreign_cities": foreign_cities,
        "population_confidence": "decoded",
        "warnings": warnings,
        "rules_mismatch": unknown_in_rules,
        "rules_mismatch_owned": unknown_owned + unknown_buildings,
        "unknown_hashes": save.unknown_types,
    }
    map_data = {
        "source": f"civ6save:{filename}",
        "width": save.width,
        "height": save.height,
        "count": len(tiles),
        # Civ VI rows grow northwards (y = 0 is the south edge). The renderer
        # draws such maps flipped so north is up; coordinates stay as in game.
        "orientation": "north_up",
        "tiles": tiles,
        "save_import": meta,
    }
    capital = next((c for c in cities.values() if "BUILDING_PALACE" in c["buildings"]), None)
    game = {
        "turn": save.turn_estimate if isinstance(save.turn_estimate, int) and save.turn_estimate >= 1 else 1,
        "units": [],
        "game_speed": speed,
        "technologies": [],
        "civics": [],
        "research": {},
        "empire": {
            "civilization": info.get("civilization") if _in_rules(db, "Civilizations", "CivilizationType", info.get("civilization")) else None,
            "leader": _leader_for(db, info),
            "government": None,
            "pantheon": None,
            "policy_slots": {},
            # Without decoded technologies/civics, prerequisite checks would
            # wrongly block everything the save already has.
            "enforce_prerequisites": False,
        },
        "sim_history": [],
    }
    player_state = _player_state_documents(save, player, db)
    if player_state["decoded"]:
        game["technologies"] = player_state["technologies"]
        game["civics"] = player_state["civics"]
        game["research"] = player_state["research"]
        game["empire"]["boosted"] = player_state["boosted"]
        game["empire"]["government"] = player_state["government"]
        game["empire"]["policy_slots"] = player_state["policy_slots"]
        game["empire"]["city_states"] = player_state["city_states"]
        game["empire"]["pantheon"] = player_state.get("pantheon")
        game["empire"]["enforce_prerequisites"] = True
    game["difficulties"] = _difficulty_documents(save, db, difficulties)
    congress = _congress_documents(save, player, db)
    if congress:
        game["empire"]["world_congress"] = congress
    warnings.extend(player_state["warnings"])
    if ruleset:
        game["ruleset"] = ruleset
    # Amenity sources outside the map (traded luxuries, garrisons, religion,
    # great people, war weariness) are not decoded, so the computed amenity
    # state would be a guess; its yield/growth effect starts switched off.
    from app.services import app_config_service as cfg

    amenity_on = cfg.get("save_import.json", "apply_amenity_effects", False) is True
    game["simulation_overrides"] = {"apply_amenity_effects": amenity_on}
    if not amenity_on:
        warnings.append("쾌적도 효과(성장·산출 %)는 꺼 둔 상태로 가져옵니다. 교역 사치품·주둔 유닛·종교 등 "
                    "쾌적도 출처를 세이브에서 읽지 못해 계산값이 실제와 다를 수 있습니다.")
    report = {
        "file": filename,
        "player": player,
        "player_label": _player_label(info),
        "cities": len(cities),
        "capital": capital["name"] if capital else None,
        "population_decoded": population_decoded,
        "citizens_from_save": len(cities) - len(needs_auto_assign),
        "needs_auto_assign": needs_auto_assign,
        "production": [
            {"city": city["name"], "category": entry["category"], "target_id": entry["target_id"],
             "progress": city["production_progress"].get(entry["queue_id"], 0.0),
             "locked_cost": entry.get("locked_cost"), "from_save": entry.get("from_save")}
            for city in cities.values() for entry in city["production_queue"]
        ],
        "foreign_cities": len(foreign_cities),
        "game_speed_used": speed,
        "game_speed_in_save": original_speed,
        "turn": game["turn"],
        "warnings": warnings,
        "ruleset": ruleset or "base",
        "player_state": {k: player_state.get(k) for k in ("decoded", "government", "pantheon", "city_states", "warnings")} | {
            "technologies": len(player_state.get("technologies") or []),
            "civics": len(player_state.get("civics") or []),
            "policies": sum(len(v) for v in (player_state.get("policy_slots") or {}).values())},
    }
    return map_data, cities, game, report


def _production_queue(city, city_id: str, wonder_types: set[str]) -> tuple[list[dict], dict[str, float]]:
    """Current production first, then districts/wonders that are placed but unfinished."""
    queue: list[dict] = []
    progress: dict[str, float] = {}

    def add(category, target, x, y, amount, locked_cost, source):
        if not isinstance(target, str) or target.startswith("UNKNOWN_"):
            return
        digest = hashlib.sha1(f"{city_id}|{category}|{target}|{x},{y}".encode()).hexdigest()
        queue_id = "prod-" + digest[:12]
        if any(entry["queue_id"] == queue_id for entry in queue):
            return
        entry = {"queue_id": queue_id, "category": category, "target_id": target, "x": x, "y": y, "from_save": source}
        if isinstance(locked_cost, int) and locked_cost > 0:
            entry["locked_cost"] = locked_cost
        queue.append(entry)
        if isinstance(amount, (int, float)) and amount > 0:
            progress[queue_id] = round(float(amount), 4)

    unfinished = {(k["category"], k["type"], k["x"], k["y"]): k for k in city.construction}
    current = city.production
    if current:
        category = current["kind"]
        if category == "building" and current["type"] in wonder_types:
            category = "wonder"
        record = unfinished.get((category, current["type"], current["x"], current["y"]))
        add(category, current["type"], current["x"], current["y"], current["progress"],
            record.get("cost") if record and category == "district" else None, "current")
    for item in city.construction:
        add(item["category"], item["type"], item["x"], item["y"], item["progress"],
            item.get("cost") if item["category"] == "district" else None, "unfinished")
    return queue, progress


def _known_types(db) -> set[str]:
    cached = _names_cache.get("known_types")
    key = id(db)
    if cached is None or _names_cache.get("known_key") != key:
        cached = {row.get("Type") for row in db.rows("Types") if row.get("Type")}
        _names_cache.update({"known_types": cached, "known_key": key})
    return cached


def _in_rules(db, table: str, column: str, value: str | None) -> bool:
    return bool(value) and value in db.index(table, column)


def _auto_assign(report: dict, city_ids: list[str] | None) -> None:
    """Place citizens with the balanced strategy (all cities, or only ``city_ids``)."""
    from app.services.citizen_assignment_service import write_assignment, optimize_city
    from app.services.city_growth_service import compute_amenities
    from app.services.modifier_engine import run_modifiers
    from app.services.simulation_service import _mutate

    def mutation(ctx):
        book = run_modifiers(ctx)
        amenities = compute_amenities(ctx, book)
        placed = 0
        for city_id in list(ctx.city_views):
            if city_ids is not None and city_id not in city_ids:
                continue
            plan = optimize_city(ctx, book, city_id, amenities[city_id], "balanced", None)
            write_assignment(ctx, city_id, plan["assignment"])
            placed += 1
        return placed

    placed, _ = _mutate(None, mutation)
    report["auto_assigned_cities"] = placed


@synchronized_mutation
def apply_save_import(payload: dict) -> dict:
    token = payload.get("token")
    if not isinstance(token, str):
        raise ApplicationError(422, "token이 필요합니다.")
    filename, save = _cached(token)
    player = payload.get("player")
    if player is None:
        player = default_player(save)
    if not isinstance(player, int) or isinstance(player, bool) or player not in save.players:
        raise ApplicationError(422, "가져올 플레이어를 선택하세요.")
    previous = payload.get("difficulties")
    map_data, cities, game, report = build_documents(save, player, filename,
                                                     difficulties=previous if isinstance(previous, dict) else None)
    snapshot = {"map": map_data, "cities": cities, "game_state": game}
    map_data, cities = validate_map_snapshot(snapshot)
    state = validate_game_state_snapshot(game, cities, map_data)
    write_json_transaction({
        locations.map_file(): map_data,
        locations.cities_file(): cities,
        locations.game_state_file(): state,
    })
    # auto_assign=True re-optimizes every city and ignores the save's citizens;
    # cities whose citizen data could not be read are always filled in.
    targets = None if payload.get("auto_assign") else report["needs_auto_assign"]
    if targets is None or targets:
        try:
            _auto_assign(report, targets)
        except ApplicationError:
            raise
        except Exception as error:  # keep the import; report the failed step
            report["warnings"].append(f"시민 자동 배치에 실패해 비워 두었습니다: {type(error).__name__}")
    from app.repositories.city_repository import city_repository
    from app.repositories.map_repository import map_repository
    from app.services.game_data_service import game_state

    return {
        "map": map_repository.get_map({}),
        "cities": list(city_repository.get_all().values()),
        "game_state": game_state(),
        "import_report": report,
    }


# ------------------------------------------------------------- export
EXPORT_COLUMNS = ["save", "turn", "player", "civilization", "leader", "difficulty", "ai", "city", "x", "y",
                  "population", "citizens_from_save", "food", "production", "gold", "science", "culture", "faith",
                  "science_raw", "culture_raw", "gold_raw", "production_raw", "food_raw", "faith_raw",
                  "percent_science", "percent_culture", "percent_gold", "percent_production",
                  "amenity_score", "amenity_state"]


def _trunc(value: float) -> float:
    """The game shows yields cut to one decimal (15.59 → 15.5)."""
    import math

    return math.floor(value * 10 + 1e-6) / 10


def export_city_yields(raw: bytes, filename: str | None = None, difficulties: dict | None = None) -> list[dict]:
    """Studio yields of every city of every major civilization in one save
    (city-states, free cities and the spectator are skipped). Each player is
    computed the way an import as that player would be; citizens come from
    the save (cities whose assignment could not be read are marked)."""
    from app.services.simulation_service import context_for, statics
    from app.services.turn_simulation_service import evaluate

    preview = preview_save(raw, filename)
    filename_, save = _cached(preview["token"])
    st = statics()
    rows = []
    for player, info in sorted(save.players.items()):
        if info.get("minor") or info.get("spectator") or player >= FREE_CITIES_PLAYER or not info.get("cities"):
            continue
        map_data, cities, game, report = build_documents(save, player, filename_, difficulties=difficulties)
        snapshot = {"map": map_data, "cities": cities, "game_state": game}
        map_data, cities = validate_map_snapshot(snapshot)
        state = validate_game_state_snapshot(game, cities, map_data)
        ctx = context_for({"map": map_data, "cities": cities, "game": state}, st)
        result = evaluate(ctx, include_details=True)
        unread = set(report.get("needs_auto_assign") or [])
        for city_id, item in sorted(result["cities"].items(), key=lambda kv: kv[1]["name"]):
            totals = item["yields"]["totals"]
            percent = item["yields"].get("percent") or {}
            city = ctx.cities[city_id]
            amenity = item.get("amenities") or {}
            rows.append({
                "save": filename_, "turn": save.turn_estimate, "player": player,
                "civilization": info.get("civilization"), "leader": game["empire"].get("leader"),
                "difficulty": ctx.player.handicap, "ai": not ctx.player.is_human,
                "city": item["name"], "x": city.get("x"), "y": city.get("y"),
                "population": item["population"],
                "citizens_from_save": city_id not in unread,
                **{k.lower(): _trunc(totals.get(k, 0.0)) for k in ("Food", "Production", "Gold", "Science", "Culture", "Faith")},
                **{f"{k.lower()}_raw": round(totals.get(k, 0.0), 4) for k in ("Science", "Culture", "Gold", "Production", "Food", "Faith")},
                **{f"percent_{k.lower()}": percent.get(k, 0.0) for k in ("Science", "Culture", "Gold", "Production")},
                "amenity_score": amenity.get("score"), "amenity_state": amenity.get("happiness"),
            })
    return rows


def export_city_yields_csv(raw: bytes, filename: str | None = None, difficulties: dict | None = None,
                           header: bool = True) -> str:
    import csv
    import io

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
    if header:
        writer.writeheader()
    for row in export_city_yields(raw, filename, difficulties):
        writer.writerow(row)
    return out.getvalue()
