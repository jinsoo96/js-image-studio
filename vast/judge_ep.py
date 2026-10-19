"""그림을 보는 모델(judge) 주소 고르기: 첫째 VLM_URL·VLM_MODEL, 안 되면 VLM_FALLBACK 의 후보를 차례로(/v1/models 로 살아 있는지 본다).
(2026-10-18 외출: 사내 judge 서버가 502 로 죽었는데 검사가 조용히 '통과'로 넘어갔다. 사내 vLLM 은 자주 죽고 죽어도 조용하다)

    VLM_URL=https://.../v1  VLM_MODEL=qwen3.8-27b  VLM_API_KEY=(필요하면)
    VLM_FALLBACK="http://localhost:12361/v1|Qwen3-VL-8B-FP8,https://other/v1|model|key"   쉼표로 여럿, 각 '주소|모델[|키]'
아무 곳에도 닿지 않으면 JudgeDown 을 던진다(부르는 쪽이 '검사 못 함'으로 처리, 통과로 넘기지 않는다).
"""
import json
import re
import os
import urllib.request

DEFAULT_URL = "https://judge-gateway.invalid/v1"
DEFAULT_MODEL = "qwen3.8-27b"


class JudgeDown(RuntimeError):
    pass


_picked = None


def candidates() -> list:
    out = [(os.environ.get("VLM_URL", DEFAULT_URL), os.environ.get("VLM_MODEL", DEFAULT_MODEL), os.environ.get("VLM_API_KEY", "EMPTY"))]
    for item in os.environ.get("VLM_FALLBACK", "").split(","):
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
            req = urllib.request.Request(url + "/models", headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=8) as r:
                ids = [m.get("id") for m in json.loads(r.read().decode()).get("data", [])]
            if model in ids or not ids:
                _picked = (url, model, key)
                return _picked
            tried.append(f"{url}: {model} 없음({', '.join(map(str, ids))[:80]})")
        except Exception as e:  # noqa: BLE001
            tried.append(f"{url}: {type(e).__name__} {str(e)[:60]}")
    raise JudgeDown("그림 보는 모델에 닿지 않는다: " + " / ".join(tried))


def body_extra(model: str) -> dict:
    """Qwen3 의 생각 끄기는 자체 호스팅 vLLM 에서만 chat_template_kwargs 로(다른 곳은 모르는 칸이라 거절할 수 있다)."""
    return {"chat_template_kwargs": {"enable_thinking": False}} if "qwen" in model.lower() else {}


def chat(content: list, max_tokens: int = 600, temperature: float = 0.0, timeout: float = 120) -> str:
    """OpenAI 호환 chat 한 번. 실패하면 한 번 다른 후보를 골라 다시, 그래도 안 되면 JudgeDown."""
    for attempt in (0, 1):
        url, model, key = endpoint(refresh=attempt == 1)
        body = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": content}], **body_extra(model)}
        req = urllib.request.Request(url + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                # 본문이 비어 None 으로 오는 때가 있다(생각만 하고 끝난 답). 부르는 쪽 re·json 이 None 에서 터져 컷이 '실패'로 빠졌다(2026-10-19 p054)
                return json.loads(r.read().decode())["choices"][0]["message"].get("content") or ""
        except Exception as e:  # noqa: BLE001
            if attempt == 1:
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
