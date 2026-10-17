#!/usr/bin/env python3
"""
Построение/обновление графа тем и доменов (topic-graph.json).

Граф трёхуровневый: домен -> тема -> файл (data-model.md, Clarifications
spec.md Q2). Diff поверх существующего графа определяет dirty-домены/темы
(graph-dirty-domains.json) - вход для synthesize_taxonomy.py --merge.

Режимы:
  --full    : построить граф с нуля (игнорировать существующий topic-graph.json)
  (по умолчанию): инкрементальный diff поверх существующего графа
"""

import json
import sys
import io
from pathlib import Path
from datetime import datetime

# Установить UTF-8 кодировку для вывода (защита от ошибок на Windows)
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')


def get_project_root():
    """Определить корень проекта по расположению скрипта."""
    return Path(__file__).resolve().parents[4]


def load_json(path: Path, default=None):
    """Безопасно загрузить JSON-файл, вернуть default при отсутствии/ошибке."""
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[WARN] Ошибка чтения {path.name}: {e}", file=sys.stderr)
        return default


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# === Контракт topic-graph.json (contracts/topic-graph-schema.md) ===

def empty_graph() -> dict:
    return {
        "revision": 0,
        "generated_at": None,
        "nodes": {},
        "edges": [],
        "domains": {},
    }


def load_graph(temp_dir: Path, full_mode: bool) -> dict:
    """
    Загрузить topic-graph.json. Отсутствие файла или --full интерпретируется
    как "граф ещё не строился" (contracts/topic-graph-schema.md, раздел
    "Совместимость") - не ошибка, а пустой граф с revision=0.
    """
    graph_path = temp_dir / "topic-graph.json"
    if full_mode:
        # --full пересобирает узлы и рёбра, но счётчик ревизий продолжается:
        # иначе graph-revision-log.json получит повторяющиеся номера.
        previous = load_json(graph_path, None)
        graph = empty_graph()
        if isinstance(previous, dict) and isinstance(previous.get("revision"), int):
            graph["revision"] = previous["revision"]
        return graph
    graph = load_json(graph_path, None)
    if graph is None:
        return empty_graph()
    graph.setdefault("revision", 0)
    graph.setdefault("nodes", {})
    graph.setdefault("edges", [])
    graph.setdefault("domains", {})
    return graph


def save_graph(temp_dir: Path, graph: dict) -> dict:
    """Сохранить граф, инкрементировав revision и обновив generated_at."""
    graph["revision"] = graph.get("revision", 0) + 1
    graph["generated_at"] = datetime.now().isoformat()
    save_json(temp_dir / "topic-graph.json", graph)
    return graph


def append_revision_log(temp_dir: Path, revision: int, dirty_domains: list) -> None:
    """Вести graph-revision-log.json - история применённых diff'ов (research.md п.4)."""
    log_path = temp_dir / "graph-revision-log.json"
    log = load_json(log_path, {"entries": []})
    log.setdefault("entries", [])
    log["entries"].append({
        "revision": revision,
        "timestamp": datetime.now().isoformat(),
        "dirty_domains": dirty_domains,
    })
    save_json(log_path, log)


# === Источники данных для построения узлов ===

def load_path_hash_map(temp_dir: Path) -> dict:
    """
    path -> content_hash для всех файлов текущего прогона Фазы 1
    (vault-scan.json).
    """
    scan = load_json(temp_dir / "vault-scan.json", {})
    path_hash = {}
    for entry in scan.get("files", []):
        if "path" in entry and "content_hash" in entry:
            path_hash[entry["path"]] = entry["content_hash"]
    return path_hash


def load_new_domain_analyses(temp_dir: Path) -> dict:
    """domain_id -> {path -> file_info} из свежих {domain_id}-analysis.json этого прогона."""
    result = {}
    for file in temp_dir.glob("*-analysis.json"):
        if file.name == "vault-structure-analysis.json":
            continue
        stem = file.stem
        domain_id = stem[:-len("-analysis")] if stem.endswith("-analysis") else stem
        data = load_json(file, None)
        if not isinstance(data, dict):
            continue
        files = data.get("files", [])
        by_path = {}
        for file_info in files:
            if isinstance(file_info, dict) and isinstance(file_info.get("path"), str):
                # путь нормализуется к виду node_id (прямые слеши)
                by_path[file_info["path"].replace("\\", "/")] = file_info
        result[domain_id] = by_path
    return result


def load_path_to_topic(taxonomy: dict) -> dict:
    """path -> (domain_id, topic_id) из существующей taxonomy.json (топики предыдущего прогона)."""
    mapping = {}
    for domain in (taxonomy or {}).get("domains", []):
        domain_id = domain.get("domain_id")
        for topic in domain.get("topics", []) or []:
            topic_id = topic.get("topic_id")
            if not topic_id:
                continue
            for path in topic.get("note_paths", []) or []:
                mapping[path] = (domain_id, topic_id)
    return mapping


