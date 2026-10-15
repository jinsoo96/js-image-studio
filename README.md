# webtoon-studio

세로 스크롤 웹툰을 만드는 작업실. 사용자가 이야기를 주면 콘티 → 힉스필드 작화(Seedream 5.0 Pro) → 말풍선 합성 → PDF·epub·예고편까지 만든다.
세션 규칙은 `CLAUDE.md`, 사용자가 짚은 문제와 막는 법은 `LESSONS.md`, 지금 상태는 `worklogs\OPEN-ISSUES.md`.

## 폴더 지도
```
CLAUDE.md                 세션 첫 장(읽는 순서·지킬 것)
README.md                 이 파일
LESSONS.md                교훈집(그리기 전에 반드시)
series\
  d-plus-18-hf\           D+18(지금 작품). README.md 부터
  mudae-dwi-hf\           무대 뒤(지금 작품). README.md 부터
  _legacy\                옛 ComfyUI 판 글 자료(참고만)
tools\                    도구(아래 표)
mcp\                      작업실 MCP 서버(agent app·Qwen 에서 쓰기, mcp\README.md)
vast\                     회사 vast.ai GPU 의 ComfyUI(Qwen Image 2.1)로 컷 그리기·세로 워크플로우(vast\README.md)
requirements.txt          파이썬 패키지(정확한 버전)
state\                    hf_uploads\(힉스필드에 올린 참고 그림 id, 계정별) · mcp_jobs\·mcp_sheets\(MCP 작업 기록·모아보기). 깃에 안 올림
exports\<날짜>\           사용자에게 넘긴 PDF·epub·예고편
reader\index.html         열람 페이지 원본(올리는 법은 reader\README.md)
worklogs\                 OPEN-ISSUES.md · 2026-10-history.md(지난 기록) · 날짜 메모 · acting\GUIDE.md(연기 쓰는 법)
fonts\                    식자 글꼴(OFL)
models\                   detect\(말풍선용 얼굴 찾기)·u2net\(배경 지우기)은 지금도 씀. lora*\ 는 쉬는 ComfyUI 판
cloud\ style\ workflows\  쉬는 ComfyUI·RunPod 설정(쓰지 않음)
.venv\                    파이썬(pillow·numpy·rembg·onnxruntime)
```

## 작품 폴더 구성(`series\<작품>-hf\`)
```
README.md            회차 목록·정본 표시·다음 할 일
SERIES.md            설정·인물·회차 줄거리      STYLE.md   그림 규칙·결정 이력
cast.json            인물 목록(id·영어 이름·나이 특징, 컷 도구가 읽음)
assets\              <id>_sheet.png(설정표) · <id>_outfits.png(옷) · <id>_expressions.png(표정 12) · locations\<장소>.png · ref\ · lineup\
ASSETS.md            에셋 목록·빠짐 점검(tools\hf_assets.py 가 만듦)
prompts\             설정표·옷·장소 프롬프트(make_*_prompts.py 가 씀), panels\<회차>\ 컷 프롬프트(자동)
make_sheet_prompts.py · make_location_prompts.py · make_expression_prompts.py
episodes\epNNN\
  make_storyboard.py → storyboard.json   콘티(대사·순서·말풍선 자리). 정본은 make_storyboard.py
  build_panels.py    → panels.json       컷 설명(인물·옷·장소·구도·연기 ACT·장면 배치 SCENES·어깨 너머 CAM·재사용 REUSE)
  assets\            그 회차에만 쓰는 옷 시트(있으면 먼저 씀)
  panels_hf\         힉스필드가 그린 원본(+ hf_log\ 비용·프롬프트 기록, _rejected\ 다시 그리기 전 원본)
  panels\ parts\     합성용(가로 1600, tools\hf_place.py 가 놓음)
  trailer.json       예고편 구성
  out\               <ep>_full.png · slices\ · <ep>.pdf · <ep>.epub · trailer.mp4·gif
  review\            점검표(합성 때 다시 생김, 지워도 됨)
```

