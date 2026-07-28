"""LLM → 우리 로직으로 맵 1개를 이미지로 뽑는 통합 테스트 (실제 Solar 호출).

파이프라인(= 우리가 목표한 "LLM 감독 + 컴파일러" 구조의 첫 실전):
  사용자 쿼리 → Solar(structured output)가 MapPlan(테마·구성) 설계 → 우리 blueprint
  컴파일러가 **큐레이션 vignette 라이브러리**로 조립·렌더. 샘플맵 100% 복사가 아니라
  발굴한 조각을 우리 로직으로 배치한다. LLM 은 raw 타일을 절대 만들지 않는다.

실행:
  # 기본 쿼리(스폰지밥 비키니시티)로 이미지 생성
  JWT_SECRET_KEY=test-secret uv run pytest agent/tests/generation/test_llm_map_image.py -s
  # 커스텀 쿼리
  JWT_SECRET_KEY=test-secret QUERY="사막 도적단의 은신처" \
      uv run python -m agent.tests.generation.test_llm_map_image

이미지는 리포지토리 루트에 llm_map.png 로 저장된다.
"""

import asyncio
import os
from pathlib import Path
from typing import Literal

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agent.core.config import agent_config
from agent.core.llm_client import invoke_llm
from agent.generation.mapgen.blueprint import compile_blueprint
from agent.generation.mapgen.layout import pack
from agent.generation.mapgen.tile_renderer import render_data_to_png
from agent.generation.mapgen.vignette import Vignette
from agent.generation.mapgen.vignette_mine import mine_curated, mine_dir
from app.backend.core.config import settings

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SAMPLEMAPS = Path(settings.BASE_GAME_PATH) / "samplemaps"
_DEFAULT_QUERY = "스폰지밥의 비키니시티 모험기"

# 큐레이션 라이브러리가 실제로 보유한 테마(이 셋 중에서만 LLM 이 고른다 — 타일셋2 기준)
_THEMES = ("grassland", "snow", "desert")


class MapPlan(BaseModel):
    """LLM 이 설계하는 '맵 기획'. raw 타일이 아니라 테마·구성만 정한다."""

    theme: Literal["grassland", "snow", "desert"] = Field(
        description="맵 바닥 테마. 쿼리에 가장 어울리는 것. 바다/도시 등은 grassland 로 근사."
    )
    map_name: str = Field(description="맵 이름 (예: 비키니시티)")
    has_landmark: bool = Field(description="중앙에 큰 랜드마크(성·탑·피라미드)를 둘지")
    n_houses: int = Field(ge=0, le=4, description="배치할 집 개수 (0~4)")
    n_temples: int = Field(ge=0, le=2, description="배치할 신전/중형 건물 개수 (0~2)")
    include_props: bool = Field(description="석상·우물 같은 소품을 둘지")
    tree_border: bool = Field(description="맵 가장자리를 나무로 두를지")
    reasoning: str = Field(description="이 구성이 쿼리에 어울리는 이유(한국어 1~2문장)")


_SYSTEM_PROMPT = f"""당신은 RPG 타일맵 레벨 디자이너입니다.
사용자의 게임 아이디어를 받아, 어울리는 마을/맵의 '구성 기획(MapPlan)'을 설계합니다.

중요:
- 당신은 개별 타일을 그리지 않습니다. 오직 테마와 어떤 구조물을 얼마나 둘지만 정합니다.
  실제 타일 배치·렌더링은 별도 엔진이 검증된 구조물 조각으로 수행합니다.
- theme 는 반드시 {_THEMES} 중 하나입니다. 바다·해저·도시처럼 딱 맞는 게 없으면 가장
  가까운 grassland 로 근사하세요(눈=한랭/설원, 사막=열사/유적).
- 쿼리 분위기에 맞게 landmark/house/temple/props/tree_border 를 정하세요.
  (예: 웅장한 왕국 → 랜드마크 O, 집 많이 / 조용한 은신처 → 랜드마크 X, 집 적게)

반드시 JSON 스키마를 정확히 따르세요."""


async def plan_map(query: str) -> MapPlan:
    """쿼리 → MapPlan (Solar structured output)."""
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=f"게임 아이디어: {query}\n이 맵의 MapPlan 을 설계하세요."),
    ]
    result = await invoke_llm(messages, structured_output=MapPlan, temperature=0.2)
    assert isinstance(result, MapPlan)
    return result


