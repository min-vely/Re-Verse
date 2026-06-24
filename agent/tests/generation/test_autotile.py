"""autotile 모듈 — shape 계산 + 맵 적용 테스트."""

from agent.generation.mapgen.autotile import (
    apply_autotile,
    base_of,
    compute_shape,
    corner_mask,
    is_floor_autotile,
)
from agent.generation.mapgen.tile_constants import get_tile, make_empty_data, set_tile


def test_base_of():
    assert base_of(2816) == 2816
    assert base_of(2816 + 5) == 2816
    assert base_of(4351) == 4304  # A2 마지막 kind 시작 (4351-(2303%48)=4304)
    assert base_of(100) == 100  # 비오토타일은 그대로


def test_is_floor_autotile():
    assert is_floor_autotile(2816) is True  # A2
    assert is_floor_autotile(4351) is True
    assert is_floor_autotile(2048) is True  # A1 물 (kind0=짝수, FLOOR)
    assert is_floor_autotile(2048 + 48) is False  # A1 kind1=홀수, WATERFALL 제외
    assert is_floor_autotile(5888) is False  # A4


def test_corner_mask_requires_both_edges():
    """코너(대각) 비트는 양옆 두 변이 모두 연결일 때만 유효."""
    # NE(bit4) 만 있고 N(bit0)/E(bit1) 없음 → NE 무효화
    assert corner_mask(1 << 4) == 0
    # N + E + NE → NE 유효
    raw = 1 | 2 | (1 << 4)
    assert corner_mask(raw) == raw
    # N 만 있고 E 없으면 NE 무효
    assert corner_mask(1 | (1 << 4)) == 1


def test_compute_shape_full_connection_is_fill():
    """8방향 완전 연결 → 내부 채움 shape 0."""
    assert compute_shape(0xFF) == 0


def test_apply_autotile_uniform_is_unchanged():
    """균일 지형 + oob_connected=True → 모든 칸 완전 연결 → shape 0 유지(변화 없음)."""
    w = h = 5
    data = make_empty_data(w, h)
    for y in range(h):
        for x in range(w):
            set_tile(data, x, y, w, h, 0, 2816)
    changed = apply_autotile(data, w, h, layer=0, oob_connected=True)
    assert changed == 0
    assert all(get_tile(data, x, y, w, h, 0) == 2816 for y in range(h) for x in range(w))


def test_apply_autotile_creates_edges():
    """다른 지형이 섞이면 경계 칸에 shape(>0)가 생긴다."""
    w = h = 7
    data = make_empty_data(w, h)
    for y in range(h):
        for x in range(w):
            set_tile(data, x, y, w, h, 0, 2816)  # grass
    # 가운데에 dirt 한 칸 → 주변 grass 에 경계 shape 발생
    set_tile(data, 3, 3, w, h, 0, 3584)
    changed = apply_autotile(data, w, h, layer=0, oob_connected=True)
    assert changed > 0
    # dirt 인접 grass 중 적어도 하나는 shape 가 붙음
    neighbors = [(3, 2), (3, 4), (2, 3), (4, 3)]
    assert any(get_tile(data, x, y, w, h, 0) != 2816 for x, y in neighbors)
