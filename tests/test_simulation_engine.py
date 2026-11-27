"""Accuracy and behaviour tests for the simulation engine (section 13).

Expected numbers are derived by hand from Data.zip values on small synthetic
maps, so a failing test points at a concrete rule.
"""

import json
import unittest
from copy import deepcopy

from app.services import production_engine
from app.services.city_growth_service import (
    compute_amenities,
    compute_housing,
    growth_status,
    growth_threshold,
    housing_growth_multiplier,
    required_amenities,
)
from app.services.citizen_assignment_service import optimize_city, set_tile_citizens
from app.services.modifier_engine import ModifierEngine, run_modifiers
from app.services.requirement_engine import RequirementEvaluator, city_subject, plot_subject
from app.repositories.rules_db_repository import RulesDB
from app.services.sim_context import build_context, refresh_views
from app.services.turn_simulation_service import evaluate, simulate_one_turn, simulate_turns
from app.services.yield_breakdown_service import compute_city, specialist_slots
from app.services.yield_service import calculate_city_yields

from tests.sim_fixtures import (
    base_rules,
    bundle,
    claim,
    context,
    make_city,
    make_game,
    make_map,
    real_catalog,
    statics,
    tile,
)


def seoul(population=3, **extra):
    map_data = make_map()
    city = make_city(population=population, **extra)
    claim(map_data, city)
    return map_data, {city["id"]: city}


def city_totals(b, city_id="city-a", rules=None, assignment=None):
    ctx = context(b, rules)
    book = run_modifiers(ctx)
    amenities = compute_amenities(ctx, book)
    return compute_city(ctx, book, city_id, assignment, amenity_state=amenities[city_id])


class TileAndCityYieldTests(unittest.TestCase):
    def test_city_center_minimum_palace_and_population(self):
        m, cities = seoul(population=3)
        result = city_totals(bundle(m, cities))
        # Grass center: 2F 0P -> floor to 2F 1P. Palace +2P +5G +2S +1C. Pop 3: 1.5S 0.6C.
        self.assertEqual(result["totals"]["Food"], 2.0)
        self.assertEqual(result["totals"]["Production"], 3.0)
        self.assertEqual(result["totals"]["Gold"], 5.0)
        self.assertAlmostEqual(result["totals"]["Science"], 3.5)
        self.assertAlmostEqual(result["totals"]["Culture"], 1.6)
        self.assertEqual(result["unassigned_citizens"], 3)

    def test_assigning_a_citizen_changes_yields_by_the_tile_yield(self):
        m, cities = seoul(population=3)
        before = city_totals(bundle(m, cities))
        tile(m, 6, 4)["worked"] = True
        tile(m, 6, 4)["resource"] = "RESOURCE_WHEAT"
        after = city_totals(bundle(m, cities))
        # Grass 2F + Wheat +1F (Resource_YieldChanges).
        self.assertAlmostEqual(after["totals"]["Food"] - before["totals"]["Food"], 3.0)
        self.assertEqual(after["assignment"], {"6,4": 1})
        self.assertEqual(after["unassigned_citizens"], 2)

    def test_engine_matches_legacy_calculation_with_new_rules_disabled(self):
        m, cities = seoul(population=4)
        for x, y, extra in ((6, 4, {"resource": "RESOURCE_WHEAT"}), (4, 4, {"feature": "FEATURE_FOREST"}),
                            (5, 3, {"terrain": "TERRAIN_PLAINS_HILLS"}), (4, 5, {"improvement": "IMPROVEMENT_FARM"})):
            tile(m, x, y).update({"worked": True, **extra})
        rules = base_rules(apply_modifiers=False, apply_amenity_effects=False, resource_visibility_requires_tech=False)
        engine = city_totals(bundle(m, cities), rules=rules)
        legacy = calculate_city_yields(cities["city-a"], m, real_catalog(), rules)
        for key, value in legacy["totals"].items():
            self.assertAlmostEqual(engine["totals"][key], value, places=6, msg=key)

    def test_mountain_without_xml_yields_is_zero_not_grass_fallback(self):
        m, cities = seoul(population=1)
        tile(m, 6, 4).update({"terrain": "TERRAIN_GRASS_MOUNTAIN", "worked": True})
        result = city_totals(bundle(m, cities))
        mountain = next(t for t in result["tiles"] if t["key"] == "6,4")
        self.assertEqual(mountain["yields"]["Food"], 0.0)

    def test_hidden_strategic_resource_needs_reveal_technology(self):
        m, cities = seoul(population=1)
        tile(m, 6, 4).update({"resource": "RESOURCE_NITER", "worked": True})
        hidden = city_totals(bundle(m, cities))
        game = make_game(technologies=["TECH_MILITARY_ENGINEERING"])
        shown = city_totals(bundle(m, cities, game))
        self.assertGreater(shown["totals"]["Production"] + shown["totals"]["Food"],
                           hidden["totals"]["Production"] + hidden["totals"]["Food"])

    def test_improvement_bonus_requires_its_technology(self):
        m, cities = seoul(population=1)
        tile(m, 6, 4).update({"terrain": "TERRAIN_PLAINS_HILLS", "improvement": "IMPROVEMENT_MINE", "worked": True})
        db = statics().db
        bonus = [r for r in db.group("Improvement_BonusYieldChanges", "ImprovementType")["IMPROVEMENT_MINE"]
                 if r.get("PrereqTech")][0]
        without = city_totals(bundle(m, cities))
        with_tech = city_totals(bundle(m, cities, make_game(technologies=[bonus["PrereqTech"]])))
        key = bonus["YieldType"].removeprefix("YIELD_").title()
        factor = 1 + without["percent"][key] / 100  # amenity state % applies on top
        self.assertAlmostEqual(with_tech["totals"][key] - without["totals"][key], float(bonus["BonusYieldChange"]) * factor)
        self.assertTrue(any(c["kind"] == "improvement_bonus" for t in with_tech["tiles"] for c in t["components"]))


