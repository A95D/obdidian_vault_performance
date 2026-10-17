#!/usr/bin/env bash
# llm_fetch.sh <удалённый путь> <папка назначения>
# Забирает файл из песочницы llm в папку назначения. llm get кладёт файл
# относительно текущей папки, поэтому работаем во временной папке и не
# засоряем корень проекта.
set -euo pipefail
REMOTE="${1:?укажи удалённый путь, например llm-phase2/data-vault-analysis.json}"
DEST="${2:?укажи папку назначения}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

( cd "$TMP" && llm get "$REMOTE" </dev/null ) >&2 || { echo "llm_fetch: не удалось забрать $REMOTE" >&2; exit 1; }
SRC="$TMP/$REMOTE"
[ -f "$SRC" ] || { echo "llm_fetch: файла нет в песочнице: $REMOTE" >&2; exit 1; }
mkdir -p "$DEST"
mv -f "$SRC" "$DEST/$(basename "$REMOTE")"
echo "llm_fetch: $REMOTE -> $DEST/$(basename "$REMOTE")" >&2
