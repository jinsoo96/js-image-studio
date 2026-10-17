# vast\ : vast.ai ComfyUI 에서 그리기 (Qwen Image 2.1)

회사 공용 GPU(vast.ai RTX 5090 32GB, ComfyUI 0.37)에서 힉스필드와 **같은 컷 설명(`panels.json`)** 으로 컷을 그린다. 크레딧은 들지 않고 GPU 시간만 쓴다.
2026-10-15 사용자 "vastai 에서 이 정도 이미지 수준의 웹툰 만들게 워크플로우 세로로"에서 시작.

## 지킬 것
- **회사 공용 인스턴스다.** 조회·그리기·모델 받기만 한다. 인스턴스 만들기·끄기·지우기, 결제 설정, 남의 워크플로우 수정은 하지 않는다. 기준선 `image_anima_base_v1.json` 은 건드리지 않는다.
- 접속 정보는 `D:\.claude\CREDENTIALS.local.md` 의 vast.ai 절에 있다. API 키는 계정 전체 권한이라 조회에만 쓴다. 키·토큰은 출력·커밋하지 않는다. 도우미가 알아서 읽는다(환경변수 `VAST_KEY` 가 있으면 그걸 먼저).
- 모델은 서버의 Model Manager Neo(ComfyUI 확장) 또는 HuggingFace 로만 받는다(`comfy_client.py fetch`). 다른 경로는 받기가 잘 안 된다(관리자 안내).
- 서버 큐는 다른 사람과 같이 쓴다. 중단(`/interrupt`)은 지금 도는 작업이 내 것(client_id `webtoon-studio`)일 때만 한다.

