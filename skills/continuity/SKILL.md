---
name: continuity
description: 같은 인물·같은 장소로 컷을 이어 그린다(컨티뉴얼 생성). 참고 그림(장소·얼굴·표정 칸)을 고르고 ComfyUI 워크플로우로 그린다. 컷 설명이 준비된 뒤, 또는 agent platform 에서 장면을 그려 달라고 할 때.
---

# 이어 그리기

## 핵심
같은 장면의 컷은 **같은 장소 그림과 같은 인물 참고 그림**을 계속 쓴다. 그래야 얼굴·머리·장소가 이어진다. 그림은 ComfyUI 워크플로우(Qwen Image 2.1)가 그린다.

## 참고 그림 고르기(근거 LESSONS 16)
- 인물: 시트를 통째로 넣지 않는다(시트의 줄 세운 전신·표정 칸을 베낀다). 시트 정면 전신의 **머리만**(무표정, `assets/vast_refs/<id>_bust.png`).
- 연기: 연기 글에 맞는 **표정 칸**을 얼굴 참고로 넣으면 그 표정을 따라 그린다(`--expr`, 표정 시트 12칸 + 센 표정 12가지에서 사내 Qwen 이 고름).
- 장소: 넓은 컷은 장소 그림을 맨 앞(`<image1>`, 구도가 첫 그림을 따른다). 클로즈업은 인물 먼저, 장소는 맨 뒤에 "배경만 흐리게", 장면 배치 문장은 뺀다.
- 앞 컷 그림은 참고로 넣지 않는다(그 구도를 통째로 가져와 사람이 는다).
- 글 속 이름 옆에 그림 번호를 붙인다("Woobin (the person in <image2>)").

## 작업실에서
- `vast/comfy_panel.py <작품 폴더> <panels.json> --episode <회차> --turbo --expr --fix --check`(MCP `vast_draw`). 결과 `panels_hf_vast/`.
- 한 화를 한 번에는 `tools/make_episode.py`(MCP `make_episode`).
- 인원이 하나 더 나오거나 잔상이 끼면 seed 만 바꿔 그 컷을 다시.

## agent platform 에서
도구: `webtoon_panel_one_character`(장소 + 인물 1명), `webtoon_panel_two_characters`(장소 + 인물 2명), `image_free`(참고 그림 없이). 모두 agent platform 이미지 생성 카탈로그의 ComfyUI 워크플로우다.
참고 그림 이름은 `studio_<작품>_<인물 id>.png`, 장소는 `studio_<작품>_loc_<장소>.png`(작품 부분은 도구의 허용값 목록에 나온 그대로). 도구의 허용값 목록에서 정확히 고른다(예: 밤거리 = studio_<작품>_loc_night_street.png). 맞는 장소가 없으면 사용자에게 알린다.
프롬프트는 영어로: 샷, 무엇이 일어나는지, 연기(눈썹·눈·입·몸짓), 옷, 카메라. 장소는 `<image1>`, 인물은 "the person in <image2>"(두 명이면 `<image3>` 도). 인원은 "exactly 1 person" 또는 "exactly 2 people"로 못 박는다.
크기: 세로 컷은 기본(1184x1760), 가로 컷은 width 1664 · height 1248.
그린 뒤 결과 그림을 보고 인원·좌우·시선·표정이 설명과 다르면 seed 를 바꿔 그 컷만 다시 그린다(한 컷에 두 번까지). 한 번의 답에서 컷 6장 안팎까지 그리고, 남으면 "이어서 그릴까요?"로 끝낸다.
