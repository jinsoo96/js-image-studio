---
name: asset
description: 인물·장소 에셋을 만든다. 새 장소·새 인물 시트 시안, 표정 은행(센 표정 12가지)·베이스 몸. 새 화에 처음 나오는 장소·인물이 있거나 표정이 모자랄 때.
---

# 에셋

## 언제
- 새 웹툰을 열 때(MCP `new_series`: 작품 설정 한 장 → 콘티 전에 시트·장소 그림을 보려면 `draw_candidates(작품)`, 아니면 첫 회차 `make_episode` 가 없는 인물 시트·장소 시안을 전부 만들고 멈춘다)
- 콘티에 assets 에 없는 장소·인물이 나올 때(`make_episode` 가 알아서 시안을 만들고 멈춘다)
- 새 인물 시트를 만든 뒤, 그 인물의 표정 은행과 베이스 몸이 필요할 때

## 순서
1. 프롬프트: 새 웹툰 생성기로 연 작품(`series.json` 이 있는 작품)은 MCP `add_to_series`(= `tools/new_series.py`)가 설정에서 `prompts/loc_*.txt`·`sheet_*.txt` 를 쓴다. `series.json` 이 없는 옛 작품은 그 작품 폴더의 `make_location_prompts.py` LOC 에 한 줄, `make_sheet_prompts.py` 에 한 줄 → 돌린다(`add_to_series` 가 그렇다고 알려 준다).
2. 시안: `vast/new_assets.py <작품 폴더> location|sheet <이름>` → `assets/_candidates/`. 장소는 남는 사람 지우기까지 한다. 새 작품의 첫 인물 시트는 본보기 없이 글로만, 그다음 인물은 먼저 나온 시트(시안 포함)를 그림체·배치 본보기로 쓴다. 다시 뽑기는 MCP `redo_candidate`(지난 시안은 `_candidates/_rejected/`).
3. **사용자 확인**: 시안을 보여 주고 고르면 `--accept`(또는 `make_episode --accept-new`)로 assets 에 넣는다. 이미 있는 그림은 덮지 않는다.
4. 인물 참고 그림: `vast/sheet_refs.py`(머리·얼굴 칸·표정 칸 자르기) → `vast/make_assets.py <작품 폴더> <인물|all> both --res 768`(센 표정 12가지 · 회색 기본복 베이스 몸, 큐에 한꺼번에 넣어 장당 약 3.5초).

## 규칙
- 새 인물은 나이가 젊게 흐르는 경향이 있다(중년 팀장이 30대로). 시안을 꼭 사람이 보고, 마음에 안 들면 그 시트만 다시 뽑는다(MCP `redo_candidate`, 생김새를 바꾸려면 먼저 `add_to_series`).
- 베이스 몸은 맨몸 대신 몸선이 보이는 회색 반팔·반바지.
- 남의 작품 그림은 참고 그림으로 넣지 않는다. 실존 인물 얼굴 금지.
