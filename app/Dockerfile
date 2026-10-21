FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY banjjak ./banjjak
COPY migrations ./migrations
RUN pip install --no-cache-dir . && useradd --uid 10001 app
USER 10001
EXPOSE 8000
# 같은 이미지로 web · 수집기(python -m banjjak.collect) · 마이그레이션(alembic upgrade head)을 돌린다
CMD ["uvicorn", "banjjak.main:app", "--host", "0.0.0.0", "--port", "8000"]
