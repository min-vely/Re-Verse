"""layout.pack 테스트 — 고정 슬롯을 대체한 건물 패킹.

고정 슬롯 시절의 실패 모드를 그대로 회귀 항목으로 박아둔다:
겹침, 맵 밖 이탈, 큰길 관통, 랜드마크가 자리를 못 잡고 통째로 탈락.
"""

from pathlib import Path

import pytest

from agent.generation.mapgen.layout import _STREET_H, footprint, pack, plan_canvas
from agent.generation.mapgen.vignette import Vignette
from app.backend.core.config import settings

_TID = 2
_WALL = 5888  # A4 벽 — structural_mask 가 구조물로 인정하는 재질
_SAMPLEMAPS = Path(settings.BASE_GAME_PATH) / "samplemaps"


def _box_vignette(name: str, w: int, h: int) -> Vignette:
    """벽으로 꽉 찬 w x h 구조물(실루엣 = 전체 사각형)."""
    layers = [[0] * (w * h) for _ in range(6)]
    layers[0] = [_WALL] * (w * h)
    return Vignette(name=name, width=w, height=h, tileset_id=_TID, layers=layers)


def _rects(lay) -> list[tuple[int, int, int, int]]:
    """배치 결과 → 실루엣 bbox 목록(맵 좌표)."""
    out = []
    for spec in (*lay.houses, *lay.props):
        vig = lay.vignettes[spec["vignette"]]
        ox, oy, w, h = footprint(vig)
        x, y = spec["x"] + ox, spec["y"] + oy
        out.append((x, y, x + w - 1, y + h - 1))
    return out


def _overlap(a, b) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def test_footprint_uses_silhouette_not_canvas() -> None:
    """배치 크기는 vignette 원본 크기가 아니라 실루엣 bbox 여야 한다."""
    w, h = 6, 8
    layers = [[0] * (w * h) for _ in range(6)]
    for y in range(2, 6):  # 위 2줄·아래 2줄은 빈 여백
        for x in range(1, 5):
            layers[0][y * w + x] = _WALL
    vig = Vignette(name="pad", width=w, height=h, tileset_id=_TID, layers=layers)
    assert footprint(vig) == (1, 2, 4, 4)


def test_plan_canvas_fits_tallest_above_street() -> None:
    """가장 큰 건물이 여유까지 포함해 큰길 위 밴드에 들어가야 한다(탈락 방지)."""
    W, H, street_y = plan_canvas([(25, 27), (6, 9), (6, 9)])
    edge, margin = 2, 2
    assert street_y - margin - (edge + margin) >= 27  # 위 밴드 높이 >= 최대 건물
    assert H >= street_y + _STREET_H + margin + 9 + margin + edge  # 아래 밴드도 확보
    assert W >= 25 + 2 * (edge + margin)


def test_pack_no_overlap_and_in_bounds() -> None:
    """대형 랜드마크 + 소형 집 혼합에서 겹침 0, 맵 밖 이탈 0."""
    wish = [_box_vignette("L", 25, 27), _box_vignette("M", 11, 8)]
    wish += [_box_vignette(f"S{i}", 7, 6) for i in range(4)]
    lay = pack(wish, filler=[_box_vignette("F", 7, 6)], seed=3)

    rects = _rects(lay)
    assert len(rects) >= len(wish)
    for i, a in enumerate(rects):
        assert 0 <= a[0] and 0 <= a[1] and a[2] < lay.width and a[3] < lay.height, "맵 밖 이탈"
        for b in rects[i + 1 :]:
            assert not _overlap(a, b), f"건물 겹침 {a} vs {b}"


def test_pack_keeps_street_clear() -> None:
    """어떤 건물도 큰길 2줄을 밟지 않는다(밟으면 길이 건물 밑으로 사라진다)."""
    wish = [_box_vignette("L", 20, 18)] + [_box_vignette(f"S{i}", 8, 7) for i in range(5)]
    lay = pack(wish, seed=1)
    band = range(lay.street_y, lay.street_y + _STREET_H)
    for x0, y0, x1, y1 in _rects(lay):
        assert not (set(range(y0, y1 + 1)) & set(band)), "건물이 큰길을 덮음"


def test_pack_places_landmark() -> None:
    """랜드마크는 가장 먼저 자리를 잡으므로 절대 탈락하면 안 된다."""
    lay = pack([_box_vignette("castle", 25, 27), _box_vignette("hut", 6, 6)], seed=0)
    assert lay.dropped == []
    assert any(lay.vignettes[h["vignette"]].name == "castle" for h in lay.houses)


def test_pack_is_deterministic() -> None:
    """같은 seed → 같은 배치(생성 재현성)."""
    wish = [_box_vignette("A", 12, 10), _box_vignette("B", 8, 8), _box_vignette("C", 6, 6)]
    a = pack(wish, filler=[_box_vignette("F", 6, 6)], seed=11)
    b = pack(wish, filler=[_box_vignette("F", 6, 6)], seed=11)
    assert (a.width, a.height, a.street_y, a.houses) == (b.width, b.height, b.street_y, b.houses)


def test_pack_fills_gaps_with_variety() -> None:
    """빈 곳 채우기는 풀을 돌아가며 쓴다 — 같은 집만 복제하면 안 된다."""
    pool = [_box_vignette(f"P{i}", 7, 6) for i in range(4)]
    lay = pack([_box_vignette("L", 18, 16)], filler=pool, seed=5)
    names = [lay.vignettes[h["vignette"]].name for h in lay.houses if h["vignette"] != "L"]
    assert len(set(names)) > 1, f"채우기가 한 종류만 반복함: {names}"


@pytest.mark.skipif(not _SAMPLEMAPS.is_dir(), reason="샘플맵 없음")
@pytest.mark.parametrize("theme", ["grassland", "snow", "desert"])
def test_pack_real_curated_library(theme: str) -> None:
    """실제 큐레이션 라이브러리로도 겹침·탈락 없이 배치된다."""
    from agent.generation.mapgen.vignette_mine import mine_curated

    structs = [v for v in mine_curated(_SAMPLEMAPS, _TID) if v.theme == theme]
    assert structs, f"{theme} 구조물 없음"
    tiers = {k: [v for v in structs if v.size_class == k] for k in ("small", "medium", "large")}
    smalls = tiers["small"] or tiers["medium"] or tiers["large"]
    wish = tiers["large"][:1] + tiers["medium"][:1] + [smalls[i % len(smalls)] for i in range(3)]

    lay = pack(wish, filler=smalls + tiers["medium"] + tiers["large"][1:], seed=7)
    assert lay.dropped == [], f"{theme}: 배치 실패 {lay.dropped}"
    rects = _rects(lay)
    for i, a in enumerate(rects):
        assert a[2] < lay.width and a[3] < lay.height
        for b in rects[i + 1 :]:
            assert not _overlap(a, b), f"{theme}: 겹침 {a} vs {b}"
