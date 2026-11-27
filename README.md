# Civ VI City Studio

문명 6 도시 하나하나를 턴 단위로 계산해 보는 시뮬레이터입니다. 세이브 파일을 열면 지도, 도시, 시민 배치, 생산 대기열, 기술과 정책, 도시국가 사절, 교역로까지 그대로 가져오고, 다음 몇 턴 동안 도시가 어떻게 자라는지 계산합니다. 숫자가 왜 그렇게 나왔는지는 도시 장부에 한 줄씩 남습니다. 어느 XML 행에서 왔는지, 어떤 정책이 몇 퍼센트를 더했는지까지요.

알렉산드리아(90턴, 몰려드는 폭풍) 기준으로 게임 툴팁의 생산력 27을 항목별로 맞춰 놓았습니다. 타일 12, 산업구역 6, 건물 5, 수출 교역로 3, 도시국가 보너스 1.

현재 버전: **0.5.2**

0.5.1과 비교해 세이브에서 읽는 범위가 넓어졌고, 난이도와 설정 파일이 생겼으며, 게임 화면과 숫자를 맞추는 보정이 크게 늘었습니다. 확인 기준은 직접 플레이한 90턴 마케도니아 세이브와 관전 세이브 31턴(AI 6개 문명, 황제)의 도시 툴팁입니다.

- 세이브에서 새로 읽는 것
  - 자연재해(범람, 화산 토양, 폭풍·눈보라·모래폭풍)로 늘어난 타일 산출. 장부에 "자연재해로 늘어난 산출"로 나옵니다(알렉산드리아 식량 35, 펠라 식량 39).
  - 시설 약탈과 건물 약탈(수리 필요). 약탈된 시설은 시설 산출과 기술·사회제도 보너스가, 약탈된 건물은 산출과 효과가 빠지고, 지도에 "(약탈)"로 표시됩니다(메디오라눔 트러플 금 3, 물레방앗간).
  - 도시 건물에 걸린 걸작(펠라 궁전 +4, 모헨조다로 원형극장 +2 두 개).
  - 총독의 위치·진급·정착 턴. AI 총독까지 읽고, 정착이 끝난 총독의 진급 효과를 적용합니다(인구 비례 효과, 특수지구 생산 % 포함).
  - 다른 문명·도시국가에서 들어오는 교역로, 세계 의회 결의(무역 조약 "들어오는 교역로마다 금 +4" 등).
  - 지도자가 비어 있는 슬롯은 문명에 지도자가 하나뿐이면 그 지도자로 채웁니다.
- 난이도
  - "난이도 설정…" 창에서 문명마다 난이도(개척자~신)와 AI/사람을 고릅니다. AI는 게임 데이터(HIGH_DIFFICULTY_*_SCALING)대로 보너스를 받고 장부에 "난이도 보너스"로 나옵니다. 세이브 주인은 사람, 관전 세이브는 모두 AI로 채웁니다.
  - 보너스 표를 칸마다 고칠 수 있고 `app/config/difficulty.json`에 저장됩니다(게임 데이터와 다른 칸은 노란색, 되돌리기 버튼).
  - AI 추가 %(과학·생산력·금 +6)를 `ai_extra_percent`로 둡니다. 게임 데이터에는 없지만 관전 세이브의 황제 AI 도시들과 맞는 값입니다.
- 계산 보정
  - % 보정을 게임처럼 하나씩 1/256 단위로 반올림해 더합니다(24% → 61/256). 인구당 과학·문화도 같은 방식입니다(문화 30% → 77/256, 0.5.1의 "정확히 0.3"을 바꿈). `simulation.fixed_point_percent`로 끌 수 있습니다.
  - 가져온 도시에 쾌적도 효과(불만 −10%, 행복 +10% 등)를 적용합니다(`app/config/save_import.json`의 `apply_amenity_effects`). 베이징·콘스탄티노플의 불만 상태가 게임과 맞습니다. 다만 사치품 교환 같은 쾌적도 출처를 다 읽지 못해 일부 도시(코바 등)는 상태가 틀릴 수 있습니다.
  - 걸작 종류별 산출 효과(콩고 은키시: 유물·유적·조각상마다 식량 +2, 생산력 +2, 금 +4, 신앙 +1).
  - 시설 인접 규칙, 특수지구 인접 보너스를 다른 산출로 옮기는 효과(테오도라: 성지 인접 농장 +1 신앙, 성지 신앙 인접만큼 문화), 도심에 붙는 인접 규칙(말리: 인접 사막·사막 언덕마다 식량·신앙 +1), 수도까지 거리 조건(육천: 수도 6타일 이내 모든 산출 +10%, 7타일 이상 −15%).
  - 종주국 판정에서 도시국가 자신의 기록을 빼고(오클랜드), 시대 조건(REQUIREMENT_PLAYER_ERA_AT_LEAST)을 계산합니다(산업 시대 해안 타일 생산력 +2).
- 설정 파일
  - `app/config/`에 사용자가 고칠 설정을 모았습니다. `game_rules.json`(app/data에서 이동, data 폴더에 사본이 있으면 그쪽 우선), `difficulty.json`, `save_import.json`(쾌적도 효과, 관전 세이브 전원 AI). JSON을 직접 고쳐도 다음 요청부터 반영됩니다.
- 화면과 도구
  - "산출 CSV 내보내기": 세이브 여러 개를 골라, 도시국가를 뺀 모든 문명의 도시 산출(게임처럼 0.1 단위 내림, 보정 전 값, % 합계, 쾌적도 상태)을 CSV 하나로 받습니다. 턴별로 게임 값과 비교할 때 씁니다.
  - 하단 "자원별 장부" 탭: 한 가지 산출을 모든 도시에 걸쳐 항목별로 보고, 도시를 펼치면 계산식이 나옵니다.
  - 타일 수동 보정(장부에 "수동 보정"으로 표시), 지도 "타일 상세" 모드의 시설 이름 표시.
  - 데이터 추출이 Data.zip 옆의 DLC 폴더나 DLC zip도 찾습니다. 세이브를 불러오거나 문명을 바꾸면 정책·총독 화면을 새 규칙으로 다시 읽습니다(전에는 "총독 미지원"이 뜰 수 있었음).
- 알려진 차이(관전 세이브 31턴): 콘스탄티노플 식량(+3), 베이징 문화(+2), 나란호 금(국제 교역로 금이 게임에는 없음)은 아직 원인을 모릅니다.

**0.5.1**

- 첫 실행 화면과 `collect_game_data.bat`. 게임 폴더나 복사한 폴더를 지정하고 추출을 누르면 이전 배포본과 같은 데이터가 만들어집니다(내용이 같은지 표 단위로 대조했습니다).
- `tools/build_catalog.py` 추가. 예전 배포본의 `catalog.json`을 똑같이 다시 만드는 생성기입니다.
- `run_windows.bat`이 패키지 설치를 처음 한 번만 하고, 서버가 뜨고 3초 뒤 브라우저를 엽니다.
- 도시국가 보너스 중 "건물·특수지구를 지을 때 +N" 같은 것을 도시 생산력에 넣어 게임 툴팁과 같은 숫자가 나오게 했습니다.
- 교역로 기록을 그 기록이 들어 있는 플레이어 기준으로 나눠, 도시국가 상인의 기록이 내 교역로로 들어오던 문제를 고쳤습니다.
- 인구당 문화를 정확히 0.3으로 계산하고, 화면의 산출은 게임처럼 소수 첫째 자리에서 버립니다.
- 걸작을 도시에 넣으면 산출에 더합니다.
- 지도 "타일 상세" 모드, 불러오기 진행 막대, 다른 문명 보기.

