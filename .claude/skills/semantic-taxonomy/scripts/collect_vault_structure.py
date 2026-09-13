#!/usr/bin/env python3
"""
Сбор структуры Obsidian Vault (замена PowerShell скрипта).

Рекурсивное сканирование всех .md файлов и создание JSON с метаданными.
Cross-platform (Windows, Linux, macOS).
"""

import json
import sys
import io
from pathlib import Path
from datetime import datetime
import os
from dotenv import load_dotenv

# Установить UTF-8 кодировку для вывода (защита от ошибок на Windows)
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')


def get_project_root():
    """Определить корень проекта по расположению скрипта."""
    # Скрипт находится в: .claude/skills/semantic-taxonomy/scripts/
    # Корень проекта на 4 уровня выше
    return Path(__file__).resolve().parents[4]


def load_vault_path():
    """Загрузить путь к vault из .env или переменной окружения."""
    project_root = get_project_root()
    env_file = project_root / ".env"

    # Загрузить .env если существует
    if env_file.exists():
        load_dotenv(env_file)

    vault_path = os.getenv("VAULT_PATH")

    if not vault_path:
        raise ValueError(
            "Переменная VAULT_PATH не установлена.\n"
            "Установите её в .env файле или через: export VAULT_PATH='/path/to/vault'"
        )

    # Удалить кавычки если они присутствуют (защита от ошибок при форматировании .env)
    vault_path = vault_path.strip('"\'')

    return Path(vault_path)


def collect_vault_structure(vault_path: Path) -> dict:
    """Собрать структуру vault'а."""

    # Проверить доступность vault
    if not vault_path.exists():
        raise FileNotFoundError(f"Хранилище не найдено по пути: {vault_path}")

    if not vault_path.is_dir():
        raise NotADirectoryError(f"Путь не является папкой: {vault_path}")

    print(f"Сканирование хранилища: {vault_path}", file=sys.stderr)

    # Инициализировать массивы
    all_files = []
    domains_dict = {}  # Группировка по доменам (первый сегмент пути)
    folder_structure = {
        "name": vault_path.name,
        "files": [],
        "file_count": 0
    }

    try:
        # Найти все markdown файлы рекурсивно
        md_files = list(vault_path.glob("**/*.md"))
    except PermissionError as e:
        raise PermissionError(f"Нет доступа к vault: {e}")

    if not md_files:
        print("Markdown файлы не найдены", file=sys.stderr)

    for file in md_files:
        # Вычислить относительный путь от корня vault
        relative_path = str(file.relative_to(vault_path))

        # Использовать прямые слэши для кроссплатформенности
        relative_path_posix = relative_path.replace("\\", "/")

        # Определить домен (первый сегмент пути или 'root' если файл в корне)
        path_parts = relative_path_posix.split("/")
        domain_id = path_parts[0].lower().replace(" ", "-") if len(path_parts) > 1 and path_parts[0] else "root"
        if len(path_parts) == 1:
            domain_id = "root"

        # Инициализировать домен если его еще нет
        if domain_id not in domains_dict:
            domains_dict[domain_id] = {
                "domain_id": domain_id,
                "domain_name": path_parts[0] if domain_id != "root" else "Root",
                "files": []
            }

        # Добавить в список папок
        folder_structure["files"].append(relative_path_posix)
        folder_structure["file_count"] += 1

        # Создать объект файла
        try:
            file_size = file.stat().st_size
        except OSError:
            file_size = 0

        file_obj = {
            "path": relative_path_posix,
            "folder": str(Path(relative_path_posix).parent),
            "name": file.name,
            "size": file_size
        }
        all_files.append(file_obj)

        # Добавить файл в группу домена
        domain_file_obj = {
            "path": relative_path_posix,
            "name": file.stem
        }
        domains_dict[domain_id]["files"].append(domain_file_obj)

    print(f"Найдено .md файлов: {len(md_files)}", file=sys.stderr)

    # Подсчитать папки
    try:
        folder_count = len(set(p.parent for p in md_files))
    except Exception:
        folder_count = len(set(file_obj["folder"] for file_obj in all_files))

    # Построить финальный результат
    result = {
        "folder_structure": folder_structure,
        "all_files": all_files,
        "domains": list(domains_dict.values()),
        "metadata": {
            "total_folders": folder_count,
            "total_files": len(md_files),
            "total_domains": len(domains_dict),
            "ingest_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "vault_path": str(vault_path)
        }
    }

    return result


