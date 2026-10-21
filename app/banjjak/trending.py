"""순위: 가장 최근 사진과 24시간 안의 가장 이른 사진의 차이로 하루 별 증가를 계산한다."""
from datetime import timedelta

from sqlalchemy import func, select

from banjjak import db


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
        .where(s.c.captured_at >= latest - timedelta(hours=24),
               r.c.id.in_(select(s.c.repo_id).where(s.c.captured_at == latest)))
        .order_by(r.c.id, s.c.captured_at)
    ).mappings()

    by_repo = {}
    for row in rows:
        by_repo.setdefault(row["id"], []).append(row)

    out = []
    for snaps in by_repo.values():
        first, last = snaps[0], snaps[-1]
        hours = (last["captured_at"] - first["captured_at"]).total_seconds() / 3600
        if hours >= 1:
            per_day, estimated = (last["stars"] - first["stars"]) * 24 / hours, False
        else:
            # ponytail: 기록이 하나뿐이면 생성 후 평균으로 추정한다. 다음 수집부터 실측으로 바뀐다
            age_days = max((latest - last["created_at"]).total_seconds() / 86400, 1)
            per_day, estimated = last["stars"] / age_days, True
        out.append({
            "full_name": last["full_name"], "url": last["html_url"], "description": last["description"],
            "language": last["language"], "stars": last["stars"],
            "stars_per_day": round(per_day, 1), "estimated": estimated,
        })
    out.sort(key=lambda x: x["stars_per_day"], reverse=True)
    for i, x in enumerate(out[:limit], 1):
        x["rank"] = i
    return out[:limit]
