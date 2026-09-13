#!/usr/bin/env python3
"""
Очистка временных файлов семантической таксономии.

Два режима работы:
  --full    : полная очистка (шаг 0) — удаляет ВСЁ включая финальные артефакты
  (по умолчанию): частичная очистка — удаляет всё в temp_files кроме финальных артефактов
"""

import sys
import io
from pathlib import Path

# Установить UTF-8 кодировку для вывода (защита от ошибок на Windows)
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

def get_project_root():
    """Определить корень проекта по расположению скрипта."""
    # Скрипт находится в: .claude/skills/semantic-taxonomy/scripts/cleanup.py
    # Корень проекта на 4 уровня выше
    return Path(__file__).resolve().parents[4]

def partial_cleanup(project_root):
    """Частичная очистка — удаляет промежуточные файлы в temp_files, сохраняет финальные артефакты."""
    temp_dir = project_root / ".claude" / "temp_files"

    # Финальные артефакты, которые нужно сохранить
    final_artifacts = {"taxonomy.json"}

    deleted_files = []

    # Удалить все файлы в temp_files кроме финальных артефактов
    if temp_dir.exists():
        try:
            for file_path in temp_dir.iterdir():
                if file_path.is_file() and file_path.name not in final_artifacts:
                    try:
                        file_path.unlink()
                        deleted_files.append(f".claude/temp_files/{file_path.name}")
                    except Exception as e:
                        print(f"Не удалось удалить {file_path.name}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"Не удалось просканировать temp_files: {e}", file=sys.stderr)

    # Вывести результаты
    if deleted_files:
        print(f"[OK] Частичная очистка: удалено {len(deleted_files)} временных файлов")
        for f in sorted(deleted_files):
            print(f"  - {f}")

        # Показать, какие финальные артефакты остались
        remaining = set(f.name for f in temp_dir.iterdir() if f.is_file()) if temp_dir.exists() else set()
        if remaining & final_artifacts:
            print(f"\nФинальные артефакты сохранены: {', '.join(sorted(remaining & final_artifacts))}")
    else:
        print("[OK] Нет временных файлов для очистки.")

def full_cleanup(project_root):
    """Полная очистка (шаг 0) — удаляет ВСЁ в temp_files включая финальные артефакты."""
    temp_dir = project_root / ".claude" / "temp_files"
    deleted_files = []

    # Удалить всё в temp_files
    if temp_dir.exists():
        try:
            for file_path in temp_dir.iterdir():
                if file_path.is_file():
                    try:
                        file_path.unlink()
                        deleted_files.append(f".claude/temp_files/{file_path.name}")
                    except Exception as e:
                        print(f"Не удалось удалить {file_path.name}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"⚠️  Не удалось просканировать temp_files: {e}", file=sys.stderr)

    # Вывести результаты
    if deleted_files:
        print(f"[FULL] Полная очистка (шаг 0): удалено {len(deleted_files)} файлов")
        for f in sorted(deleted_files):
            print(f"  - {f}")
    else:
        print("[OK] Workspace чист.")

def main():
    full_mode = "--full" in sys.argv
    project_root = get_project_root()

    if full_mode:
        full_cleanup(project_root)
    else:
        partial_cleanup(project_root)

if __name__ == "__main__":
    main()