class DistrictAndBuildingTests(unittest.TestCase):
    def campus_map(self):
        m, cities = seoul(population=6)
        tile(m, 6, 4)["district"] = "DISTRICT_CAMPUS"
        tile(m, 7, 4)["terrain"] = "TERRAIN_GRASS_MOUNTAIN"
        tile(m, 6, 3)["terrain"] = "TERRAIN_GRASS_MOUNTAIN"
        cities["city-a"]["districts"] = ["DISTRICT_CAMPUS"]
        return m, cities

    def district(self, result, key):
        return next(d for d in result["districts"] if d["key"] == key)

    def test_campus_mountain_adjacency_and_city_center_counts_as_district(self):
        m, cities = self.campus_map()
        result = city_totals(bundle(m, cities))
        self.assertEqual(self.district(result, "6,4")["adjacency"]["Science"], 2.0)
        tile(m, 6, 5)["district"] = "DISTRICT_COMMERCIAL_HUB"
        result = city_totals(bundle(m, cities))
        # 2 mountains + floor((city center + commercial hub) / 2) = 3
        self.assertEqual(self.district(result, "6,4")["adjacency"]["Science"], 3.0)

    def test_commercial_hub_river_bonus_applies_once_for_its_own_tile(self):
        m, cities = self.campus_map()
        tile(m, 6, 5)["district"] = "DISTRICT_COMMERCIAL_HUB"
        for x, y in ((7, 5), (5, 5), (6, 6)):
            tile(m, x, y)["river"] = True
        result = city_totals(bundle(m, cities))
        self.assertEqual(self.district(result, "6,5")["adjacency"]["Gold"], 0.0)
        tile(m, 6, 5)["river"] = True
        result = city_totals(bundle(m, cities))
        self.assertEqual(self.district(result, "6,5")["adjacency"]["Gold"], 2.0)

    def test_policy_adjacency_percentage_and_building_yields(self):
        m, cities = self.campus_map()
        cities["city-a"]["buildings"].append("BUILDING_LIBRARY")
        base = city_totals(bundle(m, cities))
        game = make_game(civics=["CIVIC_RECORDED_HISTORY"],
                         empire={"government": "GOVERNMENT_CHIEFDOM",
                                 "policy_slots": {"SLOT_ECONOMIC": ["POLICY_NATURAL_PHILOSOPHY"]}})
        boosted = city_totals(bundle(m, cities, game))
        self.assertEqual(self.district(base, "6,4")["yields"]["Science"], 2.0)
        self.assertEqual(self.district(boosted, "6,4")["yields"]["Science"], 4.0)
        self.assertEqual(next(b for b in base["buildings"] if b["id"] == "BUILDING_LIBRARY")["yields"]["Science"], 2.0)
        self.assertAlmostEqual(boosted["totals"]["Science"] - base["totals"]["Science"], 2.0 * (1 + base["percent"]["Science"] / 100))

    def test_specialist_slots_come_from_buildings(self):
        m, cities = self.campus_map()
        ctx = context(bundle(m, cities))
        self.assertEqual(specialist_slots(ctx, "city-a", tile(m, 6, 4)), 0)
        cities["city-a"]["buildings"] += ["BUILDING_LIBRARY"]
        ctx = context(bundle(m, cities))
        self.assertEqual(specialist_slots(ctx, "city-a", tile(m, 6, 4)), 1)
        before = city_totals(bundle(m, cities))
        set_tile_citizens(ctx, "city-a", 6, 4, 1)
        after = city_totals(bundle(m, cities))
        self.assertAlmostEqual(after["by_category"]["specialists"]["Science"], 2.0)
        self.assertGreater(after["totals"]["Science"], before["totals"]["Science"])
        with self.assertRaises(Exception):
            set_tile_citizens(context(bundle(m, cities)), "city-a", 6, 4, 2)

    def test_building_completion_changes_next_turn_yields(self):
        m, cities = seoul(population=2)
        city = cities["city-a"]
        city["production_queue"] = [{"queue_id": "q1", "category": "building", "target_id": "BUILDING_MONUMENT", "x": None, "y": None}]
        city["production_progress"] = {"q1": 59.0}
        new_bundle, report = simulate_one_turn(bundle(m, cities), statics())
        self.assertIn("BUILDING_MONUMENT", new_bundle["cities"]["city-a"]["buildings"])
        delta = report["cities"]["city-a"]["delta"]["yields"]
        self.assertGreater(delta["Culture"], 1.9)
        self.assertTrue(any(r["category"] == "buildings" for r in report["cities"]["city-a"]["reasons"]))


