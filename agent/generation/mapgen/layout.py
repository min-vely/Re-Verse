"""레이아웃 플래너 — 구조물 vignette 목록을 '겹치지 않게, 밀도에 맞게' 배치한다.

기존 방식(고정 슬롯)의 문제: 배치를 좌표 상수로 박아두면 vignette 실제 크기를 모른다.
소형 집(6x6)과 대형 랜드마크(25x27)가 같은 슬롯 표를 쓰니 큰 것을 넣는 순간 옆 슬롯을
덮어쓰고 맵 밖으로 넘친다. 더 근본적으로 **맵 크기를 고정해두면 큰 랜드마크가 들어올 때
건물 점유율이 물리적으로 불가능한 값이 된다**(46x30 맵 + 25x27 성 = 66%). 그래서
"맵에 건물을 맞추는" 대신 **건물에 맵을 맞춘다**.

절차:
  1. `plan_canvas` — 배치할 건물들의 실제 bbox 합 ÷ 목표 밀도 로 필요한 맵 크기를 역산하고,
     가장 큰 건물이 들어갈 위치에 큰길(street) 줄을 잡는다.
  2. `pack` — 큰 것부터 점유격자에 스캔 배치. 건물 사이 margin, 맵 가장자리 edge, 큰길 2줄,
     그리고 **문 앞→큰길 진입로 통로**까지 예약해 길이 옆 건물을 관통하지 않게 한다.
  3. 목표 밀도에 못 미치면 filler 풀을 순환하며 빈 곳을 채운다(큰 공백 제거).

밀도는 여유(margin)를 포함한 점유율로 잰다 — 실제로 자리를 먹는 건 건물+간격이라서.
canonical: docs/rpgmaker/tile_rendering.md
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from agent.generation.mapgen.vignette import Vignette

_EDGE = 2  # 맵 가장자리 여백(나무 경계가 들어갈 자리)
_MARGIN = 2  # 건물 사이 최소 간격(플라자 pad 1칸 + 여유 1칸)
_STREET_H = 2  # 큰길 두께(compile_blueprint 와 동일)
_W_RANGE = (30, 90)  # 생성 맵 폭 허용 범위
_H_RANGE = (24, 70)  # 생성 맵 높이 허용 범위
# 기본 목표 밀도(건물 bbox + 여유 기준). tileset2 샘플맵 55개 실측: 지붕/벽 점유율
# 중앙값 38.6%·p25 22.6% → bbox 기준으로 환산해 '여유 있는 마을' 쪽인 0.30 을 채택.
_DENSITY = 0.30


def _slot(w: int, h: int, margin: int) -> int:
    """건물 하나가 실제로 먹는 면적 — 본체 + 플라자 pad 1칸 + 간격."""
    return (w + margin + 1) * (h + margin + 1)


def footprint(vig: Vignette) -> tuple[int, int, int, int]:
    """구조물 실루엣의 (offset_x, offset_y, 폭, 높이).

    vignette 원점이 아니라 **실루엣 bbox** 기준으로 배치해야 크기 계산이 맞는다
    (마이너가 위로 2줄 확장해둔 처마 여백 등이 원점 쪽에 붙어 있기 때문).
    """
    m = vig.structural_mask()
    if not m:
        return 0, 0, vig.width, vig.height
    xs = [x for x, _ in m]
    ys = [y for _, y in m]
    return min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


@dataclass
class Layout:
    """패킹 결과 — 그대로 compile_blueprint 에 넣을 수 있는 형태."""

    width: int
    height: int
    street_y: int
    vignettes: dict[str, Vignette] = field(default_factory=dict)
    houses: list[dict] = field(default_factory=list)
    props: list[dict] = field(default_factory=list)
    gardens: list[list[int]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)  # 자리가 없어 못 넣은 것
    density: float = 0.0  # 실제 달성 점유율(여유 포함)


class _Occupancy:
    """점유 격자 + 누적합. 임의 사각형이 비었는지 O(1) 로 검사한다.

    맵 하나에 후보 위치가 수천 개인데 매번 사각형을 훑으면 대형 건물에서 수백만 번
    비교가 된다 → 누적합으로 상수시간. 배치마다 한 번씩만 다시 굽는다.
    """

    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.g = bytearray(w * h)
        self._ps: list[int] = []
        self._dirty = True

    def mark(self, x0: int, y0: int, x1: int, y1: int) -> None:
        for y in range(max(0, y0), min(self.h, y1 + 1)):
            row = y * self.w
            for x in range(max(0, x0), min(self.w, x1 + 1)):
                self.g[row + x] = 1
        self._dirty = True

    def _sums(self) -> list[int]:
        if self._dirty:
            w, h, g = self.w, self.h, self.g
            ps = [0] * ((w + 1) * (h + 1))
            for y in range(h):
                run = 0
                for x in range(w):
                    run += g[y * w + x]
                    ps[(y + 1) * (w + 1) + x + 1] = ps[y * (w + 1) + x + 1] + run
            self._ps, self._dirty = ps, False
        return self._ps

    def free(self, x0: int, y0: int, x1: int, y1: int) -> bool:
        """사각형이 전부 비었나. 맵 밖으로 삐져나온 부분은 잘라서(clamp) 본다.

        (margin 을 붙인 검사 상자는 가장자리에서 맵을 넘는 게 정상 — 건물 본체가
        범위 안인지는 호출부가 스캔 범위로 이미 보장한다.)
        """
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.w - 1, x1), min(self.h - 1, y1)
        if x1 < x0 or y1 < y0:
            return True
        ps, w1 = self._sums(), self.w + 1
        total = (
            ps[(y1 + 1) * w1 + x1 + 1]
            - ps[y0 * w1 + x1 + 1]
            - ps[(y1 + 1) * w1 + x0]
            + ps[y0 * w1 + x0]
        )
        return total == 0


def plan_canvas(
    boxes: list[tuple[int, int]],
    *,
    density: float = _DENSITY,
    margin: int = _MARGIN,
    edge: int = _EDGE,
) -> tuple[int, int, int]:
    """배치할 건물 크기 목록 → (W, H, street_y). 맵을 내용에 맞춘다.

    - 면적: 건물 slot 합 ÷ 목표밀도. 가로:세로 ≈ 1.6:1 (마을은 가로로 긴 편이 보기 좋다).
    - street_y: 가장 큰 건물이 큰길 **위** 밴드에 여유(margin)까지 포함해 통째로 들어가야
      한다. 여기서 margin 을 빼먹으면 랜드마크가 어디에도 못 앉아 통째로 탈락한다.
    - 세로는 위 밴드(최대 건물) + 큰길 + 아래 밴드(두 번째 건물)가 다 들어갈 만큼 확보한다.
    """
    if not boxes:
        boxes = [(8, 8)]
    area = sum(_slot(w, h, margin) for w, h in boxes) / max(0.05, min(0.9, density))
    inset = edge + margin  # 건물이 놓일 수 있는 첫 줄/열
    widest = max(w for w, _ in boxes)
    heights = sorted((h for _, h in boxes), reverse=True)
    h_top = heights[0]
    h_bot = heights[1] if len(heights) > 1 else min(h_top, 8)

    width = max(round((area * 1.6) ** 0.5), widest + 2 * inset)
    width = max(_W_RANGE[0], min(_W_RANGE[1], width))
    street_y = inset + h_top + margin
    lower = _STREET_H + margin + h_bot + margin + edge  # 큰길 + 아래 밴드
    height = max(_H_RANGE[0], min(_H_RANGE[1], max(round(area / width), street_y + lower)))
    # 맵 높이가 상한에 걸리면 큰길을 위로 당긴다(그만큼 최대 건물이 탈락할 수 있다).
    street_y = max(inset + 4, min(street_y, height - lower))
    return width, height, street_y


def pack(
    wish: list[Vignette],
    *,
    filler: list[Vignette] = (),
    props: list[Vignette] = (),
    density: float = _DENSITY,
    margin: int = _MARGIN,
    edge: int = _EDGE,
    seed: int = 0,
    fill_gaps: bool = True,
) -> Layout:
    """구조물 목록을 겹치지 않게 배치하고 목표 밀도까지 채운다.

    wish   — LLM 기획이 요구한 구성(랜드마크·신전·집). 큰 것부터 우선 배치.
    filler — 밀도가 모자랄 때 순환하며 채울 여분 집 풀(보통 소형 집들).
    props  — 길 연결이 필요 없는 소품(석상·우물). 마지막에 좁은 여유로 끼워 넣는다.
    """
    rng = random.Random(seed)
    wish = sorted(wish, key=lambda v: -(footprint(v)[2] * footprint(v)[3]))
    boxes = [footprint(v)[2:] for v in wish]
    W, H, street_y = plan_canvas(boxes, density=density, margin=margin, edge=edge)

    out = Layout(width=W, height=H, street_y=street_y)
    occ = _Occupancy(W, H)
    occ.mark(0, 0, W - 1, edge - 1)  # 가장자리 예약(나무 경계)
    occ.mark(0, H - edge, W - 1, H - 1)
    occ.mark(0, 0, edge - 1, H - 1)
    occ.mark(W - edge, 0, W - 1, H - 1)
    occ.mark(0, street_y, W - 1, street_y + _STREET_H - 1)  # 큰길

    used = 0  # 여유 포함 점유 면적(밀도 측정용)
    target = density * W * H
    seq = 0

    def try_place(vig: Vignette, gap: int, lane: bool) -> bool:
        """vig 를 가장 좋은 빈자리에 놓는다. 성공하면 occ 갱신 + houses/props 기록."""
        nonlocal used, seq
        ox, oy, w, h = footprint(vig)
        if w + 2 * edge > W or h + 2 * edge > H:
            return False
        best: tuple[tuple[int, int], int, int, list[tuple[int, int, int, int]]] | None = None
        for y in range(edge, H - edge - h + 1):
            for x in range(edge, W - edge - w + 1):
                if not occ.free(x - gap, y - gap, x + w - 1 + gap, y + h - 1 + gap):
                    continue
                lanes = _approach(vig, x, y, ox, w, h, W, street_y, occ) if lane else []
                if lanes is None:
                    continue
                # 큰길에 가까울수록, 그다음 왼쪽부터 — 자연히 길 양옆으로 늘어선다.
                to_street = street_y - (y + h) if y + h <= street_y else y - (street_y + _STREET_H)
                score = (to_street, x)
                if best is None or score < best[0]:
                    best = (score, x, y, lanes)
        if best is None:
            return False
        _, x, y, lanes = best
        occ.mark(x - 1, y - 1, x + w, y + h)  # 본체 + 플라자 pad 1칸
        for lx0, ly0, lx1, ly1 in lanes:
            occ.mark(lx0, ly0, lx1, ly1)
        seq += 1
        key = f"{vig.name}#{seq}"
        out.vignettes[key] = vig
        (out.houses if lane else out.props).append({"vignette": key, "x": x - ox, "y": y - oy})
        used += _slot(w, h, margin)
        return True

    for vig in wish:
        if not try_place(vig, margin, lane=True):
            out.dropped.append(vig.name)

    if fill_gaps and filler:
        # 커서를 성공/실패 상관없이 전진시킨다 — 안 그러면 같은 집만 계속 복제된다.
        # 채우기는 기획 규모의 2배를 넘지 않게 막는다(밀도 때문에 마을이 딴 게 되면 곤란).
        pool = list(filler)
        rng.shuffle(pool)
        cap = max(2, len(wish))
        cur = misses = added = 0
        while used < target and misses < len(pool) and added < cap:
            if try_place(pool[cur % len(pool)], margin, lane=True):
                added, misses = added + 1, 0
            else:
                misses += 1
            cur += 1

    for p in props:  # 소품은 간격 1칸이면 충분(길 연결 없음)
        try_place(p, 1, lane=False)

    # 캔버스를 실제 배치 범위로 잘라낸다. 면적 역산은 건물이 사각형을 고르게 채운다고
    # 가정하지만 '거대 랜드마크 1 + 작은 집 여럿' 은 그렇게 안 깔린다 → 남는 오른쪽·아래를
    # 그대로 두면 맵 절반이 빈 벌판이 된다. 배치는 왼쪽·큰길 쪽부터 채우므로 우/하만 자른다.
    out.width, out.height = _trim(out, W, H, edge + margin, street_y)
    out.density = used / (out.width * out.height)
    out.gardens = _free_rects(occ, out.width, out.height, edge, rng)
    return out


def _trim(lay: Layout, W: int, H: int, inset: int, street_y: int) -> tuple[int, int]:
    """배치된 것들의 우/하 끝 + 여백으로 맵을 줄인다(좌표는 그대로 유효)."""
    ends_x, ends_y = [inset + 8], [street_y + _STREET_H + 4]
    for spec in (*lay.houses, *lay.props):
        vig = lay.vignettes[spec["vignette"]]
        ox, oy, w, h = footprint(vig)
        ends_x.append(spec["x"] + ox + w - 1)
        ends_y.append(spec["y"] + oy + h - 1)
    return min(W, max(ends_x) + 1 + inset), min(H, max(ends_y) + 1 + inset)


def _approach(
    vig: Vignette,
    x: int,
    y: int,
    ox: int,
    w: int,
    h: int,
    W: int,
    street_y: int,
    occ: _Occupancy,
) -> list[tuple[int, int, int, int]] | None:
    """문 앞 → 큰길 진입로(폭 2)가 지나갈 통로. 막혀 있으면 None(그 자리는 탈락).

    compile_blueprint 가 door_anchor 열에서 큰길까지 세로로 길을 깐다. 그 통로를 미리
    예약해두지 않으면 길이 옆 건물을 관통해 지나간다.
    """
    ax, _ = vig.door_anchor()
    cx = x - ox + ax  # 문 열(맵 좌표)
    if cx < 0 or cx + 1 >= W:
        return None
    if y + h <= street_y:  # 큰길 위 — 건물 아래로 내려간다
        y0, y1 = y + h, street_y - 1
    else:  # 큰길 아래 — 건물 위로 올라간다(+ 아래로 한 칸 삐져나오는 꼬리)
        y0, y1 = street_y + 2, y - 1
    rects: list[tuple[int, int, int, int]] = []
    if y1 >= y0:
        if not occ.free(cx, y0, cx + 1, y1):
            return None
        rects.append((cx, y0, cx + 1, y1))
    if y + h > street_y:
        rects.append((cx, y + h, cx + 1, y + h))
    return rects


def _free_rects(
    occ: _Occupancy, W: int, H: int, edge: int, rng: random.Random, limit: int = 2
) -> list[list[int]]:
    """남은 빈 공간에서 정원으로 쓸 사각형을 찾는다(큰 것부터, 최대 limit 개)."""
    out: list[list[int]] = []
    for gw, gh in ((12, 6), (9, 5), (6, 4)):
        for y in range(edge, H - edge - gh + 1, 2):
            for x in range(edge, W - edge - gw + 1, 2):
                if not occ.free(x, y, x + gw - 1, y + gh - 1):
                    continue
                occ.mark(x, y, x + gw - 1, y + gh - 1)
                out.append([x, y, x + gw - 1, y + gh - 1])
                if len(out) >= limit:
                    return out
    return out
