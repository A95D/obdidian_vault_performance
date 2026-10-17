#!/usr/bin/env python3
"""
Тесты synthesize_taxonomy.py --merge на копии fixtures/ (unittest, без внешних зависимостей).

Запуск:  python -m unittest test_merge_incremental   (из папки scripts)
Данные fixtures копируются во временную папку, исходные файлы не меняются. Сценарий:
прежняя taxonomy.json собирается из fixtures (три домена из taxonomy.json плюс шесть
доменов из {domain}-analysis.json), затронутыми считаются data-vault, kimball-dimensional-modeling
и postgresql-query-optimization: их дельта (файлы domain-map.json, которых не было в
прежней таксономии) приходит из анализов, темы - из {domain}-topics.validated.json.
Остальные шесть доменов - незатронутые.
"""

import sys
import io
import os
import json
import shutil
import tempfile
import unittest
import subprocess
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

SCRIPTS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = SCRIPTS_DIR.parents[3] / ".claude" / "temp_files" / "_fixtures"  # живые данные, в гит не попадают
SCRIPT = SCRIPTS_DIR / "synthesize_taxonomy.py"
sys.path.insert(0, str(SCRIPTS_DIR))

if not (FIXTURES_DIR / "taxonomy.json").exists():
    raise unittest.SkipTest("нет fixtures/taxonomy.json")

import synthesize_taxonomy as st  # noqa: E402

AFFECTED = ["data-vault", "kimball-dimensional-modeling", "postgresql-query-optimization"]
UNAFFECTED = ["inmon-corporate-warehouse", "mpp-greenplum", "requirements-beam",
              "synthetic-data", "dbt-automation", "context-engineering"]
SCAN_TIME = 1_800_000_000
GRAPH_TIME = SCAN_TIME + 100


def read_json(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def touch(path, when):
    os.utime(path, (when, when))


def single_owner(topics, paths):
    """
    Темы, где каждый путь из paths привязан хотя бы к одной теме: повторы убираются,
    непривязанные файлы дописываются в первую тему с заметками, has_notes по note_paths.
    """
    topics = json.loads(json.dumps(topics))
    seen = set()
    for t in topics:
        t["note_paths"] = [p for p in dict.fromkeys(t.get("note_paths", []))
                           if p in paths and p not in seen]
        seen.update(t["note_paths"])
    rest = [p for p in paths if p not in seen]
    if rest:
        target = next((t for t in topics if t["note_paths"]), topics[0])
        target["note_paths"] += rest
    for t in topics:
        t["has_notes"] = bool(t["note_paths"])
        t["related_domain_topics"] = []
        if not t["has_notes"]:
            t["mastery_percent"] = min(t.get("mastery_percent", 0), 15)
    return topics


def run_merge(temp_dir, *flags):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--merge", "--temp-dir", str(temp_dir)] + list(flags),
        capture_output=True, text=True, encoding="utf-8")
    return proc.returncode, proc.stderr


def file_bytes(path):
    return Path(path).read_bytes()


class MergeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="merge-inc-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copytree(FIXTURES_DIR, self.tmp, dirs_exist_ok=True)
        fixture_taxonomy = read_json(self.tmp / "taxonomy.json")
        domain_map = read_json(self.tmp / "domain-map.json")
        scan = read_json(self.tmp / "vault-scan.json")
        structure = read_json(self.tmp / "vault-structure-analysis.json")
        analyses = {}
        for d in AFFECTED + UNAFFECTED:
            analyses[d] = read_json(self.tmp / (d + "-analysis.json"))

        layout = {d["id"]: st.layout_domain_paths(d) for d in domain_map["domains"]}
        old_files = {d["domain_id"]: d["files"] for d in fixture_taxonomy["domains"]}
        old_topics = {d["domain_id"]: d["topics"] for d in fixture_taxonomy["domains"]}

        # прежняя таксономия: домены в порядке domain-map.json
        analysis_data = {}
        for d in domain_map["domains"]:
            did = d["id"]
            if did in old_files:
                files = old_files[did]
                topics = single_owner(old_topics[did], [f["path"] for f in files])
            else:
                files = analyses[did]["files"]
                topics = single_owner(analyses[did]["domain_summary"]["topics"], [f["path"] for f in files])
            analysis_data[did] = {"files": files, "domain_name": d["name"], "description": d.get("purpose", ""),
                                  "domain_summary": {"topics": topics}}
        self.old_delta = {}
        for did in AFFECTED:
            old_paths = {f["path"] for f in old_files[did]}
            self.old_delta[did] = [p for p in layout[did] if p not in old_paths]
        # прежняя таксономия содержит только старые файлы затронутых доменов
        metadata = {"vault_path": fixture_taxonomy["metadata"]["vault_path"]}
        old = st.generate_taxonomy_json(analysis_data, metadata, fixture_taxonomy["filtered_out"],
                                        domain_map["projects"], domain_map["unclassified"])
        old["metadata"]["all_files_count"] = (old["metadata"]["total_files"] + old["projects"]["count"]
                                              + old["unclassified"]["count"] + old["filtered_out"]["count"])
        old["metadata"]["mode"] = "full"
        write_json(self.tmp / "taxonomy.json", old)
        self.old = old

        delta_all = {p for ps in self.old_delta.values() for p in ps}
        for e in scan["files"]:
            e["status"] = "new" if e["path"] in delta_all else "unchanged"
        scan["new_count"] = len(delta_all)
        write_json(self.tmp / "vault-scan.json", scan)
        structure["metadata"]["mode"] = "incremental"
        structure["metadata"]["new_candidates_count"] = len(delta_all)
        structure["metadata"]["new_filtered_count"] = 1
        write_json(self.tmp / "vault-structure-analysis.json", structure)

        # ожидаемое число файлов vault: прежнее + дельта + новый шум
        self.expected_all = old["metadata"]["all_files_count"] + len(delta_all) + 1

        affected = {"generated": "2026-10-17", "topic_state": [],
                    "domains": [{"domain_id": d, "reasons": ["new_files"], "new_files": sorted(self.old_delta[d])}
                                for d in AFFECTED]}
        write_json(self.tmp / "affected-domains.json", affected)
        self.validated = {}
        for did in AFFECTED:
            topics = single_owner(analyses[did]["domain_summary"]["topics"], layout[did])
            doc = {"domain_id": did, "description": "Переоценённое описание " + did, "topics": topics}
            self.validated[did] = doc
            write_json(self.tmp / (did + "-topics.validated.json"), doc)

        for name in ("vault-scan.json", "domain-map.json"):
            touch(self.tmp / name, SCAN_TIME)
        touch(self.tmp / "topic-graph.json", GRAPH_TIME)

    def domain(self, taxonomy, domain_id):
        return next(d for d in taxonomy["domains"] if d["domain_id"] == domain_id)

    def snapshot(self):
        return {n: file_bytes(self.tmp / n) for n in ("taxonomy.json", "semantic-analysis-cache.json")}

    def assertNothingWritten(self, before):
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.tmp / "taxonomy.before.json").exists())