class GrowthHousingAmenityTests(unittest.TestCase):
    def test_growth_threshold_formula_from_global_parameters(self):
        ctx = context(bundle(*seoul()))
        self.assertAlmostEqual(growth_threshold(ctx, 1)[0], 15.0)
        self.assertAlmostEqual(growth_threshold(ctx, 2)[0], 24.0)
        self.assertAlmostEqual(growth_threshold(ctx, 3)[0], 15 + 16 + 2 ** 1.5)
        game = make_game(game_speed="EPIC")
        self.assertIsNone(growth_threshold(context(bundle(*seoul(), game)), 3)[0])

    def test_housing_water_and_buildings(self):
        m, cities = seoul(population=3)
        ctx = context(bundle(m, cities))
        book = run_modifiers(ctx)
        self.assertEqual(compute_housing(ctx, book, "city-a")["value"], 3.0)  # no water 2 + palace 1
        tile(m, 5, 4)["river"] = True
        tile(m, 6, 4)["improvement"] = "IMPROVEMENT_FARM"
        tile(m, 4, 4)["improvement"] = "IMPROVEMENT_FARM"
        cities["city-a"]["buildings"].append("BUILDING_GRANARY")
        ctx = context(bundle(m, cities))
        # fresh water 5 + palace 1 + granary 2 + farms 2 × 1 / 2
        self.assertEqual(compute_housing(ctx, run_modifiers(ctx), "city-a")["value"], 9.0)

    def test_housing_growth_bands(self):
        ctx = context(bundle(*seoul()))
        self.assertEqual(housing_growth_multiplier(ctx, 6, 3)[0], 1.0)
        self.assertEqual(housing_growth_multiplier(ctx, 4, 3)[0], 0.5)
        self.assertEqual(housing_growth_multiplier(ctx, 3, 3)[0], 0.25)
        self.assertEqual(housing_growth_multiplier(ctx, 3, 7)[0], 0.0)

    def test_amenity_requirement_and_happiness_effects(self):
        ctx = context(bundle(*seoul()))
        self.assertEqual([required_amenities(ctx, p)[0] for p in (1, 2, 3, 4, 5, 6)], [0, 0, 1, 1, 2, 2])
        m, cities = seoul(population=7)
        ctx = context(bundle(m, cities))
        amenities = compute_amenities(ctx, run_modifiers(ctx))["city-a"]
        self.assertEqual(amenities["score"], -2.0)  # palace 1 - required 3
        self.assertEqual(amenities["happiness"], "HAPPINESS_DISPLEASED")
        self.assertEqual(amenities["non_food_yield_modifier"], -5.0)

    def test_growth_then_next_turn_uses_new_population_and_places_citizen(self):
        m, cities = seoul(population=1, stored_food=14.0)
        tile(m, 5, 4)["river"] = True
        tile(m, 6, 4)["worked"] = True
        new_bundle, report = simulate_one_turn(bundle(m, cities), statics())
        city = new_bundle["cities"]["city-a"]
        self.assertEqual(city["population"], 2)
        # food 2 (center) + 2 (grass) - 2 = 2 surplus; palace amenity 1 vs required 0 -> HAPPY
        # (Happinesses GrowthModifier +10%) -> 2.2; 14 + 2.2 = 16.2 >= 15 -> grow, keep 1.2
        self.assertAlmostEqual(city["stored_food"], 1.2)
        nxt = report["cities"]["city-a"]["next"]
        self.assertEqual(nxt["population"], 2)
        self.assertEqual(sum(nxt["assignment"].values()), 2)
        self.assertGreater(nxt["yields"]["Food"], report["cities"]["city-a"]["current"]["yields"]["Food"])
        types = [e["type"] for e in report["cities"]["city-a"]["events"]]
        self.assertIn("growth", types)
        self.assertIn("new_citizen_placed", types)

    def test_new_citizen_can_wait_for_manual_placement(self):
        m, cities = seoul(population=1, stored_food=14.0, new_citizen_policy="manual")
        tile(m, 5, 4)["river"] = True
        tile(m, 6, 4)["worked"] = True
        new_bundle, report = simulate_one_turn(bundle(m, cities), statics())
        self.assertEqual(report["cities"]["city-a"]["next"]["unassigned_citizens"], 1)
        self.assertIn("new_citizen_pending", [e["type"] for e in report["cities"]["city-a"]["events"]])

    def test_growth_food_change_applies_housing_and_amenity_modifiers(self):
        m, cities = seoul(population=3)
        for x, y in ((6, 4), (4, 4), (5, 3)):
            tile(m, x, y)["worked"] = True
        ctx = context(bundle(m, cities))
        book = run_modifiers(ctx)
        amenities = compute_amenities(ctx, book)
        result = compute_city(ctx, book, "city-a", amenity_state=amenities["city-a"])
        growth = growth_status(ctx, book, "city-a", result["totals"]["Food"],
                               compute_housing(ctx, book, "city-a"), amenities["city-a"])
        self.assertEqual(growth["food_surplus"], 2.0)          # 8 food - 6
        self.assertEqual(growth["housing_multiplier"], 0.25)   # housing 3, pop 3
        self.assertEqual(growth["effective_food_change"], 0.5)


