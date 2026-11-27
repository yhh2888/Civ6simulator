# Civ VI City Studio

문명 6 도시 하나하나를 턴 단위로 계산해 보는 시뮬레이터입니다. 세이브 파일을 열면 지도, 도시, 시민 배치, 생산 대기열, 기술과 정책, 도시국가 사절, 교역로까지 그대로 가져오고, 다음 몇 턴 동안 도시가 어떻게 자라는지 계산합니다. 숫자가 왜 그렇게 나왔는지는 도시 장부에 한 줄씩 남습니다. 어느 XML 행에서 왔는지, 어떤 정책이 몇 퍼센트를 더했는지까지요.

알렉산드리아(90턴, 몰려드는 폭풍) 기준으로 게임 툴팁의 생산력 27을 항목별로 맞춰 놓았습니다. 타일 12, 산업구역 6, 건물 5, 수출 교역로 3, 도시국가 보너스 1.

현재 버전: **0.5.1**

> 비공식 팬 도구입니다. Firaxis Games, 2K와 관계가 없습니다. 게임 데이터는 포함하지 않으며, 처음 실행할 때 내 PC에 설치된 게임에서 읽어 옵니다.

## 시작하기

### 한국어

1. 압축을 풀고 `run_windows.bat`을 더블클릭합니다. 처음에는 가상환경을 만들고 패키지를 설치하느라 1~2분 걸리고, 그다음부터는 3~4초 안에 브라우저가 열립니다.
2. 처음 열면 지도 대신 **게임 데이터 준비** 화면이 나옵니다. 둘 중 하나를 고르세요.
   - **게임 폴더를 바로 지정:** 경로 칸에 문명 6 설치 폴더를 넣고 [추출]을 누릅니다. 예: `C:\Program Files (x86)\Steam\steamapps\common\Sid Meier's Civilization VI`. Steam 기본 위치에 있으면 화면에 미리 나타납니다.
   - **`collect_game_data.bat` 사용:** 게임 폴더에서 `Base\Assets\Gameplay\Data`와 `DLC`의 규칙 파일(XML, SQL, modinfo)만 골라 프로젝트의 `game_data` 폴더로 복사합니다. 그림과 소리는 복사하지 않습니다. 끝나면 경로 칸에 `game_data` 폴더를 넣고 [추출]을 누릅니다. 게임 폴더를 다른 PC로 옮겨 쓰거나, 설치 폴더를 직접 건드리고 싶지 않을 때 편합니다.
3. 추출은 몇 초에서 1분 정도 걸립니다. 기본 게임 규칙(`catalog.json`, `rules_db.json`)과, DLC 폴더가 있으면 확장팩·DLC 규칙(`rules_db_xp2.json`)이 `app\data`에 만들어지고 화면이 다시 열립니다.
4. 오른쪽 위 [세이브 가져오기]로 `.Civ6Save` 파일을 엽니다. 세이브는 보통 `문서\My Games\Sid Meier's Civilization VI\Saves`에 있습니다.

### English

1. Unzip and double-click `run_windows.bat`. The first run creates a virtual environment and installs packages (a minute or two); after that the browser opens within 3 to 4 seconds.
2. The first page is **Game data setup**, not the map. Pick one:
   - **Point at the game folder:** enter your Civilization VI install folder and press Extract, e.g. `C:\Program Files (x86)\Steam\steamapps\common\Sid Meier's Civilization VI`. Default Steam locations are offered automatically.
   - **Use `collect_game_data.bat`:** it copies only the rule files (XML, SQL, modinfo) of `Base\Assets\Gameplay\Data` and `DLC` into the project's `game_data` folder, no art or audio. Then enter `game_data` and press Extract.
3. Extraction takes a few seconds to about a minute. It writes the base rules (`catalog.json`, `rules_db.json`) and, when a DLC folder is present, the expansion and DLC rules (`rules_db_xp2.json`) into `app\data`, then reloads the page.
4. Open a `.Civ6Save` with Import save (top right). Saves are usually in `Documents\My Games\Sid Meier's Civilization VI\Saves`.

Command line, if you prefer:

```powershell
python tools/build_catalog.py  "<Civ VI>\Base\Assets\Gameplay\Data"
python tools/build_rules_db.py "<Civ VI>\Base\Assets\Gameplay\Data"
python tools/build_ruleset_db.py --base "<Civ VI>\Base\Assets\Gameplay\Data" --dlc "<Civ VI>\DLC"
```

## 무엇을 하나

### 한국어

