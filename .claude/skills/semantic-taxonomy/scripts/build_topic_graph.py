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
        return empty_graph()
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
        domain_id = file.stem.replace("-analysis", "")
        data = load_json(file, None)
        if not isinstance(data, dict):
            continue
        files = data.get("files", [])
        by_path = {}
        for file_info in files:
            if isinstance(file_info, dict) and "path" in file_info:
                by_path[file_info["path"]] = file_info
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


def composite_topic_id(domain_id: str, local_topic_id: str) -> str:
    return f"{domain_id}::{local_topic_id}"


# === Построение узлов/рёбер файлов ===

def build_file_node(path: str, domain_id: str, content_hash, existing_node,
                     domain_analyses: dict, semantic_cache: dict,
                     path_to_topic: dict, next_revision: int,
                     taxonomy_files_by_path: dict) -> dict:
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
    known_in_taxonomy = path in path_to_topic
    is_new = existing_node is None and not known_in_taxonomy
    is_changed = (not is_new) and existing_node and content_hash and existing_node.get("content_hash") != content_hash

    analysis_entry = domain_analyses.get(domain_id, {}).get(path)

    if analysis_entry:
        essence = analysis_entry.get("essence", "") or ""
        key_concepts = analysis_entry.get("key_concepts", []) or []
        title = analysis_entry.get("title") or Path(path).stem
        file_type = analysis_entry.get("type")
        if isinstance(file_type, list):
            file_type = file_type[0] if file_type else None
    elif existing_node and not is_new and not is_changed:
        essence = existing_node.get("essence", "")
        key_concepts = existing_node.get("key_concepts", [])
        title = existing_node.get("title", Path(path).stem)
        file_type = existing_node.get("type")
    elif not is_new and not is_changed:
        # Bootstrap: файл уже известен taxonomy.json, но ещё не был узлом
        # графа - essence/title/type берём из taxonomy.json (файл домена),
        # а не из Фазы 0.2 (которая отражает лишь предварительную классификацию).
        taxonomy_entry = taxonomy_files_by_path.get(path, {})
        essence = taxonomy_entry.get("essence", "") or ""
        key_concepts = taxonomy_entry.get("key_concepts", []) or []
        title = taxonomy_entry.get("title") or Path(path).stem
        file_type = taxonomy_entry.get("type")
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
                taxonomy_files_by_path,
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

    new_edges = member_of_edges + other_edges

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

    return warnings


def has_dependency_cycle(graph: dict) -> list:
    """
    Проверить отсутствие циклов в рёбрах type="dependency" внутри каждого домена
    (data-model.md, Validation rules для Graph Edge). Возвращает список строк
    с найденными циклами (не фатально - логируется, см. T019).
    """
    nodes = graph.get("nodes", {})
    dep_edges_by_domain = {}
    for edge in graph.get("edges", []):
        if edge.get("type") != "dependency":
            continue
        source_node = nodes.get(edge["source"])
        if not source_node:
            continue
        domain_id = source_node.get("domain_id")
        dep_edges_by_domain.setdefault(domain_id, []).append((edge["source"], edge["target"]))

    issues = []
    for domain_id, edges in dep_edges_by_domain.items():
        adjacency = {}
        for src, tgt in edges:
            adjacency.setdefault(src, []).append(tgt)

        WHITE, GRAY, BLACK = 0, 1, 2
        color = {}

        def visit(node, path):
            color[node] = GRAY
            for nxt in adjacency.get(node, []):
                if color.get(nxt, WHITE) == GRAY:
                    issues.append(f"Цикл dependency в домене {domain_id}: {' -> '.join(path + [nxt])}")
                elif color.get(nxt, WHITE) == WHITE:
                    visit(nxt, path + [nxt])
            color[node] = BLACK

        for node in list(adjacency.keys()):
            if color.get(node, WHITE) == WHITE:
                visit(node, [node])

    return issues


# === Dirty-domains артефакт (T006) ===

def save_dirty_domains(temp_dir: Path, graph_revision: int, dirty_domains: list,
                        dirty_topics: list, new_file_ids: list, changed_file_ids: list) -> None:
    save_json(temp_dir / "graph-dirty-domains.json", {
        "graph_revision": graph_revision,
        "dirty_domains": dirty_domains,
        "dirty_topics": dirty_topics,
        "new_file_ids": new_file_ids,
        "changed_file_ids": changed_file_ids,
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
    valid_ids = set(nodes.keys())
    next_revision = graph.get("revision", 0) + 1

    if not isinstance(result, dict):
        warnings.append(f"{file_node_id}: ответ vault-relevance-finder не является JSON-объектом, пропущен")
        return warnings

    for edge_type, field in (("related", "related"), ("dependency", "dependencies")):
        for target_id in result.get(field, []) or []:
            if target_id not in valid_ids:
                warnings.append(
                    f"{file_node_id}: отброшен несуществующий node_id из поля '{field}': {target_id}"
                )
                continue
            key = (file_node_id, target_id, edge_type)
            if any(e["source"] == file_node_id and e["target"] == target_id and e["type"] == edge_type
                   for e in graph["edges"]):
                continue
            graph["edges"].append({
                "source": file_node_id, "target": target_id, "type": edge_type,
                "added_in_revision": next_revision,
            })

    return warnings


def main():
    full_mode = "--full" in sys.argv

    project_root = get_project_root()
    temp_dir = project_root / ".claude" / "temp_files"

    print(f"[INFO] Корень проекта: {project_root}", file=sys.stderr)
    print(f"[INFO] Режим: {'FULL (--full)' if full_mode else 'INCREMENTAL'}", file=sys.stderr)

    graph = load_graph(temp_dir, full_mode)

    (new_nodes, new_edges, new_domains, new_file_ids, changed_file_ids,
     dirty_domains, dirty_topics) = build_graph_state(temp_dir, graph)

    graph["nodes"] = new_nodes
    graph["edges"] = new_edges
    graph["domains"] = new_domains

    warnings = validate_graph(graph)
    for w in warnings:
        print(f"[WARN] {w}", file=sys.stderr)

    graph = save_graph(temp_dir, graph)
    append_revision_log(temp_dir, graph["revision"], dirty_domains)
    save_dirty_domains(temp_dir, graph["revision"], dirty_domains, dirty_topics,
                        new_file_ids, changed_file_ids)

    print(f"[OK] Граф обновлён: revision={graph['revision']}, "
          f"узлов={len(graph['nodes'])}, рёбер={len(graph['edges'])}", file=sys.stderr)
    print(f"[OK] Dirty-домены: {len(dirty_domains)}, dirty-темы: {len(dirty_topics)}, "
          f"новых файлов: {len(new_file_ids)}, изменённых: {len(changed_file_ids)}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
