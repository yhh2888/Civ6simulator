"""API orchestration for the simulation engine.

Every operation works on a *target*: the active game (``scenario_id=None``)
or a stored scenario. Reads copy the target state; writes go through a single
commit function:

* main game  → ``write_json_transaction`` on the in-memory active documents
  (same persistence model as the existing editor: disk only on 저장);
* scenario   → atomic rewrite of the scenario file.

The engine itself (turn_simulation_service and friends) is pure and works on
copies, so a failing step never leaves a half-applied state.
"""

from __future__ import annotations

from typing import Any, Callable

from app.config import YIELD_KEYS
from app.core.dependencies import synchronized_mutation
from app.core.exceptions import ApplicationError
from app.repositories import locations
from app.repositories.city_repository import city_repository
from app.repositories.game_data_repository import game_data_repository
from app.repositories.json_repository import write_json_transaction
from app.repositories.map_repository import map_repository
from app.repositories.rules_db_repository import load_rules_db
from app.services import production_engine, scenario_service
from app.services import simulation_validation_service as validation
from app.services.citizen_assignment_service import (
    apply_city_settings,
    optimize_city,
    set_tile_citizens,
    set_tile_preference,
    write_assignment,
)
from app.services.city_growth_service import compute_amenities
from app.services.city_optimization_service import PRESETS, preset_list, score_totals, strategy_for_city
from app.services.empire_service import empire_options, validate_empire_update
from app.services.game_data_service import game_rules, game_state
from app.services.modifier_engine import run_modifiers
from app.services.trade_route_service import destinations as trade_destinations
from app.services.sim_context import EngineContext, build_context, copy_bundle, refresh_views, simulation_settings
from app.services.turn_simulation_service import (
    ORDER_NOTE,
    PHASES,
    Statics,
    evaluate,
    simulate_turns,
)
from app.services.yield_breakdown_service import compute_city, explain_delta

MAIN_HISTORY_LIMIT = 60


def statics() -> Statics:
    return Statics(game_data_repository.catalog(), load_rules_db(), game_rules())


def main_bundle() -> dict:
    return {"map": map_repository.get_map({}), "cities": city_repository.get_all(), "game": game_state()}


def load_bundle(scenario_id: str | None) -> dict:
    return scenario_service.state_of(scenario_id) if scenario_id else main_bundle()


def commit_bundle(scenario_id: str | None, bundle: dict, history: list[dict] | None = None) -> None:
    if scenario_id:
        scenario_service.store_state(scenario_id, bundle, history)
        return
    if history:
        bundle["game"]["sim_history"] = ((bundle["game"].get("sim_history") or []) + history)[-MAIN_HISTORY_LIMIT:]
    write_json_transaction({
        locations.map_file(): bundle["map"],
        locations.cities_file(): bundle["cities"],
        locations.game_state_file(): bundle["game"],
    })


def context_for(bundle: dict, st: Statics | None = None) -> EngineContext:
    st = st or statics()
    return build_context(bundle["map"], bundle["cities"], bundle["game"], st.catalog, st.db_for(bundle["game"]), st.rules)


def _require_city(ctx: EngineContext, city_id: str) -> None:
    if city_id not in ctx.city_views:
        raise ApplicationError(404, "City not found.")


def _mutate(scenario_id: str | None, mutation: Callable[[EngineContext], Any]) -> tuple[Any, dict]:
    st = statics()
    bundle = copy_bundle(load_bundle(scenario_id))
    ctx = context_for(bundle, st)
    result = mutation(ctx)
    commit_bundle(scenario_id, {"map": ctx.map, "cities": ctx.cities, "game": ctx.game})
    return result, {"map": ctx.map, "cities": ctx.cities, "game": ctx.game}


def _compact_map_changes(before: dict, after: dict) -> list[dict]:
    index = {(t["x"], t["y"]): t for t in before.get("tiles", [])}
    changes = []
    for tile in after.get("tiles", []):
        old = index.get((tile["x"], tile["y"]))
        if old is None or any(old.get(k) != tile.get(k) for k in ("worked", "district", "improvement", "owner_city_id")):
            changes.append({k: tile.get(k) for k in ("x", "y", "worked", "district", "improvement", "owner_city_id")})
    return changes


