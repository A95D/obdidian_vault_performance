---
name: semantic-taxonomy-builder
description: |
  Анализирует хранилище знаний и строит семантическую таксономию.
  На основе содержания файлов создаёт структурированную систему доменов,
  типов контента, learning paths и фасетной навигации.
  Выводит taxonomy.json для веб-дашборда.
  
  Триггеры: "построй таксономию", "создай семантическую систему",
  "анализ структуры vault", "taxonomy для хранилища".
  
  Не активируйся, если речь про редактирование одного файла,
  добавление одного тега или точечное улучшение — это другие навыки.
---

# Создание семантической таксономии для хранилища знаний

## Цель

Автоматизировать анализ хранилища знаний и создать:

1. **`taxonomy.json`** — полная таксономия всех файлов (домены, типы, иерархии, компоненты, learning paths)

**Основа**: анализ содержания файлов, а не существующих тегов.

---

## Подготовка: Выбор режима (ОБЯЗАТЕЛЬНО ПЕРЕД НАЧАЛОМ)

### Шаг 1: Проверка состояния

Существует ли файл `.claude/temp_files/taxonomy.json`?

- **Нет** → используй режим Full, переходи к фазе 1
- **Да** → спроси пользователя (см. шаг 2)

### Шаг 2: Выбор пользователя

Спроси: "Какой режим анализа?"

- **Full** — пересобрать таксономию с нуля (удалить старый taxonomy.json)
  - Когда: первый запуск, изменение VAULT_PATH, удаление файлов в vault, сомнения в целостности
  - Команда: `python cleanup.py --full`
  
- **Incremental** — добавить только новые файлы (сохранить старый taxonomy.json)
  - Когда: регулярное добавление заметок, быстрое обновление
  - Команда: `python cleanup.py` (без флагов)

Жди ответа перед дальнейшими действиями.

ЗАПРЕЩЕНО без выбора пользователя:
- Запускать cleanup.py, collect_vault_structure.py, удалять файлы

---

## Подготовка: Окружение

Переменные должны быть установлены в `.env`:

```env
VAULT_PATH=<путь-к-vault>
```

Проверка:
```bash
grep "^VAULT_PATH=" .env
```

Семантический анализ содержимого файлов (Фаза 0) и Фаза 2 — самые тяжёлые
части этого skill'а. По умолчанию выполняются субагентами
(`vault-topic-classifier`, `vault-domain-analyzer`) через Agent tool внутри
текущей сессии — отдельный LLM API-ключ для этого не требуется. Если в
системе настроен суб-агент `llm` (платформенная песочница, см.
`~/.llm/orchestrator.md`), эти две фазы делегируются ему — см.
раздел "Делегирование через llm" перед каждой из фаз.

### Проверка доступности llm (один раз перед Фазой 0.2)

```bash
llm-ssh test
```

Результат ("✓ Связь есть" / ошибка или команда не найдена) запомни на весь
текущий прогон skill'а — повторно перед Фазой 2 не проверяй. Если связи нет
или команда `llm` отсутствует — обе фазы выполняются как раньше, через
Agent tool, весь остальной текст про `llm` в этом файле пропускается.

---

## Поток выполнения (7 фаз)

```
Фаза 0 (Semantic Clustering)
    ├─ 0.1 prepare_clustering_batches.py [Python]  clustering-batches.json
    ├─ 0.2 vault-topic-classifier × N   [Agent tool, параллельно]  batch-*-topics.json
    └─ 0.3 cluster_from_topics.py       [Python]   vault-clusters.json
    ↓
Фаза 1 (Ingest)       [Python]           vault-structure-analysis.json
    ↓
Фаза 2 (Deep Read)    [Agent tool]       *-analysis.json (по доменам)
    ↓
Фазы 3-6 (Python)     [synthesize_taxonomy.py]
    ├─ Фаза 3: Выявление фасетов и иерархий
    ├─ Фазы 4-5: Генерация JSON → taxonomy.json
    └─ Фаза 6: Верификация и очистка
        ↓
cleanup.py (удаляет промежуточные файлы)
```

