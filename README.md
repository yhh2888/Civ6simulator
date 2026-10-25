# Civ VI City Studio — Map Editor Prototype v0.3.0

## What's included
- Uses the map CSV from the uploaded `civ6reader.zip`.
- Loads a real map grid of 84 x 54 (4,536 tiles).
- Canvas map with click selection, drag-to-pan, wheel zoom, zoom buttons and coordinate labels.
- Hover a tile to see its own yield, including whether it is currently worked or a preview.
- Visible `MAP EDITOR v0.3.0` badge and `/health` version check to catch old-server confusion.
- No-cache response headers for the main HTML.
- City center markers display the actual population.
- Cities must be at least 3 hexes apart; a new city starts owning its center and all existing adjacent tiles.
- Owned tiles are highlighted on the map and can be claimed or released manually from the selected city's panel. Claims must connect to that city's existing territory; city centers cannot be unclaimed.
- Culture-based automatic border growth is not simulated.
- Fit-to-map scale is computed from the real canvas dimensions.
- Pointy-top hex rendering uses the unchanged odd-row map coordinates; click hit-testing, fit, drag and cursor-centered zoom use the same geometry.
- Create a city on the selected tile; new cities start at population 1.
- Edit city name, population, housing, amenities, civilization, leader, governor ID, governor promotions, buildings/wonders, policies and notes.
- Edit selected tile terrain, feature, resource, improvement, district, worked status, river flag and hill override metadata.
- Persist city/map edits in local JSON files.
- Save the current map, create a non-overwriting named snapshot in `app/data/saved_maps`, and reload a validated snapshot.
- Delete cities from the selected city panel or city list; owned tiles and citizen work state are cleared.
- Calculates a rule-limited city yield total and itemized tile/district/building breakdown from the imported catalog.
- Loads frequently tuned gameplay settings and declared modifiers from `app/data/game_rules.json`.
- Queue XML-cost production for Builders, Settlers, buildings, districts and wonders; end turns to apply city Production.
- Move produced units, let Builders construct catalog-valid improvements/remove eligible features, and found cities with Settlers.

## Run (Windows PowerShell)
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```
Open **http://127.0.0.1:8010**. API docs: http://127.0.0.1:8010/docs.

**Recommended:** double-click `run_windows.bat`; it always changes into this project folder and uses port `8010` so it does not silently reuse an older server on port `8000`. The header must show `MAP EDITOR v0.3.0`. You can verify the server at http://127.0.0.1:8010/health; it must return version `0.3.0`. If the version differs, you are talking to another project/server.

If venv activation is blocked:
```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

## Yield calculation coverage

`POST /api/calculate/{city_id}` uses extracted XML yield records where present:

- Base terrain and hill yields, features, resource yield changes and improvements.
- The imported map CSV has no separate hill column: hill status is encoded in terrain IDs ending in `_HILLS`. The app map retains those terrain IDs, displays a ridge marker, and exposes the effective hill state in tile yield details. `IsRiver` in that CSV is a river flag, not a hill flag.
- City-center minimum of 2 Food and 1 Production.
- Configured flat population yields: 0.5 Science and 0.2 Culture per citizen, added to city totals and empire research.
- Only owned tiles are included; the center is always worked, with up to one additional worked tile per population citizen (total capacity: population + 1 tiles).
- XML district citizen yields and a supported subset of district adjacency rules.
- Direct building and wonder yield changes, with duplicate building IDs counted once.
- Six outputs: Food, Production, Gold, Science, Culture and Faith, with tile-level components, totals and rule coverage returned.

The result is an approximation, not an in-game simulation. The population Science/Culture values are configurable flat yields requested for this project; the similarly named percentage parameters found in the extracted game data are not implemented as percentage modifiers. Food consumption, city growth and starvation are not simulated. Hills use extracted XML yields for the matching terrain ID when available; a manual hill override adjusts direct Production by one when the XML terrain variant is unavailable or overridden. River adjacency is reported as context but does not add yields. Dynamic resource/improvement conditions, unsupported adjacency modifiers, policies, governors, civilization/leader abilities, technology/civic boosts, era/game-speed modifiers, religion, trade routes, all DLC data, and complete localization are not modeled.

## Editing gameplay rules

Edit `app/data/game_rules.json`; `app/config.py` only loads and validates this file. `GET /api/game-rules` returns the active validated settings. The server reads the JSON on each rules-dependent request, so save the file and reload the browser; a server restart is not normally required. A missing, malformed, or unsupported rules file returns an explicit HTTP 500 error instead of silently using defaults.

The JSON groups rules by purpose:

