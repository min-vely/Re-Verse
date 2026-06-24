# 타일 팔레트 — 타일 ID → 지형 의미 레퍼런스

> 절차적 맵 생성(타일을 한 칸씩 직접 까는 방식)을 위한 "어떤 타일 ID가 어떤 지형인가" 정의서.
> 기계용 데이터는 [`agent/generation/mapgen/data/tile_palette.json`](../../agent/generation/mapgen/data/tile_palette.json),
> 로더는 [`agent/generation/mapgen/palette.py`](../../agent/generation/mapgen/palette.py).
> 인코딩 규칙 원본은 [`docs/rpgmaker/tile_rendering.md`](./tile_rendering.md), 맵 구조는 [`structure.md`](./structure.md).

---

## ✅ 검증 상태: tileset 1~6 핵심 지형 검증 완료 (verified)

tileset 1~6의 핵심 지형 base_id는 실제 `base_game/data/Tilesets.json`의 `flags`(통행성) + `samplemaps/Map*.json` 사용 빈도 + 타일셋 시트 해석으로 **대조 검증**했다 (`verify_palette.py` 이슈 0건, 모두 `verified: true`).

| tileset | 핵심 지형 (통과 / 막힘) |
|---|---|
| 1 필드 | grass(2816) / water(2048) |
| 2 외곽 | grass·dirt·sand·snow·gravel·stone_path / water·wall — §5 상세 |
| 3 내부 | floor(3584)·tile_floor(4016)·carpet(4064) / wall(6512)·void(1536) |
| 4 던전 | floor(5888)·floor_gray(5936)·moss(6080) / lava(2240)·water(2528)·void(1536) |
| 5 SF외곽 | road(1554)·grass(2816)·pavement(2912)·tile(5936) / water(2624)·void(1544) |
| 6 SF내부 | floor(5936)·tile_floor(1620)·metal_floor(1603) / wall(6368)·wall2(6320) |

> ⚠️ tileset 3~6은 **지형만** 정의됨 (오브젝트·생성기 미연결). terrain 노이즈 생성기는 야외(2/5)용이고, 실내(3/6)는 방 생성기, 던전(4)은 BSP 방-복도가 적합 — 후속 과제.

> 검증 과정에서 `tile_constants.py`의 초안값이 grass(2816)를 제외하고 거의 다 틀렸음이 드러나(예: 3584는 나무가 아니라 갈색 흙바닥), 실측 기반으로 교체했다. 재검증/확장은:
> ```bash
> uv run python -m agent.generation.mapgen.verify_palette --tileset 1,2 \
>   --tilesets-path storage/games/base_game/data/Tilesets.json \
>   --maps-dir storage/games/base_game/samplemaps
> # 진단: --dump-usage N (레이어별 base_id 빈도 상위 N)
> ```
> ※ 데이터(`storage/games/`)는 `.gitignore`라 로컬에만 존재.

---

## 1. 타일 ID 인코딩 규칙

타일 ID는 임의 번호가 아니라 **타일셋 PNG 시트 내 위치로 인코딩된 값**이다. 같은 의미라도 tileset마다 ID가 다르다.

| 시트(배열 인덱스) | 종류 | tileId 범위 |
|---|---|---|
| 0 | A1 (애니메이션: 물·폭포) | 2048~2815 |
| 1 | A2 (지면 오토타일) | 2816~4351 |
| 2 | A3 (지붕 오토타일) | 4352~5887 |
| 3 | A4 (벽 오토타일) | 5888~8191 |
| 4 | A5 (일반 단일 타일) | 1536~2047 |
| 5~8 | B / C / D / E (장식·오브젝트) | 0~1023 |

**오토타일(A1~A4) shape 인코딩** — 같은 지형이라도 주변 연결 모양에 따라 48종으로 갈린다:

```
globalKind = (tileId - 2048) // 48     # 지형 종류
shape      = (tileId - 2048) %  48     # 모서리/이음새 모양 (0~47)
```

