#!/usr/bin/env python3
"""
Тесты validate_reassessed_topics.py на данных fixtures/ (unittest, без внешних зависимостей).

Запуск:  python -m unittest test_reassess   (из папки scripts)
Домен data-vault: старые файлы из taxonomy.json, дельта - файлы из data-vault-analysis.json.
"""

import sys
import io
import json
import copy
import unittest
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

SCRIPTS_DIR = Path(__file__).resolve().parent
# Живые данные прогона (личный vault, в гит не попадают): нет данных - тесты пропускаются.
FIXTURES_DIR = SCRIPTS_DIR.parents[3] / ".claude" / "temp_files" / "_fixtures"
if not (FIXTURES_DIR / "taxonomy.json").exists():
    raise unittest.SkipTest("нет .claude/temp_files/taxonomy.json")
sys.path.insert(0, str(SCRIPTS_DIR))

import validate_reassessed_topics as vrt  # noqa: E402

DOMAIN_ID = "data-vault"
OLD_TOPIC_OF = {"Data Vault 2.0.md": "dv-architecture",
                "Bridge Таблицы.md": "bridge-tables", "Bridge Таблицыё.md": "bridge-tables"}


def load(name):
    with open(FIXTURES_DIR / name, "r", encoding="utf-8") as f:
        return json.load(f)


def make_case():
    """Файлы домена и корректный ответ LLM: темы дельты из анализа плюс темы старых файлов."""
    taxonomy = load("taxonomy.json")
    analysis = load("data-vault-analysis.json")
    old = {t["topic_id"]: t for d in taxonomy["domains"] if d["domain_id"] == DOMAIN_ID for t in d["topics"]}
    delta_topics = [t for t in analysis["domain_summary"]["topics"] if t["has_notes"]]
    delta_paths = [p for t in delta_topics for p in t["note_paths"]]
    old_files = [f["path"] for d in taxonomy["domains"] if d["domain_id"] == DOMAIN_ID for f in d["files"]]
    topics = copy.deepcopy(delta_topics)
    for tid, topic in old.items():
        if topic["note_paths"]:
            topics.append(copy.deepcopy(topic))
    # ожидаемые темы без заметок из анализа дельты, которых нет среди тем с заметками
    have = {t["topic_id"] for t in topics}
    topics += [copy.deepcopy(t) for t in analysis["domain_summary"]["topics"]
               if not t["has_notes"] and t["topic_id"] not in have]
    doc = {"domain_id": DOMAIN_ID, "description": "Data Vault 2.0 и его структуры.", "topics": topics}
    return doc, set(old_files) | set(delta_paths)


def by_id(doc, topic_id):
    return next(t for t in doc["topics"] if t["topic_id"] == topic_id)


class ValidateReassessedTopicsTest(unittest.TestCase):

    def setUp(self):
        self.doc, self.paths = make_case()

    def run_validate(self):
        return vrt.validate(self.doc, self.paths)

    def test_fixture_case_is_sane(self):
        self.assertGreaterEqual(len(self.paths), 9)
        self.assertIn("Hubs.md", self.paths)
        self.assertIn("Data Vault 2.0.md", self.paths)

    def test_correct_answer(self):
        result, errors, warns = self.run_validate()
        self.assertEqual(errors, [])
        self.assertEqual(warns, [])
        self.assertEqual(len(result["topics"]), len(self.doc["topics"]))
        self.assertEqual({p for t in result["topics"] for p in t["note_paths"]}, self.paths)

    def test_has_notes_mismatch_is_overwritten(self):
        by_id(self.doc, "hubs")["has_notes"] = False
        empty = by_id(self.doc, "bridge-tables")
        empty["note_paths"] = []
        by_id(self.doc, "dv-architecture")["note_paths"].extend(OLD_TOPIC_OF_BRIDGE())
        result, errors, warns = self.run_validate()
        self.assertEqual(errors, [])
        self.assertTrue(by_id(result, "hubs")["has_notes"])
        self.assertFalse(by_id(result, "bridge-tables")["has_notes"])
        self.assertTrue(any("hubs" in w and "has_notes" in w for w in warns))

    def test_duplicate_topic_id(self):
        dup = copy.deepcopy(by_id(self.doc, "raw-vs-business-vault"))
        self.doc["topics"].append(dup)
        _, errors, _ = self.run_validate()
        self.assertTrue(any("дубль topic_id" in e for e in errors))

    def test_mastery_out_of_range(self):
        by_id(self.doc, "hubs")["mastery_percent"] = 150
        _, errors, _ = self.run_validate()
        self.assertTrue(any("mastery_percent" in e and "150" in e for e in errors))

    def test_mastery_not_integer(self):
        by_id(self.doc, "hubs")["mastery_percent"] = 55.5
        _, errors, _ = self.run_validate()
        self.assertTrue(any("mastery_percent" in e for e in errors))

    def test_expected_topic_mastery_is_clipped(self):
        by_id(self.doc, "raw-vs-business-vault")["mastery_percent"] = 80
        result, errors, warns = self.run_validate()
        self.assertEqual(errors, [])
        topic = by_id(result, "raw-vs-business-vault")
        self.assertEqual(topic["mastery_percent"], vrt.EXPECTED_MASTERY_CAP)
        self.assertFalse(topic["has_notes"])
        self.assertTrue(any("raw-vs-business-vault" in w and "сокращён" in w for w in warns))

    def test_file_without_topic(self):
        by_id(self.doc, "hubs")["note_paths"] = []
        _, errors, _ = self.run_validate()
        self.assertTrue(any("не привязан ни к одной теме" in e and "Hubs.md" in e for e in errors))

    def test_file_in_two_topics_is_allowed(self):
        by_id(self.doc, "links")["note_paths"].append("Hubs.md")
        _, errors, _ = self.run_validate()
        self.assertEqual(errors, [])

    def test_unknown_note_path(self):
        by_id(self.doc, "hubs")["note_paths"].append("Выдуманная заметка.md")
        _, errors, _ = self.run_validate()
        self.assertTrue(any("не из файлов домена" in e for e in errors))

    def test_expected_topic_with_note_paths_is_not_expected(self):
        # ожидаемая тема с заметкой становится обычной: has_notes выводится из note_paths
        topic = by_id(self.doc, "raw-vs-business-vault")
        topic["note_paths"] = ["Hubs.md"]
        by_id(self.doc, "hubs")["note_paths"] = []
        result, _, _ = self.run_validate()
        self.assertTrue(by_id(result, "raw-vs-business-vault")["has_notes"])


def OLD_TOPIC_OF_BRIDGE():
    return [p for p, t in OLD_TOPIC_OF.items() if t == "bridge-tables"]


class TestDeltaRecords(unittest.TestCase):
    """delta_records: записи дельты по path, старые записи в анализе не мешают."""

    def test_extra_old_records_are_ignored(self):
        import build_reassess_input as bri
        analysis = {"files": [{"path": "a.md"}, {"path": "b.md"}, {"path": "c.md"}]}
        out = bri.delta_records("d", ["c.md", "a.md"], analysis)
        self.assertEqual(list(out), ["c.md", "a.md"])

    def test_missing_delta_record_fails(self):
        import build_reassess_input as bri
        with self.assertRaises(ValueError):
            bri.delta_records("d", ["z.md"], {"files": [{"path": "a.md"}]})


if __name__ == "__main__":
    unittest.main()
