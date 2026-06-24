"""palette 기반 테스트 생성기 (실험용, 기존 town_generator 와 독립).

검증된 tile_palette.json 의 지형 ID 로 타일을 한 칸씩 깔아 마을풍 맵을 만든다.
기존 town_generator.py 의 로직은 건드리지 않고, palette 가 실제 생성에 쓰일 수 있는지
확인하기 위한 별도 경로다.

CLI (미리보기 PNG + 통계):
    uv run python -m agent.generation.mapgen.palette_mapgen --width 30 --height 30 --out preview.png

canonical: docs/rpgmaker/tile_palette.md
"""

import argparse
import random
from dataclasses import dataclass
from pathlib import Path

from agent.generation.mapgen import palette as pal
from agent.generation.mapgen.autotile import apply_autotile, base_of
from agent.generation.mapgen.tile_constants import (
    get_tile,
    make_empty_data,
    set_tile,
)

# 미리보기용 지형→RGB 색 (실제 타일 그림이 아니라 도식화)
_PREVIEW_COLORS: dict[str, tuple[int, int, int]] = {
    "grass": (104, 168, 76),
    "dirt": (150, 111, 70),
    "sand": (216, 198, 138),
    "snow": (235, 240, 245),
    "gravel": (150, 150, 150),
    "stone_path": (110, 110, 115),
    "water": (66, 122, 200),
    "wall": (60, 60, 64),
}


@dataclass
class MapDims:
    """MapSpec 없이도 돌릴 수 있는 최소 입력."""

    width: int
    height: int
    tileset_id: int = 2


# ── terrain 모드: value noise 고도맵 기반 자연 지형 ──────────────────────────

# 바이옴별 고도 밴드 → 지형 (낮을수록 물/저지, 높을수록 고지).
# 각 항목 = (상한 임계값, 지형 이름). 마지막 임계값은 1.0 초과(모든 고도 커버).
# ⚠️ 밴드 인접 규칙은 추측 금지 — 샘플맵 전수조사(tile_adjacency.json)로 검증한다.
# tileset2 실측: 이름붙은 지형 중 호환 인접쌍은 water↔grass, grass↔gravel 뿐.
# sand/dirt/snow 는 서로·잔디와 안 섞이고 자기 변형·구조물(A4)하고만 인접한다.
# 따라서 바이옴 = "지배 지형 1종 + 호환 물 + 호환 보조"의 호환 체인으로만 구성한다.
# (연속 밴드쌍이 tile_adjacency.compatible 을 통과하는지 test 로 강제)
_BIOMES: dict[str, list[tuple[float, str]]] = {
    # 물(잔디물가 2048) → 잔디평원 → 자갈고지. (water-grass, grass-gravel 모두 호환)
    "grassland": [(0.30, "water"), (0.86, "grass"), (1.01, "gravel")],
    # 모래사막 단일 지형(모래는 다른 지면과 안 섞임 — 변형·오브젝트로 다양성).
    "desert": [(1.01, "sand")],
    # 얼음호수(눈물가 물 2240) → 눈평원. (2240-snow 호환)
    "snow": [(0.30, "water"), (1.01, "snow")],
    # 물 많은 습지: 물 → 잔디. (water-grass 호환)
    "wetland": [(0.50, "water"), (1.01, "grass")],
    # SF외곽 도시(tileset 5): 물 → 잔디공원 → 도로 → 포장 → 타일 (road/pavement 일부 A5)
    "city": [(0.25, "water"), (0.40, "grass"), (0.70, "road"), (0.86, "pavement"), (1.01, "tile")],
}
DEFAULT_BIOME = "grassland"

# 가장자리(물가 등)가 타일에 구워진 지형 — 변형(다른 오토타일 패밀리) 시 가장자리 색이
# 이웃과 충돌하므로 변형 제외하고 캐노니컬 타일만 쓴다. (A1 물 계열이 대표적)
_NO_VARIANT_TERRAINS: frozenset[str] = frozenset({"water"})

# 바이옴별 물 패밀리(base_id). 물 타일의 구워진 물가 색이 둘레 지형과 맞아야 자연스럽다.
# 샘플맵 실측: 2048=잔디물가, 2240=눈물가. 미지정 바이옴은 palette 기본(2048) 사용.
_BIOME_WATER: dict[str, int] = {"snow": 2240}


def _water_id(biome: str, tid: int) -> int:
    """바이옴에 맞는 물 base_id (물가 색이 둘레 지형과 일치하도록)."""
    return _BIOME_WATER.get(biome, pal.get_tile_id(tid, "water"))

