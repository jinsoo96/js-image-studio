# skills : 이미지 스튜디오 스킬

이미지 스튜디오의 구조(2026-10-16 팀장 방향): **템플릿 = 스킬 + ComfyUI 워크플로우 + 출력 형식**, 핵심 코어는 ComfyUI 워크플로우.
스킬은 "무엇을 어떤 순서·규칙으로 할지"를 적은 설명서(SKILL.md)와, 그 스킬이 부르는 도구(작업실 스크립트·MCP 도구)로 이뤄진다.
이미지 전용 에이전트는 따로 두지 않는다. 어떤 에이전트든 이 작업실(MCP · 스킬)을 붙이면 이미지를 만들고, 작업에 맞는 스킬을 읽고 따른다(미사일 스튜디오의 어시스턴트, Claude Code 등).

## 템플릿
| 템플릿 | 파일 | 상태 |
|---|---|---|
| 웹툰 | `webtoon/TEMPLATE.md` | 지금(작품 가리지 않음) |
| 캐릭터 디자인 · 상품 컷 · 썸네일·배너 | 아직 없음 | 예정 |

## 스킬
| 스킬 | 언제 | 주로 부르는 것 |
|---|---|---|
| `storyboard` 콘티 | 시나리오를 받아 한 화를 짤 때 | MCP new_episode · write_file(part) · prepare_episode |
| `panel-spec` 컷 설명 | 콘티의 컷마다 인물·옷·장소·카메라·연기를 정할 때 | MCP set_panels · prepare_episode |
| `continuity` 이어 그리기 | 같은 인물·장소로 컷을 이어 그릴 때 | vast/comfy_panel.py(--expr) · MCP draw |
| `asset` 에셋 | 새 장소·인물이 나오거나 표정이 모자랄 때 | vast/new_assets.py · vast/make_assets.py · MCP draw_candidates · make_assets |
| `staging-review` 연출 검수 | 그린 뒤 인원·시점·좌우·시선·표정을 볼 때 | vast/refine.py(--fix · --check) · MCP review_panels |
| `lettering-publish` 식자·출판 | 그림을 한 화로 엮어 낼 때 | tools/run_episode.py · qa_gate · check_bubbles · export |

한 화를 한 번에: `tools/make_episode.py`(MCP make_episode)가 continuity → staging-review → lettering-publish 를 차례로 부른다. storyboard 와 panel-spec 은 사람·에이전트가 먼저 쓴다.

## 정본과 쓰는 곳
- 정본은 이 폴더의 SKILL.md 다. 규칙의 근거(사용자가 짚은 일)는 작업 PC 의 LESSONS.md(레포 밖)에 남고, SKILL.md 는 그중 지금 지켜야 할 것만 짧게 모은다.
- 에이전트는 MCP `read_doc('skill:이름')` 으로 읽는다. Claude Code 는 파일을 바로 읽는다.
- 에이전트 역할 프롬프트는 둘이다: 「새 웹툰 생성기」(새 작품 열기 → 1화, MCP `new_series`) · 「웹툰 이어 그리기」(있는 작품의 다음 화). 공통 규칙은 `mcp/AGENT_PROMPT.md`, 역할 머리말은 `mcp/agents/`. 도구·스킬은 같고 시작점만 다르다.