def _by_theme_size(structs: list[Vignette], theme: str) -> dict[str, list[Vignette]]:
    """테마의 구조물을 크기등급별로 분류."""
    out: dict[str, list[Vignette]] = {"small": [], "medium": [], "large": []}
    for v in structs:
        if v.theme == theme:
            out[v.size_class].append(v)
    return out


def plan_to_render(plan: MapPlan, seed: int = 7) -> tuple[list[int], int, int, int]:
    """MapPlan → 큐레이션 vignette 를 패킹 배치 → blueprint 컴파일 → (data, W, H, tileset).

    배치는 고정 슬롯이 아니라 `layout.pack` — 각 vignette 의 실제 실루엣 크기로 자리를
    잡고, 맵 크기도 그 내용에 맞춰 역산한다(랜드마크가 옆 건물을 덮는 문제의 근원).
    """
    structs = mine_curated(_SAMPLEMAPS, tileset_id=2)
    decor = [v for v in mine_dir(_SAMPLEMAPS, 2, include_decor=True) if v.kind == "decor"]

    theme = plan.theme if plan.theme in _THEMES else "grassland"
    tiered = _by_theme_size(structs, theme)
    # 테마에 그 등급이 없으면(눈=중/대형 없음 등) 그 등급은 건너뛴다(테마 일관성 유지).

    smalls = tiered["small"] or tiered["medium"] or tiered["large"]
    wish: list[Vignette] = []
    if plan.has_landmark and tiered["large"]:
        wish.append(tiered["large"][0])
    wish += tiered["medium"][: plan.n_temples]
    wish += [smalls[i % len(smalls)] for i in range(plan.n_houses)] if smalls else []

    props: list[Vignette] = []
    if plan.include_props:
        props = [d for d in decor if d.theme == theme and 2 <= d.height <= 3 and d.width <= 3][:3]

    # 빈 곳 채우기 풀은 소형에 중형·나머지 대형까지 섞는다 — 눈·사막은 라이브러리가
    # 작아(소형 0~3채) 소형만 쓰면 같은 집이 복제된다.
    filler = smalls + tiered["medium"] + tiered["large"][1:]
    lay = pack(wish, filler=filler, props=props, seed=seed)
    blueprint = {
        "size": [lay.width, lay.height],
        "tileset": 2,
        "theme": theme,
        "street": {"y": lay.street_y, "x0": 3, "x1": lay.width - 4},
        "houses": lay.houses,
        "props": lay.props,
        "gardens": lay.gardens if theme == "grassland" else [],
        "tree_border": plan.tree_border,
    }
    return compile_blueprint(blueprint, lay.vignettes, seed=seed)


async def generate_map_image(query: str, out_path: Path) -> MapPlan:
    """쿼리 → LLM 설계 → 우리 로직 렌더 → PNG 저장. 반환: MapPlan."""
    plan = await plan_map(query)
    data, w, h, tid = plan_to_render(plan)
    render_data_to_png(data, w, h, tid, Path(settings.BASE_GAME_PATH), str(out_path), cell=18)
    return plan


@pytest.mark.skipif(not agent_config.LLM_API_KEY, reason="LLM_API_KEY 미설정")
def test_generate_map_from_query() -> None:
    """기본 쿼리로 LLM이 설계한 맵 1개를 루트에 이미지로 뽑고, 파일 생성을 확인한다."""
    query = os.environ.get("QUERY", _DEFAULT_QUERY)
    out = _REPO_ROOT / "llm_map.png"
    plan = asyncio.run(generate_map_image(query, out))
    print(f"\n[MapPlan] theme={plan.theme} name={plan.map_name} "
          f"landmark={plan.has_landmark} houses={plan.n_houses} temples={plan.n_temples}")
    print(f"[reasoning] {plan.reasoning}")
    print(f"[image] {out}")
    assert out.exists() and out.stat().st_size > 0


if __name__ == "__main__":
    q = os.environ.get("QUERY", _DEFAULT_QUERY)
    dest = _REPO_ROOT / "llm_map.png"
    p = asyncio.run(generate_map_image(q, dest))
    print(f"쿼리: {q}")
    print(f"MapPlan: theme={p.theme}, name={p.map_name}, landmark={p.has_landmark}, "
          f"houses={p.n_houses}, temples={p.n_temples}, props={p.include_props}")
    print(f"이유: {p.reasoning}")
    print(f"이미지 저장: {dest}")
