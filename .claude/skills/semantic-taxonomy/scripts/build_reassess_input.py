#!/usr/bin/env python3
"""
Вход для переоценки тем затронутых доменов (Фаза 3б).

Читает affected-domains.json (Фаза 2), taxonomy.json и {domain_id}-analysis.json
дельты и пишет reassess-input/{domain_id}.json на каждый затронутый домен:
текущие темы (включая ожидаемые без заметок), essence и key_concepts старых
файлов, записи анализа новых и изменённых файлов. Старые тексты vault не читаются.

Использование:
  python build_reassess_input.py [--temp-dir DIR]

Код выхода: 0 - все входы собраны, 2 - нет входного файла или анализа дельты.
"""

import sys
import io
import json
import argparse
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

AFFECTED_FILENAME = "affected-domains.json"
TAXONOMY_FILENAME = "taxonomy.json"
DOMAIN_MAP_FILENAME = "domain-map.json"
OUT_DIRNAME = "reassess-input"
TOPIC_FIELDS = ("topic_id", "topic_name", "summary", "has_notes", "mastery_percent",
                "mastery_rationale", "note_paths")


def get_project_root():
    """Определить корень проекта по расположению скрипта."""
    return Path(__file__).resolve().parents[4]


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def map_domain_files(domain_map, domain_id):
    """Файлы домена по domain-map.json (None, если домена в карте нет)."""
    for dom in (domain_map or {}).get("domains", []):
        if dom.get("id") == domain_id:
            return {p for t in dom.get("topics", []) for p in t.get("files", [])}
    return None


def source_domain_ids(domain_id, taxonomy_ids, domain_map):
    """Домены taxonomy.json, из которых состоит домен (сам, либо после слияния/разделения)."""
    if domain_id in taxonomy_ids:
        return [domain_id]
    for dom in (domain_map or {}).get("domains", []):
        if dom.get("id") == domain_id:
            src = list(dom.get("merged_from") or [])
            if dom.get("split_from"):
                src.append(dom["split_from"])
            return [s for s in src if s in taxonomy_ids]
    return []


def delta_records(domain_id, new_files, analysis):
    """Записи анализа дельты по путям: по полю path, иначе по порядку списка дельты."""
    records = analysis.get("files") or []
    # Анализ может содержать и старые записи (домен, которого не было в taxonomy.json): при
    # наличии path у всех записей берём записи дельты по пути, лишние не мешают.
    if records and all(isinstance(r, dict) and r.get("path") for r in records):
        by_path = {r["path"]: r for r in records}
        missing = [p for p in new_files if p not in by_path]
        if missing:
            raise ValueError("анализ " + domain_id + ": нет записей для файлов дельты: " + ", ".join(missing))
        return {p: by_path[p] for p in new_files}
    if len(records) != len(new_files):
        raise ValueError("анализ " + domain_id + ": записей " + str(len(records)) +
                         ", файлов дельты " + str(len(new_files)))
    out = {}
    for path, rec in zip(new_files, records):
        if rec.get("path") not in (None, path):
            raise ValueError("анализ " + domain_id + ": порядок записей не совпадает с дельтой (" +
                             str(rec.get("path")) + " вместо " + path + ")")
        out[path] = rec
    return out