Домены (Фаза 1) определяются по содержимому файлов (Фаза 0), а не по
структуре папок: `collect_vault_structure.py` читает `vault-clusters.json`,
если он существует, и берёт домен файла оттуда. Если Фаза 0 не запускалась —
работает прежний folder-based fallback (домен = первый сегмент пути). Файлы
из `noise_files` (Фаза 0) полностью исключаются из `vault-structure-analysis.json`
и попадают в отдельную секцию `filtered_out` итоговой `taxonomy.json` — ни в
один домен, включая folder-based fallback, они не попадают.

### Таблица фаз

| Фаза | Инструмент | Выходной файл | Сохран. |
|------|-----------|---|---|
| 0.1 Batching | Python | clustering-batches.json | Временный |
| 0.2 Topic Classify | Agent tool | batch-{id}-topics.json | Временный |
| 0.3 Clustering | Python | vault-clusters.json | Временный |
| 1. Ingest | Python | vault-structure-analysis.json | Временный |
| 2. Deep Read | Agent tool | {domain}-analysis.json | Временный |
| 3. Synthesis | Python (в памяти) | — | — |
| 4-5. JSON Generation | Python | taxonomy.json | ✓ Финальный |
| 6. Validation & Cleanup | Python | — | — |

**Финальные артефакты**: `.claude/temp_files/taxonomy.json`

---

## Фаза 0: Семантическая кластеризация содержимого

Три шага: скрипт (подготовка) → субагенты (классификация, параллельно) →
скрипт (слияние и кластеризация). Семантический анализ содержимого — это
всегда работа субагента `vault-topic-classifier` через Agent tool, никогда
не прямой API-вызов из скрипта.

### Шаг 0.1: Подготовка батчей

```bash
python .claude/skills/semantic-taxonomy/scripts/prepare_clustering_batches.py
```

Сканирует все `.md` файлы vault'а и сначала прогоняет их через дешёвый
эвристический пре-фильтр шума (без LLM): файлы из служебных папок
(`templates/`, `attachments/`, `.trash/` и т.п.), пустые заметки и файлы
только с frontmatter отсеиваются сразу и в батчи на классификацию не
попадают — они уже мусор, незачем тратить на них Фазу 0.2 и Фазу 2. Затем
для оставшихся файлов сверяется content-hash с `semantic-analysis-cache.json`
— файлы с неизменившимся хешем в батчи тоже не попадают (экономия — не идут
на повторную классификацию). Новые/изменившиеся файлы группируются в батчи
по 15 файлов.

Выход: `.claude/temp_files/clustering-batches.json` — поля `batches`,
`cached_files`, `cached_file_count`, `prefiltered_files`,
`prefiltered_count`, `files_to_analyze`, `total_files`.

Если `files_to_analyze == 0` — все файлы уже в кеше, шаг 0.2 пропускается,
сразу переходи к шагу 0.3.

### Шаг 0.2: Классификация тем (Agent tool)

### ⚠️ КРИТИЧЕСКИ ВАЖНО: способ вызова

- **ОБЯЗАТЕЛЬНО**: используй инструмент **Agent** с `subagent_type: "vault-topic-classifier"`
- **ОБЯЗАТЕЛЬНО**: все вызовы для всех батчей — **одним сообщением**
  (несколько tool_use блоков параллельно), как в Фазе 2 для
  `vault-domain-analyzer`. Последовательные вызовы недопустимы.
- **После отправки**: в основной чат попадает только короткая 3-строчная
  сводка на батч — никакого JSON, никаких Read/Write в диалог.

**ЗАПРЕЩЕНО**: самому читать файлы vault'а или писать `batch-*-topics.json`
в основном потоке (orchestrator). Это задача только субагента
`vault-topic-classifier` или (если доступен) суб-агента `llm` по схеме ниже.

### Делегирование через llm (если `llm-ssh test` прошёл)

