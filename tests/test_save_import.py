"""Civ6Save decoder and save-import tests.

Synthetic saves (tests/civ6save_fixtures.py) pin the byte layout the decoder
relies on. If ``CIV6_SAVE_DIR`` points at a folder with real ``.Civ6Save``
files, the real-save checks run as well (they are skipped otherwise).
"""

import json
import os
import shutil
import tempfile
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main
from app.core import paths
from app.repositories.json_repository import invalidate_active_document_cache
from app.repositories.rules_db_repository import clear_rules_db_cache
from app.services import save_import_service
from app.services.civ6save import SaveFormatError, decode_save, type_hash
from app.services.civ6save.decoder import RIVER_DIRECTIONS, neighbor

from tests.civ6save_fixtures import build_body, build_save, default_layout
from tests.sim_fixtures import APP_DATA, claim, make_city, make_game, make_map

OPPOSITE = {"NE": "SW", "SW": "NE", "E": "W", "W": "E", "NW": "SE", "SE": "NW"}


class HashTests(unittest.TestCase):
    def test_type_hash_is_inverted_crc32(self):
        # Values observed in real saves (also listed by Civ6EGRM's terrain table).
        self.assertEqual(type_hash("TERRAIN_GRASS"), 2213004848)
        self.assertEqual(type_hash("TERRAIN_COAST"), 1248885265)
        self.assertEqual(type_hash("TERRAIN_OCEAN"), 1204357597)
        self.assertEqual(type_hash("IMPROVEMENT_FARM"), 168372657)


class DecoderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clear_rules_db_cache()
        cls.names = save_import_service.type_names()
        cls.save = decode_save(build_save(), cls.names)

    def test_map_and_tiles(self):
        save = self.save
        self.assertEqual((save.width, save.height), (44, 26))
        self.assertEqual(len(save.tiles), 44 * 26)
        farm = save.tile(10, 11)
        self.assertEqual((farm.resource, farm.improvement), ("RESOURCE_WHEAT", "IMPROVEMENT_FARM"))
        hills = save.tile(11, 9)
        self.assertEqual((hills.terrain, hills.feature), ("TERRAIN_PLAINS_HILLS", "FEATURE_FOREST"))
        self.assertEqual(save.tile(0, 0).terrain, "TERRAIN_OCEAN")
        self.assertIsNone(save.tile(0, 0).player)
        self.assertEqual(save.turn_estimate, 37)
        self.assertEqual(save.header["game_speed"], "GAMESPEED_STANDARD")
        self.assertEqual(save.players[0]["civilization"], "CIVILIZATION_SUMERIA")
        self.assertEqual(save.players[0]["leader"], "LEADER_GILGAMESH")

    def test_river_edges_mirror_neighbours(self):
        self.assertEqual(self.save.tile(10, 9).river_edges, ["E"])
        self.assertEqual(self.save.tile(11, 9).river_edges, ["W"])
        for t in self.save.tiles:
            for direction in t.river_edges:
                nx, ny = neighbor(t.x, t.y, direction, self.save.width)
                self.assertIn(OPPOSITE[direction], self.save.tile(nx, ny).river_edges)

    def test_cities_districts_buildings(self):
        cities = {c.name: c for c in self.save.cities}
        self.assertEqual(set(cities), {"Testopolis", "Otherton"})
        mine = cities["Testopolis"]
        self.assertEqual((mine.player, mine.x, mine.y, mine.population), (0, 10, 10, 4))
        self.assertTrue(mine.verified)
        self.assertEqual(sorted(d.type for d in mine.districts),
                         ["DISTRICT_CAMPUS", "DISTRICT_CITY_CENTER", "DISTRICT_THEATER", "DISTRICT_WONDER"])
        theater = next(d for d in mine.districts if d.type == "DISTRICT_THEATER")
        self.assertEqual((theater.completed, theater.cost), (False, 60))
        self.assertEqual({b["type"]: (b["x"], b["y"]) for b in mine.buildings},
                         {"BUILDING_PALACE": (10, 10), "BUILDING_LIBRARY": (11, 10), "BUILDING_PYRAMIDS": (9, 10)})
        self.assertEqual(self.save.tile(9, 10).wonder, "BUILDING_PYRAMIDS")
        other = cities["Otherton"]
        self.assertEqual((other.player, other.x, other.y), (1, 30, 12))
        self.assertEqual(self.save.stats["cities"], self.save.stats["city_centres"])
        self.assertEqual(self.save.warnings, [])

    def test_citizens_and_production(self):
        mine = next(c for c in self.save.cities if c.name == "Testopolis")
        citizens = mine.citizens
        self.assertTrue(citizens["verified"])
        self.assertEqual({(t["x"], t["y"]): (t["count"], t["locked"]) for t in citizens["tiles"]},
                         {(10, 11): (1, False), (11, 11): (1, True)})
        self.assertEqual([(s["x"], s["y"], s["count"], s["district"]) for s in citizens["specialists"]],
                         [(11, 10, 2, "DISTRICT_CAMPUS")])
        self.assertEqual(mine.production["kind"], "building")
        self.assertEqual(mine.production["type"], "BUILDING_GRANARY")
        self.assertAlmostEqual(mine.production["progress"], 12.5)
        self.assertIsNone(mine.production["x"])
        self.assertEqual(mine.construction, [{"category": "district", "type": "DISTRICT_THEATER", "x": 10, "y": 9,
                                              "progress": 20.0, "cost": 60}])
        other = next(c for c in self.save.cities if c.name == "Otherton")
        self.assertIsNone(other.citizens)
        self.assertIsNone(other.population)
        self.assertIsNone(other.production)

    def test_city_plot_order(self):
        from app.services.civ6save.decoder import city_plots

        plots = city_plots(22, 30, 96)
        # centre, then ring 1 starting north-east and going clockwise (game y grows north)
        self.assertEqual(plots[:7], [(22, 30), (22, 31), (23, 30), (22, 29), (21, 29), (21, 30), (21, 31)])
        self.assertEqual(len(plots), 37)
        self.assertEqual(len(set(plots)), 37)

    def test_rejects_non_saves(self):
        with self.assertRaises(SaveFormatError):
            decode_save(b"not a save at all", self.names)
        with self.assertRaises(SaveFormatError):
            decode_save(b"CIV6" + b"\x00" * 5000, self.names)

    def test_unknown_hash_is_reported_not_guessed(self):
        layout = default_layout()
        layout["tiles"][(20, 20)] = {"improvement": "IMPROVEMENT_SOMETHING_FROM_A_MOD"}
        save = decode_save(build_save(build_body(layout)), self.names)
        value = save.tile(20, 20).improvement
        self.assertTrue(value.startswith("UNKNOWN_"))
        self.assertIn(value, save.unknown_types)

    def test_unverified_district_records_are_dropped(self):
        layout = default_layout()
        # District record whose tile does not carry the district id → ignored.
        layout["cities"][0]["districts"].append((7, 12, 12, "DISTRICT_HARBOR", True, 0))
        save = decode_save(build_save(build_body(layout)), self.names)
        self.assertNotIn((12, 12), {(d.x, d.y) for d in save.districts})


class BuildDocumentsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clear_rules_db_cache()
        cls.save = decode_save(build_save(), save_import_service.type_names())

    def test_player_cities_become_simulator_cities(self):
        map_data, cities, game, report = save_import_service.build_documents(self.save, 0, "t.Civ6Save", now="x")
        self.assertEqual(map_data["orientation"], "north_up")
        self.assertEqual(len(cities), 1)
        city = next(iter(cities.values()))
        self.assertEqual((city["name"], city["x"], city["y"], city["population"]), ("Testopolis", 10, 10, 4))
        self.assertEqual(city["buildings"], ["BUILDING_PALACE", "BUILDING_LIBRARY"])
        self.assertEqual(city["wonders"], ["BUILDING_PYRAMIDS"])
        self.assertEqual(city["districts"], ["DISTRICT_CAMPUS"])  # theater is still being built
        self.assertEqual(city["import_meta"]["population"], "decoded")
        self.assertEqual(city["specialists"], {"11,10": 2})
        self.assertEqual(city["tile_preferences"], {"11,11": {"lock": True}})
        self.assertEqual(city["citizen_mode"], "manual")
        queue = city["production_queue"]
        self.assertEqual([(e["category"], e["target_id"], e["x"], e["y"], e.get("locked_cost"), e["from_save"])
                          for e in queue],
                         [("building", "BUILDING_GRANARY", None, None, None, "current"),
                          ("district", "DISTRICT_THEATER", 10, 9, 60, "unfinished")])
        self.assertEqual(city["production_progress"], {queue[0]["queue_id"]: 12.5, queue[1]["queue_id"]: 20.0})
        self.assertEqual(report["needs_auto_assign"], [])
        tiles = {(t["x"], t["y"]): t for t in map_data["tiles"]}
        self.assertEqual(tiles[(11, 10)]["district"], "DISTRICT_CAMPUS")
        self.assertEqual(tiles[(9, 10)]["district"], "DISTRICT_WONDER")
        self.assertIsNone(tiles[(10, 10)]["district"])  # city centre convention
        self.assertTrue(tiles[(10, 10)]["worked"])
        self.assertTrue(tiles[(10, 11)]["worked"])
        self.assertTrue(tiles[(11, 11)]["worked"])
        self.assertTrue(tiles[(11, 10)]["worked"])        # specialists
        self.assertFalse(tiles[(11, 9)]["worked"])
        self.assertIsNone(tiles[(10, 9)]["district"])
        self.assertEqual(tiles[(10, 9)]["district_in_progress"], "DISTRICT_THEATER")
        self.assertEqual(tiles[(10, 9)]["owner_city_id"], city["id"])
        self.assertTrue(tiles[(10, 9)]["river"])
        self.assertEqual(tiles[(10, 9)]["river_edges"], ["E"])
        # The other player's land is kept for display only.
        self.assertIsNone(tiles[(30, 12)]["owner_city_id"])
        self.assertEqual(tiles[(30, 12)]["save_owner"], 1)
        self.assertEqual([c["name"] for c in map_data["save_import"]["foreign_cities"]], ["Otherton"])
        self.assertEqual(game["turn"], 37)
        self.assertEqual(game["game_speed"], "STANDARD")
        self.assertEqual(game["empire"]["civilization"], "CIVILIZATION_SUMERIA")
        self.assertFalse(game["empire"]["enforce_prerequisites"])
        self.assertEqual(report["capital"], "Testopolis")

    def test_city_ids_are_stable(self):
        a = save_import_service.build_documents(self.save, 0, "t.Civ6Save", now="x")[1]
        b = save_import_service.build_documents(self.save, 0, "t.Civ6Save", now="y")[1]
        self.assertEqual(list(a), list(b))

    def test_save_speed_is_kept_with_growth_warning(self):
        save = decode_save(build_save(speed="GAMESPEED_EPIC"), save_import_service.type_names())
        _, _, game, report = save_import_service.build_documents(save, 0, "e.Civ6Save")
        self.assertEqual(game["game_speed"], "EPIC")
        self.assertEqual(report["game_speed_in_save"], "EPIC")
        self.assertTrue(any("EPIC" in w and "성장" in w for w in report["warnings"]))

    def test_player_without_cities_is_rejected(self):
        from app.core.exceptions import ApplicationError

        with self.assertRaises(ApplicationError):
            save_import_service.build_documents(self.save, 5, "t.Civ6Save")


class SaveImportApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name) / "data"
        self.data.mkdir()
        for name in ("catalog.json", "rules_db.json", "game_rules.json", "data_audit_report.json"):
            shutil.copy(APP_DATA / name, self.data / name)
        game_map = make_map()
        city = make_city(population=2, stored_food=0.0)
        claim(game_map, city)
        for name, value in (("map.json", game_map), ("cities.json", {city["id"]: city}), ("game_state.json", make_game())):
            (self.data / name).write_text(json.dumps(value), encoding="utf-8")
            invalidate_active_document_cache(self.data / name)
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
        self.disk_map = (self.data / "map.json").read_text(encoding="utf-8")

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        for name in ("map.json", "cities.json", "game_state.json"):
            invalidate_active_document_cache(self.data / name)
        self.temp.cleanup()

    def preview(self, raw=None):
        return self.client.post("/api/import/civ6save/preview", content=raw or build_save(),
                                headers={"Content-Type": "application/octet-stream",
                                         "X-Filename": "%ED%85%8C%EC%8A%A4%ED%8A%B8.Civ6Save"})

    def test_preview_then_apply_replaces_active_game(self):
        response = self.preview()
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertEqual(preview["filename"], "테스트.Civ6Save")
        self.assertEqual(preview["default_player"], 0)
        self.assertEqual({p["index"]: p["cities"] for p in preview["players"]}, {0: 1, 1: 1})
        self.assertTrue(preview["not_decoded"])

        response = self.client.post("/api/import/civ6save/apply", json={"token": preview["token"], "player": 0})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual([c["name"] for c in result["cities"]], ["Testopolis"])
        self.assertEqual(result["game_state"]["turn"], 37)
        self.assertNotIn("auto_assigned_cities", result["import_report"])  # citizens came from the save
        city = result["cities"][0]
        worked = {(t["x"], t["y"]) for t in result["map"]["tiles"] if t.get("owner_city_id") == city["id"] and t.get("worked")}
        self.assertEqual(worked, {(10, 10), (10, 11), (11, 11), (11, 10)})
        detail = self.client.get(f"/api/sim/cities/{city['id']}").json()["city"]
        queue = detail["production"]["queue"]
        self.assertEqual([q["target_id"] for q in queue], ["BUILDING_GRANARY", "DISTRICT_THEATER"])
        self.assertEqual(queue[0]["progress"], 12.5)
        self.assertEqual((queue[1]["cost"], queue[1]["progress"]), (60.0, 20.0))  # locked cost from the save
        # Technologies are not decoded, so prerequisite checks are off for imported games.
        self.assertFalse([r for r in queue[0]["blocked_reasons"] if r.startswith(("기술", "사회제도"))])
        # Simulation endpoints work on the imported game.
        state = self.client.get("/api/sim/state")
        self.assertEqual(state.status_code, 200, state.text)
        ledger = self.client.get(f"/api/sim/cities/{city['id']}/ledger")
        self.assertEqual(ledger.status_code, 200, ledger.text)
        self.assertEqual(self.client.post("/api/sim/turn/preview", json={"turns": 2}).status_code, 200)
        # Amenity effects start switched off for imported games; the empire panel can turn them on.
        options = self.client.get("/api/sim/empire/options").json()
        self.assertFalse(options["current"]["apply_amenity_effects"])
        updated = self.client.put("/api/sim/empire", json={"apply_amenity_effects": True})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertTrue(self.client.get("/api/sim/empire/options").json()["current"]["apply_amenity_effects"])
        # Foreign cities kept on the map are trade route destinations.
        dests = self.client.get(f"/api/sim/cities/{city['id']}").json()["trade_destinations"]
        self.assertEqual([(d["name"], d["kind"]) for d in dests], [("Otherton", "international")])
        routed = self.client.put(f"/api/sim/cities/{city['id']}/settings", json={"trade_routes": [{"destination": "30,12"}]})
        self.assertEqual(routed.status_code, 200, routed.text)
        self.assertGreater(routed.json()["after"]["Gold"], routed.json()["before"]["Gold"])
        # Nothing is written to disk until the user saves.
        self.assertEqual((self.data / "map.json").read_text(encoding="utf-8"), self.disk_map)
        saved = self.client.post("/api/maps/save-as", json={"name": "imported"})
        self.assertEqual(saved.status_code, 200, saved.text)
        loaded = self.client.post("/api/maps/load", json={"name": "imported"})
        self.assertEqual(loaded.status_code, 200, loaded.text)
        self.assertEqual(loaded.json()["map"]["orientation"], "north_up")

    def test_import_other_player(self):
        preview = self.preview().json()
        result = self.client.post("/api/import/civ6save/apply",
                                  json={"token": preview["token"], "player": 1, "auto_assign": False}).json()
        self.assertEqual([c["name"] for c in result["cities"]], ["Otherton"])
        self.assertEqual(result["game_state"]["empire"]["civilization"], "CIVILIZATION_EGYPT")
        # Otherton has no readable citizen data → filled in automatically.
        self.assertEqual(result["import_report"]["needs_auto_assign"], [result["cities"][0]["id"]])
        self.assertEqual(result["import_report"]["auto_assigned_cities"], 1)

    def test_auto_assign_replaces_save_citizens(self):
        preview = self.preview().json()
        result = self.client.post("/api/import/civ6save/apply",
                                  json={"token": preview["token"], "player": 0, "auto_assign": True}).json()
        self.assertEqual(result["import_report"]["auto_assigned_cities"], 1)

    def test_bad_inputs(self):
        self.assertEqual(self.preview(b"hello").status_code, 422)
        response = self.client.post("/api/import/civ6save/apply", json={"token": "0" * 16, "player": 0})
        self.assertEqual(response.status_code, 410)
        preview = self.preview().json()
        response = self.client.post("/api/import/civ6save/apply", json={"token": preview["token"], "player": 9})
        self.assertEqual(response.status_code, 422)


