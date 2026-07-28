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
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from agent.generation.mapgen import palette as pal
from agent.generation.mapgen.autotile import base_of
from agent.generation.mapgen.tile_constants import get_tile, make_empty_data, set_tile

_LAYERS = 6
_WATER_LO, _WATER_HI = 2048, 2815  # A1 물 범위(배경 취급)


def fill_enclosed(shape: set[tuple[int, int]], w: int, h: int) -> set[tuple[int, int]]:
    """실루엣의 **둘러싸인 빈칸**만 채워 넣는다(문·창·안뜰). 바깥은 절대 안 건드린다.

    사각형 채움(행별 min~max)과의 결정적 차이: 테두리에서 4방향 flood 로 '바깥'을 표시하고
    남은 칸만 구멍으로 본다. 옆집·울타리·주변 지면은 언제나 바깥과 이어져 있으니 실루엣에
    절대 안 들어온다. 반대로 성 안뜰(이끼 마당·독 장판)은 건물에 둘러싸여 있으니 남는다 —
    그 마당은 '누출'이 아니라 그 구조물의 디자인이다.
    """
    outside: set[tuple[int, int]] = set()
    stack = [
        (x, y)
        for x in range(w)
        for y in (0, h - 1)
        if (x, y) not in shape
    ] + [
        (x, y)
        for y in range(h)
        for x in (0, w - 1)
        if (x, y) not in shape
    ]
    outside.update(stack)
    while stack:
        cx, cy = stack.pop()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            t = (cx + dx, cy + dy)
            if 0 <= t[0] < w and 0 <= t[1] < h and t not in shape and t not in outside:
                outside.add(t)
                stack.append(t)
    return shape | {
        (x, y) for y in range(h) for x in range(w) if (x, y) not in shape and (x, y) not in outside
    }


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
    # 정밀 실루엣(로컬 좌표). 마이너가 소스맵 문맥을 보고 계산해 넣는다 — 사각형이 아니라
    # 구조물의 실제 모양이라 모서리 탑이 안 잘리고 옆집 조각도 안 딸려온다. None 이면
    # structural_mask() 가 잘라낸 사각형만 보고 추정한다(수제 vignette 하위호환).
    shape: set[tuple[int, int]] | None = field(default=None)
    # 원본에서 이 구조물을 **둘러싸고 있던** 바닥 지형 [(팔레트 지형명, 칸수), ...] 내림차순.
    # 스탬프는 그 지형을 옮기지 않지만(윤곽선 지면 제외), "이 성은 원래 흙 마당 위에
    # 있었다"는 사실은 배치 힌트로 가치가 있다 — 어울리는 바닥을 깔아주면 자연스러워진다.
    ground: tuple[tuple[str, int], ...] = field(default=())

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

    @property
    def preferred_ground(self) -> str | None:
        """이 구조물이 원본에서 딛고 있던 대표 지형 이름(팔레트 terrain). 없으면 None."""
        return self.ground[0][0] if self.ground else None

    @property
    def apron_ground(self) -> str | None:
        """마당(플라자)에 깔기 적합한 지형 — 원본 주변 지형 중 **밟고 설 수 있는** 1순위.

        1순위를 그냥 쓰면 안 된다: 성은 해자(poison_water), 물가 오두막은 연못(water_pool)이
        1순위다. 그대로 깔면 건물을 물로 두르고 진입로가 끊긴다. 통행 불가 지형은 물론이고
        **A1(물·용암 등 액체) 전체**를 뺀다 — poison_water 처럼 통행 가능으로 등록된 액체도
        마당 재질로는 부적합하다. 남는 게 없으면 None(테마 기본 길로 폴백).
        """
        blocked = pal.impassable_ids(self.tileset_id)
        for name, _ in self.ground:
            tid = pal.get_tile_id(self.tileset_id, name)
            if tid and tid not in blocked and not (_WATER_LO <= tid <= _WATER_HI):
                return name
        return None

    def ground_affinity(self, terrain: str) -> float:
        """주어진 지형이 이 구조물에 얼마나 어울리는가(0~1). 원본 주변 지형 비율 그대로.

        '눈밭에 사막 성' 같은 배치를 고를 때 가중치로 쓴다(테마 태그보다 해상도가 높다 —
        같은 grassland 라도 흙 마당 성과 잔디 마당 오두막은 어울리는 바닥이 다르다).
        """
        total = sum(n for _, n in self.ground)
        if not total:
            return 0.0
        return next((n for t, n in self.ground if t == terrain), 0) / total

    def cell(self, layer: int, x: int, y: int) -> int:
        return self.layers[layer][y * self.width + x]

    def _bg_base(self) -> int:
        """L0 에서 가장 흔한 지형 base = '배경 지형'(스탬프에서 버릴 대상·테마 판정)."""
        c = Counter(base_of(v) for v in self.layers[0] if v)
        return c.most_common(1)[0][0] if c else -1

    def structural_mask(self) -> set[tuple[int, int]]:
        """'건물 실루엣' 셀 집합. 마이너가 넣어준 정밀 shape 가 있으면 그걸 쓴다.

        shape 는 소스맵 문맥에서 계산된 **구조물의 실제 모양**이다(코어 → 부속물 확장 →
        내부 구멍만 메움). 사각형 bbox 를 그대로 쓰면 튀어나온 모서리 탑·기둥이 잘리고
        옆집·울타리 조각이 딸려오는데, shape 는 그걸 원천 차단한다.

        shape 가 없는 수제 vignette 은 잘라낸 사각형만 보고 추정한다: '지붕/벽 or 상위
        오브젝트' 셀 중 최대 8-연결 덩어리 + **둘러싸인 구멍**만 메운다(문·창·안뜰).
        예전엔 행별 가로 min~max 를 채웠는데, 그게 같은 행에 있는 남의 집까지 실루엣으로
        빨아들이는 원인이었다 — 구멍 메우기는 바깥과 연결된 칸을 절대 안 건드린다.
        """
        if self.shape is not None:
            return set(self.shape)
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
        return fill_enclosed(core, self.width, self.height)

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

    **윤곽선 칸의 바닥(L0)은 안 옮긴다.** 모서리 탑·처마 같은 부속물은 상위 레이어
    오브젝트라 그 아래 L0 는 원본 마당 흙일 뿐인데, 스프라이트의 투명한 부분으로 그 흙이
    비쳐 대상 맵에 원본 지형 조각이 남는다. 반대로 **둘러싸인 내부**(성 안뜰의 이끼·독
    장판)는 그 구조물의 디자인이므로 그대로 옮긴다 — 안/밖 구분은 실루엣이 이미 안다.
    (원본 지형 정보는 vignette.ground 메타데이터로 따로 남아 배치 힌트로 쓰인다.)

    반환: 실제로 찍힌 대상 좌표 집합(footprint — occupied 보호용).
    """
    m = mask if mask is not None else vig.structural_mask()
    placed: set[tuple[int, int]] = set()
    for (xx, yy) in m:
        # 4방향 중 하나라도 실루엣 밖이면 윤곽선 칸(바깥 공기와 닿아 있다)
        edge = any((xx + dx, yy + dy) not in m for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
        for layer in layers:
            v = vig.cell(layer, xx, yy)
            if v == 0:
                continue
            if layer == 0 and edge and not (4352 <= v <= 8191):
                continue  # 윤곽선의 지면(A1/A2/A5) — 대상 맵 바닥에 양보. 지붕·벽은 유지.
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
