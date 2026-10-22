#!/usr/bin/env python3
"""
Инкрементальный прогон semantic-taxonomy: драйвер и детерминированные шаги в одном модуле.
LLM не вызывает. Объединяет бывшие build_domain_cards.py, apply_domain_patch.py,
build_reassess_input.py и validate_reassessed_topics.py (алгоритмы и коды выхода прежние).

Драйвер (шаги по порядку, между ними оркестратор запускает агентов):
  prepare          scan дельты -> domain-cards.json (если дельта есть)
  apply            patch.json -> domain-map.json, затем collect дельты
  reassess-input   проверка анализов доменов -> reassess-input/*.json
  finalize         validate всех доменов -> граф -> merge -> verify
  status           только чтение: какой шаг следующий

Отдельные шаги (для ручного запуска и повторов):
  domain-cards          taxonomy.json -> domain-cards.json            (было build_domain_cards.py)
  apply-patch           patch.json -> domain-map.json, domain-merge-map.json,
                        affected-domains.json [--patch FILE] [--dry-run]
                                                                       (было apply_domain_patch.py)
  build-reassess-input  -> reassess-input/{domain_id}.json             (было build_reassess_input.py)
  validate-topics       --domain ID [--input FILE]
                        {domain_id}-topics.json -> {domain_id}-topics.validated.json
                                                                       (было validate_reassessed_topics.py)

Использование:
  python incremental.py <подкоманда> [--temp-dir DIR] [...]

Коды выхода:
  драйвер               0 - успех, 1 - ошибка шага, 2 - нет входа или не хватает анализов
  domain-cards          0 - записано, 2 - нет taxonomy.json или он не читается
  apply-patch           0 - патч применён (или dry-run валиден), 1 - ошибка валидации
                        (ничего не записано), 2 - нет входа
  build-reassess-input  0 - все входы собраны, 2 - нет входного файла или анализа дельты
  validate-topics       0 - ответ годен (возможны WARN), 1 - ошибки в ответе, 2 - нет входов
cleanup.py драйвер не запускает: очистка необратима, её вызывает пользователь.
"""

import sys
import io
import json
import copy
import shutil
import argparse
import subprocess
from collections import Counter
from pathlib import Path
from datetime import datetime

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

SCAN_FILENAME = "vault-scan.json"
TAXONOMY_FILENAME = "taxonomy.json"
CARDS_FILENAME = "domain-cards.json"
PATCH_FILENAME = "patch.json"
DOMAIN_MAP_FILENAME = "domain-map.json"
DOMAIN_MAP_BEFORE_FILENAME = "domain-map.before.json"
MERGE_MAP_FILENAME = "domain-merge-map.json"
AFFECTED_FILENAME = "affected-domains.json"
DIRTY_FILENAME = "graph-dirty-domains.json"
REASSESS_DIRNAME = "reassess-input"
OUT_DIRNAME = REASSESS_DIRNAME

STATUS_NEW = "new"
STATUS_CHANGED = "changed"
DELTA_STATUSES = (STATUS_NEW, STATUS_CHANGED)

# Скрипты, которые драйвер по-прежнему запускает подпроцессом
SCRIPT_COLLECT = "collect_vault_structure.py"
SCRIPT_GRAPH = "build_topic_graph.py"
SCRIPT_SYNTH = "synthesize_taxonomy.py"
# Свои шаги драйвер тоже запускает подпроцессом (этот же файл): изоляция и коды выхода
# остаются такими же, как у прежних отдельных скриптов
SCRIPT_SELF = Path(__file__).name

# ---------- domain-cards ----------
# Сколько терминов словаря попадает в карточку домена (по частоте у файлов).
VOCAB_LIMIT = 12
# Обрезка essence файла, когда он склеивается в essence темы.
ESSENCE_CUT = 160

# ---------- build-reassess-input ----------
TOPIC_FIELDS = ("topic_id", "topic_name", "summary", "has_notes", "mastery_percent",
                "mastery_rationale", "note_paths")

# ---------- validate-topics ----------
# Потолок mastery у темы без заметок (ожидаемая тема).
EXPECTED_MASTERY_CAP = 15
STRING_FIELDS = ("topic_id", "topic_name", "summary", "mastery_rationale")

# ---------- apply-patch ----------
KNOWN_OPS = (
    "add_file", "create_topic", "create_domain", "rename_domain", "move_topic",
    "merge_domains", "split_domain", "classify", "to_project", "keep_unclassified",
)
CONFIDENCES = ("high", "medium", "low")

