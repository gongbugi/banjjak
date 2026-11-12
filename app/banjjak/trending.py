"""순위: 가장 최근 사진과 하루 전 사진의 차이가 하루 별 증가다. 하루를 다 못 봤으면 본 만큼만 (실제 값은 최솟값)."""
from bisect import bisect_left, bisect_right
from datetime import timedelta

from sqlalchemy import func, select

from banjjak import db

# 가짜 별 무리 (본인 11-12): 최신 수집에서 생성 ±10분 · 별 0.8 ~ 1.25배인 레포가 자기 포함 4개 이상이면 순위에서 뺀다.
# 11-12 Azure 실측: 정상은 최대 3, 4부터 전부 스팸 (해적판 · 악성코드 미끼 이름)
CLUSTER_WINDOW, CLUSTER_MIN = timedelta(minutes=10), 4


def fake_star_clusters(lasts):
    # ponytail: 생성 시각을 10분 넘게 벌리거나 별 수를 흩은 무리는 못 거른다. 그때는 별 준 계정의 나이 같은 신호가 필요하다
    lasts = sorted(lasts, key=lambda x: x["created_at"])
    times = [x["created_at"] for x in lasts]
    out = set()
    for x in lasts:
        near = lasts[bisect_left(times, x["created_at"] - CLUSTER_WINDOW):bisect_right(times, x["created_at"] + CLUSTER_WINDOW)]
        if sum(0.8 * x["stars"] <= y["stars"] <= 1.25 * x["stars"] for y in near) >= CLUSTER_MIN:
            out.add(x["id"])
    return out


def latest_capture(conn):
    return conn.execute(select(func.max(db.star_snapshots.c.captured_at))).scalar()


def trending(conn, limit=50):
    latest = latest_capture(conn)
    if latest is None:
        return []
    s, r = db.star_snapshots, db.repos
    rows = conn.execute(
        select(r, s.c.captured_at, s.c.stars)
        .join(s, s.c.repo_id == r.c.id)
        .where(s.c.captured_at >= latest - timedelta(hours=24, minutes=30),   # 수집 시각이 밀려도 전날 기록까지 잡는다
               r.c.id.in_(select(s.c.repo_id).where(s.c.captured_at == latest)))
        .order_by(r.c.id, s.c.captured_at)
    ).mappings()

    by_repo = {}
    for row in rows:
        by_repo.setdefault(row["id"], []).append(row)

    fake = fake_star_clusters([snaps[-1] for snaps in by_repo.values()])   # 마지막 사진 = 최신 수집
    out = []
    for snaps in by_repo.values():
        first, last = snaps[0], snaps[-1]
        if last["id"] in fake:
            continue
        hours = (last["captured_at"] - first["captured_at"]).total_seconds() / 3600
        if hours >= 1:
            per_day, estimated = last["stars"] - first["stars"], False
        else:
            # ponytail: 기록이 하나뿐이면 생성 후 평균으로 추정한다. 다음 수집부터 실측으로 바뀐다
            age_days = max((latest - last["created_at"]).total_seconds() / 86400, 1)
            per_day, estimated = last["stars"] / age_days, True
        out.append({
            "id": last["id"], "full_name": last["full_name"], "url": last["html_url"], "description": last["description"],
            "language": last["language"], "stars": last["stars"],
            "stars_per_day": round(per_day, 1), "estimated": estimated, "observed_hours": int(hours),
        })
    out.sort(key=lambda x: x["stars_per_day"], reverse=True)
    for i, x in enumerate(out[:limit], 1):
        x["rank"] = i
    return out[:limit]
