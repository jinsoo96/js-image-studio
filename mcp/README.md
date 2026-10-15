# 작업실 MCP 서버(agent app·Qwen 에서 쓰기)

`server.py` 는 작업실 기능을 MCP 도구 13개로 묶은 stdio 서버다. agent app(agent platform 데스크톱) 설정의 로컬 MCP 로 붙이면, agent platform 서버의 에이전트(사내 vLLM Qwen 등)가 이 PC 의 작업실을 다룬다(로컬 MCP → agent app → agent platform).

## 도구
| 도구 | 하는 일 | 크레딧 |
|---|---|---|
| `guide` · `read_doc` | 작업실 규칙(CLAUDE.md)·문서 읽기(긴 문서는 offset) | 0 |
| `status` · `credits` | 작품·회차·작화·합성 상태, 진행 중 작업, 남은 크레딧 | 0 |
| `list_panels` · `contact_sheet` | 컷 목록 · 검수용 모아보기 그림(경로를 돌려줌, `state\mcp_sheets\`) | 0 |
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
3. agent app 를 켜고 설정 > MCP 에서 webtoon-studio 가 연결(도구 13개)인지 본다.
4. 새 채팅 → 새 에이전트: 이름 "웹툰 작업실 (Qwen)", AI 제공사 vLLM(사내), 모델 `qwen3.8-27b`, 세부설정의 시스템 프롬프트에 `AGENT_PROMPT.md` 내용, 도구 노출 방식 Flat(Qwen 은 도구 탐색 단계를 잘 못 돈다).

2026-10-15 시험(agent app 1.91.0, agent-server, qwen3.8-27b): "상태 알려줘" → status 로 4편·크레딧 정확히 답함. "p079 다시 그려줘 + 모아보기" → guide·estimate_draw·contact_sheet 를 부르고 예상 1.25 크레딧을 보여 준 뒤 확인을 기다림(크레딧 안 씀).

## 알아 둘 것
- agent app 의 다중 파드 서버에서는 로컬 MCP 도구가 90초 쉬면 안 보일 수 있다. 그러면 agent app 설정 > MCP 에서 껐다 켠다(메모리 `dex-local-mcp-tools-vanish`).
- Qwen 은 처음에 인자를 빠뜨리고 부르기도 한다. 도구가 오류 문장을 돌려주면 대개 스스로 고쳐 다시 부른다.
- 작업실 파일을 직접 고치는 일(새 회차 make_storyboard.py 쓰기 등)은 agent app 채팅의 "폴더"로 `D:\webtoon-studio` 를 연결하면 에이전트가 파일·셸 도구로 할 수 있다.
