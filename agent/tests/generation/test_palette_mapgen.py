"""palette_mapgen 테스트 — palette 기반 테스트 생성기.

기존 town_generator 와 독립. palette 지형으로 타일이 올바르게 깔리고,
오토타일 shape 보정이 적용되는지 확인.
"""

from agent.generation.mapgen import palette as pal
from agent.generation.mapgen.autotile import base_of, is_floor_autotile
from agent.generation.mapgen.palette_mapgen import (
    _BIOMES,
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
    """고도맵으로 3종 이상 지형이 배치된다."""
    d = generate_terrain_map(_terrain_dims(), seed=3, autotile=True)
    terrain_ids = set(pal.terrain_map(2).values())
    bases = {base_of(d[i]) for i in range(40 * 40)}
    assert len(bases & terrain_ids) >= 3


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


def test_variant_diversity():
    """_pick_variant 가 시드에 따라 여러 잔디 변형을 선택한다(맵 간 다양성)."""
    import random

    from agent.generation.mapgen.palette_mapgen import _pick_variant

    grass = pal.get_tile_id(2, "grass")
    picks = {_pick_variant(2, grass, random.Random(s)) for s in range(30)}
    assert len(picks) >= 2  # 시드마다 다른 변형이 선택됨


def test_variants_off_uses_base():
    """variants=False 면 원본 base_id 만 사용(결정적)."""
    d = generate_terrain_map(_terrain_dims(), seed=5, biome="grassland", variants=False)
    grass = pal.get_tile_id(2, "grass")
    bases = {base_of(d[i]) for i in range(40 * 40)}
    assert grass in bases  # 원본 잔디 그대로


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