- **세이브 가져오기.** 지형, 강, 도시와 특수지구, 시민 배치와 잠금, 현재 생산과 진행도, 연구한 기술과 사회제도, 정부와 정책, 판테온, 도시국가 사절과 종주국, 상인의 교역로를 읽습니다. 같은 세이브의 다른 문명으로 바꿔서 볼 수도 있습니다.
- **산출 계산.** 지형부터 인접 보너스, 건물, 정책과 특성, 도시국가 보너스, 교역로, 걸작까지 게임 데이터의 Modifier와 Requirement를 그대로 해석합니다.
- **도시 장부.** 모든 줄에 계산식과 출처가 붙고, 장부 합계와 도시 화면 숫자가 맞는지 매번 검산합니다. 교역로는 교역로마다 따로 장부가 있습니다.
- **턴 시뮬레이션.** 성장, 주거공간, 쾌적도, 생산 대기열과 초과분, 연구를 정해진 순서로 처리합니다. 시나리오를 복사해 서로 비교할 수 있습니다.
- **지도.** 타일마다 지형, 시설, 산출을 바로 보여 주고, 특수지구에는 인접 보너스와 지어진 건물이 함께 나옵니다.

아직 못 하는 것도 있습니다. 총독, 위인 점수, 강 범람으로 늘어난 타일 산출, 걸작이 걸린 위치, 충성도는 세이브에서 읽지 못하거나 계산하지 않습니다. 화면의 "계산 근거·미지원 규칙" 탭에 빠진 규칙이 전부 기록됩니다.

### English

- **Import Save.** Reads terrain, rivers, cities and districts, citizen assignments and locks, current production and progress, researched technologies and civics, government and policies, pantheon, city-state envoys and suzerainty, and trader trade routes. You can also switch to another civilization in the same save to view its data.
- **Yield Calculation.** Interprets the game's Modifiers and Requirements directly from game data, including terrain, adjacency bonuses, buildings, policies, traits, city-state bonuses, trade routes, and Great Works.
- **City Ledger.** Every line includes a calculation formula and its source, and the ledger totals are cross-checked against the yields shown in the city UI every time. Trade routes each have their own separate ledger.
- **Turn Simulation.** Processes growth, housing, amenities, production queues and overflow, and research in a fixed order. Scenarios can be duplicated and compared against one another.
- **Map.** Displays terrain, improvements, and yields directly on each tile. Districts show both adjacency bonuses and constructed buildings.

There are still some limitations. Governors, Great Person points, tile yields increased by river flooding, Great Work locations, and loyalty are either not read from the save or not calculated. All unsupported rules are documented in the "Calculation Details & Unsupported Rules" tab.

## 버전 기록 / Version history

### 0.1 ~ 0.4

0.3까지는 지도 위에 도시를 놓고 시민을 손으로 배치하는 편집기였습니다. 산출은 지형과 건물의 기본값 정도만 더했고, 정책이나 정부는 저장만 할 뿐 계산에 들어가지 않았습니다.

0.4에서 계산 엔진을 새로 만들었습니다. 게임의 Modifier, Requirement, 성장 공식, 생산 대기열을 해석하고, 시민 배치 최적화와 시나리오 비교, 검산되는 도시 장부가 들어갔습니다. 0.4 후반에는 세이브 파일 해독에 집중했습니다. 지도와 도시에서 시작해 시민 배치, 생산, 기술과 정책, 사절, 판테온, 교역로 순으로 읽을 수 있는 범위를 넓혔고, 확장팩과 DLC 규칙을 게임이 불러오는 순서 그대로 합치는 빌더를 만들었습니다. 이 시기 번호는 작업 한 번마다 하나씩 올렸습니다.

### 0.5

0.5부터 버전을 정리해서 붙입니다. 기능 묶음이 바뀌면 둘째 자리, 보정과 수정은 셋째 자리를 올립니다.

그리고 게임 데이터를 다시 배포하던 문제를 고쳤습니다. 0.4까지는 게임 XML에서 만든 규칙 파일(`catalog.json`, `rules_db.json`, `rules_db_xp2.json`)을 저장소와 배포 파일에 그대로 넣었습니다. 0.5부터는 이 파일들을 빼고, 사용자의 설치본에서 처음 실행할 때 만듭니다.

**0.5.1**

- 첫 실행 화면과 `collect_game_data.bat`. 게임 폴더나 복사한 폴더를 지정하고 추출을 누르면 이전 배포본과 같은 데이터가 만들어집니다(내용이 같은지 표 단위로 대조했습니다).
- `tools/build_catalog.py` 추가. 예전 배포본의 `catalog.json`을 똑같이 다시 만드는 생성기입니다.
- `run_windows.bat`이 패키지 설치를 처음 한 번만 하고, 서버가 뜨고 3초 뒤 브라우저를 엽니다.
- 도시국가 보너스 중 "건물·특수지구를 지을 때 +N" 같은 것을 도시 생산력에 넣어 게임 툴팁과 같은 숫자가 나오게 했습니다.
- 교역로 기록을 그 기록이 들어 있는 플레이어 기준으로 나눠, 도시국가 상인의 기록이 내 교역로로 들어오던 문제를 고쳤습니다.
- 인구당 문화를 정확히 0.3으로 계산하고, 화면의 산출은 게임처럼 소수 첫째 자리에서 버립니다.
- 걸작을 도시에 넣으면 산출에 더합니다.
- 지도 "타일 상세" 모드, 불러오기 진행 막대, 다른 문명 보기.

