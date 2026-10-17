#!/usr/bin/env python3
"""
Карточки доменов для дешёвого пересмотра структуры (Фаза 2, режим инкремента).

Читает ТОЛЬКО taxonomy.json (старые тексты заметок не читаются) и пишет
domain-cards.json: по карточке на домен (название, назначение, словарь, темы
с essence, число файлов) плюс секции unclassified и projects целиком.

Использование:
  python build_domain_cards.py [--temp-dir DIR]
"""

import sys
import io
import json
import argparse
from collections import Counter
from pathlib import Path
from datetime import datetime

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

TAXONOMY_FILENAME = "taxonomy.json"
CARDS_FILENAME = "domain-cards.json"

# Сколько терминов словаря попадает в карточку домена (по частоте у файлов).
VOCAB_LIMIT = 12
# Обрезка essence файла, когда он склеивается в essence темы.
ESSENCE_CUT = 160


def get_project_root():
    """Определить корень проекта по расположению скрипта."""
    return Path(__file__).resolve().parents[4]


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def top_terms(files, field):
    """Самые частые значения списочного поля у файлов домена (порядок стабилен)."""
    counter = Counter()
    for f in files:
        for term in f.get(field) or []:
            counter[term] += 1
    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [term for term, _ in ranked[:VOCAB_LIMIT]]


def topic_essence(topic, essence_by_path):
    """essence темы: essence её файлов; у темы без заметок - её summary."""
    parts = []
    for p in topic.get("note_paths") or []:
        text = (essence_by_path.get(p) or "").strip()
        if text:
            parts.append(text[:ESSENCE_CUT])
    if parts:
        return " | ".join(parts)
    return topic.get("essence") or topic.get("summary") or ""


def build_card(domain):
    files = domain.get("files") or []
    essence_by_path = {f.get("path"): f.get("essence") for f in files}
    topics = []
    for t in domain.get("topics") or []:
        note_paths = list(t.get("note_paths") or [])
        topics.append({
            "topic_id": t.get("topic_id"),
            "topic_name": t.get("topic_name"),
            "essence": topic_essence(t, essence_by_path),
            "file_count": len(note_paths),
            "has_notes": bool(note_paths),
            "mastery_percent": t.get("mastery_percent"),
            "note_paths": note_paths,
        })
    return {
        "domain_id": domain.get("domain_id"),
        "domain_name": domain.get("domain_name"),
        "purpose": domain.get("description") or domain.get("purpose") or "",
        "key_concepts": top_terms(files, "key_concepts"),
        "technologies": top_terms(files, "technologies"),
        "file_count": len(files),
        "topics": topics,
    }


def section_items(section):
    """taxonomy.json хранит projects/unclassified как {count, items}; допускается и голый список."""
    if isinstance(section, dict):
        return list(section.get("items") or [])
    return list(section or [])


def main():
    parser = argparse.ArgumentParser(description="Карточки доменов из taxonomy.json")
    parser.add_argument("--temp-dir", help="директория temp_files (по умолчанию .claude/temp_files проекта)")
    args = parser.parse_args()

    temp_dir = Path(args.temp_dir) if args.temp_dir else get_project_root() / ".claude" / "temp_files"
    taxonomy_path = temp_dir / TAXONOMY_FILENAME
    if not taxonomy_path.exists():
        print("[ОШИБКА] Нет входа " + str(taxonomy_path) + ": карточки строятся только из taxonomy.json",
              file=sys.stderr)
        return 2
    try:
        taxonomy = load_json(taxonomy_path)
    except ValueError as e:
        print("[ОШИБКА] taxonomy.json не читается как JSON: " + str(e), file=sys.stderr)
        return 2
    if not isinstance(taxonomy.get("domains"), list):
        print("[ОШИБКА] В taxonomy.json нет списка domains", file=sys.stderr)
        return 2

    cards = {
        "generated": datetime.now().strftime("%Y-%m-%d"),
        "source": TAXONOMY_FILENAME,
        "domains": [build_card(d) for d in taxonomy["domains"]],
        "unclassified": section_items(taxonomy.get("unclassified")),
        "projects": section_items(taxonomy.get("projects")),
    }

    out_path = temp_dir / CARDS_FILENAME
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cards, f, ensure_ascii=False, indent=2)
    topics_total = sum(len(c["topics"]) for c in cards["domains"])
    print("[OK] " + str(out_path) + ": доменов " + str(len(cards["domains"])) +
          ", тем " + str(topics_total) + ", unclassified " + str(len(cards["unclassified"])) +
          ", projects " + str(len(cards["projects"])), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
