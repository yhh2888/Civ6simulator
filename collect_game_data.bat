@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Civ VI City Studio - collect game data
echo =============================================================
echo  Civ VI City Studio - game data copy / 게임 데이터 복사
echo  Copies only XML / SQL / modinfo rule files from your own
echo  Civilization VI install into .\game_data (no art, no audio).
echo  내 PC에 설치된 문명 6에서 규칙 파일(XML/SQL/modinfo)만
echo  .\game_data 폴더로 복사합니다. 그림/소리 파일은 복사하지 않습니다.
echo =============================================================

set "GAME="
rem 1) Steam path from the registry
for /f "tokens=2,*" %%A in ('reg query "HKCU\Software\Valve\Steam" /v SteamPath 2^>nul ^| find "SteamPath"') do set "STEAM=%%B"
if defined STEAM (
  set "STEAM=!STEAM:/=\!"
  if exist "!STEAM!\steamapps\common\Sid Meier's Civilization VI\Base" set "GAME=!STEAM!\steamapps\common\Sid Meier's Civilization VI"
)
rem 2) common install folders
for %%P in (
  "C:\Program Files (x86)\Steam\steamapps\common\Sid Meier's Civilization VI"
  "C:\Program Files\Steam\steamapps\common\Sid Meier's Civilization VI"
  "D:\SteamLibrary\steamapps\common\Sid Meier's Civilization VI"
  "E:\SteamLibrary\steamapps\common\Sid Meier's Civilization VI"
  "C:\Program Files\Epic Games\SidMeiersCivilizationVI"
) do (
  if not defined GAME if exist "%%~P\Base" set "GAME=%%~P"
)
rem 3) ask
if not defined GAME (
  echo.
  echo 게임 폴더를 찾지 못했습니다. 설치 폴더 경로를 붙여 넣으세요.
  echo Could not find the game. Paste the install folder path:
  set /p "GAME=> "
)
set "GAME=%GAME:"=%"
if not exist "%GAME%\Base\Assets\Gameplay\Data\Terrains.xml" (
  echo.
  echo [오류] "%GAME%\Base\Assets\Gameplay\Data" 가 없습니다.
  echo [Error] "%GAME%\Base\Assets\Gameplay\Data" not found.
  pause
  exit /b 1
)

echo.
echo Game folder / 게임 폴더: %GAME%
echo Copying base rules / 기본 규칙 복사...
robocopy "%GAME%\Base\Assets\Gameplay\Data" "game_data\Base\Assets\Gameplay\Data" *.xml *.sql /S /NFL /NDL /NJH /NJS /NP >nul
if exist "%GAME%\DLC" (
  echo Copying expansion and DLC rules / 확장팩·DLC 규칙 복사... ^(may take a minute / 1분 정도^)
  robocopy "%GAME%\DLC" "game_data\DLC" *.xml *.sql *.modinfo /S /NFL /NDL /NJH /NJS /NP >nul
) else (
  echo No DLC folder - base rules only / DLC 폴더 없음 - 기본 규칙만 사용
)

echo.
echo 완료. 웹 화면의 경로 칸에 아래 폴더를 넣고 [추출]을 누르세요.
echo Done. Enter this folder in the web page and press Extract:
echo    %CD%\game_data
echo.
pause