# ------------------------------------------------------------------- state
def get_state(scenario_id: str | None = None, details: bool = False) -> dict:
    st = statics()
    ctx = context_for(load_bundle(scenario_id), st)
    result = evaluate(ctx, include_details=details)
    result["scenario_id"] = scenario_id
    result["phases"] = PHASES
    result["order_note"] = ORDER_NOTE
    result["presets"] = preset_list()
    result["settings"] = ctx.settings
    return result


def get_city(city_id: str, scenario_id: str | None = None) -> dict:
    st = statics()
    bundle = load_bundle(scenario_id)
    ctx = context_for(bundle, st)
    _require_city(ctx, city_id)
    book = run_modifiers(ctx)
    state = evaluate(ctx, book)
    city_eval = state["cities"][city_id]
    preview_bundle, reports, _ = simulate_turns(bundle, st, 1)
    report = reports[0]["cities"].get(city_id)
    return {
        "scenario_id": scenario_id,
        "turn": state["turn"],
        "city": city_eval,
        "next_turn": report,
        "phases": PHASES,
        "order_note": ORDER_NOTE,
        "modifier_audit": {
            "summary": book.summary(),
            "applied": [
                {**r.attribution(), "subject": r.subject.label(ctx), "data": {k: v for k, v in r.data.items() if not k.startswith("_")}}
                for r in book.records if r.subject.city_id in (city_id, None)
            ],
            "inactive": [i for i in book.inactive if city_eval["name"] in i.get("subject", "") or i.get("subject") == "플레이어"],
        },
        "empire": state["empire"],
        "coverage": validation.coverage(),
        "trade_destinations": trade_destinations(ctx, city_id),
        "governor_options": _governor_list(ctx.db),
        "governor_establish_turns": int(ctx.db.param("GOVERNOR_BASE_TURNS_TO_ESTABLISH", 5) or 5),
        "great_work_options": [
            {"id": r["GreatWorkType"], "type": r.get("GreatWorkObjectType")}
            for r in ctx.db.rows("GreatWorks") if r.get("GreatWorkType") and ctx.db.group(
                "GreatWork_YieldChanges", "GreatWorkType").get(r["GreatWorkType"])],
    }


def _governor_list(db) -> list[dict]:
    from app.services.empire_service import _governor_options

    options = _governor_options(db)
    return options.get("list", []) if options.get("available") else []


def city_ledger(city_id: str, scenario_id: str | None = None) -> dict:
    from app.services.yield_ledger_service import build_ledger

    ctx = context_for(load_bundle(scenario_id))
    _require_city(ctx, city_id)
    ledger = build_ledger(ctx, city_id)
    ledger["scenario_id"] = scenario_id
    return ledger


def get_difficulties(scenario_id: str | None = None) -> dict:
    from app.services import difficulty_service

    st = statics()
    bundle = load_bundle(scenario_id)
    return difficulty_service.overview(st.db_for(bundle["game"]), bundle["game"], bundle["map"])


def update_difficulties(payload: dict, scenario_id: str | None = None) -> dict:
    from app.services import difficulty_service

    st = statics()
    bundle = copy_bundle(load_bundle(scenario_id))
    db = st.db_for(bundle["game"])
    before = evaluate(context_for(copy_bundle(bundle), st), include_details=False)
    if "bonus_overrides" in payload:
        difficulty_service.save_overrides(db, payload["bonus_overrides"])
    bundle["game"]["difficulties"] = difficulty_service.clean(
        db, {k: v for k, v in payload.items() if k != "bonus_overrides"}, bundle["game"].get("difficulties"))
    after = evaluate(context_for(bundle, st), include_details=False)
    commit_bundle(scenario_id, bundle)
    impact = []
    for city_id, city_after in after["cities"].items():
        old = before["cities"].get(city_id)
        if old:
            impact.append({"city_id": city_id, "name": city_after["name"],
                           "delta": {k: round(city_after["yields"]["totals"][k] - old["yields"]["totals"][k], 4)
                                     for k in YIELD_KEYS}})
    result = difficulty_service.overview(db, bundle["game"], bundle["map"])
    result["impact"] = impact
    return result


def empire_ledger(yield_key: str, scenario_id: str | None = None) -> dict:
    from app.services.yield_ledger_service import empire_yield_ledger

    ctx = context_for(load_bundle(scenario_id))
    result = empire_yield_ledger(ctx, yield_key)
    result["scenario_id"] = scenario_id
    return result


