#!/usr/bin/env python3
"""
Детерминированное применение patch.json к domain-map.json (Фаза 2, режим инкремента).

Вход (в --temp-dir):
  patch.json          - операции архитектора (vault-domain-architect, "Режим инкремента")
  domain-map.json     - текущая карта доменов
  vault-scan.json     - пути vault и status файлов (new | changed | unchanged)
  domain-cards.json   - карточки из taxonomy.json (источник состояния тем; необязателен)

Выход:
  domain-map.json         - правится на месте, перед записью копия domain-map.before.json
  domain-merge-map.json   - старый id домена -> новый id (rename_domain и merge_domains)
  affected-domains.json   - домены для Фазы 3б (merge, split, classify, новые файлы)
                            и перенос состояния тем (topic_id из taxonomy.json)

Операции: add_file, create_topic, create_domain, rename_domain, move_topic,
merge_domains, split_domain, classify, to_project, keep_unclassified.
Любая ошибка валидации - код выхода 1, ничего не записывается.
Записи доменов, projects и unclassified вне патча не меняются.

Использование:
  python apply_domain_patch.py [--temp-dir DIR] [--patch FILE] [--dry-run]
"""

import sys
import io
import json
import copy
import shutil
import argparse
from pathlib import Path
from datetime import datetime

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

PATCH_FILENAME = "patch.json"
DOMAIN_MAP_FILENAME = "domain-map.json"
DOMAIN_MAP_BEFORE_FILENAME = "domain-map.before.json"
MERGE_MAP_FILENAME = "domain-merge-map.json"
AFFECTED_FILENAME = "affected-domains.json"
SCAN_FILENAME = "vault-scan.json"
CARDS_FILENAME = "domain-cards.json"

STATUS_NEW = "new"
STATUS_CHANGED = "changed"
DELTA_STATUSES = (STATUS_NEW, STATUS_CHANGED)

KNOWN_OPS = (
    "add_file", "create_topic", "create_domain", "rename_domain", "move_topic",
    "merge_domains", "split_domain", "classify", "to_project", "keep_unclassified",
)
CONFIDENCES = ("high", "medium", "low")

# Куда сейчас положен файл
LOC_TOPIC = "topic"
LOC_PROJECT = "project"
LOC_UNCLASSIFIED = "unclassified"


def get_project_root():
    """Определить корень проекта по расположению скрипта."""
    return Path(__file__).resolve().parents[4]


class OpError(Exception):
    """Ошибка одной операции патча (до изменения состояния)."""


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def norm_name(name):
    """Ключ сравнения названий тем при слиянии: без регистра и краевых пробелов."""
    return " ".join(str(name).split()).casefold()


def dedupe(items):
    seen = set()
    out = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


