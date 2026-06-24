"""타일 전수 카탈로그 빌더 (데이터 필요).

각 타일셋이 샘플맵에서 실제 사용하는 모든 타일을 추출해, 기계적 메타데이터
(kind, 통행성, 평균색, 빈도, 레이어 분포)를 카탈로그 JSON 으로 만든다.
시각 식별 없이 좌표 공식 + flags + 픽셀 평균만 사용 → 전수 자동화.

산출: agent/generation/mapgen/data/tile_catalog.json
이후 색 기반 자동 분류(2단계)와 생성 다양성(3단계)의 입력이 된다.

사용:
    uv run python -m agent.generation.mapgen.build_tile_catalog
"""

import argparse
import json
import logging
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from agent.generation.mapgen.autotile import apply_autotile, is_floor_autotile
from agent.generation.mapgen.palette import kind_of
from agent.generation.mapgen.tile_constants import make_empty_data, set_tile

logger = logging.getLogger(__name__)

_OUT_PATH = Path(__file__).parent / "data" / "tile_catalog.json"
_DEFAULT_BASE = Path("storage/games/base_game")
# 더미/투명 타일 제외
_SKIP = {0, 7520}


def _norm(t: int) -> int:
    return t - ((t - 2048) % 48) if t >= 2048 else t


def collect_usage(maps_dir: Path) -> dict[int, dict[int, Counter]]:
    """tileset_id → {base_id: Counter(layer)} 사용 빈도(레이어별)."""
    usage: dict[int, dict[int, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for mf in sorted(maps_dir.glob("Map*.json")):
        try:
            raw = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        tid = raw.get("tilesetId")
        w, h, data = raw.get("width", 0), raw.get("height", 0), raw.get("data", [])
        if not (tid and w and h and data):
            continue
        plane = w * h
        for layer in range(4):
            for i in range(layer * plane, (layer + 1) * plane):
                if i >= len(data):
                    break
                t = data[i]
                if t in _SKIP:
                    continue
                usage[tid][_norm(t)][layer] += 1
    return usage


def _avg_color(base_id: int, tid: int, base_game: Path, tmp: Path) -> tuple[int, int, int]:
    """타일을 3x3 렌더해 중앙 픽셀 평균색 추출."""
    from PIL import Image

    from agent.generation.mapgen.tile_renderer import render_data_to_png

    d = make_empty_data(3, 3)
    for y in range(3):
        for x in range(3):
            set_tile(d, x, y, 3, 3, 0, base_id)
    if is_floor_autotile(base_id):
        apply_autotile(d, 3, 3, layer=0, oob_connected=True)
    out = tmp / f"_cat_{tid}_{base_id}.png"
    try:
        render_data_to_png(d, 3, 3, tid, base_game, str(out), cell=24)
        from PIL import ImageStat

        img = Image.open(out).convert("RGB")
        crop = img.crop((24, 24, 48, 48))  # 중앙 타일
        return tuple(int(v) for v in ImageStat.Stat(crop).mean)  # type: ignore[return-value]
    except Exception:
        return (0, 0, 0)
    finally:
        if out.exists():
            out.unlink()


def build_catalog(base_game: Path) -> dict:
    """전수 카탈로그 생성."""
    tilesets_json = json.loads((base_game / "data" / "Tilesets.json").read_text(encoding="utf-8"))
    flags = {
        ts["id"]: ts["flags"]
        for ts in tilesets_json
        if isinstance(ts, dict) and "id" in ts and "flags" in ts
    }
    usage = collect_usage(base_game / "samplemaps")
    tmp = Path(tempfile.gettempdir())

    catalog: dict = {"schema_version": 1, "_comment": "타일 전수 카탈로그(자동생성). build_tile_catalog.py", "tilesets": {}}
    for tid in sorted(usage):
        f = flags.get(tid, [])
        entries = []
        for base_id, layer_counts in sorted(usage[tid].items()):
            total = sum(layer_counts.values())
            passable = (f[base_id] & 0x0F) == 0 if base_id < len(f) else None
            star = bool(f[base_id] & 0x10) if base_id < len(f) else False
            damage = bool(f[base_id] & 0x100) if base_id < len(f) else False
            avg = _avg_color(base_id, tid, base_game, tmp)
            entries.append(
                {
                    "base_id": base_id,
                    "kind": kind_of(base_id),
                    "passable": passable,
                    "star": star,
                    "damage": damage,
                    "rgb": list(avg),
                    "count": total,
                    "layers": {str(k): v for k, v in sorted(layer_counts.items())},
                }
            )
        catalog["tilesets"][str(tid)] = {"count": len(entries), "tiles": entries}
        logger.info("tileset %d: %d개 타일 카탈로그", tid, len(entries))
    return catalog


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="타일 전수 카탈로그 빌더")
    parser.add_argument("--base-game", type=Path, default=_DEFAULT_BASE)
    parser.add_argument("--out", type=Path, default=_OUT_PATH)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if not (args.base_game / "data" / "Tilesets.json").exists():
        parser.error(f"데이터 없음: {args.base_game} (storage/games/base_game 복원 필요)")

    catalog = build_catalog(args.base_game)
    args.out.write_text(json.dumps(catalog, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    total = sum(t["count"] for t in catalog["tilesets"].values())
    print(f"저장: {args.out} (총 {total}개 타일, {len(catalog['tilesets'])}개 타일셋)")


if __name__ == "__main__":
    main()