def city_ledger_csv(city_id: str, scenario_id: str | None = None) -> tuple[str, str]:
    from app.services.yield_ledger_service import ledger_csv

    ledger = city_ledger(city_id, scenario_id)
    return ledger_csv(ledger), f"ledger_{city_id}_turn{ledger['turn']}.csv"


def modifier_audit(scenario_id: str | None = None) -> dict:
    ctx = context_for(load_bundle(scenario_id))
    book = run_modifiers(ctx)
    return {
        "summary": book.summary(),
        "applied": [{**r.attribution(), "subject": r.subject.label(ctx),
                     "data": {k: v for k, v in r.data.items() if not k.startswith("_")}} for r in book.records],
        "skipped": book.skipped,
        "inactive": book.inactive,
    }


# -------------------------------------------------------------------- turns
def preview_turns(turns: int = 1, scenario_id: str | None = None) -> dict:
    st = statics()
    bundle = load_bundle(scenario_id)
    final, reports, history = simulate_turns(bundle, st, turns)
    return {"scenario_id": scenario_id, "committed": False, "turns": turns,
            "reports": reports if turns <= 3 else [reports[0], reports[-1]],
            "history": history, "final_turn": final["game"]["turn"]}


@synchronized_mutation
def run_turns(turns: int = 1, scenario_id: str | None = None, city_id: str | None = None) -> dict:
    st = statics()
    bundle = load_bundle(scenario_id)
    final, reports, history = simulate_turns(bundle, st, turns)
    commit_bundle(scenario_id, final, history)
    ctx = context_for(final, st)
    response = {
        "scenario_id": scenario_id,
        "committed": True,
        "turns": turns,
        "turn": final["game"]["turn"],
        "reports": reports if turns <= 3 else [reports[0], reports[-1]],
        "history": history,
        "cities": list(final["cities"].values()),
        "game_state": {k: v for k, v in final["game"].items() if k != "sim_history"},
        "map_changes": _compact_map_changes(bundle["map"], final["map"]),
        "state": evaluate(ctx, include_details=False),
    }
    if city_id and city_id in final["cities"]:
        response["city"] = get_city(city_id, scenario_id)
    return response


def history(scenario_id: str | None = None) -> dict:
    if scenario_id:
        return {"scenario_id": scenario_id, "history": scenario_service.history_of(scenario_id)}
    return {"scenario_id": None, "history": list(game_state().get("sim_history") or [])}


# ------------------------------------------------------------------ citizens
def _city_result(ctx: EngineContext, city_id: str) -> dict:
    book = run_modifiers(ctx)
    amenities = compute_amenities(ctx, book)
    return compute_city(ctx, book, city_id, amenity_state=amenities[city_id])


@synchronized_mutation
def update_city_settings(city_id: str, payload: dict, scenario_id: str | None = None) -> dict:
    def mutation(ctx: EngineContext):
        _require_city(ctx, city_id)
        before = _city_result(ctx, city_id)
        apply_city_settings(ctx, city_id, payload)
        if ctx.cities[city_id].get("citizen_mode") == "auto" and (
            "strategy" in payload or "citizen_mode" in payload or "custom_strategy" in payload
        ):
            book = run_modifiers(ctx)
            amenities = compute_amenities(ctx, book)
            plan = optimize_city(ctx, book, city_id, amenities[city_id])
            write_assignment(ctx, city_id, plan["assignment"])
            refresh_views(ctx)
        after = _city_result(ctx, city_id)
        return {"before": before["totals"], "after": after["totals"], "reasons": explain_delta(before, after),
                "city": ctx.cities[city_id]}

    result, bundle = _mutate(scenario_id, mutation)
    result["map_tiles"] = _city_tiles(bundle, city_id)
    return result


def _city_tiles(bundle: dict, city_id: str) -> list[dict]:
    return [
        {k: t.get(k) for k in ("x", "y", "worked", "district", "improvement", "owner_city_id")}
        for t in bundle["map"].get("tiles", []) if t.get("owner_city_id") == city_id
    ]


