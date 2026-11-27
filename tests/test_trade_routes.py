"""Trade routes entered on a city, per-game amenity override, strategic adjacency."""

import unittest

from app.core.exceptions import ApplicationError
from app.services.citizen_assignment_service import apply_city_settings
from app.services.modifier_engine import run_modifiers
from app.services.sim_context import simulation_settings
from app.services.snapshot_service import validate_game_state_snapshot
from app.services.trade_route_service import destinations
from app.services.yield_breakdown_service import _adjacency_count, compute_city
from app.services.yield_ledger_service import build_ledger
from tests.sim_fixtures import base_rules, bundle, claim, context, make_city, make_game, make_map, tile


def world(game=None):
    game_map = make_map(width=22, height=9)
    origin = make_city("city-a", 3, 4, population=1, name="Origin")
    other = make_city("city-b", 11, 4, population=1, name="Second")
    claim(game_map, origin)
    claim(game_map, other)
    tile(game_map, 12, 4)["district"] = "DISTRICT_COMMERCIAL_HUB"
    other["districts"] = ["DISTRICT_COMMERCIAL_HUB"]
    # A foreign city kept on the map by a save import: centre + industrial zone.
    tile(game_map, 18, 4).update({"district": "DISTRICT_CITY_CENTER", "save_owner": 3})
    tile(game_map, 19, 4).update({"district": "DISTRICT_INDUSTRIAL_ZONE", "save_owner": 3})
    game_map["save_import"] = {"foreign_cities": [{"name": "Far", "x": 18, "y": 4, "player": 3, "label": "Rival"}]}
    return bundle(game_map, {"city-a": origin, "city-b": other}, game or make_game())


class TradeRouteTests(unittest.TestCase):
    def evaluate(self, routes, game=None):
        b = world(game)
        b["cities"]["city-a"]["trade_routes"] = routes
        ctx = context(b)
        book = run_modifiers(ctx)
        return ctx, compute_city(ctx, book, "city-a")

    def test_domestic_route_uses_destination_districts(self):
        _, result = self.evaluate([{"destination": "11,4"}])
        # City centre: +1 food +1 production; commercial hub: +1 production (domestic column).
        self.assertEqual(result["by_category"]["trade_routes"]["Food"], 1.0)
        self.assertEqual(result["by_category"]["trade_routes"]["Production"], 2.0)
        route = next(c for c in result["components"] if c["category"] == "trade_routes")
        self.assertIn("국내 교역로", route["label"])
        self.assertEqual({d["id"] for d in route["details"]}, {"DISTRICT_CITY_CENTER", "DISTRICT_COMMERCIAL_HUB"})

    def test_route_timing_and_trade_ledger(self):
        b = world()
        b["game"]["turn"] = 30
        b["cities"]["city-a"]["trade_routes"] = [{"destination": "11,4", "started_turn": 25}]
        ledger = build_ledger(context(b), "city-a")
        (route,) = ledger["trade_routes"]
        timing = route["timing"]
        # Distance 8, trader 2 moves → 4 turns one way, 8 round trip; 20-turn minimum (standard) → 3 trips = 24.
        self.assertEqual((timing["distance"], timing["round_trip_turns"], timing["duration_turns"]), (8, 8, 24))
        self.assertEqual(timing["turns_left"], 25 + 24 - 30)
        self.assertEqual({l["label"] for l in route["lines"]}, {"목적지 DISTRICT_CITY_CENTER", "목적지 DISTRICT_COMMERCIAL_HUB"})

    def test_international_route_to_foreign_city_on_map(self):
        _, result = self.evaluate([{"destination": "18,4"}])
        yields = result["by_category"]["trade_routes"]
        self.assertEqual((yields["Gold"], yields["Production"]), (3.0, 1.0))  # centre gold, industrial zone production

    def test_policy_adds_to_every_route(self):
        game = make_game(empire={"government": "GOVERNMENT_CHIEFDOM", "enforce_prerequisites": False,
                                 "policy_slots": {"SLOT_ECONOMIC": ["POLICY_CARAVANSARIES"]}})
        _, result = self.evaluate([{"destination": "11,4"}, {"destination": "18,4"}], game)
        self.assertEqual(result["by_category"]["trade_routes"]["Gold"], 3.0 + 2.0 + 2.0)

    def test_no_routes_no_section_and_bad_destination_warns(self):
        _, result = self.evaluate([])
        self.assertNotIn("trade_routes", result["by_category"])
        _, result = self.evaluate([{"destination": "5,5"}])
        self.assertNotIn("trade_routes", result["by_category"])
        self.assertTrue(any("도시가 없습니다" in w for w in result["warnings"]))

    def test_ledger_reconciles_with_route(self):
        b = world()
        b["cities"]["city-a"]["trade_routes"] = [{"destination": "18,4"}]
        ledger = build_ledger(context(b), "city-a")
        self.assertTrue(ledger["reconciliation"]["ok"], ledger["reconciliation"])
        self.assertTrue(any(line["section"] == "trade_routes" for line in ledger["lines"]))

    def test_settings_validate_routes(self):
        ctx = context(world())
        apply_city_settings(ctx, "city-a", {"trade_routes": [{"destination": "11,4"}]})
        self.assertEqual(ctx.cities["city-a"]["trade_routes"], [{"destination": "11,4"}])
        with self.assertRaises(ApplicationError):
            apply_city_settings(ctx, "city-a", {"trade_routes": [{"destination": "3,4"}]})  # its own centre
        with self.assertRaises(ApplicationError):
            apply_city_settings(ctx, "city-a", {"trade_routes": [{"destination": "oops"}]})
        names = {d["name"]: d["kind"] for d in destinations(ctx, "city-a")}
        self.assertEqual(names, {"Second": "domestic", "Far": "international"})


