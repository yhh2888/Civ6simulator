"""Governors, World Congress, incoming trade routes, tile corrections, per-yield ledger."""

import struct
import unittest

from app.core.exceptions import ApplicationError
from app.services.citizen_assignment_service import apply_city_settings
from app.services.civ6save.decoder import _congress, _governors
from app.services.civ6save.hashes import TypeNames, type_hash
from app.services.modifier_engine import run_modifiers
from app.services.yield_breakdown_service import compute_city
from app.services.yield_ledger_service import build_ledger, empire_yield_ledger
from tests.sim_fixtures import make_game, statics
from tests.test_trade_routes import world


def xp2():
    from app.repositories.rules_db_repository import load_rules_db

    db = load_rules_db("RULESET_EXPANSION_2")
    if not db.available:
        raise unittest.SkipTest("rules_db_xp2.json not built")
    return db


def xp2_context(b):
    from app.services.sim_context import build_context

    st = statics()
    return build_context(b["map"], b["cities"], b["game"], st.catalog, xp2(), st.rules)


class GovernorTests(unittest.TestCase):
    def city(self, established=True):
        b = world(make_game(empire={"enforce_prerequisites": False}))
        b["cities"]["city-a"].update(population=10, governor="GOVERNOR_THE_EDUCATOR", governor_established=established,
                                     governor_promotions=["GOVERNOR_PROMOTION_EDUCATOR_LIBRARIAN",
                                                          "GOVERNOR_PROMOTION_EDUCATOR_CONNOISSEUR",
                                                          "GOVERNOR_PROMOTION_EDUCATOR_RESEARCHER"])
        ctx = xp2_context(b)
        return ctx, compute_city(ctx, run_modifiers(ctx), "city-a")

    def test_promotions_apply_once_established(self):
        _, result = self.city()
        mods = result["by_category"]["modifiers"]
        self.assertEqual((mods["Culture"], mods["Science"]), (10.0, 10.0))  # +1 per citizen each
        self.assertIn("Science", result["percent"])
        self.assertEqual(result["percent"]["Science"], 15.0)  # Librarian
        _, waiting = self.city(established=False)
        self.assertNotIn("modifiers", {k for k, v in waiting["by_category"].items() if any(v.values())})
        self.assertEqual(waiting["percent"].get("Science", 0.0), 0.0)

    def test_settings_validate_promotions(self):
        ctx, _ = self.city()
        with self.assertRaises(ApplicationError):
            apply_city_settings(ctx, "city-a", {"governor_promotions": ["GOVERNOR_PROMOTION_REDOUBT"]})
        apply_city_settings(ctx, "city-a", {"governor": "GOVERNOR_THE_BUILDER"})
        self.assertEqual(ctx.cities["city-a"]["governor_promotions"], [])  # reset on change

    def test_zoning_commissioner_speeds_districts(self):
        from app.services.production_engine import production_bonus

        b = world(make_game(empire={"enforce_prerequisites": False}))
        b["cities"]["city-a"].update(governor="GOVERNOR_THE_BUILDER", governor_established=True,
                                     governor_promotions=["GOVERNOR_PROMOTION_ZONING_COMMISSIONER"])
        ctx = xp2_context(b)
        pct, parts = production_bonus(ctx, run_modifiers(ctx), "city-a", "district", "DISTRICT_CAMPUS")
        self.assertEqual(pct, 20.0)
        self.assertTrue(any("ZONING" in p["label"] for p in parts))


