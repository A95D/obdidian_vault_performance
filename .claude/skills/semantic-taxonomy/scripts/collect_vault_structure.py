#!/usr/bin/env python3
"""
Сбор структуры Obsidian Vault — Фаза 1 (Ingest + Classify).

Три режима, вызываемые последовательно одним и тем же скриптом:

  --prepare-batches [--new-only]
      Сканирует все .md файлы vault'а, прогоняет их через дешёвый
      эвристический пре-фильтр шума (без LLM) и сверяет content-hash с
      кешем анализа. Новые/изменившиеся файлы группируются в батчи по 15
      файлов. Выход: .claude/temp_files/clustering-batches.json.
      В режиме --new-only дополнительно сидирует domain-vocabulary.json из
      доменов существующей taxonomy.json — классификатор продолжает искать
      подходящий домен среди уже известных и заводит новый только если ни
      один не подошёл, так же как в режиме Full.

  --merge [--new-only]
      После того как субагент vault-topic-classifier обработал все батчи
      (записал batch-{id}-assignments.json и обновил domain-vocabulary.json),
      собирает финальную структуру: путь файла -> домен. Выход:
      .claude/temp_files/vault-structure-analysis.json.

  (без флагов, устаревший путь) эквивалентно --merge --new-only не
  поддерживается — всегда нужно явно указать режим.

Между --prepare-batches и --merge оркестратор (SKILL.md) вызывает
vault-topic-classifier последовательно для каждого батча.
"""

import hashlib
import json
import re
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

CACHE_FILENAME = "semantic-analysis-cache.json"
BATCHES_FILENAME = "clustering-batches.json"
VOCAB_FILENAME = "domain-vocabulary.json"
OUTPUT_FILENAME = "vault-structure-analysis.json"
TAXONOMY_FILENAME = "taxonomy.json"

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024
BATCH_SIZE = 15

# Ступень 1 фильтра шума: служебные папки, не относящиеся к реальным знаниям.
NOISE_PATH_PATTERNS = [
    "templates/", "_templates/", "attachments/", "_resources/",
    "assets/", ".trash/", ".obsidian/", "excalidraw/",
]
MIN_CONTENT_CHARS = 50
FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n?", re.DOTALL)


def get_project_root():
    """Определить корень проекта по расположению скрипта."""
    # Скрипт находится в: .claude/skills/semantic-taxonomy/scripts/
    return Path(__file__).resolve().parents[4]


def load_vault_path(project_root: Path) -> Path:
    """Загрузить путь к vault из .env или переменной окружения."""
    env_file = project_root / ".env"
    if env_file.exists():
        load_dotenv(env_file)

    vault_path = os.getenv("VAULT_PATH")
    if not vault_path:
        raise ValueError(
            "Переменная VAULT_PATH не установлена.\n"
            "Установите её в .env файле или через: export VAULT_PATH='/path/to/vault'"
        )
    return Path(vault_path.strip('"\''))


def load_json(path: Path, default):
    """Загрузить JSON файл. При отсутствии/повреждении вернуть default."""
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[WARN] Не удалось прочитать {path.name}: {e}", file=sys.stderr)
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def classify_as_noise(relative_path: str, content: str):
    """
    Дешёвая эвристика без LLM: определить, является ли файл мусором
    (служебная папка, пустая заметка, файл только с frontmatter).

    Возвращает строку-причину ("template_path", "empty_content",
    "frontmatter_only") или None, если файл не мусор.
    """
    lower_path = relative_path.lower()
    for pattern in NOISE_PATH_PATTERNS:
        if pattern in lower_path:
            return "template_path"

    body = FRONTMATTER_RE.sub("", content, count=1)
    has_frontmatter = body != content
    stripped_body = body.strip()

    if has_frontmatter and not stripped_body:
        return "frontmatter_only"

    if len(stripped_body) < MIN_CONTENT_CHARS:
        return "empty_content"

    return None


