"""타일 팔레트 로더 — tile_palette.json 단일 진실 공급원(SSOT).

절차적 맵 생성기가 "지형 의미(grass/water/wall…) → 타일 ID"를 조회할 때 사용한다.
기존 tile_constants.TOWN_TILES/DUNGEON_TILES 는 하드코딩 상수였으나, 점진적으로
이 팔레트로 이관한다.

canonical: docs/rpgmaker/tile_palette.md
data: agent/generation/mapgen/data/tile_palette.json
"""

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PALETTE_PATH = Path(__file__).parent / "data" / "tile_palette.json"


@lru_cache(maxsize=1)
def load_palette() -> dict[str, Any]:
    """tile_palette.json 을 한 번만 로드해서 캐시한다.

    filter.load_metadata 의 lru_cache 패턴과 동일.
    """
    raw: dict[str, Any] = json.loads(_PALETTE_PATH.read_text(encoding="utf-8"))
    return raw


def get_tileset(tileset_id: int) -> dict[str, Any] | None:
    """tileset_id 의 팔레트 블록 반환 (없으면 None)."""
    palette = load_palette()
    return palette.get("tilesets", {}).get(str(tileset_id))


def get_terrain(tileset_id: int, name: str) -> dict[str, Any] | None:
    """tileset_id + 지형 이름 → terrain 항목({base_id, layer, passable, note}).

    예: get_terrain(2, "grass") → {"base_id": 2816, "layer": 0, "passable": True, ...}
    찾지 못하면 None.
    """
    ts = get_tileset(tileset_id)
    if not ts:
        logger.warning("palette: tileset_id=%s 정의 없음", tileset_id)
        return None
    terrain = ts.get("terrain", {}).get(name)
    if terrain is None:
        logger.warning("palette: tileset_id=%s 에 지형 '%s' 없음", tileset_id, name)
    return terrain


def get_tile_id(tileset_id: int, name: str, default: int = 0) -> int:
    """지형 이름 → base_id 만 바로 반환 (조회 실패 시 default).

    절차적 생성기에서 set_tile 호출 시 가장 자주 쓰는 헬퍼.
    """
    terrain = get_terrain(tileset_id, name)
    if terrain is None:
        return default
    return int(terrain.get("base_id", default))


def terrain_map(tileset_id: int) -> dict[str, int]:
    """tileset 의 {지형이름: base_id} 평면 매핑 반환 (tile_constants 호환용)."""
    ts = get_tileset(tileset_id)
    if not ts:
        return {}
    return {name: int(t["base_id"]) for name, t in ts.get("terrain", {}).items()}


def impassable_ids(tileset_id: int) -> set[int]:
    """tileset 에서 passable=false 인 모든 base_id 집합 반환.

    tile_constants 의 TOWN_IMPASSABLE/DUNGEON_IMPASSABLE 대체용.
    """
    ts = get_tileset(tileset_id)
    if not ts:
        return set()
    return {
        int(t["base_id"]) for t in ts.get("terrain", {}).values() if not t.get("passable", True)
    }


def get_object(tileset_id: int, name: str) -> dict[str, Any] | None:
    """tileset_id + 오브젝트 이름 → object 항목({base_id, layer, passable}).

    objects 는 terrain 과 별개인 B/C 시트 단일 타일(나무·풀·꽃 등). 없으면 None.
    """
    ts = get_tileset(tileset_id)
    if not ts:
        return None
    return ts.get("objects", {}).get(name)


def get_ground_decor(tileset_id: int, name: str) -> dict[str, Any] | None:
    """tileset_id + 데코 이름 → ground_decor 항목({base_id, passable, note}). 없으면 None.

    ground_decor 는 바닥(L0) 위 L1 에 패치로 겹쳐 까는 A2 지면 텍스처(풀밭·마른풀·눈).
    오토타일 대상이라 생성기가 군집 배치 후 apply_autotile 로 경계를 다듬는다.
    """
    ts = get_tileset(tileset_id)
    if not ts:
        return None
    return ts.get("ground_decor", {}).get(name)


def get_multitile(tileset_id: int, name: str) -> dict[str, Any] | None:
    """tileset_id + 멀티타일 이름 → {tiles[행][열], blocked[행][열]}. 없으면 None.

    여러 칸 오브젝트(나무 2x2 등). 단일 타일 objects 와 별개.
    """
    ts = get_tileset(tileset_id)
    if not ts:
        return None
    return ts.get("multitile", {}).get(name)


def multitile_names(tileset_id: int) -> list[str]:
    """tileset_id 의 멀티타일 이름 목록(_로 시작하는 메타 키 제외).

    POI·가구 등 멀티타일을 이름 prefix 로 골라 쓸 때(예: 거대 구조물 POI 풀) 사용한다.
    """
    ts = get_tileset(tileset_id)
    if not ts:
        return []
    return [n for n in ts.get("multitile", {}) if not n.startswith("_")]


def kind_of(tile_id: int) -> str:
    """tile_id 가 속한 타일셋 시트 종류(A1~A5/BCDE) 반환.

    encoding.ranges 기준. 어떤 범위에도 안 들면 "unknown".
    canonical: docs/rpgmaker/tile_rendering.md §2.
    """
    ranges: dict[str, list[int]] = load_palette().get("encoding", {}).get("ranges", {})
    for kind, (lo, hi) in ranges.items():
        if lo <= tile_id <= hi:
            return kind
    return "unknown"


def normalize_autotile_id(tile_id: int) -> int:
    """오토타일 tile_id 를 shape=0 기준 base_id(kind 시작값)로 정규화.

    A1~A4(>=2048)만 shape 가 있다. shape = (id-2048) % 48 을 빼서 kind 시작 ID 로 만든다.
    A5/BCDE(<2048) 및 0 은 그대로 반환.
    """
    base = load_palette().get("encoding", {}).get("autotile_base", 2048)
    span = load_palette().get("encoding", {}).get("shapes_per_kind", 48)
    if tile_id < base:
        return tile_id
    return tile_id - ((tile_id - base) % span)
