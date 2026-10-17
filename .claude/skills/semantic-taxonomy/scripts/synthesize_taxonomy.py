#!/usr/bin/env python3
"""
Синтез таксономии (портирование phase3-6-synthesis.js на Python).

Фазы 3-6:
  3. Синтез: выявление глобальных фасетов и иерархий
  4-5. Генерация: создание taxonomy.json
  6. Верификация: проверка целостности

Режимы:
  (без флагов)        полный пересбор по {domain}-analysis.json
  --merge [--dry-run] слияние раскладки (domain-map.json), дельты и переоценённых тем
                      затронутых доменов в существующий taxonomy.json без LLM;
                      --dry-run проверяет инварианты и ничего не пишет
  --verify [файл]     проверка готового taxonomy.json
  --temp-dir DIR      директория temp_files (по умолчанию .claude/temp_files проекта)

Запись taxonomy.json только при выполненных инвариантах; перед записью копия
taxonomy.before.json, кеш semantic-analysis-cache.json обновляется после записи.
"""

import json
import re
import sys
import io
import shutil
from pathlib import Path
from datetime import datetime

# Порог "проработки" темы, ниже которого тема попадает в рекомендации (FR-009)
RECOMMENDATION_MASTERY_THRESHOLD = 50

# Игнорируемые при сопоставлении тем короткие/служебные слова
TOPIC_NAME_STOPWORDS = {
    "и", "в", "на", "с", "для", "по", "из", "как", "или", "не",
    "the", "of", "in", "for", "to", "and", "or", "an", "a",
}

# Установить UTF-8 кодировку для вывода (защита от ошибок на Windows)
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')


def get_project_root():
    """Определить корень проекта."""
    return Path(__file__).resolve().parents[4]


