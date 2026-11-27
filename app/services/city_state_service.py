"""City-state envoy and suzerain bonuses (user-entered envoys).

Envoys are not decoded from saves, so the player enters them per city-state
(``game.empire.city_states = {"CIVILIZATION_MEXICO_CITY": {"envoys": 1,
"suzerain": false}}``). The type bonus (MINOR_CIV_<TYPE>_TRAIT) and the
unique suzerain bonus (MINOR_CIV_<NAME>_TRAIT) are stored in the data as
EFFECT_ATTACH_MODIFIER modifiers gated by requirements on the receiving
player. This module evaluates those gates and returns the attached modifiers,
which the modifier engine then resolves like any other source.

Gate requirements: HAS_GIVEN_INFLUENCE_TOKENS (MinimumTokens ≤ envoys),
IS_SUZERAIN / IS_SUZERAIN_BONUS_ENABLED (suzerain flag), IS_AT_PEACE (assumed
true). Any other requirement (alliances, religion, unit counts) fails the gate
and is reported.
"""

from __future__ import annotations

from typing import Any

from app.services.sim_context import EngineContext, as_bool, as_float

ASSUMED_TRUE = {"REQUIREMENT_PLAYER_IS_AT_PEACE"}


def _leader_chain(ctx: EngineContext, civilization: str) -> list[str]:
    leaders = ctx.db.index("Leaders", "LeaderType")
    chain: list[str] = []
    for row in ctx.db.rows("CivilizationLeaders"):
        if row.get("CivilizationType") == civilization and row.get("LeaderType"):
            current = row["LeaderType"]
            while current and current not in chain:
                chain.append(current)
                current = (leaders.get(current) or {}).get("InheritFrom")
    if not chain:  # fall back to the naming convention
        guess = "LEADER_MINOR_CIV_" + civilization.removeprefix("CIVILIZATION_")
        current = guess if guess in leaders else None
        while current and current not in chain:
            chain.append(current)
            current = (leaders.get(current) or {}).get("InheritFrom")
    return chain


def city_state_type(ctx: EngineContext, civilization: str) -> str | None:
    """INDUSTRIAL, SCIENTIFIC, ... from the leader inheritance chain."""
    for leader in _leader_chain(ctx, civilization)[1:]:
        if leader.startswith("LEADER_MINOR_CIV_") and leader != "LEADER_MINOR_CIV_DEFAULT":
            return leader.removeprefix("LEADER_MINOR_CIV_")
    return None


def city_state_traits(ctx: EngineContext, civilization: str) -> list[str]:
    chain = _leader_chain(ctx, civilization)
    return [row["TraitType"] for row in ctx.db.rows("LeaderTraits")
            if row.get("LeaderType") in chain and row.get("TraitType")]


def is_city_state(ctx: EngineContext, civilization: str | None) -> bool:
    row = ctx.db.index("Civilizations", "CivilizationType").get(civilization or "") or {}
    return row.get("StartingCivilizationLevelType") == "CIVILIZATION_LEVEL_CITY_STATE"


def game_city_states(ctx: EngineContext) -> list[dict[str, Any]]:
    """City-states with a city in an imported save (map.save_import)."""
    meta = (ctx.map or {}).get("save_import") or {}
    players = meta.get("players") or {}
    # The save lists every city-state slot; only those with a city are in play.
    with_city = {str(c.get("player")) for c in meta.get("foreign_cities") or [] if isinstance(c, dict)}
    result = []
    for index, info in sorted(players.items(), key=lambda item: int(item[0]) if str(item[0]).isdigit() else 0):
        civ = (info or {}).get("civilization")
        if civ and str(index) in with_city and is_city_state(ctx, civ):
            result.append({"civilization": civ, "player": int(index) if str(index).isdigit() else index,
                           "name": civ.removeprefix("CIVILIZATION_").replace("_", " ").title(),
                           "type": city_state_type(ctx, civ)})
    return result


