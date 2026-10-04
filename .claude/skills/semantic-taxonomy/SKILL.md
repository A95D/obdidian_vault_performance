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

Шаг 1.2 (проектирование карты доменов) и Фаза 2 (глубокий анализ) —
самые тяжёлые части этого skill'а. По умолчанию выполняются субагентами
(`vault-domain-architect`, `vault-domain-analyzer`) через Agent tool внутри
текущей сессии — отдельный LLM API-ключ для этого не требуется. Если в системе
настроен суб-агент `llm` (платформенная песочница, см. `~/.llm/orchestrator.md`),
эти два шага делегируются ему. Шаг 1.2 выполняется **ровно один раз за прогон**,
поэтому последовательность не важна.

### Проверка доступности llm (один раз перед шагом 1.2)

```bash
llm-ssh test
```

Результат ("✓ Связь есть" / ошибка или команда не найдена) запомни на весь
текущий прогон skill'а — повторно перед Фазой 2 не проверяй. Если связи нет
или команда `llm` отсутствует — обе фазы выполняются как раньше, через
Agent tool, весь остальной текст про `llm` в этом файле пропускается.

---

## Поток выполнения

```
Фаза 1 (Scan + Architect)
    ├─ 1.1 collect_vault_structure.py --scan  [Python]  vault-scan.json
    ├─ 1.2 vault-domain-architect × 1  [Agent tool, ВЕСЬ VAULT]  domain-map.json
    └─ 1.3 collect_vault_structure.py --collect  [Python]  vault-structure-analysis.json
    ↓
Фаза 2 (Deep Read)    [Agent tool]       *-analysis.json (по доменам, параллельно)
    ↓
Фаза 1.4 (Incremental Progress Graph)   [Python]     build_topic_graph.py
    ↓
Фазы 3-6 (Python)     [synthesize_taxonomy.py]
    ├─ Фаза 3: Выявление фасетов и иерархий
    ├─ Фазы 4-5: Генерация JSON → taxonomy.json
    └─ Фаза 6: Верификация и очистка
        ↓
cleanup.py (удаляет промежуточные файлы)
```

Домены определяются по содержимому заметок, а не по структуре папок.
Файлы, которым архитектор не смог назначить домен, попадают в `unclassified`
внутри `domain-map.json`, практические заметки проектов - в `projects`.
Обе секции пробрасываются дальше и попадают в финальный `taxonomy.json`
как `projects` и `unclassified` - иначе эти файлы исчезали бы из отчёта
бесшумно, и следующий прогон не смог бы их обнаружить. Отсеянные скриптом
мусорные файлы попадают в секцию `filtered_out` и ни в один домен не
попадают.

Арифметика прогона обязана сходиться:

```
total_files (в доменах) + projects.count + unclassified.count + filtered_out.count
    == metadata.all_files_count (в все .md в vault)
```

Её проверяет `verify_taxonomy` (строка `candidates_accounted`), результат
виден в stderr и в шапке дашборда. Расхождение означает, что часть заметок
никем не учтена.

Второй уровень защиты - сверка `files_exist`: каждый учтённый путь должен
реально существовать в vault. Одного счёта мало, опечатка в пути (кириллица,
регистр, лишний пробел) даёт верную арифметику и при этом оставляет
настоящий файл неучтённым - так в прогоне 2026-10-04 потерялся путь
"...Параллельный доступ.md" в домене postgresql-query-optimization. В полном
прогоне расхождение это `FAIL` (скан свежий, значит виновата карта или анализ),
в инкрементальном - `WARN`: удалённые из vault файлы остаются в таксономии
законно.

### Таблица фаз

| Фаза | Инструмент | Выходной файл | Сохран. |
|------|-----------|---|---|
| 1.1 Scan | Python | vault-scan.json | Временный |
| 1.2 Architect | Agent tool, один вызов | domain-map.json | Финальный |
| 1.3 Collect | Python | vault-structure-analysis.json | Временный |
| 2. Deep Read | Agent tool, параллельно | {domain}-analysis.json | Временный |
| 1.4 Graph | Python | topic-graph.json, graph-dirty-domains.json | graph.json сохраняется |
| 3. Synthesis | Python (в памяти) | - | - |
| 4-5. JSON Generation | Python | taxonomy.json | ✓ Финальный |
| 6. Validation & Cleanup | Python | - | - |