class PatchState:
    """Состояние карты в памяти. Служебные поля с префиксом "_" не попадают в файл."""

    def __init__(self, domain_map, scan, cards):
        self.domain_map = domain_map
        self.domains = copy.deepcopy(domain_map.get("domains", []))
        self.projects = copy.deepcopy(domain_map.get("projects", []))
        self.unclassified = copy.deepcopy(domain_map.get("unclassified", []))
        self.originals = {d["id"]: json.dumps(d, ensure_ascii=False, sort_keys=True)
                          for d in domain_map.get("domains", [])}
        self.scan_status = {f["path"]: f.get("status") for f in scan.get("files", [])}
        self.merge_map = {}
        self.affected = {}
        self.needs_analysis = set()
        self.warnings = []
        self.attach_sources(cards)

    # ---------- служебное ----------

    def attach_sources(self, cards):
        """Привязать к каждой теме карты тему taxonomy.json (по пересечению файлов, затем по названию)."""
        card_domains = {c["domain_id"]: c for c in (cards or {}).get("domains", [])}
        for dom in self.domains:
            dom["_touched"] = False
            card = card_domains.get(dom["id"])
            for topic in dom.get("topics", []):
                topic["_src"] = []
                if not card:
                    continue
                files = set(topic.get("files", []))
                best = None
                best_overlap = 0
                for ct in card.get("topics", []):
                    overlap = len(files & set(ct.get("note_paths") or []))
                    if overlap > best_overlap:
                        best, best_overlap = ct, overlap
                if best is None:
                    for ct in card.get("topics", []):
                        if norm_name(ct.get("topic_name", "")) == norm_name(topic.get("name", "")):
                            best = ct
                            break
                if best is not None:
                    topic["_src"].append({
                        "domain_id": card["domain_id"],
                        "topic_id": best.get("topic_id"),
                        "has_notes": best.get("has_notes"),
                        "mastery_percent": best.get("mastery_percent"),
                    })

    def domain(self, domain_id):
        for d in self.domains:
            if d["id"] == domain_id:
                return d
        raise OpError("неизвестный домен: " + str(domain_id))

    def has_domain(self, domain_id):
        return any(d["id"] == domain_id for d in self.domains)

    def topic(self, dom, name):
        for t in dom.get("topics", []):
            if t["name"] == name:
                return t
        raise OpError("в домене " + dom["id"] + " нет темы: " + str(name))

    def mark(self, dom_id, reason=None):
        """Домен затронут патчем (перенос состояния тем); reason - ещё и затронут для Фазы 3б."""
        self.domain(dom_id)["_touched"] = True
        if reason:
            self.affected.setdefault(dom_id, set()).add(reason)

    def remap_affected(self, mapping):
        """При смене id домена перенести пометки: mapping - старый id -> список новых."""
        out = {}
        for key, reasons in self.affected.items():
            for new_key in mapping.get(key, [key]):
                out.setdefault(new_key, set()).update(reasons)
        self.affected = out

    def locate(self, path):
        for dom in self.domains:
            for topic in dom.get("topics", []):
                if path in topic.get("files", []):
                    return (LOC_TOPIC, dom, topic)
        for proj in self.projects:
            if path in proj.get("files", []):
                return (LOC_PROJECT, proj, None)
        for entry in self.unclassified:
            if entry.get("path") == path:
                return (LOC_UNCLASSIFIED, entry, None)
        return None

    def detach(self, path):
        """Вынуть файл из текущего места (если он где-то учтён). Вернуть прежнее место."""
        loc = self.locate(path)
        if loc is None:
            return None
        kind, holder, topic = loc
        if kind == LOC_TOPIC:
            topic["files"] = [p for p in topic["files"] if p != path]
            holder["_touched"] = True
            self.affected.setdefault(holder["id"], set()).add("file_moved_out")
        elif kind == LOC_PROJECT:
            holder["files"] = [p for p in holder["files"] if p != path]
            if not holder["files"]:
                self.projects = [p for p in self.projects if p is not holder]
                self.warnings.append("проект без файлов удалён: " + str(holder.get("name")))
        else:
            self.unclassified = [u for u in self.unclassified if u is not holder]
        return kind

    def file_reason(self, path):
        return "new_files" if self.scan_status.get(path) in DELTA_STATUSES else "file_moved"


# ---------- проверка аргументов ----------

def need_str(args, key):
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OpError("в args нет непустой строки '" + key + "'")
    return value


def need_list(args, key, min_len=1):
    value = args.get(key)
    if not isinstance(value, list) or len(value) < min_len:
        raise OpError("в args нет списка '" + key + "' (минимум " + str(min_len) + " элементов)")
    return value


def need_scan_paths(state, paths):
    for p in paths:
        if not isinstance(p, str) or p not in state.scan_status:
            raise OpError("путь отсутствует в vault-scan.json: " + str(p))
    if len(set(paths)) != len(paths):
        raise OpError("путь повторяется в одной операции")


def need_not_off_domain(state, paths, op):
    """add_file и create_topic не берут файлы из unclassified и projects: для них classify."""
    for p in paths:
        loc = state.locate(p)
        if loc is not None and loc[0] in (LOC_PROJECT, LOC_UNCLASSIFIED):
            raise OpError("файл " + p + " учтён в " + loc[0] + ": для него нужна операция classify (не " + op + ")")


# ---------- операции ----------