def load_path_to_topic_from_structure(vault_structure: dict) -> dict:
    """
    Фаза 4 (вариант б): path -> (domain_id, topic_id) из vault-structure-analysis.json
    (domains[].topics[].note_paths, пишет collect_vault_structure.py). Используется
    только когда в taxonomy.json тем нет. Тема без topic_id берётся по topic_name.
    """
    mapping = {}
    for domain in (vault_structure or {}).get("domains", []) or []:
        domain_id = domain.get("domain_id")
        for topic in domain.get("topics", []) or []:
            topic_id = topic.get("topic_id") or topic.get("topic_name")
            if not topic_id:
                continue
            for path in topic.get("note_paths", []) or []:
                mapping[path] = (domain_id, topic_id)
    return mapping


def composite_topic_id(domain_id: str, local_topic_id: str) -> str:
    return f"{domain_id}::{local_topic_id}"


# === Построение узлов/рёбер файлов ===

def build_file_node(path: str, domain_id: str, content_hash, existing_node,
                     domain_analyses: dict, semantic_cache: dict,
                     path_to_topic: dict, next_revision: int,
                     taxonomy_files_by_path: dict,
                     known_in_taxonomy: set = None) -> dict:
    """
    Построить/обновить узел kind="file". Источник essence/key_concepts/title/type
    (data-model.md, Node kind = "file"):
      1. Свежий анализ домена этого прогона (Фаза 2), если есть
      2. Иначе, если файл не новый/не изменился - прежние поля узла графа
      3. Иначе (новый/изменившийся файл без глубокого анализа) - классификация
         Фазы 0.2 из semantic-analysis-cache.json по content_hash
    """
    # "Новый" файл - не встречался ни в графе, ни в taxonomy.json прошлых
    # прогонов. Это отличает истинно новую заметку от bootstrap-случая (граф
    # строится впервые поверх уже существующей taxonomy.json) - во втором
    # случае файл не новый и не изменившийся, домен из-за него dirty не
    # становится (research.md, граф лишь фиксирует уже известное состояние).
    # known_in_taxonomy - пути, известные taxonomy.json (без fallback по структуре,
    # чтобы темы из vault-structure не меняли определение "новый файл")
    known_paths = path_to_topic if known_in_taxonomy is None else known_in_taxonomy
    is_new = existing_node is None and path not in known_paths
    is_changed = (not is_new) and existing_node and content_hash and existing_node.get("content_hash") != content_hash

    analysis_entry = domain_analyses.get(domain_id, {}).get(path)
    mentions = None  # упоминания другим файлам, хранятся в узле как есть (Фаза 1)

    if analysis_entry:
        essence = analysis_entry.get("essence", "") or ""
        key_concepts = analysis_entry.get("key_concepts", []) or []
        title = analysis_entry.get("title") or Path(path).stem
        file_type = analysis_entry.get("type")
        mentions = analysis_entry.get("mentions")
        if isinstance(file_type, list):
            file_type = file_type[0] if file_type else None
    elif existing_node and not is_new and not is_changed:
        essence = existing_node.get("essence", "")
        key_concepts = existing_node.get("key_concepts", [])
        title = existing_node.get("title", Path(path).stem)
        file_type = existing_node.get("type")
        mentions = existing_node.get("mentions")
    elif not is_new and not is_changed:
        # Bootstrap: файл уже известен taxonomy.json, но ещё не был узлом
        # графа - essence/title/type берём из taxonomy.json (файл домена),
        # а не из Фазы 0.2 (которая отражает лишь предварительную классификацию).
        taxonomy_entry = taxonomy_files_by_path.get(path, {})
        essence = taxonomy_entry.get("essence", "") or ""
        key_concepts = taxonomy_entry.get("key_concepts", []) or []
        title = taxonomy_entry.get("title") or Path(path).stem
        file_type = taxonomy_entry.get("type")
        mentions = taxonomy_entry.get("mentions")
        if isinstance(file_type, list):
            file_type = file_type[0] if file_type else None
    else:
        # Схема кеша Фазы 1 (collect_vault_structure.py --collect): запись по
        # content_hash = {"domain_id", "essence", "key_concepts"} - essence
        # берётся из описания темы в domain-map.json.
        cache_entry = semantic_cache.get(content_hash, {}) if content_hash else {}
        essence = cache_entry.get("essence", "") or ""
        key_concepts = cache_entry.get("key_concepts", []) or []
        title = Path(path).stem
        file_type = None

    # topic_id берётся из существующей taxonomy.json (path_to_topic) независимо
    # от того, "новый" ли узел для ГРАФА - при первой постройке графа (bootstrap)
    # ни один файл ещё не существует в old_nodes, но многие уже классифицированы
    # по темам в taxonomy.json прошлых прогонов, и это не должно теряться.
    topic_ref = path_to_topic.get(path)
    if topic_ref and topic_ref[0] == domain_id:
        topic_id = composite_topic_id(domain_id, topic_ref[1])
    elif existing_node:
        topic_id = existing_node.get("topic_id")
    else:
        topic_id = None

    node = {
        "node_id": path,
        "kind": "file",
        "domain_id": domain_id,
        "topic_id": topic_id,
        "content_hash": content_hash or (existing_node or {}).get("content_hash"),
        "essence": essence,
        "key_concepts": key_concepts,
        "title": title,
        "type": file_type,
        "mentions": mentions if isinstance(mentions, list) else [],
        "graph_revision_added": (existing_node or {}).get("graph_revision_added", next_revision),
    }
    return node, is_new, is_changed


