"""Deterministic citizen assignment optimizer.

This is **not** a reproduction of the Civ VI city governor AI (its weights are
not in the game data). It is a transparent optimizer:

    maximize  Σ score(slot)   subject to   food surplus ≥ target,
                                           |assigned| ≤ citizens,
                                           locked slots included, blocked excluded

``score = Σ weight[yield] × yield + priority bonus``. The problem is solved with
a Lagrangian relaxation on the food constraint (binary search on λ, top-K
selection), followed by a 1-swap local improvement pass. Ties are broken by
tile coordinates, so identical inputs always give identical results.

Percentage modifiers that scale every non-food yield equally (amenities) do
not change rankings; food % modifiers are ignored during the search but the
final city result is always recomputed exactly.
"""

from __future__ import annotations

from typing import Any

from app.config import YIELD_KEYS
from app.services.modifier_engine import EffectBook
from app.services.sim_context import EngineContext, as_float, zero_yields

PRESETS: dict[str, dict[str, Any]] = {
    "balanced": {"label": "균형 성장", "weights": {"Food": 1.0, "Production": 1.0, "Gold": 0.6, "Science": 0.8, "Culture": 0.8, "Faith": 0.5}, "min_food_surplus": 1.0},
    "food": {"label": "식량 집중", "weights": {"Food": 3.0, "Production": 0.6, "Gold": 0.3, "Science": 0.4, "Culture": 0.4, "Faith": 0.2}, "min_food_surplus": 0.0},
    "production": {"label": "생산력 집중", "weights": {"Food": 0.6, "Production": 3.0, "Gold": 0.3, "Science": 0.4, "Culture": 0.4, "Faith": 0.2}, "min_food_surplus": 0.0},
    "gold": {"label": "금 집중", "weights": {"Food": 0.6, "Production": 0.6, "Gold": 3.0, "Science": 0.4, "Culture": 0.4, "Faith": 0.2}, "min_food_surplus": 0.0},
    "science": {"label": "과학 집중", "weights": {"Food": 0.6, "Production": 0.6, "Gold": 0.3, "Science": 3.0, "Culture": 0.4, "Faith": 0.2}, "min_food_surplus": 0.0},
    "culture": {"label": "문화 집중", "weights": {"Food": 0.6, "Production": 0.6, "Gold": 0.3, "Science": 0.4, "Culture": 3.0, "Faith": 0.2}, "min_food_surplus": 0.0},
    "faith": {"label": "신앙 집중", "weights": {"Food": 0.6, "Production": 0.6, "Gold": 0.3, "Science": 0.4, "Culture": 0.4, "Faith": 3.0}, "min_food_surplus": 0.0},
    "growth": {"label": "인구 성장 우선", "weights": {"Food": 4.0, "Production": 0.8, "Gold": 0.3, "Science": 0.4, "Culture": 0.4, "Faith": 0.2}, "min_food_surplus": 2.0},
    "build": {"label": "생산 완성 우선", "weights": {"Food": 0.8, "Production": 4.0, "Gold": 0.3, "Science": 0.3, "Culture": 0.3, "Faith": 0.2}, "min_food_surplus": 0.0},
    "custom": {"label": "사용자 지정 전략", "weights": {key: 1.0 for key in YIELD_KEYS}, "min_food_surplus": 0.0},
}

PRIORITY_WEIGHT = 1.0


def preset_list() -> list[dict]:
    return [{"id": key, **value} for key, value in PRESETS.items()]


def strategy_for_city(city: dict, default: str = "balanced") -> dict[str, Any]:
    strategy_id = city.get("strategy") or default
    if strategy_id not in PRESETS:
        strategy_id = default
    preset = PRESETS[strategy_id]
    weights = dict(preset["weights"])
    min_surplus = preset["min_food_surplus"]
    if strategy_id == "custom":
        custom = city.get("custom_strategy") or {}
        for key in YIELD_KEYS:
            if key in (custom.get("weights") or {}):
                weights[key] = as_float(custom["weights"][key])
        if "min_food_surplus" in custom:
            min_surplus = as_float(custom.get("min_food_surplus"))
    return {"id": strategy_id, "label": preset["label"], "weights": weights, "min_food_surplus": min_surplus}


def _expand(options: list[dict]) -> list[dict]:
    units = []
    for option in options:
        for index in range(int(option.get("slots", 1))):
            units.append({**option, "unit_index": index})
    return units


def _sort_key(unit: dict) -> tuple:
    return (unit["y"], unit["x"], unit["unit_index"])


