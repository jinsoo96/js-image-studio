# 열람 페이지

사용자가 폰·PC 로 보는 페이지: https://claude.ai/artifact/8rWisQfChm3tSsBbabfCSh (비공개, 사용자 소유). 원본은 이 폴더의 `index.html`.

## 탭 구성(2026-10-14 v29)
| 탭 키 | 폴더 | 내용 |
|---|---|---|
| d2v | `d2v\` | D+18 2화 v2(정본, 첫 화면) |
| mh | `mh\` | 무대 뒤 1화 |
| d2 | `d2\` | D+18 2화 v1(보관) |
| h2 | `h2\` | D+18 1화 |
| hf · d18 · md | | 옛 판(10/13판·ComfyUI 판). 페이지에 올라가 있는 사본만 남아 있다 |

탭마다 `<section id="키">` 안에 `<div class="strip" data-dir="폴더" data-ep="조각 이름 앞부분" data-n="조각 수">`, 아래 스크립트의 `COUNT`·`KEYS` 에 키를 더한다. 조각 이름은 `<data-ep>_001.jpg`(합성 결과 `out\slices\`).

## 고쳐 올리는 법
1. 이 세션에서 처음 올리면 Artifact 도구로 `read`(url 위 주소) 한 번.
2. 새 회차면 `out\slices\*.jpg` 와 `out\trailer.mp4` 를 이 폴더 `<탭 폴더>\` 로 복사하고, `index.html` 에 탭·section·COUNT·KEYS 를 더한다.
3. Artifact `publish`: file_path = 이 폴더의 `index.html`, url = 위 주소, root = 이 폴더, files = 바뀐 조각·예고편만. 한 번에 64MB 를 넘으면 두 번에 나눠 올린다.
4. 올린 뒤 `<탭 폴더>\` 의 조각은 지워도 된다(페이지에 남아 있고, 원본은 각 회차 `out\slices\`).
