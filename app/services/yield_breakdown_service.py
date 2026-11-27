"""City yield calculation with a fully attributed breakdown.

Every number in the result is a *component* with a category, a source and the
yields it contributes, so the UI can show e.g. "서울 과학 12.5 = 인구 2.5 +
캠퍼스 인접 2.0 + 도서관 2.0 + ..." and drill into the tile, building or
modifier that produced it.

Calculation order (engine-defined; see docs in README):

1. worked tile yields (terrain, feature, visible resource, improvement,
   improvement tech/civic bonuses, improvement adjacency, plot modifiers,
   configured tile rules, city-center minimum)
2. specialists on district tiles (District_CitizenYieldChanges)
3. population yields
4. district adjacency (+ district % modifiers, base changes, building copies)
5. buildings and wonders (+ building flat/% modifiers)
6. flat city modifiers, per-district modifiers, user custom effects,
   project yield conversions
7. percentage modifiers on the subtotal (city % modifiers and amenity
   NonFoodYieldModifier), summed additively
"""

from __future__ import annotations

import math
from typing import Any

from app.config import YIELD_KEYS, matching_tile_yield_modifiers
from app.services.modifier_engine import EffectBook, yield_pairs
from app.services.sim_context import (
    EngineContext,
    add_into,
    as_bool,
    as_float,
    clean_yields,
    tile_key,
    yield_key,
    zero_yields,
)

NON_FOOD = tuple(key for key in YIELD_KEYS if key != "Food")
YL = {"Food": "식량", "Production": "생산력", "Gold": "금", "Science": "과학", "Culture": "문화", "Faith": "신앙"}


def _calc_flat(values: dict) -> str:
    return " · ".join(f"{YL[k]} {values[k]:+g}" for k in YIELD_KEYS if abs(values.get(k, 0.0)) > 1e-9)


