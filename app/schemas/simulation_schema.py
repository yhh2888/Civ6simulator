from typing import Any

from pydantic import BaseModel, Field


class TurnRunRequest(BaseModel):
    turns: int = Field(default=1, ge=1, le=200)
    city_id: str | None = None


class CitySettingsRequest(BaseModel):
    citizen_mode: str | None = None
    strategy: str | None = None
    custom_strategy: dict[str, Any] | None = None
    new_citizen_policy: str | None = None
    housing_mode: str | None = None
    housing: float | None = None
    housing_extra: float | None = None
    amenities: float | None = None
    religion: str | None = None
    custom_effects: list[dict[str, Any]] | None = None
    trade_routes: list[dict[str, Any]] | None = None
    great_works: list[str] | None = None


class TileCitizensRequest(BaseModel):
    citizens: int = Field(ge=0, le=20)


class TilePreferenceRequest(BaseModel):
    lock: bool | None = None
    block: bool | None = None
    priority: float | None = None


class OptimizeRequest(BaseModel):
    strategy: str | None = None
    apply: bool = False
    compare_presets: bool = True
    custom_strategy: dict[str, Any] | None = None


class SimQueueRequest(BaseModel):
    category: str
    target_id: str
    x: int | None = None
    y: int | None = None


class SimReorderRequest(BaseModel):
    queue_id: str
    position: int = Field(ge=0)


class EmpireUpdateRequest(BaseModel):
    government: str | None = None
    policy_slots: dict[str, list[str]] | None = None
    civilization: str | None = None
    leader: str | None = None
    pantheon: str | None = None
    religion_beliefs: list[str] | None = None
    completed_projects: list[str] | None = None
    unit_copies: dict[str, int] | None = None
    boosted: dict[str, list[str]] | None = None
    enforce_prerequisites: bool | None = None
    treasury: dict[str, float] | None = None
    apply_amenity_effects: bool | None = None  # per-game override (game.simulation_overrides)
    city_states: dict[str, dict[str, Any]] | None = None  # {civ: {"envoys": n, "suzerain": bool}}


class ScenarioCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    source: str | None = None
    description: str = ""


class ScenarioRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = None


class ScenarioCompareRequest(BaseModel):
    scenario_ids: list[str]
    turns: int | None = Field(default=None, ge=1, le=200)
    city_id: str | None = None
    save_results: bool = False


class StrategyBatchRequest(BaseModel):
    city_id: str
    strategies: list[str]
    turns: int = Field(default=10, ge=1, le=200)
    name_prefix: str | None = None
    source: str | None = None


class ValidationCaseRequest(BaseModel):
    name: str | None = None
    city_id: str
    scenario_id: str | None = None
    expected: dict[str, float]
    tolerance: float = Field(default=0.05, ge=0, le=100)
    notes: str | None = None