# Куда сейчас положен файл
LOC_TOPIC = "topic"
LOC_PROJECT = "project"
LOC_UNCLASSIFIED = "unclassified"


# ========== общее ==========

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


def resolve_temp_dir(args) -> Path:
    if args.temp_dir:
        return Path(args.temp_dir)
    return get_project_root() / ".claude" / "temp_files"


def err(msg):
    print("[ОШИБКА] " + msg, file=sys.stderr)


# ========== domain-cards: карточки доменов из taxonomy.json ==========
# Читает ТОЛЬКО taxonomy.json (старые тексты заметок не читаются): по карточке на домен
# (название, назначение, словарь, темы с essence, число файлов) плюс unclassified и projects.

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


def cmd_domain_cards(args, runner=None) -> int:
    temp_dir = resolve_temp_dir(args)
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


# ========== apply-patch: применение patch.json к domain-map.json ==========
# Вход: patch.json, domain-map.json, vault-scan.json, domain-cards.json (необязателен).
# Выход: domain-map.json (копия domain-map.before.json перед записью), domain-merge-map.json
# (старый id -> новый id), affected-domains.json (домены для Фазы 3б и topic_state).
# Любая ошибка валидации - код 1, ничего не записывается. Записи вне патча не меняются.

class OpError(Exception):
    """Ошибка одной операции патча (до изменения состояния)."""


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


def cmd_apply_patch(args, runner=None) -> int:
    temp_dir = resolve_temp_dir(args)
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


# ========== build-reassess-input: вход переоценки тем (Фаза 3б) ==========
# Читает affected-domains.json, taxonomy.json, domain-map.json и {domain_id}-analysis.json дельты,
# пишет reassess-input/{domain_id}.json: текущие темы (включая ожидаемые без заметок), essence и
# key_concepts старых файлов, записи анализа дельты. Старые тексты vault не читаются.

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


def cmd_build_reassess_input(args, runner=None) -> int:
    temp_dir = resolve_temp_dir(args)

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


# ========== validate-topics: проверка ответа переоценки тем (Фаза 3б) ==========
# Ответу не доверяем. Читает {domain_id}-topics.json, пишет {domain_id}-topics.validated.json.
# Файлы домена - старые из taxonomy.json (или all_paths из reassess-input/{domain_id}.json)
# плюс дельта из affected-domains.json. Правила:
#   - has_notes выводится из note_paths (ответ перезаписывается, WARN при расхождении)
#   - note_paths только из файлов домена
#   - topic_id уникальны, обязательные поля на месте
#   - mastery_percent - целое 0-100; у тем без заметок не выше 15 (клип, WARN)
#   - каждый файл домена привязан хотя бы к одной теме
#   - удержание тем (при наличии reassess-input/{domain_id}.json): ОШИБКА, если известный
#     topic_id пропал из ответа, а его файлы остались в домене; WARN (поле warnings): тема пропала
#     вместе со всеми файлами или у неизменённой темы без дельты изменилась оценка

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


def retention_problems(topics, reassess, paths):
    """
    Удержание тем по входу переоценки. Возвращает (ошибки, предупреждения).
    Известный topic_id, пропавший из ответа, - ошибка, если его прежние файлы остались в домене
    (paths): агент не имеет права удалять и сливать темы. Если файлы темы ушли из домена, пропажа
    легитимна, пишется WARN. Прежние файлы берутся из reassess["topics"] по topic_id.
    Оценка неизменённой темы (те же note_paths, без файлов дельты) сверяется с прежней; при
    повторе topic_id во входе (слияние доменов) прежняя оценка неоднозначна и не проверяется.
    """
    errors = []
    warns = []
    answered = {t["topic_id"]: t for t in topics if isinstance(t.get("topic_id"), str)}
    prior = {}
    for t in reassess.get("topics") or []:
        prior.setdefault(t.get("topic_id"), []).append(t)
    for tid in reassess.get("known_topic_ids") or []:
        if tid in answered:
            continue
        old_files = set()
        for t in prior.get(tid, []):
            old_files |= set(t.get("note_paths") or [])
        if old_files & paths:
            errors.append("известная тема " + str(tid) + " пропала из ответа, хотя её файлы остались в домене "
                          "(удаление и слияние тем контрактом агента не предусмотрены)")
        else:
            warns.append("тема " + str(tid) + " пропала вместе со своими файлами (файлы ушли из домена)")
    delta = {r.get("path") for r in reassess.get("delta_files") or [] if isinstance(r, dict)}
    for tid, items in prior.items():
        topic = answered.get(tid)
        if topic is None or len(items) != 1:
            continue
        before = items[0]
        files = set(topic.get("note_paths") or [])
        if files != set(before.get("note_paths") or []) or files & delta:
            continue
        if topic.get("mastery_percent") != before.get("mastery_percent"):
            warns.append("тема " + str(tid) + ": файлы те же, дельты нет, а оценка изменена с " +
                         json.dumps(before.get("mastery_percent")) + " на " +
                         json.dumps(topic.get("mastery_percent")))
    return errors, warns


