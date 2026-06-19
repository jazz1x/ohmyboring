#!/usr/bin/env bash
# Migrate deprecated env-based config to boring.json.
# Reads: DRUDGE_NOTE_LANG, DRUDGE_COMPANY_SUBSTR, DISTILL_COMPANY_CWD, DRUDGE_SOURCE_DIRS
# Produces: boring.json (backs up any existing file).
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="${1:-.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck source=/dev/null
  . "$ENV_FILE"
  set +a
fi

OUT="${BORING_CONFIG:-boring.json}"
if [ -f "$OUT" ]; then
  BACKUP="$OUT.bak.$(date +%Y%m%d%H%M%S)"
  cp "$OUT" "$BACKUP"
  echo "[migrate] backed up existing $OUT to $BACKUP"
fi

NOTE_LANG="${DRUDGE_NOTE_LANG:-auto}"

# Build repo rules from deprecated company tokens.
REPOS=()
seen_rules=()
add_rule() {
  local tok="$1"
  [ -z "$tok" ] && return
  for s in "${seen_rules[@]:-}"; do
    [ "$s" = "$tok" ] && return
  done
  seen_rules+=("$tok")
  REPOS+=("    { \"match\": \"$tok\", \"origin\": \"company\", \"name\": \"$tok\" }")
}

IFS=':' read -ra COMPANY_TOKENS <<< "${DRUDGE_COMPANY_SUBSTR:-}"
for tok in "${COMPANY_TOKENS[@]:-}"; do add_rule "$tok"; done
IFS=':' read -ra DISTILL_TOKENS <<< "${DISTILL_COMPANY_CWD:-}"
for tok in "${DISTILL_TOKENS[@]:-}"; do add_rule "$tok"; done

# Build agent source dirs from deprecated DRUDGE_SOURCE_DIRS.
SRC_DIRS=()
if [ -n "${DRUDGE_SOURCE_DIRS:-}" ]; then
  IFS=':' read -ra DIRS <<< "$DRUDGE_SOURCE_DIRS"
  for d in "${DIRS[@]}"; do
    [ -z "$d" ] && continue
    SRC_DIRS+=("        \"$d\"")
  done
else
  SRC_DIRS=("        \"~/.claude/projects\"" "        \"vault/wiki\"")
fi

{
  echo "{"
  echo "  \"schema_version\": 1,"
  echo "  \"note_lang\": \"$NOTE_LANG\","
  echo "  \"repos\": ["
  if [ ${#REPOS[@]} -gt 0 ]; then
    printf '%s' "${REPOS[0]}"
    for r in "${REPOS[@]:1}"; do printf ',\n%s' "$r"; done
    echo ""
  fi
  echo "  ],"
  echo "  \"agents\": ["
  echo "    {"
  echo "      \"enabled\": true,"
  echo "      \"name\": \"default\","
  echo "      \"paths\": ["
  if [ ${#SRC_DIRS[@]} -gt 0 ]; then
    printf '%s' "${SRC_DIRS[0]}"
    for d in "${SRC_DIRS[@]:1}"; do printf ',\n%s' "$d"; done
    echo ""
  fi
  echo "      ]"
  echo "    }"
  echo "  ]"
  echo "}"
} > "$OUT"

echo "[migrate] wrote $OUT"
echo "[migrate] review it, then remove these deprecated variables from $ENV_FILE:"
echo "           DRUDGE_NOTE_LANG, DRUDGE_COMPANY_SUBSTR, DISTILL_COMPANY_CWD, DRUDGE_SOURCE_DIRS"