@unittest.skipUnless(os.environ.get("CIV6_SAVE_DIR"), "set CIV6_SAVE_DIR to a folder with real .Civ6Save files")
class RealSaveTests(unittest.TestCase):
    def test_real_saves_decode_consistently(self):
        clear_rules_db_cache()
        names = save_import_service.type_names()
        files = sorted(Path(os.environ["CIV6_SAVE_DIR"]).glob("*.Civ6Save"))
        self.assertTrue(files)
        for path in files:
            with self.subTest(save=path.name):
                save = decode_save(path.read_bytes(), names)
                self.assertEqual(len(save.tiles), save.width * save.height)
                self.assertEqual(save.stats["cities"], save.stats["city_centres"])
                self.assertEqual(save.stats["districts"], save.stats["districts_verified"])
                mismatched = 0
                for t in save.tiles:
                    for direction in t.river_edges:
                        nx, ny = neighbor(t.x, t.y, direction, save.width)
                        if 0 <= ny < save.height and OPPOSITE[direction] not in save.tile(nx, ny).river_edges:
                            mismatched += 1
                self.assertEqual(mismatched, 0)
                for city in save.cities:
                    for building in city.buildings:
                        tile = save.tile(building["x"], building["y"])
                        self.assertEqual((tile.player, tile.city_index), (city.player, city.city_index))
                    if city.citizens is not None:
                        self.assertTrue(city.citizens["verified"], city.name)
                    if city.production and city.production["progress"] is not None:
                        self.assertGreaterEqual(city.production["progress"], 0)
                player = save_import_service.default_player(save)
                if player is not None:
                    state = save.player_states.get(player) or {}
                    self.assertTrue(state.get("verified"), path.name)
                    _, _, game, report = save_import_service.build_documents(save, player, path.name, now="x")
                    self.assertTrue(report["player_state"]["decoded"])
                    if state.get("slot_layout"):
                        self.assertIsNotNone(game["empire"]["government"], report["player_state"])
        self.assertEqual(RIVER_DIRECTIONS, ("NE", "E", "SE", "SW", "W", "NW"))