def validate(doc, paths, reassess=None):
    """Вернуть (очищенный документ, ошибки, предупреждения). reassess - вход переоценки (необязателен)."""
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
    if reassess:
        retention_errors, retained = retention_problems(cleaned, reassess, paths)
        errors.extend(retention_errors)
        if retained:
            result["warnings"] = retained
            warns.extend(retained)
    return result, errors, warns


def cmd_validate_topics(args, runner=None) -> int:
    temp_dir = resolve_temp_dir(args)
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
    result, errors, warns = validate(doc, paths, reassess)
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


# ========== драйвер инкремента ==========

def run_script(name, args, runner=None) -> int:
    """Запустить соседний скрипт подпроцессом, вернуть код выхода.

    runner - подмена subprocess.run (получает список аргументов, без shell);
    результат может быть объектом с returncode или самим кодом.
    """
    cmd = [sys.executable, str(Path(__file__).resolve().parent / name)] + [str(a) for a in args]
    result = (runner or subprocess.run)(cmd)
    code = getattr(result, "returncode", result)
    return int(code)


def temp_args(temp_dir: Path):
    return ["--temp-dir", str(temp_dir)]


def read_json_or_fail(path: Path):
    """Прочитать JSON; (данные, None) или (None, код ошибки)."""
    if not path.exists():
        err("Нет входа " + path.name + ": " + str(path))
        return None, 2
    try:
        return load_json(path), None
    except ValueError as e:
        err("Файл " + path.name + " не читается как JSON: " + str(e))
        return None, 2


def affected_entries(temp_dir: Path):
    data = load_json(temp_dir / AFFECTED_FILENAME, {}) or {}
    return [e for e in data.get("domains", []) if e.get("domain_id")]


def count_statuses(scan) -> dict:
    counts = {}
    for f in (scan or {}).get("files", []):
        st = f.get("status")
        counts[st] = counts.get(st, 0) + 1
    return counts


def cmd_prepare(args, runner=None) -> int:
    temp_dir = resolve_temp_dir(args)
    code = run_script(SCRIPT_COLLECT, ["--scan", "--new-only"] + temp_args(temp_dir), runner)
    if code != 0:
        err("collect_vault_structure --scan завершился с кодом " + str(code))
        return code
    try:
        scan, fail = read_json_or_fail(temp_dir / SCAN_FILENAME)
    except OSError as e:
        err(str(e))
        return 2
    if fail:
        return fail
    counts = count_statuses(scan)
    n_new = counts.get(STATUS_NEW, 0)
    n_changed = counts.get(STATUS_CHANGED, 0)
    if not n_new and not n_changed:
        print("[OK] Дельты нет: шаги 2-8 не нужны")
        return 0
    code = run_script(SCRIPT_SELF, ["domain-cards"] + temp_args(temp_dir), runner)
    if code != 0:
        err("domain-cards завершился с кодом " + str(code))
        return code
    print("Дельта: new=" + str(n_new) + " changed=" + str(n_changed))
    print("Следующий шаг: vault-domain-architect по " + CARDS_FILENAME + ", результат - " + PATCH_FILENAME)
    return 0


def cmd_apply(args, runner=None) -> int:
    temp_dir = resolve_temp_dir(args)
    if not (temp_dir / PATCH_FILENAME).exists():
        err("Нет " + PATCH_FILENAME + ": " + str(temp_dir / PATCH_FILENAME))
        return 2
    patch_args = temp_args(temp_dir)
    if args.patch:
        patch_args += ["--patch", args.patch]
    if args.dry_run:
        patch_args.append("--dry-run")
    code = run_script(SCRIPT_SELF, ["apply-patch"] + patch_args, runner)
    if code != 0:
        err("apply-patch завершился с кодом " + str(code))
        return code
    if args.dry_run:
        print("[OK] dry-run: патч проверен, дальнейшие шаги пропущены")
        return 0
    code = run_script(SCRIPT_COLLECT, ["--collect", "--new-only"] + temp_args(temp_dir), runner)
    if code != 0:
        err("collect_vault_structure --collect завершился с кодом " + str(code))
        return code
    try:
        entries = affected_entries(temp_dir)
    except ValueError as e:
        err("Файл " + AFFECTED_FILENAME + " не читается как JSON: " + str(e))
        return 2
    with_new = [e["domain_id"] for e in entries if e.get("new_files")]
    only_reassess = [e["domain_id"] for e in entries if not e.get("new_files")]
    print("Нужен vault-domain-analyzer (режим добавления): " + (", ".join(with_new) or "нет"))
    print("Только переоценка: " + (", ".join(only_reassess) or "нет"))
    return 0


