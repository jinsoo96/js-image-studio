# 작업실 MCP 서버

`server.py` 는 웹툰 작업실 기능을 MCP 도구 46개로 묶은 서버다. MCP 를 지원하는 클라이언트(미사일 스튜디오, Claude Code 등)에 붙이면
그 클라이언트의 에이전트가 이 PC 의 작업실로 작품을 만들고 고친다. 도구 목록과 설명은 서버가 알려 주고(`tool_help` 로 전체 설명),
사람이 보는 요약은 루트 `README.md` 에 있다.

## 띄우기

| 방식 | 명령 | 쓰는 곳 |
|---|---|---|
| 주소(기본) | `.venv\Scripts\python.exe mcp\server.py --http --port 7440` → `http://127.0.0.1:7440/mcp` | 이 PC 의 클라이언트, 터널을 거친 다른 PC |
| stdio | `.venv\Scripts\python.exe mcp\server.py` | 클라이언트가 직접 띄우는 경우 |

- 이 PC 에서는 예약작업 `WEBTOON-MCP-http`(부팅 때 자동, 죽으면 1분 뒤 다시)가 `mcp\serve_http.cmd` 로 주소 방식을 띄운다. 로그 `state\mcp_http.log`.
- `server.py` 를 고친 뒤엔 예약작업을 다시 띄운다(프로세스는 이름으로 좁혀 끈다).
  ```powershell
  Stop-ScheduledTask WEBTOON-MCP-http
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ? CommandLine -like '*server.py --http*' | % { Stop-Process $_.ProcessId -Force }
  Start-ScheduledTask WEBTOON-MCP-http
  ```
  정상이면 `GET /mcp` 가 406 이다. 클라이언트는 다시 연결하면 새 도구 목록을 받는다.

## 클라이언트에 붙이기

- **미사일 스튜디오**: MCP 서버 `imagestudio` 로 `http://127.0.0.1:7440/mcp` 를 넣는다. 켜기 명령(`wake_command`)을 `tools\gpu_wake.py` 로 두면
  GPU 서버가 꺼져 있을 때 켜기 버튼이 보인다. LLM 공급자는 `state\vlm_url.txt` 를 읽는 공급자(지금 켠 LLM 상자) 또는 아무 공급자.
- **다른 MCP 클라이언트**: 종류 `http`, 주소 위. 표준 JSON 설정 예:
  `{"mcpServers": {"imagestudio": {"type": "http", "url": "http://127.0.0.1:7440/mcp"}}}`
- 도구마다 MCP 표준 성질 표시(읽기 전용·덮어쓰기·바깥 게시)가 붙어 있어, 클라이언트는 읽기 도구를 묻지 않고 덮어쓰기·게시는 묻게 정할 수 있다.

## 다른 PC 에서(원격 주소)

```
다른 PC 의 클라이언트 ──https + 열쇠 헤더──▶ Cloudflare Access ──터널──▶ 이 PC 127.0.0.1:7440/mcp ──▶ 작업실 · GPU 서버
```
- 주소 `https://webtoon.js-96.com/mcp`. Cloudflare Access 앱 `Webtoon MCP` 의 정책: ① 서비스 토큰(헤더 `CF-Access-Client-Id`·`CF-Access-Client-Secret`)
  ② 소유자 이메일 인증(브라우저로 결과 링크를 볼 때). 열쇠 값은 `D:\.claude\CREDENTIALS.local.md` 의 Cloudflare 절에만 있다. 열쇠 없는 요청은 302.
- 클라이언트 설정에 헤더 둘을 더한다:
  `{"mcpServers": {"imagestudio": {"type": "http", "url": "https://webtoon.js-96.com/mcp", "headers": {"CF-Access-Client-Id": "…", "CF-Access-Client-Secret": "…"}}}}`
- 결과 파일: `WEBTOON_PUBLIC_URL` 이 있으면 도구가 `https://webtoon.js-96.com/files/…` 링크를 준다(`exports\`·`series\` 의 pdf·epub·그림·영상만).
- 오래 걸리는 호출도 SSE 핑이 15초마다 가서 Cloudflare 100초 제한에 안 걸린다. 그래도 원칙은 한 호출 30초 안, 긴 일은 작업(job).

## 에이전트 프롬프트

- `AGENT_PROMPT.md`: 어느 에이전트든 붙일 때 쓰는 공통 규칙(작가 겸 작화 감독, 시작할 때 guide·status, 작업과 job_status 다루기, 돈·내용의 선).
- `agents\new.md`(「새 웹툰 생성기」: 장르·인물·그림체를 묻고 `new_series` → 1화) · `agents\continue.md`(「웹툰 이어 그리기」: 있는 작품의 다음 화).
  역할 머리말 + 공통 규칙을 이어 붙여 클라이언트의 어시스턴트 시스템 프롬프트로 쓴다. 도구·스킬은 같고 시작점만 다르다.
- 도구 설명이 짧아 대부분은 따로 프롬프트 없이도 돈다(`guide` 가 단계와 관문을 알려 준다).

## 서버 원칙

- 한 번 호출은 30초 안. 그리기·합성·내보내기·학습·점수는 작업(job)으로 띄우고 `job_status` 로 본다(`state\mcp_jobs\`).
- 지우는 도구는 없다(옛 판은 `_old\`, 시험 결과물은 `_tmp\`). 다시 그릴 컷의 원본은 `panels_hf\_rejected\`.
- 자식 프로세스는 stdin 을 막는다(MCP 전송로를 물려받으면 멈춘다). stdout 에 print 하지 않는다.
- 도구 설명은 첫 문단(모델에게 가는 요약, 340자 안)과 그 뒤(인자 · 돌려줌 · 다음 · 주의, `tool_help` 로 읽음)로 쓴다.

## 그림 검수(review_panels)

- 그림을 보는 모델은 `state\vlm_url.txt` 의 서버(지금 켠 LLM 상자). 바꾸려면 환경변수 `VLM_URL`·`VLM_MODEL`, 대체 후보는 `VLM_FALLBACK`(`vast\judge_ep.py`).
- 컷마다 두 번 따로 보고 둘 다 걸린 항목만 '다시', 한 번만 걸린 건 '애매'(사람 눈으로 확인)로 낸다. 컷 설명을 자세히 쓸수록 판정이 정확하다.
- 판정은 참고용이고 사용자가 고른다.
