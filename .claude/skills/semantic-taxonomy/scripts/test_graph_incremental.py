#!/usr/bin/env python3
"""
Тесты инкрементального режима build_topic_graph.py на копиях fixtures/ (unittest, без внешних зависимостей).

Запуск:  python -m unittest test_graph_incremental   (из папки scripts)
Данные fixtures копируются во временную папку, исходные файлы не меняются. Сценарий:
граф ревизии 3 из fixtures - прежний граф, taxonomy.json собирается из его файловых
узлов, дельта (новые и изменённые файлы) добавляется в каждом тесте отдельно.
"""

import sys
import io
import os
import json
import copy
import shutil
import tempfile
import unittest
import subprocess
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

SCRIPTS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = SCRIPTS_DIR.parents[3] / ".claude" / "temp_files" / "_fixtures"  # живые данные, в гит не попадают
SCRIPT = SCRIPTS_DIR / "build_topic_graph.py"
sys.path.insert(0, str(SCRIPTS_DIR))

if not (FIXTURES_DIR / "topic-graph.json").exists():
    raise unittest.SkipTest("нет fixtures/topic-graph.json")

import build_topic_graph as btg  # noqa: E402

SCAN_TIME = 1_800_000_000
FRESH_TIME = SCAN_TIME + 100
STALE_TIME = SCAN_TIME - 100