**Финальные артефакты**: `.claude/temp_files/taxonomy.json`,
`.claude/temp_files/domain-map.json`, `.claude/temp_files/semantic-analysis-cache.json`,
`.claude/temp_files/topic-graph.json`, `.claude/temp_files/graph-revision-log.json`

---

## Фаза 1: Сбор структуры и проектирование карты доменов

Три шага: скрипт (скан и отсев мусора) → **один** субагент (проектирование
доменов и раскладка файлов) → скрипт (сборка структуры и обновление кеша).
Внешнего справочника нет: границы доменов выводит агент из материала.

### Шаг 1.1: Скан vault

Режим Full:
```bash
python .claude/skills/semantic-taxonomy/scripts/collect_vault_structure.py --scan
```

Режим Incremental:
```bash
python .claude/skills/semantic-taxonomy/scripts/collect_vault_structure.py --scan --new-only
```

Сканирует все `.md` файлы и прогоняет их через эвристический пре-фильтр
шума **без LLM**: служебные папки (`templates/`, `attachments/`, `.trash/`,
`.obsidian/`, `_resources/` и т.п.), пустые заметки, файлы только с
frontmatter и файлы, состоящие только из вставок изображений
(`embed_only`) - синтаксис вставок сам по себе даёт достаточно символов,
чтобы пройти порог длины, поэтому такие строки вырезаются перед проверкой.
Затем content-hash сверяется с `semantic-analysis-cache.json`: поле
`in_cache` показывает архитектору, что заметка не менялась.

Выход: `.claude/temp_files/vault-scan.json` - поля `total_files`,
`candidate_count`, `cached_count`, `files` (`path`, `folder`, `name`,
`content_hash`, `in_cache`), `filtered_out`, `mode`.

### Шаг 1.2: Проектирование карты доменов (Agent tool)

### ⚠️ КРИТИЧЕСКИ ВАЖНО: способ вызова

- **ОБЯЗАТЕЛЬНО**: инструмент **Agent** с `subagent_type: "vault-domain-architect"`
- **ОБЯЗАТЕЛЬНО**: **ровно один вызов на весь прогон**. Не по батчам, не
  по частям. Агент обязан увидеть весь материал, иначе вернётся дрейф
  границ между частями.
- **После вызова**: в чат попадает только 5-строчная сводка агента, никакого
  JSON, никаких Read/Write в диалог.

**ЗАПРЕЩЕНО**: оркестратору самому читать заметки vault или писать
`domain-map.json`. Это задача только субагента.

Если существующий `domain-map.json` уже есть (инкрементальный прогон) - передай
его агенту: он расширяет и правит карту, а не изобретает заново.

```python
Agent({
    description: "Карта доменов: весь vault",
    subagent_type: "vault-domain-architect",
    prompt: f"""
    Список заметок: {scan["files"]}
    Существующая карта (если есть): {existing_map or "нет, строим с нуля"}
    """
})
```

Правила определения доменов (П1-П5), правило минимума, антифрагментационный
проход и запреты живут в `.claude/agents/vault-domain-architect.md`. Оркестратор
их не дублирует.

### Шаг 1.3: Сборка структуры

Режим Full:
```bash
python .claude/skills/semantic-taxonomy/scripts/collect_vault_structure.py --collect
```

Режим Incremental:
```bash
python .claude/skills/semantic-taxonomy/scripts/collect_vault_structure.py --collect --new-only
```

Читает `vault-scan.json` + `domain-map.json`, строит
`path -> domain -> topic`, обновляет кеш
(`content_hash -> {domain_id, essence, key_concepts}`) и применяет
`.claude/temp_files/domain-merge-map.json` (`{старый_id: новый_id}`), если
карта доменов изменилась - без этого старый домен вернётся в следующем
прогоне.