class OptimizerTests(unittest.TestCase):
    def varied_map(self):
        m, cities = seoul(population=2)
        tile(m, 6, 4).update({"terrain": "TERRAIN_PLAINS_HILLS", "improvement": "IMPROVEMENT_MINE"})
        tile(m, 4, 4).update({"resource": "RESOURCE_WHEAT", "improvement": "IMPROVEMENT_FARM"})
        tile(m, 5, 3).update({"terrain": "TERRAIN_PLAINS_HILLS", "feature": "FEATURE_FOREST"})
        tile(m, 4, 5).update({"terrain": "TERRAIN_COAST", "resource": "RESOURCE_FISH"})
        return m, cities

    def plan(self, m, cities, strategy):
        ctx = context(bundle(m, cities))
        book = run_modifiers(ctx)
        amenities = compute_amenities(ctx, book)
        plan = optimize_city(ctx, book, "city-a", amenities["city-a"], strategy)
        result = compute_city(ctx, book, "city-a", plan["assignment"], amenity_state=amenities["city-a"])
        return plan, result

    def test_strategies_change_actual_assignment_not_bonus_numbers(self):
        m, cities = self.varied_map()
        food_plan, food = self.plan(m, cities, "food")
        prod_plan, prod = self.plan(m, cities, "production")
        self.assertNotEqual(food_plan["assignment"], prod_plan["assignment"])
        self.assertGreater(food["totals"]["Food"], prod["totals"]["Food"])
        self.assertGreater(prod["totals"]["Production"], food["totals"]["Production"])
        # Totals are fully explained by real components (no strategy bonus category).
        self.assertNotIn("strategy", food["by_category"])

    def test_optimizer_is_deterministic(self):
        m, cities = self.varied_map()
        first = self.plan(m, cities, "balanced")[0]
        second = self.plan(deepcopy(m), deepcopy(cities), "balanced")[0]
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))

    def test_food_constraint_keeps_growth_when_feasible(self):
        m, cities = self.varied_map()
        plan, result = self.plan(m, cities, "growth")
        self.assertTrue(plan["feasible_food_target"])
        self.assertGreaterEqual(result["totals"]["Food"] - 2 * 2, 2.0)

    def test_locked_and_blocked_tiles_are_respected(self):
        m, cities = self.varied_map()
        cities["city-a"]["tile_preferences"] = {"4,4": {"block": True}, "6,3": {"lock": True}}
        plan, _ = self.plan(m, cities, "food")
        self.assertNotIn("4,4", plan["assignment"])
        self.assertIn("6,3", plan["assignment"])
        self.assertLessEqual(sum(plan["assignment"].values()), 2)

    def test_manual_assignment_cannot_exceed_population(self):
        m, cities = seoul(population=1)
        ctx = context(bundle(m, cities))
        set_tile_citizens(ctx, "city-a", 6, 4, 1)
        with self.assertRaises(Exception):
            set_tile_citizens(ctx, "city-a", 4, 4, 1)
        with self.assertRaises(Exception):
            set_tile_citizens(ctx, "city-a", 5, 4, 1)  # city center