def build_input(entry, taxonomy, domain_map, analysis):
    domain_id = entry["domain_id"]
    new_files = list(entry.get("new_files") or [])
    by_id = {d["domain_id"]: d for d in taxonomy.get("domains", [])}
    sources = [by_id[s] for s in source_domain_ids(domain_id, by_id, domain_map)]
    keep = map_domain_files(domain_map, domain_id)

    topics = []
    topic_of = {}
    for dom in sources:
        for topic in dom.get("topics", []):
            topics.append({k: topic.get(k) for k in TOPIC_FIELDS})
            for p in topic.get("note_paths") or []:
                topic_of[p] = topic.get("topic_id")
    topic_ids = {t["topic_id"] for t in topics}

    delta = delta_records(domain_id, new_files, analysis)
    old_files = []
    for dom in sources:
        for f in dom.get("files", []):
            path = f.get("path")
            if path in delta or (keep is not None and path not in keep):
                continue
            old_files.append({"path": path, "essence": f.get("essence"),
                              "key_concepts": f.get("key_concepts") or [],
                              "topic_id": topic_of.get(path)})
    # Файл переехал в домен из другого домена без изменения (add_file, move_topic, merge/split):
    # его запись берётся из taxonomy.json исходного домена, темы старого домена к нему не относятся.
    if keep is not None:
        placed = {f["path"] for f in old_files}
        records = {f.get("path"): f for dom in taxonomy.get("domains", []) for f in dom.get("files", [])}
        for path in sorted(keep - placed - set(delta)):
            f = records.get(path)
            if f is None:
                raise ValueError("файл " + str(path) + " стоит в домене " + domain_id + " по domain-map.json, "
                                 "но его нет ни в taxonomy.json, ни в дельте")
            old_files.append({"path": path, "essence": f.get("essence"),
                              "key_concepts": f.get("key_concepts") or [], "topic_id": None})
    summary = analysis.get("domain_summary") or {}
    return {
        "domain_id": domain_id,
        "domain_name": (sources[0].get("domain_name") if len(sources) == 1 else None) or analysis.get("domain_name") or domain_id,
        "description": " ".join(d.get("description") or "" for d in sources).strip(),
        "reasons": entry.get("reasons") or [],
        "topics": topics,
        "old_files": old_files,
        "delta_files": [dict(rec, path=path) for path, rec in delta.items()],
        "delta_summary": {k: summary.get(k) for k in ("key_themes", "main_concepts", "topics") if k in summary},
        "all_paths": sorted({f["path"] for f in old_files} | set(new_files)),
        "known_topic_ids": sorted(topic_ids),
    }


def main():
    parser = argparse.ArgumentParser(description="Вход переоценки тем затронутых доменов (Фаза 3б)")
    parser.add_argument("--temp-dir", help="директория temp_files (по умолчанию .claude/temp_files проекта)")
    args = parser.parse_args()
    temp_dir = Path(args.temp_dir) if args.temp_dir else get_project_root() / ".claude" / "temp_files"

    for name in (AFFECTED_FILENAME, TAXONOMY_FILENAME):
        if not (temp_dir / name).exists():
            print("[ОШИБКА] Нет входа " + name + ": " + str(temp_dir / name), file=sys.stderr)
            return 2
    try:
        affected = load_json(temp_dir / AFFECTED_FILENAME)
        taxonomy = load_json(temp_dir / TAXONOMY_FILENAME)
        domain_map = load_json(temp_dir / DOMAIN_MAP_FILENAME)
    except ValueError as e:
        print("[ОШИБКА] Входной файл не читается как JSON: " + str(e), file=sys.stderr)
        return 2

    failed = 0
    written = 0
    for entry in affected.get("domains", []):
        domain_id = entry["domain_id"]
        analysis_path = temp_dir / (domain_id + "-analysis.json")
        if entry.get("new_files") and not analysis_path.exists():
            print("[ОШИБКА] Нет анализа дельты для " + domain_id + ": " + str(analysis_path), file=sys.stderr)
            failed += 1
            continue
        try:
            analysis = load_json(analysis_path, default={}) or {}
            data = build_input(entry, taxonomy, domain_map, analysis)
        except ValueError as e:
            print("[ОШИБКА] " + domain_id + ": " + str(e), file=sys.stderr)
            failed += 1
            continue
        save_json(temp_dir / OUT_DIRNAME / (domain_id + ".json"), data)
        written += 1
        print("[OK] " + domain_id + ": тем " + str(len(data["topics"])) + ", старых файлов " +
              str(len(data["old_files"])) + ", дельта " + str(len(data["delta_files"])))
    print("Записано входов: " + str(written) + ", с ошибками: " + str(failed))
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
