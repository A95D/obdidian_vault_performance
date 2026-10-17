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
  
- **Incremental** — добавить новые и изменённые файлы (сохранить старый taxonomy.json)
  - Когда: регулярное добавление и правка заметок, быстрое обновление
  - Команда: `python cleanup.py` (без флагов)
  - Поток, порядок фаз и защиты - в разделе «Инкрементальный прогон» ниже

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
    (в режиме Incremental граф строго после Фаз 2, 3 и 3б, см. ниже)
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
| 3б. Reassess (Incremental) | llm или Agent tool | {domain}-topics.validated.json | Временный |
| 3. Synthesis | Python (в памяти) | - | - |
| 4-5. JSON Generation | Python | taxonomy.json | ✓ Финальный |
| 6. Validation & Cleanup | Python | - | - |

**Финальные артефакты**: `.claude/temp_files/taxonomy.json`,
`.claude/temp_files/domain-map.json`, `.claude/temp_files/semantic-analysis-cache.json`,
`.claude/temp_files/topic-graph.json`, `.claude/temp_files/graph-revision-log.json`

---

## Инкрементальный прогон (режим Incremental)

Цель: новые и изменённые заметки добавляются дёшево. Дорогое (чтение текстов, LLM)
- только дельта и записи `unclassified`/`projects`; структура пересматривается по
уже накопленным данным (`taxonomy.json`, кеш, граф), стоимость растёт от числа
доменов, а не файлов. Данные файла (`essence`, `type`, `paradigm`, `components`,
`technologies`, `dependencies`) при переезде между доменами не пересчитываются.

### Порядок фаз (менять нельзя)

```
1. scan      collect_vault_structure.py --scan --new-only        vault-scan.json (status)
2. cards     build_domain_cards.py                               domain-cards.json
3. architect vault-domain-architect, режим инкремента            patch.json
4. apply     apply_domain_patch.py                               domain-map.json, domain-merge-map.json, affected-domains.json
             collect_vault_structure.py --collect --new-only     vault-structure-analysis.json
5. analyzer  vault-domain-analyzer, режим «добавление»           {domain_id}-analysis.json (только дельта)
6. reassess  build_reassess_input.py -> vault-topic-reassessor
             -> validate_reassessed_topics.py --domain ID        {domain_id}-topics.validated.json
7. граф      build_topic_graph.py                                topic-graph.json
8. merge     synthesize_taxonomy.py --merge                      taxonomy.json (без LLM), затем кеш
```

Граф строго после шагов 5-6. Дельты нет (нет `new | changed`) - шаги 2-8 не нужны.

**Восстановление при несогласованном графе.** Если граф не содержит файлов, уже учтённых в
`taxonomy.json` (оборванный прогон), статусы по хешам инвертируются. Одноразовый выход:
`--scan --new-only --baseline-taxonomy` - `status` считается по путям относительно `taxonomy.json`
(`new` - пути, которых там нет), в `vault-scan.json` поле `baseline: taxonomy`; изменённые файлы
так не обнаружить. Анализы доменов с дельтой, свежие по содержимому (хеши совпадают с хешами в
графе), но старше скана по времени, допустимо обновить `touch` только после такой сверки.
Тесты `scripts/test_*.py` читают замороженный снимок `.claude/temp_files/_fixtures/` (нет снимка - пропуск).

- **Карточки** (шаг 2): название, назначение, словарь, темы с `essence`, число
  файлов, плюс секции `unclassified` и `projects` целиком. Читается только `taxonomy.json`.
- **Архитектор** (шаг 3): вход - карточки и дельта. Выход - патч операций:
  `add_file`, `create_topic`, `create_domain`, `rename_domain`, `move_topic`,
  `merge_domains`, `split_domain`, а для записей `unclassified`/`projects` -
  `classify`, `to_project`, `keep_unclassified` (с новыми `reason` и `suggestion`).
  У каждой операции обоснование по П1-П5. Спорный случай (вторая школа или новый
  подход) не блокирует: `confidence: medium`, правится в следующем патче
  `merge_domains` или `split_domain`. Записи `unclassified` пересматриваются на каждом
  инкременте: «Bronze Layer» ушёл туда, пока не было заметок про Silver и Gold.
  Изменённая старая заметка остаётся в своём домене и теме, если архитектор не
  выдал `move_topic` или `add_file`.
