import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import main
from app.config import RuleConfigurationError, load_game_rules


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
            (Path(main.__file__).parent / "data" / "game_rules.json").read_text(
                encoding="utf-8"
            )
        )
        write_json(self.data_dir / "game_rules.json", self.rules)
        self.data_patch = patch.object(main, "DATA", self.data_dir)
        self.saved_patch = patch.object(main, "SAVED_MAPS", self.saved_dir)
        self.game_patch = patch.object(main, "GAME_STATE", self.data_dir / "game_state.json")
        self.data_patch.start()
        self.saved_patch.start()
        self.game_patch.start()

    def tearDown(self):
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
        result = main.save_map_as(main.SaveMapRequest(name="Backup"))
        self.assertEqual(result["name"], "Backup")
        snapshot = json.loads((self.saved_dir / "Backup.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["map"], self.map_data)
        with self.assertRaises(HTTPException) as error:
            main.save_map_as(main.SaveMapRequest(name="Backup"))
        self.assertEqual(error.exception.status_code, 409)

        loaded = main.load_saved_map(main.SaveMapRequest(name="Backup"))
        self.assertEqual(loaded["map"], self.map_data)
        self.assertEqual(loaded["cities"], [self.city])
        self.assertEqual(loaded["game_state"]["units"], [unit])
        self.assertEqual(main.read(self.data_dir / "map.json", {}), self.map_data)

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
        with self.assertRaises(HTTPException) as error:
            main.load_saved_map(main.SaveMapRequest(name="Broken"))
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(main.read(self.data_dir / "map.json", {}), before)

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
        with self.assertRaises(HTTPException) as nonadjacent:
            main.move_unit("builder-1", main.UnitMoveRequest(x=4, y=2))
        self.assertEqual(nonadjacent.exception.status_code, 422)
        moved = main.move_unit("builder-1", main.UnitMoveRequest(x=2, y=2))
        self.assertEqual(moved["moves_remaining"], 0)
        with self.assertRaises(HTTPException) as spent:
            main.move_unit("builder-1", main.UnitMoveRequest(x=3, y=2))
        self.assertEqual(spent.exception.status_code, 409)
        self.set_catalog(self.production_catalog())
        main.end_turn()
        self.assertEqual(main.game_state()["units"][0]["moves_remaining"], 2)


if __name__ == "__main__":
    unittest.main()
