# Примеры для semantic-taxonomy

Полные примеры JSON и структур, на которые ссылается SKILL.md.

## Фаза 1: vault-structure-analysis.json

Структура файла vault-structure-analysis.json после сканирования:

```
Vault Structure:
├── DWH/                    → domain: dwh
├── Data Vault/             → domain: data-vault
├── Сбор требований/        → domain: requirements
├── Infrastructure/         → domain: infrastructure
└── Learning Projects/      → domain: learning-project
```

Содержит: `metadata` (total_files, total_domains), `domains` (массив с domain_id, domain_name, files[path, name]), `all_files` (плоский список всех файлов).

---

## Фаза 2: Пример анализа одного файла

Результат анализа в domain-analysis.json:

```
Файл: "Star Schema.md"
- Суть: описание многомерной модели данных для витрин
- Роль: основной компонент в Kimball-подходе
- Ключевые концепции: факты, измерения, нормализация, денормализация
- Упоминания: Fact Tables, Dimension Tables, Snowflake Schema
- Уровень: beginner
```

Каждый файл в domain-analysis.json содержит: `essence`, `role`, `key_concepts`, `mentions`, `difficulty_level`.

---

## Фаза 3.2: Пример DAG иерархии

```
Domain Root (overview)
├─ Paradigm A (foundational)
│  ├─ Component A1
│  ├─ Component A2
│  └─ Pattern A
├─ Paradigm B (alternative)
│  ├─ Component B1
│  └─ Pattern B
└─ Comparison / Advanced
   └─ Trade-offs between A and B
```

Для каждого домена строится направленный ациклический граф (DAG). Каждый узел имеет ≤2 родителей, нет циклических зависимостей.

---

## Фаза 3.3: Пример Learning Paths

```json
{
  "domain": "dwh",
  "learning_paths": {
    "beginner": [
      "DWH/Overview.md (что это такое в принципе)",
      "Kimball-Approach.md (практически применимый вариант)",
      "Star-Schema.md (основная структура)",
      "Fact-Tables.md (основной компонент)",
      "Dimension-Tables.md (второй основной компонент)"
    ],
    "intermediate": [
      "Bus-Architecture.md (интеграция нескольких витрин)",
      "Medallion-Architecture.md (слои обработки)"
    ],
    "advanced": [
      "Inmon-Approach.md (альтернативная парадигма)",
      "Data-Vault-2.0.md (сравнение парадигм)"
    ]
  }
}
```

Три маршрута обучения для каждого домена (beginner/intermediate/advanced). Каждый path — логичная цепочка от простого к сложному.

---

## Фазы 4-5: taxonomy.json (полная структура)

Финальная таксономия, объединяющая все результаты фаз 2-3:

```json
{
  "metadata": {
    "vault_path": "C:\\path\\to\\vault",
    "analysis_date": "2026-07-26",
    "total_files": 63,
    "total_domains": 6,
    "analyzer_notes": "Full semantic taxonomy based on file content analysis"
  },
  
  "domains": [
    {
      "domain_id": "dwh",
      "domain_name": "Data Warehouse",
      "description": "Classical data warehouse design paradigms",
      "file_count": 11,
      "paradigms": ["inmon", "kimball", "medallion"],
      
      "files": [
        {
          "id": "file_1",
          "path": "DWH/Overview.md",
          "title": "Data Warehouse Overview",
          "essence": "Introduction to data warehouse concepts and main design paradigms",
          "type": "concept",
          "paradigm": null,
          "components": [],
          "technologies": [],
          "difficulty": "beginner",
          "related": ["DWH/Kimball-Approach.md"],
          "dependencies": [],
          "practical_importance": "critical",
          "status": "published"
        },
        {
          "id": "file_2",
          "path": "DWH/Star-Schema.md",
          "title": "Star Schema",
          "essence": "Multidimensional data model for data marts in Kimball approach",
          "type": "concept",
          "paradigm": "kimball",
          "components": ["fact-table", "dimension-table"],
          "technologies": ["sql", "postgresql"],
          "difficulty": "beginner",
          "related": ["DWH/Fact-Tables.md", "DWH/Dimension-Tables.md"],
          "dependencies": ["DWH/Kimball-Approach.md"],
          "practical_importance": "high",
          "status": "published"
        }
      ],
      
      "learning_paths": {
        "beginner": {
          "description": "From zero knowledge to understanding basics",
          "files": ["DWH/Overview.md", "DWH/Kimball-Approach.md", "DWH/Star-Schema.md"],
          "estimated_hours": 4,
          "mastery_level": "Can explain basics and design a simple star schema"
        },
        "intermediate": {
          "description": "Deeper patterns and integration",
          "files": ["DWH/Bus-Architecture.md", "DWH/Medallion-Architecture.md"],
          "estimated_hours": 8
        },
        "advanced": {
          "description": "Paradigm comparison and optimization",
          "files": ["DWH/Inmon-Approach.md", "DWH/Paradigm-Comparison.md"],
          "estimated_hours": 6
        }
      }
    }
  ],
  
  "global_facets": {
    "types": ["concept", "architecture", "pattern", "methodology", "reference", "guide", "checklist", "case-study"],
    "paradigms": ["inmon", "kimball", "data-vault-2.0", "medallion", "beam"],
    "technologies": ["postgresql", "dbt", "docker", "git", "python", "sql"],
    "components": ["fact-table", "dimension-table", "hub", "link", "satellite", "pit-table", "bridge-table"],
    "difficulties": ["beginner", "intermediate", "advanced"],
    "domains": ["dwh", "data-vault", "requirements", "infrastructure", "learning-project"],
    "statuses": ["published", "wip", "draft", "archived"],
    "importance_levels": ["low", "medium", "high", "critical"]
  },
  
  "statistics": {
    "total_files": 63,
    "files_per_domain": {"dwh": 11, "data-vault": 9, "...": "..."},
    "files_per_type": {"concept": 25, "architecture": 12, "...": "..."},
    "files_per_difficulty": {"beginner": 20, "intermediate": 25, "advanced": 18},
    "files_per_paradigm": {"kimball": 8, "data-vault-2.0": 9, "...": "..."}
  }
}
```

Структура включает: metadata, domains (каждый с files, learning_paths), global_facets, statistics.

---

## Таблица фасетов (Фаза 3.1)

| Фасет | Примеры значений | Примечание |
|---|---|---|
| **domain** | dwh, data-vault, requirements, infrastructure, learning-project, deep-coding | разделение по предметным областям |
| **type** | concept, architecture, pattern, methodology, reference, guide, checklist, case-study | тип содержимого |
| **paradigm** | inmon, kimball, data-vault-2.0, medallion, beam | архитектурная парадигма (не всегда применима) |
| **components** | fact-table, dimension-table, hub, link, satellite, pit-table, bridge-table | технические элементы |
| **technologies** | postgresql, dbt, docker, git, python, sql | используемые инструменты |
| **difficulty** | beginner, intermediate, advanced | сложность материала |