- `city`: minimum center-to-center distance in hexes, initial population, initial territory radius in hexes, maximum population, additional worked tiles per population citizen, center minimum yields, and `population_yields` (flat yield points per citizen, currently `Science: 0.5` and `Culture: 0.2`). The center is always one worked tile; total worker capacity is `1 + population * worked_tiles_per_population`.
- `production`: initial progress in production points, per-category `cost_overrides`, and overflow policy. XML `Cost` is used unless an explicit JSON override exists. `same_turn_queue` carries remaining production into later queue items; `stop_at_item` discards the remainder after completing the current item.
- `tile_yields`: fallback terrain yields used only when the XML catalog lacks that terrain, plus the production delta for a hill fallback/manual hill toggle.
- `districts`: `default_max_per_city` is the fallback cap when a per-district override is absent; `overrides` configures entity-specific placement/prerequisite rules.
- `buildings`: `overrides` configures entity-specific prerequisite rules.
- `units`: `default_build_charges` and `default_base_moves` apply only when the XML row has no corresponding value; per-unit `overrides` can set prerequisites, population restrictions/cost, and unit values.
- `improvements`: `default_build_charges` applies when no per-improvement override is present; `feature_removal_charge_cost` sets the charge cost for XML-eligible feature removal. Per-improvement `overrides` configure placement prerequisites and action charge cost.
- `tile_yield_modifiers`: additive rules with unique `id`, optional `description`/`enabled`, all-required `when` conditions, and `yield_changes`. Values are points per tile. For example:

```json
{
  "id": "worked-hills-production",
  "description": "Additional Production on worked hills",
  "enabled": true,
  "when": {"is_hills": true, "worked": true},
  "yield_changes": {"Production": 1}
}
```

Supported conditions are `terrain`, `terrain_contains`, `feature`, `resource`, `improvement`, `district`, `worked`, `city_center`, `is_hills`, and `river_adjacent`. Exact-value conditions also accept a list, meaning any listed value matches. Additive rules apply once per tile after XML terrain/feature/resource/improvement/district-citizen yields and before the city-center minimum floor. Non-worked tiles can display the modifier result but do not contribute to city totals. The response records each modifier's `before_yields`, `changes`, and `after_yields`; these details are included in the city breakdown and tile hover tooltip.
- `districts.overrides`: optional per-district `allowed_terrains`, technology/civic prerequisite overrides, and `max_per_city`.
- `buildings.overrides`: optional per-building technology/civic/district prerequisite overrides and additional `required_buildings`.
- `units.overrides`: optional technology/civic prerequisite overrides, `min_population`, `build_charges`, `base_moves`, and `population_cost`.
- `improvements.overrides`: optional `allowed_terrains`, `allowed_features`, `allowed_resources`, technology/civic prerequisites, and builder charge cost (`build_charges`).

Catalog precedence: extracted XML remains the source for costs, prerequisites, placement conditions, and unit values unless a matching JSON override is explicitly present. JSON values replace the matching XML field; they are not copied into the rules file by default. For improvements, an absent condition list falls back to XML, while a present list replaces that condition category. Costs are overridden under `production.cost_overrides`, grouped by production category and game entity ID.

The loader validates the schema, supported fields, types, unique modifier IDs, and yield keys. Error messages include the modifier/entity ID when available. Adding a new condition kind still requires code support in `app/config.py` and the simulation; JSON only combines existing conditions. This configuration layer is not a general Civilization VI Modifier/RequirementSet interpreter: modifier arguments, complex requirement sets, game-speed modifiers, policies, governors, civilization/leader abilities, and turn-triggered effects remain unsupported unless separately implemented.

`GET /api/tiles/{x}/{y}/yields` returns the hovered tile's own terrain, feature, resource, improvement and worked-district yields. Owned tiles use the owning city's current work state; other tiles show a worked-tile preview without city-center minimums. City-wide, building and district-adjacency bonuses are excluded from this tile-only view.

