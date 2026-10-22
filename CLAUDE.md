# Obsidian Vault Performance - навигация по проекту

## Что это за проект

Инструменты семантического анализа Obsidian-хранилища: таксономия доменов по содержимому файлов, результат в веб-дашборд (`dashboard.html`).

В начале сессии прочитай `.claude/plans/`, `.claude/retrospectives/`, `.claude/skills/*/SKILL.md`.

## Где что искать

Код в `.claude/skills/semantic-taxonomy/`, пути ниже сокращены до `scripts/...`.

| Что нужно | Куда смотреть |
|---|---|
| Анализ одного домена | `.claude/agents/vault-domain-analyzer.md` |
| Карта доменов по всему vault (Фаза 1.2) | `.claude/agents/vault-domain-architect.md` |
| related/dependencies для новой заметки | `.claude/agents/vault-relevance-finder.md` |
| Оркестрация таксономии | `.claude/skills/semantic-taxonomy/SKILL.md` |
| Скан vault и фильтр мусора (Фаза 1.1, без LLM) | `scripts/collect_vault_structure.py --scan` |
| Граф тем и dirty-пересчёт (Фаза 1.4) | `scripts/build_topic_graph.py` → `topic-graph.json`, `graph-dirty-domains.json` |
| Финализация и проверка покрытия | `scripts/synthesize_taxonomy.py` (`--merge`, `--verify`) |
| Инкремент: драйвер | `scripts/incremental.py` (`prepare`, `apply`, `reassess-input`, `finalize`, `status`) |
| Инкремент: карточки и патч архитектора | `scripts/incremental.py domain-cards` → `domain-cards.json`; `apply-patch` (`patch.json` → `domain-map.json`, `domain-merge-map.json`, `affected-domains.json`) |
| Инкремент: переоценка тем (Фаза 3б) | `.claude/agents/vault-topic-reassessor.md`; вход `scripts/incremental.py build-reassess-input`; проверка `validate-topics --domain ID` → `{domain_id}-topics.validated.json` |
| Пакет файлов в песочницу `llm` | `scripts/llm_send.sh` (функция `llm_send`, лог `llm_send.log`) |
| Схема и примеры анализа домена | `.claude/skills/analyze-vault-domain/references/` |
| Путь к vault | `.env`, переменная `VAULT_PATH` |
| Запуск дашборда | `start-dashboard.bat` |
| Python-зависимости | `requirements.txt` |

## Правила репо

1. Домены vault анализируются только через Agent tool (`vault-domain-architect`, `vault-domain-analyzer`) или суб-агентом `llm` (правило 8). Файлы домена вручную в основном потоке не читать и не писать.
2. Не публиковать без явной команды пользователя.
3. `git add` только поимённо. Никогда `git add .` или `git add -A`.
4. `.env` не читать целиком, только `grep "^VAR=" .env`.
5. В чат не выводить сырой JSON анализа, только summary.
6. Временные файлы анализа живут в `.claude/temp_files/`, чистит `scripts/cleanup.py`.
7. Каждый `.md` файл vault учтён ровно один раз:
   - `metadata.total_files` + `projects.count` + `unclassified.count` + `filtered_out.count` = `metadata.all_files_count`; сумму проверяют `candidates_accounted` (`--verify`) и шапка дашборда;
   - `files_exist`: каждый путь существует в vault, иначе верный счёт скрывает опечатку;
   - `projects` - практика проектов; `unclassified` - домен не назначен (`reason`, `suggestion`); `filtered_out` - отсеянный шум, проверять, если фильтр мог отсечь ценное;
   - навигационные хабы (файлы из одних ссылок) мусором не считаются, идут в `unclassified`;
   - мусор (служебные папки, пустые заметки, `embed_only`) отсекает `--scan` до агента, агенту список приходит очищенным.
8. Тяжёлые шаги (код >100 строк, многошаговая разработка, анализ логов, классификация Фаз 1 и 2 `semantic-taxonomy`) делегируй суб-агенту `llm`:
   - один раз за сессию проверь `llm-ssh test` (без модели), при успехе отправляй по схеме из `SKILL.md` (см. `~/.llm/orchestrator.md`); результат интегрируй и проверяй локально;
   - каждый вызов `llm` с префиксом `LLM_MODEL=<алиас модели оркестратора>` (алиасы в `SKILL.md`); не полагаться на дефолт песочницы и `~/.llm/config`; исключение - `llm clean` и `llm-ssh test`;
   - нет связи или команды `llm` - работай через Agent tool. Отправка содержимого vault в песочницу разрешена: vault и подписка принадлежат пользователю.
