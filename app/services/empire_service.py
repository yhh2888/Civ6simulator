"""Empire-level settings: government, policy cards, civilization/leader, religion.

Stored in ``game_state["empire"]`` (preserved by saves and named snapshots).
Validation uses Governments / Government_SlotCounts / GovernmentSlots /
Policies / ObsoletePolicies / CivilizationLeaders / Beliefs from rules_db.

Governors: with expansion rules (rules_db_xp2.json) promotions are applied
through GovernorPromotionModifiers once the governor is established in its
city; the base-game data has no Governors tables.

World Congress: ``empire["world_congress"]`` lists resolution results; those
whose effect attaches a modifier to a target player apply when the player is
the target (targets_me).
"""

from __future__ import annotations

import math
from typing import Any

from app.core.exceptions import ApplicationError
from app.repositories.rules_db_repository import RulesDB
from app.services.modifier_engine import ECONOMY_RELATED_UNSUPPORTED, SUPPORTED_EFFECTS
from app.services.sim_context import as_bool, as_float

GOVERNOR_STATUS = {
    "available": False,
    "reason": (
        "지금 적용된 규칙(기본 게임)에는 Governors / GovernorPromotions 표가 없습니다. "
        "총독은 확장팩 규칙이라, 확장팩 세이브를 불러오면 확장팩 규칙 DB(rules_db_xp2.json)로 계산합니다. "
        "세이브를 불러왔는데도 이 문구가 보이면 첫 실행 화면의 데이터 추출에서 DLC 폴더가 함께 잡혔는지 확인하세요."
    ),
}


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def sanitize_empire(empire: dict) -> dict:
    """Shape-check stored empire settings (used by snapshots)."""
    result: dict[str, Any] = {}
    for key in ("government", "civilization", "leader", "pantheon"):
        value = empire.get(key)
        result[key] = value if isinstance(value, str) and value else None
    slots = empire.get("policy_slots") if isinstance(empire.get("policy_slots"), dict) else {}
    result["policy_slots"] = {
        str(slot): [p for p in policies if isinstance(p, str) and p]
        for slot, policies in slots.items()
        if isinstance(policies, list)
    }
    result["religion_beliefs"] = [b for b in empire.get("religion_beliefs") or [] if isinstance(b, str) and b]
    result["completed_projects"] = [p for p in empire.get("completed_projects") or [] if isinstance(p, str) and p]
    copies = empire.get("unit_copies") if isinstance(empire.get("unit_copies"), dict) else {}
    result["unit_copies"] = {str(k): int(v) for k, v in copies.items() if _finite(v) and v >= 0}
    treasury = empire.get("treasury") if isinstance(empire.get("treasury"), dict) else {}
    result["treasury"] = {k: float(treasury.get(k, 0.0)) for k in ("Gold", "Faith") if _finite(treasury.get(k, 0.0))}
    boosted = empire.get("boosted") if isinstance(empire.get("boosted"), dict) else {}
    result["boosted"] = {
        category: [item for item in boosted.get(category) or [] if isinstance(item, str)]
        for category in ("technology", "civic")
    }
    result["enforce_prerequisites"] = empire.get("enforce_prerequisites", True) is not False
    result["city_states"] = _clean_city_states(empire.get("city_states"))
    result["world_congress"] = _clean_congress(empire.get("world_congress"))
    return result


def _clean_congress(value: Any) -> list[dict]:
    result = []
    if not isinstance(value, list):
        return result
    for item in value[:20]:
        if not isinstance(item, dict) or not str(item.get("resolution") or "").startswith("WC_RES_"):
            continue
        effect = item.get("effect")
        if effect not in (1, 2):
            continue
        entry = {"resolution": item["resolution"], "effect": effect, "targets_me": item.get("targets_me") is True,
                 "enabled": item.get("enabled", True) is not False}
        for key in ("target_kind", "target_player", "votes", "source"):
            if key in item:
                entry[key] = item[key]
        result.append(entry)
    return result


def _clean_city_states(value: Any) -> dict[str, dict]:
    """{civilization: {"envoys": 0..99, "suzerain": bool}}; empty entries dropped."""
    result: dict[str, dict] = {}
    if not isinstance(value, dict):
        return result
    for civ, entry in value.items():
        if not isinstance(civ, str) or not civ.startswith("CIVILIZATION_") or not isinstance(entry, dict):
            continue
        envoys = entry.get("envoys", 0)
        envoys = int(envoys) if _finite(envoys) else 0
        envoys = max(0, min(99, envoys))
        suzerain = entry.get("suzerain") is True
        if envoys or suzerain:
            result[civ] = {"envoys": envoys, "suzerain": suzerain}
    return result


