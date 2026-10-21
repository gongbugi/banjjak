"""분석기 (code-v1): 순위에 든 레포의 README를 읽어 카드를 만든다. LLM 없이 정해진 규칙으로만.

CronJob이 매시 20분(수집 20분 뒤)에 돌린다. 결과는 방식 이름과 함께 쌓아서, 나중에 붙일 AI판(#19)과
같은 README로 나란히 비교한다. 규칙을 고치면 METHOD를 code-v2로 올린다 (기준선이 흔들리지 않게).
"""
import base64
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from banjjak import db
from banjjak.trending import trending

METHOD = "code-v1"
RANKED = 50                      # 화면에 보이는 순위만 분석한다
RECHECK = timedelta(days=1)      # README가 바뀌었나는 하루 한 번까지만 본다
MAX_README = 300_000             # 아주 긴 README는 앞부분만 저장 · 분석한다


class NotFound(Exception):
    """레포에 README가 없다."""


def _kw(*words):
    # 낱말 경계로 찾는다("ai"가 "detail"에 걸리지 않게). 끝이 *면 앞부분만 맞으면 된다
    parts = [re.escape(w[:-1]) + r"\w*" if w.endswith("*") else re.escape(w) + r"(?![a-z0-9])" for w in words]
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + ")")


# 큰 분야 9개. 위에서부터 먼저 맞는 것 (실측 근거와 목록 확정: #5 댓글)
CATEGORIES = [
    ("ai-coding", "AI 코딩 도구",
     {"claude-code", "codex", "agent-skills", "claude-skills", "mcp", "mcp-server", "coding-agents", "coding-agent",
      "cursor", "gemini-cli", "claude-code-plugin", "agent-skill", "claude-code-skill", "claude-code-skills",
      "copilot", "opencode", "vibe-coding"},
     _kw("claude code", "claude-code", "codex", "coding agent*", "mcp", "agent skill*", "claude skill*", "cursor",
         "copilot", "vibe cod*", "gemini cli", "opencode", "subagent*", "agentic coding")),
    ("security", "보안",
     {"security", "cybersecurity", "security-tools", "reverse-engineering", "pentest", "pentesting", "malware",
      "privacy", "osint", "red-team", "ctf"},
     _kw("security", "vulnerab*", "exploit*", "reverse engineer*", "malware", "pentest*", "privacy", "osint",
         "jailbreak*")),
    ("lists", "목록 · 학습 자료",
     {"awesome-list", "awesome", "tutorial", "course", "learning", "roadmap", "books", "interview"},
     _kw("awesome", "curated list", "tutorial*", "course", "roadmap", "cheatsheet", "interview", "from scratch",
         "教程", "学习", "知识")),
    ("ai-models", "AI 모델 · 추론",
     {"llama-cpp", "inference", "fine-tuning", "diffusion", "transformers", "pytorch", "llm-inference",
      "quantization", "vllm"},
     _kw("inference", "model weights", "fine-tun*", "pretrain*", "quantiz*", "diffusion model*", "language model*",
         "neural net*", "pytorch", "benchmark*", "dataset*", "open-weight*", "tts", "speech")),
    ("ai-apps", "AI 에이전트 · LLM 앱",
     {"llm", "ai-agents", "ai-agent", "agent", "agents", "computer-use", "rag", "chatbot", "ai", "ai-video", "ocr",
      "deepseek", "gpt", "openai", "multi-agent", "ai-assistant", "langchain"},
     _kw("llm*", "agent*", "gpt*", "rag", "chatbot*", "ai-powered", "ai powered", "multimodal", "openai", "deepseek",
         "qwen", "gemini", "assistant*", "ai", "智能体", "大模型")),
    ("creative", "그래픽 · 영상 · 게임",
     {"motion-graphics", "animation", "art", "svg", "video", "canvas", "creative-coding", "threejs", "webgl",
      "motion-design", "ffmpeg", "game", "gamedev", "music", "audio", "3d", "shader", "image"},
     _kw("animation*", "motion graphic*", "motion design", "video*", "3d", "shader*", "render*", "image*", "music",
         "audio", "game*", "pixel*", "drawing", "font*", "emulator*", "running natively", "pc port", "remake",
         "动画", "游戏", "视频")),
    ("apps", "데스크톱 · 모바일 앱",
     {"macos", "windows", "ios", "android", "iphone", "desktop-app", "tauri", "swiftui", "electron", "menubar"},
     _kw("macos", "mac app", "menu bar", "ios app", "iphone", "android", "desktop app", "windows app",
         "reimplementation", "alternative to", "open source alternative", "open-source alternative", "launcher")),
    ("infra", "인프라 · 셀프 호스팅",
     {"self-hosted", "selfhosted", "docker", "kubernetes", "database", "devops", "homelab", "proxy", "networking",
      "local-first"},
     _kw("self-hosted", "self hosted", "kubernetes", "docker", "database", "proxy", "vpn", "homelab", "network*")),
    ("dev-tools", "개발 도구",
     {"developer-tools", "cli", "terminal", "homebrew", "git", "compiler", "debugger", "tui", "react", "nextjs",
      "shadcn", "design-system", "vue", "svelte", "tailwindcss", "frontend", "typescript"},
     _kw("cli", "terminal", "developer*", "compiler", "debug*", "package manager", "command-line", "command line",
         "editor", "tui", "type checker", "linter", "react", "next.js", "framework", "ui component*",
         "design system", "frontend", "tailwind")),
]
CATEGORY_NAMES = {slug: name for slug, name, _, _ in CATEGORIES} | {"unclassified": "미분류"}

