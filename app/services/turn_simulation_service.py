"""Turn simulation engine.

The engine works on a *state bundle* ``{"map", "cities", "game"}`` and never
touches the active documents directly: :func:`simulate_turns` deep-copies the
bundle, runs the phases below and returns the new bundle plus a report. The
caller decides whether to commit (main game) or store (scenario).

Turn phases (engine-defined order; Civ VI's internal order is not in the data,
so the order is documented and reported with every run):

 1. 상태 확인 — validate the bundle, build indexes
 2. 시민 배분 — re-optimize cities in ``auto`` mode
 3. 산출량 계산 — modifiers, amenities, housing, city yields (this turn's yields)
 4. 식량 — stored food += effective food change (growth modifiers, housing)
 5. 인구 성장 — growth when stored ≥ threshold (one per turn; remainder kept)
 6. 생산 — production applied to the queue with % bonuses and overflow policy
 7. 생산 완료 — buildings/districts/wonders/units/projects take effect
 8. 연구 — empire Science → technology, Culture → civic (game-speed costs)
 9. 국고 — Gold and Faith accumulate (maintenance is reported, see coverage)
10. 시민 갱신 — new citizens placed (or left pending), capacity trimmed
11. 다음 턴 — turn + 1, unit moves reset
12. 재계산 — next-turn yields recomputed and compared with this turn
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from app.config import YIELD_KEYS
from app.core.exceptions import ApplicationError
from app.repositories.rules_db_repository import RulesDB
from app.services import production_engine
from app.services.citizen_assignment_service import (
    city_mode,
    new_citizen_policy,
    optimize_city,
    place_new_citizens,
    trim_to_capacity,
    write_assignment,
)
from app.services.city_growth_service import (
    compute_amenities,
    compute_housing,
    growth_status,
    growth_threshold,
)
from app.services.modifier_engine import EffectBook, run_modifiers
from app.services.sim_context import (
    EngineContext,
    add_into,
    as_float,
    build_context,
    clean_yields,
    copy_bundle,
    refresh_views,
    zero_yields,
)
from app.services.yield_breakdown_service import compute_city, explain_delta

PHASES = [
    {"id": 1, "name": "상태 확인"},
    {"id": 2, "name": "시민 배분 (자동 모드 도시 재최적화)"},
    {"id": 3, "name": "타일·도시 산출량 계산 (Modifier, 쾌적도, 주거공간 포함)"},
    {"id": 4, "name": "식량 소비 및 저장 식량 갱신"},
    {"id": 5, "name": "인구 성장"},
    {"id": 6, "name": "생산력 적용 및 대기열 처리"},
    {"id": 7, "name": "생산 완료 처리"},
    {"id": 8, "name": "기술·사회제도 연구"},
    {"id": 9, "name": "금·신앙 누적"},
    {"id": 10, "name": "새 시민 배치 / 시민 수 조정"},
    {"id": 11, "name": "턴 증가 및 유닛 이동력 회복"},
    {"id": 12, "name": "다음 턴 산출량 재계산 및 리포트"},
]

ORDER_NOTE = (
    "처리 순서는 이 시뮬레이터가 정의한 순서입니다. Civ VI 내부의 정확한 처리 순서는 "
    "Data.zip에 포함되어 있지 않아 검증되지 않았습니다. 모든 단계는 턴 시작 시점에 계산한 "
    "산출량을 사용하므로, 이번 턴에 완성된 건물·성장한 인구는 다음 턴부터 반영됩니다."
)


class Statics:
    def __init__(self, catalog: dict, db: RulesDB, rules: dict):
        self.catalog = catalog
        self.db = db
        self.rules = rules

    def db_for(self, game: dict | None) -> RulesDB:
        """Rules database for a game's ruleset (games imported from saves can
        use the expansion rules); the default database otherwise."""
        ruleset = game.get("ruleset") if isinstance(game, dict) else None
        if not ruleset:
            return self.db
        from app.repositories.rules_db_repository import load_rules_db

        return load_rules_db(ruleset)


# ---------------------------------------------------------------- research
def research_cost(ctx: EngineContext, category: str, target_id: str) -> tuple[float | None, str]:
    table, key = ("Technologies", "TechnologyType") if category == "technology" else ("Civics", "CivicType")
    row = ctx.db.index(table, key).get(target_id)
    if row is None:
        raw = ctx.catalog.get("raw_tables", {}).get(f"{table}::{table}", []) if isinstance(ctx.catalog, dict) else []
        row = next((r for r in raw if isinstance(r, dict) and r.get(key) == target_id), None)
    base = as_float((row or {}).get("Cost"), 0.0)
    if base <= 0:
        return None, "유효한 Cost 없음"
    multiplier, note = ctx.speed_cost_multiplier()
    return base * multiplier, f"XML Cost {base:g} × {multiplier:g} ({note})"


def _boost_percent(ctx: EngineContext, category: str, target_id: str) -> float:
    column = "TechnologyType" if category == "technology" else "CivicType"
    for row in ctx.db.rows("Boosts"):
        if row.get(column) == target_id:
            return as_float(row.get("Boost"), 50.0)
    return 0.0


def research_summary(ctx: EngineContext, science: float, culture: float) -> dict[str, Any]:
    result = {}
    for category, rate in (("technology", science), ("civic", culture)):
        track = (ctx.game.get("research") or {}).get(category) or {}
        target = track.get("current_id")
        cost, note = research_cost(ctx, category, target) if target else (None, None)
        progress = as_float((track.get("progress") or {}).get(target)) if target else 0.0
        turns = math.ceil(max(0.0, cost - progress) / rate) if cost and rate > 0 else None
        result[category] = {"current_id": target, "cost": cost, "cost_note": note, "progress": progress,
                            "yield_per_turn": round(rate, 4), "turns_remaining": turns,
                            "overflow": as_float(track.get("overflow"))}
    return result


# -------------------------------------------------------------- evaluation
def evaluate(ctx: EngineContext, book: EffectBook | None = None, *, include_details: bool = True) -> dict[str, Any]:
    book = book or run_modifiers(ctx)
    amenities = compute_amenities(ctx, book)
    cities: dict[str, Any] = {}
    empire = zero_yields()
    for city_id in sorted(ctx.city_views):
        city = ctx.city_views[city_id].city
        housing = compute_housing(ctx, book, city_id)
        result = compute_city(ctx, book, city_id, amenity_state=amenities[city_id])
        conversions = production_engine.project_conversions(ctx, city_id, result["totals"]["Production"])
        if conversions:
            result = compute_city(ctx, book, city_id, amenity_state=amenities[city_id], project_conversion=conversions)
        growth = growth_status(ctx, book, city_id, result["totals"]["Food"], housing, amenities[city_id])
        queue = production_engine.queue_status(ctx, book, city_id, result["totals"]["Production"])
        add_into(empire, result["totals"])
        cities[city_id] = {
            "id": city_id, "name": city.get("name", city_id), "x": city["x"], "y": city["y"],
            "population": int(city.get("population", 1)),
            "citizen_mode": city_mode(ctx, city), "strategy": city.get("strategy") or ctx.settings.get("default_strategy"),
            "new_citizen_policy": new_citizen_policy(ctx, city),
            "yields": result, "growth": growth, "housing": housing, "amenities": amenities[city_id],
            "production": {"per_turn": result["totals"]["Production"], "queue": queue,
                           "history": (city.get("production_history") or [])[-10:]},
        }
        if not include_details:
            cities[city_id]["yields"] = {k: result[k] for k in ("totals", "by_category", "assignment", "warnings",
                                                                "population", "capacity", "citizens_assigned",
                                                                "unassigned_citizens")}
    maintenance = building_maintenance(ctx)
    return {
        "turn": ctx.game.get("turn", 1),
        "game_speed": ctx.game_speed(),
        "cities": cities,
        "empire": {
            "yields": clean_yields(empire),
            "maintenance": maintenance,
            "net_gold": round(empire["Gold"] - maintenance["total"], 4),
            "research": research_summary(ctx, empire["Science"], empire["Culture"]),
            "treasury": dict(ctx.empire.get("treasury") or {}),
            "government": ctx.player.government, "policies": ctx.player.policies,
            "civilization": ctx.player.civilization, "leader": ctx.player.leader,
            "traits": sorted(ctx.player.traits),
            "luxuries": amenities.get("_luxuries"),
        },
        "modifiers": book.summary(),
    }


def building_maintenance(ctx: EngineContext) -> dict[str, Any]:
    buildings = ctx.db.index("Buildings", "BuildingType")
    districts = ctx.db.index("Districts", "DistrictType")
    units = ctx.db.index("Units", "UnitType")
    items = []
    for view in ctx.city_views.values():
        for building in view.buildings:
            amount = as_float(buildings.get(building, {}).get("Maintenance"))
            if amount:
                items.append({"label": f"{view.city.get('name')} {building}", "amount": amount})
        for tile in view.district_tiles:
            amount = as_float(districts.get(tile.get("district"), {}).get("Maintenance"))
            if amount:
                items.append({"label": f"{view.city.get('name')} {tile['district']}", "amount": amount})
    for unit in ctx.game.get("units") or []:
        amount = as_float(units.get(unit.get("unit_type"), {}).get("Maintenance"))
        if amount:
            items.append({"label": f"유닛 {unit.get('unit_type')}", "amount": amount})
    return {"total": round(sum(i["amount"] for i in items), 4), "items": items,
            "note": "XML Maintenance 합계. 정책/정부의 유지비 감면은 미지원."}


def city_snapshot(city_eval: dict) -> dict[str, Any]:
    growth, production = city_eval["growth"], city_eval["production"]
    head = production["queue"][0] if production["queue"] else None
    return {
        "population": city_eval["population"],
        "stored_food": round(growth["stored_food"], 4),
        "food_required": growth["food_required"],
        "food_yield": growth["food_yield"],
        "food_consumption": growth["food_consumption"],
        "food_surplus": growth["food_surplus"],
        "effective_food_change": growth["effective_food_change"],
        "turns_to_growth": growth["turns_to_growth"],
        "housing": city_eval["housing"]["value"],
        "amenity_score": city_eval["amenities"]["score"],
        "happiness": city_eval["amenities"]["happiness"],
        "yields": city_eval["yields"]["totals"],
        "assignment": city_eval["yields"]["assignment"],
        "unassigned_citizens": city_eval["yields"]["unassigned_citizens"],
        "production_item": head["target_id"] if head else None,
        "production_category": head["category"] if head else None,
        "production_progress": head["progress"] if head else None,
        "production_cost": head["cost"] if head else None,
        "production_eta": head["estimated_turns"] if head else None,
    }


# ------------------------------------------------------------- simulation
def _context(bundle: dict, statics: Statics) -> EngineContext:
    for key in ("map", "cities", "game"):
        if not isinstance(bundle.get(key), dict):
            raise ApplicationError(422, f"시뮬레이션 상태에 {key} 문서가 없습니다.")
    return build_context(bundle["map"], bundle["cities"], bundle["game"], statics.catalog, statics.db_for(bundle["game"]), statics.rules)


def simulate_one_turn(bundle: dict, statics: Statics) -> tuple[dict, dict]:
    """Run one turn on a deep copy. Returns (new_bundle, report)."""
    for key in ("map", "cities", "game"):
        if not isinstance(bundle.get(key), dict):
            raise ApplicationError(422, f"시뮬레이션 상태에 {key} 문서가 없습니다.")
    state = copy_bundle(bundle)
    ctx = _context(state, statics)
    turn = int(ctx.game.get("turn", 1))
    events: list[dict] = []
    phase_log: list[dict] = []

    # Phase 2: auto assignment
    book = run_modifiers(ctx)
    amenities = compute_amenities(ctx, book)
    for city_id in sorted(ctx.city_views):
        city = ctx.city_views[city_id].city
        if city_mode(ctx, city) == "auto":
            plan = optimize_city(ctx, book, city_id, amenities[city_id])
            write_assignment(ctx, city_id, plan["assignment"])
            events.append({"phase": 2, "city_id": city_id, "type": "auto_assignment",
                           "strategy": plan["strategy"]["id"], "assignment": plan["assignment"]})
    refresh_views(ctx)
    phase_log.append({"phase": 2, "note": "자동 모드 도시 재배치 완료"})

    # Phase 3: yields of this turn
    book = run_modifiers(ctx)
    before = evaluate(ctx, book)
    empire_yields = before["empire"]["yields"]

    # Phase 4-5: food and growth
    grown: dict[str, int] = {}
    for city_id in sorted(ctx.city_views):
        city = ctx.city_views[city_id].city
        growth = before["cities"][city_id]["growth"]
        stored = max(0.0, growth["stored_food"] + growth["effective_food_change"])
        population = int(city.get("population", 1))
        required = growth["food_required"]
        growths = 0
        limit = int(statics.rules["food"]["max_growths_per_turn"])
        max_pop = int(statics.rules["city"]["maximum_population"])
        while required is not None and growths < limit and population < max_pop and stored >= required - 1e-9:
            stored -= required
            population += 1
            growths += 1
            required, _ = growth_threshold(ctx, population)
        if growth["effective_food_change"] < 0 and growth["stored_food"] + growth["effective_food_change"] < 0:
            if ctx.settings.get("starvation_pop_loss") and population > 1:
                population -= 1
                events.append({"phase": 5, "city_id": city_id, "type": "starvation", "population": population})
            else:
                events.append({"phase": 4, "city_id": city_id, "type": "food_shortage",
                               "note": "저장 식량 0 (인구 감소는 설정으로만 적용)"})
        city["stored_food"] = round(stored, 6)
        if growths:
            city["population"] = population
            grown[city_id] = growths
            events.append({"phase": 5, "city_id": city_id, "type": "growth", "population": population,
                           "stored_food_after": round(stored, 4)})

    # Phase 6-7: production (uses this turn's production yield)
    production_reports = {}
    for city_id in sorted(ctx.city_views):
        production = before["cities"][city_id]["yields"]["totals"]["Production"]
        report = production_engine.apply_production(ctx, book, city_id, production, turn, ctx.game, ctx.map)
        production_reports[city_id] = report
        for completion in report["completed"]:
            events.append({"phase": 7, "city_id": city_id, "type": "completed", "item_type": completion["type"],
                           **{k: v for k, v in completion.items() if k != "type"}})
        for event in report["events"]:
            events.append({"phase": 6, "city_id": city_id, **event})
    refresh_views(ctx)

    # Phase 8: research
    research_events = []
    research = ctx.game.setdefault("research", {})
    for category, earned in (("technology", empire_yields["Science"]), ("civic", empire_yields["Culture"])):
        track = research.setdefault(category, {"current_id": None, "progress": {}})
        track.setdefault("progress", {})
        target = track.get("current_id")
        pool = earned + as_float(track.pop("overflow", 0.0))
        if not target:
            if pool > 0:
                research_events.append({"category": category, "type": "idle", "discarded": round(pool, 4),
                                        "note": "선택된 연구가 없어 이번 턴 산출이 사용되지 않음"})
            continue
        cost, _ = research_cost(ctx, category, target)
        if cost is None:
            research_events.append({"category": category, "id": target, "type": "blocked", "reason": "비용 없음"})
            continue
        boosted = (ctx.empire.get("boosted") or {}).get(category) or []
        progress = as_float(track["progress"].get(target))
        applied_boost = track.setdefault("boost_applied", [])
        if target in boosted and target not in applied_boost:
            pct = _boost_percent(ctx, category, target)
            progress += cost * pct / 100.0
            applied_boost.append(target)
            research_events.append({"category": category, "id": target, "type": "boost", "percent": pct})
        progress += pool
        if progress >= cost - 1e-9:
            overflow = progress - cost
            track["progress"][target] = cost
            completed_key = "technologies" if category == "technology" else "civics"
            ctx.game.setdefault(completed_key, [])
            if target not in ctx.game[completed_key]:
                ctx.game[completed_key].append(target)
            track["current_id"] = None
            if ctx.settings.get("research_overflow") == "carry" and overflow > 0:
                track["overflow"] = round(overflow, 6)
            research_events.append({"category": category, "id": target, "type": "completed",
                                    "overflow": round(overflow, 4)})
        else:
            track["progress"][target] = round(progress, 6)
    for item in research_events:
        events.append({"phase": 8, **item})

    # Phase 9: treasury
    empire = ctx.game.setdefault("empire", {})
    treasury = empire.setdefault("treasury", {"Gold": 0.0, "Faith": 0.0})
    treasury["Gold"] = round(as_float(treasury.get("Gold")) + before["empire"]["net_gold"], 4)
    treasury["Faith"] = round(as_float(treasury.get("Faith")) + empire_yields["Faith"], 4)

    # Phase 10: citizens after growth / population loss
    refresh_views(ctx)
    book_after = run_modifiers(ctx)
    amenities_after = compute_amenities(ctx, book_after)
    for city_id in sorted(ctx.city_views):
        city = ctx.city_views[city_id].city
        removed = trim_to_capacity(ctx, book_after, city_id)
        if removed:
            events.append({"phase": 10, "city_id": city_id, "type": "citizens_removed", "tiles": removed})
        if grown.get(city_id):
            if city_mode(ctx, city) == "auto":
                plan = optimize_city(ctx, book_after, city_id, amenities_after[city_id])
                write_assignment(ctx, city_id, plan["assignment"])
                events.append({"phase": 10, "city_id": city_id, "type": "new_citizen_auto_optimized",
                               "assignment": plan["assignment"]})
            elif new_citizen_policy(ctx, city) == "auto":
                placed = place_new_citizens(ctx, book_after, city_id, grown[city_id], amenities_after[city_id])
                events.append({"phase": 10, "city_id": city_id, "type": "new_citizen_placed", "tiles": placed})
            else:
                events.append({"phase": 10, "city_id": city_id, "type": "new_citizen_pending",
                               "count": grown[city_id], "note": "수동 배치 대기"})

    # Phase 11: advance turn
    ctx.game["turn"] = turn + 1
    unit_rows = ctx.db.index("Units", "UnitType")
    for unit in ctx.game.get("units") or []:
        row = unit_rows.get(unit.get("unit_type"), {})
        unit["moves_remaining"] = int(as_float(row.get("BaseMoves"), statics.rules["units"]["default_base_moves"])) or 1

    # Phase 12: projection for the next turn (auto cities re-optimized first)
    refresh_views(ctx)
    projected = copy_bundle({"map": ctx.map, "cities": ctx.cities, "game": ctx.game})
    projection_ctx = build_context(projected["map"], projected["cities"], projected["game"],
                                   statics.catalog, ctx.db, statics.rules)
    projection_book = run_modifiers(projection_ctx)
    projection_amenities = compute_amenities(projection_ctx, projection_book)
    for city_id in sorted(projection_ctx.city_views):
        if city_mode(projection_ctx, projection_ctx.city_views[city_id].city) == "auto":
            plan = optimize_city(projection_ctx, projection_book, city_id, projection_amenities[city_id])
            write_assignment(projection_ctx, city_id, plan["assignment"])
    refresh_views(projection_ctx)
    after = evaluate(projection_ctx)

    report = build_report(before, after, events, production_reports, turn)
    new_bundle = {"map": ctx.map, "cities": ctx.cities, "game": ctx.game}
    return new_bundle, report


def build_report(before: dict, after: dict, events: list[dict], production_reports: dict, turn: int) -> dict:
    cities = {}
    for city_id, city_before in before["cities"].items():
        city_after = after["cities"].get(city_id)
        snap_before = city_snapshot(city_before)
        snap_after = city_snapshot(city_after) if city_after else None
        deltas = {}
        if snap_after:
            for key in ("population", "stored_food", "food_yield", "food_surplus", "housing", "amenity_score"):
                a, b = snap_after.get(key), snap_before.get(key)
                if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                    deltas[key] = round(a - b, 4)
            deltas["yields"] = {k: round(snap_after["yields"][k] - snap_before["yields"][k], 4) for k in YIELD_KEYS}
        cities[city_id] = {
            "name": city_before["name"],
            "current": snap_before,
            "next": snap_after,
            "delta": deltas,
            "reasons": explain_delta(city_before["yields"], city_after["yields"]) if city_after else [],
            "events": [e for e in events if e.get("city_id") == city_id],
            "production": production_reports.get(city_id),
        }
    return {
        "turn_processed": turn,
        "next_turn": turn + 1,
        "phases": PHASES,
        "order_note": ORDER_NOTE,
        "cities": cities,
        "empire": {"current": before["empire"]["yields"], "next": after["empire"]["yields"],
                   "research_before": before["empire"]["research"], "research_after": after["empire"]["research"],
                   "treasury": after["empire"]["treasury"], "net_gold": before["empire"]["net_gold"]},
        "events": events,
        "modifiers": after["modifiers"],
    }


def history_entry(report: dict) -> dict:
    return {
        "turn": report["turn_processed"],
        "cities": {
            cid: {
                "name": data["name"],
                "population": data["current"]["population"],
                "stored_food": data["current"]["stored_food"],
                "food_required": data["current"]["food_required"],
                "yields": data["current"]["yields"],
                "assignment": data["current"]["assignment"],
                "production_item": data["current"]["production_item"],
                "production_progress": data["current"]["production_progress"],
                "production_cost": data["current"]["production_cost"],
                "turns_to_growth": data["current"]["turns_to_growth"],
                "completed": [e.get("id") for e in data["events"] if e.get("type") == "completed"],
                "units": [e.get("id") for e in data["events"] if e.get("type") == "completed" and e.get("item_type") == "unit"],
                "buildings": [e.get("id") for e in data["events"] if e.get("type") == "completed"
                              and e.get("item_type") in ("building", "wonder", "district")],
                "grew": any(e.get("type") == "growth" for e in data["events"]),
                "population_after": data["next"]["population"] if data["next"] else None,
            }
            for cid, data in report["cities"].items()
        },
        "empire": {
            "yields": report["empire"]["current"],
            "technology": report["empire"]["research_before"]["technology"],
            "civic": report["empire"]["research_before"]["civic"],
            "completed_research": [
                {"category": e["category"], "id": e["id"]} for e in report["events"]
                if e.get("phase") == 8 and e.get("type") == "completed"
            ],
            "treasury": report["empire"]["treasury"],
        },
    }


def simulate_turns(bundle: dict, statics: Statics, turns: int = 1) -> tuple[dict, list[dict], list[dict]]:
    limit = int(statics.rules.get("simulation", {}).get("max_simulation_turns", 100) if isinstance(statics.rules.get("simulation"), dict) else 100)
    if turns < 1 or turns > limit:
        raise ApplicationError(422, f"턴 수는 1 ~ {limit} 범위여야 합니다.")
    reports = []
    history = []
    current = bundle
    for _ in range(turns):
        current, report = simulate_one_turn(current, statics)
        reports.append(report)
        history.append(history_entry(report))
    return current, reports, history