def op_add_file(state, args):
    path = need_str(args, "path")
    dom_id = need_str(args, "domain_id")
    topic_name = need_str(args, "topic")
    need_scan_paths(state, [path])
    need_not_off_domain(state, [path], "add_file")
    dom = state.domain(dom_id)
    topic = state.topic(dom, topic_name)
    if path in topic["files"]:
        raise OpError("файл " + path + " уже в теме " + topic_name)
    reason = state.file_reason(path)
    state.detach(path)
    topic["files"].append(path)
    state.mark(dom_id, reason)


def op_create_topic(state, args):
    dom_id = need_str(args, "domain_id")
    name = need_str(args, "name")
    essence = need_str(args, "essence")
    files = need_list(args, "files")
    need_scan_paths(state, files)
    need_not_off_domain(state, files, "create_topic")
    dom = state.domain(dom_id)
    if any(t["name"] == name for t in dom.get("topics", [])):
        raise OpError("тема " + name + " уже есть в домене " + dom_id)
    reasons = set(state.file_reason(p) for p in files)
    for p in files:
        state.detach(p)
    dom.setdefault("topics", []).append({"name": name, "files": list(files), "essence": essence, "_src": []})
    for reason in reasons:
        state.mark(dom_id, reason)


def op_create_domain(state, args):
    dom_id = need_str(args, "id")
    name = need_str(args, "name")
    purpose = need_str(args, "purpose")
    vocabulary = args.get("vocabulary", [])
    topics = need_list(args, "topics")
    if state.has_domain(dom_id):
        raise OpError("домен " + dom_id + " уже существует")
    all_files = []
    for t in topics:
        if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not t["name"].strip():
            raise OpError("у темы нового домена нет name")
        if not isinstance(t.get("files"), list) or not t["files"]:
            raise OpError("тема " + str(t.get("name")) + " нового домена пуста")
        if not isinstance(t.get("essence"), str) or not t["essence"].strip():
            raise OpError("у темы " + t["name"] + " нет essence")
        all_files.extend(t["files"])
    if len(set(t["name"] for t in topics)) != len(topics):
        raise OpError("названия тем нового домена повторяются")
    need_scan_paths(state, all_files)
    need_not_off_domain(state, all_files, "create_domain")
    reasons = set(state.file_reason(p) for p in all_files)
    for p in all_files:
        state.detach(p)
    state.domains.append({
        "id": dom_id,
        "name": name,
        "purpose": purpose,
        "rationale": args.get("rationale", ""),
        "decision": "created",
        "merged_from": [],
        "split_from": "",
        "vocabulary": list(vocabulary),
        "topics": [{"name": t["name"], "files": list(t["files"]), "essence": t["essence"], "_src": []}
                   for t in topics],
        "_touched": True,
    })
    for reason in reasons:
        state.mark(dom_id, reason)


def op_rename_domain(state, args):
    dom_id = need_str(args, "domain_id")
    new_name = need_str(args, "new_name")
    new_id = args.get("new_id") or dom_id
    dom = state.domain(dom_id)
    if new_id != dom_id and state.has_domain(new_id):
        raise OpError("домен " + new_id + " уже существует")
    dom["name"] = new_name
    if args.get("purpose"):
        dom["purpose"] = args["purpose"]
    dom["decision"] = "renamed"
    dom["_touched"] = True
    if new_id != dom_id:
        dom["id"] = new_id
        state.merge_map[dom_id] = new_id
        state.remap_affected({dom_id: [new_id]})


def op_move_topic(state, args):
    from_id = need_str(args, "from_domain")
    to_id = need_str(args, "to_domain")
    topic_name = need_str(args, "topic")
    if from_id == to_id:
        raise OpError("from_domain и to_domain совпадают")
    src = state.domain(from_id)
    dst = state.domain(to_id)
    topic = state.topic(src, topic_name)
    new_name = args.get("new_name") or topic_name
    if any(t["name"] == new_name for t in dst.get("topics", [])):
        raise OpError("в домене " + to_id + " уже есть тема " + new_name + " (для склейки нужен merge_domains)")
    src["topics"] = [t for t in src["topics"] if t is not topic]
    topic["name"] = new_name
    dst.setdefault("topics", []).append(topic)
    state.mark(from_id, "move_topic")
    state.mark(to_id, "move_topic")


