#!/usr/bin/env python3
"""
Слияние результатов классификации тем и построение семантических кластеров
(Фаза 0, шаг 3 из 3).

Только механика, без LLM: читает clustering-batches.json (список батчей +
файлы, обслуженные из кеша), читает batch-{batch_id}-topics.json для каждого
батча (результат работы субагента vault-topic-classifier), обновляет кеш
анализа по content-hash и строит кластеры.

Вход:
  .claude/temp_files/clustering-batches.json
  .claude/temp_files/batch-{batch_id}-topics.json (по одному на батч)
  .claude/temp_files/semantic-analysis-cache.json (кеш анализа тем)

Выход: .claude/temp_files/vault-clusters.json
"""

import json
import re
import sys
import io
import time
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

# Установить UTF-8 кодировку для вывода (защита от ошибок на Windows)
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

CONFIDENCE_THRESHOLD = 0.5
ALGORITHM_VERSION = "2.0.0"
CACHE_FILENAME = "semantic-analysis-cache.json"
BATCHES_FILENAME = "clustering-batches.json"
OUTPUT_FILENAME = "vault-clusters.json"


def get_project_root():
    """Определить корень проекта по расположению скрипта."""
    # Скрипт находится в: .claude/skills/semantic-taxonomy/scripts/
    return Path(__file__).resolve().parents[4]


def load_json(path: Path, required: bool = True) -> dict:
    """Загрузить JSON файл. При отсутствии обязательного файла - исключение."""
    if not path.exists():
        if required:
            raise FileNotFoundError(f"Файл не найден: {path}")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_cache(cache_path: Path) -> dict:
    """Загрузить кеш результатов анализа. Повреждённый кеш игнорируется."""
    if not cache_path.exists():
        return {}
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[WARN] Не удалось прочитать кеш анализа, начинаем с пустого: {e}", file=sys.stderr)
        return {}


