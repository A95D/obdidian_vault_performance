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

Шаг классификации внутри Фазы 1 и Фаза 2 — самые тяжёлые части этого
skill'а. По умолчанию выполняются субагентами (`vault-topic-classifier`,
`vault-domain-analyzer`) через Agent tool внутри текущей сессии — отдельный
LLM API-ключ для этого не требуется. Если в системе настроен суб-агент `llm`
(платформенная песочница, см. `~/.llm/orchestrator.md`), эти два шага
делегируются ему — см. раздел "Делегирование через llm" перед каждым из них.
Для шага классификации Фазы 1 разницы между путём через Agent tool и через
`llm` больше нет — оба последовательные, батч за батчем.

### Проверка доступности llm (один раз перед шагом классификации Фазы 1)

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
Фаза 1 (Ingest + Classify)
    ├─ 1.1 collect_vault_structure.py --prepare-batches  [Python]  clustering-batches.json, domain-vocabulary.json
    ├─ 1.2 vault-topic-classifier × N  [Agent tool, ПОСЛЕДОВАТЕЛЬНО]  batch-*-assignments.json + domain-vocabulary.json (накопление)
    └─ 1.3 collect_vault_structure.py --merge  [Python]  vault-structure-analysis.json
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

Домены определяются по содержимому файлов (шаг 1.2), а не по структуре
папок: `collect_vault_structure.py --merge` берёт домен файла из
`batch-*-assignments.json` (свежая классификация) и кеша (неизменившиеся
файлы). Файлы, не получившие уверенного домена (`unclassified`), всё равно
получают домен через folder-based fallback. Файлы из `noise_files`
(эвристический пре-фильтр шага 1.1 + `skipped` шага 1.2) полностью
исключаются из `vault-structure-analysis.json` и попадают в отдельную
секцию `filtered_out` итоговой `taxonomy.json` — ни в один домен, включая
folder-based fallback, они не попадают.

### Таблица фаз

| Фаза | Инструмент | Выходной файл | Сохран. |
|------|-----------|---|---|
| 1.1 Batching | Python | clustering-batches.json, domain-vocabulary.json | Временный |
| 1.2 Classify + Cluster | Agent tool, последовательно | batch-{id}-assignments.json, domain-vocabulary.json | Временный |
| 1.3 Merge | Python | vault-structure-analysis.json | Временный |
| 2. Deep Read | Agent tool | {domain}-analysis.json | Временный |
| 3. Synthesis | Python (в памяти) | — | — |
| 4-5. JSON Generation | Python | taxonomy.json | ✓ Финальный |
| 6. Validation & Cleanup | Python | — | — |

**Финальные артефакты**: `.claude/temp_files/taxonomy.json`

---

## Фаза 1: Сбор структуры и сквозная классификация доменов (Ingest + Classify)

Три шага: скрипт (подготовка батчей) → субагент (классификация и
кластеризация по доменам, один тип AI-вызова, батч за батчем) → скрипт
(слияние). Семантический анализ содержимого — это всегда работа субагента
`vault-topic-classifier` через Agent tool, никогда не прямой API-вызов из
скрипта.

Vault пользователя может содержать сотни файлов — один агент не прочитает
всё за один вызов. Батчи остаются необходимостью, но, в отличие от прежней
Фазы 0, батчи обрабатываются **последовательно**, не параллельно: список
доменов (`domain-vocabulary.json`) накапливается по ходу — каждый следующий
батч получает уже существующий словарь и обязан сначала проверить, подходит
ли существующий домен, прежде чем заводить новый. Это осознанный компромисс
параллелизма ради консистентности названий доменов между батчами.

### Шаг 1.1: Подготовка батчей

Режим Full:
```bash
python .claude/skills/semantic-taxonomy/scripts/collect_vault_structure.py --prepare-batches
```

Режим Incremental:
```bash
python .claude/skills/semantic-taxonomy/scripts/collect_vault_structure.py --prepare-batches --new-only
```

Сканирует все `.md` файлы vault'а и сначала прогоняет их через дешёвый
эвристический пре-фильтр шума (без LLM): файлы из служебных папок
(`templates/`, `attachments/`, `.trash/` и т.п.), пустые заметки и файлы
только с frontmatter отсеиваются сразу и в батчи на классификацию не
попадают — они уже мусор, незачем тратить на них шаг 1.2 и Фазу 2. Затем
для оставшихся файлов сверяется content-hash с `semantic-analysis-cache.json`
— файлы с неизменившимся хешем в батчи тоже не попадают (экономия — не идут
на повторную классификацию). Новые/изменившиеся файлы группируются в батчи
по 15 файлов, порядок батчей фиксированный (по пути файла).

