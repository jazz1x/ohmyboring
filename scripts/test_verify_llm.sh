#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
REAL_PYTHON3="$(command -v python3)"
trap 'rm -rf "$TMP"' EXIT INT TERM

mkdir -p "$TMP/bin"

cat >"$TMP/bin/curl" <<'SH'
#!/bin/sh
models_json_ollama() {
    old_ifs=$IFS
    IFS=,
    first=1
    printf '{"models":['
    for model in $VERIFY_MODELS; do
        if [ -n "$model" ]; then
            if [ "$first" -eq 0 ]; then
                printf ','
            fi
            printf '{"name":"%s","model":"%s"}' "$model" "$model"
            first=0
        fi
    done
    printf ']}'
    IFS=$old_ifs
}

url=
while [ "$#" -gt 0 ]; do
    case "$1" in
        -m|-o|-w)
            shift
            ;;
        -*)
            ;;
        *)
            url="$1"
            ;;
    esac
    shift || break
done

case "$url" in
    */api/tags)
        models_json_ollama
        ;;
    *)
        echo "unexpected curl url: $url" >&2
        exit 22
        ;;
esac
SH
chmod +x "$TMP/bin/curl"

cat >"$TMP/bin/python3" <<SH
#!/bin/sh
REAL_PYTHON3='$REAL_PYTHON3'

models_json_openai() {
    old_ifs=\$IFS
    IFS=,
    first=1
    printf '{"object":"list","data":['
    for model in \$VERIFY_MODELS; do
        if [ -n "\$model" ]; then
            if [ "\$first" -eq 0 ]; then
                printf ','
            fi
            printf '{"id":"%s","object":"model"}' "\$model"
            first=0
        fi
    done
    printf ']}'
    IFS=\$old_ifs
}

contains_model() {
    case ",\$VERIFY_MODELS," in
        *,\$1,*) return 0 ;;
        *) return 1 ;;
    esac
}

if [ "\${1:-}" != "-" ]; then
    exec "\$REAL_PYTHON3" "\$@"
fi

if [ "\$#" -eq 3 ]; then
    case "\${VERIFY_HTTP_CODE:-200}" in
        200)
            models_json_openai
            exit 0
            ;;
        401)
            echo "HTTP 401 unauthorized" >&2
            exit 3
            ;;
        *)
            echo "HTTP \${VERIFY_HTTP_CODE:-000}" >&2
            exit 2
            ;;
    esac
fi

if [ "\$#" -eq 4 ]; then
    model="\$3"
    if contains_model "\$model"; then
        printf '%s\n' "\$VERIFY_EMBED_DIM"
        exit 0
    fi
    echo "HTTP 404 model not found" >&2
    exit 2
fi

echo "unexpected python3 invocation" >&2
exit 2
SH
chmod +x "$TMP/bin/python3"

write_config() {
    provider="$1"
    model="$2"
    embed_model="$3"
    embed_dim="$4"
    api_key_env="${5:-}"
    {
        printf '{\n'
        printf '  "llm": {\n'
        printf '    "provider": "%s",\n' "$provider"
        printf '    "base_url": "http://127.0.0.1:43210/v1",\n'
        printf '    "model": "%s",\n' "$model"
        printf '    "embed_model": "%s",\n' "$embed_model"
        printf '    "embed_dim": %s,\n' "$embed_dim"
        if [ -n "$api_key_env" ]; then
            printf '    "api_key_env": "%s",\n' "$api_key_env"
        fi
        printf '    "bootstrap": "manual"\n'
        printf '  }\n'
        printf '}\n'
    } >"$TMP/boring.json"
}

make_provider_script() {
    provider="$1"
    mkdir -p "$TMP/scripts/llm-providers"
    cat >"$TMP/scripts/llm-providers/$provider.sh" <<'SH'
#!/bin/sh
exit 0
SH
    chmod +x "$TMP/scripts/llm-providers/$provider.sh"
}

run_verify() {
    VERIFY_MODELS="$1" \
    VERIFY_EMBED_DIM="$2" \
    VERIFY_HTTP_CODE="${3:-200}" \
    PATH="$TMP/bin:$PATH" \
    BORING_HOME="$TMP" \
    BORING_CONFIG="$TMP/boring.json" \
    sh "$ROOT/scripts/verify-llm.sh"
}

make_provider_script ollama
make_provider_script lmstudio

write_config ollama "qwen3:14b" "bge-m3" 1024
if ! run_verify "qwen3:14b,bge-m3" 1024 >"$TMP/pass.out" 2>&1; then
    cat "$TMP/pass.out"
    echo "FAIL: verify-llm should pass when actual embedding dimension matches" >&2
    exit 1
fi
case "$(cat "$TMP/pass.out")" in
  *"actual embedding dimension is 1024"*) ;;
  *)
    cat "$TMP/pass.out"
    echo "FAIL: verify-llm did not report actual embedding dimension" >&2
    exit 1
    ;;
esac

write_config lmstudio "google/gemma-4-12b-qat" "bge-m3" 1024
if run_verify "google/gemma-4-12b-qat,bge-m3" 768 >"$TMP/dim-fail.out" 2>&1; then
    cat "$TMP/dim-fail.out"
    echo "FAIL: verify-llm should fail when actual embedding dimension differs" >&2
    exit 1
fi
case "$(cat "$TMP/dim-fail.out")" in
  *"actual embedding dimension mismatch"*) ;;
  *)
    cat "$TMP/dim-fail.out"
    echo "FAIL: verify-llm dimension mismatch message missing" >&2
    exit 1
    ;;
esac

write_config lmstudio "google/gemma-4-12b-qat" "bge-m3" 1024
if run_verify "google/gemma-4-12b-qat,text-embedding-nomic-embed-text-v1.5" 768 >"$TMP/missing.out" 2>&1; then
    cat "$TMP/missing.out"
    echo "FAIL: verify-llm should fail when LM Studio lacks bge-m3" >&2
    exit 1
fi
case "$(cat "$TMP/missing.out")" in
  *"embed model 'bge-m3' not found"*) ;;
  *)
    cat "$TMP/missing.out"
    echo "FAIL: verify-llm missing model message absent" >&2
    exit 1
    ;;
esac

write_config lmstudio "google/gemma-4-12b-qat" "bge-m3" 1024
if run_verify "google/gemma-4-12b-qat,bge-m3" 1024 401 >"$TMP/auth-fail.out" 2>&1; then
    cat "$TMP/auth-fail.out"
    echo "FAIL: verify-llm should fail when auth is required but llm.api_key_env is missing" >&2
    exit 1
fi
case "$(cat "$TMP/auth-fail.out")" in
  *"llm.api_key_env is required"*) ;;
  *)
    cat "$TMP/auth-fail.out"
    echo "FAIL: verify-llm missing api_key_env message absent" >&2
    exit 1
    ;;
esac

echo "verify-llm tests passed"
