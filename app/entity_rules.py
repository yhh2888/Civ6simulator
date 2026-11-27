"""Catalog inventory and prerequisite checks for Civilization VI entities."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


ENTITY_SOURCES = {
    "buildings": ("Buildings::Buildings", "BuildingType", "building"),
    "wonders": ("Buildings::Buildings", "BuildingType", "wonder"),
    "units": ("Units::Units", "UnitType", None),
    "policies": ("Policies::Policies", "PolicyType", None),
    "governments": ("Governments::Governments", "GovernmentType", None),
    "improvements": ("Improvements::Improvements", "ImprovementType", None),
    "districts": ("Districts::Districts", "DistrictType", None),
    "projects": ("Projects::Projects", "ProjectType", None),
    "technologies": ("Technologies::Technologies", "TechnologyType", None),
    "civics": ("Civics::Civics", "CivicType", None),
}


def _rows(catalog: dict, table: str) -> list[dict]:
    rows = catalog.get("raw_tables", {}).get(table, [])
    return (
        [row for row in rows if isinstance(row, dict)]
        if isinstance(rows, list)
        else []
    )


def entity_rows(catalog: dict, category: str) -> list[tuple[str, dict]]:
    source = ENTITY_SOURCES[category]
    table, id_key, kind = source
    kinds = {}
    if kind:
        kinds = {
            row.get("Type"): row.get("Kind")
            for row in _rows(catalog, "Buildings::Types")
        }
    result = []
    for row in _rows(catalog, table):
        entity_id = row.get(id_key)
        if not isinstance(entity_id, str):
            continue
        if kind:
            is_wonder = (
                _flag(row.get("IsWonder"))
                if "IsWonder" in row
                else kinds.get(entity_id) == "KIND_WONDER"
            )
            if is_wonder != (category == "wonders"):
                continue
        result.append((entity_id, row))
    return result


def research_prerequisites(
    catalog: dict, category: str, target_id: str
) -> list[str]:
    if category == "technologies":
        table, target_key, prereq_key = (
            "Technologies::TechnologyPrereqs",
            "Technology",
            "PrereqTech",
        )
    elif category == "civics":
        table, target_key, prereq_key = (
            "Civics::CivicPrereqs",
            "Civic",
            "PrereqCivic",
        )
    else:
        raise ValueError(f"Unknown research category: {category}")
    return sorted(
        {
            row[prereq_key]
            for row in _rows(catalog, table)
            if row.get(target_key) == target_id
            and isinstance(row.get(prereq_key), str)
        }
    )


def missing_research_requirements(
    row: dict, technologies: Iterable[str], civics: Iterable[str]
) -> list[str]:
    unlocked_technologies = set(technologies)
    unlocked_civics = set(civics)
    missing = []
    tech = row.get("PrereqTech")
    civic = row.get("PrereqCivic")
    if isinstance(tech, str) and tech and tech not in unlocked_technologies:
        missing.append(f"Requires technology {tech}.")
    if isinstance(civic, str) and civic and civic not in unlocked_civics:
        missing.append(f"Requires civic {civic}.")
    return missing


def _flag(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1")


def _hex_distance(first: dict, second: dict) -> int:
    first_q = first["x"] - (first["y"] - (first["y"] & 1)) // 2
    second_q = second["x"] - (second["y"] - (second["y"] & 1)) // 2
    first_r, second_r = first["y"], second["y"]
    return max(
        abs(first_q - second_q),
        abs(first_r - second_r),
        abs((first_q + first_r) - (second_q + second_r)),
    )


def _districts_for_city(city: dict, map_data: dict) -> set[str]:
    districts = {
        tile.get("district")
        for tile in map_data.get("tiles", [])
        if tile.get("owner_city_id") == city.get("id") and tile.get("district")
    }
    districts.update(
        item for item in city.get("districts", []) if isinstance(item, str)
    )
    districts.add("DISTRICT_CITY_CENTER")
    return districts


def _tile_placement_error(
    catalog: dict,
    category: str,
    entity_id: str,
    row: dict,
    city: dict | None,
    tile: dict,
    map_data: dict,
    cities: dict | None = None,
) -> str | None:
    if city is None:
        return "A city is required to validate this placement."
    if tile.get("owner_city_id") != city.get("id"):
        return "Placement requires a tile owned by the selected city."
    if (tile.get("x"), tile.get("y")) == (city.get("x"), city.get("y")):
        return "This entity cannot replace the city center."

    terrain = tile.get("terrain")
    feature = tile.get("feature")
    resource = tile.get("resource")
    neighbors = [
        neighbor
        for neighbor in map_data.get("tiles", [])
        if _hex_distance(neighbor, tile) == 1
    ]
    adjacent_river = any(
        neighbor.get("river") or neighbor.get("river_wires")
        for neighbor in neighbors
    )
    adjacent_land = any(
        neighbor.get("terrain")
        and not str(neighbor.get("terrain")).endswith(("COAST", "OCEAN", "LAKE"))
        for neighbor in neighbors
    )
    if category == "districts":
        if tile.get("district") or "MOUNTAIN" in str(terrain):
            return "The selected tile is occupied or is not a valid district site."
        if _flag(row.get("Coast")) and not str(terrain).endswith("COAST"):
            return "This district must be placed on a coast tile."
        if _flag(row.get("AdjacentToLand")) and not adjacent_land:
            return "This district requires adjacent land."
        if _flag(row.get("NoAdjacentCity")) and any(
            other.get("id") != city.get("id")
            and _hex_distance(tile, other) <= 1
            for other in (cities or {}).values()
            if isinstance(other, dict)
            and isinstance(other.get("x"), int)
            and isinstance(other.get("y"), int)
        ):
            return "This district cannot be adjacent to another city center."
        if _flag(row.get("Aqueduct")):
            adjacent_to_city = _hex_distance(tile, city) == 1
            fresh_water_nearby = (
                tile.get("river")
                or tile.get("river_wires")
                or "MOUNTAIN" in str(terrain)
                or str(terrain).endswith("LAKE")
                or adjacent_river
                or any(
                    "MOUNTAIN" in str(neighbor.get("terrain"))
                    or str(neighbor.get("terrain")).endswith("LAKE")
                    for neighbor in neighbors
                )
            )
            if not adjacent_to_city or not fresh_water_nearby:
                return "An Aqueduct requires a city-center and fresh-water connection."
        valid_terrains = [
            item.get("TerrainType")
            for item in _rows(catalog, "Districts::District_ValidTerrains")
            if item.get("DistrictType") == entity_id
        ]
        valid_terrains = row.get("_configured_allowed_terrains", valid_terrains)
        if valid_terrains and terrain not in valid_terrains:
            return "This district cannot be placed on the selected terrain."
        required_features = [
            item.get("FeatureType")
            for item in _rows(catalog, "Districts::District_RequiredFeatures")
            if item.get("DistrictType") == entity_id
        ]
        if required_features and feature not in required_features:
            return "This district requires a specific feature on the selected tile."
    elif category == "wonders":
        if tile.get("district"):
            return "A wonder requires an unoccupied tile."
        valid_terrains = [
            item.get("TerrainType")
            for item in _rows(catalog, "Buildings::Building_ValidTerrains")
            if item.get("BuildingType") == entity_id
        ]
        if valid_terrains and terrain not in valid_terrains:
            return "This wonder cannot be placed on the selected terrain."
        valid_features = [
            item.get("FeatureType")
            for item in _rows(catalog, "Buildings::Building_ValidFeatures")
            if item.get("BuildingType") == entity_id
        ]
        required_features = [
            item.get("FeatureType")
            for item in _rows(catalog, "Buildings::Building_RequiredFeatures")
            if item.get("BuildingType") == entity_id
        ]
        if required_features and feature not in required_features:
            return "This wonder requires a specific feature on the selected tile."
        if valid_features and feature not in valid_features:
            return "This wonder cannot be placed on the selected feature."
        adjacent_district = row.get("AdjacentDistrict")
        if adjacent_district and not any(
            neighbor.get("district") == adjacent_district
            for neighbor in neighbors
        ):
            return f"This wonder requires an adjacent {adjacent_district}."
        adjacent_resource = row.get("AdjacentResource")
        if adjacent_resource and not any(
            neighbor.get("resource") == adjacent_resource
            for neighbor in neighbors
        ):
            return f"This wonder requires adjacent {adjacent_resource}."
        if _flag(row.get("AdjacentToMountain")) and not any(
            "MOUNTAIN" in str(neighbor.get("terrain"))
            for neighbor in neighbors
        ):
            return "This wonder requires an adjacent mountain."
        if _flag(row.get("MustBeAdjacentLand")) and not adjacent_land:
            return "This wonder requires adjacent land."
        if _flag(row.get("MustNotBeLake")) and str(terrain).endswith("LAKE"):
            return "This wonder cannot be placed on a lake."
        if _flag(row.get("RequiresAdjacentRiver")) and not adjacent_river:
            return "This wonder requires an adjacent river."
        if _flag(row.get("RequiresRiver")) and not (
            tile.get("river") or tile.get("river_wires") or adjacent_river
        ):
            return "This wonder requires a river."
        if _flag(row.get("RequiresReligion")) and not city.get("religion"):
            return "This wonder requires a religion in the city."
    elif category == "improvements":
        if not _flag(row.get("Buildable")):
            return "This improvement is not marked buildable in the extracted game data."
        valid_terrains = [
            item.get("TerrainType")
            for item in _rows(catalog, "Improvements::Improvement_ValidTerrains")
            if item.get("ImprovementType") == entity_id
        ]
        valid_terrains = row.get("_configured_allowed_terrains", valid_terrains)
        valid_features = [
            item.get("FeatureType")
            for item in _rows(catalog, "Improvements::Improvement_ValidFeatures")
            if item.get("ImprovementType") == entity_id
        ]
        valid_features = row.get("_configured_allowed_features", valid_features)
        resource_rules = [
            item
            for item in _rows(catalog, "Improvements::Improvement_ValidResources")
            if item.get("ImprovementType") == entity_id
        ]
        configured_resources = row.get("_configured_allowed_resources")
        if configured_resources is not None:
            resource_rules = [
                {"ResourceType": allowed_resource}
                for allowed_resource in configured_resources
                if resource == allowed_resource
            ]
        else:
            resource_rules = [
                rule for rule in resource_rules if rule.get("ResourceType") == resource
            ]
        feature_valid = feature in valid_features
        if not (
            terrain in valid_terrains
            or feature_valid
            or resource_rules
        ):
            return (
                "The selected tile does not meet this improvement's terrain, "
                "feature, or resource conditions."
            )
        if any(
            _flag(rule.get("MustRemoveFeature")) for rule in resource_rules
        ) and feature:
            return "Remove the required feature before building this improvement."
        if feature and not feature_valid and not resource_rules:
            return "Remove the incompatible feature before building this improvement."
        if tile.get("district"):
            return "An improvement cannot be built on a district tile."
        if _flag(row.get("Coast")) and not any(
            str(neighbor.get("terrain", "")).endswith("COAST")
            for neighbor in neighbors
        ):
            return "This improvement requires an adjacent coast tile."
        if _flag(row.get("RequiresAdjacentBonusOrLuxury")):
            resource_classes = {
                item.get("ResourceType"): item.get("ResourceClassType")
                for item in _rows(catalog, "Resources::Resources")
            }
            if not any(
                str(resource_classes.get(neighbor.get("resource"), "")).upper()
                in ("RESOURCECLASS_BONUS", "RESOURCECLASS_LUXURY")
                for neighbor in neighbors
            ):
                return "This improvement requires an adjacent bonus or luxury resource."
    return None


def entity_availability(
    catalog: dict,
    category: str,
    entity_id: str,
    row: dict,
    state: dict,
    city: dict | None = None,
    tile: dict | None = None,
    map_data: dict | None = None,
    cities: dict | None = None,
    rules: dict | None = None,
) -> list[str]:
    technologies = state.get("technologies", [])
    civics = state.get("civics", [])
    reasons = missing_research_requirements(row, technologies, civics)

    if category in ("technologies", "civics"):
        completed_key = "technologies" if category == "technologies" else "civics"
        if entity_id in state.get(completed_key, []):
            reasons.append("This research is already completed.")
        else:
            missing = [
                prerequisite
                for prerequisite in research_prerequisites(catalog, category, entity_id)
                if prerequisite not in state.get(completed_key, [])
            ]
            if missing:
                reasons.append("Requires prerequisite research: " + ", ".join(missing) + ".")

    if city is not None:
        if category == "units":
            try:
                required_population = int(row.get("PrereqPopulation", 0) or 0)
            except (TypeError, ValueError):
                reasons.append("Invalid population prerequisite data.")
            else:
                if city.get("population", 1) < required_population:
                    reasons.append(f"Requires city population {required_population}.")
            required_district = row.get("PrereqDistrict")
            if required_district and required_district not in _districts_for_city(
                city, map_data or {}
            ):
                reasons.append(f"Requires completed district {required_district}.")
        if category in ("buildings", "wonders", "projects"):
            districts = _districts_for_city(city, map_data or {})
            required_district = row.get("PrereqDistrict")
            if required_district and required_district not in districts:
                reasons.append(f"Requires completed district {required_district}.")
            if category in ("buildings", "wonders"):
                owned = set(city.get("buildings", [])) | set(city.get("wonders", []))
                if _flag(row.get("InternalOnly")):
                    reasons.append("This entity is internal-only in the extracted game data.")
                if entity_id in owned:
                    reasons.append("This entity is already present in this city.")
                required_buildings = []
                required_buildings.extend(
                    prerequisite.get("PrereqBuilding")
                    for prerequisite in _rows(catalog, "Buildings::BuildingPrereqs")
                    if prerequisite.get("Building") == entity_id
                )
                if rules:
                    required_buildings.extend(
                        rules.get("buildings", {})
                        .get("overrides", {})
                        .get(entity_id, {})
                        .get("required_buildings", [])
                    )
                for prerequisite in required_buildings:
                    if prerequisite and prerequisite not in owned:
                        reason = f"Requires building {prerequisite}."
                        if reason not in reasons:
                            reasons.append(reason)
                for exclusion in _rows(
                    catalog, "Buildings::MutuallyExclusiveBuildings"
                ):
                    if (
                        exclusion.get("Building") == entity_id
                        and exclusion.get("MutuallyExclusiveBuilding") in owned
                    ):
                        reasons.append(
                            "Mutually exclusive building already present: "
                            f"{exclusion['MutuallyExclusiveBuilding']}."
                        )
                try:
                    player_limit = int(row.get("MaxPlayerInstances", 0) or 0)
                    world_limit = int(row.get("MaxWorldInstances", 0) or 0)
                except (TypeError, ValueError):
                    reasons.append("Invalid entity instance-limit data.")
                else:
                    all_cities = (cities or {}).values()
                    player_count = sum(
                        other.get("buildings", []).count(entity_id)
                        + other.get("wonders", []).count(entity_id)
                        for other in all_cities
                        if isinstance(other, dict)
                    )
                    if player_limit and player_count >= player_limit:
                        reasons.append("The player-wide instance limit has been reached.")
                    if category == "wonders":
                        world_count = sum(
                            other.get("wonders", []).count(entity_id)
                            for other in (cities or {}).values()
                            if isinstance(other, dict)
                        )
                        if world_limit and world_count >= world_limit:
                            reasons.append("The world wonder instance limit has been reached.")
            if category == "buildings" and _flag(row.get("RequiresAdjacentRiver")):
                center_tile = next(
                    (
                        item
                        for item in (map_data or {}).get("tiles", [])
                        if item.get("x") == city.get("x")
                        and item.get("y") == city.get("y")
                    ),
                    None,
                )
                river_adjacent = center_tile and (
                    center_tile.get("river") or center_tile.get("river_wires")
                    or any(
                        _hex_distance(center_tile, neighbor) == 1
                        and (neighbor.get("river") or neighbor.get("river_wires"))
                        for neighbor in (map_data or {}).get("tiles", [])
                    )
                )
                if not river_adjacent:
                    reasons.append("This building requires the city center to be river-adjacent.")
        if category == "districts":
            if _flag(row.get("CityCenter")) or _flag(row.get("InternalOnly")):
                reasons.append("This district cannot be placed as a normal city district.")
            if rules:
                override = rules.get("districts", {}).get("overrides", {}).get(
                    entity_id, {}
                )
                max_per_city = override.get(
                    "max_per_city",
                    rules.get("districts", {}).get("default_max_per_city", 1),
                )
                built_count = sum(
                    tile.get("owner_city_id") == city.get("id")
                    and tile.get("district") == entity_id
                    for tile in (map_data or {}).get("tiles", [])
                )
                built_count = max(
                    built_count,
                    city.get("districts", []).count(entity_id),
                )
                if built_count >= max_per_city:
                    reasons.append("This district has reached its per-city limit.")
        if category == "improvements" and tile is not None:
            if tile.get("improvement") == entity_id:
                reasons.append("This improvement is already present on the selected tile.")

    if tile is not None and category in ("districts", "wonders", "improvements"):
        reason = _tile_placement_error(
            catalog,
            category,
            entity_id,
            row,
            city,
            tile,
            map_data or {},
            cities,
        )
        if reason:
            reasons.append(reason)
    return list(dict.fromkeys(reasons))
