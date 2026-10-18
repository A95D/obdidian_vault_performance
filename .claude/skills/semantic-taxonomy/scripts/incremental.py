#!/usr/bin/env python3
"""
Единый драйвер инкремента: вызывает существующие скрипты подпроцессом, проверяет
коды выхода и входные файлы, останавливается на первой ошибке. LLM не вызывает.

Подкоманды:
  prepare        scan дельты -> domain-cards.json (если дельта есть)
  apply          patch.json -> domain-map.json, затем collect дельты
  reassess-input проверка анализов доменов -> reassess-input/*.json
  finalize       validate всех доменов -> граф -> merge -> verify
  status         только чтение: какой шаг следующий

Использование:
  python incremental.py <подкоманда> [--temp-dir DIR] [...]

Код выхода: 0 - успех, 1 - ошибка шага, 2 - нет входа или не хватает анализов.
cleanup.py драйвер не запускает: очистка необратима, её вызывает пользователь.
"""

import sys
import io
import json
import argparse
import subprocess
from pathlib import Path

if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

SCAN_FILENAME = "vault-scan.json"
CARDS_FILENAME = "domain-cards.json"
PATCH_FILENAME = "patch.json"
DOMAIN_MAP_FILENAME = "domain-map.json"
AFFECTED_FILENAME = "affected-domains.json"
DIRTY_FILENAME = "graph-dirty-domains.json"
REASSESS_DIRNAME = "reassess-input"

STATUS_NEW = "new"
STATUS_CHANGED = "changed"

SCRIPT_COLLECT = "collect_vault_structure.py"
SCRIPT_CARDS = "build_domain_cards.py"
SCRIPT_PATCH = "apply_domain_patch.py"
SCRIPT_REASSESS = "build_reassess_input.py"
SCRIPT_VALIDATE = "validate_reassessed_topics.py"
SCRIPT_GRAPH = "build_topic_graph.py"
SCRIPT_SYNTH = "synthesize_taxonomy.py"


def get_project_root():
    """Определить корень проекта по расположению скрипта."""
    return Path(__file__).resolve().parents[4]


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_temp_dir(args) -> Path:
    if args.temp_dir:
        return Path(args.temp_dir)
    return get_project_root() / ".claude" / "temp_files"


def err(msg):
    print("[ОШИБКА] " + msg, file=sys.stderr)


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
    code = run_script(SCRIPT_CARDS, temp_args(temp_dir), runner)
    if code != 0:
        err("build_domain_cards завершился с кодом " + str(code))
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
    code = run_script(SCRIPT_PATCH, patch_args, runner)
    if code != 0:
        err("apply_domain_patch завершился с кодом " + str(code))
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
    code = run_script(SCRIPT_REASSESS, temp_args(temp_dir), runner)
    if code != 0:
        err("build_reassess_input завершился с кодом " + str(code))
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
        code = run_script(SCRIPT_VALIDATE, ["--domain", e["domain_id"]] + temp_args(temp_dir), runner)
        if code != 0:
            err("validate_reassessed_topics: " + e["domain_id"] + " код " + str(code))
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
    parser = argparse.ArgumentParser(description="Драйвер инкремента Obsidian Vault Performance")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--temp-dir", help="директория temp_files (по умолчанию .claude/temp_files проекта)")
        p.set_defaults(func=func)
        return p

    add("prepare", cmd_prepare, "scan дельты и карточки доменов")
    p = add("apply", cmd_apply, "применить patch.json и собрать дельту")
    p.add_argument("--patch", help="файл патча (пробрасывается в apply_domain_patch)")
    p.add_argument("--dry-run", action="store_true", help="только проверить патч")
    add("reassess-input", cmd_reassess_input, "входы переоценки тем")
    p = add("finalize", cmd_finalize, "validate, граф, merge, verify")
    p.add_argument("--dry-run", action="store_true", help="merge без записи, без verify")
    add("status", cmd_status, "показать следующий шаг")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
