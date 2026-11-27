"""Accuracy bookkeeping: coverage registry, in-game validation cases, data audit.

Status vocabulary (never upgraded without evidence):

* ``검증 완료`` – an in-game observation stored as a validation case matched
  the engine for this item (dynamic; requires a passing case);
* ``미검증`` – implemented from Data.zip values, not yet compared with the game;
* ``부분 구현`` – implemented, but part of the rule (formula, distribution,
  ordering) is an interpretation or uses incomplete map data;
* ``미지원`` – not simulated; the engine reports it instead of guessing.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.config import YIELD_KEYS
from app.core.exceptions import ApplicationError
from app.repositories import locations
from app.repositories.rules_db_repository import RulesDB
from app.services.modifier_engine import ECONOMY_RELATED_UNSUPPORTED, SUPPORTED_EFFECTS
from app.services.requirement_engine import supported_requirement_types

COVERAGE: list[dict[str, Any]] = [
    {"id": "terrain", "label": "지형·언덕 기본 산출", "status": "미검증", "basis": "XML Terrain_YieldChanges (catalog와 값 일치 확인)"},
    {"id": "feature", "label": "지형 특성 산출", "status": "미검증", "basis": "XML Feature_YieldChanges"},
    {"id": "resource", "label": "자원 산출 + 발견 기술", "status": "미검증", "basis": "Resource_YieldChanges, Resources.PrereqTech"},
    {"id": "improvement", "label": "시설 산출 + 기술/사회제도 보너스 + 시설 인접", "status": "미검증", "basis": "Improvement_YieldChanges, Improvement_BonusYieldChanges, Improvement_Adjacencies"},
    {"id": "city_center", "label": "도심 최소 산출", "status": "미검증", "basis": "game_rules.json (GlobalParameters YIELD_*_CITY_TERRAIN_REPLACE 2/1과 동일 값)"},
    {"id": "specialists", "label": "특수지구 전문가 슬롯·산출", "status": "미검증", "basis": "Buildings.CitizenSlots, District_CitizenYieldChanges"},
    {"id": "district_adjacency", "label": "특수지구 인접 보너스", "status": "부분 구현", "basis": "District_Adjacencies; 강변 판정은 타일 강 플래그 근사, 소수 보너스는 내림 처리(미검증)"},
    {"id": "buildings", "label": "건물·불가사의 직접 산출 + 지구 산출 복제", "status": "미검증", "basis": "Building_YieldChanges, Building_YieldDistrictCopies"},
    {"id": "population_yields", "label": "인구당 과학·문화", "status": "부분 구현", "basis": "확장팩 규칙 게임은 GlobalParameters(몰려드는 폭풍 50%/30%)를 1/256 고정소수점으로 사용(인구 11 → 문화 3.27, 게임 표시 3.2). 기본 규칙 게임은 game_rules.json(0.5/0.2), simulation.population_yields_source로 전환 가능"},
    {"id": "policies", "label": "정책 카드 효과", "status": "부분 구현", "basis": "PolicyModifiers → 지원 효과만 적용, 나머지는 감사 로그"},
    {"id": "government", "label": "정부 효과·슬롯", "status": "부분 구현", "basis": "Government_SlotCounts, GovernmentModifiers (불가사의 건설 보너스만 해석)"},
    {"id": "traits", "label": "문명·지도자 특성", "status": "부분 구현", "basis": "CivilizationTraits/LeaderTraits → TraitModifiers, 고유 유닛/건물/지구 대체"},
    {"id": "technology_civic", "label": "기술·사회제도 Modifier", "status": "부분 구현", "basis": "TechnologyModifiers, CivicModifiers"},
    {"id": "religion", "label": "판테온·신앙", "status": "부분 구현", "basis": "BeliefModifiers; 자국 도시가 판테온을 따른다고 가정, 종교 전파 미지원"},
    {"id": "governors", "label": "총독", "status": "미지원", "basis": "Data.zip에 Governors 테이블 없음 (확장팩 규칙)"},
    {"id": "trade_routes", "label": "교역로", "status": "미지원", "basis": "교역로 생성·경로·산출 시뮬레이션 없음"},
    {"id": "amenities", "label": "쾌적도 (요구량·상태 효과)", "status": "부분 구현", "basis": "CITY_POP_PER_AMENITY, Happinesses; 사치 자원 분배 순서는 근사"},
    {"id": "housing", "label": "주거공간", "status": "부분 구현", "basis": "CITY_POPULATION_* 파라미터, Buildings/Districts/Improvements.Housing; 호수 구분 데이터 없음"},
    {"id": "growth", "label": "식량 소비·성장 요구량", "status": "미검증", "basis": "CITY_FOOD_CONSUMPTION_PER_POPULATION, CITY_GROWTH_THRESHOLD/MULTIPLIER/EXPONENT (표준 속도)"},
    {"id": "growth_modifiers", "label": "성장 수정치 결합", "status": "부분 구현", "basis": "쾌적도·Modifier %를 합산 후 주거공간 배율 적용 (결합 방식 미검증)"},
    {"id": "growth_speed", "label": "표준 외 게임 속도의 성장 비용", "status": "미지원", "basis": "데이터에 성장 배율 없음"},
    {"id": "starvation", "label": "기아 인구 감소", "status": "미지원", "basis": "규칙 미확인 — 설정으로만 활성화"},
    {"id": "production_cost", "label": "생산 비용 (기본·이전 생산 수·게임 속도)", "status": "부분 구현", "basis": "Cost, COST_PROGRESSION_PREVIOUS_COPIES, GameSpeeds.CostMultiplier; 특수지구 비용 증가 공식은 미지원"},
    {"id": "production_bonus", "label": "생산 % 보너스 (정책·특성)", "status": "미검증", "basis": "EFFECT_ADJUST_*_PRODUCTION Modifier"},
    {"id": "overflow", "label": "생산 초과분", "status": "부분 구현", "basis": "기본 carry_to_next(턴당 1개 완성, 초과분 다음 항목) — 게임 대조 전"},
    {"id": "projects", "label": "특수지구 프로젝트 산출 전환", "status": "미검증", "basis": "Project_YieldConversions; 위인 점수·완료 효과 미지원"},
    {"id": "research", "label": "연구 비용·진행", "status": "부분 구현", "basis": "Cost × GameSpeeds.CostMultiplier; 유레카/영감은 사용자가 획득 표시 시 Boosts.Boost% 적용"},
    {"id": "maintenance", "label": "유지비", "status": "부분 구현", "basis": "Buildings/Districts/Units.Maintenance 합계; 감면 Modifier 미지원"},
    {"id": "borders", "label": "문화 영토 확장", "status": "미지원", "basis": "타일 선택 알고리즘 미확인 — 수동 영토 지정 유지"},
    {"id": "great_people", "label": "위인·걸작·관광", "status": "미지원", "basis": "범위 밖"},
    {"id": "optimizer", "label": "시민 자동 배치", "status": "부분 구현", "basis": "결정론적 가중치 최적화 — 게임 내 도시 관리 AI와 동일하지 않음"},
    {"id": "turn_order", "label": "턴 처리 순서", "status": "부분 구현", "basis": "시뮬레이터 정의 순서 (게임 내부 순서 데이터 없음)"},
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_cases() -> list[dict]:
    path = locations.validation_cases_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [case for case in data.get("cases", []) if isinstance(case, dict)] if isinstance(data, dict) else []


def save_cases(cases: list[dict]) -> None:
    path = locations.validation_cases_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": "civ6-validation-cases", "cases": cases}, ensure_ascii=False, indent=2),
                    encoding="utf-8")


EXPECTED_FIELDS = (*YIELD_KEYS, "population", "food_surplus", "food_required", "housing", "amenity_score",
                   "production_cost", "turns_to_growth")


def add_case(payload: dict) -> dict:
    expected = payload.get("expected") or {}
    if not isinstance(expected, dict) or not expected:
        raise ApplicationError(422, "expected에 게임에서 확인한 값을 하나 이상 입력하세요.")
    unknown = set(expected) - set(EXPECTED_FIELDS)
    if unknown:
        raise ApplicationError(422, f"지원하지 않는 비교 항목: {', '.join(sorted(unknown))}")
    for key, value in expected.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ApplicationError(422, f"{key} 값은 숫자여야 합니다.")
    if not payload.get("city_id"):
        raise ApplicationError(422, "city_id가 필요합니다.")
    case = {
        "id": "case-" + uuid4().hex[:8],
        "name": str(payload.get("name") or "게임 대조 사례")[:80],
        "city_id": payload["city_id"],
        "scenario_id": payload.get("scenario_id"),
        "expected": expected,
        "tolerance": float(payload.get("tolerance", 0.05)),
        "notes": str(payload.get("notes") or "")[:500],
        "created_at": _now(),
    }
    cases = load_cases()
    cases.append(case)
    save_cases(cases)
    return case


def delete_case(case_id: str) -> dict:
    cases = load_cases()
    remaining = [case for case in cases if case.get("id") != case_id]
    if len(remaining) == len(cases):
        raise ApplicationError(404, "Validation case not found.")
    save_cases(remaining)
    return {"deleted": case_id}


def actual_values(city_eval: dict) -> dict[str, float | None]:
    totals = city_eval["yields"]["totals"]
    growth = city_eval["growth"]
    queue = city_eval["production"]["queue"]
    return {
        **{key: totals[key] for key in YIELD_KEYS},
        "population": city_eval["population"],
        "food_surplus": growth["food_surplus"],
        "food_required": growth["food_required"],
        "housing": city_eval["housing"]["value"],
        "amenity_score": city_eval["amenities"]["score"],
        "production_cost": queue[0]["cost"] if queue else None,
        "turns_to_growth": growth["turns_to_growth"],
    }


def compare_case(case: dict, city_eval: dict | None) -> dict:
    if city_eval is None:
        return {**case, "status": "실행 불가", "reason": "도시를 찾을 수 없음", "fields": []}
    actual = actual_values(city_eval)
    fields = []
    for key, expected in case["expected"].items():
        value = actual.get(key)
        ok = value is not None and abs(float(value) - float(expected)) <= case.get("tolerance", 0.05) + 1e-9
        fields.append({"field": key, "expected": expected, "actual": value,
                       "difference": None if value is None else round(float(value) - float(expected), 4), "match": ok})
    passed = all(item["match"] for item in fields)
    return {**case, "status": "일치" if passed else "불일치", "fields": fields}


def coverage(results: list[dict] | None = None) -> list[dict]:
    items = [dict(item) for item in COVERAGE]
    if results:
        passed_yields = [r for r in results if r.get("status") == "일치"
                         and any(f["field"] in YIELD_KEYS for f in r.get("fields", []))]
        if passed_yields:
            for item in items:
                if item["id"] in ("terrain", "feature", "resource", "improvement", "city_center", "buildings"):
                    item["evidence"] = f"게임 대조 사례 {len(passed_yields)}건 일치 (해당 도시 구성에 한함)"
    return items


# --------------------------------------------------------------- data audit
def data_audit(db: RulesDB, catalog: dict) -> dict[str, Any]:
    path = locations.data_audit_file()
    report = {}
    if path.exists():
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = {"error": "data_audit_report.json을 읽을 수 없음"}
    entity_checks = []
    for label, catalog_key, table, column in (
        ("Technologies", "technologies", "Technologies", "TechnologyType"),
        ("Civics", "civics", "Civics", "CivicType"),
        ("Buildings", "buildings", "Buildings", "BuildingType"),
        ("Districts", "districts", "Districts", "DistrictType"),
        ("Improvements", "improvements", "Improvements", "ImprovementType"),
        ("Features", "features", "Features", "FeatureType"),
        ("Terrains", "terrains", "Terrains", "TerrainType"),
        ("Resources", "resources", "Resources", "ResourceType"),
        ("Policies", "policies", "Policies", "PolicyType"),
        ("Leaders", "leaders", "Leaders", "LeaderType"),
    ):
        catalog_ids = {item.get("id") for item in catalog.get(catalog_key, []) if isinstance(item, dict)}
        db_ids = {row.get(column) for row in db.rows(table) if row.get(column)}
        entity_checks.append({
            "entity": label, "catalog": len(catalog_ids), "xml": len(db_ids),
            "missing_in_catalog": sorted(db_ids - catalog_ids)[:30],
            "extra_in_catalog": sorted(i for i in catalog_ids - db_ids if i)[:30],
        })
    for label, table, column in (("Units", "Units", "UnitType"), ("Governments", "Governments", "GovernmentType"),
                                 ("Projects", "Projects", "ProjectType"), ("Beliefs", "Beliefs", "BeliefType"),
                                 ("Civilizations", "Civilizations", "CivilizationType")):
        raw = catalog.get("raw_tables", {}).get(f"{table}::{table}", [])
        catalog_ids = {row.get(column) for row in raw if isinstance(row, dict) and row.get(column)}
        db_ids = {row.get(column) for row in db.rows(table) if row.get(column)}
        entity_checks.append({"entity": label, "catalog": len(catalog_ids), "xml": len(db_ids),
                              "missing_in_catalog": sorted(db_ids - catalog_ids)[:30], "extra_in_catalog": []})
    # Prerequisite links
    techs = set(db.index("Technologies", "TechnologyType"))
    civics = set(db.index("Civics", "CivicType"))
    broken_prereqs = []
    for table, column in (("Buildings", "BuildingType"), ("Units", "UnitType"), ("Districts", "DistrictType"),
                          ("Improvements", "ImprovementType"), ("Policies", "PolicyType"), ("Projects", "ProjectType")):
        for row in db.rows(table):
            if row.get("PrereqTech") and row["PrereqTech"] not in techs:
                broken_prereqs.append({"entity": row.get(column), "missing": row["PrereqTech"]})
            if row.get("PrereqCivic") and row["PrereqCivic"] not in civics:
                broken_prereqs.append({"entity": row.get(column), "missing": row["PrereqCivic"]})
    # Modifier links
    modifiers = db.index("Modifiers", "ModifierId")
    dynamics = db.index("DynamicModifiers", "ModifierType")
    requirement_sets = db.index("RequirementSets", "RequirementSetId")
    link_tables = ("PolicyModifiers", "GovernmentModifiers", "BuildingModifiers", "TraitModifiers", "BeliefModifiers",
                   "DistrictModifiers", "TechnologyModifiers", "CivicModifiers", "ImprovementModifiers")
    missing_modifiers, effect_status = [], {"supported": 0, "economy_unsupported": 0, "out_of_scope": 0}
    by_source: dict[str, dict[str, int]] = {}
    for table in link_tables:
        stats = by_source.setdefault(table, {"links": 0, "supported": 0, "economy_unsupported": 0, "out_of_scope": 0, "missing": 0})
        for row in db.rows(table):
            stats["links"] += 1
            modifier = modifiers.get(row.get("ModifierId") or "")
            if modifier is None:
                stats["missing"] += 1
                missing_modifiers.append({"table": table, "modifier": row.get("ModifierId")})
                continue
            effect = (dynamics.get(modifier.get("ModifierType") or "") or {}).get("EffectType")
            key = "supported" if effect in SUPPORTED_EFFECTS else (
                "economy_unsupported" if effect in ECONOMY_RELATED_UNSUPPORTED else "out_of_scope")
            stats[key] += 1
            effect_status[key] += 1
    missing_sets = []
    for modifier in modifiers.values():
        for column in ("OwnerRequirementSetId", "SubjectRequirementSetId"):
            set_id = modifier.get(column)
            if set_id and set_id not in requirement_sets:
                missing_sets.append({"modifier": modifier.get("ModifierId"), "set": set_id})
    requirement_types: dict[str, int] = {}
    for row in db.rows("Requirements"):
        requirement_types[row.get("RequirementType") or "?"] = requirement_types.get(row.get("RequirementType") or "?", 0) + 1
    supported_types = set(supported_requirement_types())
    return {
        "rules_db": {"available": db.available, **db.meta},
        "xml_vs_catalog": report,
        "entities": entity_checks,
        "broken_prerequisites": broken_prereqs[:50],
        "modifier_links": {"by_source": by_source, "totals": effect_status,
                           "missing_modifier_rows": missing_modifiers[:50],
                           "missing_requirement_sets": missing_sets[:50]},
        "requirements": {
            "types_in_data": len(requirement_types),
            "supported_types": sorted(supported_types),
            "unsupported_types": sorted(
                ({"type": t, "count": n} for t, n in requirement_types.items() if t not in supported_types),
                key=lambda item: -item["count"],
            ),
        },
        "global_parameters_used": {
            name: db.global_parameters.get(name)
            for name in ("CITY_FOOD_CONSUMPTION_PER_POPULATION", "CITY_GROWTH_THRESHOLD", "CITY_GROWTH_MULTIPLIER",
                         "CITY_GROWTH_EXPONENT", "CITY_HOUSING_LEFT_ZERO_GROWTH", "CITY_HOUSING_LEFT_25PCT_GROWTH",
                         "CITY_HOUSING_LEFT_50PCT_GROWTH", "CITY_POPULATION_RIVER_LAKE", "CITY_POPULATION_COAST",
                         "CITY_POPULATION_NO_WATER", "CITY_POPULATION_AQUEDUCT_BOOST", "CITY_POPULATION_AQUEDUCT_MIN",
                         "CITY_POP_PER_AMENITY", "CITY_AMENITIES_FOR_FREE", "DISTRICT_POPULATION_REQUIRED_PER",
                         "SCIENCE_PERCENTAGE_YIELD_PER_POP", "CULTURE_PERCENTAGE_YIELD_PER_POP")
        },
        "known_discrepancies": [
            "catalog.json은 XML 자식 요소를 읽지 못해 Modifier·Requirement·GameSpeeds 등 181개 테이블이 빈 행으로 저장되어 있었습니다. 시뮬레이션 엔진은 rules_db.json(직접 재추출)을 사용합니다.",
            "catalog.json에 GlobalParameters가 없습니다(XML이 <Replace> 행 사용).",
            "인구당 과학·문화: game_rules.json 0.5/0.2 vs GlobalParameters 70%/30%.",
            "기존 인접 계산은 상업 중심지 강 보너스를 '강 인접 타일 수'만큼 반복 적용하고 도심을 인접 특수지구로 세지 않았습니다. 새 엔진은 이를 수정했습니다(/api/calculate는 하위 호환을 위해 기존 방식 유지).",
            "기존 계산은 산출량 행이 없는 지형(예: TERRAIN_GRASS_MOUNTAIN)에 이름 일부가 같은 대체 지형 산출(초원 식량 2)을 적용했습니다. 새 엔진은 XML에 정의된 지형이면 0으로 계산합니다.",
            "기존 계산은 특수지구 타일 작업 시 지형 산출까지 더했습니다. 새 엔진은 건물이 제공하는 전문가 슬롯과 District_CitizenYieldChanges만 사용합니다.",
        ],
    }