class ProductionTests(unittest.TestCase):
    def test_settler_cost_progression_and_population_cost(self):
        m, cities = seoul(population=3)
        cities["city-a"]["production_history"] = [{"turn": 1, "category": "unit", "target_id": "UNIT_SETTLER"}]
        ctx = context(bundle(m, cities))
        cost, steps = production_engine.item_cost(ctx, "unit", "UNIT_SETTLER")
        self.assertEqual(cost, 80 + 30 * 1)
        self.assertTrue(any(step["step"] == "progression" for step in steps))
        cities["city-a"]["production_queue"] = [{"queue_id": "q", "category": "unit", "target_id": "UNIT_SETTLER", "x": None, "y": None}]
        cities["city-a"]["production_progress"] = {"q": 109.0}
        new_bundle, _ = simulate_one_turn(bundle(m, cities), statics())
        self.assertEqual(new_bundle["cities"]["city-a"]["population"], 2)
        self.assertEqual(new_bundle["game"]["units"][0]["unit_type"], "UNIT_SETTLER")

    def test_game_speed_multiplies_costs(self):
        m, cities = seoul()
        standard = production_engine.item_cost(context(bundle(m, cities)), "building", "BUILDING_MONUMENT")[0]
        epic = production_engine.item_cost(context(bundle(m, cities, make_game(game_speed="EPIC"))), "building", "BUILDING_MONUMENT")[0]
        self.assertEqual(standard, 60.0)
        self.assertEqual(epic, 90.0)

    def test_overflow_carries_to_next_item_one_completion_per_turn(self):
        m, cities = seoul(population=1)
        city = cities["city-a"]
        city["production_queue"] = [
            {"queue_id": "a", "category": "building", "target_id": "BUILDING_MONUMENT", "x": None, "y": None},
            {"queue_id": "b", "category": "unit", "target_id": "UNIT_WARRIOR", "x": None, "y": None},
        ]
        city["production_progress"] = {"a": 59.0, "b": 0.0}
        new_bundle, report = simulate_one_turn(bundle(m, cities), statics())
        # Production 3 (center 1 + palace 2) × 1.05 (HAPPY) = 3.15: 1 completes the monument,
        # 2.15 carries into the warrior; only one item completes this turn.
        after = new_bundle["cities"]["city-a"]
        self.assertEqual([e["target_id"] for e in after["production_queue"]], ["UNIT_WARRIOR"])
        self.assertAlmostEqual(after["production_progress"]["b"], 2.15)
        self.assertTrue(any(e.get("type") == "overflow_carried" for e in report["events"]))

    def test_policy_production_bonus_and_eta_recalculation(self):
        m, cities = seoul(population=1)
        cities["city-a"]["production_queue"] = [{"queue_id": "w", "category": "unit", "target_id": "UNIT_WARRIOR", "x": None, "y": None}]
        cities["city-a"]["production_progress"] = {"w": 0.0}
        plain = evaluate(context(bundle(m, cities)))["cities"]["city-a"]["production"]["queue"][0]
        game = make_game(empire={"government": "GOVERNMENT_CHIEFDOM", "policy_slots": {"SLOT_MILITARY": ["POLICY_AGOGE"]}})
        agoge = evaluate(context(bundle(m, cities, game)))["cities"]["city-a"]["production"]["queue"][0]
        self.assertEqual(plain["production_bonus_percent"], 0.0)
        self.assertEqual(agoge["production_bonus_percent"], 50.0)
        self.assertLess(agoge["estimated_turns"], plain["estimated_turns"])

    def test_availability_reasons(self):
        m, cities = seoul(population=1)
        ctx = context(bundle(m, cities))
        self.assertIn("기술 TECH_WRITING 필요", production_engine.availability(ctx, "city-a", "district", "DISTRICT_CAMPUS", 6, 4))
        self.assertTrue(any("인구" in r for r in production_engine.availability(ctx, "city-a", "unit", "UNIT_SETTLER")))
        self.assertTrue(any("특수지구" in r for r in production_engine.availability(ctx, "city-a", "building", "BUILDING_LIBRARY")))

    def test_district_project_converts_production(self):
        m, cities = seoul(population=1)
        tile(m, 6, 4)["district"] = "DISTRICT_CAMPUS"
        cities["city-a"]["production_queue"] = [{"queue_id": "p", "category": "project", "target_id": "PROJECT_ENHANCE_DISTRICT_CAMPUS", "x": None, "y": None}]
        result = evaluate(context(bundle(m, cities)))["cities"]["city-a"]["yields"]
        self.assertAlmostEqual(result["by_category"]["projects"]["Science"], result["totals"]["Production"] * 0.15)