class PlayerStateTests(unittest.TestCase):
    """Technologies, civics, government and policies from the per-player tables."""

    @classmethod
    def setUpClass(cls):
        from app.repositories.rules_db_repository import load_rules_db
        from tests.civ6save_fixtures import envoy_array, policy_block, tech_triple, trade_route_op

        clear_rules_db_cache()
        db = load_rules_db()
        cls.techs = [r["TechnologyType"] for r in db.rows("Technologies")][:30]
        cls.civics = [r["CivicType"] for r in db.rows("Civics")][:25]
        policies = [r["PolicyType"] for r in db.rows("Policies")][:40]
        cls.military = next(p["PolicyType"] for p in db.rows("Policies") if p["GovernmentSlotType"] == "SLOT_MILITARY")
        cls.economic = next(p["PolicyType"] for p in db.rows("Policies") if p["GovernmentSlotType"] == "SLOT_ECONOMIC")
        policies = list(dict.fromkeys(policies + [cls.military, cls.economic]))

        def save_with(tiers, slots):
            layout = default_layout()
            layout["player_states"] = {0: (
                tech_triple(cls.techs, set(cls.techs[:3]), {cls.techs[5]}, {cls.techs[4]: 12.5, cls.techs[0]: 9.0})
                + b"\x00" * 64
                + tech_triple(cls.civics, set(cls.civics[:2]), set(), {cls.civics[3]: 7.0})
                + b"\x00" * 64
                + policy_block(policies, tiers, slots)
                + b"\x00" * 64 + envoy_array({5: 3, 6: 1})
                + b"\x00" * 64 + struct.pack("<I", type_hash("BELIEF_GODDESS_OF_FESTIVALS"))
                + b"\x00" * 64 + trade_route_op((10, 10), (30, 12), 7)
                + b"\x00" * 16 + trade_route_op((10, 10), (99, 99), 7))}  # not a city: ignored
            # A route record in player 1's block (e.g. a city-state trader) is not player 0's.
            layout["player_states"][1] = envoy_array({5: 2}) + b"\x00" * 16 + trade_route_op((10, 10), (30, 12), 9)
            cs = ((5, "CIVILIZATION_BOLOGNA", "LEADER_MINOR_CIV_BOLOGNA"), (6, "CIVILIZATION_GENEVA", "LEADER_MINOR_CIV_GENEVA"))
            return decode_save(build_save(build_body(layout), extra_slots=cs), save_import_service.type_names())

        # Monarchy's slots: 2 military, 1 economic, 1 diplomatic, 2 wildcard (codes 1, 0, 2, 4).
        cls.save = save_with(["GOVERNMENT_OLIGARCHY", "GOVERNMENT_MONARCHY"],
                             [(cls.military, 1), (None, 1), (cls.economic, 0), (None, 2), (None, 4), (None, 4)])
        cls.unmatched = save_with(["GOVERNMENT_MONARCHY"], [(cls.military, 1), (None, 1), (None, 1)])

    def test_decoder_reads_tables(self):
        state = self.save.player_states[0]
        self.assertTrue(state["verified"])
        self.assertEqual(state["technologies"], self.techs[:3])
        self.assertEqual(state["boosted_technologies"], [self.techs[5]])
        self.assertEqual(state["tech_progress"][self.techs[4]], 12.5)
        self.assertEqual(state["civics"], self.civics[:2])
        self.assertEqual(state["government_candidates"], ["GOVERNMENT_OLIGARCHY", "GOVERNMENT_MONARCHY", None, None])
        self.assertEqual(state["slot_layout"].count("SLOT_WILDCARD"), 2)
        self.assertEqual([p["policy"] for p in state["policies"]], [self.military, self.economic])

    def test_documents_get_government_policies_and_research(self):
        _, _, game, report = save_import_service.build_documents(self.save, 0, "t.Civ6Save", now="x")
        empire = game["empire"]
        self.assertEqual(empire["government"], "GOVERNMENT_MONARCHY")  # the tier whose slots match
        self.assertEqual(empire["policy_slots"], {"SLOT_MILITARY": [self.military], "SLOT_ECONOMIC": [self.economic]})
        self.assertTrue(empire["enforce_prerequisites"])
        self.assertEqual(game["technologies"], sorted(self.techs[:3]))
        self.assertEqual(game["civics"], sorted(self.civics[:2]))
        # Progress of finished technologies is dropped; the rest is kept.
        self.assertEqual(game["research"]["technology"]["progress"], {self.techs[4]: 12.5})
        self.assertEqual(game["research"]["civic"]["progress"], {self.civics[3]: 7.0})
        self.assertEqual(empire["boosted"]["technology"], [self.techs[5]])
        self.assertEqual(report["player_state"]["government"], "GOVERNMENT_MONARCHY")
        self.assertEqual(game["simulation_overrides"], {"apply_amenity_effects": False})

    def test_envoys_suzerain_and_pantheon(self):
        state = self.save.player_states[0]
        self.assertEqual(state["envoys"], {5: 3, 6: 1})
        self.assertEqual(state["suzerain_of"], [5])  # 3 envoys, more than player 1's 2
        self.assertIn("BELIEF_GODDESS_OF_FESTIVALS", state["beliefs_in_block"])
        _, _, game, _ = save_import_service.build_documents(self.save, 0, "t.Civ6Save", now="x")
        self.assertEqual(game["empire"]["city_states"], {
            "CIVILIZATION_BOLOGNA": {"envoys": 3, "suzerain": True},
            "CIVILIZATION_GENEVA": {"envoys": 1, "suzerain": False}})
        self.assertEqual(game["empire"]["pantheon"], "BELIEF_GODDESS_OF_FESTIVALS")

    def test_trade_routes_from_traders(self):
        self.assertEqual(self.save.trade_routes, [
            {"origin": (10, 10), "origin_player": 0, "destination": (30, 12), "destination_player": 1,
             "started_turn": 7, "owner": 0},
            {"origin": (10, 10), "origin_player": 0, "destination": (30, 12), "destination_player": 1,
             "started_turn": 9, "owner": 1}])
        _, cities, _, _ = save_import_service.build_documents(self.save, 0, "t.Civ6Save", now="x")
        origin = next(c for c in cities.values() if c["x"] == 10 and c["y"] == 10)
        self.assertEqual(origin["trade_routes"], [{"destination": "30,12", "started_turn": 7}])

    def test_unmatched_slots_leave_government_unset(self):
        _, _, game, report = save_import_service.build_documents(self.unmatched, 0, "t.Civ6Save", now="x")
        self.assertIsNone(game["empire"]["government"])
        self.assertEqual(game["empire"]["policy_slots"], {})
        self.assertTrue(any("정부를 확정하지 못했습니다" in w for w in report["warnings"]))
        # Technologies are still applied.
        self.assertEqual(game["technologies"], sorted(self.techs[:3]))

    def test_save_without_tables_keeps_prerequisites_off(self):
        save = decode_save(build_save(), save_import_service.type_names())
        _, _, game, report = save_import_service.build_documents(save, 0, "t.Civ6Save", now="x")
        self.assertFalse(game["empire"]["enforce_prerequisites"])
        self.assertFalse(report["player_state"]["decoded"])

if __name__ == "__main__":
    unittest.main()
