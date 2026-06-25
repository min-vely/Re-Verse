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
    # 레이어 분산(palette object layer): grass_tuft/flower/bush_clump=L2, reed/shrub/rock=L3.
    # 풀밭 텍스처(L1)는 _GROUND_DECOR 가 따로 깐다. 단일 식생은 종류·밀도를 늘려 다양화.
    "grassland": [
        ("grass_tuft", "grass", 0.100),
        ("flower", "grass", 0.050),
        ("bush_clump", "grass", 0.040),
        ("reed", "grass", 0.030),
        ("shrub", "grass", 0.025),
        ("berry_bush", "grass", 0.015),
        ("rock", "grass", 0.010),
    ],
    # 오아시스(grass)엔 풀·꽃·관목, 사막(sand)엔 죽은 나무·바위가 드물게
    "desert": [
        ("grass_tuft", "grass", 0.100),
        ("flower", "grass", 0.030),
        ("bush_clump", "grass", 0.040),
        ("shrub", "grass", 0.025),
        ("dead_tree", "sand", 0.014),
        ("desert_rock", "sand", 0.012),
    ],
    "snow": [
        ("bush_clump", "snow", 0.020),
        ("rock", "snow", 0.012),
    ],
    "wetland": [
        ("grass_tuft", "grass", 0.090),
        ("flower", "grass", 0.030),
        ("bush_clump", "grass", 0.040),
        ("reed", "grass", 0.050),
        ("shrub", "grass", 0.020),
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
    # 벽 인접(wall_adjacent)으로만 배치돼 후보가 적어 밀도를 높게 잡는다. rug 는 카펫 러그
    # 영역과 중복이라 제외. table/barrel/chair 는 막힘/통과 섞여 벽 따라 정돈된다.
    # 바닥에 자연스러운 것만(통·항아리·화분·바구니). 곰인형(teddy)·책더미(book_stack)는
    # 바닥에 두면 어색해 제외 — 대신 _place_on_furniture 가 침대/책장 위에 올린다.
    "interior": [
        ("barrel", "floor", 0.05),
        ("pottery", "floor", 0.04),
        ("bucket", "floor", 0.03),
        ("plant_pot", "floor", 0.04),
        ("basket", "floor", 0.03),
        ("chair", "floor", 0.04),
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
    # 야외 나무는 L3 군집(_place_multitile cluster_seed)으로 숲처럼 모이게 배치한다.
    "grassland": [("tree", "grass", 0.060)],
    "desert": [("tree", "grass", 0.040)],  # 오아시스 나무
    "snow": [("snow_tree", "snow", 0.050)],
    "wetland": [("tree", "grass", 0.045)],
    "dungeon": [("ice_crystal", "floor", 0.010)],
    # 벽쪽 배치(against_wall) — 다양한 가구를 낮은 밀도로 섞어 방마다 다르게.
    # organ(파이프오르간)은 교회용 + 철창살처럼 보여 일반 집엔 제외.
    "interior": [
        ("bed_large", "floor", 0.04),
        ("sofa", "floor", 0.03),
        ("bookshelf2", "floor", 0.05),
        ("cabinet", "floor", 0.05),
        ("piano", "floor", 0.02),
        ("fireplace", "floor", 0.03),
        ("clock", "floor", 0.04),
    ],
    # SF외곽: 잔디공원에 벤치·펜스
    "city": [
        ("bench", "grass", 0.020),
        ("fence", "grass", 0.015),
    ],
    "sf_interior": [("railing", "tile_floor", 0.015)],
}

# 바이옴 → [(데코 이름, 대상 지형, 덮는 비율 0~1)]. ground_decor 의 A2 풀밭 텍스처를
# 바닥(L0) 위 L1 에 노이즈 패치로 겹쳐 깐다. 샘플맵 L1 의 본체(긴풀 등). coverage 가
# 클수록 넓게 덮는다. 같은 칸엔 먼저 깐 데코가 우선(겹침 방지).
_GROUND_DECOR: dict[str, list[tuple[str, str, float]]] = {
    "grassland": [("tall_grass", "grass", 0.45), ("grass_dark", "grass", 0.20)],
    "desert": [("dry_grass", "sand", 0.35), ("tall_grass", "grass", 0.55)],
    "snow": [("snow_patch", "snow", 0.30)],
    "wetland": [("tall_grass", "grass", 0.55), ("grass_dark", "grass", 0.25)],
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


def _blocking_avoid(data: list[int], width: int, height: int) -> set[tuple[int, int]]:
    """막힘 오브젝트를 두면 길이 끊기는 칸(관절점 + 1칸 통로)을 반환한다.

    현재 통행 가능(L5=0) 칸을 그래프로 보고 관절점(articulation point)을 찾는다 —
    관절점에 막힘 오브젝트를 두면 통로가 끊겨 플레이어가 못 지나간다(외나무다리·복도).
    1칸 너비 일직선 통로도 병목이라 함께 회피한다. tile_checker 의 관절점 구현(Tarjan)을 재사용.
    """
    from agent.generation.mapgen.tile_checker import find_articulation_points

    walkable = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if get_tile(data, x, y, width, height, 5) == 0
    }
    avoid = find_articulation_points(walkable)
    for x, y in walkable:
        nbrs = [
            (x + dx, y + dy)
            for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0))
            if (x + dx, y + dy) in walkable
        ]
        if len(nbrs) == 2:
            (x1, y1), (x2, y2) = nbrs
            if abs(x1 - x2) + abs(y1 - y2) > 1:  # 일직선 1칸 통로
                avoid.add((x, y))
    return avoid


