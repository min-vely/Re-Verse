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


def _tree_border(
    data: list[int], W: int, H: int, tid: int, occupied: set[tuple[int, int]], rng: random.Random
) -> None:
    """맵 가장자리를 나무로 둘러 자연 경계를 만든다(집·길 위는 피함)."""
    tree = pal.get_multitile(tid, "tree")
    if not tree:
        return
    tiles, blocked = tree["tiles"], tree["blocked"]
    mh, mw = len(tiles), len(tiles[0])
    grass_bases = {pal.get_tile_id(tid, "grass")}

    def place(x: int, y: int) -> None:
        cells = [(x + dx, y + dy) for dy in range(mh) for dx in range(mw)]
        if any(
            not (0 <= cx < W and 0 <= cy < H)
            or (cx, cy) in occupied
            or base_of(get_tile(data, cx, cy, W, H, 0)) not in grass_bases
            or get_tile(data, cx, cy, W, H, 1) != 0
            for cx, cy in cells
        ):
            return
        for dy in range(mh):
            for dx in range(mw):
                set_tile(data, x + dx, y + dy, W, H, 1, int(tiles[dy][dx]))
                if blocked[dy][dx]:
                    set_tile(data, x + dx, y + dy, W, H, 5, 1)
                occupied.add((x + dx, y + dy))

    band = 3
    for x in range(0, W, mw):
        for y in list(range(0, band)) + list(range(H - band - mh, H - mh + 1)):
            if rng.random() < 0.75:
                place(x, y)
    for y in range(0, H, mh):
        for x in list(range(0, band)) + list(range(W - band - mw, W - mw + 1)):
            if rng.random() < 0.75:
                place(x, y)


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


def _frame_buildings(
    data: list[int],
    W: int,
    H: int,
    tid: int,
    boxes: list[tuple[int, int, int, int]],
    occupied: set[tuple[int, int]],
    rng: random.Random,
    radius: int = 3,
) -> None:
    """각 건물 포장마당 바깥의 잔디 링에 관목·꽃·풀을 흩어 건물을 초목으로 감싼다.

    포장 pad(bbox+1) 은 건너뛰고 그 바깥 band(bbox+2..bbox+radius) 의 잔디 칸에만 배치 →
    건물→포장마당→초목 프레이밍(샘플맵처럼 건물이 환경에 녹아든다). 건물 사이 맨잔디 공백도
    함께 줄인다. 이미 점유된 칸(집·길·정원·나무)·비잔디 칸은 건너뛴다.
    """
    grass = pal.get_tile_id(tid, "grass")
    pool = [
        (pal.get_object(tid, n), d)
        for n, d in (
            ("bush_clump", 0.30),
            ("shrub", 0.22),
            ("grass_tuft", 0.28),
            ("flower", 0.18),
            ("berry_bush", 0.12),
        )
    ]
    pool = [(o, d) for o, d in pool if o]
    if not pool:
        return
    for x0, y0, x1, y1 in boxes:
        for y in range(max(0, y0 - radius), min(H, y1 + radius + 1)):
            for x in range(max(0, x0 - radius), min(W, x1 + radius + 1)):
                if x0 - 1 <= x <= x1 + 1 and y0 - 1 <= y <= y1 + 1:
                    continue  # 포장 pad 안은 건너뜀 — 바깥 잔디 링만 감싼다
                if (x, y) in occupied or get_tile(data, x, y, W, H, 1) != 0:
                    continue
                if base_of(get_tile(data, x, y, W, H, 0)) != grass:
                    continue
                for obj, dens in pool:
                    if rng.random() < dens:
                        set_tile(data, x, y, W, H, int(obj.get("layer", 1)), int(obj["base_id"]))
                        occupied.add((x, y))
                        break


def _plant_gardens(
    data: list[int],
    W: int,
    H: int,
    tid: int,
    gardens: list[list[int]],
    occupied: set[tuple[int, int]],
    rng: random.Random,
) -> None:
    """정원 구획에만 꽃·풀·관목을 촘촘히 깐다(맵 전체 도배가 아니라 '구획'에만 = 정돈된 정원)."""
    grass_bases = {pal.get_tile_id(tid, "grass")}
    pool = [
        (pal.get_object(tid, n), d)
        for n, d in (("flower", 0.30), ("grass_tuft", 0.35), ("bush_clump", 0.15), ("shrub", 0.12))
    ]
    pool = [(o, d) for o, d in pool if o]
    for x0, y0, x1, y1 in gardens:
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                if (x, y) in occupied or get_tile(data, x, y, W, H, 1) != 0:
                    continue
                if base_of(get_tile(data, x, y, W, H, 0)) not in grass_bases:
                    continue
                for obj, dens in pool:
                    if rng.random() < dens:
                        set_tile(data, x, y, W, H, 1, int(obj["base_id"]))
                        break


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

    # 4) 구조물 스탬프 — 원본 가장자리 보존(오토타일 재계산 안 당함)
    occupied: set[tuple[int, int]] = set()
    for vig, hx, hy, mask in house_masks:
        occupied |= stamp(data, W, H, vig, hx, hy, mask=mask)

    # 4b) 소품(props: 분수·우물·석상 등) — 길 연결 없이 그 자리에 얹는다(플라자 악센트)
    for p in blueprint.get("props", []):
        vig = vignettes.get(p["vignette"])
        if vig:
            occupied |= stamp(data, W, H, vig, p["x"], p["y"], mask=vig.structural_mask())

    # 4c) 건물 프레이밍 — 포장마당 바깥 잔디 링을 초목으로 감싸 공백을 줄이고 환경에 녹인다
    if blueprint.get("frame", True) and boxes:
        _frame_buildings(data, W, H, tid, boxes, occupied, rng)

    # 5) 정원(구획 한정) → 6) 나무 경계
    _plant_gardens(data, W, H, tid, blueprint.get("gardens", []), occupied, rng)
    if blueprint.get("tree_border"):
        _tree_border(data, W, H, tid, occupied, rng)
    return data, W, H, tid
