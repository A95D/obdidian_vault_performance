#!/usr/bin/env python3
"""
Подготовка батчей для семантической кластеризации содержимого Obsidian Vault
(Фаза 0, шаг 1 из 3).

Только механика, без LLM: сканирует все .md файлы vault'а, сверяет
content-hash каждого файла с кешем анализа и группирует новые/изменившиеся
файлы в батчи. Батчи затем анализирует субагент vault-topic-classifier
(Agent tool, параллельно по одному вызову на батч).

Кеш анализа: .claude/temp_files/semantic-analysis-cache.json
  (ключ - sha256 содержимого файла, значение - результат анализа темы;
  валиден, пока содержимое файла не изменилось - без TTL).

Выход: .claude/temp_files/clustering-batches.json
"""

import hashlib
import json
import os
import re
import sys
import io
from pathlib import Path

from dotenv import load_dotenv

# Установить UTF-8 кодировку для вывода (защита от ошибок на Windows)
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

CACHE_FILENAME = "semantic-analysis-cache.json"
OUTPUT_FILENAME = "clustering-batches.json"
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024
BATCH_SIZE = 15

# Ступень 1 фильтра шума: служебные папки, не относящиеся к реальным знаниям.
NOISE_PATH_PATTERNS = [
    "templates/", "_templates/", "attachments/", "_resources/",
    "assets/", ".trash/", ".obsidian/", "excalidraw/",
]
MIN_CONTENT_CHARS = 50
FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n?", re.DOTALL)


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


def get_project_root():
    """Определить корень проекта по расположению скрипта."""
    # Скрипт находится в: .claude/skills/semantic-taxonomy/scripts/
    return Path(__file__).resolve().parents[4]


def load_vault_path(project_root: Path) -> Path:
    """Загрузить VAULT_PATH из .env."""
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


def compute_content_hash(content: str) -> str:
    """SHA256 хеш содержимого файла (для кеша анализа)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def load_cache(cache_path: Path) -> dict:
    """Загрузить кеш результатов анализа. Повреждённый кеш игнорируется."""
    if not cache_path.exists():
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[WARN] Не удалось прочитать кеш анализа, начинаем с пустого: {e}", file=sys.stderr)
        return {}


def read_markdown_files(vault_path: Path):
    """
    Прочитать все .md файлы vault'а.

    Возвращает список (relative_path_posix, content).
    Файлы, которые не удалось прочитать (кодировка, права доступа, размер),
    пропускаются с предупреждением в stderr и не прерывают обработку.
    """
    if not vault_path.exists():
        raise FileNotFoundError(f"Хранилище не найдено по пути: {vault_path}")
    if not vault_path.is_dir():
        raise NotADirectoryError(f"Путь не является папкой: {vault_path}")

    files = []
    md_paths = list(vault_path.glob("**/*.md"))

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


def main():
    """Основной поток: Фаза 0, шаг 1 - подготовка батчей для классификации."""
    try:
        project_root = get_project_root()
        temp_dir = project_root / ".claude" / "temp_files"

        print(f"[INFO] Корень проекта: {project_root}", file=sys.stderr)

        vault_path = load_vault_path(project_root)
        print(f"[INFO] Vault path: {vault_path}", file=sys.stderr)

        print("[STEP 1] Чтение markdown-файлов vault...", file=sys.stderr)
        files = read_markdown_files(vault_path)
        total_files = len(files)
        print(f"[OK] Найдено {total_files} файлов", file=sys.stderr)

        if total_files == 0:
            print("[INFO] Markdown файлы не найдены. Батчи не создаются.", file=sys.stderr)
            output_path = temp_dir / OUTPUT_FILENAME
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump({
                    "batches": [],
                    "cached_files": [],
                    "cached_file_count": 0,
                    "prefiltered_files": [],
                    "prefiltered_count": 0,
                    "files_to_analyze": 0,
                    "total_files": 0,
                }, f, indent=2, ensure_ascii=False)
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

        cache_path = temp_dir / CACHE_FILENAME
        cache = load_cache(cache_path)

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
        }

        output_path = temp_dir / OUTPUT_FILENAME
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        if len(new_or_changed) == 0:
            print("[INFO] Все файлы уже в кеше - вызов субагентов не требуется.", file=sys.stderr)

        print(f"[OK] Сохранено в: {output_path}", file=sys.stderr)
        return 0

    except (ValueError, FileNotFoundError, NotADirectoryError, PermissionError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[ERROR] Неожиданная ошибка: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