- **Применение патча** (шаг 4): детерминированно. `rename_domain` и `move_topic`
  переносят темы с оценками по `topic_id`, `merge_domains` склеивает темы с
  одинаковым названием, `split_domain` распределяет темы. Домены после `merge`,
  `split`, `classify` и с новыми файлами - затронутые (`affected-domains.json`),
  их темы переоцениваются. `--dry-run` проверяет патч, ничего не записывая.
- **Режим «добавление»** (шаг 5): агент `vault-domain-analyzer` получает тексты
  только дельты плюс `essence` и `key_concepts` старых файлов домена из
  `taxonomy.json`. В `files` - записи только дельты, `total_files` - число файлов
  дельты. Зависимости направлены от новых файлов к любым, у изменённых файлов
  исходящие `dependencies` пересобираются, взаимных зависимостей и циклов нет.
  Описание режима - в конце `vault-domain-analyzer.md`.
- **Фаза 3б** (шаг 6): дешёвый вызов LLM на каждый затронутый домен. Вход
  (`reassess-input/{domain_id}.json`): текущие темы с `has_notes`,
  `mastery_percent`, `mastery_rationale`, `note_paths` (ожидаемые темы без заметок
  включительно), `essence` и `key_concepts` старых файлов, анализ дельты. Выход -
  `{domain_id}-topics.json` в схеме `domain_summary.topics`: привязка новых заметок
  к темам, оценки, новые темы, ожидаемые темы (`has_notes: false`,
  `mastery_percent` не выше 15), обновлённое `description`. Ответу не доверяем:
  `validate_reassessed_topics.py` выводит `has_notes` из `note_paths`, сверяет
  `note_paths` с файлами домена, проверяет уникальность `topic_id`, диапазон
  `mastery_percent` 0-100 и потолок 15 у тем без заметок. Покрытие - это
  `mastery_percent` по теме (полоса и бейдж в `dashboard.html`), процента домена нет.
- **Граф и merge** (шаги 7-8): см. Фазу 1.4 и Фазы 3-6. Learning paths строит скрипт
  по `difficulty` файлов, парадигмы - по `taxonomy.json`, LLM для них не нужен.

### Режим «добавление»

Так называется режим `vault-domain-analyzer`, когда в задании указан режим
«добавление» или список дельты. Он переопределяет шаги чтения и записи полного
режима (границы доменов, `boundary_flags`, формат ответа и ошибки - как в полном).
В строке 2 сводки - число файлов дельты. Вызов анализатора для домена без дельты
в этом режиме не делается.

### Коды выхода и защиты

Каждая защита - код с ненулевым выходом и сообщением, а не пункт чек-листа.

| Скрипт | 0 | 1 | 2 |
|---|---|---|---|
| `apply_domain_patch.py` | патч применён | ошибка валидации (файл не ровно в одном месте, пустая тема), ничего не записано | - |
| `build_reassess_input.py` | входы собраны | - | нет входного файла или анализа дельты |
| `validate_reassessed_topics.py` | ответ годен (возможны WARN) | ошибки в ответе | нет входов |
| `build_topic_graph.py` | граф записан | входы не готовы, цикл `dependency` с ребром дельты, структура и граф расходятся (граф не записан) | ошибка аргументов |
| `synthesize_taxonomy.py --merge` | `taxonomy.json` записан | нарушен инвариант (`taxonomy.json` и кеш не изменены) | нет входов или они устарели |
| `synthesize_taxonomy.py --verify` | все проверки OK | хотя бы один `FAIL` | - |
| `llm_send` | число файлов в `llm ls` совпало | ошибка `llm put` или расхождение | неверные аргументы |

Защиты:
- **Контракт входов.** Граф не строится, если у домена с дельтой нет
  `{domain_id}-analysis.json` свежее `vault-scan.json`, а у затронутого домена нет
  свежего `{domain_id}-topics.validated.json`. `--merge` не запускается без всех
  входов и без графа новее `vault-scan.json` и `domain-map.json`.
