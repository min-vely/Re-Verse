"""tile_palette.json 검증 스크립트 (실제 데이터 필요).

저장소에는 Tilesets.json / MapXXX.json 이 .gitignore(storage/games/) 로 제외되어 있어
**로컬에서 데이터 복원 후에만** 실행할 수 있다.

하는 일:
  1) 샘플맵들의 data 배열을 레이어 0~3 으로 풀어, tileset 별로 실제 등장하는
     base_id(오토타일은 shape 정규화) 빈도를 집계.
  2) tile_palette.json 의 각 terrain.base_id 가 (a) 실제로 쓰이는지,
     (b) Tilesets.json.flags[base_id] 의 통행 비트(& 0x0F)와 palette 의 passable 이 맞는지 대조.
  3) 불일치/미사용을 리포트. --fix 시 passable 을 flags 기준으로 교정하고 verified=true 로 전환.

사용:
  uv run python -m agent.generation.mapgen.verify_palette --tileset 1,2
  uv run python -m agent.generation.mapgen.verify_palette --tileset 2 --fix

canonical: docs/rpgmaker/tile_palette.md §검증 상태
"""

import argparse
import json
import logging
from collections import Counter
from pathlib import Path

from agent.generation.mapgen import palette as pal
from agent.generation.mapgen.tile_checker import FLAG_DAMAGE

logger = logging.getLogger(__name__)

_PALETTE_PATH = Path(__file__).parent / "data" / "tile_palette.json"

# flags 하위 4비트(0x0F) 중 하나라도 켜져 있으면 해당 방향 통행 불가
_IMPASSABLE_BITS = 0x0F


def _default_paths() -> tuple[Path, Path]:
    """settings.BASE_GAME_PATH 기준 (Tilesets.json, samplemaps 디렉토리) 반환."""
    from app.backend.core.config import settings

    base = Path(settings.BASE_GAME_PATH)
    return base / "data" / "Tilesets.json", base / "samplemaps"


def load_tileset_flags(tilesets_path: Path) -> dict[int, list[int]]:
    """Tilesets.json → {tileset_id: flags[]} 매핑."""
    data = json.loads(tilesets_path.read_text(encoding="utf-8"))
    out: dict[int, list[int]] = {}
    for ts in data:
        if isinstance(ts, dict) and "id" in ts and "flags" in ts:
            out[int(ts["id"])] = ts["flags"]
    return out


def collect_usage(maps_dir: Path) -> dict[int, Counter]:
    """샘플맵들에서 tileset_id 별 base_id 빈도(레이어 0~3, 오토타일 정규화) 집계."""
    usage: dict[int, Counter] = {}
    map_files = sorted(maps_dir.glob("Map*.json"))
    if not map_files:
        logger.warning("샘플맵 없음: %s", maps_dir)
        return usage

    for mf in map_files:
        try:
            raw = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("맵 로드 실패: %s", mf.name)
            continue
        if not isinstance(raw, dict):
            continue  # 맵 JSON 이 아닌 파일(배열 등) 건너뜀
        tid = int(raw.get("tilesetId", 0))
        w, h = raw.get("width", 0), raw.get("height", 0)
        data = raw.get("data", [])
        if not (w and h and data):
            continue
        counter = usage.setdefault(tid, Counter())
        plane = w * h
        # 레이어 0~3 만 (4=그림자, 5=리전)
        for layer in range(4):
            for i in range(layer * plane, (layer + 1) * plane):
                if i >= len(data):
                    break
                tile_id = data[i]
                if tile_id == 0:
                    continue
                counter[pal.normalize_autotile_id(tile_id)] += 1
    return usage