@synchronized_mutation
def assign_tile(city_id: str, x: int, y: int, citizens: int, scenario_id: str | None = None) -> dict:
    def mutation(ctx: EngineContext):
        _require_city(ctx, city_id)
        before = _city_result(ctx, city_id)
        assignment = set_tile_citizens(ctx, city_id, x, y, citizens)
        after = _city_result(ctx, city_id)
        return {"assignment": assignment, "before": before["totals"], "after": after["totals"],
                "reasons": explain_delta(before, after), "warnings": after["warnings"]}

    result, bundle = _mutate(scenario_id, mutation)
    result["map_tiles"] = _city_tiles(bundle, city_id)
    return result


@synchronized_mutation
def tile_preference(city_id: str, x: int, y: int, payload: dict, scenario_id: str | None = None) -> dict:
    def mutation(ctx: EngineContext):
        _require_city(ctx, city_id)
        return {"preference": set_tile_preference(ctx, city_id, x, y, payload),
                "tile_preferences": ctx.cities[city_id].get("tile_preferences") or {}}

    result, bundle = _mutate(scenario_id, mutation)
    result["map_tiles"] = _city_tiles(bundle, city_id)
    return result


def optimize(city_id: str, strategy: str | None = None, apply: bool = False, compare_presets: bool = True,
             custom_strategy: dict | None = None, scenario_id: str | None = None) -> dict:
    if strategy and strategy not in PRESETS:
        raise ApplicationError(422, "알 수 없는 운영 전략입니다.")

    def plan_for(ctx: EngineContext):
        _require_city(ctx, city_id)
        book = run_modifiers(ctx)
        amenities = compute_amenities(ctx, book)
        current = compute_city(ctx, book, city_id, amenity_state=amenities[city_id])
        plan = optimize_city(ctx, book, city_id, amenities[city_id], strategy, custom_strategy)
        optimized = compute_city(ctx, book, city_id, plan["assignment"], amenity_state=amenities[city_id])
        comparison = []
        if compare_presets:
            for preset_id in PRESETS:
                if preset_id == "custom" and not custom_strategy and not ctx.cities[city_id].get("custom_strategy"):
                    continue
                alt = optimize_city(ctx, book, city_id, amenities[city_id], preset_id, custom_strategy)
                alt_result = compute_city(ctx, book, city_id, alt["assignment"], amenity_state=amenities[city_id])
                comparison.append({"strategy": preset_id, "label": PRESETS[preset_id]["label"],
                                   "totals": alt_result["totals"], "assignment": alt["assignment"],
                                   "difference_vs_selected": {k: round(alt_result["totals"][k] - optimized["totals"][k], 4)
                                                              for k in YIELD_KEYS}})
        weights = plan["strategy"]["weights"]
        return {
            "plan": plan,
            "current_totals": current["totals"],
            "optimized_totals": optimized["totals"],
            "difference": {k: round(optimized["totals"][k] - current["totals"][k], 4) for k in YIELD_KEYS},
            "score_current": round(score_totals(current["totals"], weights), 4),
            "score_optimized": round(score_totals(optimized["totals"], weights), 4),
            "reasons": explain_delta(current, optimized),
            "presets": comparison,
            "optimizer_note": "결정론적 가중치 최적화입니다. Civ VI 도시 관리 AI의 가중치는 데이터에 없어 재현하지 않습니다.",
        }

    if not apply:
        ctx = context_for(copy_bundle(load_bundle(scenario_id)))
        return {**plan_for(ctx), "applied": False}

    @synchronized_mutation
    def apply_plan():
        def mutation(ctx: EngineContext):
            result = plan_for(ctx)
            write_assignment(ctx, city_id, result["plan"]["assignment"])
            if strategy:
                ctx.cities[city_id]["strategy"] = strategy
            return result

        result, bundle = _mutate(scenario_id, mutation)
        result["map_tiles"] = _city_tiles(bundle, city_id)
        return {**result, "applied": True}

    return apply_plan()