def build_graph_state(temp_dir: Path, graph: dict):
    """
    Построить обновлённое состояние узлов/рёбер graph на основе текущего
    vault-structure-analysis.json + источников из load_new_domain_analyses/
    load_path_hash_map/load_path_to_topic (T004, T005).

    Возвращает (new_nodes, new_edges, new_domains, new_file_ids, changed_file_ids,
    dirty_domains, dirty_topics).
    """
    vault_structure = load_json(temp_dir / "vault-structure-analysis.json", {}) or {}
    taxonomy = load_json(temp_dir / "taxonomy.json", {}) or {}
    semantic_cache = load_json(temp_dir / "semantic-analysis-cache.json", {}) or {}
    path_hash_map = load_path_hash_map(temp_dir)
    domain_analyses = load_new_domain_analyses(temp_dir)
    path_to_topic = load_path_to_topic(taxonomy)
    known_in_taxonomy = set(path_to_topic)
    if not path_to_topic:
        # Фаза 4 (вариант б): на первом прогоне taxonomy.json ещё без тем -
        # темы берём из vault-structure-analysis.json (domains[].topics[].note_paths)
        path_to_topic = load_path_to_topic_from_structure(vault_structure)
        if path_to_topic:
            print(f"[INFO] taxonomy.json без тем: темы из vault-structure-analysis.json "
                  f"({len(path_to_topic)} файлов)", file=sys.stderr)

    taxonomy_files_by_path = {}
    for domain in (taxonomy or {}).get("domains", []):
        for f in domain.get("files", []) or []:
            if isinstance(f, dict) and "path" in f:
                taxonomy_files_by_path[f["path"]] = f

    old_nodes = graph.get("nodes", {})
    old_edges = graph.get("edges", [])
    old_edge_index = {(e["source"], e["target"], e["type"]): e for e in old_edges}

    next_revision = graph.get("revision", 0) + 1

    new_nodes = {}
    member_of_edges = []
    other_edges = [e for e in old_edges if e.get("type") not in ("member_of",)]

    domain_member_files = {}
    topic_member_files = {}

    new_file_ids = []
    changed_file_ids = []
    dirty_domains = set()
    dirty_topics = set()

    domain_entries = vault_structure.get("domains", [])
    if not domain_entries:
        # vault-structure-analysis.json отсутствует (например, удалён cleanup.py) -
        # fallback: список доменов/файлов берём напрямую из taxonomy.json.
        domain_entries = taxonomy.get("domains", [])

    for domain_entry in domain_entries:
        domain_id = domain_entry["domain_id"]
        domain_member_files.setdefault(domain_id, [])

        for f in domain_entry.get("files", []):
            path = f["path"]
            content_hash = path_hash_map.get(path)
            existing_node = old_nodes.get(path)

            node, is_new, is_changed = build_file_node(
                path, domain_id, content_hash, existing_node,
                domain_analyses, semantic_cache, path_to_topic, next_revision,
                taxonomy_files_by_path, known_in_taxonomy=known_in_taxonomy,
            )
            new_nodes[path] = node
            domain_member_files[domain_id].append(path)

            if node["topic_id"]:
                topic_member_files.setdefault(node["topic_id"], []).append(path)

            if is_new:
                new_file_ids.append(path)
                dirty_domains.add(domain_id)
            elif is_changed:
                changed_file_ids.append(path)
                dirty_domains.add(domain_id)
                if node["topic_id"]:
                    dirty_topics.add(node["topic_id"])

            # member_of: файл -> домен
            key = (path, domain_id, "member_of")
            member_of_edges.append(old_edge_index.get(key, {
                "source": path, "target": domain_id, "type": "member_of",
                "added_in_revision": next_revision,
            }))

            # member_of: файл -> тема (если тема уже определена)
            if node["topic_id"]:
                key = (path, node["topic_id"], "member_of")
                member_of_edges.append(old_edge_index.get(key, {
                    "source": path, "target": node["topic_id"], "type": "member_of",
                    "added_in_revision": next_revision,
                }))

    # Узлы доменов/тем
    for domain_id, member_files in domain_member_files.items():
        old_domain_node = old_nodes.get(domain_id, {})
        new_nodes[domain_id] = {
            "node_id": domain_id,
            "kind": "domain",
            "parent_domain_id": None,
            "member_file_ids": member_files,
            "last_progress_revision": old_domain_node.get("last_progress_revision", 0),
        }

    for topic_id, member_files in topic_member_files.items():
        domain_id = topic_id.split("::", 1)[0]
        old_topic_node = old_nodes.get(topic_id, {})
        new_nodes[topic_id] = {
            "node_id": topic_id,
            "kind": "topic",
            "parent_domain_id": domain_id,
            "member_file_ids": member_files,
            "last_progress_revision": old_topic_node.get("last_progress_revision", 0),
        }

    new_domains = {
        domain_id: {
            "node_count": len(member_files),
            "last_progress_revision": new_nodes[domain_id]["last_progress_revision"],
        }
        for domain_id, member_files in domain_member_files.items()
    }

    # Рёбра, чей source или target исчез из графа (файл удалён из vault), не переносятся
    kept_other_edges = [e for e in other_edges
                        if e.get("source") in new_nodes and e.get("target") in new_nodes]
    if len(kept_other_edges) != len(other_edges):
        print(f"[INFO] Удалено рёбер к исчезнувшим узлам: {len(other_edges) - len(kept_other_edges)}",
              file=sys.stderr)
    new_edges = member_of_edges + kept_other_edges

    return (new_nodes, new_edges, new_domains, new_file_ids, changed_file_ids,
            sorted(dirty_domains), sorted(dirty_topics))


