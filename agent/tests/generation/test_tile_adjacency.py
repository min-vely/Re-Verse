"""tile_adjacency 인접 규칙 로더 테스트.

전수조사 자산(tile_adjacency.json)은 커밋돼 있어 게임 데이터 없이 동작한다.
"""

from agent.generation.mapgen import palette as pal
from agent.generation.mapgen import tile_adjacency as adj


def test_loads():
    a = adj.load_adjacency()
    assert "2" in a["tilesets"]
    assert a["tilesets"]["2"]["tiles"]  # 비어있지 않음


def test_water_neighbors_grass():
    """tileset2 물(2048)의 지면 이웃 1위는 잔디(샘플맵 실측)."""
    g = adj.ground_neighbors(2, pal.get_tile_id(2, "water"))
    grass = pal.get_tile_id(2, "grass")
    top = max(g, key=lambda k: g[k])
    assert top == grass


def test_water_grass_compatible_sand_not():
    """물-잔디는 호환, 물-모래는 비호환(잔디물가 물이라 모래로 둘러싸면 어색)."""
    water = pal.get_tile_id(2, "water")
    assert adj.compatible(2, water, pal.get_tile_id(2, "grass"))
    assert not adj.compatible(2, water, pal.get_tile_id(2, "sand"))


def test_single_tiles_always_compatible():
    """A5(1536~2047)·B~E 단일타일은 가장자리가 없어 항상 호환."""
    assert adj.compatible(2, 1554, pal.get_tile_id(2, "grass"))  # 도로(A5)


def test_unknown_returns_empty():
    assert adj.ground_neighbors(2, 999999) == {}
    assert adj.compatible(2, 999999, 888888)  # 미관측은 보수적으로 허용
