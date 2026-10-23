"""그림을 보는 모델(judge) 주소 고르기: 첫째 VLM_URL·VLM_MODEL, 안 되면 VLM_FALLBACK 의 후보를 차례로(/v1/models 로 살아 있는지 본다).
(2026-10-18 외출: 사내 judge 서버가 502 로 죽었는데 검사가 조용히 '통과'로 넘어갔다. 사내 vLLM 은 자주 죽고 죽어도 조용하다)

    VLM_URL=https://.../v1  VLM_MODEL=qwen3.8-27b  VLM_API_KEY=(필요하면)
    VLM_FALLBACK="http://localhost:12361/v1|Qwen3-VL-8B-FP8,https://other/v1|model|key"   쉼표로 여럿, 각 '주소|모델[|키]'
state/vlm_url.txt('주소|모델', 한 줄)가 있으면 VLM_URL 다음 후보로 둔다(2026-10-22 사내 게이트웨이·vast 가 함께 꺼져 RunPod 파드의 Qwen 3.8 무검열을 씀).
아무 곳에도 닿지 않으면 JudgeDown 을 던진다(부르는 쪽이 '검사 못 함'으로 처리, 통과로 넘기지 않는다).
"""
import json
import re
import os
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://judge-gateway.invalid/v1"
DEFAULT_MODEL = "qwen3.8-27b"
POD_FILE = Path(__file__).resolve().parents[1] / "state" / "vlm_url.txt"
# RunPod 프록시 앞의 Cloudflare 가 파이썬 기본 User-Agent 를 403(1010)으로 막는다
UA = {"User-Agent": "webtoon-studio/1.0"}


class JudgeDown(RuntimeError):
    pass


_picked = None


def candidates() -> list:
    """고를 차례: 환경변수 VLM_URL(준 때만) → state/vlm_url.txt(지금 켠 서버, runpod_cloud use 가 적음) → VLM_FALLBACK → 사내 게이트웨이.
    모델·서버를 갈아끼우려면 state/vlm_url.txt 한 줄('주소|모델|키')만 바꾸면 된다: OpenAI 호환 chat(그림은 image_url)이면 어느 모델이든.
    (예전엔 사내 게이트웨이가 늘 첫째라, 꺼진 동안 작업마다 그 주소를 먼저 기다렸다. 2026-10-23 사용자 "LLM 모델·서버도 갈아낄 수 있어야")"""
    out = []
    if os.environ.get("VLM_URL"):
        out.append((os.environ["VLM_URL"], os.environ.get("VLM_MODEL", DEFAULT_MODEL), os.environ.get("VLM_API_KEY", "EMPTY")))
    if POD_FILE.exists():
        parts = [x.strip() for x in POD_FILE.read_text(encoding="utf-8").strip().split("|")]
        if len(parts) >= 2 and parts[0]:
            out.append((parts[0], parts[1], parts[2] if len(parts) > 2 else "EMPTY"))
    # 대체 후보를 안 줬으면 이 작업실의 사내 VL 터널(agent app 로컬 MCP 가 쓰는 localhost:12361)을 둔다. 사내 judge 가 502 로 반나절 죽었을 때
    # 대체가 없어 모든 검사가 '검사불가'로 끝났다(2026-10-20 외출). 대체 모델(8B)은 약해서 job 로그에 고른 주소가 남는다(endpoint)
    for item in ((os.environ.get("VLM_FALLBACK") or "http://localhost:12361/v1|Qwen3-VL-8B-FP8") + f",{DEFAULT_URL}|{DEFAULT_MODEL}").split(","):
        parts = [x.strip() for x in item.split("|")]
        if len(parts) >= 2 and parts[0]:
            out.append((parts[0], parts[1], parts[2] if len(parts) > 2 else "EMPTY"))
    return [(u.rstrip("/"), m, k) for u, m, k in out]


