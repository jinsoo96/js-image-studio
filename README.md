# 웹툰 작업실 (`webtoon-studio`)

**무엇을 하나**: 기존 작품의 그림(설정표·표정표·장소·컷)을 참고해 그 작품의 인물과 그림체를 따라 하면서, 우리 GPU 서버의 ComfyUI(공개 모델)로 세로 스크롤 웹툰을 대화만으로 만든다. 대화 LLM(미사일 스튜디오·agent app 에이전트)이 작업실 MCP 도구를 골라 쓴다.

흐름: 콘티 → 관문(격리 judge) → ComfyUI 작화(그림 상자, Qwen Image 2.1 Uncensored fp8 + 참고 그림 + 그림체 LoRA) → 검수(LLM 상자의 VLM 이 연출·인물·소품을 봄) → 고치기 → 말풍선 합성 → PDF·epub·예고편.

**힉스필드(Seedream)는 쓰지 않는다**(2026-10-22 부터). D+18 1·2화 정본 컷이 그때 그린 것으로 남아 있을 뿐이다. 폴더 이름 `panels_hf\` 와 `hf_*` 도구 이름은 그 시절 이름이 남은 것이고, 지금 `panels_hf\` 는 "본편 컷" 폴더다(아래 작품 폴더 구성).

세션 규칙은 `CLAUDE.md`, 사용자가 짚은 문제와 막는 법은 `LESSONS.md`, 지금 상태는 `worklogs\OPEN-ISSUES.md`.

## 구성
```
[사용자] ─ 대화 ─▶ 미사일 스튜디오(이 PC 7700) ──HTTP MCP──▶ 작업실 MCP 서버 mcp\server.py (이 PC 7440, https://webtoon.js-96.com/mcp)
                   agent app 에이전트(어느 PC든) ────────────────▶        │ 작업(job)을 띄움: state\mcp_jobs\
                                                                    ▼
                                         vast\comfy_panel.py · panel_edit.py · lora.py · tools\run_episode.py …(이 PC 파이썬)
                                                                    │ SSH 터널(tools\vast_cloud.py)
                                         ┌──────────────────────────┴───────────────────────────┐
                                         ▼                                                      ▼
                       그림 상자 cloud\vast_img.json                            LLM 상자 cloud\vast_llm.json
                       ComfyUI 8188 + 에이전트 8189                             vLLM 8000(Qwen3.8-27B 4비트)
                       Qwen Image 2.1 UC fp8 · 터보 LoRA · 그림체 LoRA          대화 모델이자 그림 검수(VLM)
```
- **서버를 바꿔 끼우는 자리**: 그림 도구는 `state\comfy_url.txt`(ComfyUI 주소)만, 글·검수 도구와 미사일 스튜디오는 `state\vlm_url.txt`(`주소|모델|키`)만 본다. 서버를 켜는 도구가 이 두 파일을 다시 쓴다. vast 대신 RunPod(`tools\runpod_cloud.py`)여도 같다.
- **LLM 을 바꿔 끼우기**: LLM 상자에서 `tools\vast_cloud.py --profile vast_llm llm --model <이름>`(후보는 `cloud\runpod_qwen.json` 의 `llm_options`, 되돌리기 `default`). 미사일 스튜디오에서 다른 공급자(DashScope 등)를 골라도 도구는 그대로 돈다.
- **GPU 가 필요 없는 도구**: 읽기(`find`·`list_panels`·`panel_info`·`contact_sheet`…)와 합성·내보내기(`compose`·`export`)는 이 PC 에서 돈다. 그리기·편집·학습만 그림 상자가 필요하다.

## 폴더 지도
```
CLAUDE.md                 세션 첫 장(읽는 순서·지킬 것)
README.md                 이 파일
LESSONS.md                교훈집(그리기 전에 반드시)
series\                   작품 폴더. 작업 PC 에만(깃에 안 올림, series\README.md)
mcp\                      작업실 MCP 서버(도구 45개, mcp\README.md)·에이전트 프롬프트
vast\                     ComfyUI 로 그리기·고치기·학습하는 코드와 서버 워크플로우(mcp_use_webtoon_*.json, vast\README.md)
cloud\                    GPU 서버 설정: vast_img.json(그림 상자)·vast_llm.json(LLM 상자)·runpod_qwen.json(설치 내용·LLM 후보)·runpod_bootstrap.sh(상자 안 설치·워치독)
tools\                    도구(아래 표)
skills\                   이미지 스튜디오 스킬(SKILL.md 6개 + 웹툰 템플릿). 작업 노하우의 정본
tests\                    돈 0 단위시험(pytest)·미사일 스튜디오 시나리오(studio_scenarios.json)
requirements.txt          파이썬 패키지(정확한 버전)
state\                    스위치 파일(comfy_url·vlm_url)·MCP 작업 기록(mcp_jobs)·로그. 깃에 안 올림
models\                   loras\(학습한 LoRA 보관함, 서버를 켤 때 자동으로 올림)·detect\(말풍선용 얼굴 찾기)·u2net\(배경 지우기). 깃에 안 올림
exports\<날짜>\           사용자에게 넘긴 PDF·epub·예고편. 깃에 안 올림
reader\                   열람 페이지(index.html 은 작품 내용이라 깃에 안 올림, 올리는 법 reader\README.md)
worklogs\                 OPEN-ISSUES.md · 날짜별 작업 기록
fonts\                    식자 글꼴(OFL)
style\ workflows\         쉬는 옛 ComfyUI 판(style\ 은 옛 무대 뒤 그림체라 깃에 안 올림)
.venv\                    파이썬
```

## 작품 폴더 구성(`series\<작품>-hf\`, 작업 PC 에만)
```
README.md            회차 목록·정본 표시·다음 할 일
SERIES.md            설정·인물·회차 줄거리      STYLE.md   그림 규칙·결정 이력
series.json          제목·작가 표기               cast.json  인물 목록(id·한글·영어 이름·특징, 컷 도구가 읽음)
assets\              <id>_sheet.png(설정표) · 표정표 · locations\<장소>.png · vast_refs\(서버용 참고 그림, 자동)
prompts\             그림체(style*.txt)·장소·인물 프롬프트, panels\<회차>\ 컷 프롬프트(자동)
lora\loras.json      그림체·인물 LoRA 목록(파일·켜짐 on·세기·학습 기록). 가중치는 models\loras\
episodes\epNNN\      (같은 화의 다른 판은 epNNN_v2 …)
  make_storyboard.py → storyboard.json   콘티(대사·순서·말풍선 자리)
  build_panels.py    → panels.json       컷 설명(인물·옷·장소·구도·연기·장면 배치·카메라·재사용)
  panels_hf\         본편 컷(이름은 옛것. 지금은 GPU 서버로 그린 컷을 adopt_panel 로 넣은 것, 다시 그리기 전 원본은 _rejected\)
  panels_hf_vast\    GPU 서버로 새로 그린 판(본편에 넣기 전)
  panels\ parts\     합성용(가로 1600)
  out\               <ep>_full.png · slices\ · <ep>.pdf · <ep>.epub · 예고편
  review\            점검표·다시 그린 컷 비교(합성 때 다시 생김)
```

## 한 화 만드는 순서
**대화로(기본)**: 미사일 스튜디오나 agent app 에서 "<작품> N화 만들자" → 에이전트가 콘티를 쓰고 MCP `make_episode` → 결과 PDF 와 "사람 눈으로 확인할 컷"을 보고 걸린 컷만 `vast_draw`·`edit_panel`·`adopt_panel` → `compose`·`export`.

**손으로(같은 일)**:
```powershell
$py = "D:\webtoon-studio\.venv\Scripts\python.exe"; $env:PYTHONUTF8 = 1; $env:PYTHONIOENCODING = "utf-8"
# 0) GPU 서버 켜기(두 상자 동시, 약 15분). 다 쓰면 끄기
& $py -u tools\gpu_wake.py
# 1) 한 화를 끝까지(콘티 준비 → 에셋 점검 → 그리기 → 놓기·합성·점검 → 내보내기). 처음 나오는 장소·인물이면 시안을 만들고 멈춘다(종료 코드 3) → 고른 뒤 --accept-new
& $py tools\make_episode.py series\<작품>-hf <회차>
# 2) 컷 몇 개만 다시 그리기(본편에 바로 넣지 않고 panels_hf_vast\ 로). 연출 확인(--check)이 걸리면 다시 그리기·방향 편집
& $py vast\comfy_panel.py series\<작품>-hf series\<작품>-hf\episodes\<회차>\panels.json --episode <회차> --only p003,p008 --variant vast --turbo --expr --fix --check
# 3) 합성·점검(GPU 없음) → 예고편·epub
& $py tools\run_episode.py series\<작품>-hf\episodes\<회차>; & $py tools\qa_gate.py <같은 폴더>; & $py tools\check_bubbles.py <같은 폴더>
& $py tools\trailer2.py <같은 폴더>; & $py tools\to_epub.py <같은 폴더>
# 4) 끄기(놀면 15분 뒤 스스로 지워지지만 끝나면 바로)
& $py tools\vast_cloud.py --profile vast_img down; & $py tools\vast_cloud.py --profile vast_llm down
```

## GPU 서버(vast 개인 계정, 두 상자)
- **켜기**: `tools\gpu_wake.py`(미사일 스튜디오의 켜기 버튼도 이것). `state\gpu_cloud.json` 의 설정 둘(`vast_llm`·`vast_img`)을 함께 깨운다. 시세·CUDA·신뢰도·나라·내려받기 요금으로 기계를 고르고, 상자 안 설치는 `cloud\runpod_bootstrap.sh`. 그림 상자 약 13~15분, LLM 상자 약 15분(모델 받기 포함).
- **끄기**: 상자 안 워치독이 15분 동안 일이 없으면(ComfyUI 큐·최근 그림·vLLM 요청 모두 없음) 스스로 지운다, 켠 지 3시간이면 무조건. 이 PC 의 예약작업 `WEBTOON-budget-guard` 가 10분마다 잔액을 보고 바닥(`min_balance_usd`) 아래면 지운다. 손으로는 `vast_cloud.py --profile <설정> down`. 멈춰 두지 않으므로 보관비는 0(매번 새로 설치).
- **LoRA**: 학습한 LoRA 는 서버에서 `models\loras\` 로 내려받고(`loras pull`, 학습 작업 끝에 자동), 서버를 켤 때 없는 것을 올린다(`loras push`).
- **회사 공용 vast 인스턴스는 쓰지 않고 건드리지 않는다**. 키는 `D:\.claude\CREDENTIALS.local.md` 의 개인 vast 절, SSH 키 `~\.ssh\vast_webtoon_ed25519`.

## 미사일 스튜디오·agent app 에서 쓰기
- **미사일 스튜디오**(이 PC 7700): 공급자 `runpod-qwen` 이 `state\vlm_url.txt` 를 읽어 LLM 상자의 모델을 쓰고, MCP 서버 `webtoon-studio` 의 켜기 명령이 `tools\gpu_wake.py` 다. 상자가 꺼져 있으면 다른 공급자로 바꿔 대화한다.
- **agent app**: 작업실 PC 의 agent app 는 `http://127.0.0.1:7440/mcp`, 다른 PC 의 agent app 는 `https://webtoon.js-96.com/mcp` 에 헤더 `CF-Access-Client-Id`·`CF-Access-Client-Secret`(값은 작업실 PC 의 비밀값 파일)로 등록한다. agent app 의 MCP 목록은 PC 마다 따로라 대화를 시작하는 PC 마다 등록해야 한다. 에이전트 설정·시스템 프롬프트(`mcp\AGENT_PROMPT.md`)는 `mcp\README.md`.
- **작업실 PC**: 예약작업 `WEBTOON-MCP-http`(부팅 때 자동) → `mcp\serve_http.cmd` → `server.py --http --port 7440`. `server.py` 를 고치면 이 예약작업을 다시 띄운다. 결과 PDF·컷은 `https://webtoon.js-96.com/files/…` 링크(소유자 이메일 인증).

## 도구
| 지금 쓰는 도구 | 하는 일 |
|---|---|
| `vast\comfy_panel.py` | 컷 그리기: panels.json 의 컷 설명 + 참고 그림(설정표·표정 칸·장소) + 그림체 LoRA 로 프롬프트를 만들어 ComfyUI 워크플로우에 넣는다. `--check` 면 LLM 상자의 VLM 이 연출을 보고 걸리면 다시 그리기·방향 편집 |
| `vast\panel_edit.py` · `vast\qa_fix.py` | 그린 컷 한 장 고치기(표정·소품·남는 사람 지우기) |
| `vast\lora.py` · `vast\restyle.py` | 그림체·인물 LoRA 학습(서버의 WebtoonTrainLora 노드)·등록·견주기 |
| `tools\vast_cloud.py` · `gpu_wake.py` · `gpu_loras.py` | GPU 서버 켜기·끄기·상태·지킴이·LLM 바꿔 끼우기·LoRA 올리고 내리기 |
| `tools\make_episode.py` | 한 화를 끝까지 한 번에(작품을 가리지 않음) |
| `tools\run_episode.py` · `compose.py` · `lettering.py` | 합성 재료 컷 → 세로 합성·말풍선(말하는 사람 쪽·얼굴 피함) → PDF → 점검표 |
| `tools\hf_place.py` | 본편 컷(panels_hf\) → 합성용(panels\, 가로 1600). 이름만 옛것 |
| `tools\qa_gate.py` · `check_bubbles.py` · `char_check.py` · `face_detect.py` · `continuity_check.py` | 넘기기 전 기계 점검(자막 문장·말풍선 얼굴 가림·인물 같은지·이어짐) |
| `tools\phone_ui.py` · `splash.py` · `sepia_flashback.py` · `tv_frame.py` · `ecg_graphic.py` · `strip_panel.py` | 합성 재료 컷(휴대폰 화면·표지·회상·TV 화면 등) |
| `tools\to_pdf.py` · `to_epub.py` · `trailer2.py` | PDF · epub · 세로 예고편 |
| `tools\new_series.py` · `storyboard_from_spec.py` · `panels_from_spec.py` | 새 작품·콘티·컷 설명 틀 |
| `tools\ailism_publish.py` | Ailism Original 에 게시(MCP `ailism_works`·`ailism_work`·`publish_ailism`, 키는 CREDENTIALS 의 Ailism 절) |
| `tools\studio_test.py` · `mcp_smoke.py` · `hardcode_scan.py` · `prompt_dryrun.py` | 시험(아래) |

| 쉬는 도구 | |
|---|---|
| `tools\hf_panel.py` · `hf_gen.py` · `hf_patch.py` · `hf_assets.py` | 힉스필드 그리기(쓰지 않음). 단 `hf_panel.py` 의 `names()` 는 vast 도구들이 읽으니 지우지 않는다 |
| `tools\runpod_cloud.py` · `runpod_wake.py` | RunPod 파드(잔액이 RunPod 에 남아 있어 다시 쓸 수 있음) |
| `tools\comfy.py` · `render.py` · `train_lora.py` | 옛 ComfyUI 판. `comfy.py` 는 공용 함수(ROOT·load_json)가 있어 지우지 않는다 |

## 시험
- **돈 0**: `.venv\Scripts\python -m pytest tests`(단위시험) · `tools\mcp_smoke.py`(MCP 읽기 도구 전수·그리기 지시 점검·하드코딩 검사, GPU·대화 모델 안 씀).
- **미사일 스튜디오 시나리오**: `tools\studio_test.py [시나리오 …] [--provider <공급자>] [--series <작품 폴더,…>] [--gpu]`. 실제 미사일 스튜디오에 말을 걸어 도구 호출·답·숫자(컷 수·인물 컷 수·첫 등장)를 작품 데이터와 대조한다. 이름이 `작품별_` 로 시작하는 시나리오는 작품마다 펼쳐지고, 시험이 만든 사본·결과물은 `series\_tmp\studio_test_<시각>\` 로 옮겨 둔다. `--gpu` 시나리오는 GPU 서버 시간(돈)이 든다. `--list` 는 돌리지 않고 펼친 말만 보인다.
- **아직 없는 것**: 그린 그림이 원작 그림체와 닮았는지 재는 점수(지금 시험은 도구 호출·숫자·정직성만 잰다, `worklogs\OPEN-ISSUES.md`).

## 다른 PC 에서 작업실을 새로 차리기(레포 `github.com/jinsoo96/webtoon-studio`, 비공개)
다른 PC 에서 **작업만** 할 거면 위 "agent app 에서 쓰기"(주소 등록)다. 이 절은 작업실 자체를 옮기거나 하나 더 차릴 때.
1. 받기: `git clone https://github.com/jinsoo96/webtoon-studio.git`
2. 파이썬: `uv venv .venv --python 3.12` → `uv pip install --python .venv\Scripts\python.exe -r requirements.txt`
3. 작품 폴더 `series\` 는 레포에 없다. 작업 PC 에서 따로 옮겨 온다(LoRA 가중치 `models\loras\`, 얼굴 찾기·배경 지우기 모델 `models\detect`·`models\u2net` 도 함께).
4. GPU 서버: 개인 vast 키(CREDENTIALS)와 SSH 키 `~\.ssh\vast_webtoon_ed25519` 를 두고 `tools\vast_cloud.py --profile vast_img status` 로 확인.
