"""Blueprint 컴파일러 — LLM이 쓴 '의미적 설계도'를 예쁜 타일 맵으로 컴파일한다.

핵심 아이디어(역할 분담):
  - LLM(감독)  : "무엇을 어디에" 를 정한다 → Blueprint(JSON). raw 타일은 절대 안 뱉음.
  - 컴파일러(렌더러): 설계도를 받아 바닥 오토타일·집 스탬프·길·경계를 결정론적으로 조립.
                 미학(L2 장면 구성)은 vignette(사람이 만든 집)에서 상속, 정확성은 코드가 보장.

이 파일은 프로토타입(실험용). 기존 palette_mapgen/town_generator 는 건드리지 않는다.

Blueprint 스키마(= LLM 출력 계약):
  {
    "size": [W, H],
    "tileset": 2,
    "ground": "grass",            # 바닥 지형 이름(palette terrain)
    "houses": [ {"vignette": "green_house", "x": 3, "y": 3}, ... ],
    "paths":  [ [x0,y0,x1,y1], ... ],   # 직교 길 세그먼트(L자 연결)
    "tree_border": true,
    "gardens": [ [x0,y0,x1,y1], ... ]   # 꽃·풀을 촘촘히 까는 정원 구획
  }
"""

from __future__ import annotations

import random

from agent.generation.mapgen import palette as pal
from agent.generation.mapgen.autotile import apply_autotile, base_of
from agent.generation.mapgen.palette_mapgen import _value_noise
from agent.generation.mapgen.tile_constants import get_tile, make_empty_data, set_tile
from agent.generation.mapgen.vignette import Vignette, stamp

# 바이옴별 길 재질 기본값 — A2 길 타일의 전이 가장자리는 바닥색이 구워져 있어 바닥과
# 맞아야 자연스럽다(stone_path 초록=잔디용, 눈엔 cobble_grey2 회색, 사막엔 cobble_tan).
_THEME_PATH: dict[str, str] = {
    "grassland": "stone_path",
    "wetland": "stone_path",
    "snow": "cobble_grey2",
    "desert": "cobble_tan",
}
# 바이옴별 바닥 지형 기본값(blueprint 가 ground 를 안 주면 사용).
_THEME_GROUND: dict[str, str] = {
    "grassland": "grass",
    "wetland": "grass",
    "snow": "snow",
    "desert": "sand",
}

# 테마별 식생·소품 풀 — (palette objects 이름, 배치 확률). 건물 프레이밍과 정원에 공용.
# 잔디 자산을 눈·사막에 그대로 쓰면 초록이 뜨므로 테마마다 따로 골라야 한다.
# 눈 목록 근거: 눈 샘플맵 11개 L3 실측 상위(눈뭉치·눈 위 풀·장작더미·관목) + flags 통행성.
_THEME_FLORA: dict[str, tuple[tuple[str, float], ...]] = {
    "grassland": (
        ("bush_clump", 0.30),
        ("shrub", 0.22),
        ("grass_tuft", 0.28),
        ("flower", 0.18),
        ("berry_bush", 0.12),
    ),
    # 확률 근거: 눈 샘플맵 11개의 **소품 커버리지는 2.4%**(최대 5%)뿐이다. 나열 순서대로
    # 시도해 첫 성공에서 멈추므로 셀당 배치율은 합계에 수렴 → 합 0.055 로 맞춰 잡았다
    # (프레이밍 링·정원이 맵의 40% 가까이라 셀당 확률이 곧 전체 커버리지가 되지 않는다).
    # 잔디 풀은 합이 1에 가까워 링 전체를 덮는다 — 눈은 실측이 훨씬 희소해 그대로 쓰면 도배됨.
    "snow": (
        ("town_snow_218", 0.015),
        ("town_snow_217", 0.011),
        ("town_snow_220", 0.010),
        ("town_snow_219", 0.008),
        ("town_flora_230", 0.005),
        ("town_snow_223", 0.004),
        ("town_flora_226", 0.002),
    ),
}
_THEME_FLORA["wetland"] = _THEME_FLORA["grassland"]