Батчи обрабатываются последовательно (песочница `llm` — одна сессия, не
параллельные вызовы, в отличие от Agent tool). Для каждого батча:

```bash
grep "^VAULT_PATH=" .env                      # взять путь к vault
llm put <VAULT_PATH>/<файлы_батча>            # закинуть файлы батча в песочницу
llm put .claude/agents/vault-topic-classifier.md   # ТЗ (схема и правила классификации)
llm --fresh "Выполни инструкцию из vault-topic-classifier.md для batch_id=\"{batch_id}\", файлы см. в отправленных. Запиши результат в batch-{batch_id}-topics.json по описанной схеме."
llm get batch-{batch_id}-topics.json          # забрать результат
```

Полученный `batch-{batch_id}-topics.json` положить в `.claude/temp_files/` —
дальше он используется в шаге 0.3 точно так же, как если бы его написал
`vault-topic-classifier` через Agent tool. Если `llm` вернул невалидный JSON
или не создал файл — для этого батча выполнить fallback через Agent tool
(`vault-topic-classifier`), остальные батчи это не блокирует.

В диалог — та же 3-строчная сводка на батч, что и для Agent tool, без
сырого JSON.

Как выполнять (если `llm` недоступен — обычный путь через Agent tool):

1. Прочитать `clustering-batches.json` — список батчей
2. Для каждого батча запустить Agent tool (в одном сообщении параллельно):

```python
for batch in batches:
    Agent({
        description: f"Классификация тем: {batch['batch_id']}",
        subagent_type: "vault-topic-classifier",
        prompt: f"""
        batch_id: "{batch['batch_id']}"
        files: {batch['files']}
        """
    })
```

3. Ожидать завершения всех субагентов — каждый запишет свой
   `batch-{batch_id}-topics.json`

### Шаг 0.3: Слияние и кластеризация

```bash
python .claude/skills/semantic-taxonomy/scripts/cluster_from_topics.py
```

Читает `clustering-batches.json` + все `batch-*-topics.json` + кеш,
обновляет кеш новыми результатами, строит кластеры по primary_topic
(multi-topic файлы с низкой уверенностью пытаются присоединиться по
пересечению secondary_topics). Файлы с confidence_score ниже порога (0.5)
или без семантического совпадения попадают в `unclassified_files` — это
темы, которым не нашлось пары, но не мусор: такие файлы всё равно получат
домен через folder-based fallback в Фазе 1.

Отдельно собирается `noise_files` — объединение файлов, отсеянных
эвристикой на шаге 0.1 (`prefiltered_files`), и файлов, которые сам
классификатор пометил как `read_error`/`empty_or_too_short` в поле
`skipped`. Это и есть настоящий мусор: в отличие от `unclassified_files`,
файлы из `noise_files` в Фазе 1 полностью исключаются из
`vault-structure-analysis.json` и никогда не получают домен ни
семантически, ни через folder-based fallback.

Выход: `.claude/temp_files/vault-clusters.json` — с полями
`unclassified_files` и `noise_files` как раздельными категориями.

### Проверка

> Чеклист ниже — для внутренней проверки перед переходом к следующей фазе.
> В диалог выводить не построчный чеклист, а одну итоговую строку по фазе
> (например: "Фаза 0: семантическая кластеризация — ок").

- [ ] Файл `.claude/temp_files/vault-clusters.json` создан
- [ ] `statistics.total_files` совпадает с количеством .md файлов в vault
- [ ] `statistics.clustered_files + statistics.unclassified_count + statistics.noise_count == total_files`
- [ ] Каждый кластер содержит ≥1 файл, `cluster_id` в формате `^[a-z0-9\-]+$`
- [ ] `quality_metrics.coverage_percent` разумен для валидации через dashboard.html (SC-004)

---

## Фаза 1: Подготовка данных (Ingest)

### Команды

Режим Full:
```bash
python .claude/skills/semantic-taxonomy/scripts/collect_vault_structure.py
```

Режим Incremental:
```bash
python .claude/skills/semantic-taxonomy/scripts/collect_vault_structure.py --new-only
```

