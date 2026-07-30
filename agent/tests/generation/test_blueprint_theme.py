"""blueprint 컴파일러의 테마별 장식 패스 테스트 (눈 테마 중심).

배경: 장식 3패스(프레이밍·정원·나무 경계)가 잔디 자산·잔디 바닥에 하드코딩돼 있어서
눈/사막 테마에서는 전부 no-op 이었다 — 건물과 길만 있는 휑한 맵이 나왔다. 테마 테이블
(`_THEME_FLORA`/`_THEME_TREES`/`_THEME_GROUND_DECOR`)로 분리한 뒤의 회귀를 막는다.

밀도 수치는 실측을 보며 조정할 값이라 단언하지 않는다. 대신 구조적 성질을 검증한다:
장식이 실제로 깔리는가 / 다른 테마 자산이 새지 않는가 / 길·건물을 덮지 않는가 / 결정론.
"""

from pathlib import Path

import pytest

from agent.generation.mapgen import palette as pal
from agent.generation.mapgen.autotile import base_of
from agent.generation.mapgen.blueprint import (
    _THEME_FLORA,
    _THEME_GROUND_DECOR,
    _THEME_TREES,
    compile_blueprint,
)
from agent.generation.mapgen.tile_constants import get_tile
from app.backend.core.config import settings

_TID = 2
_SAMPLEMAPS = Path(settings.BASE_GAME_PATH) / "samplemaps"

# 눈 테마 자산(팔레트 등록값). 잔디 자산이 눈 맵에 새면 초록이 그대로 뜬다.
_SNOW_TREES = {192, 193, 200, 201, 194, 195, 202, 203, 221, 229}
_GRASS_ONLY = {"bush_clump", "shrub", "grass_tuft", "flower", "berry_bush"}


def _snow_blueprint(**over) -> dict:
    """건물 없는 순수 지형 blueprint — 장식 패스만 검증한다(샘플맵 불필요)."""
    bp = {
        "size": [30, 24],
        "tileset": _TID,
        "theme": "snow",
        "houses": [],
        "gardens": [[4, 4, 12, 10]],
        "tree_border": True,
    }
    bp.update(over)
    return bp


def _layer_bases(data: list[int], w: int, h: int, layer: int) -> set[int]:
    return {
        base_of(v) for y in range(h) for x in range(w) if (v := get_tile(data, x, y, w, h, layer))
    }


def test_snow_theme_tables_resolve() -> None:
    """테마 테이블의 이름이 전부 팔레트에 실재해야 한다(오타·미등록 조기 발견)."""
    for name, _w in _THEME_TREES["snow"]:
        assert pal.get_multitile(_TID, name), f"멀티타일 미등록: {name}"
    for name, _d in _THEME_FLORA["snow"]:
        assert pal.get_object(_TID, name), f"오브젝트 미등록: {name}"
    for name, target, _c in _THEME_GROUND_DECOR["snow"]:
        assert pal.get_ground_decor(_TID, name), f"ground_decor 미등록: {name}"
        assert pal.get_tile_id(_TID, target), f"대상 지형 미등록: {target}"


def test_snow_decor_fills_layers() -> None:
    """눈 테마는 바닥 텍스처(L1)와 나무(L3)가 실제로 깔린다 — 이전엔 둘 다 0 이었다."""
    data, w, h, _ = compile_blueprint(_snow_blueprint(), {}, seed=3)
    patch = pal.get_ground_decor(_TID, "snow_patch")
    assert patch
    assert base_of(int(patch["base_id"])) in _layer_bases(data, w, h, 1)
    assert _SNOW_TREES & _layer_bases(data, w, h, 3), "눈 나무가 하나도 배치되지 않았다"


def test_snow_map_has_no_grass_assets() -> None:
    """눈 맵에 잔디 테마 식생이 새지 않는다(테마 테이블 분리의 핵심)."""
    data, w, h, _ = compile_blueprint(_snow_blueprint(), {}, seed=5)
    placed = _layer_bases(data, w, h, 1) | _layer_bases(data, w, h, 2) | _layer_bases(data, w, h, 3)
    for name in _GRASS_ONLY:
        obj = pal.get_object(_TID, name)
        if obj:
            assert int(obj["base_id"]) not in placed, f"잔디 자산 {name} 이 눈 맵에 배치됐다"


def test_decor_passes_can_be_disabled() -> None:
    """장식 패스는 전부 끌 수 있다(before 비교·디버깅용)."""
    bp = _snow_blueprint(
        ground_decor=False, frame=False, tree_border=False, forest=False, gardens=[]
    )
    data, w, h, _ = compile_blueprint(bp, {}, seed=3)
    for layer in (1, 2, 3):
        assert not _layer_bases(data, w, h, layer), f"L{layer} 가 비어 있지 않다"


def test_trees_do_not_cover_street() -> None:
    """나무·소품은 바닥이 테마 지형인 칸에만 — 큰길(포장) 위를 덮으면 길이 끊긴다."""
    bp = _snow_blueprint(street={"y": 12, "x0": 2, "x1": 27})
    data, w, h, _ = compile_blueprint(bp, {}, seed=11)
    path_id = pal.get_tile_id(_TID, "cobble_grey2")
    for y in range(h):
        for x in range(w):
            if base_of(get_tile(data, x, y, w, h, 0)) != path_id:
                continue
            for layer in (1, 2, 3):
                assert get_tile(data, x, y, w, h, layer) == 0, f"길 ({x},{y}) L{layer} 가 덮였다"


def test_snow_compile_deterministic() -> None:
    """같은 seed → 같은 결과(장식 패스가 rng 를 소비하는 순서까지 고정)."""
    a = compile_blueprint(_snow_blueprint(), {}, seed=42)[0]
    b = compile_blueprint(_snow_blueprint(), {}, seed=42)[0]
    assert a == b


@pytest.mark.skipif(not _SAMPLEMAPS.exists(), reason="샘플맵 없음")
def test_snow_village_denser_than_bare() -> None:
    """큐레이션 눈집을 얹은 실제 마을도 장식이 켜지면 상위 레이어가 확실히 채워진다."""
    from agent.generation.mapgen.layout import pack
    from agent.generation.mapgen.vignette_mine import mine_curated

    snow = [v for v in mine_curated(_SAMPLEMAPS, _TID) if v.theme == "snow"]
    if not snow:
        pytest.skip("눈 큐레이션 구조물 없음")
    lay = pack(snow[:1], filler=snow, props=[], seed=7)
    base = {
        "size": [lay.width, lay.height],
        "tileset": _TID,
        "theme": "snow",
        "street": {"y": lay.street_y, "x0": 3, "x1": lay.width - 4},
        "houses": lay.houses,
        "gardens": lay.gardens,
        "tree_border": True,
    }
    off = {**base, "ground_decor": False, "frame": False, "tree_border": False, "forest": False,
           "gardens": []}
    n = lay.width * lay.height

    def upper_cells(bp: dict) -> int:
        data, w, h, _ = compile_blueprint(bp, lay.vignettes, seed=7)
        return sum(
            any(get_tile(data, x, y, w, h, layer) for layer in (1, 2, 3))
            for y in range(h)
            for x in range(w)
        )

    bare, decorated = upper_cells(off), upper_cells(base)
    assert decorated > bare * 2, f"장식 효과 부족: {bare}/{n} → {decorated}/{n}"