- **Единый источник «что нового».** Только `status` в `vault-scan.json`. Структура, граф и
  слияние новизну по-своему не считают.
- **Структура и граф сверяются.** После построения множество файлов графа сравнивается с
  `taxonomy.json` плюс дельта; расхождение - `FAIL`.
- **Циклы `dependency`.** Цикл с ребром дельты - `FAIL` с перечнем файлов (лечение:
  повторный анализ только затронутых файлов дельты); цикл только из старых рёбер -
  предупреждение.
- **Арифметика.** `candidates_accounted` и `files_exist` проверяются перед записью
  `taxonomy.json`; запись только при `OK`.
- **Неизменность незатронутого.** Записи доменов вне патча перед записью сверяются с
  прежними побайтово, кроме `recommended_topics` и `related_domain_topics`.
- **Согласованность тем.** `has_notes == bool(note_paths)`, каждый `note_path` есть
  среди файлов домена, каждый файл домена привязан хотя бы к одной теме.
- **Откат.** Перед записью `taxonomy.json`, `topic-graph.json` и `domain-map.json` скрипт
  сохраняет `*.before.json` (`taxonomy.before.json`, `topic-graph.before.json`,
  `domain-map.before.json`). Ручные бэкапы не нужны.
- **Дрейф кеша.** `semantic-analysis-cache.json` пишется после успешной записи
  `taxonomy.json`, а не на сборке структуры. Оборванный прогон оставляет кеш прежним.

### Канал LLM

Выбирается один раз за прогон по `llm-ssh test`. Шаги 3, 5 и 6 идут через `llm` (правило 8
`CLAUDE.md`, `LLM_MODEL=<алиас>` в каждом вызове, см. «Делегирование через llm»), при
сбое - Agent tool только для этого шага. Песочница чистится один раз (`llm clean`), до
первого `put`.

### Хелпер llm_send.sh

`scripts/llm_send.sh` (функция `llm_send`) - единственный способ отправить пакет в
песочницу, ручные циклы в прогонах запрещены:

```bash
source .claude/skills/semantic-taxonomy/scripts/llm_send.sh
llm_send --root "$VAULT_PATH" --extra .claude/agents/vault-domain-analyzer.md --extra .claude/temp_files/domain-cards.json
llm_send --root "$VAULT_PATH" --paths список.txt --extra ТЗ.md
```

Без `--paths` список строится из `vault-scan.json` (файлы `new | changed`, перевод
строки LF, без `\r`). Хелпер проверяет уникальность basename (`llm put` кладёт
плоско, одинаковые имена перезатирают друг друга), отправляет каждый файл с
`</dev/null 3<&-`, пишет ошибки в `.claude/temp_files/llm_send.log` и сверяет число
файлов в `llm ls` с ожидаемым. `llm clean` он не вызывает.

### Подводные камни llm

- Список путей без `\r`: текстовый режим Python на Windows пишет `\r\n`, и `llm put`
  отвечает «Нет такого файла/папки». Пиши `newline="\n"` или `tr -d '\r'`.
- `llm put` внутри `while read ... <&3` наследует дескриптор 3 со списком: вызывай
  `llm put "$f" </dev/null 3<&-`. Без `</dev/null` `llm` съедает stdin цикла и цикл
  завершается с кодом 0.
- Не глуши вывод (`>/dev/null 2>&1`): без текста ошибки причину не найти. Пиши в лог
  (`llm_send.log`) и проверяй код возврата.
- Голый `llm get` кладёт файл относительно текущей папки: забирай через временную папку.
- Одинаковые basename в разных папках vault перезатирают друг друга - не отправлять.

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
`in_cache` - справочное, источником «что уже в таксономии» оно не служит
(кеш пишется после `taxonomy.json`, но не хранит путей).