### Что происходит

1. Сканирование всех .md файлов в vault рекурсивно
2. Определение структуры папок (предварительные домены)
3. Выходной файл: `.claude/temp_files/vault-structure-analysis.json`

Содержит: `metadata` (total_files, total_domains), `domains` (domain_id, domain_name, files), `all_files` (плоский список).

### Проверка

> Чеклист ниже — для внутренней проверки перед переходом к следующей фазе.
> В диалог выводить не построчный чеклист, а одну итоговую строку по фазе
> (например: "Фаза 1: сбор структуры — ок").

- [ ] Файл `.claude/temp_files/vault-structure-analysis.json` создан
- [ ] `total_files` совпадает с ожиданием
- [ ] `total_domains` соответствует числу папок верхнего уровня
- [ ] `domains` содержит список с полями domain_id, domain_name, files
- [ ] Все файлы распределены без пропусков
- [ ] Для incremental-режима: если новых файлов нет, domains пуст и total_files = 0 (дальше не запускать фазу 2)

---

## Фаза 2: Анализ содержания (Deep Read)

### Инструмент

Агент `vault-domain-analyzer` (`.claude/agents/vault-domain-analyzer.md`) запускается параллельно через **Agent tool** (`subagent_type: "vault-domain-analyzer"`) для каждого домена.

### ⚠️ КРИТИЧЕСКИ ВАЖНО: способ вызова

- **ОБЯЗАТЕЛЬНО**: используй инструмент **Agent** с `subagent_type: "vault-domain-analyzer"`
- **ОБЯЗАТЕЛЬНО**: все вызовы Agent tool для всех доменов должны быть отправлены **в одном сообщении** (несколько tool_use блоков параллельно)
  - Последовательные вызовы недопустимы — каждый ждёт завершения предыдущего
- **После отправки**: в основной чат должна попасть только сводка (по 4 строки текста на домен)
  - Никакого JSON, никаких промежуточных статусов Read/Write в диалог

**ЗАПРЕЩЕНО в Фазе 2**: самостоятельно вызывать Read/Write/Grep/Glob для файлов
доменов или для `{domain_id}-analysis.json` в основном потоке (orchestrator).
Анализ содержимого файлов домена и запись результата — это задача **только**
субагента `vault-domain-analyzer`, а не оркестратора.

Чек-лист перед началом Фазы 2 (пройди мысленно, прежде чем делать что-либо ещё):
1. Я собираюсь вызвать инструмент **Agent**, а не Read/Write? Если нет — стоп.
2. У вызова указан `subagent_type: "vault-domain-analyzer"`?
3. Все домены отправлены одним сообщением, параллельно?

Если в какой-то момент Фазы 2 обнаруживаешь, что читаешь файл домена или
пишешь `{domain_id}-analysis.json` напрямую (не через Agent tool) — это ошибка:
останови эти действия и перезапусти шаг через Agent tool.

### Делегирование через llm (если `llm-ssh test` прошёл на шаге Фазы 0)

Домены обрабатываются последовательно (песочница `llm` — одна сессия). Для
каждого домена:

```bash
grep "^VAULT_PATH=" .env                              # взять путь к vault
llm put .claude/temp_files/vault-structure-analysis.json
llm put <VAULT_PATH>/<файлы_домена>                   # файлы этого domain_id
llm put .claude/agents/vault-domain-analyzer.md       # ТЗ (схема анализа)
llm put .claude/skills/analyze-vault-domain/references/REFERENCE.md
llm --fresh "Выполни инструкцию из vault-domain-analyzer.md для domain_id=\"{domain_id}\", domain_name=\"{domain_name}\". Файлы домена уже отправлены. Запиши результат в {domain_id}-analysis.json по схеме из REFERENCE.md."
llm get {domain_id}-analysis.json                     # забрать результат
```

