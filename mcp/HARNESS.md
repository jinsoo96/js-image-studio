# 웹툰 하네스: 이 MCP 로 일하는 법

> 이 문서는 MCP 가 guide() 로 내준다. 에이전트 시스템 프롬프트에 규칙을 쓰지 않는다: 순서는 이 문서와 도구 결과 끝의 "다음:" 이, 규칙은 관문(격리 judge)과 그림 검수가 강제한다.
> (2026-10-18 사용자 "붙이면 끝, 작업 안내나 검수는 하네스화로". agent platform 하네스와 같은 원칙: 절대 규칙은 프롬프트가 아니라 judge 가 강제한다.)

## 0. 시작
- 이 문서를 읽은 뒤 status() 로 작품·회차·도는 작업을 본다. 사용자에게는 한국어로 짧게 답한다. em dash(—)는 쓰지 않는다.
- 도구 결과 끝에 "다음:" 이 있으면 그대로 따른다. 관문에 걸리면 고칠 점을 고치고 같은 도구를 다시 부른다.
- 일의 노하우는 스킬에 있다: read_doc("skill:storyboard") 콘티 · read_doc("skill:panel-spec") 컷 설명 · read_doc("skill:continuity") 이어 그리기 · read_doc("skill:asset") 에셋.

## 1. 하네스 단계(어느 에이전트가 붙어도 같다)
| 단계 | 도구 | 관문·검사(통과해야 다음 단계) |
|---|---|---|
| 작품 열기 | new_series(spec) · add_to_series | 설정 칸 검사(인물 who·main·exprs, 장소 영어 설명). 그림체는 사용자와 고른다(style_compare 로 후보 비교표 → add_to_series(…, 'style')) |
| 콘티 | new_episode → set_storyboard(컷 목록을 표로, 몇 컷씩 나눠도 됨. 파이썬을 쓰지 않는다) | prepare_episode 의 **콘티 관문**(skills/storyboard/judge.json): 설정·직함 일치, 이유 있는 행동, 장면 연결, 결론 내레이션 금지, 직접 감정 고백 금지, 대사 길이 |
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

## 4. 저장소(작업실) 둘러보기·다루기
작품·그림·내보낸 것은 모두 작업실 저장소에 쌓인다. 일마다 따로 된 도구를 찾지 말고 아래 일반 도구로 둘러보고 옮긴다(지우는 도구는 없다: 안 쓰게 된 옛 판은 `_old/` 로 옮긴다(자동으로 안 지움, 10일 넘으면 목록만 알림). 시험용 결과물은 `_tmp/` 로 옮기면 10일 뒤 저절로 지워진다(예약작업 WEBTOON-clean-old).
- 구조: `series/<작품>-hf/` = series.json(설정·그림체·인물) · assets/(인물 시트 `<id>_sheet.png` · locations/ 장소 · vast_refs/ 참고 그림·표정 은행 · _candidates/ 시안) ·
  episodes/<회차>/(storyboard_spec.json 콘티 표(옛 회차는 make_storyboard.py) · panels_spec.json 컷 설명 · panels_hf/ 본편 그림 · panels_hf_vast/ 다시 그린 그림 · out/ 합성본). `exports/<날짜>/` 내보낸 PDF·epub·예고편. `worklogs/` 작업 기록.
- 보기: list_files(path, pattern='*.png') 목록·크기·고친 시각 · read_file 글 · contact_sheet(paths='폴더·파일·* 패턴') 그림(시트·시안·참고 그림·장소도) · file_link 사용자에게 줄 주소.
- 다루기: copy_file(파일·폴더·패턴 → 작품 폴더·exports·worklogs, 그림 포함) · move_file(옮기기·이름 바꾸기, 같은 이름이 있으면 안 옮김, 그 작품에 작업이 돌면 안 옮김).
- 예) 회차를 다른 버전으로: new_episode(작품, 'ep001_v2', template='ep001')(같은 화면 콘티·컷 설명·그림을 통째로 옮겨 연다) → 바꿀 컷만 set_storyboard(같은 id 는 덮음)·set_panels →
  prepare_episode → 바뀐 컷만 vast_draw(ids) → compare_panel(redraw) → adopt_panel → compose → export(버전은 파일 이름 끝에 _v2 가 붙어 원본을 덮지 않는다).
  그림을 처음부터 다시 그릴 버전이면 그렇게 연 뒤 panels_hf 를 move_file 로 _old 로 옮기고 make_episode.
