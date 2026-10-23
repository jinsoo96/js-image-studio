# 작업실 MCP 서버(agent app·Qwen 에서 쓰기)

`server.py` 는 작업실 기능을 MCP 도구 30개로 묶은 stdio 서버다. agent app(agent platform 데스크톱) 설정의 로컬 MCP 로 붙이면, agent platform 서버의 에이전트(사내 vLLM Qwen 등)가 이 PC 의 작업실을 다룬다(로컬 MCP → agent app → agent platform).

## 도구
| 도구 | 하는 일 | 크레딧 |
|---|---|---|
| `guide` · `read_doc` | 작업실 규칙(CLAUDE.md)·문서 읽기(긴 문서는 offset) | 0 |
| `status` · `credits` | 작품·회차·작화·합성 상태, 진행 중 작업, 남은 크레딧 | 0 |
| `list_panels` · `contact_sheet` | 컷 목록 · 검수용 모아보기 그림(경로를 돌려줌, `state\mcp_sheets\`) | 0 |
| `review_panels` | 그림 검수: 그림을 보는 모델(`qwen3.8-27b`, agent platform 공인망 게이트웨이)이 컷 그림 + 컷 설명을 맞대어 인원·좌우·시선·옷·표정·배경 판정과 고칠 점. 8컷 이하는 바로, 더 많으면 작업(`state\mcp_reviews\`). 본체 `vision_review.py`(명령줄로도 돈다) | 0 |
| `list_files` · `read_file` · `write_file` · `copy_file` | 작업실 안 파일 보기·읽기·쓰기. 쓰기는 작품 폴더의 글 파일(.py .json .md .txt)·worklogs·LESSONS·reader 만, 도구·그림·에셋·.git 금지. 덮어쓰기 전 원본은 `state\mcp_backups\`, 파이썬·JSON 은 문법 검사, em dash 거절. `copy_file` 은 내용을 읽지 않고 그대로 복사(긴 콘티를 옮길 때) | 0 |
| `new_episode` | 새 회차 폴더를 앞 회차 틀(make_storyboard·build_panels·trailer)로 연다. 있는 회차는 건드리지 않음 | 0 |
| `prepare_episode` | make_storyboard.py → build_panels.py | 0 |
| `compose` · `export` | 합성·PDF·기계 점검 / 예고편·epub·exports 복사(작업) | 0 |
| `job_status` | 작업 상태와 로그 끝(작업 기록은 `state\mcp_jobs\`). `wait` 초(50까지)를 주면 끝날 때까지 기다렸다 답한다(옛 job_wait 를 합침) | 0 |

원칙: 한 번 호출은 30초 안(agent platform 브리지 제한), 오래 걸리는 일은 작업으로 띄우고 job_status. 지우는 도구는 없다. 자식 프로세스는 stdin 을 막는다(MCP 전송로를 물려받으면 멈춘다).

## 어느 PC 에서든 쓰기(주소 방식, 2026-10-19~ 기본)
agent app 의 MCP 목록은 PC 마다 따로고(서버 동기화 없음), 서버는 도구 호출을 **대화를 시작한 기기**로 고정한다. 그래서 다른 PC·웹에서 시작한 대화는
이 PC 의 stdio 서버를 못 쓴다. 이 PC(24시간)를 유일한 작업실로 두고 MCP 를 주소로 연다.

```
다른 PC 의 agent app ──https + 열쇠 헤더──▶ Cloudflare Access ──터널──▶ 이 PC 127.0.0.1:7440/mcp (server.py --http) ──▶ D:\webtoon-studio · vast
```
- 서버: 예약작업 `WEBTOON-MCP-http`(부팅 때 자동, S4U, 죽으면 1분 뒤 다시) → `mcp\serve_http.cmd` → `server.py --http --port 7440`. 로그 `state\mcp_http.log`.
- **server.py 를 고친 뒤엔 예약작업을 다시 띄운다**(agent app 를 껐다 켜는 것으로는 안 바뀐다):
  `Stop-ScheduledTask WEBTOON-MCP-http; Get-CimInstance Win32_Process | ? CommandLine -match 'server.py --http' | % { Stop-Process $_.ProcessId -Force }; Start-ScheduledTask WEBTOON-MCP-http`
  그다음 agent app 설정 > MCP 에서 webtoon-studio 를 껐다 켜면 새 도구 목록이 서버에 올라간다.
- 주소 `https://webtoon.js-96.com/mcp`. Cloudflare Access 앱 `Webtoon MCP`: 정책 ① 서비스 토큰 `webtoon-mcp-dex`(agent app 헤더용, 1년) ② 소유자 이메일 OTP(브라우저로 결과 링크 볼 때).
  열쇠 값은 `D:\.claude\CREDENTIALS.local.md` Cloudflare 절 "웹툰 MCP 원격 주소" 줄에만 있다. 열쇠 없는 요청은 Cloudflare 로그인으로 돌려보낸다(302).
- 다른 PC 에서 처음 한 번: agent app 설치·김진수 로그인 → 설정 > MCP > 서버 추가: 종류 `http`, 주소 위, 헤더 `CF-Access-Client-Id`·`CF-Access-Client-Secret`.
  JSON 가져오기로 넣어도 된다: `{"mcpServers": {"webtoon-studio": {"type": "http", "url": "https://webtoon.js-96.com/mcp", "headers": {"CF-Access-Client-Id": "…", "CF-Access-Client-Secret": "…"}}}}`
- 이 PC 의 agent app 는 같은 서버를 `http://127.0.0.1:7440/mcp`(열쇠 없음)로 붙인다. stdio 로 되돌리기: 아래 2번 설정(백업 `%APPDATA%\agent-connector\connector.backup-20261019-before-http.json`).
- 결과 파일 링크: `WEBTOON_PUBLIC_URL` 이 있으면 export·job_status 가 `https://webtoon.js-96.com/files/exports/…` 링크를 준다(`exports\`·`series\` 의 pdf·epub·그림·영상만, 브라우저는 이메일 OTP).
- 오래 걸리는 호출: 응답이 SSE 라 15초마다 핑이 가서 Cloudflare 100초 제한에 안 걸린다(라이브러리 기본). 그래도 원칙은 30초 안, 긴 일은 작업(job)으로.
- 시험(2026-10-19): 열쇠 없이 /mcp·/files·/ 전부 302, 열쇠로 initialize·도구 34개·status 1.3초, 8컷 review_panels 28초, PDF 링크 200(7.4MB). 파이썬 기본 UA(`Python-urllib`)는 Cloudflare 봇 규칙에 403(브라우저·agent app 는 상관없음).

## agent app 에 붙이기(이 PC, stdio 방식: 주소 방식 전 기본, 되돌릴 때)
1. 파이썬 준비: 작업실 `.venv` 에 `requirements.txt`(정확한 버전) 설치. `uv pip install --python .venv\Scripts\python.exe -r requirements.txt`
2. agent app 를 끄고 `%APPDATA%\agent-connector\connector.json` 에 다음을 넣는다(agent app 설정 > MCP 화면에서 넣어도 같다).
   ```json
   "mcp": true,
   "mcpServers": [{"name": "webtoon-studio", "transport": "stdio",
     "command": "D:/webtoon-studio/.venv/Scripts/python.exe", "args": ["D:/webtoon-studio/mcp/server.py"],
     "env": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}, "enabled": true}]
   ```
3. agent app 를 켜고 설정 > MCP 에서 webtoon-studio 가 연결(도구 34개)인지 본다. server.py 를 고친 뒤엔 agent app 를 다시 켜야 새 도구가 보인다.
4. 에이전트는 둘이고 도구·스킬·ComfyUI 워크플로우는 같다(2026-10-18 사용자). 시작점만 다르다.
   | 에이전트 | 하는 일 | agent-server 워크플로우 |
   |---|---|---|
   | 「새 웹툰 생성기」 | 장르·인물·그림체를 묻고 작품 설정 → `new_series` → 1화 콘티·컷 설명 → `make_episode`(인물 시트·장소 시안에서 멈춤, `redo_candidate` 로 다시 뽑기) | `wf_webtoon_new_series` |
   | 「웹툰 이어 그리기」 | 있는 작품(D+18·무대 뒤, 새로 연 작품)의 다음 화(옛 이름 "이미지 스튜디오 작업실 (Qwen)" · "웹툰 작업실 (Qwen)") | `wf_1792023050405_k5kq9b2` |
   둘 다 Agent Geny · vLLM `qwen3.8-27b`, 시스템 프롬프트 = `agents\new.md`·`agents\continue.md` 머리말 + 공통 `AGENT_PROMPT.md`. 고친 뒤 `.venv\Scripts\python mcp\agents\apply.py apply`. 계정은 agent-server (redacted)(사용자 번호 49, 본인 계정).

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
| `comfy_server` | 서버 상태(인스턴스·VRAM·큐·모델 파일) · 받는 중인 모델 · 버전 전환 · 재시작 · 로그 · 상류 수정 붙이기 |
| `edit_panel` · `preview_panel` | 그린 컷의 일부만 고치기(편집 워크플로우, refs 로 인물 얼굴 맞추기) · 본편은 그대로 두고 미리 보기(지시 또는 인물 디자인) |
| `panel_versions` · `compare_panel` | 예전 판 보기 · 고치기 전/후 그림 |
| `vast_draw` | 같은 panels.json 을 vast ComfyUI(Qwen Image 2.1)로 그림 → `panels_hf_vast\`. turbo·expr 기본, aspect 로 세로, seed 로 다시. 한 번에 80컷까지 |
| `vast_make_assets` | 시트에서 참고 그림 자르기 + 베이스 몸·센 표정 12가지 |
| `new_series` · `add_to_series` · `redo_candidate` · `style_compare` | 새 작품 열기(작품 설정 한 장 → `series\<slug>-hf`, `tools\new_series.py`) · 인물·장소 더하기, 그림체·사람 규칙 바꾸기 · 마음에 안 드는 시안 다시 그리기 · 화풍 후보 비교표(그림체 여러 개 x 장면, `tools\style_probe.py`) |
| `make_episode` | **한 화를 한 번에**(어느 작품이든): 콘티 준비 → 에셋 점검(새 장소·인물은 시안 후 멈춤, `accept_new`) → vast 그리기 → 합성·점검 → 내보내기. 본편 그림이 있는 컷은 안 건드림 |
| `adopt_panel` | 사용자가 고른 판을 본편(panels_hf)으로: source=vast(다시 그린 판) · preview(미리 보기) · version(예전 판으로 되돌리기). 원본은 `_rejected\` |

`contact_sheet`·`review_panels` 에 `folder="vast"` 를 주면 vast 그림을 본다. 인스턴스를 켜고 끄는 도구는 두지 않는다. 에이전트 프롬프트(`AGENT_PROMPT.md`) v1.4 에 vast 쓰는 때와 순서를 넣었다("vast 로", "크레딧 없이" 라고 하면 vast_draw).

## agent-server 에 저장되는 것(2026-10-15 확인)
- 에이전트 「새 웹툰 생성기」·「웹툰 이어 그리기」 자체가 agent-server 워크플로우다(Agent Geny 노드 하나 + 시스템 프롬프트). 웹·agent app 어디서 열어도 같은 에이전트.
- 작업실 도구는 워크플로우 안에 저장되지 않는다. 대화할 때 이 PC 의 agent app 가 로컬 MCP 로 붙여 준다. 그래서 그림·합성은 이 PC 에서 agent app 가 켜져 있을 때만 된다.
- 캔버스의 "MCP Tool Loader" 노드는 서버에 떠 있는 MCP 세션을 고르는 노드인데 agent-server 세션 목록(`/api/mcp/sessions`)이 비어 있고, 팀 방향도 서버 호스팅 MCP 를 없애고 로컬 MCP → agent app → agent platform 이다. 로컬 도구를 노드로 박은 고정 파이프라인은 지금은 못 만든다.
- 나눌 수는 있다: "Workflow Tool" 노드로 다른 저장 워크플로우(예: 콘티 에이전트, 작화·검수 에이전트)를 도구로 부르는 관리자-작업자 구성.

## agent app 시험에서 고친 것(2026-10-16)
- 그림은 2분 만에 다 그려졌는데, 에이전트가 job_status 를 몇 초 간격으로 반복하다 agent app 의 반복 방지에 걸려 결과를 보고하지 못했다 → `job_wait`(최대 25초씩 기다림) 추가, job_status 에 걸린 시간·끝난 컷 수·사람 눈으로 확인할 컷. 고친 뒤 같은 요청이 끝까지 보고됐다.
- 에이전트가 회차를 "1화", 컷을 "30" 으로 불렀다 → 둘 다 받아 준다(ep001, p030).
- agent app 의 Qwen 은 한 번에 약 8천 토큰까지 쓴다. 한 화 콘티(약 16KB)·컷 설명(약 22KB)은 write_file 한 번에 못 담는다 → 나눠 쓰고 고친다(처음엔 start_file·append_file·check_file 세 도구, 2026-10-22 부터 `write_file` 의 part first·more·last 와 `edit_file`).
- Qwen 이 콘티(make_storyboard.py)는 잘 썼지만 컷 설명(build_panels.py)을 코드로 쓰다 잘리고 작업실에 없는 함수를 지어냈다 → 컷 설명은 표(panels_spec.json)를 `set_panels` 로 몇 컷씩 채우고, 공용 변환기 `tools/panels_from_spec.py` 가 panels.json 을 만든다. new_episode 는 이제 표 방식으로 연다.
- 시나리오 한 줄로 시험 회차를 끝까지 시켰다: 콘티 작성(56줄, 문법 통과) → 컷 설명 5개 표로 채움 → make_episode(5컷 그리기, 연출 확인에서 1컷 다시) → 기계 점검이 표지판식 자막 1개를 막음 → 에이전트가 edit_file 로 고쳐 다시 합성(막는 경고 0) → 내보내기. 결과물은 다 나왔지만 한 대화에서 도구 44번·36분을 끌자 마지막 답이 사내 모델 시간 초과(180초)로 실패했다 → 긴 작업은 부르고 바로 답을 끝내고, 한 화는 콘티 → 그리기 → 결과 확인으로 답을 나눠 진행한다(AGENT_PROMPT).
