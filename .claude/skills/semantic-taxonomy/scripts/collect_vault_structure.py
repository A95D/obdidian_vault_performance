#!/usr/bin/env python3
"""
Сбор структуры Obsidian Vault - Фаза 1 (Scan + Collect).

Два режима, вызываемые последовательно одним и тем же скриптом.

  --scan [--new-only]
      Сканирует все .md файлы vault и прогоняет их через дешёвый
      эвристический пре-фильтр шума (без LLM): служебные папки, пустые
      заметки, заметки состоящие только из вставок изображений. Сверяет
      content-hash с кешем, чтобы отделить новое от неизменившегося.
      Выход: .claude/temp_files/vault-scan.json.

  --collect [--new-only]
      После того как vault-domain-architect отработал и записал
      .claude/temp_files/domain-map.json, собирает финальную структуру
      файл -> домен -> тема. Обновляет кеш и применяет
      .claude/temp_files/domain-merge-map.json, если карта изменилась.
      Выход: .claude/temp_files/vault-structure-analysis.json.

Между --scan и --collect оркестратор (SKILL.md) вызывает ровно один раз
vault-domain-architect на весь vault. Батчей больше нет.
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

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

CACHE_FILENAME = "semantic-analysis-cache.json"
SCAN_FILENAME = "vault-scan.json"
DOMAIN_MAP_FILENAME = "domain-map.json"
MERGE_MAP_FILENAME = "domain-merge-map.json"
OUTPUT_FILENAME = "vault-structure-analysis.json"
TAXONOMY_FILENAME = "taxonomy.json"

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024

# Служебные папки, не относящиеся к реальным знаниям.
NOISE_PATH_PATTERNS = [
    "templates/", "_templates/", "attachments/", "_resources/",
    "assets/", ".trash/", ".obsidian/", "excalidraw/",
]
MIN_CONTENT_CHARS = 50
FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n?", re.DOTALL)
# Строки-вставки: wikilink-картинки, markdown-картинки, embed-синтаксис.
EMBED_LINE_RE = re.compile(r"^\s*(?:!?\[\[|!?\[|\[!)")
RULE_LINE_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
BACKSLASH = chr(92)


def get_project_root():
    """Определить корень проекта по расположению скрипта."""
    return Path(__file__).resolve().parents[4]


def load_vault_path(project_root: Path) -> Path:
    """Загрузить путь к vault из .env или переменной окружения."""
    env_file = project_root / ".env"
    if env_file.exists():
        load_dotenv(env_file)
    vault_path = os.getenv("VAULT_PATH")
    if not vault_path:
        raise ValueError("Переменная VAULT_PATH не установлена.")
    return Path(vault_path.strip(chr(34)).strip(chr(39)))


def load_json(path: Path, default):
    """Загрузить JSON. При отсутствии или повреждении вернуть default."""
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print("[WARN] Не удалось прочитать " + path.name + ": " + str(e), file=sys.stderr)
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def strip_embeds(body: str) -> str:
    """
    Убрать frontmatter, строки-вставки и разделители.
    Заметка из одних изображений иначе набирает порог длины за счёт
    синтаксиса вставок и проходит фильтр как содержательная.
    """
    body = FRONTMATTER_RE.sub("", body, count=1)
    kept = []
    for line in body.splitlines():
        if EMBED_LINE_RE.match(line):
            continue
        if RULE_LINE_RE.match(line):
            continue
        kept.append(line)
    return "\n".join(kept)


def classify_as_noise(relative_path: str, content: str):
    """
    Дешёвая эвристика без LLM: является ли файл мусором.
    Возвращает причину или None, если файл не мусор.
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

    if len(strip_embeds(content).strip()) < MIN_CONTENT_CHARS:
        return "embed_only"

    return None