def save_cache(cache_path: Path, cache: dict) -> None:
    """Сохранить кеш результатов анализа."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def collect_prefiltered_noise(batches_data: dict):
    """Файлы, отсеянные эвристикой (Ступень 1) в prepare_clustering_batches.py."""
    return [
        {"path": f["path"], "reason": f["reason"], "stage": "prefilter"}
        for f in batches_data.get("prefiltered_files", [])
    ]


def collect_analyses(temp_dir: Path, batches_data: dict, cache: dict):
    """
    Собрать финальный список анализов файлов: из batch-*-topics.json (новые/
    изменившиеся файлы) + из кеша (файлы с неизменившимся content_hash).

    Возвращает (analyses, unclassified, noise_files, new_analyses_count).
    Файлы, помеченные классификатором как read_error/empty_or_too_short,
    считаются шумом (noise_files), а не unclassified - это не "валидный
    контент без пары", а брак, который нужно полностью исключить из доменов.
    """
    analyses = []
    unclassified = []
    noise_files = []
    new_analyses_count = 0

    path_to_hash = {}
    for batch in batches_data.get("batches", []):
        for file_entry in batch.get("files", []):
            path_to_hash[file_entry["path"]] = file_entry["content_hash"]

    for batch in batches_data.get("batches", []):
        batch_id = batch["batch_id"]
        topics_path = temp_dir / f"batch-{batch_id}-topics.json"
        try:
            topics_data = load_json(topics_path, required=True)
        except FileNotFoundError as e:
            print(f"[WARN] Результат классификации батча {batch_id} не найден: {e}", file=sys.stderr)
            for file_entry in batch.get("files", []):
                unclassified.append({
                    "path": file_entry["path"],
                    "reason": "other",
                    "details": f"batch_result_missing: {batch_id}",
                })
            continue

        for file_result in topics_data.get("files", []):
            path = file_result["path"]
            analysis = {
                "primary_topic": file_result["primary_topic"],
                "secondary_topics": file_result.get("secondary_topics", []),
                "key_concepts": file_result.get("key_concepts", []),
                "confidence_score": float(file_result.get("confidence_score", 0.0)),
            }
            analyses.append({"path": path, **analysis})
            new_analyses_count += 1

            content_hash = path_to_hash.get(path)
            if content_hash:
                cache[content_hash] = analysis

        for skipped in topics_data.get("skipped", []):
            noise_files.append({
                "path": skipped["path"],
                "reason": skipped.get("reason", "classification_skipped"),
                "stage": "classifier",
            })

    for cached_file in batches_data.get("cached_files", []):
        content_hash = cached_file["content_hash"]
        if content_hash in cache:
            analysis = cache[content_hash]
            analyses.append({"path": cached_file["path"], **analysis})
        else:
            unclassified.append({
                "path": cached_file["path"],
                "reason": "other",
                "details": "cache_entry_missing_unexpectedly",
            })

    return analyses, unclassified, noise_files, new_analyses_count


def normalize_cluster_id(topic: str) -> str:
    """Привести тему к формату cluster_id (^[a-z0-9\\-]+$)."""
    slug = topic.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug or "misc"


def jaccard_similarity(a: set, b: set) -> float:
    """Мера сходства двух множеств слов (для сопоставления multi-topic файлов)."""
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


def tokenize(text: str) -> set:
    return {w.lower() for w in text.split() if len(w) > 2}


def build_clusters(analyses, threshold: float = CONFIDENCE_THRESHOLD):
    """
    Сгруппировать проанализированные файлы в семантические кластеры по primary_topic.

    Файлы с низкой средней уверенностью темы помечаются как unclassified
    (reason="confidence_too_low"). Multi-topic файлы, не попавшие в основную
    группу, пытаются присоединиться по secondary_topics
    (reason="no_semantic_match" при неудаче).
    """
    topic_groups = defaultdict(list)
    for analysis in analyses:
        topic_groups[analysis["primary_topic"].lower()].append(analysis)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    clusters = {}
    unclassified = []
    assigned_paths = set()

    for topic, group in topic_groups.items():
        avg_confidence = sum(f["confidence_score"] for f in group) / len(group)

        if avg_confidence < threshold:
            for f in group:
                unclassified.append({
                    "path": f["path"],
                    "reason": "confidence_too_low",
                    "details": f"confidence={f['confidence_score']:.2f}",
                })
            continue

        cluster_id = normalize_cluster_id(topic)
        base_id = cluster_id
        suffix = 2
        while cluster_id in clusters:
            cluster_id = f"{base_id}-{suffix}"
            suffix += 1

        cluster_name = topic.title()[:100]

        all_concepts = set()
        for f in group:
            all_concepts.update(f["key_concepts"])
        theme_description = ", ".join(sorted(all_concepts)[:10])

        cluster_files = []
        for f in group:
            confidence = min(1.0, f["confidence_score"] * 1.1)
            cluster_files.append({
                "path": f["path"],
                "topic": f["primary_topic"],
                "confidence": round(confidence, 4),
            })
            assigned_paths.add(f["path"])

        representative = max(cluster_files, key=lambda cf: cf["confidence"])["path"]

        clusters[cluster_id] = {
            "cluster_id": cluster_id,
            "cluster_name": cluster_name,
            "theme_description": theme_description,
            "files": cluster_files,
            "representative_file": representative,
            "created_date": now,
            "last_updated": now,
            "metadata": {
                "count": len(cluster_files),
                "average_confidence": round(avg_confidence, 4),
                "representative_concepts": sorted(all_concepts)[:5],
            },
        }

    # Multi-topic файлы, не попавшие ни в один кластер по primary_topic,
    # пытаются присоединиться по пересечению secondary_topics с темой кластера
    for analysis in analyses:
        if analysis["path"] in assigned_paths:
            continue
        if any(u["path"] == analysis["path"] for u in unclassified):
            continue

        best_cluster_id = None
        best_score = 0.0
        secondary_tokens = set()
        for topic in analysis.get("secondary_topics", []):
            secondary_tokens |= tokenize(topic)

        for cluster_id, cluster in clusters.items():
            cluster_tokens = tokenize(cluster["theme_description"]) | tokenize(cluster["cluster_name"])
            score = jaccard_similarity(secondary_tokens, cluster_tokens)
            if score > best_score:
                best_score = score
                best_cluster_id = cluster_id

        if best_cluster_id and best_score >= threshold:
            cluster = clusters[best_cluster_id]
            confidence = round(best_score, 4)
            cluster["files"].append({
                "path": analysis["path"],
                "topic": analysis["primary_topic"],
                "confidence": confidence,
            })
            cluster["metadata"]["count"] = len(cluster["files"])
            assigned_paths.add(analysis["path"])
        else:
            unclassified.append({
                "path": analysis["path"],
                "reason": "no_semantic_match",
                "details": f"best_match_score={best_score:.2f}",
            })

    return list(clusters.values()), unclassified


def format_result(clusters, unclassified, noise_files, vault_path: str, total_files: int,
                   processing_time_seconds: float, new_analyses_count: int, cache_hits: int) -> dict:
    """Собрать финальный dict под контракт OUTPUT-clustering-result.json."""
    clustered_files = sum(len(c["files"]) for c in clusters)
    unclassified_count = len(unclassified)
    noise_count = len(noise_files)

    all_confidences = [f["confidence"] for c in clusters for f in c["files"]]

    statistics = {
        "total_files": total_files,
        "clustered_files": clustered_files,
        "unclassified_count": unclassified_count,
        "noise_count": noise_count,
        "cluster_count": len(clusters),
        "average_cluster_size": (clustered_files / len(clusters)) if clusters else 0,
        "largest_cluster": max(clusters, key=lambda c: len(c["files"]))["cluster_id"] if clusters else None,
        "smallest_cluster": min(clusters, key=lambda c: len(c["files"]))["cluster_id"] if clusters else None,
    }

    quality_metrics = {
        "average_confidence": (sum(all_confidences) / len(all_confidences)) if all_confidences else 0,
        "coverage_percent": (clustered_files / total_files * 100) if total_files > 0 else 0,
        "min_confidence": min(all_confidences) if all_confidences else 0,
        "max_confidence": max(all_confidences) if all_confidences else 0,
    }

    metadata = {
        "vault_path": str(vault_path),
        "analysis_date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "algorithm_version": ALGORITHM_VERSION,
        "input_file_count": total_files,
        "processing_time_seconds": round(processing_time_seconds, 2),
        "new_analyses_count": new_analyses_count,
        "cache_hits": cache_hits,
    }

    return {
        "clusters": clusters,
        "unclassified_files": unclassified,
        "noise_files": noise_files,
        "statistics": statistics,
        "quality_metrics": quality_metrics,
        "metadata": metadata,
    }


def main():
    """Основной поток: Фаза 0, шаг 3 - слияние результатов и построение кластеров."""
    try:
        start_time = time.time()
        project_root = get_project_root()
        temp_dir = project_root / ".claude" / "temp_files"

        print(f"[INFO] Корень проекта: {project_root}", file=sys.stderr)

        batches_path = temp_dir / BATCHES_FILENAME
        print("[STEP 1] Чтение clustering-batches.json...", file=sys.stderr)
        batches_data = load_json(batches_path, required=True)
        total_files = batches_data.get("total_files", 0)

        if total_files == 0:
            print("[INFO] Нет файлов для кластеризации.", file=sys.stderr)
            return 0

        cache_path = temp_dir / CACHE_FILENAME
        cache = load_cache(cache_path)

        print("[STEP 2] Сбор результатов классификации батчей и кеша...", file=sys.stderr)
        analyses, unclassified_from_collection, noise_from_classifier, new_analyses_count = collect_analyses(
            temp_dir, batches_data, cache
        )
        noise_from_prefilter = collect_prefiltered_noise(batches_data)
        cache_hits = len(analyses) - new_analyses_count
        print(
            f"[OK] Собрано анализов: {len(analyses)} "
            f"(новых: {new_analyses_count}, из кеша: {cache_hits})",
            file=sys.stderr,
        )

        save_cache(cache_path, cache)

        print("[STEP 3] Построение семантических кластеров...", file=sys.stderr)
        clusters, unclassified_from_clustering = build_clusters(analyses, CONFIDENCE_THRESHOLD)
        unclassified = unclassified_from_collection + unclassified_from_clustering
        noise_files = noise_from_prefilter + noise_from_classifier
        print(
            f"[OK] Создано {len(clusters)} кластеров, unclassified: {len(unclassified)}, "
            f"шум: {len(noise_files)}",
            file=sys.stderr,
        )

        processing_time = time.time() - start_time

        vault_path = None
        env_file = project_root / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("VAULT_PATH="):
                    vault_path = line.split("=", 1)[1].strip().strip('"\'')
                    break

        result = format_result(
            clusters=clusters,
            unclassified=unclassified,
            noise_files=noise_files,
            vault_path=vault_path or "",
            total_files=total_files,
            processing_time_seconds=processing_time,
            new_analyses_count=new_analyses_count,
            cache_hits=cache_hits,
        )

        output_path = temp_dir / OUTPUT_FILENAME
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        print(f"\n[SUMMARY] Итоги семантической кластеризации:", file=sys.stderr)
        print(f"  - Файлов проанализировано: {total_files}", file=sys.stderr)
        print(f"  - Кластеров создано: {len(clusters)}", file=sys.stderr)
        print(f"  - Unclassified: {len(unclassified)}", file=sys.stderr)
        print(f"  - Шум (исключён из доменов): {len(noise_files)}", file=sys.stderr)
        print(f"  - Coverage: {result['quality_metrics']['coverage_percent']:.1f}%", file=sys.stderr)
        print(f"  - Время: {processing_time:.1f}с", file=sys.stderr)
        print(f"  - Сохранено в: {output_path}", file=sys.stderr)

        return 0

    except (ValueError, FileNotFoundError, NotADirectoryError, PermissionError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[ERROR] Неожиданная ошибка: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
