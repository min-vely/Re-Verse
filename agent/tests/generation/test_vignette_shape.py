"""vignette 실루엣(shape) 회귀 테스트 — '정사각형으로 자르지 않는다'.

사각형 bbox 를 그대로 오리고 찍으면 두 가지가 동시에 망가진다:
  ① 본체 밖으로 튀어나온 모서리 탑·기둥이 세로로 **잘린다**
  ② 사각형 안에 들어온 옆집·울타리·지면이 **딸려온다**(눈밭에 떠 있는 잘린 흰 땅 조각)
둘 다 여기서 잠근다.
"""

from pathlib import Path

import pytest

from agent.generation.mapgen import palette as pal
from agent.generation.mapgen.tile_constants import get_tile, make_empty_data, set_tile
from agent.generation.mapgen.vignette import Vignette, fill_enclosed, stamp
from app.backend.core.config import settings

_TID = 2
_WALL = 5888  # A4 벽 = 구조물 코어 재질
_SAMPLEMAPS = Path(settings.BASE_GAME_PATH) / "samplemaps"


def test_fill_enclosed_fills_holes_only() -> None:
    """도넛의 가운데 구멍은 메우고 바깥은 안 건드린다."""
    ring = {(x, y) for x in range(1, 4) for y in range(1, 4)} - {(2, 2)}
    out = fill_enclosed(ring, 5, 5)
    assert (2, 2) in out, "둘러싸인 구멍이 안 메워짐"
    assert out == ring | {(2, 2)}, "바깥 칸이 딸려 들어옴"


def test_fill_enclosed_does_not_bridge_separate_blobs() -> None:
    """같은 행의 떨어진 두 덩어리를 이으면 안 된다 — 그게 '옆집 딸려옴'의 원인이었다.

    예전 구현은 행별 가로 min~max 를 채워서 (0,0)~(4,0) 을 통째로 실루엣에 넣었다.
    """
    blobs = {(0, 0), (1, 0), (3, 0), (4, 0)}
    assert fill_enclosed(blobs, 5, 3) == blobs


def test_stored_shape_wins_over_heuristic() -> None:
    """마이너가 넣어준 shape 가 있으면 추정 로직을 쓰지 않는다."""
    w = h = 4
    layers = [[0] * (w * h) for _ in range(6)]
    layers[0] = [_WALL] * (w * h)  # 추정 로직이면 16칸 전부가 실루엣
    vig = Vignette(name="s", width=w, height=h, tileset_id=_TID, layers=layers)
    assert len(vig.structural_mask()) == 16
    vig.shape = {(1, 1), (2, 1)}
    assert vig.structural_mask() == {(1, 1), (2, 1)}


def test_stamp_drops_outline_ground_keeps_interior() -> None:
    """윤곽선의 바닥은 대상 맵에 양보하고, 둘러싸인 내부 바닥(디자인)은 그대로 옮긴다.

    모서리 탑 아래 L0 는 원본 마당 흙일 뿐이라 옮기면 안 되고, 성 안뜰의 이끼·독 장판은
    그 구조물의 디자인이라 옮겨야 한다. 둘의 차이는 '바깥 공기와 닿아 있는가' 하나뿐이다.
    """
    n = 5
    grass = pal.get_tile_id(_TID, "grass")
    sand = pal.get_tile_id(_TID, "sand")
    layers = [[0] * (n * n) for _ in range(6)]
    layers[0] = [_WALL] * (n * n)
    layers[0][2 * n + 0] = grass  # 윤곽선(왼쪽 변)의 지면 — 버려야 한다
    layers[1][2 * n + 0] = 100  # 그 칸의 부속물(탑 스프라이트)
    layers[0][2 * n + 2] = grass  # 둘러싸인 내부의 지면 — 지켜야 한다
    vig = Vignette(name="keep", width=n, height=n, tileset_id=_TID, layers=layers)
    vig.shape = {(x, y) for x in range(n) for y in range(n)}

    W = H = 9
    data = make_empty_data(W, H)
    for yy in range(H):
        for xx in range(W):
            set_tile(data, xx, yy, W, H, 0, sand)
    stamp(data, W, H, vig, 2, 2)

    assert get_tile(data, 2, 4, W, H, 0) == sand, "윤곽선 지면이 대상 맵에 찍힘"
    assert get_tile(data, 2, 4, W, H, 1) == 100, "윤곽선 부속물(탑)까지 사라짐"
    assert get_tile(data, 4, 4, W, H, 0) == grass, "둘러싸인 내부 디자인이 지워짐"
    assert get_tile(data, 3, 2, W, H, 0) == _WALL, "윤곽선 벽이 사라짐"