## 한 화 만드는 순서
```powershell
$py = "D:\webtoon-studio\.venv\Scripts\python.exe"; $env:PYTHONUTF8 = 1; $env:PYTHONIOENCODING = "utf-8"
$ep = "series\d-plus-18-hf\episodes\ep003"
# 0) 새 인물·옷·장소가 필요하면: make_*_prompts.py 에 한 줄 → hf_gen.py 로 2k 한 장 → 사용자 확인 → assets\ 에 두고 cast.json·ASSETS.md
& $py tools\hf_gen.py --model seedream_v5_pro --resolution 2k --aspect 16:9 --out series\d-plus-18-hf\assets\locations --name hotel_room --prompt-file series\d-plus-18-hf\prompts\loc_hotel_room.txt
# 1) 콘티와 컷 설명(앞 회차 파일을 본떠서. 2화 v2 가 가장 최신 본보기: 재사용 REUSE·어깨 너머 CAM·옷 시트 고르기)
& $py $ep\make_storyboard.py; & $py $ep\build_panels.py
# 2) 그리기: 기준 컷(SCENES 의 anchor)이 먼저 그려지게. 두 줄로 나눠 동시에 돌려도 되지만 같은 컷이 두 목록에 들어가면 안 된다
& $py tools\hf_panel.py series\d-plus-18-hf $ep\panels.json --episode ep003 --n 1 [--only p003,p005]
# 3) 검수: 장면별로 펼쳐 보고(공간·좌우·인원·옷·표정), 걸린 컷만 _rejected 로 옮긴 뒤 --only 로 다시
# 4) 놓기 → 합성(합성 재료 컷 + compose + PDF + 점검표) → 기계 점검
& $py tools\hf_place.py $ep; & $py tools\run_episode.py $ep
& $py tools\qa_gate.py $ep; & $py tools\check_bubbles.py $ep        # 막는 경고 0 이어야 넘긴다
# 5) 말풍선 얹은 조각(out\slices)을 처음부터 끝까지 통독 → 예고편·epub → exports\<날짜>\ 로 복사 → 열람 페이지(reader\README.md)
& $py tools\trailer2.py $ep; & $py tools\to_epub.py $ep
```
- 작은 부분만 고칠 때(계급장·손가락 등): `tools\hf_patch.py <그림> --at x,y --size s --prompt-file <txt>`(원본은 _rejected 로).
- 크레딧 확인: `D:\.claude\tools\higgsfield\hf.exe account status`.

## 도구
| 지금 쓰는 도구 | 하는 일 |
|---|---|
| `hf_panel.py` | 컷 그리기(panels.json → panels_hf\). 인물 시트·옷 시트·표정 시트·장소·앞 컷을 참고 그림으로. 검열 오탐이면 시트를 빼고 한 번 더. `camera` 칸이 있으면 어깨 너머 숏 |
| `hf_gen.py` | 힉스필드 한 장 그리기(설정표·장소 등). 실행 전 비용을 찍고 로그를 남김 |
| `hf_patch.py` | 그림의 작은 부분만 다시 그리기 |
| `hf_place.py` | panels_hf\ → panels\ (가로 1600). 다시 그린 뒤 꼭 |
| `hf_assets.py` | 에셋 목록·빠짐 점검 → ASSETS.md |
| `run_episode.py` | 합성 재료 컷 → compose → PDF → 점검표(그릴 컷이 없으면 파드를 켜지 않음) |
| `compose.py` · `lettering.py` | 컷을 세로로 쌓고 말풍선·내레이션·효과음을 얹음(말풍선은 말하는 사람 쪽·얼굴 피함) |
| `phone_ui.py` | 휴대폰 화면(카톡·통화 기록·차단 목록·인스타·달력)·제목 화면(작가 표기) |
| `qa_gate.py` · `check_bubbles.py` · `char_check.py` · `face_detect.py` | 넘기기 전 기계 점검(자막 문장·말풍선 얼굴 가림·인물 같은지) |
| `to_pdf.py` · `to_epub.py` · `trailer2.py` | PDF · epub(작가 역할 포함) · 세로 예고편(mp4·gif, trailer.json) |
| `splash.py` | 회차 오프닝 표지(무대 뒤, --credit 작가 표기) |
| `sepia_flashback.py` · `tv_frame.py` · `ecg_graphic.py` · `strip_panel.py` | 합성 재료 컷(콘티 composite 로 run_episode 가 부름) |
| `review.py` | 점검표(run_episode 가 부름) |
| `collage.py` · `cover.py` · `lineup.py` | 표지·라인업 만들 때 가끔 |

