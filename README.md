# Said-Done Bot

**Голосовое → расшифровка → [📋 Извлечь задачи] [📝 Сделать резюме] → результат + оценка.**

---

## Что делает

1. Пересылаешь голосовое (или несколько) → бот распознаёт через faster-whisper
2. Показывает расшифровку с кнопками 📋 Извлечь задачи и 📝 Сделать резюме
3. Выбор → LLM (OpenRouter) извлекает задачи или делает резюме
4. Оценка 1-5 → сохраняется в БД (включая transcript + full LLM output для отладки)

### Мульти-голосовые

- 3 сек debounce buffer: пересланные подряд голосовые собираются в пачку
- Максимум 5 голосовых за раз (CPU-ограничение: ~30-60 сек на 1 мин аудио)
- Прогресс-бар по количеству файлов: `🎙 1/3 (0%) → 2/3 (33%) → 3/3 (66%) → Готово!`
- LLM-merge: транскрипты объединяются — удаляются повторы, восстанавливается контекст

### Trial-система

- 20 бесплатных обработок → survey (4 вопроса) → +10 → блок
- Рейтинг 1-5: для задач И резюме
- Кнопки действий expire через 60 сек или при нажатии обеих

---

## Структура

```
said-done-bot/
├── config.yaml                 # Секреты (не коммитить!)
├── config.example.yaml         # Шаблон конфига
├── requirements.txt            # Python зависимости
├── .gitignore
├── README.md                   # Этот файл
├── core/
│   ├── transcriber.py          # faster-whisper транскрибация
│   ├── task_extractor.py       # LLM task extraction + merge_transcripts
│   ├── db.py                   # SQLite инициализация
│   ├── db_ops.py               # CRUD операции
│   └── messages.py             # Все пользовательские строки
├── prompts/
│   └── task-extraction.md      # System prompt для извлечения задач
├── entrypoints/
│   └── telegram_bot.py         # aiogram бот (основной файл)
├── view_feedback.py            # CLI viewer для feedback из БД
├── data/
│   └── bot.db                  # SQLite база
├── logs/
└── systemd/
    └── said-done-bot.service   # systemd unit
```

---

## Пайплайн

```
Голосовое(1-5) → faster-whisper → [транскрипты]
  ↓ (если > 1)
LLM merge → объединённый текст (убраны повторы, восстановлен контекст)
  ↓
Пользователь: [📋 Извлечь задачи] [📝 Сделать резюме]
  ↓
LLM (OpenRouter) → результат + [⭐ 1-5] → сохранение в БД
  ↓ (если 1-4)
Комментарий → сохранение → опционально survey
```

### Task extraction patterns

Извлекает задачи из любых формулировок:
| Формулировка | Пример |
|---|---|
| Императив | "позвони", "купи" |
| Потребность | "надо бы", "стоит" |
| Цель/намерение | "нам нужно", "планируем" |
| Мечта/желание | "мечтаю", "хотелось бы" |
| Ситуация → действие | "им выгодно X" |
| Проблема | "не работает" → починить |

---

## Деплой

```bash
# Через systemd (уже настроено)
sudo systemctl restart said-done-bot.service
sudo journalctl -u said-done-bot.service -f

# Push на GitHub → CI/CD
git push origin master
```

### Просмотр фидбека

```bash
python3 view_feedback.py              # все записи
python3 view_feedback.py 42           # конкретная запись
```

---

## Механика

- **STT:** faster-whisper (model: small, CPU, int8)
- **LLM:** OpenRouter (qwen/qwen-2.5-72b-instruct)
- **Framework:** aiogram 3.x
- **DB:** SQLite (voice_requests, users, feedback)
- **Бот:** @Talk_Do_bot (id: 8705886522)
- **GitHub:** https://github.com/stepanenkoviktor0110-boop/said-done-bot