class ModifierAndRequirementTests(unittest.TestCase):
    def fake_db(self):
        return RulesDB({"tables": {
            "RequirementSets": [
                {"RequirementSetId": "ANY_SET", "RequirementSetType": "REQUIREMENTSET_TEST_ANY"},
                {"RequirementSetId": "ALL_SET", "RequirementSetType": "REQUIREMENTSET_TEST_ALL"},
                {"RequirementSetId": "UNKNOWN_SET", "RequirementSetType": "REQUIREMENTSET_TEST_ALL"},
            ],
            "RequirementSetRequirements": [
                {"RequirementSetId": "ANY_SET", "RequirementId": "R_FOREST"},
                {"RequirementSetId": "ANY_SET", "RequirementId": "R_HILLS"},
                {"RequirementSetId": "ALL_SET", "RequirementId": "R_FOREST"},
                {"RequirementSetId": "ALL_SET", "RequirementId": "R_NOT_WHEAT"},
                {"RequirementSetId": "UNKNOWN_SET", "RequirementId": "R_ALIEN"},
            ],
            "Requirements": [
                {"RequirementId": "R_FOREST", "RequirementType": "REQUIREMENT_PLOT_FEATURE_TYPE_MATCHES"},
                {"RequirementId": "R_HILLS", "RequirementType": "REQUIREMENT_PLOT_TERRAIN_TYPE_MATCHES"},
                {"RequirementId": "R_NOT_WHEAT", "RequirementType": "REQUIREMENT_PLOT_RESOURCE_TYPE_MATCHES", "Inverse": "true"},
                {"RequirementId": "R_ALIEN", "RequirementType": "REQUIREMENT_SOMETHING_NEW"},
            ],
            "RequirementArguments": [
                {"RequirementId": "R_FOREST", "Name": "FeatureType", "Value": "FEATURE_FOREST"},
                {"RequirementId": "R_HILLS", "Name": "TerrainType", "Value": "TERRAIN_GRASS_HILLS"},
                {"RequirementId": "R_NOT_WHEAT", "Name": "ResourceType", "Value": "RESOURCE_WHEAT"},
            ],
            "Policies": [{"PolicyType": "POLICY_TEST", "GovernmentSlotType": "SLOT_ECONOMIC"}],
            "PolicyModifiers": [{"PolicyType": "POLICY_TEST", "ModifierId": "ATTACH"},
                                {"PolicyType": "POLICY_TEST", "ModifierId": "UNKNOWN_REQ"}],
            "Modifiers": [
                {"ModifierId": "ATTACH", "ModifierType": "MOD_ATTACH_CITIES"},
                {"ModifierId": "INNER", "ModifierType": "MOD_CITY_PLOTS", "SubjectRequirementSetId": "ALL_SET"},
                {"ModifierId": "UNKNOWN_REQ", "ModifierType": "MOD_CITY_FLAT", "SubjectRequirementSetId": "UNKNOWN_SET"},
            ],
            "ModifierArguments": [
                {"ModifierId": "ATTACH", "Name": "ModifierId", "Value": "INNER"},
                {"ModifierId": "INNER", "Name": "YieldType", "Value": "YIELD_GOLD"},
                {"ModifierId": "INNER", "Name": "Amount", "Value": "3"},
                {"ModifierId": "UNKNOWN_REQ", "Name": "YieldType", "Value": "YIELD_GOLD"},
                {"ModifierId": "UNKNOWN_REQ", "Name": "Amount", "Value": "100"},
            ],
            "DynamicModifiers": [
                {"ModifierType": "MOD_ATTACH_CITIES", "CollectionType": "COLLECTION_PLAYER_CITIES", "EffectType": "EFFECT_ATTACH_MODIFIER"},
                {"ModifierType": "MOD_CITY_PLOTS", "CollectionType": "COLLECTION_CITY_PLOT_YIELDS", "EffectType": "EFFECT_ADJUST_PLOT_YIELD"},
                {"ModifierType": "MOD_CITY_FLAT", "CollectionType": "COLLECTION_OWNER", "EffectType": "EFFECT_ADJUST_CITY_YIELD_CHANGE"},
            ],
        }})

    def test_requirement_any_all_inverse_and_unsupported(self):
        m, cities = seoul()
        tile(m, 6, 4)["feature"] = "FEATURE_FOREST"
        tile(m, 4, 4).update({"feature": "FEATURE_FOREST", "resource": "RESOURCE_WHEAT"})
        tile(m, 5, 3)["terrain"] = "TERRAIN_GRASS_HILLS"
        b = bundle(m, cities)
        ctx = build_context(b["map"], b["cities"], b["game"], real_catalog(), self.fake_db(), base_rules())
        evaluator = RequirementEvaluator(ctx)
        self.assertTrue(evaluator.evaluate_set("ANY_SET", plot_subject(5, 3, "city-a"))["met"])
        self.assertFalse(evaluator.evaluate_set("ANY_SET", plot_subject(4, 5, "city-a"))["met"])
        self.assertTrue(evaluator.evaluate_set("ALL_SET", plot_subject(6, 4, "city-a"))["met"])
        self.assertFalse(evaluator.evaluate_set("ALL_SET", plot_subject(4, 4, "city-a"))["met"])
        self.assertIsNone(evaluator.evaluate_set("UNKNOWN_SET", city_subject("city-a"))["met"])

    def test_attach_chain_applies_and_unsupported_requirement_is_logged(self):
        m, cities = seoul()
        tile(m, 6, 4)["feature"] = "FEATURE_FOREST"
        game = make_game(empire={"policy_slots": {"SLOT_ECONOMIC": ["POLICY_TEST"]}})
        b = bundle(m, cities, game)
        ctx = build_context(b["map"], b["cities"], b["game"], real_catalog(), self.fake_db(), base_rules())
        book = ModifierEngine(ctx).run()
        plots = [r for r in book.records if r.effect == "EFFECT_ADJUST_PLOT_YIELD"]
        self.assertEqual([(r.subject.x, r.subject.y) for r in plots], [(6, 4)])
        self.assertEqual(plots[0].source_id, "POLICY_TEST")
        self.assertTrue(any(s["modifier_id"] == "UNKNOWN_REQ" and s["category"] == "requirement_unsupported" for s in book.skipped))
        self.assertFalse(any(r.modifier_id == "UNKNOWN_REQ" for r in book.records))

    def test_policy_change_updates_city_yields(self):
        m, cities = seoul(population=1)
        base = city_totals(bundle(m, cities))
        game = make_game(empire={"government": "GOVERNMENT_CHIEFDOM",
                                 "policy_slots": {"SLOT_ECONOMIC": ["POLICY_GOD_KING"]}})
        god_king = city_totals(bundle(m, cities, game))
        # +1 each, then the HAPPY amenity state (+5% non-food, Happinesses.xml) applies.
        self.assertEqual(base["percent"]["Gold"], 5.0)
        self.assertAlmostEqual(god_king["totals"]["Gold"] - base["totals"]["Gold"], 1.05)
        self.assertAlmostEqual(god_king["totals"]["Faith"] - base["totals"]["Faith"], 1.05)
        self.assertTrue(any(c["kind"] == "city_flat" and c["attribution"]["source_id"] == "POLICY_GOD_KING"
                            for c in god_king["components"]))


