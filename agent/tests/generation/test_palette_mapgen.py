"""palette_mapgen 테스트 — palette 기반 테스트 생성기.

기존 town_generator 와 독립. palette 지형으로 타일이 올바르게 깔리고,
오토타일 shape 보정이 적용되는지 확인.
"""

import pytest

from agent.generation.mapgen import palette as pal
from agent.generation.mapgen.autotile import base_of, is_floor_autotile
from agent.generation.mapgen.palette_mapgen import (
    _BIOMES,
    _DUNGEON_THEMES,
    MapDims,
    generate_dungeon_map,
    generate_interior_map,
    generate_palette_map,
    generate_terrain_map,
)
from agent.generation.mapgen.tile_constants import get_tile

W = H = 30


def _dims():
    return MapDims(width=W, height=H, tileset_id=2)


def _count_shaped(data):
    return sum(
        1 for i in range(W * H) if is_floor_autotile(data[i]) and data[i] != base_of(data[i])
    )


def test_output_size():
    dims = _dims()
    data = generate_palette_map(dims, seed=1)
    assert len(data) == dims.width * dims.height * 6


def test_base_ids_are_palette_terrain():
    """오토타일 shape 를 제거(base_of)하면 레이어 0~1 모든 타일은 palette ID 이거나 0."""
    data = generate_palette_map(_dims(), seed=1, autotile=True)
    valid = set(pal.terrain_map(2).values()) | {0}
    for layer in (0, 1):
        for y in range(H):
            for x in range(W):
                assert base_of(get_tile(data, x, y, W, H, layer)) in valid


def test_deterministic():
    """같은 seed → 동일 결과 (autotile 포함)."""
    assert generate_palette_map(_dims(), seed=42) == generate_palette_map(_dims(), seed=42)


def test_autotile_toggle():
    """autotile=False 는 shape 없음(base only), True 는 경계 shape 발생."""
    off = generate_palette_map(_dims(), seed=7, autotile=False)
    on = generate_palette_map(_dims(), seed=7, autotile=True)
    assert _count_shaped(off) == 0
    assert _count_shaped(on) > 0


def test_autotile_preserves_passability():
    """오토타일 on/off 가 통행 레이어(5)를 바꾸지 않는다."""
    off = generate_palette_map(_dims(), seed=7, autotile=False)
    on = generate_palette_map(_dims(), seed=7, autotile=True)
    plane = W * H
    assert off[5 * plane : 6 * plane] == on[5 * plane : 6 * plane]


def test_impassable_layer_set_for_water_and_wall():
    """water/wall(base 기준) 좌표는 통행 불가(레이어 5 = 1)."""
    data = generate_palette_map(_dims(), seed=7, autotile=True)
    water = pal.get_tile_id(2, "water")
    wall = pal.get_tile_id(2, "wall")
    for y in range(H):
        for x in range(W):
            l0 = base_of(get_tile(data, x, y, W, H, 0))
            l1 = base_of(get_tile(data, x, y, W, H, 1))
            if l0 == water or l1 == wall:
                assert get_tile(data, x, y, W, H, 5) == 1


def test_has_walkable_and_blocked():
    data = generate_palette_map(_dims(), seed=3)
    blocked = sum(1 for y in range(H) for x in range(W) if get_tile(data, x, y, W, H, 5) == 1)
    assert 0 < blocked < W * H


def test_water_autotile_applied():
    """연못(water=2048, A1) 가장자리에도 shape 보정이 적용된다."""
    data = generate_palette_map(_dims(), seed=7, autotile=True)
    water = pal.get_tile_id(2, "water")  # 2048
    shaped_water = sum(
        1 for i in range(W * H) if base_of(data[i]) == water and data[i] != water
    )
    assert shaped_water > 0


# ── terrain 모드 (노이즈 기반 자연 지형) ──────────────────────────────────────


def _terrain_dims():
    return MapDims(width=40, height=40, tileset_id=2)


def test_terrain_mode_diverse():
    """고도맵으로 여러 지형(물/잔디/자갈 등)이 배치된다."""
    d = generate_terrain_map(_terrain_dims(), seed=3, autotile=True, variants=False)
    terrain_ids = set(pal.terrain_map(2).values())
    bases = {base_of(d[i]) for i in range(40 * 40)}
    assert len(bases & terrain_ids) >= 3