# === Валидация (data-model.md / contracts/topic-graph-schema.md) ===

def validate_graph(graph: dict) -> list:
    """Вернуть список строк-предупреждений о нарушениях validation rules. Не фатально."""
    warnings = []
    nodes = graph.get("nodes", {})

    for edge in graph.get("edges", []):
        if edge["source"] not in nodes:
            warnings.append(f"Ребро ссылается на несуществующий узел-источник: {edge['source']}")
        if edge["target"] not in nodes:
            warnings.append(f"Ребро ссылается на несуществующий узел-цель: {edge['target']}")

    for node_id, node in nodes.items():
        if node.get("kind") == "file":
            domain_id = node.get("domain_id")
            if domain_id and domain_id not in nodes:
                warnings.append(f"Файл {node_id}: domain_id {domain_id} не найден среди узлов")
            topic_id = node.get("topic_id")
            if topic_id:
                topic_node = nodes.get(topic_id)
                if not topic_node:
                    warnings.append(f"Файл {node_id}: topic_id {topic_id} не найден среди узлов")
                elif topic_node.get("parent_domain_id") != domain_id:
                    warnings.append(
                        f"Файл {node_id}: тема {topic_id} принадлежит другому домену, "
                        f"чем member_of ребро файла"
                    )
        elif node.get("kind") == "topic":
            parent = node.get("parent_domain_id")
            if parent and parent not in nodes:
                warnings.append(f"Тема {node_id}: parent_domain_id {parent} не найден среди узлов")

    # Фаза 5: домен с файлами без рёбер dependency должен иметь явный статус связей.
    # Старые графы без relations_status - только предупреждения, не ошибка.
    dep_count = {}
    for edge in graph.get("edges", []):
        if edge.get("type") != "dependency":
            continue
        src = nodes.get(edge.get("source"))
        if src:
            key = src.get("domain_id")
            dep_count[key] = dep_count.get(key, 0) + 1
    domains_meta = graph.get("domains") if isinstance(graph.get("domains"), dict) else {}
    file_domains = sorted(
        {n.get("domain_id") for n in nodes.values() if n.get("kind") == "file" and n.get("domain_id")},
        key=str,
    )
    for domain_id in file_domains:
        info = domains_meta.get(domain_id)
        status = info.get("relations_status") if isinstance(info, dict) else None
        has_deps = dep_count.get(domain_id, 0) > 0
        if not has_deps and status not in ("derived", "no_dependencies_found", "dependencies_unresolved"):
            warnings.append(
                f"Домен {domain_id}: нет рёбер dependency, статус связей: "
                f"{status or 'не задан'} (связи не выводились)"
            )
        elif status == "dependencies_unresolved" and not has_deps:
            warnings.append(
                f"Домен {domain_id}: в анализе есть зависимости, но ни одна не разрешена в граф "
                f"(неоднозначны или не найдены), рёбер dependency нет"
            )
        elif status == "derived" and not has_deps:
            warnings.append(f"Домен {domain_id}: статус связей derived, но рёбер dependency нет")

    return warnings


def has_dependency_cycle(graph: dict) -> list:
    """
    Проверить отсутствие циклов в рёбрах type="dependency" (data-model.md, Validation
    rules для Graph Edge). Проверка идёт по всему графу: цикл внутри одного домена
    находится, и цикл через несколько доменов тоже - зависимости между доменами
    возникают при резолве basename. Возвращает список строк с найденными циклами
    (не фатально - логируется, см. T019).
    """
    nodes = graph.get("nodes", {})
    adjacency = {}
    for edge in graph.get("edges", []):
        if edge.get("type") != "dependency":
            continue
        if edge.get("source") not in nodes:
            continue
        adjacency.setdefault(edge["source"], []).append(edge["target"])

    issues = []
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {}

    def visit(node, path):
        color[node] = GRAY
        for nxt in adjacency.get(node, []):
            if color.get(nxt, WHITE) == GRAY:
                issues.append(f"Цикл dependency: {' -> '.join(path + [nxt])}")
            elif color.get(nxt, WHITE) == WHITE:
                visit(nxt, path + [nxt])
        color[node] = BLACK

    for node in list(adjacency.keys()):
        if color.get(node, WHITE) == WHITE:
            visit(node, [node])

    return issues


# === Dirty-domains артефакт (T006) ===

def save_dirty_domains(temp_dir: Path, graph_revision: int, dirty_domains: list,
                        dirty_topics: list, new_file_ids: list, changed_file_ids: list,
                        relevance_required: list = None) -> None:
    save_json(temp_dir / "graph-dirty-domains.json", {
        "graph_revision": graph_revision,
        "dirty_domains": dirty_domains,
        "dirty_topics": dirty_topics,
        "new_file_ids": new_file_ids,
        "changed_file_ids": changed_file_ids,
        # Фаза 3: новые файлы, для которых нужен вызов vault-relevance-finder
        # (нет свежего анализа домена; связи остальных уже посчитаны Фазой 2)
        "relevance_required": list(relevance_required or []),
    })