# 바이옴 → [(오브젝트 이름, 배치 대상 지형, 밀도)]. 오브젝트는 palette objects 에 정의.
_BIOME_OBJECTS: dict[str, list[tuple[str, str, float]]] = {
    # 나무(184~186)·덤불(176)은 멀티타일이라 제외. 단일타일 식생·바위로만 구성.
    "grassland": [
        ("grass_tuft", "grass", 0.060),
        ("flower", "grass", 0.030),
        ("berry_bush", "grass", 0.015),
        ("rock", "grass", 0.008),
    ],
    # 오아시스(grass)엔 풀·꽃, 사막(sand)엔 죽은 나무·바위가 드물게
    "desert": [
        ("grass_tuft", "grass", 0.080),
        ("flower", "grass", 0.025),
        ("dead_tree", "sand", 0.012),
        ("desert_rock", "sand", 0.010),
    ],
    "snow": [("rock", "snow", 0.010)],
    "wetland": [
        ("grass_tuft", "grass", 0.050),
        ("flower", "grass", 0.025),
    ],
    # 던전(tileset 4): 방 바닥(floor) 위에 바위·종유석
    "dungeon": [
        ("pebbles", "floor", 0.030),
        ("rock", "floor", 0.020),
        ("big_rock", "floor", 0.010),
        ("stalagmite", "floor", 0.015),
    ],
    # 용암던전(tileset 4): 화산 바닥(lava_floor) 위에 바위 (용암 위엔 배치 안 됨)
    "lava": [
        ("rock", "lava_floor", 0.020),
        ("big_rock", "lava_floor", 0.012),
        ("stalagmite", "lava_floor", 0.012),
    ],
    # 얼음던전: 얼음동굴 바닥에 바위·종유석
    "ice": [
        ("pebbles", "ice_floor", 0.025),
        ("rock", "ice_floor", 0.015),
        ("stalagmite", "ice_floor", 0.012),
    ],
    # 독던전: 일반 바닥에 바위
    "poison": [
        ("pebbles", "poison_floor", 0.025),
        ("rock", "poison_floor", 0.015),
    ],
    # 모래/수정/이끼/어둠 던전: 각 테마 바닥에 바위
    "sand": [("pebbles", "sand_floor", 0.025), ("rock", "sand_floor", 0.015)],
    "crystal": [("pebbles", "crystal_floor", 0.020), ("rock", "crystal_floor", 0.012)],
    "moss": [("pebbles", "moss_floor", 0.025), ("rock", "moss_floor", 0.015)],
    "dark": [("pebbles", "dark_floor", 0.020), ("rock", "dark_floor", 0.015)],
    # 실내(tileset 3): 마루 위에 가구
    "interior": [
        ("chair", "floor", 0.030),
        ("rug", "floor", 0.020),
        ("barrel", "floor", 0.020),
    ],
    # SF외곽(tileset 5): 도로·포장에 소화전
    "city": [
        ("hydrant", "road", 0.012),
        ("hydrant", "pavement", 0.012),
    ],
    # SF내부(tileset 6): 흰 타일 바닥(tile_floor) 위에 기계·가구
    "sf_interior": [
        ("plant", "tile_floor", 0.030),
        ("cabinet", "tile_floor", 0.020),
        ("toilet", "tile_floor", 0.015),
        ("pipe", "tile_floor", 0.012),
    ],
}

# 바이옴 → [(멀티타일 이름, 대상 지형, 밀도)]. 멀티타일은 palette multitile 에 정의.
_BIOME_MULTITILE: dict[str, list[tuple[str, str, float]]] = {
    "grassland": [("tree", "grass", 0.012)],
    "desert": [("tree", "grass", 0.020)],  # 오아시스 나무
    "snow": [("snow_tree", "snow", 0.015)],
    "wetland": [("tree", "grass", 0.010)],
    "dungeon": [("ice_crystal", "floor", 0.010)],
    # 벽쪽 배치(against_wall)라 후보가 적어 밀도를 높게 잡는다
    "interior": [
        ("bed", "floor", 0.10),
        ("bookshelf", "floor", 0.08),
        ("pillar", "floor", 0.05),
    ],
    # SF외곽: 잔디공원에 벤치·펜스
    "city": [
        ("bench", "grass", 0.020),
        ("fence", "grass", 0.015),
    ],
    "sf_interior": [("railing", "tile_floor", 0.015)],
}


def _value_noise(width: int, height: int, cells: int, seed: int):
    """저해상도 랜덤 그리드 + bilinear 보간으로 부드러운 0~1 노이즈 필드 생성."""
    import numpy as np

    rng = np.random.default_rng(seed)
    grid = rng.random((cells + 1, cells + 1))
    ys = np.linspace(0, cells, height)
    xs = np.linspace(0, cells, width)
    y0 = np.floor(ys).astype(int)
    x0 = np.floor(xs).astype(int)
    y1 = np.minimum(y0 + 1, cells)
    x1 = np.minimum(x0 + 1, cells)
    fy = (ys - y0)[:, None]
    fx = (xs - x0)[None, :]
    g00 = grid[np.ix_(y0, x0)]
    g01 = grid[np.ix_(y0, x1)]
    g10 = grid[np.ix_(y1, x0)]
    g11 = grid[np.ix_(y1, x1)]
    top = g00 * (1 - fx) + g01 * fx
    bot = g10 * (1 - fx) + g11 * fx
    return top * (1 - fy) + bot * fy


def _terr_ids(tid: int) -> dict[str, int]:
    """tileset 의 {지형이름: base_id} 전체 매핑 (배치 대상 지형 조회용)."""
    ts = pal.get_tileset(tid)
    if not ts:
        return {}
    return {n: int(v["base_id"]) for n, v in ts.get("terrain", {}).items()}


def _pure_variants(tid: int, base_id: int) -> list[int]:
    """find_variants 중 '같은 지형의 순수 텍스처 변형'만 — 변형의 지배 이웃 색군이
    base 와 같은 것만 남긴다.

    오토타일 변형(색거리 기반)은 대부분 다른 지형의 전이 타일이다(예: 모래 옆에 오는
    '잔디 변형' 3248 은 실제로 모래 타일). 그런 걸 변형으로 깔면 가장자리 색이 충돌한다.
    샘플맵 인접 데이터로 지배 이웃 색군이 base 와 다른 변형을 걸러낸다. 지면 오토타일은
    대개 패밀리당 텍스처 1개뿐이라 결과가 [base](캐노니컬)로 수렴한다 → 충돌 없음.
    """
    try:
        from agent.generation.mapgen import tile_adjacency as adj
        from agent.generation.mapgen import tile_catalog as tc

        base_meta = tc.get_tile_meta(tid, base_id)
        if not base_meta:
            return [base_id]
        base_grp = tc.color_group(base_meta["rgb"])
        out = [base_id]
        for v in tc.find_variants(tid, base_id, max_dist=48.0):
            if v == base_id:
                continue
            g = adj.ground_neighbors(tid, v)
            if not g:
                continue  # 인접 데이터 없는 희귀 타일은 보수적으로 제외
            dom = max(g, key=lambda k: g[k])
            dmeta = tc.get_tile_meta(tid, dom)
            if dmeta and tc.color_group(dmeta["rgb"]) == base_grp:
                out.append(v)
        return out
    except Exception:
        return [base_id]


