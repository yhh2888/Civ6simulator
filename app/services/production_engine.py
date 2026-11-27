"""Production options, costs, bonuses and queue processing for the simulator.

Costs (all data-driven, each step reported in ``cost_breakdown``):

* base ``Cost`` from Units / Buildings / Districts / Projects
  (``production.cost_overrides`` in game_rules.json still wins);
* ``COST_PROGRESSION_PREVIOUS_COPIES``: Cost + Param1 × copies already
  produced by the player (Settlers, Builders, ...);
* game speed ``GameSpeeds.CostMultiplier``.

District ``COST_PROGRESSION_NUM_UNDER_AVG_PLUS_TECH`` and
``COST_PROGRESSION_GAME_PROGRESS`` formulas are not contained in the data and
are reported as unsupported (base cost is used).

Production bonuses (+% toward an item) come from modifier effects such as
Agoge, Ilkum, Colonization, Corvée and civilization traits.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any
from uuid import uuid4

from app.services.modifier_engine import EffectBook
from app.services.sim_context import EngineContext, as_bool, as_float, refresh_views, tile_key, yield_key, zero_yields

CATEGORIES = ("unit", "building", "wonder", "district", "project")


def _rows(ctx: EngineContext, category: str) -> dict[str, dict]:
    if category == "unit":
        return ctx.db.index("Units", "UnitType")
    if category in ("building", "wonder"):
        return ctx.db.index("Buildings", "BuildingType")
    if category == "district":
        return ctx.db.index("Districts", "DistrictType")
    if category == "project":
        return ctx.db.index("Projects", "ProjectType")
    return {}


def _catalog_rows(ctx: EngineContext, category: str) -> dict[str, dict]:
    """Fallback to catalog raw tables (test fixtures, or no rules_db)."""
    raw = ctx.catalog.get("raw_tables", {}) if isinstance(ctx.catalog, dict) else {}
    table, key = {
        "unit": ("Units::Units", "UnitType"),
        "building": ("Buildings::Buildings", "BuildingType"),
        "wonder": ("Buildings::Buildings", "BuildingType"),
        "district": ("Districts::Districts", "DistrictType"),
        "project": ("Projects::Projects", "ProjectType"),
    }[category]
    return {row[key]: row for row in raw.get(table, []) if isinstance(row, dict) and isinstance(row.get(key), str)}


def entity_row(ctx: EngineContext, category: str, entity_id: str) -> dict | None:
    row = _rows(ctx, category).get(entity_id)
    if row is None:
        row = _catalog_rows(ctx, category).get(entity_id)
    return row


def is_wonder(ctx: EngineContext, building_id: str) -> bool:
    row = entity_row(ctx, "building", building_id) or {}
    if as_bool(row.get("IsWonder")):
        return True
    raw = ctx.catalog.get("raw_tables", {}) if isinstance(ctx.catalog, dict) else {}
    for item in raw.get("Buildings::Types", []):
        if isinstance(item, dict) and item.get("Type") == building_id:
            return item.get("Kind") == "KIND_WONDER"
    return False


def copies_produced(ctx: EngineContext, unit_id: str) -> int:
    count = int(as_float((ctx.empire.get("unit_copies") or {}).get(unit_id), 0))
    for city in ctx.cities.values():
        for item in city.get("production_history") or []:
            if isinstance(item, dict) and item.get("category") == "unit" and item.get("target_id") == unit_id:
                count += 1
    return count


def entry_cost(ctx: EngineContext, entry: dict) -> tuple[float | None, list[dict]]:
    """Cost of a queue entry. A cost locked in by the game (district placed in a
    Civ VI save) wins over the rules-based estimate."""
    locked = entry.get("locked_cost")
    if isinstance(locked, (int, float)) and not isinstance(locked, bool) and math.isfinite(locked) and locked > 0:
        return float(locked), [{"step": "locked", "value": float(locked),
                                "note": "세이브에 기록된 고정 비용 (게임이 배치 시점에 확정한 값)"}]
    return item_cost(ctx, entry["category"], entry["target_id"])


def item_cost(ctx: EngineContext, category: str, entity_id: str) -> tuple[float | None, list[dict]]:
    row = entity_row(ctx, category, entity_id)
    if row is None:
        return None, [{"step": "data", "note": "데이터에 없는 항목"}]
    overrides = ctx.rules["production"]["cost_overrides"].get(category, {}) or {}
    steps: list[dict] = []
    if entity_id in overrides:
        base = float(overrides[entity_id])
        steps.append({"step": "base", "value": base, "note": "game_rules.json cost_overrides"})
    else:
        base = as_float(row.get("Cost"), 0.0)
        if base <= 0:
            return None, [{"step": "base", "value": base, "note": "유효한 Cost 없음"}]
        steps.append({"step": "base", "value": base, "note": "XML Cost"})
    cost = base
    model = row.get("CostProgressionModel")
    if model and ctx.settings.get("apply_cost_progression", True):
        param = as_float(row.get("CostProgressionParam1"))
        if model == "COST_PROGRESSION_PREVIOUS_COPIES" and category == "unit":
            copies = copies_produced(ctx, entity_id)
            cost = base + param * copies
            steps.append({"step": "progression", "value": cost,
                          "note": f"{model}: {base:g} + {param:g} × 이전 생산 {copies}개"})
        else:
            steps.append({"step": "progression", "value": cost,
                          "note": f"{model}(Param {param:g}) 공식은 데이터에 없어 미지원 — 기본 비용 사용", "unsupported": True})
    multiplier, speed_note = ctx.speed_cost_multiplier()
    if multiplier != 1.0:
        cost = cost * multiplier
    steps.append({"step": "game_speed", "value": cost, "note": speed_note, "multiplier": multiplier})
    return round(cost, 4), steps


def production_bonus(ctx: EngineContext, book: EffectBook, city_id: str, category: str, entity_id: str) -> tuple[float, list[dict]]:
    row = entity_row(ctx, category, entity_id) or {}
    pct = 0.0
    parts: list[dict] = []
    era = ctx.entity_era(row)
    era_index = ctx.era_index(era)
    for record in book.records:
        effect = record.effect
        if record.subject.kind == "city" and record.subject.city_id != city_id:
            continue
        data = record.data
        amount = as_float(data.get("Amount"))
        hit = False
        if effect == "EFFECT_ADJUST_UNIT_TAG_ERA_PRODUCTION" and category == "unit":
            hit = row.get("PromotionClass") == data.get("UnitPromotionClass") and era == data.get("EraType")
        elif effect == "EFFECT_ADJUST_UNIT_PRODUCTION" and category == "unit":
            hit = entity_id == data.get("UnitType")
        elif effect == "EFFECT_ADJUST_DISTRICT_PRODUCTION" and category == "district":
            hit = data.get("DistrictType") in ctx.district_aliases(entity_id)
        elif effect == "EFFECT_ADJUST_ALL_DISTRICTS_PRODUCTION" and category == "district":
            hit = True
        elif effect == "EFFECT_ADJUST_BUILDING_PRODUCTION" and category == "building":
            if data.get("BuildingType"):
                hit = data["BuildingType"] in ctx.building_aliases(entity_id)
            elif data.get("DistrictType"):
                hit = row.get("PrereqDistrict") in ctx.district_aliases(data["DistrictType"]) or row.get("PrereqDistrict") == data["DistrictType"]
        elif effect == "EFFECT_ADJUST_WONDER_ERA_PRODUCTION" and category == "wonder":
            start = ctx.era_index(data.get("StartEra")) if data.get("StartEra") else 0
            end = ctx.era_index(data.get("EndEra")) if data.get("EndEra") else 99
            hit = start <= era_index <= end
        elif effect == "EFFECT_ADJUST_FLAT_BONUS" and category == "wonder":
            hit = data.get("BonusType") == "GOVERNMENTBONUS_WONDER_CONSTRUCTION"
        if hit and amount:
            pct += amount
            parts.append({"label": f"{record.source_kind} {record.source_id}", "percent": amount,
                          "attribution": record.attribution()})
    return pct, parts


FLAT_PRODUCTION_EFFECTS = {
    "EFFECT_ADJUST_CITY_PRODUCTION_BUILDING": ("building", "wonder"),
    "EFFECT_ADJUST_CITY_PRODUCTION_DISTRICT": ("district",),
    "EFFECT_ADJUST_CITY_PRODUCTION_UNIT": ("unit",),
}


def production_flat(ctx: EngineContext, book: EffectBook, city_id: str, category: str) -> tuple[float, list[dict]]:
    """Flat production added only while the city builds a matching item
    (e.g. industrial city-state envoys: +1 toward buildings and districts)."""
    total, parts = 0.0, []
    for effect, categories in FLAT_PRODUCTION_EFFECTS.items():
        if category not in categories:
            continue
        for record in book.for_effect(effect):
            if record.subject.city_id not in (None, city_id):
                continue
            amount = as_float(record.data.get("Amount"))
            if amount:
                total += amount
                parts.append({"label": f"{record.source_kind} {record.source_id}", "amount": amount,
                              "attribution": record.attribution()})
    return total, parts


def specialty_district_limit(ctx: EngineContext, population: int) -> tuple[int | None, str]:
    per = ctx.db.param("DISTRICT_POPULATION_REQUIRED_PER")
    if not per:
        return None, "DISTRICT_POPULATION_REQUIRED_PER 데이터 없음"
    limit = 1 + int((max(1, population) - 1) // per)
    return limit, f"1 + floor((인구 {population} - 1) / {per:g})"


def _unique_blocked(ctx: EngineContext, category: str, entity_id: str, row: dict) -> str | None:
    trait = row.get("TraitType")
    if trait and trait not in ctx.player.traits:
        return f"고유 항목 ({trait} 특성 필요)"
    table, unique_col, replaces_col = {
        "unit": ("UnitReplaces", "CivUniqueUnitType", "ReplacesUnitType"),
        "building": ("BuildingReplaces", "CivUniqueBuildingType", "ReplacesBuildingType"),
        "wonder": ("BuildingReplaces", "CivUniqueBuildingType", "ReplacesBuildingType"),
        "district": ("DistrictReplaces", "CivUniqueDistrictType", "ReplacesDistrictType"),
    }.get(category, (None, None, None))
    if table:
        for rep in ctx.db.rows(table):
            if rep.get(replaces_col) == entity_id:
                unique = rep.get(unique_col)
                unique_row = entity_row(ctx, category, unique) or {}
                if unique_row.get("TraitType") in ctx.player.traits:
                    return f"고유 항목 {unique}이(가) 대체함"
    return None


def availability(ctx: EngineContext, city_id: str, category: str, entity_id: str,
                 x: int | None = None, y: int | None = None, ignore_queue_id: str | None = None) -> list[str]:
    """Reasons an item cannot be produced (empty list = available)."""
    reasons: list[str] = []
    view = ctx.city_views.get(city_id)
    if view is None:
        return ["도시 없음"]
    city = view.city
    row = entity_row(ctx, category, entity_id)
    if row is None:
        return ["데이터에 없는 항목"]
    if category in ("building", "wonder") and is_wonder(ctx, entity_id) != (category == "wonder"):
        return ["분류 불일치 (건물/불가사의)"]
    if as_bool(row.get("InternalOnly")):
        reasons.append("내부 전용 항목")
    tech, civic = row.get("PrereqTech"), row.get("PrereqCivic")
    # empire.enforce_prerequisites = false (e.g. a game imported from a save,
    # whose researched technologies are not decoded) skips tech/civic checks.
    enforce = (ctx.empire or {}).get("enforce_prerequisites", True) is not False
    if enforce and tech and tech not in ctx.player.technologies:
        reasons.append(f"기술 {tech} 필요")
    if enforce and civic and civic not in ctx.player.civics:
        reasons.append(f"사회제도 {civic} 필요")
    blocked = _unique_blocked(ctx, category, entity_id, row)
    if blocked:
        reasons.append(blocked)
    cost, _ = item_cost(ctx, category, entity_id)
    if cost is None:
        reasons.append("유효한 생산 비용 없음")
    queue = [e for e in city.get("production_queue") or [] if e.get("queue_id") != ignore_queue_id]
    population = int(city.get("population", 1))
    if category == "unit":
        if as_bool(row.get("MustPurchase")):
            reasons.append("구매 전용 유닛")
        if str(row.get("CanTrain", "true")).lower() == "false":
            reasons.append("생산 불가 유닛 (CanTrain=false)")
        if as_bool(row.get("EnabledByReligion")) or as_bool(row.get("FoundReligion")):
            reasons.append("종교 시스템 유닛 (미지원)")
        need_pop = int(as_float(row.get("PrereqPopulation"), 0))
        if population < need_pop:
            reasons.append(f"인구 {need_pop} 필요")
        district = row.get("PrereqDistrict")
        if district and district not in view.district_types:
            reasons.append(f"특수지구 {district} 필요")
        for prereq in ctx.db.group("Unit_BuildingPrereqs", "Unit").get(entity_id, []):
            if prereq.get("PrereqBuilding") and prereq["PrereqBuilding"] not in view.building_types:
                reasons.append(f"건물 {prereq['PrereqBuilding']} 필요")
        if row.get("StrategicResource"):
            reasons.append(f"전략 자원 {row['StrategicResource']} 보유량은 시뮬레이션하지 않음 (확인 필요)")
    elif category in ("building", "wonder"):
        district = row.get("PrereqDistrict")
        if district and district not in view.district_types:
            reasons.append(f"특수지구 {district} 필요")
        for prereq in ctx.db.group("BuildingPrereqs", "Building").get(entity_id, []):
            if prereq.get("PrereqBuilding") and prereq["PrereqBuilding"] not in view.building_types:
                reasons.append(f"선행 건물 {prereq['PrereqBuilding']} 필요")
        for pair in ctx.db.rows("MutuallyExclusiveBuildings"):
            if pair.get("Building") == entity_id and pair.get("MutuallyExclusiveBuilding") in view.building_types:
                reasons.append(f"{pair['MutuallyExclusiveBuilding']}와 동시 건설 불가")
        if as_bool(row.get("EnabledByReligion")):
            reasons.append("종교 신앙으로만 해금되는 건물 (미지원)")
        if entity_id in view.building_types or any(e.get("target_id") == entity_id for e in queue):
            reasons.append("이미 건설했거나 대기열에 있음")
        if as_bool(row.get("Capital")) and city_id != ctx.player.capital_id:
            reasons.append("수도 전용")
        max_player = int(as_float(row.get("MaxPlayerInstances"), 0))
        if max_player > 0:
            count = sum(
                1 for other in ctx.city_views.values() if entity_id in other.buildings + other.wonders
            ) + sum(
                1 for other in ctx.cities.values() for e in other.get("production_queue") or []
                if e.get("target_id") == entity_id and e.get("queue_id") != ignore_queue_id
            )
            if count >= max_player:
                reasons.append("플레이어당 건설 한도 도달")
        if category == "wonder":
            if any(entity_id in other.wonders for other in ctx.city_views.values()) or any(
                e.get("target_id") == entity_id and e.get("queue_id") != ignore_queue_id
                for other in ctx.cities.values() for e in other.get("production_queue") or []
            ):
                reasons.append("이미 건설되었거나 다른 도시 대기열에 있음")
            reasons.extend(placement_errors(ctx, city_id, category, entity_id, row, x, y, ignore_queue_id))
    elif category == "district":
        built = sum(1 for t in view.district_tiles if t.get("district") == entity_id)
        queued = sum(1 for e in queue if e.get("category") == "district" and e.get("target_id") == entity_id)
        if built + queued >= 1 and entity_id != "DISTRICT_NEIGHBORHOOD":
            reasons.append("이미 건설했거나 대기열에 있음")
        if as_bool(row.get("RequiresPopulation")):
            limit, formula = specialty_district_limit(ctx, population)
            current = sum(1 for t in view.district_tiles if ctx.is_specialty_district(t.get("district", ""))) + sum(
                1 for e in queue if e.get("category") == "district" and ctx.is_specialty_district(e.get("target_id", ""))
            )
            if limit is not None and current >= limit:
                reasons.append(f"전문 특수지구 한도 {limit}개 ({formula})")
        reasons.extend(placement_errors(ctx, city_id, category, entity_id, row, x, y, ignore_queue_id))
    elif category == "project":
        district = row.get("PrereqDistrict")
        if district and district not in view.district_types:
            reasons.append(f"특수지구 {district} 필요")
        if as_bool(row.get("SpaceRace")) or as_bool(row.get("WMD")) or row.get("PrereqResource"):
            reasons.append("우주 경쟁·WMD 프로젝트의 효과는 미지원")
        max_player = int(as_float(row.get("MaxPlayerInstances"), 0))
        if max_player and entity_id in ctx.player.completed_projects:
            reasons.append("이미 완료한 프로젝트")
        if any(e.get("target_id") == entity_id for e in queue):
            reasons.append("이미 대기열에 있음")
    return reasons


def placement_errors(ctx: EngineContext, city_id: str, category: str, entity_id: str, row: dict,
                     x: int | None, y: int | None, ignore_queue_id: str | None) -> list[str]:
    if x is None or y is None:
        return ["배치할 타일 좌표 필요"]
    view = ctx.city_views[city_id]
    tile = ctx.tile(x, y)
    if tile is None:
        return ["지도에 없는 타일"]
    errors = []
    if (x, y) not in view.tile_keys:
        errors.append("도시 소유 타일이 아님")
    if (x, y) == view.center:
        errors.append("도심에는 배치 불가")
    if tile.get("district"):
        errors.append("이미 특수지구/불가사의가 있는 타일")
    for other in ctx.cities.values():
        for entry in other.get("production_queue") or []:
            if entry.get("queue_id") != ignore_queue_id and entry.get("category") in ("district", "wonder") \
                    and (entry.get("x"), entry.get("y")) == (x, y):
                errors.append("다른 대기열 항목이 이미 예약한 타일")
    terrain = tile.get("terrain", "")
    water = terrain.endswith(("COAST", "OCEAN"))
    table = "District_ValidTerrains" if category == "district" else "Building_ValidTerrains"
    column = "DistrictType" if category == "district" else "BuildingType"
    valid = [r.get("TerrainType") for r in ctx.db.rows(table) if r.get(column) == entity_id]
    if valid and terrain not in valid:
        errors.append(f"허용 지형 아님 ({', '.join(sorted(v for v in valid if v))})")
    elif not valid:
        if "MOUNTAIN" in terrain:
            errors.append("산에는 배치 불가")
        coast_required = as_bool(row.get("Coast")) or as_bool(row.get("MustBeAdjacentLand"))
        if water and not coast_required:
            errors.append("수상 타일에는 배치 불가")
        if coast_required and not water:
            errors.append("해안/수상 타일 필요")
    if category == "district" and as_bool(row.get("NoAdjacentCity")):
        if any((n["x"], n["y"]) == v.center for n in ctx.neighbors(tile) for v in ctx.city_views.values()):
            errors.append("도심과 인접한 타일에는 배치 불가")
    required_features = [
        r.get("FeatureType") for r in ctx.db.rows("District_RequiredFeatures" if category == "district" else "Building_RequiredFeatures")
        if r.get(column) == entity_id
    ]
    if required_features and tile.get("feature") not in required_features:
        errors.append(f"필요 지형 특성 {', '.join(required_features)}")
    if category == "wonder":
        if as_bool(row.get("RequiresRiver")) and not (tile.get("river") or tile.get("river_wires")):
            errors.append("강변 타일 필요")
        if row.get("AdjacentDistrict") and not any(
            row["AdjacentDistrict"] in ctx.district_aliases(n.get("district")) or
            (row["AdjacentDistrict"] == "DISTRICT_CITY_CENTER" and (n["x"], n["y"]) == view.center)
            for n in ctx.neighbors(tile)
        ):
            errors.append(f"인접 특수지구 {row['AdjacentDistrict']} 필요")
        if as_bool(row.get("AdjacentToMountain")) and not any("MOUNTAIN" in n.get("terrain", "") for n in ctx.neighbors(tile)):
            errors.append("인접한 산 필요")
    return errors


def production_options(ctx: EngineContext, book: EffectBook, city_id: str) -> list[dict]:
    options = []
    sources = [
        ("unit", ctx.db.rows("Units") or list(_catalog_rows(ctx, "unit").values()), "UnitType"),
        ("district", ctx.db.rows("Districts") or list(_catalog_rows(ctx, "district").values()), "DistrictType"),
        ("building", ctx.db.rows("Buildings") or list(_catalog_rows(ctx, "building").values()), "BuildingType"),
        ("project", ctx.db.rows("Projects") or list(_catalog_rows(ctx, "project").values()), "ProjectType"),
    ]
    seen = set()
    for category, rows, key in sources:
        for row in rows:
            entity_id = row.get(key)
            if not entity_id:
                continue
            cat = category
            if category == "building" and is_wonder(ctx, entity_id):
                cat = "wonder"
            if (cat, entity_id) in seen:
                continue
            seen.add((cat, entity_id))
            if cat == "district" and entity_id in ("DISTRICT_CITY_CENTER", "DISTRICT_WONDER"):
                continue
            cost, steps = item_cost(ctx, cat, entity_id)
            reasons = availability(ctx, city_id, cat, entity_id)
            placement_only = bool(reasons) and all(r == "배치할 타일 좌표 필요" for r in reasons)
            pct, parts = production_bonus(ctx, book, city_id, cat, entity_id)
            options.append({
                "category": cat, "id": entity_id, "name": row.get("Name", entity_id),
                "cost": cost, "cost_breakdown": steps,
                "available": not reasons or placement_only,
                "requires_placement": cat in ("district", "wonder"),
                "reasons": [r for r in reasons if r != "배치할 타일 좌표 필요"],
                "production_bonus_percent": pct, "production_bonus": parts,
                "era": ctx.entity_era(row),
            })
    options.sort(key=lambda item: (CATEGORIES.index(item["category"]), not item["available"], item["cost"] or 0, item["id"]))
    return options


def new_queue_entry(category: str, entity_id: str, x: int | None = None, y: int | None = None) -> dict:
    return {"queue_id": "prod-" + uuid4().hex[:12], "category": category, "target_id": entity_id, "x": x, "y": y}


def queue_status(ctx: EngineContext, book: EffectBook, city_id: str, production_per_turn: float) -> list[dict]:
    """Queue with costs, bonuses and ETA assuming constant production and carry-over."""
    city = ctx.city_views[city_id].city
    progress = city.get("production_progress") or {}
    policy = ctx.settings.get("overflow_policy", "carry_to_next")
    cumulative = 0
    carry_raw = 0.0
    result = []
    queue = city.get("production_queue") or []
    head_flat = production_flat(ctx, book, city_id, queue[0]["category"])[0] if queue else 0.0
    for index, entry in enumerate(queue):
        cost, steps = entry_cost(ctx, entry)
        pct, parts = production_bonus(ctx, book, city_id, entry["category"], entry["target_id"])
        flat, flat_parts = production_flat(ctx, book, city_id, entry["category"])
        factor = max(0.0, 1 + pct / 100.0)
        # production_per_turn already holds the bonus for the item at the head
        # of the queue (ledger section "build_bonus"); swap it for this item's.
        rate = (production_per_turn - head_flat + flat) * factor
        done = as_float(progress.get(entry["queue_id"]))
        remaining = None if cost is None else max(0.0, cost - done)
        eta = None
        if remaining is not None and rate > 0:
            left = remaining - carry_raw * factor
            turns = max(1, math.ceil(left / rate)) if left > 0 else 1
            cumulative += turns
            eta = cumulative
            overflow = turns * rate - max(0.0, left)
            carry_raw = overflow / factor if (factor and policy != "stop_at_item") else 0.0
        elif remaining is not None:
            carry_raw = 0.0
        reasons = availability(ctx, city_id, entry["category"], entry["target_id"], entry.get("x"), entry.get("y"),
                               ignore_queue_id=entry["queue_id"])
        result.append({
            **entry, "position": index, "cost": cost, "cost_breakdown": steps, "progress": round(done, 4),
            "remaining": None if remaining is None else round(remaining, 4),
            "production_bonus_percent": pct, "production_bonus": parts,
            "production_flat": flat, "production_flat_parts": flat_parts,
            "effective_production": round(rate, 4), "estimated_turns": eta,
            "blocked_reasons": reasons, "progress_percent": (min(100.0, done / cost * 100) if cost else None),
        })
    return result


def project_conversions(ctx: EngineContext, city_id: str, production: float) -> list[dict]:
    city = ctx.city_views[city_id].city
    queue = city.get("production_queue") or []
    if not queue or queue[0].get("category") != "project":
        return []
    project = queue[0]["target_id"]
    result = []
    for row in ctx.db.group("Project_YieldConversions", "ProjectType").get(project, []):
        key = yield_key(row.get("YieldType"))
        pct = as_float(row.get("PercentOfProductionRate"))
        if key:
            values = zero_yields()
            values[key] = production * pct / 100.0
            result.append({"project": project, "yields": values,
                           "label": f"{project}: 생산력 {pct:g}% → {key}"})
    return result


def apply_production(ctx: EngineContext, book: EffectBook, city_id: str, production: float, turn: int,
                     game: dict, map_data: dict) -> dict[str, Any]:
    """Apply one turn of production to the city's queue (mutates ctx documents)."""
    city = ctx.city_views[city_id].city
    city.setdefault("production_queue", [])
    city.setdefault("production_progress", {})
    city.setdefault("production_history", [])
    policy = ctx.settings.get("overflow_policy", "carry_to_next")
    remaining = max(0.0, float(production))
    events: list[dict] = []
    # The head item's build bonus (city-state envoys) is already part of the
    # city's production (yield section "build_bonus").
    completions: list[dict] = []
    applied_total = 0.0
    guard = 0
    while city["production_queue"] and remaining > 1e-9 and guard < 50:
        guard += 1
        entry = city["production_queue"][0]
        cost, _ = entry_cost(ctx, entry)
        blocked = availability(ctx, city_id, entry["category"], entry["target_id"], entry.get("x"), entry.get("y"),
                               ignore_queue_id=entry["queue_id"])
        if cost is None or blocked:
            events.append({"type": "blocked", "queue_id": entry["queue_id"], "target_id": entry["target_id"],
                           "reasons": blocked or ["비용 없음"]})
            break
        pct, _ = production_bonus(ctx, book, city_id, entry["category"], entry["target_id"])
        factor = max(0.0, 1 + pct / 100.0)
        done = as_float(city["production_progress"].get(entry["queue_id"]))
        needed = max(0.0, cost - done)
        if factor <= 0:
            break
        raw_needed = needed / factor
        raw_applied = min(raw_needed, remaining)
        done += raw_applied * factor
        remaining -= raw_applied
        applied_total += raw_applied
        city["production_progress"][entry["queue_id"]] = done
        if done + 1e-9 < cost:
            break
        overflow_raw = remaining
        completion = complete_item(ctx, city_id, entry, game, map_data, turn)
        completions.append(completion)
        refresh_views(ctx)
        city["production_history"].append({"turn": turn, "category": entry["category"], "target_id": entry["target_id"]})
        city["production_queue"].pop(0)
        city["production_progress"].pop(entry["queue_id"], None)
        city["current_production"] = city["production_queue"][0]["queue_id"] if city["production_queue"] else None
        if policy == "stop_at_item":
            remaining = 0.0
            events.append({"type": "overflow_discarded", "amount": round(overflow_raw, 4)})
            break
        if policy == "carry_to_next":
            if city["production_queue"] and overflow_raw > 0:
                nxt = city["production_queue"][0]
                npct, _ = production_bonus(ctx, book, city_id, nxt["category"], nxt["target_id"])
                city["production_progress"][nxt["queue_id"]] = as_float(
                    city["production_progress"].get(nxt["queue_id"])) + overflow_raw * max(0.0, 1 + npct / 100.0)
                events.append({"type": "overflow_carried", "amount": round(overflow_raw, 4), "to": nxt["target_id"]})
            elif overflow_raw > 0:
                city["production_overflow"] = round(as_float(city.get("production_overflow")) + overflow_raw, 4)
                events.append({"type": "overflow_stored", "amount": round(overflow_raw, 4)})
            remaining = 0.0
            break
        # same_turn_queue: keep looping with the remainder.
    if not city["production_queue"] and remaining > 0 and policy != "stop_at_item":
        events.append({"type": "idle", "unused": round(remaining, 4)})
    return {"applied": round(applied_total, 4), "unused": round(remaining, 4), "completed": completions, "events": events}