В режиме Incremental хеш файла сверяется с `content_hash` его узла в
`topic-graph.json`: файла нет в графе - `status: new`, хеш отличается -
`changed`, совпал - `unchanged`. Нет `topic-graph.json` - прогон полный
(сверка по кешу невозможна: в нём нет путей). Скан также печатает пути из
`taxonomy.json`, которых нет в vault (удалённые и переименованные).

Выход: `.claude/temp_files/vault-scan.json` - поля `total_files`,
`candidate_count`, `cached_count`, `new_count`, `changed_count`,
`unchanged_count`, `files` (`path`, `folder`, `name`, `content_hash`,
`in_cache`, `status`), `filtered_out`, `mode`. `status` - единственный
источник «что нового» для всех следующих шагов.

### Шаг 1.2: Проектирование карты доменов (Agent tool или llm)

### ⚠️ КРИТИЧЕСКИ ВАЖНО: способ вызова

- **ОБЯЗАТЕЛЬНО**: инструмент **Agent** с `subagent_type: "vault-domain-architect"`
- **ОБЯЗАТЕЛЬНО**: **ровно один вызов на весь прогон**. Не по батчам, не
  по частям. Агент обязан увидеть весь материал, иначе вернётся дрейф
  границ между частями.
- **После вызова**: в чат попадает только 5-строчная сводка агента, никакого
  JSON, никаких Read/Write в диалог.

**ЗАПРЕЩЕНО**: оркестратору самому читать заметки vault или писать
`domain-map.json`. Это задача только субагента.

В режиме Incremental этот шаг заменён дешёвым пересмотром структуры по
карточкам доменов (см. «Инкрементальный прогон»): архитектор не получает весь
vault и не читает старые заметки. Вызов на весь vault - только для Full.

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

### Вариант через llm (если `llm-ssh test` прошёл)

Вместо Agent tool тот же один вызов архитектора выполняет суб-агент `llm`
(правило 8 `CLAUDE.md`). Результат тот же: `domain-map.json` в
`.claude/temp_files/`. Если `llm` вернул невалидный JSON или не создал файл -
fallback на Agent tool (один вызов), остальной прогон не блокируется.

Отправка файлов - только хелпером `scripts/llm_send.sh` (см. «Хелпер llm_send.sh»),
ручные циклы запрещены. Подводные камни (из-за них шаг 1.2 через `llm` сорвался
2026-10-17):
- Список путей, записанный Python на Windows текстовым режимом, содержит
  `\r\n`. `read -r` оставляет `\r` в пути, и `llm put` отвечает «Нет такого
  файла/папки», хотя файл есть. Записывай список с `newline="\n"` или
  прогоняй через `tr -d '\r'`.
- `llm put` внутри `while read ... <&3` наследует дескриптор 3 со списком.
  Закрывай его для вызова: `llm put "$f" </dev/null 3<&-`.
- Не глуши вывод `llm put` (`>/dev/null 2>&1`): без текста ошибки причину не
  найти. Пиши в лог и проверяй код возврата.

Порядок:

```bash
llm clean                                             # один раз за прогон, до первого put
llm ls </dev/null                                     # пусто, кроме . и ..
```

Отправка: `llm put` кладёт файл под его basename в `/workspace/agent` (плоско,
без подпапок). Поэтому до отправки проверь, что basename всех путей из
`vault-scan.json` уникален (`os.path.basename`). Если два файла с одинаковым
именем в разных папках - не отправляй, остановись и сообщи. Отправь:

```bash
llm put .claude/temp_files/vault-scan.json </dev/null
llm put .claude/agents/vault-domain-architect.md </dev/null
# каждый файл из vault-scan.json["files"][*]["path"] - через список без \r,
# с 3<&- и с логом ошибок (см. подводные камни выше)
```

Проверь `llm ls </dev/null`: число файлов = число кандидатов + 2.

Запуск (в фоне, по правилу 11 `CLAUDE.md`):

```bash
LLM_MODEL=<алиас> llm --fresh "Выполни инструкцию из vault-domain-architect.md. Вход: vault-scan.json, поле files - список кандидатов; их заметки лежат в текущей папке под своими именами. Запиши domain-map.json строго по схеме из инструкции; пути в карте - точно как поле path в скане. В ответе - 5 строк сводки, без JSON." </dev/null
```

