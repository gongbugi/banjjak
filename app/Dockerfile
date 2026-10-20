FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml ./
COPY banjjak ./banjjak
RUN pip install --no-cache-dir . && useradd --uid 10001 app
USER 10001
EXPOSE 8000
CMD ["uvicorn", "banjjak.main:app", "--host", "0.0.0.0", "--port", "8000"]