def test_apron_ground_skips_liquids() -> None:
    """마당 재질은 액체를 피한다 — 성의 해자·오두막의 연못이 지형 1순위인 경우가 있다."""
    vig = Vignette(name="g", width=1, height=1, tileset_id=_TID, layers=[[0]] * 6)

    vig.ground = (("water_pool", 30), ("dirt", 5))
    assert vig.preferred_ground == "water_pool"  # 원본 기록은 그대로 남는다
    assert vig.apron_ground == "dirt"  # 깔 때는 밟을 수 있는 것으로

    vig.ground = (("poison_water", 10),)
    assert vig.apron_ground is None, "액체뿐이면 폴백해야 한다"

    vig.ground = ()
    assert vig.preferred_ground is None and vig.apron_ground is None


def test_ground_affinity_is_a_ratio() -> None:
    """지형 적합도는 원본 주변 비율 — 배치 가중치로 쓴다."""
    vig = Vignette(name="g", width=1, height=1, tileset_id=_TID, layers=[[0]] * 6)
    vig.ground = (("dirt", 75), ("grass", 25))
    assert vig.ground_affinity("dirt") == 0.75
    assert vig.ground_affinity("grass") == 0.25
    assert vig.ground_affinity("snow") == 0.0


@pytest.mark.skipif(not _SAMPLEMAPS.is_dir(), reason="샘플맵 없음")
class TestCuratedLibrary:
    """실제 큐레이션 라이브러리로 확인하는 항목들."""

    @staticmethod
    def _curated():
        from agent.generation.mapgen.vignette_mine import mine_curated

        vigs = mine_curated(_SAMPLEMAPS, _TID)
        assert vigs, "큐레이션 구조물이 발굴되지 않음"
        return vigs

    def test_shape_never_covers_whole_rectangle(self) -> None:
        """실루엣이 사각형 전체면 주변 지면·옆집을 그대로 찍고 있다는 뜻이다."""
        for v in self._curated():
            m = v.structural_mask()
            assert len(m) < v.width * v.height, f"{v.name}: 사각형 전체가 실루엣"

    def test_shape_extends_beyond_core_for_corner_towers(self) -> None:
        """Map021_s1(성)의 팔각 모서리 탑은 A3/A4 코어 **밖**에 있다 — 안 잘려야 한다."""
        v = next(x for x in self._curated() if x.name == "Map021_s1")
        m = v.structural_mask()
        core_x = [x for (x, y) in m if 4352 <= v.cell(0, x, y) <= 8191]
        assert core_x, "코어 셀이 없음"
        assert min(x for x, _ in m) < min(core_x), "왼쪽 모서리 탑이 잘림"
        assert max(x for x, _ in m) > max(core_x), "오른쪽 모서리 탑이 잘림"

    def test_shape_is_connected(self) -> None:
        """실루엣은 한 덩어리여야 한다(떨어진 조각 = 남의 집 부스러기)."""
        for v in self._curated():
            m = v.structural_mask()
            assert Vignette._largest_component(m) == m, f"{v.name}: 실루엣이 쪼개짐"

    def test_ground_metadata_is_recorded(self) -> None:
        """구조물마다 원본에서 딛고 있던 지형이 기록돼야 배치 힌트로 쓸 수 있다."""
        vigs = self._curated()
        assert all(v.ground for v in vigs), "지형 메타데이터가 빈 구조물이 있음"
        castle = next(v for v in vigs if v.name == "Map021_s1")
        assert castle.preferred_ground == "dirt", "성은 흙 마당 위에 있었다"
        assert castle.ground_affinity("dirt") > 0.5

    def test_decor_does_not_carry_its_background(self) -> None:
        """장식 소품의 실루엣 칸은 전부 '오브젝트가 있는 칸' 이거나 '내부 구멍' 이어야 한다.

        오브젝트 없는 칸이 실루엣 테두리에 있으면 그건 소품이 서 있던 눈밭·잔디를 사각형째
        들고 온 것 — 대상 맵 한가운데 떠 있는 '잘린 땅' 조각으로 보인다.
        """
        from agent.generation.mapgen.vignette_mine import mine_dir

        decor = [v for v in mine_dir(_SAMPLEMAPS, _TID, include_decor=True) if v.kind == "decor"]
        assert decor, "장식이 발굴되지 않음"
        for v in decor:
            m = v.structural_mask()
            for x, y in m:
                if any(v.cell(ly, x, y) for ly in (1, 2, 3)):
                    continue  # 오브젝트 칸 — 정상
                nbrs = [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
                interior = all(
                    0 <= nx < v.width and 0 <= ny < v.height and (nx, ny) in m for nx, ny in nbrs
                )
                assert interior, f"{v.name}: ({x},{y}) 배경 칸이 실루엣 가장자리에 붙어 있음"