class MergeAffectedDomainTest(MergeBase):
    def test_affected_domain_replaced_with_validated_topics(self):
        rc, err = run_merge(self.tmp)
        self.assertEqual(rc, 0, err)
        taxonomy = read_json(self.tmp / "taxonomy.json")
        self.assertEqual(taxonomy["metadata"]["mode"], "incremental")
        for did in AFFECTED:
            dom = self.domain(taxonomy, did)
            doc = self.validated[did]
            self.assertEqual(dom["description"], doc["description"])
            self.assertEqual([t["topic_id"] for t in dom["topics"]], [t["topic_id"] for t in doc["topics"]])
            self.assertEqual([t["mastery_percent"] for t in dom["topics"]],
                             [t["mastery_percent"] for t in doc["topics"]])
            old_count = self.domain(self.old, did)["file_count"]
            self.assertEqual(dom["file_count"], old_count + len(self.old_delta[did]))
        self.assertEqual(taxonomy["statistics"]["total_files"], sum(d["file_count"] for d in taxonomy["domains"]))
        self.assertEqual(taxonomy["metadata"]["all_files_count"], self.expected_all)

    def test_old_file_records_kept_and_delta_added(self):
        rc, err = run_merge(self.tmp)
        self.assertEqual(rc, 0, err)
        taxonomy = read_json(self.tmp / "taxonomy.json")
        dom = self.domain(taxonomy, "data-vault")
        old_dom = self.domain(self.old, "data-vault")
        by_path = {f["path"]: f for f in dom["files"]}
        for f in old_dom["files"]:
            self.assertEqual(by_path[f["path"]], f)
        for p in self.old_delta["data-vault"]:
            self.assertIn(p, by_path)

    def test_unaffected_domains_unchanged_except_derived_fields(self):
        rc, err = run_merge(self.tmp)
        self.assertEqual(rc, 0, err)
        taxonomy = read_json(self.tmp / "taxonomy.json")
        for did in UNAFFECTED:
            self.assertEqual(
                st.canonical_json(st.strip_derived(self.domain(taxonomy, did))),
                st.canonical_json(st.strip_derived(self.domain(self.old, did))), did)

    def test_recommendations_recomputed_across_domains(self):
        # тема соседнего домена закрывается: её mastery поднимается выше порога, рекомендация уходит
        did = "data-vault"
        doc = self.validated[did]
        for t in doc["topics"]:
            t["mastery_percent"] = 100
            t["has_notes"] = True
        write_json(self.tmp / (did + "-topics.validated.json"), doc)
        # has_notes=True у тем без заметок нарушит согласованность, поэтому берём только темы с заметками
        for t in doc["topics"]:
            if not t["note_paths"]:
                t["has_notes"] = False
                t["mastery_percent"] = 10
        write_json(self.tmp / (did + "-topics.validated.json"), doc)
        rc, err = run_merge(self.tmp)
        self.assertEqual(rc, 0, err)
        taxonomy = read_json(self.tmp / "taxonomy.json")
        closed = {t["topic_id"] for t in doc["topics"] if t["note_paths"]}
        for dom in taxonomy["domains"]:
            for rec in dom["recommended_topics"]:
                if rec["domain_id"] == did:
                    self.assertNotIn(rec["topic_id"], closed)

    def test_rename_of_unaffected_domain_via_merge_map(self):
        domain_map = read_json(self.tmp / "domain-map.json")
        for d in domain_map["domains"]:
            if d["id"] == "inmon-corporate-warehouse":
                d["id"] = "inmon-dwh"
        write_json(self.tmp / "domain-map.json", domain_map)
        write_json(self.tmp / "domain-merge-map.json", {"inmon-corporate-warehouse": "inmon-dwh"})
        touch(self.tmp / "domain-map.json", SCAN_TIME)
        rc, err = run_merge(self.tmp)
        self.assertEqual(rc, 0, err)
        taxonomy = read_json(self.tmp / "taxonomy.json")
        ids = [d["domain_id"] for d in taxonomy["domains"]]
        self.assertIn("inmon-dwh", ids)
        self.assertNotIn("inmon-corporate-warehouse", ids)
        self.assertEqual(len(self.domain(taxonomy, "inmon-dwh")["files"]),
                         len(self.domain(self.old, "inmon-corporate-warehouse")["files"]))


