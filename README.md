# banjjak (반짝)

지금 뜨는 GitHub 레포를 AI가 쉽고 빠르게 정리하고, AI가 함께 운영하는 서비스.

- 매시간 GitHub 공식 API로 새 레포와 별 수를 모아 **별 증가 속도로 순위를 직접 계산**한다
- 새로 뜬 레포의 README를 AI가 요약·분류한다. 근거 문장은 코드가 원문과 대조한다
- 운영 AI가 서비스를 지켜보고, 위험 등급에 따라 알림만 하거나 조치한다
- 장애 주입기가 무작위로 서비스를 망가뜨린다. 정답 기록으로 운영 AI를 채점한다

## 구조

| 경로 | 무엇 |
|---|---|
| `app/` | 파이썬 앱 (FastAPI) |
| `deploy/k8s/` | k3s에 올리는 매니페스트 (kustomize) |

## 로컬 실행

```bash
cd app && pip install -e ".[dev]" && pytest && uvicorn banjjak.main:app --reload
```