def merge_topic_into(topics, topic):
    """Добавить тему в список: одноимённая склеивается (файлы объединяются, essence первой)."""
    for existing in topics:
        if norm_name(existing["name"]) == norm_name(topic["name"]):
            existing["files"] = dedupe(existing["files"] + topic["files"])
            existing["_src"] = existing.get("_src", []) + topic.get("_src", [])
            return
    topics.append(topic)


def op_merge_domains(state, args):
    sources = need_list(args, "sources", 2)
    target_id = need_str(args, "target_id")
    name = need_str(args, "name")
    if len(set(sources)) != len(sources):
        raise OpError("sources повторяются")
    doms = [state.domain(s) for s in sources]
    if target_id not in sources and state.has_domain(target_id):
        raise OpError("target_id " + target_id + " существует и не входит в sources")
    ordered = sorted(doms, key=lambda d: 0 if d["id"] == target_id else 1)
    base = copy.deepcopy(ordered[0])
    merged_topics = []
    merged_from = list(base.get("merged_from", []))
    vocabulary = list(base.get("vocabulary", []))
    for d in ordered:
        for t in d.get("topics", []):
            merge_topic_into(merged_topics, copy.deepcopy(t))
        if d is not ordered[0]:
            vocabulary = dedupe(vocabulary + list(d.get("vocabulary", [])))
            merged_from = dedupe(merged_from + list(d.get("merged_from", [])))
        if d["id"] != target_id:
            merged_from = dedupe(merged_from + [d["id"]])
    base["id"] = target_id
    base["name"] = name
    if args.get("purpose"):
        base["purpose"] = args["purpose"]
    base["decision"] = "merged"
    base["merged_from"] = merged_from
    base["vocabulary"] = vocabulary
    base["topics"] = merged_topics
    base["_touched"] = True
    first_pos = min(state.domains.index(d) for d in doms)
    state.domains = [d for d in state.domains if d not in doms]
    state.domains.insert(min(first_pos, len(state.domains)), base)
    for s in sources:
        if s != target_id:
            state.merge_map[s] = target_id
    state.remap_affected({s: [target_id] for s in sources})
    state.mark(target_id, "merge")


def op_split_domain(state, args):
    dom_id = need_str(args, "domain_id")
    parts = need_list(args, "parts", 2)
    dom = state.domain(dom_id)
    part_ids = []
    assigned = []
    for part in parts:
        if not isinstance(part, dict):
            raise OpError("часть split_domain не объект")
        pid = need_str(part, "id")
        need_str(part, "name")
        need_list(part, "topics")
        part_ids.append(pid)
        assigned.extend(part["topics"])
    if len(set(part_ids)) != len(part_ids):
        raise OpError("id частей повторяются")
    for pid in part_ids:
        if pid != dom_id and state.has_domain(pid):
            raise OpError("домен " + pid + " уже существует")
    existing_names = [t["name"] for t in dom.get("topics", [])]
    unknown = [n for n in assigned if n not in existing_names]
    if unknown:
        raise OpError("в домене " + dom_id + " нет тем: " + ", ".join(unknown))
    if len(set(assigned)) != len(assigned):
        raise OpError("тема отнесена к нескольким частям")
    missing = [n for n in existing_names if n not in assigned]
    if missing:
        raise OpError("темы не распределены по частям: " + ", ".join(missing))
    topics_by_name = {t["name"]: t for t in dom["topics"]}
    new_domains = []
    for part in parts:
        new_dom = copy.deepcopy(dom)
        new_dom["id"] = part["id"]
        new_dom["name"] = part["name"]
        if part.get("purpose"):
            new_dom["purpose"] = part["purpose"]
        new_dom["vocabulary"] = list(part.get("vocabulary", []))
        new_dom["decision"] = "split"
        new_dom["merged_from"] = []
        new_dom["split_from"] = dom_id
        new_dom["topics"] = [copy.deepcopy(topics_by_name[n]) for n in part["topics"]]
        new_dom["_touched"] = True
        new_domains.append(new_dom)
    pos = state.domains.index(dom)
    state.domains[pos:pos + 1] = new_domains
    # Граф и --merge опираются на merge-map: старый id отображается в часть с наибольшей долей
    # файлов (при равенстве - первая в parts). Файлы остальных частей граф берёт из раскладки.
    # Часть, сохранившая старый id, отображения не требует.
    if dom_id not in part_ids:
        heir = max(new_domains, key=lambda d: sum(len(t["files"]) for t in d["topics"]))
        state.merge_map[dom_id] = heir["id"]
    state.remap_affected({dom_id: part_ids})
    for pid in part_ids:
        state.mark(pid, "split")