def compute_content_hash(content: str) -> str:
    """SHA256 хеш содержимого файла (для кеша анализа)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def read_markdown_files(vault_path: Path):
    """
    Прочитать все .md файлы vault'а, отсортированные по пути (фиксированный
    порядок обхода — важно для детерминированной нумерации батчей при
    последовательной классификации).

    Возвращает список (relative_path_posix, content). Файлы, которые не
    удалось прочитать, пропускаются с предупреждением в stderr.
    """
    if not vault_path.exists():
        raise FileNotFoundError(f"Хранилище не найдено по пути: {vault_path}")
    if not vault_path.is_dir():
        raise NotADirectoryError(f"Путь не является папкой: {vault_path}")

    files = []
    md_paths = sorted(vault_path.glob("**/*.md"), key=lambda p: str(p.relative_to(vault_path)))

    for file_path in md_paths:
        relative_path = str(file_path.relative_to(vault_path)).replace("\\", "/")

        try:
            if file_path.stat().st_size > MAX_FILE_SIZE_BYTES:
                print(f"[WARN] Пропуск {relative_path}: файл больше {MAX_FILE_SIZE_BYTES // (1024*1024)} МБ", file=sys.stderr)
                continue
        except OSError as e:
            print(f"[WARN] Пропуск {relative_path}: ошибка доступа к файлу ({e})", file=sys.stderr)
            continue

        try:
            content = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError, OSError) as e:
            print(f"[WARN] Пропуск {relative_path}: ошибка чтения ({e})", file=sys.stderr)
            continue

        files.append((relative_path, content))

    return files


def load_known_paths(project_root: Path) -> set:
    """Загрузить известные пути файлов из существующей taxonomy.json."""
    taxonomy = load_json(project_root / ".claude" / "temp_files" / TAXONOMY_FILENAME, None)
    known_paths = set()
    if not taxonomy:
        return known_paths
    for domain in taxonomy.get("domains", []):
        for file_info in domain.get("files", []):
            if isinstance(file_info, dict) and "path" in file_info:
                known_paths.add(file_info["path"])
    return known_paths


def seed_vocabulary_from_taxonomy(project_root: Path) -> dict:
    """
    Построить стартовый domain-vocabulary.json из доменов существующей
    taxonomy.json (incremental-режим) — агент продолжает использовать уже
    известные domain_id вместо того, чтобы заводить дубли.
    """
    taxonomy = load_json(project_root / ".claude" / "temp_files" / TAXONOMY_FILENAME, None)
    domains = []
    if taxonomy:
        for domain in taxonomy.get("domains", []):
            domains.append({
                "domain_id": domain.get("domain_id"),
                "domain_name": domain.get("domain_name", domain.get("domain_id")),
                "description": domain.get("description", ""),
            })
    return {"domains": domains}


def build_batches(files_to_analyze, batch_size: int):
    """Сгруппировать файлы в батчи по batch_size, вернуть список батчей."""
    batches = []
    for i in range(0, len(files_to_analyze), batch_size):
        chunk = files_to_analyze[i:i + batch_size]
        batch_id = f"batch-{(i // batch_size) + 1:04d}"
        batches.append({
            "batch_id": batch_id,
            "files": [{"path": path, "name": Path(path).name, "content_hash": file_hash} for path, file_hash in chunk],
        })
    return batches


def cmd_prepare_batches(project_root: Path, new_only: bool) -> int:
    temp_dir = project_root / ".claude" / "temp_files"
    vault_path = load_vault_path(project_root)
    print(f"[INFO] Vault path: {vault_path}", file=sys.stderr)

    print("[STEP 1] Чтение markdown-файлов vault...", file=sys.stderr)
    files = read_markdown_files(vault_path)
    total_files = len(files)
    print(f"[OK] Найдено {total_files} файлов", file=sys.stderr)

    vocab = seed_vocabulary_from_taxonomy(project_root) if new_only else {"domains": []}
    save_json(temp_dir / VOCAB_FILENAME, vocab)
    print(f"[OK] domain-vocabulary.json инициализирован ({len(vocab['domains'])} доменов)", file=sys.stderr)

    if total_files == 0:
        print("[INFO] Markdown файлы не найдены. Батчи не создаются.", file=sys.stderr)
        save_json(temp_dir / BATCHES_FILENAME, {
            "batches": [], "cached_files": [], "cached_file_count": 0,
            "prefiltered_files": [], "prefiltered_count": 0,
            "files_to_analyze": 0, "total_files": 0, "mode": "incremental" if new_only else "full",
        })
        return 0

    print("[STEP 1.5] Эвристический пре-фильтр шума...", file=sys.stderr)
    prefiltered_files = []
    candidate_files = []
    for path, content in files:
        reason = classify_as_noise(path, content)
        if reason:
            prefiltered_files.append({"path": path, "reason": reason})
        else:
            candidate_files.append((path, content))
    print(f"[OK] Отфильтровано как шум: {len(prefiltered_files)}", file=sys.stderr)

    cache = load_json(temp_dir / CACHE_FILENAME, {})

    print("[STEP 2] Проверка кеша анализа по content-hash...", file=sys.stderr)
    new_or_changed = []
    cached_files = []
    for path, content in candidate_files:
        content_hash = compute_content_hash(content)
        if content_hash in cache:
            cached_files.append({"path": path, "content_hash": content_hash})
        else:
            new_or_changed.append((path, content_hash))
    print(f"[OK] В кеше: {len(cached_files)}, требуют анализа: {len(new_or_changed)}", file=sys.stderr)

    print("[STEP 3] Группировка файлов в батчи...", file=sys.stderr)
    batches = build_batches(new_or_changed, BATCH_SIZE)
    print(f"[OK] Создано батчей: {len(batches)} (по {BATCH_SIZE} файлов)", file=sys.stderr)

    result = {
        "batches": batches,
        "cached_files": cached_files,
        "cached_file_count": len(cached_files),
        "prefiltered_files": prefiltered_files,
        "prefiltered_count": len(prefiltered_files),
        "files_to_analyze": len(new_or_changed),
        "total_files": total_files,
        "mode": "incremental" if new_only else "full",
    }
    save_json(temp_dir / BATCHES_FILENAME, result)

    if len(new_or_changed) == 0:
        print("[INFO] Все файлы уже в кеше - вызов субагентов не требуется, переходи сразу к --merge.", file=sys.stderr)

    print(f"[OK] Сохранено в: {temp_dir / BATCHES_FILENAME}", file=sys.stderr)
    return 0


def collect_batch_assignments(temp_dir: Path, batches_data: dict, cache: dict):
    """
    Собрать path -> domain_id из batch-{id}-assignments.json (новые файлы) +
    кеша (файлы с неизменившимся content_hash). Параллельно собрать
    unclassified и noise_files.

    Возвращает (path_to_domain, unclassified, noise_files, new_count).
    """
    path_to_domain = {}
    unclassified = []
    noise_files = []
    new_count = 0

    path_to_hash = {}
    for batch in batches_data.get("batches", []):
        for file_entry in batch.get("files", []):
            path_to_hash[file_entry["path"]] = file_entry["content_hash"]

    for batch in batches_data.get("batches", []):
        batch_id = batch["batch_id"]
        assignments_path = temp_dir / f"batch-{batch_id}-assignments.json"
        data = load_json(assignments_path, None)
        if data is None:
            print(f"[WARN] Результат классификации батча {batch_id} не найден: {assignments_path}", file=sys.stderr)
            for file_entry in batch.get("files", []):
                unclassified.append({
                    "path": file_entry["path"],
                    "reason": "batch_result_missing",
                    "details": batch_id,
                })
            continue

        for a in data.get("assignments", []):
            path = a["path"]
            domain_id = a["domain_id"]
            path_to_domain[path] = domain_id
            new_count += 1

            content_hash = path_to_hash.get(path)
            if content_hash:
                cache[content_hash] = {
                    "domain_id": domain_id,
                    "essence": a.get("essence", ""),
                    "key_concepts": a.get("key_concepts", []),
                }

        for u in data.get("unclassified", []):
            unclassified.append({"path": u["path"], "reason": u.get("reason", "other")})

        for s in data.get("skipped", []):
            noise_files.append({"path": s["path"], "reason": s.get("reason", "classification_skipped"), "stage": "classifier"})

    for cached_file in batches_data.get("cached_files", []):
        content_hash = cached_file["content_hash"]
        cache_entry = cache.get(content_hash)
        if cache_entry:
            path_to_domain[cached_file["path"]] = cache_entry["domain_id"]
        else:
            unclassified.append({"path": cached_file["path"], "reason": "cache_entry_missing_unexpectedly"})

    return path_to_domain, unclassified, noise_files, new_count


def collect_vault_structure(vault_path: Path, project_root: Path) -> dict:
    """Собрать структуру vault'а на основе итогов классификации Фазы 1."""
    if not vault_path.exists():
        raise FileNotFoundError(f"Хранилище не найдено по пути: {vault_path}")
    if not vault_path.is_dir():
        raise NotADirectoryError(f"Путь не является папкой: {vault_path}")

    print(f"Сканирование хранилища: {vault_path}", file=sys.stderr)

    temp_dir = project_root / ".claude" / "temp_files"
    batches_data = load_json(temp_dir / BATCHES_FILENAME, None)
    if batches_data is None:
        raise FileNotFoundError(
            f"{BATCHES_FILENAME} не найден. Сначала запусти "
            f"collect_vault_structure.py --prepare-batches."
        )

    vocab = load_json(temp_dir / VOCAB_FILENAME, {"domains": []})
    vocab_names = {d["domain_id"]: d.get("domain_name", d["domain_id"]) for d in vocab.get("domains", [])}

    cache = load_json(temp_dir / CACHE_FILENAME, {})
    path_to_domain, unclassified, noise_from_classifier, new_count = collect_batch_assignments(
        temp_dir, batches_data, cache
    )
    save_json(temp_dir / CACHE_FILENAME, cache)

    noise_files = [
        {"path": f["path"], "reason": f["reason"], "stage": "prefilter"}
        for f in batches_data.get("prefiltered_files", [])
    ] + noise_from_classifier
    noise_paths = {f["path"] for f in noise_files}

    domain_mode = "semantic" if path_to_domain else "folder-based"
    print(f"Режим определения доменов: {domain_mode}", file=sys.stderr)
    if noise_paths:
        print(f"Исключено как шум (Фаза 1): {len(noise_paths)}", file=sys.stderr)
    if unclassified:
        print(f"Unclassified (идут по folder-based fallback): {len(unclassified)}", file=sys.stderr)

    all_files = []
    domains_dict = {}
    folder_structure = {"name": vault_path.name, "files": [], "file_count": 0}

    try:
        md_files = list(vault_path.glob("**/*.md"))
    except PermissionError as e:
        raise PermissionError(f"Нет доступа к vault: {e}")

    if not md_files:
        print("Markdown файлы не найдены", file=sys.stderr)

    for file in md_files:
        relative_path = str(file.relative_to(vault_path))
        relative_path_posix = relative_path.replace("\\", "/")

        if relative_path_posix in noise_paths:
            continue

        if relative_path_posix in path_to_domain:
            domain_id = path_to_domain[relative_path_posix]
            domain_name = vocab_names.get(domain_id, domain_id.replace("-", " ").title())
        else:
            path_parts = relative_path_posix.split("/")
            if len(path_parts) == 1:
                domain_id = "root"
                domain_name = "Root"
            else:
                domain_id = path_parts[0].lower().replace(" ", "-") if path_parts[0] else "root"
                domain_name = path_parts[0]

        if domain_id not in domains_dict:
            domains_dict[domain_id] = {"domain_id": domain_id, "domain_name": domain_name, "files": []}

        folder_structure["files"].append(relative_path_posix)
        folder_structure["file_count"] += 1

        try:
            file_size = file.stat().st_size
        except OSError:
            file_size = 0

        all_files.append({
            "path": relative_path_posix,
            "folder": str(Path(relative_path_posix).parent),
            "name": file.name,
            "size": file_size,
        })
        domains_dict[domain_id]["files"].append({"path": relative_path_posix, "name": file.stem})

    print(f"Найдено .md файлов: {len(md_files)}", file=sys.stderr)

    try:
        folder_count = len(set(p.parent for p in md_files))
    except Exception:
        folder_count = len(set(file_obj["folder"] for file_obj in all_files))

    return {
        "folder_structure": folder_structure,
        "all_files": all_files,
        "domains": list(domains_dict.values()),
        "filtered_out": {"count": len(noise_files), "files": noise_files},
        "metadata": {
            "total_folders": folder_count,
            "total_files": len(all_files),
            "total_domains": len(domains_dict),
            "ingest_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "vault_path": str(vault_path),
            "domain_mode": domain_mode,
        },
    }