class GreatWorkTests(unittest.TestCase):
    def test_great_work_yields_and_validation(self):
        b = world()
        ctx = context(b)
        work = next(r["GreatWorkType"] for r in ctx.db.rows("GreatWork_YieldChanges") if r.get("YieldType") == "YIELD_CULTURE")
        apply_city_settings(ctx, "city-a", {"great_works": [work]})
        result = compute_city(ctx, run_modifiers(ctx), "city-a")
        self.assertGreater(result["by_category"]["great_works"]["Culture"], 0)
        self.assertTrue(build_ledger(ctx, "city-a")["reconciliation"]["ok"])
        with self.assertRaises(ApplicationError):
            apply_city_settings(ctx, "city-a", {"great_works": ["GREATWORK_NOPE"]})


class GameOverrideTests(unittest.TestCase):
    def test_amenity_override_per_game(self):
        rules = base_rules()
        self.assertTrue(simulation_settings(rules, make_game())["apply_amenity_effects"])
        game = make_game(simulation_overrides={"apply_amenity_effects": False, "apply_modifiers": False})
        settings = simulation_settings(rules, game)
        self.assertFalse(settings["apply_amenity_effects"])
        self.assertTrue(settings["apply_modifiers"])  # only listed switches can be overridden

    def test_snapshot_keeps_override(self):
        b = world(make_game(simulation_overrides={"apply_amenity_effects": False, "junk": 1}))
        state = validate_game_state_snapshot(b["game"], b["cities"], b["map"])
        self.assertEqual(state["simulation_overrides"], {"apply_amenity_effects": False})


class StrategicAdjacencyTests(unittest.TestCase):
    def test_counts_only_discovered_strategic_resources(self):
        b = world()
        tile(b["map"], 4, 4)["resource"] = "RESOURCE_IRON"
        rule = {"AdjacentResourceClass": "RESOURCECLASS_STRATEGIC"}
        ctx = context(b)
        count, _ = _adjacency_count(ctx, tile(b["map"], 3, 4), rule, set())
        self.assertEqual(count, 0)  # iron needs Bronze Working to be seen
        b["game"]["technologies"] = ["TECH_BRONZE_WORKING"]
        ctx = context(b)
        count, note = _adjacency_count(ctx, tile(b["map"], 3, 4), rule, set())
        self.assertEqual(count, 1, note)


if __name__ == "__main__":
    unittest.main()