Готовность - файл `domain-map.json` в `llm ls`, а не текст ответа.

Забрать результат и проверить:

```bash
TMP="$(mktemp -d)"
(cd "$TMP" && llm get domain-map.json </dev/null) && mv -f "$TMP/domain-map.json" .claude/temp_files/; rm -rf "$TMP"
```

Далее `python ... collect_vault_structure.py --collect`. Его stderr должен быть
пустым по части отброшенных путей и пустых `reason`/`suggestion`. Любое
сообщение там - ошибка архитектора, её нужно исправить до Фазы 2.

### Шаг 1.3: Сборка структуры

Режим Full:
```bash
python .claude/skills/semantic-taxonomy/scripts/collect_vault_structure.py --collect
```

Режим Incremental (запускается после применения патча, `apply_domain_patch.py`):
```bash
python .claude/skills/semantic-taxonomy/scripts/collect_vault_structure.py --collect --new-only
```

Дельта определяется по `status` в `vault-scan.json` (`new` и `changed`), а не по
пути относительно `taxonomy.json`: изменённая заметка с прежним путём не теряется.
`new_candidates_count` и `new_filtered_count` считаются по тому же признаку.

Контракт `metadata.delta_only: true` (в полном режиме `false`): `domains[].files` и `all_files` -
только дельта, старые файлы доменов берутся из `taxonomy.json`. `--merge`, `--verify` и
`build_topic_graph.py` отказываются работать при `mode=incremental` без этого флага (рассинхрон
версий) - перезапусти `--collect --new-only`. В инкременте `--verify` сверяет пути со всем
`vault-scan.json` (`files` и `filtered_out`), а удалённые старые файлы дают WARN.

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

- **Режим Incremental** (`--merge`):
  - Граф к Фазе 2 ещё не строился (он идёт после Фаз 2, 3 и 3б), поэтому
    `graph-dirty-domains.json` здесь не источник. Домены для анализа - из
    `affected-domains.json` (результат `apply_domain_patch.py`) и доменов, куда
    попали файлы `new | changed` из `vault-scan.json`.
  - Агент работает в режиме «добавление»: читает только файлы дельты, остальные
    записи домена берёт из `taxonomy.json`. Домены без дельты не анализируются.
  - Дельты нет (нет `new | changed`) - Фаза 2 и 3б пропускаются.

**Зачем**: в incremental режиме глубоко читаются только новые и изменённые заметки - экономия времени и токенов.

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
2. Если Incremental: я взял домены из `affected-domains.json` и доменов с файлами `new | changed` и включил режим «добавление»?
3. Я собираюсь вызвать инструмент **Agent**, а не Read/Write? Если нет — стоп.
4. У вызова указан `subagent_type: "vault-domain-analyzer"`?
5. Все (отфильтрованные) домены отправлены одним сообщением, параллельно?

Если в какой-то момент Фазы 2 обнаруживаешь, что читаешь файл домена или
пишешь `{domain_id}-analysis.json` напрямую (не через Agent tool) — это ошибка:
останови эти действия и перезапусти шаг через Agent tool.

### Делегирование через llm (если `llm-ssh test` прошёл на шаге классификации Фазы 1)

Домены обрабатываются последовательно (песочница `llm` — одна сессия). Для
каждого домена:

Модель песочницы передаётся явно: каждый вызов `llm` идёт с префиксом
`LLM_MODEL=<алиас>`. Алиас определяет оркестратор сам, по своему ID модели из
системного промпта (поле «exact model ID»), по семейству:

| ID оркестратора | `LLM_MODEL` |
|---|---|
| `claude-opus-*` | `opus` |
| `claude-sonnet-*` | `sonnet` |
| `claude-haiku-*` | `haiku` |

Правила:
- Алиас определяется заново перед каждым вызовом `llm`: модель могла смениться
  командой `/model` в середине сессии.
- Полный ID (`claude-sonnet-5-5`) не передавать: песочница выдаёт предупреждение.
  Проверено: `sonnet` и `opus` принимаются без предупреждений, `haiku` отвечает,
  но пишет `unrecognized_model`.