class CongressAndIncomingTests(unittest.TestCase):
    def build(self, targets_me=True):
        b = world(make_game(empire={"enforce_prerequisites": False, "world_congress": [
            {"resolution": "WC_RES_TRADE_TREATY", "effect": 1, "targets_me": targets_me}]}))
        b["cities"]["city-a"]["incoming_routes"] = [{"origin": "18,4", "origin_name": "Far"}]
        return xp2_context(b)

    def test_trade_treaty_adds_gold_per_incoming_route(self):
        ctx = self.build()
        result = compute_city(ctx, run_modifiers(ctx), "city-a")
        self.assertEqual(result["by_category"]["incoming_routes"]["Gold"], 4.0)
        ledger = build_ledger(ctx, "city-a")
        self.assertTrue(ledger["reconciliation"]["ok"])
        self.assertIn("incoming_routes", {s["id"] for s in ledger["sections"]})

    def test_not_target_or_no_routes(self):
        ctx = self.build(targets_me=False)
        self.assertNotIn("incoming_routes", compute_city(ctx, run_modifiers(ctx), "city-a")["by_category"])
        ctx = self.build()
        ctx.cities["city-a"]["incoming_routes"] = []
        self.assertNotIn("incoming_routes", compute_city(ctx, run_modifiers(ctx), "city-a")["by_category"])

    def test_empire_options_list_resolutions_and_governors(self):
        from app.services.empire_service import empire_options

        options = empire_options(xp2(), make_game())
        treaty = next(r for r in options["resolutions"] if r["id"] == "WC_RES_TRADE_TREATY")
        self.assertIn("APPLY_INCREASES_TRADE_TO_GOLD_TO_PLAYER", treaty["effects"]["1"])
        self.assertTrue(options["governors"]["available"])
        self.assertIn("GOVERNOR_THE_EDUCATOR", {g["id"] for g in options["governors"]["list"]})

    def test_empire_keeps_congress(self):
        from app.services.empire_service import sanitize_empire, validate_empire_update

        game = make_game(empire={"world_congress": [{"resolution": "WC_RES_TRADE_TREATY", "effect": 1, "targets_me": True}]})
        self.assertEqual(sanitize_empire(game["empire"])["world_congress"][0]["effect"], 1)
        with self.assertRaises(ApplicationError):
            validate_empire_update(xp2(), game, {"world_congress": [{"resolution": "NOT_A_RESOLUTION", "effect": 1}]})


class TileAdjustAndYieldLedgerTests(unittest.TestCase):
    def test_tile_adjustment_and_empire_ledger(self):
        b = world()
        ctx = xp2_context(b)
        before = compute_city(ctx, run_modifiers(ctx), "city-a")["totals"]["Food"]
        apply_city_settings(ctx, "city-a", {"tile_adjustments": {"3,4": {"yields": {"Food": 1}, "note": "flood"}}})
        result = compute_city(ctx, run_modifiers(ctx), "city-a")
        self.assertEqual(result["totals"]["Food"], before + 1)
        self.assertTrue(build_ledger(ctx, "city-a")["reconciliation"]["ok"])
        with self.assertRaises(ApplicationError):
            apply_city_settings(ctx, "city-a", {"tile_adjustments": {"bad": {"yields": {"Food": 1}}}})
        ledger = empire_yield_ledger(ctx, "Food")
        self.assertEqual({c["city_id"] for c in ledger["cities"]}, {"city-a", "city-b"})
        self.assertAlmostEqual(ledger["total"], sum(c["total"] for c in ledger["cities"]))
        for c in ledger["cities"]:
            self.assertAlmostEqual(sum(c["sections"].values()), c["total"])
        with self.assertRaises(ApplicationError):
            empire_yield_ledger(ctx, "Tourism")


