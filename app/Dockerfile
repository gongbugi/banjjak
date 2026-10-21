FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY banjjak ./banjjak
COPY migrations ./migrations
RUN pip install --no-cache-dir . && useradd --uid 10001 app
USER 10001
EXPOSE 8000 9100
# 같은 이미지로 web(앱 8000 · 지표 9100) · 수집기(python -m banjjak.collect) · 마이그레이션(alembic upgrade head)을 돌린다
CMD ["python", "-m", "banjjak.serve"]