def _gate(ctx: EngineContext, set_id: str | None, state: dict) -> tuple[bool, str | None]:
    """(passes, unsupported reason) for the attach modifier's requirement set."""
    if not set_id:
        return True, None
    set_row = ctx.db.index("RequirementSets", "RequirementSetId").get(set_id) or {}
    any_mode = set_row.get("RequirementSetType") == "REQUIREMENTSET_TEST_ANY"
    requirements = ctx.db.index("Requirements", "RequirementId")
    arguments = ctx.db.group("RequirementArguments", "RequirementId")
    results = []
    for link in ctx.db.group("RequirementSetRequirements", "RequirementSetId").get(set_id, []):
        rid = link.get("RequirementId")
        row = requirements.get(rid) or {}
        kind = row.get("RequirementType")
        args = {a.get("Name"): a.get("Value") for a in arguments.get(rid, [])}
        if kind == "REQUIREMENT_PLAYER_HAS_GIVEN_INFLUENCE_TOKENS":
            value = state["envoys"] >= as_float(args.get("MinimumTokens"), 1)
        elif kind in ("REQUIREMENT_PLAYER_IS_SUZERAIN", "REQUIREMENT_PLAYER_IS_SUZERAIN_BONUS_ENABLED"):
            value = state["suzerain"]
        elif kind in ASSUMED_TRUE:
            value = True
        else:
            return False, f"평가할 수 없는 조건 {kind}"
        if as_bool(row.get("Inverse")):
            value = not value
        results.append(value)
    if not results:
        return True, None
    return (any(results) if any_mode else all(results)), None


def attached_modifiers(ctx: EngineContext, dynamic) -> tuple[list[tuple[str, str]], list[dict]]:
    """[(source_id, attached ModifierId)] granted by the entered envoys/suzerainty,
    plus skipped entries ({source_id, modifier_id, reason})."""
    granted, skipped = [], []
    modifiers = ctx.db.index("Modifiers", "ModifierId")
    links = ctx.db.group("TraitModifiers", "TraitType")
    arguments = ctx.db.group("ModifierArguments", "ModifierId")
    for civ, state in sorted(ctx.player.city_states.items()):
        for trait in city_state_traits(ctx, civ):
            source_id = f"{civ} {trait}"
            for link in links.get(trait, []):
                modifier_id = link.get("ModifierId")
                row = modifiers.get(modifier_id or "") or {}
                _, effect = dynamic(row.get("ModifierType"))
                if effect != "EFFECT_ATTACH_MODIFIER":
                    continue  # effects owned by the city-state itself
                passes, reason = _gate(ctx, row.get("SubjectRequirementSetId"), state)
                if reason:
                    skipped.append({"source_id": source_id, "modifier_id": modifier_id, "reason": reason})
                if not passes:
                    continue
                attached = next((a.get("Value") for a in arguments.get(modifier_id, []) if a.get("Name") == "ModifierId"), None)
                if attached:
                    granted.append((source_id, attached))
    return granted, skipped


def trade_bonus_modifiers(ctx: EngineContext, civilization: str) -> list[str]:
    """Modifiers a city-state grants to trade routes sent to it (…_TO_OTHERS)."""
    modifiers = ctx.db.index("Modifiers", "ModifierId")
    dynamic = ctx.db.index("DynamicModifiers", "ModifierType")
    result = []
    for trait in city_state_traits(ctx, civilization):
        for link in ctx.db.group("TraitModifiers", "TraitType").get(trait, []):
            row = modifiers.get(link.get("ModifierId") or "") or {}
            effect = (dynamic.get(row.get("ModifierType") or "") or {}).get("EffectType")
            if effect == "EFFECT_ADJUST_TRADE_ROUTE_YIELD_TO_OTHERS" and not row.get("SubjectRequirementSetId"):
                result.append(link["ModifierId"])
    return result
