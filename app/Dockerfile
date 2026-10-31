FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml alembic.ini requirements.lock ./
COPY banjjak ./banjjak
COPY migrations ./migrations
RUN pip install --no-cache-dir -c requirements.lock . && useradd --uid 10001 app
USER 10001
EXPOSE 8000 9100
# 같은 이미지로 web(앱 8000 · 지표 9100) · 수집기(python -m banjjak.collect) · 마이그레이션(alembic upgrade head)을 돌린다
CMD ["python", "-m", "banjjak.serve"]
