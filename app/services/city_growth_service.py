"""Housing, amenities and food growth from Civ VI data.

Values read from rules_db.json (Data.zip):

* GlobalParameters: CITY_FOOD_CONSUMPTION_PER_POPULATION, CITY_GROWTH_THRESHOLD,
  CITY_GROWTH_MULTIPLIER, CITY_GROWTH_EXPONENT, CITY_HOUSING_LEFT_*_GROWTH,
  CITY_POPULATION_RIVER_LAKE / COAST / NO_WATER, CITY_POPULATION_AQUEDUCT_*,
  CITY_POP_PER_AMENITY, CITY_AMENITIES_FOR_FREE
* Buildings/Districts/Improvements Housing and Entertainment columns,
  Improvements.TilesRequired, Resources.Happiness, Features.AddsFreshWater
* Happinesses: amenity score bands, GrowthModifier, NonFoodYieldModifier

Where the data gives a parameter but not the exact formula (e.g. how growth
modifiers combine), the chosen interpretation is reported in ``notes`` and the
item is marked as partially implemented in the coverage registry.
"""

from __future__ import annotations

import math
from typing import Any

from app.services.modifier_engine import EffectBook
from app.services.sim_context import EngineContext, as_bool, as_float

WATER_TERRAINS = ("TERRAIN_COAST", "TERRAIN_OCEAN")


def _param(ctx: EngineContext, name: str, fallback: float) -> tuple[float, str]:
    value = ctx.db.param(name)
    if value is None:
        return fallback, f"{name} 데이터 없음 → 기존 설정값 {fallback}"
    return value, f"GlobalParameters.{name}={ctx.db.global_parameters.get(name)}"


def growth_threshold(ctx: EngineContext, population: int) -> tuple[float | None, str]:
    """Food needed to grow from ``population``; Standard speed only (verified scope)."""
    if ctx.game_speed() != "STANDARD":
        return None, f"{ctx.game_speed()} 속도의 성장 비용 배율은 데이터로 확인되지 않아 미지원"
    base, _ = _param(ctx, "CITY_GROWTH_THRESHOLD", 15.0)
    mult, _ = _param(ctx, "CITY_GROWTH_MULTIPLIER", 8.0)
    exponent, _ = _param(ctx, "CITY_GROWTH_EXPONENT", 1.5)
    n = max(1, int(population))
    value = base + mult * (n - 1) + (n - 1) ** exponent
    return value, f"{base:g} + {mult:g}×(인구-1) + (인구-1)^{exponent:g}"


def food_consumption(ctx: EngineContext, population: int) -> tuple[float, str]:
    if ctx.db.has_param("CITY_FOOD_CONSUMPTION_PER_POPULATION"):
        per, source = _param(ctx, "CITY_FOOD_CONSUMPTION_PER_POPULATION", 2.0)
    else:
        per = float(ctx.rules["food"]["food_per_population"])
        source = "game_rules.json food.food_per_population"
    return per * population, source


# ---------------------------------------------------------------- housing
def fresh_water_status(ctx: EngineContext, city_id: str) -> dict[str, Any]:
    view = ctx.city_views[city_id]
    center = ctx.tile(*view.center)
    if center is None:
        return {"kind": "none", "reason": "도심 타일 없음"}
    fresh_features = {
        row["FeatureType"] for row in ctx.db.rows("Features") if row.get("FeatureType") and as_bool(row.get("AddsFreshWater"))
    }
    if center.get("river") or center.get("river_wires"):
        return {"kind": "fresh", "reason": "도심 타일 강 플래그"}
    neighbors = ctx.neighbors(center)
    for neighbor in neighbors:
        if neighbor.get("lake"):
            return {"kind": "fresh", "reason": f"인접 호수 ({neighbor['x']},{neighbor['y']})"}
        if neighbor.get("feature") in fresh_features:
            return {"kind": "fresh", "reason": f"인접 {neighbor.get('feature')} (Features.AddsFreshWater)"}
    for neighbor in neighbors:
        if neighbor.get("terrain") in WATER_TERRAINS:
            return {"kind": "coast", "reason": f"인접 해안 ({neighbor['x']},{neighbor['y']}) — 지도에 호수 구분이 없어 해안으로 처리"}
    return {"kind": "none", "reason": "인접 담수·해안 없음"}