В режиме `--new-only` дополнительно: `domain-vocabulary.json` сидируется из
доменов существующей `taxonomy.json` (чтобы батчи продолжали использовать
уже известные domain_id и искали подходящий домен среди них в первую
очередь). Создание нового домена при отсутствии подходящего работает
одинаково в обоих режимах (Full и Incremental) — см. шаг 1.2. В режиме Full
`domain-vocabulary.json` создаётся пустым.

Выход: `.claude/temp_files/clustering-batches.json` — поля `batches`,
`cached_files`, `cached_file_count`,
`prefiltered_files`, `prefiltered_count`, `files_to_analyze`, `total_files`.
И `.claude/temp_files/domain-vocabulary.json` — стартовый словарь.

Если `files_to_analyze == 0` — все файлы уже в кеше, шаг 1.2 пропускается,
сразу переходи к шагу 1.3.

### Шаг 1.2: Классификация и кластеризация доменов (Agent tool)

### ⚠️ КРИТИЧЕСКИ ВАЖНО: способ вызова

- **ОБЯЗАТЕЛЬНО**: используй инструмент **Agent** с `subagent_type: "vault-topic-classifier"`
- **ОБЯЗАТЕЛЬНО**: вызовы для батчей — **строго последовательно**, один за
  другим, каждый следующий вызывается только после завершения предыдущего
  (в отличие от Фазы 2, где `vault-domain-analyzer` запускается одним
  сообщением параллельно). Параллельные вызовы здесь недопустимы — агент
  следующего батча должен увидеть домены, уже добавленные предыдущим.
- **После каждого вызова**: в основной чат попадает только короткая
  3-строчная сводка на батч — никакого JSON, никаких Read/Write в диалог.

**ЗАПРЕЩЕНО**: самому читать файлы vault'а или писать
`batch-*-assignments.json`/`domain-vocabulary.json` в основном потоке
(orchestrator). Это задача только субагента `vault-topic-classifier` или
(если доступен) суб-агента `llm` по схеме ниже.

### Делегирование через llm (если `llm-ssh test` прошёл)

Путь через `llm` не отличается от Agent tool по порядку вызовов — оба
последовательные. Для каждого батча (по порядку):

```bash
grep "^VAULT_PATH=" .env                      # взять путь к vault
llm put <VAULT_PATH>/<файлы_батча>            # закинуть файлы батча в песочницу
llm put .claude/temp_files/domain-vocabulary.json  # текущий словарь доменов
llm put .claude/agents/vault-topic-classifier.md   # ТЗ (схема и правила классификации)
llm --fresh "Выполни инструкцию из vault-topic-classifier.md для batch_id=\"{batch_id}\", файлы см. в отправленных. Запиши результат в batch-{batch_id}-assignments.json и обнови domain-vocabulary.json по описанной схеме."
llm get batch-{batch_id}-assignments.json     # забрать результат
llm get domain-vocabulary.json                # забрать обновлённый словарь - вход для СЛЕДУЮЩЕГО батча
```

Обновлённый `domain-vocabulary.json` обязательно положить обратно в
`.claude/temp_files/` **и в песочницу `llm`** перед следующим батчем — иначе
словарь не накопится. Если `llm` вернул невалидный JSON или не создал
файл — для этого батча выполнить fallback через Agent tool
(`vault-topic-classifier`), остальные батчи это не блокирует.

В диалог — та же 3-строчная сводка на батч, что и для Agent tool, без
сырого JSON.

Как выполнять (если `llm` недоступен — обычный путь через Agent tool):

1. Прочитать `clustering-batches.json` — список батчей, по порядку
2. Для каждого батча по очереди (ждать завершения перед следующим):

```python
for batch in batches:  # строго последовательно, не параллельно
    Agent({
        description: f"Классификация домена: {batch['batch_id']}",
        subagent_type: "vault-topic-classifier",
        prompt: f"""
        batch_id: "{batch['batch_id']}"
        files: {batch['files']}
        """
    })
    # дождаться ответа, только потом переходить к следующему batch
```

3. Каждый агент читает/обновляет `domain-vocabulary.json` сам и пишет свой
   `batch-{batch_id}-assignments.json`

### Шаг 1.3: Слияние

Режим Full:
```bash
python .claude/skills/semantic-taxonomy/scripts/collect_vault_structure.py --merge
```

Режим Incremental:
```bash
python .claude/skills/semantic-taxonomy/scripts/collect_vault_structure.py --merge --new-only
```