- 예) 인물 한 명 생김새·옷 바꾸기(작품 전체에 걸린다: 옛 회차를 다시 그려도 새 모습): add_to_series(작품, 'cast', <id>, …) → redo_candidate(sheet, <id>) →
  contact_sheet(paths=assets/_candidates) 로 보여 주고 고르기 → move_file(옛 시트 → assets/_old/) · move_file(시안 → assets/) →
  move_file(assets/vast_refs/<id>_* → assets/_old/refs/) → vast_make_assets(작품, <id>) 로 참고 그림·표정 은행 다시 만들기 → 그 인물이 나온 컷을 다시 그린다.
- 예) 그림체부터 다시 잡기(사용자 "그림체 후보 골라 보고해", "시안부터 다시"). 단계마다 보여 주고 고른 뒤 다음으로 간다:
  1. 화풍 후보: style_compare(name, styles={후보 4~6개, 지금 그림체는 "series:<작품>"}, scenes={주인공 얼굴 장면 · 조연이 같이 나오는 장면 · 배경 장면}, series=작품)
     → contact_sheet(paths=…/compare.jpg). 사용자 말("조연이 너무 예쁨", "너무 못난이")은 후보 문단에 넣어 다시 부른다(같은 이름이면 바뀐 칸만 다시 그린다).
  2. 확정: add_to_series(작품, 'style', 'style', 고른 문단). 누구만 미남·미녀이고 나머지는 평범한지 같은 규칙은 add_to_series(작품, 'people_rule', 'rule', 문장).
  3. 인물 시안(단계별 후보): 인물마다 prompts/sheet_<id>.txt 를 read_file → 생김새 문장만 단계별로 바꿔 write_file(prompts/sheet_<id>_p1.txt, _p2 …,
     예: p1 평범한 얼굴 · p2 살짝 못생긴 얼굴) → redo_candidate(sheet, '<id>_p1') … → contact_sheet(paths=assets/_candidates) 로 단계를 나란히 보여 주고 고르기.
     고른 단계의 문장은 add_to_series(cast, <id>) 의 who 에 넣어 다음 시트·프롬프트도 그 얼굴이 되게 한다.
  4. 바꿔 넣기: move_file(assets/*_sheet.png → assets/_old/<이름>/) → 고른 시안을 move_file(assets/_candidates/<id>_p2_sheet.png → assets/<id>_sheet.png) →
     장소도 redo_candidate(location, <장소>) 를 장소마다 → move_file(assets/locations/*.png → assets/_old/<이름>/locations/) · move_file(_candidates/locations/*.png → assets/locations/) →
     move_file(assets/vast_refs/* → assets/_old/<이름>/refs/) → vast_make_assets(작품, 'all', 'both') · 'subtle' · 'turn'(참고 그림은 시트에서 저절로 잘린다. 전신끼리 맞붙은 시트도 된다).
  5. 다시 그리기: 옛 그림을 남길 회차는 copy_file 로 새 버전(ep001_v2)을 열고, 같은 회차를 덮으려면 그 회차 panels_hf·panels_hf_vast·panels 를 _old 로 옮긴 뒤
     prepare_episode(관문) → make_episode. 연기가 어색하다는 말이 있었으면 컷 설명의 얼굴 방향·보는 곳·소품 동작부터 고친다(set_panels).

## 5. 긴 작업과 대화
- 있는 파일을 옮기거나 틀로 쓸 땐 copy_file(src, dst) 한 번으로 복사하고 고칠 곳만 edit_file 로 고친다(read_file 로 읽어 write_file 로 다시 치지 않는다: 수만 자가 컨텍스트에 들어와 무거워지고 끊긴다).
- 콘티·컷 설명은 set_storyboard·set_panels 로 몇 컷씩 넣는다(파일을 write_file 로 통째로 쓰지 않는다). 다른 긴 글 파일만 write_file 을 part="first" → "more" → "last" 로 나눠 쓴다(last 에서 문법 검사). 일부만 고칠 땐 edit_file.
- 한 번의 답에서 도구를 30번 넘게 부르지 않는다(사내 모델이 마지막 답을 시간 안에 못 시작한다). 콘티·컷 설명이 끝나면 답을 끝내고, 그리기는 다음 답에서.
- prepare_episode·make_episode·vast_draw·compose·export 는 모두 작업이다(긴 한 화는 몇 분에서 몇 시간): state 가 running 이면 panels_done/panels_total·eta_s 를 알리고 답을 끝낸다. 작업은 대화·턴 제한과 상관없이 이어 도니 기다리며 도구를 되풀이하지 않는다. 작업 번호를 잃었거나 새 대화면 job_status(job_id 비움) 목록으로 이어 본다. stalled 가 true 면 멈춘 것이니 그대로 사용자에게 알린다.
- make_episode·vast_draw 는 job_id 를 돌려준다. 기다리지 말고 작업 번호와 예상 시간을 알리고 답을 끝낸다. 사용자가 "다 됐어?" 하면 job_status 한 번. 컷 몇 장처럼 짧은 작업만 job_status(wait=50) 로 기다린다(최대 4번).
- job_status 는 JSON 이다. state 가 needs_review 면 실패가 아니라 사람 확인 차례다: review(컷·issues·note)나 candidates 를 사용자에게 보여 주고 답을 끝낸다. links 가 있으면 그 링크를 준다.
- 판(Qwen UC·Anima·MOSS·NoctQ …)은 고르지 않아도 된다: vast_draw·make_episode 가 컷마다 자동으로 고른다(vast/router.py). 작품 그림체 갈래(series.json style_family: webtoon·anime·realistic, 없으면 webtoon)와 같은 판 중 참고 그림을 받는 판이 바탕이고, 작품 그림체 LoRA 를 학습했으면 그 판, 이름 있는 인물이 나오는 컷인데 바탕 판이 글로만 그리면 같은 그림체에서 참고 그림을 받는 판으로 옮긴다(그림체가 다른 판으로는 옮기지 않는다). 고른 판과 이유는 작업 로그에 컷마다 남는다. 역할(그리기·부분,벗기기, 고치기·에셋·시안·학습)도 도구가 그 판의 역할 워크플로우를 고른다. 사용자가 판을 말했을 때만 vast_draw(model=...) 를 쓴다(작품 전체를 바꿀 땐 series.json 의 model). 쓸 수 있는 판·역할·모델 파일 상태는 draw_models(). 컷이 흐리거나 얼굴 선이 거칠면 vast_draw(refine=True)(컷당 약 2배 시간, 클로즈업·표지에만).
- **이미 그린 컷은 통째로 다시 그리지 말고 틀린 곳만 고친다**(크레딧 0, 컷당 30초~1분, 다시 그리기는 다른 곳까지 바뀐다). 고르는 순서: ① 예전 판이 더 나았으면 panel_versions → adopt_panel(source='version') ② 일부만 틀림(소품, 사람 하나 지우기·넣기, 입, 손, 물건 크기)은 edit_panel(지시는 영어로 'In <image1>, … Keep everything else exactly the same') ③ 얼굴이 다른 사람 같으면 edit_panel(refs=그 인물 id, 얼굴만 <image2> 처럼) ④ 사람 자리·테이블·구도·카메라가 틀리면 set_panels 로 컷 설명을 고치고 vast_draw(같은 장면 앞 컷으로 자리를 맞춘다, 본편은 그대로, compare_panel(what="redraw") 로 본편과 나란히 보고 고르면 adopt_panel). 고친 뒤에는 compare_panel 로 전/후 그림을 직접 보고(지시한 곳이 고쳐졌나, 얼굴·옷·사람 수가 바뀌지 않았나), 나빠졌으면 되돌린다. 자동 검수(review_panels·job 의 review)는 놓치는 게 많다: 넘기기 전에 contact_sheet 로 장면 단위 모아보기를 직접 보고, 떠난 사람이 남아 있나·같은 장면에서 자리가 바뀌나·소품 크기와 시대가 맞나를 본다. 컷을 다 고친 뒤 compose 는 한 번만. 어떤 판·역할 워크플로우가 있는지는 draw_models().
- 인물 얼굴이 컷마다 흔들리면 인물 LoRA 를 쓴다: loras(series) 로 등록 상태·학습할 수 있는 인물을 보고, lora_train(series, character) 로 학습한다(작업으로 돈다, 데이터·캡션은 도구가 만든다. loras 에 training_disabled 가 있으면 지금 서버에선 학습하지 않고 그 대안을 사용자에게 알린다). 학습이 끝나면 그 판으로 그리는 그 인물 컷에 자동으로 붙는다. 몇 컷 그려 보고 얼굴이 굳거나 그림체가 무너지면 loras(series, character, strength=…, on="false") 로 강도를 내리거나 끈다. 다른 판(예: Anima)으로 그리는 컷에는 붙지 않는다.
- 찾아볼 때는 파일을 읽어 짐작하지 말고 도구로 묻는다: find(컷 번호·낱말·인물 이름 → 작품·회차·컷과 status), panel_info(컷 하나의 콘티·컷 설명·그림 위치), list_panels(회차의 컷 목록), job_status(job_id 비움: 돌았거나 도는 작업), draw_models(그릴 수 있는 판). 컷 번호는 p22·22·p022 어느 꼴로 줘도 도구가 실제 번호로 풀어 준다. 같은 번호가 여러 회차(ep002 · ep002_v2 · _vast 복사본)에 있으면 find 결과의 episode 를 보고 사용자가 말한 회차를 고른다.
- 도구 오류는 JSON 이다: error(종류) · given(내가 준 값) · reason · candidates(실제로 있는 값)가 있으면 그 중에서 골라 한 번만 다시 부른다. attempt 는 같은 실패를 몇 번째 보는지다: 2 이상이면 같은 호출을 되풀이하지 말고, 받은 오류를 그대로 사용자에게 알리고 어떻게 할지 묻는다.
- 도구 오류가 JSON 이면 그 값대로 한다: busy 는 running_job 을 job_status(wait=50) 로 기다린다(같은 도구를 다시 부르지 않는다). not_found 는 candidates 중 맞는 경로로 한 번 다시 부른다(경로를 짐작해 바꿔 가며 되풀이하지 않는다).
- 그림·모아보기·PDF 를 사용자에게 보여 줄 때는 결과의 link·drawn·links 주소를 준다. 이 MCP 의 파일은 작업실 PC 에 있어 agent app 작업 공간의 ShareFile·Bash·Read 로는 열리지 않는다. 이미 있는 그림은 file_link 로 주소를 받는다. 그림을 직접 봐야 하면 contact_sheet: 결과에 그림이 같이 온다(링크는 잠겨 있어 WebFetch 로는 안 열린다). 한 번 보고 판단한다.
- 같은 입력(같은 명령·같은 콘티·컷 설명)으로 같은 작업을 다시 부르면 도구가 repeated_job 오류와 이전 작업을 돌려준다. 같은 걸 다시 돌려도 같은 결과다: 사용자와 무엇을 바꿀지 정해 컷 설명을 고치거나 seed 를 바꾼 뒤 부른다.

## 6. 돈·장비·내용
- 그리기는 GPU 서버(ComfyUI, 크레딧 0) 하나로 한다: vast_draw·make_episode 가 쓰는 서버는 state/comfy_url.txt 가 가리키는 곳(지금은 RunPod 파드)이고 guide 첫 줄에 지금 주소가 나온다. 인스턴스를 켜고 끄거나 만들고 지우지 않는다(도구도 없다).
- 한 컷을 바꿔 보여 줄 때: 일부만 바꾸면 preview_panel(지시 instruction, 또는 인물 디자인 character+design) → compare_panel(what="preview") → 고르면 adopt_panel(source='preview'), 통째로 다시 그려 보려면 vast_draw(ids=그 컷) → compare_panel(what="redraw") → 고르면 adopt_panel. 둘 다 고르기 전까지 본편은 안 바뀐다.
- 인물은 모두 성인. 실존 인물·실제 회사 이름·상표는 쓰지 않는다. 원작이 있는 글은 이름·직함·호칭을 원작대로 둔다.
