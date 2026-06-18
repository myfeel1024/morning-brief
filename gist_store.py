"""GitHub Gist 기반 JSON 영속 저장 — 재배포 후에도 데이터 유지.

환경변수:
  GITHUB_GIST_TOKEN  : gist 권한이 있는 GitHub PAT
  ALERTS_GIST_ID     : 저장에 사용할 Gist ID (URL의 해시 부분)

설정 없으면 로컬 파일만 사용하고 Gist 기능은 무시됨.
"""

import os
import json
import requests

_TOKEN   = os.getenv("GITHUB_GIST_TOKEN", "")
_GIST_ID = os.getenv("ALERTS_GIST_ID", "")
_BASE    = "https://api.github.com/gists"


def _hdrs() -> dict:
    return {
        "Authorization": f"token {_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def load_json(filename: str):
    """Gist에서 JSON 파일 읽기. 설정 없거나 실패 시 None 반환."""
    if not (_TOKEN and _GIST_ID):
        return None
    try:
        r = requests.get(f"{_BASE}/{_GIST_ID}", headers=_hdrs(), timeout=10)
        if r.status_code == 200:
            files = r.json().get("files", {})
            if filename in files:
                raw = files[filename].get("content", "")
                return json.loads(raw) if raw else None
    except Exception as e:
        print(f"[gist_store] load_json 실패 ({filename}): {e}")
    return None


def save_json(filename: str, data) -> bool:
    """Gist에 JSON 파일 저장. 성공 시 True."""
    if not (_TOKEN and _GIST_ID):
        return False
    try:
        payload = {
            "files": {
                filename: {
                    "content": json.dumps(data, ensure_ascii=False, indent=2)
                }
            }
        }
        r = requests.patch(f"{_BASE}/{_GIST_ID}", headers=_hdrs(), json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[gist_store] save_json 실패 ({filename}): {e}")
        return False
