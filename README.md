# Said-Done Bot

**Минимальный Telegram-бот: голосовое → расшифровка → кнопка «Сделать резюме» → резюме.**

---

## Что делает

1. Пересылаешь голосовое → бот распознаёт через faster-whisper
2. Показывает расшифровку с кнопкой 📝 Сделать резюме
3. Нажимаешь кнопку → LLM (OpenRouter) делает краткое резюме от первого лица

---

## Структура

```
said-done-bot/
├── config.yaml                 # Секреты (не коммитить!)
├── config.example.yaml         # Шаблон конфига
├── requirements.txt            # Python зависимости
├── .gitignore
├── core/
│   └── transcriber.py          # faster-whisper транскрибация
├── entrypoints/
│   └── telegram_bot.py         # aiogram бот
├── logs/
└── systemd/
    └── said-done-bot.service   # systemd unit
```

---

## Деплой

```bash
# Через systemd (уже настроено)
sudo systemctl restart said-done-bot.service
sudo journalctl -u said-done-bot.service -f
```

---

## Механика

```
voice → transcribe_ogg() → показать текст → [📝 Сделать резюме] → llm_summarize()
```

- **STT:** faster-whisper (model: small, CPU, int8)
- **LLM:** OpenRouter (qwen/qwen-2.5-72b-instruct)
- **Framework:** aiogram 3.x
