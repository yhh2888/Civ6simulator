"""City yield ledger: a line-by-line account of why a city produces what it does.

The ledger re-reads the attributed result of ``yield_breakdown_service`` and
turns every contribution into one ledger line, in calculation order:

    작업 타일 → 전문가 → 인구 → 특수지구 → 건물·불가사의 → 도시 Modifier
    → 사용자 정의 → 프로젝트 → [소계] → % 보정 → [합계]

Each line carries

* the yields it adds (and a running total),
* the formula in words,
* its data source: Data.zip file / table / key with the raw XML rows, or the
  game_rules.json path, or "사용자 입력",
* for modifiers: link table → Modifiers → DynamicModifiers → arguments and the
  evaluated owner/subject RequirementSets (why it applied).

It also lists what did *not* count (unworked tiles, hidden resources, unmet
prerequisites, inactive or unsupported modifiers) and the growth, housing,
amenity and production sub-ledgers. A reconciliation check proves that the
lines add up to the totals shown elsewhere in the app.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from app.config import YIELD_KEYS
from app.services.modifier_engine import EffectBook, run_modifiers
from app.services.sim_context import EngineContext, add_into, as_float, clean_yields, zero_yields
from app.services.turn_simulation_service import evaluate

YL = {"Food": "식량", "Production": "생산력", "Gold": "금", "Science": "과학", "Culture": "문화", "Faith": "신앙"}
SECTION_LABELS = {
    "tiles": "작업 타일",
    "specialists": "전문가",
    "population": "인구 기본 산출",
    "districts": "특수지구",
    "buildings": "건물·불가사의",
    "great_works": "걸작 (도시에 넣은 것)",
    "trade_routes": "교역로 (출발 도시 몫)",
    "incoming_routes": "수입 교역로 (다른 문명·도시국가 상인이 이 도시로)",
    "build_bonus": "현재 생산 항목 보너스 (건물·특수지구·불가사의 / 유닛)",
    "modifiers": "정책·특성 등 도시 Modifier",
    "custom": "사용자 정의 효과",
    "projects": "프로젝트 산출 전환",
    "percent": "% 보정",
}
SECTION_ORDER = ["tiles", "specialists", "population", "districts", "buildings", "great_works", "trade_routes", "incoming_routes",
                 "build_bonus", "modifiers", "custom", "projects"]
LINK_TABLES = {
    "government": "GovernmentModifiers", "policy": "PolicyModifiers", "trait": "TraitModifiers",
    "technology": "TechnologyModifiers", "civic": "CivicModifiers", "belief": "BeliefModifiers",
    "building": "BuildingModifiers", "district": "DistrictModifiers", "improvement": "ImprovementModifiers",
    "governor": "GovernorPromotionModifiers", "congress": "ResolutionEffects",
}
XML_REFS = {
    "terrain": ("Terrain_YieldChanges", "TerrainType"),
    "feature": ("Feature_YieldChanges", "FeatureType"),
    "resource": ("Resource_YieldChanges", "ResourceType"),
    "improvement": ("Improvement_YieldChanges", "ImprovementType"),
    "improvement_bonus": ("Improvement_BonusYieldChanges", "Id"),
    "improvement_adjacency": ("Adjacency_YieldChanges", "ID"),
    "adjacency": ("Adjacency_YieldChanges", "ID"),
    "specialist": ("District_CitizenYieldChanges", "DistrictType"),
    "building_base": ("Building_YieldChanges", "BuildingType"),
    "district_copy": ("Building_YieldDistrictCopies", "BuildingType"),
    "project_conversion": ("Project_YieldConversions", "ProjectType"),
}
MODIFIER_KINDS = {"modifier", "modifier_adjacency", "adjacency_percent", "district_base", "modifier_percent",
                  "city_flat", "per_district"}
EPS = 1e-6


def _fmt(value: float) -> str:
    text = f"{value:+.2f}".rstrip("0").rstrip(".")
    return "+0" if text in ("+", "-", "+-0", "-0") else text


def yields_text(values: dict) -> str:
    parts = [f"{YL[k]} {_fmt(values[k])}" for k in YIELD_KEYS if abs(values.get(k, 0.0)) > EPS]
    return ", ".join(parts) or "0"


def _raw_rows(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in row.items() if k != "_file"} for row in rows]


def xml_source(ctx: EngineContext, table: str, column: str, value: Any) -> dict:
    rows = ctx.db.group(table, column).get(str(value), []) if value is not None else []
    if not rows:
        return {"origin": "Data.zip", "table": table, "key": f"{column}={value}", "file": None, "rows": [],
                "note": "rules_db.json에서 행을 찾지 못함"}
    return {"origin": "Data.zip", "file": f"{rows[0].get('_file')}.xml", "table": table,
            "key": f"{column}={value}", "rows": _raw_rows(rows)}


def modifier_source(ctx: EngineContext, attribution: dict) -> dict:
    modifier_id = attribution.get("modifier_id")
    row = ctx.db.index("Modifiers", "ModifierId").get(modifier_id or "", {})
    dynamic = ctx.db.index("DynamicModifiers", "ModifierType").get(row.get("ModifierType") or "", {})
    args = _raw_rows(ctx.db.group("ModifierArguments", "ModifierId").get(modifier_id or "", []))
    link_table = LINK_TABLES.get(attribution.get("source_kind"), "")
    chain = attribution.get("chain") or [modifier_id]
    link_rows = [
        r for r in ctx.db.group(link_table, "ModifierId").get(chain[0], [])
    ] if link_table else []
    return {
        "origin": "Data.zip",
        "file": f"{row.get('_file')}.xml" if row else None,
        "table": "Modifiers",
        "key": f"ModifierId={modifier_id}",
        "rows": _raw_rows([row]) if row else [],
        "modifier": {
            "source": f"{attribution.get('source_kind')} {attribution.get('source_id')}",
            "link": {"table": link_table, "file": f"{link_rows[0].get('_file')}.xml" if link_rows else None,
                     "rows": _raw_rows(link_rows)},
            "chain": chain,
            "modifier_type": row.get("ModifierType"),
            "collection": dynamic.get("CollectionType"),
            "effect": dynamic.get("EffectType"),
            "arguments": args,
            "requirements": attribution.get("trace") or {},
        },
    }


def config_source(path: str, note: str | None = None) -> dict:
    return {"origin": "game_rules.json", "file": "app/config/game_rules.json", "table": None, "key": path,
            "rows": [], "note": note}


def _modifier_formula(attribution: dict, args: list[dict]) -> str:
    arg_text = ", ".join(f"{a.get('Name')}={a.get('Value')}" for a in args if a.get("Name"))
    chain = " → ".join(attribution.get("chain") or [attribution.get("modifier_id")])
    return f"{attribution.get('source_kind')} {attribution.get('source_id')} → {chain} ({attribution.get('effect')}; {arg_text})"


class _Ledger:
    def __init__(self, ctx: EngineContext):
        self.ctx = ctx
        self.lines: list[dict] = []
        self.running = zero_yields()

    def add(self, section: str, group: str, kind: str, label: str, yields: dict, formula: str,
            source: dict, location: str | None = None, extra: dict | None = None) -> None:
        values = clean_yields(yields)
        add_into(self.running, values)
        line = {
            "no": len(self.lines) + 1,
            "section": section,
            "section_label": SECTION_LABELS.get(section, section),
            "group": group,
            "kind": kind,
            "label": label,
            "location": location,
            "yields": values,
            "formula": formula,
            "source": source,
            "running": clean_yields(self.running),
        }
        if extra:
            line.update(extra)
        self.lines.append(line)


def _component_source(ctx: EngineContext, component: dict, origin_note: str | None = None) -> dict:
    kind = component.get("kind")
    attribution = component.get("attribution")
    if attribution:
        return modifier_source(ctx, attribution)
    if kind in XML_REFS:
        table, column = XML_REFS[kind]
        return xml_source(ctx, table, column, component.get("id"))
    if kind == "configured_rule":
        return config_source(f"tile_yield_modifiers[{component.get('id')}]")
    if kind == "center_minimum":
        source = config_source("city.city_center_min_yields")
        source["note"] = (
            "Data.zip GlobalParameters YIELD_FOOD_CITY_TERRAIN_REPLACE="
            f"{ctx.db.global_parameters.get('YIELD_FOOD_CITY_TERRAIN_REPLACE')}, "
            f"YIELD_PRODUCTION_CITY_TERRAIN_REPLACE={ctx.db.global_parameters.get('YIELD_PRODUCTION_CITY_TERRAIN_REPLACE')}"
        )
        return source
    if kind == "population":
        if "GlobalParameters" in (component.get("source") or ""):
            return {"origin": "Data.zip", "file": "GlobalParameters.xml", "table": "GlobalParameters",
                    "key": "SCIENCE/CULTURE_PERCENTAGE_YIELD_PER_POP", "rows": []}
        return config_source("city.population_yields",
                             "Data.zip GlobalParameters 값(과학 70%, 문화 30%)과 다름 — simulation.population_yields_source로 전환 가능")
    if kind == "great_work":
        return xml_source(ctx, "GreatWork_YieldChanges", "GreatWorkType", component.get("id"))
    if kind == "cs_trade":
        return xml_source(ctx, "Modifiers", "ModifierId", component.get("id"))
    if kind == "trade_district":
        source = xml_source(ctx, "District_TradeRouteYields", "DistrictType", component.get("id"))
        source["note"] = f"열 {component.get('column')} 사용"
        return source
    if kind == "event_yields":
        return {"origin": "세이브", "file": None, "table": None, "key": "map plot yield changes", "rows": [],
                "note": "RandomEvent_Yields 확률로 정해진 결과를 세이브에서 그대로 읽음"}
    if kind == "user_tile":
        return {"origin": "사용자 입력", "file": None, "table": None,
                "key": f"city.tile_adjustments[{component.get('id')}]", "rows": [], "note": "게임 데이터가 아님"}
    if kind in ("user_effect",):
        return {"origin": "사용자 입력", "file": None, "table": None, "key": f"city.custom_effects[{component.get('id')}]",
                "rows": [], "note": "게임 데이터가 아님"}
    return {"origin": origin_note or "엔진", "file": None, "table": None, "key": component.get("id"), "rows": []}


def _leaf_formula(ctx: EngineContext, component: dict, source: dict) -> str:
    kind = component.get("kind")
    values = component.get("yields", {})
    if component.get("attribution"):
        path = _modifier_formula(component["attribution"], source.get("modifier", {}).get("arguments", []))
        return f"{component['calc']} ← {path}" if component.get("calc") else path
    if kind in ("terrain", "feature", "resource", "improvement", "building_base", "specialist"):
        rows = source.get("rows") or []
        if rows:
            row_text = "; ".join(f"{r.get('YieldType', '').replace('YIELD_', '')} {r.get('YieldChange')}" for r in rows)
            extra = ""
            if kind == "terrain" and component.get("source") and "언덕" in component.get("source", ""):
                extra = " (+ 수동 언덕 설정 보정)"
            return f"{source.get('table')} [{component.get('id')}]: {row_text}{extra}"
        if kind == "terrain":
            return component.get("source") or "지형 산출 없음"
    if kind == "improvement_bonus":
        rows = source.get("rows") or [{}]
        prereq = rows[0].get("PrereqTech") or rows[0].get("PrereqCivic")
        return f"{component.get('improvement')}: {prereq} 보유 → {yields_text(values)} (Improvement_BonusYieldChanges Id {component.get('id')})"
    if kind == "center_minimum":
        return f"도심 최소 식량·생산력에 맞춰 보충 → {yields_text(values)}"
    return component.get("label") or kind


def _add_component_leaves(ledger: _Ledger, section: str, group: str, components: list[dict], location: str | None = None) -> None:
    for component in components:
        if component.get("kind") == "resource_hidden":
            continue
        source = _component_source(ledger.ctx, component)
        label = component.get("label")
        if component.get("kind") == "building_base":
            label = f"{component.get('id')} 기본 산출"
        ledger.add(section, group, component.get("kind"), label, component.get("yields", {}),
                   _leaf_formula(ledger.ctx, component, source), source, location or component.get("tile"))


def _trade_route_ledger(ctx: EngineContext, result: dict) -> list[dict]:
    """One entry per route: why it yields what it does, and how long it runs."""
    routes = []
    for component in result.get("components", []):
        if component.get("category") != "trade_routes":
            continue
        lines = []
        for detail in component.get("details") or []:
            source = _component_source(ctx, detail)
            lines.append({"label": detail.get("label"), "yields": clean_yields(detail.get("yields") or {}),
                          "calc": detail.get("calc") or (f"{detail.get('column')} 열" if detail.get("column") else ""),
                          "source": source})
        routes.append({"label": component.get("label"), "destination": component.get("destination"),
                       "kind": component.get("route_kind"), "yields": clean_yields(component.get("yields") or {}),
                       "timing": component.get("timing"), "lines": lines})
    return routes


def build_ledger(ctx: EngineContext, city_id: str, book: EffectBook | None = None,
                 state: dict | None = None) -> dict[str, Any]:
    book = book or run_modifiers(ctx)
    state = state or evaluate(ctx, book)
    city_eval = state["cities"][city_id]
    result = city_eval["yields"]
    ledger = _Ledger(ctx)
    components = result["components"]

    # 1. tiles (worked tiles, one group per tile)
    for component in components:
        if component["category"] != "tiles":
            continue
        _add_component_leaves(ledger, "tiles", component["label"], component.get("details") or [], component.get("tile"))
    # 2. specialists
    for component in components:
        if component["category"] == "specialists":
            source = xml_source(ctx, "District_CitizenYieldChanges", "DistrictType", component.get("id"))
            ledger.add("specialists", component["label"], "specialist", component["label"], component["yields"],
                       f"{component['label']} × District_CitizenYieldChanges[{component.get('id')}]", source, component.get("tile"))
    # 3. population
    for component in components:
        if component["category"] == "population":
            source = _component_source(ctx, component)
            ledger.add("population", component["label"], "population", component["label"], component["yields"],
                       f"{component['label']} = {yields_text(component['yields'])}", source)
    # 4. districts, 5. buildings (one group per district/building)
    for section in ("districts", "buildings", "great_works", "trade_routes", "incoming_routes", "build_bonus"):
        for component in components:
            if component["category"] != section:
                continue
            details = component.get("details") or []
            if details:
                _add_component_leaves(ledger, section, component["label"], details, component.get("tile"))
            else:
                ledger.add(section, component["label"], component["kind"], component["label"], component["yields"],
                           component["label"], _component_source(ctx, component), component.get("tile"))
    # 6-8. modifiers, custom, projects
    for section in ("modifiers", "custom", "projects"):
        for component in components:
            if component["category"] == section:
                source = _component_source(ctx, component)
                ledger.add(section, component["label"], component["kind"], component["label"], component["yields"],
                           _leaf_formula(ctx, component, source), source)
    subtotal = dict(ledger.running)

    # 9. percentage lines: each percentage applies to the subtotal of its yield.
    percent_component = next((c for c in components if c["category"] == "percent"), None)
    for item in (percent_component or {}).get("details") or []:
        yields = zero_yields()
        keys = [k for k in YIELD_KEYS if k != "Food"] if item.get("yield") == "NonFood" else [item.get("yield")]
        pct = as_float(item.get("effective", item.get("percent")))
        for key in keys:
            if key in yields:
                yields[key] = subtotal[key] * pct / 100.0
        shown = (f"{as_float(item.get('percent')):+g}%→{pct:+.3f}%" if abs(pct - as_float(item.get("percent"))) > 1e-9
                 else f"{pct:+g}%")
        formula = "; ".join(
            f"{YL[k]} 소계 {subtotal[k]:.2f} × {shown} = {_fmt(yields[k])}"
            for k in keys if k in yields and abs(subtotal[k]) > EPS
        ) or f"{shown} (소계 0)"
        if item.get("attribution"):
            source = modifier_source(ctx, item["attribution"])
        elif item.get("source"):
            source = {"origin": "Data.zip", "file": "Happinesses.xml", "table": "Happinesses",
                      "key": f"HappinessType={city_eval['amenities'].get('happiness')}",
                      "rows": _raw_rows([r for r in ctx.db.rows("Happinesses")
                                         if r.get("HappinessType") == city_eval["amenities"].get("happiness")])}
        else:
            source = {"origin": "사용자 입력", "file": None, "table": None, "key": "city.custom_effects[].percent", "rows": []}
        ledger.add("percent", "% 보정 (소계 기준, 합산 적용)", "percent", item.get("label"), yields, formula, source)

    # Reconciliation
    totals = result["totals"]
    diff = {k: round(ledger.running[k] - totals[k], 6) for k in YIELD_KEYS}
    section_totals = {}
    for line in ledger.lines:
        bucket = section_totals.setdefault(line["section"], zero_yields())
        add_into(bucket, line["yields"])
    category_check = {
        section: {k: round(section_totals.get(section, zero_yields())[k] - result["by_category"].get(section, {}).get(k, 0.0), 6)
                  for k in YIELD_KEYS}
        for section in set(section_totals) | set(result["by_category"])
    }
    reconciled = all(abs(v) < 1e-4 for v in diff.values()) and all(
        abs(v) < 1e-4 for values in category_check.values() for v in values.values()
    )

    return {
        "city_id": city_id,
        "name": city_eval["name"],
        "turn": state["turn"],
        "game_speed": state["game_speed"],
        "data_origin": {
            "base_yields": "Data.zip (rules_db.json)" if ctx.db.available else "catalog.json (rules_db.json 없음)",
            "rules_db": ctx.db.meta,
            "config": "app/config/game_rules.json",
        },
        "lines": ledger.lines,
        "sections": [
            {"id": s, "label": SECTION_LABELS[s], "yields": clean_yields(section_totals[s]),
             "lines": sum(1 for l in ledger.lines if l["section"] == s)}
            for s in SECTION_ORDER + ["percent"] if s in section_totals
        ],
        "subtotal": clean_yields(subtotal),
        "totals": totals,
        "reconciliation": {"ok": reconciled, "difference": diff, "by_section": category_check,
                           "note": "장부 줄의 합계와 엔진 합계(도시 화면 숫자)를 비교한 결과"},
        "trade_routes": _trade_route_ledger(ctx, result),
        "excluded": _excluded(ctx, book, city_id, city_eval),
        "growth": _growth_ledger(ctx, city_eval),
        "housing": city_eval["housing"],
        "amenities": {k: v for k, v in city_eval["amenities"].items()},
        "production": _production_ledger(ctx, city_eval),
        "assumptions": [
            "% 보정은 같은 산출의 모든 % 값을 더한 뒤 소계에 한 번 적용합니다(합산 방식은 게임 대조 전).",
            "턴 처리 순서와 성장 수정치 결합 방식은 시뮬레이터가 정한 해석입니다.",
            "출처가 game_rules.json 또는 사용자 입력인 줄은 Data.zip 값이 아닙니다.",
        ],
    }


def _excluded(ctx: EngineContext, book: EffectBook, city_id: str, city_eval: dict) -> list[dict]:
    items: list[dict] = []
    result = city_eval["yields"]
    for tile in result["tiles"]:
        if tile.get("worked") or tile.get("district"):
            for item in tile.get("excluded") or []:
                if tile.get("worked"):
                    items.append({"category": "적용 안 된 타일 요소", "location": tile["key"], **item})
        if not tile.get("worked") and not tile.get("district") and not tile.get("city_center"):
            items.append({"category": "작업하지 않는 타일", "location": tile["key"], "kind": "unworked_tile",
                          "label": f"타일 ({tile['key']}) {tile.get('terrain')}", "yields": tile["yields"],
                          "reason": "시민이 배치되지 않아 합계에 포함되지 않음 (참고용 산출)"})
        if tile.get("district") and not tile.get("citizens") and tile.get("specialist_slots"):
            items.append({"category": "비어 있는 전문가 슬롯", "location": tile["key"], "kind": "specialist_slot",
                          "label": f"{tile['district']} 슬롯 {tile['specialist_slots']}개", "yields": tile.get("per_citizen"),
                          "reason": "전문가가 배치되지 않음 (시민 1명당 산출 표시)"})
    for district in result["districts"]:
        for rule in district.get("inactive_rules") or []:
            items.append({"category": "적용 안 된 인접 규칙", "location": district["key"], "kind": "adjacency_rule",
                          "label": f"{district['id']} {rule.get('rule')}", "reason": rule.get("reason"),
                          "table": "Adjacency_YieldChanges", "key": rule.get("rule")})
    city_name = city_eval["name"]
    for item in book.inactive:
        subject = item.get("subject", "")
        if subject == "플레이어" or subject.endswith(city_name) or f"도시 {city_name}" == subject:
            items.append({"category": "조건 미충족 Modifier", "kind": "modifier_inactive",
                          "label": f"{item['source_kind']} {item['source_id']}", "key": item["modifier_id"],
                          "effect": item.get("effect"), "reason": item.get("reason"), "table": "Modifiers"})
    view = ctx.city_views[city_id]
    own_sources = set(view.buildings + view.wonders) | {t.get("district") for t in view.district_tiles}
    out_of_scope = 0
    for item in book.skipped:
        relevant = item["source_kind"] in ("government", "policy", "trait", "technology", "civic", "belief") or (
            item["source_kind"] in ("building", "district") and item["source_id"] in own_sources)
        if not relevant:
            continue
        if item["category"] == "out_of_scope":
            out_of_scope += 1
            continue
        items.append({"category": "미지원으로 제외된 Modifier", "kind": "modifier_skipped",
                      "label": f"{item['source_kind']} {item['source_id']}", "key": item["modifier_id"],
                      "effect": item.get("effect"), "reason": item.get("reason"), "table": "Modifiers"})
    if out_of_scope:
        items.append({"category": "미지원으로 제외된 Modifier", "kind": "out_of_scope_count",
                      "label": f"도시 경제와 무관한 효과 {out_of_scope}건 (전투·외교 등)",
                      "reason": "산출량에 영향 없음 — 감사 로그(계산 근거 탭)에서 전체 확인"})
    if result.get("unassigned_citizens"):
        items.append({"category": "배치되지 않은 시민", "kind": "unassigned",
                      "label": f"시민 {result['unassigned_citizens']}명",
                      "reason": "타일·전문가에 배치되지 않은 시민은 산출 없음으로 계산 (GOLD_PERCENTAGE_YIELD_PER_UNASSIGNED_POP는 의미 미확인으로 미적용)"})
    return items


def _growth_ledger(ctx: EngineContext, city_eval: dict) -> list[dict]:
    lines = _growth_lines(ctx, city_eval)
    for line in lines:
        if isinstance(line.get("value"), float):
            line["value"] = round(line["value"], 4)
    return lines


def _growth_lines(ctx: EngineContext, city_eval: dict) -> list[dict]:
    g = city_eval["growth"]
    a = city_eval["amenities"]
    lines = [
        {"label": "식량 산출", "value": g["food_yield"], "formula": "위 산출 장부의 식량 합계", "source": "산출 장부"},
        {"label": "식량 소비", "value": -g["food_consumption"],
         "formula": f"인구 {g['population']} × {g['food_consumption'] / max(1, g['population']):g}", "source": g["consumption_source"]},
        {"label": "식량 잉여", "value": g["food_surplus"], "formula": "산출 − 소비", "source": "계산"},
    ]
    for modifier in g["growth_modifiers"]:
        lines.append({"label": f"성장 수정치: {modifier['label']}", "value": modifier["percent"], "unit": "%",
                      "formula": "잉여가 양수일 때만 적용, 합산",
                      "source": "Happinesses.GrowthModifier" if "쾌적도" in modifier["label"] else (modifier.get("attribution") or {}).get("modifier_id")})
    lines.append({"label": "주거공간 배율", "value": g["housing_multiplier"], "unit": "×", "formula": g["housing_reason"],
                  "source": "GlobalParameters CITY_HOUSING_LEFT_*"})
    surplus = g["food_surplus"]
    if surplus > 0:
        formula = f"{surplus:g} × (1 {g['growth_modifier_percent']:+g}%) × {g['housing_multiplier']:g}"
    else:
        formula = "잉여가 0 이하이면 수정치 없이 그대로 반영"
    lines.append({"label": "이번 턴 식량 변화", "value": g["effective_food_change"], "formula": formula, "source": "계산"})
    lines.append({"label": "저장 식량", "value": g["stored_food"], "formula": "현재 저장량", "source": "도시 상태"})
    lines.append({"label": "다음 턴 저장 식량(성장 전)", "value": round(max(0.0, g["stored_food"] + g["effective_food_change"]), 4),
                  "formula": "저장 + 변화 (0 미만은 0)", "source": "계산"})
    lines.append({"label": "성장 필요 식량", "value": g["food_required"], "formula": g["required_formula"],
                  "source": "GlobalParameters CITY_GROWTH_*"})
    lines.append({"label": "성장까지", "value": g["turns_to_growth"], "unit": "턴",
                  "formula": "ceil((필요 − 저장) ÷ 변화)", "source": "계산"})
    lines.append({"label": "쾌적도 상태", "value": a["score"], "formula": f"공급 {a['supply']:g} − 요구 {a['required']} ({a['required_formula']}) → {a.get('happiness')}",
                  "source": "Happinesses.xml"})
    return lines


def _production_ledger(ctx: EngineContext, city_eval: dict) -> dict:
    production = city_eval["production"]
    queue = production["queue"]
    head = queue[0] if queue else None
    return {
        "per_turn": production["per_turn"],
        "overflow_policy": ctx.settings.get("overflow_policy"),
        "current": None if head is None else {
            "target_id": head["target_id"], "category": head["category"],
            "cost_steps": head["cost_breakdown"], "cost": head["cost"],
            "bonus": head["production_bonus"], "bonus_percent": head["production_bonus_percent"],
            "effective_production": head["effective_production"],
            "formula": (f"생산력 {production['per_turn']:g}"
                        + (f" + 건설 항목 보너스 {head.get('production_flat', 0):g}" if head.get("production_flat") else "")
                        + f" × (1 {head['production_bonus_percent']:+g}%) = {head['effective_production']:g} / 턴"),
            "progress": head["progress"], "remaining": head["remaining"], "eta": head["estimated_turns"],
        },
        "queue": [{"target_id": e["target_id"], "cost": e["cost"], "eta": e["estimated_turns"]} for e in queue],
    }


def ledger_csv(ledger: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["도시", ledger["name"], "턴", ledger["turn"], "검산", "일치" if ledger["reconciliation"]["ok"] else "불일치"])
    writer.writerow([])
    header = ["번호", "구분", "그룹", "항목", "위치", "계산식", "출처", "파일", "테이블", "키",
              *[YL[k] for k in YIELD_KEYS], *[f"누계 {YL[k]}" for k in YIELD_KEYS]]
    writer.writerow(header)
    for line in ledger["lines"]:
        source = line["source"]
        writer.writerow([
            line["no"], line["section_label"], line["group"], line["label"], line.get("location") or "",
            line["formula"], source.get("origin"), source.get("file") or "", source.get("table") or "", source.get("key") or "",
            *[round(line["yields"][k], 4) for k in YIELD_KEYS], *[round(line["running"][k], 4) for k in YIELD_KEYS],
        ])
    writer.writerow(["", "소계", "", "", "", "", "", "", "", "", *[round(ledger["subtotal"][k], 4) for k in YIELD_KEYS]])
    writer.writerow(["", "합계", "", "", "", "", "", "", "", "", *[round(ledger["totals"][k], 4) for k in YIELD_KEYS]])
    writer.writerow([])
    writer.writerow(["제외 항목"])
    writer.writerow(["분류", "위치", "항목", "사유", "테이블", "키", *[YL[k] for k in YIELD_KEYS]])
    for item in ledger["excluded"]:
        yields = item.get("yields") or {}
        writer.writerow([item.get("category"), item.get("location") or "", item.get("label"), item.get("reason") or "",
                         item.get("table") or "", item.get("key") or "", *[yields.get(k, "") for k in YIELD_KEYS]])
    writer.writerow([])
    writer.writerow(["성장 장부"])
    writer.writerow(["항목", "값", "계산", "출처"])
    for line in ledger["growth"]:
        writer.writerow([line["label"], line["value"], line["formula"], line.get("source") or ""])
    return "﻿" + buffer.getvalue()


def empire_yield_ledger(ctx: EngineContext, yield_key: str) -> dict[str, Any]:
    """One yield across every city: per-city section totals and the lines
    that produce it (same engine run as the city ledgers)."""
    if yield_key not in YIELD_KEYS:
        from app.core.exceptions import ApplicationError

        raise ApplicationError(422, f"yield는 {', '.join(YIELD_KEYS)} 중 하나입니다.")
    book = run_modifiers(ctx)
    state = evaluate(ctx, book)
    cities, columns, grand = [], [], 0.0
    for city_id in sorted(ctx.city_views, key=lambda c: ctx.city_views[c].city.get("name", c)):
        ledger = build_ledger(ctx, city_id, book, state)
        sections = {}
        lines = []
        for line in ledger["lines"]:
            value = line["yields"].get(yield_key, 0.0)
            if abs(value) <= EPS:
                continue
            sections[line["section"]] = sections.get(line["section"], 0.0) + value
            lines.append({"section": line["section"], "group": line["group"], "label": line["label"],
                          "location": line["location"], "value": round(value, 4), "formula": line["formula"]})
        for section in sections:
            if section not in columns:
                columns.append(section)
        total = ledger["totals"].get(yield_key, 0.0)
        grand += total
        cities.append({"city_id": city_id, "name": ledger["name"], "population": state["cities"][city_id]["yields"]["population"],
                       "total": total, "sections": {k: round(v, 4) for k, v in sections.items()}, "lines": lines,
                       "reconciled": ledger["reconciliation"]["ok"]})
    order = SECTION_ORDER + ["percent"]
    columns.sort(key=lambda s: order.index(s) if s in order else len(order))
    return {"yield": yield_key, "yield_label": YL[yield_key], "turn": state["turn"],
            "columns": [{"id": s, "label": SECTION_LABELS.get(s, s)} for s in columns],
            "cities": cities, "total": round(grand, 4),
            "section_totals": {s: round(sum(c["sections"].get(s, 0.0) for c in cities), 4) for s in columns}}
