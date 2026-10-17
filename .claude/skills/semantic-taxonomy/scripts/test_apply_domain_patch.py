#!/usr/bin/env python3
"""
Тесты apply_domain_patch.py на живых данных fixtures/ (unittest, без внешних зависимостей).

Запуск:  python -m unittest test_apply_domain_patch   (из папки scripts)
Данные fixtures копируются во временную папку, исходные файлы не меняются.
"""

import sys
import io
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
# Живые данные прогона (личный vault, в гит не попадают): нет данных - тесты пропускаются.
FIXTURES_DIR = SCRIPTS_DIR.parents[3] / ".claude" / "temp_files" / "_fixtures"
sys.path.insert(0, str(SCRIPTS_DIR))

if not (FIXTURES_DIR / "taxonomy.json").exists():
    raise unittest.SkipTest("нет .claude/temp_files/taxonomy.json")

import apply_domain_patch as adp  # noqa: E402
import build_domain_cards as bdc  # noqa: E402


def load(name):
    with open(FIXTURES_DIR / name, "r", encoding="utf-8") as f:
        return json.load(f)


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def make_cards():
    taxonomy = load("taxonomy.json")
    return {
        "domains": [bdc.build_card(d) for d in taxonomy["domains"]],
        "unclassified": bdc.section_items(taxonomy.get("unclassified")),
        "projects": bdc.section_items(taxonomy.get("projects")),
    }


def op(name, args, rationale="П1: тест", confidence="high"):
    return {"op": name, "args": args, "rationale": rationale, "confidence": confidence}


def patch_of(*ops):
    return {"generated": "2026-10-17", "operations": list(ops)}


# Файл из темы с двумя файлами: после вытаскивания тема остаётся непустой.
DELTA_TOPIC = "Подход Кимбалла и многомерное моделирование"
KIMBALL = "kimball-dimensional-modeling"


class PatchTestBase(unittest.TestCase):
    def setUp(self):
        self.map = load("domain-map.json")
        self.scan = load("vault-scan.json")
        self.cards = make_cards()

    def make_delta_file(self):
        """Сделать из файла темы с двумя файлами новый, ещё не учтённый: вернуть его путь."""
        dom = next(d for d in self.map["domains"] if d["id"] == KIMBALL)
        topic = next(t for t in dom["topics"] if t["name"] == DELTA_TOPIC)
        path = topic["files"].pop()
        for f in self.scan["files"]:
            if f["path"] == path:
                f["status"] = adp.STATUS_NEW
        return path

    def run_patch(self, *ops):
        return adp.apply_patch(self.map, patch_of(*ops), self.scan, self.cards)

    def ok(self, *ops):
        state, errors = self.run_patch(*ops)
        self.assertEqual(errors, [])
        return state

    def fails(self, fragment, *ops):
        state, errors = self.run_patch(*ops)
        self.assertTrue(errors, "ожидалась ошибка валидации")
        self.assertTrue(any(fragment in e for e in errors), "нет '" + fragment + "' в: " + str(errors))

    @staticmethod
    def dom_of(state, dom_id):
        return next(d for d in state.domains if d["id"] == dom_id)

    @staticmethod
    def all_files(state):
        paths = []
        for d in state.domains:
            for t in d["topics"]:
                paths.extend(t["files"])
        for p in state.projects:
            paths.extend(p["files"])
        paths.extend(u["path"] for u in state.unclassified)
        return paths