- Семейство не из таблицы (например `fable`) - не угадывать и не подставлять
  дефолт: остановиться и спросить пользователя, какой алиас использовать.
- Без `LLM_MODEL` `llm` берёт модель из `~/.llm/config` или дефолт песочницы -
  так не делать. Исключение: `llm clean` и `llm-ssh test`, им модель не нужна.

Перед передачей файлов прогона песочница полностью очищается (старые результаты
прошлых прогонов иначе попадают в выборку). Очистка выполняется один раз за
прогон, до первого `llm put`, и проверяется через `llm ls`. Если в выводе
`llm ls` остались файлы или папки (кроме `.` и `..`), передачу не начинать.

```bash
llm clean                                             # полная очистка /workspace/agent (без модели, проверка внутри)
llm ls </dev/null                                     # повторная проверка: пусто, кроме . и ..
grep "^VAULT_PATH=" .env                              # взять путь к vault
llm put .claude/temp_files/vault-structure-analysis.json
llm put <VAULT_PATH>/<файлы_домена>                   # файлы этого domain_id
llm put .claude/agents/vault-domain-analyzer.md       # ТЗ (схема анализа)
llm put .claude/skills/analyze-vault-domain/references/REFERENCE.md
LLM_MODEL=<алиас> llm --fresh "Выполни инструкцию из vault-domain-analyzer.md для domain_id=\"{domain_id}\", domain_name=\"{domain_name}\". Файлы домена уже отправлены. Запиши результат в {domain_id}-analysis.json по схеме из REFERENCE.md." </dev/null
TMP="$(mktemp -d)"                                    # забрать результат: llm get кладёт файл относительно cwd,
(cd "$TMP" && llm get llm-phase2/{domain_id}-analysis.json </dev/null) && mv -f "$TMP/llm-phase2/{domain_id}-analysis.json" .claude/temp_files/; rm -rf "$TMP"
```

Очистка песочницы (`llm clean`) выполняется один раз за прогон, до первого
`llm put`. Если шаг 1.2 уже шёл через `llm` в этом прогоне - повторно не
делай, иначе стираются результаты прошлых вызовов этого же прогона.

Правила для циклов с `llm` (нарушение этих правил уже ломало прогон):
- Каждый вызов `llm` получает `</dev/null`. Без этого `llm` читает stdin и
  съедает оставшийся список доменов цикла, при этом цикл завершается с кодом 0.
- Список доменов читается через отдельный дескриптор (`while read ... <&3; done 3< список`).
- Файлы забираются только через `llm get` во временной папке (блок выше): голый `llm get` кладёт файл
  относительно текущей папки, то есть в корень проекта.
- Не писать вывод вызова `llm` в файлы-логи в `.claude/temp_files/` (вроде
  `_p2_{domain_id}.log`): ни один скрипт их не читает. Сводку и ошибку возвращает
  сам фоновый вызов, готовность - по `{domain_id}-analysis.json`, а не по логу.
- После цикла проверить, что есть все `{domain_id}-analysis.json` и что JSON
  валиден с полем `domain_summary`. Для недостающих доменов - fallback через
  Agent tool.

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
(в incremental-режиме - после Фаз 2, 3 и 3б строго; если дельты нет и Фазы 2/3б
пропущены, то сразу после шага 1.3), перед Фазами 3-6.

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

1. Скрипт строит/обновляет `.claude/temp_files/topic-graph.json` - граф домен -> тема -> файл.
   Узлы файлов и доменов создаются всегда, рёбра `member_of` связывают файл с доменом и,
   если тема известна, с темой. Узлы тем (`kind="topic"`) появляются только для файлов,
   привязанных к теме: по `taxonomy.json` прошлых прогонов или, пока тем в `taxonomy.json`
   нет (первый прогон), по `vault-structure-analysis.json` (`domains[].topics[].note_paths`).
