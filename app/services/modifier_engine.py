"""Data-driven Civ VI modifier resolution.

Pipeline (all from rules_db.json):

    source (policy, government, trait, building, district, tech, civic, belief,
    improvement) -> *Modifiers link table* -> Modifiers row -> DynamicModifiers
    (CollectionType + EffectType) -> ModifierArguments

For every modifier the engine

1. evaluates ``OwnerRequirementSetId`` against the owner,
2. expands the collection into subjects,
3. evaluates ``SubjectRequirementSetId`` against each subject,
4. applies the effect if it is supported, otherwise records it as skipped.

Applied effects are collected in an :class:`EffectBook`; the yield engine reads
the book and attributes every number to its originating modifier. Nothing in
this module mutates game state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.requirement_engine import (
    RequirementEvaluator,
    Subject,
    city_subject,
    district_subject,
    player_subject,
    plot_subject,
)
from app.services.sim_context import EngineContext, as_bool, as_float, yield_key

SOURCE_LINKS = (
    ("government", "GovernmentModifiers", "GovernmentType"),
    ("policy", "PolicyModifiers", "PolicyType"),
    ("trait", "TraitModifiers", "TraitType"),
    ("technology", "TechnologyModifiers", "TechnologyType"),
    ("civic", "CivicModifiers", "CivicType"),
    ("belief", "BeliefModifiers", "BeliefType"),
    ("building", "BuildingModifiers", "BuildingType"),
    ("district", "DistrictModifiers", "DistrictType"),
    ("improvement", "ImprovementModifiers", "ImprovementType"),
)

SUPPORTED_EFFECTS = {
    "EFFECT_ADJUST_CITY_YIELD_CHANGE": "도시 산출량 고정 증감",
    "EFFECT_ADJUST_CITY_YIELD_MODIFIER": "도시 산출량 % 증감",
    "EFFECT_ADJUST_PLOT_YIELD": "타일 산출량 증감",
    "EFFECT_ADJUST_BUILDING_YIELD_CHANGE": "건물 산출량 고정 증감",
    "EFFECT_ADJUST_BUILDING_YIELD_MODIFIER": "건물 산출량 % 증감",
    "EFFECT_ADJUST_DISTRICT_YIELD_MODIFIER": "특수지구 인접 보너스 % 증감",
    "EFFECT_ADJUST_DISTRICT_BASE_YIELD_CHANGE": "특수지구 기본 산출량 증감",
    "EFFECT_ADJUST_CITY_YIELD_PER_DISTRICT": "전문 특수지구당 도시 산출량",
    "EFFECT_DISTRICT_ADJACENCY": "추가 특수지구 인접 규칙(인접 특수지구)",
    "EFFECT_TERRAIN_ADJACENCY": "추가 특수지구 인접 규칙(지형)",
    "EFFECT_FEATURE_ADJACENCY": "추가 특수지구 인접 규칙(지형 특성)",
    "EFFECT_ADJUST_POLICY_HOUSING": "주거공간 증감",
    "EFFECT_ADJUST_POLICY_AMENITY": "쾌적도 증감",
    "EFFECT_ADJUST_CITY_GROWTH": "성장률 % 증감",
    "EFFECT_ADJUST_UNIT_TAG_ERA_PRODUCTION": "유닛(승급 분류·시대) 생산 % 보너스",
    "EFFECT_ADJUST_UNIT_PRODUCTION": "특정 유닛 생산 % 보너스",
    "EFFECT_ADJUST_DISTRICT_PRODUCTION": "특수지구 생산 % 보너스",
    "EFFECT_ADJUST_BUILDING_PRODUCTION": "건물 생산 % 보너스",
    "EFFECT_ADJUST_WONDER_ERA_PRODUCTION": "시대별 불가사의 생산 % 보너스",
    "EFFECT_ADJUST_FLAT_BONUS": "정부 고유 보너스(불가사의 건설만 지원)",
    "EFFECT_ATTACH_MODIFIER": "Modifier 부착(연쇄 해석)",
    "EFFECT_ADJUST_TRADE_ROUTE_YIELD": "교역로 산출량 증감(모든 교역로, 입력한 교역로에만)",
    "EFFECT_ADJUST_CITY_PRODUCTION_BUILDING": "건물(불가사의 포함) 생산 중일 때 생산력 +N (현재 생산 항목 보너스)",
    "EFFECT_ADJUST_CITY_PRODUCTION_DISTRICT": "특수지구 생산 중일 때 생산력 +N (현재 생산 항목 보너스)",
    "EFFECT_ADJUST_CITY_PRODUCTION_UNIT": "유닛 생산 중일 때 생산력 +N (현재 생산 항목 보너스)",
    "EFFECT_ADJUST_TRADE_ROUTE_YIELD_FOR_DOMESTIC": "국내 교역로 산출량 증감(입력한 교역로에만)",
    "EFFECT_ADJUST_TRADE_ROUTE_YIELD_FOR_INTERNATIONAL": "국제 교역로 산출량 증감(입력한 교역로에만)",
    "EFFECT_ADJUST_CITY_TRADE_ROUTE_YIELD_FOR_DOMESTIC": "도시의 국내 교역로 산출량 증감(입력한 교역로에만)",
    "EFFECT_ADJUST_CITY_TRADE_ROUTE_YIELD_FOR_INTERNATIONAL": "도시의 국제 교역로 산출량 증감(입력한 교역로에만)",
}

# Effects that clearly influence city economy but are not simulated yet.
ECONOMY_RELATED_UNSUPPORTED = {
    "EFFECT_ADJUST_TRADE_ROUTE_YIELD_TO_OTHERS",
    "EFFECT_ADJUST_TRADE_ROUTE_CAPACITY",
    "EFFECT_ADJUST_GREAT_PERSON_POINTS",
    "EFFECT_ADJUST_DISTRICT_GREAT_PERSON_POINTS",
    "EFFECT_ADJUST_GREAT_PERSON_POINTS_PERCENT",
    "EFFECT_ADJUST_CITY_GREATWORK_YIELD",
    "EFFECT_ADJUST_CITY_TOURISM",
    "EFFECT_ADJUST_BONUS_RATE",
    "EFFECT_ADJUST_ACCUMULATING_BONUS",
    "EFFECT_ADJUST_PLAYER_YIELD_CHANGE_PER_TRIBUTARY",
    "EFFECT_ADJUST_PLAYER_YIELD_MODIFIER_PER_TRIBUTARY",
    "EFFECT_ADD_RELIGIOUS_BUILDING",
    "EFFECT_ADJUST_UNIT_MAINTENANCE_DISCOUNT",
    "EFFECT_ADJUST_PLOT_PURCHASE_COST",
    "EFFECT_ADJUST_PLAYER_GOVERNMENT_SLOT_TYPE",
    "EFFECT_ADJUST_CITY_YIELD_PER_POPULATION",
}

SUPPORTED_COLLECTIONS = {
    "COLLECTION_OWNER",
    "COLLECTION_PLAYER_CITIES",
    "COLLECTION_ALL_CITIES",
    "COLLECTION_PLAYER_CAPITAL_CITY",
    "COLLECTION_CITY_PLOT_YIELDS",
    "COLLECTION_PLAYER_PLOT_YIELDS",
    "COLLECTION_SINGLE_PLOT_YIELDS",
    "COLLECTION_PLAYER_DISTRICTS",
    "COLLECTION_CITY_DISTRICTS",
    "COLLECTION_MAJOR_PLAYERS",
    "COLLECTION_ALL_PLAYERS",
}


@dataclass
class EffectRecord:
    effect: str
    modifier_id: str
    source_kind: str
    source_id: str
    subject: Subject
    data: dict[str, Any]
    owner_city_id: str | None = None
    chain: list[str] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)

    def attribution(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "modifier_id": self.modifier_id,
            "effect": self.effect,
            "chain": list(self.chain),
            "trace": self.trace,
        }


@dataclass
class EffectBook:
    records: list[EffectRecord] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    inactive: list[dict[str, Any]] = field(default_factory=list)

    def for_effect(self, effect: str) -> list[EffectRecord]:
        return [record for record in self.records if record.effect == effect]

    def city_records(self, city_id: str, effect: str) -> list[EffectRecord]:
        return [
            record
            for record in self.records
            if record.effect == effect and record.subject.city_id == city_id
        ]

    def plot_records(self, x: int, y: int) -> list[EffectRecord]:
        return [
            record
            for record in self.records
            if record.effect == "EFFECT_ADJUST_PLOT_YIELD"
            and record.subject.x == x
            and record.subject.y == y
        ]

    def summary(self) -> dict[str, Any]:
        by_effect: dict[str, int] = {}
        for record in self.records:
            by_effect[record.effect] = by_effect.get(record.effect, 0) + 1
        skipped_by_reason: dict[str, int] = {}
        for item in self.skipped:
            skipped_by_reason[item["category"]] = skipped_by_reason.get(item["category"], 0) + 1
        return {
            "applied_effects": len(self.records),
            "applied_by_effect": dict(sorted(by_effect.items())),
            "skipped": len(self.skipped),
            "skipped_by_category": skipped_by_reason,
            "inactive_conditions": len(self.inactive),
        }


def split_values(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def yield_pairs(args: dict[str, str]) -> list[tuple[str, float]]:
    yields = split_values(args.get("YieldType"))
    amounts = split_values(args.get("Amount"))
    pairs = []
    for index, yield_id in enumerate(yields):
        key = yield_key(yield_id)
        if key is None:
            continue
        amount_text = amounts[index] if index < len(amounts) else (amounts[-1] if amounts else "0")
        pairs.append((key, as_float(amount_text)))
    return pairs


class ModifierEngine:
    def __init__(self, ctx: EngineContext):
        self.ctx = ctx
        self.requirements = RequirementEvaluator(ctx)
        self.book = EffectBook()

    # ------------------------------------------------------------ data access
    def modifier_row(self, modifier_id: str) -> dict | None:
        return self.ctx.db.index("Modifiers", "ModifierId").get(modifier_id)

    def modifier_args(self, modifier_id: str) -> dict[str, str]:
        args: dict[str, str] = {}
        for row in self.ctx.db.group("ModifierArguments", "ModifierId").get(modifier_id, []):
            if row.get("Name"):
                args[row["Name"]] = row.get("Value")
        return args

    def dynamic(self, modifier_type: str | None) -> tuple[str | None, str | None]:
        row = self.ctx.db.index("DynamicModifiers", "ModifierType").get(modifier_type or "")
        if not row:
            return None, None
        return row.get("CollectionType"), row.get("EffectType")

    # ---------------------------------------------------------------- sources
    def sources(self) -> list[tuple[str, str, Subject, str]]:
        """Return (source_kind, source_id, owner_subject, modifier_id)."""
        ctx, player = self.ctx, self.ctx.player
        result: list[tuple[str, str, Subject, str]] = []
        player_owned = {
            "government": [player.government] if player.government else [],
            "policy": player.policies,
            "trait": sorted(player.traits),
            "technology": sorted(player.technologies),
            "civic": sorted(player.civics),
            "belief": ([player.pantheon] if player.pantheon else []) + player.beliefs,
        }
        links = {kind: (table, column) for kind, table, column in SOURCE_LINKS}
        for kind, ids in player_owned.items():
            table, column = links[kind]
            grouped = ctx.db.group(table, column)
            for source_id in ids:
                for row in grouped.get(source_id, []):
                    if row.get("ModifierId"):
                        result.append((kind, source_id, player_subject(), row["ModifierId"]))
        if player.city_states:
            from app.services.city_state_service import attached_modifiers

            granted, skipped = attached_modifiers(ctx, self.dynamic)
            for source_id, modifier_id in granted:
                result.append(("city_state", source_id, player_subject(), modifier_id))
            for item in skipped:
                self._skip("requirement_unsupported", "city_state", item["source_id"], item["modifier_id"], item["reason"])
        building_links = ctx.db.group("BuildingModifiers", "BuildingType")
        district_links = ctx.db.group("DistrictModifiers", "DistrictType")
        improvement_links = ctx.db.group("ImprovementModifiers", "ImprovementType")
        for city_id, view in sorted(ctx.city_views.items()):
            for building in view.buildings + view.wonders:
                for row in building_links.get(building, []):
                    if row.get("ModifierId"):
                        result.append(("building", building, city_subject(city_id), row["ModifierId"]))
            for tile in view.district_tiles:
                for row in district_links.get(tile.get("district"), []):
                    if row.get("ModifierId"):
                        result.append(
                            (
                                "district",
                                tile["district"],
                                district_subject(city_id, tile["x"], tile["y"], tile["district"]),
                                row["ModifierId"],
                            )
                        )
            for tile in view.tiles:
                for row in improvement_links.get(tile.get("improvement"), []):
                    if row.get("ModifierId"):
                        result.append(
                            (
                                "improvement",
                                tile["improvement"],
                                plot_subject(tile["x"], tile["y"], city_id),
                                row["ModifierId"],
                            )
                        )
        return result

    # ------------------------------------------------------------ collections
    def _all_city_subjects(self) -> list[Subject]:
        return [city_subject(city_id) for city_id in sorted(self.ctx.city_views)]

    def _city_plots(self, city_id: str) -> list[Subject]:
        view = self.ctx.city_views.get(city_id)
        if not view:
            return []
        return [plot_subject(t["x"], t["y"], city_id) for t in view.tiles]

    def _city_districts(self, city_id: str) -> list[Subject]:
        view = self.ctx.city_views.get(city_id)
        if not view:
            return []
        subjects = [district_subject(city_id, view.center[0], view.center[1], "DISTRICT_CITY_CENTER")]
        subjects += [
            district_subject(city_id, t["x"], t["y"], t["district"])
            for t in view.district_tiles
            if t.get("district") != "DISTRICT_WONDER"
        ]
        return subjects

    def collection_subjects(self, collection: str, owner: Subject) -> tuple[list[Subject] | None, str | None]:
        if collection == "COLLECTION_OWNER":
            return [owner], None
        if collection in ("COLLECTION_PLAYER_CITIES", "COLLECTION_ALL_CITIES"):
            note = "단일 플레이어 시뮬레이션: 자국 도시만 대상" if collection == "COLLECTION_ALL_CITIES" else None
            return self._all_city_subjects(), note
        if collection == "COLLECTION_PLAYER_CAPITAL_CITY":
            capital = self.ctx.player.capital_id
            return ([city_subject(capital)] if capital else []), None
        if collection == "COLLECTION_CITY_PLOT_YIELDS":
            if not owner.city_id:
                return None, "도시가 없는 소유자"
            return self._city_plots(owner.city_id), None
        if collection == "COLLECTION_PLAYER_PLOT_YIELDS":
            return [s for city_id in sorted(self.ctx.city_views) for s in self._city_plots(city_id)], None
        if collection == "COLLECTION_SINGLE_PLOT_YIELDS":
            if owner.x is None:
                return None, "타일이 없는 소유자"
            return [plot_subject(owner.x, owner.y, owner.city_id)], None
        if collection == "COLLECTION_PLAYER_DISTRICTS":
            return [s for city_id in sorted(self.ctx.city_views) for s in self._city_districts(city_id)], None
        if collection == "COLLECTION_CITY_DISTRICTS":
            if not owner.city_id:
                return None, "도시가 없는 소유자"
            return self._city_districts(owner.city_id), None
        if collection in ("COLLECTION_MAJOR_PLAYERS", "COLLECTION_ALL_PLAYERS"):
            return [player_subject()], "단일 플레이어 시뮬레이션: 자국만 대상"
        return None, f"미지원 컬렉션 {collection}"

    # ---------------------------------------------------------------- engine
    def _skip(self, category: str, kind: str, source_id: str, modifier_id: str, reason: str, effect: str | None = None):
        self.book.skipped.append(
            {
                "category": category,
                "source_kind": kind,
                "source_id": source_id,
                "modifier_id": modifier_id,
                "effect": effect,
                "reason": reason,
            }
        )

    def run(self) -> EffectBook:
        if not self.ctx.settings.get("apply_modifiers", True):
            return self.book
        if not self.ctx.db.available:
            self._skip("data", "-", "-", "-", "rules_db.json이 없어 Modifier를 해석하지 않음")
            return self.book
        for kind, source_id, owner, modifier_id in self.sources():
            self.resolve(kind, source_id, owner, modifier_id, depth=0)
        return self.book

    def resolve(self, kind: str, source_id: str, owner: Subject, modifier_id: str, depth: int,
                chain: list[str] | None = None) -> None:
        chain = list(chain or []) + [modifier_id]
        row = self.modifier_row(modifier_id)
        if row is None:
            self._skip("missing", kind, source_id, modifier_id, "Modifiers 행 없음")
            return
        collection, effect = self.dynamic(row.get("ModifierType"))
        if effect is None:
            self._skip("missing", kind, source_id, modifier_id, f"DynamicModifiers에 {row.get('ModifierType')} 없음")
            return
        if as_bool(row.get("RunOnce")):
            self._skip("one_shot", kind, source_id, modifier_id, "1회성(RunOnce) 효과는 턴 시뮬레이션에서 미지원", effect)
            return
        if effect not in SUPPORTED_EFFECTS:
            category = "economy_unsupported" if effect in ECONOMY_RELATED_UNSUPPORTED else "out_of_scope"
            self._skip(category, kind, source_id, modifier_id, f"미지원 효과 {effect}", effect)
            return
        owner_set = row.get("OwnerRequirementSetId")
        owner_result = self.requirements.evaluate_set(owner_set, owner)
        if owner_result["met"] is None:
            self._skip("requirement_unsupported", kind, source_id, modifier_id, f"소유자 조건 {owner_set} 평가 불가", effect)
            return
        if owner_result["met"] is False:
            self.book.inactive.append(
                {"source_kind": kind, "source_id": source_id, "modifier_id": modifier_id, "effect": effect,
                 "reason": f"소유자 조건 {owner_set} 미충족", "subject": owner.label(self.ctx)}
            )
            return
        subjects, note = self.collection_subjects(collection or "", owner)
        if subjects is None:
            self._skip("collection_unsupported", kind, source_id, modifier_id, note or "컬렉션 해석 불가", effect)
            return
        args = self.modifier_args(modifier_id)
        subject_set = row.get("SubjectRequirementSetId")
        for subject in subjects:
            result = self.requirements.evaluate_set(subject_set, subject)
            if result["met"] is None:
                self._skip(
                    "requirement_unsupported", kind, source_id, modifier_id,
                    f"대상 조건 {subject_set} 평가 불가: {self._first_unknown(result)}", effect,
                )
                return
            if result["met"] is False:
                if subject.kind in ("player", "city"):
                    self.book.inactive.append(
                        {"source_kind": kind, "source_id": source_id, "modifier_id": modifier_id, "effect": effect,
                         "reason": f"대상 조건 {subject_set} 미충족", "subject": subject.label(self.ctx)}
                    )
                continue
            if effect == "EFFECT_ATTACH_MODIFIER":
                inner = args.get("ModifierId")
                if not inner or depth >= 4:
                    self._skip("missing", kind, source_id, modifier_id, "부착할 ModifierId 없음 또는 연쇄 깊이 초과", effect)
                    return
                self.resolve(kind, source_id, subject, inner, depth + 1, chain)
                continue
            self.book.records.append(
                EffectRecord(
                    effect=effect,
                    modifier_id=modifier_id,
                    source_kind=kind,
                    source_id=source_id,
                    subject=subject,
                    data={**args, "_collection_note": note} if note else dict(args),
                    owner_city_id=owner.city_id,
                    chain=chain,
                    trace={
                        "collection": collection,
                        "collection_note": note,
                        "owner": _trace_set(owner_result, owner.label(self.ctx)),
                        "subject": _trace_set(result, subject.label(self.ctx)),
                    },
                )
            )

    @staticmethod
    def _first_unknown(result: dict) -> str:
        for item in result.get("requirements", []):
            if item.get("met") is None:
                return f"{item.get('id')} ({item.get('reason')})"
        return result.get("reason") or "알 수 없음"


def _trace_set(result: dict, subject_label: str) -> dict[str, Any]:
    """Compact, JSON-friendly record of a RequirementSet evaluation."""
    def item(req: dict) -> dict[str, Any]:
        return {
            "id": req.get("id"), "type": req.get("type"), "args": req.get("args") or {},
            "inverse": req.get("inverse", False), "met": req.get("met"), "reason": req.get("reason"),
        }

    return {
        "set": result.get("set"),
        "type": result.get("type"),
        "met": result.get("met"),
        "subject": subject_label,
        "requirements": [item(req) for req in result.get("requirements") or []],
    }


def run_modifiers(ctx: EngineContext) -> EffectBook:
    return ModifierEngine(ctx).run()
