import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import main
from app.core import paths
from app.config import RuleConfigurationError, load_game_rules
from app.repositories.map_repository import map_repository
from app.repositories.json_repository import invalidate_active_document_cache


def make_map(width=7, height=5):
    tiles = [
        {
            "x": x,
            "y": y,
            "terrain": "TERRAIN_GRASS",
            "owner_city_id": None,
            "worked": False,
        }
        for y in range(height)
        for x in range(width)
    ]
    return {
        "width": width,
        "height": height,
        "count": len(tiles),
        "tiles": tiles,
    }


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    invalidate_active_document_cache(path)


class MapFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.data_dir = self.root / "data"
        self.saved_dir = self.data_dir / "saved_maps"
        self.map_data = make_map()
        self.city = {
            "id": "city-existing",
            "name": "Existing",
            "x": 1,
            "y": 2,
            "population": 1,
            "buildings": [],
            "wonders": [],
        }
        self.city["technologies"] = []
        self.city["civics"] = []
        self.cities = {self.city["id"]: self.city}
        for tile in self.map_data["tiles"]:
            if main.hex_distance(tile["x"], tile["y"], 1, 2) <= 1:
                tile["owner_city_id"] = self.city["id"]
        write_json(self.data_dir / "map.json", self.map_data)
        write_json(self.data_dir / "cities.json", self.cities)
        write_json(self.data_dir / "catalog.json", {})
        self.rules = json.loads(
            (Path(main.__file__).parent / "config" / "game_rules.json").read_text(
                encoding="utf-8"
            )
        )
        write_json(self.data_dir / "game_rules.json", self.rules)
        self.data_patch = patch.object(main, "DATA", self.data_dir)
        self.saved_patch = patch.object(main, "SAVED_MAPS", self.saved_dir)
        self.game_patch = patch.object(main, "GAME_STATE", self.data_dir / "game_state.json")
        self.paths_data_patch = patch.object(paths, "DATA", self.data_dir)
        self.paths_saved_patch = patch.object(paths, "SAVED_MAPS", self.saved_dir)
        self.paths_game_patch = patch.object(
            paths, "GAME_STATE", self.data_dir / "game_state.json"
        )
        self.data_patch.start()
        self.saved_patch.start()
        self.game_patch.start()
        self.paths_data_patch.start()
        self.paths_saved_patch.start()
        self.paths_game_patch.start()

    def tearDown(self):
        self.paths_game_patch.stop()
        self.paths_saved_patch.stop()
        self.paths_data_patch.stop()
        self.game_patch.stop()
        self.saved_patch.stop()
        self.data_patch.stop()
        self.temp_dir.cleanup()

    def set_catalog(self, catalog):
        write_json(self.data_dir / "catalog.json", catalog)

    def set_units(self, units):
        write_json(main.GAME_STATE, {"turn": 1, "units": units})

    def production_catalog(self, production=5, builder_cost=10, settler_cost=10):
        return {
            "terrain_yields": {"TERRAIN_GRASS": {"Production": production}},
            "building_yields": {},
            "raw_tables": {
                "Units::Units": [
                    {
                        "UnitType": "UNIT_BUILDER",
                        "Cost": str(builder_cost),
                        "BuildCharges": "3",
                        "BaseMoves": "2",
                    },
                    {
                        "UnitType": "UNIT_SETTLER",
                        "Cost": str(settler_cost),
                        "BaseMoves": "2",
                        "FoundCity": "true",
                        "PopulationCost": "1",
                        "PrereqPopulation": "2",
                    },
                ],
                "Buildings::Buildings": [],
                "Buildings::Types": [],
                "Buildings::BuildingPrereqs": [],
                "Districts::Districts": [],
                "Districts::Types": [],
                "Districts::District_ValidTerrains": [],
            },
        }

    def test_city_spacing_and_initial_territory(self):
        with self.assertRaises(HTTPException) as error:
            main.create_city(main.CityCreate(name="Too close", x=3, y=2))
        self.assertEqual(error.exception.status_code, 409)

        city = main.create_city(main.CityCreate(name="New", x=4, y=2))
        saved_map = main.read(self.data_dir / "map.json", {})
        owned = [
            tile
            for tile in saved_map["tiles"]
            if tile.get("owner_city_id") == city["id"]
        ]
        self.assertEqual(len(owned), 7)
        self.assertTrue(
            next(tile for tile in owned if (tile["x"], tile["y"]) == (4, 2))[
                "worked"
            ]
        )

    def test_city_delete_clears_only_its_owned_tiles(self):
        other_city_id = "city-other"
        self.cities[other_city_id] = {
            **self.city,
            "id": other_city_id,
            "name": "Other",
            "x": 5,
        }
        map_data = main.read(self.data_dir / "map.json", {})
        other_tile = next(tile for tile in map_data["tiles"] if tile["x"] == 6 and tile["y"] == 2)
        other_tile["owner_city_id"] = other_city_id
        other_tile["worked"] = True
        write_json(self.data_dir / "cities.json", self.cities)
        write_json(self.data_dir / "map.json", map_data)

        result = main.delete_city("city-existing")
        self.assertEqual(result["deleted"], "city-existing")
        saved_map = main.read(self.data_dir / "map.json", {})
        self.assertTrue(
            all(
                tile.get("owner_city_id") != "city-existing" and not (
                    tile.get("owner_city_id") is None
                    and main.hex_distance(tile["x"], tile["y"], 1, 2) <= 1
                    and tile["worked"]
                )
                for tile in saved_map["tiles"]
            )
        )
        self.assertEqual(
            next(tile for tile in saved_map["tiles"] if tile["x"] == 6 and tile["y"] == 2)[
                "owner_city_id"
            ],
            other_city_id,
        )

    def test_save_as_round_trip_and_existing_name_protection(self):
        unit = {
            "id": "unit-saved",
            "unit_type": "UNIT_BUILDER",
            "owner_city_id": self.city["id"],
            "x": 1,
            "y": 2,
            "moves_remaining": 2,
            "charges_remaining": 3,
        }
        self.set_units([unit])
        self.city["stored_food"] = 7.25
        self.city["production_queue"] = [
            {
                "queue_id": "prod-saved",
                "category": "unit",
                "target_id": "UNIT_BUILDER",
            }
        ]
        self.city["production_progress"] = {"prod-saved": 4.5}
        self.city["current_production"] = "prod-saved"
        game_state = {
            "turn": 7,
            "units": [unit],
            "game_speed": "STANDARD",
            "technologies": ["TECH_SAVED"],
            "civics": ["CIVIC_SAVED"],
            "research": {
                "technology": {
                    "current_id": "TECH_SAVED",
                    "progress": {"TECH_SAVED": 12.5},
                },
                "civic": {
                    "current_id": "CIVIC_SAVED",
                    "progress": {"CIVIC_SAVED": 8.25},
                },
            },
        }
        write_json(self.data_dir / "cities.json", self.cities)
        write_json(main.GAME_STATE, game_state)
        main.save_map()
        result = main.save_map_as(main.SaveMapRequest(name="Backup"))
        self.assertEqual(result["name"], "Backup")
        snapshot = json.loads((self.saved_dir / "Backup.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["map"], self.map_data)
        self.assertEqual(snapshot["game_state"]["turn"], 7)
        self.assertEqual(snapshot["cities"][self.city["id"]]["stored_food"], 7.25)
        self.assertEqual(
            snapshot["cities"][self.city["id"]]["production_progress"],
            {"prod-saved": 4.5},
        )
        with self.assertRaises(HTTPException) as error:
            main.save_map_as(main.SaveMapRequest(name="Backup"))
        self.assertEqual(error.exception.status_code, 409)

        loaded = main.load_saved_map(main.SaveMapRequest(name="Backup"))
        self.assertEqual(loaded["map"], self.map_data)
        loaded_city = loaded["cities"][0]
        self.assertEqual(
            {key: loaded_city[key] for key in self.city},
            self.city,
        )
        self.assertEqual(loaded_city["stored_food"], 7.25)
        self.assertEqual(loaded_city["food_required"], 15.0)
        self.assertEqual(loaded_city["production_progress"], {"prod-saved": 4.5})
        self.assertEqual(loaded["game_state"]["units"], [unit])
        self.assertEqual(loaded["game_state"]["game_speed"], "STANDARD")
        self.assertEqual(loaded["game_state"]["turn"], 7)
        self.assertEqual(loaded["game_state"]["technologies"], ["TECH_SAVED"])
        self.assertEqual(
            loaded["game_state"]["research"]["technology"]["progress"],
            {"TECH_SAVED": 12.5},
        )
        self.assertEqual(main.read(self.data_dir / "map.json", {}), self.map_data)
        self.assertEqual(
            main.read(main.GAME_STATE, {}),
            loaded["game_state"],
        )

    def test_map_changes_are_only_written_to_active_files_after_manual_save(self):
        map_path = self.data_dir / "map.json"
        original_bytes = map_path.read_bytes()

        main.update_tile(0, 0, {"terrain": "TERRAIN_PLAINS"})

        self.assertEqual(map_path.read_bytes(), original_bytes)
        self.assertEqual(
            main.read(map_path, {})["tiles"][0]["terrain"], "TERRAIN_PLAINS"
        )

        main.save_map()
        saved_bytes = map_path.read_bytes()
        self.assertNotEqual(saved_bytes, original_bytes)
        self.assertEqual(
            json.loads(saved_bytes)["tiles"][0]["terrain"], "TERRAIN_PLAINS"
        )

        main.update_tile(0, 0, {"terrain": "TERRAIN_DESERT"})
        self.assertEqual(map_path.read_bytes(), saved_bytes)
        self.assertEqual(
            main.read(map_path, {})["tiles"][0]["terrain"], "TERRAIN_DESERT"
        )
        main.save_map_as(main.SaveMapRequest(name="ManualState"))
        main.update_tile(0, 0, {"terrain": "TERRAIN_MOUNTAIN"})
        main.save_map()
        committed_bytes = map_path.read_bytes()
        loaded = main.load_saved_map(main.SaveMapRequest(name="ManualState"))
        self.assertEqual(loaded["map"]["tiles"][0]["terrain"], "TERRAIN_DESERT")
        self.assertEqual(
            main.read(map_path, {})["tiles"][0]["terrain"], "TERRAIN_DESERT"
        )
        self.assertEqual(
            json.loads(committed_bytes)["tiles"][0]["terrain"],
            "TERRAIN_MOUNTAIN",
        )
        self.assertEqual(map_path.read_bytes(), committed_bytes)

    def test_tile_details_reuse_yield_service_and_only_report_stored_facts(self):
        catalog = {
            "terrain_yields": {"TERRAIN_GRASS": {"Food": 2}},
            "feature_yields": {"FEATURE_FOREST": {"Production": 1}},
            "resource_yields": {"RESOURCE_BANANAS": {"Food": 1}},
            "improvement_yields": {"IMPROVEMENT_FARM": {"Food": 1}},
            "terrains": [
                {
                    "id": "TERRAIN_GRASS",
                    "label": "Grass",
                    "raw": {"MovementCost": "1"},
                }
            ],
            "features": [
                {
                    "id": "FEATURE_FOREST",
                    "label": "Forest",
                    "raw": {"DefenseModifier": "3", "Appeal": "1"},
                }
            ],
            "resources": [{"id": "RESOURCE_BANANAS", "label": "Bananas"}],
            "improvements": [{"id": "IMPROVEMENT_FARM", "label": "Farm"}],
            "districts": [{"id": "DISTRICT_CAMPUS", "label": "Campus"}],
            "buildings": [{"id": "BUILDING_LIBRARY", "label": "Library"}],
        }
        self.set_catalog(catalog)
        worked_tile = next(
            tile
            for tile in self.map_data["tiles"]
            if (tile["x"], tile["y"]) == (2, 2)
        )
        worked_tile.update(
            {
                "feature": "FEATURE_FOREST",
                "resource": "RESOURCE_BANANAS",
                "improvement": "IMPROVEMENT_FARM",
                "worked": True,
            }
        )
        self.city["population"] = 3
        self.city["districts"] = ["DISTRICT_CAMPUS"]
        self.city["buildings"] = ["BUILDING_LIBRARY"]
        district_tile = next(
            tile
            for tile in self.map_data["tiles"]
            if (tile["x"], tile["y"]) == (0, 1)
        )
        district_tile.update({"district": "DISTRICT_CAMPUS", "worked": False})
        write_json(self.data_dir / "map.json", self.map_data)
        write_json(self.data_dir / "cities.json", self.cities)

        detail = main.tile_details(2, 2)
        expected = main.calculate_tile_yields(2, 2)
        self.assertEqual(detail["yields"], expected)
        self.assertEqual(detail["owner_city"]["name"], self.city["name"])
        self.assertEqual(detail["owner_city"]["population"], 3)
        self.assertEqual(detail["citizens_working"], 1)
        self.assertTrue(detail["workable"])
        self.assertEqual(detail["movement_cost"], "1")
        self.assertEqual(
            detail["defense_modifiers"],
            [{"source": "feature", "id": "FEATURE_FOREST", "value": 3.0}],
        )
        self.assertIsNone(detail["appeal"])
        self.assertIsNone(detail["continent"])

        district_detail = main.tile_details(0, 1)
        self.assertEqual(district_detail["tile"]["district"], "DISTRICT_CAMPUS")
        self.assertEqual(
            district_detail["owner_city"]["buildings"], ["BUILDING_LIBRARY"]
        )
        self.assertFalse(district_detail["district_building_assignment_available"])
        self.assertFalse(district_detail["workable"])

        center = main.tile_details(self.city["x"], self.city["y"])
        self.assertTrue(center["is_city_center"])
        self.assertEqual(center["citizens_working"], 0)

        unowned = main.tile_details(6, 4)
        self.assertIsNone(unowned["owner_city"])
        self.assertEqual(unowned["citizens_working"], 0)
        self.assertTrue(unowned["yields"]["preview"])
        self.assertFalse(unowned["workable"])

    def test_save_name_and_snapshot_validation(self):
        with self.assertRaises(HTTPException):
            main.saved_map_path("../outside")
        invalid_unit_state = {
            "turn": 1,
            "units": [
                {
                    "id": "bad-unit",
                    "unit_type": "UNIT_BUILDER",
                    "owner_city_id": self.city["id"],
                    "x": [],
                    "y": 2,
                    "moves_remaining": 2,
                    "charges_remaining": 1,
                }
            ],
        }
        with self.assertRaises(HTTPException) as unit_error:
            main.validate_game_state_snapshot(
                invalid_unit_state, self.cities, self.map_data
            )
        self.assertEqual(unit_error.exception.status_code, 422)
        with self.assertRaises(HTTPException):
            main.validate_map_snapshot(
                {
                    "map": {"width": 2, "height": 1, "tiles": [{"x": 0, "y": 0, "terrain": "T"}]},
                    "cities": {},
                }
            )
        self.saved_dir.mkdir(parents=True)
        (self.saved_dir / "Broken.json").write_text(
            json.dumps({"map": {}, "cities": {}}), encoding="utf-8"
        )
        before = main.read(self.data_dir / "map.json", {})
        before_cities = main.read(self.data_dir / "cities.json", {})
        before_state = main.read(main.GAME_STATE, {})
        with self.assertRaises(HTTPException) as error:
            main.load_saved_map(main.SaveMapRequest(name="Broken"))
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(main.read(self.data_dir / "map.json", {}), before)
        self.assertEqual(main.read(self.data_dir / "cities.json", {}), before_cities)
        self.assertEqual(main.read(main.GAME_STATE, {}), before_state)

    def test_yields_obey_population_and_include_extracted_bonuses(self):
        map_data = {
            "width": 3,
            "height": 2,
            "tiles": [
                {
                    "x": 1,
                    "y": 0,
                    "terrain": "TERRAIN_GRASS",
                    "owner_city_id": "city-yields",
                    "worked": True,
                },
                {
                    "x": 0,
                    "y": 0,
                    "terrain": "TERRAIN_GRASS",
                    "feature": "FEATURE_FOREST",
                    "resource": "RESOURCE_RICE",
                    "improvement": "IMPROVEMENT_FARM",
                    "owner_city_id": "city-yields",
                    "worked": True,
                },
                {
                    "x": 1,
                    "y": 1,
                    "terrain": "TERRAIN_MOUNTAIN",
                    "owner_city_id": "city-yields",
                    "worked": True,
                },
                {
                    "x": 0,
                    "y": 1,
                    "terrain": "TERRAIN_GRASS",
                    "district": "DISTRICT_CAMPUS",
                    "owner_city_id": "city-yields",
                    "worked": False,
                },
                {"x": 2, "y": 0, "terrain": "TERRAIN_GRASS"},
                {"x": 2, "y": 1, "terrain": "TERRAIN_GRASS"},
            ],
        }
        city = {
            "id": "city-yields",
            "name": "Yields",
            "x": 1,
            "y": 0,
            "population": 1,
            "buildings": [],
            "wonders": [],
        }
        catalog = {
            "terrain_yields": {"TERRAIN_GRASS": {"Food": 2}},
            "feature_yields": {"FEATURE_FOREST": {"Production": 1}},
            "improvement_yields": {"IMPROVEMENT_FARM": {"Production": 1}},
            "resources": [{"id": "RESOURCE_RICE", "raw": {"ResourceClassType": "BONUS"}}],
            "raw_tables": {
                "Resources::Resource_YieldChanges": [
                    {
                        "ResourceType": "RESOURCE_RICE",
                        "YieldType": "YIELD_FOOD",
                        "YieldChange": 1,
                    }
                ],
                "Districts::District_Adjacencies": [
                    {
                        "DistrictType": "DISTRICT_CAMPUS",
                        "YieldChangeId": "Campus_Mountain",
                    }
                ],
                "Districts::Adjacency_YieldChanges": [
                    {
                        "ID": "Campus_Mountain",
                        "AdjacentTerrain": "TERRAIN_MOUNTAIN",
                        "TilesRequired": 1,
                        "YieldType": "YIELD_SCIENCE",
                        "YieldChange": 1,
                    }
                ],
            },
        }
        result = main.calculate_city_yields(city, map_data, catalog)
        self.assertEqual(result["totals"]["Food"], 5)
        self.assertEqual(result["totals"]["Production"], 3)
        self.assertEqual(result["totals"]["Science"], 1.5)
        self.assertEqual(result["worker_limit"], 1)
        self.assertEqual(result["ignored_worked_tile_count"], 1)
        self.assertEqual(len([tile for tile in result["tiles"] if tile["worked"]]), 2)

    def test_population_can_work_one_extra_tile_per_citizen(self):
        center_tile = next(
            tile
            for tile in self.map_data["tiles"]
            if (tile["x"], tile["y"]) == (self.city["x"], self.city["y"])
        )
        center_tile["worked"] = True
        write_json(self.data_dir / "map.json", self.map_data)
        additional_tiles = [
            tile
            for tile in self.map_data["tiles"]
            if tile.get("owner_city_id") == self.city["id"]
            and (tile["x"], tile["y"]) != (self.city["x"], self.city["y"])
        ]
        tile_a, tile_b = additional_tiles[:2]
        main.update_tile(tile_a["x"], tile_a["y"], {"worked": True})
        with self.assertRaises(HTTPException) as full:
            main.update_tile(tile_b["x"], tile_b["y"], {"worked": True})
        self.assertEqual(full.exception.status_code, 409)

        self.city["population"] = 2
        write_json(self.data_dir / "cities.json", self.cities)
        main.update_tile(tile_b["x"], tile_b["y"], {"worked": True})
        worked = [
            tile
            for tile in main.read(self.data_dir / "map.json", {})["tiles"]
            if tile.get("worked")
        ]
        self.assertEqual(len(worked), 3)

        self.city["population"] = 1
        main.update_city(self.city["id"], {"population": 1})
        worked = [
            tile
            for tile in main.read(self.data_dir / "map.json", {})["tiles"]
            if tile.get("worked")
        ]
        self.assertEqual(len(worked), 2)

    def test_hill_terrain_and_manual_override_are_reflected_in_yields(self):
        hill_tile = {
            "x": 1,
            "y": 0,
            "terrain": "TERRAIN_GRASS_HILLS",
            "owner_city_id": "city-hills",
            "worked": True,
        }
        map_data = {
            "width": 2,
            "height": 1,
            "tiles": [
                {
                    "x": 0,
                    "y": 0,
                    "terrain": "TERRAIN_GRASS",
                    "owner_city_id": "city-hills",
                    "worked": True,
                },
                hill_tile,
            ],
        }
        city = {
            "id": "city-hills",
            "name": "Hills",
            "x": 0,
            "y": 0,
            "population": 1,
            "buildings": [],
            "wonders": [],
        }
        catalog = {
            "terrain_yields": {
                "TERRAIN_GRASS": {"Food": 2},
                "TERRAIN_GRASS_HILLS": {"Food": 2, "Production": 1},
            }
        }

        result = main.calculate_city_yields(city, map_data, catalog)
        worked_hill = next(tile for tile in result["tiles"] if tile["x"] == 1)
        self.assertTrue(worked_hill["is_hills"])
        self.assertEqual(worked_hill["hill_source"], "terrain_id")
        self.assertEqual(worked_hill["base_yields"]["Production"], 1)

        hill_tile["hill_override"] = False
        result = main.calculate_city_yields(city, map_data, catalog)
        worked_flat = next(tile for tile in result["tiles"] if tile["x"] == 1)
        self.assertFalse(worked_flat["is_hills"])
        self.assertEqual(worked_flat["hill_source"], "manual_override")
        self.assertEqual(worked_flat["base_yields"]["Production"], 0)

        map_data["tiles"][0]["hill_override"] = True
        result = main.calculate_city_yields(city, map_data, catalog)
        overridden_hill = next(tile for tile in result["tiles"] if tile["x"] == 0)
        self.assertTrue(overridden_hill["is_hills"])
        self.assertEqual(overridden_hill["base_yields"]["Production"], 1)

    def test_configured_tile_yield_modifier_applies_and_is_explained(self):
        hill = next(
            tile
            for tile in self.map_data["tiles"]
            if (tile["x"], tile["y"]) == (2, 2)
        )
        hill["terrain"] = "TERRAIN_GRASS_HILLS"
        hill["worked"] = True
        write_json(self.data_dir / "map.json", self.map_data)
        self.set_catalog(
            {
                "terrain_yields": {
                    "TERRAIN_GRASS": {"Food": 2},
                    "TERRAIN_GRASS_HILLS": {"Food": 2, "Production": 1},
                }
            }
        )
        modifier = {
            "id": "test-worked-hills-faith",
            "description": "Test-configured bonus",
            "enabled": True,
            "when": {"is_hills": True, "worked": True},
            "yield_changes": {"Faith": 2},
        }
        rules = copy.deepcopy(self.rules)
        rules["tile_yield_modifiers"] = [modifier]
        result = main.calculate_city_yields(
            self.city,
            self.map_data,
            main.read(self.data_dir / "catalog.json", {}),
            rules,
        )

        applied_tile = next(
            tile for tile in result["tiles"] if (tile["x"], tile["y"]) == (2, 2)
        )
        self.assertEqual(applied_tile["yields"]["Faith"], 2)
        self.assertEqual(len(applied_tile["applied_modifiers"]), 1)
        detail = applied_tile["applied_modifiers"][0]
        self.assertEqual(detail["id"], "test-worked-hills-faith")
        self.assertEqual(detail["before_yields"]["Faith"], 0)
        self.assertEqual(detail["changes"]["Faith"], 2)
        self.assertEqual(detail["after_yields"]["Faith"], 2)

    def test_tile_yield_modifier_configuration_rejects_unknown_rules(self):
        invalid = copy.deepcopy(self.rules)
        invalid["tile_yield_modifiers"] = [
            {
                "id": "invalid-yield",
                "when": {"unknown_rule": True},
                "yield_changes": {"Faith": 1},
            }
        ]
        write_json(self.data_dir / "game_rules.json", invalid)
        with self.assertRaisesRegex(RuleConfigurationError, "invalid-yield"):
            load_game_rules(self.data_dir / "game_rules.json")

    def test_missing_or_invalid_rules_fail_with_clear_api_error(self):
        from app.core import paths

        self.addCleanup(setattr, paths, "CONFIG", paths.CONFIG)
        paths.CONFIG = self.data_dir  # no fallback to app/config
        rules_path = self.data_dir / "game_rules.json"
        rules_path.unlink()
        with self.assertRaisesRegex(RuleConfigurationError, "game_rules.json"):
            main.get_game_rules()

        rules_path.write_text("{ invalid json", encoding="utf-8")
        with self.assertRaisesRegex(
            RuleConfigurationError, "Cannot read gameplay rules file"
        ):
            main.get_game_rules()

    def test_json_tile_modifier_reaches_calculation_and_tile_yield_apis(self):
        hill = next(
            tile
            for tile in self.map_data["tiles"]
            if (tile["x"], tile["y"]) == (2, 2)
        )
        hill["terrain"] = "TERRAIN_GRASS_HILLS"
        hill["worked"] = True
        write_json(self.data_dir / "map.json", self.map_data)
        self.set_catalog(
            {
                "terrain_yields": {
                    "TERRAIN_GRASS": {"Food": 2},
                    "TERRAIN_GRASS_HILLS": {"Food": 2, "Production": 1},
                }
            }
        )
        rules = copy.deepcopy(self.rules)
        rules["tile_yield_modifiers"] = [
            {
                "id": "json-worked-hills-faith",
                "description": "JSON configured Faith",
                "enabled": True,
                "when": {"is_hills": True, "worked": True},
                "yield_changes": {"Faith": 2},
            }
        ]
        write_json(self.data_dir / "game_rules.json", rules)

        city_result = main.calculate_city("city-existing")
        tile_result = main.calculate_tile_yields(2, 2)
        city_tile = next(
            tile for tile in city_result["tiles"] if (tile["x"], tile["y"]) == (2, 2)
        )
        self.assertEqual(city_tile["yields"]["Faith"], 2)
        self.assertEqual(city_result["totals"]["Faith"], 2)
        self.assertEqual(
            city_tile["applied_modifiers"][0]["id"], "json-worked-hills-faith"
        )
        self.assertEqual(
            city_tile["applied_modifiers"][0]["after_yields"]["Faith"], 2
        )
        self.assertEqual(
            tile_result["applied_modifiers"][0]["changes"]["Faith"], 2
        )

    def test_population_yields_scale_and_appear_in_city_breakdown(self):
        self.city["population"] = 3
        result = main.calculate_city_yields(
            self.city,
            self.map_data,
            main.read(self.data_dir / "catalog.json", {}),
            self.rules,
        )

        self.assertEqual(result["breakdown"]["population"]["Science"], 1.5)
        self.assertAlmostEqual(result["breakdown"]["population"]["Culture"], 0.6)
        self.assertEqual(result["totals"]["Science"], 1.5)
        self.assertAlmostEqual(result["totals"]["Culture"], 0.6)

    def test_legacy_game_rules_are_migrated_with_population_yields(self):
        legacy_rules = copy.deepcopy(self.rules)
        legacy_rules["schema_version"] = 1
        legacy_rules["city"].pop("population_yields")
        write_json(self.data_dir / "game_rules.json", legacy_rules)

        migrated = load_game_rules(self.data_dir / "game_rules.json")

        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(
            migrated["city"]["population_yields"],
            {"Science": 0.5, "Culture": 0.2},
        )

    def test_game_rules_cost_override_is_used_in_production(self):
        self.set_catalog(self.production_catalog(production=5, builder_cost=10))
        rules = copy.deepcopy(self.rules)
        rules["production"]["cost_overrides"]["unit"]["UNIT_BUILDER"] = 3
        write_json(self.data_dir / "game_rules.json", rules)

        options = main.production_catalog(
            main.read(self.data_dir / "catalog.json", {}), main.game_rules()
        )
        builder = next(item for item in options if item["id"] == "UNIT_BUILDER")
        self.assertEqual(builder["cost"], 3)
        self.assertEqual(builder["cost_source"], "game_rules.json override")

        main.add_production(
            self.city["id"],
            main.ProductionQueueRequest(category="unit", target_id="UNIT_BUILDER"),
        )
        result = main.end_turn()
        self.assertEqual(len(result["completed"]), 1)
        self.assertEqual(result["completed"][0]["type"], "unit")

    def test_empire_research_uses_science_culture_and_unlocks_prerequisites(self):
        catalog = self.production_catalog(production=0)
        catalog["terrain_yields"]["TERRAIN_GRASS"] = {
            "Science": 6,
            "Culture": 4,
        }
        catalog["raw_tables"].update(
            {
                "Technologies::Technologies": [
                    {"TechnologyType": "TECH_MINING", "Cost": "10"}
                ],
                "Technologies::TechnologyPrereqs": [],
                "Civics::Civics": [
                    {"CivicType": "CIVIC_CODE_OF_LAWS", "Cost": "9"}
                ],
                "Civics::CivicPrereqs": [],
            }
        )
        self.set_catalog(catalog)
        mine = {"PrereqTech": "TECH_MINING"}
        self.assertEqual(
            main.city_prerequisite_error(self.city, mine),
            "Requires technology TECH_MINING.",
        )
        main.select_research(
            "technology", main.ResearchSelectRequest(target_id="TECH_MINING")
        )
        main.select_research(
            "civic", main.ResearchSelectRequest(target_id="CIVIC_CODE_OF_LAWS")
        )
        main.add_production(
            self.city["id"],
            main.ProductionQueueRequest(category="unit", target_id="UNIT_BUILDER"),
        )

        first_turn = main.end_turn()
        self.assertEqual(first_turn["completed_research"], [])
        status = main.research_status()
        self.assertEqual(status["science_per_turn"], 6.5)
        self.assertEqual(status["culture_per_turn"], 4.2)
        self.assertEqual(status["tracks"]["technology"]["progress"], 6.5)
        self.assertEqual(status["tracks"]["civic"]["progress"], 4.2)
        queued_city = main.read(self.data_dir / "cities.json", {})[self.city["id"]]
        queue_id = queued_city["production_queue"][0]["queue_id"]
        self.assertEqual(queued_city["production_progress"][queue_id], 1)

        second_turn = main.end_turn()
        self.assertEqual(
            second_turn["completed_research"],
            [{"category": "technology", "id": "TECH_MINING"}],
        )
        self.assertIn("TECH_MINING", second_turn["game_state"]["technologies"])
        self.assertIsNone(second_turn["game_state"]["research"]["technology"]["current_id"])
        self.assertIsNone(main.city_prerequisite_error(self.city, mine))
        self.assertEqual(
            second_turn["game_state"]["research"]["civic"]["progress"][
                "CIVIC_CODE_OF_LAWS"
            ],
            8.4,
        )
        queued_city = main.read(self.data_dir / "cities.json", {})[self.city["id"]]
        self.assertEqual(queued_city["production_progress"][queue_id], 2)
        third_turn = main.end_turn()
        self.assertEqual(
            third_turn["completed_research"],
            [{"category": "civic", "id": "CIVIC_CODE_OF_LAWS"}],
        )
        self.assertIn("CIVIC_CODE_OF_LAWS", third_turn["game_state"]["civics"])
        queued_city = main.read(self.data_dir / "cities.json", {})[self.city["id"]]
        self.assertEqual(queued_city["production_progress"][queue_id], 3)

    def test_research_prerequisites_and_missing_cost_are_rejected(self):
        catalog = self.production_catalog()
        catalog["raw_tables"].update(
            {
                "Technologies::Technologies": [
                    {"TechnologyType": "TECH_MINING", "Cost": "25"},
                    {"TechnologyType": "TECH_MASONRY", "Cost": "50"},
                    {"TechnologyType": "TECH_UNCOSTED"},
                ],
                "Technologies::TechnologyPrereqs": [
                    {"Technology": "TECH_MASONRY", "PrereqTech": "TECH_MINING"}
                ],
                "Civics::Civics": [],
                "Civics::CivicPrereqs": [],
            }
        )
        self.set_catalog(catalog)
        with self.assertRaises(HTTPException) as prerequisite:
            main.select_research(
                "technology", main.ResearchSelectRequest(target_id="TECH_MASONRY")
            )
        self.assertEqual(prerequisite.exception.status_code, 409)
        with self.assertRaises(HTTPException) as missing_cost:
            main.select_research(
                "technology", main.ResearchSelectRequest(target_id="TECH_UNCOSTED")
            )
        self.assertEqual(missing_cost.exception.status_code, 409)

    def test_tile_yield_preview_uses_tile_yields_without_city_center_bonus(self):
        self.set_catalog(
            {
                "terrain_yields": {
                    "TERRAIN_PLAINS": {"Food": 1, "Production": 1},
                    "TERRAIN_GRASS": {"Food": 2},
                }
            }
        )
        tile = next(
            item
            for item in self.map_data["tiles"]
            if (item["x"], item["y"]) == (6, 4)
        )
        tile["terrain"] = "TERRAIN_PLAINS"
        write_json(self.data_dir / "map.json", self.map_data)

        preview = main.calculate_tile_yields(6, 4)
        self.assertTrue(preview["preview"])
        self.assertFalse(preview["city_center"])
        self.assertEqual(preview["yields"]["Food"], 1)
        self.assertEqual(preview["yields"]["Production"], 1)

        center = main.calculate_tile_yields(self.city["x"], self.city["y"])
        self.assertFalse(center["preview"])
        self.assertTrue(center["city_center"])
        self.assertEqual(center["yields"]["Production"], 1)

        with self.assertRaises(HTTPException) as error:
            main.calculate_tile_yields(99, 99)
        self.assertEqual(error.exception.status_code, 404)

    def test_builder_production_progresses_and_creates_one_unit(self):
        self.set_catalog(self.production_catalog(production=5, builder_cost=10))
        request = main.ProductionQueueRequest(
            category="unit", target_id="UNIT_BUILDER"
        )
        main.add_production(self.city["id"], request)
        first_turn = main.end_turn()
        self.assertEqual(first_turn["completed"], [])
        city = main.read(self.data_dir / "cities.json", {})[self.city["id"]]
        self.assertEqual(city["production_progress"][city["current_production"]], 5)
        self.assertEqual(main.game_state()["units"], [])

        second_turn = main.end_turn()
        self.assertEqual(len(second_turn["completed"]), 1)
        self.assertEqual(second_turn["completed"][0]["id"], "UNIT_BUILDER")
        self.assertEqual(len(main.game_state()["units"]), 1)
        main.end_turn()
        self.assertEqual(len(main.game_state()["units"]), 1)

    def test_unit_defaults_from_game_rules_apply_when_xml_values_are_missing(self):
        catalog = self.production_catalog(production=10, builder_cost=5)
        catalog["raw_tables"]["Units::Units"][0].pop("BuildCharges")
        catalog["raw_tables"]["Units::Units"][0].pop("BaseMoves")
        self.set_catalog(catalog)
        self.rules["units"]["default_build_charges"] = 4
        self.rules["units"]["default_base_moves"] = 3
        write_json(self.data_dir / "game_rules.json", self.rules)

        main.add_production(
            self.city["id"],
            main.ProductionQueueRequest(category="unit", target_id="UNIT_BUILDER"),
        )
        result = main.end_turn()

        self.assertEqual(len(result["completed"]), 1)
        unit = main.game_state()["units"][0]
        self.assertEqual(unit["charges_remaining"], 4)
        self.assertEqual(unit["moves_remaining"], 3)

    def test_production_overflow_carries_to_next_queue_item(self):
        catalog = self.production_catalog(production=12, builder_cost=5, settler_cost=7)
        self.set_catalog(catalog)
        self.city["population"] = 2
        self.cities[self.city["id"]] = self.city
        write_json(self.data_dir / "cities.json", self.cities)
        main.add_production(
            self.city["id"],
            main.ProductionQueueRequest(category="unit", target_id="UNIT_BUILDER"),
        )
        main.add_production(
            self.city["id"],
            main.ProductionQueueRequest(category="unit", target_id="UNIT_SETTLER"),
        )
        result = main.end_turn()
        self.assertEqual([item["id"] for item in result["completed"]], [
            "UNIT_BUILDER",
            "UNIT_SETTLER",
        ])
        self.assertEqual(
            {unit["unit_type"] for unit in main.game_state()["units"]},
            {"UNIT_BUILDER", "UNIT_SETTLER"},
        )
        self.assertEqual(
            main.read(self.data_dir / "cities.json", {})[self.city["id"]]["population"],
            1,
        )

    def test_production_queue_reordering_changes_current_item(self):
        self.set_catalog(self.production_catalog())
        main.add_production(
            self.city["id"],
            main.ProductionQueueRequest(category="unit", target_id="UNIT_BUILDER"),
        )
        main.add_production(
            self.city["id"],
            main.ProductionQueueRequest(category="unit", target_id="UNIT_BUILDER"),
        )
        city = main.read(self.data_dir / "cities.json", {})[self.city["id"]]
        second_queue_id = city["production_queue"][1]["queue_id"]
        result = main.reorder_production(
            self.city["id"],
            main.ProductionReorderRequest(queue_id=second_queue_id, position=0),
        )
        self.assertEqual(result["current_production"], second_queue_id)
        self.assertEqual(result["queue"][0]["queue_id"], second_queue_id)

    def test_concurrent_turn_requests_apply_production_without_lost_updates(self):
        self.set_catalog(self.production_catalog(production=3, builder_cost=20))
        main.add_production(
            self.city["id"],
            main.ProductionQueueRequest(category="unit", target_id="UNIT_BUILDER"),
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: main.end_turn(), range(2)))
        city = main.read(self.data_dir / "cities.json", {})[self.city["id"]]
        queue_id = city["current_production"]
        self.assertEqual(main.game_state()["turn"], 3)
        self.assertEqual(city["production_progress"][queue_id], 6)
        self.assertEqual(sum(len(result["completed"]) for result in results), 0)

    def test_invalid_production_targets_and_settler_population_are_rejected(self):
        self.set_catalog(self.production_catalog())
        with self.assertRaises(HTTPException) as invalid:
            main.add_production(
                self.city["id"],
                main.ProductionQueueRequest(category="unit", target_id="UNIT_WARRIOR"),
            )
        self.assertEqual(invalid.exception.status_code, 422)
        with self.assertRaises(HTTPException) as population:
            main.add_production(
                self.city["id"],
                main.ProductionQueueRequest(category="unit", target_id="UNIT_SETTLER"),
            )
        self.assertEqual(population.exception.status_code, 409)
        catalog = self.production_catalog()
        catalog["raw_tables"]["Units::Units"][0]["Cost"] = "0"
        self.set_catalog(catalog)
        with self.assertRaises(HTTPException) as cost:
            main.add_production(
                self.city["id"],
                main.ProductionQueueRequest(category="unit", target_id="UNIT_BUILDER"),
            )
        self.assertEqual(cost.exception.status_code, 422)
        self.city["population"] = 2
        self.cities[self.city["id"]] = self.city
        write_json(self.data_dir / "cities.json", self.cities)
        catalog["raw_tables"]["Units::Units"][1]["PrereqPopulation"] = "3"
        self.set_catalog(catalog)
        with self.assertRaises(HTTPException) as xml_population:
            main.add_production(
                self.city["id"],
                main.ProductionQueueRequest(category="unit", target_id="UNIT_SETTLER"),
            )
        self.assertEqual(xml_population.exception.status_code, 409)

    def test_builder_builds_only_valid_improvements_and_consumes_charge(self):
        catalog = self.production_catalog()
        catalog["raw_tables"].update(
            {
                "Improvements::Improvements": [
                    {
                        "ImprovementType": "IMPROVEMENT_FARM",
                        "Cost": "0",
                        "Buildable": "true",
                    }
                ],
                "Improvements::Improvement_ValidBuildUnits": [
                    {
                        "ImprovementType": "IMPROVEMENT_FARM",
                        "UnitType": "UNIT_BUILDER",
                    }
                ],
                "Improvements::Improvement_ValidTerrains": [
                    {
                        "ImprovementType": "IMPROVEMENT_FARM",
                        "TerrainType": "TERRAIN_GRASS",
                    }
                ],
                "Improvements::Improvement_ValidFeatures": [],
                "Improvements::Improvement_ValidResources": [],
            }
        )
        self.set_catalog(catalog)
        tile = next(
            item for item in self.map_data["tiles"] if (item["x"], item["y"]) == (2, 2)
        )
        self.set_units(
            [
                {
                    "id": "builder-1",
                    "unit_type": "UNIT_BUILDER",
                    "owner_city_id": self.city["id"],
                    "x": self.city["x"],
                    "y": self.city["y"],
                    "moves_remaining": 2,
                    "charges_remaining": 2,
                }
            ]
        )
        with self.assertRaises(HTTPException) as city_center:
            main.builder_build(
                "builder-1",
                main.BuilderActionRequest(improvement_id="IMPROVEMENT_FARM"),
            )
        self.assertEqual(city_center.exception.status_code, 409)
        self.set_units(
            [
                {
                    "id": "builder-1",
                    "unit_type": "UNIT_BUILDER",
                    "owner_city_id": self.city["id"],
                    "x": 2,
                    "y": 2,
                    "moves_remaining": 2,
                    "charges_remaining": 2,
                }
            ]
        )
        result = main.builder_build(
            "builder-1",
            main.BuilderActionRequest(improvement_id="IMPROVEMENT_FARM"),
        )
        self.assertEqual(result["tile"]["improvement"], "IMPROVEMENT_FARM")
        self.assertEqual(result["unit"]["charges_remaining"], 1)

        tile["terrain"] = "TERRAIN_MOUNTAIN"
        write_json(self.data_dir / "map.json", self.map_data)
        with self.assertRaises(HTTPException) as invalid:
            main.builder_build(
                "builder-1",
                main.BuilderActionRequest(improvement_id="IMPROVEMENT_FARM"),
            )
        self.assertEqual(invalid.exception.status_code, 409)

    def test_builder_removes_only_catalogued_features_with_required_tech(self):
        catalog = self.production_catalog()
        catalog["raw_tables"]["Features::Features"] = [
            {
                "FeatureType": "FEATURE_FOREST",
                "Removable": "true",
                "RemoveTech": "TECH_MINING",
            }
        ]
        self.set_catalog(catalog)
        self.city["technologies"] = ["TECH_MINING"]
        write_json(self.data_dir / "cities.json", self.cities)
        self.rules["improvements"]["feature_removal_charge_cost"] = 2
        write_json(self.data_dir / "game_rules.json", self.rules)
        tile = next(
            item for item in self.map_data["tiles"] if (item["x"], item["y"]) == (2, 2)
        )
        tile["feature"] = "FEATURE_FOREST"
        write_json(self.data_dir / "map.json", self.map_data)
        self.set_units(
            [
                {
                    "id": "builder-1",
                    "unit_type": "UNIT_BUILDER",
                    "owner_city_id": self.city["id"],
                    "x": 2,
                    "y": 2,
                    "moves_remaining": 2,
                    "charges_remaining": 2,
                }
            ]
        )
        result = main.builder_remove_feature("builder-1")
        self.assertIsNone(result["tile"]["feature"])
        self.assertEqual(result["unit"]["charges_remaining"], 0)

    def test_district_default_limit_is_loaded_from_game_rules(self):
        catalog = self.production_catalog(production=10)
        catalog["raw_tables"]["Districts::Districts"] = [
            {"DistrictType": "DISTRICT_CAMPUS", "Cost": "10"}
        ]
        self.set_catalog(catalog)
        self.rules["districts"]["default_max_per_city"] = 2
        write_json(self.data_dir / "game_rules.json", self.rules)

        owned_sites = [
            (tile["x"], tile["y"])
            for tile in self.map_data["tiles"]
            if tile.get("owner_city_id") == self.city["id"]
            and (tile["x"], tile["y"]) != (self.city["x"], self.city["y"])
        ]
        for x, y in owned_sites[:2]:
            main.add_production(
                self.city["id"],
                main.ProductionQueueRequest(
                    category="district",
                    target_id="DISTRICT_CAMPUS",
                    x=x,
                    y=y,
                ),
            )
        with self.assertRaises(HTTPException) as error:
            main.add_production(
                self.city["id"],
                main.ProductionQueueRequest(
                    category="district",
                    target_id="DISTRICT_CAMPUS",
                    x=owned_sites[2][0],
                    y=owned_sites[2][1],
                ),
            )
        self.assertEqual(error.exception.status_code, 409)

    def test_settler_founds_city_and_is_consumed(self):
        self.set_catalog(self.production_catalog())
        self.set_units(
            [
                {
                    "id": "settler-1",
                    "unit_type": "UNIT_SETTLER",
                    "owner_city_id": self.city["id"],
                    "x": 4,
                    "y": 2,
                    "moves_remaining": 2,
                    "charges_remaining": 0,
                }
            ]
        )
        result = main.settler_found_city(
            "settler-1", main.FoundCityRequest(name="New Settlement")
        )
        city = result["city"]
        self.assertEqual((city["x"], city["y"], city["population"]), (4, 2, 1))
        self.assertEqual(main.game_state()["units"], [])
        self.assertEqual(
            sum(
                tile.get("owner_city_id") == city["id"]
                for tile in main.read(self.data_dir / "map.json", {})["tiles"]
            ),
            7,
        )

    def test_settler_rejects_distance_and_territory_conflicts(self):
        self.set_catalog(self.production_catalog())
        self.set_units(
            [
                {
                    "id": "settler-1",
                    "unit_type": "UNIT_SETTLER",
                    "owner_city_id": self.city["id"],
                    "x": 3,
                    "y": 2,
                    "moves_remaining": 2,
                    "charges_remaining": 0,
                }
            ]
        )
        with self.assertRaises(HTTPException) as distance:
            main.settler_found_city(
                "settler-1", main.FoundCityRequest(name="Too Close")
            )
        self.assertEqual(distance.exception.status_code, 409)
        self.assertEqual(len(main.game_state()["units"]), 1)

        self.city["x"], self.city["y"] = 1, 2
        state = main.game_state()
        state["units"][0]["x"] = 4
        write_json(main.GAME_STATE, state)
        tile = next(
            item for item in self.map_data["tiles"] if (item["x"], item["y"]) == (5, 2)
        )
        cities = self.cities | {
            "city-other": {
                **self.city,
                "id": "city-other",
                "name": "Other",
                "x": 0,
                "y": 0,
            }
        }
        tile["owner_city_id"] = "city-other"
        write_json(self.data_dir / "map.json", self.map_data)
        write_json(self.data_dir / "cities.json", cities)
        with self.assertRaises(HTTPException) as conflict:
            main.settler_found_city(
                "settler-1", main.FoundCityRequest(name="Overlap")
            )
        self.assertEqual(conflict.exception.status_code, 409)

    def test_settler_cannot_found_city_on_xml_water_terrain(self):
        catalog = self.production_catalog()
        catalog["raw_tables"]["Terrains::Terrains"] = [
            {"TerrainType": "TERRAIN_COAST", "Water": "true"}
        ]
        self.set_catalog(catalog)
        self.map_data["tiles"] = [
            tile
            for tile in self.map_data["tiles"]
            if not (tile["x"] == 4 and tile["y"] == 2)
        ] + [
            {
                "x": 4,
                "y": 2,
                "terrain": "TERRAIN_COAST",
                "owner_city_id": None,
                "worked": False,
            }
        ]
        write_json(self.data_dir / "map.json", self.map_data)
        self.set_units(
            [
                {
                    "id": "settler-1",
                    "unit_type": "UNIT_SETTLER",
                    "owner_city_id": self.city["id"],
                    "x": 4,
                    "y": 2,
                    "moves_remaining": 2,
                    "charges_remaining": 0,
                }
            ]
        )
        with self.assertRaises(HTTPException) as error:
            main.settler_found_city(
                "settler-1", main.FoundCityRequest(name="Coastal City")
            )
        self.assertEqual(error.exception.status_code, 409)

    def test_district_and_building_prerequisites_and_completion_yields(self):
        catalog = self.production_catalog(production=100)
        catalog["raw_tables"]["Districts::Districts"] = [
            {
                "DistrictType": "DISTRICT_CAMPUS",
                "Cost": "10",
                "PrereqTech": "TECH_WRITING",
                "RequiresPlacement": "true",
            }
        ]
        catalog["raw_tables"]["Buildings::Buildings"] = [
            {
                "BuildingType": "BUILDING_LIBRARY",
                "Cost": "15",
                "PrereqDistrict": "DISTRICT_CAMPUS",
            }
        ]
        catalog["raw_tables"]["Buildings::Types"] = [
            {"Type": "BUILDING_LIBRARY", "Kind": "KIND_BUILDING"}
        ]
        catalog["building_yields"] = {"BUILDING_LIBRARY": {"Science": 2}}
        self.set_catalog(catalog)
        with self.assertRaises(HTTPException) as prerequisite:
            main.add_production(
                self.city["id"],
                main.ProductionQueueRequest(
                    category="district",
                    target_id="DISTRICT_CAMPUS",
                    x=2,
                    y=2,
                ),
            )
        self.assertEqual(prerequisite.exception.status_code, 409)

        with self.assertRaises(HTTPException) as missing_district:
            main.add_production(
                self.city["id"],
                main.ProductionQueueRequest(
                    category="building", target_id="BUILDING_LIBRARY"
                ),
            )
        self.assertEqual(missing_district.exception.status_code, 409)

        self.city["technologies"] = ["TECH_WRITING"]
        write_json(self.data_dir / "cities.json", self.cities)
        main.add_production(
            self.city["id"],
            main.ProductionQueueRequest(
                category="district",
                target_id="DISTRICT_CAMPUS",
                x=2,
                y=2,
            ),
        )
        result = main.end_turn()
        self.assertEqual(result["completed"][0]["type"], "district")
        self.assertEqual(
            result["map_changes"],
            [{"x": 2, "y": 2, "district": "DISTRICT_CAMPUS", "worked": False, "improvement": None}],
        )
        map_data = main.read(self.data_dir / "map.json", {})
        self.assertEqual(
            next(tile for tile in map_data["tiles"] if (tile["x"], tile["y"]) == (2, 2))[
                "district"
            ],
            "DISTRICT_CAMPUS",
        )
        main.add_production(
            self.city["id"],
            main.ProductionQueueRequest(
                category="building", target_id="BUILDING_LIBRARY"
            ),
        )
        self.assertEqual(
            main.calculate_city_yields(
                self.city, map_data, catalog
            )["totals"]["Science"],
            0.5,
        )
        completed = main.end_turn()
        self.assertEqual(completed["completed"][0]["type"], "building")
        city = main.read(self.data_dir / "cities.json", {})[self.city["id"]]
        self.assertIn("BUILDING_LIBRARY", city["buildings"])
        self.assertEqual(
            main.calculate_city_yields(
                city, main.read(self.data_dir / "map.json", {}), catalog
            )["totals"]["Science"],
            2.5,
        )

    def test_unit_movement_checks_adjacency_and_turn_resets_movement(self):
        self.set_units(
            [
                {
                    "id": "builder-1",
                    "unit_type": "UNIT_BUILDER",
                    "owner_city_id": self.city["id"],
                    "x": 1,
                    "y": 2,
                    "moves_remaining": 1,
                    "charges_remaining": 3,
                }
            ]
        )
        state_path = main.GAME_STATE
        original_state_bytes = state_path.read_bytes()
        with self.assertRaises(HTTPException) as nonadjacent:
            main.move_unit("builder-1", main.UnitMoveRequest(x=4, y=2))
        self.assertEqual(nonadjacent.exception.status_code, 422)
        with patch(
            "app.services.unit_service.map_repository.get_map",
            side_effect=AssertionError("Movement must not copy the full map."),
        ):
            moved = main.move_unit("builder-1", main.UnitMoveRequest(x=2, y=2))
        self.assertEqual(moved["moves_remaining"], 0)
        self.assertEqual(state_path.read_bytes(), original_state_bytes)
        with self.assertRaises(HTTPException) as spent:
            main.move_unit("builder-1", main.UnitMoveRequest(x=3, y=2))
        self.assertEqual(spent.exception.status_code, 409)
        main.save_map()
        self.assertEqual(
            json.loads(state_path.read_text(encoding="utf-8"))["units"][0]["x"],
            2,
        )
        self.set_catalog(self.production_catalog())
        main.end_turn()
        self.assertEqual(main.game_state()["units"][0]["moves_remaining"], 2)

    def test_map_tile_lookup_index_tracks_map_updates(self):
        self.assertTrue(map_repository.has_tile(2, 2))
        map_data = map_repository.get_map({})
        map_data["tiles"] = [
            tile
            for tile in map_data["tiles"]
            if (tile["x"], tile["y"]) != (2, 2)
        ]
        map_repository.save_map(map_data)
        self.assertFalse(map_repository.has_tile(2, 2))

    def test_entity_catalog_reports_research_and_placement_availability(self):
        catalog = {
            "raw_tables": {
                "Buildings::Types": [
                    {"Type": "BUILDING_LIBRARY", "Kind": "KIND_BUILDING"},
                    {"Type": "BUILDING_WONDER", "Kind": "KIND_WONDER"},
                ],
                "Buildings::Buildings": [
                    {
                        "BuildingType": "BUILDING_LIBRARY",
                        "PrereqTech": "TECH_WRITING",
                    },
                    {"BuildingType": "BUILDING_WONDER"},
                ],
                "Districts::Districts": [
                    {
                        "DistrictType": "DISTRICT_CAMPUS",
                        "PrereqCivic": "CIVIC_WRITING",
                    }
                ],
                "Districts::District_ValidTerrains": [
                    {
                        "DistrictType": "DISTRICT_CAMPUS",
                        "TerrainType": "TERRAIN_GRASS",
                    }
                ],
                "Units::Units": [
                    {"UnitType": "UNIT_TEST", "PrereqCivic": "CIVIC_WRITING"}
                ],
                "Policies::Policies": [
                    {"PolicyType": "POLICY_TEST", "PrereqCivic": "CIVIC_WRITING"}
                ],
                "Governments::Governments": [
                    {
                        "GovernmentType": "GOVERNMENT_TEST",
                        "PrereqCivic": "CIVIC_WRITING",
                    }
                ],
                "Improvements::Improvements": [
                    {"ImprovementType": "IMPROVEMENT_TEST", "PrereqTech": "TECH_WRITING"}
                ],
                "Improvements::Improvement_ValidTerrains": [
                    {
                        "ImprovementType": "IMPROVEMENT_TEST",
                        "TerrainType": "TERRAIN_GRASS",
                    }
                ],
                "Projects::Projects": [
                    {"ProjectType": "PROJECT_TEST", "PrereqDistrict": "DISTRICT_CAMPUS"}
                ],
                "Technologies::Technologies": [
                    {"TechnologyType": "TECH_WRITING", "Name": "Writing"}
                ],
                "Technologies::TechnologyPrereqs": [
                    {
                        "Technology": "TECH_WRITING",
                        "PrereqTech": "TECH_POTTERY",
                    }
                ],
                "Civics::Civics": [{"CivicType": "CIVIC_WRITING"}],
            }
        }
        self.set_catalog(catalog)

        result = main.get_entity_catalog()
        self.assertEqual(
            set(result["categories"]),
            {
                "buildings",
                "wonders",
                "units",
                "policies",
                "governments",
                "improvements",
                "districts",
                "projects",
                "technologies",
                "civics",
            },
        )
        building = result["categories"]["buildings"]["items"][0]
        self.assertFalse(building["available"])
        self.assertEqual(
            building["unavailable_reasons"], ["Requires technology TECH_WRITING."]
        )
        wonder = result["categories"]["wonders"]["items"][0]
        self.assertTrue(wonder["available"])
        technology = result["categories"]["technologies"]["items"][0]
        self.assertEqual(
            technology["unavailable_reasons"],
            ["Requires prerequisite research: TECH_POTTERY."],
        )

        self.set_units([])
        write_json(
            main.GAME_STATE,
            {
                "turn": 1,
                "units": [],
                "technologies": ["TECH_WRITING"],
                "civics": ["CIVIC_WRITING"],
            },
        )
        placement = main.get_entity_catalog(
            category="districts",
            city_id=self.city["id"],
            x=2,
            y=2,
        )
        campus = placement["categories"]["districts"]["items"][0]
        self.assertTrue(campus["available"])
        invalid_placement = main.get_entity_catalog(
            category="districts",
            city_id=self.city["id"],
            x=3,
            y=2,
        )
        self.assertFalse(
            invalid_placement["categories"]["districts"]["items"][0]["available"]
        )

    def test_force_research_unlock_and_lock_updates_legacy_city_state(self):
        catalog = self.production_catalog()
        catalog["raw_tables"].update(
            {
                "Technologies::Technologies": [
                    {"TechnologyType": "TECH_FORCED", "Cost": "10"}
                ],
                "Technologies::TechnologyPrereqs": [
                    {
                        "Technology": "TECH_FORCED",
                        "PrereqTech": "TECH_MISSING",
                    }
                ],
                "Civics::Civics": [
                    {"CivicType": "CIVIC_FORCED", "Cost": "10"}
                ],
                "Civics::CivicPrereqs": [
                    {"Civic": "CIVIC_FORCED", "PrereqCivic": "CIVIC_MISSING"}
                ],
            }
        )
        self.set_catalog(catalog)
        unlocked = main.set_research_availability(
            "technology",
            "TECH_FORCED",
            main.ResearchAvailabilityRequest(unlocked=True),
        )
        self.assertIn("TECH_FORCED", unlocked["technologies"])
        self.assertIsNone(main.city_prerequisite_error(self.city, {"PrereqTech": "TECH_FORCED"}))

        main.set_research_availability(
            "civic",
            "CIVIC_FORCED",
            main.ResearchAvailabilityRequest(unlocked=True),
        )
        self.assertIn("CIVIC_FORCED", main.game_state()["civics"])
        state = main.game_state()
        state["research"]["technology"]["current_id"] = "TECH_FORCED"
        state["research"]["technology"]["progress"]["TECH_FORCED"] = 8
        self.city["technologies"] = ["TECH_FORCED"]
        self.city["civics"] = ["CIVIC_FORCED"]
        write_json(main.GAME_STATE, state)
        write_json(self.data_dir / "cities.json", self.cities)

        locked = main.set_research_availability(
            "technology",
            "TECH_FORCED",
            main.ResearchAvailabilityRequest(unlocked=False),
        )
        self.assertNotIn("TECH_FORCED", locked["technologies"])
        self.assertIsNone(locked["tracks"]["technology"]["current_id"])
        self.assertNotIn(
            "TECH_FORCED",
            locked["tracks"]["technology"]["completed"],
        )
        saved_city = main.read(self.data_dir / "cities.json", {})[
            self.city["id"]
        ]
        self.assertNotIn("TECH_FORCED", saved_city["technologies"])
        self.assertIn("CIVIC_FORCED", main.game_state()["civics"])
        civic_lock = main.set_research_availability(
            "civic",
            "CIVIC_FORCED",
            main.ResearchAvailabilityRequest(unlocked=False),
        )
        self.assertNotIn("CIVIC_FORCED", civic_lock["civics"])
        with self.assertRaises(HTTPException) as unknown:
            main.set_research_availability(
                "technology",
                "TECH_UNKNOWN",
                main.ResearchAvailabilityRequest(unlocked=True),
            )
        self.assertEqual(unknown.exception.status_code, 404)

    def test_force_create_and_delete_any_catalogued_unit(self):
        catalog = self.production_catalog()
        catalog["raw_tables"]["Units::Units"].append(
            {
                "UnitType": "UNIT_FORCE_TEST",
                "BaseMoves": "3",
                "BuildCharges": "2",
                "PrereqTech": "TECH_FORCE_REQUIRED",
            }
        )
        self.set_catalog(catalog)
        with self.assertRaises(HTTPException) as unavailable:
            main.force_create_unit(
                main.ForceUnitCreateRequest(
                    unit_type="UNIT_FORCE_TEST",
                    owner_city_id=self.city["id"],
                )
            )
        self.assertEqual(unavailable.exception.status_code, 409)
        forced_options = main.get_entity_catalog(
            category="units",
            city_id=self.city["id"],
        )["categories"]["units"]["items"]
        self.assertIn("UNIT_FORCE_TEST", {item["id"] for item in forced_options})
        self.assertFalse(
            next(
                item
                for item in forced_options
                if item["id"] == "UNIT_FORCE_TEST"
            )["available"]
        )
        write_json(
            main.GAME_STATE,
            {"turn": 1, "units": [], "technologies": ["TECH_FORCE_REQUIRED"]},
        )
        unit = main.force_create_unit(
            main.ForceUnitCreateRequest(
                unit_type="UNIT_FORCE_TEST",
                owner_city_id=self.city["id"],
            )
        )
        self.assertEqual(unit["unit_type"], "UNIT_FORCE_TEST")
        self.assertEqual((unit["x"], unit["y"]), (self.city["x"], self.city["y"]))
        self.assertEqual(unit["moves_remaining"], 3)
        self.assertEqual(unit["charges_remaining"], 2)
        self.assertEqual(main.game_state()["units"], [unit])
        snapshot_state = main.validate_game_state_snapshot(
            main.game_state(),
            self.cities,
            self.map_data,
        )
        self.assertEqual(snapshot_state["units"], [unit])

        result = main.force_delete_unit(unit["id"])
        self.assertEqual(result, {"deleted": unit["id"]})
        self.assertEqual(main.game_state()["units"], [])
        with self.assertRaises(HTTPException) as missing:
            main.force_delete_unit(unit["id"])
        self.assertEqual(missing.exception.status_code, 404)
        with self.assertRaises(HTTPException) as unknown:
            main.force_create_unit(
                main.ForceUnitCreateRequest(
                    unit_type="UNIT_UNKNOWN",
                    owner_city_id=self.city["id"],
                )
            )
        self.assertEqual(unknown.exception.status_code, 422)

    def test_force_add_entities_checks_requirements_and_tile_placement(self):
        catalog = self.production_catalog()
        catalog["raw_tables"].update(
            {
                "Buildings::Types": [
                    {"Type": "BUILDING_TEST", "Kind": "KIND_BUILDING"},
                    {"Type": "BUILDING_WONDER_TEST", "Kind": "KIND_BUILDING"},
                ],
                "Buildings::Buildings": [
                    {
                        "BuildingType": "BUILDING_TEST",
                        "PrereqTech": "TECH_TEST",
                        "PrereqDistrict": "DISTRICT_CAMPUS",
                    },
                    {
                        "BuildingType": "BUILDING_WONDER_TEST",
                        "IsWonder": "true",
                        "PrereqCivic": "CIVIC_TEST",
                        "MaxWorldInstances": "1",
                    },
                ],
                "Buildings::BuildingPrereqs": [],
                "Buildings::MutuallyExclusiveBuildings": [],
                "Districts::Districts": [
                    {"DistrictType": "DISTRICT_CAMPUS", "PrereqTech": "TECH_TEST"}
                ],
                "Districts::District_ValidTerrains": [
                    {
                        "DistrictType": "DISTRICT_CAMPUS",
                        "TerrainType": "TERRAIN_GRASS",
                    }
                ],
                "Improvements::Improvements": [
                    {
                        "ImprovementType": "IMPROVEMENT_TEST",
                        "Buildable": "true",
                        "PrereqCivic": "CIVIC_TEST",
                    }
                ],
                "Improvements::Improvement_ValidTerrains": [
                    {
                        "ImprovementType": "IMPROVEMENT_TEST",
                        "TerrainType": "TERRAIN_GRASS",
                    }
                ],
                "Improvements::Improvement_ValidFeatures": [],
                "Improvements::Improvement_ValidResources": [],
            }
        )
        self.set_catalog(catalog)
        write_json(
            main.GAME_STATE,
            {
                "turn": 1,
                "units": [],
                "technologies": ["TECH_TEST"],
            },
        )
        tiles = [
            tile
            for tile in self.map_data["tiles"]
            if tile["owner_city_id"] == self.city["id"]
            and (tile["x"], tile["y"]) != (self.city["x"], self.city["y"])
        ]
        district_tile, wonder_tile, improvement_tile = tiles[:3]

        with self.assertRaises(HTTPException) as missing_civic:
            main.force_add_entity(
                main.ForceEntityRequest(
                    category="wonders",
                    entity_id="BUILDING_WONDER_TEST",
                    city_id=self.city["id"],
                    x=wonder_tile["x"],
                    y=wonder_tile["y"],
                )
            )
        self.assertEqual(missing_civic.exception.status_code, 409)

        with self.assertRaises(HTTPException) as missing_district:
            main.force_add_entity(
                main.ForceEntityRequest(
                    category="buildings",
                    entity_id="BUILDING_TEST",
                    city_id=self.city["id"],
                )
            )
        self.assertEqual(missing_district.exception.status_code, 409)
        self.assertIn("Requires completed district", missing_district.exception.detail)
        district = main.force_add_entity(
            main.ForceEntityRequest(
                category="districts",
                entity_id="DISTRICT_CAMPUS",
                city_id=self.city["id"],
                x=district_tile["x"],
                y=district_tile["y"],
            )
        )
        self.assertEqual(district["category"], "districts")
        building = main.force_add_entity(
            main.ForceEntityRequest(
                category="buildings",
                entity_id="BUILDING_TEST",
                city_id=self.city["id"],
            )
        )
        self.assertEqual(building["id"], "BUILDING_TEST")

        write_json(
            main.GAME_STATE,
            {
                "turn": 1,
                "units": [],
                "technologies": ["TECH_TEST"],
                "civics": ["CIVIC_TEST"],
            },
        )
        wonder = main.force_add_entity(
            main.ForceEntityRequest(
                category="wonders",
                entity_id="BUILDING_WONDER_TEST",
                city_id=self.city["id"],
                x=wonder_tile["x"],
                y=wonder_tile["y"],
            )
        )
        self.assertEqual(wonder["category"], "wonders")
        improvement = main.force_add_entity(
            main.ForceEntityRequest(
                category="improvements",
                entity_id="IMPROVEMENT_TEST",
                city_id=self.city["id"],
                x=improvement_tile["x"],
                y=improvement_tile["y"],
            )
        )
        self.assertEqual(improvement["category"], "improvements")

        with self.assertRaises(HTTPException) as invalid_site:
            main.force_add_entity(
                main.ForceEntityRequest(
                    category="improvements",
                    entity_id="IMPROVEMENT_TEST",
                    city_id=self.city["id"],
                    x=self.city["x"],
                    y=self.city["y"],
                )
            )
        self.assertEqual(invalid_site.exception.status_code, 409)

    def test_force_wonder_checks_adjacent_mountain_requirement(self):
        catalog = {
            "raw_tables": {
                "Buildings::Types": [
                    {"Type": "BUILDING_MOUNTAIN_WONDER", "Kind": "KIND_BUILDING"}
                ],
                "Buildings::Buildings": [
                    {
                        "BuildingType": "BUILDING_MOUNTAIN_WONDER",
                        "IsWonder": "true",
                        "AdjacentToMountain": "true",
                        "MaxWorldInstances": "1",
                    }
                ],
                "Buildings::BuildingPrereqs": [],
                "Buildings::MutuallyExclusiveBuildings": [],
            }
        }
        self.set_catalog(catalog)
        target = next(
            tile
            for tile in self.map_data["tiles"]
            if tile["owner_city_id"] == self.city["id"]
            and (tile["x"], tile["y"]) != (self.city["x"], self.city["y"])
        )
        with self.assertRaises(HTTPException) as no_mountain:
            main.force_add_entity(
                main.ForceEntityRequest(
                    category="wonders",
                    entity_id="BUILDING_MOUNTAIN_WONDER",
                    city_id=self.city["id"],
                    x=target["x"],
                    y=target["y"],
                )
            )
        self.assertIn("adjacent mountain", no_mountain.exception.detail)

        adjacent = next(
            tile
            for tile in self.map_data["tiles"]
            if main.hex_distance(
                target["x"], target["y"], tile["x"], tile["y"]
            ) == 1
        )
        adjacent["terrain"] = "TERRAIN_MOUNTAIN"
        write_json(self.data_dir / "map.json", self.map_data)
        result = main.force_add_entity(
            main.ForceEntityRequest(
                category="wonders",
                entity_id="BUILDING_MOUNTAIN_WONDER",
                city_id=self.city["id"],
                x=target["x"],
                y=target["y"],
            )
        )
        self.assertEqual(result["id"], "BUILDING_MOUNTAIN_WONDER")


if __name__ == "__main__":
    unittest.main()