# === Вход/применение vault-relevance-finder (T015, T017, T019) ===

def build_relevance_request(graph: dict, file_node_id: str) -> dict:
    """
    Подготовить вход для субагента vault-relevance-finder на одну заметку
    (contracts/vault-relevance-finder-contract.md). Передаются только сжатые
    поля, без полного essence чужих файлов и без исходного текста.
    """
    nodes = graph.get("nodes", {})
    file_node = nodes[file_node_id]
    domain_id = file_node["domain_id"]
    domain_node = nodes.get(domain_id, {})

    domain_nodes = []
    for member_id in domain_node.get("member_file_ids", []):
        if member_id == file_node_id:
            continue
        member = nodes.get(member_id)
        if not member or member.get("kind") != "file":
            continue
        domain_nodes.append({
            "node_id": member_id,
            "title": member.get("title", ""),
            "key_concepts": member.get("key_concepts", []),
            "type": member.get("type"),
        })

    return {
        "new_file": {
            "node_id": file_node_id,
            "essence": file_node.get("essence", ""),
            "key_concepts": file_node.get("key_concepts", []),
        },
        "domain_nodes": domain_nodes,
    }


def apply_relevance_result(graph: dict, file_node_id: str, result: dict) -> list:
    """
    Применить ответ vault-relevance-finder к графу: добавить рёбра related/
    dependency, отбросив невалидные node_id (contracts/vault-relevance-finder-contract.md,
    "Обработка ошибок"). Возвращает список предупреждений (отброшенные id).
    """
    warnings = []
    nodes = graph.get("nodes", {})
    next_revision = graph.get("revision", 0) + 1

    if not isinstance(result, dict):
        warnings.append(f"{file_node_id}: ответ vault-relevance-finder не является JSON-объектом, пропущен")
        return warnings

    # Контракт vault-relevance-finder: цели - только файлы того же домена (domain_nodes),
    # домены и темы как цели не принимаются.
    domain_id = nodes.get(file_node_id, {}).get("domain_id")
    valid_ids = {nid for nid, n in nodes.items()
                 if n.get("kind") == "file" and n.get("domain_id") == domain_id}

    lists = {}
    for field in ("related", "dependencies"):
        values = result.get(field) or []
        if not isinstance(values, list):
            warnings.append(f"{file_node_id}: поле '{field}' не является списком, пропущено")
            values = []
        lists[field] = values

    # Узел может быть только в одном поле: зависимость сильнее связи "related".
    dependency_targets = {v for v in lists["dependencies"] if isinstance(v, str)}

    for edge_type, field in (("related", "related"), ("dependency", "dependencies")):
        for target_id in lists[field]:
            if not isinstance(target_id, str) or target_id not in valid_ids:
                warnings.append(
                    f"{file_node_id}: отброшен node_id вне домена (поле '{field}'): {target_id}"
                )
                continue
            if target_id == file_node_id:
                warnings.append(f"{file_node_id}: самоссылка в поле '{field}' пропущена")
                continue
            if edge_type == "related" and target_id in dependency_targets:
                warnings.append(
                    f"{file_node_id}: {target_id} указан и в related, и в dependencies, оставлено dependencies"
                )
                continue
            if any(e["source"] == file_node_id and e["target"] == target_id and e["type"] == edge_type
                   for e in graph["edges"]):
                continue
            graph["edges"].append({
                "source": file_node_id, "target": target_id, "type": edge_type,
                "added_in_revision": next_revision,
            })

    return warnings


def refresh_relations_status(graph: dict, domain_ids) -> int:
    """
    После применения ответов relevance-finder: домен с рёбрами dependency получает
    relations_status="derived", иначе статус расходился бы с графом. Возвращает число
    изменённых доменов.
    """
    nodes = graph.get("nodes", {})
    dep_domains = {
        nodes[e["source"]].get("domain_id") for e in graph.get("edges", [])
        if e.get("type") == "dependency" and e.get("source") in nodes
    }
    domains = graph.setdefault("domains", {})
    changed = 0
    for domain_id in domain_ids:
        info = domains.get(domain_id)
        if not isinstance(info, dict) or domain_id not in dep_domains:
            continue
        if info.get("relations_status") != "derived":
            info["relations_status"] = "derived"
            changed += 1
    return changed


# === Семантические рёбра из анализа доменов (Фаза 1, dependency) ===

# Ключи объекта-ссылки, которые считаются путём/именем файла. title и name не годятся:
# заголовок заметки - не путь, и по совпадению строки связь выводить нельзя.
REF_KEYS = ("path", "node_id", "file", "target")


def extract_dependency_ref(item):
    """
    Элемент dependencies анализа: строка (REFERENCE.md, "Поля анализа файла") или,
    на всякий случай, объект. Вернуть строку-ссылку или None.
    """
    if isinstance(item, str):
        return item.strip() or None
    if isinstance(item, dict):
        for key in REF_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def build_basename_index(nodes: dict) -> dict:
    """basename -> [node_id] по всем узлам kind="file"."""
    index = {}
    for node_id, node in nodes.items():
        if node.get("kind") == "file":
            index.setdefault(Path(node_id).name, []).append(node_id)
    return index