## 상세 문서

## 1. 기존 프로젝트 분석 결과 (v0.3.0 기준)

| 질문 | 분석 결과 |
|---|---|
| 턴 종료 처리 순서 | `POST /api/turn/end`: 모든 도시 산출량 계산 → 도시 순서대로 식량·성장 → 생산(같은 턴에 대기열 여러 개 완성 가능) → 기술/사회제도 → 턴+1. 처리 순서는 cities.json 순서. |
| 산출량 계산 | catalog.json의 지형·특성·자원·시설·건물 산출 + 일부 지구 인접. 정책·정부·특성·Modifier·쾌적도·주거공간 없음. |
| 성장·식량 | 소비 2/인구, 성장식 15+8(n−1)+(n−1)^1.5, 표준 속도만. 주거공간·쾌적도 효과 없음. |
| 시민 배분 | 수동(`tile.worked`)만. 맵 순서로 앞 타일부터 인정, 자동 배분 없음. |
| 건물·지구·불가사의 | 직접 산출 + 단일 조건 인접 규칙. 전문가 슬롯 개념 없음. |
| 정책·정부·총독·특성 | 도시별 체크박스로 저장만 하고 계산에 반영 안 됨. |
| 데이터 정확성 | **catalog.json의 181개 테이블(Modifiers·ModifierArguments·Requirements·RequirementSets·GameSpeeds·…)이 빈 행으로 저장**되어 있었음. 추출기가 XML 자식 요소를 읽지 못함. GlobalParameters(`<Replace>` 행)는 통째로 누락. 산출량 테이블 자체 값은 XML과 일치. |
| 기존 계산의 오류 | ① 상업 중심지 강 보너스를 "강 인접 타일 수"만큼 반복 적용 ② 도심을 인접 특수지구로 세지 않음 ③ 산출 행이 없는 산(`TERRAIN_GRASS_MOUNTAIN`)에 초원 대체값(식량 2) 적용 ④ 지구 타일 작업 시 지형 산출까지 더함. |

**데이터 범위:** `rules_db.json`은 기본 게임 데이터(`Base/Assets/Gameplay/Data`)로 만듭니다. 확장팩·DLC 규칙은 `rules_db_xp2.json`에 따로 있고, 세이브가 몰려드는 폭풍 규칙이면 그쪽을 씁니다. 두 파일 모두 저장소에 넣지 않고 사용자의 설치본에서 만듭니다.

## 2. 구조

기존 FastAPI 구조(Controller → Service → Repository)를 유지하고, 엔진은 **상태 묶음 `{map, cities, game}`의 복사본에서만 계산**합니다.

```text
tools/build_catalog.py               게임 Data 폴더 → app/data/catalog.json
tools/build_rules_db.py              게임 Data 폴더 → app/data/rules_db.json + data_audit_report.json
tools/build_ruleset_db.py            Data + DLC → app/data/rules_db_xp2.json (확장팩·DLC 규칙)
app/services/game_data_setup_service.py  첫 실행 데이터 준비 (경로 찾기, 백그라운드 생성)
app/repositories/rules_db_repository.py   rules_db 읽기(캐시)
app/services/
  sim_context.py                     인덱스·헥스 이웃·플레이어/도시 뷰, 시뮬레이션 설정
  requirement_engine.py              RequirementSet 평가 (ALL/ANY, Inverse, 중첩, 미지원=None)
  modifier_engine.py                 출처→Modifier→DynamicModifiers→컬렉션/조건→효과, 감사 로그
  yield_breakdown_service.py         구성요소별 산출 계산 (타일·전문가·인구·지구·건물·Modifier·%)
  city_growth_service.py             식량·성장·주거공간·쾌적도 (GlobalParameters, Happinesses)
  city_optimization_service.py       결정론적 시민 배치 최적화 (프리셋 10종)
  citizen_assignment_service.py      수동 배치·자동 모드·새 시민·잠금/금지/우선순위
  production_engine.py               생산 가능 여부·비용(진행 모델·게임 속도)·% 보너스·대기열·초과분
  turn_simulation_service.py         12단계 턴 엔진, 다중 턴, 리포트, 기록
  empire_service.py                  정부·정책 슬롯·문명·지도자·판테온 검증, 지원 범위 분석
  scenario_service.py                시나리오 파일 저장소 (app/data/scenarios/)
  simulation_service.py              API 오케스트레이션 (현재 게임 / 시나리오 대상)
  simulation_validation_service.py   정확도 상태 레지스트리, 게임 대조 사례, 데이터 감사
app/controllers/simulation_controller.py
app/static/sim.js, sim.css           새 UI (기존 index.html 스크립트와 연동)
```

게임 데이터가 없으면 첫 실행 화면이 먼저 뜹니다. 명령줄로 다시 만들 때는 위 "시작하기"의 명령을 쓰세요.

## 2-1. 산출 데이터의 출처

- 지형·지형 특성·자원·시설·건물의 기본 산출은 `rules_db.json`(게임 XML을 직접 읽어 만든 것)의 `Terrain/Feature/Resource/Improvement/Building_YieldChanges`에서 읽습니다. `rules_db.json`이 없을 때만 `catalog.json`을 대신 씁니다. 두 값은 현재 완전히 같습니다(감사로 확인).
- 게임 데이터가 아닌 값은 장부에 출처가 따로 표시됩니다: 인구당 과학·문화(`game_rules.json`), 도심 최소 산출(`game_rules.json`, 게임 데이터 값과 동일), 사용자 정의 효과(사용자 입력).
- 화면 개요의 "기존 계산 결과" 접힘 영역만 기존 `/api/calculate`(catalog.json, 기존 규칙)를 사용합니다.

## 2-2. 도시 산출 장부

하단 **"도시 장부"** 탭과 `GET /api/sim/cities/{id}/ledger`(CSV: `/ledger.csv`)는 도시 산출을 계산 순서대로 한 줄씩 기록합니다.

- 줄마다: 더한 산출, 계산식(예: `인접 과학 2 × +100% = +2`), 출처(게임 데이터 파일·테이블·키와 원본 XML 행 / game_rules.json 경로 / 사용자 입력), 누계.
- Modifier 줄을 누르면: 출처 → 연결 테이블 행 → Modifier(부착 경로 포함) → 대상 범위·효과·인자 → 소유자/대상 조건을 하나씩 평가한 결과(✓/✕와 이유).
- 소계, % 보정(소계 × %), 합계. **검산**: 장부 줄의 합과 도시 화면 합계가 일치하는지 매번 확인해 표시합니다.
- 합계에 들어가지 않은 것: 작업하지 않는 타일, 발견 기술이 없는 자원, 선행 조건 미충족 시설 보너스·인접 규칙, 조건 미충족 또는 미지원 Modifier, 배치되지 않은 시민.
- 성장 장부(산출 → 소비 → 잉여 → 수정치 → 주거 배율 → 변화 → 필요량 → 남은 턴), 주거공간·쾌적도 구성, 생산 장부(비용 단계, 보너스, 투입 생산력).