def _connect_regions(data: list[int], width: int, height: int, floor_id: int) -> None:
    """분리된 통행 가능 영역들을 벽 1칸씩 뚫어 하나로 잇는다(문 막힘·고립 방 복구).

    block_avoid(관절점 회피)로도 막지 못한 구조적 분리(문이 자식 내벽에 막히는 등)나
    가구가 막은 통로를 사후 복구한다. 통행 가능(L5=0) 영역을 BFS 라벨링해, 가장 큰 영역과
    1칸 벽을 사이에 둔 다른 영역을 찾으면 그 벽을 floor 로 뚫어 연결한다(수렴까지 반복).

    뚫는 경로가 가구(멀티타일)를 관통하면 그 칸만 지워져 반쪽 가구가 남는다 —
    이는 호출 측에서 _clean_partial_multitiles 로 사후 정리한다(반쪽 가구를 통째 제거).
    """
    for _ in range(width * height):  # 안전 한도
        walk = {
            (x, y)
            for y in range(height)
            for x in range(width)
            if get_tile(data, x, y, width, height, 5) == 0
        }
        if len(walk) < 2:
            return
        label: dict[tuple[int, int], int] = {}
        comps: list[list[tuple[int, int]]] = []
        for s in walk:
            if s in label:
                continue
            comp: list[tuple[int, int]] = []
            stack = [s]
            label[s] = len(comps)
            while stack:
                cx, cy = stack.pop()
                comp.append((cx, cy))
                for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                    t = (cx + dx, cy + dy)
                    if t in walk and t not in label:
                        label[t] = len(comps)
                        stack.append(t)
            comps.append(comp)
        if len(comps) <= 1:
            return
        comps.sort(key=len, reverse=True)
        main = set(comps[0])
        connected = False
        for comp in comps[1:]:  # 큰 영역과 벽(1~3칸)을 사이에 둔 영역을 뚫어 연결
            for cx, cy in comp:
                for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                    for dist in (2, 3, 4):  # 벽 1·2·3칸 너머에 main 이 있으면 그 벽들을 뚫음
                        bx, by = cx + dist * dx, cy + dist * dy
                        if 0 <= bx < width and 0 <= by < height and (bx, by) in main:
                            for k in range(1, dist):
                                wx, wy = cx + k * dx, cy + k * dy
                                for li in range(6):
                                    set_tile(data, wx, wy, width, height, li, 0)
                                set_tile(data, wx, wy, width, height, 0, floor_id)
                            connected = True
                            break
                    if connected:
                        break
                if connected:
                    break
            if connected:
                break
        if not connected:
            return  # 3칸 벽으로도 못 잇는 영역 — 더 진행 안 함


def _fill_isolated_pockets(
    data: list[int], width: int, height: int, wall_top: int, max_size: int = 3
) -> None:
    """벽으로 완전히 둘러싸여 못 잇는 작은 고립 칸(≤max_size)을 벽으로 메운다.

    _connect_regions 가 3칸 벽 한도로도 잇지 못한 1~몇 칸짜리 고립 포켓(문 없는 1칸 방
    등)은 도달 불가라 플레이 공간이 아니다. 긴 터널로 억지로 잇기보다 벽으로 메워
    '섬'을 없앤다(큰 영역은 건드리지 않아 실제 방 손실 없음).
    """
    walk = {
        (x, y)
        for y in range(height)
        for x in range(width)
        if get_tile(data, x, y, width, height, 5) == 0
    }
    label: dict[tuple[int, int], int] = {}
    comps: list[list[tuple[int, int]]] = []
    for s in walk:
        if s in label:
            continue
        comp: list[tuple[int, int]] = []
        stack = [s]
        label[s] = len(comps)
        while stack:
            cx, cy = stack.pop()
            comp.append((cx, cy))
            for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                t = (cx + dx, cy + dy)
                if t in walk and t not in label:
                    label[t] = len(comps)
                    stack.append(t)
        comps.append(comp)
    if len(comps) <= 1:
        return
    comps.sort(key=len, reverse=True)
    for comp in comps[1:]:  # 가장 큰 영역 외 — 작은 포켓만 벽으로 메움
        if len(comp) > max_size:
            continue
        for x, y in comp:
            for li in range(6):
                set_tile(data, x, y, width, height, li, 0)
            set_tile(data, x, y, width, height, 0, wall_top)
            set_tile(data, x, y, width, height, 5, 1)  # 벽 = 통행 불가


def _clean_partial_multitiles(data: list[int], width: int, height: int, tileset: int) -> None:
    """반쪽만 남은 멀티타일(가구)을 통째로 지운다 — _connect_regions 가 가구를 관통해
    뚫으면 한 칸이 비어 반쪽 가구가 남는 걸 정리한다.

    palette 의 각 멀티타일에 대해, 좌상 앵커 타일이 깔린 위치를 찾아 footprint 전체가
    온전한지 검사한다. 한 칸이라도 빠졌으면 남은 칸을 모두 지워(L1~L3 비우고 통행 복구)
    반쪽 가구가 보이지 않게 한다.
    """
    ts = pal.get_tileset(tileset)
    if not ts:
        return
    for mt in (ts.get("multitile") or {}).values():
        if not isinstance(mt, dict):
            continue  # "_note" 등 메타 항목 건너뜀
        tiles = mt.get("tiles")
        if not tiles:
            continue
        mh, mw = len(tiles), len(tiles[0])
        if mh == 1 and mw == 1:
            continue  # 단일 타일은 반쪽 개념 없음
        total = mh * mw
        for layer in (1, 2, 3):
            # 1) 온전한 배치 셀을 보호집합으로 수집(인접한 멀쩡한 가구 오제거 방지)
            protected: set[tuple[int, int]] = set()
            partials: list[list[tuple[int, int]]] = []
            for y in range(height - mh + 1):
                for x in range(width - mw + 1):
                    matched = [
                        (x + dx, y + dy)
                        for dy in range(mh)
                        for dx in range(mw)
                        if base_of(get_tile(data, x + dx, y + dy, width, height, layer))
                        == int(tiles[dy][dx])
                    ]
                    if len(matched) == total:
                        protected.update(matched)  # 온전한 가구
                    elif matched:
                        partials.append(matched)  # 일부만 일치(반쪽 후보)
            # 2) 보호되지 않은 반쪽 셀만 제거 — 가구를 지우면 평바닥이므로 통행(L5)도 복구
            for matched in partials:
                for cell in matched:
                    if cell not in protected:
                        set_tile(data, cell[0], cell[1], width, height, layer, 0)
                        set_tile(data, cell[0], cell[1], width, height, 5, 0)


