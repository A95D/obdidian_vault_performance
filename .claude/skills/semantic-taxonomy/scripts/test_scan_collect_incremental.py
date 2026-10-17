#!/usr/bin/env python3
"""
Тесты --scan / --collect --new-only и контракта delta_only (unittest, без внешних данных).

Запуск:  python -m unittest test_scan_collect_incremental   (из папки scripts)
Всё создаётся во временной папке: tmp/.claude/temp_files/ и tmp/vault/. .env не создаётся,
VAULT_PATH задаётся через переменную окружения.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

import build_topic_graph  # noqa: E402
import collect_vault_structure as cvs  # noqa: E402
import synthesize_taxonomy  # noqa: E402

TEXT_A = "Заметка A. " + "Содержательный текст про хранилища данных и моделирование. " * 3
TEXT_B_OLD = "Заметка B, прежняя версия. " + "Текст про индексы и планы запросов в базах данных. " * 3
TEXT_B_NEW = "Заметка B, новая версия. " + "Текст про индексы и планы запросов в базах данных. " * 3 + " Дополнение."
TEXT_C = "Заметка C. " + "Новый материал про измерения и факты в хранилище. " * 3
TEXT_NOISE = "коротко"  # меньше MIN_CONTENT_CHARS: отсеивается как empty_content


def quiet(func, *args, **kwargs):
    """Вызов с подавленным stderr: вывод тестов остаётся чистым."""
    with contextlib.redirect_stderr(io.StringIO()):
        return func(*args, **kwargs)


class IncrementalBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.temp_dir = self.root / ".claude" / "temp_files"
        self.vault = self.root / "vault"
        self.temp_dir.mkdir(parents=True)
        self.vault.mkdir()
        patcher = mock.patch.dict(os.environ, {"VAULT_PATH": str(self.vault)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, text in (("A.md", TEXT_A), ("B.md", TEXT_B_NEW), ("C.md", TEXT_C), ("noise.md", TEXT_NOISE)):
            (self.vault / name).write_text(text, encoding="utf-8")

    def write_json(self, name, data):
        (self.temp_dir / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def read_json(self, name):
        return json.loads((self.temp_dir / name).read_text(encoding="utf-8"))

    def write_graph(self, with_c=False):
        nodes = {
            "A.md": {"kind": "file", "content_hash": cvs.compute_content_hash(TEXT_A)},
            "B.md": {"kind": "file", "content_hash": cvs.compute_content_hash(TEXT_B_OLD)},
        }
        if with_c:
            nodes["C.md"] = {"kind": "file", "content_hash": cvs.compute_content_hash(TEXT_C)}
        self.write_json(cvs.GRAPH_FILENAME, {"nodes": nodes})

    def write_cache(self, texts):
        self.write_json(cvs.CACHE_FILENAME, {cvs.compute_content_hash(t): {} for t in texts})

    def scan(self, new_only=True, baseline_taxonomy=False):
        self.assertEqual(quiet(cvs.cmd_scan, self.root, new_only, baseline_taxonomy), 0)
        return self.read_json(cvs.SCAN_FILENAME)

    @staticmethod
    def by_path(scan):
        return {e["path"]: e for e in scan["files"]}


class ScanTests(IncrementalBase):
    def test_1_scan_with_cache_and_graph(self):
        self.write_graph()
        self.write_cache([TEXT_A])
        scan = self.scan()
        files = self.by_path(scan)
        self.assertEqual(files["A.md"]["status"], "unchanged")
        self.assertEqual(files["B.md"]["status"], "changed")
        self.assertEqual(files["C.md"]["status"], "new")
        self.assertEqual(scan["baseline"], "graph")
        self.assertEqual((scan["new_count"], scan["changed_count"], scan["unchanged_count"]), (1, 1, 1))
        self.assertEqual(scan["cached_count"], 1)
        self.assertTrue(files["A.md"]["in_cache"])
        self.assertFalse(files["B.md"]["in_cache"])
        self.assertEqual(scan["candidate_count"], 3)
        self.assertEqual([f["path"] for f in scan["filtered_out"]], ["noise.md"])

    def test_2_cache_does_not_define_novelty(self):
        self.write_graph()
        self.write_cache([TEXT_A, TEXT_C])
        files = self.by_path(self.scan())
        self.assertEqual(files["C.md"]["status"], "new")
        self.assertTrue(files["C.md"]["in_cache"])

    def test_3_no_graph(self):
        self.write_cache([TEXT_A, TEXT_C])
        scan = self.scan()
        self.assertEqual(scan["baseline"], "none")
        self.assertEqual({e["status"] for e in scan["files"]}, {"new"})
        self.assertEqual(scan["new_count"], 3)
        self.assertEqual(scan["cached_count"], 2)

    def test_4_empty_or_missing_cache(self):
        self.write_graph()
        for prepare in (lambda: self.write_json(cvs.CACHE_FILENAME, {}), lambda: None):
            prepare()
            scan = self.scan()
            self.assertEqual(scan["cached_count"], 0)
            statuses = {e["path"]: e["status"] for e in scan["files"]}
            self.assertEqual(statuses, {"A.md": "unchanged", "B.md": "changed", "C.md": "new"})

    def test_5_baseline_taxonomy(self):
        self.write_json(cvs.TAXONOMY_FILENAME, {
            "domains": [{"domain_id": "d1", "files": [{"path": "A.md"}, {"path": "B.md"}]}],
        })
        scan = self.scan(baseline_taxonomy=True)
        statuses = {e["path"]: e["status"] for e in scan["files"]}
        self.assertEqual(statuses, {"A.md": "unchanged", "B.md": "unchanged", "C.md": "new"})
        self.assertEqual(scan["baseline"], "taxonomy")


class CollectTests(IncrementalBase):
    def prepare(self):
        self.write_graph()
        self.write_cache([TEXT_A])
        self.scan()
        self.write_json(cvs.DOMAIN_MAP_FILENAME, {
            "domains": [{"id": "d1", "name": "Домен 1",
                         "topics": [{"name": "t1", "files": ["A.md", "B.md", "C.md"]}]}],
        })

    def collect(self, new_only):
        self.assertEqual(quiet(cvs.cmd_collect, self.root, new_only), 0)
        return self.read_json(cvs.OUTPUT_FILENAME)

    def test_6_collect_new_only(self):
        self.prepare()
        data = self.collect(True)
        self.assertEqual({f["path"] for f in data["domains"][0]["files"]}, {"B.md", "C.md"})
        self.assertEqual({f["path"] for f in data["all_files"]}, {"B.md", "C.md"})
        meta = data["metadata"]
        self.assertEqual(meta["mode"], "incremental")
        self.assertIs(meta["delta_only"], True)
        self.assertEqual(meta["new_candidates_count"], 2)
        self.assertEqual(meta["total_files"], 2)

    def test_7_collect_full(self):
        self.prepare()
        data = self.collect(False)
        self.assertEqual({f["path"] for f in data["domains"][0]["files"]}, {"A.md", "B.md", "C.md"})
        self.assertEqual(data["metadata"]["mode"], "full")
        self.assertIs(data["metadata"]["delta_only"], False)


class ContractTests(IncrementalBase):
    def test_8_check_delta_contract(self):
        for module in (synthesize_taxonomy, build_topic_graph):
            check = module.check_delta_contract
            with self.subTest(module=module.__name__):
                self.assertTrue(check({"mode": "incremental"}))
                self.assertTrue(check({"mode": "incremental", "delta_only": False}))
                self.assertEqual(check({"mode": "incremental", "delta_only": True}), [])
                self.assertEqual(check({"mode": "full", "delta_only": False}), [])
                self.assertTrue(check({"mode": "full", "delta_only": True}))
                self.assertEqual(check({}), [])
                self.assertEqual(check(None), [])

    def test_9_run_merge_rejects_incremental_without_flag(self):
        for name in (synthesize_taxonomy.TAXONOMY_FILENAME, synthesize_taxonomy.SCAN_FILENAME,
                     synthesize_taxonomy.DOMAIN_MAP_FILENAME, synthesize_taxonomy.AFFECTED_FILENAME,
                     synthesize_taxonomy.GRAPH_FILENAME):
            self.write_json(name, {})
        # граф пишем последним, чтобы он был не старше скана и карты доменов
        self.write_json(synthesize_taxonomy.STRUCTURE_FILENAME, {"metadata": {"mode": "incremental"}})
        self.write_json(synthesize_taxonomy.GRAPH_FILENAME, {})
        taxonomy_path = self.temp_dir / synthesize_taxonomy.TAXONOMY_FILENAME
        before = taxonomy_path.read_bytes()
        self.assertEqual(quiet(synthesize_taxonomy.run_merge, self.temp_dir, True), 2)
        self.assertEqual(taxonomy_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
