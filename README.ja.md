# oh-my-boring

[한국어](README.md) · [English](README.en.md) · **[日本語](README.ja.md)**

[![CI](https://github.com/jazz1x/oh-my-boring/actions/workflows/ci.yml/badge.svg)](https://github.com/jazz1x/oh-my-boring/actions/workflows/ci.yml)
![Rust](https://img.shields.io/badge/engine-Rust%20edition%202024-000?logo=rust)
![Postgres](https://img.shields.io/badge/store-Postgres%2016%20%2B%20pgvector-336791?logo=postgresql&logoColor=white)
![Ollama](https://img.shields.io/badge/LLM-Ollama%20(local)-000)
![cloud](https://img.shields.io/badge/cloud-none-success)

**セルフホスト型の個人メモリRAG。** Claude Code（あるいは任意のMarkdownノート）での作業経験が自動でローカルのベクトルDBに蓄積され、*「前にこれどうやったっけ？」* を後から引き出せる。**クラウド0・データ100%ローカル。**

> 面倒で後回しにしがちな作業 — 過去の仕事を覚えて掘り起こすという退屈な仕事 — を **drudge**（下働き）エンジンが黙々と肩代わりする。

```text
セッション・ノート ──蒸留──▶ vault/raw ──compile──▶ vault/wiki ──ingest──▶ pgvector(+グラフ) ──recall──▶ 回答
   ▲ Claude Code                  (LLMキュレーション)              (埋め込み·BM25·CTE)       ▲ make ask / Slack
   └ SessionEnd フックが自動でトリガー ──────────────────────────────────────────────────┘
```

---

## なぜ使うか

- **自動蓄積** — セッションが終わるとフックが「問題解決の物語」へ蒸留して取り込む。手動の整理は不要。
- **ローカル専用** — 埋め込みも合成もホストのOllama。外部API・トークンは0。ノートはディスクの外に出ない。
- **ベクトル + グラフ** — 単なる類似検索ではなく、problem/solution/tool/concept のノードとエッジまで抽出（GraphRAG）。
- **会社/個人の分離（任意）** — env トークン1つで特定パスを `origin=company` にタグ付け・隔離。既定はすべて personal。

---

## レイヤー

| # | レイヤー | 役割 | 技術 | 公開 | `make up` の既定 |
|---|---|---|---|---|:---:|
| 1 | **Ollama**（ホスト） | 埋め込み `bge-m3`（1024次元）· 合成 `gemma4:12b`（think=false） | ホストプロセス | `127.0.0.1:11434` | 必須[^ollama] |
| 2 | **drudge**（Rustエンジン） | ingest·retrieve·graph·compile·distill（HTTP + 4hスケジューラ） | axum / tokio | `127.0.0.1:7700` | ✓ |
| 3 | **Postgres + pgvector** | `knowledge` = ベクトル（HNSW）+ BM25 + node/edge 再帰CTEグラフ | `pgvector/pgvector:pg16` | `127.0.0.1:5432` | ✓ |
| 4 | **フック**（ホスト・Python） | セッション → エンジンを繋ぐ糊（distill·recall·collect） | `python3` | — | 手動インストール[^hooks] |
| 5 | **hermes-agent**（任意） | Slackアシスタント + 自律cron（Socket Mode） | 外部イメージ | — | ✗（`--profile agent`）[^agent] |

[^ollama]: ホストで `ollama serve` が起動している必要がある。コンテナは `host.docker.internal` 経由で到達。
[^hooks]: `~/.claude/settings.json` に自分で登録する — 下記 [自己拡張ループ](#自己拡張ループ) を参照。
[^agent]: `hermes-agent` は本リポジトリに含まれないサードパーティ製イメージ（Nous Hermes Agent）。別途ビルドして `docker compose --profile agent up -d`。

> コアは **#2 + #3 + ホストの#1**。#4（フック）を足すと自動蓄積が回り、#5 は完全に任意。

---

## 事前準備

| インストール | 用途 | 確認 |
|---|---|---|
| **Docker**（Compose v2） | コンテナスタック | `docker compose version` |
| **Ollama** | ローカルの埋め込み・合成 | `ollama --version` · [ollama.com](https://ollama.com) または `brew install ollama` |
| **Python 3** | ホストフックの実行 | `python3 --version`（macOSに標準搭載） |
| ディスク ~10GB | モデル2つ | `gemma4:12b`（~8GB）+ `bge-m3`（~1.2GB） — `make up`/`make models` が自動pull |

> **クローン先**: `~/oh-my-boring` を推奨。フック・`start.sh`・vault のパスがこの場所を前提とする。別の場所に置く場合は [フックのパス](#自己拡張ループ) を合わせる必要がある。

---

## クイックスタート

```bash
git clone git@github.com:jazz1x/oh-my-boring.git ~/oh-my-boring
cd ~/oh-my-boring
cp .env.example .env          # Slackを使わないならそのままでよい（コアは .env なしでも動く）
make up                       # Ollama確認 → モデルpull → ビルド → 起動 → 初回sync
make smoke                    # end-to-end を一度確認
make ask Q="dockerのビルドキャッシュ問題、前にどうやって直したっけ？"
```

`make up` = `start.sh`: Ollama ヘルスチェック → モデル pull → `docker compose up -d --build`（postgres + drudge）→ `/health` 待機。初回の取り込み（startup sync）はバックグラウンドで数分。

---

## 自己拡張ループ

セッションが終わると勝手に溜まる — これが核心の価値。3つのホストフックがトリガーで、**重い処理（LLM蒸留・スクラブ・書き込み）はエンジン（`/distill`）が SSOT として行う**。

```text
① 終了/中断  →  distill-session.py（SessionEnd/Stop フック）
                  トランスクリプト抽出 → POST /distill → エンジンが蒸留・機密スクラブ・vault/raw 書き込み
② sync       →  compile（raw→wiki キュレーション）→ 埋め込み → pgvector upsert → グラフ抽出
                  [4hスケジューラ · make sync · セッション終了直後に自動]
③ recall     →  make ask / recall.py（プロンプトごとに自動注入）/ hermes-agent（Slack）
                  ベクトル + BM25 RRF で top-K
```

| フック | Claude Code イベント | 動作 |
|---|---|---|
| `hooks/distill-session.py` | `SessionEnd` / `Stop` | セッション抽出 → POST `/distill` → raw ノート書き込み + mtime 補正。git remote のリポジトリslugも `repo/<slug>` タグに |
| `hooks/recall.py` | `UserPromptSubmit` | プロンプトに関連する過去経験を `/search` で回収しコンテキスト注入 |
| `hooks/collect-sessions.py` | cron / `make collect` | SessionEnd で取り逃した過去セッションをバックフィル（少量ずつ） |

**フックのインストール**（永続化）— `~/.claude/settings.json`:

```jsonc
{
  "hooks": {
    "SessionEnd": [
      { "type": "command", "command": "python3 ~/oh-my-boring/hooks/distill-session.py", "timeout": 130, "async": true }
    ],
    "UserPromptSubmit": [
      { "type": "command", "command": "python3 ~/oh-my-boring/hooks/recall.py", "timeout": 10 }
    ]
  }
}
```

> distill/recall が動くにはエンジン（drudge）が起動している必要がある。起動していなければ静かに no-op — **セッションは決してブロックしない**。

---

## ソース & 回収

- **取り込み対象**（`DRUDGE_SOURCE_DIRS`、compose 既定）: `~/.claude/projects`（Claude Code メモリ）+ `vault/wiki`（蒸留・キュレーション済みノート）。
- **即時記録**: `make remember M="bge-m3 の埋め込みは1024次元"` → raw に書いてから sync。
- **回収**: `make ask Q="..."`（単発）· `recall.py`（プロンプトごとに自動）· Slack（`hermes-agent` 有効時）。

---

## 会社/個人タグ付け（任意・既定オフ）

特定パス配下の文書を `origin=company` にタグ付けし ingest から除外するには、env トークンを設定するだけ:

```bash
DRUDGE_COMPANY_SUBSTR=acme:acme-kb    # Rust ingest/origin/audit（パスのsubstring）
DISTILL_COMPANY_CWD=acme              # セッション蒸留フック（cwdのsubstring）
```

**コード変更0 — env のみ。** 空にすれば会社の概念自体がオフになり、すべて `personal`。

---

## コマンド一覧

全体は `make help`。よく使うもの:

| コマンド | 説明 |
|---|---|
| `make up` | セットアップ+起動（Ollama確認 · モデルpull · ビルド · 起動） |
| `make ask Q="質問"` | 単発クエリ（回収 + LLM合成 + 出典） |
| `make sync` | 手動取り込み（compile → ingest → extract） |
| `make remember M="内容"` | 一行メモを即記録 + 取り込み |
| `make collect [N=3]` | 過去セッションのバックフィル（1回N件） |
| `make smoke` | end-to-end スモークテスト |
| `make logs` | drudge エンジンのログ |
| `make psql` | Postgres に直接接続（グラフを覗く） |
| `make guard` | 構造ゲート（fmt + clippy + test）— CI と同一 |
| `make down` | 停止（データ `./data` は保持） |
| `make reset` | ⚠️ Postgres データも初期化（ソースから再取り込み） |

---

## 設定（env）

コアは `.env` なしで動く。既定値は `docker-compose.yml` の `drudge` 環境に埋め込まれている。

| 変数 | 既定 | 用途 |
|---|---|---|
| `SLACK_APP_TOKEN` / `SLACK_BOT_TOKEN` | — | `hermes-agent`（Slack）を有効化するときのみ |
| `DRUDGE_LLM_MODEL` | `gemma4:12b` | 合成モデル（think=false 固定） |
| `DRUDGE_EMBED_MODEL` | `bge-m3` | 埋め込み（1024次元） |
| `DRUDGE_SOURCE_DIRS` | `~/.claude/projects:vault/wiki` | 取り込みソース（`:` 区切り） |
| `DRUDGE_SYNC_HOURS` | `4` | バックグラウンド sync 間隔 |
| `DRUDGE_COMPANY_SUBSTR` / `DISTILL_COMPANY_CWD` | — | 会社タグ付け（上記参照） |

---

## 開発 · ガードレール

- **SSOT ドキュメント**: `drudge/{PHILOSOPHY,RUST-STYLE,ENFORCEMENT}.md`。
- **原則**: ROP（Result レール）· Parse-don't-validate · Clean Architecture · 最も単純で動くもの。
- **ゲート**（ローカル `make guard` == CI）: `rustfmt --check` + `clippy -D warnings`（`unsafe` forbid + `all`/`pedantic` deny）+ `cargo test`。テストはスタック非依存（DB不要）。
- **CI**（`.github/workflows/ci.yml`）: PR と main push のたびに `rust-gate`（guard.sh）+ `gitleaks`（機密スキャン）。ブランチ保護が両方を必須化 — admin も回避不可、直接 push・force-push・削除も禁止。

---

## ディレクトリ

```text
oh-my-boring/
├─ drudge/             # Rustエンジン（ingest·retrieve·graph·compile·distill·serve）
│  └─ src/{ingest,retrieve,extract,graph,vault,distill,serve,store,ollama,...}.rs
├─ hooks/              # ホストフック（distill-session · recall · collect-sessions）
├─ scripts/            # guard.sh（ゲート）· smoke.sh · eval-gate.sh
├─ vault/              # raw（蒸留）→ compile → wiki（キュレーション）。ingest 対象
├─ data/              # Postgres 永続化（pgdata）— gitignore
├─ docker-compose.yml  # postgres + drudge（+ --profile agent: hermes-agent）
├─ start.sh            # make up の実体（Ollama·モデル·ビルド·ヘルス）
└─ Makefile            # コマンドの入口
```