def op_classify(state, args):
    path = need_str(args, "path")
    dom_id = need_str(args, "domain_id")
    topic_name = need_str(args, "topic")
    need_scan_paths(state, [path])
    loc = state.locate(path)
    if loc is None or loc[0] not in (LOC_PROJECT, LOC_UNCLASSIFIED):
        raise OpError("classify берёт файл только из unclassified или projects: " + path)
    dom = state.domain(dom_id)
    topic = None
    for t in dom.get("topics", []):
        if t["name"] == topic_name:
            topic = t
    if topic is None and not (isinstance(args.get("essence"), str) and args["essence"].strip()):
        raise OpError("темы " + topic_name + " нет в домене " + dom_id + ": для новой темы нужен essence")
    state.detach(path)
    # У файла из unclassified/projects прежней записи анализа нет (в taxonomy.json у них только
    # path/reason/suggestion): переносить нечего, домену нужен анализ, даже если файл не менялся.
    if state.scan_status.get(path) not in DELTA_STATUSES:
        state.needs_analysis.add(path)
    if topic is None:
        dom.setdefault("topics", []).append(
            {"name": topic_name, "files": [path], "essence": args["essence"], "_src": []})
    else:
        topic["files"].append(path)
    state.mark(dom_id, "classify")


def op_to_project(state, args):
    path = need_str(args, "path")
    project_name = need_str(args, "project")
    need_scan_paths(state, [path])
    loc = state.locate(path)
    if loc is None:
        raise OpError("файл " + path + " нигде не учтён: to_project переносит учтённые файлы")
    target = None
    for p in state.projects:
        if p.get("name") == project_name:
            target = p
    if target is not None and loc[0] == LOC_PROJECT and loc[1] is target:
        # Идемпотентно: файл уже в этом проекте (в скане он new, пока хеши off-domain записей не
        # записаны, и архитектор по самопроверке называет его в операции). Отклонять патч незачем.
        state.warnings.append("файл " + path + " уже в проекте " + project_name + ": без изменений")
        return
    if target is None and not (isinstance(args.get("why"), str) and args["why"].strip()):
        raise OpError("проекта " + project_name + " нет: для нового проекта нужен why")
    state.detach(path)
    if target is None:
        state.projects.append({"name": project_name, "files": [path], "why": args["why"]})
    else:
        target["files"].append(path)


def op_keep_unclassified(state, args):
    path = need_str(args, "path")
    reason = need_str(args, "reason")
    suggestion = need_str(args, "suggestion")
    need_scan_paths(state, [path])
    loc = state.locate(path)
    if loc is None or loc[0] != LOC_UNCLASSIFIED:
        raise OpError("keep_unclassified относится только к записи unclassified: " + path)
    loc[1]["reason"] = reason
    loc[1]["suggestion"] = suggestion


OP_HANDLERS = {
    "add_file": op_add_file,
    "create_topic": op_create_topic,
    "create_domain": op_create_domain,
    "rename_domain": op_rename_domain,
    "move_topic": op_move_topic,
    "merge_domains": op_merge_domains,
    "split_domain": op_split_domain,
    "classify": op_classify,
    "to_project": op_to_project,
    "keep_unclassified": op_keep_unclassified,
}


