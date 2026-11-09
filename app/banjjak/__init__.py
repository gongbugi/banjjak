import os


def secret(name, required=False):
    """비밀값 하나. NAME_FILE이 있으면 그 파일(compose secrets: /run/secrets/...), 없으면 환경 변수 NAME(k8s Secret).
    끝 줄바꿈만 뗀다 (postgres 이미지의 *_FILE과 같다). required인데 둘 다 없으면 os.environ[NAME]처럼 KeyError"""
    path = os.environ.get(name + "_FILE")
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read().rstrip("\n")
    return os.environ[name] if required else os.environ.get(name)
