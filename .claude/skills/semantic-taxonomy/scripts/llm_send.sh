#!/usr/bin/env bash
# Отправка пакета файлов в песочницу llm одной функцией (инвариант плана
# "согласованный инкремент"). Ручные циклы отправки в прогонах запрещены.
#
# Использование (source или прямой запуск):
#   source scripts/llm_send.sh
#   llm_send --root VAULT_DIR [--scan vault-scan.json] [--extra FILE]...
#   llm_send --root VAULT_DIR --paths СПИСОК.txt [--extra FILE]...
#
#   --scan FILE   список берётся из поля path файлов со status new|changed
#                 (по умолчанию .claude/temp_files/vault-scan.json)
#   --paths FILE  явный список путей, по одному на строку (вместо скана)
#   --root DIR    корень vault, к которому относятся пути (по умолчанию поле vault_path скана)
#   --extra FILE  дополнительный файл (ТЗ, карточки), путь как есть; можно несколько раз
#
# Не вызывает llm clean: очистка песочницы - один раз за прогон, до первого put.
# Ошибки пишутся в .claude/temp_files/llm_send.log. Код выхода: 0 - число файлов
# в llm ls совпало с ожидаемым, 1 - ошибка отправки или расхождение, 2 - неверные аргументы.

LLM_SEND_LOG="${LLM_SEND_LOG:-.claude/temp_files/llm_send.log}"

llm_send() {
    local scan=".claude/temp_files/vault-scan.json" root="" paths_file="" extras=() py
    while [ $# -gt 0 ]; do
        case "$1" in
            --scan) scan="$2"; shift 2 ;;
            --paths) paths_file="$2"; shift 2 ;;
            --root) root="$2"; shift 2 ;;
            --extra) extras+=("$2"); shift 2 ;;
            *) echo "[ОШИБКА] llm_send: неизвестный аргумент $1" >&2; return 2 ;;
        esac
    done
    py="$(command -v python3 || command -v python)"
    if [ -z "$py" ]; then echo "[ОШИБКА] llm_send: нет python" >&2; return 2; fi
    mkdir -p "$(dirname "$LLM_SEND_LOG")"

    local list; list="$(mktemp)"
    if [ -n "$paths_file" ]; then
        [ -f "$paths_file" ] || { echo "[ОШИБКА] llm_send: нет файла списка $paths_file" >&2; rm -f "$list"; return 2; }
        tr -d '\r' < "$paths_file" | sed '/^$/d' > "$list"
        [ -n "$root" ] || { echo "[ОШИБКА] llm_send: с --paths нужен --root" >&2; rm -f "$list"; return 2; }
    else
        [ -f "$scan" ] || { echo "[ОШИБКА] llm_send: нет $scan" >&2; rm -f "$list"; return 2; }
        # Список пишется с newline="\n": на Windows текстовый режим даёт \r\n, а \r ломает llm put.
        "$py" - "$scan" "$list" <<'PY' || { rm -f "$list"; return 2; }
import io, json, sys
scan = json.load(open(sys.argv[1], encoding="utf-8"))
paths = [f["path"] for f in scan.get("files", []) if f.get("status") in ("new", "changed")]
with io.open(sys.argv[2], "w", encoding="utf-8", newline="\n") as out:
    for p in paths:
        out.write(p + "\n")
PY
        if [ -z "$root" ]; then
            root="$("$py" -c 'import json,sys; print(json.load(open(sys.argv[1],encoding="utf-8")).get("vault_path",""))' "$scan" | tr -d '\r')"
        fi
        [ -n "$root" ] || { echo "[ОШИБКА] llm_send: нет vault_path, задай --root" >&2; rm -f "$list"; return 2; }
    fi
    tr -d '\r' < "$list" > "$list.lf" && mv -f "$list.lf" "$list"

    local count extra_count=${#extras[@]} expected
    count="$(grep -c . "$list")"
    if [ "$count" -eq 0 ]; then
        echo "[ОШИБКА] llm_send: список путей пуст (нет new|changed)" >&2; rm -f "$list"; return 1
    fi
    # llm put кладёт файлы плоско, под basename: одинаковые имена перезатрут друг друга.
    local all_names dup
    all_names="$( { sed 's|.*/||' "$list"; for e in "${extras[@]}"; do basename "$e"; done; } )"
    dup="$(printf '%s\n' "$all_names" | sort | uniq -d | head -3)"
    if [ -n "$dup" ]; then
        echo "[ОШИБКА] llm_send: одинаковые basename, отправка остановлена: $dup" >&2; rm -f "$list"; return 1
    fi

    local failed=0 f
    # Список читается через дескриптор 3, а в llm put он закрыт (3<&-), stdin - /dev/null.
    while IFS= read -r f <&3; do
        if ! llm put "$root/$f" </dev/null 3<&- >> "$LLM_SEND_LOG" 2>&1; then
            echo "[ОШИБКА] llm put не удался: $f (подробности в $LLM_SEND_LOG)" >&2
            echo "[ОШИБКА] llm put: $f" >> "$LLM_SEND_LOG"
            failed=$((failed + 1))
        fi
    done 3< "$list"
    for f in "${extras[@]}"; do
        if ! llm put "$f" </dev/null 3<&- >> "$LLM_SEND_LOG" 2>&1; then
            echo "[ОШИБКА] llm put не удался: $f (подробности в $LLM_SEND_LOG)" >&2
            echo "[ОШИБКА] llm put: $f" >> "$LLM_SEND_LOG"
            failed=$((failed + 1))
        fi
    done
    rm -f "$list"

    expected=$((count + extra_count))
    local actual
    # llm ls печатает формат ls -la: строки "total", "." и ".." не файлы, имя - с 9-го поля (бывают пробелы).
    actual="$(llm ls </dev/null 3<&- 2>> "$LLM_SEND_LOG" | tr -d '\r' | grep -E '^[-dl]' | awk '{ for (i = 1; i <= 8; i++) $i = ""; sub(/^ +/, ""); if ($0 != "." && $0 != "..") n++ } END { print n + 0 }')"
    actual="$(echo "$actual" | tr -d ' ')"
    if [ "$failed" -ne 0 ] || [ "$actual" -ne "$expected" ]; then
        echo "[ОШИБКА] llm_send: ожидалось файлов $expected, в llm ls $actual, неудачных put $failed" >&2
        echo "[ОШИБКА] расхождение: ожидалось $expected, llm ls $actual, неудачных put $failed" >> "$LLM_SEND_LOG"
        return 1
    fi
    echo "[OK] llm_send: отправлено $expected (дельта $count, доп. $extra_count), llm ls совпал"
    return 0
}

# Прямой запуск: ./llm_send.sh --root DIR ...
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    llm_send "$@"
fi