## 2-3. Civ VI 세이브 파일 불러오기

상단 **[세이브 가져오기]** 로 실제 게임의 `.Civ6Save`(몰려드는 폭풍 세이브로 확인)를 열면, 지도·내 도시·특수지구·건물·시민 배치·현재 생산을 읽어 현재 게임을 교체합니다. 세이브 파일은 문서화되지 않은 형식이라, 실제 세이브 6개(그중 2개는 "시민 1명 이동·생산 변경"만 다른 비교용)로 **교차 확인된 부분만** 읽고 나머지는 "읽지 않음/추정"으로 표시합니다.

| 항목 | 상태 | 확인 방법 |
|---|---|---|
| 지형·지형 특성·자원·시설·도로 | 읽음 | 타입은 이름의 CRC-32(반전) 해시로 저장됨. 규칙 DB의 Types + `app/services/civ6save/type_names_extra.txt`로 이름 복원, 모르는 해시는 `UNKNOWN_xxxxxxxx`로 남김 |
| 강(6면)·절벽 | 읽음 | 강 모서리가 이웃 타일과 서로 일치(실제 세이브 4개에서 불일치 0) |
| 타일 소유(플레이어·도시) | 읽음 | 도심·특수지구 기록과 같은 도시 번호 |
| 도시 이름·위치·소유자 | 읽음 | 도심 특수지구 수 = 도시 수 |
| 특수지구 종류·위치 | 읽음 | 타일 소유 정보의 특수지구 번호와 일치하는 기록만 사용 |
| 특수지구 완성 여부·고정 비용 | 읽음 | 짓는 중인 특수지구는 완성 목록에서 빼고 생산 대기열에 진행도·고정 비용과 함께 넣음(지도에는 점선 표시) |
| 지어진 건물·불가사의 | 읽음 | 건물마다 저장된 타일 번호가 도심/해당 특수지구와 일치 |
| 시민 배치·잠금·전문가 | 읽음 | 도시마다 반경 3칸 37칸 배열 2개(칸별 시민 수 / 손으로 잠근 칸). 두 세이브 비교(시민 1명 이동 = 한 칸이 배열 1에서 빠지고 배열 2에 들어감)와, 서로 다른 세이브 5개의 일하는 칸 1,300여 개가 모두 그 도시 땅인지로 확인. 잠금은 `tile_preferences.lock`, 전문가는 `specialists` |
| 인구 | 읽음 | 두 배열의 시민 합(도심 제외). 시민 이동 전후 같은 값(13) |
| 현재 생산·진행도 | 읽음 | 생산 항목(유닛/건물·불가사의/특수지구/프로젝트)과 위치. 진행도는 256배 고정소수점. 생산 변경 비교와 게임 화면(캠퍼스 77/143)으로 확인 |
| 연구한 기술·사회제도, 유레카·영감, 연구 진행도 | 읽음 | 플레이어마다 (완료 / 부스트 / 진행도×256) 표 3개. 표가 그 플레이어의 첫 도시 뒤, 다음 플레이어 첫 도시 앞에 있는지로 주인 확인. 기술을 읽으면 선행 조건 검사를 켭니다(`enforce_prerequisites=true`) |
| 정부·장착 정책 | 읽음 | 정부 단계별 (키, 정부) 4쌍 + 정책 표 5개 + 슬롯 목록(정책, 슬롯 종류). 4쌍 중 **슬롯 구성(Government_SlotCounts)이 슬롯 목록과 같은 정부**를 현재 정부로 확정. 맞는 정부가 없거나 둘 이상이면 정부·정책을 비워 두고 경고. 예: 마케도니아 세이브 = 과두정·군주정 중 군주정(군사2·경제1·외교1·와일드2) |
| 턴 | **추정** | 플레이어별 진행 턴 목록의 값 |
| 지금 연구 중인 기술 | 읽지 않음 | 진행도만 있음. 연구 대상은 직접 고릅니다 |
| 대기열 2번째 이후 | 읽지 않음 | 받은 세이브 260여 도시가 모두 1개뿐이라 형식 미확인 |
| 교역로 | 읽지 않음 | 위치를 찾지 못했습니다. 도시 패널에서 목적지를 직접 입력하면 계산합니다(아래) |
| 비축 식량, 종교·쾌적도·충성도·유닛 | 읽지 않음 | 기본값 |

- **내 문명 선택:** 미리보기 창에서 가져올 플레이어를 고릅니다(기본: 플레이어 0, 관전 세이브면 첫 문명). 그 플레이어의 도시만 시뮬레이터 도시가 되고, 다른 문명·도시국가의 국경과 도시는 지도에 색 테두리와 네모 표시로만 나옵니다(`save_owner`, `save_import.foreign_cities`).
- **방향:** 게임 좌표(y=0이 남쪽)를 그대로 쓰고, 지도에 `"orientation": "north_up"`을 넣어 화면에서만 북쪽이 위로 오게 뒤집어 그립니다. 육각형 이웃 계산은 바뀌지 않습니다. 가로 순환(지도 좌우 연결)은 시뮬레이터에서 계산하지 않습니다.
- **게임 속도:** 세이브의 속도를 그대로 씁니다. 생산·연구 비용은 데이터(GameSpeeds.CostMultiplier) 배율을 적용하지만, 성장 식량은 표준 속도만 검증되어 다른 속도에서는 성장 계산이 "미지원"으로 표시됩니다.
- **시민 배치:** 기본은 세이브 값 그대로이고 도시는 수동 배분 모드로 들어옵니다. 미리보기 창에서 "균형 전략으로 다시 배치"를 고르면 최적화기로 바꿉니다. 시민 정보를 못 읽은 도시는 항상 자동 배치합니다.
- **생산:** 세이브의 현재 항목이 대기열 1번, 배치만 해 둔 특수지구·불가사의가 그 뒤에 들어갑니다(`from_save: current/unfinished`). 특수지구는 게임이 배치 때 고정한 비용(`locked_cost`)을 그대로 씁니다. 교역로·총독·도시국가 보너스는 빠지므로 턴당 생산력이 게임보다 낮게 나올 수 있으니, 완성 턴은 장부와 함께 보세요.
- **확장팩 규칙:** 몰려드는 폭풍 세이브는 `app/data/rules_db_xp2.json`(기본 게임 + 문명의 흥망성쇠 + 몰려드는 폭풍 + DLC)로 계산합니다. 가져온 게임에는 `game_state.ruleset = "RULESET_EXPANSION_2"`가 들어가고, 다른 게임은 계속 기본 `rules_db.json`을 씁니다. 파일이 없으면 기본 규칙으로 계산하고 경고합니다.
- **쾌적도:** 교역으로 받은 사치품·주둔 유닛(용병대 정책)·종교·위인 같은 쾌적도 출처를 세이브에서 읽지 못하므로, 가져온 게임은 쾌적도의 성장·산출 % 효과를 **끈 상태**(`game_state.simulation_overrides.apply_amenity_effects=false`)로 시작합니다. 쾌적도 값 자체는 계속 표시되며, 정책·문명 탭의 "이 게임에서 쾌적도 효과 적용"으로 켤 수 있습니다.
- 가져온 결과는 메모리에만 있고 **[저장]** 또는 **[다른 이름으로 저장]** 해야 파일로 남습니다.