class CityStateTests(unittest.TestCase):
    """Envoy bonuses from MINOR_CIV_*_TRAIT (expansion rules) and build-type production."""

    @classmethod
    def setUpClass(cls):
        from app.repositories.rules_db_repository import load_rules_db
        cls.xp2 = load_rules_db("RULESET_EXPANSION_2")
        if not cls.xp2.available:
            raise unittest.SkipTest("rules_db_xp2.json not built")

    def build(self, envoys, queue_category="building", target="BUILDING_GRANARY"):
        from app.services.sim_context import build_context
        from tests.sim_fixtures import statics

        b = world(make_game(empire={"enforce_prerequisites": False,
                                    "city_states": {"CIVILIZATION_BRUSSELS": {"envoys": envoys, "suzerain": False}}}))
        origin = b["cities"]["city-a"]
        origin["buildings"] = ["BUILDING_WORKSHOP"]
        origin["production_queue"] = [{"queue_id": "q1", "category": queue_category, "target_id": target}]
        b["map"]["save_import"]["players"] = {"11": {"civilization": "CIVILIZATION_BRUSSELS"}}
        b["map"]["save_import"]["foreign_cities"].append({"name": "Brussels", "x": 15, "y": 7, "player": 11})
        tile(b["map"], 15, 7)["district"] = "DISTRICT_CITY_CENTER"
        st = statics()
        return build_context(b["map"], b["cities"], b["game"], st.catalog, self.xp2, st.rules)

    def test_one_envoy_gives_flat_production_toward_buildings(self):
        from app.services.city_state_service import game_city_states
        from app.services.production_engine import production_flat

        ctx = self.build(1)
        self.assertEqual([c["type"] for c in game_city_states(ctx)], ["INDUSTRIAL"])
        book = run_modifiers(ctx)
        flat, parts = production_flat(ctx, book, "city-a", "building")
        self.assertGreaterEqual(flat, 1.0)  # workshop city (+ capital) bonus
        self.assertTrue(all("CIVILIZATION_BRUSSELS" in p["label"] for p in parts))
        self.assertEqual(production_flat(ctx, book, "city-a", "unit")[0], 0.0)
        self.assertEqual(production_flat(ctx, run_modifiers(self.build(0)), "city-a", "building")[0], 0.0)

    def test_route_to_city_state_gets_its_bonus(self):
        ctx = self.build(0)
        ctx.cities["city-a"]["trade_routes"] = [{"destination": "15,7"}]
        result = compute_city(ctx, run_modifiers(ctx), "city-a")
        route = next(c for c in result["components"] if c["category"] == "trade_routes")
        self.assertTrue(any(d.get("kind") == "cs_trade" for d in route["details"]), route["details"])

    def test_empire_update_validates_city_states(self):
        from app.services.empire_service import sanitize_empire, validate_empire_update

        game = make_game()
        empire, _ = validate_empire_update(self.xp2, game, {"city_states": {"CIVILIZATION_BRUSSELS": {"envoys": 3}}})
        self.assertEqual(empire["city_states"], {"CIVILIZATION_BRUSSELS": {"envoys": 3, "suzerain": False}})
        with self.assertRaises(ApplicationError):
            validate_empire_update(self.xp2, game, {"city_states": {"CIVILIZATION_GREECE": {"envoys": 1}}})
        self.assertEqual(sanitize_empire({"city_states": {"CIVILIZATION_X": {"envoys": 0}}})["city_states"], {})


class ExpansionCityRuleTests(unittest.TestCase):
    """Population yields in 1/256 fixed point and the monument's full-loyalty culture (expansion rules)."""

    def test_population_culture_and_monument(self):
        from app.repositories.rules_db_repository import load_rules_db
        from app.services.sim_context import build_context
        from tests.sim_fixtures import statics

        xp2 = load_rules_db("RULESET_EXPANSION_2")
        if not xp2.available:
            self.skipTest("rules_db_xp2.json not built")
        b = world(make_game(empire={"enforce_prerequisites": False}))
        b["cities"]["city-a"].update(population=11, buildings=["BUILDING_MONUMENT"])
        st = statics()

        def evaluate():
            ctx = build_context(b["map"], b["cities"], b["game"], st.catalog, xp2, st.rules)
            return compute_city(ctx, run_modifiers(ctx), "city-a")

        result = evaluate()
        self.assertAlmostEqual(result["by_category"]["population"]["Culture"], 3.3)
        self.assertAlmostEqual(result["by_category"]["population"]["Science"], 5.5)
        self.assertEqual(result["by_category"]["modifiers"]["Culture"], 1.0)  # +1 at full loyalty
        b["cities"]["city-a"]["full_loyalty"] = False
        self.assertNotIn("Culture", {k for k, v in evaluate()["by_category"].get("modifiers", {}).items() if v})


class BuildBonusTests(CityStateTests):
    """The head item's envoy bonus is part of city production; other queue items swap it out."""

    def test_build_bonus_in_city_production_and_queue(self):
        from app.services.production_engine import queue_status

        ctx = self.build(1)
        ctx.cities["city-a"]["production_queue"].append({"queue_id": "q2", "category": "unit", "target_id": "UNIT_SCOUT"})
        book = run_modifiers(ctx)
        result = compute_city(ctx, book, "city-a")
        bonus = result["by_category"]["build_bonus"]["Production"]
        self.assertGreaterEqual(bonus, 1.0)
        total = result["totals"]["Production"]
        queue = queue_status(ctx, book, "city-a", total)
        self.assertAlmostEqual(queue[0]["effective_production"], total)  # granary: bonus already counted once
        self.assertAlmostEqual(queue[1]["effective_production"], total - bonus)  # unit: industrial bonus does not apply
