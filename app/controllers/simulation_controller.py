"""Simulation, scenario and validation endpoints (all new; legacy APIs unchanged).

Every city/turn endpoint accepts an optional ``scenario_id`` query parameter.
Without it the active game is used.
"""

from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import Response

from app.schemas.simulation_schema import (
    CitySettingsRequest,
    EmpireUpdateRequest,
    OptimizeRequest,
    ScenarioCompareRequest,
    ScenarioCreateRequest,
    ScenarioRenameRequest,
    SimQueueRequest,
    SimReorderRequest,
    StrategyBatchRequest,
    TileCitizensRequest,
    TilePreferenceRequest,
    TurnRunRequest,
    ValidationCaseRequest,
)
from app.services import scenario_service, simulation_service as sim

router = APIRouter()


def _dump(model) -> dict:
    return model.model_dump(exclude_unset=True)


# ------------------------------------------------------------------ state
@router.get("/api/sim/state")
def sim_state(scenario_id: str | None = None, details: bool = False):
    return sim.get_state(scenario_id, details)


@router.get("/api/sim/cities/{city_id}")
def sim_city(city_id: str, scenario_id: str | None = None):
    return sim.get_city(city_id, scenario_id)


@router.get("/api/sim/cities/{city_id}/ledger")
def sim_city_ledger(city_id: str, scenario_id: str | None = None):
    return sim.city_ledger(city_id, scenario_id)


@router.get("/api/sim/difficulties")
def sim_difficulties(scenario_id: str | None = None):
    return sim.get_difficulties(scenario_id)


@router.put("/api/sim/difficulties")
def sim_update_difficulties(payload: dict, scenario_id: str | None = None):
    return sim.update_difficulties(payload, scenario_id)


@router.get("/api/sim/ledger/yield/{yield_key}")
def sim_empire_ledger(yield_key: str, scenario_id: str | None = None):
    return sim.empire_ledger(yield_key, scenario_id)