→ palette의 `base_id`는 **shape=0(=full) 기준 ID**다. 절차적 생성 1차에서는 base_id 단일값으로 깔고, 오토타일 모서리 보정은 후속 과제(현 `town_generator`와 동일 방식). 정규화는 `palette.normalize_autotile_id()`.

---

## 2. 맵 data 배열 레이어 구조

`MapXXX.json`의 `data`는 1차원 정수 배열, 길이 `width × height × 6`.

```
index = layer × (width × height) + y × width + x
```

| 레이어 | MZ 표준 용도 | 값 |
|---|---|---|
| 0~3 | 일반 타일 (tileId) | 0=빈 칸, 그 외 타일 ID |
| 4 | 그림자 (4비트 플래그) | 0~15 |
| 5 | 리전 ID | 0~255 |

> ⚠️ **주의**: 이 프로젝트의 알고리즘 생성기/`tile_checker.is_walkable` 폴백은 **레이어 5를 통행 플래그(0=가능, 1=불가)로 자체 약속**해서 쓴다. 이는 MZ 표준(레이어 5 = 리전 ID)과 다르다. 샘플맵을 읽을 때와 알고리즘 맵을 만들 때 레이어 5의 의미가 다름에 유의.

---

## 3. flags 비트 — 타일 통행/속성

실제 통행성은 `Tilesets.json`의 `flags[tileId]` 정수로 결정된다 (출처: [`tile_checker.py`](../../agent/generation/mapgen/tile_checker.py) L13-22).

| 상수 | 값 | 의미 |
|---|---|---|
| FLAG_IMP_DOWN | 0x01 | 아래쪽 통행 불가 |
| FLAG_IMP_LEFT | 0x02 | 왼쪽 통행 불가 |
| FLAG_IMP_RIGHT | 0x04 | 오른쪽 통행 불가 |
| FLAG_IMP_UP | 0x08 | 위쪽 통행 불가 |
| FLAG_STAR | 0x10 | 플레이어 위에 표시 (천장·숲 상단) |
| FLAG_LADDER | 0x20 | 사다리 |
| FLAG_BUSH | 0x40 | 풀숲 (하단 반투명) |
| FLAG_COUNTER | 0x80 | 카운터 (상점 테이블) |
| FLAG_DAMAGE | 0x100 | 데미지 바닥 (독늪·용암) |

- 통행 판정: `flags[tileId] & 0x0F == 0` → 4방향 모두 통행 가능. `> 0` → 일부 방향 막힘.
- palette의 `passable` 필드는 이 통행 비트를 사람이 읽기 쉽게 요약한 **초안**이며, 검증 시 `flags & 0x0F == 0`과 대조한다.

---

## 4. tileset_id 정체성 (중요)

코드 곳곳에 혼선이 있어 기준을 못박는다.

| tileset_id | 표준 이름 | 실제 용도 | mode |
|---|---|---|---|
| 1 | 필드 (World_A1/A2) | 월드맵(전체 지도) | 0 |
| 2 | 외곽 (Outside_A1~A5) | 마을·숲·사막·야외 | 1 |
| 3 | 내부 (Inside) | 실내 | 1 |
| 4 | 던전 (Dungeon) | 동굴·미궁 | 1 |
| 5 | SF 외곽 | 현대·SF 야외 | 1 |
| 6 | SF 내부 | 현대·SF 실내 | 1 |

(출처: [`integrator.py`](../../agent/generation/nodes/integrator.py) `load_base_tilesets` L110-210, [`intent/extractor.py`](../../agent/generation/mapgen/intent/extractor.py) L26-33)

> ⚠️ **알려진 모순** (절차적 생성기 구현 시 교정 대상):
> - `mapgen/__init__.py`의 `MAP_SIZE_BY_TYPE`는 `town → tileset 1`, `dungeon → tileset 2`로 매핑하지만, **실제 마을 샘플맵은 전부 tileset 2(외곽)**, 던전은 tileset 4가 맞다. 따라서 이 팔레트는 **마을(town) = tileset 2** 기준으로 정의했다.
> - `tile_constants.py`의 주석 "tileset_id=1 Exterior"는 틀렸다 (1은 World).

---

## 5. 팔레트 — tileset 1~2

