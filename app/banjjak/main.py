from fastapi import FastAPI
from prometheus_client import make_asgi_app

app = FastAPI(title="banjjak")
app.mount("/metrics", make_asgi_app())


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
