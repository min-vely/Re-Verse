"""오토타일 shape lookup 테이블 추출기 (데이터 기반, 데이터 필요).

샘플맵(이미 올바르게 칠해진 정답)에서 "이웃 코너패턴 -> shape" 매핑을 추출한다.
RPG Maker MZ floor 오토타일 알고리즘을 직접 구현하는 대신 정답 데이터에서 학습한다.

핵심 규칙 (검증됨):
  - 8방향 이웃이 같은 오토타일 kind(동일 base_id)면 "연결".
  - 코너(대각) 비트는 인접한 두 변이 둘 다 연결일 때만 유효(코너 정규화).
  - 정규화된 코너패턴(47종) -> shape 는 1:1 대응.
  - 각 패턴의 최빈 shape 채택 → 제작자 노이즈(경계에 채움타일) 제거.

산출: agent/generation/mapgen/data/autotile_floor_shapes.json

사용 (데이터 복원 후):
    uv run python -m agent.generation.mapgen.build_autotile_table \
        --maps-dir storage/games/base_game/samplemaps

canonical: docs/rpgmaker/tile_palette.md, docs/rpgmaker/tile_rendering.md §3
"""

import argparse
import json
import logging
from collections import Counter, defaultdict
from pathlib import Path

from agent.generation.mapgen.autotile import _DIRS, base_of, corner_mask, is_floor_autotile

logger = logging.getLogger(__name__)

_OUT_PATH = Path(__file__).parent / "data" / "autotile_floor_shapes.json"


def build_table(maps_dir: Path) -> tuple[dict[int, int], dict]:
    """샘플맵에서 lookup({corner_mask: shape})과 품질 통계를 추출."""
    table: dict[int, Counter] = defaultdict(Counter)
    samples = 0

    for mf in sorted(maps_dir.glob("Map*.json")):
        try:
            raw = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(raw, dict) or raw.get("tilesetId") != 2:
            continue
        w, h, data = raw.get("width", 0), raw.get("height", 0), raw.get("data", [])
        if not (w and h and data):
            continue

        def g(x: int, y: int) -> int:
            return data[y * w + x]  # noqa: B023  (layer 0)

        for y in range(1, h - 1):
            for x in range(1, w - 1):
                t = g(x, y)
                if not is_floor_autotile(t):
                    continue
                base = base_of(t)
                shape = t - base
                m = 0
                for bit, (dx, dy) in enumerate(_DIRS):
                    nb = g(x + dx, y + dy)
                    if nb >= 2048 and base_of(nb) == base:
                        m |= 1 << bit
                table[corner_mask(m)][shape] += 1
                samples += 1

    lookup = {m: cnt.most_common(1)[0][0] for m, cnt in table.items()}
    match = sum(cnt[lookup[m]] for m, cnt in table.items())
    stats = {
        "samples": samples,
        "patterns": len(lookup),
        "distinct_shapes": len(set(lookup.values())),
        "consistency_pct": round(100 * match / samples, 2) if samples else 0.0,
    }
    return lookup, stats


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="오토타일 shape lookup 추출")
    parser.add_argument(
        "--maps-dir",
        type=Path,
        default=Path("storage/games/base_game/samplemaps"),
        help="샘플맵 디렉토리",
    )
    parser.add_argument("--out", type=Path, default=_OUT_PATH)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if not args.maps_dir.exists():
        parser.error(f"샘플맵 디렉토리 없음: {args.maps_dir} (데이터 복원 필요)")

    lookup, stats = build_table(args.maps_dir)
    if not lookup:
        parser.error("추출된 패턴이 없습니다. tilesetId=2 샘플맵이 있는지 확인하세요.")

    payload = {
        "schema_version": 1,
        "type": "floor",
        "_comment": (
            "RPG Maker MZ floor 오토타일 shape lookup. corner_mask(8bit, 코너정규화) -> shape(0~47). "
            "샘플맵에서 데이터 기반 추출. 재생성: build_autotile_table.py. "
            "마스크 비트: N E S W NE SE SW NW (코너는 양옆 변이 모두 연결일 때만 유효)."
        ),
        "stats": stats,
        "table": {str(k): v for k, v in sorted(lookup.items())},
    }
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"저장: {args.out}")
    print(f"  표본={stats['samples']} 패턴={stats['patterns']} "
          f"distinct_shapes={stats['distinct_shapes']} 일관성={stats['consistency_pct']}%")


if __name__ == "__main__":
    main()
