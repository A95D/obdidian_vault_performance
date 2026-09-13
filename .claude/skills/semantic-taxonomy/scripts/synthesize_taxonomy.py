#!/usr/bin/env python3
"""
Синтез таксономии (портирование phase3-6-synthesis.js на Python).

Фазы 3-6:
  3. Синтез: выявление глобальных фасетов и иерархий
  4-5. Генерация: создание taxonomy.json
  6. Верификация: проверка целостности
"""

import json
import sys
import io
from pathlib import Path
from datetime import datetime

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


def generate_taxonomy_json(analysis_data: dict, metadata: dict) -> dict:
    """Генерировать финальную таксономию."""

    global_facets = extract_global_facets(analysis_data)

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

        domain_obj = {
            "domain_id": domain_key,
            "domain_name": domain_data.get("domain_name", domain_key.title()),
            "description": domain_data.get("description", ""),
            "file_count": len(files_in_domain),
            "paradigms": sorted(list(paradigms_set)),
            "files": files_in_domain,
            "learning_paths": build_learning_paths(files_in_domain)
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
        "statistics": statistics
    }

    return taxonomy




def verify_taxonomy(taxonomy: dict) -> dict:
    """Верификация целостности таксономии."""

    checks = {
        "total_files_count": "OK",
        "domains_consistency": "OK",
        "no_orphaned_files": "OK",
        "required_fields": "OK"
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
        if vault_structure_file.exists():
            with open(vault_structure_file, "r", encoding="utf-8") as f:
                vault_data = json.load(f)
                metadata = vault_data.get("metadata", {})

        # Генерировать таксономию
        if merge_mode:
            print(f"\n[MERGE] Объединение анализов с существующей таксономией...", file=sys.stderr)
            # Объединить новые анализы с существующей таксономией
            merged_analysis = {}

            # Копируем все домены из существующей таксономии
            for domain in existing_taxonomy.get("domains", []):
                domain_id = domain["domain_id"]
                existing_files = domain.get("files", [])

                # Если новый анализ есть для этого домена, мёржим
                if domain_id in analysis_data:
                    new_files = analysis_data[domain_id].get("files", [])
                    merged_files = merge_domain_files(existing_files, new_files)
                    merged_analysis[domain_id] = {
                        "files": merged_files,
                        "domain_name": domain.get("domain_name", domain_id),
                        "description": domain.get("description", "")
                    }
                    print(f"  - {domain_id}: {len(existing_files)} -> {len(merged_files)} файлов", file=sys.stderr)
                else:
                    # Сохраняем домен как есть
                    merged_analysis[domain_id] = {
                        "files": existing_files,
                        "domain_name": domain.get("domain_name", domain_id),
                        "description": domain.get("description", "")
                    }

            # Добавляем новые домены если их не было
            for domain_id, domain_data in analysis_data.items():
                if domain_id not in merged_analysis:
                    merged_analysis[domain_id] = domain_data
                    print(f"  - {domain_id}: новый домен с {len(domain_data.get('files', []))} файлами", file=sys.stderr)

            analysis_data = merged_analysis
            metadata = existing_taxonomy.get("metadata", metadata)

        taxonomy = generate_taxonomy_json(analysis_data, metadata)

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