Читает `clustering-batches.json` + все `batch-*-assignments.json` +
`domain-vocabulary.json` + кеш, обновляет кеш новыми результатами
(`content_hash -> {domain_id, essence, key_concepts}`), строит
`path -> domain_id` напрямую из решений агента (без отдельной кластеризации
скриптом — она уже выполнена в шаге 1.2). Файлы из `unclassified`
(`low_confidence`) не мусор — получают домен через
folder-based fallback. Файлы из `skipped` (`read_error`/
`empty_or_too_short`) объединяются с `prefiltered_files` шага 1.1 в
`noise_files` — настоящий мусор, полностью исключается из доменов.

Выход: `.claude/temp_files/vault-structure-analysis.json`. Содержит:
`metadata` (total_files, total_domains), `domains` (domain_id, domain_name,
files), `all_files` (плоский список), `filtered_out` (noise_files).

### Проверка

> Чеклист ниже — для внутренней проверки перед переходом к следующей фазе.
> В диалог выводить не построчный чеклист, а одну итоговую строку по фазе
> (например: "Фаза 1: сбор структуры и классификация доменов — ок").

- [ ] Файл `.claude/temp_files/vault-structure-analysis.json` создан
- [ ] `domain-vocabulary.json` не содержит двух доменов с идентичным/почти
  идентичным `description` (признак того, что агент не сверился со
  словарём перед созданием нового домена)
- [ ] `total_files` совпадает с ожиданием
- [ ] `domains` содержит список с полями domain_id, domain_name, files
- [ ] Все файлы распределены без пропусков (домен или `filtered_out`)
- [ ] Для incremental-режима: если новых файлов нет, domains пуст и total_files = 0 (дальше не запускать фазу 2)

---

## Фаза 2: Анализ содержания (Deep Read)

### Выбор доменов: Full vs Incremental режим

**Перед запуском агентов проверь режим:**

- **Режим `--full`** (или режим Full в интерактивном выборе):
  - Анализировать **ВСЕ** домены из `vault-structure-analysis.json`
  - Игнорировать `graph-dirty-domains.json` если он существует

- **Режим `--merge`** (или Incremental в интерактивном выборе):
  - Проверить существует ли файл `.claude/temp_files/graph-dirty-domains.json` (результат Фазы 1.4)
  - Если файл **существует и `dirty_domains` не пуст**:
    ```bash
    python3 -c "import json; d=json.load(open('.claude/temp_files/graph-dirty-domains.json')); print('dirty_domains:', d.get('dirty_domains', []))"
    ```
    → Анализировать **только эти домены** (ускорение, пересчитаны только изменённые)
  - Если файл **отсутствует или `dirty_domains` пуст**:
    → **Пропустить Фазу 2 целиком**, перейти сразу к Фазе 1.4 (если не запущена) и затем Фазы 3-6

**Зачем**: в incremental режиме не переанализируем домены, где ничего не изменилось — экономия времени и токенов.

### Инструмент

Агент `vault-domain-analyzer` (`.claude/agents/vault-domain-analyzer.md`) запускается параллельно через **Agent tool** (`subagent_type: "vault-domain-analyzer"`) для каждого домена (или подмножества в incremental режиме).

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
1. Я проверил режим (full vs merge) и определил список доменов? Если нет — стоп.
2. Если merge-режим: я проверил `graph-dirty-domains.json` и отсеял non-dirty домены? 
3. Я собираюсь вызвать инструмент **Agent**, а не Read/Write? Если нет — стоп.
4. У вызова указан `subagent_type: "vault-domain-analyzer"`?
5. Все (отфильтрованные) домены отправлены одним сообщением, параллельно?

Если в какой-то момент Фазы 2 обнаруживаешь, что читаешь файл домена или
пишешь `{domain_id}-analysis.json` напрямую (не через Agent tool) — это ошибка:
останови эти действия и перезапусти шаг через Agent tool.

### Делегирование через llm (если `llm-ssh test` прошёл на шаге классификации Фазы 1)

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

## Фаза 1.4: Обновление графа тем и доменов

Добавлена фичей `003-incremental-progress-graph`. Выполняется после Фазы 2
(или сразу после шага 1.3 в incremental-режиме, если Фаза 2 для новых файлов
не запускалась — см. ниже), перед Фазами 3-6.

### Команды

Режим Full (полная пересборка графа):
```bash
python .claude/skills/semantic-taxonomy/scripts/build_topic_graph.py --full
```

Режим Incremental (diff поверх существующего графа, по умолчанию):
```bash
python .claude/skills/semantic-taxonomy/scripts/build_topic_graph.py
```

### Что происходит