class TestOperations(PatchTestBase):
    def test_add_file(self):
        path = self.make_delta_file()
        state = self.ok(op("add_file", {"path": path, "domain_id": KIMBALL, "topic": "Таблицы фактов"}))
        topic = next(t for t in self.dom_of(state, KIMBALL)["topics"] if t["name"] == "Таблицы фактов")
        self.assertIn(path, topic["files"])
        self.assertEqual(state.affected[KIMBALL], {"new_files"})
        self.assertEqual(self.all_files(state).count(path), 1)

    def test_create_topic(self):
        path = self.make_delta_file()
        state = self.ok(op("create_topic", {"domain_id": KIMBALL, "name": "Новая тема",
                                            "essence": "о новом", "files": [path]}))
        topic = next(t for t in self.dom_of(state, KIMBALL)["topics"] if t["name"] == "Новая тема")
        self.assertEqual(topic["files"], [path])
        self.assertIn("new_files", state.affected[KIMBALL])

    def test_create_domain(self):
        path = self.make_delta_file()
        state = self.ok(op("create_domain", {
            "id": "new-domain", "name": "Новый домен", "purpose": "для теста", "vocabulary": ["а"],
            "topics": [{"name": "Тема", "essence": "сущность", "files": [path]}]}))
        dom = self.dom_of(state, "new-domain")
        self.assertEqual(dom["decision"], "created")
        self.assertIn("new_files", state.affected["new-domain"])

    def test_rename_domain(self):
        state = self.ok(op("rename_domain", {"domain_id": "data-vault", "new_id": "data-vault-2",
                                             "new_name": "Data Vault 2.0 (метод)"}))
        self.assertEqual(state.merge_map, {"data-vault": "data-vault-2"})
        dom = self.dom_of(state, "data-vault-2")
        self.assertEqual(dom["decision"], "renamed")
        self.assertEqual(state.affected, {})
        # Состояние тем переезжает вместе с темами по topic_id
        carried = adp.build_affected(state)["topic_state"]
        sources = [s for e in carried if e["domain_id"] == "data-vault-2"
                   for t in e["topics"] for s in t["state_from"]]
        self.assertIn("dv-architecture", [s["topic_id"] for s in sources])

    def test_move_topic_carries_state(self):
        state = self.ok(op("move_topic", {"from_domain": "data-vault", "topic": "Архитектура Data Vault 2.0",
                                          "to_domain": KIMBALL}))
        self.assertTrue(any(t["name"] == "Архитектура Data Vault 2.0" for t in self.dom_of(state, KIMBALL)["topics"]))
        self.assertEqual(state.affected, {})
        carried = adp.build_affected(state)["topic_state"]
        moved = next(t for e in carried if e["domain_id"] == KIMBALL
                     for t in e["topics"] if t["topic_name"] == "Архитектура Data Vault 2.0")
        self.assertEqual(moved["state_from"][0]["topic_id"], "dv-architecture")
        self.assertEqual(moved["state_from"][0]["mastery_percent"], 40)

    def test_merge_domains(self):
        state = self.ok(op("merge_domains", {"sources": ["dbt-automation", "synthetic-data"],
                                             "target_id": "dbt-automation", "name": "Инструменты"}))
        dom = self.dom_of(state, "dbt-automation")
        self.assertEqual(dom["decision"], "merged")
        self.assertEqual(dom["merged_from"], ["synthetic-data"])
        self.assertEqual(len(dom["topics"]), 3)
        self.assertFalse(any(d["id"] == "synthetic-data" for d in state.domains))
        self.assertEqual(state.merge_map, {"synthetic-data": "dbt-automation"})
        self.assertEqual(state.affected["dbt-automation"], {"merge"})

    def test_merge_domains_same_topic_name_glued(self):
        kimball = next(d for d in self.map["domains"] if d["id"] == KIMBALL)
        vault = next(d for d in self.map["domains"] if d["id"] == "data-vault")
        vault["topics"][0]["name"] = kimball["topics"][4]["name"]  # "Таблицы фактов"
        merged_files = vault["topics"][0]["files"] + kimball["topics"][4]["files"]
        state = self.ok(op("merge_domains", {"sources": [KIMBALL, "data-vault"],
                                             "target_id": "dwh-methods", "name": "Методы DWH"}))
        dom = self.dom_of(state, "dwh-methods")
        glued = [t for t in dom["topics"] if t["name"] == "Таблицы фактов"]
        self.assertEqual(len(glued), 1)
        self.assertEqual(sorted(glued[0]["files"]), sorted(merged_files))
        self.assertEqual(len(dom["topics"]), 8 + 8 - 1)
        self.assertEqual(state.merge_map, {KIMBALL: "dwh-methods", "data-vault": "dwh-methods"})

    def test_split_domain(self):
        names = [t["name"] for t in next(d for d in self.map["domains"] if d["id"] == "data-vault")["topics"]]
        state = self.ok(op("split_domain", {"domain_id": "data-vault", "parts": [
            {"id": "data-vault-core", "name": "DV: ядро", "topics": names[:4]},
            {"id": "data-vault-ops", "name": "DV: эксплуатация", "topics": names[4:]}]}))
        self.assertFalse(any(d["id"] == "data-vault" for d in state.domains))
        core = self.dom_of(state, "data-vault-core")
        self.assertEqual(core["split_from"], "data-vault")
        self.assertEqual([t["name"] for t in core["topics"]], names[:4])
        self.assertEqual(state.affected["data-vault-core"], {"split"})
        self.assertEqual(state.affected["data-vault-ops"], {"split"})

    def test_classify_from_unclassified(self):
        state = self.ok(op("classify", {"path": "Bronze Layer.md", "domain_id": "dbt-automation",
                                        "topic": "Слой Bronze", "essence": "Сырой слой"}))
        self.assertNotIn("Bronze Layer.md", [u["path"] for u in state.unclassified])
        topic = next(t for t in self.dom_of(state, "dbt-automation")["topics"] if t["name"] == "Слой Bronze")
        self.assertEqual(topic["files"], ["Bronze Layer.md"])
        self.assertEqual(state.affected["dbt-automation"], {"classify"})

    def test_classify_from_project(self):
        state = self.ok(op("classify", {"path": "Git.md", "domain_id": "dbt-automation",
                                        "topic": "dbt как инструмент"}))
        project = next(p for p in state.projects if p["name"] == "Olist DWH")
        self.assertNotIn("Git.md", project["files"])

    def test_to_project(self):
        state = self.ok(op("to_project", {"path": "Сбор требований.md", "project": "Olist DWH"}))
        project = next(p for p in state.projects if p["name"] == "Olist DWH")
        self.assertIn("Сбор требований.md", project["files"])
        self.assertNotIn("Сбор требований.md", [u["path"] for u in state.unclassified])

    def test_keep_unclassified_updates_entry(self):
        state = self.ok(op("keep_unclassified", {"path": "Bronze Layer.md", "reason": "новая причина",
                                                 "suggestion": "новое предложение"}))
        entry = next(u for u in state.unclassified if u["path"] == "Bronze Layer.md")
        self.assertEqual(entry["reason"], "новая причина")
        self.assertEqual(entry["suggestion"], "новое предложение")

    def test_untouched_entries_stay_identical(self):
        state = self.ok(op("keep_unclassified", {"path": "Bronze Layer.md", "reason": "р", "suggestion": "с"}))
        out = adp.build_map_output(state)
        before = {d["id"]: d for d in self.map["domains"]}
        for dom in out["domains"]:
            self.assertEqual(dom, before[dom["id"]])
        others_before = [u for u in self.map["unclassified"] if u["path"] != "Bronze Layer.md"]
        others_after = [u for u in out["unclassified"] if u["path"] != "Bronze Layer.md"]
        self.assertEqual(others_before, others_after)
        self.assertEqual(out["projects"], self.map["projects"])


