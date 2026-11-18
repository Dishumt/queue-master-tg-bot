"""Налаштування з .env або змінних середовища."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_path: str


def _read_env_file(path: Path) -> dict[str, str]:
    """Мінімальний парсер .env: рядки KEY=VALUE, # — коментар."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config(env_file: str = ".env") -> Config:
    # Справжні змінні середовища мають пріоритет над файлом.
    env = _read_env_file(Path(env_file)) | dict(os.environ)
    token = env.get("BOT_TOKEN", "")
    if not token or token.startswith("123456:"):
        raise SystemExit(
            "Не задано BOT_TOKEN. Скопіюй .env.example у .env і впиши токен від @BotFather."
        )
    return Config(bot_token=token, db_path=env.get("DB_PATH") or "queue.db")