def test_biome_bands_data_compatible():
    """모든 바이옴의 연속 밴드쌍이 샘플맵 인접 데이터상 호환이어야 한다(어색한 인접 방지)."""
    from agent.generation.mapgen import tile_adjacency as adj
    from agent.generation.mapgen.palette_mapgen import _BIOMES, _water_id

    biome_ts = {"grassland": 2, "desert": 2, "snow": 2, "wetland": 2, "city": 5}
    for biome, bands in _BIOMES.items():
        ts = biome_ts[biome]
        ids = [
            (nm, _water_id(biome, ts) if nm == "water" else pal.get_tile_id(ts, nm))
            for _, nm in bands
        ]
        for (na, a), (nb, b) in zip(ids, ids[1:]):
            assert adj.compatible(ts, a, b), f"{biome}: {na}({a})-{nb}({b}) 비호환 인접"


def test_terrain_deterministic():
    """같은 seed → 동일 결과 (numpy default_rng 결정적)."""
    a = generate_terrain_map(MapDims(20, 20, 2), seed=5)
    b = generate_terrain_map(MapDims(20, 20, 2), seed=5)
    assert a == b


def test_terrain_water_blocked():
    """terrain 모드의 물 타일도 통행 불가로 표시된다."""
    d = generate_terrain_map(_terrain_dims(), seed=3, autotile=True, variants=False)
    water = pal.get_tile_id(2, "water")
    checked = False
    for i in range(40 * 40):
        if base_of(d[i]) == water:
            x, y = i % 40, i // 40
            assert get_tile(d, x, y, 40, 40, 5) == 1
            checked = True
    assert checked  # 물 타일이 실제로 존재해 검증됐는지


def test_pure_variants_exclude_foreign_terrain():
    """_pure_variants 는 색만 비슷한 다른 지형 타일을 제외한다.

    잔디 변형으로 잡히는 3248(실제 모래)·3632(실제 흙)는 순수 변형이 아니다.
    오토타일 지면은 보통 순수 변형이 없어 [base] 로 수렴한다(충돌 방지).
    """
    from agent.generation.mapgen.palette_mapgen import _pure_variants

    grass = pal.get_tile_id(2, "grass")
    pure = set(_pure_variants(2, grass))
    assert grass in pure
    assert 3248 not in pure  # 모래 타일
    assert 3632 not in pure  # 흙 타일


def test_variants_off_uses_base():
    """variants=False 면 원본 base_id 만 사용(결정적)."""
    d = generate_terrain_map(_terrain_dims(), seed=5, biome="grassland", variants=False)
    grass = pal.get_tile_id(2, "grass")
    bases = {base_of(d[i]) for i in range(40 * 40)}
    assert grass in bases  # 원본 잔디 그대로


def test_terrain_no_foreign_terrain_bleed():
    """지면은 캐노니컬만 사용 — 변형이 다른 지형(모래·흙·눈)을 흘리지 않는다.

    grassland 의 지면 base 는 {물·잔디·자갈} 캐노니컬뿐이어야 한다(variant_regions 무관).
    """
    allowed = {pal.get_tile_id(2, n) for n in ("water", "grass", "gravel")}
    terrain_ids = set(pal.terrain_map(2).values())
    for vr in (1, 4):
        d = generate_terrain_map(_terrain_dims(), seed=5, biome="grassland", variant_regions=vr)
        used = {base_of(d[i]) for i in range(40 * 40)} & terrain_ids
        assert used <= allowed, f"vr={vr}: 외래 지형 누출 {used - allowed}"


def test_region_variants_deterministic():
    """같은 seed·variant_regions → 동일 결과."""
    a = generate_terrain_map(MapDims(30, 30, 2), seed=7, variant_regions=3)
    b = generate_terrain_map(MapDims(30, 30, 2), seed=7, variant_regions=3)
    assert a == b


def test_region_variants_one_equals_map_level():
    """variant_regions=1 은 기존 맵단위 동작과 완전히 동일(회귀 가드)."""
    a = generate_terrain_map(MapDims(30, 30, 2), seed=9, variant_regions=1)
    b = generate_terrain_map(MapDims(30, 30, 2), seed=9)  # 기본값
    assert a == b


def test_all_biomes_render():
    """모든 바이옴 프리셋이 정상 크기 맵을 생성한다."""
    for b in _BIOMES:
        d = generate_terrain_map(_terrain_dims(), seed=3, biome=b)
        assert len(d) == 40 * 40 * 6


def test_desert_sand_dominant():
    """desert 바이옴은 모래가 지배적(40%+)."""
    d = generate_terrain_map(_terrain_dims(), seed=3, biome="desert", variants=False)
    sand = pal.get_tile_id(2, "sand")
    sand_cnt = sum(1 for i in range(40 * 40) if base_of(d[i]) == sand)
    assert sand_cnt > 40 * 40 * 0.4