def cmd_reassess_input(args, runner=None) -> int:
    temp_dir = resolve_temp_dir(args)
    scan_path = temp_dir / SCAN_FILENAME
    if not scan_path.exists():
        err("Нет входа " + SCAN_FILENAME + ": " + str(scan_path))
        return 2
    try:
        if not (temp_dir / AFFECTED_FILENAME).exists():
            err("Нет входа " + AFFECTED_FILENAME + ": " + str(temp_dir / AFFECTED_FILENAME))
            return 2
        entries = affected_entries(temp_dir)
    except ValueError as e:
        err("Файл " + AFFECTED_FILENAME + " не читается как JSON: " + str(e))
        return 2
    scan_mtime = scan_path.stat().st_mtime
    missing = []
    for e in entries:
        if not e.get("new_files"):
            continue
        p = temp_dir / (e["domain_id"] + "-analysis.json")
        if not p.exists() or p.stat().st_mtime < scan_mtime:
            missing.append(e["domain_id"])
    if missing:
        err("Нет свежего анализа (отсутствует или старше " + SCAN_FILENAME + "): " + ", ".join(missing))
        return 2
    code = run_script(SCRIPT_SELF, ["build-reassess-input"] + temp_args(temp_dir), runner)
    if code != 0:
        err("build-reassess-input завершился с кодом " + str(code))
        return code
    domains = sorted(p.stem for p in (temp_dir / REASSESS_DIRNAME).glob("*.json"))
    print("Домены для vault-topic-reassessor: " + (", ".join(domains) or "нет"))
    return 0


def cmd_finalize(args, runner=None) -> int:
    temp_dir = resolve_temp_dir(args)
    try:
        if not (temp_dir / AFFECTED_FILENAME).exists():
            err("Нет входа " + AFFECTED_FILENAME + ": " + str(temp_dir / AFFECTED_FILENAME))
            return 2
        entries = affected_entries(temp_dir)
    except ValueError as e:
        err("Файл " + AFFECTED_FILENAME + " не читается как JSON: " + str(e))
        return 2
    failed = []
    for e in entries:
        code = run_script(SCRIPT_SELF, ["validate-topics", "--domain", e["domain_id"]] + temp_args(temp_dir), runner)
        if code != 0:
            err("validate-topics: " + e["domain_id"] + " код " + str(code))
            failed.append(e["domain_id"])
    if failed:
        err("Провалили проверку тем (" + str(len(failed)) + "): " + ", ".join(failed))
        return 1

    code = run_script(SCRIPT_GRAPH, temp_args(temp_dir), runner)
    if code != 0:
        err("build_topic_graph завершился с кодом " + str(code))
        return code

    merge_args = ["--merge"] + temp_args(temp_dir)
    if args.dry_run:
        merge_args.append("--dry-run")
    code = run_script(SCRIPT_SYNTH, merge_args, runner)
    if code != 0:
        err("synthesize_taxonomy --merge завершился с кодом " + str(code))
        return code
    if args.dry_run:
        print("[OK] dry-run: merge проверен, --verify и разбор relevance пропущены")
        return 0

    code = run_script(SCRIPT_SYNTH, ["--verify", str(temp_dir / "taxonomy.json")], runner)
    if code != 0:
        err("synthesize_taxonomy --verify завершился с кодом " + str(code))
        return code

    try:
        dirty = load_json(temp_dir / DIRTY_FILENAME, {}) or {}
    except ValueError:
        dirty = {}
    relevance = dirty.get("relevance_required") or []
    if relevance:
        print("[WARN] Нужна оценка релевантности связей: " + str(len(relevance)) +
              ". Merge уже выполнен, это дополнительный шаг: "
              "build_topic_graph.py --relevance-requests ... / --apply-relevance ...", file=sys.stderr)
    print("[OK] Инкремент завершён (cleanup.py не запускался - вызывается пользователем)")
    return 0