`base_id`는 shape=0 기준. `layer`는 배치 레이어. `passable`은 통행(초안). `kind`는 base_id가 속한 시트(자동 도출).

### tileset 1 — 필드(World), mode=0 · 월드맵 (검증됨)
> samplemaps 빈도 + flags 로 검증. 추가 지형은 `--dump-usage 1`로 확장.

| 지형 | base_id | kind | layer | 통행 | 비고 |
|---|---|---|---|---|---|
| grass | 2816 | A2 | 0 | ✅ | 잔디 (used 8193) |
| water | 2048 | A1 | 0 | ❌ | 물 (used 30049) |

### tileset 2 — 외곽(Outside), mode=1 · 마을·야외 (검증됨, 이슈 0)
> `Outside_A2.png` 시트 해석 + `Tilesets.json.flags` + samplemaps 빈도로 확정한 핵심 지형. grass 외 기존 초안값은 전부 틀려 교체됨.

| 지형 | base_id | kind | layer | 통행 | flags | 시트(tx,ty) · 비고 |
|---|---|---|---|---|---|---|
| grass | 2816 | A2 | 0 | ✅ | 0x0600 | (0,2) 초록 잔디 |
| dirt | 3584 | A2 | 0 | ✅ | 0x0600 | (0,4) 갈색 흙바닥 ← 기존 "tree" 오류 자리 |
| sand | 3200 | A2 | 0 | ✅ | 0x0600 | (0,3) 모래 |
| snow | 3968 | A2 | 0 | ✅ | 0x0600 | (0,5) 눈 |
| gravel | 2912 | A2 | 0 | ✅ | 0x0600 | (2,2) 회색 자갈 |
| stone_path | 2960 | A2 | 0 | ✅ | 0x0600 | (3,2) 회색 포장길 |
| water | 2048 | A1 | 0 | ❌ | 0x080F | 물 |
| wall | 3488 | A2 | 1 | ❌ | 0x0E0F | (6,3) 회색 성벽/장애물 |

> 나무·문·상점 등 **오브젝트 타일은 B/C 시트(base_id 0~1023)**에 있어 이번 핵심 범위에서 제외(후속 확장). A2 시트의 노란 캐노피(3008 등)는 통행 가능(bush)이라 장애물이 아님.

---

## 6. 사용법 (절차적 생성기에서)

```python
from agent.generation.mapgen import palette

palette.get_tile_id(2, "grass")        # -> 2816
palette.get_terrain(2, "tree")          # -> {"base_id": 3584, "layer": 1, "passable": False, ...}
palette.impassable_ids(2)               # -> {3584, 3456, 4352, 2370, 2050, ...}
palette.terrain_map(2)                   # -> {"grass": 2816, "tree": 3584, ...} (tile_constants 호환)
palette.kind_of(2816)                    # -> "A2"
palette.normalize_autotile_id(2821)      # -> 2816 (shape 제거)
```

---

## 7. 미해결 / 후속 과제

- [x] **검증 완료**: `verify_palette.py` 이슈 0건, tileset 1~2 `verified: true`.
- [x] **A1 범위 의심값 해소**: 초안의 building/door/well/path류는 전부 틀린 ID로 확인되어 실측 핵심 지형으로 교체.
- [ ] **오브젝트 타일 확장**: 나무·문·상점 등은 B/C 시트(base_id 0~1023)에 있음. tileset 2에 오브젝트 지형 추가.
- [x] **오토타일 shape 계산**: `autotile.py`(데이터 기반 lookup, A1 물 + A2 지면 floor) 구현. 47패턴↔47shape 1:1. 실제 렌더(`tile_renderer.py`)로 이음새 확인.
- [ ] **A4(벽) 오토타일**: 진짜 벽 타일(A4)은 별도 WALL_AUTOTILE_TABLE(16 shape) 필요. 현재 wall 은 A2 성벽으로 floor 처리 중.
- [ ] **tileset 3~6 팔레트 확장**.
- [ ] `MAP_SIZE_BY_TYPE`의 tileset_id 매핑 교정 (town 1→2 등).
