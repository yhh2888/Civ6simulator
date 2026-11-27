"""Shared, read-only context for the simulation engine.

An ``EngineContext`` is built from one *state bundle* (map, cities and game
state documents) plus static data (catalog.json, rules_db.json and
game_rules.json). Engine modules never mutate the context; the turn engine
mutates its own deep copy of the bundle and rebuilds the context afterwards.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.config import YIELD_KEYS
from app.repositories.rules_db_repository import RulesDB

DEFAULT_SIMULATION_SETTINGS: dict[str, Any] = {
    "population_yields_source": "config",
    "apply_modifiers": True,
    "apply_amenity_effects": True,
    "apply_housing_growth_penalty": True,
    "apply_game_speed_costs": True,
    "apply_cost_progression": True,
    "resource_visibility_requires_tech": True,
    "luxury_amenities": "auto",
    "overflow_policy": "carry_to_next",
    "research_overflow": "carry",
    "starvation_pop_loss": False,
    "max_simulation_turns": 100,
    "default_citizen_mode": "manual",
    "default_strategy": "balanced",
    "default_new_citizen_policy": "auto",
}

SPEED_IDS = {
    "STANDARD": "GAMESPEED_STANDARD",
    "QUICK": "GAMESPEED_QUICK",
    "ONLINE": "GAMESPEED_ONLINE",
    "EPIC": "GAMESPEED_EPIC",
    "MARATHON": "GAMESPEED_MARATHON",
}

YIELD_IDS = {f"YIELD_{key.upper()}": key for key in YIELD_KEYS}


def zero_yields() -> dict[str, float]:
    return {key: 0.0 for key in YIELD_KEYS}


def add_into(total: dict[str, float], values: dict[str, float], factor: float = 1.0) -> None:
    for key in YIELD_KEYS:
        total[key] = total.get(key, 0.0) + float(values.get(key, 0.0)) * factor


def clean_yields(values: dict[str, float]) -> dict[str, float]:
    return {key: round(float(values.get(key, 0.0)), 6) for key in YIELD_KEYS}


def yield_key(yield_id: str | None) -> str | None:
    if not yield_id:
        return None
    return YIELD_IDS.get(yield_id)


def as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def as_bool(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1")


def tile_key(x: int, y: int) -> str:
    return f"{x},{y}"


def parse_tile_key(key: str) -> tuple[int, int] | None:
    try:
        x_text, y_text = str(key).split(",")
        return int(x_text), int(y_text)
    except (ValueError, AttributeError):
        return None


def neighbor_coords(x: int, y: int) -> list[tuple[int, int]]:
    """Odd-row offset neighbors; consistent with game_data_service.hex_distance."""
    if y % 2 == 0:
        deltas = ((1, 0), (-1, 0), (-1, -1), (0, -1), (-1, 1), (0, 1))
    else:
        deltas = ((1, 0), (-1, 0), (0, -1), (1, -1), (0, 1), (1, 1))
    return [(x + dx, y + dy) for dx, dy in deltas]


def hex_distance(x1: int, y1: int, x2: int, y2: int) -> int:
    q1 = x1 - (y1 - (y1 & 1)) // 2
    q2 = x2 - (y2 - (y2 & 1)) // 2
    return max(abs(q1 - q2), abs(y1 - y2), abs((q1 + y1) - (q2 + y2)))


GAME_SETTING_OVERRIDES = ("apply_amenity_effects",)


def simulation_settings(rules: dict, game: dict | None = None) -> dict[str, Any]:
    """Global simulation settings, then per-game overrides.

    A game may override a few switches (game["simulation_overrides"]); save
    imports use this to switch off amenity effects whose sources (traded
    luxuries, garrisons, religion...) are not decoded.
    """
    settings = deepcopy(DEFAULT_SIMULATION_SETTINGS)
    configured = rules.get("simulation") if isinstance(rules, dict) else None
    if isinstance(configured, dict):
        settings.update(configured)
    overrides = (game or {}).get("simulation_overrides") if isinstance(game, dict) else None
    if isinstance(overrides, dict):
        for key in GAME_SETTING_OVERRIDES:
            if isinstance(overrides.get(key), bool):
                settings[key] = overrides[key]
    return settings


def empire_settings(game: dict) -> dict[str, Any]:
    empire = game.get("empire") if isinstance(game, dict) else None
    empire = dict(empire) if isinstance(empire, dict) else {}
    empire.setdefault("government", None)
    empire.setdefault("policy_slots", {})
    empire.setdefault("civilization", None)
    empire.setdefault("leader", None)
    empire.setdefault("pantheon", None)
    empire.setdefault("religion_beliefs", [])
    empire.setdefault("completed_projects", [])
    empire.setdefault("unit_copies", {})
    empire.setdefault("treasury", {"Gold": 0.0, "Faith": 0.0})
    empire.setdefault("boosted", {"technology": [], "civic": []})
    return empire


@dataclass
class CityView:
    city: dict
    id: str
    center: tuple[int, int]
    tiles: list[dict]
    tile_keys: set[tuple[int, int]]
    buildings: list[str]
    building_types: set[str]
    wonders: list[str]
    district_tiles: list[dict]
    district_types: set[str]
    is_capital: bool


@dataclass
class PlayerView:
    technologies: set[str]
    civics: set[str]
    government: str | None
    policies: list[str]
    civilization: str | None
    leader: str | None
    traits: set[str]
    pantheon: str | None
    beliefs: list[str]
    completed_projects: set[str]
    capital_id: str | None
    # civilization type → {"envoys": int, "suzerain": bool} (user-entered)
    city_states: dict[str, dict] = field(default_factory=dict)


@dataclass
class EngineContext:
    map: dict
    cities: dict[str, dict]
    game: dict
    catalog: dict
    db: RulesDB
    rules: dict
    settings: dict[str, Any]
    empire: dict[str, Any]
    tile_index: dict[tuple[int, int], dict] = field(default_factory=dict)
    city_views: dict[str, CityView] = field(default_factory=dict)
    player: PlayerView | None = None
    _neighbor_cache: dict[tuple[int, int], list[dict]] = field(default_factory=dict)

    # ------------------------------------------------------------------ map
    def tile(self, x: int, y: int) -> dict | None:
        return self.tile_index.get((x, y))

    def neighbors(self, tile: dict) -> list[dict]:
        key = (tile["x"], tile["y"])
        cached = self._neighbor_cache.get(key)
        if cached is None:
            cached = [
                self.tile_index[pos]
                for pos in neighbor_coords(*key)
                if pos in self.tile_index
            ]
            self._neighbor_cache[key] = cached
        return cached

    def tiles_within(self, tile: dict, min_range: int, max_range: int) -> Iterable[dict]:
        x0, y0 = tile["x"], tile["y"]
        for dy in range(-max_range, max_range + 1):
            for dx in range(-max_range - 1, max_range + 2):
                other = self.tile_index.get((x0 + dx, y0 + dy))
                if other is None:
                    continue
                distance = hex_distance(x0, y0, other["x"], other["y"])
                if min_range <= distance <= max_range:
                    yield other

    # -------------------------------------------------------------- helpers
    def game_speed(self) -> str:
        return str(self.game.get("game_speed") or self.rules.get("game_speed") or "STANDARD")

    def speed_cost_multiplier(self) -> tuple[float, str]:
        if not self.settings.get("apply_game_speed_costs", True):
            return 1.0, "게임 속도 비용 배율 비활성화 (game_rules.simulation)"
        speed_id = SPEED_IDS.get(self.game_speed())
        row = self.db.index("GameSpeeds", "GameSpeedType").get(speed_id or "")
        if row is None:
            return 1.0, "GameSpeeds 데이터 없음: 배율 1.0 사용"
        multiplier = as_float(row.get("CostMultiplier"), 100.0) / 100.0
        return multiplier, f"GameSpeeds.{speed_id}.CostMultiplier={row.get('CostMultiplier')}"

    def era_index(self, era: str | None) -> int:
        row = self.db.index("Eras", "EraType").get(era or "")
        return int(as_float(row.get("ChronologyIndex"), 0)) if row else 0

    def entity_era(self, row: dict | None) -> str:
        """Era of a unit/building from its prerequisite technology or civic."""
        if not row:
            return "ERA_ANCIENT"
        tech = row.get("PrereqTech")
        civic = row.get("PrereqCivic")
        if tech:
            tech_row = self.db.index("Technologies", "TechnologyType").get(tech)
            if tech_row and tech_row.get("EraType"):
                return tech_row["EraType"]
        if civic:
            civic_row = self.db.index("Civics", "CivicType").get(civic)
            if civic_row and civic_row.get("EraType"):
                return civic_row["EraType"]
        return "ERA_ANCIENT"

    def district_aliases(self, district_id: str | None) -> set[str]:
        if not district_id:
            return set()
        aliases = {district_id}
        for row in self.db.rows("DistrictReplaces"):
            if row.get("CivUniqueDistrictType") == district_id and row.get("ReplacesDistrictType"):
                aliases.add(row["ReplacesDistrictType"])
        return aliases

    def building_aliases(self, building_id: str) -> set[str]:
        aliases = {building_id}
        for row in self.db.rows("BuildingReplaces"):
            if row.get("CivUniqueBuildingType") == building_id and row.get("ReplacesBuildingType"):
                aliases.add(row["ReplacesBuildingType"])
        return aliases

    def is_specialty_district(self, district_id: str) -> bool:
        row = self.db.index("Districts", "DistrictType").get(district_id)
        return bool(row and as_bool(row.get("RequiresPopulation")))


def _player_view(cities: dict, game: dict, empire: dict, db: RulesDB) -> PlayerView:
    technologies = set(game.get("technologies") or [])
    civics = set(game.get("civics") or [])
    for city in cities.values():
        technologies.update(item for item in city.get("technologies") or [] if isinstance(item, str))
        civics.update(item for item in city.get("civics") or [] if isinstance(item, str))
    capital_id = None
    for city_id, city in cities.items():
        if "BUILDING_PALACE" in (city.get("buildings") or []):
            capital_id = city_id
            break
    if capital_id is None and cities:
        capital_id = next(iter(cities))
    civilization = empire.get("civilization")
    leader = empire.get("leader")
    if capital_id and (not civilization or not leader):
        capital = cities[capital_id]
        civilization = civilization or capital.get("civilization")
        leader = leader or capital.get("leader")
    traits: set[str] = set()
    for row in db.rows("CivilizationTraits"):
        if civilization and row.get("CivilizationType") == civilization and row.get("TraitType"):
            traits.add(row["TraitType"])
    leader_chain: list[str] = []
    current = leader
    leaders = db.index("Leaders", "LeaderType")
    while current and current not in leader_chain:
        leader_chain.append(current)
        current = (leaders.get(current) or {}).get("InheritFrom")
    for row in db.rows("LeaderTraits"):
        if row.get("LeaderType") in leader_chain and row.get("TraitType"):
            traits.add(row["TraitType"])
    policies: list[str] = []
    for slot_policies in (empire.get("policy_slots") or {}).values():
        for policy in slot_policies or []:
            if isinstance(policy, str) and policy and policy not in policies:
                policies.append(policy)
    city_states: dict[str, dict] = {}
    for civ, entry in (empire.get("city_states") or {}).items():
        if isinstance(entry, dict):
            envoys = int(as_float(entry.get("envoys")))
            if envoys > 0 or entry.get("suzerain"):
                city_states[civ] = {"envoys": max(0, envoys), "suzerain": bool(entry.get("suzerain"))}
    return PlayerView(
        city_states=city_states,
        technologies=technologies,
        civics=civics,
        government=empire.get("government"),
        policies=policies,
        civilization=civilization,
        leader=leader,
        traits=traits,
        pantheon=empire.get("pantheon"),
        beliefs=[b for b in empire.get("religion_beliefs") or [] if isinstance(b, str)],
        completed_projects=set(empire.get("completed_projects") or []),
        capital_id=capital_id,
    )


def build_context(
    map_data: dict,
    cities: dict,
    game: dict,
    catalog: dict,
    db: RulesDB,
    rules: dict,
) -> EngineContext:
    ctx = EngineContext(
        map=map_data,
        cities=cities,
        game=game,
        catalog=catalog,
        db=db,
        rules=rules,
        settings=simulation_settings(rules, game),
        empire=empire_settings(game),
    )
    ctx.tile_index = {
        (tile["x"], tile["y"]): tile
        for tile in map_data.get("tiles", [])
        if isinstance(tile, dict) and "x" in tile and "y" in tile
    }
    refresh_views(ctx)
    return ctx


def refresh_views(ctx: EngineContext) -> None:
    """Rebuild player and city views after the documents changed."""
    map_data, cities, db = ctx.map, ctx.cities, ctx.db
    ctx.empire = empire_settings(ctx.game)
    ctx.player = _player_view(cities, ctx.game, ctx.empire, db)
    ctx.city_views = {}
    owned: dict[str, list[dict]] = {city_id: [] for city_id in cities}
    for tile in map_data.get("tiles", []):
        owner = tile.get("owner_city_id")
        if owner in owned:
            owned[owner].append(tile)
    for city_id, city in cities.items():
        center = (city["x"], city["y"])
        tiles = owned.get(city_id, [])
        center_tile = ctx.tile_index.get(center)
        if center_tile is not None and center_tile not in tiles:
            tiles = [center_tile, *tiles]
        tiles = sorted(tiles, key=lambda t: (t["y"], t["x"]))
        district_tiles = [
            t for t in tiles if t.get("district") and (t["x"], t["y"]) != center
        ]
        district_types = {"DISTRICT_CITY_CENTER"}
        for t in district_tiles:
            district_types |= ctx.district_aliases(t["district"])
        for district in city.get("districts") or []:
            if isinstance(district, str):
                district_types |= ctx.district_aliases(district)
        buildings = [b for b in dict.fromkeys(city.get("buildings") or []) if isinstance(b, str)]
        wonders = [w for w in dict.fromkeys(city.get("wonders") or []) if isinstance(w, str)]
        building_types: set[str] = set()
        for building in buildings + wonders:
            building_types |= ctx.building_aliases(building)
        ctx.city_views[city_id] = CityView(
            city=city,
            id=city_id,
            center=center,
            tiles=tiles,
            tile_keys={(t["x"], t["y"]) for t in tiles},
            buildings=buildings,
            building_types=building_types,
            wonders=wonders,
            district_tiles=district_tiles,
            district_types=district_types,
            is_capital=city_id == ctx.player.capital_id,
        )


def _copy_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return deepcopy(value)
    return value


def copy_map(map_data: dict) -> dict:
    """Fast copy of a map document (tiles are flat dicts)."""
    copied = {key: _copy_value(value) for key, value in map_data.items() if key != "tiles"}
    copied["tiles"] = [
        {key: _copy_value(value) for key, value in tile.items()} if isinstance(tile, dict) else tile
        for tile in map_data.get("tiles", [])
    ]
    return copied


def copy_bundle(bundle: dict) -> dict:
    return {
        "map": copy_map(bundle["map"]),
        "cities": deepcopy(bundle["cities"]),
        "game": deepcopy(bundle["game"]),
    }
