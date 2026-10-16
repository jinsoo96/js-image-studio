# 작업실 MCP 서버(agent app·Qwen 에서 쓰기)

`server.py` 는 작업실 기능을 MCP 도구 24개로 묶은 stdio 서버다. agent app(agent platform 데스크톱) 설정의 로컬 MCP 로 붙이면, agent platform 서버의 에이전트(사내 vLLM Qwen 등)가 이 PC 의 작업실을 다룬다(로컬 MCP → agent app → agent platform).

## 도구
| 도구 | 하는 일 | 크레딧 |
|---|---|---|
| `guide` · `read_doc` | 작업실 규칙(CLAUDE.md)·문서 읽기(긴 문서는 offset) | 0 |
| `status` · `credits` | 작품·회차·작화·합성 상태, 진행 중 작업, 남은 크레딧 | 0 |
| `list_panels` · `contact_sheet` | 컷 목록 · 검수용 모아보기 그림(경로를 돌려줌, `state\mcp_sheets\`) | 0 |
| `review_panels` | 그림 검수: 그림을 보는 모델(`qwen3.8-27b`, agent platform 공인망 게이트웨이)이 컷 그림 + 컷 설명을 맞대어 인원·좌우·시선·옷·표정·배경 판정과 고칠 점. 8컷 이하는 바로, 더 많으면 작업(`state\mcp_reviews\`). 본체 `vision_review.py`(명령줄로도 돈다) | 0 |
| `list_files` · `read_file` · `write_file` | 작업실 안 파일 보기·읽기·쓰기. 쓰기는 작품 폴더의 글 파일(.py .json .md .txt)·worklogs·LESSONS·reader 만, 도구·그림·에셋·.git 금지. 덮어쓰기 전 원본은 `state\mcp_backups\`, 파이썬·JSON 은 문법 검사, em dash 거절 | 0 |
| `new_episode` | 새 회차 폴더를 앞 회차 틀(make_storyboard·build_panels·trailer)로 연다. 있는 회차는 건드리지 않음 | 0 |
| `prepare_episode` | make_storyboard.py → build_panels.py | 0 |
| `estimate_draw` | 그릴 컷과 예상 크레딧 | 0 |
| `draw_panels` · `redraw_panels` | 힉스필드 작화(작업). `confirm_credits` 가 예상 비용 이상이어야 돈다, 한 번에 40컷. 다시 그리기는 원본을 `_rejected` 로 | 장당 1.25 |
| `compose` · `export` | 합성·PDF·기계 점검 / 예고편·epub·exports 복사(작업) | 0 |
| `job_status` | 작업 상태와 로그 끝(작업 기록은 `state\mcp_jobs\`) | 0 |

원칙: 한 번 호출은 30초 안(agent platform 브리지 제한), 오래 걸리는 일은 작업으로 띄우고 job_status. 지우는 도구는 없다. 자식 프로세스는 stdin 을 막는다(MCP 전송로를 물려받으면 멈춘다).

## agent app 에 붙이기
1. 파이썬 준비: 작업실 `.venv` 에 `requirements.txt`(정확한 버전) 설치. `uv pip install --python .venv\Scripts\python.exe -r requirements.txt`
2. agent app 를 끄고 `%APPDATA%\agent-connector\connector.json` 에 다음을 넣는다(agent app 설정 > MCP 화면에서 넣어도 같다).
   ```json
   "mcp": true,
   "mcpServers": [{"name": "webtoon-studio", "transport": "stdio",
     "command": "D:/webtoon-studio/.venv/Scripts/python.exe", "args": ["D:/webtoon-studio/mcp/server.py"],
     "env": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}, "enabled": true}]
   ```
3. agent app 를 켜고 설정 > MCP 에서 webtoon-studio 가 연결(도구 24개)인지 본다. server.py 를 고친 뒤엔 agent app 를 다시 켜야 새 도구가 보인다.
4. 새 채팅 → 새 에이전트: 이름 "웹툰 작업실 (Qwen)", AI 제공사 vLLM(사내), 모델 `qwen3.8-27b`, 세부설정의 시스템 프롬프트에 `AGENT_PROMPT.md` 내용. 도구 노출은 기본(hierarchy)으로도 도구를 잘 찾았다.
   지금 있는 에이전트: agent-server 계정 (redacted)(사용자 번호 49 "김진수", 관리자 권한이 붙어 있을 뿐 본인 계정), 워크플로우 `wf_1792023050405_k5kq9b2`. 프롬프트를 바꾸면 `D:\dex-gaps\tools\xgen_agent_prompt.py set <id> AGENT_PROMPT.md` 로 다시 저장한다.

2026-10-15 시험(agent app 1.91.0, agent-server, qwen3.8-27b): "상태 알려줘" → status 로 4편·크레딧 정확히 답함. "p079 다시 그려줘 + 모아보기" → guide·estimate_draw·contact_sheet 를 부르고 예상 1.25 크레딧을 보여 준 뒤 확인을 기다림(크레딧 안 씀). "2화 v2 콘티 열어 호텔 장면 정리 + 열린 일" → read_file 로 p073~p079 컷·대사 표와 열린 일 3개를 정확히 답함.

## 알아 둘 것
- agent app 의 다중 파드 서버에서는 로컬 MCP 도구가 90초 쉬면 안 보일 수 있다. 그러면 agent app 설정 > MCP 에서 껐다 켠다(메모리 `dex-local-mcp-tools-vanish`).
- Qwen 은 처음에 인자를 빠뜨리고 부르기도 한다. 도구가 오류 문장을 돌려주면 대개 스스로 고쳐 다시 부른다.
- 작업실 파일을 직접 고치는 일(새 회차 make_storyboard.py 쓰기 등)은 agent app 채팅의 "폴더"로 `D:\webtoon-studio` 를 연결하면 에이전트가 파일·셸 도구로 할 수 있다.

## 그림 검수(review_panels) 알아 둘 것
- Qwen 에이전트는 그림을 못 보지만, 같은 qwen3.8-27b 를 그림 입력으로 부르면 그림을 본다(2026-10-15 확인: 호텔 컷의 인원·좌우·시선·옷·표정을 정확히 읽음). 그래서 검수를 도구 안에서 따로 부른다.
- 시험(D+18 2화 v2): 호텔 6컷 5.5초(p079 "재민 시선이 정면" 지적), 식당 21컷 작업 18초(다시 후보 3컷 중 1컷은 타당, 2컷은 컷 설명이 덜 적혀 생긴 과민 판정). 판정은 참고용이고 사용자가 고른다.
- 한 번만 보면 판정이 흔들렸다(같은 호텔 컷을 처음엔 p079, 다음엔 p078 이 걸렸고 p078 은 없는 목걸이를 봤다). 그래서 컷마다 두 번 따로 보고 둘 다 걸린 항목만 '다시', 한 번만 걸린 건 '애매'(사람 눈으로 확인)로 낸다. 그림은 1280px, temperature 0. 그래도 서버가 완전히 같은 답을 주진 않으니 최종 판단은 사람이 한다.
- 컷 설명(build_panels.py 의 P·CAM·ACT)을 자세히 쓸수록 판정이 정확해진다. 장면 공통 옷과 이 컷 행동이 다르면 행동을 따르게 했다.
- 그림은 회사 agent platform 게이트웨이로 간다(외부 서비스 아님). 바꾸려면 환경변수 VLM_URL·VLM_MODEL.

## vast 도구(크레딧 0, 회사 공용 GPU, 2026-10-15)
| 도구 | 하는 일 |
|---|---|
| `vast_status` | 인스턴스·남은 VRAM·서버 큐·Qwen 모델이 있는지 |
| `vast_draw` | 같은 panels.json 을 vast ComfyUI(Qwen Image 2.1)로 그림 → `panels_hf_vast\`. turbo·expr 기본, aspect 로 세로, seed 로 다시. 한 번에 80컷까지 |
| `vast_make_assets` | 시트에서 참고 그림 자르기 + 베이스 몸·센 표정 12가지 |
| `make_episode` | **한 화를 한 번에**(어느 작품이든): 콘티 준비 → 에셋 점검(새 장소·인물은 시안 후 멈춤, `accept_new`) → vast 그리기 → 합성·점검 → 내보내기. 본편 그림이 있는 컷은 안 건드림 |
| `vast_episode` | 본편은 그대로 두고 `<회차>_vast` 폴더를 만들어 vast 컷으로 그리기 → 말풍선 합성 → PDF → 점검까지 한 번에. 내보내기는 `export(작품, "<회차>_vast")`(이름 끝 _vast판) |
| `vast_adopt` | 사용자가 고른 vast 컷을 본편(panels_hf)으로, 원본은 `_rejected\*_before_vast.png` |

`contact_sheet`·`review_panels` 에 `folder="vast"` 를 주면 vast 그림을 본다. 인스턴스를 켜고 끄는 도구는 두지 않는다. 에이전트 프롬프트(`AGENT_PROMPT.md`) v1.4 에 vast 쓰는 때와 순서를 넣었다("vast 로", "크레딧 없이" 라고 하면 vast_draw).

## agent-server 에 저장되는 것(2026-10-15 확인)
- 에이전트 "웹툰 작업실 (Qwen)" 자체가 agent-server 워크플로우다(`wf_1792023050405_k5kq9b2`, Agent Geny 노드 하나 + 시스템 프롬프트). 웹·agent app 어디서 열어도 같은 에이전트.
- 작업실 도구는 워크플로우 안에 저장되지 않는다. 대화할 때 이 PC 의 agent app 가 로컬 MCP 로 붙여 준다. 그래서 그림·합성은 이 PC 에서 agent app 가 켜져 있을 때만 된다.
- 캔버스의 "MCP Tool Loader" 노드는 서버에 떠 있는 MCP 세션을 고르는 노드인데 agent-server 세션 목록(`/api/mcp/sessions`)이 비어 있고, 팀 방향도 서버 호스팅 MCP 를 없애고 로컬 MCP → agent app → agent platform 이다. 로컬 도구를 노드로 박은 고정 파이프라인은 지금은 못 만든다.
- 나눌 수는 있다: "Workflow Tool" 노드로 다른 저장 워크플로우(예: 콘티 에이전트, 작화·검수 에이전트)를 도구로 부르는 관리자-작업자 구성.