## 파일
| 파일 | 하는 일 |
|---|---|
| `comfy_client.py` | 접속(주소·토큰을 vast API 로 매번 읽음, 인스턴스를 다시 만들어도 됨) · 모델·워크플로우 목록 · 모델 받기 · 큐에 넣고 결과 받기 |
| `sheet_refs.py` | 캐릭터 시트에서 Qwen 용 참고 그림 자르기 → `series\<작품>\assets\vast_refs\`(`<id>_bust.png` 무표정 머리가 기본), 표정 시트 12칸 `<id>_expr_01..12.png` + 칸 이름 `<id>_expr.json`(`--expr` 용) |
| `new_assets.py` | 새 장소·새 인물 시트 시안(`assets\_candidates\`, 고른 뒤 `--accept`). 장소는 남는 사람 지우기까지. 프롬프트는 작품의 `prompts\loc_*`·`sheet_*` |
| `make_assets.py` | 인물 에셋 늘리기(Qwen 편집. 큐에 한꺼번에 넣고 768 이면 장당 약 3.5초, 한 장씩 1024 면 12초): 회색 기본복 베이스 몸 `<id>_base.png`, 시트에 없는 센 표정 12가지 `<id>_xp_01..12.png`(오열·이 악물고 울기·고함·경멸·경악·씁쓸한 웃음 등, `<id>_expr.json` 의 extra). 맨몸은 만들지 않는다 |
| `comfy_panel.py` | 컷 그리기: `panels.json` → `episodes\<회차>\panels_hf_vast\`(힉스필드 `panels_hf\` 는 안 건드림) |
| `saved_workflow.py` | 서버 저장 워크플로우(생성·편집)를 불러 API 형식으로 바꾸고 입력값만 채움. 그리는 스크립트가 모두 이걸 거친다(`comfy_panel.graph`) |
| `make_workflow.py` | 서버 화면에서 손으로 돌리는 세로 워크플로우를 만들어 저장 |
| `webtoon_vertical_qwen21.json` | 서버에 저장한 그 워크플로우의 사본 |
| `refine.py` | 2단 다듬기: 그림 보는 사내 Qwen 이 컷 설명에 없는 사람을 옷·자리로 짚고, Qwen 편집으로 그 사람만 지운다(`--fix`, 원본은 `_stage1\`). 긴 다듬기 지시 실험도 이 파일 |
| `webtoon_panel_2stage_qwen21.json` | 서버에 저장한 2단 워크플로우(1단 그리기 → 2단 편집 다듬기) 사본 |

## 쓰는 법
```
.venv\Scripts\python vast\comfy_client.py info                     # GPU·남은 VRAM·노드 수
.venv\Scripts\python vast\sheet_refs.py series\d-plus-18-hf          # 시트가 새로 생기거나 바뀌면 한 번
.venv\Scripts\python vast\make_assets.py series\d-plus-18-hf woobin both --res 768   # 새 인물이면: 베이스 몸 + 센 표정 12가지(--expr 가 같이 고른다)
.venv\Scripts\python vast\make_assets.py series\mudae-dwi-hf all both --res 768      # 작품 전체. 이미 있는 건 건너뛴다(끊겨도 다시 돌리면 이어짐)
.venv\Scripts\python vast\comfy_panel.py series\d-plus-18-hf series\d-plus-18-hf\episodes\ep002_v2\panels.json --episode ep002_v2 --only p003,p045 --turbo
.venv\Scripts\python vast\comfy_panel.py ... --aspect 2:3 --variant vast_v   # 세로 컷(9:16·2:3·3:4)
.venv\Scripts\python vast\comfy_panel.py ... --seed 7                        # 사람이 하나 더 나오거나 잔상이 끼면 시드만 바꿔 다시
.venv\Scripts\python vast\comfy_panel.py ... --expr                          # 연기에 맞는 표정 칸을 얼굴 참고로(연기가 세짐)
```
본편에 쓰려면 검수 뒤 그 컷을 `panels_hf\` 로 옮기고(원본은 `_rejected\`) `tools\hf_place.py` 로 `panels\` 에 놓는다.

## 서버 화면에서 손으로
포털(Open 버튼) → ComfyUI → 워크플로우 목록의 `webtoon_vertical_qwen21` → 왼쪽 참고 그림(넣은 순서가 `<image1>`, `<image2>` …)을 바꾸고 컷 설명을 고쳐 실행. 사용법은 워크플로우 안 노트에. 결과는 서버 `output\webtoon_vertical\`.
- 기본은 세로 2:3(1184x1760), 40스텝, cfg 1, euler/simple. "터보 LoRA" 노드를 켜고(Ctrl+B) steps 8 이면 시안.
- 다시 만들기: `make_workflow.py --prompt-file <컷.txt> --ref <그림> ... --out vast\webtoon_vertical_qwen21.json --save --force`

저장 워크플로우 한 벌(스크립트가 큐에 넣는 그래프와 같은 구성을 화면에서 열어 고칠 수 있게 펼친 것, 예시 그림은 D+18 우빈):
| 종류 | 워크플로우 | 같은 일을 하는 스크립트 |
|---|---|---|
| 생성 | `webtoon_vertical_qwen21` · `webtoon_panel_2stage_qwen21`(그리기 → 편집 다듬기) | `comfy_panel.py` |
| 편집 | `webtoon_edit_qwen21`(남는 사람 지우기 · 연기 · 옷) | `refine.py`(--fix · --check) |
| 에셋 | `webtoon_asset_expression_qwen21`(표정 은행) · `webtoon_asset_base_qwen21`(기본복) | `make_assets.py` |
| 시안 | `webtoon_candidate_location_qwen21`(장소, 2단 사람 지우기) · `webtoon_candidate_sheet_qwen21`(인물 시트) | `new_assets.py` |

편집·에셋·시안 다섯 개는 `make_workflow.py library [--force]` 가 한꺼번에 만들어 저장한다(사본도 `vast\` 에). 프롬프트는 그 스크립트의 것(make_assets 의 STRONG·BASE, new_assets 의 candidate_prompt)을 그대로 가져온다.

**도구는 저장 워크플로우를 불러 쓴다**(2026-10-17, `saved_workflow.py`). 그리는 스크립트(comfy_panel · refine · make_assets · new_assets, 그래서 agent app MCP 도구도)는 그릴 때마다 서버의 `webtoon_vertical_qwen21`(생성)·`webtoon_edit_qwen21`(편집)을 불러 API 형식으로 바꾸고 프롬프트·참고 그림·크기·시드·스텝·터보만 채운다. 에셋·시안 워크플로우는 이 둘과 같은 구성에 그 일의 기본 프롬프트를 넣은 손 작업용이다.
- 화면에서 두 워크플로우를 고치면(모델·LoRA·샘플러·cfg·네거티브, 노드 추가) 다음 그리기부터 작업실·agent app 가 따른다. agent platform 카탈로그는 등록할 때 사본을 떠 가므로, 워크플로우를 고친 뒤나 vast 인스턴스를 바꾼 뒤 `xgen_catalog.py sync` 를 한 번 돌린다. 워크플로우·참고 그림·vast 주소가 바뀐 것만 알아채 다시 올리고, 바뀐 게 없으면 agent platform 에 로그인하지 않는다. 로그인이 한 번 실패하면 `state\xgen_catalog_sync.json` 에 멈춤 표시를 남긴다(지우면 다시 돈다). 10분 예약 작업은 사용자가 원치 않아 지웠다(2026-10-18).
- 화면에서 고친 뒤 `make_workflow.py ... --save --force`·`library --force` 를 돌리면 고친 것을 덮는다.
- 서버에서 못 불러오면 같은 구성을 코드로 짜서 그리고 경고를 한 번 찍는다(`comfy_panel._graph_code`).
- 확인: 같은 시드로 저장 워크플로우 판과 코드 판을 그려 생성·편집 모두 픽셀이 같았다.

## 2단 다듬기(2026-10-16)
1단만으로는 남는 사람(같은 인물 둘, 낯선 사람, 뒤쪽 작은 사람)이 남는 컷이 있었다. `--fix`(MCP `vast_draw`·`vast_episode` 는 기본으로 켬)면 그린 직후:
1. 그림을 보는 사내 Qwen(qwen3.8-27b)에게 "컷 설명에 없는 사람"을 옷·자리로 한 줄씩 묻는다(없으면 끝, 멀쩡한 컷은 GPU 를 안 씀)
2. 그 문장으로 짧은 지우기 지시를 만들어 Qwen 편집(1단 그림 = `<image1>`)으로 그 사람만 지운다(약 11~20초)
3. 한 번 더 세어 보고 남았으면 한 번 더(최대 2번). 1단 원본은 `_stage1\`
결과: 무대 뒤 숙소 소파 컷 8명 넘게 → 3명, D+18 3인 식당 4명 → 3명. 지시를 길게(인원·연기·얼굴을 한꺼번에) 주면 같은 인물 둘 중 하나를 못 지웠다. 짧게 콕 집어야 지운다.
서버 화면용 `webtoon_panel_2stage_qwen21` 은 2단 지시를 손으로 쓴다. **1단 그림을 보고 적는다**: 미리 적은 문장(옷 배치)이 1단 그림과 다르면 진짜 인물까지 지운다(시험에서 검은 티 멤버가 같이 지워짐).
남은 과제: 지우고 남은 인물의 얼굴·머리색이 설정과 다를 때(p003 우빈 머리가 검정) 고치는 단계.

## 3단 연출 확인(2026-10-16 사용자 "시점·표정·공간 자각·카메라 각도를 잘 고려해야")
`--check`(MCP 는 기본으로 켬)면 2단 뒤에 그림 검수(`mcp/vision_review`, 인원·좌우시선·옷·표정연기·배경·기타, 두 번 보고 둘 다 걸린 것만)로 본다.
- 표정·옷만 틀리면 Qwen 편집으로 그 부분만 고친다(검수가 준 고칠 점도 같이). 고친 뒤 다시 보고 더 나빠졌으면 되돌린다
- 좌우·시선·배경·인원이 틀리면 구도 문제라 시드를 바꿔 다시 그린다(`--retries`, 기본 1번). 가장 나은 판을 남기고, 남은 문제는 "사람 눈으로 확인"으로 찍는다
시험(D+18 2화 v2, 무작위 시드 5컷): 4컷 바로 통과, p008 은 "와인잔을 살짝 든 자세가 아님"이 걸려 편집 뒤 통과. 컷당 20~60초 더 든다.

## 스크립트로 그린 컷을 화면에서 보기
`comfy_panel.py`·MCP `vast_draw` 는 화면을 거치지 않고 같은 구성의 그래프를 ComfyUI API(`/prompt`)로 보낸다(화면에서 Queue 를 누르는 것과 같음). 그래서 캔버스에는 안 보이고 서버 기록(대기열·기록 창, client_id `webtoon-studio`)과 서버 `output\webtoon_studio\` 에 남는다.
2026-10-16 부터는 컷마다 그 컷의 화면용 워크플로우(프롬프트·참고 그림·시드·터보 여부 그대로)를 같이 실어 보낸다. 결과 PNG(서버 output 이나 이 PC 의 `panels_hf_vast\`)를 ComfyUI 화면에 끌어다 놓으면 그 컷을 그린 워크플로우가 캔버스로 열린다. 그 전에 그린 그림에는 API 그래프만 들어 있다.

## 모델(서버 `models\`, HuggingFace `Comfy-Org/Qwen-Image-2.1`)
| 폴더 | 파일 | 크기 |
|---|---|---|
| diffusion_models | `qwen_image_2.1_bf16.safetensors` | 14.2GB |
| text_encoders | `qwen3vl_8b_int8_convrot.safetensors` | 9.4GB |
| vae | `qwen_image_2.1_vae_bf16.safetensors` | 0.7GB |
| loras | `qwen_image_2.1_turbo_lora_avg_rank_178_bf16.safetensors`(8스텝) | 0.9GB |

DiT bf16 + 글 인코더 int8 이면 둘 다 32GB VRAM 에 같이 올라간다. 라이선스는 qwen-research(모델 카드). 개인 비공개 작업 밖으로 쓰려면 라이선스부터 확인한다.

## 속도(RTX 5090, 2026-10-15 실측)
- 40스텝: 컷당 90~160초(참고 그림 2~4장). 참고 그림 10장이면 400초가 넘는다.
- 터보 8스텝: 27~50초. 이번 시험에선 40스텝과 눈에 띄는 차이가 없었다 → 시안·대량은 터보.
- 서버를 켠 뒤 첫 컷은 모델을 올리느라 1~2분 더 걸린다.

## Qwen 에 맞춘 것(자세히는 `LESSONS.md` 16, `comfy_panel.qwen_refs` 설명)
- 프롬프트는 짧게: 컷 → 연기 → 동작·공간 → 옷 → 카메라 → 인원 → 참고 그림 → 그림체 → 규칙 한 줄. 힉스필드 프롬프트(4천 자)를 그대로 쓰면 연기가 묻힌다.
- 인물 참고는 시트 정면 전신의 머리만. 시트·옷 시트·표정 시트를 통째로 넣으면 그 배치를 베낀다. 앞 컷(`refs_extra`)도 넣지 않는다.
- 장소는 넓은 컷이면 맨 앞, 클로즈업이면 맨 뒤(배경만). 클로즈업에선 장면 배치 문장(SPACE)을 뺀다.

## 아직 힉스필드보다 약한 것
- 연기 강도: `--expr` 로 표정 칸을 넣으면 세진다(사내 Qwen 이 연기 글에 맞는 칸을 고름, 표정 시트가 있는 인물만: D+18 3명·무대 뒤 5명). 그래도 힉스필드보다는 약하다. 컷 설명의 `acting` 을 보이는 사실로 쓰면 더 낫다("눈물이 두 뺨을 타고 흐름, 아랫입술을 깨묾"). 칸 고르기가 가끔 빗나간다(가볍게 웃는 컷에 차가운 시선).
- 시드에 따라 인물이 하나 더 나오거나 머리 잔상이 낀다 → 그린 뒤 인원을 세고 시드만 바꿔 다시.
- 비슷하게 생긴 여자 멤버 여럿이 한 컷에 나오면 1단에서 사람이 늘어난다(무대 뒤 p063 멤버 3인 소파가 시드를 바꿔도 5~7명). `--fix` 2단 다듬기로 3명이 됐다. 남녀·나이가 뚜렷이 다른 3인(무대 뒤 p091 카페, D+18 p003)은 맞게 나왔다.
- `--expr` 표정 칸엔 시트 기본 옷이 같이 들어 있어 옷이 따라올 수 있다(p091 태식이 흰 티 대신 후드티, review_panels 가 잡음).
- agent app 의 Qwen 에이전트도 쓸 수 있다: MCP 도구 `vast_status`·`vast_draw`·`vast_make_assets`·`vast_adopt`·`vast_episode`(`mcp\README.md`). 한 화를 PDF 까지는 `vast_episode` 하나로(본편은 그대로, `<회차>_vast` 폴더).
- 옷 시트를 못 쓴다 → 옷은 글(`outfit`)로만.
- 한 화 통째: D+18 2화 v2 를 `ep002_v2_vast` 로 그려 PDF·epub·예고편까지 냈다(2026-10-16, `exports\2026-10-16\*_vast판.*`). 새 컷 31개 중 4개가 걸려 다시 그렸다(LESSONS 16). 재사용 컷 35개는 힉스필드 그림 그대로라 한 화 안에 두 엔진 그림이 섞여 있다.

## agent platform 에서 바로 그리기(ComfyUI 노드 · 이미지 생성 카탈로그, 2026-10-16)
팀장 피드백: "이미지 스튜디오로, 내부에 웹툰 템플릿, ComfyUI + 스킬, 핵심 코어는 ComfyUI 워크플로우" · "컨티뉴얼 생성 파이프라인을 잘 만들어 두면 agent platform 에서 ComfyUI 를 연결해 만드는 것도".
agent platform 캔버스에는 ComfyUI 노드가 있고, 관리자 이미지 생성 카탈로그에 [ComfyUI 주소 + 워크플로우 + 열어 둘 입력]을 등록하면 그 노드가 에이전트 도구가 된다. 이 PC·agent app 없이 agent platform 서버가 vast ComfyUI 를 바로 부른다.
- `xgen_catalog.py assets`: 참고 그림 라이브러리를 ComfyUI input 에 올림(`studio_<작품>_<인물>.png` 22개, `studio_<작품>_loc_<장소>.png` 39개). 이름이 같으면 덮어씀
- `xgen_catalog.py validate · test <key> '<json>' · apply · list`: 카탈로그 항목 3개(`studio_webtoon_panel_1` 인물 1명, `studio_webtoon_panel_2` 인물 2명, `studio_image_free` 자유 생성). 입력 = 프롬프트 · 장소 · 인물(허용값 목록) · 크기 · 시드
- `xgen_studio_agent.py apply`: agent platform 워크플로우 「이미지 스튜디오 (Qwen)」(`wf_image_studio_qwen`) = Agent Geny + ComfyUI 노드 3개, 시스템 프롬프트에 웹툰 템플릿 스킬(이어 그리기 규칙)과 인물·장소 이름 목록
- 시험: agent app 에서 "우빈 밤거리 두 컷 이어서" → 두 컷 모두 같은 얼굴·같은 밤거리, 2분 49초, 결과는 agent platform 파일 저장소 '결과물'
- 권한: 처음에 `role_allowlist []` + 이 계정 번호로 묶었더니 실행 사용자가 노드에 안 넘어오는 경로(agent app)에서 "권한 없음"으로 막혔다 → agent-server 사용자 전체(None), 기존 test_anima 항목과 같은 설정
- 장소 이름을 에이전트가 짐작해 틀렸다("nightstreet") → 시스템 프롬프트에 정확한 이름 목록을 넣으니 맞게 골랐다
- vast 인스턴스를 다시 만들면 주소·토큰이 바뀐다 → `xgen_catalog.py assets` 와 `apply` 를 다시 돌린다
- 문서: `exports/2026-10-16/이미지스튜디오/` (파이프라인 · 시스템 구조 PDF)
