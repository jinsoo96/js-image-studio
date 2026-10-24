"""도구 코드에 특정 작품의 이름(제목·slug·인물·장소)이 박혀 있는지 찾는다(GPU·모델 안 씀, 돈 0).
도구는 어느 작품에서나 같아야 하므로, 작품 이름은 series/ 의 설정에서 읽어야지 코드에 적혀 있으면 안 된다.
찾을 이름은 박지 않고 series/*/ 의 series.json·cast.json·characters.json·README 제목에서 모은다.

    .venv/Scripts/python tools/hardcode_scan.py [--all]

실패(종료 코드 1)로 치는 것 두 가지:
  코드      — 이름·보통 문자열에 작품 이름. 설정 읽기로 바꾼다.
  모델 글   — 대화·판정 모델이 읽는 글(mcp/ 의 도구 설명, skills/ 의 문서·판정 질문)에 작품 이름.
              모델이 그 작품 쪽으로 끌려가므로 일반 예로 바꾼다.
주석(왜 이렇게 고쳤는지 남긴 기록)과 시험 자료(*cases*.json, 작품을 이름으로 불러야 한다)는 실패가 아니다: --all 이면 같이 보인다.
"""
import argparse
import io
import json
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN = ["mcp", "vast", "tools", "skills"]  # 도구 코드. series/ 안(작품 자기 설정·작품 전용 스크립트)은 보지 않는다
MODEL_READ = ["mcp", "skills"]  # 여기의 세 따옴표 글·.md·.json 은 모델이 읽는다(도구 설명·read_doc·판정 질문·에이전트 프롬프트)
HUMAN_DOCS = {"mcp/README.md"}  # 그 가운데 사람만 읽는 문서(skills/README.md 는 read_doc('skill') 이 내주므로 모델 글)
EXT = {".py", ".json", ".md"}


def _load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _person_names(v) -> list[str]:
    """인물 항목 하나에서 이름들: ko·name(나이 괄호 뗌), en 의 괄호 앞."""
    out = []
    if isinstance(v, dict):
        for k in ("ko", "name"):
            if isinstance(v.get(k), str):
                out.append(re.sub(r"\s*\(.*?\)", "", v[k]).strip())
        if isinstance(v.get("en"), str):
            out.append(re.sub(r"\s*\(.*?\)", "", v["en"]).strip())
    return [n for n in out if len(n) >= 2]


def names_of(sd: Path) -> dict[str, str]:
    """{찾을 글: 무엇인지}. 일반 낱말(hero·leader 같은 인물 키)은 따옴표로 둘러싼 키로만 찾는다."""
    found: dict[str, str] = {}
    slug = sd.name[:-3] if sd.name.endswith("-hf") else sd.name
    found[sd.name] = "작품 폴더"
    found[slug] = "작품 slug"
    sj = _load(sd / "series.json") or {}
    title = sj.get("title") or ""
    readme = sd / "README.md"
    if not title and readme.exists():
        m = re.search(r"^#\s*(.+)$", readme.read_text(encoding="utf-8"), re.M)
        title = re.split(r"\s+[—-]\s+", m.group(1).strip())[0] if m else ""
    if title:
        found[title] = "작품 제목"
        base = re.sub(r"\s*[\(（].*?[\)）]", "", title).strip()
        if base and base != title:
            found[base] = "작품 제목"
    people = {}
    for src in (sj.get("cast"), _load(sd / "cast.json"), _load(sd / "characters.json")):
        if isinstance(src, dict):
            people.update({k: v for k, v in src.items() if not k.startswith("_")})
        elif isinstance(src, list):
            people.update({str(v.get("id") or i): v for i, v in enumerate(src) if isinstance(v, dict)})
    for key, v in people.items():
        found[f'"{key}"'] = "인물 키"
        found[f"'{key}'"] = "인물 키"
        for n in _person_names(v):
            found[n] = "인물 이름"
    locs = sj.get("locations")
    keys = list(locs) if isinstance(locs, dict) else [l.get("id") for l in locs if isinstance(l, dict)] if isinstance(locs, list) else []
    for k in keys:
        if k and "_" in k:  # 'night' 같은 일반 낱말은 빼고, 이름처럼 쓰는 장소 키만
            found[k] = "장소 키"
    return found