def resolve_file_ref(ref: str, source_id: str, nodes: dict, basename_index: dict):
    """
    Резолв ссылки анализа в node_id файла графа. Порядок: точное совпадение node_id
    (с .md и без), затем basename. Неоднозначность не угадывается.
    Ссылка с папкой (Old/Note.md) - это утверждение о пути: она должна совпасть с
    хвостом node_id целиком, иначе не резолвится (basename другой папки не подходит).
    Если среди кандидатов есть сам источник и есть другие файлы с тем же именем,
    ссылка неоднозначна (на себя или на другой файл - неизвестно).
    Возвращает (target_id | None, candidates, status), где status:
      "ok"        - цель принята;
      "self"      - единственный кандидат - сам файл, ребро не создаётся;
      "none"      - файла с таким путём/именем в графе нет;
      "ambiguous" - кандидатов несколько и ни один не выделяется доменом.
    """
    ref = ref.replace("\\", "/").strip()
    variants = [ref] if ref.lower().endswith(".md") else [ref, ref + ".md"]

    for variant in variants:
        if nodes.get(variant, {}).get("kind") == "file":
            if variant == source_id:
                return None, [], "self"
            return variant, [variant], "ok"

    full = variants[-1]
    pool = basename_index.get(Path(full).name, [])
    if "/" in ref:
        cands = [c for c in pool if c == full or c.endswith("/" + full)]
    else:
        cands = list(pool)
    if not cands:
        return None, [], "none"
    if cands == [source_id]:
        return None, [], "self"
    if source_id in cands and len(cands) > 1:
        return None, cands, "ambiguous"

    # Цель в том же домене приоритетнее; ровно одна цель в графе - принимаем.
    source_domain = nodes.get(source_id, {}).get("domain_id")
    same = [c for c in cands if nodes[c].get("domain_id") == source_domain]
    if len(same) == 1:
        return same[0], cands, "ok"
    if len(cands) == 1:
        return cands[0], cands, "ok"
    return None, same or cands, "ambiguous"


def seed_edges_from_analysis(graph: dict, temp_dir: Path, domain_analyses_or_path,
                             only_sources=None) -> dict:
    """
    Фаза 1: построить рёбра type="dependency" из поля dependencies каждого файла
    {domain}-analysis.json. mentions в рёбра НЕ превращаются (упоминание не утверждение
    о зависимости): они сохраняются в узле файла (build_file_node).

    domain_analyses_or_path: dict domain_id -> {path -> file_info} (как из
    load_new_domain_analyses), путь к папке с анализами или None (тогда temp_dir).
    only_sources: None - все файлы с анализом (полный режим); множество node_id -
    только эти файлы (инкрементально: новые и изменённые со свежим анализом). Для
    них прежние рёбра dependency пересобираются, остальные рёбра не трогаются.
    Дубликаты рёбер не создаются, самоссылки пропускаются, неоднозначные и
    ненайденные ссылки не дают ребра и попадают в stderr. Проставляет
    graph["domains"][d]["relations_status"] для обработанных доменов:
      "derived"                 - есть рёбра dependency;
      "dependencies_unresolved" - ссылки были, но ни одна не разрешилась в граф;
      "no_dependencies_found"   - анализ не содержит зависимостей.
    Возвращает статистику {"added", "ambiguous", "unresolved", "unknown_source"}.
    """
    if domain_analyses_or_path is None:
        domain_analyses = load_new_domain_analyses(temp_dir)
    elif isinstance(domain_analyses_or_path, dict):
        domain_analyses = domain_analyses_or_path
    else:
        domain_analyses = load_new_domain_analyses(Path(domain_analyses_or_path))

    nodes = graph.get("nodes", {})
    edges = graph.setdefault("edges", [])
    domains = graph.setdefault("domains", {})
    next_revision = graph.get("revision", 0) + 1
    basename_index = build_basename_index(nodes)
    stats = {"added": 0, "ambiguous": 0, "unresolved": 0, "unknown_source": 0}

    # Файлы, для которых пересобираем зависимости: (node_id, domain_id, file_info)
    to_seed = []
    for analysis_domain, by_path in domain_analyses.items():
        for raw_path, file_info in by_path.items():
            if not isinstance(file_info, dict):
                continue
            path = str(raw_path).replace("\\", "/")
            if only_sources is not None and path not in only_sources:
                continue
            if path not in nodes or nodes[path].get("kind") != "file":
                stats["unknown_source"] += 1
                print(f"[WARN] {raw_path}: файла нет в графе (анализ домена {analysis_domain}), "
                      f"его зависимости пропущены", file=sys.stderr)
                continue
            to_seed.append((path, nodes[path].get("domain_id"), file_info))

    # Инкрементально: прежние рёбра dependency этих файлов заменяются свежим анализом
    if only_sources is not None:
        reseed = {p for p, _, _ in to_seed}
        edges[:] = [e for e in edges
                    if not (e.get("type") == "dependency" and e.get("source") in reseed)]
    existing = {(e.get("source"), e.get("target"), e.get("type")) for e in edges}

    unresolved_domains = set()
    processed_domains = set()
    for path, domain_id, file_info in to_seed:
        processed_domains.add(domain_id)
        deps = file_info.get("dependencies") or []
        if not isinstance(deps, list):
            print(f"[WARN] {path}: поле dependencies не является списком, пропущено", file=sys.stderr)
            continue
        for item in deps:
            ref = extract_dependency_ref(item)
            if not ref:
                stats["unresolved"] += 1
                unresolved_domains.add(domain_id)
                print(f"[WARN] {path}: элемент dependencies без ссылки на файл, пропущен: {item!r}",
                      file=sys.stderr)
                continue
            target, cands, status = resolve_file_ref(ref, path, nodes, basename_index)
            if status == "self":
                continue
            if status == "none":
                stats["unresolved"] += 1
                unresolved_domains.add(domain_id)
                print(f"[WARN] {path}: зависимость '{ref}' не найдена в графе, ребро не создано",
                      file=sys.stderr)
                continue
            if status == "ambiguous":
                stats["ambiguous"] += 1
                unresolved_domains.add(domain_id)
                print(f"[WARN] {path}: зависимость '{ref}' неоднозначна, ребро не создано. "
                      f"Кандидаты: {', '.join(cands)}", file=sys.stderr)
                continue
            key = (path, target, "dependency")
            if key in existing:
                continue
            existing.add(key)
            edges.append({
                "source": path, "target": target, "type": "dependency",
                "added_in_revision": next_revision,
            })
            stats["added"] += 1

    derived_domains = {
        nodes[e["source"]].get("domain_id") for e in edges
        if e.get("type") == "dependency" and e.get("source") in nodes
    }
    status_domains = set(domain_analyses) if only_sources is None else processed_domains
    for domain_id in status_domains:
        if domain_id not in domains or not isinstance(domains[domain_id], dict):
            continue
        if domain_id in derived_domains:
            status = "derived"
        elif domain_id in unresolved_domains:
            status = "dependencies_unresolved"
        else:
            status = "no_dependencies_found"
        domains[domain_id]["relations_status"] = status

    return stats