def cmd_merge(project_root: Path, new_only: bool) -> int:
    vault_path = load_vault_path(project_root)
    vault_data = collect_vault_structure(vault_path, project_root)

    if new_only:
        print(f"\n[INCREMENTAL] Режим: INCREMENTAL (--new-only)", file=sys.stderr)
        known_paths = load_known_paths(project_root)

        if not known_paths:
            print(f"[WARN] Файл taxonomy.json не найден. Переключение на полный режим.", file=sys.stderr)
            vault_data['metadata']['mode'] = 'full'
        else:
            print(f"[OK] Загружены {len(known_paths)} известных путей", file=sys.stderr)

            filtered_all_files = [f for f in vault_data['all_files'] if f['path'] not in known_paths]
            filtered_domains = []
            for domain in vault_data['domains']:
                filtered_files = [f for f in domain['files'] if f['path'] not in known_paths]
                if filtered_files:
                    domain['files'] = filtered_files
                    filtered_domains.append(domain)

            vault_data['all_files'] = filtered_all_files
            vault_data['domains'] = filtered_domains
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

    output_path = project_root / ".claude" / "temp_files" / OUTPUT_FILENAME
    save_json(output_path, vault_data)
    print(f"Сохранено в: {output_path}", file=sys.stderr)

    print(f"\n[SUMMARY] Итоги сборки структуры:", file=sys.stderr)
    print(f"  - Файлов: {vault_data['metadata']['total_files']}", file=sys.stderr)
    print(f"  - Папок: {vault_data['metadata']['total_folders']}", file=sys.stderr)
    print(f"  - Доменов: {vault_data['metadata']['total_domains']}", file=sys.stderr)
    print(f"  - Режим: {vault_data['metadata']['mode']}", file=sys.stderr)
    print(f"  - Дата: {vault_data['metadata']['ingest_date']}", file=sys.stderr)

    return 0


def main():
    """Основной поток: диспетчер по режиму (--prepare-batches | --merge)."""
    new_only = "--new-only" in sys.argv
    try:
        project_root = get_project_root()
        print(f"Корень проекта: {project_root}", file=sys.stderr)

        if "--prepare-batches" in sys.argv:
            return cmd_prepare_batches(project_root, new_only)
        elif "--merge" in sys.argv:
            return cmd_merge(project_root, new_only)
        else:
            print(
                "[ERROR] Укажи режим: --prepare-batches или --merge "
                "(опционально с --new-only).",
                file=sys.stderr,
            )
            return 1

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