def compute_content_hash(content: str) -> str:
    """SHA256 хеш содержимого файла (для кеша анализа)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def read_markdown_files(vault_path: Path):
    """Прочитать все .md файлы vault в фиксированном порядке обхода."""
    if not vault_path.exists():
        raise FileNotFoundError("Хранилище не найдено: " + str(vault_path))
    if not vault_path.is_dir():
        raise NotADirectoryError("Путь не является папкой: " + str(vault_path))

    files = []
    md_paths = sorted(vault_path.glob("**/*.md"), key=lambda p: str(p.relative_to(vault_path)))

    for file_path in md_paths:
        relative_path = str(file_path.relative_to(vault_path)).replace(BACKSLASH, "/")
        try:
            if file_path.stat().st_size > MAX_FILE_SIZE_BYTES:
                print("[WARN] Пропуск (слишком большой): " + relative_path, file=sys.stderr)
                continue
        except OSError as e:
            print("[WARN] Пропуск (нет доступа): " + relative_path + " " + str(e), file=sys.stderr)
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError, OSError) as e:
            print("[WARN] Пропуск (ошибка чтения): " + relative_path + " " + str(e), file=sys.stderr)
            continue
        files.append((relative_path, content))

    return files


def section_items(section):
    """Достать список записей секции, принимая и list, и {count, items}."""
    if isinstance(section, list):
        return section
    if isinstance(section, dict):
        items = section.get("items")
        return items if isinstance(items, list) else []
    return []


def paths_from_projects_section(section) -> set:
    """Пути файлов из секции projects."""
    paths = set()
    for entry in section_items(section):
        if not isinstance(entry, dict):
            continue
        files = entry.get("files", [])
        if isinstance(files, str):
            files = [files]
        if not isinstance(files, list):
            continue
        for p in files:
            if isinstance(p, str) and p.strip():
                paths.add(p.strip().replace(BACKSLASH, "/"))
    return paths


def paths_from_unclassified_section(section) -> set:
    """Пути файлов из секции unclassified."""
    paths = set()
    for entry in section_items(section):
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) and entry["path"].strip():
            paths.add(entry["path"].strip().replace(BACKSLASH, "/"))
    return paths


def load_known_paths(project_root: Path) -> set:
    """
    Загрузить известные пути файлов из существующей taxonomy.json.

    Учитываются не только домены, но и секции projects/unclassified: файлы,
    которым архитектор не назначил домен, иначе каждый инкрементальный прогон
    снова объявлял бы их новыми (см. план 2026-10-04-accounted-non-domain-files).
    """
    taxonomy = load_json(project_root / ".claude" / "temp_files" / TAXONOMY_FILENAME, None)
    known_paths = set()
    if not taxonomy:
        return known_paths
    for domain in taxonomy.get("domains", []):
        for file_info in domain.get("files", []):
            if isinstance(file_info, dict) and "path" in file_info:
                known_paths.add(file_info["path"])
    known_paths |= paths_from_projects_section(taxonomy.get("projects"))
    known_paths |= paths_from_unclassified_section(taxonomy.get("unclassified"))
    return known_paths


def load_known_filtered_paths(project_root: Path) -> set:
    """Пути файлов, уже перечисленные в filtered_out прежней taxonomy.json."""
    taxonomy = load_json(project_root / ".claude" / "temp_files" / TAXONOMY_FILENAME, None)
    if not taxonomy:
        return set()
    filtered = taxonomy.get("filtered_out") or {}
    return {f["path"] for f in filtered.get("files", [])
            if isinstance(f, dict) and isinstance(f.get("path"), str)}


def normalize_projects_section(raw_projects, scan_paths: set):
    """
    Привести projects[] из domain-map.json к форме {name, files[], why}.

    Записи с путями, которых нет среди кандидатов vault-scan.json, отбрасываются
    (опечатка архитектора) и возвращаются вторым элементом для stderr. Пустой
    `why` - расхождение с контрактом vault-domain-architect.md, тоже в stderr.
    """
    items = []
    dropped = []
    for entry in section_items(raw_projects):
        if not isinstance(entry, dict):
            dropped.append(str(entry))
            continue
        name = str(entry.get("name") or "").strip()
        why = str(entry.get("why") or "").strip()
        if not why:
            print("[WARN] Проект без поля 'why': " + (name or "<без имени>"), file=sys.stderr)
        files = entry.get("files", [])
        if isinstance(files, str):
            files = [files]
        if not isinstance(files, list):
            print("[WARN] Проект " + (name or "<без имени>") +
                  ": поле 'files' не список - запись отброшена", file=sys.stderr)
            dropped.append(name or "<без имени>")
            continue
        clean = []
        for p in files:
            if not isinstance(p, str) or not p.strip():
                continue
            norm = p.strip().replace(BACKSLASH, "/")
            if norm not in scan_paths:
                dropped.append(norm)
                continue
            if norm not in clean:
                clean.append(norm)
        if not clean:
            print("[WARN] Проект " + (name or "<без имени>") +
                  ": не осталось ни одного файла из vault - запись отброшена", file=sys.stderr)
            continue
        items.append({"name": name or "(проект без имени)", "why": why, "files": clean})
    return items, dropped


def normalize_unclassified_section(raw_unclassified, scan_paths: set):
    """
    Привести unclassified[] из domain-map.json к форме {path, reason, suggestion}.

    reason/suggestion обязательны по контракту vault-domain-architect.md (Шаг 4):
    отсутствие - расхождение в stderr, а не молчаливый дефолт.
    """
    items = []
    dropped = []
    seen = set()
    for entry in section_items(raw_unclassified):
        if not isinstance(entry, dict):
            dropped.append(str(entry))
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path.strip():
            dropped.append("<запись без path>")
            continue
        path = path.strip().replace(BACKSLASH, "/")
        if path in seen:
            continue
        seen.add(path)
        if path not in scan_paths:
            dropped.append(path)
            continue
        reason = str(entry.get("reason") or "").strip()
        suggestion = str(entry.get("suggestion") or "").strip()
        if not reason or not suggestion:
            print("[WARN] unclassified без reason/suggestion: " + path, file=sys.stderr)
        items.append({"path": path, "reason": reason, "suggestion": suggestion})
    return items, dropped

def cmd_scan(project_root: Path, new_only: bool) -> int:
    """
    Шаг 1: собрать список заметок, годных к анализу, и отсеять мусор.
    Выход: vault-scan.json - вход для vault-domain-architect.
    """
    temp_dir = project_root / ".claude" / "temp_files"
    vault_path = load_vault_path(project_root)
    print("[INFO] Vault path: " + str(vault_path), file=sys.stderr)

    print("[STEP 1] Чтение markdown-файлов vault...", file=sys.stderr)
    files = read_markdown_files(vault_path)
    total_files = len(files)
    print("[OK] Найдено " + str(total_files) + " файлов", file=sys.stderr)

    print("[STEP 1.5] Эвристический пре-фильтр шума...", file=sys.stderr)
    filtered_out = []
    candidate_files = []
    for path, content in files:
        reason = classify_as_noise(path, content)
        if reason:
            filtered_out.append({"path": path, "reason": reason})
        else:
            candidate_files.append((path, content))
    print("[OK] Отсеяно как мусор: " + str(len(filtered_out)), file=sys.stderr)

    cache = load_json(temp_dir / CACHE_FILENAME, {})

    print("[STEP 2] Сверка с кешем по content-hash...", file=sys.stderr)
    entries = []
    cached_count = 0
    for path, content in candidate_files:
        content_hash = compute_content_hash(content)
        is_cached = (not new_only) and (content_hash in cache)
        if is_cached:
            cached_count += 1
        entries.append({
            "path": path,
            "folder": str(Path(path).parent),
            "name": Path(path).name,
            "content_hash": content_hash,
            "in_cache": is_cached,
        })
    print("[OK] К анализу: " + str(len(entries)) + ", из них в кеше: " + str(cached_count), file=sys.stderr)

    result = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "vault_path": str(vault_path),
        "total_files": total_files,
        "candidate_count": len(entries),
        "cached_count": cached_count,
        "files": entries,
        "filtered_out": filtered_out,
        "mode": "incremental" if new_only else "full",
    }
    save_json(temp_dir / SCAN_FILENAME, result)
    print("[OK] Сохранено в: " + str(temp_dir / SCAN_FILENAME), file=sys.stderr)
    print("[NEXT] Запустить vault-domain-architect, затем --collect", file=sys.stderr)
    return 0


def cmd_collect(project_root: Path, new_only: bool) -> int:
    """
    Шаг 2: из domain-map.json собрать структуру файл -> домен -> тема.
    Обновляет кеш, применяет карту слияния доменов.
    """
    temp_dir = project_root / ".claude" / "temp_files"
    vault_path = load_vault_path(project_root)

    scan = load_json(temp_dir / SCAN_FILENAME, None)
    if scan is None:
        raise FileNotFoundError(SCAN_FILENAME + " не найден. Сначала запусти --scan.")

    domain_map = load_json(temp_dir / DOMAIN_MAP_FILENAME, None)
    if domain_map is None:
        raise FileNotFoundError(DOMAIN_MAP_FILENAME + " не найден. Сначала запусти vault-domain-architect.")

    map_domains = domain_map.get("domains", [])
    print("[INFO] Доменов в карте: " + str(len(map_domains)), file=sys.stderr)

    # Файлы вне доменов: практические заметки проектов и то, чему архитектор
    # не смог назначить домен. Без чтения этих двух ключей они исчезали из
    # отчёта бесшумно (см. план 2026-10-04-accounted-non-domain-files).
    scan_paths = {e["path"] for e in scan.get("files", [])
                  if isinstance(e, dict) and isinstance(e.get("path"), str)}
    projects_items, dropped_projects = normalize_projects_section(
        domain_map.get("projects"), scan_paths)
    unclassified_items, dropped_unclassified = normalize_unclassified_section(
        domain_map.get("unclassified"), scan_paths)
    for label, dropped in (("projects", dropped_projects),
                           ("unclassified", dropped_unclassified)):
        if dropped:
            print("[WARN] " + label + ": отброшено записей с путями вне vault-scan.json: " +
                  str(len(dropped)) + " - " + ", ".join(sorted(dropped)), file=sys.stderr)
    projects_count = sum(len(p["files"]) for p in projects_items)
    unclassified_count = len(unclassified_items)
    print("[INFO] Вне доменов: в проектах " + str(projects_count) +
          ", не распознано " + str(unclassified_count), file=sys.stderr)

    merge_map = load_json(temp_dir / MERGE_MAP_FILENAME, {})

    path_to_domain = {}
    path_to_essence = {}
    domains_out = []
    for dom in map_domains:
        domain_id = dom["id"]
        name = dom.get("name", domain_id)
        purpose = dom.get("purpose", "")
        files_in_domain = []
        topics_out = []
        for topic in dom.get("topics", []):
            tname = topic.get("name", "topic")
            topic_paths = []
            for p in topic.get("files", []):
                path_to_domain[p] = domain_id
                path_to_essence[p] = topic.get("essence", "")
                entry = {"path": p, "name": Path(p).stem}
                topic_paths.append(entry)
                files_in_domain.append(entry)
            if topic_paths:
                topics_out.append({
                    "topic_id": tname,
                    "topic_name": tname,
                    "note_paths": [t["path"] for t in topic_paths],
                })
        domains_out.append({
            "domain_id": domain_id,
            "domain_name": name,
            "purpose": purpose,
            "files": files_in_domain,
            "topics": topics_out,
        })

    off_domain_paths = set()
    for project in projects_items:
        off_domain_paths.update(project["files"])
    off_domain_paths.update(u["path"] for u in unclassified_items)
    clashes = sorted(off_domain_paths & set(path_to_domain))
    if clashes:
        print("[WARN] Файлы учтены и в домене, и в projects/unclassified: " +
              ", ".join(clashes), file=sys.stderr)

    cache = load_json(temp_dir / CACHE_FILENAME, {})
    hash_by_path = {e["path"]: e["content_hash"] for e in scan.get("files", [])}
    updated = 0
    for path, domain_id in path_to_domain.items():
        content_hash = hash_by_path.get(path)
        if not content_hash:
            continue
        cache[content_hash] = {
            "domain_id": domain_id,
            "essence": path_to_essence.get(path, ""),
            "key_concepts": [],
        }
        updated += 1

    if merge_map:
        for entry in cache.values():
            if entry.get("domain_id") in merge_map:
                entry["domain_id"] = merge_map[entry["domain_id"]]
    save_json(temp_dir / CACHE_FILENAME, cache)
    suffix = (", применено слияний: " + str(len(merge_map))) if merge_map else ""
    print("[OK] Кеш обновлён: " + str(updated) + " записей" + suffix, file=sys.stderr)

    all_files = []
    total = 0
    try:
        md_files = list(vault_path.glob("**/*.md"))
    except PermissionError as e:
        raise PermissionError("Нет доступа к vault: " + str(e))

    noise_paths = {f["path"] for f in scan.get("filtered_out", [])}
    for f in md_files:
        rel = str(f.relative_to(vault_path)).replace(BACKSLASH, "/")
        if rel in noise_paths:
            continue
        total += 1
        try:
            size = f.stat().st_size
        except OSError:
            size = 0
        all_files.append({"path": rel, "folder": str(Path(rel).parent), "name": f.name, "size": size})

    vault_data = {
        "folder_structure": {"name": vault_path.name, "files": [a["path"] for a in all_files], "file_count": total},
        "all_files": all_files,
        "domains": domains_out,
        "projects": {"count": projects_count, "items": projects_items},
        "unclassified": {"count": unclassified_count, "items": unclassified_items},
        "filtered_out": {"count": len(scan.get("filtered_out", [])), "files": scan.get("filtered_out", [])},
        "metadata": {
            "total_files": total,
            # total_files - кандидаты (кандидаты = все .md минус filtered_out),
            # scanned_md_count - все .md в vault, включая отсеянный шум.
            # Проверка покрытия в synthesize_taxonomy.py считает по обоим.
            "candidate_count": total,
            "scanned_md_count": len(md_files),
            "total_domains": len(domains_out),
            "domain_mode": "architect",
            "collect_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "vault_path": str(vault_path),
            "projects_count": projects_count,
            "unclassified_count": unclassified_count,
        },
    }

    if new_only:
        known_paths = load_known_paths(project_root)
        if not known_paths:
            print("[WARN] taxonomy.json не найден. Остаёмся в полном режиме.", file=sys.stderr)
            vault_data["metadata"]["mode"] = "full"
        else:
            filtered_all = [a for a in all_files if a["path"] not in known_paths]
            filtered_domains = []
            for dom in domains_out:
                kept = [a for a in dom["files"] if a["path"] not in known_paths]
                if kept:
                    dom["files"] = kept
                    filtered_domains.append(dom)
            filtered_projects = []
            for project in projects_items:
                kept = [f for f in project["files"] if f not in known_paths]
                if kept:
                    filtered_projects.append(dict(project, files=kept))
            filtered_unclassified = [
                u for u in unclassified_items if u["path"] not in known_paths
            ]
            vault_data["projects"] = {
                "count": sum(len(p["files"]) for p in filtered_projects),
                "items": filtered_projects,
            }
            vault_data["unclassified"] = {"count": len(filtered_unclassified),
                                          "items": filtered_unclassified}
            vault_data["metadata"]["projects_count"] = vault_data["projects"]["count"]
            vault_data["metadata"]["unclassified_count"] = vault_data["unclassified"]["count"]
            # Сколько кандидатов и шума добавилось к уже учтённому - по этим
            # числам synthesize_taxonomy.py продолжает арифметику покрытия
            # поверх прежней taxonomy.json.
            known_filtered = load_known_filtered_paths(project_root)
            vault_data["metadata"]["new_candidates_count"] = len(
                [e for e in scan.get("files", []) if e.get("path") not in known_paths])
            vault_data["metadata"]["new_filtered_count"] = len(
                [f for f in scan.get("filtered_out", [])
                 if isinstance(f, dict) and f.get("path") not in known_filtered])
            vault_data["all_files"] = filtered_all
            vault_data["domains"] = filtered_domains
            vault_data["metadata"]["total_files"] = len(filtered_all)
            vault_data["metadata"]["total_domains"] = len(filtered_domains)
            vault_data["metadata"]["mode"] = "incremental"
            print("[OK] Отфильтровано: " + str(len(filtered_all)) + " новых из " + str(len(known_paths)), file=sys.stderr)
    else:
        vault_data["metadata"]["mode"] = "full"

    save_json(temp_dir / OUTPUT_FILENAME, vault_data)
    print("")
    print("[SUMMARY] Итоги сборки структуры:", file=sys.stderr)
    print("  - Файлов: " + str(total), file=sys.stderr)
    print("  - Доменов: " + str(len(domains_out)), file=sys.stderr)
    print("  - В проектах: " + str(vault_data["projects"]["count"]), file=sys.stderr)
    print("  - Не распознано: " + str(vault_data["unclassified"]["count"]), file=sys.stderr)
    print("  - Режим: " + str(vault_data["metadata"]["mode"]), file=sys.stderr)
    return 0


def main():
    """Диспетчер по режиму (--scan | --collect)."""
    new_only = "--new-only" in sys.argv
    try:
        project_root = get_project_root()
        print("Корень проекта: " + str(project_root), file=sys.stderr)

        if "--scan" in sys.argv:
            return cmd_scan(project_root, new_only)
        elif "--collect" in sys.argv:
            return cmd_collect(project_root, new_only)
        else:
            print("[ERROR] Укажи режим: --scan или --collect (опционально с --new-only).", file=sys.stderr)
            return 1

    except (ValueError, FileNotFoundError, NotADirectoryError, PermissionError) as e:
        print("Ошибка: " + str(e), file=sys.stderr)
        return 1
    except Exception as e:
        print("Неожиданная ошибка: " + str(e), file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