명령줄 도구(웹 앱 없이):

```powershell
python tools/civ6save_to_map.py "C:\Users\...\My Games\Sid Meier's Civilization VI\Saves\Single\내 게임.Civ6Save"
python tools/civ6save_to_map.py 내게임.Civ6Save --out out --saved-map "내 게임 143턴" --player 0
```

`<이름>.svg`(북쪽이 위인 지도 그림: 지형·강·국경·도시·특수지구)와 `<이름>.summary.json`(플레이어·도시·특수지구·건물·경고)을 만들고, `--saved-map`을 주면 `app/data/saved_maps/`에 저장본을 써서 [저장본 불러오기]로 열 수 있습니다.

확장팩 규칙 DB 다시 만들기(게임 설치 폴더의 XML 필요):

```powershell
python tools/build_ruleset_db.py --base "<Civ VI 설치 폴더>\Base\Assets\Gameplay\Data" --dlc "<Civ VI 설치 폴더>\DLC" --ruleset RULESET_EXPANSION_2 --out app/data/rules_db_xp2.json
```

게임이 DB를 만드는 방식을 SQLite로 그대로 따라 합니다: `Data/Schema`의 스키마(트리거 포함) → 기본 XML → 각 `.modinfo`의 UpdateDatabase 중 조건(RuleSetInUse·GameCoreInUse·LeaderPlayable·ModInUse·ConfigurationValueMatches)을 통과한 것만 LoadOrder → 모드 순서(확장팩1, 2, 그 외) → 파일 Priority 순으로 적용(Row/Replace/InsertOrIgnore/Update/Delete, 삭제는 외래 키 연쇄 포함). 결과는 364개 표, 약 4만 9천 행입니다. 모르는 조건은 거짓으로 처리하고 기록합니다.

### 교역로 (v0.4.5, 직접 입력)

도시 패널 → "이 도시에서 출발하는 교역로"에서 목적지(내 다른 도시 = 국내, 지도에 남은 다른 문명·도시국가 도시 = 국제)를 고릅니다(`city.trade_routes = [{"destination": "x,y"}]`). 출발 도시가 받는 몫만 계산합니다:

- 목적지의 완성된 특수지구(도심 포함)마다 `District_TradeRouteYields`의 `YieldChangeAsDomesticDestination` / `YieldChangeAsInternationalDestination`
- 교역로 고정 보너스 Modifier: `EFFECT_ADJUST_TRADE_ROUTE_YIELD`(모든 교역로, 예: 삼각 무역 금+4 신앙+1), `..._FOR_DOMESTIC/_FOR_INTERNATIONAL`, `EFFECT_ADJUST_CITY_TRADE_ROUTE_YIELD_FOR_DOMESTIC/_FOR_INTERNATIONAL`
- 계산하지 않음: 목적지 도시가 받는 몫, 자원·시설·지형 수에 따른 보너스, 동맹·종주국 보너스, 교역로 % 보정, 교역로 수 한도 확인

### 도시국가 사절 (v0.4.6, 직접 입력)

정책·문명 탭 → "도시국가 사절"에서 세이브에 도시가 있는 도시국가마다 사절 수와 종주국 여부를 넣습니다(`empire.city_states = {"CIVILIZATION_BRUSSELS": {"envoys": 1, "suzerain": false}}`). 사절 수는 세이브에서 읽지 못합니다.

- 데이터의 `MINOR_CIV_<유형>_TRAIT`(사절 1·3·6명 유형 보너스)와 `MINOR_CIV_<이름>_TRAIT`(종주국 고유 보너스)의 EFFECT_ATTACH_MODIFIER를 조건(HAS_GIVEN_INFLUENCE_TOKENS, IS_SUZERAIN)대로 평가해 붙입니다. 평화 상태는 참으로 가정하고, 동맹·종교·유닛 수 조건이 붙은 보너스는 제외하고 기록합니다.
- 예: 산업형 도시국가 사절 1명 → 작업장이 있는 도시가 **건물(불가사의 포함)·특수지구를 생산할 때만** 생산력 +1, 수도 +1 (`EFFECT_ADJUST_CITY_PRODUCTION_BUILDING/_DISTRICT`). 게임 툴팁처럼 **도시 생산력에 포함**되며, 장부의 "현재 생산 항목 보너스" 구역에 출처(도시국가)별로 나옵니다. 대기열 1번 항목 기준으로 계산하고, 대기열의 다른 항목은 그 항목에 맞는 보너스로 바꿔 완성 턴을 계산합니다. 게임 툴팁의 "건물, 특수지구 및 불가사의에서 +N"이 이 값입니다.
- 군사형 도시국가 사절: 유닛 생산 중 생산력 +N(`EFFECT_ADJUST_CITY_PRODUCTION_UNIT`, 수도·병영/마구간·영사관 등 조건별).
- 반영하지 않는 도시국가 보너스: 위인 점수(볼로냐 종주국), 충성도(프레슬라프 종주국), 동맹 등급·종교 조건 보너스. 계산 근거 탭에 기록됩니다.
- 도시국가로 가는 교역로는 그 도시국가의 `EFFECT_ADJUST_TRADE_ROUTE_YIELD_TO_OTHERS` 보너스를 받습니다(사절 불필요).

### 확장팩 규칙 세부 보정 (v0.4.7)

- **인구당 과학·문화:** 확장팩 규칙 게임은 GlobalParameters 값(과학 50%, 문화 30%)을 게임처럼 1/256 고정소수점으로 씁니다. 30% → 76/256 = 0.296875라서 인구 11 → 문화 3.27(게임 표시 3.2), 인구 8 → 2.38(표시 2.3).
- **기념비 충성도 보너스:** `REQUIREMENT_CITY_HAS_FULL_LOYALTY` 지원. 충성도는 시뮬레이션하지 않으므로 도시는 충성도 최대로 가정합니다(도시 문서 `full_loyalty: false`로 끔). 게임처럼 건물 기본 산출이 아니라 Modifier(보정치) 줄에 문화 +1로 표시됩니다.
- **도서관 +3:** 과학형 도시국가(예: 볼로냐) 사절 1명이 도서관에 과학 +1(`EFFECT_ADJUST_BUILDING_YIELD_CHANGE`)을 줍니다. 사절을 입력하면 도서관이 +3으로 계산되고, 수도에는 과학 +1이 따로 붙습니다.
- **교역로 해독 (v0.4.9):** 상인의 `UNITOPERATION_MAKE_TRADE_ROUTE` 기록(해시 뒤 +16 시작 턴, +36 목적지 x·y, +44 출발 x·y)에서 읽습니다. 게임 화면의 알렉산드리아→마드리드, 펠라→톨레도, 모헨조다로→마드리드로 확인했습니다. 가져올 때 출발 도시의 `trade_routes`에 목적지와 시작 턴이 들어갑니다. 출발·목적지가 모두 지도 위 도심일 때만 읽습니다.