# -------------------------------------------------------------- production
def production_options(city_id: str, scenario_id: str | None = None) -> dict:
    ctx = context_for(load_bundle(scenario_id))
    _require_city(ctx, city_id)
    book = run_modifiers(ctx)
    result = compute_city(ctx, book, city_id, amenity_state=compute_amenities(ctx, book)[city_id])
    return {"city_id": city_id, "production_per_turn": result["totals"]["Production"],
            "options": production_engine.production_options(ctx, book, city_id),
            "cost_notes": [
                "비용 = XML Cost (+ 이전 생산 수 증가) × 게임 속도 배율. 특수지구 비용 증가(기술 진행·평균 이하 할인) 공식은 미지원.",
                "생산 % 보너스는 정책·특성 Modifier에서 계산되며 해당 항목에 투입되는 생산력에 곱해집니다.",
            ]}


def _queue_payload(ctx: EngineContext, city_id: str) -> dict:
    book = run_modifiers(ctx)
    result = compute_city(ctx, book, city_id, amenity_state=compute_amenities(ctx, book)[city_id])
    return {"production_per_turn": result["totals"]["Production"],
            "queue": production_engine.queue_status(ctx, book, city_id, result["totals"]["Production"]),
            "overflow_policy": ctx.settings.get("overflow_policy")}


@synchronized_mutation
def queue_add(city_id: str, payload: dict, scenario_id: str | None = None) -> dict:
    category, target = payload.get("category"), payload.get("target_id")
    if category not in production_engine.CATEGORIES or not target:
        raise ApplicationError(422, "category와 target_id가 필요합니다.")

    def mutation(ctx: EngineContext):
        _require_city(ctx, city_id)
        reasons = production_engine.availability(ctx, city_id, category, target, payload.get("x"), payload.get("y"))
        if reasons:
            raise ApplicationError(409, "; ".join(reasons))
        city = ctx.cities[city_id]
        entry = production_engine.new_queue_entry(category, target, payload.get("x"), payload.get("y"))
        city.setdefault("production_queue", []).append(entry)
        city.setdefault("production_progress", {})[entry["queue_id"]] = 0.0
        if city.get("production_overflow"):
            city["production_progress"][entry["queue_id"]] = float(city.pop("production_overflow"))
        city["current_production"] = city["production_queue"][0]["queue_id"]
        refresh_views(ctx)
        return _queue_payload(ctx, city_id)

    return _mutate(scenario_id, mutation)[0]


@synchronized_mutation
def queue_reorder(city_id: str, queue_id: str, position: int, scenario_id: str | None = None) -> dict:
    def mutation(ctx: EngineContext):
        _require_city(ctx, city_id)
        queue = ctx.cities[city_id].setdefault("production_queue", [])
        index = next((i for i, e in enumerate(queue) if e.get("queue_id") == queue_id), None)
        if index is None:
            raise ApplicationError(404, "Production queue item not found.")
        if not 0 <= position < len(queue):
            raise ApplicationError(422, "대기열 범위를 벗어난 위치입니다.")
        queue.insert(position, queue.pop(index))
        ctx.cities[city_id]["current_production"] = queue[0]["queue_id"]
        return _queue_payload(ctx, city_id)

    return _mutate(scenario_id, mutation)[0]


@synchronized_mutation
def queue_remove(city_id: str, queue_id: str, scenario_id: str | None = None) -> dict:
    def mutation(ctx: EngineContext):
        _require_city(ctx, city_id)
        city = ctx.cities[city_id]
        queue = city.setdefault("production_queue", [])
        if not any(e.get("queue_id") == queue_id for e in queue):
            raise ApplicationError(404, "Production queue item not found.")
        city["production_queue"] = [e for e in queue if e.get("queue_id") != queue_id]
        (city.get("production_progress") or {}).pop(queue_id, None)
        city["current_production"] = city["production_queue"][0]["queue_id"] if city["production_queue"] else None
        refresh_views(ctx)
        return _queue_payload(ctx, city_id)

    return _mutate(scenario_id, mutation)[0]


# ------------------------------------------------------------------ empire
def get_empire_options(scenario_id: str | None = None) -> dict:
    bundle = load_bundle(scenario_id)
    result = empire_options(load_rules_db(bundle["game"].get("ruleset")), bundle["game"])
    overrides = bundle["game"].get("simulation_overrides") or {}
    global_amenity = simulation_settings(statics().rules).get("apply_amenity_effects", True)
    result["current"]["apply_amenity_effects"] = overrides.get("apply_amenity_effects", global_amenity)
    result["game_overrides"] = dict(overrides)
    from app.services.city_state_service import game_city_states

    ctx = context_for(bundle)
    entered = (bundle["game"].get("empire") or {}).get("city_states") or {}
    result["city_states"] = [
        {**cs, "envoys": (entered.get(cs["civilization"]) or {}).get("envoys", 0),
         "suzerain": (entered.get(cs["civilization"]) or {}).get("suzerain", False)}
        for cs in game_city_states(ctx)]
    return result