# ---------- итоговая валидация ----------

def validate_final(state):
    errors = []
    places = {}
    for dom in state.domains:
        if not dom.get("topics"):
            errors.append("домен " + dom["id"] + " без тем")
        for topic in dom.get("topics", []):
            if not topic.get("files"):
                errors.append("пустая тема: " + dom["id"] + " / " + topic["name"])
            for p in topic.get("files", []):
                places.setdefault(p, []).append("домен " + dom["id"] + " / тема " + topic["name"])
    for proj in state.projects:
        for p in proj.get("files", []):
            places.setdefault(p, []).append("проект " + str(proj.get("name")))
    for entry in state.unclassified:
        places.setdefault(entry.get("path"), []).append("unclassified")
    for p, where in sorted(places.items(), key=lambda kv: str(kv[0])):
        if len(where) > 1:
            errors.append("файл в нескольких местах: " + str(p) + " -> " + "; ".join(where))
    ids = [d["id"] for d in state.domains]
    for dom_id in sorted(set(i for i in ids if ids.count(i) > 1)):
        errors.append("домен " + dom_id + " встречается несколько раз")
    for p, status in sorted(state.scan_status.items()):
        if status in DELTA_STATUSES and p not in places:
            errors.append("файл дельты (" + str(status) + ") нигде не учтён: " + p)
    # Неизменность незатронутого: домены вне патча совпадают с прежними побайтово.
    for dom in state.domains:
        if dom["_touched"]:
            continue
        before = state.originals.get(dom["id"])
        if before is None or before != json.dumps(strip_private(dom), ensure_ascii=False, sort_keys=True):
            errors.append("незатронутый домен изменился: " + dom["id"])
    return errors