def _pick_variant(tid: int, base_id: int, rng: random.Random) -> int:
    """순수 변형(같은 지형 텍스처) 중 하나를 빈도 가중으로 선택(맵당 1회).

    오토타일 충돌을 피하려 칸마다 바꾸지 않고 맵 단위로 고른다. 순수 변형이 없으면
    (지면 오토타일은 대개 그렇다) 원본 base_id 그대로.
    """
    try:
        from agent.generation.mapgen import tile_catalog as tc

        variants = _pure_variants(tid, base_id)
        if len(variants) <= 1:
            return base_id
        weights = [(tc.get_tile_meta(tid, v) or {}).get("count", 1) for v in variants]
        return rng.choices(variants, weights=weights)[0]
    except Exception:
        return base_id


def _variant_set(tid: int, base_id: int) -> set[int]:
    """지형의 순수 변형 base_id 집합 (배치 매칭용 — 변형 바닥 위에도 오브젝트 배치)."""
    return set(_pure_variants(tid, base_id)) | {base_id}


def _assign_region_variants(
    tid: int, base_id: int, n: int, rng: random.Random
) -> list[int]:
    """영역 n개에 변형을 배정 — 영역끼리 되도록 다른 변형(부족하면 순환).

    _pick_variant 의 빈도 가중은 흔한 타일로 쏠려 영역을 나눠도 같은 변형만 나오므로,
    여기선 변형 목록을 셔플해 영역마다 다른 변형을 깐다(맵 내 패치 효과). 변형이
    1개뿐이면 전부 원본. 색이 가까운(rgb≤48) 변형만 모이므로 패치는 부드럽다.
    """
    vs = _pure_variants(tid, base_id)
    if len(vs) <= 1:
        return [base_id] * n
    rng.shuffle(vs)
    return [vs[z % len(vs)] for z in range(n)]


