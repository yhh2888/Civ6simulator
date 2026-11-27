from __future__ import annotations

from app.config import YIELD_KEYS
from app.services.game_data_service import hex_distance


def calculate_district_adjacency(
    city_tiles: list[dict], tiles: list[dict], catalog: dict
) -> tuple[list[dict], dict[str, float]]:
    district_links = catalog.get("raw_tables", {}).get(
        "Districts::District_Adjacencies", []
    )
    adjacency_rules = {
        row.get("ID"): row
        for row in catalog.get("raw_tables", {}).get(
            "Districts::Adjacency_YieldChanges", []
        )
        if isinstance(row, dict) and isinstance(row.get("ID"), str)
    }
    feature_rows = {
        item.get("id"): item
        for item in catalog.get("features", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    natural_wonders = {
        feature_id
        for feature_id, item in feature_rows.items()
        if str(item.get("raw", {}).get("NaturalWonder", "")).lower() == "true"
    }
    supported_conditions = {
        "AdjacentTerrain",
        "AdjacentFeature",
        "AdjacentRiver",
        "AdjacentNaturalWonder",
        "AdjacentWonder",
        "AdjacentResource",
        "AdjacentSeaResource",
        "AdjacentImprovement",
        "AdjacentDistrict",
        "OtherDistrictAdjacent",
    }
    districts = []
    district_totals = {key: 0.0 for key in YIELD_KEYS}
    for district_tile in city_tiles:
        district_id = district_tile.get("district")
        if not district_id:
            continue
        neighbors = [
            other
            for other in tiles
            if hex_distance(
                district_tile["x"], district_tile["y"], other["x"], other["y"]
            )
            == 1
        ]
        district_yields = {key: 0.0 for key in YIELD_KEYS}
        district_components = []
        links = [
            row
            for row in district_links
            if isinstance(row, dict) and row.get("DistrictType") == district_id
        ]
        for link in links:
            rule = adjacency_rules.get(link.get("YieldChangeId"), {})
            conditions = {
                key: value
                for key, value in rule.items()
                if key in supported_conditions
                and value not in (None, "", "false", "0")
            }
            if len(conditions) != 1:
                continue
            condition, expected = next(iter(conditions.items()))
            matching = 0
            for neighbor in neighbors:
                neighbor_terrain = neighbor.get("terrain")
                neighbor_feature = neighbor.get("feature")
                neighbor_resource = neighbor.get("resource")
                if condition == "AdjacentTerrain":
                    matching += neighbor_terrain == expected
                elif condition == "AdjacentFeature":
                    matching += neighbor_feature == expected
                elif condition == "AdjacentRiver":
                    matching += bool(
                        neighbor.get("river") or neighbor.get("river_wires")
                    )
                elif condition in ("AdjacentNaturalWonder", "AdjacentWonder"):
                    matching += (
                        neighbor_feature in natural_wonders
                        or neighbor.get("district") == "DISTRICT_WONDER"
                    )
                elif condition == "AdjacentResource":
                    matching += bool(neighbor_resource)
                elif condition == "AdjacentSeaResource":
                    matching += bool(neighbor_resource) and str(
                        neighbor_terrain
                    ).endswith(("COAST", "OCEAN"))
                elif condition == "AdjacentImprovement":
                    matching += neighbor.get("improvement") == expected
                elif condition == "AdjacentDistrict":
                    matching += neighbor.get("district") == expected
                elif condition == "OtherDistrictAdjacent":
                    matching += bool(neighbor.get("district"))
            try:
                tiles_required = max(1, int(rule.get("TilesRequired", 1)))
                change = float(rule.get("YieldChange", 0) or 0)
            except (TypeError, ValueError):
                continue
            amount = (matching // tiles_required) * change
            yield_key = str(rule.get("YieldType", "")).removeprefix("YIELD_").title()
            if amount and yield_key in YIELD_KEYS:
                district_yields[yield_key] += amount
                district_components.append(
                    {
                        "rule": link.get("YieldChangeId"),
                        "yield": yield_key,
                        "matching_tiles": matching,
                        "amount": amount,
                    }
                )
        for yield_key, amount in district_yields.items():
            district_totals[yield_key] += amount
        districts.append(
            {
                "x": district_tile["x"],
                "y": district_tile["y"],
                "id": district_id,
                "yields": district_yields,
                "components": district_components,
            }
        )
    return districts, district_totals
