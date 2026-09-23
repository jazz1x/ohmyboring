#!/bin/sh
# 데이터 이전 대조 검사 발판 — 사본 B(boring_rehearsal_new)를 A(boring_rehearsal)에서 복제하고
# 임시 엔진 둘(parity-a :7701, parity-b :7702)을 docker run 으로 띄운다.
#
# 운영과의 거리: 운영 DB(boring)는 어떤 쓰기도 질의도 하지 않고, docker compose 는 쓰지 않는다
# (compose 는 name: oh-my-boring 고정이라 어디서 쳐도 운영 컨테이너를 재생성한다 — 사이클 4 사고).
# postgres 컨테이너에 가는 것은 사본·스크래치 DB 뿐이다.
#
# 검색 품질(③)을 재려면 eval 픽스처(data/eval/fixtures/eval-*.md)가 사본 안에 있어야 한다 — 그래서
# 임시 엔진에게는 빈 볼트가 아니라 픽스처만 든 임시 볼트를 /parity-vault 에 마운트해 준다. 경로를
# /vault 가 아니게 한 이유: 기동 즉시 run_sync 가 /parity-vault/wiki 를 관리 경로로 보고 거기 없는
# /vault/wiki/* 행을 통째로 지우는(prune) 사고를 막기 위해서다(ingest.rs::is_managed_path 접두 비교).
# 브리핑은 BORING_BRIEF_HOUR=99 로 끈다(0..23 밖이면 안 돈다, scheduler.rs:316).
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
POSTGRES_CONTAINER=boring-postgres
DB_USER=boring
A_DB="${PARITY_A_DB:-boring_rehearsal}"
B_DB="${PARITY_B_DB:-boring_rehearsal_new}"
IMAGE=boring-drudge
NETWORK=oh-my-boring_default
VAULT_A="$ROOT/data/loop/parity-vault-a"
VAULT_B="$ROOT/data/loop/parity-vault-b"

psql_at() { docker exec -i "$POSTGRES_CONTAINER" psql -U "$DB_USER" -d "$1" -At -c "$2"; }

claim_count() { psql_at "$1" "select count(*) from claim"; }

wait_ready() {
  url=$1
  name=$2
  n=0
  # 기동 즉시 startup sync 가 도는 동안 sync:"running" 이다. /health 200 만 기다리면 픽스처 수집이
  # 덜 된 사본을 재는 경주가 되므로, sync:"idle" 이 될 때까지 기다린다.
  until curl -fsS "$url/health" 2>/dev/null | grep -q '"sync":"idle"'; do
    n=$((n + 1))
    if [ "$n" -ge 150 ]; then
      echo "parity-harness: $name 가 300초 안에 /health 200 + sync idle 이 안 됐다" >&2
      docker logs "$name" --tail 30 >&2 || true
      exit 1
    fi
    sleep 2
  done
}

run_engine() {
  name=$1
  dsn=$2
  vault=$3
  port=$4
  docker run --rm -d --name "$name" --network "$NETWORK" \
    -e "PG_DSN=$dsn" \
    -e "BORING_VECTOR=on" \
    -e "BORING_BRIEF_HOUR=99" \
    -e "BORING_SYNC_HOURS=4" \
    -e "BORING_VAULT_DIR=/parity-vault" \
    -e "BORING_LLM_BASE_URL=${BORING_LLM_BASE_URL:-}" \
    -e "BORING_LLM_MODEL=${BORING_LLM_MODEL:-}" \
    -e "BORING_LLM_API_KEY=${BORING_LLM_API_KEY:-}" \
    -v "$vault:/parity-vault" \
    -p "$port:7700" "$IMAGE"
}

up() {
  a_claims=$(claim_count "$A_DB")
  docker exec "$POSTGRES_CONTAINER" dropdb -U "$DB_USER" --if-exists "$B_DB"
  docker exec "$POSTGRES_CONTAINER" createdb -U "$DB_USER" "$B_DB"
  docker exec "$POSTGRES_CONTAINER" pg_dump -U "$DB_USER" -Fc "$A_DB" \
    | docker exec -i "$POSTGRES_CONTAINER" pg_restore -U "$DB_USER" -d "$B_DB" --no-owner --no-privileges
  b_claims=$(claim_count "$B_DB")
  if [ "$b_claims" != "$a_claims" ]; then
    echo "parity-harness: 사본 B claim 수 $b_claims 가 A 의 $a_claims 와 다르다" >&2
    exit 1
  fi

  rm -rf "$VAULT_A" "$VAULT_B"
  mkdir -p "$VAULT_A/wiki" "$VAULT_B/wiki"
  cp "$ROOT"/data/eval/fixtures/eval-*.md "$VAULT_A/wiki/"
  cp "$ROOT"/data/eval/fixtures/eval-*.md "$VAULT_B/wiki/"

  docker rm -f parity-a parity-b >/dev/null 2>&1 || true
  run_engine parity-a "postgresql://boring:boring@boring-postgres:5432/$A_DB" "$VAULT_A" 7701
  run_engine parity-b "postgresql://boring:boring@boring-postgres:5432/$B_DB" "$VAULT_B" 7702
  wait_ready "http://127.0.0.1:7701" parity-a
  wait_ready "http://127.0.0.1:7702" parity-b
  echo "parity-harness: up — parity-a(:7701, $A_DB) · parity-b(:7702, $B_DB) /health 200 + sync idle, claim A=$a_claims B=$b_claims"
}

down() {
  docker rm -f parity-a parity-b >/dev/null 2>&1 || true
  # 사본 A 에 남은 eval 픽스처를 지운다 — store.rs::delete_document 과 같은 순서(document → edge →
  # claim, chunk 는 document FK cascade)라 사본 A 는 발판 돌리기 전 행 수로 돌아간다.
  psql_at "$A_DB" "DELETE FROM document WHERE source_path LIKE '/parity-vault/wiki/%';" >/dev/null
  psql_at "$A_DB" "DELETE FROM edge WHERE src LIKE 'doc:/parity-vault/%' OR dst LIKE 'doc:/parity-vault/%';" >/dev/null
  psql_at "$A_DB" "DELETE FROM claim WHERE source_path LIKE '/parity-vault/wiki/%';" >/dev/null
  docker exec "$POSTGRES_CONTAINER" dropdb -U "$DB_USER" --if-exists "$B_DB"
  rm -rf "$VAULT_A" "$VAULT_B"
  echo "parity-harness: down — parity-a·parity-b 내림, $A_DB 의 eval 픽스처 정리, $B_DB 삭제, $A_DB 는 남김"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  *) echo "usage: sh scripts/parity-harness.sh {up|down}" >&2; exit 2 ;;
esac
