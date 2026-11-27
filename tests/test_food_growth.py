import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import main
from app.core import paths
from app.config import load_game_rules
from app.core.game_speed import GROWTH_COST_MULTIPLIERS, GAME_SPEEDS
from app.repositories.json_repository import invalidate_active_document_cache
from app.services.food_service import (
    calculate_food_consumption,
    calculate_food_required,
    city_food_status,
    process_city_food_turn,
)
from app.services.yield_service import calculate_city_yields


class FoodGrowthTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.data_dir = self.root / "data"
        self.saved_dir = self.data_dir / "saved_maps"
        self.game_state_path = self.data_dir / "game_state.json"
        self.rules = load_game_rules(
            Path(main.__file__).parent / "config" / "game_rules.json"
        )
        self.data_dir.mkdir()
        self._patches = [
            patch.object(main, "DATA", self.data_dir),
            patch.object(main, "SAVED_MAPS", self.saved_dir),
            patch.object(main, "GAME_STATE", self.game_state_path),
            patch.object(paths, "DATA", self.data_dir),
            patch.object(paths, "SAVED_MAPS", self.saved_dir),
            patch.object(paths, "GAME_STATE", self.game_state_path),
        ]
        for item in self._patches:
            item.start()

    def tearDown(self):
        for item in reversed(self._patches):
            item.stop()
        self.temp_dir.cleanup()

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        invalidate_active_document_cache(path)

    @staticmethod
    def city(population=1, stored_food=0.0):
        return {
            "id": "city-test",
            "name": "Test City",
            "x": 1,
            "y": 1,
            "population": population,
            "stored_food": stored_food,
            "buildings": [],
            "wonders": [],
        }

    def test_growth_requirement_formula_at_standard_speed(self):
        self.assertEqual(calculate_food_required(1, "STANDARD"), 15.0)
        self.assertEqual(calculate_food_required(2, "STANDARD"), 24.0)
        self.assertAlmostEqual(calculate_food_required(3, "STANDARD"), 33.8284271247)
        self.assertAlmostEqual(calculate_food_required(4, "STANDARD"), 44.1961524227)

    def test_growth_requirement_rejects_non_positive_population(self):
        for population in (0, -1):
            with self.subTest(population=population):
                with self.assertRaises(ValueError):
                    calculate_food_required(population, "STANDARD")

    def test_standard_speed_and_unverified_speeds(self):
        self.assertEqual(self.rules["game_speed"], "STANDARD")
        self.assertEqual(GROWTH_COST_MULTIPLIERS["STANDARD"], 1.0)
        self.assertEqual(
            GAME_SPEEDS,
            {"STANDARD", "QUICK", "ONLINE", "EPIC", "MARATHON"},
        )
        for speed in GAME_SPEEDS - {"STANDARD"}:
            with self.subTest(speed=speed):
                with self.assertRaisesRegex(ValueError, "not verified"):
                    calculate_food_required(1, speed)

    def test_configured_food_consumption_scales_with_population(self):
        self.assertEqual(
            calculate_food_consumption(3, "STANDARD", self.rules), 6.0
        )

    def test_city_food_uses_existing_yield_calculation(self):
        city = self.city()
        map_data = {
            "tiles": [
                {
                    "x": 1,
                    "y": 1,
                    "terrain": "TERRAIN_TEST",
                    "owner_city_id": city["id"],
                    "worked": True,
                },
                {
                    "x": 2,
                    "y": 1,
                    "terrain": "TERRAIN_TEST",
                    "owner_city_id": city["id"],
                    "worked": True,
                },
            ]
        }
        catalog = {"terrain_yields": {"TERRAIN_TEST": {"Food": 1}}}
        result = calculate_city_yields(city, map_data, catalog, self.rules)
        status = city_food_status(
            city, result["totals"]["Food"], "STANDARD", self.rules
        )
        self.assertEqual(result["totals"]["Food"], 3.0)
        self.assertEqual(status["food_yield"], result["totals"]["Food"])
        map_data["tiles"][1]["worked"] = False
        unworked_result = calculate_city_yields(city, map_data, catalog, self.rules)
        self.assertEqual(unworked_result["totals"]["Food"], 2.0)

    def test_food_api_matches_existing_city_yield_api(self):
        city = self.city()
        map_data = {
            "width": 3,
            "height": 3,
            "count": 2,
            "tiles": [
                {
                    "x": 1,
                    "y": 1,
                    "terrain": "TERRAIN_TEST",
                    "owner_city_id": city["id"],
                    "worked": True,
                },
                {
                    "x": 2,
                    "y": 1,
                    "terrain": "TERRAIN_TEST",
                    "owner_city_id": city["id"],
                    "worked": True,
                },
            ],
        }
        catalog = {"terrain_yields": {"TERRAIN_TEST": {"Food": 1}}}
        self.write_json(self.data_dir / "map.json", map_data)
        self.write_json(self.data_dir / "cities.json", {city["id"]: city})
        self.write_json(self.data_dir / "catalog.json", catalog)
        self.write_json(self.data_dir / "game_rules.json", self.rules)
        self.write_json(
            self.game_state_path,
            {"turn": 1, "units": [], "game_speed": "STANDARD"},
        )

        yields = main.calculate_city(city["id"])
        food = main.get_city_food(city["id"])

        self.assertEqual(food["food_yield"], yields["totals"]["Food"])
        self.assertEqual(food["food_yield"], 3.0)
        self.assertEqual(food["food_consumption"], 2.0)
        self.assertEqual(food["food_surplus"], 1.0)

    def test_growth_accumulates_surplus_and_recalculates_requirement(self):
        rules = copy.deepcopy(self.rules)
        rules["food"]["max_growths_per_turn"] = 1
        city = self.city(stored_food=15.0)

        initial_status = city_food_status(city, 4.0, "STANDARD", rules)
        result = process_city_food_turn(city, 4.0, "STANDARD", rules)

        self.assertEqual(initial_status["food_surplus"], 2.0)
        self.assertEqual(city["population"], 2)
        self.assertEqual(city["stored_food"], 2.0)
        self.assertEqual(city["food_required"], 24.0)
        self.assertEqual(result["food_surplus"], 0.0)

    def test_food_deficit_reduces_storage_but_never_below_zero(self):
        city = self.city(stored_food=1.0)
        result = process_city_food_turn(city, 0.0, "STANDARD", self.rules)
        self.assertEqual(result["food_surplus"], -2.0)
        self.assertEqual(city["stored_food"], 0.0)
        self.assertEqual(city["population"], 1)

    def test_growth_is_automatic_by_default_and_subtracts_required_food(self):
        city = self.city(stored_food=100.0)
        result = process_city_food_turn(city, 2.0, "STANDARD", self.rules)
        self.assertEqual(city["population"], 2)
        self.assertEqual(city["stored_food"], 85.0)
        self.assertTrue(result["growth_enabled"])
        self.assertEqual(result["growths"], 1)

    def test_insufficient_food_keeps_population_and_accumulates_storage(self):
        rules = copy.deepcopy(self.rules)
        rules["food"]["max_growths_per_turn"] = 1
        city = self.city(stored_food=14.0)

        process_city_food_turn(city, 2.0, "STANDARD", rules)

        self.assertEqual(city["population"], 1)
        self.assertEqual(city["stored_food"], 14.0)

    def test_configured_growth_cap_allows_multiple_growths_and_keeps_remainder(self):
        rules = copy.deepcopy(self.rules)
        rules["food"]["max_growths_per_turn"] = 2
        city = self.city(stored_food=50.0)

        result = process_city_food_turn(city, 2.0, "STANDARD", rules)

        self.assertEqual(result["growths"], 2)
        self.assertEqual(city["population"], 3)
        self.assertEqual(city["stored_food"], 11.0)
        self.assertAlmostEqual(city["food_required"], 33.8284271247)

    def test_turn_end_persists_growth_and_required_food(self):
        rules = copy.deepcopy(self.rules)
        rules["food"]["max_growths_per_turn"] = 1
        city = self.city(stored_food=15.0)
        tiles = [
            {
                "x": x,
                "y": y,
                "terrain": "TERRAIN_GRASS",
                "owner_city_id": city["id"]
                if max(abs(x - 1), abs(y - 1), abs((x - 1) + (y - 1))) <= 1
                else None,
                "worked": x == 1 and y == 1,
            }
            for y in range(5)
            for x in range(7)
        ]
        map_data = {"width": 7, "height": 5, "count": len(tiles), "tiles": tiles}
        other_city = self.city()
        other_city.update(
            {"id": "city-other", "name": "Other City", "x": 5, "y": 3}
        )
        other_center = next(
            tile for tile in tiles if (tile["x"], tile["y"]) == (5, 3)
        )
        other_center["owner_city_id"] = other_city["id"]
        other_center["worked"] = True
        self.write_json(self.data_dir / "map.json", map_data)
        self.write_json(
            self.data_dir / "cities.json",
            {city["id"]: city, other_city["id"]: other_city},
        )
        self.write_json(self.data_dir / "catalog.json", {})
        self.write_json(self.data_dir / "game_rules.json", rules)
        self.write_json(
            self.game_state_path,
            {"turn": 1, "units": [], "game_speed": "STANDARD"},
        )
        original_map = (self.data_dir / "map.json").read_bytes()
        original_cities = (self.data_dir / "cities.json").read_bytes()
        original_game_state = self.game_state_path.read_bytes()

        result = main.end_turn(city["id"])
        saved = main.read(self.data_dir / "cities.json", {})
        updated = saved[city["id"]]
        self.assertEqual(result["turn"], 2)
        self.assertEqual(updated["population"], 2)
        self.assertEqual(updated["stored_food"], 0.0)
        self.assertEqual(updated["food_required"], 24.0)
        self.assertEqual(updated["food_yield"], 2.0)
        self.assertEqual(updated["food_consumption"], 4.0)
        self.assertEqual(updated["food_surplus"], -2.0)
        self.assertEqual(result["cities"][0]["id"], city["id"])
        self.assertEqual(result["selected_city_food"]["population"], 2)
        self.assertEqual(result["selected_city_yields"]["totals"]["Food"], 2.0)
        self.assertEqual(result["selected_city_production"]["turn"], 2)
        self.assertEqual(result["selected_city_production"]["city_id"], city["id"])
        self.assertEqual(result["map_changes"], [])
        unselected_result = main.end_turn()
        self.assertNotIn("selected_city_yields", unselected_result)
        self.assertEqual((self.data_dir / "map.json").read_bytes(), original_map)
        self.assertEqual((self.data_dir / "cities.json").read_bytes(), original_cities)
        self.assertEqual(self.game_state_path.read_bytes(), original_game_state)

    def test_snapshot_preserves_food_state_and_game_speed(self):
        city = self.city(stored_food=7.25)
        city["x"] = 0
        city["y"] = 0
        map_data = {
            "width": 1,
            "height": 1,
            "count": 1,
            "tiles": [
                {
                    "x": 0,
                    "y": 0,
                    "terrain": "TERRAIN_GRASS",
                    "owner_city_id": city["id"],
                    "worked": True,
                }
            ],
        }
        city["food_yield"] = 2.0
        self.write_json(self.data_dir / "map.json", map_data)
        self.write_json(self.data_dir / "cities.json", {city["id"]: city})
        self.write_json(self.data_dir / "catalog.json", {})
        self.write_json(self.data_dir / "game_rules.json", self.rules)
        self.write_json(
            self.game_state_path,
            {"turn": 1, "units": [], "game_speed": "QUICK"},
        )

        main.save_map_as(main.SaveMapRequest(name="FoodState"))
        loaded = main.load_saved_map(main.SaveMapRequest(name="FoodState"))

        self.assertEqual(loaded["game_state"]["game_speed"], "QUICK")
        self.assertEqual(loaded["cities"][0]["stored_food"], 7.25)
        self.assertIsNone(loaded["cities"][0]["food_required"])


if __name__ == "__main__":
    unittest.main()
