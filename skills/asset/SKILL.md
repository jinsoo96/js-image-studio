---
name: asset
description: 인물·장소 에셋을 만든다. 새 장소·새 인물 시트 시안, 표정 은행(센 표정 12가지)·베이스 몸, agent platform 용 참고 그림 라이브러리. 새 화에 처음 나오는 장소·인물이 있거나 표정이 모자랄 때.
---

# 에셋

## 언제
- 콘티에 assets 에 없는 장소·인물이 나올 때(`make_episode` 가 알아서 시안을 만들고 멈춘다)
- 새 인물 시트를 만든 뒤, 그 인물의 표정 은행과 베이스 몸이 필요할 때
- agent platform 에서 쓸 참고 그림 라이브러리를 새로 올리거나 vast 인스턴스가 바뀌었을 때

## 순서
1. 프롬프트: 장소는 작품의 `make_location_prompts.py` LOC 에 한 줄, 인물은 `make_sheet_prompts.py` 에 한 줄 → 돌리면 `prompts/loc_*.txt` · `sheet_*.txt`.
2. 시안: `vast/new_assets.py <작품 폴더> location|sheet <이름>` → `assets/_candidates/`. 장소는 남는 사람 지우기까지 한다.
3. **사용자 확인**: 시안을 보여 주고 고르면 `--accept`(또는 `make_episode --accept-new`)로 assets 에 넣는다. 이미 있는 그림은 덮지 않는다.
4. 인물 참고 그림: `vast/sheet_refs.py`(머리·얼굴 칸·표정 칸 자르기) → `vast/make_assets.py <작품 폴더> <인물|all> both --res 768`(센 표정 12가지 · 회색 기본복 베이스 몸, 큐에 한꺼번에 넣어 장당 약 3.5초).
5. agent platform 라이브러리: `vast/xgen_catalog.py assets`(studio_* 이름으로 ComfyUI input 에).

## 규칙
- 새 인물은 나이가 젊게 흐르는 경향이 있다(중년 팀장이 30대로). 시안을 꼭 사람이 보고, 마음에 안 들면 그 시트만 힉스필드로(크레딧 확인 뒤).
- 베이스 몸은 맨몸 대신 몸선이 보이는 회색 반팔·반바지.
- 남의 작품 그림은 참고 그림으로 넣지 않는다. 실존 인물 얼굴 금지.