class TestValidation(PatchTestBase):
    def test_file_in_two_places(self):
        self.map["unclassified"].append({"path": "Data Vault 2.0.md", "reason": "р", "suggestion": "с"})
        self.fails("файл в нескольких местах: Data Vault 2.0.md")

    def test_empty_topic(self):
        # Тема "Хабы" состоит из одного файла: перенос лишает её единственной заметки
        dom = next(d for d in self.map["domains"] if d["id"] == "data-vault")
        path = next(t for t in dom["topics"] if t["name"] == "Хабы")["files"][0]
        self.fails("пустая тема: data-vault / Хабы",
                   op("add_file", {"path": path, "domain_id": "data-vault", "topic": "Ссылки"}))

    def test_unknown_op(self):
        self.fails("неизвестный op", op("explode_domain", {}))

    def test_unknown_domain_and_topic(self):
        self.fails("неизвестный домен: nope",
                   op("move_topic", {"from_domain": "nope", "topic": "x", "to_domain": KIMBALL}))
        self.fails("нет темы: Нет такой",
                   op("move_topic", {"from_domain": "data-vault", "topic": "Нет такой", "to_domain": KIMBALL}))

    def test_path_missing_in_scan(self):
        self.fails("путь отсутствует в vault-scan.json",
                   op("keep_unclassified", {"path": "Призрак.md", "reason": "р", "suggestion": "с"}))

    def test_delta_file_left_unplaced(self):
        path = self.make_delta_file()
        self.fails("файл дельты (new) нигде не учтён: " + path)

    def test_missing_rationale_and_confidence(self):
        bad = {"op": "keep_unclassified", "args": {"path": "Bronze Layer.md", "reason": "р", "suggestion": "с"}}
        self.fails("нет rationale", bad)
        bad["rationale"] = "П1"
        bad["confidence"] = "maybe"
        self.fails("confidence", bad)

    def test_split_must_cover_all_topics(self):
        names = [t["name"] for t in next(d for d in self.map["domains"] if d["id"] == "data-vault")["topics"]]
        self.fails("темы не распределены по частям", op("split_domain", {"domain_id": "data-vault", "parts": [
            {"id": "a", "name": "А", "topics": names[:3]}, {"id": "b", "name": "Б", "topics": names[3:6]}]}))

    def test_classify_requires_off_domain_file(self):
        self.fails("только из unclassified или projects",
                   op("classify", {"path": "Data Vault 2.0.md", "domain_id": KIMBALL,
                                   "topic": "Таблицы фактов"}))