@synchronized_mutation
def update_empire(payload: dict, scenario_id: str | None = None) -> dict:
    st = statics()
    bundle = copy_bundle(load_bundle(scenario_id))
    before_ctx = context_for(copy_bundle(bundle), st)
    before = evaluate(before_ctx, include_details=False)
    payload = dict(payload)
    amenity = payload.pop("apply_amenity_effects", None)
    empire, warnings = validate_empire_update(st.db_for(bundle["game"]), bundle["game"], payload)
    bundle["game"]["empire"] = empire
    if isinstance(amenity, bool):
        bundle["game"].setdefault("simulation_overrides", {})["apply_amenity_effects"] = amenity
    after_ctx = context_for(bundle, st)
    after = evaluate(after_ctx, include_details=False)
    commit_bundle(scenario_id, bundle)
    impact = []
    for city_id, city_after in after["cities"].items():
        old = before["cities"].get(city_id)
        if not old:
            continue
        delta = {k: round(city_after["yields"]["totals"][k] - old["yields"]["totals"][k], 4) for k in YIELD_KEYS}
        impact.append({"city_id": city_id, "name": city_after["name"], "before": old["yields"]["totals"],
                       "after": city_after["yields"]["totals"], "delta": delta,
                       "housing_delta": round(city_after["housing"]["value"] - old["housing"]["value"], 4),
                       "amenity_delta": round(city_after["amenities"]["score"] - old["amenities"]["score"], 4)})
    return {"empire": empire, "warnings": warnings, "impact": impact, "modifiers": after["modifiers"]}


# --------------------------------------------------------------- scenarios
def list_scenarios() -> dict:
    return {"scenarios": scenario_service.list_all()}


@synchronized_mutation
def create_scenario(name: str, source: str | None = None, description: str = "") -> dict:
    if source and source != "current":
        bundle = scenario_service.state_of(source)
        label = f"scenario:{source}"
    else:
        bundle = main_bundle()
        bundle["game"].pop("sim_history", None)
        label = "current"
    return scenario_service.summary(scenario_service.create(name, bundle, source=label, description=description))


def get_scenario(scenario_id: str) -> dict:
    scenario = scenario_service.load(scenario_id)
    return {**scenario_service.summary(scenario), "history": scenario.get("history") or []}


@synchronized_mutation
def apply_scenario_to_game(scenario_id: str) -> dict:
    bundle = scenario_service.state_of(scenario_id)
    bundle["game"]["sim_history"] = list(game_state().get("sim_history") or [])
    commit_bundle(None, bundle)
    return {"applied": scenario_id, "turn": bundle["game"].get("turn"),
            "note": "현재 게임(메모리)에 적용되었습니다. 디스크에 남기려면 '저장'을 누르세요."}


def _final_metrics(history_rows: list[dict], final_eval: dict, city_id: str | None) -> dict:
    cities = final_eval["cities"]
    selected = [cities[city_id]] if city_id and city_id in cities else list(cities.values())
    totals = {k: round(sum(c["yields"]["totals"][k] for c in selected), 4) for k in YIELD_KEYS}
    cumulative = {k: 0.0 for k in YIELD_KEYS}
    completed: list[str] = []
    for row in history_rows:
        for cid, data in row["cities"].items():
            if city_id and cid != city_id:
                continue
            for k in YIELD_KEYS:
                cumulative[k] += data["yields"][k]
            completed += data.get("completed") or []
    return {
        "population": sum(c["population"] for c in selected),
        "yields": totals,
        "cumulative": {k: round(v, 4) for k, v in cumulative.items()},
        "completed": completed,
        "research": final_eval["empire"]["research"],
        "technologies_completed": [
            item["id"] for row in history_rows for item in row["empire"]["completed_research"] if item["category"] == "technology"
        ],
        "civics_completed": [
            item["id"] for row in history_rows for item in row["empire"]["completed_research"] if item["category"] == "civic"
        ],
    }