def save_as_json(data: dict, output_path: str, project_root: Path) -> Path:
    """Сохранить данные в JSON файл."""

    # Построить полный путь (относительно корня проекта)
    full_path = project_root / output_path
    full_path.parent.mkdir(parents=True, exist_ok=True)

    # Сохранить с красивым форматированием
    with open(full_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Сохранено в: {full_path}", file=sys.stderr)
    return full_path


def load_known_paths(project_root: Path) -> set:
    """Загрузить известные пути файлов из существующей taxonomy.json."""
    taxonomy_path = project_root / ".claude" / "temp_files" / "taxonomy.json"
    known_paths = set()

    if not taxonomy_path.exists():
        return known_paths

    try:
        with open(taxonomy_path, "r", encoding="utf-8") as f:
            taxonomy = json.load(f)
            for domain in taxonomy.get("domains", []):
                for file_info in domain.get("files", []):
                    if isinstance(file_info, dict) and "path" in file_info:
                        known_paths.add(file_info["path"])
    except (json.JSONDecodeError, IOError) as e:
        print(f"⚠️  Ошибка чтения taxonomy.json: {e}", file=sys.stderr)

    return known_paths


def main():
    """Основной поток."""
    try:
        project_root = get_project_root()
        print(f"Корень проекта: {project_root}", file=sys.stderr)

        # Загрузить путь к vault
        vault_path = load_vault_path()

        # Собрать информацию о структуре
        vault_data = collect_vault_structure(vault_path)

        # Проверить режим
        new_only_mode = "--new-only" in sys.argv

        if new_only_mode:
            print(f"\n[INCREMENTAL] Режим: INCREMENTAL (--new-only)", file=sys.stderr)
            known_paths = load_known_paths(project_root)

            if not known_paths:
                print(f"[WARN] Файл taxonomy.json не найден. Переключение на полный режим.", file=sys.stderr)
                new_only_mode = False
            else:
                print(f"[OK] Загружены {len(known_paths)} известных путей", file=sys.stderr)

                # Фильтровать файлы - оставляем только новые
                filtered_all_files = [f for f in vault_data['all_files'] if f['path'] not in known_paths]
                filtered_domains = []

                for domain in vault_data['domains']:
                    filtered_files = [f for f in domain['files'] if f['path'] not in known_paths]
                    if filtered_files:
                        domain['files'] = filtered_files
                        filtered_domains.append(domain)

                vault_data['all_files'] = filtered_all_files
                vault_data['domains'] = filtered_domains

                # Обновить метаданные
                vault_data['metadata']['total_files'] = len(filtered_all_files)
                vault_data['metadata']['total_domains'] = len(filtered_domains)
                vault_data['metadata']['mode'] = 'incremental'

                new_files_count = len(filtered_all_files)
                print(f"[OK] Отфильтровано: {new_files_count} новых файлов из {len(known_paths)} известных", file=sys.stderr)

                if new_files_count == 0:
                    print(f"[INFO] Новых файлов не найдено. Домены не будут обработаны.", file=sys.stderr)
        else:
            print(f"\n[FULL] Режим: FULL (полный пересбор)", file=sys.stderr)
            vault_data['metadata']['mode'] = 'full'

        # Сохранить в JSON
        output_path = ".claude/temp_files/vault-structure-analysis.json"
        saved_path = save_as_json(vault_data, output_path, project_root)

        # Показать результаты
        print(f"\n[SUMMARY] Итоги сканирования:", file=sys.stderr)
        print(f"  - Файлов: {vault_data['metadata']['total_files']}", file=sys.stderr)
        print(f"  - Папок: {vault_data['metadata']['total_folders']}", file=sys.stderr)
        print(f"  - Доменов: {vault_data['metadata']['total_domains']}", file=sys.stderr)
        print(f"  - Режим: {vault_data['metadata']['mode']}", file=sys.stderr)
        print(f"  - Дата: {vault_data['metadata']['ingest_date']}", file=sys.stderr)

        return 0

    except (ValueError, FileNotFoundError, NotADirectoryError, PermissionError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Неожиданная ошибка: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())