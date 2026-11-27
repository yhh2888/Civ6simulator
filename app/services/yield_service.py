from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.exceptions import ApplicationError

from app.core.dependencies import synchronized_mutation
from app.repositories import locations
from app.repositories.map_repository import map_repository
from app.repositories.city_repository import city_repository
from app.repositories.tile_repository import tile_repository
from app.repositories.game_data_repository import game_data_repository
from app.repositories.json_repository import read, write, write_atomically, write_json_transaction
from app.config import RuleConfigurationError, YIELD_KEYS, load_game_rules, matching_tile_yield_modifiers
from app.entity_rules import ENTITY_SOURCES, entity_availability, entity_rows, missing_research_requirements, research_prerequisites as entity_research_prerequisites
from app.schemas.city_schema import CityCreate, TileOwnership
from app.schemas.snapshot_schema import SaveMapRequest
from app.schemas.game_schema import (BuilderActionRequest, FoundCityRequest, ForceEntityRequest, ForceUnitCreateRequest, ProductionQueueRequest, ProductionReorderRequest, ResearchAvailabilityRequest, ResearchSelectRequest, UnitMoveRequest)

from app.services.game_data_service import game_rules, hex_distance, raw_table
from app.services.adjacency_service import calculate_district_adjacency
def yield_values(values: Any) -> dict[str, float]:
    result = {key: 0.0 for key in YIELD_KEYS}
    if not isinstance(values, dict):
        return result
    for key in YIELD_KEYS:
        try:
            result[key] = float(values.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue
    return result


def add_yield_values(total: dict[str, float], values: dict[str, float]):
    for key in YIELD_KEYS:
        total[key] += values.get(key, 0.0)


def extracted_yield_map(catalog: dict, catalog_key: str, list_key: str) -> dict:
    values = catalog.get(catalog_key)
    if isinstance(values, dict):
        return values
    return {
        item["id"]: item.get("yields", {})
        for item in catalog.get(list_key, [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def raw_yield_map(catalog: dict, table_name: str, item_column: str) -> dict:
    result = {}
    rows = catalog.get("raw_tables", {}).get(table_name, [])
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = row.get(item_column)
        yield_id = row.get("YieldType")
        if not isinstance(item_id, str) or not isinstance(yield_id, str):
            continue
        key = yield_id.removeprefix("YIELD_").title()
        if key not in YIELD_KEYS:
            continue
        try:
            result.setdefault(item_id, {})[key] = (
                result.setdefault(item_id, {}).get(key, 0.0)
                + float(row.get("YieldChange", 0) or 0)
            )
        except (TypeError, ValueError):
            continue
    return result


def calculate_city_yields(
    city: dict, map_data: dict, catalog: dict, rules: dict | None = None
) -> dict:
    rules = rules or game_rules()
    yield_keys = tuple(YIELD_KEYS)
    terrain_yields = extracted_yield_map(catalog, "terrain_yields", "terrains")
    feature_yields = extracted_yield_map(catalog, "feature_yields", "features")
    improvement_yields = extracted_yield_map(
        catalog, "improvement_yields", "improvements"
    )
    resource_yields = raw_yield_map(
        catalog, "Resources::Resource_YieldChanges", "ResourceType"
    )
    district_citizen_yields = raw_yield_map(
        catalog, "Districts::District_CitizenYieldChanges", "DistrictType"
    )
    building_yields = extracted_yield_map(catalog, "building_yields", "buildings")
    resources = {
        item["id"]: item.get("raw", {}).get("ResourceClassType", "UNKNOWN")
        for item in catalog.get("resources", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    tiles = map_data.get("tiles", [])
    city_id = city["id"]
    center = (city["x"], city["y"])
    city_tiles = [
        tile
        for tile in tiles
        if tile.get("owner_city_id") == city_id
        or (tile["x"], tile["y"]) == center
    ]
    other_worked = [
        tile
        for tile in city_tiles
        if (tile["x"], tile["y"]) != center and bool(tile.get("worked"))
    ]
    available_worker_slots = (
        max(0, int(city.get("population", rules["city"]["initial_population"])))
        * rules["city"]["worked_tiles_per_population"]
    )
    active_workers = {
        (tile["x"], tile["y"])
        for tile in other_worked[:available_worker_slots]
    }
    totals = {key: 0.0 for key in yield_keys}
    base_totals = {key: 0.0 for key in yield_keys}
    tile_bonus_totals = {key: 0.0 for key in yield_keys}
    tile_results = []

    for tile in city_tiles:
        terrain_id = tile.get("terrain", "")
        feature_id = tile.get("feature")
        resource_id = tile.get("resource")
        improvement_id = tile.get("improvement")
        district_id = tile.get("district")
        is_center = (tile["x"], tile["y"]) == center
        selected_to_work = bool(tile.get("worked")) and not is_center
        worked = is_center or (
            selected_to_work and (tile["x"], tile["y"]) in active_workers
        )
        base = yield_values(terrain_yields.get(terrain_id))
        if terrain_id not in terrain_yields:
            base_terrain = next(
                (
                    name
                    for name in ("GRASS", "PLAINS", "DESERT", "TUNDRA", "SNOW", "COAST", "OCEAN", "LAKE")
                    if f"TERRAIN_{name}" in terrain_id
                ),
                None,
            )
            fallback = rules["tile_yields"]["fallback_terrain_yields"]
            base = yield_values(fallback.get(base_terrain, {}))
            if "HILLS" in terrain_id:
                base["Production"] += rules["tile_yields"]["hill_production_delta"]
        terrain_is_hills = "HILLS" in terrain_id
        hill_override = tile.get("hill_override")
        is_hills = (
            bool(hill_override)
            if hill_override is not None
            else terrain_is_hills
        )
        if hill_override is not None and bool(hill_override) != terrain_is_hills:
            delta = rules["tile_yields"]["hill_production_delta"]
            base["Production"] += delta if hill_override else -delta
            base["Production"] = max(0.0, base["Production"])
        bonus = {key: 0.0 for key in yield_keys}
        components = []
        for kind, item_id, source_map in (
            ("feature", feature_id, feature_yields),
            ("resource", resource_id, resource_yields),
            ("improvement", improvement_id, improvement_yields),
        ):
            if item_id and item_id in source_map:
                values = yield_values(source_map[item_id])
                add_yield_values(bonus, values)
                components.append({"type": kind, "id": item_id, "yields": values})
        modifier_context = {
            "terrain": terrain_id,
            "feature": feature_id,
            "resource": resource_id,
            "improvement": improvement_id,
            "district": district_id,
            "worked": worked,
            "city_center": is_center,
            "is_hills": is_hills,
            "river_adjacent": bool(tile.get("river") or tile.get("river_wires")),
        }
        if worked and district_id in district_citizen_yields:
            values = yield_values(district_citizen_yields[district_id])
            add_yield_values(bonus, values)
            components.append(
                {"type": "district_citizen", "id": district_id, "yields": values}
            )
        modifier_results = []
        for modifier in matching_tile_yield_modifiers(
            modifier_context, rules["tile_yield_modifiers"]
        ):
            before = {
                key: base[key] + bonus[key]
                for key in yield_keys
            }
            values = yield_values(modifier["yield_changes"])
            add_yield_values(bonus, values)
            after = {
                key: base[key] + bonus[key]
                for key in yield_keys
            }
            detail = {
                "type": "modifier",
                "id": modifier["id"],
                "description": modifier.get("description", ""),
                "before_yields": before,
                "changes": values,
                "after_yields": after,
            }
            modifier_results.append(detail)
            components.append(detail)
        if is_center:
            center_minimums = rules["city"]["city_center_min_yields"]
            for yield_name, minimum in center_minimums.items():
                bonus[yield_name] += max(
                    0.0, minimum - base[yield_name] - bonus[yield_name]
                )
        total = {key: base[key] + bonus[key] for key in yield_keys}
        if worked:
            add_yield_values(totals, total)
            add_yield_values(base_totals, base)
            add_yield_values(tile_bonus_totals, bonus)
        tile_results.append(
            {
                "x": tile["x"],
                "y": tile["y"],
                "worked": worked,
                "selected_to_work": selected_to_work,
                "city_center": is_center,
                "owner_city_id": tile.get("owner_city_id"),
                "terrain": terrain_id,
                "is_hills": is_hills,
                "hill_source": (
                    "manual_override"
                    if hill_override is not None
                    else "terrain_id"
                ),
                "feature": feature_id,
                "resource": resource_id,
                "resource_class": resources.get(resource_id),
                "improvement": improvement_id,
                "district": district_id,
                "river_adjacent": bool(tile.get("river") or tile.get("river_wires")),
                "base_yields": base,
                "bonus_yields": bonus,
                "yields": total,
                "components": components,
                "applied_modifiers": modifier_results,
            }
        )

    population = max(0, int(city.get("population", rules["city"]["initial_population"])))
    population_yields = {
        key: amount * population
        for key, amount in yield_values(rules["city"]["population_yields"]).items()
    }
    add_yield_values(totals, population_yields)

    districts, district_totals = calculate_district_adjacency(
        city_tiles, tiles, catalog
    )
    for district in districts:
        add_yield_values(totals, district["yields"])

    building_results = []
    building_totals = {key: 0.0 for key in YIELD_KEYS}
    building_ids = dict.fromkeys(city.get("buildings", []) + city.get("wonders", []))
    for building_id in building_ids:
        values = building_yields.get(building_id)
        if values is None:
            continue
        yields = yield_values(values)
        add_yield_values(totals, yields)
        add_yield_values(building_totals, yields)
        building_results.append({"id": building_id, "yields": yields})

    return {
        "city": city,
        "totals": totals,
        "breakdown": {
            "worked_terrain": base_totals,
            "worked_tile_bonuses": tile_bonus_totals,
            "population": population_yields,
            "district_adjacency": district_totals,
            "buildings": building_totals,
        },
        "tiles": tile_results,
        "districts": districts,
        "buildings": building_results,
        "worker_limit": available_worker_slots,
        "selected_worked_tile_count": len(other_worked),
        "ignored_worked_tile_count": max(
            0, len(other_worked) - available_worker_slots
        ),
        "coverage": {
            "implemented": [
                "XML terrain, feature, resource and improvement yield changes where extracted",
                "city-center minimum yields configured in game_rules.json",
                "population-based yields configured in game_rules.json",
                "owned and worked tile filtering; city center is always worked and citizen work capacity is configured in game_rules.json",
                "declared tile yield modifiers with per-modifier before/change/after details",
                "selected XML district adjacency rules for neighboring terrain, features, rivers, resources, improvements, wonders and districts",
                "XML district citizen yield changes when that district tile is worked",
                "direct building and wonder yield changes from extracted XML",
            ],
            "not_implemented": [
                "technology, civic, era, game-speed and conditional yield modifiers",
                "non-standard game-speed growth costs and starvation penalties",
                "policy, governor, civilization, leader, religion, city-state and trade-route effects",
                "feature/resource-specific dynamic rules and improvement unlock requirements",
                "district adjacency rules requiring modifier arguments or unsupported conditions",
                "complete DLC coverage and Korean localization",
            ],
        },
        "caveats": [
            "River edges are approximated from the tile river flag or RiverWires; their housing effects are not yields.",
            "Population Science and Culture are configurable flat yields per citizen. Food consumption and Standard-speed growth use configurable settings; starvation penalties are not simulated.",
            "The city center is always worked; citizens can work additional owned tiles according to game_rules.json. If more are selected, the first tiles in map data order are counted and excess selections are reported.",
            "Only yields explicitly present in the extracted XML are used; absent entries are not inferred as zero-valued game rules.",
        ],
    }


def calculate_city(city_id: str):
    cities = city_repository.get_all()
    if city_id not in cities:
        raise ApplicationError(404, "City not found")
    return calculate_city_yields(
        cities[city_id],
        map_repository.get_map({}),
        game_data_repository.catalog(),
    )


def calculate_tile_yields(x: int, y: int):
    map_data = map_repository.get_map({})
    catalog = game_data_repository.catalog()
    cities = city_repository.get_all()
    return _calculate_tile_yields_from_data(x, y, map_data, catalog, cities)


def _calculate_tile_yields_from_data(
    x: int, y: int, map_data: dict, catalog: dict, cities: dict
) -> dict:
    tile = tile_repository.find(map_data, x, y)
    if tile is None:
        raise ApplicationError(404, "Tile not found")

    city = cities.get(tile.get("owner_city_id"))
    if city:
        result = calculate_city_yields(city, map_data, catalog)
        tile_result = next(
            (item for item in result["tiles"] if item["x"] == x and item["y"] == y),
            None,
        )
        if tile_result is not None:
            return {**tile_result, "preview": False}

    preview_id = f"__tile_preview__{x}_{y}"
    preview_map = {
        **map_data,
        "tiles": [
            {
                **item,
                **(
                    {"owner_city_id": preview_id, "worked": True}
                    if item.get("x") == x and item.get("y") == y
                    else {}
                ),
            }
            for item in map_data.get("tiles", [])
        ],
    }
    preview_city = {
        "id": preview_id,
        "name": "Tile yield preview",
        "x": -1,
        "y": -1,
        "population": 1,
        "buildings": [],
        "wonders": [],
    }
    result = calculate_city_yields(preview_city, preview_map, catalog)
    tile_result = next(
        (item for item in result["tiles"] if item["x"] == x and item["y"] == y),
        None,
    )
    if tile_result is None:
        raise ApplicationError(500, "Unable to calculate tile yields")
    return {**tile_result, "preview": True}


def tile_details(x: int, y: int) -> dict:
    map_data = map_repository.get_map({})
    tile = tile_repository.find(map_data, x, y)
    if tile is None:
        raise ApplicationError(404, "Tile not found")

    catalog = game_data_repository.catalog()
    cities = city_repository.get_all()
    yields = _calculate_tile_yields_from_data(x, y, map_data, catalog, cities)
    city = cities.get(tile.get("owner_city_id"))
    is_city_center = bool(
        city and (city.get("x"), city.get("y")) == (x, y)
    )
    worked = bool(yields.get("worked"))
    citizens_working = int(bool(city) and worked and not is_city_center)

    def catalog_item(group: str, item_id: str | None) -> dict | None:
        if not item_id:
            return None
        items = catalog.get(group, [])
        if not isinstance(items, list):
            return None
        return next(
            (
                item
                for item in items
                if isinstance(item, dict) and item.get("id") == item_id
            ),
            None,
        )

    def raw_values(item: dict | None) -> dict:
        raw = item.get("raw", {}) if item else {}
        return raw if isinstance(raw, dict) else {}

    terrain = catalog_item("terrains", tile.get("terrain"))
    feature = catalog_item("features", tile.get("feature"))
    modifiers = []
    for source_name, item_id, item in (
        ("terrain", tile.get("terrain"), terrain),
        ("feature", tile.get("feature"), feature),
    ):
        raw = raw_values(item)
        defense = raw.get("DefenseModifier")
        try:
            defense_value = float(defense)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(defense_value):
            continue
        modifiers.append(
            {"source": source_name, "id": item_id, "value": defense_value}
        )

    wonder_id = None
    if city and tile.get("district") == "DISTRICT_WONDER":
        wonder_tiles = [
            item
            for item in map_data.get("tiles", [])
            if item.get("owner_city_id") == city.get("id")
            and item.get("district") == "DISTRICT_WONDER"
        ]
        wonders = city.get("wonders", [])
        if not isinstance(wonders, list):
            wonders = []
        if len(wonder_tiles) == 1 and len(wonders) == 1:
            wonder_id = wonders[0]

    buildings = city.get("buildings", []) if city else []
    if not isinstance(buildings, list):
        buildings = []
    return {
        "tile": dict(tile),
        "yields": yields,
        "owner_city": (
            {
                "id": city["id"],
                "name": city.get("name", city["id"]),
                "civilization": city.get("civilization"),
                "population": city.get("population"),
                "buildings": list(buildings),
            }
            if city
            else None
        ),
        "is_city_center": is_city_center,
        "citizens_working": citizens_working,
        "workable": bool(
            city and not is_city_center and not tile.get("district")
        ),
        "movement_cost": raw_values(terrain).get("MovementCost"),
        "road_movement_cost": None,
        "rail_movement_cost": None,
        "defense_modifiers": modifiers,
        "appeal": None,
        "continent": tile.get("continent"),
        "wonder_id": wonder_id,
        "district_building_assignment_available": False,
    }


def add_yields(total, values):
    for key, val in (values or {}).items():
        try:
            total[key] = total.get(key, 0) + float(val)
        except (TypeError, ValueError):
            pass
