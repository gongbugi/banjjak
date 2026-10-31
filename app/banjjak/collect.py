"""수집기: 최근 생긴 레포 후보를 GitHub 검색으로 찾고, 별 개수를 기록한다. CronJob이 매시간 한 번 돌린다."""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects.postgresql import insert

from banjjak import db
from banjjak.heartbeat import ping

WINDOW_DAYS = 30     # 최근 30일 안에 생긴 레포만 (오래된 레포의 급등은 못 잡는다)
MIN_STARS = 100      # 상한 500이 먼저 걸려 실제 하한은 그날 500번째 별 수
PER_PAGE = 100
MAX_PAGES = 5        # 최대 500개 추적 (검색이 별 많은 순이라 위쪽 500개)


def pages(search, query, max_pages):
    """search(query, page) -> GitHub 검색 응답(dict). 쪽마다 items를 내준다.
    한 건도 못 받았거나 잘린 결과면 실패로 끝낸다: GitHub은 장애 중에도 200 + 빈(또는 모자란) 결과를 줄 수 있다
    (2026-10-23 00:00 · 01:00 KST 0건, 10-27 21:00 16건).
    그걸 "성공"으로 끝내면 Job · 하트비트는 정상인데 데이터만 묵는다 → 실패여야 재시도 · 하트비트 끊김 · 운영 AI 진단이 울린다."""
    count, seen = 0, None
    for page in range(1, max_pages + 1):
        body = search(query, page)
        items = body["items"]
        # GitHub이 이 결과를 믿어도 되는지 (incomplete_results: 검색이 시간 안에 다 못 돌았다). 사후에 원인을 가리려고 남긴다
        seen = f"total_count={body.get('total_count')} · incomplete_results={body.get('incomplete_results')}"
        print(f"search page {page}: {len(items)} items · {seen}", flush=True)
        yield items
        count += len(items)
        if len(items) < PER_PAGE:
            # 모자란 쪽 = 보통은 마지막 쪽. 그런데 GitHub이 "다 못 돌았다"고 하면 결과가 잘린 것이다 (2026-10-27 21:00: 16건)
            # → 그 회차를 실패로 (트랜잭션이 되돌려져 이전 수집이 화면에 남는다). 다 찬 쪽은 표시가 떠도 쓴다
            if body.get("incomplete_results"):
                raise RuntimeError(f"GitHub 검색 {page}쪽이 {len(items)}건인데 다 못 돌았다고 한다 ({seen}) "
                                   "→ 잘린 결과를 성공으로 저장하지 않는다")
            break
    if count == 0:
        raise RuntimeError(f"GitHub 검색이 0건을 돌려줬다 ({seen}) → 빈 결과를 성공으로 저장하지 않는다")


def save_repo(conn, it, now):
    row = {k: it[k] for k in ("id", "full_name", "html_url", "description", "language", "created_at")}
    row["topics"] = it.get("topics", [])   # 분야 분류의 첫 단서 (실측: 절반 정도만 붙어 있다)
    row["last_seen_at"] = now
    conn.execute(insert(db.repos).values(row).on_conflict_do_update(
        index_elements=["id"], set_={k: row[k] for k in row if k != "id"}))


def collect_once(search, conn, now):
    """기록한 레포 수를 돌려준다."""
    since = (now - timedelta(days=WINDOW_DAYS)).date().isoformat()
    count = 0
    for items in pages(search, f"created:>={since} stars:>={MIN_STARS}", MAX_PAGES):
        for it in items:
            save_repo(conn, it, now)
            conn.execute(insert(db.star_snapshots).values(
                repo_id=it["id"], captured_at=now, stars=it["stargazers_count"]).on_conflict_do_nothing())
        count += len(items)
    return count


def github_search(token):
    def search(query, page):
        url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
            {"q": query, "sort": "stars", "order": "desc", "per_page": PER_PAGE, "page": page})
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    return search


def main():
    with db.engine().begin() as conn:
        n = collect_once(github_search(os.environ["GITHUB_TOKEN"]), conn, datetime.now(timezone.utc))
    print(f"collected {n} repos")
    ping(os.environ.get("HEARTBEAT_URL"))   # 저장까지 끝났을 때만


if __name__ == "__main__":
    main()