class TurnEngineTests(unittest.TestCase):
    def scenario_bundle(self):
        m, cities = seoul(population=2, stored_food=5.0, citizen_mode="auto", strategy="balanced")
        tile(m, 5, 4)["river"] = True
        tile(m, 6, 4).update({"resource": "RESOURCE_WHEAT", "improvement": "IMPROVEMENT_FARM"})
        tile(m, 4, 4).update({"terrain": "TERRAIN_PLAINS_HILLS", "improvement": "IMPROVEMENT_MINE"})
        cities["city-a"]["production_queue"] = [
            {"queue_id": "q1", "category": "building", "target_id": "BUILDING_MONUMENT", "x": None, "y": None},
            {"queue_id": "q2", "category": "unit", "target_id": "UNIT_WARRIOR", "x": None, "y": None},
        ]
        game = make_game(research={"technology": {"current_id": "TECH_POTTERY", "progress": {}},
                                   "civic": {"current_id": "CIVIC_CODE_OF_LAWS", "progress": {}}})
        return bundle(m, cities, game)

    def test_same_input_gives_identical_multi_turn_results(self):
        first = simulate_turns(self.scenario_bundle(), statics(), 8)
        second = simulate_turns(self.scenario_bundle(), statics(), 8)
        self.assertEqual(json.dumps(first[0], sort_keys=True), json.dumps(second[0], sort_keys=True))
        self.assertEqual(json.dumps(first[2], sort_keys=True), json.dumps(second[2], sort_keys=True))

    def test_simulation_does_not_mutate_the_input_state(self):
        original = self.scenario_bundle()
        snapshot = json.dumps(original, sort_keys=True)
        simulate_turns(original, statics(), 5)
        self.assertEqual(json.dumps(original, sort_keys=True), snapshot)

    def test_history_accumulates_per_turn(self):
        final, reports, history = simulate_turns(self.scenario_bundle(), statics(), 10)
        self.assertEqual([row["turn"] for row in history], list(range(1, 11)))
        self.assertEqual(final["game"]["turn"], 11)
        completed = [item for row in history for item in row["cities"]["city-a"]["completed"]]
        self.assertIn("BUILDING_MONUMENT", completed)
        research = [r["id"] for row in history for r in row["empire"]["completed_research"]]
        self.assertIn("TECH_POTTERY", research)
        self.assertIn("TECH_POTTERY", final["game"]["technologies"])
        self.assertEqual(len(reports[0]["phases"]), 12)

    def test_research_cost_uses_game_speed_and_overflow_carries(self):
        b = self.scenario_bundle()
        b["game"]["research"]["technology"]["progress"] = {"TECH_POTTERY": 24.5}
        final, reports, _ = simulate_turns(b, statics(), 1)
        track = final["game"]["research"]["technology"]
        self.assertIsNone(track["current_id"])
        self.assertGreater(track.get("overflow", 0), 0)


