#!/usr/bin/env python3
"""
Синтез таксономии (портирование phase3-6-synthesis.js на Python).

Фазы 3-6:
  3. Синтез: выявление глобальных фасетов и иерархий
  4-5. Генерация: создание taxonomy.json
  6. Верификация: проверка целостности
"""

import json
import re
import sys
import io
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


def generate_taxonomy_json(analysis_data: dict, metadata: dict, filtered_out: dict = None) -> dict:
    """Генерировать финальную таксономию."""

    global_facets = extract_global_facets(analysis_data)

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
        "total_files": total_files,
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

    taxonomy = {
        "metadata": {
            "vault_path": metadata.get("vault_path", ""),
            "analysis_date": datetime.now().strftime("%Y-%m-%d"),
            "total_files": total_files,
            "total_domains": len(domains),
            "analyzer_notes": "Full semantic taxonomy based on file content analysis"
        },
        "domains": domains,
        "global_facets": global_facets,
        "statistics": statistics,
        "filtered_out": filtered_out or {"count": 0, "files": []}
    }

    return taxonomy




def verify_taxonomy(taxonomy: dict) -> dict:
    """Верификация целостности таксономии."""

    checks = {
        "total_files_count": "OK",
        "domains_consistency": "OK",
        "no_orphaned_files": "OK",
        "required_fields": "OK",
        "topic_required_fields": "OK"
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
    for domain in taxonomy["domains"]:
        for topic in domain.get("topics", []):
            if not isinstance(topic, dict):
                bad_topics += 1
                continue
            required_topic_fields = ["topic_id", "topic_name", "summary"]
            if not all(topic.get(field) for field in required_topic_fields):
                bad_topics += 1

    if bad_topics > 0:
        checks["topic_required_fields"] = f"WARN: {bad_topics} topics missing topic_id/topic_name/summary"

    return checks


def main():
    """Главный поток синтеза."""

    try:
        project_root = get_project_root()
        temp_dir = project_root / ".claude" / "temp_files"

        print(f"[INFO] Корень проекта: {project_root}", file=sys.stderr)
        print(f"[INFO] Директория temp_files: {temp_dir}", file=sys.stderr)

        # Проверить режим
        merge_mode = "--merge" in sys.argv
        existing_taxonomy = None

        if merge_mode:
            print(f"\n[MERGE] Режим: INCREMENTAL MERGE (--merge)", file=sys.stderr)
            existing_taxonomy = load_existing_taxonomy(temp_dir)

            if not existing_taxonomy:
                print(f"[WARN] Файл taxonomy.json не найден. Переключение на полный режим.", file=sys.stderr)
                merge_mode = False
            else:
                print(f"[OK] Загружена существующая таксономия ({existing_taxonomy['metadata']['total_files']} файлов)", file=sys.stderr)
        else:
            print(f"\n[FULL] Режим: FULL (полный пересбор)", file=sys.stderr)

        # === ФАЗА 3-6 ===
        print("\n[PROCESS] Фаза 3-6: Синтез, проектирование, генерация, верификация...",
              file=sys.stderr)

        # Загрузить анализы
        analysis_data = load_analysis_files(temp_dir)
        if not analysis_data:
            if merge_mode:
                print("[INFO] Файлы анализа не найдены. Сохранение существующей таксономии без изменений.", file=sys.stderr)
                # Обновить дату анализа и сохранить
                existing_taxonomy['metadata']['analysis_date'] = datetime.now().strftime("%Y-%m-%d")
                existing_taxonomy['metadata']['analyzer_notes'] = "Incremental update: no new files to merge"
                taxonomy_path = temp_dir / "taxonomy.json"
                with open(taxonomy_path, "w", encoding="utf-8") as f:
                    json.dump(existing_taxonomy, f, indent=2, ensure_ascii=False)
                print(f"[SAVE] Сохранено: {taxonomy_path}", file=sys.stderr)
                return 0
            else:
                print("[ERROR] Файлы анализа доменов не найдены", file=sys.stderr)
                return 1

        print(f"[OK] Загружено {len(analysis_data)} новых доменов для анализа", file=sys.stderr)

        # Загрузить метаданные из phase 1
        vault_structure_file = temp_dir / "vault-structure-analysis.json"
        metadata = {}
        filtered_out = None
        if vault_structure_file.exists():
            with open(vault_structure_file, "r", encoding="utf-8") as f:
                vault_data = json.load(f)
                metadata = vault_data.get("metadata", {})
                filtered_out = vault_data.get("filtered_out")

        if filtered_out is None and merge_mode and existing_taxonomy:
            # Инкрементальный прогон без нового Фазы 1 - сохранить прежний список шума
            filtered_out = existing_taxonomy.get("filtered_out")

        # Генерировать таксономию
        if merge_mode:
            print(f"\n[MERGE] Объединение анализов с существующей таксономией...", file=sys.stderr)
            # Объединить новые анализы с существующей таксономией
            merged_analysis = {}

            # Копируем все домены из существующей таксономии
            for domain in existing_taxonomy.get("domains", []):
                domain_id = domain["domain_id"]
                existing_files = domain.get("files", [])
                # Темы из предыдущей сборки (уже в финальном формате taxonomy.json)
                existing_topics = domain.get("topics", [])

                # Если новый анализ есть для этого домена, мёржим
                if domain_id in analysis_data:
                    new_files = analysis_data[domain_id].get("files", [])
                    merged_files = merge_domain_files(existing_files, new_files)
                    new_topics = get_domain_topics(analysis_data[domain_id])
                    merged_analysis[domain_id] = {
                        "files": merged_files,
                        "domain_name": domain.get("domain_name", domain_id),
                        "description": domain.get("description", ""),
                        # Новый анализ домена полностью переопределяет темы (пересчитаны заново)
                        "domain_summary": {"topics": new_topics if new_topics else existing_topics}
                    }
                    print(f"  - {domain_id}: {len(existing_files)} -> {len(merged_files)} файлов", file=sys.stderr)
                else:
                    # Сохраняем домен как есть, включая ранее построенные темы
                    merged_analysis[domain_id] = {
                        "files": existing_files,
                        "domain_name": domain.get("domain_name", domain_id),
                        "description": domain.get("description", ""),
                        "domain_summary": {"topics": existing_topics}
                    }

            # Добавляем новые домены если их не было
            for domain_id, domain_data in analysis_data.items():
                if domain_id not in merged_analysis:
                    merged_analysis[domain_id] = domain_data
                    print(f"  - {domain_id}: новый домен с {len(domain_data.get('files', []))} файлами", file=sys.stderr)

            analysis_data = merged_analysis
            metadata = existing_taxonomy.get("metadata", metadata)

        taxonomy = generate_taxonomy_json(analysis_data, metadata, filtered_out)

        # Верификация
        checks = verify_taxonomy(taxonomy)
        print("\n[CHECK] Верификация:", file=sys.stderr)
        for check, result in checks.items():
            print(f"  - {check}: {result}", file=sys.stderr)

        # Обновить metadata если merge режим
        if merge_mode:
            new_files_count = len(analysis_data) if isinstance(analysis_data, dict) else len([f for domain in analysis_data.values() for f in domain.get('files', [])])
            taxonomy['metadata']['analyzer_notes'] = f"Incremental update: new files merged into existing taxonomy"
            taxonomy['metadata']['mode'] = 'incremental'
        else:
            taxonomy['metadata']['analyzer_notes'] = "Full semantic taxonomy based on file content analysis"
            taxonomy['metadata']['mode'] = 'full'

        # Сохранить taxonomy.json
        taxonomy_path = temp_dir / "taxonomy.json"
        with open(taxonomy_path, "w", encoding="utf-8") as f:
            json.dump(taxonomy, f, indent=2, ensure_ascii=False)
        print(f"\n[SAVE] Сохранено: {taxonomy_path}", file=sys.stderr)

        print("\n[SUCCESS] Синтез таксономии завершён", file=sys.stderr)
        return 0

    except Exception as e:
        print(f"[ERROR] Ошибка: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
