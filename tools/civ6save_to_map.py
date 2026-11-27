"""Decode a Civilization VI save (.Civ6Save) into a map picture and app data.

Usage (from the project root):

    python tools/civ6save_to_map.py "C:/.../My Game.Civ6Save"
    python tools/civ6save_to_map.py save.Civ6Save --player 0 --out out_dir
    python tools/civ6save_to_map.py save.Civ6Save --saved-map "내 게임 143턴"

Outputs (in --out, default: next to the save):
  <name>.svg            map picture (north up): terrain, rivers, borders,
                        cities, districts. Opens in any browser.
  <name>.summary.json   what was decoded: players, cities, districts,
                        buildings, warnings, unknown type hashes.
  --saved-map NAME      also writes app/data/saved_maps/NAME.json, which the
                        web app opens with [저장본 불러오기].

The same decoder runs inside the web app ([세이브 가져오기] button).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.civ6save import SaveFormatError, decode_save  # noqa: E402
from app.services.civ6save.decoder import RIVER_DIRECTIONS, SaveData, neighbor  # noqa: E402

TERRAIN_COLORS = {
    "GRASS": "#547d4c", "PLAINS": "#858148", "DESERT": "#c3a164", "TUNDRA": "#71827d",
    "SNOW": "#d7e1df", "COAST": "#2b6681", "OCEAN": "#183d59",
}
FEATURE_COLORS = {
    "FEATURE_FOREST": "#174b30", "FEATURE_JUNGLE": "#0b613d", "FEATURE_MARSH": "#284d4c",
    "FEATURE_OASIS": "#59bca4", "FEATURE_REEF": "#64b8b8",
}
PLAYER_COLORS = [
    "#e5484d", "#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#8e6cd8",
    "#e0e0e0", "#14a8b8", "#b5651d", "#7f8cff", "#ff8fb1", "#a3a33a", "#5fbf9f", "#c06c84",
]
DISTRICT_LETTERS = {
    "DISTRICT_CAMPUS": "C", "DISTRICT_HOLY_SITE": "H", "DISTRICT_COMMERCIAL_HUB": "$",
    "DISTRICT_THEATER": "T", "DISTRICT_ENCAMPMENT": "E", "DISTRICT_HARBOR": "B",
    "DISTRICT_INDUSTRIAL_ZONE": "I", "DISTRICT_WONDER": "W", "DISTRICT_AQUEDUCT": "A",
    "DISTRICT_ENTERTAINMENT_COMPLEX": "P", "DISTRICT_NEIGHBORHOOD": "N", "DISTRICT_GOVERNMENT": "G",
}


def player_color(player: int) -> str:
    if player == 62:
        return "#9aa3a8"
    return PLAYER_COLORS[player % len(PLAYER_COLORS)]


def terrain_color(terrain: str) -> str:
    key = terrain.replace("TERRAIN_", "")
    if key.endswith("_MOUNTAIN"):
        return "#686b6b"
    base = TERRAIN_COLORS.get(key.split("_")[0], "#596a65")
    if key.endswith("_HILLS"):
        r, g, b = (int(base[i : i + 2], 16) for i in (1, 3, 5))
        return f"#{int(r * .82):02x}{int(g * .82):02x}{int(b * .82):02x}"
    return base


def render_svg(save: SaveData, radius: float = 10.0) -> str:
    w = math.sqrt(3) * radius
    width_px = (save.width + 0.5) * w + 8
    height_px = (save.height - 1) * 1.5 * radius + 2 * radius + 8

    def centre(x: int, y: int) -> tuple[float, float]:
        row = save.height - 1 - y  # north up
        return 4 + w / 2 + x * w + (w / 2 if y % 2 else 0), 4 + radius + row * 1.5 * radius

    def corners(cx: float, cy: float) -> list[tuple[float, float]]:
        return [(cx + radius * math.cos(math.radians(60 * i - 90)), cy + radius * math.sin(math.radians(60 * i - 90)))
                for i in range(6)]

    def pts(points) -> str:
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width_px:.0f} {height_px:.0f}" '
           f'width="{width_px:.0f}" height="{height_px:.0f}" font-family="system-ui, sans-serif">',
           f'<rect width="100%" height="100%" fill="#0c171d"/>']
    layer_tiles, layer_marks, layer_rivers, layer_borders, layer_labels = [], [], [], [], []
    tiles = {(t.x, t.y): t for t in save.tiles}
    centres = {(c.x, c.y) for c in save.cities}
    district_at = {(d.x, d.y): d.type for d in save.districts}
    for t in save.tiles:
        cx, cy = centre(t.x, t.y)
        cs = corners(cx, cy)
        fill = "#c8e6ed" if t.feature == "FEATURE_ICE" else terrain_color(t.terrain)
        layer_tiles.append(f'<polygon points="{pts(cs)}" fill="{fill}" stroke="#14252d" stroke-width="0.5"/>')
        if t.feature and t.feature != "FEATURE_ICE":
            color = FEATURE_COLORS.get(t.feature, "#e0c050")
            layer_marks.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius * .28:.1f}" fill="{color}"/>')
        if t.resource:
            layer_marks.append(f'<circle cx="{cx + radius * .3:.1f}" cy="{cy - radius * .25:.1f}" r="{radius * .16:.1f}" fill="#f1d078"/>')
        if t.improvement:
            s = radius * .3
            layer_marks.append(f'<rect x="{cx - s / 2:.1f}" y="{cy + radius * .12:.1f}" width="{s:.1f}" height="{s:.1f}" fill="#f3e4ba"/>')
        for i, direction in enumerate(RIVER_DIRECTIONS):
            # corner i → i+1 is edge NE, E, SE, SW, W, NW in a north-up drawing
            if direction in t.river_edges:
                (x1, y1), (x2, y2) = cs[i], cs[(i + 1) % 6]
                layer_rivers.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>')
            if t.player is not None:
                nx, ny = neighbor(t.x, t.y, direction, save.width)
                other = tiles.get((nx, ny))
                if other is None or other.player != t.player:
                    (x1, y1), (x2, y2) = cs[i], cs[(i + 1) % 6]
                    layer_borders.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                                         f'stroke="{player_color(t.player)}"/>')
        district = district_at.get((t.x, t.y))
        if district and (t.x, t.y) not in centres:
            letter = DISTRICT_LETTERS.get(district, "◆")
            layer_labels.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius * .45:.1f}" fill="#121c22" stroke="#fff" stroke-width="0.8"/>'
                f'<text x="{cx:.1f}" y="{cy + radius * .25:.1f}" font-size="{radius * .7:.1f}" fill="#fff" '
                f'text-anchor="middle" font-weight="700">{escape(letter)}</text>')
    for c in save.cities:
        cx, cy = centre(c.x, c.y)
        s = radius * .62
        label = c.name + (f" {c.population}" if c.population else "")
        layer_labels.append(
            f'<rect x="{cx - s:.1f}" y="{cy - s:.1f}" width="{2 * s:.1f}" height="{2 * s:.1f}" '
            f'fill="{player_color(c.player)}" stroke="#000" stroke-width="1"/>'
            f'<text x="{cx:.1f}" y="{cy - radius * .9:.1f}" font-size="{radius * .95:.1f}" fill="#fff" '
            f'text-anchor="middle" font-weight="700" stroke="#000" stroke-width="2.2" paint-order="stroke">{escape(label)}</text>')
    out += layer_tiles + layer_marks
    out.append(f'<g stroke="#69c7ef" stroke-width="{radius * .22:.1f}" stroke-linecap="round">{"".join(layer_rivers)}</g>')
    out.append(f'<g stroke-width="{radius * .16:.1f}" stroke-linecap="round">{"".join(layer_borders)}</g>')
    out += layer_labels
    out.append("</svg>")
    return "\n".join(out)


def summary_json(save: SaveData) -> dict:
    return {
        "width": save.width, "height": save.height, "header": save.header,
        "turn_estimate": save.turn_estimate, "players": save.players, "stats": save.stats,
        "warnings": save.warnings, "unknown_types": save.unknown_types,
        "cities": [{
            "name": c.name, "player": c.player, "x": c.x, "y": c.y,
            "population": c.population, "owned_tiles": c.owned_tiles,
            "districts": [{"type": d.type, "x": d.x, "y": d.y, "completed": d.completed} for d in c.districts],
            "buildings": c.buildings,
            "citizens": c.citizens,
            "production": c.production,
            "construction": c.construction,
        } for c in save.cities],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("save", type=Path)
    parser.add_argument("--out", type=Path, default=None, help="output folder (default: next to the save)")
    parser.add_argument("--player", type=int, default=None, help="player index for --saved-map (default: first major civ)")
    parser.add_argument("--saved-map", default=None, help="also write app/data/saved_maps/<NAME>.json")
    args = parser.parse_args(argv)

    from app.services.save_import_service import build_documents, default_player, type_names

    raw = args.save.read_bytes()
    try:
        save = decode_save(raw, type_names())
    except SaveFormatError as error:
        print(f"해독 실패: {error}", file=sys.stderr)
        return 2
    out_dir = args.out or args.save.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = args.save.stem
    (out_dir / f"{stem}.svg").write_text(render_svg(save), encoding="utf-8")
    (out_dir / f"{stem}.summary.json").write_text(
        json.dumps(summary_json(save), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{save.width}x{save.height} · 도시 {len(save.cities)} · 특수지구 {len(save.districts)} · "
          f"턴(추정) {save.turn_estimate}")
    for index, info in sorted(save.players.items()):
        if info.get("cities"):
            print(f"  플레이어 {index}: {info.get('civilization')} / {info.get('leader')} — 도시 {info['cities']}")
    for warning in save.warnings:
        print(f"  주의: {warning}")
    print(f"→ {out_dir / (stem + '.svg')}")
    print(f"→ {out_dir / (stem + '.summary.json')}")
    if args.saved_map:
        from app.services.snapshot_service import saved_map_path, validate_game_state_snapshot, validate_map_snapshot

        player = args.player if args.player is not None else default_player(save)
        if player is None:
            print("가져올 도시가 있는 플레이어가 없습니다.", file=sys.stderr)
            return 3
        map_data, cities, game, report = build_documents(save, player, args.save.name)
        snapshot = {"format": "civ6-city-studio-map", "format_version": 1,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                    "map": map_data, "cities": cities, "game_state": game}
        validate_map_snapshot(snapshot)
        snapshot["game_state"] = validate_game_state_snapshot(game, cities, map_data)
        path = saved_map_path(args.saved_map)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        print(f"→ {path}  (웹 앱의 [저장본 불러오기]로 열기. 시민 자동 배치는 웹 앱 가져오기에서만 수행)")
        for warning in report["warnings"]:
            print(f"  주의: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