class MergeInvariantsTest(MergeBase):
    def test_unaffected_edit_fails(self):
        # правка незатронутого домена: paradigms не совпадают с файлами, пересчёт их изменил бы
        old = read_json(self.tmp / "taxonomy.json")
        self.domain(old, "mpp-greenplum")["paradigms"] = ["Ручная правка"]
        write_json(self.tmp / "taxonomy.json", old)
        before = self.snapshot()
        rc, err = run_merge(self.tmp)
        self.assertEqual(rc, 1, err)
        self.assertIn("Незатронутый домен mpp-greenplum изменился", err)
        self.assertNothingWritten(before)

    def test_unaffected_check_function_detects_change(self):
        new = json.loads(json.dumps(self.old))
        self.domain(new, "mpp-greenplum")["description"] = "Другое описание"
        errors = st.check_unaffected_unchanged(new, self.old, {}, set(AFFECTED), {})
        self.assertEqual(len(errors), 1)
        self.assertIn("mpp-greenplum", errors[0])
        # производные поля правкой не считаются
        derived = json.loads(json.dumps(self.old))
        dom = self.domain(derived, "mpp-greenplum")
        dom["recommended_topics"] = []
        dom["topics"][0]["related_domain_topics"] = ["x:y"]
        self.assertEqual(st.check_unaffected_unchanged(derived, self.old, {}, set(AFFECTED), {}), [])

    def test_unaffected_layout_change_fails(self):
        # файл появился в незатронутом домене в domain-map.json, а в affected-domains.json его нет
        domain_map = read_json(self.tmp / "domain-map.json")
        moved = None
        for d in domain_map["domains"]:
            if d["id"] == "data-vault":
                moved = d["topics"][0]["files"].pop()
        for d in domain_map["domains"]:
            if d["id"] == "mpp-greenplum":
                d["topics"][0]["files"].append(moved)
        write_json(self.tmp / "domain-map.json", domain_map)
        touch(self.tmp / "domain-map.json", SCAN_TIME)
        before = self.snapshot()
        rc, err = run_merge(self.tmp)
        self.assertEqual(rc, 1, err)
        self.assertIn("mpp-greenplum", err)
        self.assertNothingWritten(before)

    def test_topic_note_path_outside_domain_fails(self):
        did = "data-vault"
        doc = self.validated[did]
        doc["topics"][0]["note_paths"].append("Нет такого файла в домене.md")
        doc["topics"][0]["has_notes"] = True
        write_json(self.tmp / (did + "-topics.validated.json"), doc)
        before = self.snapshot()
        rc, err = run_merge(self.tmp)
        self.assertEqual(rc, 1, err)
        self.assertIn("согласованность тем", err)
        self.assertIn("note_path не из файлов домена", err)
        self.assertNothingWritten(before)

    def test_topic_has_notes_mismatch_fails(self):
        did = "kimball-dimensional-modeling"
        doc = self.validated[did]
        doc["topics"][0]["has_notes"] = not doc["topics"][0]["has_notes"]
        write_json(self.tmp / (did + "-topics.validated.json"), doc)
        before = self.snapshot()
        rc, err = run_merge(self.tmp)
        self.assertEqual(rc, 1, err)
        self.assertIn("has_notes", err)
        self.assertNothingWritten(before)

    def test_file_in_no_topic_fails(self):
        # Файл может подкреплять несколько тем, но сирот быть не должно.
        did = "postgresql-query-optimization"
        doc = self.validated[did]
        owner = next(t for t in doc["topics"] if t["note_paths"])
        before = self.snapshot()

        owner["note_paths"].pop(0)
        write_json(self.tmp / (did + "-topics.validated.json"), doc)
        rc, err = run_merge(self.tmp)
        self.assertEqual(rc, 1, err)
        self.assertIn("не привязан ни к одной теме", err)
        self.assertNothingWritten(before)

    def test_candidates_accounted_failure_blocks_write(self):
        structure = read_json(self.tmp / "vault-structure-analysis.json")
        structure["metadata"]["new_candidates_count"] += 1
        write_json(self.tmp / "vault-structure-analysis.json", structure)
        before = self.snapshot()
        rc, err = run_merge(self.tmp)
        self.assertEqual(rc, 1, err)
        self.assertIn("candidates_accounted", err)
        self.assertNothingWritten(before)

    def test_unknown_new_path_fails_files_exist(self):
        domain_map = read_json(self.tmp / "domain-map.json")
        for d in domain_map["domains"]:
            if d["id"] == "data-vault":
                d["topics"][0]["files"][0] += " (опечатка)"
        write_json(self.tmp / "domain-map.json", domain_map)
        touch(self.tmp / "domain-map.json", SCAN_TIME)
        before = self.snapshot()
        rc, err = run_merge(self.tmp)
        self.assertEqual(rc, 1, err)
        self.assertIn("(опечатка)", err)
        self.assertNothingWritten(before)

    def test_stale_graph_blocks_merge(self):
        touch(self.tmp / "topic-graph.json", SCAN_TIME - 10)
        before = self.snapshot()
        rc, err = run_merge(self.tmp)
        self.assertEqual(rc, 2, err)
        self.assertIn("topic-graph.json старше", err)
        self.assertNothingWritten(before)

    def test_missing_graph_blocks_merge(self):
        (self.tmp / "topic-graph.json").unlink()
        before = self.snapshot()
        rc, err = run_merge(self.tmp)
        self.assertEqual(rc, 2, err)
        self.assertIn("Нет входа topic-graph.json", err)
        self.assertNothingWritten(before)

    def test_missing_validated_topics_fails(self):
        (self.tmp / "data-vault-topics.validated.json").unlink()
        before = self.snapshot()
        rc, err = run_merge(self.tmp)
        self.assertEqual(rc, 1, err)
        self.assertIn("data-vault-topics.validated.json", err)
        self.assertNothingWritten(before)