### English

**0.1 to 0.4.** Up to 0.3 this was an editor: you placed cities on a map and assigned citizens by hand. Yields came from base terrain and building values only; policies and governments were stored but never counted.

0.4 brought a new calculation engine. It reads the game's Modifiers and Requirements, growth formulas and production queues, and adds citizen optimisation, scenario comparison and a city ledger that checks itself against the totals. The later 0.4 releases went into save files: first the map and cities, then citizens, production, technologies and policies, envoys, the pantheon and trade routes. A builder now merges expansion and DLC rules in the same order the game loads them. Version numbers in this period went up with every piece of work.

**0.5.** Versions now follow one rule: a new feature set raises the second number, fixes raise the third.

0.5 also stops redistributing game data. Until 0.4 the rule files generated from the game's XML (`catalog.json`, `rules_db.json`, `rules_db_xp2.json`) were committed and shipped. From 0.5 they are left out and built on first run from your own installation.

**0.5.1**

- First-run setup screen and `collect_game_data.bat`. Point it at the game folder or at the copied folder, press Extract, and you get the same data the old packages carried (checked table by table).
- `tools/build_catalog.py`, which rebuilds the former `catalog.json` exactly.
- `run_windows.bat` installs packages only once and opens the browser three seconds after the server starts.
- City-state bonuses such as "+N while building buildings or districts" now count towards city production, so the number matches the in-game tooltip.
- Trade route records are assigned to the player whose data block holds them; a city-state trader's record no longer shows up as your route.
- Culture per citizen is exactly 0.3, and yields on screen are cut down to one decimal the way the game shows them.
- Great works placed in a city add their yields.
- Tile detail map mode, a loading bar and a switch to view other civilizations.

## 0.6까지의 방향 / Road to 0.6

아래는 지금 보이는 빈틈을 기준으로 한 예상입니다. 세이브 형식을 얼마나 더 풀 수 있느냐에 따라 순서와 범위는 바뀔 수 있습니다.

- **세이브에서 더 읽기 (0.5.x).** 강 범람으로 늘어난 타일 산출, 걸작이 걸린 건물, 지금 연구 중인 기술과 사회제도, 창시한 종교의 신앙. 범람 보너스는 알렉산드리아 식량이 게임보다 3 적게 나오는 원인으로 보고 있습니다. 범람 전후 세이브가 있으면 찾기 쉬워집니다.
- **총독과 위인 (0.5.x ~ 0.6).** 총독 배치와 진급, 위인 점수와 후보 비용. 위인 점수가 들어가야 초과분이 신앙으로 바뀌는 규칙과 볼로냐 같은 종주국 보너스를 계산할 수 있습니다.
- **쾌적도와 충성도.** 교역으로 받은 사치품, 주둔 유닛, 종교에서 오는 쾌적도를 읽어 지금 꺼 둔 쾌적도 효과를 기본으로 켜는 것이 목표입니다. 충성도는 압력 계산까지 가야 해서 0.6 이후가 될 수 있습니다.
- **여러 문명 동시 보기 (0.6).** 지금은 한 번에 한 문명만 계산합니다. 0.6에서는 세이브 안의 모든 문명을 한꺼번에 계산해 비교하는 화면을 생각하고 있습니다.
- **게임 속도.** 성장 비용은 표준 속도만 검증되어 있습니다. 온라인, 빠름, 서사시, 마라톤 속도의 성장식을 실제 세이브로 맞춥니다.
- **문서.** 기술 문서를 `docs/`로 옮기고, 게임 화면과 대조한 사례를 모아 둡니다.

### English

These are expectations based on the gaps we can see today. Order and scope depend on how much more of the save format can be read.

- **Reading more from saves (0.5.x).** Tile yields gained from river floods, which building holds each great work, the technology and civic currently being researched, and the beliefs of a founded religion. Floods are the likely reason Alexandria shows 3 less food than the game; a save from before and after a flood would make them easy to find.
- **Governors and great people (0.5.x to 0.6).** Governor placement and promotions, great person points and candidate costs. Great person points are needed for the rule that turns the overflow into faith, and for suzerain bonuses such as Bologna's.
- **Amenities and loyalty.** Read amenities from traded luxuries, garrisons and religion so the amenity effect, switched off for imported games today, can be on by default. Loyalty needs pressure calculations and may land after 0.6.
- **All civilizations at once (0.6).** Today one civilization is calculated at a time. 0.6 is meant to calculate every civilization in a save together and compare them side by side.
- **Game speeds.** Growth costs are verified for Standard speed only. Online, Quick, Epic and Marathon growth will be matched against real saves.
- **Documentation.** Move the technical docs to `docs/` and collect cases checked against the game's own screens.