# 테마별 나무(palette multitile 이름, 선택 가중치). 눈은 3종을 섞어 실루엣을 다양화한다
# (snow_fir 1x2 전나무 / snow_tree 2x2 작은 눈나무 / snow_tree_large 2x2 빽빽한 눈나무).
# 2x2 나무를 균등하게 뽑으면 군집에서 사각 타일이 맞물려 체크무늬처럼 정렬된다 — 폭이
# 1칸인 전나무를 주력으로 두고 2x2 는 악센트로 섞어야 숲 실루엣이 흐트러진다.
_THEME_TREES: dict[str, tuple[tuple[str, float], ...]] = {
    "grassland": (("tree", 1.0),),
    "wetland": (("tree", 1.0),),
    "snow": (("snow_fir", 0.6), ("snow_tree", 0.3), ("snow_tree_large", 0.1)),
}
# 정원 구획에 나무가 섞일 확률. 눈 마을은 샘플맵도 L3 의 대부분이 나무라 숲처럼 채운다.
_THEME_GARDEN_TREE: dict[str, float] = {"grassland": 0.03, "wetland": 0.05, "snow": 0.12}
# 맵 가장자리 나무 경계의 밴드 폭·시도 확률.
_BORDER_BAND = 3
_BORDER_PROB = 0.35
# 건물 밖 빈 공간에 심는 숲 군집(테마별 시도 확률). 노이즈 임계를 넘은 덩어리에만 심어
# 나무가 뭉쳐 숲이 되게 한다 — 눈 샘플맵 실측 나무 커버리지 13.1% 를 목표로 맞춘 값.
_THEME_FOREST: dict[str, float] = {"snow": 0.35}
_FOREST_THRESHOLD = 0.62


def _fill_ground(data: list[int], W: int, H: int, tid: int, ground: str) -> None:
    gid = pal.get_tile_id(tid, ground)
    for y in range(H):
        for x in range(W):
            set_tile(data, x, y, W, H, 0, gid)


def _draw_path(data: list[int], W: int, H: int, tid: int, seg: list[int], path_name: str) -> None:
    """직교 L자 길 세그먼트를 폭 2로 바닥(L0)에 깐다."""
    pid = pal.get_tile_id(tid, path_name)
    if not pid:
        return
    x0, y0, x1, y1 = seg
    # 가로 먼저, 세로 나중(L자)
    for x in range(min(x0, x1), max(x0, x1) + 1):
        for dy in (0, 1):
            set_tile(data, x, y0 + dy, W, H, 0, pid)
    for y in range(min(y0, y1), max(y0, y1) + 1):
        for dx in (0, 1):
            set_tile(data, x1 + dx, y, W, H, 0, pid)


# 테마별 바닥 텍스처(L1 에 겹쳐 까는 A2 데코) — (ground_decor 이름, 대상 지형, 커버리지).
# 커버리지는 대상 지형 칸 중 덮을 비율(분위수 임계라 값이 곧 비율이다).
# snow 0.10 근거: 눈 샘플맵 11개 실측 눈더미 커버리지 9.6%(중앙값 8.8%).
# grassland/desert 는 아직 안 켠다 — 잔디 맵 밀도는 이미 승인된 상태라 별도 실측 후 조정.
_THEME_GROUND_DECOR: dict[str, tuple[tuple[str, str, float], ...]] = {
    "snow": (("snow_patch", "snow", 0.10),),
}

_TREE_LAYER = 3  # 나무는 L3 — 샘플맵 실측 본체 레이어이고, L1 바닥 텍스처와 겹쳐 깔린다.


