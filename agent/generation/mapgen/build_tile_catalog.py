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
import re
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


def collect_sheet_tiles(base_game: Path) -> dict[int, set[int]]:
    """tileset_id → 시트에 비투명으로 실제 존재하는 B~E(0~1023) tile_id 전수.

    samplemaps 사용 여부와 무관 — 타일셋 이미지 시트를 직접 스캔한다(선인장·부엌가구처럼
    샘플맵에 안 쓰인 오브젝트도 포함). B~E 좌표 공식은 tile_renderer._draw_tile 과 동일.
    """
    from agent.generation.mapgen.tile_renderer import load_tileset_images

    result: dict[int, set[int]] = {}
    for tid in (1, 2, 3, 4, 5, 6):
        imgs = load_tileset_images(tid, base_game)
        found: set[int] = set()
        for tile_id in range(1024):
            si = 5 + tile_id // 256
            if si >= len(imgs) or imgs[si] is None:
                continue
            sheet = imgs[si]
            sx = ((tile_id // 128 % 2) * 8 + tile_id % 8) * 48
            sy = (tile_id % 256 // 8 % 16) * 48
            if sx + 48 > sheet.width or sy + 48 > sheet.height:
                continue
            if sheet.crop((sx, sy, sx + 48, sy + 48)).split()[3].getextrema()[1] > 0:
                found.add(tile_id)  # 알파 최대 >0 = 비투명 타일 존재
        result[tid] = found
    return result


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
    sheets = collect_sheet_tiles(base_game)  # 시트 전수(B~E) — samplemaps 미사용 타일도 포함
    tmp = Path(tempfile.gettempdir())

    catalog: dict = {"schema_version": 1, "_comment": "타일 전수 카탈로그(자동생성). build_tile_catalog.py", "tilesets": {}}
    for tid in sorted(set(usage) | set(sheets)):
        f = flags.get(tid, [])
        entries = []
        # 카탈로그 대상 = samplemaps 사용 타일 ∪ 시트 비투명 전수(B~E). count=0 은 미사용 타일.
        all_ids = set(usage.get(tid, {})) | sheets.get(tid, set())
        for base_id in sorted(all_ids):
            layer_counts = usage.get(tid, {}).get(base_id, Counter())
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
    enrich_with_palette(catalog, base_game, recompute_missing=False)  # palette 의미정보 결합
    return catalog


# 타일셋 → 바이옴 친화 태그 (생성기 biome 연동의 기준)
_TS_BIOME = {
    1: ["world"],
    2: ["grassland", "desert", "snow", "wetland", "town"],
    3: ["interior"],
    4: ["dungeon", "lava", "ice", "poison", "sand", "crystal", "moss", "dark"],
    5: ["city", "sf_outdoor"],
    6: ["sf_interior"],
}


def _category(name: str) -> str:
    """palette 이름에서 의미 카테고리 도출(끝의 변형/번호 접미 제거)."""
    c = re.sub(r"_\d+$", "", name)   # _320 같은 id 접미
    c = re.sub(r"_[a-z]$", "", c)    # _a/_b 변형 접미
    c = re.sub(r"\d+$", "", c)       # grass2 → grass
    return c or name


def _palette_index(tid: int, palette: dict):
    """tid의 base_id→[이름들], 멀티타일 member base_id→[멀티이름], 멀티 footprint."""
    t = palette["tilesets"].get(str(tid), {})
    names: dict[int, list[str]] = {}
    for sec in ("terrain", "objects"):
        for k, v in (t.get(sec) or {}).items():
            if isinstance(v, dict) and "base_id" in v:
                names.setdefault(int(v["base_id"]), []).append(k)
    mt_member: dict[int, list[str]] = {}
    mt_info: dict[str, dict] = {}
    for k, v in (t.get("multitile") or {}).items():
        if isinstance(v, dict) and "tiles" in v:
            mt_info[k] = {"rows": len(v["tiles"]), "cols": max(len(r) for r in v["tiles"])}
            for r in v["tiles"]:
                for x in r:
                    mt_member.setdefault(int(x), []).append(k)
    return names, mt_member, mt_info


def enrich_with_palette(catalog: dict, base_game: Path, recompute_missing: bool = True) -> dict:
    """catalog 타일에 palette 의미정보(name/category/multitile/biome) 결합 + 누락 타일 보강.

    기존 rgb/count/layers/passable 은 보존(재계산 안 함). 타일셋별 categories 역색인 추가.
    """
    palette = json.loads(
        (Path(__file__).parent / "data" / "tile_palette.json").read_text(encoding="utf-8")
    )
    tilesets_json = json.loads((base_game / "data" / "Tilesets.json").read_text(encoding="utf-8"))
    flags = {
        ts["id"]: ts["flags"]
        for ts in tilesets_json
        if isinstance(ts, dict) and "id" in ts and "flags" in ts
    }
    tmp = Path(tempfile.gettempdir())
    for tid in range(1, 7):
        names, mt_member, mt_info = _palette_index(tid, palette)
        tcat = catalog["tilesets"].setdefault(str(tid), {"count": 0, "tiles": []})
        by_id = {e["base_id"]: e for e in tcat["tiles"]}
        f = flags.get(tid, [])
        # palette 엔 있으나 catalog 에 없는 타일 보강
        for bid in sorted((set(names) | set(mt_member)) - set(by_id)):
            avg = _avg_color(bid, tid, base_game, tmp) if recompute_missing else (0, 0, 0)
            e = {
                "base_id": bid,
                "kind": kind_of(bid),
                "passable": (f[bid] & 0x0F) == 0 if bid < len(f) else None,
                "star": bool(f[bid] & 0x10) if bid < len(f) else False,
                "damage": bool(f[bid] & 0x100) if bid < len(f) else False,
                "rgb": list(avg),
                "count": 0,
                "layers": {},
            }
            tcat["tiles"].append(e)
            by_id[bid] = e
        # 의미필드 부여 + 카테고리 역색인
        biome = _TS_BIOME.get(tid, [])
        cat_index: dict[str, list[int]] = defaultdict(list)
        for e in tcat["tiles"]:
            nms = names.get(e["base_id"], [])
            e["name"] = nms[0] if nms else None
            if len(nms) > 1:
                e["aliases"] = nms[1:]
            e["category"] = _category(nms[0]) if nms else None
            e["multitile"] = mt_member.get(e["base_id"], [])
            e["biome"] = biome
            if e["category"]:
                cat_index[e["category"]].append(e["base_id"])
        tcat["tiles"].sort(key=lambda e: e["base_id"])
        tcat["count"] = len(tcat["tiles"])
        tcat["categories"] = {c: sorted(set(ids)) for c, ids in sorted(cat_index.items())}
        tcat["multitiles"] = mt_info
    catalog["schema_version"] = 2
    return catalog


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="타일 전수 카탈로그 빌더")
    parser.add_argument("--base-game", type=Path, default=_DEFAULT_BASE)
    parser.add_argument("--out", type=Path, default=_OUT_PATH)
    parser.add_argument("--enrich-only", action="store_true",
                        help="rgb 재계산 없이 기존 카탈로그에 palette 의미정보만 결합")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if not (args.base_game / "data" / "Tilesets.json").exists():
        parser.error(f"데이터 없음: {args.base_game} (storage/games/base_game 복원 필요)")

    if args.enrich_only:
        catalog = json.loads(args.out.read_text(encoding="utf-8"))
        catalog = enrich_with_palette(catalog, args.base_game)
    else:
        catalog = build_catalog(args.base_game)
    args.out.write_text(json.dumps(catalog, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    total = sum(t["count"] for t in catalog["tilesets"].values())
    print(f"저장: {args.out} (총 {total}개 타일, {len(catalog['tilesets'])}개 타일셋)")


if __name__ == "__main__":
    main()