def _py_parts(text: str) -> dict[int, tuple[str, str, str]]:
    """줄 번호 → (코드, 주석, 세 따옴표 문자열). 코드 = 이름·보통 문자열. 세 따옴표는 독스트링(MCP 도구면 대화 모델이 읽는 도구 설명)."""
    parts: dict[int, list[list[str]]] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                kind = 1
            elif tok.type == tokenize.STRING:
                kind = 2 if re.match(r"^[rbuRBUfF]*('''|\"\"\")", tok.string) else 0
            elif tok.type == tokenize.NAME:
                kind = 0
            else:
                continue
            for i, piece in enumerate(tok.string.split("\n")):  # 여러 줄 문자열은 이름이 실제로 나온 줄에만
                parts.setdefault(tok.start[0] + i, [[], [], []])[kind].append(piece)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return {i + 1: (ln, "", "") for i, ln in enumerate(text.splitlines())}
    return {n: tuple(" ".join(x) for x in v) for n, v in parts.items()}


def _tool_doc_lines(text: str) -> set[int]:
    """@…tool 이 붙은 함수의 독스트링 줄(MCP 가 도구 설명으로 모델에 보낸다). 다른 독스트링은 사람용."""
    import ast
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any("tool" in ast.unparse(d) for d in node.decorator_list):
            b = node.body[0] if node.body else None
            if isinstance(b, ast.Expr) and isinstance(getattr(b, "value", None), ast.Constant) and isinstance(b.value.value, str):
                out.update(range(b.lineno, (b.end_lineno or b.lineno) + 1))
    return out


def scan() -> list[tuple[str, int, str, str, str, str]]:
    """(파일, 줄, 작품, 무엇, 글, 종류). 종류: 코드 · 모델 글 · 주석 · 시험 자료."""
    series = sorted(p for p in (ROOT / "series").iterdir() if p.is_dir() and not p.name.startswith("_"))
    targets: list[tuple[str, str, str]] = []  # (글, 무엇, 작품)
    for sd in series:
        targets += [(t, what, sd.name) for t, what in names_of(sd).items()]
    hits = []
    for d in SCAN:
        model_read = d in MODEL_READ
        for f in sorted((ROOT / d).rglob("*")):
            if f.suffix not in EXT or not f.is_file() or "__pycache__" in f.parts or f.resolve() == Path(__file__).resolve():
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            if f.suffix == ".py":
                tool_doc = _tool_doc_lines(text) if model_read else set()
                rows = [(n, [("코드", c), ("주석", m), ("모델 글" if n in tool_doc else "주석", s)]) for n, (c, m, s) in sorted(_py_parts(text).items())]
            else:
                kind = "시험 자료" if "cases" in f.stem else "모델 글" if model_read and f.relative_to(ROOT).as_posix() not in HUMAN_DOCS else "주석"
                # JSON 의 "_why"·"why" 칸은 사람용 메모라 모델에 가지 않는다(checks.json 의 why 는 코드가 읽지 않는다)
                rows = [(i + 1, [("주석" if re.match(r'\s*"(_\w*|why)"\s*:', ln) else kind, ln)]) for i, ln in enumerate(text.split("\n"))]
            for n, chunks in rows:
                for t, what, sname in targets:
                    kind = next((k for k, txt in chunks if t in txt), "")
                    if kind:
                        hits.append((str(f.relative_to(ROOT)), n, sname, what, t, kind))
    return hits


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="주석·시험 자료에 나온 것도 보인다(실패로는 안 친다)")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    hits = scan()
    bad = [h for h in hits if h[5] in ("코드", "모델 글")]
    for path, n, sname, what, t, kind in (hits if a.all else bad):
        print(f"{path}:{n}  [{kind}] {sname} {what} {t!r}")
    count = {k: sum(1 for h in hits if h[5] == k) for k in ("코드", "모델 글", "주석", "시험 자료")}
    print(f"\n작품 이름: 코드 {count['코드']} · 모델이 읽는 글 {count['모델 글']} (이 둘이 0 이어야 한다) · 주석 {count['주석']} · 시험 자료 {count['시험 자료']}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