def compute_housing(ctx: EngineContext, book: EffectBook, city_id: str) -> dict[str, Any]:
    view = ctx.city_views[city_id]
    city = view.city
    components = []
    water = fresh_water_status(ctx, city_id)
    param_name = {"fresh": "CITY_POPULATION_RIVER_LAKE", "coast": "CITY_POPULATION_COAST"}.get(
        water["kind"], "CITY_POPULATION_NO_WATER"
    )
    fallback = {"fresh": 5.0, "coast": 3.0}.get(water["kind"], 2.0)
    water_housing, source = _param(ctx, param_name, fallback)
    has_aqueduct = "DISTRICT_AQUEDUCT" in view.district_types
    if has_aqueduct:
        if water["kind"] == "fresh":
            boost, boost_source = _param(ctx, "CITY_POPULATION_AQUEDUCT_BOOST", 2.0)
            water_housing += boost
            source += f"; 송수로 +{boost:g} ({boost_source})"
        else:
            minimum, minimum_source = _param(ctx, "CITY_POPULATION_AQUEDUCT_MIN", 6.0)
            water_housing = max(water_housing, minimum)
            source += f"; 송수로 최소 {minimum:g} ({minimum_source})"
    components.append({"label": f"물 공급 ({water['reason']})", "amount": water_housing, "source": source})
    buildings = ctx.db.index("Buildings", "BuildingType")
    for building in view.buildings + view.wonders:
        amount = as_float(buildings.get(building, {}).get("Housing"))
        if amount:
            components.append({"label": building, "amount": amount, "source": "XML Buildings.Housing"})
    districts = ctx.db.index("Districts", "DistrictType")
    for tile in view.district_tiles:
        amount = as_float(districts.get(tile.get("district"), {}).get("Housing"))
        if amount:
            components.append({"label": tile["district"], "amount": amount, "source": "XML Districts.Housing"})
    improvements = ctx.db.index("Improvements", "ImprovementType")
    improvement_counts: dict[str, int] = {}
    for tile in view.tiles:
        if tile.get("improvement") and (tile["x"], tile["y"]) != view.center:
            improvement_counts[tile["improvement"]] = improvement_counts.get(tile["improvement"], 0) + 1
    for improvement, count in sorted(improvement_counts.items()):
        row = improvements.get(improvement, {})
        housing = as_float(row.get("Housing"))
        required = max(1.0, as_float(row.get("TilesRequired"), 1.0))
        if housing:
            amount = housing * count / required
            components.append({"label": f"{improvement} × {count}", "amount": amount,
                               "source": f"XML Improvements.Housing {housing:g} / TilesRequired {required:g}"})
    for record in book.city_records(city_id, "EFFECT_ADJUST_POLICY_HOUSING"):
        components.append({"label": f"{record.source_kind} {record.source_id}", "amount": as_float(record.data.get("Amount")),
                           "source": record.modifier_id, "attribution": record.attribution()})
    extra = as_float(city.get("housing_extra"))
    if extra:
        components.append({"label": "사용자 추가 주거공간", "amount": extra, "source": "사용자 입력"})
    computed = sum(item["amount"] for item in components)
    mode = city.get("housing_mode") or "computed"
    value = float(city.get("housing", computed)) if mode == "manual" else computed
    return {"mode": mode, "value": value, "computed": computed, "manual_value": city.get("housing"),
            "components": components, "fresh_water": water}


def housing_growth_multiplier(ctx: EngineContext, housing: float, population: int) -> tuple[float, str]:
    if not ctx.settings.get("apply_housing_growth_penalty", True):
        return 1.0, "주거공간 성장 감소 비활성화"
    left = housing - population
    zero, _ = _param(ctx, "CITY_HOUSING_LEFT_ZERO_GROWTH", -4.0)
    quarter, _ = _param(ctx, "CITY_HOUSING_LEFT_25PCT_GROWTH", 0.0)
    half, _ = _param(ctx, "CITY_HOUSING_LEFT_50PCT_GROWTH", 1.0)
    if left <= zero:
        return 0.0, f"남은 주거공간 {left:g} ≤ {zero:g} → 성장 0%"
    if left <= quarter:
        return 0.25, f"남은 주거공간 {left:g} ≤ {quarter:g} → 성장 25%"
    if left <= half:
        return 0.5, f"남은 주거공간 {left:g} ≤ {half:g} → 성장 50%"
    return 1.0, f"남은 주거공간 {left:g} → 성장 100%"


