import pytest

from banjjak import secret


def test_file_wins_over_env(monkeypatch, tmp_path):
    # compose는 /run/secrets/ 파일로, k8s는 환경 변수로 준다. 파일이 있으면 파일
    f = tmp_path / "github-token"
    f.write_text("from-file\n", encoding="utf-8")   # 끝 줄바꿈은 뗀다
    monkeypatch.setenv("BANJJAK_T", "from-env")
    monkeypatch.setenv("BANJJAK_T_FILE", str(f))
    assert secret("BANJJAK_T") == "from-file"
    monkeypatch.delenv("BANJJAK_T_FILE")
    assert secret("BANJJAK_T") == "from-env"


def test_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("BANJJAK_T", raising=False)
    monkeypatch.delenv("BANJJAK_T_FILE", raising=False)
    assert secret("BANJJAK_T") is None
    with pytest.raises(KeyError):
        secret("BANJJAK_T", required=True)
    (tmp_path / "t").write_text("", encoding="utf-8")   # 빈 파일 = 그 기능을 끈다 (선택 비밀값)
    monkeypatch.setenv("BANJJAK_T_FILE", str(tmp_path / "t"))
    assert secret("BANJJAK_T") == ""