def read_json(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def touch(path, when):
    os.utime(path, (when, when))


class GraphIncrementalBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="graph-inc-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copytree(FIXTURES_DIR, self.tmp, dirs_exist_ok=True)
        self.graph = read_json(self.tmp / "topic-graph.json")
        files = {k: n for k, n in self.graph["nodes"].items() if n["kind"] == "file"}
        self.old_files = files
        self.domain_files = {}
        for path, node in files.items():
            self.domain_files.setdefault(node["domain_id"], []).append(path)

        # taxonomy.json: файлы доменов из прежнего графа, без projects/unclassified
        write_json(self.tmp / "taxonomy.json", {
            "domains": [
                {"domain_id": d, "files": [{"path": p, "essence": files[p]["essence"]} for p in paths]}
                for d, paths in self.domain_files.items()
            ],
            "projects": {"count": 0, "items": []},
            "unclassified": {"count": 0, "items": []},
        })
        # структура инкремента без дельты: старые файлы берутся из графа
        self.structure = {"domains": [], "projects": {"items": []}, "unclassified": {"items": []}}
        write_json(self.tmp / "vault-structure-analysis.json", self.structure)
        self.scan = {"files": [{"path": p, "content_hash": n["content_hash"], "status": "unchanged"}
                               for p, n in files.items()]}
        self.save_scan()
        for name in ("domain-map.json", "graph-dirty-domains.json", "graph-revision-log.json"):
            # *.before.json от прежних прогонов в fixtures не бывает: проверка бэкапа честная
            self.assertFalse((self.tmp / name.replace(".json", ".before.json")).exists())
        self.set_times()

    # --- помощники сценария ---

    def save_scan(self):
        write_json(self.tmp / "vault-scan.json", self.scan)

    def set_times(self, analysis_time=FRESH_TIME):
        for p in self.tmp.glob("*.json"):
            touch(p, analysis_time if p.name.endswith("-analysis.json") else SCAN_TIME - 1000)
        touch(self.tmp / "vault-scan.json", SCAN_TIME)

    def run_graph(self, *flags):
        proc = subprocess.run([sys.executable, str(SCRIPT), "--temp-dir", str(self.tmp)] + list(flags),
                              capture_output=True, text=True, encoding="utf-8")
        return proc.returncode, proc.stderr

    def add_new_file(self, path, domain_id, dependencies=()):
        """Новый файл: в структуре домена, в скане со статусом new, запись в анализе домена."""
        self.structure["domains"].append({"domain_id": domain_id, "files": [{"path": path}]})
        write_json(self.tmp / "vault-structure-analysis.json", self.structure)
        self.scan["files"].append({"path": path, "content_hash": "new-" + path, "status": "new"})
        self.save_scan()
        self.add_analysis_entry(domain_id, path, dependencies)

    def add_analysis_entry(self, domain_id, path, dependencies):
        analysis_path = self.tmp / (domain_id + "-analysis.json")
        analysis = read_json(analysis_path)
        analysis["files"] = [f for f in analysis["files"] if f.get("path") != path]
        analysis["files"].append({"path": path, "title": Path(path).stem, "essence": "тест",
                                  "type": "concept", "key_concepts": [], "mentions": [],
                                  "dependencies": list(dependencies)})
        write_json(analysis_path, analysis)

    def mark_changed(self, path):
        for entry in self.scan["files"]:
            if entry["path"] == path:
                entry["status"] = "changed"
                entry["content_hash"] = "changed-" + path
        self.save_scan()

    def graph_after(self):
        return read_json(self.tmp / "topic-graph.json")

    @staticmethod
    def deps(graph, only_sources=None):
        return {(e["source"], e["target"]) for e in graph["edges"]
                if e["type"] == "dependency" and (only_sources is None or e["source"] in only_sources)}

    def raw(self, name):
        return (self.tmp / name).read_bytes()


class FreshnessTests(GraphIncrementalBase):
    def test_baseline_keeps_old_nodes_and_edges(self):
        code, err = self.run_graph()
        self.assertEqual(code, 0, err)
        after = self.graph_after()
        self.assertEqual(self.deps(after), self.deps(self.graph))
        self.assertEqual({k for k, n in after["nodes"].items() if n["kind"] == "file"}, set(self.old_files))
        member_of = {(e["source"], e["target"]) for e in after["edges"] if e["type"] == "member_of"}
        self.assertEqual(member_of, {(p, n["domain_id"]) for p, n in self.old_files.items()})

    def test_missing_analysis_is_error(self):
        self.add_new_file("Новая заметка.md", "data-vault")
        (self.tmp / "data-vault-analysis.json").unlink()
        before = self.raw("topic-graph.json")
        code, err = self.run_graph()
        self.assertEqual(code, 1, err)
        self.assertIn("data-vault-analysis.json", err)
        self.assertEqual(self.raw("topic-graph.json"), before)

    def test_stale_analysis_is_error(self):
        self.add_new_file("Новая заметка.md", "data-vault")
        self.set_times(analysis_time=STALE_TIME)
        before = self.raw("topic-graph.json")
        code, err = self.run_graph()
        self.assertEqual(code, 1, err)
        self.assertIn("старше vault-scan.json", err)
        self.assertEqual(self.raw("topic-graph.json"), before)
        self.assertFalse((self.tmp / "topic-graph.before.json").exists())

    def test_fresh_analysis_adds_delta_and_keeps_old_dependencies(self):
        self.add_new_file("Новая заметка.md", "data-vault", dependencies=["Hubs.md"])
        self.set_times()
        code, err = self.run_graph()
        self.assertEqual(code, 0, err)
        after = self.graph_after()
        self.assertIn("Новая заметка.md", after["nodes"])
        self.assertEqual(after["nodes"]["Новая заметка.md"]["domain_id"], "data-vault")
        self.assertEqual(self.deps(after) - self.deps(self.graph), {("Новая заметка.md", "Hubs.md")})
        self.assertTrue(self.deps(self.graph) <= self.deps(after))
        dirty = read_json(self.tmp / "graph-dirty-domains.json")
        self.assertEqual(dirty["new_file_ids"], ["Новая заметка.md"])
        self.assertIn("data-vault", dirty["dirty_domains"])

    def test_unclassified_delta_file_needs_no_analysis_and_has_no_node(self):
        self.scan["files"].append({"path": "Заметка без домена.md", "content_hash": "h", "status": "new"})
        self.save_scan()
        self.structure["unclassified"]["items"].append(
            {"path": "Заметка без домена.md", "reason": "тест", "suggestion": "тест"})
        write_json(self.tmp / "vault-structure-analysis.json", self.structure)
        self.set_times()
        code, err = self.run_graph()
        self.assertEqual(code, 0, err)
        self.assertNotIn("Заметка без домена.md", self.graph_after()["nodes"])

    def test_affected_domain_needs_validated_topics(self):
        write_json(self.tmp / "affected-domains.json",
                   {"domains": [{"domain_id": "data-vault", "reasons": ["new_files"], "new_files": []}]})
        self.set_times()
        code, err = self.run_graph()
        self.assertEqual(code, 1, err)
        self.assertIn("data-vault-topics.validated.json", err)
        validated = self.tmp / "data-vault-topics.validated.json"
        write_json(validated, {"topics": []})
        touch(validated, STALE_TIME)
        code, err = self.run_graph()
        self.assertEqual(code, 1, err)
        touch(validated, FRESH_TIME)
        code, err = self.run_graph()
        self.assertEqual(code, 0, err)
        self.assertIn("data-vault", read_json(self.tmp / "graph-dirty-domains.json")["dirty_domains"])

    def test_scan_without_status_is_error(self):
        for entry in self.scan["files"]:
            del entry["status"]
        self.save_scan()
        self.set_times()
        code, err = self.run_graph()
        self.assertEqual(code, 1, err)
        self.assertIn("status", err)


class CycleTests(GraphIncrementalBase):
    def test_cycle_with_delta_edge_fails(self):
        # в прежнем графе Links.md -> Hubs.md; изменённый Hubs.md теперь зависит от Links.md
        self.assertIn(("Links.md", "Hubs.md"), self.deps(self.graph))
        self.mark_changed("Hubs.md")
        self.add_analysis_entry("data-vault", "Hubs.md", ["Links.md"])
        self.set_times()
        before = self.raw("topic-graph.json")
        code, err = self.run_graph()
        self.assertEqual(code, 1, err)
        self.assertIn("[FAIL] Цикл dependency с ребром дельты", err)
        self.assertIn("Hubs.md", err)
        self.assertIn("Links.md", err)
        self.assertEqual(self.raw("topic-graph.json"), before)

    def test_old_cycle_only_warns(self):
        self.assertTrue(btg.find_dependency_cycles(self.graph), "в fixtures есть старые циклы")
        code, err = self.run_graph()
        self.assertEqual(code, 0, err)
        self.assertIn("[WARN] Цикл dependency из старых рёбер", err)
        self.assertNotIn("[FAIL]", err)

    def test_changed_file_dependencies_are_rebuilt(self):
        self.mark_changed("Hubs.md")
        self.add_analysis_entry("data-vault", "Hubs.md", [])
        old_hubs_edges = self.deps(self.graph, {"Hubs.md"})
        self.set_times()
        code, err = self.run_graph()
        self.assertEqual(code, 0, err)
        after = self.graph_after()
        self.assertEqual(self.deps(after, {"Hubs.md"}), set())
        self.assertEqual(self.deps(after) | old_hubs_edges, self.deps(self.graph) | set())
        self.assertEqual(after["nodes"]["Hubs.md"]["content_hash"], "changed-Hubs.md")

    def test_find_cycles_unit(self):
        graph = {"nodes": {k: {"kind": "file"} for k in "abcd"},
                 "edges": [{"source": s, "target": t, "type": "dependency"}
                           for s, t in (("a", "b"), ("b", "c"), ("c", "b"), ("d", "a"))]}
        self.assertEqual(btg.find_dependency_cycles(graph), [["b", "c", "b"]])


class RenameAndReconcileTests(GraphIncrementalBase):
    def test_rename_domain_by_merge_map(self):
        write_json(self.tmp / "domain-merge-map.json", {"data-vault": "data-vault-dwh"})
        self.set_times()
        code, err = self.run_graph()
        self.assertEqual(code, 0, err)
        after = self.graph_after()
        self.assertNotIn("data-vault", after["nodes"])
        self.assertNotIn("data-vault", after["domains"])
        domain = after["nodes"]["data-vault-dwh"]
        self.assertEqual(domain["kind"], "domain")
        self.assertEqual(sorted(domain["member_file_ids"]), sorted(self.domain_files["data-vault"]))
        for path in self.domain_files["data-vault"]:
            self.assertEqual(after["nodes"][path]["domain_id"], "data-vault-dwh")
        member_targets = {e["target"] for e in after["edges"] if e["type"] == "member_of"}
        self.assertNotIn("data-vault", member_targets)
        self.assertIn("data-vault-dwh", member_targets)
        self.assertEqual(self.deps(after), self.deps(self.graph))

    def test_merge_domains_by_merge_map(self):
        write_json(self.tmp / "domain-merge-map.json", {"inmon-corporate-warehouse": "data-vault"})
        self.set_times()
        code, err = self.run_graph()
        self.assertEqual(code, 0, err)
        after = self.graph_after()
        self.assertNotIn("inmon-corporate-warehouse", after["nodes"])
        expected = set(self.domain_files["data-vault"]) | set(self.domain_files["inmon-corporate-warehouse"])
        self.assertEqual(set(after["nodes"]["data-vault"]["member_file_ids"]), expected)
        self.assertEqual(after["domains"]["data-vault"]["node_count"], len(expected))
        self.assertEqual(self.deps(after), self.deps(self.graph))

    def test_rename_topic_nodes_in_unit(self):
        graph = {"nodes": {"a": {"kind": "domain", "last_progress_revision": 1},
                           "a::t": {"kind": "topic", "parent_domain_id": "a", "last_progress_revision": 0},
                           "f.md": {"kind": "file", "domain_id": "a", "topic_id": "a::t"}},
                 "edges": [{"source": "f.md", "target": "a::t", "type": "member_of"}],
                 "domains": {"a": {"node_count": 1}}}
        self.assertEqual(btg.apply_merge_map_to_graph(graph, {"a": "b"}), 1)
        self.assertEqual(set(graph["nodes"]), {"b", "b::t", "f.md"})
        self.assertEqual(graph["nodes"]["f.md"]["topic_id"], "b::t")
        self.assertEqual(graph["edges"][0]["target"], "b::t")
        self.assertEqual(set(graph["domains"]), {"b"})

    def test_reconcile_fails_on_delta_file_without_node(self):
        # новый файл есть в скане, но структура не разложила его ни в домен, ни в unclassified/projects
        self.scan["files"].append({"path": "Потерянная заметка.md", "content_hash": "h", "status": "new"})
        self.save_scan()
        self.set_times()
        before = self.raw("topic-graph.json")
        code, err = self.run_graph()
        self.assertEqual(code, 1, err)
        self.assertIn("[FAIL] Файлы из taxonomy.json/дельты без узла в графе", err)
        self.assertIn("Потерянная заметка.md", err)
        self.assertEqual(self.raw("topic-graph.json"), before)

    def test_layout_file_outside_scan_warns(self):
        taxonomy = read_json(self.tmp / "taxonomy.json")
        taxonomy["domains"][0]["files"].append({"path": "Призрак.md"})
        write_json(self.tmp / "taxonomy.json", taxonomy)
        self.set_times()
        code, err = self.run_graph()
        self.assertEqual(code, 0, err)
        self.assertIn("[WARN] Файл раскладки отсутствует в vault-scan.json: Призрак.md", err)

    def test_reconcile_fails_on_extra_graph_node(self):
        taxonomy = read_json(self.tmp / "taxonomy.json")
        removed = taxonomy["domains"][0]["files"].pop()["path"]
        write_json(self.tmp / "taxonomy.json", taxonomy)
        self.set_times()
        code, err = self.run_graph()
        self.assertEqual(code, 1, err)
        self.assertIn(removed, err)

    def test_related_between_domains_warns(self):
        graph = copy.deepcopy(self.graph)
        a, b = self.domain_files["data-vault"][0], self.domain_files["inmon-corporate-warehouse"][0]
        graph["edges"].append({"source": a, "target": b, "type": "related", "added_in_revision": 3})
        write_json(self.tmp / "topic-graph.json", graph)
        self.set_times()
        code, err = self.run_graph()
        self.assertEqual(code, 0, err)
        self.assertIn("[WARN] related между доменами", err)


class BackupTests(GraphIncrementalBase):
    def test_backup_before_write(self):
        original = {n: self.raw(n) for n in ("topic-graph.json", "graph-dirty-domains.json",
                                              "graph-revision-log.json")}
        code, err = self.run_graph()
        self.assertEqual(code, 0, err)
        for name, data in original.items():
            self.assertEqual(self.raw(name.replace(".json", ".before.json")), data, name)
        self.assertNotEqual(self.raw("topic-graph.json"), original["topic-graph.json"])

    def test_dry_run_writes_nothing(self):
        names = ("topic-graph.json", "graph-dirty-domains.json", "graph-revision-log.json")
        original = {n: self.raw(n) for n in names}
        listing = sorted(p.name for p in self.tmp.iterdir())
        code, err = self.run_graph("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("[DRY-RUN]", err)
        self.assertEqual({n: self.raw(n) for n in names}, original)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), listing)

    def test_dry_run_still_fails_on_checks(self):
        self.add_new_file("Новая заметка.md", "data-vault")
        self.set_times(analysis_time=STALE_TIME)
        code, err = self.run_graph("--dry-run")
        self.assertEqual(code, 1, err)


class FullModeTests(GraphIncrementalBase):
    def test_full_mode_skips_incremental_contract(self):
        (self.tmp / "vault-scan.json").unlink()
        code, err = self.run_graph("--full")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.graph_after()["revision"], self.graph["revision"] + 1)


if __name__ == "__main__":
    unittest.main()