def load_dirty_domains(temp_dir: Path) -> dict:
    """
    Загрузить graph-dirty-domains.json (T010/T011). Отсутствие файла -
    build_topic_graph.py ещё не запускался в этом прогоне, не ошибка:
    вызывающий код в этом случае не сужает область пересчёта.
    """
    dirty_path = temp_dir / "graph-dirty-domains.json"
    if not dirty_path.exists():
        return None
    try:
        with open(dirty_path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[WARN] Ошибка чтения graph-dirty-domains.json: {e}", file=sys.stderr)
        return None


def load_existing_taxonomy(temp_dir: Path) -> dict:
    """Загрузить существующую таксономию если она есть."""
    taxonomy_path = temp_dir / "taxonomy.json"
    if not taxonomy_path.exists():
        return None

    try:
        with open(taxonomy_path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[WARN] Ошибка чтения existing taxonomy.json: {e}", file=sys.stderr)
        return None


def section_items(section) -> list:
    """Достать записи секции, принимая и list, и {count, items}."""
    if isinstance(section, list):
        return section
    if isinstance(section, dict):
        items = section.get("items")
        return items if isinstance(items, list) else []
    return []


def section_count(section) -> int:
    """Размер секции: явный count, если он есть, иначе число записей."""
    if isinstance(section, list):
        return len(section)
    if isinstance(section, dict):
        if isinstance(section.get("count"), int):
            return section["count"]
        return len(section_items(section))
    return 0


def normalize_projects_section(projects) -> dict:
    """
    Привести секцию projects к форме {"count": N, "items": [{name, files, why}]}.

    count считается по самим файлам, а не берётся из входа: файлы - это то,
    что попадает в арифметику покрытия, доверять им и есть смысл.
    """
    items = []
    total = 0
    for entry in section_items(projects):
        if not isinstance(entry, dict):
            continue
        files = entry.get("files", [])
        if isinstance(files, str):
            files = [files]
        if not isinstance(files, list):
            continue
        clean = []
        for f in files:
            if isinstance(f, str) and f.strip() and f not in clean:
                clean.append(f.strip())
        if not clean:
            continue
        total += len(clean)
        items.append({
            "name": str(entry.get("name") or ""),
            "why": str(entry.get("why") or ""),
            "files": clean,
        })
    return {"count": total, "items": items}


def normalize_unclassified_section(unclassified) -> dict:
    """Привести секцию unclassified к форме {"count": N, "items": [{path, reason, suggestion}]}."""
    items = []
    seen = set()
    for entry in section_items(unclassified):
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        path = path.strip()
        if path in seen:
            continue
        seen.add(path)
        items.append({
            "path": path,
            "reason": str(entry.get("reason") or ""),
            "suggestion": str(entry.get("suggestion") or ""),
        })
    return {"count": len(items), "items": items}


def merge_projects_sections(existing, new) -> dict:
    """
    Объединить проекты прежней taxonomy.json с проектами этого прогона.

    Инкрементальный прогон приносит только новые файлы, поэтому старые
    проекты из existing должны сохраниться. Ключ слияния - имя проекта,
    внутри - объединение списков файлов.
    """
    by_name = {}
    for section in (existing, new):
        for item in normalize_projects_section(section)["items"]:
            key = item["name"] or item["files"][0]
            merged = by_name.get(key)
            if merged is None:
                by_name[key] = {"name": item["name"], "why": item["why"], "files": list(item["files"])}
                continue
            if item["why"] and not merged["why"]:
                merged["why"] = item["why"]
            for f in item["files"]:
                if f not in merged["files"]:
                    merged["files"].append(f)
    items = list(by_name.values())
    return {"count": sum(len(i["files"]) for i in items), "items": items}


def merge_unclassified_sections(existing, new) -> dict:
    """Объединить unclassified прежней taxonomy.json с новым по пути файла."""
    by_path = {}
    for section in (existing, new):
        for item in normalize_unclassified_section(section)["items"]:
            by_path[item["path"]] = item
    items = list(by_path.values())
    return {"count": len(items), "items": items}


def collect_accounted_paths(taxonomy: dict) -> set:
    """Все пути файлов, учтённых в таксономии: домены + projects + unclassified."""
    paths = set()
    for domain in taxonomy.get("domains") or []:
        if not isinstance(domain, dict):
            continue
        for file_info in domain.get("files") or []:
            if isinstance(file_info, dict) and isinstance(file_info.get("path"), str):
                paths.add(file_info["path"])
    for item in section_items(taxonomy.get("projects")):
        if not isinstance(item, dict):
            continue
        files = item.get("files")
        if isinstance(files, str):
            files = [files]
        for path in files or []:
            if isinstance(path, str):
                paths.add(path)
    for item in section_items(taxonomy.get("unclassified")):
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            paths.add(item["path"])
    return paths


def known_paths_from_structure(vault_data: dict):
    """Пути файлов vault по vault-structure-analysis.json: кандидаты + шум."""
    if not isinstance(vault_data, dict):
        return None
    paths = {f["path"] for f in vault_data.get("all_files") or []
             if isinstance(f, dict) and isinstance(f.get("path"), str)}
    for f in (vault_data.get("filtered_out") or {}).get("files", []) or []:
        if isinstance(f, dict) and isinstance(f.get("path"), str):
            paths.add(f["path"])
    return paths or None


def known_paths_from_scan(scan_path: Path):
    """Все пути vault по vault-scan.json: files + filtered_out. Нет скана или он нечитаем - None."""
    if not scan_path.exists():
        return None
    try:
        with open(scan_path, "r", encoding="utf-8-sig") as f:
            scan = json.load(f)
    except (json.JSONDecodeError, IOError):
        return None
    if not isinstance(scan, dict):
        return None
    paths = set()
    for key in ("files", "filtered_out"):
        for f in scan.get(key) or []:
            if isinstance(f, dict) and isinstance(f.get("path"), str):
                paths.add(f["path"])
    return paths or None


def check_delta_contract(metadata) -> list:
    """
    Контракт delta_only (collect_vault_structure.py): в инкременте domains[].files и all_files -
    только дельта. Возвращает список ошибок; пустой/нечитаемый metadata - без ошибок
    (структуры может не быть).
    """
    if not isinstance(metadata, dict) or not metadata:
        return []
    errors = []
    mode = metadata.get("mode")
    delta_only = metadata.get("delta_only")
    if mode == "incremental" and delta_only is not True:
        errors.append("mode=incremental без delta_only: рассинхрон версий collect и synthesize, "
                      "перезапусти --collect --new-only")
    if delta_only is True and mode != "incremental":
        errors.append("противоречие: delta_only=true, а mode=" + str(mode) + " (ожидался incremental)")
    return errors


def count_accounted_files(taxonomy: dict):
    """Сколько файлов уже учтено в готовой taxonomy.json (None - неизвестно)."""
    if not isinstance(taxonomy, dict):
        return None
    meta = taxonomy.get("metadata") or {}
    stored = meta.get("all_files_count")
    if isinstance(stored, int):
        return stored
    total = meta.get("total_files")
    if not isinstance(total, int):
        return None
    return (
        total
        + section_count(taxonomy.get("projects"))
        + section_count(taxonomy.get("unclassified"))
        + section_count(taxonomy.get("filtered_out"))
    )


def resolve_all_files_count(metadata: dict, filtered_out, existing_taxonomy: dict, merge_mode: bool):
    """
    Сколько .md файлов в vault должна покрывать итоговая taxonomy.json.

    Полный прогон (и любая полная Фаза 1) - все .md vault, то есть
    кандидаты плюс отсеянный шум. Инкрементальная Фаза 1 - уже учтённое в
    прежней taxonomy.json плюс новые кандидаты и новый шум этого прогона:
    база прежней сборки тут единственный источник, отсканированного размера
    vault недостаточно, он не знает про удалённые файлы, а инкрементальный
    режим их и не отслеживает. None - число неизвестно, тогда проверка
    покрытия пропускается.
    """
    filtered_count = section_count(filtered_out)
    if merge_mode:
        base = count_accounted_files(existing_taxonomy) if existing_taxonomy else None
        if not metadata:
            # Фазы 1 в этом прогоне не было - остаётся только прежняя сборка
            return base
        if metadata.get("mode") == "incremental":
            if base is None:
                return None
            new_candidates = metadata.get("new_candidates_count")
            new_noise = metadata.get("new_filtered_count")
            return (
                base
                + (new_candidates if isinstance(new_candidates, int) else 0)
                + (new_noise if isinstance(new_noise, int) else 0)
            )
        # Фаза 1 отработала в полном режиме: vault отсканирован целиком,
        # прибавлять прежнюю базу нельзя - файлы в карте и в прежней сборке
        # одни и те же, и сумма разошлась бы вдвое
    scanned = metadata.get("scanned_md_count")
    if isinstance(scanned, int):
        return scanned
    candidates = metadata.get("candidate_count")
    if isinstance(candidates, int):
        return candidates + filtered_count
    total = metadata.get("total_files")
    if isinstance(total, int):
        return total + filtered_count
    return None


def backfill_non_domain_sections(taxonomy: dict, projects, unclassified, all_files_count) -> bool:
    """
    Дописать projects/unclassified и счётчики покрытия в готовую taxonomy.json,
    если их в ней ещё нет (старый формат, либо инкрементальный прогон, где
    домены не пересчитывались). Ничего не перезаписывает - только дополняет.
    """
    changed = False
    meta = taxonomy.setdefault("metadata", {})
    stats = taxonomy.setdefault("statistics", {})
    for key, normalize, raw in (
        ("projects", normalize_projects_section, projects),
        ("unclassified", normalize_unclassified_section, unclassified),
    ):
        if taxonomy.get(key) is None and raw is not None:
            section = normalize(raw)
            taxonomy[key] = section
            meta[key + "_count"] = section["count"]
            stats[key + "_count"] = section["count"]
            changed = True
    if not isinstance(meta.get("all_files_count"), int) and isinstance(all_files_count, int):
        meta["all_files_count"] = all_files_count
        changed = True
    return changed


def merge_domain_files(existing_files: list, new_files: list) -> list:
    """Объединить старые и новые файлы домена, дедуплицируя по path."""
    # Создать словарь по path для быстрого поиска
    existing_by_path = {f["path"]: f for f in existing_files if isinstance(f, dict) and "path" in f}
    new_by_path = {f["path"]: f for f in new_files if isinstance(f, dict) and "path" in f}

    # Новые анализы побеждают при коллизии
    existing_by_path.update(new_by_path)

    return list(existing_by_path.values())


def normalize_file_info(file_info: dict) -> dict:
    """Нормализовать данные файла: преобразовать списки в строки для type и paradigm."""
    if not isinstance(file_info, dict):
        return file_info

    # Нормализовать type: если список, взять первый элемент или пустую строку
    if "type" in file_info and isinstance(file_info["type"], list):
        file_info["type"] = file_info["type"][0] if file_info["type"] else "unknown"

    # Нормализовать paradigm: если список, взять первый элемент или None
    if "paradigm" in file_info and isinstance(file_info["paradigm"], list):
        file_info["paradigm"] = file_info["paradigm"][0] if file_info["paradigm"] else None

    # Нормализовать components: гарантировать что это массив
    if "components" in file_info and isinstance(file_info["components"], str):
        file_info["components"] = [file_info["components"]]
    elif "components" not in file_info:
        file_info["components"] = []

    # Нормализовать technologies: гарантировать что это массив
    if "technologies" in file_info and isinstance(file_info["technologies"], str):
        file_info["technologies"] = [file_info["technologies"]]
    elif "technologies" not in file_info:
        file_info["technologies"] = []

    return file_info


def load_analysis_files(temp_dir: Path) -> dict:
    """Загрузить все файлы анализа доменов."""
    analysis_data = {}

    for file in temp_dir.glob("*-analysis.json"):
        if file.name == "vault-structure-analysis.json":
            continue

        try:
            with open(file, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
                domain_key = file.stem.replace("-analysis", "")

                # Нормализовать структуру: если это массив, обернуть в объект с ключом "files"
                if isinstance(data, list):
                    data = {"files": data}

                # Нормализовать данные в файлах домена
                if isinstance(data, dict) and "files" in data:
                    normalized_files = []
                    for file_info in data["files"]:
                        normalized_files.append(normalize_file_info(file_info))
                    data["files"] = normalized_files

                analysis_data[domain_key] = data
        except (json.JSONDecodeError, IOError) as e:
            print(f"Ошибка чтения {file.name}: {e}", file=sys.stderr)

    return analysis_data


def extract_global_facets(analysis_data: dict) -> dict:
    """Выявить глобальные фасеты из анализов."""
    facets = {
        "types": set(),
        "paradigms": set(),
        "technologies": set(),
        "components": set(),
        "difficulties": set(),
        "domains": set(),
        "statuses": {"published", "wip", "draft", "archived"},
        "importance_levels": {"low", "medium", "high", "critical"}
    }

    for domain_key, domain_data in analysis_data.items():
        facets["domains"].add(domain_key)

        if isinstance(domain_data, dict) and "files" in domain_data:
            for file_info in domain_data["files"]:
                if isinstance(file_info, dict):
                    if file_info.get("type"):
                        facets["types"].add(file_info["type"])
                    if file_info.get("paradigm"):
                        facets["paradigms"].add(file_info["paradigm"])
                    if file_info.get("difficulty"):
                        difficulty_level = difficulty_to_level(file_info["difficulty"])
                        facets["difficulties"].add(difficulty_level)

                    if isinstance(file_info.get("components"), list):
                        for comp in file_info["components"]:
                            facets["components"].add(comp)

                    if isinstance(file_info.get("technologies"), list):
                        for tech in file_info["technologies"]:
                            facets["technologies"].add(tech)

    # Конвертировать sets в sorted lists
    result = {}
    for k, v in facets.items():
        if isinstance(v, set):
            result[k] = sorted(list(v), key=str)
        else:
            result[k] = v
    return result


def difficulty_to_level(difficulty) -> str:
    """Конвертировать числовой difficulty или строку в уровень сложности."""
    if isinstance(difficulty, int):
        if difficulty <= 1:
            return "beginner"
        elif difficulty <= 2:
            return "intermediate"
        else:
            return "advanced"
    elif isinstance(difficulty, str):
        difficulty_lower = difficulty.lower()
        if difficulty_lower in ("beginner", "начинающий", "базовый"):
            return "beginner"
        elif difficulty_lower in ("intermediate", "средний", "промежуточный"):
            return "intermediate"
        elif difficulty_lower in ("advanced", "продвинутый", "высокий"):
            return "advanced"
    return "intermediate"


def build_learning_paths(domain_files: list) -> dict:
    """Построить learning paths для домена."""
    # Сортировка по сложности
    beginner_files = []
    intermediate_files = []
    advanced_files = []

    for file_info in domain_files:
        if isinstance(file_info, dict):
            difficulty_raw = file_info.get("difficulty", "intermediate")
            difficulty = difficulty_to_level(difficulty_raw)
            path = file_info.get("path", "")

            if difficulty == "beginner":
                beginner_files.append(path)
            elif difficulty == "advanced":
                advanced_files.append(path)
            else:
                intermediate_files.append(path)

    return {
        "beginner": {
            "description": "From zero knowledge to understanding basics",
            "files": beginner_files,
            "estimated_hours": 4
        },
        "intermediate": {
            "description": "Deeper patterns and integration",
            "files": intermediate_files,
            "estimated_hours": 8
        },
        "advanced": {
            "description": "Paradigm comparison and optimization",
            "files": advanced_files,
            "estimated_hours": 6
        }
    }


CYRILLIC_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify(text: str) -> str:
    """Транслитерировать и привести строку к slug формату (kebab-case)."""
    if not text:
        return ""
    text = text.lower()
    text = "".join(CYRILLIC_TRANSLIT.get(ch, ch) for ch in text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text


def normalize_topic(topic: dict, used_ids: set) -> dict:
    """
    Нормализовать один объект темы к текущей схеме REFERENCE.md
    (см. feedback: старые анализы доменов используют "name"/"topic" вместо
    "topic_name" и "description" вместо "summary", без topic_id).
    """
    if not isinstance(topic, dict):
        return topic

    if not topic.get("topic_name"):
        topic["topic_name"] = topic.get("name") or topic.get("topic") or ""

    if not topic.get("summary"):
        topic["summary"] = topic.get("description", "") or ""

    if not topic.get("topic_id"):
        base_id = slugify(topic["topic_name"]) or "topic"
        topic_id = base_id
        suffix = 2
        while topic_id in used_ids:
            topic_id = f"{base_id}-{suffix}"
            suffix += 1
        topic["topic_id"] = topic_id
    used_ids.add(topic["topic_id"])

    topic.setdefault("mastery_percent", 0)
    topic.setdefault("mastery_rationale", "")
    topic.setdefault("has_notes", bool(topic.get("note_paths")))
    topic.setdefault("note_paths", [])
    topic.setdefault("related_domain_topics", [])

    return topic


def get_domain_topics(domain_data: dict) -> list:
    """Достать topics[] из domain_summary анализа домена (устойчиво к отсутствию поля)."""
    if not isinstance(domain_data, dict):
        return []
    domain_summary = domain_data.get("domain_summary")
    if not isinstance(domain_summary, dict):
        return []
    topics = domain_summary.get("topics")
    if not isinstance(topics, list):
        return []
    used_ids = {t["topic_id"] for t in topics if isinstance(t, dict) and t.get("topic_id")}
    return [normalize_topic(t, used_ids) for t in topics]


def tokenize_topic_name(name: str) -> set:
    """
    Токенизировать название темы для грубого сопоставления смежных тем между
    доменами. Используются 6-символьные префиксы слов, а не сами слова —
    без этого русские словоформы ("кэширование" / "кэширования") никогда не
    совпадут при точном сравнении. Это эвристика, а не морфологический анализ
    (без новых зависимостей, см. research.md п.4/п.6) — возможны как ложные
    срабатывания на разных словах с общим префиксом, так и пропуски на словах
    с непохожими префиксами при синонимах.
    """
    if not name:
        return set()
    words = re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]+", name.lower())
    return {w[:6] for w in words if len(w) > 3 and w not in TOPIC_NAME_STOPWORDS}


def compute_related_domain_topics(analysis_data: dict) -> None:
    """
    Сопоставить темы разных доменов по пересечению ключевых слов в topic_name
    и заполнить `related_domain_topics` каждой темы (мутирует topics in-place).

    Выполняется в Python, а не агентом: все домены Фазы 2 анализируются
    параллельно одним сообщением, поэтому агент одного домена не видит темы
    других доменов (см. research.md п.4).
    """
    indexed_topics = []
    for domain_id, domain_data in analysis_data.items():
        for topic in get_domain_topics(domain_data):
            if isinstance(topic, dict) and topic.get("topic_name") and topic.get("topic_id"):
                indexed_topics.append((domain_id, topic, tokenize_topic_name(topic["topic_name"])))

    for domain_id, topic, tokens in indexed_topics:
        related = []
        if tokens:
            for other_domain_id, other_topic, other_tokens in indexed_topics:
                if other_domain_id == domain_id:
                    continue
                if tokens & other_tokens:
                    ref = f"{other_domain_id}:{other_topic['topic_id']}"
                    if ref not in related:
                        related.append(ref)
        topic["related_domain_topics"] = related


def compute_recommended_topics(domain_id: str, topics: list, analysis_data: dict) -> list:
    """
    Построить отсортированный список рекомендаций для домена: сначала темы
    самого домена с has_notes=false или низким mastery_percent, затем смежные
    темы других доменов (через related_domain_topics), тоже неизученные/слабо
    проработанные (FR-009, FR-010; data-model.md -> RecommendedTopic).
    """
    recommendations = []

    for topic in topics:
        if not isinstance(topic, dict):
            continue
        mastery_percent = topic.get("mastery_percent", 0)
        has_notes = topic.get("has_notes", True)
        if not has_notes or mastery_percent < RECOMMENDATION_MASTERY_THRESHOLD:
            recommendations.append({
                "domain_id": domain_id,
                "topic_id": topic.get("topic_id"),
                "topic_name": topic.get("topic_name"),
                "mastery_percent": mastery_percent,
                "is_cross_domain": False,
                "reason": "не изучено" if not has_notes else "низкая проработка относительно уровня эксперта",
            })

    seen_cross_domain = set()
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        for ref in topic.get("related_domain_topics", []) or []:
            if ":" not in ref:
                continue
            other_domain_id, other_topic_id = ref.split(":", 1)
            other_domain_data = analysis_data.get(other_domain_id)
            if not other_domain_data:
                continue
            for other_topic in get_domain_topics(other_domain_data):
                if not isinstance(other_topic, dict) or other_topic.get("topic_id") != other_topic_id:
                    continue
                other_mastery = other_topic.get("mastery_percent", 0)
                other_has_notes = other_topic.get("has_notes", True)
                if other_has_notes and other_mastery >= RECOMMENDATION_MASTERY_THRESHOLD:
                    continue
                cross_key = (other_domain_id, other_topic_id)
                if cross_key in seen_cross_domain:
                    continue
                seen_cross_domain.add(cross_key)
                recommendations.append({
                    "domain_id": other_domain_id,
                    "topic_id": other_topic_id,
                    "topic_name": other_topic.get("topic_name"),
                    "mastery_percent": other_mastery,
                    "is_cross_domain": True,
                    "reason": f"смежно с «{topic.get('topic_name')}»",
                })

    recommendations.sort(key=lambda r: r.get("mastery_percent", 0))
    return recommendations


def generate_taxonomy_json(analysis_data: dict, metadata: dict, filtered_out: dict = None,
                           projects=None, unclassified=None) -> dict:
    """
    Генерировать финальную таксономию.

    projects и unclassified - файлы, которым архитектор не назначил домен.
    Раньше они не доходили до taxonomy.json вообще, и прогон не мог обнаружить
    их потерю (см. план 2026-10-04-accounted-non-domain-files).
    """

    global_facets = extract_global_facets(analysis_data)
    projects_section = normalize_projects_section(projects)
    unclassified_section = normalize_unclassified_section(unclassified)

    # Сопоставить смежные темы между доменами до построения объектов доменов
    # (мутирует topics внутри analysis_data in-place — см. research.md п.4)
    compute_related_domain_topics(analysis_data)

    domains = []
    total_files = 0

    for domain_key, domain_data in analysis_data.items():
        if not isinstance(domain_data, dict):
            continue

        files_in_domain = domain_data.get("files", [])
        total_files += len(files_in_domain)

        # Найти парадигмы для этого домена
        paradigms_set = set()
        for file_info in files_in_domain:
            if isinstance(file_info, dict) and file_info.get("paradigm"):
                paradigms_set.add(file_info["paradigm"])

        domain_topics = get_domain_topics(domain_data)

        domain_obj = {
            "domain_id": domain_key,
            "domain_name": domain_data.get("domain_name", domain_key.title()),
            "description": domain_data.get("description", ""),
            "file_count": len(files_in_domain),
            "paradigms": sorted(list(paradigms_set)),
            "files": files_in_domain,
            "learning_paths": build_learning_paths(files_in_domain),
            "topics": domain_topics,
            "recommended_topics": compute_recommended_topics(domain_key, domain_topics, analysis_data)
        }
        domains.append(domain_obj)

    # Вычислить статистику
    statistics = {
        # total_files намеренно считает только домены: от него зависят
        # verify_taxonomy и dashboard.calculateStats. Файлы вне доменов живут
        # в projects_count/unclassified_count и в metadata.all_files_count.
        "total_files": total_files,
        "projects_count": projects_section["count"],
        "unclassified_count": unclassified_section["count"],
        "files_per_domain": {d["domain_id"]: d["file_count"] for d in domains},
        "files_per_type": {},
        "files_per_difficulty": {
            "beginner": 0,
            "intermediate": 0,
            "advanced": 0
        }
    }

    # Подсчитать файлы по типам и сложности
    for domain in domains:
        for file_info in domain["files"]:
            if isinstance(file_info, dict):
                file_type = file_info.get("type", "unknown")
                statistics["files_per_type"][file_type] = \
                    statistics["files_per_type"].get(file_type, 0) + 1

                difficulty_raw = file_info.get("difficulty", "intermediate")
                difficulty = difficulty_to_level(difficulty_raw)
                statistics["files_per_difficulty"][difficulty] += 1

    taxonomy_metadata = {
        "vault_path": metadata.get("vault_path", ""),
        "analysis_date": datetime.now().strftime("%Y-%m-%d"),
        "total_files": total_files,
        "total_domains": len(domains),
        "projects_count": projects_section["count"],
        "unclassified_count": unclassified_section["count"],
        "analyzer_notes": "Full semantic taxonomy based on file content analysis"
    }
    # all_files_count - база для проверки покрытия. В старых taxonomy.json его
    # нет, поэтому поле не добавляется, а проверка помечается SKIP.
    all_files_count = metadata.get("all_files_count")
    if isinstance(all_files_count, int):
        taxonomy_metadata["all_files_count"] = all_files_count

    taxonomy = {
        "metadata": taxonomy_metadata,
        "domains": domains,
        "global_facets": global_facets,
        "statistics": statistics,
        "projects": projects_section,
        "unclassified": unclassified_section,
        "filtered_out": filtered_out or {"count": 0, "files": []}
    }

    return taxonomy




def verify_taxonomy(taxonomy: dict, known_paths=None, strict_paths: bool = False) -> dict:
    """
    Верификация целостности таксономии.

    known_paths - пути файлов, которые реально есть в vault (кандидаты плюс
    отсеянный шум). Без них сверка учтённых путей пропускается.
    strict_paths - трактовать несовпадение как ошибку, а не предупреждение.
    Так делает полный прогон, где скан свежий и расхождение означает опечатку
    в карте доменов или в анализе. В инкрементальном прогоне удалённые из
    vault файлы остаются в таксономии законно (см. "Ограничения режима
    Incremental" в SKILL.md), поэтому там это предупреждение.
    """

    checks = {
        "total_files_count": "OK",
        "domains_consistency": "OK",
        "no_orphaned_files": "OK",
        "required_fields": "OK",
        "topic_required_fields": "OK",
        "candidates_accounted": "OK",
        "files_exist": "OK",
    }

    total_files = taxonomy["metadata"]["total_files"]
    files_in_domains = sum(d["file_count"] for d in taxonomy["domains"])

    if total_files != files_in_domains:
        checks["total_files_count"] = f"WARN: {total_files} != {files_in_domains}"

    # Проверить обязательные поля в файлах
    orphaned = 0
    missing_title = 0
    for domain in taxonomy["domains"]:
        for file_info in domain["files"]:
            if isinstance(file_info, dict):
                required = ["path", "type", "title"]
                if not all(field in file_info for field in required):
                    orphaned += 1
                    checks["required_fields"] = f"WARN: {orphaned} files missing required fields"
                elif file_info.get("title") is None:
                    missing_title += 1

    if missing_title > 0:
        checks["title_completeness"] = f"INFO: {missing_title} files have null title (partial analysis)"

    # Проверить обязательные поля в темах доменов (data-model.md -> Topic; см. регрессию schema drift)
    bad_topics = 0
    bad_domains = set()
    for domain in taxonomy["domains"]:
        for topic in domain.get("topics", []):
            if not isinstance(topic, dict):
                bad_topics += 1
                bad_domains.add(domain.get("domain_id"))
                continue
            required_topic_fields = ["topic_id", "topic_name", "summary"]
            if not all(topic.get(field) for field in required_topic_fields):
                bad_topics += 1
                bad_domains.add(domain.get("domain_id"))

    if bad_topics > 0:
        checks["topic_required_fields"] = (
            f"WARN: {bad_topics} topics missing topic_id/topic_name/summary "
            f"in {len(bad_domains)} domains: {', '.join(sorted(str(d) for d in bad_domains))}"
        )

    # Покрытие: каждый .md файл vault обязан оказаться либо в домене, либо в
    # projects, либо в unclassified, либо в filtered_out. Без этой проверки
    # пропавшие файлы не давали о себе знать нигде (54 + 0 + 0 + 4 = 58 != 75).
    projects_count = section_count(taxonomy.get("projects"))
    unclassified_count = section_count(taxonomy.get("unclassified"))
    filtered_count = section_count(taxonomy.get("filtered_out"))
    accounted = total_files + projects_count + unclassified_count + filtered_count
    all_files_count = (taxonomy.get("metadata") or {}).get("all_files_count")
    accounting = (
        f"{total_files} (в доменах) + {projects_count} (projects) "
        f"+ {unclassified_count} (unclassified) + {filtered_count} (filtered_out) "
        f"= {accounted}"
    )
    if not isinstance(all_files_count, int):
        checks["candidates_accounted"] = "SKIP: в metadata нет all_files_count (старый формат taxonomy.json)"
    elif accounted == all_files_count:
        checks["candidates_accounted"] = "OK: " + accounting
    else:
        checks["candidates_accounted"] = f"FAIL: {accounting} != {all_files_count} .md в vault"

    # Сверка идентичности, а не только количества: опечатка в пути (кириллица,
    # регистр, лишний пробел) даёт верный счёт и при этом настоящий файл
    # остаётся неучтённым - именно так в прогоне 2026-10-04 потерялся путь
    # "...Параллельный доступ.md" из домена postgresql-query-optimization.
    if known_paths is None:
        checks["files_exist"] = "SKIP: пути vault не переданы"
    else:
        accounted_paths = collect_accounted_paths(taxonomy)
        unknown = sorted(accounted_paths - set(known_paths))
        if unknown:
            shown = ", ".join(unknown[:3]) + (", ..." if len(unknown) > 3 else "")
            message = f"{len(unknown)} учтённых путей нет в vault: {shown}"
            checks["files_exist"] = ("FAIL: " if strict_paths else "WARN: ") + message
        else:
            checks["files_exist"] = f"OK: {len(accounted_paths)} путей сверены с vault"

    return checks


def print_checks(checks: dict) -> None:
    """Напечатать результаты верификации."""
    print("\n[CHECK] Верификация:", file=sys.stderr)
    for check, result in checks.items():
        print(f"  - {check}: {result}", file=sys.stderr)


def verify_file(taxonomy_path: Path) -> int:
    """
    Прогнать verify_taxonomy по готовому файлу, ничего не пересобирая.

    Нужен, чтобы проверить чужой или урезанный taxonomy.json: сам прогон
    всегда перезаписывает файл, и увидеть, как верификация ловит потерю
    файлов, иначе невозможно.
    """
    if not taxonomy_path.exists():
        print(f"[ERROR] Файл не найден: {taxonomy_path}", file=sys.stderr)
        return 1
    with open(taxonomy_path, "r", encoding="utf-8-sig") as f:
        taxonomy = json.load(f)
    print(f"[INFO] Проверка файла: {taxonomy_path}", file=sys.stderr)
    known_paths = None
    strict_paths = False
    structure_path = taxonomy_path.parent / "vault-structure-analysis.json"
    if structure_path.exists():
        try:
            with open(structure_path, "r", encoding="utf-8") as f:
                structure = json.load(f)
            known_paths = known_paths_from_structure(structure)
            strict_paths = (structure.get("metadata") or {}).get("mode") != "incremental"
        except (json.JSONDecodeError, IOError):
            structure = None
            known_paths = None
        if isinstance(structure, dict):
            delta_errors = check_delta_contract(structure.get("metadata"))
            if delta_errors:
                for e in delta_errors:
                    print(f"[FAIL] контракт delta_only: {e}", file=sys.stderr)
                return 1
            if not strict_paths:
                # Инкремент: all_files - только дельта, поэтому known_paths берём из скана
                # (все пути vault: кандидаты и шум). strict_paths остаётся False:
                # удалённые старые файлы - WARN.
                scan_known = known_paths_from_scan(taxonomy_path.parent / "vault-scan.json")
                if scan_known:
                    known_paths = scan_known
    checks = verify_taxonomy(taxonomy, known_paths, strict_paths)
    print_checks(checks)
    failed = [name for name, result in checks.items() if str(result).startswith("FAIL")]
    if failed:
        print(f"[FAIL] Провалено проверок: {len(failed)} - {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


# Имена входов и выходов режима --merge
TAXONOMY_FILENAME = "taxonomy.json"
TAXONOMY_BEFORE_FILENAME = "taxonomy.before.json"
SCAN_FILENAME = "vault-scan.json"
DOMAIN_MAP_FILENAME = "domain-map.json"
MERGE_MAP_FILENAME = "domain-merge-map.json"
AFFECTED_FILENAME = "affected-domains.json"
GRAPH_FILENAME = "topic-graph.json"
STRUCTURE_FILENAME = "vault-structure-analysis.json"
CACHE_FILENAME = "semantic-analysis-cache.json"
DELTA_STATUSES = ("new", "changed")
# Производные поля: пересчитываются по всем доменам, из сравнения неизменности исключены
DERIVED_DOMAIN_FIELDS = ("recommended_topics",)
DERIVED_TOPIC_FIELDS = ("related_domain_topics",)


class MergeError(Exception):
    """Ошибка входов или раскладки --merge: сообщение печатается как есть, код выхода 1."""


def read_json_file(path: Path, default=None):
    """Прочитать JSON. Отсутствие файла - default, повреждённый файл - MergeError."""
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        raise MergeError("Не читается " + path.name + ": " + str(e))


def write_json_file(path: Path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def canonical_json(obj) -> str:
    """Канонический JSON для сравнения записей (порядок ключей не важен)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def resolve_domain_id(domain_id, merge_map: dict):
    """Старый id домена -> актуальный по domain-merge-map.json (цепочки разворачиваются)."""
    seen = set()
    while isinstance(domain_id, str) and domain_id in merge_map and domain_id not in seen:
        seen.add(domain_id)
        domain_id = merge_map[domain_id]
    return domain_id


def check_merge_inputs(temp_dir: Path) -> list:
    """
    Контракт входов --merge (инвариант 3a): все входы на месте, а topic-graph.json
    новее vault-scan.json и domain-map.json (граф строится после Фаз 2, 3 и 3б).
    Возвращает список ошибок (пусто - входы в порядке).
    """
    errors = []
    required = (TAXONOMY_FILENAME, SCAN_FILENAME, DOMAIN_MAP_FILENAME, AFFECTED_FILENAME,
                GRAPH_FILENAME, STRUCTURE_FILENAME)
    for name in required:
        if not (temp_dir / name).exists():
            errors.append("Нет входа " + name + " (" + str(temp_dir / name) + ")")
    graph_path = temp_dir / GRAPH_FILENAME
    if graph_path.exists():
        graph_mtime = graph_path.stat().st_mtime
        for name in (SCAN_FILENAME, DOMAIN_MAP_FILENAME):
            path = temp_dir / name
            if path.exists() and graph_mtime < path.stat().st_mtime:
                errors.append(GRAPH_FILENAME + " старше " + name +
                              ": пересобери граф (build_topic_graph.py) после Фаз 1-3б")
    return errors


def load_validated_topics(temp_dir: Path, domain_id: str) -> dict:
    """{domain_id}-topics.validated.json затронутого домена (Фаза 3б). Нет файла - MergeError."""
    name = domain_id + "-topics.validated.json"
    doc = read_json_file(temp_dir / name)
    if doc is None:
        raise MergeError("Затронутый домен " + domain_id + ": нет " + name + " (Фаза 3б не выполнена)")
    if not isinstance(doc, dict) or not isinstance(doc.get("topics"), list) or not doc["topics"]:
        raise MergeError(name + ": нет непустого списка topics")
    return doc


def pick_file_record(path: str, domain_id: str, delta_paths: set, old_by_path: dict,
                     analysis_by_domain: dict, warnings: list) -> dict:
    """
    Запись файла для сборки домена. Дельта (status new|changed) - из {domain}-analysis.json,
    остальные - прежняя запись из taxonomy.json (в т.ч. из другого домена, если файл переехал).
    Файл, которого нет в taxonomy.json и нет в дельте, берётся из анализа с предупреждением.
    """
    analysed = (analysis_by_domain.get(domain_id) or {}).get(path)
    if analysed is None:
        for records in analysis_by_domain.values():
            if path in records:
                analysed = records[path]
                break
    old = old_by_path.get(path)
    if path in delta_paths:
        if analysed is not None:
            return analysed
        if old is not None:
            warnings.append("нет анализа для файла дельты, оставлена прежняя запись: " + path)
            return old
        raise MergeError("Нет записи файла " + path + ": он в дельте (new|changed), но не найден ни в одном "
                         "{domain}-analysis.json, ни в taxonomy.json")
    if old is not None:
        return old
    if analysed is not None:
        warnings.append("файла нет в taxonomy.json, запись взята из анализа: " + path)
        return analysed
    raise MergeError("Нет записи файла " + path + ": его нет ни в taxonomy.json, ни в {domain}-analysis.json")


def layout_domain_paths(map_domain: dict) -> list:
    """Пути файлов домена из domain-map.json в порядке тем, без повторов."""
    paths = []
    for topic in map_domain.get("topics", []) or []:
        for p in topic.get("files", []) or []:
            if isinstance(p, str) and p not in paths:
                paths.append(p)
    return paths


def build_merged_analysis(temp_dir: Path, existing: dict, domain_map: dict, merge_map: dict,
                          affected_ids: set, scan_status: dict, warnings: list):
    """
    Собрать analysis_data для generate_taxonomy_json из актуальной раскладки (domain-map.json).
    Затронутый домен: files = старые записи из taxonomy.json плюс дельта, темы и description из
    {domain}-topics.validated.json. Незатронутый: запись taxonomy.json как есть.
    Возвращает (analysis_data, old_domains_by_new_id).
    """
    delta_paths = {p for p, st in scan_status.items() if st in DELTA_STATUSES}
    old_by_path = {}
    old_by_new_id = {}
    for old_dom in existing.get("domains", []) or []:
        new_id = resolve_domain_id(old_dom.get("domain_id"), merge_map)
        old_by_new_id.setdefault(new_id, []).append(old_dom)
        for f in old_dom.get("files", []) or []:
            if isinstance(f, dict) and isinstance(f.get("path"), str):
                old_by_path.setdefault(f["path"], f)

    analysis_raw = load_analysis_files(temp_dir)
    analysis_by_domain = {}
    for key, data in analysis_raw.items():
        analysis_by_domain[key] = {f["path"]: f for f in data.get("files", []) or []
                                   if isinstance(f, dict) and isinstance(f.get("path"), str)}

    merged = {}
    for map_dom in domain_map.get("domains", []) or []:
        domain_id = map_dom.get("id")
        if not isinstance(domain_id, str) or not domain_id:
            raise MergeError("domain-map.json: домен без id")
        if domain_id in merged:
            raise MergeError("domain-map.json: повтор домена " + domain_id)
        old_doms = old_by_new_id.get(domain_id, [])
        if domain_id in affected_ids:
            validated = load_validated_topics(temp_dir, domain_id)
            files = []
            for path in layout_domain_paths(map_dom):
                files.append(pick_file_record(path, domain_id, delta_paths, old_by_path,
                                              analysis_by_domain, warnings))
            description = validated.get("description") or \
                (old_doms[0].get("description") if len(old_doms) == 1 else "") or map_dom.get("purpose", "")
            merged[domain_id] = {
                "files": files,
                "domain_name": map_dom.get("name") or domain_id,
                "description": description,
                "domain_summary": {"topics": validated["topics"]},
            }
        else:
            if len(old_doms) != 1:
                raise MergeError("Домен " + domain_id + " не в " + AFFECTED_FILENAME + ", но "
                                 + ("его нет в taxonomy.json" if not old_doms else
                                    "в него слиты несколько доменов taxonomy.json")
                                 + ": такой домен должен быть затронутым")
            old_dom = old_doms[0]
            renamed = old_dom.get("domain_id") != domain_id
            merged[domain_id] = {
                "files": old_dom.get("files", []),
                "domain_name": (map_dom.get("name") or domain_id) if renamed
                else old_dom.get("domain_name", domain_id),
                "description": old_dom.get("description", ""),
                "domain_summary": {"topics": old_dom.get("topics", [])},
            }
    return merged, old_by_new_id


def strip_derived(domain: dict, drop_identity: bool = False) -> dict:
    """Копия записи домена без производных полей (и без id/названия, если домен переименован)."""
    copy_ = json.loads(json.dumps(domain))
    for field in DERIVED_DOMAIN_FIELDS:
        copy_.pop(field, None)
    if drop_identity:
        copy_.pop("domain_id", None)
        copy_.pop("domain_name", None)
    for topic in copy_.get("topics", []) or []:
        if isinstance(topic, dict):
            for field in DERIVED_TOPIC_FIELDS:
                topic.pop(field, None)
    return copy_


def check_unaffected_unchanged(taxonomy: dict, existing_snapshot: dict, old_by_new_id: dict,
                               affected_ids: set, merge_map: dict) -> list:
    """
    Инвариант 3c: записи доменов вне affected-domains.json совпадают с прежними
    (канонический JSON), кроме recommended_topics и related_domain_topics.
    У переименованного домена не сравниваются domain_id и domain_name. Прежний домен,
    пропавший из итога без слияния/переименования, тоже ошибка.
    """
    errors = []
    new_by_id = {d["domain_id"]: d for d in taxonomy.get("domains", [])}
    for old_dom in existing_snapshot.get("domains", []) or []:
        old_id = old_dom.get("domain_id")
        new_id = resolve_domain_id(old_id, merge_map)
        if new_id in affected_ids:
            continue
        new_dom = new_by_id.get(new_id)
        if new_dom is None:
            errors.append("Домен " + str(old_id) + " пропал из taxonomy.json, хотя не затронут (нет в domain-map.json)")
            continue
        renamed = new_id != old_id
        if canonical_json(strip_derived(old_dom, renamed)) != canonical_json(strip_derived(new_dom, renamed)):
            errors.append("Незатронутый домен " + str(new_id) + " изменился относительно прежней taxonomy.json")
    return errors


def check_layout_matches_old(domain_map: dict, old_by_new_id: dict, affected_ids: set) -> list:
    """Раскладка незатронутого домена в domain-map.json обязана совпадать с его файлами в taxonomy.json."""
    errors = []
    for map_dom in domain_map.get("domains", []) or []:
        domain_id = map_dom.get("id")
        old_doms = old_by_new_id.get(domain_id, [])
        if domain_id in affected_ids or len(old_doms) != 1:
            continue
        old_paths = {f["path"] for f in old_doms[0].get("files", []) if isinstance(f, dict) and "path" in f}
        new_paths = set(layout_domain_paths(map_dom))
        if old_paths != new_paths:
            errors.append("Домен " + str(domain_id) + " не в " + AFFECTED_FILENAME +
                          ", но его файлы в domain-map.json отличаются от taxonomy.json (добавлено " +
                          str(len(new_paths - old_paths)) + ", убрано " + str(len(old_paths - new_paths)) + ")")
    return errors


def check_topic_consistency(taxonomy: dict) -> list:
    """
    Инвариант 3d: has_notes == bool(note_paths), каждый note_path есть среди файлов домена,
    каждый файл домена привязан хотя бы к одной теме (файл может подкреплять несколько тем).
    """
    errors = []
    for domain in taxonomy.get("domains", []) or []:
        domain_id = domain.get("domain_id")
        file_paths = [f["path"] for f in domain.get("files", []) if isinstance(f, dict) and "path" in f]
        file_set = set(file_paths)
        owners = {}
        for topic in domain.get("topics", []) or []:
            if not isinstance(topic, dict):
                continue
            tid = topic.get("topic_id")
            note_paths = topic.get("note_paths") or []
            if bool(topic.get("has_notes")) != bool(note_paths):
                errors.append(str(domain_id) + ":" + str(tid) + ": has_notes=" +
                              json.dumps(topic.get("has_notes")) + ", а note_paths " +
                              ("пуст" if not note_paths else "непуст"))
            for p in note_paths:
                if p not in file_set:
                    errors.append(str(domain_id) + ":" + str(tid) + ": note_path не из файлов домена: " + str(p))
                owners.setdefault(p, []).append(tid)
        for p in file_paths:
            count = len(owners.get(p, []))
            if count == 0:
                errors.append(str(domain_id) + ": файл не привязан ни к одной теме: " + p)
    return errors


def failed_checks(checks: dict) -> list:
    return [name for name, result in checks.items() if str(result).startswith("FAIL")]


def save_taxonomy_with_backup(temp_dir: Path, taxonomy: dict) -> Path:
    """Инвариант 3e: перед записью taxonomy.json копия прежнего файла в taxonomy.before.json."""
    taxonomy_path = temp_dir / TAXONOMY_FILENAME
    if taxonomy_path.exists():
        shutil.copy2(taxonomy_path, temp_dir / TAXONOMY_BEFORE_FILENAME)
        print("[BACKUP] " + TAXONOMY_BEFORE_FILENAME, file=sys.stderr)
    write_json_file(taxonomy_path, taxonomy)
    print("\n[SAVE] Сохранено: " + str(taxonomy_path), file=sys.stderr)
    return taxonomy_path


def update_semantic_cache(temp_dir: Path, taxonomy: dict) -> int:
    """
    Инвариант 3f: кеш hash -> {domain_id, essence, key_concepts} обновляется только после
    успешной записи taxonomy.json. Хеши путей берутся из vault-scan.json, domain_id в кеше
    приводится к актуальному по domain-merge-map.json. Возвращает число записанных записей.
    """
    scan = read_json_file(temp_dir / SCAN_FILENAME)
    if not isinstance(scan, dict):
        print("[WARN] Нет " + SCAN_FILENAME + ": кеш " + CACHE_FILENAME + " не обновлён", file=sys.stderr)
        return 0
    hash_by_path = {e["path"]: e.get("content_hash") for e in scan.get("files", []) or []
                    if isinstance(e, dict) and isinstance(e.get("path"), str)}
    cache = read_json_file(temp_dir / CACHE_FILENAME, {})
    if not isinstance(cache, dict):
        cache = {}
    updated = 0
    for domain in taxonomy.get("domains", []) or []:
        for f in domain.get("files", []) or []:
            if not isinstance(f, dict):
                continue
            content_hash = hash_by_path.get(f.get("path"))
            if not content_hash:
                continue
            cache[content_hash] = {
                "domain_id": domain["domain_id"],
                "essence": f.get("essence", ""),
                "key_concepts": f.get("key_concepts", []) or [],
            }
            updated += 1
    merge_map = read_json_file(temp_dir / MERGE_MAP_FILENAME, {}) or {}
    if merge_map:
        for entry in cache.values():
            if isinstance(entry, dict) and entry.get("domain_id") in merge_map:
                entry["domain_id"] = resolve_domain_id(entry["domain_id"], merge_map)
    write_json_file(temp_dir / CACHE_FILENAME, cache)
    suffix = (", применено слияний: " + str(len(merge_map))) if merge_map else ""
    print("[OK] Кеш обновлён после записи taxonomy.json: " + str(updated) + " записей" + suffix, file=sys.stderr)
    return updated


def run_merge(temp_dir: Path, dry_run: bool) -> int:
    """
    Фаза 5: слияние в taxonomy.json без LLM. Раскладка - domain-map.json, затронутые домены -
    affected-domains.json и {domain}-topics.validated.json, дельта - {domain}-analysis.json.
    Запись taxonomy.json только если инварианты 3a-3d выполнены. Коды выхода: 0 - OK,
    1 - нарушен инвариант, 2 - нет входов или они устарели.
    """
    print("\n[MERGE] Режим: INCREMENTAL MERGE (--merge)" + (" --dry-run" if dry_run else ""), file=sys.stderr)

    errors = check_merge_inputs(temp_dir)
    if errors:
        for e in errors:
            print("[FAIL] контракт входов: " + e, file=sys.stderr)
        print("[FAIL] --merge не запущен, taxonomy.json не изменён", file=sys.stderr)
        return 2

    existing = read_json_file(temp_dir / TAXONOMY_FILENAME)
    existing_snapshot = json.loads(json.dumps(existing))
    domain_map = read_json_file(temp_dir / DOMAIN_MAP_FILENAME)
    scan = read_json_file(temp_dir / SCAN_FILENAME)
    structure = read_json_file(temp_dir / STRUCTURE_FILENAME)
    delta_errors = check_delta_contract(structure.get("metadata") if isinstance(structure, dict) else None)
    if delta_errors:
        for e in delta_errors:
            print("[FAIL] контракт delta_only: " + e, file=sys.stderr)
        print("[FAIL] --merge не запущен, taxonomy.json не изменён", file=sys.stderr)
        return 2
    affected_raw = read_json_file(temp_dir / AFFECTED_FILENAME)
    merge_map = read_json_file(temp_dir / MERGE_MAP_FILENAME, {}) or {}
    if not isinstance(merge_map, dict):
        merge_map = {}

    scan_files = scan.get("files") if isinstance(scan, dict) else None
    if not isinstance(scan_files, list):
        raise MergeError(SCAN_FILENAME + " не читается или без списка files")
    scan_status = {e["path"]: e.get("status") for e in scan_files
                   if isinstance(e, dict) and isinstance(e.get("path"), str)}
    if any(st not in ("new", "changed", "unchanged") for st in scan_status.values()):
        raise MergeError(SCAN_FILENAME + ": у файлов нет status new|changed|unchanged (перезапусти --scan)")
    affected_ids = {resolve_domain_id(d.get("domain_id"), merge_map)
                    for d in (affected_raw or {}).get("domains", []) or []
                    if isinstance(d, dict) and d.get("domain_id")}
    print("[MERGE] Затронутые домены: " + (", ".join(sorted(affected_ids)) or "нет"), file=sys.stderr)

    warnings = []
    analysis_data, old_by_new_id = build_merged_analysis(
        temp_dir, existing, domain_map, merge_map, affected_ids, scan_status, warnings)
    for w in warnings:
        print("[WARN] " + w, file=sys.stderr)

    layout_errors = check_layout_matches_old(domain_map, old_by_new_id, affected_ids)

    metadata_struct = structure.get("metadata", {}) if isinstance(structure, dict) else {}
    filtered_out = (structure or {}).get("filtered_out") or existing.get("filtered_out")
    # domain-map.json после apply_domain_patch - полная раскладка, включая projects/unclassified
    projects = normalize_projects_section(
        domain_map["projects"] if domain_map.get("projects") is not None else existing.get("projects"))
    unclassified = normalize_unclassified_section(
        domain_map["unclassified"] if domain_map.get("unclassified") is not None else existing.get("unclassified"))
    all_files_count = resolve_all_files_count(metadata_struct, filtered_out, existing, True)
    metadata = dict(existing.get("metadata", {}))
    if isinstance(all_files_count, int):
        metadata["all_files_count"] = all_files_count

    taxonomy = generate_taxonomy_json(analysis_data, metadata, filtered_out, projects, unclassified)
    taxonomy["metadata"]["analyzer_notes"] = "Incremental update: layout, delta and reassessed topics merged into existing taxonomy"
    taxonomy["metadata"]["mode"] = "incremental"
    for domain in taxonomy["domains"]:
        print("  - " + domain["domain_id"] + ": " + str(domain["file_count"]) + " файлов, тем " +
              str(len(domain["topics"])) + (" (затронут)" if domain["domain_id"] in affected_ids else ""),
              file=sys.stderr)

    # Инвариант 3b: арифметика покрытия и сверка путей перед записью
    known_paths = set(scan_status) | {f["path"] for f in scan.get("filtered_out", []) or []
                                      if isinstance(f, dict) and isinstance(f.get("path"), str)}
    checks = verify_taxonomy(taxonomy, known_paths, False)
    # Удалённые из vault старые файлы остаются предупреждением (SKILL.md), а путь, которого
    # не было ни в прежней таксономии, ни в скане, - опечатка раскладки и ошибка
    old_paths = collect_accounted_paths(existing_snapshot)
    typos = sorted(collect_accounted_paths(taxonomy) - known_paths - old_paths)
    if typos:
        checks["files_exist"] = "FAIL: " + str(len(typos)) + " новых путей нет в vault: " + ", ".join(typos[:3])
    print_checks(checks)

    problems = []
    for name in failed_checks(checks):
        problems.append("проверка " + name + ": " + str(checks[name]))
    problems += ["неизменность незатронутого: " + e for e in layout_errors]
    problems += ["неизменность незатронутого: " + e for e in
                 check_unaffected_unchanged(taxonomy, existing_snapshot, old_by_new_id, affected_ids, merge_map)]
    problems += ["согласованность тем: " + e for e in check_topic_consistency(taxonomy)]
    if problems:
        for p in problems:
            print("[FAIL] " + p, file=sys.stderr)
        print("[FAIL] Нарушено инвариантов: " + str(len(problems)) + ". taxonomy.json и кеш не изменены", file=sys.stderr)
        return 1

    if dry_run:
        print("[DRY-RUN] Инварианты выполнены: доменов " + str(len(taxonomy["domains"])) + ", файлов " +
              str(taxonomy["statistics"]["total_files"]) + ". Ничего не записано", file=sys.stderr)
        return 0

    save_taxonomy_with_backup(temp_dir, taxonomy)
    update_semantic_cache(temp_dir, taxonomy)
    print("\n[SUCCESS] Слияние в taxonomy.json завершено", file=sys.stderr)
    return 0


def option_value(flag: str):
    """Значение аргумента '--flag <значение>'. None, если флага нет."""
    if flag in sys.argv:
        pos = sys.argv.index(flag) + 1
        if pos < len(sys.argv) and not sys.argv[pos].startswith("-"):
            return sys.argv[pos]
    return None


def main():
    """Главный поток синтеза."""

    try:
        temp_dir_arg = option_value("--temp-dir")
        if temp_dir_arg:
            temp_dir = Path(temp_dir_arg)
        else:
            project_root = get_project_root()
            temp_dir = project_root / ".claude" / "temp_files"
            print(f"[INFO] Корень проекта: {project_root}", file=sys.stderr)
        print(f"[INFO] Директория temp_files: {temp_dir}", file=sys.stderr)

        if "--verify" in sys.argv:
            # путь можно не указывать - тогда проверяется штатный taxonomy.json
            after = sys.argv[sys.argv.index("--verify") + 1:]
            target = Path(after[0]) if after and not after[0].startswith("-") else temp_dir / "taxonomy.json"
            return verify_file(target)

        merge_mode = "--merge" in sys.argv
        dry_run = "--dry-run" in sys.argv
        if dry_run and not merge_mode:
            print("[ERROR] --dry-run поддерживается только вместе с --merge", file=sys.stderr)
            return 1

        if merge_mode:
            if (temp_dir / TAXONOMY_FILENAME).exists():
                return run_merge(temp_dir, dry_run)
            print(f"[WARN] Файл taxonomy.json не найден. Переключение на полный режим.", file=sys.stderr)

        # Данные Фазы 1 полного прогона
        vault_structure_file = temp_dir / "vault-structure-analysis.json"
        metadata = {}
        filtered_out = None
        projects = None
        unclassified = None
        vault_data = None
        if vault_structure_file.exists():
            try:
                with open(vault_structure_file, "r", encoding="utf-8") as f:
                    vault_data = json.load(f)
                metadata = vault_data.get("metadata", {})
                filtered_out = vault_data.get("filtered_out")
                projects = vault_data.get("projects")
                unclassified = vault_data.get("unclassified")
            except (json.JSONDecodeError, IOError) as e:
                print(f"[WARN] Ошибка чтения {vault_structure_file.name}: {e}", file=sys.stderr)

        # Пути файлов vault для сверки учтённых файлов с настоящими: в полном
        # прогоне скан свежий, расхождение это опечатка
        known_paths = known_paths_from_structure(vault_data)
        strict_paths = (metadata.get("mode") != "incremental")
        delta_errors = check_delta_contract(metadata)
        if delta_errors:
            for e in delta_errors:
                print(f"[ERROR] контракт delta_only: {e}", file=sys.stderr)
            return 1

        print(f"\n[FULL] Режим: FULL (полный пересбор)", file=sys.stderr)

        # === ФАЗА 3-6 ===
        print("\n[PROCESS] Фаза 3-6: Синтез, проектирование, генерация, верификация...",
              file=sys.stderr)

        # Загрузить анализы
        analysis_data = load_analysis_files(temp_dir)
        if not analysis_data:
            print("[ERROR] Файлы анализа доменов не найдены", file=sys.stderr)
            return 1

        print(f"[OK] Загружено {len(analysis_data)} доменов для анализа", file=sys.stderr)

        projects = normalize_projects_section(projects)
        unclassified = normalize_unclassified_section(unclassified)

        # Сколько .md в vault должна покрывать итоговая taxonomy.json - база
        # для проверки candidates_accounted.
        all_files_count = resolve_all_files_count(metadata, filtered_out, None, False)
        metadata = dict(metadata)
        if isinstance(all_files_count, int):
            metadata["all_files_count"] = all_files_count

        taxonomy = generate_taxonomy_json(analysis_data, metadata, filtered_out, projects, unclassified)
        taxonomy['metadata']['analyzer_notes'] = "Full semantic taxonomy based on file content analysis"
        taxonomy['metadata']['mode'] = 'full'

        # Верификация: запись taxonomy.json только если candidates_accounted и files_exist не FAIL
        checks = verify_taxonomy(taxonomy, known_paths, strict_paths)
        print_checks(checks)
        failed = failed_checks(checks)
        if failed:
            print(f"[FAIL] Провалено проверок: {len(failed)} - {', '.join(failed)}. "
                  "taxonomy.json и кеш не изменены", file=sys.stderr)
            return 1

        # Сохранить taxonomy.json (с копией прежнего в taxonomy.before.json), затем обновить кеш
        save_taxonomy_with_backup(temp_dir, taxonomy)
        update_semantic_cache(temp_dir, taxonomy)

        print("\n[SUCCESS] Синтез таксономии завершён", file=sys.stderr)
        return 0

    except MergeError as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[ERROR] Ошибка: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
