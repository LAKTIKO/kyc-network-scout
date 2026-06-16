#!/usr/bin/env bash
# tests/stress_test.sh — Фаза 1 стрес-тестів KYC Scout (БЕЗ медіа, дешево).
#
# Проганяє матрицю крайових входів через `docker compose run --rm kyc ...`,
# витягує coverage / score / рівень / error і друкує зведену таблицю.
# Кожен виклик обгорнутий у timeout + захист: один збій не валить матрицю.
#
# Запуск:  bash tests/stress_test.sh
# УВАГА:   усі кейси з --no-media. Медіа (Serper+скрейпер) — це Фаза 2, окремо.

set -u

PER_CASE_TIMEOUT="${PER_CASE_TIMEOUT:-180}"

# macOS не має `timeout` за замовчуванням; пробуємо timeout → gtimeout → без.
TIMEOUT_PREFIX=()
if command -v timeout >/dev/null 2>&1; then
    TIMEOUT_PREFIX=(timeout "${PER_CASE_TIMEOUT}")
elif command -v gtimeout >/dev/null 2>&1; then
    TIMEOUT_PREFIX=(gtimeout "${PER_CASE_TIMEOUT}")
else
    echo "⚠  timeout/gtimeout не знайдено — кейси без жорсткого ліміту часу." >&2
fi

# Матриця: "мітка|вхід|прапор". Порожній прапор = юрособа; --person = фізособа.
CASES=(
    "юр: валідний Приватбанк|14360570|"
    "юр: неіснуючий код|12121212|"
    "юр: закороткий 4ц|1234|"
    "юр: задовгий 9ц|123456789|"
    "юр: сміття в коді|12ab5678|"
    "юр: назва→резолвінг|ПриватБанк|"
    "юр: неоднозначна назва|Банк|"
    "юр: неіснуюча назва|asdfghjkl|"
    "фіз: санкційна|Дерипаска Олег|--person"
    "фіз: чиста/тезки|Іваненко Петро|--person"
    "фіз: неіснуюча|asdfgh qwerty|--person"
)

RESULTS_FILE="$(mktemp)"
PARSER="$(mktemp).py"
trap 'rm -f "$RESULTS_FILE" "$PARSER"' EXIT

# Парсер виводу у TSV-рядок. ВАЖЛИВО: окремий файл, а не `python3 - <<PY`,
# бо heredoc і пайп конкурують за stdin (heredoc виграє → output не доходить).
cat > "$PARSER" <<'PY'
import sys, re
label, inp, rc = sys.argv[1], sys.argv[2], int(sys.argv[3])
data = sys.stdin.read()

def grab(pat):
    m = re.search(pat, data)
    return m.group(1).strip() if m else ""

score = grab(r'Trust score:\s*(\d+)\s*/\s*100')
level = grab(r'Рівень:\s*([A-ZА-ЯІЇЄ]+)')
cov   = grab(r'Покриття:\s*(.+)')

if cov:
    parts = dict(re.findall(r'(\w+)=(\w+)', cov))
    cov = "r:%s s:%s m:%s" % (parts.get('registry', '?'),
                              parts.get('sanctions', '?'),
                              parts.get('adverse_media', '?'))

err = ""
if rc == 124:
    err = "TIMEOUT"
# Граційна відмова (❌ ...) — НЕ краш: неоднозначність / не знайдено за назвою.
m = re.search(r'❌\s*(.+)', data)
if not err and m:
    err = m.group(1)[:58]
# Справжні аварії.
if not err:
    m = re.search(r'(Failed after [^\n]+|Traceback|[A-Za-z]*Error: [^\n]+'
                  r'|впав[^\n]*)', data)
    if m:
        err = m.group(1)[:58]
if not err and rc != 0 and not score:
    err = "exit %d" % rc
if not score and not err:
    err = "no-summary"

print("\t".join([label, inp[:18], cov or "—", score or "—",
                 level or "—", err or "—"]))
PY

echo "▶ Фаза 1: ${#CASES[@]} кейсів, timeout=${PER_CASE_TIMEOUT}s кожен, БЕЗ медіа"
echo

for case in "${CASES[@]}"; do
    IFS='|' read -r label input flag <<< "$case"

    printf '  …  %-28s (%s)\n' "$label" "$input" >&2

    # Збираємо аргументи: прапор додаємо лише якщо непорожній.
    args=(run --rm kyc "$input")
    [ -n "$flag" ] && args+=("$flag")
    args+=(--no-media -v)

    # Виклик у захисті: збій/таймаут не зупиняє матрицю.
    # bash 3.2 (macOS) під set -u не дає розгорнути порожній масив —
    # тому гілкуємо за кількістю елементів TIMEOUT_PREFIX.
    if [ "${#TIMEOUT_PREFIX[@]}" -gt 0 ]; then
        output="$("${TIMEOUT_PREFIX[@]}" docker compose "${args[@]}" 2>&1)"
    else
        output="$(docker compose "${args[@]}" 2>&1)"
    fi
    rc=$?

    # Парсимо вивід у TSV-рядок (python — надійніше для кирилиці/regex).
    printf '%s' "$output" | python3 "$PARSER" "$label" "$input" "$rc" >> "$RESULTS_FILE"

done

echo
echo "════════════════════════════ ЗВЕДЕНА ТАБЛИЦЯ ════════════════════════════"
{
    printf 'КЕЙС\tВХІД\tПОКРИТТЯ\tSCORE\tРІВЕНЬ\tNOTE/ПОМИЛКА\n'
    printf '────\t────\t────────\t─────\t──────\t────────────\n'
    cat "$RESULTS_FILE"
} | column -t -s $'\t'
echo "═════════════════════════════════════════════════════════════════════════"
echo
echo "Легенда coverage: r=registry  s=sanctions  m=adverse_media"
echo "  checked=перевірено · not_found=не знайдено · error=збій · skipped=пропущено"