### 세이브에서 사절·판테온 읽기 (v0.4.8)

- **사절:** 주요 문명마다 `u32 64` 뒤에 플레이어 슬롯 64개의 사절 수(1/256 고정소수점) 배열이 있습니다. 실제 게임 화면 값(빌뉴스 3, 오클랜드 4, 카불 3, 프레슬라프 4, 볼로냐 9)으로 위치를 확인했습니다. 배열은 그 플레이어의 첫 도시 뒤 ~ 다음 플레이어 첫 도시 앞에 있는 것으로 주인을 정합니다.
- **종주국:** 저장 위치를 찾지 못해 계산으로 판정합니다 — 사절 3명 이상이며 다른 모든 문명보다 많을 때(동률이면 아님. 게임은 동률일 때 기존 종주국 유지).
- **판테온:** 플레이어 블록 안에 나오는 신앙 타입 중 분류가 BELIEF_CLASS_PANTHEON인 것이 하나면 그것으로 정합니다(예: 축제의 여신 — 플랜테이션 문화 +1). 종교 창시 신앙은 아직 읽지 않습니다.

### 교역로 기간과 교역로 장부 (v0.4.8)

- 교역로 산출은 산출 장부의 "교역로" 구역에 들어가고, 장부 탭 아래 **교역로 장부**에 교역로마다 목적지 특수지구별 `District_TradeRouteYields` 행, 정책·도시국가 보너스, 기간을 따로 보여 줍니다.
- 기간(근사): 게임은 `TRADE_ROUTE_TURN_DURATION_BASE`(20턴)를 채울 때까지 왕복을 반복합니다. 게임 속도로 줄이지 않습니다 — 온라인 속도 세이브에서 66턴에 시작한 펠라→톨레도가 90턴에도 진행 중이기 때문입니다. 왕복 시간은 직선거리 ÷ 상인 이동력(`Units.UNIT_TRADER.BaseMoves`), 도로 비용 1로 추정합니다. 실제 경로(강·언덕·빠른 도로)는 계산하지 않으므로 1~2턴 어긋날 수 있습니다. 교역로에 시작 턴(`started_turn`)을 넣으면 남은 턴을 표시합니다.
- 장부는 도시를 선택할 때 도시 상세와 동시에 불러와서, 장부 탭을 열면 바로 보입니다.

### 교역로 소유자, 인구 산출, 표시 방식, 걸작

- **교역로 주인:** 교역로 기록이 들어 있는 플레이어 블록으로 주인을 정합니다. 도시국가 상인이 내 도시 좌표로 만든 기록(예: 메디올라눔 → 빌뉴스)은 내 교역로가 아닙니다.
- **인구당 산출:** GlobalParameters 백분율을 그대로 씁니다(문화 0.3). 메디올라눔 인구 5 → 게임 표시 +1.5와 일치. v0.4.7의 1/256 고정소수점 가정은 철회.
- **표시:** 산출은 게임처럼 소수 첫째 자리에서 **버림**합니다(3.29 → 3.2). 계산 값 자체는 그대로입니다.
- **걸작 (직접 입력):** 도시 개요 탭 → "걸작"에서 걸작을 고르면 `GreatWork_YieldChanges`를 더합니다(장부 "걸작" 구역). 세이브의 걸작 목록(만든 걸작과 시점)은 찾았지만 어느 도시·건물에 걸려 있는지는 아직 못 읽습니다. 테마 보너스·걸작 강화 정책은 미반영.

### 지도 타일 상세, 진행 막대, 다른 문명 보기

- **지도 "타일 상세" 모드(기본):** 내 도시 타일마다 지형·지형 특성, 시설·자원, 산출(색 점 + 숫자: 식량·생산·금·과학·문화·신앙)을 타일 안에 표시합니다. 도심은 도시 이름·인구·산출·건물, 특수지구는 이름·인접 보너스·지어진 건물(`Buildings.PrereqDistrict` 기준)을 보여 줍니다. 확대할수록 줄이 늘어납니다.
- **불러오기 진행 막대:** 화면 맨 위에 세이브 업로드(실제 진행률) → 해독(서버 진행률이 없어 추정) → 지도·도시 계산 단계를 표시합니다.
- **다른 문명 보기:** 지도 제목 옆 "다른 문명 보기…"에서 같은 세이브의 다른 문명·도시국가로 전환합니다(해독 결과 캐시 30분 동안). 현재 게임 문서를 그 문명 기준으로 다시 만듭니다.

API: `POST /api/import/civ6save/preview` (본문 = 세이브 바이트, `X-Filename` 헤더 선택) → 요약과 `token`. `POST /api/import/civ6save/apply` `{token, player, auto_assign}` → `/api/maps/load`와 같은 응답 + `import_report`. 미리보기는 서버 메모리에 30분(최근 2개)까지 보관됩니다.

해독 방식은 Civ6EGRM(SamuelH91)과 civ6save-editing(lucienmaloney)의 분석을 참고했고, 코드는 새로 작성했습니다.

## 3. 턴 처리 단계

1 상태 확인 → 2 자동 모드 도시 시민 재배치 → 3 산출량 계산(Modifier·쾌적도·주거공간) → 4 식량 → 5 성장(턴당 1회, 남은 식량 이월) → 6 생산 적용(% 보너스·초과분) → 7 완성 처리 → 8 연구(게임 속도 비용, 부스트) → 9 금·신앙 누적 → 10 새 시민 배치/대기, 인구 감소 시 정리 → 11 턴+1 → 12 다음 턴 재계산·리포트.

이 순서는 **시뮬레이터가 정의한 순서**입니다. Civ VI 내부 처리 순서는 데이터에 없어 검증되지 않았습니다. 모든 단계는 턴 시작 산출량을 사용하므로, 이번 턴에 완성된 건물·성장한 인구는 다음 턴부터 반영됩니다.

## 4. 정확도 상태 (화면의 "계산 근거·미지원 규칙" 탭과 동일)

`검증 완료`는 실제 게임 값과 대조 사례가 일치할 때만 표시합니다. 게임을 직접 실행해 대조하지 못했으므로 **현재 검증 완료 항목은 없습니다.**

| 상태 | 항목 |
|---|---|
| 미검증 (데이터 그대로 구현) | 지형·특성·자원(+발견 기술)·시설(+기술 보너스, 시설 인접)·도심 최소 산출·전문가·건물·성장 요구량·생산 % 보너스·프로젝트 산출 전환 |
| 부분 구현 | 특수지구 인접(강 판정 근사, 0.5 보너스 내림), 인구당 과학·문화(설정값 0.5/0.2 vs 데이터 70%/30%), 정책·정부·특성·기술·종교 Modifier(지원 효과만), 쾌적도(사치 분배 순서 근사), 주거공간(호수 구분 없음), 성장 수정치 결합 방식, 생산 비용(지구 비용 증가 공식 없음), 초과분 정책, 연구, 유지비, 자동 배치, 턴 순서 |
| 미지원 | 교역로의 목적지 몫·자원별 보너스(출발 도시 기본 몫은 부분 구현), 표준 외 속도의 성장 비용, 기아 인구 감소(설정으로만), 문화 영토 확장, 위인·걸작·관광, 전략 자원 보유량 |

