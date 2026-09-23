#!/bin/sh
# 데이터 이전 대조 검사 발판 — 기준 DB(PARITY_BASE_DB, 기본 boring_snapshot_0923)에서 사본 둘
# (boring_parity_a, boring_parity_b)을 만들고 임시 엔진 둘(parity-a :7701, parity-b :7702,
# docker run — compose 는 name 고정이라 어디서 쳐도 운영 컨테이너를 재생성한다, 사이클 4 사고)을 띄운다.
# 엔진이 기준 DB에 붙는 일은 없다 — up/down 모두 기준에는 읽기(pg_dump)만, 쓰기는 사본 둘뿐이다.
#
# 엔진 설정은 라이브 boring-drudge 와 같은 자리에서 같은 걸 쓴다: boring.json 을 /app/boring.json 에
# 읽기 전용으로 마운트하고 BORING_CONFIG 로 가리킨다(컨테이너 기본값이 아닌 이유: 기본 boring.json 은
# allow_company_origin=false 라 회사 출처 프로젝트(foodspring-front 등) 카드가 전부 비어 ② 눈이 멀기
# 때문 — docker inspect boring-drudge 의 마운트·env 가 정본). TZ 는 라이브 값(Asia/Seoul).
#
# 검색 품질(③)을 재려면 eval 픽스처(data/eval/fixtures/eval-*.md)가 사본 안에 있어야 해서 임시 볼트를
# /parity-vault 에 마운트해 양쪽에 대칭 수집(기동 sync)한다. /vault 를 쓰면 안 된다 — 관리 경로가
# /vault/wiki 로 잡혀 prune 이 사본의 기존 행을 통째로 지운다(ingest.rs::is_managed_path 접두 비교).
# 브리핑은 BORING_BRIEF_HOUR=99 로 끈다(0..23 밖이면 안 돈다, scheduler.rs:316).
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
POSTGRES_CONTAINER=boring-postgres
DB_USER=boring
BASE_DB="${PARITY_BASE_DB:-boring_snapshot_0923}"
A_DB=boring_parity_a
B_DB=boring_parity_b
IMAGE="${PARITY_IMAGE:-boring-drudge}"
NETWORK=oh-my-boring_default
VAULT_A="$ROOT/data/loop/parity-vault-a"
VAULT_B="$ROOT/data/loop/parity-vault-b"

psql_at() { docker exec -i "$POSTGRES_CONTAINER" psql -U "$DB_USER" -d "$1" -At -c "$2"; }

claim_count() { psql_at "$1" "select count(*) from claim"; }

clone_db() {
  src=$1
  dst=$2
  docker exec "$POSTGRES_CONTAINER" dropdb -U "$DB_USER" --if-exists "$dst"
  docker exec "$POSTGRES_CONTAINER" createdb -U "$DB_USER" "$dst"
  docker exec "$POSTGRES_CONTAINER" pg_dump -U "$DB_USER" -Fc "$src" \
    | docker exec -i "$POSTGRES_CONTAINER" pg_restore -U "$DB_USER" -d "$dst" --no-owner --no-privileges
}

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
    -e "BORING_CONFIG=/app/boring.json" \
    -e "BORING_VECTOR=on" \
    -e "BORING_BRIEF_HOUR=99" \
    -e "BORING_SYNC_HOURS=4" \
    -e "BORING_VAULT_DIR=/parity-vault" \
    -e "BORING_LLM_BASE_URL=${BORING_LLM_BASE_URL:-}" \
    -e "BORING_LLM_MODEL=${BORING_LLM_MODEL:-}" \
    -e "BORING_LLM_API_KEY=${BORING_LLM_API_KEY:-}" \
    -e "TZ=Asia/Seoul" \
    -v "$ROOT/boring.json:/app/boring.json:ro" \
    -v "$vault:/parity-vault" \
    -p "$port:7700" "$IMAGE"
}

up() {
  # boring.json 이 없으면 docker 가 파일 대신 디렉터리를 조용히 만들어 엔진이 묵은 설정으로 뜬다 —
  # ② 가 눈먼 채 진행되는 것보다 여기서 멈추는 게 맞다.
  if [ ! -f "$ROOT/boring.json" ]; then
    echo "parity-harness: $ROOT/boring.json 이 없다 — 읽기 전용 마운트 대신 디렉터리가 생겨 조용히 깨진다" >&2
    exit 2
  fi
  base_claims=$(claim_count "$BASE_DB")
  clone_db "$BASE_DB" "$A_DB"
  clone_db "$BASE_DB" "$B_DB"
  a_claims=$(claim_count "$A_DB")
  b_claims=$(claim_count "$B_DB")
  if [ "$a_claims" != "$base_claims" ] || [ "$b_claims" != "$base_claims" ]; then
    echo "parity-harness: 사본 claim 수 A=$a_claims B=$b_claims 가 기준 $base_claims 와 다르다" >&2
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
  echo "parity-harness: up — parity-a(:7701, $A_DB) · parity-b(:7702, $B_DB) /health 200 + sync idle, claim 기준=$base_claims A=$a_claims B=$b_claims"
}

down() {
  docker rm -f parity-a parity-b >/dev/null 2>&1 || true
  docker exec "$POSTGRES_CONTAINER" dropdb -U "$DB_USER" --if-exists "$A_DB"
  docker exec "$POSTGRES_CONTAINER" dropdb -U "$DB_USER" --if-exists "$B_DB"
  rm -rf "$VAULT_A" "$VAULT_B"
  echo "parity-harness: down — parity-a·parity-b 내림, ${A_DB}·${B_DB} 삭제, 기준 ${BASE_DB} 는 무접촉"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  *) echo "usage: sh scripts/parity-harness.sh {up|down}" >&2; exit 2 ;;
esac