# "Claude Code로 만든 앱"은 AI 코딩 도구가 아니다: 무엇으로 만들었나는 지우고 본다
_BUILT_WITH = re.compile(
    r"(built|made|created|written|coded|developed|generated|vibe[- ]coded)\s+(entirely\s+|fully\s+)?"
    r"(with|using|by|in)\s+(claude( code| opus[\w .]*)?|codex|cursor|copilot|gemini( cli)?|ai)\b", re.I)


def categorize(full_name, description, topics):
    text = _BUILT_WITH.sub(" ", f"{full_name.replace('/', ' ')} {description or ''}").lower()
    tags = {t.lower() for t in topics}
    for slug, _, cat_topics, keywords in CATEGORIES:
        if tags & cat_topics or keywords.search(text):
            return slug
    return "unclassified"


_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HTML_HEADING = re.compile(r"^\s*<h([1-6])[^>]*>(.*?)</h\1>", re.I)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_USAGE_HEADING = re.compile(
    r"install|quick\s*start|getting started|get started|usage|setup|how to use|"
    r"설치|사용법|시작하기|빠른 시작|安装|使用|快速开始|快速上手|インストール|使い方", re.I)


def _lines(readme):
    """(줄, 코드 블록 안인가, 제목 수준 · 글) — 코드 블록 안의 '#'을 제목으로 읽지 않는다."""
    fence = None
    for line in readme.splitlines():
        m = _FENCE.match(line)
        if fence:
            closing = m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not line.strip(fence[0] + " \t")
            if closing:
                fence = None
                yield line, "close", None
            else:
                yield line, "code", None
            continue
        if m:
            fence = m.group(1)
            yield line, "open", None
            continue
        h = _HEADING.match(line) or _HTML_HEADING.match(line)
        yield line, "text", (len(h.group(1)) if h.group(1).startswith("#") else int(h.group(1)), h.group(2)) if h else None


def extract_usage(readme):
    """설치 · 사용법 제목 아래 첫 코드 블록을 글자 그대로. 없으면 None (화면에는 "README에 없음")."""
    section, block = None, None
    for line, kind, heading in _lines(readme):
        if kind == "open":
            block = [] if section else None
        elif kind == "code":
            if block is not None:
                block.append(line)
        elif kind == "close":
            body = "\n".join(block or []).strip("\n")
            if body.strip():
                return body
            block = None
        elif heading:
            level, title = heading
            if section and level <= section:
                section = None
            if _USAGE_HEADING.search(title):
                section = level
    return None


