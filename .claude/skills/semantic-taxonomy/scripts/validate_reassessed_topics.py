#!/usr/bin/env python3
"""
Проверка ответа LLM по переоценке тем (Фаза 3б). Ответу не доверяем.

Читает {domain_id}-topics.json, пишет очищенный {domain_id}-topics.validated.json.
Файлы домена - старые из taxonomy.json (или all_paths из reassess-input/{domain_id}.json,
если он есть) плюс дельта из affected-domains.json.

Правила:
  - has_notes выводится из note_paths (ответ перезаписывается, WARN при расхождении)
  - note_paths только из файлов домена
  - topic_id уникальны, обязательные поля на месте
  - mastery_percent - целое 0-100; у тем без заметок не выше 15 (клип, WARN)
  - каждый файл домена привязан хотя бы к одной теме (один файл может подкреплять несколько тем)

Использование:
  python validate_reassessed_topics.py --domain DOMAIN_ID [--temp-dir DIR] [--input FILE]

Код выхода: 0 - ответ годен (возможны WARN), 1 - ошибки в ответе, 2 - нет входов.
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

TAXONOMY_FILENAME = "taxonomy.json"
AFFECTED_FILENAME = "affected-domains.json"
REASSESS_DIRNAME = "reassess-input"
# Потолок mastery у темы без заметок (ожидаемая тема).
EXPECTED_MASTERY_CAP = 15
STRING_FIELDS = ("topic_id", "topic_name", "summary", "mastery_rationale")


def get_project_root():
    """Определить корень проекта по расположению скрипта."""
    return Path(__file__).resolve().parents[4]


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def is_int(value):
    """Целое число (bool в JSON не считается числом)."""
    return isinstance(value, int) and not isinstance(value, bool)


def domain_paths(domain_id, taxonomy, affected, reassess=None):
    """Множество файлов домена: старые из taxonomy.json плюс дельта из affected-domains.json."""
    if reassess and reassess.get("all_paths"):
        return set(reassess["all_paths"])
    paths = set()
    for dom in (taxonomy or {}).get("domains", []):
        if dom.get("domain_id") == domain_id:
            paths.update(f["path"] for f in dom.get("files", []))
    for entry in (affected or {}).get("domains", []):
        if entry.get("domain_id") == domain_id:
            paths.update(entry.get("new_files") or [])
    return paths


def validate(doc, paths):
    """Вернуть (очищенный документ, ошибки, предупреждения)."""
    errors = []
    warns = []
    if not isinstance(doc, dict) or not isinstance(doc.get("topics"), list) or not doc["topics"]:
        return None, ["в ответе нет непустого списка topics"], warns
    description = doc.get("description")
    if not isinstance(description, str) or not description.strip():
        warns.append("нет обновлённого description домена")
        description = ""

    seen_ids = set()
    owner = {}
    cleaned = []
    for i, topic in enumerate(doc["topics"]):
        label = "тема #" + str(i + 1)
        if not isinstance(topic, dict):
            errors.append(label + ": не объект")
            continue
        tid = topic.get("topic_id")
        if isinstance(tid, str) and tid.strip():
            label = "тема " + tid
        for field in STRING_FIELDS:
            if not isinstance(topic.get(field), str) or not topic[field].strip():
                errors.append(label + ": нет обязательного поля " + field)
        if isinstance(tid, str):
            if tid in seen_ids:
                errors.append(label + ": дубль topic_id")
            seen_ids.add(tid)

        note_paths = topic.get("note_paths")
        if not isinstance(note_paths, list) or not all(isinstance(p, str) for p in note_paths):
            errors.append(label + ": note_paths должен быть списком строк")
            note_paths = []
        unique_paths = list(dict.fromkeys(note_paths))
        if len(unique_paths) != len(note_paths):
            warns.append(label + ": повторы в note_paths убраны")
        for p in unique_paths:
            if p not in paths:
                errors.append(label + ": путь не из файлов домена: " + p)
            else:
                owner.setdefault(p, tid)

        has_notes = bool(unique_paths)
        if topic.get("has_notes") != has_notes:
            warns.append(label + ": has_notes " + json.dumps(topic.get("has_notes")) +
                         " заменён на " + json.dumps(has_notes) + " по note_paths")

        mastery = topic.get("mastery_percent")
        if not is_int(mastery) or not 0 <= mastery <= 100:
            errors.append(label + ": mastery_percent должен быть целым 0-100, получено " + json.dumps(mastery))
            mastery = 0
        elif not has_notes and mastery > EXPECTED_MASTERY_CAP:
            warns.append(label + ": нет заметок, mastery_percent " + str(mastery) +
                         " сокращён до " + str(EXPECTED_MASTERY_CAP))
            mastery = EXPECTED_MASTERY_CAP

        item = dict(topic)
        item.update({"has_notes": has_notes, "note_paths": unique_paths,
                     "mastery_percent": mastery, "related_domain_topics": []})
        cleaned.append(item)

    for p in sorted(paths - set(owner)):
        errors.append("файл домена не привязан ни к одной теме: " + p)

    result = {"domain_id": doc.get("domain_id"), "description": description, "topics": cleaned}
    return result, errors, warns


def main():
    parser = argparse.ArgumentParser(description="Проверка ответа переоценки тем (Фаза 3б)")
    parser.add_argument("--domain", required=True, help="domain_id")
    parser.add_argument("--temp-dir", help="директория temp_files (по умолчанию .claude/temp_files проекта)")
    parser.add_argument("--input", help="путь к {domain_id}-topics.json (по умолчанию temp-dir)")
    args = parser.parse_args()
    temp_dir = Path(args.temp_dir) if args.temp_dir else get_project_root() / ".claude" / "temp_files"
    in_path = Path(args.input) if args.input else temp_dir / (args.domain + "-topics.json")
    out_path = temp_dir / (args.domain + "-topics.validated.json")

    for path in (in_path, temp_dir / TAXONOMY_FILENAME, temp_dir / AFFECTED_FILENAME):
        if not path.exists():
            print("[ОШИБКА] Нет входа: " + str(path), file=sys.stderr)
            return 2
    try:
        doc = load_json(in_path)
        taxonomy = load_json(temp_dir / TAXONOMY_FILENAME)
        affected = load_json(temp_dir / AFFECTED_FILENAME)
        reassess = load_json(temp_dir / REASSESS_DIRNAME / (args.domain + ".json"))
    except ValueError as e:
        print("[ОШИБКА] Вход не читается как JSON: " + str(e), file=sys.stderr)
        return 2

    paths = domain_paths(args.domain, taxonomy, affected, reassess)
    if not paths:
        print("[ОШИБКА] У домена " + args.domain + " нет файлов (ни в taxonomy.json, ни в дельте)", file=sys.stderr)
        return 2
    result, errors, warns = validate(doc, paths)
    for w in warns:
        print("[WARN] " + args.domain + ": " + w, file=sys.stderr)
    if errors:
        if out_path.exists():
            out_path.unlink()  # старый validated не должен пережить отклонённый ответ
        for e in errors:
            print("[FAIL] " + args.domain + ": " + e, file=sys.stderr)
        print("Ответ отклонён: ошибок " + str(len(errors)) + ", " + out_path.name + " не записан", file=sys.stderr)
        return 1
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("[OK] " + args.domain + ": тем " + str(len(result["topics"])) + ", файлов " + str(len(paths)) +
          ", WARN " + str(len(warns)) + " -> " + out_path.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