class MergeSideEffectsTest(MergeBase):
    def test_backup_created_with_previous_content(self):
        before = file_bytes(self.tmp / "taxonomy.json")
        rc, err = run_merge(self.tmp)
        self.assertEqual(rc, 0, err)
        self.assertEqual(file_bytes(self.tmp / "taxonomy.before.json"), before)
        self.assertNotEqual(file_bytes(self.tmp / "taxonomy.json"), before)

    def test_dry_run_writes_nothing(self):
        before = self.snapshot()
        rc, err = run_merge(self.tmp, "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("[DRY-RUN]", err)
        self.assertNothingWritten(before)

    def test_dry_run_reports_violation(self):
        did = "data-vault"
        doc = self.validated[did]
        doc["topics"][0]["has_notes"] = not doc["topics"][0]["has_notes"]
        write_json(self.tmp / (did + "-topics.validated.json"), doc)
        before = self.snapshot()
        rc, err = run_merge(self.tmp, "--dry-run")
        self.assertEqual(rc, 1, err)
        self.assertNothingWritten(before)

    def test_cache_updated_after_success_with_merge_map(self):
        cache = read_json(self.tmp / "semantic-analysis-cache.json")
        cache["stale-hash-not-in-scan"] = {"domain_id": "old-domain", "essence": "x", "key_concepts": []}
        write_json(self.tmp / "semantic-analysis-cache.json", cache)
        write_json(self.tmp / "domain-merge-map.json", {"old-domain": "data-vault"})
        rc, err = run_merge(self.tmp)
        self.assertEqual(rc, 0, err)
        cache = read_json(self.tmp / "semantic-analysis-cache.json")
        self.assertEqual(cache["stale-hash-not-in-scan"]["domain_id"], "data-vault")
        scan = read_json(self.tmp / "vault-scan.json")
        hash_by_path = {e["path"]: e["content_hash"] for e in scan["files"]}
        taxonomy = read_json(self.tmp / "taxonomy.json")
        dom = self.domain(taxonomy, "data-vault")
        for f in dom["files"]:
            entry = cache[hash_by_path[f["path"]]]
            self.assertEqual(entry["domain_id"], "data-vault")
            self.assertEqual(entry["essence"], f.get("essence", ""))
            self.assertEqual(entry["key_concepts"], f.get("key_concepts", []))

    def test_cache_not_written_on_failure(self):
        cache_before = file_bytes(self.tmp / "semantic-analysis-cache.json")
        doc = self.validated["data-vault"]
        doc["topics"][0]["note_paths"].append("Нет такого файла в домене.md")
        write_json(self.tmp / "data-vault-topics.validated.json", doc)
        rc, err = run_merge(self.tmp)
        self.assertEqual(rc, 1, err)
        self.assertEqual(file_bytes(self.tmp / "semantic-analysis-cache.json"), cache_before)

    def test_collect_script_no_longer_writes_cache(self):
        source = (SCRIPTS_DIR / "collect_vault_structure.py").read_text(encoding="utf-8")
        self.assertNotIn("save_json(temp_dir / CACHE_FILENAME", source)


class FullModeCacheTest(unittest.TestCase):
    """Полный (не инкрементальный) режим тоже обновляет кеш в конце, после записи taxonomy.json."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="merge-full-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copytree(FIXTURES_DIR, self.tmp, dirs_exist_ok=True)
        (self.tmp / "taxonomy.json").unlink()
        scan = read_json(self.tmp / "vault-scan.json")
        structure = read_json(self.tmp / "vault-structure-analysis.json")
        structure["metadata"]["mode"] = "full"
        structure["all_files"] = [{"path": e["path"]} for e in scan["files"]]
        # покрытие полного прогона: файлы доменов, projects, unclassified и шум из структуры
        domain_files = sum(len(read_json(self.tmp / (d["domain_id"] + "-analysis.json"))["files"])
                           for d in structure["domains"])
        structure["metadata"]["scanned_md_count"] = (domain_files + structure["projects"]["count"]
                                                     + structure["unclassified"]["count"]
                                                     + structure["filtered_out"]["count"])
        self.structure = structure
        write_json(self.tmp / "vault-structure-analysis.json", structure)

    def run_full(self):
        proc = subprocess.run([sys.executable, str(SCRIPT), "--temp-dir", str(self.tmp)],
                              capture_output=True, text=True, encoding="utf-8")
        return proc.returncode, proc.stderr

    def test_full_mode_updates_cache_after_write(self):
        cache_before = read_json(self.tmp / "semantic-analysis-cache.json")
        rc, err = self.run_full()
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.tmp / "taxonomy.json").exists())
        self.assertIn("Кеш обновлён после записи", err)
        cache_after = read_json(self.tmp / "semantic-analysis-cache.json")
        taxonomy = read_json(self.tmp / "taxonomy.json")
        scan = read_json(self.tmp / "vault-scan.json")
        hash_by_path = {e["path"]: e["content_hash"] for e in scan["files"]}
        for dom in taxonomy["domains"]:
            for f in dom["files"]:
                self.assertEqual(cache_after[hash_by_path[f["path"]]]["domain_id"], dom["domain_id"])
        self.assertGreaterEqual(len(cache_after), len(cache_before))

    def test_full_mode_failure_writes_neither_taxonomy_nor_cache(self):
        self.structure["metadata"]["scanned_md_count"] += 1
        write_json(self.tmp / "vault-structure-analysis.json", self.structure)
        cache_before = file_bytes(self.tmp / "semantic-analysis-cache.json")
        rc, err = self.run_full()
        self.assertEqual(rc, 1, err)
        self.assertIn("candidates_accounted", err)
        self.assertFalse((self.tmp / "taxonomy.json").exists())
        self.assertEqual(file_bytes(self.tmp / "semantic-analysis-cache.json"), cache_before)


if __name__ == "__main__":
    unittest.main()
