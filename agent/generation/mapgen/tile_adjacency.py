"""타일 인접 규칙 로더 — 어떤 지형 옆에 어떤 지형이 와야 자연스러운가(데이터 기반).

build_tile_adjacency.py 가 샘플맵 전수조사로 만든 tile_adjacency.json 을 읽어,
생성기가 "각 지형을 데이터상 호환되는 이웃으로 둘러싸도록" 질의에 쓴다.
오토타일 가장자리가 타일에 구워져 있어, 호환 안 되는 인접은 어색해 보인다.

canonical: docs/rpgmaker/tile_palette.md
"""

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PATH = Path(__file__).parent / "data" / "tile_adjacency.json"


@lru_cache(maxsize=1)
def load_adjacency() -> dict[str, Any]:
    """tile_adjacency.json 1회 로드."""
    return json.loads(_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=64)
def _tiles(tileset_id: int) -> dict[str, Any]:
    ts = load_adjacency().get("tilesets", {}).get(str(tileset_id))
    return ts["tiles"] if ts else {}


def ground_neighbors(tileset_id: int, base: int) -> dict[int, float]:
    """base 의 지면 이웃 분포 {neighbor_base: fraction}. 데이터 없으면 빈 dict."""
    info = _tiles(tileset_id).get(str(base))
    if not info:
        return {}
    return {int(k): v for k, v in info["ground"].items()}


def compatible(tileset_id: int, a: int, b: int, thresh: float = 0.04) -> bool:
    """a 와 b 가 인접해도 자연스러운가 — 한쪽 지면이웃 분포에서 thresh 이상이면 OK.

    같은 base 는 항상 OK. 데이터에 a·b 둘 다 없으면(미관측) 보수적으로 True(막지 않음).
    """
    if a == b:
        return True
    # A5(1536~2047)·B~E(0~1023) 단일/오브젝트 타일은 가장자리가 구워져 있지 않아 충돌 없음
    if a < 2048 or b < 2048:
        return True
    ga, gb = ground_neighbors(tileset_id, a), ground_neighbors(tileset_id, b)
    if not ga and not gb:
        return True
    return ga.get(b, 0.0) >= thresh or gb.get(a, 0.0) >= thresh


def best_bridge(tileset_id: int, a: int, b: int, thresh: float = 0.04) -> int | None:
    """a 와 b 사이에 끼울 전이 지형 — a·b 양쪽과 호환되는 지형 중 최적(min 분포 최대).

    예: 호환 안 되는 두 지형 사이에 데이터가 추천하는 중간 지형을 삽입. 없으면 None.
    """
    if compatible(tileset_id, a, b, thresh):
        return None
    ga, gb = ground_neighbors(tileset_id, a), ground_neighbors(tileset_id, b)
    cands = set(ga) & set(gb)
    best, best_score = None, 0.0
    for t in cands:
        if t in (a, b):
            continue
        score = min(ga.get(t, 0.0), gb.get(t, 0.0))
        if score > best_score:
            best, best_score = t, score
    return best if best_score >= thresh else None
