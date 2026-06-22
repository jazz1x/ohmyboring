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
# 더미 main + lib 으로 deps만 미리 컴파일 (Cargo.toml에 [lib]가 있으므로 둘 다 필요).
RUN mkdir src && printf 'pub mod dummy {}\n' > src/lib.rs && echo 'fn main(){}' > src/main.rs \
    && cargo build --release 2>&1 || true \
    && rm -rf src

# 실제 소스 복사 + 본 빌드
COPY src ./src
# touch both entry points so cargo rebuilds the lib + bin after the dummy dependency layer.
RUN touch src/main.rs src/lib.rs && cargo build --release

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
# builder 와 동일 코드네임(trixie) — GLIBC 일치 보장.
FROM debian:trixie-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Data dir (volume mount point) — owned by the non-root user below.
RUN mkdir -p /data

COPY --from=builder /build/target/release/drudge /usr/local/bin/drudge

# Run as a non-root user (defense-in-depth; satisfies trivy DS-0002).
# Bind mounts (vault, ~/.claude) are uid-remapped by Docker Desktop on macOS;
# on Linux, pass a matching uid if host-file ownership matters.
RUN useradd --system --uid 10001 --no-create-home drudge \
    && chown -R drudge /data
USER drudge

# ── Default env ───────────────────────────────────────────────────────────────
ENV DRUDGE_HTTP_ADDR=0.0.0.0:7700
# DRUDGE_LLM_BASE_URL, DRUDGE_SOURCE_DIRS, DRUDGE_SYNC_HOURS: injected at runtime

EXPOSE 7700

VOLUME ["/data"]

CMD ["drudge", "serve"]