# --------------------------------------------------------------- amenities
def required_amenities(ctx: EngineContext, population: int) -> tuple[int, str]:
    per, _ = _param(ctx, "CITY_POP_PER_AMENITY", 2.0)
    free, _ = _param(ctx, "CITY_AMENITIES_FOR_FREE", 1.0)
    per = per or 2.0
    value = max(0, int(math.ceil(population / per) - free))
    return value, f"ceil(인구 {population} / {per:g}) - {free:g} (GlobalParameters 해석)"


def luxury_allocation(ctx: EngineContext, base_supply: dict[str, float]) -> tuple[dict[str, float], list[dict]]:
    """Distribute improved luxury resources (Resources.Happiness cities each)."""
    allocation = {city_id: 0.0 for city_id in ctx.city_views}
    details: list[dict] = []
    if ctx.settings.get("luxury_amenities", "auto") != "auto":
        return allocation, details
    valid = ctx.db.group("Improvement_ValidResources", "ResourceType")
    resources = ctx.db.index("Resources", "ResourceType")
    owned: dict[str, int] = {}
    for view in ctx.city_views.values():
        for tile in view.tiles:
            resource = tile.get("resource")
            row = resources.get(resource or "")
            if not row or row.get("ResourceClassType") != "RESOURCECLASS_LUXURY":
                continue
            improvement = tile.get("improvement")
            allowed = {r.get("ImprovementType") for r in valid.get(resource, [])}
            if improvement and (not allowed or improvement in allowed):
                owned[resource] = owned.get(resource, 0) + 1
    need = {}
    for city_id, view in ctx.city_views.items():
        required, _ = required_amenities(ctx, int(view.city.get("population", 1)))
        need[city_id] = required - base_supply.get(city_id, 0.0)
    for resource in sorted(owned):
        per_copy = int(as_float(resources[resource].get("Happiness"), 0))
        recipients = sorted(ctx.city_views, key=lambda cid: (-(need[cid] - allocation[cid]), cid))[: per_copy]
        for city_id in recipients:
            allocation[city_id] += 1
        details.append({"resource": resource, "copies": owned[resource], "cities": recipients,
                        "per_resource": per_copy})
    return allocation, details


def happiness_band(ctx: EngineContext, score: float) -> dict[str, Any]:
    score = math.floor(score)
    rows = ctx.db.rows("Happinesses")
    for row in rows:
        if not row.get("HappinessType"):
            continue
        low = row.get("MinimumAmenityScore")
        high = row.get("MaximumAmenityScore")
        if (low is None or score >= as_float(low)) and (high is None or score <= as_float(high)):
            return {
                "happiness": row["HappinessType"],
                "growth_modifier": as_float(row.get("GrowthModifier")),
                "non_food_yield_modifier": as_float(row.get("NonFoodYieldModifier")),
                "source": "XML Happinesses",
            }
    return {"happiness": None, "growth_modifier": 0.0, "non_food_yield_modifier": 0.0,
            "source": "Happinesses 데이터 없음"}


