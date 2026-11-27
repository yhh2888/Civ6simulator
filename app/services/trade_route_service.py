"""Trade route yields for the origin city (user-entered routes).

Routes are stored on the origin city as ``city["trade_routes"]``:
``[{"destination": "x,y"}]`` where ``x,y`` is a city centre. A destination
that is one of the player's own cities is a domestic route; any other city
centre on the map (cities imported from a save keep foreign cities on the
map) is international.

What is computed (Gathering Storm rules):

* District_TradeRouteYields of every completed district in the destination
  (city centre included): ``YieldChangeAsDomesticDestination`` or
  ``YieldChangeAsInternationalDestination``.
* Routes to a city-state: its EFFECT_ADJUST_TRADE_ROUTE_YIELD_TO_OTHERS
  bonus (e.g. industrial city-states: +1 production), no envoys needed.
* Flat trade route modifiers: EFFECT_ADJUST_TRADE_ROUTE_YIELD (all routes),
  ..._FOR_DOMESTIC / ..._FOR_INTERNATIONAL (player level) and
  EFFECT_ADJUST_CITY_TRADE_ROUTE_YIELD_FOR_DOMESTIC / _FOR_INTERNATIONAL
  (origin city level).

Not computed: yields the destination city receives, per-resource and
per-improvement route bonuses, alliance/suzerain bonuses, percent modifiers.
Active routes are not decoded from saves, so they must be entered by hand.
"""

from __future__ import annotations

import math
from typing import Any

from app.services.sim_context import EngineContext, as_float, hex_distance, zero_yields

MAX_ROUTES_PER_CITY = 20

TRADE_ROUTE_EFFECTS = {
    "EFFECT_ADJUST_TRADE_ROUTE_YIELD": ("player", None),
    "EFFECT_ADJUST_TRADE_ROUTE_YIELD_FOR_DOMESTIC": ("player", "domestic"),
    "EFFECT_ADJUST_TRADE_ROUTE_YIELD_FOR_INTERNATIONAL": ("player", "international"),
    "EFFECT_ADJUST_CITY_TRADE_ROUTE_YIELD_FOR_DOMESTIC": ("city", "domestic"),
    "EFFECT_ADJUST_CITY_TRADE_ROUTE_YIELD_FOR_INTERNATIONAL": ("city", "international"),
}

_YIELD_KEYS = {
    "YIELD_FOOD": "Food", "YIELD_PRODUCTION": "Production", "YIELD_GOLD": "Gold",
    "YIELD_SCIENCE": "Science", "YIELD_CULTURE": "Culture", "YIELD_FAITH": "Faith",
}


def parse_key(value: Any) -> tuple[int, int] | None:
    try:
        x, y = (int(part) for part in str(value).split(","))
    except (TypeError, ValueError):
        return None
    return x, y


def _foreign_cities(ctx: EngineContext) -> list[dict]:
    meta = (ctx.map or {}).get("save_import") or {}
    return [c for c in meta.get("foreign_cities") or [] if isinstance(c, dict) and "x" in c and "y" in c]


def destinations(ctx: EngineContext, origin_id: str | None = None) -> list[dict]:
    """Every city a route can go to: own cities first, then foreign ones."""
    result = []
    for city_id, view in sorted(ctx.city_views.items(), key=lambda item: item[1].city.get("name", item[0])):
        if city_id == origin_id:
            continue
        result.append({"key": f"{view.center[0]},{view.center[1]}", "name": view.city.get("name", city_id),
                       "kind": "domestic", "city_id": city_id})
    for city in _foreign_cities(ctx):
        result.append({"key": f"{city['x']},{city['y']}", "name": city.get("name"), "kind": "international",
                       "owner": city.get("label"), "player": city.get("player")})
    return result


def _foreign_districts(ctx: EngineContext, center: tuple[int, int], player: Any) -> list[str]:
    """Completed districts of a foreign city: tiles of the same save owner whose
    nearest same-owner city centre is this one (within the 3-ring)."""
    centers = [(c["x"], c["y"]) for c in _foreign_cities(ctx) if c.get("player") == player]
    found = ["DISTRICT_CITY_CENTER"]
    for (x, y), tile in ctx.tile_index.items():
        district = tile.get("district")
        if not district or district in ("DISTRICT_CITY_CENTER", "DISTRICT_WONDER") or tile.get("save_owner") != player:
            continue
        distance = hex_distance(x, y, *center)
        if distance > 3:
            continue
        if any(hex_distance(x, y, *other) < distance for other in centers if other != center):
            continue
        found.append(district)
    return found