def endpoint(refresh: bool = False) -> tuple:
    """(주소, 모델, 키). 한 번 고른 건 이 실행 동안 쓴다(refresh=True 면 다시 고른다)."""
    global _picked
    if _picked and not refresh:
        return _picked
    tried = []
    for url, model, key in candidates():
        try:
            req = urllib.request.Request(url + "/models", headers={**UA, "Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=8) as r:
                ids = [m.get("id") for m in json.loads(r.read().decode()).get("data", [])]
            if model in ids or not ids:
                _picked = (url, model, key)
                return _picked
            tried.append(f"{url}: {model} 없음({', '.join(map(str, ids))[:80]})")
        except Exception as e:  # noqa: BLE001
            tried.append(f"{url}: {type(e).__name__} {str(e)[:60]}")
    raise JudgeDown("그림 보는 모델에 닿지 않는다: " + " / ".join(tried))


_plain = set()  # 생각 끄기 칸을 거절한 주소(이 실행 동안 그 칸 없이 보낸다)


def body_extra(model: str, url: str = "") -> dict:
    """생각(추론문) 끄기. 자체 호스팅 vLLM 의 Qwen3 계열은 chat_template_kwargs 로 끈다. 다른 모델·서버가 이 칸을 모른다며 400 을 주면
    chat 이 그 주소엔 칸 없이 다시 보낸다(모델 이름으로만 정하면 다른 서버로 갈아끼울 때 깨졌다)."""
    if url in _plain:
        return {}
    return {"chat_template_kwargs": {"enable_thinking": False}} if "qwen" in model.lower() else {}


def _strip_think(txt: str) -> str:
    """생각을 못 끈 모델이 본문 앞에 붙인 <think>…</think> 를 뺀다(판정 JSON·숫자를 읽는 쪽이 그 글에 걸렸다)."""
    return re.sub(r"(?s)^\s*<think>.*?</think>\s*", "", txt or "")


def chat(content: list, max_tokens: int = 600, temperature: float = 0.0, timeout: float = 120) -> str:
    """OpenAI 호환 chat 한 번. 실패하면 한 번 다른 후보를 골라 다시, 그래도 안 되면 JudgeDown."""
    for attempt in (0, 1, 2):
        url, model, key = endpoint(refresh=attempt == 2)
        body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": content}], **body_extra(model, url)}
        req = urllib.request.Request(url + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={**UA, "Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                # 본문이 비어 None 으로 오는 때가 있다(생각만 하고 끝난 답). 부르는 쪽 re·json 이 None 에서 터져 컷이 '실패'로 빠졌다(2026-10-19 p054)
                return _strip_think(json.loads(r.read().decode())["choices"][0]["message"].get("content") or "")
        except urllib.error.HTTPError as e:
            if e.code in (400, 422) and "chat_template_kwargs" in body and url not in _plain:
                _plain.add(url)  # 그 칸을 모르는 서버: 같은 주소에 칸 없이 한 번 더
                continue
            if attempt == 2:
                raise JudgeDown(f"{url} HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:100]}") from e
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                raise JudgeDown(f"{url} {type(e).__name__}: {str(e)[:100]}") from e
    raise JudgeDown("unreachable")


def parse_json(txt: str, repair: bool = True) -> dict:
    """판정 모델의 답 글에서 첫 JSON 객체를 꺼낸다(검수·관문이 같이 쓴다). 처음 '{' 부터 끝 '}' 까지 통째로 읽으면 뒤에 붙은 설명이나 두 번째 객체 때문에 깨졌다
    (2026-10-19 외출 1화 검사: 'Extra data'·'Expecting delimiter' 로 검사불가가 여러 컷)."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", txt):
        try:
            obj = dec.raw_decode(txt[m.start():])[0]
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    s = txt.find("{")
    if repair and s >= 0:  # repair=False: 관문처럼 잘린 답으로 통과시키면 안 되는 곳  # 답이 토큰 한도에서 잘렸다: 열린 따옴표·괄호를 닫아 앞부분을 살린다(2026-10-19 D+18 p022 검사불가)
        for cut in (txt[s:], txt[s:txt.rfind(",")] if txt.rfind(",") > s else None):
            if cut:
                try:
                    obj = json.loads(_close_json(cut))
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    return obj
    raise ValueError("JSON 없는 답: " + txt[:80])


def _close_json(s: str) -> str:
    """잘린 JSON 글의 열린 문자열·괄호를 닫는다."""
    stack, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            esc = (ch == "\\") and not esc
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    return s + ('"' if in_str else "") + "".join("}" if b == "{" else "]" for b in reversed(stack))