def _place_ground_texture(data: list[int], W: int, H: int, tid: int, theme: str, seed: int) -> None:
    """바닥(L0) 위 L1 에 A2 지면 텍스처를 노이즈 덩어리로 겹쳐 깐다(단색 바닥 제거).

    palette_mapgen._place_ground_decor 와 같은 아이디어지만 임계를 **분위수**로 잡는다.
    절대 임계(`1 - coverage`)는 bilinear 보간이 노이즈 극값을 깎기 때문에 실제 비율과
    어긋나고, 같은 설정에서도 seed 에 따라 2%~23% 로 요동쳤다. 분위수는 필드 분포에
    상대적이라 seed 가 바뀌어도 커버리지가 안정적이다.

    포장·길 칸은 L0 가 대상 지형이 아니라 자동으로 제외된다. ground_decor 는 통행 가능이라
    통행성에 영향이 없다. 끝에 L1 오토타일로 패치 경계를 자연스럽게 굽는다.
    """
    import numpy as np

    specs = _THEME_GROUND_DECOR.get(theme, ())
    placed = False
    for i, (name, target, coverage) in enumerate(specs):
        dec = pal.get_ground_decor(tid, name)
        tgt = pal.get_tile_id(tid, target)
        if not dec or not tgt:
            continue
        nz = _value_noise(W, H, cells=max(2, min(W, H) // 8), seed=seed + 17 * (i + 1))
        thr = float(np.quantile(nz, 1.0 - coverage))
        base_id = int(dec["base_id"])
        for y in range(H):
            for x in range(W):
                if get_tile(data, x, y, W, H, 1) != 0:
                    continue  # 먼저 깐 텍스처 우선(겹침 방지)
                if base_of(get_tile(data, x, y, W, H, 0)) != tgt:
                    continue
                if float(nz[y, x]) >= thr:
                    set_tile(data, x, y, W, H, 1, base_id)
                    placed = True
    if placed:
        apply_autotile(data, W, H, layer=1, oob_connected=False)


def _place_trees(
    data: list[int],
    W: int,
    H: int,
    tid: int,
    theme: str,
    ground_id: int,
    cells: list[tuple[int, int]],
    occupied: set[tuple[int, int]],
    rng: random.Random,
    prob: float,
    cluster_seed: int | None = None,
) -> None:
    """주어진 칸들에 테마 나무(멀티타일)를 확률적으로 심는다.

    나무 종류마다 크기가 달라(1x2 전나무·2x2 눈나무) footprint 전체가 빈 바닥일 때만 놓는다.
    맵 경계 밴드에 쓰면 자연 경계, 정원 구획에 쓰면 숲 군집이 된다.

    cluster_seed 를 주면 저주파 노이즈 임계를 통과한 칸에만 심는다 — 균일 확률로 뿌리면
    나무가 밴드에 일렬로 서서 '심어놓은 가로수'처럼 보인다. 덩어리로 뭉쳐야 숲이 된다.
    """
    specs = [
        (spec, wgt)
        for name, wgt in _THEME_TREES.get(theme, ())
        if (spec := pal.get_multitile(tid, name))
    ]
    if not specs:
        return
    trees = [s for s, _ in specs]
    weights = [w for _, w in specs]
    mask = None
    if cluster_seed is not None:
        mask = _value_noise(W, H, cells=max(2, min(W, H) // 7), seed=cluster_seed)
    for x, y in cells:
        if mask is not None and float(mask[y, x]) < _FOREST_THRESHOLD:
            continue
        if rng.random() >= prob:
            continue
        spec = rng.choices(trees, weights=weights, k=1)[0]
        tiles, blocked = spec["tiles"], spec["blocked"]
        mh, mw = len(tiles), len(tiles[0])
        foot = [(x + dx, y + dy) for dy in range(mh) for dx in range(mw)]
        if any(
            not (0 <= cx < W and 0 <= cy < H)
            or (cx, cy) in occupied
            or base_of(get_tile(data, cx, cy, W, H, 0)) != ground_id
            or get_tile(data, cx, cy, W, H, _TREE_LAYER) != 0
            for cx, cy in foot
        ):
            continue
        for dy in range(mh):
            for dx in range(mw):
                set_tile(data, x + dx, y + dy, W, H, _TREE_LAYER, int(tiles[dy][dx]))
                if blocked[dy][dx]:
                    set_tile(data, x + dx, y + dy, W, H, 5, 1)
                occupied.add((x + dx, y + dy))


def _border_cells(W: int, H: int, rng: random.Random) -> list[tuple[int, int]]:
    """맵 가장자리 밴드의 칸 목록(셔플) — 나무 경계 시도 순서."""
    cells = [
        (x, y)
        for y in range(H)
        for x in range(W)
        if x < _BORDER_BAND or x >= W - _BORDER_BAND or y < _BORDER_BAND or y >= H - _BORDER_BAND
    ]
    rng.shuffle(cells)
    return cells


def _plaza_pad(
    data: list[int], W: int, H: int, box: tuple[int, int, int, int], pid: int, pad: int = 1
) -> None:
    """건물 footprint bbox 를 pad 칸 넓혀 그 안 바닥(L0)을 포장재로 채운다.

    오토타일 **전에** 호출해야 잔디↔포장 가장자리가 자연스럽게 구워지고, 이후 집이 그 위에
    스탬프되어 건물 주변만 '포장된 마당'으로 남는다(맨잔디 위에 뜬 느낌 제거 — 샘플맵의
    '개발된 구역' 텍스처). 오목한 코너·처마 밑도 포장이 메워 실루엣이 또렷해진다.
    """
    if not pid:
        return
    x0, y0, x1, y1 = box
    for y in range(max(0, y0 - pad), min(H, y1 + pad + 1)):
        for x in range(max(0, x0 - pad), min(W, x1 + pad + 1)):
            set_tile(data, x, y, W, H, 0, pid)


def _flora_pool(tid: int, theme: str) -> list[tuple[dict, float]]:
    """테마 식생 풀 — (palette object, 확률). 팔레트에 없는 이름은 조용히 빠진다."""
    out = []
    for name, dens in _THEME_FLORA.get(theme, ()):
        obj = pal.get_object(tid, name)
        if obj:
            out.append((obj, dens))
    return out


def _scatter_flora(
    data: list[int],
    W: int,
    H: int,
    ground_id: int,
    pool: list[tuple[dict, float]],
    cells: list[tuple[int, int]],
    occupied: set[tuple[int, int]],
    rng: random.Random,
    scale: float = 1.0,
) -> None:
    """주어진 칸들에 테마 식생을 흩는다(바닥이 그 테마 지형이고 비어 있는 칸만).

    점유 판정은 해당 오브젝트가 쓰는 레이어로 한다 — L1 은 바닥 텍스처(ground_decor)가
    덮고 있어서 L1 유무로 걸러 버리면 데코를 깐 맵에서 식생이 전멸한다.
    """
    for x, y in cells:
        if (x, y) in occupied or base_of(get_tile(data, x, y, W, H, 0)) != ground_id:
            continue
        for obj, dens in pool:
            if rng.random() < dens * scale:
                layer = int(obj.get("layer", 3))
                if get_tile(data, x, y, W, H, layer) != 0:
                    break
                set_tile(data, x, y, W, H, layer, int(obj["base_id"]))
                if not obj.get("passable", True):
                    set_tile(data, x, y, W, H, 5, 1)
                occupied.add((x, y))
                break


def _frame_buildings(
    data: list[int],
    W: int,
    H: int,
    tid: int,
    theme: str,
    ground_id: int,
    boxes: list[tuple[int, int, int, int]],
    occupied: set[tuple[int, int]],
    rng: random.Random,
    radius: int = 3,
) -> None:
    """각 건물 포장마당 바깥의 바닥 링에 테마 식생을 흩어 건물을 초목으로 감싼다.

    포장 pad(bbox+1) 은 건너뛰고 그 바깥 band(bbox+2..bbox+radius) 에만 배치 →
    건물→포장마당→초목 프레이밍(샘플맵처럼 건물이 환경에 녹아든다). 건물 사이 빈 바닥도
    함께 줄인다. 이미 점유된 칸(집·길·정원·나무)·다른 지형 칸은 건너뛴다.
    """
    pool = _flora_pool(tid, theme)
    if not pool:
        return
    for x0, y0, x1, y1 in boxes:
        cells = [
            (x, y)
            for y in range(max(0, y0 - radius), min(H, y1 + radius + 1))
            for x in range(max(0, x0 - radius), min(W, x1 + radius + 1))
            # 포장 pad 안은 건너뜀 — 바깥 링만 감싼다
            if not (x0 - 1 <= x <= x1 + 1 and y0 - 1 <= y <= y1 + 1)
        ]
        _scatter_flora(data, W, H, ground_id, pool, cells, occupied, rng)


def _plant_gardens(
    data: list[int],
    W: int,
    H: int,
    tid: int,
    theme: str,
    ground_id: int,
    gardens: list[list[int]],
    occupied: set[tuple[int, int]],
    rng: random.Random,
) -> None:
    """정원 구획을 테마 식생으로 촘촘히 채운다(맵 전체 도배가 아니라 '구획'에만).

    나무도 낮은 확률로 섞는다 — 눈 마을 샘플맵은 L3 의 대부분이 나무라 정원이 아니라
    설원 숲처럼 채워야 자연스럽다(테마별 확률 `_THEME_GARDEN_TREE`).
    """
    pool = _flora_pool(tid, theme)
    if not pool:
        return
    tree_prob = _THEME_GARDEN_TREE.get(theme, 0.0)
    for x0, y0, x1, y1 in gardens:
        cells = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
        if tree_prob:
            shuffled = list(cells)
            rng.shuffle(shuffled)
            _place_trees(data, W, H, tid, theme, ground_id, shuffled, occupied, rng, tree_prob)
        _scatter_flora(data, W, H, ground_id, pool, cells, occupied, rng, scale=1.2)


def compile_blueprint(
    blueprint: dict, vignettes: dict[str, Vignette], seed: int = 0
) -> tuple[list[int], int, int, int]:
    """Blueprint + vignette 라이브러리 → (data, W, H, tileset_id).

    순서(핵심): 바닥·길을 먼저 **오토타일까지** 마치고 → 집을 나중에 스탬프한다.
    스탬프 뒤에 오토타일을 재실행하면 집이 가져온 (원본 지형에 맞던) 지면 가장자리를
    새 바닥 이웃 기준으로 다시 계산해 엉뚱한 전이 타일(예: 눈 위인데 잔디색 테두리)을
    굽는다. 그래서 집 스탬프는 오토타일 **이후**에 얹어 원본 가장자리를 보존한다.
    """
    rng = random.Random(seed)
    W, H = blueprint["size"]
    tid = blueprint.get("tileset", 2)
    data = make_empty_data(W, H)
    theme = blueprint.get("theme", "grassland")
    ground = blueprint.get("ground") or _THEME_GROUND.get(theme, "grass")
    path_name = blueprint.get("path") or _THEME_PATH.get(theme, "stone_path")
    pid = pal.get_tile_id(tid, path_name)
    houses = blueprint.get("houses", [])
    street = blueprint.get("street")

    # 1) 바닥
    _fill_ground(data, W, H, tid, ground)

    # 2) 큰길(street, 가로 2칸) + 각 집 door-anchor→큰길 진입로 — 전부 오토타일 전에 그린다
    if street:
        sy, sx0, sx1 = street["y"], street.get("x0", 0), street.get("x1", W - 1)
        for x in range(sx0, sx1 + 1):
            for dy in (0, 1):
                set_tile(data, x, sy + dy, W, H, 0, pid)
    house_masks: list[tuple[Vignette, int, int, set]] = []
    boxes: list[tuple[int, int, int, int]] = []  # 건물 footprint bbox(플라자·프레이밍용)
    do_plaza = blueprint.get("plaza", True)
    for h in houses:
        vig = vignettes.get(h["vignette"])
        if not vig:
            continue
        hx, hy = h["x"], h["y"]
        mask = vig.structural_mask()
        house_masks.append((vig, hx, hy, mask))
        # footprint bbox(대상 좌표) — 플라자 pad(오토타일 전)·프레이밍(스탬프 후) 공용
        if mask:
            xs = [hx + mx for mx, _ in mask]
            ys = [hy + my for _, my in mask]
            box = (min(xs), min(ys), max(xs), max(ys))
            boxes.append(box)
            if do_plaza:
                # 마당 재질은 **그 구조물이 원본에서 딛고 있던 지형**을 우선한다(vignette.ground).
                # 성은 흙 마당 위에, 여관은 눈길 위에 있었다 — 그 바닥을 깔아주면 건물이 자기
                # 원래 자리처럼 앉는다. 팔레트에 없거나 정보가 없으면 테마 기본 길로 폴백.
                apron = pal.get_tile_id(tid, vig.apron_ground or "") or pid
                _plaza_pad(data, W, H, box, apron, pad=1)
        if street and pid:  # 문 앞 → 큰길까지 수직 진입로(폭 2)
            ax, ay = vig.door_anchor(mask)
            gx, gy = hx + ax, hy + ay
            lo, hi = sorted((gy + 1, street["y"]))
            for yy in range(lo, hi + 1):
                for dx in (0, 1):
                    set_tile(data, gx + dx, yy, W, H, 0, pid)
    for seg in blueprint.get("paths", []):
        _draw_path(data, W, H, tid, seg, path_name)

    # 3) 바닥/길 오토타일 (집 스탬프 전에 완료 — 이후 재실행 안 함)
    apply_autotile(data, W, H, layer=0, oob_connected=True)

    # 3b) 바닥 텍스처(L1) — 눈더미·풀밭 A2 패치를 노이즈 덩어리로 겹쳐 단색 바닥을 없앤다.
    # 샘플맵 L1 의 본체(눈 마을 실측 16%)이고, 포장·길 칸은 L0 가 지형이 아니라 자동 제외된다.
    # 집 스탬프 전에 깔아야 건물 실루엣 안쪽이 원본 L1 로 덮인다.
    if blueprint.get("ground_decor", True):
        _place_ground_texture(data, W, H, tid, theme, seed)

    # 4) 구조물 스탬프 — 원본 가장자리 보존(오토타일 재계산 안 당함)
    occupied: set[tuple[int, int]] = set()
    for vig, hx, hy, mask in house_masks:
        occupied |= stamp(data, W, H, vig, hx, hy, mask=mask)

    # 4b) 소품(props: 분수·우물·석상 등) — 길 연결 없이 그 자리에 얹는다(플라자 악센트)
    for p in blueprint.get("props", []):
        vig = vignettes.get(p["vignette"])
        if vig:
            occupied |= stamp(data, W, H, vig, p["x"], p["y"], mask=vig.structural_mask())

    # 4c) 건물 프레이밍 — 포장마당 바깥 바닥 링을 초목으로 감싸 공백을 줄이고 환경에 녹인다
    ground_id = pal.get_tile_id(tid, ground)
    if blueprint.get("frame", True) and boxes:
        _frame_buildings(data, W, H, tid, theme, ground_id, boxes, occupied, rng)

    # 5) 정원(구획 한정) → 6) 나무 경계 → 7) 숲 군집
    _plant_gardens(data, W, H, tid, theme, ground_id, blueprint.get("gardens", []), occupied, rng)
    if blueprint.get("tree_border"):
        _place_trees(
            data,
            W,
            H,
            tid,
            theme,
            ground_id,
            _border_cells(W, H, rng),
            occupied,
            rng,
            _BORDER_PROB,
            cluster_seed=seed + 91,
        )
    # 건물·길 밖에 남은 빈 바닥을 숲 군집으로 채운다(맵 중앙이 휑하게 남는 문제).
    forest = _THEME_FOREST.get(theme, 0.0)
    if forest and blueprint.get("forest", True):
        cells = [(x, y) for y in range(H) for x in range(W)]
        rng.shuffle(cells)
        _place_trees(
            data,
            W,
            H,
            tid,
            theme,
            ground_id,
            cells,
            occupied,
            rng,
            forest,
            cluster_seed=seed + 137,
        )
    return data, W, H, tid