def resolve_destination(ctx: EngineContext, origin_id: str, route: dict) -> tuple[dict | None, str | None]:
    position = parse_key(route.get("destination"))
    if position is None:
        return None, f"교역로 목적지 {route.get('destination')!r} 형식 오류 (x,y)"
    for city_id, view in ctx.city_views.items():
        if view.center == position:
            if city_id == origin_id:
                return None, "교역로 목적지가 출발 도시 자신입니다."
            districts = ["DISTRICT_CITY_CENTER"] + [
                t["district"] for t in view.district_tiles if t.get("district") != "DISTRICT_WONDER"]
            return {"key": f"{position[0]},{position[1]}", "name": view.city.get("name", city_id),
                    "kind": "domestic", "districts": districts}, None
    for city in _foreign_cities(ctx):
        if (city["x"], city["y"]) == position:
            players = ((ctx.map or {}).get("save_import") or {}).get("players") or {}
            civilization = (players.get(str(city.get("player"))) or {}).get("civilization")
            return {"key": f"{position[0]},{position[1]}", "name": city.get("name"), "kind": "international",
                    "owner": city.get("label"), "civilization": civilization,
                    "districts": _foreign_districts(ctx, position, city.get("player"))}, None
    tile = ctx.tile_index.get(position) or {}
    if tile.get("district") == "DISTRICT_CITY_CENTER":
        return {"key": f"{position[0]},{position[1]}", "name": f"도시 ({position[0]},{position[1]})",
                "kind": "international", "districts": ["DISTRICT_CITY_CENTER"]}, None
    return None, f"교역로 목적지 {position[0]},{position[1]}에 도시가 없습니다."


def _record_yields(record) -> dict[str, float]:
    values = zero_yields()
    types = [t.strip() for t in str(record.data.get("YieldType") or "").split(",") if t.strip()]
    amounts = [a.strip() for a in str(record.data.get("Amount") or "").split(",") if a.strip()]
    for index, yield_type in enumerate(types):
        key = _YIELD_KEYS.get(yield_type)
        if key and amounts:
            values[key] += as_float(amounts[min(index, len(amounts) - 1)])
    return values


def route_timing(ctx: EngineContext, origin: tuple[int, int], destination: tuple[int, int]) -> dict:
    """Estimated length of a route and when the trader must be sent again.

    The game keeps the trader going round trips until the route has lasted at
    least TRADE_ROUTE_TURN_DURATION_BASE turns (20). Not scaled by game speed:
    in an ONLINE-speed save, Pella→Toledo started on turn 66 is still running
    on turn 90, which a 10-turn minimum would not allow. Travel is estimated from the hex
    distance and the trader's BaseMoves on a road costing 1 per tile; the real
    path (rivers, hills, faster roads) is not simulated.
    """
    base = as_float(ctx.db.param("TRADE_ROUTE_TURN_DURATION_BASE", 20) or 20)
    minimum = math.ceil(base)
    trader = ctx.db.index("Units", "UnitType").get("UNIT_TRADER") or {}
    moves = max(1, int(as_float(trader.get("BaseMoves"), 1)))
    distance = hex_distance(*origin, *destination)
    one_way = max(1, math.ceil(distance / moves))
    round_trip = 2 * one_way
    trips = max(1, math.ceil(minimum / round_trip))
    return {
        "distance": distance, "trader_moves": moves, "one_way_turns": one_way, "round_trip_turns": round_trip,
        "minimum_turns": minimum, "round_trips": trips, "duration_turns": trips * round_trip,
        "formula": (f"최소 {minimum}턴(게임 속도 무관), 거리 {distance}칸 ÷ 이동력 {moves} → 편도 {one_way}턴, "
                    f"왕복 {round_trip}턴 × {trips}회 = {trips * round_trip}턴 (근사: 직선거리·도로 비용 1)"),
        "source": f"GlobalParameters.TRADE_ROUTE_TURN_DURATION_BASE={base:g}, Units.UNIT_TRADER.BaseMoves={moves}",
    }


def route_results(ctx: EngineContext, book, city_id: str) -> tuple[list[dict], list[str]]:
    """Origin-city yields of each route entered on the city."""
    view = ctx.city_views[city_id]
    routes = [r for r in view.city.get("trade_routes") or [] if isinstance(r, dict)][:MAX_ROUTES_PER_CITY]
    if not routes:
        return [], []
    table = ctx.db.group("District_TradeRouteYields", "DistrictType")
    modifiers = []
    for effect, (level, kind) in TRADE_ROUTE_EFFECTS.items():
        for record in book.for_effect(effect):
            if level == "city" and record.subject.city_id != city_id:
                continue
            if level == "player" and record.subject.city_id not in (None, city_id):
                continue
            modifiers.append((record, kind))
    results, warnings = [], []
    for index, route in enumerate(routes):
        destination, error = resolve_destination(ctx, city_id, route)
        if error:
            warnings.append(error)
            continue
        column = ("YieldChangeAsDomesticDestination" if destination["kind"] == "domestic"
                  else "YieldChangeAsInternationalDestination")
        details, total = [], zero_yields()
        for district in destination["districts"]:
            values = zero_yields()
            for row in table.get(district, []):
                key = _YIELD_KEYS.get(row.get("YieldType") or "")
                if key:
                    values[key] += as_float(row.get(column))
            if any(values.values()):
                for key, amount in values.items():
                    total[key] += amount
                details.append({"kind": "trade_district", "id": district, "label": f"목적지 {district}",
                                "yields": values, "column": column})
        civilization = destination.get("civilization")
        if civilization:
            from app.services.city_state_service import is_city_state, trade_bonus_modifiers

            if is_city_state(ctx, civilization):
                arguments = ctx.db.group("ModifierArguments", "ModifierId")
                for modifier_id in trade_bonus_modifiers(ctx, civilization):
                    args = {a.get("Name"): a.get("Value") for a in arguments.get(modifier_id, [])}
                    values = zero_yields()
                    key = _YIELD_KEYS.get(args.get("YieldType") or "")
                    if key:
                        values[key] += as_float(args.get("Amount"))
                    if any(values.values()):
                        for k, amount in values.items():
                            total[k] += amount
                        details.append({"kind": "cs_trade", "id": modifier_id,
                                        "label": f"도시국가 {civilization} 교역로 보너스", "yields": values})
        for record, kind in modifiers:
            if kind and kind != destination["kind"]:
                continue
            values = _record_yields(record)
            if any(values.values()):
                for key, amount in values.items():
                    total[key] += amount
                details.append({"kind": "modifier", "record": record,
                                "label": f"{record.source_kind} {record.source_id}", "yields": values})
        position = parse_key(destination["key"])
        timing = route_timing(ctx, view.center, position)
        started = route.get("started_turn")
        if isinstance(started, (int, float)):
            timing["ends_turn"] = int(started) + timing["duration_turns"]
            timing["turns_left"] = max(0, timing["ends_turn"] - int(ctx.game.get("turn") or 0))
        results.append({"index": index, "destination": destination, "details": details, "yields": total,
                        "timing": timing})
    return results, warnings