## Important limitations
- This is an editor prototype, not a complete Civilization VI rules simulator.
- Map is loaded from the supplied CSV; map data may be from a specific save/game state and is not a procedural map generator.
- The map editor's tile terrain options are based on values present on the imported map. Feature/resource/improvement options are from the extracted catalog.
- The yield API uses hex distance for selected district adjacency conditions. Territory ownership is stored separately: new cities claim their radius-1 area, and later expansion is manual rather than culture-driven.
- The uploaded XML archive does not guarantee full DLC or Korean localization coverage. Labels are derived from identifiers, and localization keys remain in the source catalog.
- City and map data are stored in `app/data/cities.json` and `app/data/map.json`; back them up before replacing files.
- Named map snapshots are stored in `app/data/saved_maps/`. Saving with an existing name is rejected; loading a snapshot replaces the current map and city JSON files after validation.
- Production state is stored with each city in `app/data/cities.json`; turn number and unit instances are stored in `app/data/game_state.json`. Snapshots include both.
- State-changing endpoints are serialized within the single server process. The default local Uvicorn setup is supported; multi-worker deployment needs a shared database or cross-process lock.
- Production costs use extracted base XML `Cost` unless `production.cost_overrides` explicitly overrides them. Cost progression models, game speed, technologies/policies/civilization cost modifiers and production discounts are not simulated.
- Technology and civic research are empire-wide: each turn the active technology receives the sum of all cities' Science, and the active civic receives the sum of all cities' Culture. Costs and prerequisite edges come from the extracted XML catalog. Research progress is saved in `app/data/game_state.json`; existing per-city technology/civic selections are treated as already unlocked for backwards compatibility. Only one item per track can be active; excess yields after completing it are discarded, and boosts, eurekas, inspirations, policy effects and game-speed cost changes are not modeled.
- Turn overflow follows `production.overflow_policy`; cities are processed from their saved JSON order. This is a defined editor policy, not a claim of full game simulation.
- Unit support is limited to Builders and Settlers. Movement is one adjacent hex per movement point, without terrain costs, embarkation, combat or pathfinding; effective XML or JSON `BaseMoves` is restored at turn end.
- Some structural rules remain fixed: a city center always counts as one worked tile, city population cannot fall below one, and movement is limited to one adjacent hex per movement point. Unit movement range, fallback unit action values, district default cap, and Builder charge costs are configurable as described above.
- Builder improvement legality uses available XML build-unit, terrain, feature, resource and technology conditions, with optional overrides in `game_rules.json`. Missing condition tables cannot be inferred. Eligible feature removal uses extracted removal data and the configured charge cost.
- Completed districts and buildings affect yields; queued/incomplete work does not. District placement checks ownership, terrain rows when present, and occupied/reserved tiles. Building checks include extracted technology/civic, district and building prerequisites. Some district population caps and placement/prerequisite rules are not available or simulated.
- Wonders are separately recorded, require a valid owned placement tile and extracted terrain/feature constraints when present, and honor XML world/player instance limits where present. Full wonder-specific placement and game-state restrictions are not modeled.
- Settler founding checks XML `FoundCity`, map bounds, water terrain when the XML row is available, city spacing, city-center occupancy and radius-1 tile ownership. It consumes the Settler and initializes a population-1 city. City placement's full terrain, strategic-resource, loyalty and diplomatic rules are not modeled.
- Run focused backend regression tests with `.\.venv\Scripts\python.exe -m unittest discover -s tests`.

## Relevant API

- `POST /api/map/save` — persist the current map and city files.
- `POST /api/maps/save-as` with `{"name":"My Map"}` — create a validated `.json` snapshot; existing names are not overwritten.
- `GET /api/maps/saved` — list snapshots.
- `POST /api/maps/load` with `{"name":"My Map"}` — validate and load a snapshot into the active map and city files.
- `DELETE /api/cities/{city_id}` — delete a city and clear its tile owners/work states.
- `POST /api/calculate/{city_id}` — return calculated totals and calculation breakdown.
- `GET /api/game-rules` — return the validated gameplay rules currently used by simulation.
- `GET /api/tiles/{x}/{y}/yields` — return the selected tile's own yields for the hover tooltip; unknown coordinates return `404`.
- `GET /api/game` — return the current turn and saved unit instances.
- `GET /api/research` — return empire-wide technology/civic research choices, prerequisites, progress, completed unlocks, and summed Science/Culture per turn.
- `PUT /api/research/{category}` — select `technology` or `civic` research; body: `{"target_id":"TECH_MINING"}` or a civic ID. XML prerequisites must already be completed.
- `GET /api/production/catalog?city_id={city_id}` — list XML-derived production options, costs and city-specific availability reasons.
- `GET /api/cities/{city_id}/production` — return production per turn, queue progress, remaining costs, estimated turns and cost caveats.
- `POST /api/cities/{city_id}/production/queue` — append a production target. Body: `{"category":"unit","target_id":"UNIT_BUILDER"}`. Districts and wonders also require placement coordinates, e.g. `{"category":"district","target_id":"DISTRICT_CAMPUS","x":12,"y":8}`.
- `PUT /api/cities/{city_id}/production/order` — move a queue item; body: `{"queue_id":"prod-…","position":0}`. Position zero becomes the active production.
- `DELETE /api/cities/{city_id}/production/queue/{queue_id}` — cancel an item and discard its accumulated progress.
- `POST /api/turn/end` — apply each city's current calculated Production, complete eligible items, carry overflow forward through that city's queue, advance the turn and refresh unit movement.
- `POST /api/units/{unit_id}/move` — move one adjacent hex; body: `{"x":12,"y":8}`.
- `POST /api/units/{unit_id}/build` — construct a valid improvement; body: `{"improvement_id":"IMPROVEMENT_FARM"}`.
- `POST /api/units/{unit_id}/remove-feature` — remove the Builder's current XML-removable feature.
- `POST /api/units/{unit_id}/found-city` — found a city at the Settler's position; body: `{"name":"New City"}`.

Production queue requests with missing costs, unsupported targets, invalid prerequisites or invalid placements return a 4xx response instead of substituting a guessed cost. Unit, city, map and turn changes are written together using staged JSON replacement with rollback on write failure. The browser exposes these APIs in the city production and unit action panels; manual building/wonder editing remains available as a separate editor capability.
- Original game data belongs to its rights holders. Avoid redistributing proprietary data publicly without checking applicable terms.
