"""Citizen assignment: manual edits, automatic optimization, new citizens.

Assignment storage (backwards compatible):

* ordinary tiles: ``tile.worked`` on the map (existing field);
* district specialists: ``city.specialists[{"x,y"}] = count`` (new field);
* the city center is always worked and never counts toward citizens.

City settings (new, optional fields on the city document):

* ``citizen_mode``: ``"manual"`` (default, respects existing map flags) or
  ``"auto"`` (re-optimized at the start of every simulated turn);
* ``strategy`` / ``custom_strategy``: optimizer preset;
* ``new_citizen_policy``: ``"auto"`` (place new citizens on the best slot) or
  ``"manual"`` (leave them unassigned until the user places them);
* ``tile_preferences[{"x,y"}] = {"lock", "block", "priority"}``.
"""

from __future__ import annotations

from typing import Any

from app.core.exceptions import ApplicationError
from app.services.city_optimization_service import (
    PRESETS,
    optimize_assignment,
    strategy_for_city,
)
from app.services.city_growth_service import food_consumption
from app.services.modifier_engine import EffectBook
from app.services.sim_context import EngineContext, as_float, parse_tile_key, refresh_views, tile_key
from app.services.yield_breakdown_service import (
    compute_city,
    current_assignment,
    specialist_slots,
    workable_options,
    worker_capacity,
)

MODES = ("manual", "auto")
NEW_CITIZEN_POLICIES = ("auto", "manual")


def city_mode(ctx: EngineContext, city: dict) -> str:
    mode = city.get("citizen_mode") or ctx.settings.get("default_citizen_mode", "manual")
    return mode if mode in MODES else "manual"


def new_citizen_policy(ctx: EngineContext, city: dict) -> str:
    policy = city.get("new_citizen_policy") or ctx.settings.get("default_new_citizen_policy", "auto")
    return policy if policy in NEW_CITIZEN_POLICIES else "auto"


def write_assignment(ctx: EngineContext, city_id: str, assignment: dict[str, int]) -> None:
    """Store an assignment in the (copied) documents of ``ctx``."""
    view = ctx.city_views[city_id]
    city = view.city
    specialists: dict[str, int] = {}
    for tile in view.tiles:
        position = (tile["x"], tile["y"])
        key = tile_key(*position)
        if position == view.center:
            tile["worked"] = True
            continue
        count = int(assignment.get(key, 0))
        if tile.get("district"):
            tile["worked"] = count > 0
            if count > 0:
                specialists[key] = count
        else:
            tile["worked"] = count > 0
    city["specialists"] = specialists


def fixed_food(ctx: EngineContext, book: EffectBook, city_id: str, amenity_state: dict | None) -> float:
    result = compute_city(ctx, book, city_id, assignment={}, amenity_state=amenity_state)
    return result["totals"]["Food"]


def optimize_city(
    ctx: EngineContext,
    book: EffectBook,
    city_id: str,
    amenity_state: dict | None = None,
    strategy_id: str | None = None,
    custom_strategy: dict | None = None,
) -> dict[str, Any]:
    view = ctx.city_views[city_id]
    city = dict(view.city)
    if strategy_id:
        city["strategy"] = strategy_id
    if custom_strategy is not None:
        city["custom_strategy"] = custom_strategy
    strategy = strategy_for_city(city, ctx.settings.get("default_strategy", "balanced"))
    options = workable_options(ctx, book, city_id)
    consumption, _ = food_consumption(ctx, int(view.city.get("population", 1)))
    result = optimize_assignment(
        ctx, book, city_id,
        strategy=strategy,
        fixed_food=fixed_food(ctx, book, city_id, amenity_state),
        consumption=consumption,
        capacity=worker_capacity(ctx, view.city),
        options=options,
    )
    return result


def place_new_citizens(ctx: EngineContext, book: EffectBook, city_id: str, count: int,
                       amenity_state: dict | None = None) -> list[str]:
    """Greedily place ``count`` new citizens on the best free slots."""
    placed: list[str] = []
    for _ in range(max(0, count)):
        assignment = current_assignment(ctx, city_id)
        used = sum(assignment.values())
        if used >= worker_capacity(ctx, ctx.city_views[city_id].city):
            break
        optimized = optimize_city(ctx, book, city_id, amenity_state)
        # Keep existing placements; add the best slot from the optimized plan.
        candidates = [
            item for item in optimized["chosen"]
            if assignment.get(item["key"], 0) < optimized["assignment"].get(item["key"], 0)
        ]
        if not candidates:
            options = [o for o in workable_options(ctx, book, city_id)
                       if assignment.get(o["key"], 0) < o.get("slots", 1)
                       and not ((ctx.city_views[city_id].city.get("tile_preferences") or {}).get(o["key"]) or {}).get("block")]
            weights = optimized["strategy"]["weights"]
            options.sort(key=lambda o: (-sum(weights.get(k, 0) * as_float(o["yields"].get(k)) for k in weights), o["y"], o["x"]))
            if not options:
                break
            key = options[0]["key"]
        else:
            key = candidates[0]["key"]
        assignment[key] = assignment.get(key, 0) + 1
        write_assignment(ctx, city_id, assignment)
        placed.append(key)
    return placed


