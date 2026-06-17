# oh-my-boring

[English](README.md) · [한국어](README.ko.md) · **日本語**

[![CI](https://github.com/jazz1x/oh-my-boring/actions/workflows/ci.yml/badge.svg)](https://github.com/jazz1x/oh-my-boring/actions/workflows/ci.yml)
![version](https://img.shields.io/badge/version-0.1.0-blue)
![Rust](https://img.shields.io/badge/engine-Rust%20edition%202024-000?logo=rust)
![Python](https://img.shields.io/badge/hooks-Python%203-3776AB?logo=python)
![Docker](https://img.shields.io/badge/deploy-Docker-2496ED?logo=docker)
![gemma4](https://img.shields.io/badge/LLM-gemma4:12b-000?logo=ollama)

**セルフホスティング型パーソナルメモリ RAG。** Claude Code のセッションがローカルで人が読める wiki に蒸留され、*"前これどうやったっけ？"* を呼び出して使います。**クラウド 0 · 100% ローカル。**

```bash
git clone https://github.com/jazz1x/oh-my-boring.git ~/oh-my-boring
cd ~/oh-my-boring
make up
make ask Q="docker build cache の問題、どう直したっけ？"
```

> **Docker**、**Ollama**（または OpenAI-compatible サーバー）、**Python 3**、**jq** が必要です。

---

## 機能

1. **自動蓄積** — セッション終了時に `vault/wiki` に整理されたマークダウンノートとして保存。手動管理不要。
2. **マークダウン中心のメモリ** — プレーンテキストで人に優しく、git diff 可能。検索もマークダウンを直接読みます。
3. **ローカル専用** — 埋め込みと要約が Ollama などローカル LLM で実行。外部 API やトークン不要。

オプションの **pgvector** アクセラレータ（`DRUDGE_VECTOR=on`）を有効にすると、類似度検索 + GraphRAG が追加されます。

---

## アーキテクチャ

```mermaid
flowchart LR
  subgraph SRC [sources]
    CC([Claude Code session])
  end
  subgraph WRITE [WRITE · gated]
    D["distill-session.py"] --> REM["drudge remember"]
  end
  WIKI[("vault/wiki<br/>primary memory")]
  subgraph RD [READ · open]
    ASK([make ask])
    REC([recall.py])
    MCP([MCP recall])
  end
  SRC --> WRITE --> WIKI --> RD
  WIKI -. "DRUDGE_VECTOR=on" .-> PG[("pgvector")]
  PG -. accelerate .-> RD
```

- **Read door** — 高速、LLM 不要。`make ask`、`recall.py`、MCP `recall` が `vault/wiki` を直接読みます。
- **Write door** — gated。`distill-session.py` がローカル LLM を呼び出し、drudge の `remember` MCP tool で書き込みます。

---

## 設定

ポリシーは **`boring.json`**（`make up` で `boring.example.json` から生成）に記述します：

| Key | 用途 |
|---|---|
| `note_lang` | `auto` · `ko` · `en` |
| `repos[]` | パス/remote ルール → `origin=personal/company/mirror/community` |
| `agents[]` | vector mode の ingest source |

シークレット/ランタイムスイッチは **`.env`**：

| Variable | 用途 |
|---|---|
| `DRUDGE_VECTOR` | `on` で pgvector 有効化（オプション） |
| `DRUDGE_LLM_BASE_URL` | OpenAI-compatible endpoint、デフォルト `http://localhost:11434/v1` |
| `DRUDGE_LLM_MODEL` / `DRUDGE_EMBED_MODEL` | デフォルト `gemma4:12b` / `bge-m3` |
| `SLACK_APP_TOKEN` / `SLACK_BOT_TOKEN` | オプション Slack assistant |

---

## コマンド

| Command | 説明 |
|---|---|
| `make up` | drudge 起動（hermes-agent イメージがある場合のみ一緒に起動） |
| `make ask Q="..."` | recall + 要約を一度に実行 |
| `make sync` | vault の再取り込み |
| `make remember M="text"` | 1 行ノートを書き込み |
| `make smoke` | end-to-end smoke test |
| `make logs` | drudge ログ |
| `make guard` | fmt + clippy + test |
| `make down` | コンテナ停止 |

---

## エージェントアダプター

`agents/` は外部エージェントを drudge エンジンに接続する **ホスト側アダプター** です。すべてのアダプターは同じ MCP/HTTP インターフェースを通じて drudge と通信し、いずれも必須ではありません。

旧 `hooks/` パスは backward-compatible な symlink セットとして残っているため、既存の Claude Code `settings.json` エントリや cron job は壊れません。

| アダプター | パス | 消費主体 | エントリポイント | 役割 |
|---|---|---|---|---|
| Claude Code | `agents/claude-code/distill-session.py` | `SessionEnd` / `Stop` hook | セッションを要約し `remember` を呼び出す |
| Claude Code | `agents/claude-code/recall.py` | `UserPromptSubmit` hook | 関連 snippet を取得しプロンプト context に注入 |
| hermes-agent | `agents/hermes/ingest-worker.py` | `hermes cron --script` | cron tick ごとに 1 セッションずつバックフィル |
| scheduler | `agents/schedulers/collect-sessions.py` | cron / launchd / 手動 | 古いセッションの lazy バックフィル |
| shared | `agents/shared/boring_config.py` | アダプター import | `boring.json` ポリシーローダー |

### トークン予算

自動検索はエージェントの context window を爆発させる可能性があるため、検索面は予算を意識しています。

- MCP `recall` は `max_tokens`、`max_results` を受け取ります。
- HTTP `/search` は `max_tokens`、`max_results` を受け取ります。
- `recall.py` は `RECALL_MAX_TOKENS` / `RECALL_MAX_RESULTS` で注入 context を制限します。
- `ask`/`brief` 合成は取得した context を固定文字数上限以下に保ちます。

### その他のエージェント

MCP に対応したエージェントならどれも drudge を利用できます。MCP server 登録:

```yaml
mcp_servers:
  drudge:
    url: http://drudge:7700/mcp
    transport: http
```

利用可能な tools: `recall` · `remember` · `sync` · `config_get` · `classify_repo`。

### オプション: hermes-agent

[hermes-agent](https://hermes-agent.org) はサードパーティの自律 supervisor です。Slack、オーケストレーション、cron ベースのバックフィルを drudge の MCP バックエンド経由で動かせます。イメージを別途ビルドすれば `make up` が自動的に検出します。

---

## デプロイ

| Mode | 方法 |
|---|---|
| **Docker**（デフォルト） | `make up` |
| **Native** | `cd drudge && cargo run --release -- serve` |

---

## 開発 · ガードレール

- SSOT ドキュメント: `drudge/{PHILOSOPHY,RUST-STYLE,ENFORCEMENT}.md`
- `make guard` = `rustfmt --check` + `clippy -D warnings` + `cargo test`
- CI: `rust-gate` · `gitleaks` · `cargo-deny` · `trivy`
- `unsafe_code = "forbid"`

---

## トラブルシューティング

| 症状 | 解決 |
|---|---|
| `make up` 失敗 | Ollama を確認: `curl -sf http://127.0.0.1:11434/api/tags` |
| ポート競合 | `lsof -i :7700 :5432 :11434` |
| agent が起動しない | `OMB_CORE_ONLY=1 make up` で core-only 実行。hermes イメージは別途ビルドが必要 |

---

## ディレクトリ

```text
oh-my-boring/
├─ drudge/                  # Rust エンジン
├─ agents/                  # ホスト側エージェントアダプター
│  ├─ claude-code/          # Claude Code hooks
│  ├─ hermes/               # hermes-agent cron
│  ├─ schedulers/           # cron/launchd バックフィル
│  └─ shared/               # ポリシー/設定ライブラリ
├─ hooks/                   # backward-compatible symlink → agents/
├─ scripts/                 # guard.sh · smoke.sh
├─ vault/                   # raw → wiki メモリ
├─ data/                    # Postgres データ (gitignored)
├─ docker-compose.yml
├─ start.sh
├─ boring.json              # ポリシー (make up 時に生成)
└─ Makefile
```
