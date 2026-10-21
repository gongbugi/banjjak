"""웹 실행: 앱은 8000, 지표는 9100. Service · Ingress는 8000만 알고, 9100은 파드를 직접 긁는 수집기만 본다."""
import uvicorn
from prometheus_client import start_http_server

if __name__ == "__main__":
    start_http_server(9100)
    uvicorn.run("banjjak.main:app", host="0.0.0.0", port=8000)
