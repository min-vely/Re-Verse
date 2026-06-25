"""실제 타일셋 PNG 로 맵을 렌더 (데이터 필요).

app/frontend/src/components/game/MapViewer.jsx 의 drawTile/drawAutoTile 을
Python+Pillow 로 포팅. 오토타일 쿼터피스 조합까지 동일하게 처리해
생성 결과의 자연스러운 이음새를 눈으로 확인할 수 있다.

타일셋 이미지(storage/games/base_game/img/tilesets/*.png)는 .gitignore 라 로컬에만 존재.

canonical: docs/rpgmaker/tile_rendering.md §3
"""

import json
import logging
from pathlib import Path
from typing import Any

from agent.generation.mapgen.tile_constants import get_tile

logger = logging.getLogger(__name__)

# ── 오토타일 테이블 (MapViewer.jsx 와 동일, rmmz_core.js 기준) ──
FLOOR_AUTOTILE_TABLE = [
    [[2, 4], [1, 4], [2, 3], [1, 3]], [[2, 0], [1, 4], [2, 3], [1, 3]],
    [[2, 4], [3, 0], [2, 3], [1, 3]], [[2, 0], [3, 0], [2, 3], [1, 3]],
    [[2, 4], [1, 4], [2, 3], [3, 1]], [[2, 0], [1, 4], [2, 3], [3, 1]],
    [[2, 4], [3, 0], [2, 3], [3, 1]], [[2, 0], [3, 0], [2, 3], [3, 1]],
    [[2, 4], [1, 4], [2, 1], [1, 3]], [[2, 0], [1, 4], [2, 1], [1, 3]],
    [[2, 4], [3, 0], [2, 1], [1, 3]], [[2, 0], [3, 0], [2, 1], [1, 3]],
    [[2, 4], [1, 4], [2, 1], [3, 1]], [[2, 0], [1, 4], [2, 1], [3, 1]],
    [[2, 4], [3, 0], [2, 1], [3, 1]], [[2, 0], [3, 0], [2, 1], [3, 1]],
    [[0, 4], [1, 4], [0, 3], [1, 3]], [[0, 4], [3, 0], [0, 3], [1, 3]],
    [[0, 4], [1, 4], [0, 3], [3, 1]], [[0, 4], [3, 0], [0, 3], [3, 1]],
    [[2, 2], [1, 2], [2, 3], [1, 3]], [[2, 2], [1, 2], [2, 3], [3, 1]],
    [[2, 2], [1, 2], [2, 1], [1, 3]], [[2, 2], [1, 2], [2, 1], [3, 1]],
    [[2, 4], [3, 4], [2, 3], [3, 3]], [[2, 4], [3, 4], [2, 1], [3, 3]],
    [[2, 0], [3, 4], [2, 3], [3, 3]], [[2, 0], [3, 4], [2, 1], [3, 3]],
    [[2, 4], [1, 4], [2, 5], [1, 5]], [[2, 0], [1, 4], [2, 5], [1, 5]],
    [[2, 4], [3, 0], [2, 5], [1, 5]], [[2, 0], [3, 0], [2, 5], [1, 5]],
    [[0, 4], [3, 4], [0, 3], [3, 3]], [[2, 2], [1, 2], [2, 5], [1, 5]],
    [[0, 2], [1, 2], [0, 3], [1, 3]], [[0, 2], [1, 2], [0, 3], [3, 1]],
    [[2, 2], [3, 2], [2, 3], [3, 3]], [[2, 2], [3, 2], [2, 1], [3, 3]],
    [[2, 4], [3, 4], [2, 5], [3, 5]], [[2, 0], [3, 4], [2, 5], [3, 5]],
    [[0, 4], [1, 4], [0, 5], [1, 5]], [[0, 4], [3, 0], [0, 5], [1, 5]],
    [[0, 2], [3, 2], [0, 3], [3, 3]], [[0, 2], [1, 2], [0, 5], [1, 5]],
    [[0, 4], [3, 4], [0, 5], [3, 5]], [[2, 2], [3, 2], [2, 5], [3, 5]],
    [[0, 2], [3, 2], [0, 5], [3, 5]], [[0, 0], [1, 0], [0, 1], [1, 1]],
]
WALL_AUTOTILE_TABLE = [
    [[2, 2], [1, 2], [2, 1], [1, 1]], [[0, 2], [1, 2], [0, 1], [1, 1]],
    [[2, 0], [1, 0], [2, 1], [1, 1]], [[0, 0], [1, 0], [0, 1], [1, 1]],
    [[2, 2], [3, 2], [2, 1], [3, 1]], [[0, 2], [3, 2], [0, 1], [3, 1]],
    [[2, 0], [3, 0], [2, 1], [3, 1]], [[0, 0], [3, 0], [0, 1], [3, 1]],
    [[2, 2], [1, 2], [2, 3], [1, 3]], [[0, 2], [1, 2], [0, 3], [1, 3]],
    [[2, 0], [1, 0], [2, 3], [1, 3]], [[0, 0], [1, 0], [0, 3], [1, 3]],
    [[2, 2], [3, 2], [2, 3], [3, 3]], [[0, 2], [3, 2], [0, 3], [3, 3]],
    [[2, 0], [3, 0], [2, 3], [3, 3]], [[0, 0], [3, 0], [0, 3], [3, 3]],
]
WATERFALL_AUTOTILE_TABLE = [
    [[2, 0], [1, 0], [2, 1], [1, 1]], [[0, 0], [1, 0], [0, 1], [1, 1]],
    [[2, 0], [3, 0], [2, 1], [3, 1]], [[0, 0], [3, 0], [0, 1], [3, 1]],
]