class DecoderTests(unittest.TestCase):
    def test_governor_record(self):
        names = TypeNames(["GOVERNOR_THE_BUILDER", "GOVERNOR_PROMOTION_BUILDER_GUILDMASTER", "GOVERNOR_PROMOTION_ZONING_COMMISSIONER"])
        label = b"LOC_GOVERNOR_THE_BUILDER_NAME\x00"
        record = b"\x00" + struct.pack("<HHH", 2, 6, 8) + b"\x00" * 20 + struct.pack("<HHHH", 2, 6, 8, 5) + b"\x00\x00"
        promos = (struct.pack("<I", type_hash("GOVERNOR_PROMOTION_BUILDER_GUILDMASTER")) + b"\x01"
                  + struct.pack("<I", type_hash("GOVERNOR_PROMOTION_ZONING_COMMISSIONER")) + b"\x00")
        body = b"\x00" * 16 + struct.pack("<II", type_hash("GOVERNOR_THE_BUILDER"), len(label) - 1) + label + record + promos + b"\xff" * 8
        (gov,) = _governors(body, names, [])
        self.assertEqual((gov["type"], gov["city_index"], gov["turns"]), ("GOVERNOR_THE_BUILDER", 6, 5))
        self.assertEqual(gov["promotions"], ["GOVERNOR_PROMOTION_BUILDER_GUILDMASTER"])

    def test_congress_votes(self):
        names = TypeNames(["WC_RES_TRADE_TREATY"])
        body = (b"\x00" * 8 + struct.pack("<II", type_hash("WC_RES_TRADE_TREATY"), type_hash("PLAYER"))
                + struct.pack("<i", 0) + struct.pack("<5i", 1, 1, 2, 1, 2) + b"\xff" * 8)
        (item,) = _congress(body, names)
        self.assertEqual((item["target_kind"], item["target"], item["effect"]), ("PLAYER", 0, 1))


if __name__ == "__main__":
    unittest.main()


class PlotYieldTests(unittest.TestCase):
    def test_decode_plot_yield_list(self):
        from app.services.civ6save.decoder import PLOT_YIELD_ORDER, _plot_yields

        width, height = 4, 2
        template = struct.pack("<I", 6) + b"".join(struct.pack("<Ii", type_hash(y), 0) for y in PLOT_YIELD_ORDER)
        slots = [-1] * 8
        slots[5] = 0  # (1, 1)
        slots[2] = 1  # (2, 0)
        entry = lambda food, prod: struct.pack("<I", 6) + b"".join(
            struct.pack("<Ii", type_hash(y), {"YIELD_FOOD": food, "YIELD_PRODUCTION": prod}.get(y, 0)) for y in PLOT_YIELD_ORDER)
        body = (b"\x00" * 10 + template + struct.pack("<I", 8) + struct.pack("<8i", *slots)
                + struct.pack("<I", 2) + entry(1, 1) + entry(2, 0))
        self.assertEqual(_plot_yields(body, width, height),
                         {(1, 1): {"YIELD_FOOD": 1, "YIELD_PRODUCTION": 1}, (2, 0): {"YIELD_FOOD": 2}})

    def test_event_yields_added_to_tile(self):
        b = world()
        ctx = xp2_context(b)
        before = compute_city(ctx, run_modifiers(ctx), "city-a")
        tile = next(t for t in b["map"]["tiles"] if (t["x"], t["y"]) == (3, 4))
        tile["event_yields"] = {"Food": 1, "Production": 1}
        ctx = xp2_context(b)
        after = compute_city(ctx, run_modifiers(ctx), "city-a")
        self.assertEqual(after["totals"]["Food"], before["totals"]["Food"] + 1)
        self.assertTrue(build_ledger(ctx, "city-a")["reconciliation"]["ok"])