# 알아둘 점: 이런 말이 든 README 문장을 그대로 인용한다 (뜻 판단은 #19의 AI 몫)
NOTE_KINDS = [
    ("telemetry", "사용 기록 전송",
     re.compile(r"telemetry|analytics|usage (data|statistics|stats)|anonymous(ly)? (usage|data)|opt[- ]out", re.I)),
    ("limits", "무료 한도 · 요금",
     re.compile(r"free tier|free plan|rate[- ]limit|quota|pricing|paid plan|subscription|requires? an? api key", re.I)),
    ("license", "라이선스 제약",
     re.compile(r"non-?commercial|commercial use|\bagpl|proprietary|all rights reserved|personal use only|"
                r"source[- ]available", re.I)),
    ("security", "보안", re.compile(r"security (fix|patch|advisory|issue)|vulnerabilit|\bcve-\d", re.I)),
    ("maturity", "아직 실험 단계",
     re.compile(r"\bexperimental\b|\balpha\b|early (preview|access)|not (yet )?(ready )?for production|"
                r"not production[- ]ready|use at your own risk|work in progress|breaking changes", re.I)),
]
NOTE_NAMES = {kind: name for kind, name, _ in NOTE_KINDS}
_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+")
_LIST_MARK = re.compile(r"^\s*(?:[-*+>]|\d+[.)])\s+")


def extract_notes(readme):
    notes, seen = [], set()
    for line, kind, heading in _lines(readme):
        text = line.strip()
        # 코드 · 제목 · 표 · HTML · 배지 줄은 문장이 아니다
        if kind != "text" or heading or not text or text[0] in "|<!" or text.startswith("[!["):
            continue
        text = _LIST_MARK.sub("", text)
        for sentence in _SENTENCE_END.split(text):
            for note_kind, _, pattern in NOTE_KINDS:
                if note_kind not in seen and pattern.search(sentence):
                    seen.add(note_kind)
                    notes.append({"kind": note_kind, "quote": sentence.strip()[:400]})
    return notes


def analyze(repo, readme):
    return {"category": categorize(repo["full_name"], repo["description"], repo["topics"]),
            "usage": extract_usage(readme), "notes": extract_notes(readme)}


def _record_failure(conn, repo_id, now, error):
    conn.execute(insert(db.analyses).values(repo_id=repo_id, method=METHOD, analyzed_at=now, checked_at=now,
                                            ok=False, error=error))


def analyze_once(fetch_readme, conn, now):
    """순위에 든 레포 중 처음 보거나 하루가 지난 것만 README를 본다. (분석 수, 실패 수)를 돌려준다."""
    a = db.analyses
    analyzed = failed = 0
    for item in trending(conn, limit=RANKED):
        repo = conn.execute(select(db.repos).where(db.repos.c.id == item["id"])).mappings().one()
        last = conn.execute(select(a).where(a.c.repo_id == repo["id"], a.c.method == METHOD)
                            .order_by(a.c.checked_at.desc()).limit(1)).mappings().first()
        if last and now - last["checked_at"] < RECHECK:
            continue
        try:
            readme = fetch_readme(repo["full_name"])
        except NotFound:
            _record_failure(conn, repo["id"], now, "README 없음")
            failed += 1
            continue
        except Exception as e:   # 네트워크 · 한도 초과 등: 실패로 남기고 다음 레포로
            _record_failure(conn, repo["id"], now, f"README 가져오기 실패: {e}"[:500])
            failed += 1
            continue
        if last and last["ok"] and last["readme_sha"] == readme["sha"]:
            conn.execute(a.update().where(a.c.id == last["id"]).values(checked_at=now))   # 그대로다
            continue
        conn.execute(insert(db.readmes).values(repo_id=repo["id"], sha=readme["sha"], content=readme["content"],
                                               fetched_at=now).on_conflict_do_nothing())
        conn.execute(insert(a).values(repo_id=repo["id"], method=METHOD, readme_sha=readme["sha"], analyzed_at=now,
                                      checked_at=now, ok=True, **analyze(repo, readme["content"])))
        analyzed += 1
    return analyzed, failed


def github_readme(token):
    def fetch(full_name):
        req = urllib.request.Request(f"https://api.github.com/repos/{full_name}/readme", headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(full_name) from e
            raise
        content = base64.b64decode(body["content"]).decode("utf-8", errors="replace")
        return {"sha": body["sha"], "content": content[:MAX_README]}
    return fetch


def main():
    with db.engine().begin() as conn:
        n, failed = analyze_once(github_readme(os.environ["GITHUB_TOKEN"]), conn, datetime.now(timezone.utc))
    print(f"analyzed {n}, failed {failed}")


if __name__ == "__main__":
    main()
