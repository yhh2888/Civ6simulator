"""HTTP-level tests for simulation, scenarios, persistence and validation."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.core import paths
from app.repositories.json_repository import invalidate_active_document_cache
from app.repositories.rules_db_repository import clear_rules_db_cache

from tests.sim_fixtures import APP_DATA, claim, make_city, make_game, make_map, tile


class SimulationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory()
        shared = Path(cls.shared.name)
        for name in ("catalog.json", "rules_db.json", "game_rules.json", "data_audit_report.json"):
            shutil.copy((APP_DATA.parent / "config" if name == "game_rules.json" else APP_DATA) / name, shared / name)

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()
        clear_rules_db_cache()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name) / "data"
        self.data.mkdir()
        for name in ("catalog.json", "rules_db.json", "game_rules.json", "data_audit_report.json"):
            (self.data / name).symlink_to(Path(self.shared.name) / name)
        self.map = make_map()
        city = make_city(population=3, stored_food=4.0)
        claim(self.map, city)
        tile(self.map, 5, 4)["river"] = True
        tile(self.map, 6, 4).update({"terrain": "TERRAIN_PLAINS_HILLS", "improvement": "IMPROVEMENT_MINE"})
        tile(self.map, 4, 4).update({"resource": "RESOURCE_WHEAT", "improvement": "IMPROVEMENT_FARM"})
        tile(self.map, 6, 4)["worked"] = True
        self.cities = {city["id"]: city}
        self.game = make_game(civics=["CIVIC_CODE_OF_LAWS"],
                              research={"technology": {"current_id": "TECH_POTTERY", "progress": {}},
                                        "civic": {"current_id": None, "progress": {}}})
        self.write("map.json", self.map)
        self.write("cities.json", self.cities)
        self.write("game_state.json", self.game)
        self.patches = [
            patch.object(main, "DATA", self.data), patch.object(main, "SAVED_MAPS", self.data / "saved_maps"),
            patch.object(main, "GAME_STATE", self.data / "game_state.json"),
            patch.object(paths, "DATA", self.data), patch.object(paths, "SAVED_MAPS", self.data / "saved_maps"),
            patch.object(paths, "GAME_STATE", self.data / "game_state.json"),
        ]
        for item in self.patches:
            item.start()
        clear_rules_db_cache()
        self.client = TestClient(main.app)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        for name in ("map.json", "cities.json", "game_state.json"):
            invalidate_active_document_cache(self.data / name)
        self.temp.cleanup()

    def write(self, name, value):
        path = self.data / name
        path.write_text(json.dumps(value), encoding="utf-8")
        invalidate_active_document_cache(path)

    def ok(self, response):
        self.assertLess(response.status_code, 400, response.text)
        return response.json()

    # ------------------------------------------------------------------
    def test_city_detail_has_breakdown_next_turn_and_coverage(self):
        data = self.ok(self.client.get("/api/sim/cities/city-a"))
        self.assertIn("components", data["city"]["yields"])
        self.assertEqual(data["next_turn"]["current"]["population"], 3)
        self.assertEqual(len(data["phases"]), 12)
        statuses = {item["status"] for item in data["coverage"]}
        self.assertTrue(statuses <= {"검증 완료", "부분 구현", "미검증", "미지원"})
        self.assertNotIn("검증 완료", statuses)

    def test_preview_does_not_change_state_and_commit_does(self):
        before = self.ok(self.client.get("/api/game"))["turn"]
        self.ok(self.client.post("/api/sim/turn/preview", json={"turns": 3}))
        self.assertEqual(self.ok(self.client.get("/api/game"))["turn"], before)
        result = self.ok(self.client.post("/api/sim/turn", json={"turns": 2, "city_id": "city-a"}))
        self.assertEqual(result["turn"], before + 2)
        self.assertEqual(self.ok(self.client.get("/api/game"))["turn"], before + 2)
        self.assertEqual(len(self.ok(self.client.get("/api/sim/history"))["history"]), 2)
        # Committed only in memory: disk unchanged until 저장.
        self.assertEqual(json.loads((self.data / "game_state.json").read_text())["turn"], before)

    def test_scenarios_are_independent_and_preserve_the_game(self):
        game_turn = self.ok(self.client.get("/api/game"))["turn"]
        a = self.ok(self.client.post("/api/scenarios", json={"name": "A"}))
        b = self.ok(self.client.post("/api/scenarios", json={"name": "B"}))
        self.ok(self.client.put(f"/api/sim/cities/city-a/settings?scenario_id={a['id']}",
                                json={"citizen_mode": "auto", "strategy": "food"}))
        self.ok(self.client.post(f"/api/sim/turn?scenario_id={a['id']}", json={"turns": 4}))
        self.assertEqual(self.ok(self.client.get(f"/api/scenarios/{a['id']}"))["turn"], game_turn + 4)
        self.assertEqual(self.ok(self.client.get(f"/api/scenarios/{b['id']}"))["turn"], game_turn)
        self.assertEqual(self.ok(self.client.get("/api/game"))["turn"], game_turn)
        city = self.ok(self.client.get("/api/cities"))[0]
        self.assertNotEqual(city.get("strategy"), "food")
        reset = self.ok(self.client.post(f"/api/scenarios/{a['id']}/reset"))
        self.assertEqual(reset["turn"], game_turn)
        self.assertEqual(reset["turns_simulated"], 0)

    def test_strategy_batch_compares_real_simulations(self):
        result = self.ok(self.client.post("/api/scenarios/strategy-batch",
                                          json={"city_id": "city-a", "strategies": ["food", "production"], "turns": 5}))
        rows = {row["name"].split(" · ")[-1]: row for row in result["comparison"]["rows"]}
        self.assertEqual(set(rows), {"식량 집중", "생산력 집중"})
        self.assertGreaterEqual(rows["식량 집중"]["final"]["cumulative"]["Food"],
                                rows["생산력 집중"]["final"]["cumulative"]["Food"])
        self.assertGreaterEqual(rows["생산력 집중"]["final"]["cumulative"]["Production"],
                                rows["식량 집중"]["final"]["cumulative"]["Production"])
        self.assertTrue(all(len(row["series"]) == 5 for row in rows.values()))
        listed = self.ok(self.client.get("/api/scenarios"))["scenarios"]
        self.assertEqual(len(listed), 2)
        self.ok(self.client.delete(f"/api/scenarios/{result['created'][0]}"))
        self.assertEqual(len(self.ok(self.client.get("/api/scenarios"))["scenarios"]), 1)

    def test_save_and_load_reproduce_identical_results(self):
        self.ok(self.client.put("/api/sim/empire", json={"government": "GOVERNMENT_CHIEFDOM",
                                                         "policy_slots": {"SLOT_ECONOMIC": ["POLICY_URBAN_PLANNING"]},
                                                         "enforce_prerequisites": False}))
        self.ok(self.client.put("/api/sim/cities/city-a/settings", json={"citizen_mode": "auto", "strategy": "production"}))
        self.ok(self.client.post("/api/sim/turn", json={"turns": 3}))
        before = self.ok(self.client.get("/api/sim/state"))
        self.ok(self.client.post("/api/maps/save-as", json={"name": "sim-roundtrip"}))
        self.ok(self.client.post("/api/sim/turn", json={"turns": 2}))
        self.ok(self.client.post("/api/maps/load", json={"name": "sim-roundtrip"}))
        after = self.ok(self.client.get("/api/sim/state"))
        self.assertEqual(json.dumps(before["cities"], sort_keys=True), json.dumps(after["cities"], sort_keys=True))
        self.assertEqual(before["empire"]["policies"], ["POLICY_URBAN_PLANNING"])
        self.assertEqual(after["empire"]["policies"], ["POLICY_URBAN_PLANNING"])
        future_a = self.ok(self.client.post("/api/sim/turn/preview", json={"turns": 3}))["history"]
        future_b = self.ok(self.client.post("/api/sim/turn/preview", json={"turns": 3}))["history"]
        self.assertEqual(json.dumps(future_a, sort_keys=True), json.dumps(future_b, sort_keys=True))

    def test_policy_slots_are_validated_and_change_yields_immediately(self):
        wrong = self.client.put("/api/sim/empire", json={"government": "GOVERNMENT_CHIEFDOM",
                                                         "policy_slots": {"SLOT_MILITARY": ["POLICY_URBAN_PLANNING"]}})
        self.assertEqual(wrong.status_code, 409)
        too_many = self.client.put("/api/sim/empire", json={"government": "GOVERNMENT_CHIEFDOM",
                                                            "policy_slots": {"SLOT_ECONOMIC": ["POLICY_URBAN_PLANNING", "POLICY_GOD_KING"]}})
        self.assertEqual(too_many.status_code, 409)
        locked = self.client.put("/api/sim/empire", json={"government": "GOVERNMENT_CHIEFDOM",
                                                          "policy_slots": {"SLOT_ECONOMIC": ["POLICY_NATURAL_PHILOSOPHY"]}})
        self.assertEqual(locked.status_code, 409)
        result = self.ok(self.client.put("/api/sim/empire", json={
            "government": "GOVERNMENT_CHIEFDOM", "policy_slots": {"SLOT_ECONOMIC": ["POLICY_GOD_KING"]},
            "enforce_prerequisites": False}))
        self.assertGreater(result["impact"][0]["delta"]["Gold"], 0)
        options = self.ok(self.client.get("/api/sim/empire/options"))
        self.assertFalse(options["governors"]["available"])
        god_king = next(p for p in options["policies"] if p["id"] == "POLICY_GOD_KING")
        self.assertEqual(god_king["support"]["status"], "효과 해석 지원 (미검증)")

    def test_citizen_endpoints_and_capacity(self):
        self.ok(self.client.put("/api/sim/cities/city-a/citizens/4/4", json={"citizens": 1}))
        self.ok(self.client.put("/api/sim/cities/city-a/citizens/5/3", json={"citizens": 1}))
        over = self.client.put("/api/sim/cities/city-a/citizens/4/5", json={"citizens": 1})
        self.assertEqual(over.status_code, 409)
        center = self.client.put("/api/sim/cities/city-a/citizens/5/4", json={"citizens": 1})
        self.assertEqual(center.status_code, 409)
        self.ok(self.client.put("/api/sim/cities/city-a/tile-preferences/4/4", json={"block": True}))
        tiles = {f"{t['x']},{t['y']}": t for t in self.ok(self.client.get("/api/map"))["tiles"] if t.get("owner_city_id")}
        self.assertFalse(tiles["4,4"]["worked"])
        plan = self.ok(self.client.post("/api/sim/cities/city-a/optimize", json={"strategy": "food", "apply": True}))
        self.assertNotIn("4,4", plan["plan"]["assignment"])
        self.assertTrue(plan["applied"])
        self.assertTrue(any(row["strategy"] == "production" for row in plan["presets"]))

    def test_production_queue_endpoints(self):
        options = self.ok(self.client.get("/api/sim/cities/city-a/production/options"))["options"]
        warrior = next(o for o in options if o["id"] == "UNIT_WARRIOR")
        self.assertTrue(warrior["available"])
        self.ok(self.client.post("/api/sim/cities/city-a/production/queue", json={"category": "unit", "target_id": "UNIT_WARRIOR"}))
        queue = self.ok(self.client.post("/api/sim/cities/city-a/production/queue",
                                         json={"category": "building", "target_id": "BUILDING_MONUMENT"}))["queue"]
        self.assertEqual([q["target_id"] for q in queue], ["UNIT_WARRIOR", "BUILDING_MONUMENT"])
        self.assertLess(queue[0]["estimated_turns"], queue[1]["estimated_turns"])
        queue = self.ok(self.client.put("/api/sim/cities/city-a/production/order",
                                        json={"queue_id": queue[1]["queue_id"], "position": 0}))["queue"]
        self.assertEqual(queue[0]["target_id"], "BUILDING_MONUMENT")
        blocked = self.client.post("/api/sim/cities/city-a/production/queue",
                                   json={"category": "district", "target_id": "DISTRICT_CAMPUS", "x": 6, "y": 5})
        self.assertEqual(blocked.status_code, 409)
        queue = self.ok(self.client.delete(f"/api/sim/cities/city-a/production/queue/{queue[0]['queue_id']}"))["queue"]
        self.assertEqual(len(queue), 1)
        # Legacy production API still reads the shared queue.
        legacy = self.ok(self.client.get("/api/cities/city-a/production"))
        self.assertEqual(len(legacy["queue"]), 1)

    def test_validation_cases_and_data_audit(self):
        state = self.ok(self.client.get("/api/sim/state"))
        food = state["cities"]["city-a"]["yields"]["totals"]["Food"]
        self.ok(self.client.post("/api/validation/cases", json={"city_id": "city-a", "expected": {"Food": food}}))
        self.ok(self.client.post("/api/validation/cases", json={"city_id": "city-a", "expected": {"Food": food + 3}}))
        results = self.ok(self.client.get("/api/validation/cases"))["results"]
        self.assertEqual([r["status"] for r in results], ["일치", "불일치"])
        bad = self.client.post("/api/validation/cases", json={"city_id": "city-a", "expected": {"Mana": 1}})
        self.assertEqual(bad.status_code, 422)
        audit = self.ok(self.client.get("/api/data/audit"))
        self.assertTrue(audit["rules_db"]["available"])
        self.assertGreater(audit["modifier_links"]["totals"]["supported"], 0)
        self.assertEqual(audit["xml_vs_catalog"]["summary"]["empty_row_tables"], 181)

    def test_city_ledger_endpoints(self):
        ledger = self.ok(self.client.get("/api/sim/cities/city-a/ledger"))
        self.assertTrue(ledger["reconciliation"]["ok"])
        state = self.ok(self.client.get("/api/sim/state"))
        self.assertEqual(ledger["totals"], state["cities"]["city-a"]["yields"]["totals"])
        response = self.client.get("/api/sim/cities/city-a/ledger.csv")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response.headers["content-type"])
        self.assertIn("ledger_city-a", response.headers["content-disposition"])
        scenario = self.ok(self.client.post("/api/scenarios", json={"name": "L"}))
        scoped = self.ok(self.client.get(f"/api/sim/cities/city-a/ledger?scenario_id={scenario['id']}"))
        self.assertEqual(scoped["scenario_id"], scenario["id"])

    def test_modifier_audit_lists_skipped_effects(self):
        self.ok(self.client.put("/api/sim/empire", json={"civilization": "CIVILIZATION_GREECE", "leader": "LEADER_PERICLES"}))
        audit = self.ok(self.client.get("/api/sim/modifiers"))
        self.assertGreater(audit["summary"]["skipped"], 0)
        self.assertTrue(all("reason" in item for item in audit["skipped"]))


if __name__ == "__main__":
    unittest.main()