def _catalog_yield_map(catalog: dict, catalog_key: str, list_key: str) -> dict:
    values = catalog.get(catalog_key)
    if isinstance(values, dict):
        return values
    return {
        item["id"]: item.get("yields", {})
        for item in catalog.get(list_key, [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _raw_yield_map(rows: list[dict], item_column: str) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = row.get(item_column)
        key = yield_key(row.get("YieldType"))
        if not isinstance(item_id, str) or key is None:
            continue
        result.setdefault(item_id, {})
        result[item_id][key] = result[item_id].get(key, 0.0) + as_float(row.get("YieldChange"))
    return result


def _yields(values: Any) -> dict[str, float]:
    result = zero_yields()
    if isinstance(values, dict):
        for key in YIELD_KEYS:
            result[key] = as_float(values.get(key, 0))
    return result


class YieldTables:
    """Static yield tables, cached per catalog/db identity."""

    _cache: dict[tuple[int, int], "YieldTables"] = {}

    def __init__(self, ctx: EngineContext):
        catalog = ctx.catalog
        raw = catalog.get("raw_tables", {}) if isinstance(catalog, dict) else {}
        db = ctx.db
        if db.available and db.rows("Terrain_YieldChanges"):
            # Base yields straight from the Data.zip XML (rules_db.json).
            self.origin = "Data.zip"
            self.terrain = _raw_yield_map(db.rows("Terrain_YieldChanges"), "TerrainType")
            self.feature = _raw_yield_map(db.rows("Feature_YieldChanges"), "FeatureType")
            self.improvement = _raw_yield_map(db.rows("Improvement_YieldChanges"), "ImprovementType")
            self.building = _raw_yield_map(db.rows("Building_YieldChanges"), "BuildingType")
            self.resource = _raw_yield_map(db.rows("Resource_YieldChanges"), "ResourceType")
        else:
            # Fallback when rules_db.json is missing (e.g. minimal test fixtures).
            self.origin = "catalog.json"
            self.terrain = _catalog_yield_map(catalog, "terrain_yields", "terrains")
            self.feature = _catalog_yield_map(catalog, "feature_yields", "features")
            self.improvement = _catalog_yield_map(catalog, "improvement_yields", "improvements")
            self.building = _catalog_yield_map(catalog, "building_yields", "buildings")
            self.resource = _raw_yield_map(raw.get("Resources::Resource_YieldChanges", []), "ResourceType")
        # Citizen (specialist) yields: prefer rules_db, fall back to catalog.
        citizen_rows = ctx.db.rows("District_CitizenYieldChanges") or raw.get(
            "Districts::District_CitizenYieldChanges", []
        )
        self.district_citizen = _raw_yield_map(citizen_rows, "DistrictType")
        self.known_terrains = {
            item["id"] for item in catalog.get("terrains", []) if isinstance(item, dict) and isinstance(item.get("id"), str)
        } | set(ctx.db.index("Terrains", "TerrainType"))
        self.resource_class = {
            item["id"]: item.get("raw", {}).get("ResourceClassType", "UNKNOWN")
            for item in catalog.get("resources", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }

    @classmethod
    def for_context(cls, ctx: EngineContext) -> "YieldTables":
        key = (id(ctx.catalog), id(ctx.db))
        tables = cls._cache.get(key)
        if tables is None:
            if len(cls._cache) > 8:
                cls._cache.clear()
            tables = cls(ctx)
            cls._cache[key] = tables
        return tables


def _component(category: str, kind: str, source_id: str | None, label: str, yields: dict, **extra) -> dict:
    item = {
        "category": category,
        "kind": kind,
        "id": source_id,
        "label": label,
        "yields": clean_yields(yields),
    }
    item.update({k: v for k, v in extra.items() if v is not None})
    return item


# --------------------------------------------------------------- tile yields
def _tech_or_civic_met(ctx: EngineContext, row: dict) -> tuple[bool, str | None]:
    tech, civic = row.get("PrereqTech"), row.get("PrereqCivic")
    if tech and tech not in ctx.player.technologies:
        return False, f"기술 {tech} 필요"
    if civic and civic not in ctx.player.civics:
        return False, f"사회제도 {civic} 필요"
    obsolete = row.get("ObsoleteTech")
    if obsolete and obsolete in ctx.player.technologies:
        return False, f"{obsolete} 연구로 폐기"
    obsolete_civic = row.get("ObsoleteCivic")
    if obsolete_civic and obsolete_civic in ctx.player.civics:
        return False, f"{obsolete_civic} 채택으로 폐기"
    return True, None


def _adjacency_count(ctx: EngineContext, tile: dict, rule: dict, center_positions: set) -> tuple[int | None, str]:
    natural_wonders = {
        row["FeatureType"] for row in ctx.db.rows("Features") if row.get("FeatureType") and as_bool(row.get("NaturalWonder"))
    }
    neighbors = ctx.neighbors(tile)

    def district_at(t: dict) -> str | None:
        if (t["x"], t["y"]) in center_positions:
            return "DISTRICT_CITY_CENTER"
        return t.get("district")

    if rule.get("AdjacentTerrain"):
        return sum(1 for n in neighbors if n.get("terrain") == rule["AdjacentTerrain"]), f"인접 지형 {rule['AdjacentTerrain']}"
    if rule.get("AdjacentFeature"):
        return sum(1 for n in neighbors if n.get("feature") == rule["AdjacentFeature"]), f"인접 특성 {rule['AdjacentFeature']}"
    if as_bool(rule.get("AdjacentRiver")):
        on_river = bool(tile.get("river") or tile.get("river_wires"))
        return (1 if on_river else 0), "강변 여부(타일 강 플래그)"
    if as_bool(rule.get("AdjacentNaturalWonder")):
        return sum(1 for n in neighbors if n.get("feature") in natural_wonders), "인접 자연경관"
    if as_bool(rule.get("AdjacentWonder")):
        return sum(1 for n in neighbors if n.get("district") == "DISTRICT_WONDER"), "인접 불가사의"
    if as_bool(rule.get("AdjacentSeaResource")):
        return (
            sum(1 for n in neighbors if n.get("resource") and str(n.get("terrain", "")).endswith(("COAST", "OCEAN"))),
            "인접 해상 자원",
        )
    if as_bool(rule.get("AdjacentResource")):
        return sum(1 for n in neighbors if n.get("resource")), "인접 자원"
    if rule.get("AdjacentImprovement"):
        return sum(1 for n in neighbors if n.get("improvement") == rule["AdjacentImprovement"]), f"인접 시설 {rule['AdjacentImprovement']}"
    if rule.get("AdjacentDistrict"):
        expected = rule["AdjacentDistrict"]
        return (
            sum(1 for n in neighbors if expected in ctx.district_aliases(district_at(n))),
            f"인접 특수지구 {expected}",
        )
    if as_bool(rule.get("OtherDistrictAdjacent")):
        return (
            sum(1 for n in neighbors if district_at(n) and district_at(n) != "DISTRICT_WONDER"),
            "인접 특수지구(도심 포함, 불가사의 제외)",
        )
    resource_class = rule.get("AdjacentResourceClass")
    if resource_class and resource_class != "NO_RESOURCECLASS":
        resources = ctx.db.index("Resources", "ResourceType")
        check_tech = ctx.settings.get("resource_visibility_requires_tech", True)

        def matches(n: dict) -> bool:
            row = resources.get(n.get("resource") or "")
            if not row or row.get("ResourceClassType") != resource_class:
                return False
            prereq = row.get("PrereqTech")
            return not (check_tech and prereq and prereq not in ctx.player.technologies)

        return sum(1 for n in neighbors if matches(n)), f"인접 {resource_class} 자원(발견한 것만)"
    return None, "지원하지 않는 인접 조건"


def compute_tile(
    ctx: EngineContext,
    book: EffectBook,
    tile: dict,
    city_id: str | None,
    *,
    worked: bool,
    is_center: bool,
) -> dict:
    """Yields of one (non-district) plot with component attribution."""
    tables = YieldTables.for_context(ctx)
    rules = ctx.rules
    components: list[dict] = []
    excluded: list[dict] = []
    terrain_id = tile.get("terrain", "")
    base = _yields(tables.terrain.get(terrain_id))
    source = "XML Terrain_YieldChanges"
    if terrain_id not in tables.terrain and terrain_id in tables.known_terrains:
        source = "XML: 이 지형은 Terrain_YieldChanges 행이 없음 → 0"
    elif terrain_id not in tables.terrain:
        base_terrain = next(
            (
                name
                for name in ("GRASS", "PLAINS", "DESERT", "TUNDRA", "SNOW", "COAST", "OCEAN", "LAKE")
                if f"TERRAIN_{name}" in terrain_id
            ),
            None,
        )
        base = _yields(rules["tile_yields"]["fallback_terrain_yields"].get(base_terrain, {}))
        source = "game_rules.json fallback"
        if "HILLS" in terrain_id:
            base["Production"] += rules["tile_yields"]["hill_production_delta"]
    terrain_is_hills = "HILLS" in terrain_id
    hill_override = tile.get("hill_override")
    is_hills = bool(hill_override) if hill_override is not None else terrain_is_hills
    if hill_override is not None and bool(hill_override) != terrain_is_hills:
        delta = rules["tile_yields"]["hill_production_delta"]
        base["Production"] = max(0.0, base["Production"] + (delta if hill_override else -delta))
        source += " + 수동 언덕 설정"
    components.append(_component("tile", "terrain", terrain_id, f"지형 {terrain_id}", base, source=source))
    total = dict(base)

    feature_id = tile.get("feature")
    if feature_id and feature_id in tables.feature:
        values = _yields(tables.feature[feature_id])
        add_into(total, values)
        components.append(_component("tile", "feature", feature_id, f"지형 특성 {feature_id}", values))
    events = tile.get("event_yields")
    if isinstance(events, dict):
        values = _yields(events)
        if any(values.values()):
            add_into(total, values)
            components.append(_component("tile", "event_yields", "PLOT_YIELD_CHANGES",
                                         "자연재해로 늘어난 산출 (범람·화산·폭풍 등, 세이브 값)", values,
                                         source="세이브 지도 구역의 타일별 산출 변화 목록"))
    resource_id = tile.get("resource")
    if resource_id and resource_id in tables.resource:
        values = _yields(tables.resource[resource_id])
        resource_row = ctx.db.index("Resources", "ResourceType").get(resource_id, {})
        prereq = resource_row.get("PrereqTech")
        if (
            prereq
            and ctx.settings.get("resource_visibility_requires_tech", True)
            and prereq not in ctx.player.technologies
        ):
            components.append(
                _component("tile", "resource_hidden", resource_id, f"자원 {resource_id} (미발견: {prereq} 필요)", zero_yields())
            )
            excluded.append({"kind": "resource", "id": resource_id, "label": f"자원 {resource_id}",
                             "yields": clean_yields(values), "reason": f"발견 기술 {prereq} 미보유 (Resources.PrereqTech)",
                             "table": "Resources", "key": resource_id})
        else:
            add_into(total, values)
            components.append(_component("tile", "resource", resource_id, f"자원 {resource_id}", values))
    improvement_id = tile.get("improvement")
    if improvement_id and is_center:
        excluded.append({"kind": "improvement", "id": improvement_id, "label": f"시설 {improvement_id}",
                         "reason": "도심 타일의 시설은 계산하지 않음", "table": "Improvement_YieldChanges", "key": improvement_id})
    if improvement_id and not is_center and tile.get("improvement_pillaged"):
        excluded.append({"kind": "improvement", "id": improvement_id, "label": f"시설 {improvement_id} (약탈됨)",
                         "reason": "약탈된 시설은 수리 전까지 산출이 없음 (세이브의 약탈 표시)",
                         "table": "Improvement_YieldChanges", "key": improvement_id})
        components.append(_component("tile", "improvement_pillaged", improvement_id,
                                     f"시설 {improvement_id} (약탈됨: 산출 없음)", zero_yields(),
                                     source="세이브 타일 기록의 약탈 표시"))
    elif improvement_id and not is_center:
        if improvement_id in tables.improvement:
            values = _yields(tables.improvement[improvement_id])
            add_into(total, values)
            components.append(_component("tile", "improvement", improvement_id, f"시설 {improvement_id}", values))
        for row in ctx.db.group("Improvement_BonusYieldChanges", "ImprovementType").get(improvement_id, []):
            key = yield_key(row.get("YieldType"))
            if key is None:
                continue
            met, reason = _tech_or_civic_met(ctx, row)
            values = zero_yields()
            values[key] = as_float(row.get("BonusYieldChange"))
            prereq = row.get("PrereqTech") or row.get("PrereqCivic")
            if not met:
                excluded.append({"kind": "improvement_bonus", "id": row.get("Id"), "label": f"{improvement_id} 보너스 ({prereq})",
                                 "yields": clean_yields(values), "reason": reason,
                                 "table": "Improvement_BonusYieldChanges", "key": row.get("Id")})
                continue
            add_into(total, values)
            components.append(
                _component("tile", "improvement_bonus", row.get("Id"), f"{improvement_id} 보너스 ({prereq})", values,
                           source="XML Improvement_BonusYieldChanges", improvement=improvement_id)
            )
        adjacency_rules = ctx.db.index("Adjacency_YieldChanges", "ID")
        centers = {view.center for view in ctx.city_views.values()}
        for link in ctx.db.group("Improvement_Adjacencies", "ImprovementType").get(improvement_id, []):
            rule = adjacency_rules.get(link.get("YieldChangeId"))
            key = yield_key((rule or {}).get("YieldType"))
            if not rule or key is None:
                continue
            met, reason = _tech_or_civic_met(ctx, rule)
            if not met:
                excluded.append({"kind": "improvement_adjacency", "id": rule.get("ID"), "label": f"{improvement_id} 인접 규칙 {rule.get('ID')}",
                                 "reason": reason, "table": "Adjacency_YieldChanges", "key": rule.get("ID")})
                continue
            count, label = _adjacency_count(ctx, tile, rule, centers)
            if not count:
                continue
            required = max(1, int(as_float(rule.get("TilesRequired"), 1)))
            amount = (count // required) * as_float(rule.get("YieldChange"))
            if amount:
                values = zero_yields()
                values[key] = amount
                add_into(total, values)
                components.append(
                    _component("tile", "improvement_adjacency", rule.get("ID"), f"{improvement_id} 인접 ({label} {count}개)", values,
                               source="XML Improvement_Adjacencies")
                )
    # Data-driven plot modifiers (buildings, policies, beliefs, ...).
    for record in book.plot_records(tile["x"], tile["y"]):
        values = zero_yields()
        for key, amount in yield_pairs(record.data):
            values[key] += amount
        add_into(total, values)
        components.append(
            _component("tile", "modifier", record.modifier_id, f"{record.source_kind} {record.source_id}", values,
                       attribution=record.attribution(), calc=f"타일 산출 {_calc_flat(values)}")
        )
    # Configured tile rules from game_rules.json (legacy feature).
    context = {
        "terrain": terrain_id,
        "feature": feature_id,
        "resource": resource_id,
        "improvement": improvement_id,
        "district": tile.get("district"),
        "worked": worked,
        "city_center": is_center,
        "is_hills": is_hills,
        "river_adjacent": bool(tile.get("river") or tile.get("river_wires")),
    }
    for modifier in matching_tile_yield_modifiers(context, rules["tile_yield_modifiers"]):
        values = _yields(modifier["yield_changes"])
        add_into(total, values)
        components.append(
            _component("tile", "configured_rule", modifier["id"], modifier.get("description") or modifier["id"], values,
                       source="game_rules.json tile_yield_modifiers")
        )
    if is_center:
        floor = zero_yields()
        for key, minimum in rules["city"]["city_center_min_yields"].items():
            floor[key] = max(0.0, float(minimum) - total.get(key, 0.0))
        if any(floor.values()):
            add_into(total, floor)
            components.append(_component("tile", "center_minimum", "CITY_CENTER", "도심 최소 산출량", floor,
                                         source="game_rules.json city_center_min_yields"))
    # User correction for this plot (e.g. river flood bonuses the save does
    # not store): added as typed, shown as its own line.
    owner = ctx.cities.get(city_id) if city_id else None
    adjust = ((owner or {}).get("tile_adjustments") or {}).get(tile_key(tile["x"], tile["y"]))
    if isinstance(adjust, dict):
        values = _yields(adjust.get("yields"))
        if any(values.values()):
            add_into(total, values)
            components.append(_component("tile", "user_tile", tile_key(tile["x"], tile["y"]),
                                         f"수동 보정: {adjust.get('note') or '게임 값에 맞춤'}", values,
                                         source="사용자 입력 (게임 데이터 아님)"))
    return {
        "x": tile["x"],
        "y": tile["y"],
        "key": tile_key(tile["x"], tile["y"]),
        "is_hills": is_hills,
        "yields": clean_yields(total),
        "components": components,
        "excluded": excluded,
    }


# ------------------------------------------------------------- city helpers
def specialist_slots(ctx: EngineContext, city_id: str, tile: dict) -> int:
    """Citizen slots on a district tile: district + buildings that require it."""
    district_id = tile.get("district")
    if not district_id or district_id == "DISTRICT_WONDER":
        return 0
    view = ctx.city_views[city_id]
    aliases = ctx.district_aliases(district_id)
    district_row = ctx.db.index("Districts", "DistrictType").get(district_id, {})
    slots = int(as_float(district_row.get("CitizenSlots"), 0))
    buildings = ctx.db.index("Buildings", "BuildingType")
    for building in view.buildings:
        row = buildings.get(building, {})
        if row.get("PrereqDistrict") in aliases:
            slots += int(as_float(row.get("CitizenSlots"), 0))
    return slots


def worker_capacity(ctx: EngineContext, city: dict) -> int:
    population = max(0, int(city.get("population", ctx.rules["city"]["initial_population"])))
    return population * int(ctx.rules["city"]["worked_tiles_per_population"])


def current_assignment(ctx: EngineContext, city_id: str) -> dict[str, int]:
    """Assignment stored in the documents: map worked flags + specialist counts."""
    view = ctx.city_views[city_id]
    specialists = view.city.get("specialists") or {}
    assignment: dict[str, int] = {}
    for tile in view.tiles:
        position = (tile["x"], tile["y"])
        if position == view.center:
            continue
        key = tile_key(*position)
        if tile.get("district"):
            count = int(as_float(specialists.get(key), 1 if tile.get("worked") else 0))
            if count > 0:
                assignment[key] = count
        elif tile.get("worked"):
            assignment[key] = 1
    return assignment


def workable_options(ctx: EngineContext, book: EffectBook, city_id: str) -> list[dict]:
    """Every slot a citizen could fill, with per-citizen yields."""
    view = ctx.city_views[city_id]
    tables = YieldTables.for_context(ctx)
    options = []
    for tile in view.tiles:
        position = (tile["x"], tile["y"])
        if position == view.center:
            continue
        district_id = tile.get("district")
        key = tile_key(*position)
        if district_id:
            if district_id == "DISTRICT_WONDER":
                continue
            slots = specialist_slots(ctx, city_id, tile)
            if slots <= 0:
                continue
            per = _yields(tables.district_citizen.get(district_id, {}))
            if not any(per.values()):
                for alias in ctx.district_aliases(district_id):
                    per = _yields(tables.district_citizen.get(alias, {}))
                    if any(per.values()):
                        break
            options.append({"key": key, "x": tile["x"], "y": tile["y"], "kind": "specialist",
                            "district": district_id, "slots": slots, "yields": clean_yields(per)})
        else:
            result = compute_tile(ctx, book, tile, city_id, worked=True, is_center=False)
            options.append({"key": key, "x": tile["x"], "y": tile["y"], "kind": "tile",
                            "slots": 1, "yields": result["yields"], "components": result["components"]})
    return options


# ------------------------------------------------------------- districts
def district_adjacency(ctx: EngineContext, book: EffectBook, city_id: str) -> list[dict]:
    view = ctx.city_views[city_id]
    links = ctx.db.group("District_Adjacencies", "DistrictType")
    rules = ctx.db.index("Adjacency_YieldChanges", "ID")
    excluded = {
        row.get("YieldChangeId")
        for row in ctx.db.rows("ExcludedAdjacencies")
        if row.get("TraitType") in ctx.player.traits
    }
    centers = {v.center for v in ctx.city_views.values()}
    extra_rules = [
        record
        for record in book.records
        if record.effect in ("EFFECT_DISTRICT_ADJACENCY", "EFFECT_TERRAIN_ADJACENCY", "EFFECT_FEATURE_ADJACENCY",
                             "EFFECT_IMPROVEMENT_ADJACENCY")
        and record.subject.city_id == city_id
    ]
    tiles = list(view.district_tiles)
    # The city centre only takes modifier-added rules (Mali: +1 food, +1 faith
    # per adjacent desert / desert hills).
    center_tile = ctx.tile_index.get(view.center)
    if center_tile is not None and any(r.data.get("DistrictType") == "DISTRICT_CITY_CENTER" for r in extra_rules):
        tiles.append({**center_tile, "district": "DISTRICT_CITY_CENTER", "_center": True})
    results = []
    for tile in tiles:
        district_id = tile.get("district")
        if district_id == "DISTRICT_WONDER":
            continue
        own_links = [] if tile.get("_center") else links.get(district_id, [])
        if not own_links:
            for alias in sorted(ctx.district_aliases(district_id) - {district_id}):
                own_links = links.get(alias, [])
                if own_links:
                    break
        adjacency = zero_yields()
        components = []
        inactive = []
        for link in own_links:
            rule_id = link.get("YieldChangeId")
            rule = rules.get(rule_id)
            key = yield_key((rule or {}).get("YieldType"))
            if not rule or key is None:
                continue
            if rule_id in excluded:
                inactive.append({"rule": rule_id, "reason": "문명 특성으로 제외(ExcludedAdjacencies)"})
                continue
            met, reason = _tech_or_civic_met(ctx, rule)
            if not met:
                inactive.append({"rule": rule_id, "reason": reason})
                continue
            count, label = _adjacency_count(ctx, tile, rule, centers)
            if count is None:
                inactive.append({"rule": rule_id, "reason": label})
                continue
            required = max(1, int(as_float(rule.get("TilesRequired"), 1)))
            amount = (count // required) * as_float(rule.get("YieldChange"))
            if amount:
                adjacency[key] += amount
                values = zero_yields()
                values[key] = amount
                components.append(_component("district", "adjacency", rule_id,
                                             f"{label} {count}개 ÷ {required} × {rule.get('YieldChange')}", values,
                                             source="XML District_Adjacencies"))
        aliases = ctx.district_aliases(district_id)
        for record in extra_rules:
            data = record.data
            if data.get("DistrictType") not in aliases and data.get("DistrictType") != district_id:
                continue
            key = yield_key(data.get("YieldType"))
            if key is None:
                continue
            required = max(1, int(as_float(data.get("TilesRequired"), 1)))
            if record.effect == "EFFECT_DISTRICT_ADJACENCY":
                pseudo = {"OtherDistrictAdjacent": "true"}
            elif record.effect == "EFFECT_TERRAIN_ADJACENCY":
                pseudo = {"AdjacentTerrain": data.get("TerrainType")}
            elif record.effect == "EFFECT_IMPROVEMENT_ADJACENCY":
                pseudo = {"AdjacentImprovement": data.get("ImprovementType")}
            else:
                pseudo = {"AdjacentFeature": data.get("FeatureType")}
            count, label = _adjacency_count(ctx, tile, pseudo, centers)
            amount = ((count or 0) // required) * as_float(data.get("Amount"))
            if amount:
                adjacency[key] += amount
                values = zero_yields()
                values[key] = amount
                components.append(_component("district", "modifier_adjacency", record.modifier_id,
                                             f"{record.source_kind} {record.source_id}: {label} {count}개", values,
                                             attribution=record.attribution(),
                                             calc=f"{label} {count}개 ÷ {required} × {as_float(data.get('Amount')):g}"))
        total = dict(adjacency)
        for record in book.records:
            if record.subject.kind != "district" or record.subject.x != tile["x"] or record.subject.y != tile["y"]:
                continue
            if record.effect == "EFFECT_ADJUST_DISTRICT_YIELD_MODIFIER":
                values = zero_yields()
                calc = []
                for key, pct in yield_pairs(record.data):
                    values[key] += adjacency.get(key, 0.0) * pct / 100.0
                    calc.append(f"인접 {YL[key]} {adjacency.get(key, 0.0):g} × {pct:+g}% = {adjacency.get(key, 0.0) * pct / 100.0:+g}")
                if any(values.values()):
                    add_into(total, values)
                    components.append(_component("district", "adjacency_percent", record.modifier_id,
                                                 f"{record.source_kind} {record.source_id}: 인접 보너스 % 증가", values,
                                                 attribution=record.attribution(), calc="; ".join(calc)))
            elif record.effect == "EFFECT_ADJUST_DISTRICT_YIELD_BASED_ON_ADJACENCY_BONUS":
                # Theodora: Holy Sites give Culture equal to their Faith adjacency.
                mirror, grant = yield_key(record.data.get("YieldTypeToMirror")), yield_key(record.data.get("YieldTypeToGrant"))
                if mirror and grant and adjacency.get(mirror):
                    values = zero_yields()
                    values[grant] = adjacency[mirror]
                    add_into(total, values)
                    components.append(_component("district", "adjacency_mirror", record.modifier_id,
                                                 f"{record.source_kind} {record.source_id}", values,
                                                 attribution=record.attribution(),
                                                 calc=f"인접 {YL[mirror]} {adjacency[mirror]:g} → {YL[grant]} +{adjacency[mirror]:g}"))
            elif record.effect == "EFFECT_ADJUST_DISTRICT_BASE_YIELD_CHANGE":
                values = zero_yields()
                for key, amount in yield_pairs(record.data):
                    values[key] += amount
                add_into(total, values)
                components.append(_component("district", "district_base", record.modifier_id,
                                             f"{record.source_kind} {record.source_id}", values,
                                             attribution=record.attribution(), calc=f"특수지구 기본 {_calc_flat(values)}"))
        if tile.get("_center") and not any(total.values()):
            continue
        results.append({
            "x": tile["x"], "y": tile["y"], "key": tile_key(tile["x"], tile["y"]),
            "id": district_id, "adjacency": clean_yields(adjacency),
            "yields": clean_yields(total), "components": components, "inactive_rules": inactive,
        })
    return results


# ------------------------------------------------------------------- city
def compute_city(
    ctx: EngineContext,
    book: EffectBook,
    city_id: str,
    assignment: dict[str, int] | None = None,
    *,
    amenity_state: dict | None = None,
    project_conversion: list[dict] | None = None,
) -> dict:
    view = ctx.city_views[city_id]
    city = view.city
    tables = YieldTables.for_context(ctx)
    if assignment is None:
        assignment = current_assignment(ctx, city_id)
    capacity = worker_capacity(ctx, city)
    components: list[dict] = []
    tiles_out: list[dict] = []
    warnings: list[str] = []
    totals_by_category: dict[str, dict[str, float]] = {}

    def add(category: str, values: dict) -> None:
        bucket = totals_by_category.setdefault(category, zero_yields())
        add_into(bucket, values)

    # 1. tiles + specialists
    used = 0
    accepted: dict[str, int] = {}
    tile_by_key = {tile_key(t["x"], t["y"]): t for t in view.tiles}
    for key in sorted(assignment, key=lambda k: tuple(int(v) for v in k.split(","))[::-1]):
        count = int(assignment[key])
        tile = tile_by_key.get(key)
        if tile is None or count <= 0 or (tile["x"], tile["y"]) == view.center:
            if tile is None:
                warnings.append(f"배치된 타일 {key}은 이 도시 소유가 아님 (무시)")
            continue
        if tile.get("district"):
            slots = specialist_slots(ctx, city_id, tile)
            if slots <= 0:
                warnings.append(f"{key} {tile.get('district')}: 전문가 슬롯 없음 (건물 필요)")
                continue
            count = min(count, slots)
        else:
            count = 1
        if used + count > capacity:
            count = capacity - used
            warnings.append(f"{key}: 시민 수 한도 초과로 일부 배치 무시")
        if count <= 0:
            continue
        used += count
        accepted[key] = count

    # Which built building sits in which district (Buildings.PrereqDistrict;
    # wonders are on their own tiles).
    buildings_index = ctx.db.index("Buildings", "BuildingType")
    buildings_by_district: dict[str, list[str]] = {}
    for building in city.get("buildings") or []:
        row = buildings_index.get(building) or {}
        if as_bool(row.get("IsWonder")):
            continue
        buildings_by_district.setdefault(row.get("PrereqDistrict") or "DISTRICT_CITY_CENTER", []).append(building)

    for tile in view.tiles:
        position = (tile["x"], tile["y"])
        key = tile_key(*position)
        is_center = position == view.center
        count = accepted.get(key, 0)
        if tile.get("district") and not is_center:
            tile_buildings = buildings_by_district.get(tile["district"], []) + [
                b for alias in ctx.district_aliases(tile["district"]) if alias != tile["district"]
                for b in buildings_by_district.get(alias, [])]
            per = _yields(tables.district_citizen.get(tile["district"], {}))
            if not any(per.values()):
                for alias in ctx.district_aliases(tile["district"]):
                    per = _yields(tables.district_citizen.get(alias, {}))
                    if any(per.values()):
                        break
            slots = specialist_slots(ctx, city_id, tile)
            yields = zero_yields()
            tile_components = []
            if count:
                add_into(yields, per, count)
                comp = _component("specialists", "specialist", tile["district"],
                                  f"{tile['district']} 전문가 {count}명", yields, tile=key)
                components.append(comp)
                tile_components.append(comp)
                add("specialists", yields)
            tiles_out.append({"key": key, "x": tile["x"], "y": tile["y"], "worked": bool(count),
                              "citizens": count, "district": tile["district"], "specialist_slots": slots,
                              "per_citizen": clean_yields(per), "yields": clean_yields(yields),
                              "components": tile_components, "workable": slots > 0,
                              "buildings": sorted(set(tile_buildings))})
            continue
        worked = is_center or count > 0
        result = compute_tile(ctx, book, tile, city_id, worked=worked, is_center=is_center)
        if worked:
            add("tiles", result["yields"])
            components.append(_component("tiles", "worked_tile", key,
                                         ("도심 " if is_center else "타일 ") + f"({key}) {tile.get('terrain')}",
                                         result["yields"], tile=key, details=result["components"]))
        tiles_out.append({"key": key, "x": tile["x"], "y": tile["y"], "worked": worked, "citizens": 1 if worked and not is_center else 0,
                          "city_center": is_center, "terrain": tile.get("terrain"), "feature": tile.get("feature"),
                          "resource": tile.get("resource"), "improvement": tile.get("improvement"),
                          "improvement_pillaged": bool(tile.get("improvement_pillaged")),
                          "is_hills": result["is_hills"], "yields": result["yields"],
                          "components": result["components"], "excluded": result["excluded"],
                          "workable": not is_center,
                          **({"district": "DISTRICT_CITY_CENTER",
                              "buildings": sorted(set(buildings_by_district.get("DISTRICT_CITY_CENTER", [])))}
                             if is_center else {})})

    # 3. population
    population = max(0, int(city.get("population", ctx.rules["city"]["initial_population"])))
    use_params = (ctx.settings.get("population_yields_source") == "global_parameters"
                  or bool((ctx.db.meta or {}).get("ruleset")))  # expansion rules: always the data values
    if use_params and ctx.db.has_param("SCIENCE_PERCENTAGE_YIELD_PER_POP"):
        per_pop = zero_yields()
        # 1/256 fixed point, rounded: 30 % → 77/256 per citizen. Mediolanum
        # (pop 5) shows culture +1.5 (76/256 would give 1.48); Niani (pop 4,
        # culture +39 %) shows 8.6 where exactly 0.3 gives 8.597.
        fixed_point = ctx.settings.get("fixed_point_percent", True)
        for key, name in (("Science", "SCIENCE_PERCENTAGE_YIELD_PER_POP"), ("Culture", "CULTURE_PERCENTAGE_YIELD_PER_POP")):
            pct = ctx.db.param(name, 0) or 0
            per_pop[key] = math.floor(pct * 2.56 + 0.5) / 256 if fixed_point else pct / 100.0
        pop_source = "GlobalParameters SCIENCE/CULTURE_PERCENTAGE_YIELD_PER_POP"
    else:
        per_pop = _yields(ctx.rules["city"]["population_yields"])
        pop_source = "game_rules.json city.population_yields"
    pop_values = zero_yields()
    add_into(pop_values, per_pop, population)
    if any(pop_values.values()):
        components.append(_component("population", "population", None, f"인구 {population} × 시민 기본 산출", pop_values,
                                     source=pop_source))
        add("population", pop_values)

    # 4. districts
    districts = district_adjacency(ctx, book, city_id)
    district_by_type: dict[str, dict] = {}
    for district in districts:
        for alias in ctx.district_aliases(district["id"]):
            district_by_type.setdefault(alias, district)
        if any(district["yields"].values()):
            components.append(_component("districts", "district", district["id"],
                                         f"{district['id']} ({district['key']})", district["yields"],
                                         tile=district["key"], details=district["components"]))
            add("districts", district["yields"])

    # 5. buildings
    buildings_out = []
    for building in city.get("pillaged_buildings") or []:
        components.append(_component("buildings", "building_pillaged", building,
                                     f"건물 {building} (약탈됨: 수리 전까지 산출·효과 없음)", zero_yields(),
                                     source="세이브 도시 건물 표의 약탈 표시"))
    building_rows = ctx.db.index("Buildings", "BuildingType")
    for building in dict.fromkeys(view.buildings + view.wonders):
        direct = _yields(tables.building.get(building))
        details = []
        if any(direct.values()):
            details.append(_component("buildings", "building_base", building, "XML Building_YieldChanges", direct))
        total = dict(direct)
        for copy in ctx.db.group("Building_YieldDistrictCopies", "BuildingType").get(building, []):
            row = building_rows.get(building, {})
            district = district_by_type.get(row.get("PrereqDistrict") or "")
            old_key, new_key = yield_key(copy.get("OldYieldType")), yield_key(copy.get("NewYieldType"))
            if district and old_key and new_key:
                values = zero_yields()
                values[new_key] = district["adjacency"].get(old_key, 0.0)
                if values[new_key]:
                    add_into(total, values)
                    details.append(_component("buildings", "district_copy", building,
                                              f"{district['id']} 인접 {old_key} → {new_key} 복제", values,
                                              source="XML Building_YieldDistrictCopies"))
        for record in book.city_records(city_id, "EFFECT_ADJUST_BUILDING_YIELD_CHANGE"):
            if record.data.get("BuildingType") not in ctx.building_aliases(building):
                continue
            values = zero_yields()
            for key, amount in yield_pairs(record.data):
                values[key] += amount
            add_into(total, values)
            details.append(_component("buildings", "modifier", record.modifier_id,
                                      f"{record.source_kind} {record.source_id}", values, attribution=record.attribution(),
                                      calc=f"{building} 산출 {_calc_flat(values)}"))
        for record in book.city_records(city_id, "EFFECT_ADJUST_BUILDING_YIELD_MODIFIER"):
            target = record.data.get("BuildingType")
            if target and target not in ctx.building_aliases(building):
                continue
            values = zero_yields()
            calc = []
            for key, pct in yield_pairs(record.data):
                values[key] += direct.get(key, 0.0) * pct / 100.0
                calc.append(f"{building} 기본 {YL[key]} {direct.get(key, 0.0):g} × {pct:+g}%")
            if any(values.values()):
                add_into(total, values)
                details.append(_component("buildings", "modifier_percent", record.modifier_id,
                                          f"{record.source_kind} {record.source_id} (+%)", values,
                                          attribution=record.attribution(), calc="; ".join(calc)))
        if any(total.values()) or details:
            buildings_out.append({"id": building, "yields": clean_yields(total), "components": details,
                                  "is_wonder": building in view.wonders})
            if any(total.values()):
                components.append(_component("buildings", "wonder" if building in view.wonders else "building",
                                             building, building, total, details=details))
                add("buildings", total)

    # 5a. great works placed in the city (entered on the city; not decoded)
    great_works = ctx.db.group("GreatWork_YieldChanges", "GreatWorkType")
    work_rows = ctx.db.index("GreatWorks", "GreatWorkType")
    # Per-object-type extras such as Kongo's Nkisi (+2 food, +2 production,
    # +4 gold, +1 faith per relic, artifact and sculpture).
    object_bonus = book.city_records(city_id, "EFFECT_ADJUST_CITY_GREATWORK_YIELD")
    for work in city.get("great_works") or []:
        values = zero_yields()
        details = []
        for row in great_works.get(work, []):
            key = yield_key(row.get("YieldType"))
            if key:
                values[key] += as_float(row.get("YieldChange"))
        object_type = (work_rows.get(work) or {}).get("GreatWorkObjectType")
        for record in object_bonus:
            if object_type and record.data.get("GreatWorkObjectType") == object_type:
                key = yield_key(record.data.get("YieldType"))
                amount = as_float(record.data.get("YieldChange"))
                if key and amount:
                    values[key] += amount
                    extra = zero_yields()
                    extra[key] = amount
                    details.append(_component("great_works", "great_work_bonus", record.modifier_id,
                                              f"{record.source_kind} {record.source_id}", extra,
                                              attribution=record.attribution()))
        if any(values.values()):
            components.append(_component("great_works", "great_work", work, f"걸작 {work}", values,
                                         details=details or None))
            add("great_works", values)

    # 5b. trade routes (origin-city yields of routes entered on the city)
    from app.services.trade_route_service import route_results

    routes, route_warnings = route_results(ctx, book, city_id)
    warnings.extend(route_warnings)
    for route in routes:
        destination = route["destination"]
        kind_label = "국내" if destination["kind"] == "domestic" else "국제"
        details = []
        for item in route["details"]:
            if item["kind"] == "modifier":
                record = item["record"]
                details.append(_component("trade_routes", "modifier", record.modifier_id, item["label"], item["yields"],
                                          attribution=record.attribution(),
                                          calc=f"{kind_label} 교역로 {_calc_flat(item['yields'])}"))
            elif item["kind"] == "cs_trade":
                details.append(_component("trade_routes", "cs_trade", item["id"], item["label"], item["yields"]))
            else:
                details.append(_component("trade_routes", "trade_district", item["id"], item["label"], item["yields"],
                                          column=item["column"]))
        if any(route["yields"].values()) or details:
            components.append(_component("trade_routes", "trade_route", destination["key"],
                                         f"{kind_label} 교역로 → {destination['name']} ({destination['key']})",
                                         route["yields"], details=details, destination=destination["key"],
                                         route_kind=destination["kind"], timing=route["timing"]))
            add("trade_routes", route["yields"])

    # 5b'. incoming routes (other players' traders ending here): the
    # destination gets only what …_FROM_OTHERS modifiers grant (e.g. the World
    # Congress trade treaty: +4 gold per incoming route).
    from app.services.trade_route_service import incoming_results

    for route in incoming_results(ctx, book, city_id):
        details = [_component("incoming_routes", "modifier", item["record"].modifier_id, item["label"], item["yields"],
                              attribution=item["record"].attribution(),
                              calc=f"수입 교역로 {_calc_flat(item['yields'])}")
                   for item in route["details"]]
        if any(route["yields"].values()):
            components.append(_component("incoming_routes", "incoming_route", route["origin"],
                                         f"수입 교역로 ← {route['origin_name']} ({route['origin']})",
                                         route["yields"], details=details, origin=route["origin"]))
            add("incoming_routes", route["yields"])

    # 5c. production toward the item being built (e.g. city-state envoys:
    # +1 toward buildings/districts/units). Counted in the city's production,
    # as the game's tooltip does ("buildings, districts and wonders +N").
    queue = city.get("production_queue") or []
    if queue:
        from app.services.production_engine import production_flat

        head = queue[0]
        flat, flat_parts = production_flat(ctx, book, city_id, head.get("category"))
        if flat:
            details = [_component("build_bonus", "modifier", None, part["label"], {**zero_yields(), "Production": part["amount"]},
                                  attribution=part["attribution"],
                                  calc=f"{head.get('target_id')} 생산 중 → 생산력 +{part['amount']:g}")
                       for part in flat_parts]
            values = {**zero_yields(), "Production": flat}
            components.append(_component("build_bonus", "build_bonus", head.get("target_id"),
                                         f"현재 생산 항목 보너스 ({head.get('target_id')})", values, details=details))
            add("build_bonus", values)

    # 6. flat modifiers / custom / projects
    for record in book.city_records(city_id, "EFFECT_ADJUST_CITY_YIELD_CHANGE"):
        values = zero_yields()
        for key, amount in yield_pairs(record.data):
            values[key] += amount
        components.append(_component("modifiers", "city_flat", record.modifier_id,
                                     f"{record.source_kind} {record.source_id}", values, attribution=record.attribution(),
                                     calc=f"도시 산출 {_calc_flat(values)}"))
        add("modifiers", values)
    for record in book.city_records(city_id, "EFFECT_ADJUST_CITY_YIELD_PER_POPULATION"):
        values = zero_yields()
        for key, amount in yield_pairs(record.data):
            values[key] += amount * population
        if any(values.values()):
            components.append(_component("modifiers", "per_population", record.modifier_id,
                                         f"{record.source_kind} {record.source_id} × 인구 {population}",
                                         values, attribution=record.attribution(),
                                         calc=" · ".join(f"{YL[k]} {a:g} × 인구 {population}" for k, a in yield_pairs(record.data))))
            add("modifiers", values)
    specialty_count = sum(1 for t in view.district_tiles if ctx.is_specialty_district(t.get("district", "")))
    for record in book.city_records(city_id, "EFFECT_ADJUST_CITY_YIELD_PER_DISTRICT"):
        values = zero_yields()
        for key, amount in yield_pairs(record.data):
            values[key] += amount * specialty_count
        if any(values.values()):
            components.append(_component("modifiers", "per_district", record.modifier_id,
                                         f"{record.source_kind} {record.source_id} × 전문 특수지구 {specialty_count}",
                                         values, attribution=record.attribution(),
                                         calc=" · ".join(f"{YL[k]} {a:g} × 전문 특수지구 {specialty_count}개" for k, a in yield_pairs(record.data))))
            add("modifiers", values)
    custom_percent = zero_yields()
    for effect in city.get("custom_effects") or []:
        if not isinstance(effect, dict) or effect.get("enabled") is False:
            continue
        values = _yields(effect.get("yields"))
        if any(values.values()):
            components.append(_component("custom", "user_effect", effect.get("id"),
                                         f"사용자 정의: {effect.get('label') or effect.get('id')}", values,
                                         source="사용자 입력 (게임 데이터 아님)"))
            add("custom", values)
        add_into(custom_percent, _yields(effect.get("percent")))
    for conversion in project_conversion or []:
        values = _yields(conversion.get("yields"))
        if any(values.values()):
            components.append(_component("projects", "project_conversion", conversion.get("project"),
                                         conversion.get("label", "프로젝트 산출 전환"), values,
                                         source="XML Project_YieldConversions"))
            add("projects", values)

    subtotal = zero_yields()
    for bucket in totals_by_category.values():
        add_into(subtotal, bucket)

    # 7. percentage modifiers
    percent = zero_yields()
    percent_components = []
    for record in book.city_records(city_id, "EFFECT_ADJUST_CITY_YIELD_MODIFIER"):
        for key, pct in yield_pairs(record.data):
            percent[key] += pct
            label = f"{record.source_kind} {record.source_id}"
            if "DIFFICULTY" in (record.modifier_id or ""):
                from app.services.difficulty_service import LEVEL_KO

                label = (f"난이도 보너스 ({LEVEL_KO.get(ctx.player.handicap, ctx.player.handicap)}, "
                         f"{'플레이어' if ctx.player.is_human else 'AI'}) · {record.modifier_id}")
            percent_components.append({"yield": key, "percent": pct, "label": label,
                                       "attribution": record.attribution()})
    for key, pct in custom_percent.items():
        if pct:
            percent[key] += pct
            percent_components.append({"yield": key, "percent": pct, "label": "사용자 정의 % 효과"})
    if amenity_state and ctx.settings.get("apply_amenity_effects", True):
        pct = amenity_state.get("non_food_yield_modifier", 0.0)
        if pct:
            for key in NON_FOOD:
                percent[key] += pct
            percent_components.append({"yield": "NonFood", "percent": pct,
                                       "label": f"쾌적도 상태 {amenity_state.get('happiness')}",
                                       "source": "XML Happinesses.NonFoodYieldModifier"})
    # AI extra % (app/config/difficulty.json ai_extra_percent): seen in game,
    # not in the game data.
    from app.services.difficulty_service import ai_extra_percent

    for key, pct in ai_extra_percent(ctx.player).items():
        if pct:
            percent[key] += pct
            percent_components.append({"yield": key, "percent": pct, "label": "AI 추가 % (app/config/difficulty.json)",
                                       "source": "설정값 (게임 데이터 아님)"})
    # The game keeps each % modifier as a 1/256 fixed-point fraction, rounded
    # one by one, and sums those (24 % → 61/256 = 23.83 %).
    effective = zero_yields()
    fixed_point = ctx.settings.get("fixed_point_percent", True)
    for item in percent_components:
        item["effective"] = (math.floor(item["percent"] * 2.56 + 0.5) / 2.56) if fixed_point else item["percent"]
        for key in (list(NON_FOOD) if item["yield"] == "NonFood" else [item["yield"]]):
            effective[key] += item["effective"]
    percent_values = zero_yields()
    for key in YIELD_KEYS:
        percent_values[key] = subtotal[key] * effective[key] / 100.0
    if any(percent_values.values()):
        components.append(_component("percent", "percent_total", None, "% 보정 합계", percent_values,
                                     details=percent_components))
        add("percent", percent_values)
    totals = dict(subtotal)
    add_into(totals, percent_values)

    unassigned = max(0, population - used)
    if unassigned:
        warnings.append(f"배치되지 않은 시민 {unassigned}명 (산출 없음으로 계산)")
    return {
        "city_id": city_id,
        "name": city.get("name", city_id),
        "population": population,
        "totals": clean_yields(totals),
        "subtotal": clean_yields(subtotal),
        "percent": clean_yields(percent),
        "by_category": {k: clean_yields(v) for k, v in totals_by_category.items()},
        "components": components,
        "tiles": tiles_out,
        "districts": districts,
        "buildings": buildings_out,
        "assignment": dict(sorted(accepted.items())),
        "capacity": capacity,
        "citizens_assigned": used,
        "unassigned_citizens": unassigned,
        "warnings": warnings,
    }


def explain_delta(before: dict, after: dict) -> list[dict]:
    """Category-level reasons why yields changed between two city results."""
    reasons = []
    categories = sorted(set(before.get("by_category", {})) | set(after.get("by_category", {})))
    labels = {
        "tiles": "작업 타일", "specialists": "전문가", "population": "인구 기본 산출",
        "districts": "특수지구 인접", "buildings": "건물·불가사의", "trade_routes": "교역로", "great_works": "걸작",
        "build_bonus": "현재 생산 항목 보너스", "modifiers": "정책·특성 등 Modifier",
        "custom": "사용자 정의 효과", "projects": "프로젝트 전환", "percent": "% 보정",
    }
    for category in categories:
        old = before.get("by_category", {}).get(category, {})
        new = after.get("by_category", {}).get(category, {})
        delta = {key: round(new.get(key, 0.0) - old.get(key, 0.0), 4) for key in YIELD_KEYS}
        if any(abs(v) > 1e-9 for v in delta.values()):
            reasons.append({"category": category, "label": labels.get(category, category), "delta": delta})
    old_assign, new_assign = before.get("assignment", {}), after.get("assignment", {})
    added = sorted(set(new_assign) - set(old_assign))
    removed = sorted(set(old_assign) - set(new_assign))
    if added or removed:
        reasons.append({"category": "assignment", "label": "시민 배치 변경", "added_tiles": added, "removed_tiles": removed})
    if before.get("population") != after.get("population"):
        reasons.append({"category": "population_change", "label": "인구 변화",
                        "from": before.get("population"), "to": after.get("population")})
    return reasons


def round_half(value: float) -> float:
    return math.floor(value * 10 + 0.5) / 10