def _zone_map(width: int, height: int, seed: int, n_regions: int) -> list[list[int]]:
    """저주파 노이즈를 n_regions 등분 → 칸별 zone id(0..n_regions-1).

    같은 지형이라도 영역마다 다른 변형을 깔기 위한 구획. 저주파라 큰 덩어리로
    나뉘어, 변형끼리 생기는 오토타일 이음새가 드물다(변형은 색이 가까워 부드러운
    패치로 보인다). 고도맵과 다른 seed 를 써 지형 경계와 변형 경계를 분리한다.
    """
    nz = _value_noise(width, height, cells=max(2, min(width, height) // 16), seed=seed)
    lo = float(nz.min())
    span = float(nz.max()) - lo + 1e-9
    return [
        [min(n_regions - 1, int((float(nz[y, x]) - lo) / span * n_regions)) for x in range(width)]
        for y in range(height)
    ]


def _smooth_idx(idx: list[list[int]], width: int, height: int, passes: int = 2) -> None:
    """밴드 인덱스 격자를 majority 필터로 매끈하게(작은 조각·들쭉날쭉 경계 제거). in-place.

    8방향 중 5칸 이상이 동일 인덱스일 때만 교체 → 큰 덩어리는 유지, 고립 조각은 흡수.
    """
    dirs = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]
    for _ in range(passes):
        snap = [row[:] for row in idx]
        for y in range(height):
            for x in range(width):
                counts: dict[int, int] = {}
                for dx, dy in dirs:
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < width and 0 <= ny < height:
                        v = snap[ny][nx]
                        counts[v] = counts.get(v, 0) + 1
                best = max(counts, key=lambda k: counts[k])
                if counts[best] >= 5 and best != snap[y][x]:
                    idx[y][x] = best


def _relax_transitions(idx: list[list[int]], width: int, height: int) -> None:
    """인접 칸 밴드 인덱스 차이를 ≤1로 강제 — 호환 지형끼리만 인접(전이 보정). in-place.

    idx[c] = min(idx[c], 직교이웃_최소 + 1) 을 수렴까지 반복. 수렴 시 모든 변에서
    |Δidx|≤1 이 보장돼, 물(0)↔잔디(2)처럼 단계를 건너뛴 인접이 사라지고 사이에
    모래(1) 같은 전이 밴드가 자동으로 한 줄 삽입된다(고지가 저지 쪽으로 깎임).
    """
    ortho = [(0, -1), (1, 0), (0, 1), (-1, 0)]
    changed = True
    guard = 0
    while changed and guard < width + height + 8:
        changed = False
        guard += 1
        for y in range(height):
            for x in range(width):
                nb_min = None
                for dx, dy in ortho:
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < width and 0 <= ny < height:
                        v = idx[ny][nx]
                        if nb_min is None or v < nb_min:
                            nb_min = v
                if nb_min is not None and idx[y][x] > nb_min + 1:
                    idx[y][x] = nb_min + 1
                    changed = True


def _place_objects(
    data: list[int], width: int, height: int, tid: int, biome: str, rng: random.Random
) -> None:
    """바이옴별 오브젝트(나무·풀·꽃 등)를 어울리는 지형 위(레이어1)에 확률 배치.

    통행 불가 오브젝트(나무 등)는 레이어5도 막는다. 이미 레이어1이 찬 칸은 건너뛴다.
    """
    specs = _BIOME_OBJECTS.get(biome, [])
    if not specs:
        return
    terr_ids = _terr_ids(tid)
    for name, target, density in specs:
        obj = pal.get_object(tid, name)
        tgt = terr_ids.get(target)
        if not obj or tgt is None:
            continue
        tgt_set = _variant_set(tid, tgt)  # 변형 바닥도 대상에 포함
        for y in range(height):
            for x in range(width):
                if get_tile(data, x, y, width, height, 1) != 0:
                    continue  # 이미 오브젝트 있음
                if base_of(get_tile(data, x, y, width, height, 0)) not in tgt_set:
                    continue
                if rng.random() < density:
                    set_tile(data, x, y, width, height, 1, int(obj["base_id"]))
                    if not obj.get("passable", True):
                        set_tile(data, x, y, width, height, 5, 1)


def _place_multitile(
    data: list[int],
    width: int,
    height: int,
    tid: int,
    biome: str,
    rng: random.Random,
    against_wall: bool = False,
) -> None:
    """바이옴별 멀티타일 오브젝트(나무 2x2 등)를 대상 지형 위에 확률 배치.

    NxM 칸이 전부 대상 지형 + 레이어1 비어있을 때만 배치(겹침 방지).
    blocked=true 칸은 레이어5도 막는다.
    against_wall=True 면 가구 윗줄(y-1)이 전부 벽(통행불가)인 위치에만 배치(벽에 등 댐).
    """
    specs = _BIOME_MULTITILE.get(biome, [])
    if not specs:
        return
    terr_ids = _terr_ids(tid)
    for name, target, density in specs:
        mt = pal.get_multitile(tid, name)
        tgt = terr_ids.get(target)
        if not mt or tgt is None:
            continue
        tiles, blocked = mt["tiles"], mt["blocked"]
        mh, mw = len(tiles), len(tiles[0])
        tgt_set = _variant_set(tid, tgt)  # 변형 바닥도 대상
        for y in range(height - mh + 1):
            for x in range(width - mw + 1):
                fits = all(
                    base_of(get_tile(data, x + dx, y + dy, width, height, 0)) in tgt_set
                    and get_tile(data, x + dx, y + dy, width, height, 1) == 0
                    for dy in range(mh)
                    for dx in range(mw)
                )
                if against_wall:
                    # 가구 윗줄(y-1)이 전부 벽(통행불가)이어야 벽에 등 댄 것으로 간주
                    fits = fits and y > 0 and all(
                        get_tile(data, x + dx, y - 1, width, height, 5) == 1 for dx in range(mw)
                    )
                if not fits or rng.random() >= density:
                    continue
                for dy in range(mh):
                    for dx in range(mw):
                        set_tile(data, x + dx, y + dy, width, height, 1, int(tiles[dy][dx]))
                        if blocked[dy][dx]:
                            set_tile(data, x + dx, y + dy, width, height, 5, 1)


def generate_terrain_map(
    dims: MapDims,
    seed: int = 0,
    autotile: bool = True,
    biome: str = DEFAULT_BIOME,
    objects: bool = True,
    variants: bool = True,
    variant_regions: int = 1,
) -> list[int]:
    """value noise 고도맵으로 바이옴별 지형을 자연스럽게 배치.

    낮은 고도=물/저지, 높은 고도=고지. 바이옴(_BIOMES)이 고도→지형 매핑을 정한다.
    저주파 노이즈로 큰 덩어리를 만들고, majority 스무딩으로 작은 조각을 정리한다.
    """
    w, h, tid = dims.width, dims.height, dims.tileset_id
    s = seed or w * h
    if biome not in _BIOMES:  # 지형·오브젝트 모두 동일 바이옴으로 폴백
        biome = DEFAULT_BIOME
    bands = _BIOMES[biome]

    # 저주파(큰 덩어리) 주도 + 약한 세부 변화. 셀이 작을수록 지형이 커진다.
    elev = _value_noise(w, h, cells=max(2, min(w, h) // 12), seed=s)
    elev = elev + 0.3 * _value_noise(w, h, cells=max(4, min(w, h) // 6), seed=s + 1)
    lo, hi = float(elev.min()), float(elev.max())
    elev = (elev - lo) / (hi - lo + 1e-9)

    # 1) 고도 → 밴드 인덱스 격자(0=최저지)
    names = [nm for _, nm in bands]
    idx = [[len(bands) - 1] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            e = float(elev[y, x])
            for i, (thr, _nm) in enumerate(bands):
                if e < thr:
                    idx[y][x] = i
                    break

    # 2) 작은 조각 정리 → 3) 전이 보정(인접 밴드 차 ≤1 → 호환 지형끼리만 인접)
    _smooth_idx(idx, w, h, passes=2)
    _relax_transitions(idx, w, h)

    # 4) 변형 선택: variant_regions>1=영역단위 패치, 아니면 맵단위 1종(variants=False=원본).
    #    물 등 가장자리가 구워진 지형(_NO_VARIANT_TERRAINS)은 변형 시 충돌하므로 캐노니컬 고정.
    if variants and variant_regions > 1:
        zones = _zone_map(w, h, s + 13, variant_regions)
        zone_ids = [{} for _ in range(variant_regions)]
        for bi, nm in enumerate(names):
            base = _water_id(biome, tid) if nm == "water" else pal.get_tile_id(tid, nm)
            if nm in _NO_VARIANT_TERRAINS:
                vs = [base] * variant_regions
            else:
                vs = _assign_region_variants(
                    tid, base, variant_regions, random.Random(s + 31 * (bi + 1))
                )
            for z in range(variant_regions):
                zone_ids[z][nm] = vs[z]
    else:
        zones = None
        vrng = random.Random(s + 7)
        flat_ids = {}
        for nm in names:
            base = _water_id(biome, tid) if nm == "water" else pal.get_tile_id(tid, nm)
            use_var = variants and nm not in _NO_VARIANT_TERRAINS
            flat_ids[nm] = _pick_variant(tid, base, vrng) if use_var else base

    data = make_empty_data(w, h)
    for y in range(h):
        for x in range(w):
            nm = names[idx[y][x]]
            tile = zone_ids[zones[y][x]][nm] if zones is not None else flat_ids[nm]
            set_tile(data, x, y, w, h, 0, tile)

    if autotile:
        apply_autotile(data, w, h, layer=0, oob_connected=True)
    # 바이옴 물(2240 등 palette 외 id 포함)도 통행 불가로 보장
    impass = set(pal.impassable_ids(tid)) | {_water_id(biome, tid)}
    _set_passability_by_base(data, w, h, impass)

    if objects:
        rng = random.Random(s + 99)
        _place_multitile(data, w, h, tid, biome, rng)  # 멀티타일 먼저(2x2 공간 확보)
        _place_objects(data, w, h, tid, biome, rng)  # 단일은 남은 빈 칸에
    return data


# 던전 테마 → (바닥, 풀, 오브젝트 바이옴). 풀 타일은 타일셋 그림으로 직접 확인한 것:
#   용암 2240(A1, 암반테두리) / 얼음 2624(A1, 수정테두리) — 통행불가 물 해저드
#   독 4304(A2, 보라 독장판) — damage 플래그, 통행가능(밟으면 데미지)
# 테두리(가장자리)는 오토타일에 구워져 있어 apply_autotile 이 자동으로 그린다.
# 풀의 통행 여부는 palette passable 플래그로 자동 결정(impassable_ids).
_DUNGEON_THEMES: dict[str, dict[str, str]] = {
    "lava": {"floor": "lava_floor", "pool": "lava", "objects": "lava"},
    "ice": {"floor": "ice_floor", "pool": "ice_water", "objects": "ice"},
    "poison": {"floor": "poison_floor", "pool": "poison_panel", "objects": "poison"},
    "sand": {"floor": "sand_floor", "pool": "sand_water", "objects": "sand"},
    "crystal": {"floor": "crystal_floor", "pool": "crystal_water", "objects": "crystal"},
    "moss": {"floor": "moss_floor", "pool": "moss_water", "objects": "moss"},
    "dark": {"floor": "dark_floor", "objects": "dark"},  # 풀 없음(어둠 바닥만)
}


def _place_pools(
    data: list[int], w: int, h: int, rooms: list, floor_base: int, pool: int, rng: random.Random
) -> None:
    """방 내부에 테마 풀(용암/얼린물/독 등) 블롭을 둔다. 복도가 이후 뚫어 연결성 보장.

    풀은 방 테두리에서 (반경+1)칸 안쪽에만 둬 바깥에 바닥 버퍼(→테두리)를 보장한다.
    큰 방은 풀 2개까지.
    """
    for room in rooms:
        # 용암 끝(cx±r)이 방 테두리 1칸 안쪽까지만 들어가도록 최대 반경 계산(바닥 버퍼 보장).
        rmax = (min(room.width, room.height) - 3) // 2
        if rmax < 1:  # 너무 작은 방은 용암 못 둠
            continue
        pools = 2 if (room.width >= 9 and room.height >= 9 and rng.random() < 0.5) else 1
        for _ in range(pools):
            if rng.random() > 0.7:
                continue
            r = rng.randint(1, min(2, rmax))
            lo_x, hi_x = room.x + r + 1, room.x + room.width - 2 - r
            lo_y, hi_y = room.y + r + 1, room.y + room.height - 2 - r
            if lo_x > hi_x or lo_y > hi_y:
                continue
            cx, cy = rng.randint(lo_x, hi_x), rng.randint(lo_y, hi_y)
            for y in range(cy - r, cy + r + 1):
                for x in range(cx - r, cx + r + 1):
                    if (x - cx) ** 2 + (y - cy) ** 2 <= r * r and (
                        base_of(get_tile(data, x, y, w, h, 0)) == floor_base
                    ):
                        set_tile(data, x, y, w, h, 0, pool)


def generate_dungeon_map(
    dims: MapDims,
    seed: int = 0,
    objects: bool = True,
    variants: bool = True,
    theme: str = "dungeon",
) -> list[int]:
    """BSP 방-복도 던전 생성 (tileset 4). 방=바닥, 방 밖=검은 공백(void, 막힘).

    기존 dungeon_generator 의 BSP 분할을 재사용하고, 타일은 palette tileset 4 를 쓴다.
    theme(_DUNGEON_THEMES): "lava"/"ice"/"poison" 이면 테마 바닥 + 풀(블롭) + 테두리로
    테마 던전을 만든다(풀·테두리는 샘플맵 실측 매칭쌍). "dungeon"은 일반(풀 없음).
    오브젝트는 방 바닥 위에 배치. A4 벽 오토타일은 후속.
    """
    from agent.generation.mapgen.dungeon_generator import (
        BSPNode,
        _collect_rooms,
        _create_rooms,
        _split,
    )

    w, h = dims.width, dims.height
    s = seed or w * h
    cfg = _DUNGEON_THEMES.get(theme)  # None 이면 일반 던전(풀 없음)
    base_floor = pal.get_tile_id(4, cfg["floor"] if cfg else "floor")
    floor = _pick_variant(4, base_floor, random.Random(s + 7)) if variants else base_floor
    void = pal.get_tile_id(4, "void")

    random.seed(s)  # dungeon_generator 의 BSP 는 전역 random 사용 → 결정적 시드
    data = make_empty_data(w, h)
    for y in range(h):
        for x in range(w):
            set_tile(data, x, y, w, h, 0, void)  # 전체를 공백으로

    root = BSPNode(1, 1, w - 2, h - 2)
    _split(root)
    _create_rooms(root)
    rooms = _collect_rooms(root)

    # 방을 바닥으로 파냄
    for room in rooms:
        for ry in range(room.y, min(room.y + room.height, h - 1)):
            for rx in range(room.x, min(room.x + room.width, w - 1)):
                set_tile(data, rx, ry, w, h, 0, floor)

    pool = pal.get_tile_id(4, cfg["pool"]) if cfg and "pool" in cfg else 0
    if pool:  # 테마 풀을 복도보다 먼저 → 복도가 풀을 뚫어 연결성 보장
        _place_pools(data, w, h, rooms, base_of(floor), pool, random.Random(s + 53))

    # 인접 방을 L자 복도로 연결 (풀 위를 지나면 바닥으로 뚫어 길 확보)
    for i in range(len(rooms) - 1):
        x1, y1 = rooms[i].center
        x2, y2 = rooms[i + 1].center
        for x in range(min(x1, x2), max(x1, x2) + 1):
            set_tile(data, x, y1, w, h, 0, floor)
        for y in range(min(y1, y2), max(y1, y2) + 1):
            set_tile(data, x2, y, w, h, 0, floor)

    if pool:  # A1/A2 테마 풀의 가장자리(테두리) shape 를 오토타일이 자동으로 그림
        apply_autotile(data, w, h, layer=0, oob_connected=False)

    _set_passability_by_base(data, w, h, pal.impassable_ids(4))  # void·테마 물 막힘

    if objects:
        biome = cfg["objects"] if cfg else "dungeon"
        rng = random.Random(s + 99)
        _place_multitile(data, w, h, 4, biome, rng)
        _place_objects(data, w, h, 4, biome, rng)
    return data


def generate_interior_map(
    dims: MapDims,
    seed: int = 0,
    objects: bool = True,
    tileset: int = 3,
    biome: str = "interior",
    floor_name: str = "floor",
    wall_name: str = "wall",
    wall_top_name: str | None = None,
    variants: bool = True,
) -> list[int]:
    """BSP 방 실내 생성. 방 테두리=벽, 내부=바닥, 방 밖=공백(없으면 벽).

    복도는 벽을 바닥으로 뚫어 문 역할을 한다. 가구는 바닥 위에 배치.
    tileset 3(실내)/6(SF내부) 공용. tileset 6은 void 가 없어 wall 로 방 밖을 채운다.
    floor_name/wall_name 으로 색 대비가 큰 지형을 선택할 수 있다.
    """
    from agent.generation.mapgen.dungeon_generator import (
        BSPNode,
        _collect_rooms,
        _create_rooms,
        _split,
    )

    w, h = dims.width, dims.height
    s = seed or w * h
    base_floor = pal.get_tile_id(tileset, floor_name)
    floor = _pick_variant(tileset, base_floor, random.Random(s + 7)) if variants else base_floor
    wall = pal.get_tile_id(tileset, wall_name)  # 앞면/정면 벽
    wall_top = pal.get_tile_id(tileset, wall_top_name) if wall_top_name else wall  # 윗면/천장
    vt = pal.get_terrain(tileset, "void")
    void = int(vt["base_id"]) if vt else wall_top  # void 없으면 윗면 벽으로 방 밖

    random.seed(s)
    data = make_empty_data(w, h)
    for y in range(h):
        for x in range(w):
            set_tile(data, x, y, w, h, 0, void)

    root = BSPNode(1, 1, w - 2, h - 2)
    _split(root)
    _create_rooms(root)
    rooms = _collect_rooms(root)

    # 각 방: 테두리는 벽돌 벽, 내부는 마루
    for room in rooms:
        x0, y0 = room.x, room.y
        x1, y1 = min(room.x + room.width - 1, w - 1), min(room.y + room.height - 1, h - 1)
        for ry in range(y0, y1 + 1):
            for rx in range(x0, x1 + 1):
                edge = rx in (x0, x1) or ry in (y0, y1)
                set_tile(data, rx, ry, w, h, 0, wall_top if edge else floor)

    # 인접 방 중심을 마루 복도로 연결(벽을 뚫어 문 역할)
    for i in range(len(rooms) - 1):
        x1, y1 = rooms[i].center
        x2, y2 = rooms[i + 1].center
        for x in range(min(x1, x2), max(x1, x2) + 1):
            set_tile(data, x, y1, w, h, 0, floor)
        for y in range(min(y1, y2), max(y1, y2) + 1):
            set_tile(data, x2, y, w, h, 0, floor)

    # 입체 벽: 아래가 바닥인 벽 → 앞면(face), 그 외는 윗면(top) 유지
    if wall_top_name:
        for y in range(h):
            for x in range(w):
                if base_of(get_tile(data, x, y, w, h, 0)) == wall_top:
                    below = base_of(get_tile(data, x, y + 1, w, h, 0)) if y + 1 < h else -1
                    if below == floor:
                        set_tile(data, x, y, w, h, 0, wall)

    # 윗면 벽(STAR, flags 통과)도 게임상 벽이므로 통행 차단
    impass = set(pal.impassable_ids(tileset)) | {wall, wall_top}
    _set_passability_by_base(data, w, h, impass)

    if objects:
        rng = random.Random(s + 99)
        _place_multitile(data, w, h, tileset, biome, rng, against_wall=True)  # 가구 벽에 등 댐
        _place_objects(data, w, h, tileset, biome, rng)
    return data


def generate_palette_map(dims: MapDims, seed: int = 0, autotile: bool = True) -> list[int]:
    """palette 지형으로 마을풍 타일맵 생성. 반환: flat 1D list[int] (w×h×6).

    레이어 0=바닥, 1=오브젝트(벽), 5=통행 플래그.
    autotile=True 면 A2 지면 타일에 shape 보정을 적용해 자연스러운 이음새를 만든다.
    """
    rng = random.Random(seed if seed else dims.width * dims.height)
    w, h, tid = dims.width, dims.height, dims.tileset_id
    data = make_empty_data(w, h)

    def tid_of(name: str) -> int:
        return pal.get_tile_id(tid, name)

    grass = tid_of("grass")
    dirt = tid_of("dirt")
    sand = tid_of("sand")
    stone = tid_of("stone_path")
    water = tid_of("water")
    wall = tid_of("wall")

    # 1) 바닥 전체 잔디
    for y in range(h):
        for x in range(w):
            set_tile(data, x, y, w, h, 0, grass)

    # 2) 흙/모래 블롭 몇 개 (랜덤 원형 패치)
    for _ in range(max(2, (w * h) // 200)):
        cx, cy = rng.randint(3, w - 4), rng.randint(3, h - 4)
        r = rng.randint(2, 4)
        patch = dirt if rng.random() < 0.6 else sand
        for y in range(max(1, cy - r), min(h - 1, cy + r + 1)):
            for x in range(max(1, cx - r), min(w - 1, cx + r + 1)):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    set_tile(data, x, y, w, h, 0, patch)

    # 3) 중앙 십자형 돌길
    for x in range(1, w - 1):
        set_tile(data, x, h // 2, w, h, 0, stone)
    for y in range(1, h - 1):
        set_tile(data, w // 2, y, w, h, 0, stone)

    # 4) 연못 1개 (원형 물)
    px, py = rng.randint(5, w - 6), rng.randint(5, h - 6)
    pr = rng.randint(2, 3)
    for y in range(max(1, py - pr), min(h - 1, py + pr + 1)):
        for x in range(max(1, px - pr), min(w - 1, px + pr + 1)):
            if (x - px) ** 2 + (y - py) ** 2 <= pr * pr:
                set_tile(data, x, y, w, h, 0, water)

    # 5) 외곽 테두리 벽 (레이어 1)
    for x in range(w):
        set_tile(data, x, 0, w, h, 1, wall)
        set_tile(data, x, h - 1, w, h, 1, wall)
    for y in range(h):
        set_tile(data, 0, y, w, h, 1, wall)
        set_tile(data, w - 1, y, w, h, 1, wall)

    # 6) 건물 자리 몇 개 (2×2 벽 블록, 길/물 위 회피)
    for _ in range(max(1, (w * h) // 150)):
        bx, by = rng.randint(2, w - 4), rng.randint(2, h - 4)
        if any(
            get_tile(data, bx + dx, by + dy, w, h, 0) in (stone, water)
            for dx in range(2)
            for dy in range(2)
        ):
            continue
        for dx in range(2):
            for dy in range(2):
                set_tile(data, bx + dx, by + dy, w, h, 1, wall)

    # 7) 오토타일 shape 보정 (A2 지면 — 바닥/벽 레이어)
    #    통행 레이어 계산 전에 적용 (shape 가 바뀌어도 base 는 불변이라 통행성 영향 없음)
    if autotile:
        apply_autotile(data, w, h, layer=0, oob_connected=True)
        apply_autotile(data, w, h, layer=1, oob_connected=False)

    # 8) 통행 불가 레이어 (palette 의 impassable 사용: water=L0, wall=L1)
    #    impassable_ids 는 base_id 기준이므로 shape 적용 전/후 모두 base_of 로 비교해야 정확.
    _set_passability_by_base(data, w, h, pal.impassable_ids(tid))

    return data


def _set_passability_by_base(
    data: list[int], width: int, height: int, impassable_bases: set[int]
) -> None:
    """base_id 기준 통행 불가 레이어(5) 설정. 오토타일 shape 가 적용돼도 정확.

    tile_constants.set_passability 는 정확한 ID 매칭이라 shape 가 붙으면 놓친다.
    여기서는 base_of 로 정규화해 비교한다.
    """
    from agent.generation.mapgen.autotile import base_of

    for y in range(height):
        for x in range(width):
            l0 = base_of(get_tile(data, x, y, width, height, 0))
            l1 = base_of(get_tile(data, x, y, width, height, 1))
            if l0 in impassable_bases or l1 in impassable_bases:
                set_tile(data, x, y, width, height, 5, 1)


def _stats(data: list[int], dims: MapDims) -> dict[str, int]:
    """지형별 타일 수 + walkable 수 집계."""
    w, h, tid = dims.width, dims.height, dims.tileset_id
    id_to_name = {pal.get_tile_id(tid, n): n for n in _PREVIEW_COLORS}
    counts: dict[str, int] = {}
    walkable = 0
    for y in range(h):
        for x in range(w):
            top = get_tile(data, x, y, w, h, 1) or get_tile(data, x, y, w, h, 0)
            name = id_to_name.get(base_of(top), f"id:{top}")
            counts[name] = counts.get(name, 0) + 1
            if get_tile(data, x, y, w, h, 5) == 0:
                walkable += 1
    counts["_walkable"] = walkable
    return counts


def render_preview(data: list[int], dims: MapDims, out_path: str, cell: int = 12) -> None:
    """지형색 도식 PNG 저장 (pillow). 실제 타일 그림이 아닌 색상 미리보기."""
    from PIL import Image, ImageDraw

    w, h, tid = dims.width, dims.height, dims.tileset_id
    id_to_name = {pal.get_tile_id(tid, n): n for n in _PREVIEW_COLORS}
    img = Image.new("RGB", (w * cell, h * cell), (20, 20, 20))
    d = ImageDraw.Draw(img)
    for y in range(h):
        for x in range(w):
            top = get_tile(data, x, y, w, h, 1) or get_tile(data, x, y, w, h, 0)
            name = id_to_name.get(base_of(top), None)
            color = _PREVIEW_COLORS.get(name, (200, 0, 200))  # 모르는 타일=마젠타
            d.rectangle([x * cell, y * cell, x * cell + cell, y * cell + cell], fill=color)
    img.save(out_path)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="palette 기반 테스트 맵 생성기")
    parser.add_argument("--width", type=int, default=30)
    parser.add_argument("--height", type=int, default=30)
    parser.add_argument("--tileset", type=int, default=2)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--mode",
        choices=[
            "town", "terrain", "dungeon",
            "lava_dungeon", "ice_dungeon", "poison_dungeon",
            "sand_dungeon", "crystal_dungeon", "moss_dungeon", "dark_dungeon",
            "interior", "sf_outside", "sf_interior",
        ],
        default="town",
        help="town/terrain/dungeon·{lava,ice,poison,sand,crystal,moss,dark}_dungeon(t4)/...",
    )
    parser.add_argument(
        "--biome",
        choices=sorted(_BIOMES),
        default=DEFAULT_BIOME,
        help="terrain 모드 바이옴 프리셋",
    )
    parser.add_argument("--out", default=None, help="미리보기 PNG 경로 (생략 시 통계만)")
    parser.add_argument("--no-autotile", action="store_true", help="오토타일 shape 보정 비활성화")
    parser.add_argument(
        "--no-objects", action="store_true", help="terrain 모드 오브젝트(나무·풀 등) 배치 비활성화"
    )
    parser.add_argument(
        "--no-variants", action="store_true", help="지형 텍스처 변형(맵 간 다양성) 비활성화"
    )
    parser.add_argument(
        "--variant-regions",
        type=int,
        default=3,
        help="terrain 변형을 맵 내 N개 영역으로 나눠 적용(1=맵 전체 1종, 기본 3)",
    )
    parser.add_argument(
        "--render", default=None, help="실제 타일셋으로 렌더한 PNG 경로 (--base-game 필요)"
    )
    parser.add_argument(
        "--base-game",
        type=Path,
        default=Path("storage/games/base_game"),
        help="타일셋 이미지가 있는 base_game 경로",
    )
    args = parser.parse_args(argv)

    _dungeon_modes = {"dungeon": "dungeon", **{f"{t}_dungeon": t for t in _DUNGEON_THEMES}}
    _MODE_TILESET = {
        **{m: 4 for m in _dungeon_modes}, "interior": 3, "sf_outside": 5, "sf_interior": 6,
    }
    if args.mode in _MODE_TILESET:
        args.tileset = _MODE_TILESET[args.mode]  # 모드별 tileset 고정 (렌더도 동일)
    dims = MapDims(width=args.width, height=args.height, tileset_id=args.tileset)
    if args.mode in _dungeon_modes:
        data = generate_dungeon_map(
            dims,
            seed=args.seed,
            objects=not args.no_objects,
            variants=not args.no_variants,
            theme=_dungeon_modes[args.mode],
        )
        mode_label = args.mode
    elif args.mode == "interior":
        data = generate_interior_map(
            dims,
            seed=args.seed,
            objects=not args.no_objects,
            wall_top_name="wall_top",
            variants=not args.no_variants,
        )
        mode_label = "interior"
    elif args.mode == "sf_interior":
        data = generate_interior_map(
            dims,
            seed=args.seed,
            objects=not args.no_objects,
            tileset=6,
            biome="sf_interior",
            floor_name="tile_floor",  # 흰 타일(밝) — 어두운 벽과 대비
            wall_name="dark_wall",  # 어두운 금속벽
            variants=not args.no_variants,
        )
        mode_label = "sf_interior"
    elif args.mode == "sf_outside":
        data = generate_terrain_map(
            dims,
            seed=args.seed,
            autotile=not args.no_autotile,
            biome="city",
            objects=not args.no_objects,
            variants=not args.no_variants,
            variant_regions=args.variant_regions,
        )
        mode_label = "sf_outside/city"
    elif args.mode == "terrain":
        data = generate_terrain_map(
            dims,
            seed=args.seed,
            autotile=not args.no_autotile,
            biome=args.biome,
            objects=not args.no_objects,
            variants=not args.no_variants,
            variant_regions=args.variant_regions,
        )
        mode_label = f"terrain/{args.biome}"
    else:
        data = generate_palette_map(dims, seed=args.seed, autotile=not args.no_autotile)
        mode_label = "town"

    print(f"생성 완료: mode={mode_label} {args.width}x{args.height} tileset={args.tileset} seed={args.seed}")
    for name, cnt in sorted(_stats(data, dims).items(), key=lambda kv: -kv[1]):
        print(f"  {name:12s} {cnt}")

    if args.out:
        render_preview(data, dims, args.out)
        print(f"색상 미리보기 저장: {args.out}")

    if args.render:
        from agent.generation.mapgen.tile_renderer import render_data_to_png

        render_data_to_png(data, args.width, args.height, args.tileset, args.base_game, args.render)
        print(f"실제 타일 렌더 저장: {args.render}")


if __name__ == "__main__":
    main()