def _place_objects(
    data: list[int],
    width: int,
    height: int,
    tid: int,
    biome: str,
    rng: random.Random,
    wall_adjacent: bool = False,
    floor_targets: set[int] | None = None,
    avoid_cells: set[tuple[int, int]] | None = None,
    block_avoid: set[tuple[int, int]] | None = None,
) -> None:
    """바이옴별 오브젝트(나무·풀·꽃 등)를 어울리는 지형 위(레이어1)에 확률 배치.

    통행 불가 오브젝트(나무 등)는 레이어5도 막는다. 이미 레이어1이 찬 칸은 건너뛴다.
    wall_adjacent=True 면 상하좌우 중 하나가 통행 불가(벽·가구)인 칸에만 배치(실내 가구
    정돈 — 방 한가운데 흩어지지 않고 벽을 따라 놓인다).
    floor_targets 가 주어지면 spec 의 target 지형 대신 그 base_id 집합(여러 바닥 종류)을
    배치 대상으로 쓴다 — 집 모델처럼 방마다 바닥이 다를 때 모든 바닥에 가구가 놓이게.
    """
    specs = _BIOME_OBJECTS.get(biome, [])
    if not specs:
        return
    terr_ids = _terr_ids(tid)

    def near_wall(x: int, y: int) -> bool:
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < width and 0 <= ny < height and get_tile(data, nx, ny, width, height, 5) == 1:
                return True
        return False

    for name, target, density in specs:
        obj = pal.get_object(tid, name)
        if not obj:
            continue
        if floor_targets is not None:
            tgt_set = floor_targets
        else:
            tgt = terr_ids.get(target)
            if tgt is None:
                continue
            tgt_set = _variant_set(tid, tgt)  # 변형 바닥도 대상에 포함
        layer = int(obj.get("layer", 1))  # palette 가 정한 레이어(L1~L3)에 배치
        blocks = not obj.get("passable", True)  # 막힘 오브젝트만 통로(관절점) 회피
        for y in range(height):
            for x in range(width):
                if get_tile(data, x, y, width, height, layer) != 0:
                    continue  # 그 레이어에 이미 오브젝트 있음
                if avoid_cells and (x, y) in avoid_cells:
                    continue  # 문·입구 통로엔 배치 안 함(길 막힘 방지)
                if blocks and block_avoid and (x, y) in block_avoid:
                    continue  # 막힘 오브젝트가 통로(관절점)를 막지 않게
                if base_of(get_tile(data, x, y, width, height, 0)) not in tgt_set:
                    continue
                if wall_adjacent and not near_wall(x, y):
                    continue  # 벽 옆이 아니면 건너뜀(가구 정돈)
                if rng.random() < density:
                    set_tile(data, x, y, width, height, layer, int(obj["base_id"]))
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
    layer: int = 1,
    cluster_seed: int | None = None,
    floor_targets: set[int] | None = None,
    avoid_cells: set[tuple[int, int]] | None = None,
    block_avoid: set[tuple[int, int]] | None = None,
) -> list[tuple[int, int]]:
    """바이옴별 멀티타일 오브젝트(나무 2x2 등)를 대상 지형 위에 확률 배치.

    NxM 칸이 전부 대상 지형 + 해당 layer 가 비어있을 때만 배치(겹침 방지).
    blocked=true 칸은 레이어5도 막는다.
    against_wall=True 면 가구 윗줄(y-1)이 전부 벽(통행불가)인 위치에만 배치(벽에 등 댐).
    cluster_seed 가 있으면 저주파 노이즈 마스크로 배치를 군집화(숲처럼 모이게).
    layer 로 배치 레이어 지정(야외 나무=L3 상위 데코, 실내/던전=L1).
    floor_targets 가 주어지면 spec 의 target 대신 그 base_id 집합(여러 바닥)을 대상으로 쓴다.

    반환: 배치된 모든 칸(footprint) 좌표 리스트(후처리용 — 잎·기둥 전체 포함).
    """
    specs = _BIOME_MULTITILE.get(biome, [])
    if not specs:
        return []
    placed_cells: list[tuple[int, int]] = []
    terr_ids = _terr_ids(tid)
    mask = None
    if cluster_seed is not None:
        mask = _value_noise(width, height, cells=max(2, min(width, height) // 10), seed=cluster_seed)
    for name, target, density in specs:
        mt = pal.get_multitile(tid, name)
        if not mt:
            continue
        if floor_targets is not None:
            tgt_set = floor_targets
        else:
            tgt = terr_ids.get(target)
            if tgt is None:
                continue
            tgt_set = _variant_set(tid, tgt)  # 변형 바닥도 대상
        tiles, blocked = mt["tiles"], mt["blocked"]
        mh, mw = len(tiles), len(tiles[0])
        for y in range(height - mh + 1):
            for x in range(width - mw + 1):
                fits = all(
                    base_of(get_tile(data, x + dx, y + dy, width, height, 0)) in tgt_set
                    and get_tile(data, x + dx, y + dy, width, height, layer) == 0
                    and not (avoid_cells and (x + dx, y + dy) in avoid_cells)
                    and not (block_avoid and blocked[dy][dx] and (x + dx, y + dy) in block_avoid)
                    for dy in range(mh)
                    for dx in range(mw)
                )
                if against_wall:
                    # 가구 윗줄(y-1)이 전부 벽(통행불가)이어야 벽에 등 댄 것으로 간주
                    fits = fits and y > 0 and all(
                        get_tile(data, x + dx, y - 1, width, height, 5) == 1 for dx in range(mw)
                    )
                if not fits:
                    continue
                # 군집 마스크: 노이즈 높은 곳(숲)일수록 배치 확률↑, 낮은 곳은 억제
                local = density
                if mask is not None:
                    m = float(mask[y, x])
                    local = density * (m * m) * 3.0
                if rng.random() >= local:
                    continue
                for dy in range(mh):
                    for dx in range(mw):
                        set_tile(data, x + dx, y + dy, width, height, layer, int(tiles[dy][dx]))
                        placed_cells.append((x + dx, y + dy))
                        if blocked[dy][dx]:
                            set_tile(data, x + dx, y + dy, width, height, 5, 1)
    return placed_cells


def _place_ground_decor(
    data: list[int],
    width: int,
    height: int,
    tid: int,
    biome: str,
    seed: int,
    autotile: bool = True,
) -> None:
    """바닥(L0) 위 L1 에 A2 풀밭 텍스처(ground_decor)를 노이즈 패치로 겹쳐 깐다.

    샘플맵 L1 의 본체(긴풀 등) 재현. 각 데코는 저주파 노이즈 임계로 덩어리(패치)를
    만들어 단조로운 지면에 디테일을 준다. 끝나면 L1 에 apply_autotile 을 적용해 패치
    경계를 자연스럽게 만든다(A2 오토타일). ground_decor 는 통행 가능이라 통행성 영향 없음.
    """
    specs = _GROUND_DECOR.get(biome, [])
    if not specs:
        return
    terr_ids = _terr_ids(tid)
    placed = False
    for i, (name, target, coverage) in enumerate(specs):
        dec = pal.get_ground_decor(tid, name)
        tgt = terr_ids.get(target)
        if not dec or tgt is None:
            continue
        tgt_set = _variant_set(tid, tgt)  # 변형 바닥도 대상
        nz = _value_noise(
            width, height, cells=max(2, min(width, height) // 8), seed=seed + 17 * (i + 1)
        )
        thr = 1.0 - coverage  # coverage 비율만큼 임계 초과 → 그만큼 덮음
        base_id = int(dec["base_id"])
        for y in range(height):
            for x in range(width):
                if get_tile(data, x, y, width, height, 1) != 0:
                    continue  # 먼저 깐 데코 우선(겹침 방지)
                if base_of(get_tile(data, x, y, width, height, 0)) not in tgt_set:
                    continue
                if float(nz[y, x]) >= thr:
                    set_tile(data, x, y, width, height, 1, base_id)
                    placed = True
    if placed and autotile:
        apply_autotile(data, width, height, layer=1, oob_connected=False)


def _place_path(data: list[int], width: int, height: int, tid: int, seed: int) -> None:
    """맵을 가로지르는 구불구불한 흙길을 grass 바닥(L0) 위에 깐다(바닥 다양화).

    저주파 노이즈로 y 를 변위시켜 자연스러운 곡선 길을 만든다. grass 위에만 깔아
    물·모래·눈은 건드리지 않는다. autotile 전에 호출해 흙길 가장자리 shape 가 보정되게
    한다(dirt 는 grass 와 오토타일이 완전 호환은 아니지만 길이 좁아 경계가 거의 안 띈다).
    """
    grass = pal.get_tile_id(tid, "grass")
    dirt = pal.get_tile_id(tid, "dirt")
    if not grass or not dirt:
        return
    nz = _value_noise(width, height, cells=max(2, width // 8), seed=seed)
    cy = height // 2
    for x in range(width):
        yy = int(cy + (float(nz[0, x]) - 0.5) * height * 0.5)
        for dy in (0, 1):  # 길 폭 2칸
            y = yy + dy
            if 0 <= y < height and base_of(get_tile(data, x, y, width, height, 0)) == grass:
                set_tile(data, x, y, width, height, 0, dirt)


def _place_wall_shadows(data: list[int], width: int, height: int, wall_bases: set[int]) -> None:
    """건물 벽(wall_bases)의 오른쪽 바닥 한 칸에만 그림자(L4=5)를 드리운다.

    RPG Maker 그림자펜 표준: base 5(=0b0101, 왼쪽 절반). 빛이 왼쪽에서 와 벽 오른쪽에
    그림자가 진다(example1~3 실내맵 스타일). 그림자 발생원은 **벽뿐** — 가구·통은 L0 가
    바닥(통행 가능)이라 wall_bases 에 안 들어가 그림자가 안 생긴다(사용자 요청).
    오른쪽 칸이 통행 가능 바닥일 때만 깐다(벽 안쪽·벽끼리 맞닿은 곳엔 안 생김).
    """
    shadow = 5  # 0b0101 = 왼쪽 절반
    for y in range(height):
        for x in range(width - 1):
            if base_of(get_tile(data, x, y, width, height, 0)) not in wall_bases:
                continue  # L0 가 벽인 칸만 — 가구는 L0=바닥이라 제외
            rx = x + 1
            if (
                get_tile(data, rx, y, width, height, 5) == 0  # 오른쪽이 통행 가능 바닥
                and get_tile(data, rx, y, width, height, 4) == 0  # 아직 그림자 없음
            ):
                set_tile(data, rx, y, width, height, 4, shadow)


def _place_dining_set(
    data: list[int],
    width: int,
    height: int,
    tileset: int,
    rooms: list[tuple[int, int, int, int]],
    rng: random.Random,
    avoid_cells: set[tuple[int, int]] | None = None,
    block_avoid: set[tuple[int, int]] | None = None,
) -> None:
    """충분히 큰 방 중앙에 식탁(dining_table) + 둘러싼 의자를 배치(큰 방 휑함 해소).

    방 가운데에 식탁을 놓고 위·아래 줄에 의자를 깐다. 식탁/의자 자리가 비어 있을 때만
    (겹침 방지). 작은 방·이미 가구가 찬 방은 건너뛴다.
    """
    dt = pal.get_multitile(tileset, "dining_table")
    chair = pal.get_object(tileset, "chair")
    if not dt or not chair:
        return
    tiles, blocked = dt["tiles"], dt["blocked"]
    mh, mw = len(tiles), len(tiles[0])
    chair_id = int(chair["base_id"])
    for x0, y0, x1, y1 in rooms:
        rw, rh = x1 - x0 + 1, y1 - y0 + 1
        if rw < mw + 4 or rh < mh + 4 or rng.random() > 0.6:
            continue  # 식탁+의자+여백이 들어갈 큰 방만, 확률적으로
        cx = x0 + (rw - mw) // 2
        cy = y0 + (rh - mh) // 2
        spots = [(cx + dx, cy + dy) for dy in range(mh) for dx in range(mw)]
        if any(get_tile(data, sx, sy, width, height, 1) != 0 for sx, sy in spots):
            continue
        if avoid_cells and any(sp in avoid_cells for sp in spots):
            continue  # 입구·문 통로 위엔 식탁 안 둠
        if block_avoid and any(sp in block_avoid for sp in spots):
            continue  # 식탁(막힘)이 통로(관절점)를 막으면 배치 안 함
        for dy in range(mh):
            for dx in range(mw):
                set_tile(data, cx + dx, cy + dy, width, height, 1, int(tiles[dy][dx]))
                if blocked[dy][dx]:
                    set_tile(data, cx + dx, cy + dy, width, height, 5, 1)
        for dx in range(mw):  # 위·아래 줄에 의자(통로는 피함)
            up, down = (cx + dx, cy - 1), (cx + dx, cy + mh)
            if cy - 1 >= y0 and get_tile(data, *up, width, height, 1) == 0 and not (
                block_avoid and up in block_avoid
            ):
                set_tile(data, cx + dx, cy - 1, width, height, 1, chair_id)
            if cy + mh <= y1 and get_tile(data, *down, width, height, 1) == 0 and not (
                block_avoid and down in block_avoid
            ):
                set_tile(data, cx + dx, cy + mh, width, height, 1, chair_id)


def _place_kitchen(
    data: list[int],
    width: int,
    height: int,
    tileset: int,
    rooms: list[tuple[int, int, int, int]],
    rng: random.Random,
) -> tuple[int, int, int, int] | None:
    """방 하나를 부엌으로 정해 위쪽 벽 아래에 부엌 카운터(kitchen_counter 1x4)를 놓는다.

    어느 방이든 '윗칸이 벽이고 가로로 비어 있는 바닥 줄'을 찾아 카운터를 붙인다.
    반환: 부엌으로 쓴 방 영역(없으면 None) — 호출자가 그 방은 커튼 대신 창문만 두게 쓴다.
    """
    kc = pal.get_multitile(tileset, "kitchen_counter")
    if not kc:
        return None
    tiles, blocked = kc["tiles"], kc["blocked"]
    mw = len(tiles[0])  # 1x4 가로
    for x0, y0, x1, y1 in rng.sample(rooms, len(rooms)):
        for yy in range(y0, y1 + 1):
            for kx in range(x0, x1 - mw + 2):
                if all(
                    get_tile(data, kx + dx, yy, width, height, 1) == 0
                    and get_tile(data, kx + dx, yy, width, height, 5) == 0  # 바닥(통행 가능)
                    and yy > 0
                    and get_tile(data, kx + dx, yy - 1, width, height, 5) == 1  # 윗칸이 벽
                    for dx in range(mw)
                ):
                    for dx in range(mw):
                        set_tile(data, kx + dx, yy, width, height, 1, int(tiles[0][dx]))
                        if blocked[0][dx]:
                            set_tile(data, kx + dx, yy, width, height, 5, 1)
                    return (x0, y0, x1, y1)
    return None


def _place_on_furniture(data: list[int], width: int, height: int, tileset: int, rng: random.Random) -> None:
    """작은 소품을 어울리는 가구 위(L2)에 올린다 — 침대 위 곰인형, 책장 위 책더미.

    바닥에 흩어지면 어색한 소품을 가구 타일 위(상위 레이어 L2)에 확률적으로 얹는다.
    침대 앵커(bed_large 좌상=169), 책장 빈선반(bookshelf2 위칸=152) 기준.
    """
    teddy = pal.get_object(tileset, "teddy")
    book = pal.get_object(tileset, "book_stack")
    bed = pal.get_multitile(tileset, "bed_large")
    shelf = pal.get_multitile(tileset, "bookshelf2")
    bed_anchor = int(bed["tiles"][0][0]) if bed else -1
    shelf_top = int(shelf["tiles"][0][0]) if shelf else -1
    for y in range(height):
        for x in range(width):
            b = base_of(get_tile(data, x, y, width, height, 1))
            if teddy and b == bed_anchor and rng.random() < 0.5:
                set_tile(data, x, y, width, height, 2, int(teddy["base_id"]))  # 침대 위 곰인형
            elif book and b == shelf_top and rng.random() < 0.4:
                set_tile(data, x, y, width, height, 2, int(book["base_id"]))  # 책장 위 책더미


def _place_wall_decor(
    data: list[int],
    width: int,
    height: int,
    tileset: int,
    rng: random.Random,
    density: float = 0.16,
    kitchen_room: tuple[int, int, int, int] | None = None,
) -> None:
    """벽에 장식을 겹쳐 건다(L1 오버레이 — 벽 텍스처는 그대로 비친다).

    - 벽 2칸(face 위가 천장 wall_top) & 외벽(천장 위가 집 밖 void): 창문·스테인드글라스
      (세로 2칸)를 천장+면 위 L1 에 겹친다. 바깥과 면한 벽이라 창문이 자연스럽다.
    - 벽 2칸 & 내벽(천장 위가 다른 방): 태피스트리·커튼(벽걸이)을 겹친다(밖이 없어 창문 부적합).
    - 벽 1칸: 방패(shield) 단일 장식을 L1 에 건다.

    장식을 L0 가 아니라 L1 에 둬야 벽(L0)이 뒤에 남아 창문의 투명부에 벽이 비친다(검은
    배경 방지). 통행은 이미 벽(L0)이라 차단된 상태 그대로.
    """
    wall = pal.get_tile_id(tileset, "wall")
    wall_top = pal.get_tile_id(tileset, "wall_top")
    void = pal.get_tile_id(tileset, "void")
    # 벽 1칸짜리 단일 벽장식(인물화·거울·방패) — 벽면 L1 에 건다.
    decor_objs = [pal.get_object(tileset, n) for n in ("portrait", "wall_mirror", "shield")]
    decor_objs = [o for o in decor_objs if o]
    painting_wide = pal.get_multitile(tileset, "painting_wide")  # 2칸 가로 풍경화
    # 창문류(커튼 달린 창 포함)는 바깥과 면한 외벽에만 — 밖이 보여야 자연스럽다.
    outer = [pal.get_multitile(tileset, n) for n in ("window", "stained_glass", "curtain_window")]
    outer = [d for d in outer if d]
    inner = [pal.get_multitile(tileset, n) for n in ("tapestry",)]  # 벽걸이만 내벽
    inner = [d for d in inner if d]
    if not wall:
        return
    for y in range(1, height):
        for x in range(width):
            if base_of(get_tile(data, x, y, width, height, 0)) != wall:
                continue  # 앞면 벽(face)에만
            if base_of(get_tile(data, x, y - 1, width, height, 0)) != wall_top:
                # 벽 1칸(천장 없는 face). 오른쪽도 같은 1칸 벽이면 2칸 풍경화, 아니면 단일 장식
                if get_tile(data, x, y, width, height, 1) != 0:
                    continue
                rfx = x + 1
                right_face = (
                    rfx < width
                    and base_of(get_tile(data, rfx, y, width, height, 0)) == wall
                    and base_of(get_tile(data, rfx, y - 1, width, height, 0)) != wall_top
                    and get_tile(data, rfx, y, width, height, 1) == 0
                )
                if painting_wide and right_face and rng.random() < density * 0.4:
                    t = painting_wide["tiles"][0]
                    set_tile(data, x, y, width, height, 1, int(t[0]))  # 풍경화 왼쪽
                    set_tile(data, rfx, y, width, height, 1, int(t[1]))  # 오른쪽
                elif decor_objs and rng.random() < density * 0.5:
                    set_tile(data, x, y, width, height, 1, int(rng.choice(decor_objs)["base_id"]))
                continue
            # 벽 2칸. 천장 위가 집 밖(void)이면 외벽 → 창문, 아니면 내벽 → 태피스트리.
            # 단 부엌 방은 커튼/태피스트리 없이 창문만 둔다(사용자 요청).
            above = base_of(get_tile(data, x, y - 2, width, height, 0)) if y - 2 >= 0 else void
            in_kitchen = kitchen_room is not None and (
                kitchen_room[0] <= x <= kitchen_room[2] and kitchen_room[1] <= y <= kitchen_room[3]
            )
            pool = outer if ((void and above == void) or in_kitchen) else inner
            if not pool:
                continue
            if get_tile(data, x, y - 1, width, height, 1) != 0 or get_tile(data, x, y, width, height, 1) != 0:
                continue
            if rng.random() < density:
                tiles = rng.choice(pool)["tiles"]
                set_tile(data, x, y - 1, width, height, 1, int(tiles[0][0]))  # 천장 위 L1=상단
                set_tile(data, x, y, width, height, 1, int(tiles[1][0]))  # 면 위 L1=하단


def generate_terrain_map(
    dims: MapDims,
    seed: int = 0,
    autotile: bool = True,
    biome: str = DEFAULT_BIOME,
    objects: bool = True,
    variants: bool = True,
    variant_regions: int = 1,
    paths: bool = True,
) -> list[int]:
    """value noise 고도맵으로 바이옴별 지형을 자연스럽게 배치.

    낮은 고도=물/저지, 높은 고도=고지. 바이옴(_BIOMES)이 고도→지형 매핑을 정한다.
    저주파 노이즈로 큰 덩어리를 만들고, majority 스무딩으로 작은 조각을 정리한다.
    paths=True 면 grass 위에 흙길을 깔아 바닥(L0)에 변화를 준다.
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

    if paths:  # autotile 전에 — 흙길 가장자리 shape 가 보정되도록
        _place_path(data, w, h, tid, s + 71)
    if autotile:
        apply_autotile(data, w, h, layer=0, oob_connected=True)
    # 바이옴 물(2240 등 palette 외 id 포함)도 통행 불가로 보장
    impass = set(pal.impassable_ids(tid)) | {_water_id(biome, tid)}
    _set_passability_by_base(data, w, h, impass)

    if objects:
        rng = random.Random(s + 99)
        # L1 풀밭 텍스처(샘플맵 L1 본체) → L3 나무 군집 → L2/L3 단일 식생 순.
        # 막힘 오브젝트(나무·바위)는 관절점(통로)을 막지 않도록 _blocking_avoid 회피.
        _place_ground_decor(data, w, h, tid, biome, s + 5, autotile=autotile)
        ba = _blocking_avoid(data, w, h)  # 나무 배치 전 통로(관절점) 계산
        _place_multitile(data, w, h, tid, biome, rng, layer=3, cluster_seed=s + 41, block_avoid=ba)
        ba = _blocking_avoid(data, w, h)  # 나무 반영 후 재계산
        _place_objects(data, w, h, tid, biome, rng, block_avoid=ba)  # 단일 식생(막힘은 통로 회피)
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
        # 던전 복도(1칸 통로)를 막힘 오브젝트가 막지 않도록 관절점 회피
        ba = _blocking_avoid(data, w, h)
        _place_multitile(data, w, h, 4, biome, rng, block_avoid=ba)
        ba = _blocking_avoid(data, w, h)
        _place_objects(data, w, h, 4, biome, rng, block_avoid=ba)
    return data


def _partition_house(
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    rng: random.Random,
    min_room: int,
    walls: set[tuple[int, int]],
    doors: list[tuple[int, int]],
    rooms: list[tuple[int, int, int, int]],
    depth: int = 0,
) -> None:
    """집 내부 영역(x0,y0)~(x1,y1)을 빈틈없이 방으로 재귀 분할(BSP).

    던전과 달리 방 사이 여백·복도가 없다 — 분할선이 곧 내벽(walls)이고, 각 분할마다
    내벽 중간 한 칸을 문(doors)으로 뚫어 양쪽 방을 잇는다(트리 구조라 전체 연결 보장).
    더 못 나누거나 확률적으로 멈추면 그 영역을 방(rooms)으로 확정한다.
    """
    w, h = x1 - x0 + 1, y1 - y0 + 1
    can_v = w >= min_room * 2 + 1  # 세로 내벽으로 좌우 분할 가능
    can_h = h >= min_room * 2 + 1  # 가로 내벽으로 상하 분할 가능
    if (not can_v and not can_h) or (depth >= 2 and rng.random() < 0.4):
        rooms.append((x0, y0, x1, y1))
        return
    vertical = can_v if not can_h else (w >= h)
    if vertical:
        sx = rng.randint(x0 + min_room, x1 - min_room)  # 내벽 위치
        for y in range(y0, y1 + 1):
            walls.add((sx, y))
        doors.append((sx, rng.randint(y0 + 1, y1 - 1)))  # 문(양 끝 제외 중간)
        _partition_house(x0, y0, sx - 1, y1, rng, min_room, walls, doors, rooms, depth + 1)
        _partition_house(sx + 1, y0, x1, y1, rng, min_room, walls, doors, rooms, depth + 1)
    else:
        sy = rng.randint(y0 + min_room, y1 - min_room)
        for x in range(x0, x1 + 1):
            walls.add((x, sy))
        doors.append((rng.randint(x0 + 1, x1 - 1), sy))
        _partition_house(x0, y0, x1, sy - 1, rng, min_room, walls, doors, rooms, depth + 1)
        _partition_house(x0, sy + 1, x1, y1, rng, min_room, walls, doors, rooms, depth + 1)


def generate_house_map(
    dims: MapDims,
    seed: int = 0,
    objects: bool = True,
    tileset: int = 3,
    biome: str = "interior",
    floor_name: str = "floor",
    wall_name: str = "wall",
    wall_top_name: str = "wall_top",
    variants: bool = True,
) -> list[int]:
    """집 한 채 실내 생성 — 큰 직사각형 집을 내벽으로 방 분할, 문으로 연결(거주 공간).

    던전 BSP(좁은 방+복도 미로)와 달리: 집 전체가 floor 로 차고, 내벽이 방을 빈틈없이
    나누며, 복도 없이 문으로 잇는다. 일부 방엔 바닥을 타일(tile_floor)로 바꾸거나 중앙에
    카펫(carpet) 러그를 깐다. 가구는 벽을 따라 정돈 배치하고, 벽 오른쪽에 그림자를 둔다.
    집 밖은 void(검은 공백).
    """
    w, h = dims.width, dims.height
    s = seed or w * h
    base_floor = pal.get_tile_id(tileset, floor_name)
    floor = _pick_variant(tileset, base_floor, random.Random(s + 7)) if variants else base_floor
    carpet = pal.get_tile_id(tileset, "carpet")
    wall = pal.get_tile_id(tileset, wall_name)
    wall_top = pal.get_tile_id(tileset, wall_top_name)
    vt = pal.get_terrain(tileset, "void")
    void = int(vt["base_id"]) if vt else wall_top

    rng = random.Random(s)
    data = make_empty_data(w, h)
    for y in range(h):
        for x in range(w):
            set_tile(data, x, y, w, h, 0, void)  # 집 밖 = 공백

    # 집 영역(여백 1칸) 내부를 바닥으로 채우고 외벽으로 두름
    mx0, my0, mx1, my1 = 1, 1, w - 2, h - 2
    for y in range(my0, my1 + 1):
        for x in range(mx0, mx1 + 1):
            edge = x in (mx0, mx1) or y in (my0, my1)
            set_tile(data, x, y, w, h, 0, wall_top if edge else floor)

    # 집 내부를 방으로 분할(내벽 + 문)
    walls: set[tuple[int, int]] = set()
    doors: list[tuple[int, int]] = []
    rooms: list[tuple[int, int, int, int]] = []
    _partition_house(mx0 + 1, my0 + 1, mx1 - 1, my1 - 1, rng, 4, walls, doors, rooms)
    for x, y in walls:
        set_tile(data, x, y, w, h, 0, wall_top)
    for x, y in doors:
        set_tile(data, x, y, w, h, 0, floor)  # 문 = 내벽 뚫기

    # 집 아래 외벽 가운데에 입구(벽 없는 통로) — 플레이어가 밖에서 들어오는 진입점.
    # 아래 외벽부터 위로 첫 방 바닥을 만날 때까지 floor 로 뚫어 통로를 만든다.
    entry_x = (mx0 + mx1) // 2
    entry_cells: list[tuple[int, int]] = []
    for yy in range(my1, my0 - 1, -1):
        prev = base_of(get_tile(data, entry_x, yy, w, h, 0))
        set_tile(data, entry_x, yy, w, h, 0, floor)
        entry_cells.append((entry_x, yy))
        if yy < my1 and prev == base_of(floor):
            break  # 방 바닥에 닿으면 통로 완성

    # 통로 막힘 방지: 문·입구 칸에는 가구를 두지 않는다(좁은 길을 막지 않게)
    avoid: set[tuple[int, int]] = set(doors) | set(entry_cells)

    # 방마다 바닥 종류를 풀에서 골라 다양화(마루·나무결·돌·벽돌·타일) + 일부 방 중앙에 카펫 러그
    floor_pool = [base_floor]
    for nm in ("wood_floor2", "stone_floor", "brick_floor", "fancy_tile", "tile_floor"):
        fid = pal.get_tile_id(tileset, nm)
        if fid:
            floor_pool.append(fid)
    for x0, y0, x1, y1 in rooms:
        rw, rh = x1 - x0 + 1, y1 - y0 + 1
        room_floor = rng.choice(floor_pool)
        if room_floor != base_of(floor):
            for y in range(y0, y1 + 1):
                for x in range(x0, x1 + 1):
                    if base_of(get_tile(data, x, y, w, h, 0)) == base_of(floor):
                        set_tile(data, x, y, w, h, 0, room_floor)
        if carpet and rw >= 5 and rh >= 5 and rng.random() < 0.45:  # 중앙 카펫 러그
            cur = base_of(get_tile(data, x0 + 1, y0 + 1, w, h, 0))  # 방 바닥색
            for y in range(y0 + 1, y1):
                for x in range(x0 + 1, x1):
                    if base_of(get_tile(data, x, y, w, h, 0)) == cur:
                        set_tile(data, x, y, w, h, 0, carpet)

    # 입체 벽: 아래가 바닥인 벽 → 앞면(face). floors = 모든 바닥 종류 + 카펫.
    # 집마다 벽 높이를 1칸/2칸으로 다양화 — 2칸이면 천장(top) 아래에 면(face)이 한 줄 더
    # 생겨 세로 2칸 창문·태피스트리가 들어갈 자리가 된다(_place_wall_decor 가 채움).
    floors = set(floor_pool)
    if carpet:
        floors.add(carpet)
    # 문·입구 칸은 면(face)으로 막으면 안 됨(통로 끊김) — 벽 2칸일 때 보존
    passage = set(doors) | set(entry_cells)
    wall_height = rng.choice((1, 2))
    for y in range(h):
        for x in range(w):
            if base_of(get_tile(data, x, y, w, h, 0)) == wall_top:
                below = base_of(get_tile(data, x, y + 1, w, h, 0)) if y + 1 < h else -1
                if below in floors:
                    if wall_height == 2 and y + 1 < h and (x, y + 1) not in passage:
                        set_tile(data, x, y + 1, w, h, 0, wall)  # 천장 아래 칸=면(벽 2칸)
                    elif wall_height == 1:
                        set_tile(data, x, y, w, h, 0, wall)  # 천장을 면으로(벽 1칸)

    impass = set(pal.impassable_ids(tileset)) | {wall, wall_top}
    _set_passability_by_base(data, w, h, impass)

    if objects:
        orng = random.Random(s + 99)
        ft = {b for b in floor_pool}  # 모든 방 바닥에 가구가 놓이게(카펫 러그는 제외해 비움)
        kitchen_room = _place_kitchen(data, w, h, tileset, rooms, orng)  # 한 방을 부엌으로
        _place_wall_decor(data, w, h, tileset, orng, kitchen_room=kitchen_room)  # 부엌은 창문만
        # 막힘 가구·식탁이 문·통로(관절점)를 막지 않도록 _blocking_avoid 적용
        ba = _blocking_avoid(data, w, h)
        _place_dining_set(data, w, h, tileset, rooms, orng, avoid, block_avoid=ba)  # 식탁세트
        ba = _blocking_avoid(data, w, h)
        _place_multitile(
            data, w, h, tileset, biome, orng, against_wall=True, floor_targets=ft,
            avoid_cells=avoid, block_avoid=ba,
        )  # 가구 벽에 등 댐
        ba = _blocking_avoid(data, w, h)
        _place_objects(
            data, w, h, tileset, biome, orng, wall_adjacent=True, floor_targets=ft,
            avoid_cells=avoid, block_avoid=ba,
        )  # 단일 가구 벽 따라
        _place_on_furniture(data, w, h, tileset, orng)  # 침대 위 곰인형·책장 위 책
    # 분리된 영역(문 막힘·가구 막힘·고립 방)을 벽 1칸 뚫어 연결 — 모든 방 도달 보장.
    _connect_regions(data, w, h, base_floor)
    # 연결 시 가구를 관통해 뚫었으면 반쪽만 남은 가구를 통째로 제거(반쪽 침대·식탁 방지).
    if objects:
        _clean_partial_multitiles(data, w, h, tileset)
    # 못 잇는 작은 고립 칸(문 없는 1칸 방 등)은 벽으로 메워 '섬'을 없앤다.
    _fill_isolated_pockets(data, w, h, wall_top)
    # 그림자는 건물 벽에만(가구 제외). 벽장식(창문 등)도 벽이므로 wall_bases 에 포함.
    wall_bases = {wall, wall_top}
    for n in ("window", "tapestry", "curtain_window", "stained_glass"):
        mt = pal.get_multitile(tileset, n)
        if mt:
            for row in mt["tiles"]:
                wall_bases.update(int(t) for t in row)
    _place_wall_shadows(data, w, h, wall_bases)
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
    _place_wall_shadows(data, w, h, {wall, wall_top})  # 그림자는 벽에만(가구 제외)
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
        data = generate_house_map(
            dims,
            seed=args.seed,
            objects=not args.no_objects,
            variants=not args.no_variants,
        )
        mode_label = "interior/house"
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