_QUARTER = 24  # 오토타일 쿼터피스 크기(px)
_TS = 48  # 타일 1칸 크기(px)


def load_tileset_images(tileset_id: int, base_game: Path) -> list[Any]:
    """tileset_id 의 tilesetNames 에 따라 [A1,A2,A3,A4,A5,B,C,D,E] 이미지 로드.

    빈 슬롯/없는 파일은 None. 반환 길이 9.
    """
    from PIL import Image

    ts_json = json.loads((base_game / "data" / "Tilesets.json").read_text(encoding="utf-8"))
    names: list[str] = []
    for ts in ts_json:
        if isinstance(ts, dict) and ts.get("id") == tileset_id:
            names = ts.get("tilesetNames", [])
            break
    img_dir = base_game / "img" / "tilesets"
    images: list[Any] = []
    for nm in (names + [""] * 9)[:9]:
        if nm:
            p = img_dir / f"{nm}.png"
            images.append(Image.open(p).convert("RGBA") if p.exists() else None)
        else:
            images.append(None)
    return images


def _draw_autotile(canvas, img, bx, by, shape, table, cell, dx, dy) -> None:
    from PIL import Image

    t = table[shape % len(table)]
    scale = cell / _TS
    dw = max(1, round(_QUARTER * scale))
    for i, (qsx, qsy) in enumerate(t):
        sx = (bx * 2 + qsx) * _QUARTER
        sy = (by * 2 + qsy) * _QUARTER
        piece = img.crop((sx, sy, sx + _QUARTER, sy + _QUARTER))
        if dw != _QUARTER:
            piece = piece.resize((dw, dw), Image.NEAREST)
        dstx, dsty = dx + (i % 2) * dw, dy + (i // 2) * dw
        canvas.paste(piece, (dstx, dsty), piece)


def _draw_tile(canvas, tile_id, dx, dy, cell, images) -> None:
    """타일 1개 렌더 (MapViewer.jsx drawTile 포팅)."""
    from PIL import Image

    if tile_id == 0:
        return
    gk = (tile_id - 2048) // 48
    shape = (tile_id - 2048) % 48
    tx, ty = gk % 8, gk // 8

    if 2048 <= tile_id < 2816:  # A1
        img = images[0]
        if img is None:
            return
        if gk == 0:
            bx, by, table = 0, 0, FLOOR_AUTOTILE_TABLE
        elif gk == 1:
            bx, by, table = 0, 3, FLOOR_AUTOTILE_TABLE
        elif gk == 2:
            bx, by, table = 6, 0, FLOOR_AUTOTILE_TABLE
        elif gk == 3:
            bx, by, table = 6, 3, FLOOR_AUTOTILE_TABLE
        else:
            bx = (tx // 4) * 8
            by = ty * 6 + (tx // 2 % 2) * 3
            if gk % 2 == 0:
                table = FLOOR_AUTOTILE_TABLE
            else:
                bx += 6
                table = WATERFALL_AUTOTILE_TABLE
        _draw_autotile(canvas, img, bx, by, shape, table, cell, dx, dy)
    elif 2816 <= tile_id < 4352:  # A2
        img = images[1]
        if img is None:
            return
        _draw_autotile(canvas, img, tx * 2, (ty - 2) * 3, shape, FLOOR_AUTOTILE_TABLE, cell, dx, dy)
    elif 4352 <= tile_id < 5888:  # A3
        img = images[2]
        if img is None:
            return
        _draw_autotile(canvas, img, tx * 2, (ty - 6) * 2, shape, WALL_AUTOTILE_TABLE, cell, dx, dy)
    elif 5888 <= tile_id < 8192:  # A4
        img = images[3]
        if img is None:
            return
        by = int((ty - 10) * 2.5 + (0.5 if ty % 2 == 1 else 0))
        table = WALL_AUTOTILE_TABLE if ty % 2 == 1 else FLOOR_AUTOTILE_TABLE
        _draw_autotile(canvas, img, tx * 2, by, shape, table, cell, dx, dy)
    elif 1536 <= tile_id < 2048:  # A5
        img = images[4]
        if img is None:
            return
        sx = ((tile_id // 128 % 2) * 8 + tile_id % 8) * _TS
        sy = (tile_id % 256 // 8 % 16) * _TS
        piece = img.crop((sx, sy, sx + _TS, sy + _TS)).resize((cell, cell), Image.NEAREST)
        canvas.paste(piece, (dx, dy), piece)
    elif 0 <= tile_id < 1024:  # B~E
        img = images[5 + tile_id // 256]
        if img is None:
            return
        sx = ((tile_id // 128 % 2) * 8 + tile_id % 8) * _TS
        sy = (tile_id % 256 // 8 % 16) * _TS
        piece = img.crop((sx, sy, sx + _TS, sy + _TS)).resize((cell, cell), Image.NEAREST)
        canvas.paste(piece, (dx, dy), piece)


def render_data_to_png(
    data: list[int],
    width: int,
    height: int,
    tileset_id: int,
    base_game: Path,
    out_path: str,
    cell: int = 24,
) -> None:
    """맵 data(6레이어)를 실제 타일셋으로 렌더해 PNG 저장.

    레이어 순서: 바닥(L0) → 그림자(L4) → 오브젝트(L1~L3). 그림자는 바닥 위·오브젝트 아래.
    """
    from PIL import Image

    images = load_tileset_images(tileset_id, base_game)
    canvas = Image.new("RGBA", (width * cell, height * cell), (0, 0, 0, 255))
    for layer in range(4):
        if layer == 1:  # 바닥(L0) 직후, 오브젝트(L1~) 직전에 그림자
            _draw_shadows(canvas, data, width, height, cell)
        for y in range(height):
            for x in range(width):
                tid = get_tile(data, x, y, width, height, layer)
                if tid:
                    _draw_tile(canvas, tid, x * cell, y * cell, cell, images)
    canvas.convert("RGB").save(out_path)


def _draw_shadows(canvas: Any, data: list[int], width: int, height: int, cell: int) -> None:
    """L4 그림자 레이어를 연하고 흐릿한 반투명 검정 사분면으로 그린다(RPG Maker 그림자펜).

    그림자 값은 4비트 마스크: bit0(1)=좌상, bit1(2)=우상, bit2(4)=좌하, bit3(8)=우하.
    각 비트가 켜진 사분면(cell/2)을 칠한다(예: 5=0b0101=왼쪽 절반). 약한 가우시안 블러로
    가장자리를 흐릿하게 해 example1~3 처럼 자연스럽게 만든다.
    """
    from PIL import Image, ImageDraw, ImageFilter

    half = cell // 2
    quads = {1: (0, 0), 2: (half, 0), 4: (0, half), 8: (half, half)}
    overlay = Image.new("RGBA", (width * cell, height * cell), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for y in range(height):
        for x in range(width):
            s = get_tile(data, x, y, width, height, 4)
            if not s:
                continue
            for bit, (ox, oy) in quads.items():
                if s & bit:
                    px, py = x * cell + ox, y * cell + oy
                    draw.rectangle([px, py, px + half - 1, py + half - 1], fill=(0, 0, 0, 110))
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=max(0.8, cell * 0.04)))  # 살짝 흐릿
    canvas.alpha_composite(overlay)