def trim_to_capacity(ctx: EngineContext, book: EffectBook, city_id: str) -> list[str]:
    """Remove the weakest placements when population shrank (e.g. Settler)."""
    view = ctx.city_views[city_id]
    assignment = current_assignment(ctx, city_id)
    capacity = worker_capacity(ctx, view.city)
    removed: list[str] = []
    if sum(assignment.values()) <= capacity:
        return removed
    strategy = strategy_for_city(view.city, ctx.settings.get("default_strategy", "balanced"))
    options = {o["key"]: o for o in workable_options(ctx, book, city_id)}
    preferences = view.city.get("tile_preferences") or {}

    def value(key: str) -> tuple:
        option = options.get(key, {"yields": {}})
        locked = bool((preferences.get(key) or {}).get("lock"))
        score = sum(strategy["weights"].get(k, 0) * as_float(option["yields"].get(k)) for k in strategy["weights"])
        return (locked, score, key)

    while sum(assignment.values()) > capacity:
        key = min(assignment, key=value)
        assignment[key] -= 1
        if assignment[key] <= 0:
            assignment.pop(key)
        removed.append(key)
    write_assignment(ctx, city_id, assignment)
    return removed


def set_tile_citizens(ctx: EngineContext, city_id: str, x: int, y: int, citizens: int) -> dict[str, int]:
    view = ctx.city_views.get(city_id)
    if view is None:
        raise ApplicationError(404, "City not found.")
    if (x, y) == view.center:
        raise ApplicationError(409, "도심 타일은 항상 작업되며 시민 수를 바꿀 수 없습니다.")
    if (x, y) not in view.tile_keys:
        raise ApplicationError(409, "이 도시가 소유한 타일에만 시민을 배치할 수 있습니다.")
    tile = ctx.tile(x, y)
    if citizens < 0:
        raise ApplicationError(422, "시민 수는 0 이상이어야 합니다.")
    if tile.get("district") == "DISTRICT_WONDER":
        raise ApplicationError(409, "불가사의 타일에는 시민을 배치할 수 없습니다.")
    if tile.get("district"):
        slots = specialist_slots(ctx, city_id, tile)
        if citizens > slots:
            raise ApplicationError(409, f"이 특수지구의 전문가 슬롯은 {slots}개입니다 (건물이 슬롯을 제공).")
    elif citizens > 1:
        raise ApplicationError(409, "일반 타일에는 시민 1명만 배치할 수 있습니다.")
    preference = (view.city.get("tile_preferences") or {}).get(tile_key(x, y)) or {}
    if citizens and preference.get("block"):
        raise ApplicationError(409, "사용 금지로 지정된 타일입니다. 먼저 금지를 해제하세요.")
    assignment = current_assignment(ctx, city_id)
    key = tile_key(x, y)
    if citizens:
        assignment[key] = citizens
    else:
        assignment.pop(key, None)
    capacity = worker_capacity(ctx, view.city)
    if sum(assignment.values()) > capacity:
        raise ApplicationError(409, f"시민 수({view.city.get('population')})를 초과해 배치할 수 없습니다. 다른 타일에서 먼저 해제하세요.")
    write_assignment(ctx, city_id, assignment)
    view.city["citizen_mode"] = "manual"
    return assignment


