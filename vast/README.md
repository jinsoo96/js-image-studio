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
| `comfy_panel.py` | 컷 그리기: `panels.json` → `episodes\<회차>\panels_hf_vast\`(힉스필드 `panels_hf\` 는 안 건드림) |
| `make_workflow.py` | 서버 화면에서 손으로 돌리는 세로 워크플로우를 만들어 저장 |
| `webtoon_vertical_qwen21.json` | 서버에 저장한 그 워크플로우의 사본 |

## 쓰는 법
```
.venv\Scripts\python vast\comfy_client.py info                     # GPU·남은 VRAM·노드 수
.venv\Scripts\python vast\sheet_refs.py series\d-plus-18-hf          # 시트가 새로 생기거나 바뀌면 한 번
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
- 옷 시트를 못 쓴다 → 옷은 글(`outfit`)로만. 아직 한 화를 통째로 그려 합성까지 해 본 적은 없다.