Выход: `.claude/temp_files/vault-structure-analysis.json` - `metadata`
(`total_files` = кандидаты, `scanned_md_count` = все .md включая шум,
`total_domains`, `projects_count`, `unclassified_count`,
`domain_mode: "architect"`), `domains` (`domain_id`, `domain_name`,
`purpose`, `files`, `topics`), `all_files`, `projects`, `unclassified`,
`filtered_out`.

Записи `projects`/`unclassified`, чей путь не найден среди кандидатов
`vault-scan.json` (опечатка архитектора), отбрасываются с явным сообщением в
stderr. Пустые `reason`/`suggestion` у `unclassified` и пустой `why` у
проекта тоже идут в stderr - молчаливый дефолт здесь и был причиной потери
учёта.

### Проверка

> Чеклист ниже - для внутренней проверки перед переходом к следующей фазе.
> В диалог выводить не построчный чеклист, а одну итоговую строку по фазе
> (например: "Фаза 1: карта доменов - ок").

- [ ] `vault-scan.json` создан, `filtered_out` содержит только
      `template_path`, `empty_content`, `frontmatter_only`, `embed_only`
- [ ] `domain-map.json` создан, `source_files` совпадает с `candidate_count`
- [ ] Никаких двух доменов с одинаковым словарём и похожим `rationale`
- [ ] Размеры доменов различаются (ровная сетка - признак подгонки)
- [ ] Каждая заметка теории ровно в одном домене; практика - только в проектах
- [ ] Каждая запись `unclassified` имеет `reason` и `suggestion`
- [ ] `total_files` в `vault-structure-analysis.json` совпадает с ожиданием
- [ ] `projects_count + unclassified_count + файлы доменов == candidate_count`
- [ ] Ни один путь в карте доменов не расходится с путями `vault-scan.json`
      (сверка делается в Фазе 6 проверкой `files_exist`)
- [ ] В stderr нет сообщений об отброшенных путях и пустых reason/suggestion
- [ ] Incremental: если новых файлов нет, не запускать Фазу 2

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
- Проверка покрытия `candidates_accounted` - все .md vault учтены либо в
  домене, либо в `projects`/`unclassified`, либо в `filtered_out`
- Проверка `files_exist` - каждый учтённый путь существует в vault
  (FAIL в полном прогоне, WARN в инкрементальном)

Прогнать верификацию по готовому файлу, ничего не пересобирая (пригодится,
чтобы проверить чужую или урезанную `taxonomy.json`):

```bash
python .claude/skills/semantic-taxonomy/scripts/synthesize_taxonomy.py --verify
python .claude/skills/semantic-taxonomy/scripts/synthesize_taxonomy.py --verify путь/к/файлу.json
```

Код возврата 1, если хотя бы одна проверка дала `FAIL`.

После верификации запусти очистку:

```bash
python .claude/skills/semantic-taxonomy/scripts/cleanup.py
```

Удаляет временные файлы анализа (Фазы 1-5), сохраняет финальные артефакты.

### Финальная проверка

> Чеклист ниже — для внутренней проверки перед завершением. В диалог — одна
> итоговая строка (например: "Фаза 6: верификация и очистка — ок").

- [ ] Все файлы vault учтены (0 пропусков), `candidates_accounted: OK`
- [ ] `files_exist` без FAIL: ни один учтённый путь не выдуман агентом
- [ ] `projects` и `unclassified` не пусты, если файлы вне доменов есть
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
- vault-scan.json
- domain-merge-map.json (если карта менялась)
- vault-structure-analysis.json
- {domain}-analysis.json
- graph-dirty-domains.json
- другие временние артефакты

**Переживает cleanup** (кроме `--full`):
- taxonomy.json — итог анализа
- domain-map.json — карта доменов от архитектора, единственный носитель
  сведений о проектах и нераспознанных файлах между прогонами
- semantic-analysis-cache.json — кеш LLM-анализа Фазы 1
- topic-graph.json / graph-revision-log.json — состояние графа тем