def collect_relevance_required(new_file_ids: list, new_nodes: dict, domain_analyses: dict) -> list:
    """
    Фаза 3: из новых файлов оставить только те, для которых нет свежего анализа
    своего домена (по path). Для остальных связи уже посчитаны Фазой 2.
    """
    required = []
    for node_id in new_file_ids:
        domain_id = new_nodes.get(node_id, {}).get("domain_id")
        if node_id not in domain_analyses.get(domain_id, {}):
            required.append(node_id)
    return required


# === Точки входа для vault-relevance-finder (Фаза 2) ===

def read_flag_value(flag: str):
    """Путь из аргумента '--flag <путь>'. None, если флага нет. ValueError, если путь не задан."""
    if flag not in sys.argv:
        return None
    idx = sys.argv.index(flag)
    if idx + 1 >= len(sys.argv) or sys.argv[idx + 1].startswith("--"):
        raise ValueError(f"флаг {flag} требует путь к файлу")
    return Path(sys.argv[idx + 1])


def run_relevance_requests(temp_dir: Path, out_path: Path) -> int:
    """--relevance-requests: записать входы vault-relevance-finder для relevance_required."""
    dirty = load_json(temp_dir / "graph-dirty-domains.json", None)
    if not (temp_dir / "topic-graph.json").exists() or not isinstance(dirty, dict):
        print("[ERROR] Нет topic-graph.json или graph-dirty-domains.json: "
              "сначала запустите build_topic_graph.py", file=sys.stderr)
        return 1

    graph = load_graph(temp_dir, full_mode=False)
    requests = {}
    for node_id in dirty.get("relevance_required", []) or []:
        node = graph["nodes"].get(node_id)
        if not node or node.get("kind") != "file":
            print(f"[WARN] {node_id}: нет узла файла в графе, запрос не собран", file=sys.stderr)
            continue
        requests[node_id] = build_relevance_request(graph, node_id)

    save_json(out_path, {"requests": requests})
    print(f"[OK] Запросов для vault-relevance-finder: {len(requests)} -> {out_path}", file=sys.stderr)
    return 0