1. Скрипт строит/обновляет `.claude/temp_files/topic-graph.json` — трёхуровневый
   граф домен → тема → файл (узлы + рёбра `member_of`), сравнивая content_hash
   файлов с хешами, уже записанными в графе.
2. Новые и изменившиеся файлы помечают свой домен (и, если тема уже известна,
   тему) как **dirty** — список пишется в
   `.claude/temp_files/graph-dirty-domains.json` (одноразовый артефакт этого
   прогона, вход для Фаз 3-6).
3. Ревизия графа инкрементируется, история пишется в
   `.claude/temp_files/graph-revision-log.json`.

**Важно про новые файлы**: essence/key_concepts для только что добавленной
заметки берутся из классификации шага 1.2 (`semantic-analysis-cache.json`),
**не** из полноценного `vault-domain-analyzer` — `vault-domain-analyzer`
по-прежнему вызывается только на Фазе 2 для реально новых/изменившихся
доменов, если нужен глубокий анализ их файлов целиком. Если для домена не
было причины запускать Фазу 2 (например, единственная новая заметка не
требует полного переанализа домена), узел графа для неё всё равно строится —
просто с более лёгким источником essence/key_concepts.

### Поиск related/dependencies для новых файлов

Для каждого **нового** файла (не изменившегося — только для `new_file_ids`
из `graph-dirty-domains.json`) оркестратор вызывает субагента
`vault-relevance-finder` (**Agent tool**, `subagent_type: "vault-relevance-finder"`,
модель Haiku) с компактным входом: `essence`/`key_concepts` новой заметки +
сжатые узлы (`node_id`, `title`, `key_concepts`, `type`) её dirty-домена, без
доступа к файлам vault.

- Один вызов Agent tool на один новый файл; при нескольких новых файлах —
  все вызовы в одном сообщении (параллельно), по аналогии с Фазой 2.
- Ответ — строго `{"related": [...], "dependencies": [...]}`; любые `node_id`,
  не входящие в переданный список узлов домена, отбрасываются (не
  применяются как рёбра), это логируется в stderr, а не в диалог.
- Если вызов на Haiku не удался — повторить тот же вызов с `model: inherit`.
  Если и это не удалось — `related`/`dependencies` для заметки остаются
  пустыми, пересчёт остальных dirty-доменов не блокируется.
- **ЗАПРЕЩЕНО** в диалог выводить сырой JSON графа или ответ субагента
  целиком — только итоговую сводку по числу новых рёбер (правило 5 CLAUDE.md).

### Проверка

- [ ] `.claude/temp_files/topic-graph.json` создан/обновлён, `revision`
  увеличился на 1
- [ ] `.claude/temp_files/graph-dirty-domains.json` создан, `dirty_domains`
  соответствует доменам реально новых/изменившихся файлов
- [ ] Для каждого `new_file_id` был вызван `vault-relevance-finder` (или его
  `model: inherit` повтор), и только для них
- [ ] Для `changed_file_ids` без тематических изменений `vault-relevance-finder`
  НЕ вызывался (эта фичa только про новые файлы, не про пересчёт existing-связей)
- [ ] Ни один raw JSON графа/ответа субагента не попал в диалог

---

## Фазы 3-6: Синтез таксономии

### Команды

Режим Full:
```bash
python .claude/skills/semantic-taxonomy/scripts/synthesize_taxonomy.py
```

Режим Incremental (мёржить с существующей, сужено до dirty-доменов из Фазы 1.4):
```bash
python .claude/skills/semantic-taxonomy/scripts/synthesize_taxonomy.py --merge
```

Режим Incremental с полным пересчётом прогресса всех доменов (игнорирует
dirty-фильтр графа, но по-прежнему мёржит, а не пересобирает `taxonomy.json`
с нуля):
```bash
python .claude/skills/semantic-taxonomy/scripts/synthesize_taxonomy.py --merge --full
```

Скрипт выполняет фазы 3-6 последовательно внутри себя. В режиме `--merge` без
`--full`, если на этом прогоне был создан `.claude/temp_files/graph-dirty-domains.json`,
пересчёт Фаз 3-5 сужается до доменов из `dirty_domains` — остальные секции
`taxonomy.json` остаются побайтово нетронутыми. Если `dirty_domains` пуст —
скрипт завершается без изменений `taxonomy.json` (аналогично проверке Фазы 1
"новых файлов нет").

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
- domain-vocabulary.json
- batch-{id}-assignments.json
- vault-structure-analysis.json
- {domain}-analysis.json
- другие временные артефакты

**Переживает cleanup** (кроме `--full`):
- semantic-analysis-cache.json — кеш LLM-анализа Фазы 1