def set_tile_preference(ctx: EngineContext, city_id: str, x: int, y: int, payload: dict) -> dict:
    view = ctx.city_views.get(city_id)
    if view is None:
        raise ApplicationError(404, "City not found.")
    if (x, y) not in view.tile_keys or (x, y) == view.center:
        raise ApplicationError(409, "도심이 아닌 소유 타일에만 우선순위를 지정할 수 있습니다.")
    preferences = view.city.setdefault("tile_preferences", {})
    key = tile_key(x, y)
    current = dict(preferences.get(key) or {})
    for field in ("lock", "block"):
        if field in payload and payload[field] is not None:
            current[field] = bool(payload[field])
    if payload.get("priority") is not None:
        priority = as_float(payload["priority"])
        if not -10 <= priority <= 10:
            raise ApplicationError(422, "우선순위는 -10 ~ 10 범위입니다.")
        current["priority"] = priority
    if current.get("lock") and current.get("block"):
        raise ApplicationError(409, "같은 타일을 잠금과 사용 금지로 동시에 지정할 수 없습니다.")
    if not any(current.get(k) for k in ("lock", "block", "priority")):
        preferences.pop(key, None)
    else:
        preferences[key] = current
    if current.get("block"):
        assignment = current_assignment(ctx, city_id)
        if assignment.pop(key, None):
            write_assignment(ctx, city_id, assignment)
    return preferences.get(key, {})


def apply_city_settings(ctx: EngineContext, city_id: str, payload: dict) -> dict:
    view = ctx.city_views.get(city_id)
    if view is None:
        raise ApplicationError(404, "City not found.")
    city = view.city
    if "citizen_mode" in payload and payload["citizen_mode"] is not None:
        if payload["citizen_mode"] not in MODES:
            raise ApplicationError(422, "citizen_mode는 manual 또는 auto 입니다.")
        city["citizen_mode"] = payload["citizen_mode"]
    if "strategy" in payload and payload["strategy"] is not None:
        if payload["strategy"] not in PRESETS:
            raise ApplicationError(422, "알 수 없는 운영 전략입니다.")
        city["strategy"] = payload["strategy"]
    if "custom_strategy" in payload and payload["custom_strategy"] is not None:
        custom = payload["custom_strategy"]
        if not isinstance(custom, dict):
            raise ApplicationError(422, "custom_strategy는 객체여야 합니다.")
        weights = custom.get("weights") or {}
        city["custom_strategy"] = {
            "weights": {k: as_float(v) for k, v in weights.items() if k in PRESETS["custom"]["weights"]},
            "min_food_surplus": as_float(custom.get("min_food_surplus")),
        }
    if "new_citizen_policy" in payload and payload["new_citizen_policy"] is not None:
        if payload["new_citizen_policy"] not in NEW_CITIZEN_POLICIES:
            raise ApplicationError(422, "new_citizen_policy는 auto 또는 manual 입니다.")
        city["new_citizen_policy"] = payload["new_citizen_policy"]
    for field in ("housing_mode",):
        if field in payload and payload[field] is not None:
            if payload[field] not in ("computed", "manual"):
                raise ApplicationError(422, "housing_mode는 computed 또는 manual 입니다.")
            city[field] = payload[field]
    for field in ("housing", "housing_extra", "amenities"):
        if field in payload and payload[field] is not None:
            city[field] = as_float(payload[field])
    if "religion" in payload:
        city["religion"] = payload["religion"] or None
    if "custom_effects" in payload and payload["custom_effects"] is not None:
        effects = payload["custom_effects"]
        if not isinstance(effects, list):
            raise ApplicationError(422, "custom_effects는 목록이어야 합니다.")
        cleaned = []
        for index, effect in enumerate(effects):
            if not isinstance(effect, dict):
                raise ApplicationError(422, "custom_effects 항목은 객체여야 합니다.")
            cleaned.append({
                "id": str(effect.get("id") or f"custom-{index + 1}"),
                "label": str(effect.get("label") or "사용자 정의 효과")[:80],
                "enabled": effect.get("enabled", True) is not False,
                "yields": {k: as_float(v) for k, v in (effect.get("yields") or {}).items() if k in PRESETS["custom"]["weights"]},
                "percent": {k: as_float(v) for k, v in (effect.get("percent") or {}).items() if k in PRESETS["custom"]["weights"]},
            })
        city["custom_effects"] = cleaned
    if "great_works" in payload and payload["great_works"] is not None:
        works = payload["great_works"]
        known = ctx.db.index("GreatWorks", "GreatWorkType")
        if not isinstance(works, list) or len(works) > 40 or any(w not in known for w in works):
            raise ApplicationError(422, "great_works는 GreatWorks 표의 걸작 타입 목록이어야 합니다 (최대 40개).")
        city["great_works"] = list(works)
    if "trade_routes" in payload and payload["trade_routes"] is not None:
        from app.services.trade_route_service import sanitize_routes

        city["trade_routes"] = sanitize_routes(payload["trade_routes"], ctx, city_id)
    refresh_views(ctx)
    return city


def assignment_keys(assignment: dict[str, int]) -> list[tuple[int, int]]:
    return [pos for pos in (parse_tile_key(k) for k in assignment) if pos]
