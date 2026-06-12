# ── Stage 1: builder ─────────────────────────────────────────────────────────
# 코드네임 고정(trixie) — 런타임과 같은 Debian 릴리스라야 GLIBC 일치(스테이지 drift 방지).
FROM rust:1-slim-trixie AS builder

# 빌드 의존성 (rustls ring 링커용). tokio-postgres/pgvector 는 순수 Rust — C 의존성 없음.
RUN apt-get update && apt-get install -y --no-install-recommends \
    pkg-config \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 의존성 레이어 캐시: Cargo 매니페스트를 먼저 복사 후 더미 빌드 → 소스 변경 시 재빌드 최소화
COPY Cargo.toml Cargo.lock ./
# 더미 main 으로 deps만 미리 컴파일
RUN mkdir src && echo 'fn main(){}' > src/main.rs \
    && cargo build --release 2>&1 || true \
    && rm -rf src

# 실제 소스 복사 + 본 빌드
COPY src ./src
# touch main.rs so cargo sees it as newer than the cached dummy artifact
RUN touch src/main.rs && cargo build --release

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
# builder 와 동일 코드네임(trixie) — GLIBC 일치 보장.
FROM debian:trixie-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 임베디드 DB 저장 디렉터리 (볼륨 마운트 포인트)
RUN mkdir -p /data

COPY --from=builder /build/target/release/hermes /usr/local/bin/hermes

# ── 환경 변수 기본값 ──────────────────────────────────────────────────────────
ENV HERMES_SURREAL_PATH=/data/surrealdb
ENV HERMES_HTTP_ADDR=0.0.0.0:7700
# OLLAMA_HOST, HERMES_SOURCE_DIRS, HERMES_SYNC_HOURS: 런타임에 주입

EXPOSE 7700

VOLUME ["/data"]

CMD ["hermes", "serve"]