2. Новые файлы помечают только свой домен как **dirty**; изменившиеся помечают домен и,
   если их тема известна, тему. В инкременте к dirty добавляются затронутые домены
   (`affected-domains.json`) и оба домена переехавшего файла. Список пишется в
   `.claude/temp_files/graph-dirty-domains.json` (одноразовый артефакт этого прогона,
   вход для Фаз 3-6). Поэтому на первом прогоне `dirty_topics` пуст: все файлы новые.
3. Ревизия графа инкрементируется, история пишется в
   `.claude/temp_files/graph-revision-log.json`.
4. Рёбра `dependency` строятся из поля `dependencies` файлов в `{domain}-analysis.json`
   (Фаза 2). В полном режиме берутся все файлы с анализом, в инкрементальном - только
   новые и изменившиеся файлы со свежим анализом; остальные рёбра сохраняются. Ссылка
   резолвится в файл графа: сначала точный путь, затем имя файла с приоритетом своего
   домена. Неоднозначная ссылка (несколько кандидатов, ни один в своём домене) и
   ненайденная ссылка рёбра не дают, причина выводится в stderr. `mentions` хранятся в
   узле файла и в рёбра не превращаются.
5. Каждому домену присваивается `relations_status`: `derived` (есть рёбра dependency),
   `dependencies_unresolved` (ссылки были, ни одна не разрешилась), `no_dependencies_found`
   (зависимостей нет). Домен, не обработанный в этом прогоне, сохраняет прежний статус,
   без него получает `not_derived`.
6. Рёбра `related` скрипт не выводит: их создаёт только `vault-relevance-finder` (см. ниже).

**Важно про новые файлы**: в инкременте у каждого домена с дельтой обязан быть свежий
`{domain_id}-analysis.json` (режим «добавление»), у затронутого домена -
`{domain_id}-topics.validated.json` (Фаза 3б). Иначе граф не строится (см. «Коды выхода»).
Essence/key_concepts дельты берутся из этого анализа, а не из кеша классификации.
Старые узлы и рёбра сохраняются: узлы и `member_of` перестраиваются из актуальной
раскладки (в том числе для переехавших файлов), `dependency` сохраняются, у изменённых
файлов исходящие `dependency` пересобираются. Переименование и слияние доменов
применяются к узлам по `domain-merge-map.json`. `related` между доменами после
перестановки перепроверяются с предупреждением.

### Поиск related/dependencies для новых файлов

Связи `related` создаёт только `vault-relevance-finder`. Субагент вызывается не для всех
новых файлов, а только для списка `relevance_required` в `graph-dirty-domains.json`:
новых файлов, для которых нет свежего `{domain}-analysis.json` их домена в этом прогоне.
Связи остальных новых файлов уже посчитаны Фазой 2 (шаг 4 выше). Изменившиеся файлы
в `relevance_required` не попадают. В полном режиме список обычно пуст; скрипт печатает
в stderr число файлов, требующих вызова (`[INFO] Файлов, требующих вызова
vault-relevance-finder: N`).

Команды (из корня проекта):
```bash
python .claude/skills/semantic-taxonomy/scripts/build_topic_graph.py --relevance-requests .claude/temp_files/relevance-requests.json
```
Файл `relevance-requests.json` имеет вид `{"requests": {"<node_id>": {"new_file": {...}, "domain_nodes": [...]}}}`.
Для каждого `node_id` оркестратор вызывает Agent tool `vault-relevance-finder`
(`subagent_type: "vault-relevance-finder"`, модель Haiku) с объектом `requests[node_id]`
как входом. Вызовы - в одном сообщении (параллельно), по аналогии с Фазой 2. Ответ
каждого субагента - `{"related": [...], "dependencies": [...]}`. Оркестратор собирает
ответы в `.claude/temp_files/relevance-results.json` в виде
`{"results": {"<node_id>": {"related": [...], "dependencies": [...]}}}` и применяет:
```bash
python .claude/skills/semantic-taxonomy/scripts/build_topic_graph.py --apply-relevance .claude/temp_files/relevance-results.json
```

