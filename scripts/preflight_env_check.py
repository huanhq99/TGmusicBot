#!/usr/bin/env python3
"""Simple sanity checker for TGmusicbot deployments.

Reads environment variables from the current shell and optional .env file,
then reports missing required keys plus notable optional ones.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Tuple

REQUIRED_KEYS: Tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN",
    "ADMIN_USER_ID",
    "EMBY_URL",
    "EMBY_USERNAME",
    "EMBY_PASSWORD",
    "PLAYLIST_BOT_KEY",
)

OPTIONAL_KEYS: Tuple[str, ...] = (
    "WEB_USERNAME",
    "WEB_PASSWORD",
    "MUSIC_PROXY_URL",
    "MUSIC_PROXY_KEY",
    "TG_API_ID",
    "TG_API_HASH",
    "TELEGRAM_API_URL",
    "EMBY_SCAN_INTERVAL",
)

ENV_FILE = Path.cwd() / ".env"


def load_env_file(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    values: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def main() -> int:
    combined = load_env_file(ENV_FILE)
    combined.update(os.environ)

    missing = [key for key in REQUIRED_KEYS if not combined.get(key)]
    optional_missing = [key for key in OPTIONAL_KEYS if not combined.get(key)]

    if missing:
        print("[ERROR] 缺少必填环境变量:\n  - " + "\n  - ".join(missing))
    else:
        print("[OK] 所有必填环境变量均已提供。")

    if optional_missing:
        print("[INFO] 以下可选项未设置，如需对应功能请补齐:\n  - " + "\n  - ".join(optional_missing))
    else:
        print("[OK] 可选项也已全部设置。")

    if missing:
        example = "\n".join(f"{key}=..." for key in missing)
        print("\n可以在 .env 中补充，如:\n" + example)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