@router.get("/api/sim/cities/{city_id}/ledger.csv")
def sim_city_ledger_csv(city_id: str, scenario_id: str | None = None):
    text, filename = sim.city_ledger_csv(city_id, scenario_id)
    return Response(
        content=text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/api/sim/modifiers")
def sim_modifiers(scenario_id: str | None = None):
    return sim.modifier_audit(scenario_id)


@router.get("/api/sim/history")
def sim_history(scenario_id: str | None = None):
    return sim.history(scenario_id)


@router.get("/api/sim/strategies")
def sim_strategies():
    return sim.strategy_info()


# ------------------------------------------------------------------ turns
@router.post("/api/sim/turn/preview")
def sim_turn_preview(request: TurnRunRequest, scenario_id: str | None = None):
    return sim.preview_turns(request.turns, scenario_id)


@router.post("/api/sim/turn")
def sim_turn(request: TurnRunRequest, scenario_id: str | None = None):
    return sim.run_turns(request.turns, scenario_id, request.city_id)


# --------------------------------------------------------------- citizens
@router.put("/api/sim/cities/{city_id}/settings")
def sim_city_settings(city_id: str, request: CitySettingsRequest, scenario_id: str | None = None):
    return sim.update_city_settings(city_id, _dump(request), scenario_id)


@router.put("/api/sim/cities/{city_id}/citizens/{x}/{y}")
def sim_assign_tile(city_id: str, x: int, y: int, request: TileCitizensRequest, scenario_id: str | None = None):
    return sim.assign_tile(city_id, x, y, request.citizens, scenario_id)


@router.put("/api/sim/cities/{city_id}/tile-preferences/{x}/{y}")
def sim_tile_preference(city_id: str, x: int, y: int, request: TilePreferenceRequest, scenario_id: str | None = None):
    return sim.tile_preference(city_id, x, y, _dump(request), scenario_id)


@router.post("/api/sim/cities/{city_id}/optimize")
def sim_optimize(city_id: str, request: OptimizeRequest, scenario_id: str | None = None):
    return sim.optimize(city_id, request.strategy, request.apply, request.compare_presets,
                        request.custom_strategy, scenario_id)


# ------------------------------------------------------------- production
@router.get("/api/sim/cities/{city_id}/production/options")
def sim_production_options(city_id: str, scenario_id: str | None = None):
    return sim.production_options(city_id, scenario_id)


@router.post("/api/sim/cities/{city_id}/production/queue")
def sim_queue_add(city_id: str, request: SimQueueRequest, scenario_id: str | None = None):
    return sim.queue_add(city_id, _dump(request), scenario_id)


@router.put("/api/sim/cities/{city_id}/production/order")
def sim_queue_order(city_id: str, request: SimReorderRequest, scenario_id: str | None = None):
    return sim.queue_reorder(city_id, request.queue_id, request.position, scenario_id)


@router.delete("/api/sim/cities/{city_id}/production/queue/{queue_id}")
def sim_queue_remove(city_id: str, queue_id: str, scenario_id: str | None = None):
    return sim.queue_remove(city_id, queue_id, scenario_id)


# ----------------------------------------------------------------- empire
@router.get("/api/sim/empire/options")
def sim_empire_options(scenario_id: str | None = None):
    return sim.get_empire_options(scenario_id)


@router.put("/api/sim/empire")
def sim_empire_update(request: EmpireUpdateRequest, scenario_id: str | None = None):
    return sim.update_empire(_dump(request), scenario_id)


# -------------------------------------------------------------- scenarios
@router.get("/api/scenarios")
def scenarios_list():
    return sim.list_scenarios()


@router.post("/api/scenarios")
def scenarios_create(request: ScenarioCreateRequest):
    return sim.create_scenario(request.name, request.source, request.description)


@router.post("/api/scenarios/compare")
def scenarios_compare(request: ScenarioCompareRequest):
    return sim.compare_scenarios(request.scenario_ids, request.turns, request.city_id, request.save_results)


@router.post("/api/scenarios/strategy-batch")
def scenarios_strategy_batch(request: StrategyBatchRequest):
    return sim.strategy_batch(request.city_id, request.strategies, request.turns, request.name_prefix, request.source)


@router.get("/api/scenarios/{scenario_id}")
def scenarios_get(scenario_id: str):
    return sim.get_scenario(scenario_id)


@router.get("/api/scenarios/{scenario_id}/state")
def scenarios_state(scenario_id: str):
    return scenario_service.state_of(scenario_id)


@router.patch("/api/scenarios/{scenario_id}")
def scenarios_rename(scenario_id: str, request: ScenarioRenameRequest):
    return scenario_service.rename(scenario_id, request.name, request.description)


@router.delete("/api/scenarios/{scenario_id}")
def scenarios_delete(scenario_id: str):
    return scenario_service.delete(scenario_id)


@router.post("/api/scenarios/{scenario_id}/reset")
def scenarios_reset(scenario_id: str):
    return scenario_service.reset(scenario_id)


@router.post("/api/scenarios/{scenario_id}/duplicate")
def scenarios_duplicate(scenario_id: str, request: ScenarioRenameRequest):
    return sim.create_scenario(request.name, scenario_id, request.description or "")


@router.post("/api/scenarios/{scenario_id}/apply")
def scenarios_apply(scenario_id: str):
    return sim.apply_scenario_to_game(scenario_id)


# ------------------------------------------------------------- validation
@router.get("/api/validation/coverage")
def validation_coverage():
    return sim.coverage_report()


@router.get("/api/validation/cases")
def validation_cases():
    return sim.run_validation()


@router.post("/api/validation/cases")
def validation_add(request: ValidationCaseRequest):
    return sim.add_validation_case(_dump(request))


@router.delete("/api/validation/cases/{case_id}")
def validation_delete(case_id: str):
    return sim.delete_validation_case(case_id)


@router.get("/api/data/audit")
def data_audit():
    return sim.data_audit()
