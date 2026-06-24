"""오토타일 shape 보정 — 절차적으로 깐 base_id 맵에 자연스러운 이음새를 적용.

생성기는 지형을 base_id(shape=0) 단일값으로 깐다. 이 모듈이 각 칸의 8방향 이웃을
보고 올바른 shape 를 계산해 tile_id = base_id + shape 로 교정한다.

shape lookup 은 샘플맵에서 데이터 기반 추출(build_autotile_table.py)한
data/autotile_floor_shapes.json 을 사용한다.

적용 범위: A2 지면 오토타일(2816~4351). A1(물)/A4(벽)은 후속 확장.
canonical: docs/rpgmaker/tile_rendering.md §3, docs/rpgmaker/tile_palette.md
"""

import json
import logging
from functools import lru_cache
from pathlib import Path

from agent.generation.mapgen.tile_constants import get_tile, set_tile

logger = logging.getLogger(__name__)

_LOOKUP_PATH = Path(__file__).parent / "data" / "autotile_floor_shapes.json"

# floor 오토타일 적용 범위
_A1_LO = 2048  # A1 시작 (짝수 kind 만 FLOOR, 홀수는 WATERFALL)
_A2_LO, _A2_HI = 2816, 4351  # A2 지면 전체
# 8방향: N E S W NE SE SW NW (bit 0~7) — build_autotile_table.py 와 동일 순서
_DIRS = [(0, -1), (1, 0), (0, 1), (-1, 0), (1, -1), (1, 1), (-1, 1), (-1, -1)]


def base_of(tile_id: int) -> int:
    """오토타일 tile_id 의 kind 시작 ID(shape=0)."""
    return tile_id - ((tile_id - 2048) % 48) if tile_id >= 2048 else tile_id


def corner_mask(raw: int) -> int:
    """raw 8비트 연결 마스크 → 코너 정규화 마스크.

    코너(대각) 비트는 양옆 두 변이 모두 연결일 때만 유효 (RPG Maker 규칙).
    """
    n, e, s, w = raw & 1, (raw >> 1) & 1, (raw >> 2) & 1, (raw >> 3) & 1
    ne, se, sw, nw = (raw >> 4) & 1, (raw >> 5) & 1, (raw >> 6) & 1, (raw >> 7) & 1
    ne = ne if (n and e) else 0
    se = se if (s and e) else 0
    sw = sw if (s and w) else 0
    nw = nw if (n and w) else 0
    return n | e << 1 | s << 2 | w << 3 | ne << 4 | se << 5 | sw << 6 | nw << 7


@lru_cache(maxsize=1)
def _load_lookup() -> dict[int, int]:
    """autotile_floor_shapes.json → {corner_mask: shape}."""
    raw = json.loads(_LOOKUP_PATH.read_text(encoding="utf-8"))
    return {int(k): int(v) for k, v in raw["table"].items()}


def is_floor_autotile(tile_id: int) -> bool:
    """FLOOR_AUTOTILE_TABLE 을 쓰는 오토타일인지.

    - A2 지면 전체 (2816~4351)
    - A1 의 짝수 kind (물 등; 홀수 kind 는 폭포=WATERFALL 이라 제외)
    둘 다 동일한 floor shape 규칙을 공유하므로 같은 lookup 을 적용한다.
    """
    if _A2_LO <= tile_id <= _A2_HI:
        return True
    if _A1_LO <= tile_id < _A2_LO:
        return ((tile_id - 2048) // 48) % 2 == 0
    return False


def compute_shape(raw_mask: int) -> int:
    """raw 8비트 연결 마스크 → shape(0~47). 미등록 패턴은 0(채움)으로 폴백."""
    return _load_lookup().get(corner_mask(raw_mask), 0)


def apply_autotile(
    data: list[int],
    width: int,
    height: int,
    layer: int = 0,
    oob_connected: bool = True,
) -> int:
    """레이어의 A2 floor 오토타일 칸에 shape 를 in-place 적용. 교정한 칸 수 반환.

    base_of() 가 shape 를 제거하므로 in-place 수정이 같은 패스의 이웃 판정에
    영향을 주지 않는다(이웃의 base 는 불변).

    oob_connected=True 이면 맵 밖을 같은 지형의 연속으로 간주(가장자리가 깔끔).
    """
    changed = 0
    for y in range(height):
        for x in range(width):
            t = get_tile(data, x, y, width, height, layer)
            if not is_floor_autotile(t):
                continue
            base = base_of(t)
            mask = 0
            for bit, (dx, dy) in enumerate(_DIRS):
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and 0 <= ny < height:
                    nb = get_tile(data, nx, ny, width, height, layer)
                    connected = is_floor_autotile(nb) and base_of(nb) == base
                else:
                    connected = oob_connected
                if connected:
                    mask |= 1 << bit
            new_tile = base + compute_shape(mask)
            if new_tile != t:
                set_tile(data, x, y, width, height, layer, new_tile)
                changed += 1
    return changed
