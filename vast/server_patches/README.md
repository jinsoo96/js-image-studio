# vast ComfyUI 에 덧대는 상류 수정

`vast/comfy_admin.py` 가 버전을 바꾸기 전에 걷어 내고(git checkout), 바꾼 뒤 다시 붙인다(`git apply`). 상류에 이미 들어간 수정은
`git apply --check` 가 실패하니 건너뛴다. MCP `comfy_server(action="patch")` 로 지금 붙어 있나 보고 붙인다.

| 파일 | 상류 | 왜 |
|---|---|---|
| `qwen_image21_gated_residual_pr16654.diff` | Comfy-Org/ComfyUI PR #16654(이슈 #16653), 2026-10-20 미머지 | Qwen Image 2.1 의 `_gated_residual` 이 잔차를 제자리(`addcmul_`)로 더해 역전파가 읽을 텐서를 덮었다. 0.42.1 에서 TrainLoraNode 가 "CUDA illegal memory access" 로 ComfyUI 를 죽였고(dynamic VRAM 켬), 0.37 에서도 오류 없이 기울기를 망쳤을 수 있다(그때 학습한 로라의 반짝이·거친 결). 학습 중에만 새 텐서를 만들고 그리기 결과는 같다. 붙인 뒤 기본 실행 인자로 학습 스텝당 약 2초, 그리기 29초 확인(2026-10-20). |
