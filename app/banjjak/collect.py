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
MIN_STARS = 100
PER_PAGE = 100
MAX_PAGES = 5        # 최대 500개 추적


def collect_once(search, conn, now):
    """search(query, page) -> GitHub 검색 응답(dict). 기록한 레포 수를 돌려준다."""
    since = (now - timedelta(days=WINDOW_DAYS)).date().isoformat()
    query = f"created:>={since} stars:>={MIN_STARS}"
    count = 0
    for page in range(1, MAX_PAGES + 1):
        items = search(query, page)["items"]
        for it in items:
            row = {k: it[k] for k in ("id", "full_name", "html_url", "description", "language", "created_at")}
            row["topics"] = it.get("topics", [])   # 분야 분류의 첫 단서 (실측: 절반 정도만 붙어 있다)
            row["last_seen_at"] = now
            conn.execute(insert(db.repos).values(row).on_conflict_do_update(
                index_elements=["id"], set_={k: row[k] for k in row if k != "id"}))
            conn.execute(insert(db.star_snapshots).values(
                repo_id=it["id"], captured_at=now, stars=it["stargazers_count"]).on_conflict_do_nothing())
        count += len(items)
        if len(items) < PER_PAGE:
            break
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