def collect_usage_by_layer(maps_dir: Path) -> dict[int, dict[int, Counter]]:
    """tileset_id → {layer: Counter(base_id)} 집계 (레이어 0~3 분리).

    바닥(layer 0)과 오브젝트(layer 1) ID 를 분리해서 봐야 건물/나무 등을 식별할 수 있다.
    """
    usage: dict[int, dict[int, Counter]] = {}
    for mf in sorted(maps_dir.glob("Map*.json")):
        try:
            raw = json.loads(mf.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("맵 로드 실패: %s", mf.name)
            continue
        if not isinstance(raw, dict):
            continue
        tid = int(raw.get("tilesetId", 0))
        w, h = raw.get("width", 0), raw.get("height", 0)
        data = raw.get("data", [])
        if not (w and h and data):
            continue
        per_layer = usage.setdefault(tid, {})
        plane = w * h
        for layer in range(4):
            counter = per_layer.setdefault(layer, Counter())
            for i in range(layer * plane, (layer + 1) * plane):
                if i >= len(data):
                    break
                tile_id = data[i]
                if tile_id == 0:
                    continue
                counter[pal.normalize_autotile_id(tile_id)] += 1
    return usage


def dump_usage(
    tileset_ids: list[int], tilesets_path: Path, maps_dir: Path, top_n: int = 25
) -> None:
    """tileset 별 레이어 0~1 의 base_id 빈도 상위 N개 출력 (kind/통행 포함).

    올바른 path/tree/building base_id 를 역으로 찾기 위한 진단용.
    """
    flags_map = load_tileset_flags(tilesets_path)
    usage = collect_usage_by_layer(maps_dir)

    for tid in tileset_ids:
        flags = flags_map.get(tid)
        per_layer = usage.get(tid, {})
        print(f"\n===== tileset {tid} : 레이어별 base_id 빈도 상위 {top_n} =====")
        for layer in (0, 1, 2, 3):
            counter = per_layer.get(layer, Counter())
            if not counter:
                continue
            print(f"  --- layer {layer} ({'바닥' if layer == 0 else '오브젝트'}) ---")
            for base_id, cnt in counter.most_common(top_n):
                kind = pal.kind_of(base_id)
                passable = _flag_passable(flags, base_id) if flags else None
                pstr = {True: "통행O", False: "통행X", None: "?"}[passable]
                print(f"    base_id={base_id:5d}  {kind:5s}  used={cnt:7d}  {pstr}")


def _flag_passable(flags: list[int], base_id: int) -> bool | None:
    """flags[base_id] 의 통행 비트로 통행 가능 여부 판정. 범위 밖이면 None."""
    if 0 <= base_id < len(flags):
        return (flags[base_id] & _IMPASSABLE_BITS) == 0
    return None


def verify(
    tileset_ids: list[int],
    tilesets_path: Path,
    maps_dir: Path,
    fix: bool = False,
) -> int:
    """검증 실행. 불일치 개수 반환."""
    flags_map = load_tileset_flags(tilesets_path)
    usage = collect_usage(maps_dir)
    palette = json.loads(_PALETTE_PATH.read_text(encoding="utf-8"))

    total_issues = 0
    changed = False

    for tid in tileset_ids:
        ts_block = palette.get("tilesets", {}).get(str(tid))
        if not ts_block:
            print(f"[tileset {tid}] palette 정의 없음 — 건너뜀")
            continue
        flags = flags_map.get(tid)
        counter = usage.get(tid, Counter())
        print(f"\n=== tileset {tid} ({ts_block.get('name', '?')}) ===")
        if flags is None:
            print(f"  [!] Tilesets.json 에 id={tid} flags 없음 - 통행 검증 불가")
        print(f"  샘플맵 사용 base_id 종류: {len(counter)}개")

        for name, terrain in ts_block.get("terrain", {}).items():
            base_id = int(terrain["base_id"])
            used = counter.get(base_id, 0)
            issues = []

            # (a) 실제 사용 여부
            if counter and used == 0:
                issues.append("샘플맵 미사용")

            # (b) 통행 일치 + 데미지 타일 점검
            if flags is not None:
                actual = _flag_passable(flags, base_id)
                if actual is None:
                    issues.append(f"flags 범위 밖(base_id={base_id})")
                elif actual != bool(terrain.get("passable", True)):
                    issues.append(
                        f"통행 불일치(palette={terrain.get('passable')} vs flags={actual})"
                    )
                    if fix:
                        terrain["passable"] = actual
                        changed = True
                if 0 <= base_id < len(flags) and (flags[base_id] & FLAG_DAMAGE):
                    issues.append("데미지 타일(0x100)")

            status = "OK" if not issues else "  ".join(issues)
            mark = "OK " if not issues else "[!]"
            print(f"  {mark} {name:14s} base_id={base_id:5d} used={used:5d}  {status}")
            total_issues += len(issues)

        if fix and flags is not None:
            ts_block["verified"] = True
            changed = True

    if fix and changed:
        _PALETTE_PATH.write_text(
            json.dumps(palette, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n[fixed] tile_palette.json 교정 저장 완료 ({_PALETTE_PATH})")

    print(f"\n총 이슈: {total_issues}건")
    return total_issues


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="tile_palette.json 검증")
    parser.add_argument("--tileset", default="1,2", help="검증할 tileset_id (쉼표구분). 예: 1,2")
    parser.add_argument("--tilesets-path", type=Path, default=None, help="Tilesets.json 경로")
    parser.add_argument("--maps-dir", type=Path, default=None, help="샘플맵 디렉토리")
    parser.add_argument("--fix", action="store_true", help="불일치를 flags 기준으로 교정")
    parser.add_argument(
        "--dump-usage",
        type=int,
        metavar="N",
        default=None,
        help="검증 대신 레이어별 base_id 빈도 상위 N개를 출력(진단용)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    default_ts, default_maps = (None, None)
    if args.tilesets_path is None or args.maps_dir is None:
        try:
            default_ts, default_maps = _default_paths()
        except Exception as e:  # app 레이어/설정 부재
            logger.error("기본 경로 확인 실패(%s). --tilesets-path/--maps-dir 직접 지정 필요.", e)

    tilesets_path = args.tilesets_path or default_ts
    maps_dir = args.maps_dir or default_maps

    if not tilesets_path or not Path(tilesets_path).exists():
        parser.error(
            f"Tilesets.json 을 찾을 수 없습니다: {tilesets_path}\n"
            "데이터(storage/games/base_game)를 복원했는지 확인하세요."
        )
    if not maps_dir or not Path(maps_dir).exists():
        parser.error(f"샘플맵 디렉토리를 찾을 수 없습니다: {maps_dir}")

    tileset_ids = [int(t) for t in str(args.tileset).split(",") if t.strip()]
    if args.dump_usage is not None:
        dump_usage(tileset_ids, Path(tilesets_path), Path(maps_dir), top_n=args.dump_usage)
        return
    verify(tileset_ids, Path(tilesets_path), Path(maps_dir), fix=args.fix)


if __name__ == "__main__":
    main()
