"""Data-driven evaluation of Civ VI RequirementSets.

Each requirement is evaluated against a *subject* (player, city, district or
plot). Results are three-valued:

* ``True`` / ``False`` – the requirement type is supported and evaluated;
* ``None`` – the requirement type (or the data it needs) is not supported.

A RequirementSet is ``None`` whenever its outcome depends on an unsupported
requirement. The modifier engine never applies an effect whose requirements
are ``None``; it records the skipped effect in its audit log instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.services.sim_context import EngineContext, as_bool, as_float, hex_distance


@dataclass(frozen=True)
class Subject:
    kind: str  # "player" | "city" | "district" | "plot"
    city_id: str | None = None
    x: int | None = None
    y: int | None = None
    district_id: str | None = None

    @property
    def key(self) -> tuple:
        return (self.kind, self.city_id, self.x, self.y, self.district_id)

    def label(self, ctx: EngineContext) -> str:
        city = ctx.cities.get(self.city_id or "") if self.city_id else None
        city_name = city.get("name", self.city_id) if city else None
        if self.kind == "player":
            return "플레이어"
        if self.kind == "city":
            return f"도시 {city_name}"
        if self.kind == "district":
            return f"{city_name} {self.district_id} ({self.x},{self.y})"
        return f"타일 ({self.x},{self.y})" + (f" · {city_name}" if city_name else "")


def player_subject() -> Subject:
    return Subject("player")


def city_subject(city_id: str) -> Subject:
    return Subject("city", city_id=city_id)


def district_subject(city_id: str, x: int, y: int, district_id: str) -> Subject:
    return Subject("district", city_id=city_id, x=x, y=y, district_id=district_id)


def plot_subject(x: int, y: int, city_id: str | None) -> Subject:
    return Subject("plot", city_id=city_id, x=x, y=y)


# ---------------------------------------------------------------- helpers
def _requirement_args(ctx: EngineContext, requirement_id: str) -> dict[str, str]:
    args: dict[str, str] = {}
    for row in ctx.db.group("RequirementArguments", "RequirementId").get(requirement_id, []):
        if row.get("Name"):
            args[row["Name"]] = row.get("Value")
    return args


def _subject_tile(ctx: EngineContext, subject: Subject) -> dict | None:
    if subject.x is not None and subject.y is not None:
        return ctx.tile(subject.x, subject.y)
    if subject.city_id:
        view = ctx.city_views.get(subject.city_id)
        if view:
            return ctx.tile(*view.center)
    return None


def _tile_district(ctx: EngineContext, tile: dict | None) -> str | None:
    if tile is None:
        return None
    for view in ctx.city_views.values():
        if (tile["x"], tile["y"]) == view.center:
            return "DISTRICT_CITY_CENTER"
    return tile.get("district")


def _natural_wonders(ctx: EngineContext) -> set[str]:
    return {
        row["FeatureType"]
        for row in ctx.db.rows("Features")
        if row.get("FeatureType") and as_bool(row.get("NaturalWonder"))
    }


def _resource_row(ctx: EngineContext, resource: str | None) -> dict:
    return ctx.db.index("Resources", "ResourceType").get(resource or "", {})


Outcome = tuple[bool | None, str]


def _need_city(ctx: EngineContext, subject: Subject):
    return ctx.city_views.get(subject.city_id or "")


def _req_city_has_building(ctx, subject, args) -> Outcome:
    view = _need_city(ctx, subject)
    if view is None:
        return None, "도시 대상이 아님"
    building = args.get("BuildingType")
    return building in view.building_types, f"건물 {building} 보유 여부"


def _req_city_has_district(ctx, subject, args) -> Outcome:
    view = _need_city(ctx, subject)
    if view is None:
        return None, "도시 대상이 아님"
    district = args.get("DistrictType")
    return district in view.district_types, f"특수지구 {district} 보유 여부"


def _req_city_specialty_count(ctx, subject, args) -> Outcome:
    view = _need_city(ctx, subject)
    if view is None:
        return None, "도시 대상이 아님"
    amount = int(as_float(args.get("Amount"), 0))
    count = sum(
        1 for tile in view.district_tiles if ctx.is_specialty_district(tile.get("district", ""))
    )
    return count >= amount, f"전문 특수지구 {count}개 ≥ {amount}"


def _req_district_type(ctx, subject, args) -> Outcome:
    expected = args.get("DistrictType")
    district = subject.district_id
    if subject.kind in ("plot", "city"):
        district = _tile_district(ctx, _subject_tile(ctx, subject))
    if district is None:
        return False, "특수지구 없음"
    return expected in ctx.district_aliases(district), f"특수지구 {district} = {expected}"


def _plot_check(attr: str, arg: str):
    def check(ctx, subject, args) -> Outcome:
        tile = _subject_tile(ctx, subject)
        if tile is None:
            return None, "타일 대상이 아님"
        expected = args.get(arg)
        return tile.get(attr) == expected, f"타일 {attr}={tile.get(attr)} / 요구 {expected}"

    return check


def _req_terrain_class(ctx, subject, args) -> Outcome:
    tile = _subject_tile(ctx, subject)
    if tile is None:
        return None, "타일 대상이 아님"
    klass = args.get("TerrainClass")
    terrains = {
        row.get("TerrainType")
        for row in ctx.db.rows("TerrainClass_Terrains")
        if row.get("TerrainClassType") == klass
    }
    if not terrains:
        return None, f"TerrainClass {klass} 데이터 없음"
    return tile.get("terrain") in terrains, f"지형 {tile.get('terrain')} ∈ {klass}"


def _req_resource_class(ctx, subject, args) -> Outcome:
    tile = _subject_tile(ctx, subject)
    if tile is None:
        return None, "타일 대상이 아님"
    if not tile.get("resource"):
        return False, "자원 없음"
    klass = _resource_row(ctx, tile.get("resource")).get("ResourceClassType")
    return klass == args.get("ResourceClassType"), f"자원 분류 {klass}"


def _req_resource_tag(ctx, subject, args) -> Outcome:
    tile = _subject_tile(ctx, subject)
    if tile is None:
        return None, "타일 대상이 아님"
    resource = tile.get("resource")
    if not resource:
        return False, "자원 없음"
    tags = {
        row.get("Tag")
        for row in ctx.db.rows("TypeTags")
        if row.get("Type") == resource and row.get("_file") == "Resources"
    }
    return args.get("Tag") in tags, f"자원 태그 {sorted(t for t in tags if t)}"


def _req_plot_district(ctx, subject, args) -> Outcome:
    tile = _subject_tile(ctx, subject)
    if tile is None:
        return None, "타일 대상이 아님"
    district = _tile_district(ctx, tile)
    expected = args.get("DistrictType")
    return expected in ctx.district_aliases(district), f"타일 특수지구 {district}"


def _req_adjacent_district(ctx, subject, args) -> Outcome:
    tile = _subject_tile(ctx, subject)
    if tile is None:
        return None, "타일 대상이 아님"
    expected = args.get("DistrictType")
    min_range = int(as_float(args.get("MinRange"), 1 if "MinRange" not in args else 0))
    max_range = int(as_float(args.get("MaxRange"), 1))
    for other in ctx.tiles_within(tile, min_range, max_range):
        if expected in ctx.district_aliases(_tile_district(ctx, other)):
            return True, f"{min_range}~{max_range}칸 내 {expected} 있음"
    return False, f"{min_range}~{max_range}칸 내 {expected} 없음"


def _adjacent_check(predicate: Callable[[EngineContext, dict, dict], bool], label: str):
    def check(ctx, subject, args) -> Outcome:
        tile = _subject_tile(ctx, subject)
        if tile is None:
            return None, "타일 대상이 아님"
        hit = any(predicate(ctx, other, args) for other in ctx.neighbors(tile))
        return hit, f"인접 {label}: {'있음' if hit else '없음'}"

    return check


def _req_adjacent_river(ctx, subject, args) -> Outcome:
    tile = _subject_tile(ctx, subject)
    if tile is None:
        return None, "타일 대상이 아님"
    river = bool(tile.get("river") or tile.get("river_wires"))
    return river, "타일 강 플래그(강 가장자리 근사)"


def _req_player_tech(ctx, subject, args) -> Outcome:
    tech = args.get("TechnologyType")
    return tech in ctx.player.technologies, f"기술 {tech}"


def _req_player_civic(ctx, subject, args) -> Outcome:
    civic = args.get("CivicType")
    return civic in ctx.player.civics, f"사회제도 {civic}"


def _req_follows_pantheon(ctx, subject, args) -> Outcome:
    has = bool(ctx.player.pantheon)
    return has, "판테온 보유 시 자국 도시가 판테온을 따른다고 가정"


def _req_has_pantheon(ctx, subject, args) -> Outcome:
    return bool(ctx.player.pantheon), "판테온 보유"


def _req_follows_religion(ctx, subject, args) -> Outcome:
    view = _need_city(ctx, subject)
    if view is None:
        return None, "도시 대상이 아님"
    return bool(view.city.get("religion")), "도시 religion 필드"


def _req_completed_project(ctx, subject, args) -> Outcome:
    project = args.get("ProjectType")
    return project in ctx.player.completed_projects, f"프로젝트 {project} 완료"


def _req_is_human(ctx, subject, args) -> Outcome:
    return True, "시뮬레이션 플레이어는 사람으로 간주"


def _req_capital_continent(ctx, subject, args) -> Outcome:
    tile = _subject_tile(ctx, subject)
    capital = ctx.city_views.get(ctx.player.capital_id or "")
    if tile is None or capital is None:
        return None, "타일 또는 수도 없음"
    capital_tile = ctx.tile(*capital.center)
    if tile.get("continent") is None or capital_tile is None or capital_tile.get("continent") is None:
        return None, "지도에 대륙 정보 없음"
    return tile.get("continent") == capital_tile.get("continent"), "수도와 같은 대륙"


def _req_plot_is_lake(ctx, subject, args) -> Outcome:
    tile = _subject_tile(ctx, subject)
    if tile is None:
        return None, "타일 대상이 아님"
    if "lake" not in tile:
        return None, "지도 데이터에 호수 구분 없음"
    return bool(tile.get("lake")), "호수 플래그"


def _req_resource_owned(ctx, subject, args) -> Outcome:
    resource = args.get("ResourceType")
    for view in ctx.city_views.values():
        for tile in view.tiles:
            if tile.get("resource") == resource and tile.get("improvement"):
                return True, f"영토 내 개량된 {resource}"
    return False, f"영토 내 개량된 {resource} 없음 (근사)"


def _req_city_full_loyalty(ctx, subject, args) -> Outcome:
    """Loyalty is not simulated: a city counts as fully loyal unless the city
    document says otherwise (city["full_loyalty"] = false)."""
    view = _need_city(ctx, subject)
    if view is None:
        return None, "도시 대상이 아님"
    full = view.city.get("full_loyalty", True) is not False
    return full, "충성도 최대(가정: 도시 설정 full_loyalty, 기본 참)"


REQUIREMENT_HANDLERS: dict[str, Callable[[EngineContext, Subject, dict], Outcome]] = {
    "REQUIREMENT_CITY_HAS_FULL_LOYALTY": _req_city_full_loyalty,
    "REQUIREMENT_CITY_HAS_BUILDING": _req_city_has_building,
    "REQUIREMENT_CITY_HAS_DISTRICT": _req_city_has_district,
    "REQUIREMENT_CITY_HAS_X_SPECIALTY_DISTRICTS": _req_city_specialty_count,
    "REQUIREMENT_DISTRICT_TYPE_MATCHES": _req_district_type,
    "REQUIREMENT_PLOT_TERRAIN_TYPE_MATCHES": _plot_check("terrain", "TerrainType"),
    "REQUIREMENT_PLOT_FEATURE_TYPE_MATCHES": _plot_check("feature", "FeatureType"),
    "REQUIREMENT_PLOT_RESOURCE_TYPE_MATCHES": _plot_check("resource", "ResourceType"),
    "REQUIREMENT_PLOT_IMPROVEMENT_TYPE_MATCHES": _plot_check("improvement", "ImprovementType"),
    "REQUIREMENT_PLOT_TERRAIN_CLASS_MATCHES": _req_terrain_class,
    "REQUIREMENT_PLOT_RESOURCE_CLASS_TYPE_MATCHES": _req_resource_class,
    "REQUIREMENT_PLOT_RESOURCE_TAG_MATCHES": _req_resource_tag,
    "REQUIREMENT_PLOT_DISTRICT_TYPE_MATCHES": _req_plot_district,
    "REQUIREMENT_PLOT_ADJACENT_DISTRICT_TYPE_MATCHES": _req_adjacent_district,
    "REQUIREMENT_PLOT_ADJACENT_FEATURE_TYPE_MATCHES": _adjacent_check(
        lambda ctx, t, a: t.get("feature") == a.get("FeatureType"), "지형 특성"
    ),
    "REQUIREMENT_PLOT_ADJACENT_IMPROVEMENT_TYPE_MATCHES": _adjacent_check(
        lambda ctx, t, a: t.get("improvement") == a.get("ImprovementType"), "시설"
    ),
    "REQUIREMENT_PLOT_ADJACENT_RESOURCE_CLASS_TYPE_MATCHES": _adjacent_check(
        lambda ctx, t, a: bool(t.get("resource"))
        and _resource_row(ctx, t.get("resource")).get("ResourceClassType") == a.get("ResourceClassType"),
        "자원 분류",
    ),
    "REQUIREMENT_PLOT_ADJACENT_TO_WONDER": _adjacent_check(
        lambda ctx, t, a: t.get("district") == "DISTRICT_WONDER"
        or (t.get("feature") in _natural_wonders(ctx)),
        "불가사의",
    ),
    "REQUIREMENT_PLOT_ADJACENT_TO_RIVER": _req_adjacent_river,
    "REQUIREMENT_PLAYER_HAS_TECHNOLOGY": _req_player_tech,
    "REQUIREMENT_PLAYER_HAS_CIVIC": _req_player_civic,
    "REQUIREMENT_CITY_FOLLOWS_PANTHEON": _req_follows_pantheon,
    "REQUIREMENT_PLAYER_HAS_PANTHEON": _req_has_pantheon,
    "REQUIREMENT_CITY_FOLLOWS_RELIGION": _req_follows_religion,
    "REQUIREMENT_PLAYER_HAS_COMPLETED_PROJECT": _req_completed_project,
    "REQUIREMENT_PLAYER_IS_HUMAN": _req_is_human,
    "REQUIREMENT_PLOT_IS_OWNER_CAPITAL_CONTINENT": _req_capital_continent,
    "REQUIREMENT_PLOT_IS_LAKE": _req_plot_is_lake,
    "REQUIREMENT_PLAYER_HAS_RESOURCE_OWNED": _req_resource_owned,
}


class RequirementEvaluator:
    """Evaluates requirement sets with memoization for one engine context."""

    def __init__(self, ctx: EngineContext):
        self.ctx = ctx
        self._cache: dict[tuple, dict] = {}
        self.unsupported_types: dict[str, int] = {}

    def requirement_set_rows(self, set_id: str) -> tuple[str, list[str]]:
        set_row = self.ctx.db.index("RequirementSets", "RequirementSetId").get(set_id)
        set_type = (set_row or {}).get("RequirementSetType", "REQUIREMENTSET_TEST_ALL")
        requirement_ids: list[str] = []
        for row in self.ctx.db.group("RequirementSetRequirements", "RequirementSetId").get(set_id, []):
            rid = row.get("RequirementId")
            if rid and rid not in requirement_ids:
                requirement_ids.append(rid)
        return set_type, requirement_ids

    def evaluate_requirement(self, requirement_id: str, subject: Subject, depth: int = 0) -> dict:
        row = self.ctx.db.index("Requirements", "RequirementId").get(requirement_id)
        if row is None:
            return {"id": requirement_id, "type": None, "met": None, "reason": "Requirement 정의 없음"}
        requirement_type = row.get("RequirementType")
        args = _requirement_args(self.ctx, requirement_id)
        inverse = as_bool(row.get("Inverse"))
        if requirement_type == "REQUIREMENT_REQUIREMENTSET_IS_MET":
            nested = self.evaluate_set(args.get("RequirementSetId"), subject, depth + 1)
            met, reason = nested["met"], f"중첩 RequirementSet {args.get('RequirementSetId')}"
        elif requirement_type in ("REQUIREMENT_COLLECTION_ANY_MET", "REQUIREMENT_COLLECTION_ALL_MET"):
            met, reason = None, "컬렉션 조건 미지원"
        else:
            handler = REQUIREMENT_HANDLERS.get(requirement_type or "")
            if handler is None:
                self.unsupported_types[requirement_type or "?"] = (
                    self.unsupported_types.get(requirement_type or "?", 0) + 1
                )
                met, reason = None, f"미지원 Requirement 유형 {requirement_type}"
            else:
                met, reason = handler(self.ctx, subject, args)
        if inverse and met is not None:
            met = not met
        return {
            "id": requirement_id,
            "type": requirement_type,
            "args": args,
            "inverse": inverse,
            "met": met,
            "reason": reason,
        }

    def evaluate_set(self, set_id: str | None, subject: Subject, depth: int = 0) -> dict:
        if not set_id:
            return {"set": None, "met": True, "requirements": []}
        cache_key = (set_id, subject.key)
        if cache_key in self._cache:
            return self._cache[cache_key]
        if depth > 6:
            return {"set": set_id, "met": None, "requirements": [], "reason": "중첩 깊이 초과"}
        set_type, requirement_ids = self.requirement_set_rows(set_id)
        if not requirement_ids:
            known = set_id in self.ctx.db.index("RequirementSets", "RequirementSetId")
            result = {
                "set": set_id,
                "type": set_type,
                "met": True if known else None,
                "requirements": [],
                "reason": None if known else "RequirementSet 정의 없음",
            }
            self._cache[cache_key] = result
            return result
        results = [self.evaluate_requirement(rid, subject, depth) for rid in requirement_ids]
        values = [item["met"] for item in results]
        if set_type == "REQUIREMENTSET_TEST_ANY":
            if any(value is True for value in values):
                met: bool | None = True
            elif any(value is None for value in values):
                met = None
            else:
                met = False
        else:
            if any(value is False for value in values):
                met = False
            elif any(value is None for value in values):
                met = None
            else:
                met = True
        result = {"set": set_id, "type": set_type, "met": met, "requirements": results}
        self._cache[cache_key] = result
        return result


def supported_requirement_types() -> list[str]:
    return sorted(
        set(REQUIREMENT_HANDLERS)
        | {"REQUIREMENT_REQUIREMENTSET_IS_MET"}
    )


def distance_ok(a: dict, b: dict, limit: int) -> bool:
    return hex_distance(a["x"], a["y"], b["x"], b["y"]) <= limit
