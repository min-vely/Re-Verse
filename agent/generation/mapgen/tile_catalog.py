"""타일 카탈로그 로더 + 색 기반 자동 분류.

build_tile_catalog.py 가 만든 tile_catalog.json(전수 메타)을 읽어, 평균색으로
지형군을 분류하고 "변형 타일"을 찾는다. 같은 의미(잔디 등)의 색·종류·통행성이
비슷한 타일들을 묶어 생성기가 다양하게 쓸 수 있게 한다.

canonical: docs/rpgmaker/tile_palette.md
"""

import colorsys
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CATALOG_PATH = Path(__file__).parent / "data" / "tile_catalog.json"


@lru_cache(maxsize=1)
def load_catalog() -> dict[str, Any]:
    """tile_catalog.json 1회 로드."""
    return json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))


def objects_in_categories(
    tileset_id: int,
    categories: tuple[str, ...] | list[str],
) -> list[dict[str, Any]]:
    """카탈로그에서 주어진 카테고리의 '이름 있는' 단일 오브젝트 목록(배치 풀 확장용).

    멀티타일 구성타일(name 없음)·오토타일(kind A1~A4)은 제외. 각 항목은 palette 와
    연결되는 name 을 가져 pal.get_object 로 바로 배치 가능.
    반환 항목: {name, base_id, category, passable, layer}.
    """
    cat = load_catalog()
    ts = cat.get("tilesets", {}).get(str(tileset_id), {})
    want = set(categories)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in ts.get("tiles", []):
        name = e.get("name")
        if not name or name in seen or e.get("category") not in want:
            continue
        kind = str(e.get("kind", ""))
        if kind.startswith("A") and kind != "A5":
            continue  # 오토타일(지형) 제외, A5 단일은 허용
        layers = e.get("layers") or {}
        layer = int(max(layers, key=layers.get)) if layers else 1
        seen.add(name)
        out.append({
            "name": name, "base_id": e["base_id"], "category": e["category"],
            "passable": e.get("passable"), "layer": layer or 1,
        })
    return out


def color_group(rgb: tuple[int, int, int] | list[int]) -> str:
    """평균 RGB → 색군 이름 (HSV 기반).

    무채색: white/gray/black. 유채색: red/orange/yellow/green/cyan/blue/purple.
    """
    r, g, b = (x / 255 for x in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s < 0.18:  # 무채색
        if v > 0.78:
            return "white"
        if v < 0.25:
            return "black"
        return "gray"
    hd = h * 360
    if hd < 20 or hd >= 330:
        return "red"
    if hd < 45:
        return "orange"
    if hd < 70:
        return "yellow"
    if hd < 160:
        return "green"
    if hd < 200:
        return "cyan"
    if hd < 260:
        return "blue"
    return "purple"


@lru_cache(maxsize=8)
def _tiles_by_id(tileset_id: int) -> dict[int, dict]:
    cat = load_catalog()
    ts = cat.get("tilesets", {}).get(str(tileset_id))
    if not ts:
        return {}
    return {e["base_id"]: e for e in ts["tiles"]}


def get_tile_meta(tileset_id: int, base_id: int) -> dict | None:
    """카탈로그에서 타일 메타({kind, passable, rgb, count, layers}) 조회."""
    return _tiles_by_id(tileset_id).get(base_id)


def _rgb_dist(a: list[int], b: list[int]) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def find_variants(
    tileset_id: int,
    base_id: int,
    max_dist: float = 60.0,
    floor_only: bool = True,
    limit: int = 8,
) -> list[int]:
    """base_id 와 같은 의미군(색군·kind·통행성 동일 + 색거리 이내)의 변형 타일 목록.

    예: 잔디(2816) → [2816, 3008, ...] 같은 초록 A2 통과 바닥들.
    floor_only=True 면 레이어0(바닥)에 쓰인 것만. 결과는 빈도순, 자기 자신 포함, 최대 limit개.
    """
    tiles = _tiles_by_id(tileset_id)
    base = tiles.get(base_id)
    if not base:
        return [base_id]
    bg = color_group(base["rgb"])
    out: list[tuple[int, int]] = []  # (base_id, count)
    for e in tiles.values():
        if e["kind"] != base["kind"] or e["passable"] != base["passable"]:
            continue
        if color_group(e["rgb"]) != bg:
            continue
        if floor_only and e["layers"].get("0", 0) == 0:
            continue
        if _rgb_dist(base["rgb"], e["rgb"]) <= max_dist:
            out.append((e["base_id"], e["count"]))
    if not out:
        return [base_id]
    out.sort(key=lambda t: -t[1])  # 빈도순
    ids = [bid for bid, _ in out[:limit]]
    if base_id not in ids:
        ids.insert(0, base_id)
    return ids