def complete_item(ctx: EngineContext, city_id: str, entry: dict, game: dict, map_data: dict, turn: int) -> dict:
    city = ctx.city_views[city_id].city
    category, entity_id = entry["category"], entry["target_id"]
    if category == "unit":
        row = entity_row(ctx, "unit", entity_id) or {}
        seed = f"{city_id}|{entity_id}|{turn}|{len(game.get('units') or [])}"
        unit = {
            "id": "unit-" + hashlib.sha1(seed.encode()).hexdigest()[:12], "unit_type": entity_id, "owner_city_id": city_id,
            "x": city["x"], "y": city["y"],
            "moves_remaining": int(as_float(row.get("BaseMoves"), ctx.rules["units"]["default_base_moves"])) or 1,
            "charges_remaining": int(as_float(row.get("BuildCharges"), ctx.rules["units"]["default_build_charges"])),
            "created_turn": turn,
        }
        game.setdefault("units", []).append(unit)
        population_cost = int(as_float(row.get("PopulationCost"), 0))
        if population_cost:
            city["population"] = max(1, int(city.get("population", 1)) - population_cost)
        return {"type": "unit", "id": entity_id, "unit_id": unit["id"], "population_cost": population_cost}
    if category in ("district", "wonder"):
        tile = ctx.tile(entry["x"], entry["y"])
        tile["district"] = entity_id if category == "district" else "DISTRICT_WONDER"
        if category == "district":
            tile["improvement"] = None
        tile["worked"] = False
        if category == "district":
            if entity_id not in city.setdefault("districts", []):
                city["districts"].append(entity_id)
        else:
            city.setdefault("wonders", []).append(entity_id)
        (city.get("specialists") or {}).pop(tile_key(entry["x"], entry["y"]), None)
        return {"type": category, "id": entity_id, "x": entry["x"], "y": entry["y"]}
    if category == "project":
        empire = game.setdefault("empire", {})
        completed = empire.setdefault("completed_projects", [])
        if entity_id not in completed:
            completed.append(entity_id)
        return {"type": "project", "id": entity_id,
                "note": "프로젝트 완료 기록 (위인 점수·완료 효과는 미지원)"}
    city.setdefault("buildings", []).append(entity_id)
    return {"type": "building", "id": entity_id}