| 쉬는 도구(ComfyUI·RunPod 판) | |
|---|---|
| `comfy.py` | 이름은 예전 것이지만 ROOT·load_json 등 공용 함수가 있어 지금 도구들도 읽는다. 지우지 않는다 |
| `render.py` · `runpod_cloud.py` · `train_lora.py` | ComfyUI 작화·RunPod 파드·LoRA 학습. 쓰려면 `worklogs\2026-10-04-runpod.md`·`2026-10-history.md` 먼저 |

## 다른 PC 에서 시작하기(레포 `github.com/jinsoo96/webtoon-studio`, 비공개)
1. 받기: `git clone https://github.com/jinsoo96/webtoon-studio.git` (D: 가 아니어도 된다. 경로는 상대 경로로 돈다)
2. 파이썬: `uv venv .venv --python 3.12` → `uv pip install --python .venv\Scripts\python.exe -r requirements.txt`
3. 힉스필드 CLI(`hf.exe`)를 깔고 로그인, 위치가 기본값(`D:\.claude\tools\higgsfield\hf.exe`)과 다르면 환경변수 `HF_CLI` 로 준다. 계정이 달라도 참고 그림은 `state\hf_uploads\<계정>.json` 에 새로 쌓인다.
4. 레포에는 만드는 법(콘티·컷 설명·도구·문서)과 에셋(설정표·옷·표정·장소)만 있다. 그려 놓은 컷·합성본·내보내기·모델은 용량 때문에 처음 PC(D:)에만 있다. 이어 그리면 새 PC 에서 새로 쌓인다.
5. 얼굴 찾기·배경 지우기 모델(`models\detect`·`models\u2net`)은 레포에 없다. 말풍선 얼굴 피하기·오프닝 표지에 필요하면 처음 PC 에서 복사해 온다.

## agent app·Qwen 에서 쓰기
`mcp\server.py` 를 agent app 설정의 로컬 MCP 로 붙이면 agent platform 에이전트(사내 vLLM `qwen3.8-27b` 등)가 작업실 도구 22개를 쓴다(vast 로 크레딧 없이 그리는 vast_draw 등 4개 포함). 붙이는 법·에이전트 설정·시스템 프롬프트(`mcp\AGENT_PROMPT.md`)는 `mcp\README.md`. 크레딧을 쓰는 도구는 예상 비용 확인 값이 있어야 돈다.

## vast.ai GPU(ComfyUI)에서 그리기
회사 공용 vast.ai(RTX 5090)의 ComfyUI 에 Qwen Image 2.1 을 받아 두고, 같은 `panels.json` 으로 컷을 그린다(`vast\comfy_panel.py`, 크레딧 0, 터보 컷당 약 30~50초). 서버 화면용 세로 워크플로우는 `webtoon_vertical_qwen21`. 공용 장비라 조회·그리기만 하고 기준선 워크플로우는 건드리지 않는다. 쓰는 법·지킬 것은 `vast\README.md`.

## 크레딧(힉스필드 Seedream 5.0 Pro, 2026-10-14)
| 무엇 | 크레딧 |
|---|---|
| 컷 1.5k 한 장 | 1.25 |
| 설정표·장소·옷 시트 2k 한 장 | 2.5 |
| 검열 오탐("nsfw") | 0 |
| 한 화 새로 그리기(약 100컷) | 약 130 |
| 2화 v2(재사용 35컷) | 약 45 |