Modifier 연결 934개 중 **450개 효과를 해석**하고, 127개는 경제 관련 미지원, 357개는 전투·외교 등 도시 경제 밖 효과입니다. 해석하지 못한 효과와 평가할 수 없는 조건은 계산에서 제외되고 감사 로그(`GET /api/sim/modifiers`)에 이유와 함께 남습니다.

### 설정 (`game_rules.json`의 선택 섹션 `simulation`)

| 키 | 기본값 | 의미 |
|---|---|---|
| `population_yields_source` | `config` | `global_parameters`로 바꾸면 인구당 과학 0.7·문화 0.3 (기본 게임 데이터 값) |
| `overflow_policy` | `carry_to_next` | 턴당 1개 완성, 초과분은 다음 항목으로 (`same_turn_queue`, `stop_at_item` 가능) |
| `research_overflow` | `carry` | 연구 완료 후 남는 과학·문화를 다음 연구로 이월 |
| `default_citizen_mode` | `manual` | 기존 맵의 작업 타일을 그대로 사용 |
| `apply_modifiers` / `apply_amenity_effects` / `apply_housing_growth_penalty` / `apply_game_speed_costs` / `apply_cost_progression` / `resource_visibility_requires_tech` | `true` | 각 규칙 켜기/끄기 (모두 끄면 기존 `/api/calculate`와 같은 값 — 테스트로 확인) |
| `luxury_amenities` | `auto` | 개량된 사치 자원을 Resources.Happiness 도시 수만큼 분배 |
| `starvation_pop_loss` | `false` | 식량 0에서 인구 감소 (규칙 미확인이라 기본 꺼짐) |

`simulation` 섹션은 선택 사항이며 없으면 위 기본값을 씁니다. 알 수 없는 키나 잘못된 값은 다른 규칙과 마찬가지로 명확한 오류(HTTP 500 + 사유)로 거부됩니다.

## 5. 새 API (기존 URL·응답은 그대로 유지)