def incoming_results(ctx: EngineContext, book, city_id: str) -> list[dict]:
    """Destination-city yields of routes other players run into the city:
    only EFFECT_ADJUST_TRADE_ROUTE_YIELD_FROM_OTHERS modifiers on the city
    (District_TradeRouteYields gives the destination nothing)."""
    view = ctx.city_views[city_id]
    routes = [r for r in view.city.get("incoming_routes") or [] if isinstance(r, dict)][:MAX_ROUTES_PER_CITY]
    if not routes:
        return []
    records = [r for r in book.for_effect("EFFECT_ADJUST_TRADE_ROUTE_YIELD_FROM_OTHERS")
               if r.subject.city_id in (None, city_id)]
    results = []
    for route in routes:
        details, total = [], zero_yields()
        for record in records:
            # Domestic-only modifiers do not apply: these routes come from other players.
            if str(record.data.get("Domestic", "")).lower() == "true":
                continue
            values = _record_yields(record)
            if any(values.values()):
                for key, amount in values.items():
                    total[key] += amount
                details.append({"record": record, "label": f"{record.source_kind} {record.source_id}", "yields": values})
        results.append({"origin": route.get("origin"), "origin_name": route.get("origin_name") or route.get("origin"),
                        "origin_label": route.get("origin_label"), "started_turn": route.get("started_turn"),
                        "details": details, "yields": total})
    return results


def sanitize_incoming(value: Any) -> list[dict]:
    from app.core.exceptions import ApplicationError

    if not isinstance(value, list) or len(value) > MAX_ROUTES_PER_CITY:
        raise ApplicationError(422, f"incoming_routes는 최대 {MAX_ROUTES_PER_CITY}개 목록입니다.")
    cleaned = []
    for item in value:
        if not isinstance(item, dict) or parse_key(item.get("origin")) is None:
            raise ApplicationError(422, "수입 교역로 항목은 {\"origin\": \"x,y\"} 형식이어야 합니다.")
        x, y = parse_key(item["origin"])
        entry = {"origin": f"{x},{y}", "origin_name": str(item.get("origin_name") or f"{x},{y}")[:60]}
        if item.get("origin_label"):
            entry["origin_label"] = str(item["origin_label"])[:60]
        if isinstance(item.get("origin_player"), int):
            entry["origin_player"] = item["origin_player"]
        if isinstance(item.get("started_turn"), (int, float)) and 0 <= item["started_turn"] < 10000:
            entry["started_turn"] = int(item["started_turn"])
        cleaned.append(entry)
    return cleaned


def sanitize_routes(value: Any, ctx: EngineContext | None = None, city_id: str | None = None) -> list[dict]:
    from app.core.exceptions import ApplicationError

    if value is None:
        return []
    if not isinstance(value, list):
        raise ApplicationError(422, "trade_routes는 목록이어야 합니다.")
    if len(value) > MAX_ROUTES_PER_CITY:
        raise ApplicationError(422, f"교역로는 도시당 최대 {MAX_ROUTES_PER_CITY}개입니다.")
    cleaned = []
    for item in value:
        if not isinstance(item, dict) or parse_key(item.get("destination")) is None:
            raise ApplicationError(422, "교역로 항목은 {\"destination\": \"x,y\"} 형식이어야 합니다.")
        x, y = parse_key(item["destination"])
        entry = {"destination": f"{x},{y}"}
        if isinstance(item.get("started_turn"), (int, float)) and 0 <= item["started_turn"] < 10000:
            entry["started_turn"] = int(item["started_turn"])
        if ctx is not None and city_id is not None:
            _, error = resolve_destination(ctx, city_id, entry)
            if error:
                raise ApplicationError(422, error)
        cleaned.append(entry)
    return cleaned