def strip_private(obj):
    """Копия без служебных полей с префиксом "_"."""
    if isinstance(obj, dict):
        return {k: strip_private(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [strip_private(x) for x in obj]
    return obj


def validate_operation_shape(index, op):
    if not isinstance(op, dict):
        return "операция " + str(index) + ": не объект"
    label = "операция " + str(index) + " (" + str(op.get("op")) + ")"
    if op.get("op") not in KNOWN_OPS:
        return label + ": неизвестный op, допустимы " + ", ".join(KNOWN_OPS)
    if not isinstance(op.get("args"), dict):
        return label + ": args должен быть объектом"
    if not isinstance(op.get("rationale"), str) or not op["rationale"].strip():
        return label + ": нет rationale"
    if op.get("confidence") not in CONFIDENCES:
        return label + ": confidence должен быть один из " + ", ".join(CONFIDENCES)
    return None


def apply_patch(domain_map, patch, scan, cards=None):
    """Применить патч в памяти. Вернуть (state, errors)."""
    state = PatchState(domain_map, scan, cards)
    errors = []
    operations = patch.get("operations")
    if not isinstance(operations, list):
        return state, ["в patch.json нет списка operations"]
    for index, op in enumerate(operations, 1):
        shape_error = validate_operation_shape(index, op)
        if shape_error:
            errors.append(shape_error)
            continue
        try:
            OP_HANDLERS[op["op"]](state, op["args"])
        except OpError as e:
            errors.append("операция " + str(index) + " (" + op["op"] + "): " + str(e))
    if not errors:
        errors.extend(validate_final(state))
    return state, errors


# ---------- выходные данные ----------

def build_map_output(state):
    out = dict(state.domain_map)
    out["domains"] = [strip_private(d) for d in state.domains]
    out["projects"] = strip_private(state.projects)
    out["unclassified"] = strip_private(state.unclassified)
    return out


def compose_merge_map(existing, new):
    """Склеить прежнюю карту слияний с новой: цепочки a->b, b->c сворачиваются в a->c."""
    result = dict(existing or {})
    for old_id, new_id in new.items():
        result[old_id] = new_id
    for key in list(result):
        seen = {key}
        target = result[key]
        while target in result and target not in seen:
            seen.add(target)
            target = result[target]
        if target == key:
            del result[key]
        else:
            result[key] = target
    return result


def build_affected(state):
    domains_out = []
    for dom in state.domains:
        reasons = set(state.affected.get(dom["id"]) or ())
        delta_files = []
        for topic in dom.get("topics", []):
            delta_files.extend(p for p in topic["files"]
                               if state.scan_status.get(p) in DELTA_STATUSES or p in state.needs_analysis)
        # Файлы дельты, уже стоящие в карте (не через операции патча), тоже делают домен затронутым.
        if delta_files:
            reasons.add("new_files")
        if not reasons:
            continue
        domains_out.append({
            "domain_id": dom["id"],
            "reasons": sorted(reasons),
            "new_files": sorted(delta_files),
        })
    topic_state = []
    for dom in state.domains:
        if not dom["_touched"]:
            continue
        topics = []
        for topic in dom.get("topics", []):
            topics.append({"topic_name": topic["name"], "state_from": topic.get("_src", [])})
        topic_state.append({"domain_id": dom["id"], "topics": topics})
    return {
        "generated": datetime.now().strftime("%Y-%m-%d"),
        "domains": domains_out,
        "topic_state": topic_state,
    }


def main():
    parser = argparse.ArgumentParser(description="Применение patch.json к domain-map.json")
    parser.add_argument("--temp-dir", help="директория temp_files (по умолчанию .claude/temp_files проекта)")
    parser.add_argument("--patch", help="путь к patch.json (по умолчанию temp-dir/patch.json)")
    parser.add_argument("--dry-run", action="store_true", help="проверить и показать итог, ничего не записывая")
    args = parser.parse_args()

    temp_dir = Path(args.temp_dir) if args.temp_dir else get_project_root() / ".claude" / "temp_files"
    patch_path = Path(args.patch) if args.patch else temp_dir / PATCH_FILENAME
    map_path = temp_dir / DOMAIN_MAP_FILENAME

    for label, path in (("patch.json", patch_path), (DOMAIN_MAP_FILENAME, map_path),
                        (SCAN_FILENAME, temp_dir / SCAN_FILENAME)):
        if not path.exists():
            print("[ОШИБКА] Нет входа " + label + ": " + str(path), file=sys.stderr)
            return 2
    try:
        patch = load_json(patch_path)
        domain_map = load_json(map_path)
        scan = load_json(temp_dir / SCAN_FILENAME)
        cards = load_json(temp_dir / CARDS_FILENAME)
    except ValueError as e:
        print("[ОШИБКА] Входной файл не читается как JSON: " + str(e), file=sys.stderr)
        return 2
    if cards is None:
        print("[WARN] Нет " + CARDS_FILENAME + ": перенос состояния тем (topic_state) будет пустым",
              file=sys.stderr)

    state, errors = apply_patch(domain_map, patch, scan, cards)
    if errors:
        print("[ОШИБКА] Патч не применён, найдено проблем: " + str(len(errors)), file=sys.stderr)
        for e in errors:
            print("  - " + e, file=sys.stderr)
        return 1
    for w in state.warnings:
        print("[WARN] " + w, file=sys.stderr)

    new_map = build_map_output(state)
    affected = build_affected(state)
    merge_map = compose_merge_map(load_json(temp_dir / MERGE_MAP_FILENAME, {}), state.merge_map) \
        if state.merge_map else None
    summary = ("операций " + str(len(patch["operations"])) + ", доменов " + str(len(new_map["domains"])) +
               ", затронуто для Фазы 3б " + str(len(affected["domains"])) +
               ", переименований/слияний " + str(len(state.merge_map)))

    if args.dry_run:
        print("[DRY-RUN] Патч валиден, ничего не записано: " + summary, file=sys.stderr)
        return 0

    shutil.copy2(map_path, temp_dir / DOMAIN_MAP_BEFORE_FILENAME)
    save_json(map_path, new_map)
    if merge_map is not None:
        save_json(temp_dir / MERGE_MAP_FILENAME, merge_map)
    save_json(temp_dir / AFFECTED_FILENAME, affected)
    print("[OK] Патч применён: " + summary, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