def _mtime(path: Path):
    return path.stat().st_mtime if path.exists() else None


def next_step(temp_dir: Path):
    """Определить следующий шаг по наличию и mtime файлов: (строка, найденные файлы)."""
    found = [n for n in (SCAN_FILENAME, CARDS_FILENAME, PATCH_FILENAME, DOMAIN_MAP_FILENAME,
                         AFFECTED_FILENAME, REASSESS_DIRNAME, "taxonomy.json")
             if (temp_dir / n).exists()]
    scan_m = _mtime(temp_dir / SCAN_FILENAME)
    if scan_m is None:
        return "prepare (нет " + SCAN_FILENAME + ")", found
    try:
        scan = load_json(temp_dir / SCAN_FILENAME, {}) or {}
        entries = affected_entries(temp_dir)
    except ValueError:
        return "prepare (файл не читается как JSON)", found
    counts = count_statuses(scan)
    has_delta = bool(counts.get(STATUS_NEW) or counts.get(STATUS_CHANGED))
    if not has_delta:
        return "дельты нет, шаги не нужны", found
    if not (temp_dir / CARDS_FILENAME).exists():
        return "prepare (нет " + CARDS_FILENAME + ")", found
    patch_m = _mtime(temp_dir / PATCH_FILENAME)
    if patch_m is None:
        return "vault-domain-architect по " + CARDS_FILENAME + " (нет " + PATCH_FILENAME + ")", found
    map_m = _mtime(temp_dir / DOMAIN_MAP_FILENAME)
    if map_m is None or patch_m > map_m or not entries and not (temp_dir / AFFECTED_FILENAME).exists():
        return "apply", found
    for e in entries:
        if not e.get("new_files"):
            continue
        m = _mtime(temp_dir / (e["domain_id"] + "-analysis.json"))
        if m is None or m < scan_m:
            return "vault-domain-analyzer (нет свежего анализа: " + e["domain_id"] + ")", found
    reassess_dir = temp_dir / REASSESS_DIRNAME
    if not reassess_dir.is_dir():
        return "reassess-input", found
    inputs = list(reassess_dir.glob("*.json"))
    base_m = max([i.stat().st_mtime for i in inputs] + [0])
    for e in entries:
        m = _mtime(temp_dir / (e["domain_id"] + "-topics.json"))
        if m is None or m < base_m:
            return "vault-topic-reassessor (нет свежего " + e["domain_id"] + "-topics.json)", found
    return "finalize", found


def cmd_status(args, runner=None) -> int:
    temp_dir = resolve_temp_dir(args)
    step, found = next_step(temp_dir)
    print("Следующий шаг: " + step)
    print("Найдено: " + (", ".join(found) or "ничего"))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description="Инкрементальный прогон Obsidian Vault Performance")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--temp-dir", help="директория temp_files (по умолчанию .claude/temp_files проекта)")
        p.set_defaults(func=func)
        return p

    # драйвер
    add("prepare", cmd_prepare, "scan дельты и карточки доменов")
    p = add("apply", cmd_apply, "применить patch.json и собрать дельту")
    p.add_argument("--patch", help="файл патча (пробрасывается в apply-patch)")
    p.add_argument("--dry-run", action="store_true", help="только проверить патч")
    add("reassess-input", cmd_reassess_input, "входы переоценки тем")
    p = add("finalize", cmd_finalize, "validate, граф, merge, verify")
    p.add_argument("--dry-run", action="store_true", help="merge без записи, без verify")
    add("status", cmd_status, "показать следующий шаг")

    # отдельные шаги
    add("domain-cards", cmd_domain_cards, "карточки доменов из taxonomy.json")
    p = add("apply-patch", cmd_apply_patch, "применение patch.json к domain-map.json")
    p.add_argument("--patch", help="путь к patch.json (по умолчанию temp-dir/patch.json)")
    p.add_argument("--dry-run", action="store_true", help="проверить и показать итог, ничего не записывая")
    add("build-reassess-input", cmd_build_reassess_input, "вход переоценки тем затронутых доменов (Фаза 3б)")
    p = add("validate-topics", cmd_validate_topics, "проверка ответа переоценки тем (Фаза 3б)")
    p.add_argument("--domain", required=True, help="domain_id")
    p.add_argument("--input", help="путь к {domain_id}-topics.json (по умолчанию temp-dir)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
