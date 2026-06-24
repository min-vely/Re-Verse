"""tile_catalog 색 분류 + 변형 찾기 테스트.

전수 카탈로그(tile_catalog.json)는 커밋되어 있어 게임 데이터 없이도 동작한다.
"""

from agent.generation.mapgen import palette as pal
from agent.generation.mapgen import tile_catalog as tc


def test_color_group():
    assert tc.color_group((129, 193, 64)) == "green"
    assert tc.color_group((45, 161, 204)) in ("cyan", "blue")
    assert tc.color_group((255, 255, 255)) == "white"
    assert tc.color_group((25, 25, 25)) == "black"
    assert tc.color_group((150, 150, 150)) == "gray"


def test_catalog_loads():
    c = tc.load_catalog()
    assert "2" in c["tilesets"]
    assert c["tilesets"]["2"]["count"] > 100  # 수백 종


def test_find_variants_grass():
    """잔디 변형은 자기 자신 포함 + 전부 같은 색군(green)·통행성."""
    grass = pal.get_tile_id(2, "grass")
    v = tc.find_variants(2, grass)
    assert grass in v
    assert len(v) >= 2  # 변형 존재
    for bid in v:
        m = tc.get_tile_meta(2, bid)
        assert tc.color_group(m["rgb"]) == "green"
        assert m["passable"] is True


def test_find_variants_water_all_blue():
    """물 변형은 전부 cyan/blue + 통행 불가 (용암 등 안 섞임)."""
    v = tc.find_variants(2, pal.get_tile_id(2, "water"))
    for bid in v:
        m = tc.get_tile_meta(2, bid)
        assert tc.color_group(m["rgb"]) in ("cyan", "blue")
        assert m["passable"] is False


def test_find_variants_unknown():
    assert tc.find_variants(2, 999999) == [999999]
