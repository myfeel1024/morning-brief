"""GitHub 레포 기반 JSON 영속 저장 — 재배포 후에도 데이터 유지.

기존 GITHUB_PAT (repo 권한) + 레포를 그대로 활용.
별도 환경변수 불필요.

저장 위치: myfeel1024/morning-brief 레포의 _data/{filename}
"""

import os
import json
import base64
import requests

_TOKEN = os.getenv("GITHUB_PAT", "")
_REPO  = "myfeel1024/morning-brief"
_BASE  = f"https://api.github.com/repos/{_REPO}/contents/_data"


def _hdrs() -> dict:
    return {
        "Authorization": f"token {_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def load_json(filename: str):
    """GitHub 레포에서 JSON 파일 읽기. 실패 시 None 반환."""
    if not _TOKEN:
        return None
    try:
        r = requests.get(f"{_BASE}/{filename}", headers=_hdrs(), timeout=10)
        if r.status_code == 200:
            raw = base64.b64decode(r.json()["content"]).decode("utf-8")
            return json.loads(raw)
    except Exception as e:
        print(f"[remote_store] load_json 실패 ({filename}): {e}")
    return None


def save_json(filename: str, data) -> bool:
    """GitHub 레포에 JSON 파일 저장 (create or update). 성공 시 True."""
    if not _TOKEN:
        return False
    try:
        content = base64.b64encode(
            json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode("utf-8")

        # 현재 SHA 조회 (update 시 필요)
        sha = None
        r = requests.get(f"{_BASE}/{filename}", headers=_hdrs(), timeout=10)
        if r.status_code == 200:
            sha = r.json().get("sha")

        payload = {
            "message": f"chore: update {filename}",
            "content": content,
        }
        if sha:
            payload["sha"] = sha

        r = requests.put(f"{_BASE}/{filename}", headers=_hdrs(), json=payload, timeout=10)
        return r.status_code in (200, 201)
    except Exception as e:
        print(f"[remote_store] save_json 실패 ({filename}): {e}")
        return False