모든 도시/턴 API는 `?scenario_id=`로 시나리오를 대상으로 할 수 있습니다.

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/sim/state` | 전체 도시 산출·성장·생산·제국 요약 (`details=true`면 타일까지) |
| GET | `/api/sim/cities/{id}` | 구성요소별 산출, 성장, 주거, 쾌적, 대기열, 다음 턴 비교, Modifier 감사 |
| POST | `/api/sim/turn/preview` | `{turns}` 저장 없이 N턴 결과 |
| POST | `/api/sim/turn` | `{turns, city_id}` N턴 실행 후 반영 (현재 게임은 메모리, 저장 버튼으로 디스크) |
| GET | `/api/sim/history` | 턴별 기록 (현재 게임은 최근 60턴, game_state에 저장) |
| PUT | `/api/sim/cities/{id}/settings` | 배분 방식, 전략, 새 시민 정책, 주거 모드, 쾌적도, 사용자 정의 효과, 교역로(`trade_routes`) |
| PUT | `/api/sim/cities/{id}/citizens/{x}/{y}` | `{citizens}` 시민/전문가 배치 (인구·슬롯 초과 거부) |
| PUT | `/api/sim/cities/{id}/tile-preferences/{x}/{y}` | `{lock, block, priority}` |
| POST | `/api/sim/cities/{id}/optimize` | `{strategy, apply}` 최적 배치 + 전략별 비교 |
| GET/POST/PUT/DELETE | `/api/sim/cities/{id}/production/...` | 옵션(비용 단계·보너스·불가 사유), 추가, 순서, 취소 |
| GET/PUT | `/api/sim/empire/options`, `/api/sim/empire` | 정부·정책 슬롯·문명·지도자·판테온, 게임별 쾌적도 효과(`apply_amenity_effects`) (즉시 재계산 영향 반환) |
| GET/POST/PATCH/DELETE | `/api/scenarios[...]` | 목록, 생성(현재/다른 시나리오 복제), 상태, 이름, 삭제, `reset`, `duplicate`, `apply` |
| POST | `/api/scenarios/compare`, `/api/scenarios/strategy-batch` | 시나리오 비교, 도시 전략별 시나리오 일괄 생성·실행 |
| GET/POST/DELETE | `/api/validation/cases`, `GET /api/validation/coverage` | 게임 대조 사례, 정확도 상태 |
| GET | `/api/data/audit`, `/api/sim/modifiers` | XML↔카탈로그 감사, Modifier 감사 로그 |
| POST | `/api/import/civ6save/preview`, `/api/import/civ6save/apply` | Civ VI 세이브 해독 미리보기 → 선택한 플레이어로 가져오기 (2-3 참고) |

## 6. 화면

- **상단:** 턴, 속도, 연구, 제국 과학·문화·순 금, 시나리오 선택, N턴 실행/미리보기 (단축키 T = 새 엔진 1턴).
- **좌측:** 도시 카드(인구, 산출, 성장, 생산 ETA), 제국 요약.
- **지도:** 기존 84×54 렌더링(세이브를 가져오면 그 크기) + 특수지구 글자 표시, 세이브 지도의 강 모서리·다른 문명 국경/도시 + 표시 모드(식량~신앙 산출, 작업 상태, 영토, 인접 보너스), **시민 배치 모드**(클릭으로 배치/해제), 잠금🔒·금지⛔ 표시.
- **우측 탭:** 개요(현재 vs 다음 턴 표, 변화 원인, 클릭 가능한 산출 근거, 주거·쾌적·사용자 효과), 시민(전략·최적화·타일별 배치), 생산(대기열·비용 단계·보너스), 정책·문명, 기존 편집/타일/연구 도구.
- **하단:** 턴별 기록, 시나리오 비교, 그래프, 이벤트 로그, 계산 근거·미지원 규칙(+게임 대조 입력), 데이터 감사.

**화면 배치 조절 (v0.4.2):** 패널 사이의 경계선(마우스를 올리면 금색)을 끌어 도시 패널·상세 패널의 너비와 하단 패널의 높이를 바꿀 수 있습니다.
- 끝까지 줄이면 그 패널이 숨겨지고, 같은 경계선을 다시 끌면 나타납니다. 지도는 최소 폭 320px·높이 140px을 보장합니다.
- 경계선 더블클릭: 그 패널만 기본 크기로 되돌립니다. 키보드: 경계선에 포커스(Tab) 후 화살표(Shift = 크게), Enter = 숨김/표시.
- 하단 탭 막대의 "화면 배치"에서 기본 / 지도 넓게 / 장부·분석 넓게 / 상세 패널 넓게를 바로 고를 수 있습니다.
- 크기는 브라우저(localStorage)에 저장되어 새로고침 후에도 유지됩니다. 경계선을 끌 때 지도는 보던 위치를 중심으로 유지합니다.
- 1250px보다 좁은 화면에서는 패널이 세로로 쌓이며 경계선 조절은 꺼집니다.

시나리오 모드에서는 기존 편집 도구(현재 게임 전용)가 비활성화되어 실수로 원본을 바꾸지 않습니다. 기존 "레거시 턴 종료" 버튼과 `/api/turn/end`는 하위 호환을 위해 남겨 두었습니다. 단, 새 생산 탭에서만 넣을 수 있는 항목(예: 전사·프로젝트)이 대기열 맨 앞에 있으면 레거시 턴 종료는 기존처럼 422를 반환합니다.

## 7. 테스트

```powershell
python -m unittest discover -s tests -v
```

158개. 대부분은 사용자의 게임 데이터로 만든 `app/data`가 있어야 돌고, 없으면 건너뜁니다(첫 실행 화면이나 위 명령으로 먼저 만드세요). 데이터 없이 도는 것은 규칙 DB 빌더와 첫 실행 설정 테스트 8개입니다. 세이브 테스트는 실제 세이브와 같은 바이트 구조의 합성 세이브(기술·사회제도·정책 표 포함)를 만들어 검사합니다. 실제 세이브로도 검사하려면 `CIV6_SAVE_DIR`에 세이브 폴더를 지정하세요(강 모서리 일치, 도시 수 = 도심 수, 건물 위치 = 그 도시 타일, 플레이어 표 위치 검증, 정부 확정; 없으면 1개 건너뜀). 결정론, 시민 배치 변경, 성장 후 다음 턴 산출, 생산 완료·초과분, 건물 완성 후 산출 변화, 지구 인접(산·도심·강), 정책 변경, 시나리오 독립성, 원본 보존, 다중 턴 누적, 저장/불러오기 동일성, 기존 계산과의 동치성(새 규칙 끔)을 포함합니다. 기대값은 게임 데이터 값으로 직접 계산해 주석에 적었습니다.

## 8. 실제 게임과 대조하는 방법

1. 게임에서 도시 하나를 고르고 지형·시설·건물·작업 타일·정책을 시뮬레이터에 똑같이 입력합니다.
2. 하단 "계산 근거·미지원 규칙" 탭에서 게임 도시 화면의 산출량을 입력해 사례로 저장합니다.
3. 차이가 0.05 이내면 `일치`(검증 완료)로, 아니면 항목별 차이가 표시됩니다. 불일치 항목은 개요 탭의 산출 근거에서 원인을 추적할 수 있습니다.

---

# v0.3.0 Reference (기존 에디터 문서)

## What's included
- Uses the map CSV from the uploaded `civ6reader.zip`.
- Loads a real map grid of 84 x 54 (4,536 tiles).
- Canvas map with click selection, drag-to-pan, wheel zoom, zoom buttons and coordinate labels.
- Hover a tile for a yield summary; click to pin its tile inspector with catalog movement/defense data, ownership, work state and existing yield breakdown.
- Visible `CITY SIMULATOR v0.5.2` badge and `/health` version check to catch old-server confusion.
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
- Keep map, city and game-state changes in server memory until the user explicitly saves; unsaved changes are lost when the server restarts.
- Save the current map, create a non-overwriting named snapshot in `app/data/saved_maps`, and reload a validated snapshot.
- Delete cities from the selected city panel or city list; owned tiles and citizen work state are cleared.
- Calculates a rule-limited city yield total and itemized tile/district/building breakdown from the imported catalog.
- Tracks city Food yield, population consumption, stored Food and Standard-speed growth requirements; growth activation and per-turn cap are configurable.
- Loads frequently tuned gameplay settings and declared modifiers from `app/config/game_rules.json`.
- Queue XML-cost production for Builders, Settlers, buildings, districts and wonders; end turns to apply city Production.
- Move produced units, let Builders construct catalog-valid improvements/remove eligible features, and found cities with Settlers.

## Extracted entity inventory and availability

The supplied XML extraction includes 85 XML files, 570 tables and 23,196 table rows. Its entity index contains 51 buildings, 29 wonders, 97 units, 103 policies, 10 governments, 25 improvements, 21 districts, 18 projects, 68 technologies and 51 civics. These are counts from the supplied archive, not a guarantee of every DLC, mod or runtime-only game rule.

`GET /api/entities/catalog` returns those ten categories with each entity's extracted technology/civic prerequisites and an `available` flag with explicit blocking reasons. Optional `category` filters to one category. Supplying `city_id` also checks city-level population, completed-district and building prerequisites where applicable; supplying both `x` and `y` checks placement conditions for districts, wonders and improvements on that city's tile. The rules are implemented in `app/entity_rules.py`, separate from the API entry point.

This availability view does not imply that every entity can currently be produced by the editor: production remains limited to Builders, Settlers, districts, buildings and wonders. It also does not simulate full policy/government selection or every XML modifier/requirement chain.

The research panel also provides explicit force-unlock/force-lock controls for catalogued technologies and civics. Locking clears that research's progress/current selection and removes legacy city-level unlock entries so the lock remains effective. The unit panel lists every unit type in the extracted XML; force-creation checks its available technology, civic, population and district prerequisites. Buildings can be force-added to a city after their research, district, building, exclusivity and instance-limit conditions pass. Wonders, districts and improvements additionally require a selected owned tile that passes their extracted placement rules. The unit panel can also force-delete the selected unit. These controls bypass production costs, but do not bypass the listed availability and placement conditions.

## Backend structure

The FastAPI entry point in `app/main.py` creates the application, mounts static
assets, installs exception handlers, and registers feature routers. HTTP
endpoints live in `app/controllers/`; existing simulation behavior is kept in
domain-focused modules under `app/services/`. The JSON persistence primitives
and data-specific accessors are in `app/repositories/`, with canonical data
locations managed there through `app/repositories/locations.py`.

Request schemas are in `app/schemas/`, and JSON-compatible domain types are in
`app/models/`. Game rules and entity availability retain their existing
implementations in `app/config.py` and `app/entity_rules.py`. The simulator reads the existing JSON files under `app/data/` on startup.
The map, city and game-state documents are held in memory during play; the
explicit **저장** action writes those three documents, **다른 이름으로 저장**
creates a named snapshot, and **저장본 불러오기** replaces the active in-memory
state. Other gameplay actions do not write those active documents to disk.

The current service flow is:

`Controller -> Service -> Repository -> in-memory active state -> explicit save -> app/data JSON`

The city-yield service uses the dedicated adjacency service for district
bonuses. Catalog-derived entity and placement rules remain in
`app/entity_rules.py`. This structural change does not add unsupported game
simulation behavior or alter the frontend API.

```text
app/
  controllers/  city, entity, game-data, map, production, research, snapshot,
                tile, unit, web and yield routers
  core/         paths, synchronization, application errors and constants
  models/       map, city, tile, district, building, wonder, improvement, yield
  repositories/ JSON, map/city/tile, game-data and snapshot access
  schemas/      city, game, map, snapshot, tile and yield API types
  services/     map, city, game data, production, research, units, yields,
                entity rules, snapshots and district adjacency
  data/         existing game JSON and saved map snapshots
  static/       existing frontend