Полученный `{domain_id}-analysis.json` положить в `.claude/temp_files/` — он
используется в Фазах 3-6 точно так же, как если бы его написал
`vault-domain-analyzer` через Agent tool. Если `llm` вернул невалидный JSON,
не создал файл или сводка не сходится (нет `domain_summary`) — для этого
домена выполнить fallback через Agent tool (`vault-domain-analyzer`),
остальные домены это не блокирует.

В диалог — та же 4-строчная сводка на домен, что и для Agent tool, без
сырого JSON.

### Как выполнять (если `llm` недоступен — обычный путь через Agent tool)

1. Прочитать `vault-structure-analysis.json` — получить список доменов и путей файлов
2. Для каждого домена запустить Agent tool (в одном сообщении параллельно):

```python
for domain in domains:
    Agent({
        description: f"Анализ домена: {domain['domain_name']}",
        subagent_type: "vault-domain-analyzer",
        prompt: f"""
        domain_id: "{domain['domain_id']}"
        domain_name: "{domain['domain_name']}"
        """
    })
```

Список файлов домена в промпт не передаётся — агент сам читает
`vault-structure-analysis.json` и находит свой домен (см. Шаг 1 в
`vault-domain-analyzer.md`). Это не даёт полному списку путей домена
осесть в Messages оркестратора: он уходит в изолированный контекст
субагента.

3. Ожидать завершения всех субагентов — каждый запишет свой `{domain_id}-analysis.json`

### Выходной файл

Каждый domain-analysis.json содержит:
- Список файлов домена с полями: essence, role, key_concepts, mentions, difficulty_level
- Зависимости между файлами
- Фасеты (type, paradigm, component, difficulty)

Пример анализа одного файла: см. `references/examples.md#фаза-2-пример-анализа-одного-файла`

### Проверка

> Чеклист ниже — для внутренней проверки перед переходом к следующей фазе.
> В диалог выводить не построчный чеклист, а одну итоговую строку по фазе
> (например: "Фаза 2: анализ содержания — ок").

- [ ] Все файлы {domain}-analysis.json созданы в .claude/temp_files/
- [ ] Количество файлов -analysis.json = количество доменов
- [ ] Каждый файл имеет essence, role, key_concepts
- [ ] Выявлены dependencies между файлами
- [ ] Ответы subagentов в диалоге — ровно 4 строки текста (без кода, JSON, таблиц)
- [ ] JSON файлы валидны с полем domain_summary

---

## Фазы 3-6: Синтез таксономии

### Команды

Режим Full:
```bash
python .claude/skills/semantic-taxonomy/scripts/synthesize_taxonomy.py
```

Режим Incremental (мёржить с существующей):
```bash
python .claude/skills/semantic-taxonomy/scripts/synthesize_taxonomy.py --merge
```

Скрипт выполняет фазы 3-6 последовательно внутри себя.

---

## Фаза 3: Выявление фасетов и иерархий

### 3.1 Таблица фасетов

На основе результатов Фазы 2 выявляются общие измерения:

| Фасет | Примеры значений | Примечание |
|---|---|---|
| **domain** | dwh, data-vault, requirements, infrastructure, learning-project | по предметным областям |
| **type** | concept, architecture, pattern, methodology, reference, guide, checklist, case-study | тип содержимого |
| **paradigm** | inmon, kimball, data-vault-2.0, medallion, beam | парадигма (не всегда применима) |
| **components** | fact-table, dimension-table, hub, link, satellite, pit-table, bridge-table | технические элементы |
| **technologies** | postgresql, dbt, docker, git, python, sql | инструменты |
| **difficulty** | beginner, intermediate, advanced | сложность материала |

### 3.2 Иерархии (DAG)

Для каждого домена строится направленный ациклический граф. Пример: см. `references/examples.md#фаза-32-пример-dag-иерархии`

> Чеклист ниже — для внутренней проверки. В диалог — одна итоговая строка
> (например: "Фаза 3: фасеты и DAG — ок").

Проверка:
- [ ] Для каждого домена построен DAG
- [ ] Граф ациклический (нет циклических зависимостей)
- [ ] Каждый узел имеет ≤2 родителей