class PillageTests(unittest.TestCase):
    def test_pillaged_improvement_gives_nothing(self):
        from app.services.yield_breakdown_service import compute_tile

        b = world()
        tile = next(t for t in b["map"]["tiles"] if (t["x"], t["y"]) == (3, 4))
        tile["improvement"] = "IMPROVEMENT_FARM"
        ctx = xp2_context(b)
        farm = compute_tile(ctx, run_modifiers(ctx), ctx.tile(3, 4), "city-a", worked=True, is_center=False)["yields"]["Food"]
        tile["improvement_pillaged"] = True
        ctx = xp2_context(b)
        pillaged = compute_tile(ctx, run_modifiers(ctx), ctx.tile(3, 4), "city-a", worked=True, is_center=False)
        self.assertEqual(pillaged["yields"]["Food"], farm - 1)
        self.assertTrue(any(c["kind"] == "improvement_pillaged" for c in pillaged["components"]))

    def test_requirement_treats_pillaged_as_absent(self):
        from app.services.requirement_engine import REQUIREMENT_HANDLERS, plot_subject

        b = world()
        tile = next(t for t in b["map"]["tiles"] if (t["x"], t["y"]) == (3, 4))
        tile.update(improvement="IMPROVEMENT_FARM", improvement_pillaged=True)
        ctx = xp2_context(b)
        check = REQUIREMENT_HANDLERS["REQUIREMENT_PLOT_IMPROVEMENT_TYPE_MATCHES"]
        ok, _ = check(ctx, plot_subject(3, 4, "city-a"), {"ImprovementType": "IMPROVEMENT_FARM"})
        self.assertFalse(ok)


class GreatWorkAndEraTests(unittest.TestCase):
    def test_great_work_list_and_city_slots(self):
        from app.services.civ6save.decoder import _city_great_works, _great_work_list

        names = TypeNames(["GREATWORK_SUN_TZU", "GREATWORK_BABYLON_RUMI_1", "BUILDING_PALACE", "BUILDING_AMPHITHEATER"])
        record = lambda work: (struct.pack("<II", 6, type_hash(work)) + struct.pack("<III", 0, 57, 4) + b"NAME")
        body = b"\x00" * 8 + struct.pack("<I", 2) + record("GREATWORK_SUN_TZU") + record("GREATWORK_BABYLON_RUMI_1") + b"\x00" * 8
        works = _great_work_list(body, names)
        self.assertEqual(works, ["GREATWORK_SUN_TZU", "GREATWORK_BABYLON_RUMI_1"])
        buildings = {struct.pack("<I", type_hash(n)): n for n in ("BUILDING_PALACE", "BUILDING_AMPHITHEATER")}
        city = (b"\x00" * 6 + struct.pack("<I", 2)
                + struct.pack("<I", type_hash("BUILDING_PALACE")) + struct.pack("<I", 1) + struct.pack("<III", 10, 4, 1)
                + struct.pack("<I", type_hash("BUILDING_AMPHITHEATER")) + struct.pack("<I", 2)
                + struct.pack("<III", 10, 6, 0) + struct.pack("<III", 10, 6, 0xFFFFFFFF) + b"\x00" * 8)
        found = _city_great_works(city, 0, len(city), {"BUILDING_PALACE", "BUILDING_AMPHITHEATER"}, works, buildings)
        self.assertEqual(found, ["GREATWORK_BABYLON_RUMI_1", "GREATWORK_SUN_TZU"])

    def test_era_at_least(self):
        from app.services.requirement_engine import REQUIREMENT_HANDLERS, city_subject

        b = world(make_game(empire={"enforce_prerequisites": False}))
        ctx = xp2_context(b)
        check = REQUIREMENT_HANDLERS["REQUIREMENT_PLAYER_ERA_AT_LEAST"]
        ctx.player.technologies = {"TECH_MINING"}
        self.assertFalse(check(ctx, city_subject("city-a"), {"EraType": "ERA_INDUSTRIAL"})[0])
        ctx.player.technologies = {"TECH_MINING", "TECH_INDUSTRIALIZATION"}
        self.assertTrue(check(ctx, city_subject("city-a"), {"EraType": "ERA_INDUSTRIAL"})[0])


class ExportCsvTests(unittest.TestCase):
    def test_export_columns(self):
        from app.services.save_import_service import EXPORT_COLUMNS, _trunc

        self.assertEqual(_trunc(15.5999994), 15.5)
        self.assertEqual(_trunc(7.0), 7.0)
        self.assertIn("science", EXPORT_COLUMNS)
        self.assertIn("amenity_state", EXPORT_COLUMNS)
