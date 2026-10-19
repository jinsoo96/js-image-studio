# 웹툰 하네스: 이 MCP 로 일하는 법

> 이 문서는 MCP 가 guide() 로 내준다. 에이전트 시스템 프롬프트에 규칙을 쓰지 않는다: 순서는 이 문서와 도구 결과 끝의 "다음:" 이, 규칙은 관문(격리 judge)과 그림 검수가 강제한다.
> (2026-10-18 사용자 "붙이면 끝, 작업 안내나 검수는 하네스화로". agent platform 하네스와 같은 원칙: 절대 규칙은 프롬프트가 아니라 judge 가 강제한다.)

## 0. 시작
- 이 문서를 읽은 뒤 status() 로 작품·회차·도는 작업을 본다. 사용자에게는 한국어로 짧게 답한다. em dash(—)는 쓰지 않는다.
- 도구 결과 끝에 "다음:" 이 있으면 그대로 따른다. 관문에 걸리면 고칠 점을 고치고 같은 도구를 다시 부른다.
- 일의 노하우는 스킬에 있다: read_skill("storyboard") 콘티 · read_skill("panel-spec") 컷 설명 · read_skill("continuity") 이어 그리기 · read_skill("asset") 에셋.

## 1. 하네스 단계(어느 에이전트가 붙어도 같다)
| 단계 | 도구 | 관문·검사(통과해야 다음 단계) |
|---|---|---|
| 작품 열기 | new_series(spec) · add_to_series | 설정 칸 검사(인물 who·main·exprs, 장소 영어 설명). 그림체는 사용자와 고른다(그림체 후보 비교: 작업실의 tools/style_probe.py) |
| 콘티 | new_episode → make_storyboard.py 의 컷 부분을 쓴다(start_file·append_file·check_file·edit_file) | prepare_episode 의 **콘티 관문**(skills/storyboard/judge.json): 설정·직함 일치, 이유 있는 행동, 장면 연결, 결론 내레이션 금지, 직접 감정 고백 금지, 대사 길이 |
| 컷 설명 | set_panels(5~8컷씩) | prepare_episode 의 **컷 설명 관문**(skills/panel-spec/judge.json + 연출 문법 코드 검사): 마주 보기·얼굴 방향, 앉음 이어짐, 소품 종류·개수, 탈것 방향, 대사 중 뒷모습 금지, 시대 소품 |
| 그리기 | make_episode (관문을 통과해야 시작한다) | 컷마다 그린 직후 그림 검수: 인원·팔다리·독자 시선·마주 보기·현실 논리·소품·시대. 걸리면 걸린 점을 지시에 붙여 스스로 다시 그린다 |
| 이어짐 | make_episode 안에서 | 같은 장면 앞뒤 컷 비교: 자세·소품·옷·얼굴·그림체·좌우 |
| 합성·출판 | make_episode 안에서 · compose · export | 말풍선 점검(얼굴 가림·순서), 막는 경고 0 |

## 2. 새 작품(「새 웹툰 생성기」의 일)
1. 사용자가 준 것(장르·분위기·주인공·이야기)을 받고, 빠진 것만 한 번에 묻는다. "알아서"면 제안한다.
2. 작품 설정 초안을 표로 보여 주고 확인받는다: 제목 · 한 줄 소개 · 장르 · 인물 2~7명(이름·나이·역할·직함, 영어 who·main·exprs 6개) · 장소(영어) · 그림체(영어 문단) · 시대(era, era_rule 영어 한 문장).
3. new_series(spec) → new_episode(slug, "ep001") → 콘티 → 컷 설명 → prepare_episode 가 관문을 다 통과할 때까지 고친다. 여기서 한 번 답을 끝낸다("그릴까요?").
4. make_episode 를 부르고 바로 답을 끝낸다. 처음 나오는 인물·장소는 시안을 그리고 '확인 필요'로 멈춘다 → 사용자가 고르면 make_episode(..., accept_new=True).

## 3. 다음 화(「웹툰 이어 그리기」의 일)
- status() → 앞 회차의 끝(마지막 컷·대사·옷·장소)을 read_file 로 보고 이어지게 쓴다. new_episode 로 앞 회차 틀을 복사한다.
- 처음 나오는 인물·장소는 add_to_series 로 더한다(옛 작품은 그 작품의 make_sheet_prompts.py·make_location_prompts.py 에 한 줄).

## 4. 긴 작업과 대화
- 파일이 길면(콘티 16KB 안팎) start_file → append_file(4천 자 안팎씩) → check_file 로 나눠 쓴다. 일부만 고칠 땐 edit_file.
- 한 번의 답에서 도구를 30번 넘게 부르지 않는다(사내 모델이 마지막 답을 시간 안에 못 시작한다). 콘티·컷 설명이 끝나면 답을 끝내고, 그리기는 다음 답에서.
- make_episode·vast_draw 는 job_id 를 돌려준다. 기다리지 말고 작업 번호와 예상 시간을 알리고 답을 끝낸다. 사용자가 "다 됐어?" 하면 job_status 한 번.
- job_status 는 JSON 이다. state 가 needs_review 면 실패가 아니라 사람 확인 차례다: review(컷·issues·note)나 candidates 를 사용자에게 보여 주고 답을 끝낸다. links 가 있으면 그 링크를 준다.
- 그리는 판은 작품마다(series.json model): uc(기본, Qwen Image 2.1 Uncensored, 참고 그림으로 얼굴·장소를 맞춘다) · anima(애니메이션 그림체, 글만 받아 인물 생김새를 작품 설정의 who 글로 맞춘다). 고치기(편집)는 둘 다 uc. 바꿀 땐 set_model, 한 화 안에서는 섞지 않는다.
- 도구 오류가 JSON 이면 그 값대로 한다: busy 는 running_job 을 job_wait 로 기다린다(같은 도구를 다시 부르지 않는다). not_found 는 candidates 중 맞는 경로로 한 번 다시 부른다(경로를 짐작해 바꿔 가며 되풀이하지 않는다).
- 그림·모아보기·PDF 를 사용자에게 보여 줄 때는 결과의 link·drawn·links 주소를 준다. 이 MCP 의 파일은 작업실 PC 에 있어 agent app 작업 공간의 ShareFile·Bash·Read 로는 열리지 않는다. 이미 있는 그림은 file_link 로 주소를 받는다. 그림을 직접 봐야 하면 contact_sheet: 결과에 그림이 같이 온다(링크는 잠겨 있어 WebFetch 로는 안 열린다). 한 번 보고 판단한다.
- 같은 입력(같은 명령·같은 콘티·컷 설명)으로 같은 작업을 다시 부르면 도구가 repeated_job 오류와 이전 작업을 돌려준다. 같은 걸 다시 돌려도 같은 결과다: 사용자와 무엇을 바꿀지 정해 컷 설명을 고치거나 seed 를 바꾼 뒤 부른다.

## 5. 돈·장비·내용
- vast(회사 공용 GPU, 크레딧 0)가 기본이다. 인스턴스를 켜고 끄거나 만들고 지우지 않는다(도구도 없다).
- 힉스필드는 사용자 개인 결제다: estimate_draw 로 비용을 보여 주고 확인받은 뒤에만 draw_panels(confirm_credits=비용). 끝나면 남은 크레딧을 알린다.
- 인물은 모두 성인. 실존 인물·실제 회사 이름·상표는 쓰지 않는다. 원작이 있는 글은 이름·직함·호칭을 원작대로 둔다.