def slot_counts(db: RulesDB, government: str | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in db.rows("Government_SlotCounts"):
        if row.get("GovernmentType") == government and row.get("GovernmentSlotType"):
            counts[row["GovernmentSlotType"]] = counts.get(row["GovernmentSlotType"], 0) + int(as_float(row.get("NumSlots")))
    return counts


def modifier_support(db: RulesDB, modifier_ids: list[str]) -> dict[str, Any]:
    supported, economy, other, missing = [], [], [], []
    modifiers = db.index("Modifiers", "ModifierId")
    dynamics = db.index("DynamicModifiers", "ModifierType")
    for modifier_id in modifier_ids:
        row = modifiers.get(modifier_id)
        effect = (dynamics.get((row or {}).get("ModifierType") or "") or {}).get("EffectType")
        if not row or not effect:
            missing.append(modifier_id)
        elif as_bool(row.get("RunOnce")):
            other.append({"id": modifier_id, "effect": effect, "note": "1회성"})
        elif effect in SUPPORTED_EFFECTS:
            supported.append({"id": modifier_id, "effect": effect})
        elif effect in ECONOMY_RELATED_UNSUPPORTED:
            economy.append({"id": modifier_id, "effect": effect})
        else:
            other.append({"id": modifier_id, "effect": effect})
    total = len(modifier_ids)
    if total == 0:
        status = "효과 없음"
    elif len(supported) == total:
        status = "효과 해석 지원 (미검증)"
    elif supported:
        status = "부분 지원"
    elif economy:
        status = "경제 효과 미지원"
    else:
        status = "도시 경제 외 효과 (미시뮬레이션)"
    return {"status": status, "total": total, "supported": supported,
            "economy_unsupported": economy, "out_of_scope": other, "missing": missing}


def _linked(db: RulesDB, table: str, column: str, entity_id: str) -> list[str]:
    return [r["ModifierId"] for r in db.group(table, column).get(entity_id, []) if r.get("ModifierId")]


def empire_options(db: RulesDB, game: dict) -> dict[str, Any]:
    civics = set(game.get("civics") or [])
    empire = sanitize_empire(game.get("empire") or {})
    governments = []
    for row in db.rows("Governments"):
        gid = row.get("GovernmentType")
        if not gid:
            continue
        prereq = row.get("PrereqCivic")
        governments.append({
            "id": gid, "name": row.get("Name"), "prereq_civic": prereq,
            "available": not prereq or prereq in civics,
            "slots": slot_counts(db, gid),
            "support": modifier_support(db, _linked(db, "GovernmentModifiers", "GovernmentType", gid)),
        })
    obsolete: dict[str, list[str]] = {}
    for row in db.rows("ObsoletePolicies"):
        if row.get("PolicyType") and row.get("ObsoletePolicy"):
            obsolete.setdefault(row["PolicyType"], []).append(row["ObsoletePolicy"])
    policies = []
    policy_rows = db.index("Policies", "PolicyType")
    for row in db.rows("Policies"):
        pid = row.get("PolicyType")
        if not pid:
            continue
        prereq = row.get("PrereqCivic")
        replaced_by = [p for p in obsolete.get(pid, []) if (policy_rows.get(p, {}).get("PrereqCivic") in civics)]
        policies.append({
            "id": pid, "name": row.get("Name"), "slot": row.get("GovernmentSlotType"),
            "prereq_civic": prereq, "available": not prereq or prereq in civics,
            "obsoleted_by": obsolete.get(pid, []), "obsolete_now": bool(replaced_by),
            "support": modifier_support(db, _linked(db, "PolicyModifiers", "PolicyType", pid)),
        })
    leaders_by_civ: dict[str, list[str]] = {}
    for row in db.rows("CivilizationLeaders"):
        if row.get("CivilizationType") and row.get("LeaderType"):
            leaders_by_civ.setdefault(row["CivilizationType"], []).append(row["LeaderType"])
    civilizations = []
    for row in db.rows("Civilizations"):
        cid = row.get("CivilizationType")
        if not cid or row.get("StartingCivilizationLevelType") not in (None, "CIVILIZATION_LEVEL_FULL_CIV"):
            continue
        if cid not in leaders_by_civ:
            continue
        traits = [r["TraitType"] for r in db.rows("CivilizationTraits") if r.get("CivilizationType") == cid]
        modifiers = [m for t in traits for m in _linked(db, "TraitModifiers", "TraitType", t)]
        unique = {
            "units": [r["UnitType"] for r in db.rows("Units") if r.get("TraitType") in traits],
            "buildings": [r["BuildingType"] for r in db.rows("Buildings") if r.get("TraitType") in traits],
            "districts": [r["DistrictType"] for r in db.rows("Districts") if r.get("TraitType") in traits],
            "improvements": [r["ImprovementType"] for r in db.rows("Improvements") if r.get("TraitType") in traits],
        }
        civilizations.append({"id": cid, "name": row.get("Name"), "leaders": leaders_by_civ.get(cid, []),
                              "traits": traits, "unique": unique, "support": modifier_support(db, modifiers)})
    leaders = []
    for leader_list in leaders_by_civ.values():
        for leader in leader_list:
            traits = [r["TraitType"] for r in db.rows("LeaderTraits") if r.get("LeaderType") == leader]
            modifiers = [m for t in traits for m in _linked(db, "TraitModifiers", "TraitType", t)]
            unique_units = [r["UnitType"] for r in db.rows("Units") if r.get("TraitType") in traits]
            leaders.append({"id": leader, "traits": traits, "unique_units": unique_units,
                            "support": modifier_support(db, modifiers)})
    beliefs = []
    for row in db.rows("Beliefs"):
        bid = row.get("BeliefType")
        if bid:
            beliefs.append({"id": bid, "name": row.get("Name"), "class": row.get("BeliefClassType"),
                            "support": modifier_support(db, _linked(db, "BeliefModifiers", "BeliefType", bid))})
    return {
        "current": empire,
        "governments": governments,
        "policy_slot_types": [r.get("GovernmentSlotType") for r in db.rows("GovernmentSlots")],
        "policies": policies,
        "civilizations": sorted(civilizations, key=lambda c: c["id"]),
        "leaders": sorted(leaders, key=lambda l: l["id"]),
        "beliefs": beliefs,
        "governors": _governor_options(db),
        "resolutions": [
            {"id": r.get("ResolutionType"), "target_kind": r.get("TargetKind"),
             "effects": {
                 str(which): [e.get("ModifierId") for e in db.rows("ResolutionEffects")
                                             if e.get("ResolutionType") == r.get("ResolutionType")
                                             and str(e.get("WhichEffect")) == str(which)]
                 for which in (1, 2)}}
            for r in db.rows("Resolutions")],
    }


def _governor_options(db: RulesDB) -> dict:
    governors = db.rows("Governors")
    if not governors:
        return GOVERNOR_STATUS
    sets = db.group("GovernorPromotionSets", "GovernorType")
    promotions = db.index("GovernorPromotions", "GovernorPromotionType")
    prereqs = db.group("GovernorPromotionPrereqs", "GovernorPromotionType")
    links = db.group("GovernorPromotionModifiers", "GovernorPromotionType")
    return {
        "available": True,
        "establish_turns": int(db.param("GOVERNOR_BASE_TURNS_TO_ESTABLISH", 5) or 5),
        "list": [
            {"id": g.get("GovernorType"), "name": g.get("Name"),
             "promotions": [
                 {"id": row.get("GovernorPromotion"),
                  "level": (promotions.get(row.get("GovernorPromotion")) or {}).get("Level"),
                  "column": (promotions.get(row.get("GovernorPromotion")) or {}).get("Column"),
                  "base": as_bool((promotions.get(row.get("GovernorPromotion")) or {}).get("BaseAbility")),
                  "prereqs": [p.get("PrereqGovernorPromotion") for p in prereqs.get(row.get("GovernorPromotion"), [])],
                  "support": modifier_support(db, [l.get("ModifierId") for l in links.get(row.get("GovernorPromotion"), [])])}
                 for row in sets.get(g.get("GovernorType"), [])]}
            for g in governors],
    }


def validate_empire_update(db: RulesDB, game: dict, payload: dict) -> tuple[dict, list[str]]:
    empire = sanitize_empire(game.get("empire") or {})
    warnings: list[str] = []
    for key, value in payload.items():
        if key in ("government", "civilization", "leader", "pantheon"):
            empire[key] = value or None
        elif key == "policy_slots" and value is not None:
            if not isinstance(value, dict):
                raise ApplicationError(422, "policy_slots는 {슬롯: [정책]} 객체여야 합니다.")
            empire["policy_slots"] = {str(k): list(v or []) for k, v in value.items()}
        elif key in ("religion_beliefs", "completed_projects") and value is not None:
            empire[key] = [str(v) for v in value]
        elif key == "unit_copies" and value is not None:
            empire["unit_copies"] = {str(k): max(0, int(as_float(v))) for k, v in value.items()}
        elif key == "boosted" and value is not None:
            empire["boosted"] = {c: [str(i) for i in (value.get(c) or [])] for c in ("technology", "civic")}
        elif key == "enforce_prerequisites" and value is not None:
            empire["enforce_prerequisites"] = bool(value)
        elif key == "treasury" and value is not None:
            empire["treasury"] = {k: as_float(value.get(k)) for k in ("Gold", "Faith")}
        elif key == "city_states" and value is not None:
            if not isinstance(value, dict):
                raise ApplicationError(422, "city_states는 {문명: {envoys, suzerain}} 객체여야 합니다.")
            unknown = [c for c in value if db.index("Civilizations", "CivilizationType").get(c, {}).get(
                "StartingCivilizationLevelType") != "CIVILIZATION_LEVEL_CITY_STATE"]
            if unknown:
                raise ApplicationError(422, f"도시국가가 아닙니다: {', '.join(map(str, unknown[:3]))}")
            empire["city_states"] = _clean_city_states(value)
        elif key == "world_congress" and value is not None:
            known = db.index("Resolutions", "ResolutionType")
            if not isinstance(value, list) or any(not isinstance(i, dict) or i.get("resolution") not in known for i in value):
                raise ApplicationError(422, "world_congress는 Resolutions 표의 결의 목록이어야 합니다.")
            empire["world_congress"] = _clean_congress(value)
    enforce = empire.get("enforce_prerequisites", True)
    civics = set(game.get("civics") or [])
    governments = db.index("Governments", "GovernmentType")
    if empire["government"]:
        row = governments.get(empire["government"])
        if row is None:
            raise ApplicationError(422, f"알 수 없는 정부 {empire['government']}")
        if row.get("PrereqCivic") and row["PrereqCivic"] not in civics:
            message = f"정부 {empire['government']}에는 사회제도 {row['PrereqCivic']}가 필요합니다."
            if enforce:
                raise ApplicationError(409, message)
            warnings.append(message + " (선행 조건 강제 무시)")
    counts = slot_counts(db, empire["government"]) if empire["government"] else {}
    slot_types = {r.get("GovernmentSlotType"): as_bool(r.get("AllowsAnyPolicy")) for r in db.rows("GovernmentSlots")}
    policies = db.index("Policies", "PolicyType")
    seen: set[str] = set()
    for slot, chosen in empire["policy_slots"].items():
        if slot not in slot_types:
            raise ApplicationError(422, f"알 수 없는 정책 슬롯 {slot}")
        if len(chosen) > counts.get(slot, 0):
            raise ApplicationError(409, f"{empire['government'] or '정부 없음'}의 {slot} 슬롯은 {counts.get(slot, 0)}개입니다.")
        for policy in chosen:
            row = policies.get(policy)
            if row is None:
                raise ApplicationError(422, f"알 수 없는 정책 {policy}")
            if policy in seen:
                raise ApplicationError(409, f"정책 {policy}가 중복 장착되었습니다.")
            seen.add(policy)
            if not slot_types.get(slot) and row.get("GovernmentSlotType") != slot:
                raise ApplicationError(409, f"{policy}({row.get('GovernmentSlotType')})는 {slot} 슬롯에 장착할 수 없습니다.")
            if row.get("PrereqCivic") and row["PrereqCivic"] not in civics:
                message = f"정책 {policy}에는 사회제도 {row['PrereqCivic']}가 필요합니다."
                if enforce:
                    raise ApplicationError(409, message)
                warnings.append(message + " (선행 조건 강제 무시)")
    for row in db.rows("ObsoletePolicies"):
        if row.get("PolicyType") in seen and policies.get(row.get("ObsoletePolicy") or "", {}).get("PrereqCivic") in civics:
            warnings.append(f"{row['PolicyType']}는 {row['ObsoletePolicy']}가 해금되어 게임에서는 폐기됩니다.")
    if empire["civilization"] and empire["civilization"] not in db.index("Civilizations", "CivilizationType"):
        raise ApplicationError(422, f"알 수 없는 문명 {empire['civilization']}")
    if empire["leader"]:
        if empire["leader"] not in db.index("Leaders", "LeaderType"):
            raise ApplicationError(422, f"알 수 없는 지도자 {empire['leader']}")
        pairs = {(r.get("CivilizationType"), r.get("LeaderType")) for r in db.rows("CivilizationLeaders")}
        if empire["civilization"] and (empire["civilization"], empire["leader"]) not in pairs:
            warnings.append(f"{empire['leader']}는 데이터상 {empire['civilization']}의 지도자가 아닙니다.")
    beliefs = db.index("Beliefs", "BeliefType")
    if empire["pantheon"]:
        row = beliefs.get(empire["pantheon"])
        if row is None or row.get("BeliefClassType") != "BELIEF_CLASS_PANTHEON":
            raise ApplicationError(422, f"{empire['pantheon']}는 판테온 신앙이 아닙니다.")
    for belief in empire["religion_beliefs"]:
        if belief not in beliefs:
            raise ApplicationError(422, f"알 수 없는 신앙 {belief}")
    return empire, warnings