Поведение `--apply-relevance`:
- `node_id`, которого нет в графе как файла, пропускается с предупреждением в stderr.
- Цель вне домена файла, самоссылка и цель, указанная и в `related`, и в `dependencies`,
  отбрасываются или сводятся к `dependency`; каждый случай - предупреждение в stderr.
  Один битый ответ не останавливает прогон.
- Повторное применение того же файла ничего не меняет: рёбра не дублируются, revision
  не растёт. Revision растёт, только если добавлены рёбра или изменён статус домена.
- `graph-dirty-domains.json` и `dirty_domains` не меняются.
- `--relevance-requests` и `--apply-relevance` нельзя совмещать с `--full` и друг с другом;
  ошибка аргументов - код возврата 2.

`--full` пересобирает граф с нуля и стирает связи, полученные через `--apply-relevance`.
После полного прогона `related` не восстанавливаются автоматически: вызов субагента
нужен только для файлов из `relevance_required`, а для файлов с анализом Фазы 2 список пуст.

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
- [ ] Для каждого `node_id` из `relevance_required` был вызван `vault-relevance-finder`
  (или его `model: inherit` повтор), и только для них; если список пуст - вызовов нет
- [ ] Для `changed_file_ids` `vault-relevance-finder` НЕ вызывался (`relevance_required`
  содержит только новые файлы)
- [ ] Если запускался `--apply-relevance`, он завершился с кодом 0 без `[ERROR]` в stderr
- [ ] Ни один raw JSON графа/ответа субагента не попал в диалог

---

## Фазы 3-6: Синтез таксономии

### Команды

Режим Full:
```bash
python .claude/skills/semantic-taxonomy/scripts/synthesize_taxonomy.py
```

Режим Incremental (без LLM: раскладка из `domain-map.json`, затронутые домены из
`affected-domains.json`, темы из `{domain_id}-topics.validated.json`, дельта из
`{domain_id}-analysis.json`):
```bash
python .claude/skills/semantic-taxonomy/scripts/synthesize_taxonomy.py --merge
```

Режим Incremental с полным пересчётом прогресса всех доменов (игнорирует
dirty-фильтр графа, но по-прежнему мёржит, а не пересобирает `taxonomy.json`
с нуля); `--merge --dry-run` проверяет инварианты, ничего не записывая:
```bash
python .claude/skills/semantic-taxonomy/scripts/synthesize_taxonomy.py --merge --full
```

Скрипт выполняет фазы 3-6 последовательно внутри себя. В режиме `--merge`
`files` каждого домена собираются из актуальной раскладки (старые записи из
`taxonomy.json`, в том числе переехавшие, плюс записи дельты), поэтому
`global_facets`, `paradigms`, статистика и learning paths пересчитываются без
анализа старых файлов. Запись затронутого домена заменяется, темы берутся из
Фазы 3б. Незатронутые домены остаются без изменений, кроме производных полей
`recommended_topics` и `related_domain_topics` (пересчитываются по всем доменам,
порог рекомендации `RECOMMENDATION_MASTERY_THRESHOLD = 50`).

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
- Удаленные файлы из vault (останутся в taxonomy.json; скан печатает такие пути, `files_exist` даёт WARN)
- Переименованные файлы (появятся как новые)

Изменённые заметки (другой хеш при том же пути) отслеживаются: они входят в дельту.
Перемещение между доменами делает архитектор патчем (`move_topic`, `add_file`), без
чтения старых текстов. Без `topic-graph.json` дельту определить нечем - прогон полный.

Для полной синхронизации (удаления, сомнения в целостности) используйте режим Full.

---

## Итого

**Входные данные**: папка с .md файлами (Obsidian Vault по VAULT_PATH в .env)

**Выходные файлы** (в `.claude/temp_files/`):
- `taxonomy.json` — источник данных для веб-дашборда, фасетной навигации, рекомендаций

**Промежуточные файлы** (удаляются на Фазе 6):
- vault-scan.json
- domain-merge-map.json (если карта менялась)
- domain-cards.json, patch.json, affected-domains.json, reassess-input/, {domain}-topics.json, {domain}-topics.validated.json
- *.before.json (откат; перед очисткой убедись, что прогон принят)
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