```

Run the existing regression suite with:

```powershell
python -m unittest discover -s tests -v
```

## Run (Windows PowerShell)
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```
Open **http://127.0.0.1:8010**. API docs: http://127.0.0.1:8010/docs.

**Recommended:** double-click `run_windows.bat` — the browser opens by itself about 3 seconds after the server starts. Packages are installed only on the first run or when `requirements.txt` changes (a copy is kept as `.venv\requirements.installed`), and if the server is already running it just opens the browser. It always changes into this project folder and uses port `8010` so it does not silently reuse an older server on port `8000`. The header must show `CITY SIMULATOR v0.5.2`. You can verify the server at http://127.0.0.1:8010/health; it must return version `0.5.2`. If the version differs, you are talking to another project/server.

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

The result is an approximation, not an in-game simulation. The population Science/Culture values are configurable flat yields requested for this project; the similarly named percentage parameters found in the extracted game data are not implemented as percentage modifiers. City Food uses the existing yield calculation; the configurable per-population consumption default is 2 Food and should be treated as an explicit rule setting, not a value verified from the supplied data. Growth requirements use the configured formula at Standard speed. On turn end, growth is automatic when stored Food reaches the requirement: the city gains population and pays the requirement from its stored Food, with a default cap of one growth per turn (`food.max_growths_per_turn: 1`). Other speeds are recognized but their growth-cost multipliers are not implemented. Starvation penalties are not simulated. Hills use extracted XML yields for the matching terrain ID when available; a manual hill override adjusts direct Production by one when the XML terrain variant is unavailable or overridden. River adjacency is reported as context but does not add yields. Dynamic resource/improvement conditions, unsupported adjacency modifiers, policies, governors, civilization/leader abilities, technology/civic boosts, era/game-speed modifiers for other systems, religion, trade routes, all DLC data, and complete localization are not modeled.

## Editing gameplay rules

Edit `app/config/game_rules.json`; `app/config.py` only loads and validates this file. `GET /api/game-rules` returns the active validated settings. The server reads the JSON on each rules-dependent request, so save the file and reload the browser; a server restart is not normally required. A missing, malformed, or unsupported rules file returns an explicit HTTP 500 error instead of silently using defaults.

The JSON groups rules by purpose:

- `city`: minimum center-to-center distance in hexes, initial population, initial territory radius in hexes, maximum population, additional worked tiles per population citizen, center minimum yields, and `population_yields` (flat yield points per citizen, currently `Science: 0.5` and `Culture: 0.2`). The center is always one worked tile; total worker capacity is `1 + population * worked_tiles_per_population`.
- `game_speed`: current/default speed identifier (`STANDARD`, `QUICK`, `ONLINE`, `EPIC` or `MARATHON`). The active speed is also stored with game state so snapshots restore it. Only Standard growth cost is implemented, with multiplier `1.0`.
- `food`: `food_per_population` configures population consumption (default `2.0`); `max_growths_per_turn` caps automatic population growth per turn and defaults to `1`. On turn end, each growth subtracts the current population's required Food from storage before recalculating the next threshold. Growth requirement is `15 + 8 * (population - 1) + (population - 1)^1.5`. Stored Food and per-city food status are saved in `cities.json` and snapshots.
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
- Production and food state are stored with each city in `app/data/cities.json`; turn number, unit instances, research and game speed are stored in `app/data/game_state.json`. Named snapshots include the map and both state files, and restore all three together after validation.
- State-changing endpoints are serialized within the single server process. The default local Uvicorn setup is supported; multi-worker deployment needs a shared database or cross-process lock.
- Production costs use extracted base XML `Cost` unless `production.cost_overrides` explicitly overrides them. Cost progression models, game speed, technologies/policies/civilization cost modifiers and production discounts are not simulated.
- `GET /api/cities/{city_id}/food` provides the live Food yield and stored growth status. The existing yield and city APIs retain their existing URLs and response shapes.
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
- `GET /api/entities/catalog` — return the complete extracted entity inventory and technology/civic availability. Optional query parameters: `category`, `city_id`, and both `x` and `y` for tile placement checks.
- `PUT /api/research/{technology|civic}/{target_id}/availability` with `{"unlocked":true|false}` — force unlock or lock an extracted technology/civic.
- `POST /api/units/force` with `{"unit_type":"UNIT_WARRIOR","owner_city_id":"city-id"}` — force-create a catalogued unit at its owner's city center. Optional `x` and `y` together place it on another map tile.
- `DELETE /api/units/{unit_id}` — force-delete a unit.
- `POST /api/entities/force` with `{"category":"buildings","entity_id":"BUILDING_LIBRARY","city_id":"city-id"}` — force-add a condition-valid building. `wonders`, `districts`, and `improvements` also require `x` and `y` placement coordinates.
- `GET /api/tiles/{x}/{y}/yields` — return the selected tile's own yields for the hover tooltip; unknown coordinates return `404`.
- `GET /api/game` — return the current turn and saved unit instances.
- `GET /api/research` — return empire-wide technology/civic research choices, prerequisites, progress, completed unlocks, and summed Science/Culture per turn.
- `PUT /api/research/{category}` — select `technology` or `civic` research; body: `{"target_id":"TECH_MINING"}` or a civic ID. XML prerequisites must already be completed.
- `GET /api/production/catalog?city_id={city_id}` — list XML-derived production options, costs and city-specific availability reasons.
- `GET /api/cities/{city_id}/production` — return production per turn, queue progress, remaining costs, estimated turns and cost caveats.
- `POST /api/cities/{city_id}/production/queue` — append a production target. Body: `{"category":"unit","target_id":"UNIT_BUILDER"}`. Districts and wonders also require placement coordinates, e.g. `{"category":"district","target_id":"DISTRICT_CAMPUS","x":12,"y":8}`.
- `PUT /api/cities/{city_id}/production/order` — move a queue item; body: `{"queue_id":"prod-…","position":0}`. Position zero becomes the active production.
- `DELETE /api/cities/{city_id}/production/queue/{queue_id}` — cancel an item and discard its accumulated progress.
- `POST /api/turn/end` (optional `city_id` query parameter) — apply each city's current calculated Production, complete eligible items, carry overflow forward through that city's queue, advance the turn and refresh unit movement. The response includes updated cities, map changes and (when `city_id` is provided) that city's refreshed yield, food and production panels so the browser can update without reloading map or city data.
- `POST /api/units/{unit_id}/move` — move one adjacent hex; body: `{"x":12,"y":8}`.
- `POST /api/units/{unit_id}/build` — construct a valid improvement; body: `{"improvement_id":"IMPROVEMENT_FARM"}`.
- `POST /api/units/{unit_id}/remove-feature` — remove the Builder's current XML-removable feature.
- `POST /api/units/{unit_id}/found-city` — found a city at the Settler's position; body: `{"name":"New City"}`.

Production queue requests with missing costs, unsupported targets, invalid prerequisites or invalid placements return a 4xx response instead of substituting a guessed cost. Unit, city, map and turn changes are written together using staged JSON replacement with rollback on write failure. The browser exposes these APIs in the city production and unit action panels; manual building/wonder editing remains available as a separate editor capability.
- Original game data belongs to its rights holders. Avoid redistributing proprietary data publicly without checking applicable terms.
