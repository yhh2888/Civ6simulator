"""Tests for the city yield ledger (line-by-line account of city yields)."""

import copy
import unittest

from app.repositories.rules_db_repository import RulesDB
from app.services.sim_context import build_context
from app.services.yield_ledger_service import build_ledger, ledger_csv

from tests.sim_fixtures import base_rules, bundle, claim, context, make_city, make_game, make_map, real_catalog, real_db_payload, tile


def rich_city():
    m = make_map()
    city = make_city(population=7, buildings=["BUILDING_PALACE", "BUILDING_LIBRARY", "BUILDING_MONUMENT"],
                     districts=["DISTRICT_CAMPUS"])
    claim(m, city)
    tile(m, 6, 4)["district"] = "DISTRICT_CAMPUS"
    tile(m, 7, 4)["terrain"] = "TERRAIN_GRASS_MOUNTAIN"
    tile(m, 6, 3)["terrain"] = "TERRAIN_GRASS_MOUNTAIN"
    tile(m, 4, 4).update({"resource": "RESOURCE_WHEAT", "improvement": "IMPROVEMENT_FARM", "worked": True})
    tile(m, 5, 3).update({"terrain": "TERRAIN_PLAINS_HILLS", "improvement": "IMPROVEMENT_MINE", "worked": True})
    tile(m, 4, 5).update({"resource": "RESOURCE_NITER", "worked": True})
    tile(m, 4, 3).update({"feature": "FEATURE_FOREST", "worked": True})
    city["specialists"] = {"6,4": 1}
    tile(m, 6, 4)["worked"] = True
    game = make_game(technologies=["TECH_APPRENTICESHIP"], civics=["CIVIC_RECORDED_HISTORY", "CIVIC_CODE_OF_LAWS"],
                     empire={"government": "GOVERNMENT_CHIEFDOM",
                             "policy_slots": {"SLOT_ECONOMIC": ["POLICY_NATURAL_PHILOSOPHY"], "SLOT_MILITARY": ["POLICY_AGOGE"]}})
    return bundle(m, {city["id"]: city}, game)


class YieldLedgerTests(unittest.TestCase):
    def test_ledger_lines_add_up_to_city_totals(self):
        ledger = build_ledger(context(rich_city()), "city-a")
        self.assertTrue(ledger["reconciliation"]["ok"], ledger["reconciliation"])
        last = ledger["lines"][-1]["running"]
        for key, value in ledger["totals"].items():
            self.assertAlmostEqual(last[key], value, places=6)
        sections = [s["id"] for s in ledger["sections"]]
        for expected in ("tiles", "specialists", "population", "districts", "buildings", "percent"):
            self.assertIn(expected, sections)
        self.assertEqual([line["no"] for line in ledger["lines"]], list(range(1, len(ledger["lines"]) + 1)))

    def test_lines_cite_data_zip_rows(self):
        ledger = build_ledger(context(rich_city()), "city-a")
        terrain = next(l for l in ledger["lines"] if l["kind"] == "terrain" and l["location"] == "4,4")
        self.assertEqual(terrain["source"]["origin"], "Data.zip")
        self.assertEqual(terrain["source"]["file"], "Terrains.xml")
        self.assertEqual(terrain["source"]["rows"][0]["TerrainType"], "TERRAIN_GRASS")
        bonus = next(l for l in ledger["lines"] if l["kind"] == "improvement_bonus")
        self.assertEqual(bonus["source"]["rows"][0]["PrereqTech"], "TECH_APPRENTICESHIP")
        population = next(l for l in ledger["lines"] if l["kind"] == "population")
        self.assertEqual(population["source"]["origin"], "game_rules.json")

    def test_modifier_lines_explain_why_they_applied(self):
        ledger = build_ledger(context(rich_city()), "city-a")
        line = next(l for l in ledger["lines"] if l["kind"] == "adjacency_percent")
        modifier = line["source"]["modifier"]
        self.assertEqual(modifier["link"]["table"], "PolicyModifiers")
        self.assertEqual(modifier["link"]["rows"][0]["PolicyType"], "POLICY_NATURAL_PHILOSOPHY")
        self.assertEqual(modifier["effect"], "EFFECT_ADJUST_DISTRICT_YIELD_MODIFIER")
        subject = modifier["requirements"]["subject"]
        self.assertTrue(subject["met"])
        self.assertTrue(all(r["met"] for r in subject["requirements"]))
        self.assertIn("인접 과학 2 × +100% = +2", line["formula"])

    def test_excluded_items_are_listed_with_reasons(self):
        ledger = build_ledger(context(rich_city()), "city-a")
        by_kind = {e["kind"]: e for e in ledger["excluded"]}
        self.assertIn("발견 기술", by_kind["resource"]["reason"])
        self.assertIn("unworked_tile", by_kind)
        self.assertTrue(any(e["kind"] == "improvement_bonus" and "TECH_INDUSTRIALIZATION" in e["reason"] for e in ledger["excluded"]))

    def test_growth_and_production_sub_ledgers(self):
        b = rich_city()
        b["cities"]["city-a"]["production_queue"] = [{"queue_id": "w", "category": "unit", "target_id": "UNIT_WARRIOR", "x": None, "y": None}]
        ledger = build_ledger(context(b), "city-a")
        labels = [g["label"] for g in ledger["growth"]]
        self.assertIn("식량 소비", labels)
        self.assertIn("주거공간 배율", labels)
        food_line = ledger["growth"][0]
        self.assertAlmostEqual(food_line["value"], ledger["totals"]["Food"], places=4)
        self.assertEqual(ledger["production"]["current"]["bonus_percent"], 50.0)
        text = ledger_csv(ledger)
        self.assertTrue(text.startswith("﻿"))
        self.assertIn("Terrains.xml", text)
        self.assertIn("검산,일치", text)

    def test_base_yields_are_read_from_rules_db_not_catalog(self):
        payload = copy.deepcopy(real_db_payload())
        for row in payload["tables"]["Terrain_YieldChanges"]:
            if row["TerrainType"] == "TERRAIN_GRASS" and row["YieldType"] == "YIELD_FOOD":
                row["YieldChange"] = "5"
        db = RulesDB(payload)
        b = rich_city()
        ctx = build_context(b["map"], b["cities"], b["game"], real_catalog(), db, base_rules())
        ledger = build_ledger(ctx, "city-a")
        terrain = next(l for l in ledger["lines"] if l["kind"] == "terrain" and l["location"] == "4,4")
        self.assertEqual(terrain["yields"]["Food"], 5.0)
        self.assertEqual(ledger["data_origin"]["base_yields"], "Data.zip (rules_db.json)")


if __name__ == "__main__":
    unittest.main()