### 3.3 Learning Paths

Для каждого домена определяются три маршрута обучения (beginner, intermediate, advanced).

Каждый path — логичная вертикальная цепочка от простого к сложному.

Пример: см. `references/examples.md#фаза-33-пример-learning-paths`

> Чеклист ниже — для внутренней проверки. В диалог — одна итоговая строка
> (например: "Фаза 3: learning paths — ок").

Проверка:
- [ ] Для каждого домена есть 3 learning path'а
- [ ] Каждый path логичен (зависимости соблюдены, сложность растёт)
- [ ] Каждый файл домена попадает в один из path'ов

---

## Фазы 4-5: Генерация финального JSON

На основе Фаз 2-3 создаётся единая структура `taxonomy.json`.

**Выходной файл**: `.claude/temp_files/taxonomy.json` (ФИНАЛЬНЫЙ)

**Содержимое**:
- Метаданные анализа (дата, путь, total_files, total_domains)
- Все файлы по доменам с полями: id, path, title, essence, type, paradigm, components, technologies, difficulty, related, dependencies, practical_importance, status
- Learning paths для каждого домена (beginner/intermediate/advanced с файлами и estimated_hours)
- Глобальные фасеты (все возможные значения для каждого измерения)
- Статистика (распределения по доменам, типам, сложности, парадигмам)

**Полный пример**: см. `references/examples.md#фазы-4-5-taxonomyjson-полная-структура`

> Чеклист ниже — для внутренней проверки. В диалог — одна итоговая строка
> (например: "Фазы 4-5: генерация JSON — ок").

Проверка:
- [ ] Файл `.claude/temp_files/taxonomy.json` создан
- [ ] JSON валиден и может быть распарсен
- [ ] Все файлы включены (0 пропусков)
- [ ] Каждый файл имеет id, path, title, essence, type
- [ ] Каждый домен имеет learning paths (beginner, intermediate, advanced)
- [ ] global_facets содержит все возможные значения
- [ ] statistics согласуется с файлами (сумма файлов по доменам = total_files)

---

## Фаза 6: Верификация и очистка

Встроена в `synthesize_taxonomy.py`:
- Проверка целостности JSON структуры
- Проверка консистентности статистики
- Проверка наличия всех обязательных полей

После верификации запусти очистку:

```bash
python .claude/skills/semantic-taxonomy/scripts/cleanup.py
```

Удаляет временные файлы анализа (Фазы 1-5), сохраняет финальные артефакты.

### Финальная проверка

> Чеклист ниже — для внутренней проверки перед завершением. В диалог — одна
> итоговая строка (например: "Фаза 6: верификация и очистка — ок").

- [ ] Все файлы vault учтены (0 пропусков)
- [ ] Каждый файл в ровно одном домене
- [ ] Нет циклических зависимостей
- [ ] Все related и dependencies ссылки указывают на существующие файлы
- [ ] Learning paths логичны (вертикальные цепочки)
- [ ] Статистика согласуется с файлами
- [ ] Удалены временные файлы (осталось: taxonomy.json)

---

## Ограничения режима Incremental

Режим Incremental не отслеживает:
- Удаленные файлы из vault (останутся в taxonomy.json)
- Переименованные файлы (появятся как новые)
- Перемещённые файлы между доменами

Для полной синхронизации используйте режим Full.

---

## Итого

**Входные данные**: папка с .md файлами (Obsidian Vault по VAULT_PATH в .env)

**Выходные файлы** (в `.claude/temp_files/`):
- `taxonomy.json` — источник данных для веб-дашборда, фасетной навигации, рекомендаций

**Промежуточные файлы** (удаляются на Фазе 6):
- clustering-batches.json
- batch-{id}-topics.json
- vault-clusters.json
- vault-structure-analysis.json
- {domain}-analysis.json
- другие временные артефакты

**Переживает cleanup** (кроме `--full`):
- semantic-analysis-cache.json — кеш LLM-анализа Фазы 0