def compute_amenities(ctx: EngineContext, book: EffectBook) -> dict[str, dict[str, Any]]:
    """Amenity state for every city (luxuries are distributed empire-wide)."""
    buildings = ctx.db.index("Buildings", "BuildingType")
    districts = ctx.db.index("Districts", "DistrictType")
    base: dict[str, list[dict]] = {}
    for city_id, view in ctx.city_views.items():
        items = []
        for building in view.buildings + view.wonders:
            amount = as_float(buildings.get(building, {}).get("Entertainment"))
            if amount:
                items.append({"label": building, "amount": amount, "source": "XML Buildings.Entertainment"})
        for tile in view.district_tiles:
            amount = as_float(districts.get(tile.get("district"), {}).get("Entertainment"))
            if amount:
                items.append({"label": tile["district"], "amount": amount, "source": "XML Districts.Entertainment"})
        for record in book.city_records(city_id, "EFFECT_ADJUST_POLICY_AMENITY"):
            items.append({"label": f"{record.source_kind} {record.source_id}", "amount": as_float(record.data.get("Amount")),
                          "source": record.modifier_id})
        manual = as_float(view.city.get("amenities"))
        if manual:
            items.append({"label": "수동 입력 쾌적도(사치·오락 등 추가분)", "amount": manual, "source": "사용자 입력 city.amenities"})
        base[city_id] = items
    luxury, luxury_details = luxury_allocation(
        ctx, {cid: sum(i["amount"] for i in items) for cid, items in base.items()}
    )
    result = {}
    for city_id, view in ctx.city_views.items():
        items = list(base[city_id])
        if luxury.get(city_id):
            items.append({"label": "사치 자원 분배", "amount": luxury[city_id],
                          "source": "Resources.Happiness 도시 수만큼 분배 (분배 순서는 근사)"})
        supply = sum(item["amount"] for item in items)
        population = int(view.city.get("population", 1))
        required, formula = required_amenities(ctx, population)
        score = supply - required
        band = happiness_band(ctx, score)
        result[city_id] = {"supply": supply, "required": required, "required_formula": formula,
                           "score": score, "components": items, **band}
    result["_luxuries"] = {"details": luxury_details}
    return result


# ------------------------------------------------------------------ growth
def growth_status(
    ctx: EngineContext,
    book: EffectBook,
    city_id: str,
    food_yield: float,
    housing: dict,
    amenity: dict,
) -> dict[str, Any]:
    city = ctx.city_views[city_id].city
    population = int(city.get("population", 1))
    stored = max(0.0, as_float(city.get("stored_food")))
    consumption, consumption_source = food_consumption(ctx, population)
    surplus = food_yield - consumption
    required, required_formula = growth_threshold(ctx, population)
    modifiers = []
    growth_pct = 0.0
    if ctx.settings.get("apply_amenity_effects", True) and amenity.get("growth_modifier"):
        growth_pct += amenity["growth_modifier"]
        modifiers.append({"label": f"쾌적도 {amenity.get('happiness')}", "percent": amenity["growth_modifier"]})
    for record in book.city_records(city_id, "EFFECT_ADJUST_CITY_GROWTH"):
        pct = as_float(record.data.get("Amount"))
        growth_pct += pct
        modifiers.append({"label": f"{record.source_kind} {record.source_id}", "percent": pct,
                          "attribution": record.attribution()})
    housing_mult, housing_reason = housing_growth_multiplier(ctx, housing["value"], population)
    if surplus > 0:
        effective = surplus * max(0.0, 1 + growth_pct / 100.0) * housing_mult
    else:
        effective = surplus
    turns = None
    if required is not None and effective > 0:
        turns = max(1, math.ceil(max(0.0, required - stored) / effective))
    starving_turns = None
    if effective < 0 and stored > 0:
        starving_turns = math.ceil(stored / -effective)
    return {
        "population": population,
        "stored_food": stored,
        "food_yield": round(food_yield, 6),
        "food_consumption": consumption,
        "consumption_source": consumption_source,
        "food_surplus": round(surplus, 6),
        "growth_modifier_percent": growth_pct,
        "growth_modifiers": modifiers,
        "housing": housing["value"],
        "housing_multiplier": housing_mult,
        "housing_reason": housing_reason,
        "effective_food_change": round(effective, 6),
        "food_required": required,
        "required_formula": required_formula,
        "turns_to_growth": turns,
        "turns_to_starvation_empty": starving_turns,
        "growth_progress": (min(100.0, stored / required * 100.0) if required else None),
        "growth_supported": required is not None,
        "notes": [
            "성장 수정치(쾌적도·Modifier %)는 합산 후 잉여 식량에 곱하고, 그 결과에 주거공간 배율을 곱합니다 (공식 결합 방식은 미검증).",
            "식량 부족 시 저장 식량은 0 미만으로 내려가지 않으며, 인구 감소는 game_rules.simulation.starvation_pop_loss로만 적용됩니다.",
        ],
    }