def optimize_assignment(
    ctx: EngineContext,
    book: EffectBook,
    city_id: str,
    *,
    strategy: dict[str, Any] | None = None,
    fixed_food: float,
    consumption: float,
    capacity: int,
    options: list[dict],
) -> dict[str, Any]:
    """Return the optimal assignment for one city.

    ``fixed_food`` is food produced independent of citizen placement (city
    center, buildings, districts, modifiers). ``options`` is
    ``yield_breakdown_service.workable_options`` output.
    """
    city = ctx.city_views[city_id].city
    strategy = strategy or strategy_for_city(city, ctx.settings.get("default_strategy", "balanced"))
    weights = strategy["weights"]
    preferences = city.get("tile_preferences") or {}
    units = _expand(options)
    for unit in units:
        pref = preferences.get(unit["key"]) or {}
        unit["locked"] = bool(pref.get("lock"))
        unit["blocked"] = bool(pref.get("block"))
        unit["priority"] = as_float(pref.get("priority"))
        unit["base_score"] = sum(weights.get(k, 0.0) * as_float(unit["yields"].get(k)) for k in YIELD_KEYS)
        unit["score"] = unit["base_score"] + unit["priority"] * PRIORITY_WEIGHT
        unit["food"] = as_float(unit["yields"].get("Food"))
    candidates = [u for u in units if not u["blocked"]]
    locked = sorted([u for u in candidates if u["locked"]], key=_sort_key)[:capacity]
    locked_ids = {id(u) for u in locked}
    free = [u for u in candidates if id(u) not in locked_ids]
    slots = max(0, capacity - len(locked))
    target_food = consumption + strategy["min_food_surplus"] - fixed_food - sum(u["food"] for u in locked)

    def pick(lam: float) -> list[dict]:
        ranked = sorted(free, key=lambda u: (-(u["score"] + lam * u["food"]), -u["food"], _sort_key(u)))
        chosen = []
        for unit in ranked:
            if len(chosen) >= slots:
                break
            if unit["score"] + lam * unit["food"] <= 0 and lam == 0 and unit["score"] <= 0 and unit["food"] <= 0:
                continue
            chosen.append(unit)
        return chosen

    def food_of(sel: list[dict]) -> float:
        return sum(u["food"] for u in sel)

    def score_of(sel: list[dict]) -> float:
        return sum(u["score"] for u in sel)

    selection = pick(0.0)
    feasible = food_of(selection) >= target_food - 1e-9
    lam_used = 0.0
    if not feasible:
        high = 1.0
        while high < 1e6 and food_of(pick(high)) < target_food - 1e-9:
            high *= 2
        if food_of(pick(high)) < target_food - 1e-9:
            selection = sorted(free, key=lambda u: (-u["food"], -u["score"], _sort_key(u)))[:slots]
            lam_used = float("inf")
        else:
            low = 0.0
            for _ in range(48):
                mid = (low + high) / 2
                if food_of(pick(mid)) >= target_food - 1e-9:
                    high = mid
                else:
                    low = mid
            selection = pick(high)
            lam_used = high
        feasible = food_of(selection) >= target_food - 1e-9
    # 1-swap improvement that preserves feasibility (or improves food when infeasible).
    improved = True
    passes = 0
    while improved and passes < 20:
        improved = False
        passes += 1
        chosen_ids = {id(u) for u in selection}
        outside = sorted([u for u in free if id(u) not in chosen_ids], key=lambda u: (-u["score"], _sort_key(u)))
        current_food = food_of(selection)
        for out_unit in sorted(selection, key=lambda u: (u["score"], _sort_key(u))):
            for in_unit in outside:
                gain = in_unit["score"] - out_unit["score"]
                new_food = current_food - out_unit["food"] + in_unit["food"]
                ok = new_food >= target_food - 1e-9 if feasible else new_food > current_food + 1e-9
                if (gain > 1e-9 and ok) or (not feasible and ok):
                    selection = [u for u in selection if u is not out_unit] + [in_unit]
                    feasible = feasible or new_food >= target_food - 1e-9
                    improved = True
                    break
            if improved:
                break
        if len(selection) < slots:
            chosen_ids = {id(u) for u in selection}
            rest = [u for u in free if id(u) not in chosen_ids and (u["score"] > 0 or u["food"] > 0)]
            rest.sort(key=lambda u: (-u["score"], _sort_key(u)))
            if rest:
                selection.append(rest[0])
                improved = True
    final = locked + sorted(selection, key=_sort_key)
    assignment: dict[str, int] = {}
    for unit in final:
        assignment[unit["key"]] = assignment.get(unit["key"], 0) + 1
    chosen_ids = {id(u) for u in final}
    reasons = []
    for unit in sorted(final, key=lambda u: (-u["score"], _sort_key(u))):
        reasons.append({
            "key": unit["key"], "kind": unit["kind"], "yields": unit["yields"],
            "score": round(unit["score"], 4), "locked": unit["locked"], "priority": unit["priority"],
            "reason": ("잠금 타일" if unit["locked"] else
                       f"가중 점수 {unit['score']:.2f}" + (f" (식량 제약 λ={lam_used:.2f} 반영)" if lam_used and lam_used != float('inf') else "")),
        })
    alternatives = []
    for unit in sorted([u for u in units if id(u) not in chosen_ids], key=lambda u: (-u["score"], _sort_key(u)))[:8]:
        alternatives.append({"key": unit["key"], "kind": unit["kind"], "yields": unit["yields"],
                             "score": round(unit["score"], 4),
                             "excluded_because": "사용 금지 타일" if unit["blocked"] else "점수/식량 제약에서 밀림"})
    projected_food = fixed_food + sum(u["food"] for u in final)
    return {
        "strategy": strategy,
        "assignment": assignment,
        "chosen": reasons,
        "alternatives": alternatives,
        "feasible_food_target": feasible,
        "food_target": round(consumption + strategy["min_food_surplus"], 4),
        "projected_food": round(projected_food, 4),
        "lagrange_lambda": None if lam_used == float("inf") else round(lam_used, 6),
        "capacity": capacity,
        "notes": [] if feasible else ["식량 목표를 만족하는 배치가 없어 식량을 최대화했습니다."],
    }


def score_totals(totals: dict, weights: dict) -> float:
    return sum(weights.get(k, 0.0) * as_float(totals.get(k)) for k in YIELD_KEYS)


def empty_yields() -> dict:
    return zero_yields()