class TestCommandLine(PatchTestBase):
    def write_inputs(self, tmp, patch):
        for name, data in (("domain-map.json", self.map), ("vault-scan.json", self.scan),
                           ("domain-cards.json", self.cards), ("patch.json", patch)):
            with open(Path(tmp) / name, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)

    def run_cli(self, tmp, *extra):
        return subprocess.run([sys.executable, str(SCRIPTS_DIR / "apply_domain_patch.py"),
                               "--temp-dir", tmp] + list(extra),
                              capture_output=True, text=True, encoding="utf-8")

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_inputs(tmp, patch_of(op("rename_domain", {"domain_id": "data-vault", "new_id": "dv",
                                                                 "new_name": "DV"})))
            result = self.run_cli(tmp, "--dry-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((Path(tmp) / "domain-map.before.json").exists())
            self.assertFalse((Path(tmp) / "domain-merge-map.json").exists())
            self.assertEqual(read_json(Path(tmp) / "domain-map.json"), self.map)

    def test_apply_writes_all_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_inputs(tmp, patch_of(
                op("merge_domains", {"sources": ["dbt-automation", "synthetic-data"],
                                     "target_id": "dbt-automation", "name": "Инструменты"}),
                op("classify", {"path": "Bronze Layer.md", "domain_id": "dbt-automation",
                                "topic": "Слой Bronze", "essence": "Сырой слой"})))
            result = self.run_cli(tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            before = read_json(Path(tmp) / "domain-map.before.json")
            self.assertEqual(before, self.map)
            merge_map = read_json(Path(tmp) / "domain-merge-map.json")
            self.assertEqual(merge_map, {"synthetic-data": "dbt-automation"})
            affected = read_json(Path(tmp) / "affected-domains.json")
            # Затронуты домен из патча и домены с файлами дельты, уже стоящими в карте.
            by_id = {d["domain_id"]: d for d in affected["domains"]}
            status = {f["path"]: f.get("status") for f in self.scan["files"]}
            self.assertTrue({"classify", "merge"} <= set(by_id["dbt-automation"]["reasons"]))
            for dom in self.map["domains"]:
                has_delta = any(status.get(p) in ("new", "changed")
                                for t in dom["topics"] for p in t["files"])
                if dom["id"] not in ("dbt-automation", "synthetic-data"):
                    self.assertEqual(dom["id"] in by_id, has_delta, dom["id"])
            new_map = read_json(Path(tmp) / "domain-map.json")
            self.assertFalse(any("_touched" in d or "_src" in str(d) for d in new_map["domains"]))

    def test_failure_exit_code_and_no_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_inputs(tmp, patch_of(op("explode_domain", {})))
            result = self.run_cli(tmp)
            self.assertEqual(result.returncode, 1)
            self.assertIn("неизвестный op", result.stderr)
            self.assertFalse((Path(tmp) / "domain-map.before.json").exists())
            self.assertEqual(read_json(Path(tmp) / "domain-map.json"), self.map)


if __name__ == "__main__":
    unittest.main()
