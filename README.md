# Queue Master

Telegram-бот для черги на здачу лаб у груповому чаті: студенти записуються в часові слоти, бот тегає того, чия черга, і кличе наступного, коли попередній написав «Закінчив».

- Специфікація: [docs/SPEC.md](docs/SPEC.md)

## Запуск

```powershell
uv sync
copy .env.example .env   # і вписати BOT_TOKEN
uv run python -m bot
```
