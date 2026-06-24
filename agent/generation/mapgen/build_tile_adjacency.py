"""타일 인접 규칙 전수조사 빌더 (데이터 필요).

RPG Maker 오토타일은 가장자리(물가·절벽 밑 등)가 타일에 구워져 있어, 어떤 지형
옆에는 특정 지형만 와야 자연스럽다(예: 잔디물가 물 2048 → 잔디로 둘러쌈). 이 규칙을
손으로 추측하지 않고, 샘플맵 전체에서 레이어0 오토타일의 **직교 이웃 분포**를 집계해
데이터로 추출한다(오토타일 shape·카탈로그 빌더와 동일한 데이터 기반 원칙).

산출: agent/generation/mapgen/data/tile_adjacency.json
구조: tilesets[ts].tiles[base] = {
    count, obj_frac, empty_frac,
    ground: {neighbor_base: fraction},  # 지면 이웃만으로 정규화(합≈1)
}

사용:
    uv run python -m agent.generation.mapgen.build_tile_adjacency
"""

import argparse
import json
import logging
from collections import Counter, defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

_OUT_PATH = Path(__file__).parent / "data" / "tile_adjacency.json"
_DEFAULT_BASE = Path("storage/games/base_game")
_ORTHO = [(0, -1), (1, 0), (0, 1), (-1, 0)]


def _base_of(t: int) -> int:
    return t - ((t - 2048) % 48) if t >= 2048 else t


def _is_auto(t: int) -> bool:
    """A1~A4 오토타일(가장자리 구운 지형). A5/B~E(단일·오브젝트)는 제외."""
    return t >= 2048


def collect(maps_dir: Path) -> dict:
    """tileset → base → {count, ground neighbor Counter, obj, empty}."""
    occ: dict[int, Counter] = defaultdict(Counter)
    ground: dict[int, dict[int, Counter]] = defaultdict(lambda: defaultdict(Counter))
    obj_cnt: dict[int, Counter] = defaultdict(Counter)
    empty_cnt: dict[int, Counter] = defaultdict(Counter)
    nmaps: Counter = Counter()

    for mf in sorted(maps_dir.glob("Map*.json")):
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(m, dict):
            continue
        ts, w, h, data = m.get("tilesetId"), m.get("width"), m.get("height"), m.get("data")
        if ts is None or not data or not w or not h:
            continue
        nmaps[ts] += 1
        for y in range(h):
            for x in range(w):
                t = data[y * w + x]
                if not _is_auto(t):
                    continue
                b = _base_of(t)
                occ[ts][b] += 1
                for dx, dy in _ORTHO:
                    nx, ny = x + dx, y + dy
                    if not (0 <= nx < w and 0 <= ny < h):
                        continue
                    nt = data[ny * w + nx]
                    if _is_auto(nt):
                        nb = _base_of(nt)
                        if nb != b:
                            ground[ts][b][nb] += 1
                    elif nt > 0:
                        obj_cnt[ts][b] += 1
                    else:
                        empty_cnt[ts][b] += 1
    return {"occ": occ, "ground": ground, "obj": obj_cnt, "empty": empty_cnt, "nmaps": nmaps}


def build(raw: dict, min_count: int = 20) -> dict:
    """집계 → 정규화 JSON. min_count 미만 등장 base 는 노이즈로 제외."""
    occ, ground, obj_cnt, empty_cnt, nmaps = (
        raw["occ"], raw["ground"], raw["obj"], raw["empty"], raw["nmaps"]
    )
    out: dict = {"schema_version": 1, "tilesets": {}}
    for ts in sorted(occ):
        tiles = {}
        for b, n in occ[ts].items():
            if n < min_count:
                continue
            g = ground[ts][b]
            gtot = sum(g.values())
            edges = gtot + obj_cnt[ts][b] + empty_cnt[ts][b]
            gnorm = {str(nb): round(c / gtot, 4) for nb, c in g.most_common() if gtot} if gtot else {}
            tiles[str(b)] = {
                "count": n,
                "obj_frac": round(obj_cnt[ts][b] / edges, 4) if edges else 0.0,
                "empty_frac": round(empty_cnt[ts][b] / edges, 4) if edges else 0.0,
                "ground": gnorm,
            }
        out["tilesets"][str(ts)] = {"maps": nmaps[ts], "tiles": tiles}
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="타일 인접 규칙 전수조사 빌더")
    ap.add_argument("--base-game", type=Path, default=_DEFAULT_BASE)
    ap.add_argument("--out", type=Path, default=_OUT_PATH)
    ap.add_argument("--min-count", type=int, default=20)
    ap.add_argument("--show", action="store_true", help="타일셋별 요약 출력")
    args = ap.parse_args(argv)

    maps_dir = args.base_game / "samplemaps"
    if not maps_dir.exists():
        raise SystemExit(f"샘플맵 없음: {maps_dir} (게임 데이터 필요)")

    raw = collect(maps_dir)
    data = build(raw, min_count=args.min_count)
    args.out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(t["tiles"]) for t in data["tilesets"].values())
    print(f"인접 테이블 저장: {args.out} (타일셋 {len(data['tilesets'])}, base {total}종)")

    if args.show:
        for ts, td in data["tilesets"].items():
            print(f"\n=== tileset {ts} (맵 {td['maps']}) ===")
            items = sorted(td["tiles"].items(), key=lambda kv: -kv[1]["count"])
            for b, info in items[:12]:
                top = list(info["ground"].items())[:4]
                s = ", ".join(f"{nb}:{round(f * 100)}%" for nb, f in top)
                print(f"  {b:>5} (n={info['count']:6}, obj{round(info['obj_frac'] * 100)}%): {s}")


if __name__ == "__main__":
    main()