def compare_scenarios(scenario_ids: list[str], turns: int | None = None, city_id: str | None = None,
                      save_results: bool = False) -> dict:
    if not scenario_ids:
        raise ApplicationError(422, "비교할 시나리오를 선택하세요.")
    st = statics()
    rows = []
    for scenario_id in scenario_ids:
        scenario = scenario_service.load(scenario_id)
        bundle = copy_bundle(scenario["state"])
        if turns:
            final, _, hist = simulate_turns(bundle, st, turns)
            if save_results:
                scenario_service.store_state(scenario_id, final, hist)
            series = hist
        else:
            final = bundle
            series = scenario.get("history") or []
        final_eval = evaluate(context_for(final, st), include_details=False)
        rows.append({
            "scenario_id": scenario_id,
            "name": scenario.get("name"),
            "start_turn": (scenario["state"]["game"] if turns else scenario["base"]["game"]).get("turn"),
            "final_turn": final["game"].get("turn"),
            "final": _final_metrics(series, final_eval, city_id),
            "series": [
                {"turn": row["turn"],
                 "population": sum(d["population"] for cid, d in row["cities"].items() if not city_id or cid == city_id),
                 "yields": {k: round(sum(d["yields"][k] for cid, d in row["cities"].items() if not city_id or cid == city_id), 4)
                            for k in YIELD_KEYS}}
                for row in series
            ],
        })
    return {"turns": turns, "city_id": city_id, "saved": bool(turns and save_results), "rows": rows}


@synchronized_mutation
def strategy_batch(city_id: str, strategies: list[str], turns: int, name_prefix: str | None = None,
                   source: str | None = None) -> dict:
    """Create one scenario per strategy for a city, run N turns, compare."""
    unknown = [s for s in strategies if s not in PRESETS]
    if not strategies or unknown:
        raise ApplicationError(422, f"전략을 확인하세요: {', '.join(unknown) or '없음'}")
    base_bundle = scenario_service.state_of(source) if source and source != "current" else main_bundle()
    base_bundle["game"].pop("sim_history", None)
    if city_id not in base_bundle["cities"]:
        raise ApplicationError(404, "City not found.")
    created = []
    for strategy in strategies:
        bundle = copy_bundle(base_bundle)
        bundle["cities"][city_id]["citizen_mode"] = "auto"
        bundle["cities"][city_id]["strategy"] = strategy
        name = f"{name_prefix or bundle['cities'][city_id].get('name', city_id)} · {PRESETS[strategy]['label']}"
        scenario = scenario_service.create(name[:80], bundle, source="strategy-batch",
                                           description=f"{city_id} 자동 배치 전략 {strategy}")
        created.append(scenario["id"])
    comparison = compare_scenarios(created, turns, city_id, save_results=True)
    return {"created": created, "comparison": comparison}


# -------------------------------------------------------------- validation
def coverage_report() -> dict:
    results = run_validation()["results"]
    return {"statuses": ["검증 완료", "부분 구현", "미검증", "미지원"], "items": validation.coverage(results),
            "validation": {"cases": len(results), "passed": sum(1 for r in results if r["status"] == "일치")}}


def run_validation() -> dict:
    st = statics()
    results = []
    evaluations: dict[str | None, dict] = {}
    for case in validation.load_cases():
        scenario_id = case.get("scenario_id")
        try:
            if scenario_id not in evaluations:
                evaluations[scenario_id] = evaluate(context_for(load_bundle(scenario_id), st))
            city_eval = evaluations[scenario_id]["cities"].get(case["city_id"])
        except ApplicationError as error:
            results.append({**case, "status": "실행 불가", "reason": error.detail, "fields": []})
            continue
        results.append(validation.compare_case(case, city_eval))
    return {"results": results}


def add_validation_case(payload: dict) -> dict:
    return validation.add_case(payload)


def delete_validation_case(case_id: str) -> dict:
    return validation.delete_case(case_id)


def data_audit() -> dict:
    return validation.data_audit(load_rules_db(game_state().get("ruleset")), game_data_repository.catalog())


def strategy_info() -> dict:
    return {"presets": preset_list(), "note": "가중치는 최적화용 사용자 설정이며 게임 규칙이 아닙니다."}


def city_strategy(city: dict) -> dict:
    return strategy_for_city(city)