class RulesConfigTests(unittest.TestCase):
    def test_simulation_section_is_validated(self):
        import tempfile
        from pathlib import Path
        from app.config import RuleConfigurationError, load_game_rules
        from tests.sim_fixtures import APP_DATA

        rules = json.loads((APP_DATA / "game_rules.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rules.json"
            rules["simulation"] = {"overflow_policy": "same_turn_queue", "population_yields_source": "global_parameters"}
            path.write_text(json.dumps(rules), encoding="utf-8")
            self.assertEqual(load_game_rules(path)["simulation"]["overflow_policy"], "same_turn_queue")
            rules["simulation"] = {"overflow_policy": "sometimes"}
            path.write_text(json.dumps(rules), encoding="utf-8")
            with self.assertRaises(RuleConfigurationError):
                load_game_rules(path)
            rules["simulation"] = {"typo_key": True}
            path.write_text(json.dumps(rules), encoding="utf-8")
            with self.assertRaises(RuleConfigurationError):
                load_game_rules(path)

    def test_population_yields_can_follow_global_parameters(self):
        m, cities = seoul(population=4)
        config = city_totals(bundle(m, cities))
        data = city_totals(bundle(m, cities), rules=base_rules(population_yields_source="global_parameters"))
        self.assertAlmostEqual(config["by_category"]["population"]["Science"], 2.0)
        self.assertAlmostEqual(data["by_category"]["population"]["Science"], 2.8)
        self.assertAlmostEqual(data["by_category"]["population"]["Culture"], 1.2)


if __name__ == "__main__":
    unittest.main()
