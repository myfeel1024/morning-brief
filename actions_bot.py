"""
GitHub Actions 퀀트 분석기
Render 봇이 workflow_dispatch로 트리거 → 퀀트 계산 → 텔레그램 전송
환경변수: QUANT_CHAT_ID, QUANT_TOP_N (workflow inputs에서 주입)
"""

import os
import sys
from pathlib import Path


def _load_env():
    env = Path(__file__).resolve().parent / ".env"
    if not env.exists():
        return
    with open(env, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_env()


def main():
    chat_id = os.getenv("QUANT_CHAT_ID", "").strip()
    top_n   = int(os.getenv("QUANT_TOP_N", "10"))

    if not chat_id:
        print("[ERROR] QUANT_CHAT_ID 없음 — workflow_dispatch input 확인 필요")
        sys.exit(1)

    print(f"[START] 퀀트 분석: chat_id={chat_id}, top_n={top_n}")

    from quant_runner import run_quant_signal
    run_quant_signal(top_n=top_n, chat_id=chat_id)


if __name__ == "__main__":
    main()