def run_apply_relevance(temp_dir: Path, results_path: Path) -> int:
    """--apply-relevance: применить ответы vault-relevance-finder к графу (идемпотентно)."""
    if not results_path.exists():
        print(f"[ERROR] Файл ответа не найден: {results_path}", file=sys.stderr)
        return 1
    try:
        with open(results_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        print(f"[ERROR] Не удалось прочитать ответ vault-relevance-finder ({results_path.name}): {e}",
              file=sys.stderr)
        return 1

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, dict):
        print("[ERROR] В файле ответа нет объекта 'results'", file=sys.stderr)
        return 1
    if not (temp_dir / "topic-graph.json").exists():
        print("[ERROR] topic-graph.json не найден: сначала запустите build_topic_graph.py", file=sys.stderr)
        return 1

    graph = load_graph(temp_dir, full_mode=False)
    nodes = graph.get("nodes", {})
    added = 0
    touched_domains = set()
    for node_id, result in results.items():
        node = nodes.get(node_id)
        if not node or node.get("kind") != "file":
            print(f"[WARN] {node_id}: нет такого файла в графе, ответ пропущен", file=sys.stderr)
            continue
        touched_domains.add(node.get("domain_id"))
        before = len(graph["edges"])
        for w in apply_relevance_result(graph, node_id, result):
            print(f"[WARN] {w}", file=sys.stderr)
        added += len(graph["edges"]) - before

    status_changed = refresh_relations_status(graph, touched_domains)
    if added == 0 and status_changed == 0:
        print("[INFO] Новых рёбер нет, граф не изменён (revision прежний)", file=sys.stderr)
        return 0

    graph = save_graph(temp_dir, graph)
    append_revision_log(temp_dir, graph["revision"], [])
    print(f"[OK] Добавлено рёбер: {added}, доменов со статусом derived: {status_changed}, "
          f"revision={graph['revision']}", file=sys.stderr)
    return 0


def main():
    full_mode = "--full" in sys.argv

    try:
        requests_out = read_flag_value("--relevance-requests")
        apply_in = read_flag_value("--apply-relevance")
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 2
    if requests_out is not None and apply_in is not None:
        print("[ERROR] Укажите один из флагов: --relevance-requests или --apply-relevance", file=sys.stderr)
        return 2
    if (requests_out is not None or apply_in is not None) and full_mode:
        print("[ERROR] --full нельзя совмещать с --relevance-requests / --apply-relevance", file=sys.stderr)
        return 2

    project_root = get_project_root()
    temp_dir = project_root / ".claude" / "temp_files"

    if requests_out is not None:
        return run_relevance_requests(temp_dir, requests_out)
    if apply_in is not None:
        return run_apply_relevance(temp_dir, apply_in)

    print(f"[INFO] Корень проекта: {project_root}", file=sys.stderr)
    print(f"[INFO] Режим: {'FULL (--full)' if full_mode else 'INCREMENTAL'}", file=sys.stderr)

    graph = load_graph(temp_dir, full_mode)
    # Статусы связей прежнего графа переносятся доменам, которые seed не переопределил
    old_statuses = {
        d: info.get("relations_status") for d, info in (graph.get("domains") or {}).items()
        if isinstance(info, dict) and info.get("relations_status")
    }

    (new_nodes, new_edges, new_domains, new_file_ids, changed_file_ids,
     dirty_domains, dirty_topics) = build_graph_state(temp_dir, graph)

    graph["nodes"] = new_nodes
    graph["edges"] = new_edges
    graph["domains"] = new_domains

    domain_analyses = load_new_domain_analyses(temp_dir)

    # Фаза 1: рёбра dependency из анализов доменов - до validate_graph.
    # Полный режим - все файлы с анализом. Инкрементально - только новые и изменённые
    # файлы со свежим анализом: их связи иначе никто не посчитает. Остальные рёбра
    # переносятся из прошлого графа без изменений.
    if full_mode:
        stats = seed_edges_from_analysis(graph, temp_dir, domain_analyses)
    else:
        stats = seed_edges_from_analysis(graph, temp_dir, domain_analyses,
                                         only_sources=set(new_file_ids) | set(changed_file_ids))
    print(f"[OK] Рёбра dependency из анализа доменов: добавлено {stats['added']}, "
          f"неоднозначных {stats['ambiguous']}, без цели в графе {stats['unresolved']}, "
          f"файлов вне графа {stats['unknown_source']}", file=sys.stderr)

    for domain_id, info in graph["domains"].items():
        if not info.get("relations_status"):
            info["relations_status"] = old_statuses.get(domain_id, "not_derived")

    warnings = validate_graph(graph)
    for w in warnings:
        print(f"[WARN] {w}", file=sys.stderr)

    cycles = has_dependency_cycle(graph)
    for c in cycles:
        print(f"[WARN] {c}", file=sys.stderr)
    if not cycles:
        print("[OK] Циклов dependency не найдено", file=sys.stderr)

    graph = save_graph(temp_dir, graph)
    append_revision_log(temp_dir, graph["revision"], dirty_domains)

    relevance_required = collect_relevance_required(new_file_ids, new_nodes, domain_analyses)
    save_dirty_domains(temp_dir, graph["revision"], dirty_domains, dirty_topics,
                        new_file_ids, changed_file_ids, relevance_required)

    print(f"[OK] Граф обновлён: revision={graph['revision']}, "
          f"узлов={len(graph['nodes'])}, рёбер={len(graph['edges'])}", file=sys.stderr)
    print(f"[OK] Dirty-домены: {len(dirty_domains)}, dirty-темы: {len(dirty_topics)}, "
          f"новых файлов: {len(new_file_ids)}, изменённых: {len(changed_file_ids)}", file=sys.stderr)
    print(f"[INFO] Файлов, требующих вызова vault-relevance-finder: {len(relevance_required)}",
          file=sys.stderr)
    if not relevance_required and new_file_ids:
        print(f"[OK] Все новые файлы ({len(new_file_ids)}) покрыты анализом Фазы 2, "
              f"вызов vault-relevance-finder не нужен", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
