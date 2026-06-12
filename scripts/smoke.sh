#!/bin/sh
# 스모크 테스트 — 엔진이 실제로 end-to-end 도는지 *적대적으로* 확인 (happy path 가정 금지).
#   make smoke  또는  ./scripts/smoke.sh
# 주의: /bin/sh(dash)는 pipefail 미지원 → 각 단계 결과를 명시적으로 검증한다.
set -eu
URL="${DRUDGE_URL:-http://localhost:7700}"
fail() { echo "❌ $1"; exit 1; }

echo "1) 컨테이너 상태…"
ps=$(docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null) || fail "compose ps 실패"
printf '%s\n' "$ps" | grep -qE 'drudge.*Up' || fail "drudge 미가동"
printf '%s\n' "$ps" | grep -qE 'postgres.*(healthy|Up)' || fail "postgres 미가동"
printf '%s\n' "$ps" | grep -qi 'restarting' && fail "크래시루프 컨테이너 존재: $(printf '%s' "$ps" | grep -i restarting)"
printf '%s\n' "$ps" | grep -E 'postgres|drudge|joseph'

echo "2) drudge /health…"
[ "$(curl -s -o /dev/null -w '%{http_code}' -m5 "$URL/health")" = "200" ] || fail "/health != 200"

echo "3) /audit (적재 + 그래프 실측)…"
audit=$(curl -sf -m5 "$URL/audit") || fail "/audit 실패"
chunks=$(printf '%s' "$audit" | jq -r '.total_chunks // 0')
edges=$(printf '%s' "$audit" | jq -r '.graph_edges // 0')
sem=$(printf '%s' "$audit" | jq -r '.semantic_problems // 0')
[ "${chunks:-0}" -gt 0 ] || fail "코퍼스 비어있음 (chunks=0)"
[ "${edges:-0}" -gt 0 ] || fail "그래프 엣지 0 (구조 그래프 미생성)"
echo "   chunks=$chunks edges=$edges sem_problems=$sem"
[ "${sem:-0}" -gt 0 ] || echo "   ⚠ 시맨틱 0 — extract 진행 중일 수 있음(비동기). make sync 후 재확인."

echo "4) /ask (코퍼스 내 질문 — 실답변이어야, 폴백/에러면 실패)…"
ans=$(curl -sf -m120 "$URL/ask" -H 'content-type: application/json' -d '{"question":"oh-my-boring가 뭐야?"}' | jq -r '.answer') || fail "/ask 호출 실패"
[ -n "$ans" ] && [ "$ans" != "null" ] || fail "ask 빈 응답"
case "$ans" in
  *"메모리에 없음"*|*"못 찾"*|*"연결할 수 없"*|*"타임아웃"*|*"오류"*)
    fail "ask 비정상 답변(폴백/에러): $ans" ;;
esac
echo "   → $(printf '%s' "$ans" | head -c 90)…"

echo "5) /graph (CTE 그래프 이웃 — >0 단언)…"
n=$(curl -sf -m90 "$URL/graph" -H 'content-type: application/json' -d '{"query":"oh-my-boring"}' | jq -r '.graph_neighbors | length') || fail "/graph 호출 실패"
[ "${n:-0}" -gt 0 ] || fail "graph 이웃 0 (그래프 회수 작동 안 함)"
echo "   graph_neighbors=$n"

echo "6) joseph (Slack 게이트웨이) — 크래시 아님 확인…"
jstat=$(docker compose ps joseph --format '{{.Status}}' 2>/dev/null || echo "")
case "$jstat" in
  *Restarting*) fail "joseph 크래시루프: $jstat" ;;
  *Up*) docker compose logs joseph 2>&1 | grep -q 'Bolt app is running' \
          && echo "   ✅ Slack 연결됨" \
          || echo "   ⏸ joseph Up(비활성/idle) — 토큰 없으면 정상" ;;
  *) echo "   ⚠ joseph 상태: ${jstat:-없음}" ;;
esac

echo "✅ 스모크 통과 — 엔진 end-to-end 작동 (적대적 검증 포함)."
