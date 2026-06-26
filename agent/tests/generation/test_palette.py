"""tile_palette.json / palette.py 검증 테스트.

tileset 1~2 는 실제 base_game 데이터(Tilesets.json flags + samplemaps 빈도 + Outside_A2.png
시트해석)로 검증 완료된 핵심 지형 팔레트다. 이 테스트는 그 검증 결과가 회귀하지 않는지 지킨다.
"""

from agent.generation.mapgen import palette as pal


def test_palette_loads_and_has_encoding():
    data = pal.load_palette()
    assert data["schema_version"] == 1
    ranges = data["encoding"]["ranges"]
    assert ranges["A2"] == [2816, 4351]
    assert "1" in data["tilesets"] and "2" in data["tilesets"]


def test_all_tilesets_verified():
    """tileset 1~6 모두 실측 검증 완료 → verified=true."""
    tilesets = pal.load_palette()["tilesets"]
    for tid in ("1", "2", "3", "4", "5", "6"):
        assert tid in tilesets, f"tileset {tid} 누락"
        assert tilesets[tid]["verified"] is True


def test_tilesets_3to6_core_terrain():
    """tileset 3~6 핵심 지형 base_id + 통행성 (검증 결과 회귀 방지)."""
    # 실내
    assert pal.get_tile_id(3, "floor") == 3584
    assert pal.get_terrain(3, "wall")["passable"] is False
    assert pal.get_terrain(3, "void")["passable"] is False
    # 던전
    assert pal.get_tile_id(4, "floor") == 5888
    assert pal.get_terrain(4, "lava")["passable"] is False
    # SF 외곽
    assert pal.get_tile_id(5, "road") == 1554
    assert pal.get_terrain(5, "water")["passable"] is False
    # SF 내부
    assert pal.get_tile_id(6, "floor") == 5936
    assert pal.get_terrain(6, "wall")["passable"] is False


def test_tilesets_3to6_objects():
    """tileset 3~6 오브젝트 식별 결과 회귀 방지 (단일타일, 통행성)."""
    assert pal.get_object(3, "chair")["passable"] is True
    assert pal.get_object(3, "barrel")["passable"] is False
    assert pal.get_object(4, "rock")["passable"] is True
    assert pal.get_object(4, "big_rock")["passable"] is False
    assert pal.get_object(5, "hydrant")["passable"] is False
    assert pal.get_object(6, "plant")["passable"] is True
    assert pal.get_object(6, "toilet")["passable"] is False


def test_tileset2_core_terrain_values():
    """검증으로 확정된 핵심 지형 base_id (회귀 방지).

    전수 발굴로 terrain 이 확장됨 — exact-equal 대신 핵심값 불변만 가드.
    """
    expected = {
        "grass": 2816,
        "dirt": 3584,
        "sand": 3200,
        "snow": 3968,
        "gravel": 2912,
        "stone_path": 2960,
        "water": 2048,
        "wall": 3488,
    }
    tmap = pal.terrain_map(2)
    for name, base_id in expected.items():
        assert tmap.get(name) == base_id, f"핵심 지형 {name} base_id 변경됨"


def test_tileset2_passability():
    """핵심 통행성 (회귀 방지). 전수 발굴로 impassable 확장됨 — 핵심 불변만 가드."""
    assert {2048, 3488} <= pal.impassable_ids(2)
    assert pal.get_terrain(2, "grass")["passable"] is True
    assert pal.get_terrain(2, "water")["passable"] is False
    assert pal.get_terrain(2, "wall")["layer"] == 1


def test_tileset1_grass_water():
    assert pal.get_tile_id(1, "grass") == 2816
    assert pal.get_tile_id(1, "water") == 2048


def test_get_helpers():
    assert pal.get_tile_id(2, "grass") == 2816
    assert pal.get_tile_id(2, "does_not_exist", -1) == -1
    assert pal.get_terrain(99, "grass") is None  # 없는 tileset


def test_get_object():
    rock = pal.get_object(2, "rock")
    assert rock["base_id"] == 159 and rock["passable"] is False
    assert pal.get_object(2, "grass_tuft")["passable"] is True
    assert pal.get_object(2, "flower")["passable"] is True  # 161 보라꽃 통과
    assert pal.get_object(99, "rock") is None  # 없는 tileset
    assert pal.get_object(2, "nope") is None  # 없는 오브젝트


def test_get_multitile():
    tree = pal.get_multitile(2, "tree")
    assert tree["tiles"] == [[176, 177], [184, 185]]
    assert tree["blocked"] == [[False, False], [True, True]]
    assert pal.get_multitile(2, "nope") is None
    assert pal.get_multitile(99, "tree") is None


def test_tilesets_3to6_multitile():
    """tileset 3~6 멀티타일 식별 결과 회귀 방지 (크기·구성)."""
    assert pal.get_multitile(3, "bed")["tiles"] == [[171], [179]]
    # 430,431,438,439 는 책장이 아니라 무기 진열대였음(렌더 검증 후 weapon_rack 으로 정정)
    assert pal.get_multitile(3, "weapon_rack")["tiles"] == [[430, 431], [438, 439]]
    assert pal.get_multitile(3, "pillar")["tiles"] == [[444], [452]]
    assert pal.get_multitile(4, "ice_crystal")["tiles"] == [[261], [269], [277]]
    assert pal.get_multitile(5, "bench")["tiles"] == [[473, 473]]
    assert pal.get_multitile(5, "fence")["tiles"] == [[439, 439]]
    assert pal.get_multitile(6, "railing")["tiles"] == [[96, 98]]
    # blocked 그리드가 tiles 와 같은 모양인지
    for tid, nm in [(3, "bed"), (3, "weapon_rack"), (4, "ice_crystal"), (5, "bench")]:
        mt = pal.get_multitile(tid, nm)
        assert len(mt["blocked"]) == len(mt["tiles"])
        assert all(len(b) == len(t) for b, t in zip(mt["blocked"], mt["tiles"]))


def test_kind_of():
    assert pal.kind_of(2048) == "A1"
    assert pal.kind_of(2816) == "A2"
    assert pal.kind_of(4352) == "A3"
    assert pal.kind_of(5888) == "A4"
    assert pal.kind_of(1536) == "A5"
    assert pal.kind_of(100) == "BCDE"
    assert pal.kind_of(99999) == "unknown"


def test_normalize_autotile_id():
    assert pal.normalize_autotile_id(2816 + 5) == 2816
    assert pal.normalize_autotile_id(2816) == 2816
    assert pal.normalize_autotile_id(100) == 100
    assert pal.normalize_autotile_id(0) == 0
