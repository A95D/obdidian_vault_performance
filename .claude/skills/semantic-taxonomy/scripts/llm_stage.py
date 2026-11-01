#!/usr/bin/env python3
"""Перенос заметок vault в папку песочницы /workspace/agent.

/workspace в песочнице - живое монтирование папки проекта, поэтому файлы копируются
локально, в папку agent/ проекта, без llm put и SSH. Заметки vault лежат вне проекта
и в песочнице не видны, без этого шага их не обработать.

Схема плоская, без подпапок. Имя копии заметки - полный путь к ней внутри vault одной
строкой, разделители пути заменены на FLAT_SEP:
  Data Vault/Bridge Таблицы.md  ->  Data Vault＼Bridge Таблицы.md
Обычный "\" в имени файла Windows не допускает, поэтому взят полноширинный символ.
Файлы --extra (вне vault) копируются под своим именем. Имена копий должны быть уникальны
без учёта регистра: при совпадении копирование не начинается.

Использование:
  python llm_stage.py --root VAULT_DIR [--scan vault-scan.json] [--extra FILE]...
  python llm_stage.py --root VAULT_DIR --paths список.txt [--extra FILE]...
  python llm_stage.py --clean

  --scan FILE   пути берутся из поля path файлов со status new|changed
  --paths FILE  явный список путей относительно --root, по одному на строку (один файл на прогон)
  --root DIR    корень vault (по умолчанию поле vault_path скана)
  --extra FILE  файл вне vault, копируется как есть; можно несколько раз
  --clean       удалить файлы в папке песочницы (подпапки не трогает)
  --dest DIR    папка песочницы (по умолчанию PROJECT/agent = /workspace/agent)

Коды выхода: 0 - успех, 1 - ошибка копирования или расхождение, 2 - неверные аргументы.
"""
import argparse
import hashlib
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[4]
DEFAULT_DEST = PROJECT / "agent"
DEFAULT_SCAN = PROJECT / ".claude" / "temp_files" / "vault-scan.json"
FLAT_SEP = "＼"  # заменяет разделитель пути в имени копии: "\" в имени файла Windows недопустим


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_paths(args):
    if args.paths:
        if not args.root:
            raise ValueError("с --paths нужен --root")
        lines = Path(args.paths).read_text(encoding="utf-8").splitlines()
        return Path(args.root), [line.strip() for line in lines if line.strip()]
    scan = json.loads(Path(args.scan).read_text(encoding="utf-8"))
    root = Path(args.root or scan.get("vault_path", ""))
    if not str(root):
        raise ValueError("нет vault_path в скане, задай --root")
    paths = [f["path"] for f in scan.get("files", []) if f.get("status") in ("new", "changed")]
    return root, paths


def flat_name(rel):
    return FLAT_SEP.join(Path(rel).parts)


def clean(dest):
    if dest.resolve() == PROJECT.resolve():
        print("[ОШИБКА] --clean: папка назначения совпадает с корнем проекта", file=sys.stderr)
        return 2
    if not dest.is_dir():
        print(f"[OK] llm_stage: папки нет, очищать нечего: {dest}")
        return 0
    removed = 0
    for item in dest.iterdir():
        if item.is_file():
            item.unlink()
            removed += 1
    print(f"[OK] llm_stage: удалено файлов {removed} в {dest}")
    return 0


def stage(root, paths, extras, dest):
    if not paths and not extras:
        print("[ОШИБКА] llm_stage: список файлов пуст (нет new|changed)", file=sys.stderr)
        return 1
    outside = [p for p in paths if Path(p).anchor or ".." in Path(p).parts]
    if outside:
        print(f"[ОШИБКА] llm_stage: путь вне vault: {outside[:3]}", file=sys.stderr)
        return 1

    pairs = [(root / p, dest / flat_name(p)) for p in paths]
    pairs += [(Path(e), dest / Path(e).name) for e in extras]
    # Регистр не различается: на Windows Bridge.md и bridge.md - один файл.
    collide = [n for n, c in Counter(t.name.casefold() for _, t in pairs).items() if c > 1]
    if collide:
        print(f"[ОШИБКА] llm_stage: одинаковые имена копий (без учёта регистра): {collide[:3]}", file=sys.stderr)
        return 1
    missing = [str(s) for s, _ in pairs if not s.is_file()]
    if missing:
        print(f"[ОШИБКА] llm_stage: нет файлов: {missing[:3]}", file=sys.stderr)
        return 1

    dest.mkdir(parents=True, exist_ok=True)
    failed = []
    for src, target in pairs:
        try:
            shutil.copy2(src, target)
            if sha256(src) != sha256(target):
                failed.append(src.name)
        except OSError as err:
            failed.append(f"{src.name} ({err})")
    if failed:
        print(f"[ОШИБКА] llm_stage: не скопировано или не совпал хеш: {failed[:3]}", file=sys.stderr)
        return 1

    # Арифметика: в папке должны лежать ровно наши копии, ни меньше, ни больше.
    expected = {t.name for _, t in pairs}
    actual = {item.name for item in dest.iterdir() if item.is_file()}
    lost = sorted(expected - actual)
    stale = sorted(actual - expected)
    if lost or stale:
        print(f"[ОШИБКА] llm_stage: счёт не сошёлся в {dest}: копий {len(pairs)}, файлов {len(actual)}, "
              f"пропали {lost[:3]}, лишние {stale[:3]} (запусти --clean)", file=sys.stderr)
        return 1
    print(f"[OK] llm_stage: скопировано {len(pairs)} (заметки vault {len(paths)}, доп. {len(extras)}), "
          f"в {dest} файлов: {len(actual)}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Перенос заметок vault в папку песочницы")
    parser.add_argument("--scan", default=str(DEFAULT_SCAN))
    parser.add_argument("--paths")
    parser.add_argument("--root")
    parser.add_argument("--extra", action="append", default=[])
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--dest", default=str(DEFAULT_DEST))
    args = parser.parse_args()
    dest = Path(args.dest)

    if args.clean:
        return clean(dest)
    try:
        root, paths = read_paths(args)
    except (OSError, ValueError, KeyError) as err:
        print(f"[ОШИБКА] llm_stage: {err}", file=sys.stderr)
        return 2
    return stage(root, paths, args.extra, dest)


if __name__ == "__main__":
    sys.exit(main())
