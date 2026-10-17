---
name: webtoon
description: 세로 스크롤 웹툰 한 화를 만드는 템플릿. 시나리오 → 콘티 → 컷 설명 → 이어 그리기 → 연출 검수 → 식자·출판.
---

# 웹툰 템플릿

## 묶음
- **스킬(순서대로)**: `storyboard` → `panel-spec` → (`asset`, 새 장소·인물이 있으면) → `continuity` → `staging-review` → `lettering-publish`
- **ComfyUI 워크플로우(코어)**: 생성(참고 그림으로 컷) · 편집(남는 사람 지우기·표정·옷) · 에셋(표정 은행·베이스 몸) · 시안(장소·인물 시트). 서버에 저장된 것 `webtoon_vertical_qwen21` · `webtoon_panel_2stage_qwen21`, agent platform 카탈로그 `studio_webtoon_panel_1` · `studio_webtoon_panel_2` · `studio_image_free`
- **출력 형식**: 세로 조각(폭 800) · PDF · epub · 예고편(mp4·gif) → `exports/<날짜>/`

## 작품마다 바뀌는 것(데이터)
`series/<작품>-hf/` 의 `cast.json`(인물) · `prompts/style*.txt`(그림체) · `make_location_prompts.py`·`make_sheet_prompts.py`(새 장소·인물 프롬프트) · `assets/`(시트·장소·표정 은행). 스킬과 워크플로우는 작품을 가리지 않는다.

## 한 화 순서
1. 시나리오 받기(없으면 `worklogs/OPEN-ISSUES.md` 의 제안을 보여 주고 묻는다)
2. `storyboard`: 앞 회차 틀로 회차를 열고 콘티를 쓴다 → 사용자에게 컷 목록을 보여 준다
3. `panel-spec`: 컷 설명을 채운다 → prepare_episode 로 빠진 컷·연기 없는 컷이 0 인지
4. `tools/make_episode.py <작품 폴더> <회차>`: 에셋 점검(새 것은 시안 후 멈춤) → 이어 그리기 → 연출 검수 → 식자·출판
5. 결과 PDF 와 "사람 눈으로 확인할 컷"을 보여 주고 걸린 컷만 다시

## 지킬 것
인물은 모두 성인. 실존 인물·실제 회사 이름 금지. 남의 작품 그림은 참고 그림으로 넣지 않는다. em dash 쓰지 않는다.
