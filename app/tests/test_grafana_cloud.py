"""Grafana Cloud 연결 (deploy/grafana_cloud.py): rules.yml에서 sli 그룹만 YAML 그대로 떼어 낸다."""
import os
import sys

DEPLOY = os.path.join(os.path.dirname(__file__), "..", "..", "deploy")
sys.path.insert(0, DEPLOY)
import grafana_cloud   # noqa: E402


def test_sli_group_only():
    g = grafana_cloud.sli_group(open(os.path.join(DEPLOY, "ops", "rules.yml"), encoding="utf-8").read())
    lines = g.splitlines()
    assert lines[:2] == ["name: sli", "rules:"]
    assert g.count("- alert: ") == 5
    assert "Watchdog" not in g and "traefik" not in g          # 운영 VM · 입구 그룹은 뺀다
    assert not any(line.lstrip().startswith("#") for line in lines)   # 주석 줄을 벗기다 YAML이 깨지지 않게 뺀다
    assert all(line.startswith("  ") or not line.strip() for line in lines[2:])   # 그룹 안은 rules: 아래로만
    assert "expr: max(banjjak_data_age_seconds) > 7200" in g