def test_snow_biome_has_snow():
    """snow 바이옴에는 눈 타일이 존재한다."""
    d = generate_terrain_map(_terrain_dims(), seed=3, biome="snow", variants=False)
    snow = pal.get_tile_id(2, "snow")
    assert any(base_of(d[i]) == snow for i in range(40 * 40))


def test_unknown_biome_falls_back_to_default():
    """없는 바이옴은 grassland 로 폴백."""
    a = generate_terrain_map(MapDims(20, 20, 2), seed=5, biome="nonexistent")
    b = generate_terrain_map(MapDims(20, 20, 2), seed=5, biome="grassland")
    assert a == b


def test_objects_placed_and_block_passability():
    """오브젝트가 레이어1에 배치되고, 통행 불가 오브젝트(나무)는 레이어5를 막는다."""
    d = generate_terrain_map(_terrain_dims(), seed=5, biome="grassland", objects=True)
    berry = pal.get_object(2, "berry_bush")["base_id"]  # 막힘 오브젝트
    placed = blocked = 0
    for y in range(40):
        for x in range(40):
            if get_tile(d, x, y, 40, 40, 1) == berry:
                placed += 1
                if get_tile(d, x, y, 40, 40, 5) == 1:
                    blocked += 1
    assert placed > 0
    assert placed == blocked  # 열매덤불은 전부 통행 차단


def test_no_objects_keeps_layer1_empty():
    """objects=False 면 레이어1(오브젝트)이 비어 있다."""
    d = generate_terrain_map(_terrain_dims(), seed=5, objects=False)
    plane = 40 * 40
    assert all(d[i] == 0 for i in range(plane, 2 * plane))


def test_dungeon_floor_and_void():
    """던전은 방 바닥(floor)과 공백(void)이 모두 존재하고, void는 통행 불가."""
    d = generate_dungeon_map(MapDims(40, 30, 4), seed=5, variants=False)
    assert len(d) == 40 * 30 * 6
    floor = pal.get_tile_id(4, "floor")
    void = pal.get_tile_id(4, "void")
    w, h = 40, 30
    bases = {base_of(get_tile(d, x, y, w, h, 0)) for y in range(h) for x in range(w)}
    assert floor in bases and void in bases
    # void 칸은 전부 통행 불가
    for y in range(h):
        for x in range(w):
            if get_tile(d, x, y, w, h, 0) == void:
                assert get_tile(d, x, y, w, h, 5) == 1


@pytest.mark.parametrize("theme", sorted(_DUNGEON_THEMES))
def test_themed_dungeon(theme):
    """테마 던전: 테마 바닥(+옵션 풀). 풀 테두리는 오토타일이 굽고, 풀은 void에 안 닿는다.

    풀이 통행불가(용암·얼음·물)면 레이어5=1, 통행가능(독 장판)이면 0.
    """
    cfg = _DUNGEON_THEMES[theme]
    d = generate_dungeon_map(MapDims(50, 40, 4), seed=5, theme=theme, variants=False)
    w, h = 50, 40
    floor = pal.get_tile_id(4, cfg["floor"])
    void = pal.get_tile_id(4, "void")
    bases = {base_of(get_tile(d, x, y, w, h, 0)) for y in range(h) for x in range(w)}
    assert floor in bases  # 테마 바닥
    if "pool" not in cfg:  # 어둠 등 풀 없는 테마
        return
    pool = pal.get_tile_id(4, cfg["pool"])
    assert pool in bases  # 풀 존재
    pool_blocks = pool in pal.impassable_ids(4)
    for y in range(h):
        for x in range(w):
            if base_of(get_tile(d, x, y, w, h, 0)) != pool:
                continue
            assert get_tile(d, x, y, w, h, 5) == (1 if pool_blocks else 0)
            for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    nb = base_of(get_tile(d, nx, ny, w, h, 0))
                    assert nb != void, f"{theme}: 풀이 void(공백)에 접함"


def test_dungeon_deterministic():
    """같은 seed → 동일 던전 (BSP 전역 random 시드 고정)."""
    a = generate_dungeon_map(MapDims(30, 20, 4), seed=7)
    b = generate_dungeon_map(MapDims(30, 20, 4), seed=7)
    assert a == b


