"""Vignette(장면 스탬프) — 샘플맵에서 사람이 구성한 '완결된 장면'을 통째로 오려
다른 맵에 찍는다.

기존 palette 의 multitile 은 단일 레이어(주로 L1) 오브젝트였다. vignette 는 6레이어
전부(바닥·오토타일·장식·그림자·통행)를 함께 오려 붙여, 집 한 채처럼 '사람이 배치한
구성' 자체를 재사용한다. 절차 생성기가 발명 못 하는 L2(장면 구성)를 상속받는 길.

- extract(): 샘플맵 sub-rectangle → Vignette (6레이어 스탬프).
- stamp():   대상 data 의 (x,y) 에 vignette 을 겹쳐 붙인다(빈칸은 건너뜀).
- render_vignette(): 스탬프 단독 PNG 미리보기(추출 상자 검증용).

canonical: docs/rpgmaker/tile_rendering.md, tile_palette.md
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from agent.generation.mapgen import palette as pal
from agent.generation.mapgen.autotile import base_of
from agent.generation.mapgen.tile_constants import get_tile, make_empty_data, set_tile

_LAYERS = 6
_WATER_LO, _WATER_HI = 2048, 2815  # A1 물 범위(배경 취급)


def classify_theme(base_id: int, tileset_id: int) -> str:
    """배경 지형 base_id → 바이옴 테마(집을 어울리는 바닥 위에 두기 위한 태그)."""
    name = None
    for n, b in pal.terrain_map(tileset_id).items():
        if b == base_id:
            name = n.lower()
            break
    if not name:
        return "grassland"
    if "snow" in name:
        return "snow"
    if "sand" in name or "desert" in name:
        return "desert"
    if "swamp" in name or "reed" in name or "lily" in name or "poison" in name:
        return "wetland"
    return "grassland"


@dataclass
class Vignette:
    """오려낸 장면 스탬프. layers[layer] 는 h*w 평면 배열(행 우선)."""

    name: str
    width: int
    height: int
    tileset_id: int
    layers: list[list[int]]  # 길이 6, 각 원소 길이 width*height
    theme: str = field(default="grassland")  # 어울리는 바닥 바이옴(발굴 시 태깅)
    kind: str = field(default="structure")  # 종류(structure/nature/decor …)

    @property
    def size_class(self) -> str:
        """구조물 크기 등급 — 집(small)·건물(medium)·대형 랜드마크(large).

        상점·여관·시청·성·신전은 대부분 크기로 갈린다(의미 라벨은 추측이라 크기로 티어링).
        """
        s = max(self.width, self.height)
        if s <= 8:
            return "small"
        if s <= 13:
            return "medium"
        return "large"

    def cell(self, layer: int, x: int, y: int) -> int:
        return self.layers[layer][y * self.width + x]

    def _bg_base(self) -> int:
        """L0 에서 가장 흔한 지형 base = '배경 지형'(스탬프에서 버릴 대상·테마 판정)."""
        c = Counter(base_of(v) for v in self.layers[0] if v)
        return c.most_common(1)[0][0] if c else -1

    def structural_mask(self) -> set[tuple[int, int]]:
        """'건물 실루엣' 셀 집합 = (지붕/벽 or 상위레이어 오브젝트) 중 최대덩어리 + 가로채움.

        집은 지붕(A3)·벽(A4) 외에 처마·눈지붕·창·문이 상위 레이어의 B/C 오브젝트로 구성되는
        경우가 많다(snow/desert 마을) → '지붕/벽 or 상위 오브젝트가 있는 셀'을 건물로 본다
        (지붕이 B/C 여도 안 잘림). 순수 지면(A2/A1/A5 + 상위 없음)은 제외 → 집 주변 길·바닥은
        안 물어온다. 가장 큰 8-연결 덩어리만 취해 옆에 떨어진 나무·울타리 조각을 버리고, 그
        덩어리의 행별 가로 min~max 를 채워 문·창 구멍을 메운다. (마이너가 이미 타이트 bbox 로
        뽑으므로 여기선 잔여 지면·측면 잡물만 정리하면 된다.)
        """
        keep: set[tuple[int, int]] = set()
        for y in range(self.height):
            for x in range(self.width):
                is_bld = 4352 <= self.cell(0, x, y) <= 8191  # A3 지붕 + A4 벽
                has_obj = any(self.cell(ly, x, y) for ly in (1, 2, 3))
                if is_bld or has_obj:
                    keep.add((x, y))
        core = self._largest_component(keep)
        if not core:
            return keep
        rows: dict[int, list[int]] = defaultdict(list)
        for x, y in core:
            rows[y].append(x)
        filled: set[tuple[int, int]] = set()
        for y, xs in rows.items():
            for x in range(min(xs), max(xs) + 1):
                filled.add((x, y))
        return filled

    @staticmethod
    def _largest_component(cells: set[tuple[int, int]]) -> set[tuple[int, int]]:
        """셀 집합의 8-연결 컴포넌트 중 가장 큰 것만 반환(외톨이 조각 제거)."""
        seen: set[tuple[int, int]] = set()
        best: set[tuple[int, int]] = set()
        for s in cells:
            if s in seen:
                continue
            comp: set[tuple[int, int]] = set()
            stack = [s]
            seen.add(s)
            while stack:
                cx, cy = stack.pop()
                comp.add((cx, cy))
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        t = (cx + dx, cy + dy)
                        if t in cells and t not in seen:
                            seen.add(t)
                            stack.append(t)
            if len(comp) > len(best):
                best = comp
        return best

    def door_anchor(self, mask: set[tuple[int, int]] | None = None) -> tuple[int, int]:
        """집 정면(앞) 진입 앵커 = 실루엣의 가로 중앙 × 최하단 행(길을 여기로 잇는다)."""
        m = mask if mask is not None else self.structural_mask()
        if not m:
            return self.width // 2, self.height - 1
        max_y = max(y for _, y in m)
        cols = [x for x, y in m if y == max_y]
        return (min(cols) + max(cols)) // 2, max_y


def _load_map(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def extract(map_path: str | Path, x0: int, y0: int, x1: int, y1: int, name: str) -> Vignette:
    """샘플맵 [x0..x1] × [y0..y1] (양끝 포함) 사각형을 6레이어 통째로 오려낸다."""
    d = _load_map(map_path)
    W, H = d["width"], d["height"]
    data = d["data"]
    w, h = x1 - x0 + 1, y1 - y0 + 1
    layers: list[list[int]] = []
    for layer in range(_LAYERS):
        plane = [0] * (w * h)
        for yy in range(h):
            for xx in range(w):
                plane[yy * w + xx] = get_tile(data, x0 + xx, y0 + yy, W, H, layer)
        layers.append(plane)
    return Vignette(name=name, width=w, height=h, tileset_id=d.get("tilesetId", 2), layers=layers)


def stamp(
    data: list[int],
    W: int,
    H: int,
    vig: Vignette,
    x: int,
    y: int,
    layers: tuple[int, ...] = (0, 1, 2, 3, 4, 5),
    mask: set[tuple[int, int]] | None = None,
) -> set[tuple[int, int]]:
    """대상 data 의 (x,y) 좌상단에 vignette 을 실루엣 마스크로 겹쳐 붙인다.

    mask(structural_mask) 밖 셀(주변 잔디·시냇물 조각)은 건너뛰어 집 실루엣만 찍힌다.
    벽/지붕/문/그림자/통행이 원본 그대로 옮겨져 '사람이 배치한 집'이 그대로 재현된다.
    반환: 실제로 찍힌 대상 좌표 집합(footprint — occupied 보호용).
    """
    m = mask if mask is not None else vig.structural_mask()
    placed: set[tuple[int, int]] = set()
    top = min((yy for _, yy in m), default=0)  # 지붕 상단 — 캡 뒤 배경 잔재(고양이 귀) 위치
    for (xx, yy) in m:
        has_obj = any(vig.cell(ly, xx, yy) for ly in (1, 2, 3))
        for layer in layers:
            v = vig.cell(layer, xx, yy)
            if v == 0:
                continue
            # 지붕 상단 2줄에서 L0 가 배경(A5/A1물/A2지면)이고 위에 지붕 캡이 있으면 L0 를 안
            # 깐다 → 투명한 지붕 끝단 뒤로 필드(눈·잔디)가 비쳐 '고양이 귀'(물·바위 조각)가 사라짐.
            if layer == 0 and has_obj and yy <= top + 1 and 1536 <= v <= 4351:
                continue
            set_tile(data, x + xx, y + yy, W, H, layer, v)
        placed.add((x + xx, y + yy))
    return placed


def render_vignette(vig: Vignette, out_path: str, base_game: str | Path, cell: int = 24) -> None:
    """스탬프 단독을 실제 타일셋으로 렌더(추출 상자가 깔끔한지 눈으로 확인)."""
    from agent.generation.mapgen.tile_renderer import render_data_to_png

    data = make_empty_data(vig.width, vig.height)
    for layer in range(_LAYERS):
        for yy in range(vig.height):
            for xx in range(vig.width):
                set_tile(data, xx, yy, vig.width, vig.height, layer, vig.cell(layer, xx, yy))
    render_data_to_png(
        data, vig.width, vig.height, vig.tileset_id, Path(base_game), out_path, cell=cell
    )
