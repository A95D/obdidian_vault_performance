# Obsidian Vault Performance

Инструмент для семантического анализа хранилища знаний Obsidian (vault).
На основе содержимого файлов строит таксономию доменов, типов контента,
learning paths и фасетной навигации, а результат показывает в веб-дашборде.

## Что делает

1. Сканирует ваш Obsidian vault по указанному пути.
2. Анализирует содержимое файлов (не теги) через Claude Code агента.
3. Строит `taxonomy.json` — домены, типы, зависимости, маршруты обучения.
4. Показывает результат в `dashboard.html` — интерактивной карте вашего vault.

## Требования

- Python 3.12+
- Claude Code (для запуска анализа через агента `vault-domain-analyzer`)
- Установленный Obsidian vault с `.md` файлами

## Установка

1. Склонируйте репозиторий и перейдите в папку проекта.
2. Установите python-зависимости:

   ```bash
   pip install -r requirements.txt
   ```

3. Скопируйте `.env.example` в `.env` и укажите путь к вашему vault:

   ```env
   VAULT_PATH=C:\path\to\Obsidian Vault
   ```

## Первый запуск

Анализ запускается через Claude Code, а не напрямую скриптами:

1. Откройте проект в Claude Code.
2. Попросите: **"построй таксономию"** (или похожую фразу — см. триггеры
   в `.claude/skills/semantic-taxonomy/SKILL.md`).
3. Навык `semantic-taxonomy` спросит режим анализа:
   - **Full** — полная пересборка (первый запуск, смена `VAULT_PATH`).
   - **Incremental** — добавление только новых файлов.
4. Дождитесь завершения всех 6 фаз анализа. Результат — `.claude/temp_files/taxonomy.json`.

Анализ содержимого файлов домена выполняет агент `vault-domain-analyzer`
автоматически — вручную ничего читать/писать не нужно.

## Просмотр результата

Запустите дашборд:

```bash
start-dashboard.bat
```

Скрипт поднимет локальный HTTP-сервер на порту 8787 и откроет
`dashboard.html` в браузере — там доступна фасетная навигация по доменам,
типам контента и learning paths.

## Структура репозитория

```
obdidian_vault_performance/
├── CLAUDE.md              ← правила репо и навигация для Claude Code
├── dashboard.html          ← веб-дашборд для просмотра taxonomy.json
├── start-dashboard.bat     ← запуск локального сервера + открытие дашборда
├── requirements.txt        ← python-зависимости
├── .env / .env.example     ← VAULT_PATH и другие переменные окружения
└── .claude/
    ├── agents/             ← vault-domain-analyzer.md
    ├── skills/             ← analyze-vault-domain, semantic-taxonomy, vault-migration
    ├── plans/              ← технические планы
    └── temp_files/         ← временные файлы анализа (чистятся cleanup.py)
```

## Важно

- `.env` содержит путь к вашему личному vault — не публикуйте его.
- `taxonomy.json` может содержать выдержки из содержимого ваших заметок —
  учитывайте это перед тем, как делиться файлом.
- Повторный запуск анализа в режиме Incremental не отслеживает удалённые,
  переименованные или перемещённые файлы — для полной синхронизации
  используйте режим Full.

## Подробнее

- Логика анализа домена — `.claude/agents/vault-domain-analyzer.md`
- Оркестрация построения таксономии — `.claude/skills/semantic-taxonomy/SKILL.md`
- Схема и примеры анализа домена — `.claude/skills/analyze-vault-domain/references/`