def test_interior_floor_wall_void():
    """실내는 마루·벽돌벽·공백이 모두 있고, 벽·공백은 통행 불가."""
    d = generate_interior_map(MapDims(36, 28, 3), seed=5, variants=False)
    floor = pal.get_tile_id(3, "floor")
    wall = pal.get_tile_id(3, "wall")
    void = pal.get_tile_id(3, "void")
    w, h = 36, 28
    bases = {base_of(get_tile(d, x, y, w, h, 0)) for y in range(h) for x in range(w)}
    assert floor in bases and wall in bases and void in bases
    for y in range(h):
        for x in range(w):
            b = base_of(get_tile(d, x, y, w, h, 0))
            if b in (wall, void):
                assert get_tile(d, x, y, w, h, 5) == 1


def test_interior_deterministic():
    a = generate_interior_map(MapDims(30, 20, 3), seed=9)
    b = generate_interior_map(MapDims(30, 20, 3), seed=9)
    assert a == b


def test_interior_furniture_against_wall():
    """멀티타일 가구(침대·책장·기둥)는 윗칸이 벽인 위치에 배치된다."""
    d = generate_interior_map(MapDims(40, 32, 3), seed=5)
    placed = 0
    for name in ("bed", "bookshelf", "pillar"):
        head = pal.get_multitile(3, name)["tiles"][0][0]
        for y in range(1, 32):
            for x in range(40):
                if get_tile(d, x, y, 40, 32, 1) == head:
                    assert get_tile(d, x, y - 1, 40, 32, 5) == 1  # 윗칸이 벽(통행불가)
                    placed += 1
    assert placed > 0


def test_interior_raised_wall():
    """입체 벽: 앞면(face)·윗면(top) 둘 다 생기고, 윗면도 통행 차단."""
    d = generate_interior_map(MapDims(36, 28, 3), seed=5, wall_top_name="wall_top")
    wall = pal.get_tile_id(3, "wall")  # 앞면 6512
    wall_top = pal.get_tile_id(3, "wall_top")  # 윗면 6752
    w, h = 36, 28
    bases = {base_of(get_tile(d, x, y, w, h, 0)) for y in range(h) for x in range(w)}
    assert wall in bases and wall_top in bases
    # 윗면(STAR, flags 통과)도 게임상 벽이라 통행 차단돼야 함
    for y in range(h):
        for x in range(w):
            if base_of(get_tile(d, x, y, w, h, 0)) == wall_top:
                assert get_tile(d, x, y, w, h, 5) == 1


def test_sf_outside_city_biome():
    """SF외곽 city 바이옴: tileset5 지형(도로/잔디/물 등) 3종 이상 배치."""
    d = generate_terrain_map(MapDims(40, 40, 5), seed=4, biome="city", variants=False)
    city_ids = set(pal.terrain_map(5).values())
    bases = {base_of(d[i]) for i in range(40 * 40)}
    assert len(bases & city_ids) >= 3


def test_sf_interior_floor_wall():
    """SF내부(tileset6): 흰 타일 바닥 + 어두운 벽(대비), 벽은 통행 불가."""
    d = generate_interior_map(
        MapDims(36, 28, 6),
        seed=5,
        tileset=6,
        biome="sf_interior",
        floor_name="tile_floor",
        wall_name="dark_wall",
        variants=False,
    )
    floor = pal.get_tile_id(6, "tile_floor")  # 1620 흰 타일
    wall = pal.get_tile_id(6, "dark_wall")  # 6416 어두운 벽
    w, h = 36, 28
    bases = {base_of(get_tile(d, x, y, w, h, 0)) for y in range(h) for x in range(w)}
    assert floor in bases and wall in bases
    for y in range(h):
        for x in range(w):
            if base_of(get_tile(d, x, y, w, h, 0)) == wall:
                assert get_tile(d, x, y, w, h, 5) == 1


def test_multitile_tree_placed_complete():
    """나무 2x2 가 완전한 셋트로 배치되고, 기둥(하단)은 통행 차단된다."""
    d = generate_terrain_map(_terrain_dims(), seed=5, biome="grassland")
    found = 0
    for y in range(39):
        for x in range(39):
            if get_tile(d, x, y, 40, 40, 1) == 176:  # 나무 좌상단
                assert get_tile(d, x + 1, y, 40, 40, 1) == 177  # 우상
                assert get_tile(d, x, y + 1, 40, 40, 1) == 184  # 좌하(기둥)
                assert get_tile(d, x + 1, y + 1, 40, 40, 1) == 185  # 우하(기둥)
                assert get_tile(d, x, y + 1, 40, 40, 5) == 1  # 기둥 통행 차단
                assert get_tile(d, x + 1, y + 1, 40, 40, 5) == 1
                found += 1
    assert found > 0