9. Спека (`speckit-*` скиллы, `.specify/`, `specs/`) никогда не коммитится и не добавляется в git: это инструменты планирования, не часть релиза. Работа с ней локально, в отдельных сессиях.
10. Инкремент (`synthesize_taxonomy.py --merge`) работает от дельты. Dirty = `status` `new | changed` в `vault-scan.json` (`--scan --new-only` сверяет хеш с `content_hash` в `topic-graph.json`, для `projects`/`unclassified` - с `taxonomy.json`). Без графа прогон полный. Состояние - `taxonomy.json`, `domain-map.json`, граф.
    - Структура пересматривается по накопленным данным, без чтения старых текстов: `incremental.py domain-cards` (`domain-cards.json`) → `vault-domain-architect` в режиме инкремента → `patch.json` → `incremental.py apply-patch`. Тексты читаются только у дельты и записей `unclassified`/`projects`.
    - Глубокий анализ (`vault-domain-analyzer`, режим «добавление») - только дельта; старые записи берутся из `taxonomy.json`.
    - Фаза 3б: переоценка тем затронутых доменов, ответ проверяет `validate-topics`; незатронутые домены сохраняют темы как есть.
    - `build_topic_graph.py` строго после Фаз 2, 3 и 3б: нужны свежие `{domain_id}-analysis.json` и `{domain_id}-topics.validated.json`. `--merge` не запускается без графа новее `vault-scan.json` и `domain-map.json`.
    - `--merge` без LLM: раскладка доменов, парадигмы, `global_facets`, статистика и learning paths считаются кодом. `taxonomy.json` пишется только при выполненных инвариантах, кеш - после него.
    - `relevance_required` из `graph-dirty-domains.json` - новые файлы без свежего анализа домена; для них вызывается `vault-relevance-finder`, ответы применяются через `--apply-relevance`, который `graph-dirty-domains.json` не меняет. `dependency` из анализа и `related`/`dependency` от суб-агента `dirty_domains` связей не задают.
    - `--merge --full` пересчитывает прогресс всех доменов без dirty-фильтра и мёржит; `build_topic_graph.py --full` стирает связи, полученные от суб-агента.
    - Порядок и защиты - в `SKILL.md`.
11. Агенты работают без ограничения времени, ждать можно сколько угодно.
    - `tools.subagent({run_in_background: true})` - только так. Блокирующий `false` уносит агента вместе с программой по лимиту времени.
    - Не держи агента внутри программы, которая его запустила: прерывание программы останавливает и агента (`was stopped before it finished`). Запустил, программа вышла - собирай отдельным вызовом.
    - Готовность - файл на диске (`domain-map.json`, `{domain_id}-analysis.json`), а не текст ответа; дублируй через `list_agents`.
    - Пока агент работает, длинных вызовов инструментов не делай.
    - Агент молча остановился - не начинай заново: `send_message` вернёт его в контекст с уже собранным материалом.
12. Любое изменение файлов проекта - только в feature-ветке. `main` (`origin/main`) не редактировать, даже для «мелкой правки».
    - Перед первой правкой проверь `git branch`. Есть ветка, отличная от `main`, - работай в ней (`git switch feature/<имя>`), даже если её название про другую задачу; новую не создавай. Веток нет - `git switch -c feature/<описание> main`.
    - Незакоммиченные изменения чужой задачи в новую ветку не переносить без команды.
    - Коммит, пуш и вливание в `main` - только по явной команде пользователя. Ветку самостоятельно не мёржить и не удалять.
    - По команде на пуш: `git add` поимённо → коммит в ветке → `git switch main` → `git merge --no-ff feature/<имя>` (без pull-request) → `git push origin main`. После релиза: `git branch -d feature/<имя>` и `git push origin --delete feature/<имя>`.

## Архитектура

Скрипты ведут оркестрацию и не вызывают модель. LLM-агенты делают только экспертный анализ контента и не пишут код.

## Большая фича = план

Задача дольше часа оформляется планом в `.claude/plans/`: файл `YYYY-MM-DD-название-функции.md`, фазы со статусом `[ ]` или `[x]`, в конце итог - реализовано целиком или что осталось.

## Цикл в конце каждой фичи

1. Найди ошибки, докажи примером, исправь.
2. Проверь граничные случаи, входные данные, обработку ошибок.
3. Это реальная проблема проекта? Сверь с `.claude/plans/`.
4. Перечитай diff свежим взглядом, как будто видишь его впервые.
5. Security review: персональные данные и материалы третьих лиц.

## Рефлексия в конце сессии

Запиши в `.claude/retrospectives/YYYY-MM-DD_краткое-название.md`: задача, как решали, решили - да / нет / частично, что можно было лучше, что нового узнал о проекте.

## Язык

Всегда отвечай на русском. Только дефис, не длинное тире.
