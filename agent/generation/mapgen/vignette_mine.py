"""Vignette 자동 발굴 — 전 샘플맵을 훑어 '집·건물' 장면을 자동으로 오려 라이브러리화.

원리: L0 에서 건물 타일(지붕 A3 4352~5887, 벽 A4 5888~8191)의 연결 컴포넌트를 찾아
각 덩어리의 bounding box(+여백)를 vignette 후보로 뽑는다. 맵 테두리 벽(가장자리를 두르는
거대 프레임)과 얇은 울타리는 제외한다. LLM 이 blueprint 에서 고를 '집 재료 창고'를 만든다.

CLI:
    uv run python -m agent.generation.mapgen.vignette_mine --tileset 2 --out mined_houses.json
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from agent.generation.mapgen.vignette import Vignette, classify_theme, extract

_ROOF_LO, _ROOF_HI = 4352, 5887  # A3 지붕
_WALL_LO, _WALL_HI = 5888, 8191  # A4 벽

# 눈으로 검증한 '완전한 구조물' 화이트리스트(CURATE_candidates.png 리뷰). 자동 필터로 못
# 거르는 조각·벽·void 를 배제하고 확실히 깨끗한 것만. 이름=소스맵_s{컴포넌트index}(안정적).
# 자동 발굴은 후보 생성기로 쓰고, 신뢰가 필요한 라이브러리는 이 목록을 쓴다.
CURATED_STRUCTURES: frozenset[str] = frozenset({
    # ── grassland ──
    # 소형 집
    "Map017_s4", "Map182_s12", "Map010_s6", "Map135_s1", "Map123_s0",
    # 중형 건물·신전
    "Map182_s4", "Map006_s6", "Map010_s1", "Map172_s0",
    # 대형 랜드마크
    "Map021_s1", "Map020_s1", "Map030_s0",  # 성·담쟁이성·탑
    # ── snow ──
    "Map009_s2", "Map009_s4", "Map009_s5",  # 눈 여관·집·스테인드글라스집
    # ── desert ──
    "Map270_s0", "Map173_s1",  # 피라미드·사막건물
})


def mine_curated(samplemaps_dir: str | Path, tileset_id: int = 2) -> list[Vignette]:
    """검증된 CURATED_STRUCTURES 만 발굴해 반환(품질 보장 라이브러리)."""
    return [v for v in mine_dir(samplemaps_dir, tileset_id) if v.name in CURATED_STRUCTURES]


def _is_building(v: int) -> bool:
    return _ROOF_LO <= v <= _WALL_HI  # 지붕+벽 통합 범위


def _components(mask: list[list[bool]], W: int, H: int) -> list[list[tuple[int, int]]]:
    """8-연결 컴포넌트 라벨링(건물 덩어리 = 집 한 채)."""
    seen = [[False] * W for _ in range(H)]
    comps: list[list[tuple[int, int]]] = []
    for sy in range(H):
        for sx in range(W):
            if not mask[sy][sx] or seen[sy][sx]:
                continue
            comp: list[tuple[int, int]] = []
            q = deque([(sx, sy)])
            seen[sy][sx] = True
            while q:
                x, y = q.popleft()
                comp.append((x, y))
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nx, ny = x + dx, y + dy
                        if 0 <= nx < W and 0 <= ny < H and mask[ny][nx] and not seen[ny][nx]:
                            seen[ny][nx] = True
                            q.append((nx, ny))
            comps.append(comp)
    return comps


def _map_theme(data: list[int], W: int, H: int, tileset_id: int) -> str:
    """소스맵 전체의 지배 A2 지형 → 바이옴 테마(집별 로컬 bbox 는 오분류하므로 맵 단위로 판정)."""
    from collections import Counter

    from agent.generation.mapgen.autotile import base_of

    N = W * H
    c = Counter(base_of(v) for v in data[:N] if 2816 <= v <= 4351)  # A2 지면만
    if not c:
        return "grassland"
    return classify_theme(c.most_common(1)[0][0], tileset_id)


def mine_map(
    map_path: str | Path,
    *,
    min_side: int = 4,
    max_side: int = 26,
    min_fill: float = 0.18,
    edge_touch_reject: bool = True,
) -> list[Vignette]:
    """한 샘플맵에서 '구조물'(집·상점·여관·시청·성·신전·탑…) 을 **타이트하게** 추출.

    지붕(A3)+벽(A4) 연결 컴포넌트가 구조물의 코어다. bbox 는 코어에 딱 맞추되(옆·아래 여백
    0 — 주변 길·바닥·울타리·나무를 안 물어옴), **위로만** 지붕 처마·눈덮개(상위 레이어 B/C)가
    이어지는 만큼 확장한다(그래야 지붕 윗부분이 안 잘림). 종류 라벨은 타일만으론 추측이라
    크기로 티어링(size_class). 테마는 소스맵 전체 지형으로 태깅(로컬 오분류 방지).

    필터: 코어 bbox 변 길이 min_side..max_side, 채움률 >= min_fill, 가장자리 접촉·void 제외.
    """
    d = json.loads(Path(map_path).read_text(encoding="utf-8"))
    W, H = d["width"], d["height"]
    data = d["data"]
    N = W * H
    L0 = data[:N]
    theme = _map_theme(data, W, H, d.get("tilesetId", 2))
    foliage = _foliage_ids(d.get("tilesetId", 2))
    mask = [[_is_building(L0[y * W + x]) for x in range(W)] for y in range(H)]

    def upper_in_row(y: int, x0: int, x1: int) -> bool:  # 그 행에 상위레이어(지붕 처마) 있나
        return any(
            0 < data[ly * N + y * W + x] < 8192 and data[ly * N + y * W + x]
            for ly in (1, 2, 3)
            for x in range(x0, x1 + 1)
        )

    def solidity(x0: int, y0: int, x1: int, y1: int) -> float:
        """bbox 안에서 '건물다운' 셀(지붕/벽 or 식생 아닌 오브젝트) 비율.

        낮으면 마을 조각·농장(밭·연못·나무가 많음)이 한 덩어리로 병합된 것 → 단일 구조물 아님.
        """
        solid = tot = 0
        for yy in range(y0, y1 + 1):
            for xx in range(x0, x1 + 1):
                tot += 1
                if _is_building(L0[yy * W + xx]):
                    solid += 1
                    continue
                up = next((data[ly * N + yy * W + xx] for ly in (1, 2, 3) if data[ly * N + yy * W + xx]), 0)
                if up and up not in foliage:
                    solid += 1
        return solid / max(1, tot)

    out: list[Vignette] = []
    stem = Path(map_path).stem
    for i, comp in enumerate(_components(mask, W, H)):
        xs = [x for x, _ in comp]
        ys = [y for _, y in comp]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        if not (min_side <= bw <= max_side and min_side <= bh <= max_side):
            continue
        if len(comp) / (bw * bh) < min_fill:
            continue
        if edge_touch_reject and (x0 == 0 or y0 == 0 or x1 == W - 1 or y1 == H - 1):
            continue
        void = sum(1 for yy in range(y0, y1 + 1) for xx in range(x0, x1 + 1) if L0[yy * W + xx] == 0)
        if void / (bw * bh) > 0.22:
            continue
        if solidity(x0, y0, x1, y1) < 0.55:
            continue  # 밭·연못·나무 섞인 마을 조각 — 단일 구조물 아님, 제외
        if bw / bh > 1.9:
            continue  # 가로로 긴 납작한 것 = 성벽·울타리 조각(탑·집은 세로/정방형이라 통과)
        # 위로만 확장: 지붕 처마/눈덮개(상위 레이어)가 이어지는 행을 최대 2줄 포함.
        # (2줄 제한 — 무한 확장하면 집 뒤 나무 캐노피 기둥을 타고 올라간다. 처마는 1~2줄.)
        ty = y0
        while ty > 0 and y0 - ty < 2 and upper_in_row(ty - 1, x0, x1):
            ty -= 1
        vig = extract(map_path, x0, ty, x1, y1, f"{stem}_s{i}")
        vig.kind = "structure"
        vig.theme = theme
        out.append(vig)
    return out


# 자립 장식물(분수·우물·석상·표지판·노점 등) — 벽/지붕 아닌 통행불가 B/C 오브젝트 군집.
_DECOR_LO, _DECOR_HI = 1, 1023  # B~E 시트 단일 오브젝트 id


def _foliage_ids(tileset_id: int) -> set[int]:
    """식생(나무·덤불·풀·꽃) 오브젝트 id 집합 — 장식 발굴에서 나무 클러스터를 걸러낸다.

    palette 의 나무 계열 멀티타일 전 구성타일 + 이름에 풀/나무/덤불이 든 단일 오브젝트.
    (나무·풀은 절차 배치가 따로 하므로 vignette 로는 안 뽑는다.)
    """
    from agent.generation.mapgen import palette as pal

    ts = pal.get_tileset(tileset_id) or {}
    ids: set[int] = set()
    for name, mt in (ts.get("multitile") or {}).items():
        if isinstance(mt, dict) and any(k in name for k in ("tree", "palm", "bush", "shrub")):
            for row in mt.get("tiles", []):
                ids.update(int(t) for t in row if t)
    kw = (
        "tree", "grass", "bush", "flower", "reed", "shrub", "tuft", "plant", "palm", "berry",
        "wheat", "crop", "field", "vine", "ivy", "moss", "leaf", "hedge", "farm", "garden",
    )
    for name, obj in (ts.get("objects") or {}).items():
        if isinstance(obj, dict) and any(k in name for k in kw):
            ids.add(int(obj["base_id"]))
    return ids


def mine_decor(
    map_path: str | Path,
    *,
    min_cells: int = 3,
    max_side: int = 6,
    min_fill: float = 0.4,
) -> list[Vignette]:
    """구조물이 아닌 '자립 장식 오브젝트 군집'(분수·우물·석상·노점·표지판)을 추출.

    L1~L3 의 B/C 오브젝트가 뭉친 작고 조밀한 덩어리를 vignette(kind='decor')로 뽑는다.
    건물(지붕/벽) 셀에 인접한 군집은 건물 부속이라 제외(집은 mine_map 담당).
    나무 1그루·바위 1개 같은 단발은 min_cells 로 걸러, 배치된 '구조물스러운' 것만 남긴다.
    """
    d = json.loads(Path(map_path).read_text(encoding="utf-8"))
    W, H = d["width"], d["height"]
    data = d["data"]
    N = W * H
    L0 = data[:N]
    theme = _map_theme(data, W, H, d.get("tilesetId", 2))
    foliage = _foliage_ids(d.get("tilesetId", 2))
    obj = [[False] * W for _ in range(H)]
    fol = [[False] * W for _ in range(H)]
    near_bld = [[False] * W for _ in range(H)]
    for y in range(H):
        for x in range(W):
            if _is_building(L0[y * W + x]):
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        if 0 <= x + dx < W and 0 <= y + dy < H:
                            near_bld[y + dy][x + dx] = True
    for y in range(H):
        for x in range(W):
            for ly in (1, 2, 3):
                v = data[ly * N + y * W + x]
                if _DECOR_LO <= v <= _DECOR_HI:
                    obj[y][x] = True
                    if v in foliage:
                        fol[y][x] = True
                    break

    out: list[Vignette] = []
    stem = Path(map_path).stem
    for i, comp in enumerate(_components(obj, W, H)):
        if len(comp) < min_cells:
            continue
        if any(near_bld[y][x] for x, y in comp):
            continue  # 건물 부속 장식은 제외
        if sum(fol[y][x] for x, y in comp) / len(comp) > 0.34:
            continue  # 나무·풀 덩어리는 제외(식생은 절차 배치 담당)
        xs = [x for x, _ in comp]
        ys = [y for _, y in comp]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        if bw > max_side or bh > max_side:
            continue
        if len(comp) / (bw * bh) < min_fill:
            continue
        vig = extract(map_path, x0, y0, x1, y1, f"{stem}_d{i}")
        vig.kind = "decor"
        vig.theme = theme
        out.append(vig)
    return out


def mine_dir(
    samplemaps_dir: str | Path, tileset_id: int = 2, include_decor: bool = False, **kw
) -> list[Vignette]:
    """디렉터리의 모든 tileset_id 샘플맵에서 vignette 을 발굴해 모은다.

    구조물(집·상점·성…)을 기본 추출하고, include_decor=True 면 자립 장식물(분수·우물·석상)도.
    """
    found: list[Vignette] = []
    for p in sorted(Path(samplemaps_dir).glob("Map*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict) or d.get("tilesetId") != tileset_id or not d.get("data"):
            continue
        found.extend(mine_map(p, **kw))
        if include_decor:
            found.extend(mine_decor(p))
    return found
