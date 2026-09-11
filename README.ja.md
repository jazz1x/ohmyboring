# ohmyboring

[English](README.md) · [한국어](README.ko.md) · **日本語**

ohmyboring はコーディングエージェントのセッション（Claude Code、Kimi Code、Codex）をローカルの Markdown wiki に変え、以前解いたことを、いままさに解き直そうとしているプロンプトに差し込む。どう解いたかを残すものであり、想起の精度は公開で測定中だ（§ Status）。すべて手元のマシンとローカル LLM で動く — クラウドもトークンも使わない。

## Quick start

```bash
sh -c "$(curl -fsSL https://raw.githubusercontent.com/jazz1x/ohmyboring/main/install.sh)"
```

ワンライナーは `~/oh-my-boring` に clone してビルドし、フック・MCP 登録・ワーカーまで配線する。手順で行うなら:

```bash
git clone https://github.com/jazz1x/ohmyboring.git ~/oh-my-boring
cd ~/oh-my-boring
make up             # エンジン起動（llm.provider が ollama なら Ollama を立ち上げモデルを取得）
make verify-llm     # プロバイダ到達、モデル id 2 つの存在、埋め込み次元の一致
make doctor         # スタック・フック・ワーカー・最新ノート — 見つけたものは全部出し、隠さない
make collect N=20   # 過去の Claude Code セッションで vault を満たす。clone 直後は空
make ask Q="how did I fix the docker build cache problem?"
```

`make up` が 0 で終わり `http://127.0.0.1:7700/health` が 200 を返し、`make verify-llm` が `configuration looks consistent` を出し、`make doctor` に `✗` がなければ完了。

必要なもの: Docker、Python 3、jq、curl、git、make、そしてローカル LLM サーバー — Ollama（既定。`make up` が起動する）か LM Studio（サーバーを起動し、チャットモデルと埋め込みモデルを 1 つずつ載せ、`llm.provider` を `lmstudio` にして `make verify-llm`）。ほかの OpenAI 互換 `/v1` エンドポイントは `llm.provider: openai-compatible` で使う。

## What it does

セッションが終わると `SessionEnd` フックがローカル LLM でトランスクリプトを蒸留し、`vault/wiki/` にノート 1 本を作ってエンジンの `remember` 経由で保存する。プロンプトを打つと `UserPromptSubmit` フックが vault を検索し、過去のノートを最大 3 本 — それぞれ概念を共有する古いノート 1 本を添えて — フェンスの内側に差し込む。フェンスはエージェントに扱い方を告げる: 合うなら再利用し、コードと食い違うならどれが食い違うか言い、合わなければ触れない。次のセッション終了時にエンジンは何が起きたかを学ぶ — 再利用されたノートと反論されたノートがエッジになり、次の注入ではノートごとに `reused n×` / `contested n×` が付き、その順で並ぶ。

Codex にはセッションフックがないので、ホスト側ワーカーが 20 分ごとに適格なトランスクリプトを拾う。`make collect` は過去の Claude Code セッションを、`make collect-kimi` は過去の Kimi セッションを取り込み、`make distill-now` はセッションを終えずに今のものを取り込み、`make remember M="…"` は自分で書いたノートを保存する。

vault が唯一の原本だ: [Obsidian](https://obsidian.md) でそのまま開ける素の Markdown（タグと `[[wiki-NNNN]]` リンクは付いている）。`BORING_VECTOR=on` ならエンジンが pgvector インデックスとグラフ（ノート・概念・ツール・主張・セッション）を持ち、`make sync` で vault から再構築する。なければ想起は Markdown を直接読む。`make peek` は、何がセッションに注入され使われたかを示す読み取り専用のローカルページ（`127.0.0.1:7788`）を開く。

## Configuration

ポリシーは `boring.json` にある。`make up` が `boring.example.json` から作り、`boring.schema.json` で検証する。触ることになるキー:

| キー | 意味 | 読む場所 |
|---|---|---|
| `llm.provider` | `ollama`（モデルを取得）· `lmstudio`（アプリ側で載せる）· `openai-compatible` | `scripts/llm-providers/<provider>.sh`, `agents/shared/omb_env.py` |
| `llm.base_url` · `llm.model` | OpenAI 互換 `/v1` エンドポイントと、蒸留・`ask` に使うチャットモデル | 同上 |
| `llm.embed_model` · `llm.embed_dim` | 埋め込みモデルとベクトル次元 — モデルを変えたら次元を合わせて `make reset` | 同上。エンジンが起動時に次元を検査する |
| `note_lang` | `auto` · `ko` · `en` — ノートを書く言語 | `agents/shared/boring_config.py` |
| `repos[]` | パス/リモートの規則 → `origin`（`personal` / `company` / `mirror` / `community`）。会社由来の文章はエンジンの外に出ない | `agents/shared/boring_config.py` |
| `agents[]` | どのエージェントを配線するか（フック・MCP・ワーカー） | `agents/shared/agent_wiring.py` |

コンテナ内からは LLM を `host.docker.internal` で、ホストからは `localhost` で呼ぶ — `boring.example.json` はコンテナ形式になっている。

`.env` には秘密情報とランタイムの上書きだけを置く。コードが読む `BORING_*` 変数はすべて既定値つきで `.env.example` にある。両者が食い違えばコードが正で、その表をここに繰り返さない。実際に触るもの: `BORING_VECTOR=on`（pgvector + グラフ）、`BORING_LLM_API_KEY`（プロバイダが要求するとき）、`BORING_EVENT_SINK=spool`（イベントをエンジンではなくローカルファイルへ — テストとプローブが使う）。

## Commands

`make help` が 50 のターゲットを 1 行ずつ示す。日常で使うもの:

| コマンド | すること |
|---|---|
| `make up` / `make down` | スタックの起動 / 停止 |
| `make doctor` | スタック・フック・最新ノート・Codex ワーカーの診断。`make readiness` は所見 1 つで失敗する厳格版 |
| `make ask Q="…"` | 記憶から出典つきで質問 1 つに答える |
| `make remember M="…"` | いまノートを保存 |
| `make collect [N=1]` · `make collect-kimi [N=1]` · `make distill-now` | 過去セッションの取り込み · 現在セッションの取り込み |
| `make sync` | vault からインデックスとグラフを再構築（4 時間ごとにも走る） |
| `make peek` · `make events [N=20]` · `make usage` | 注入と利用の状況 · 最近のワークフローイベント · ローカルトランスクリプトからのトークン使用量 |
| `make guard` · `make quality` · `make eval` | 構造ゲート（fmt・clippy・テスト・Python）· リリース契約ゲート · 想起の回帰ゲート |

エンジンは `http://localhost:7700/mcp` で MCP も話す。`install.sh` が Claude Code・Kimi・Cursor・Codex に登録し、リポジトリ直下の `.mcp.json` がほかのクライアント向けの標準エントリ。

利用可能な tools（22個）: `recall`, `neighbors`, `claims`（記憶の想起）· `code_search`, `code_symbol`, `code_index_status`（別建ての AST コードコーパス）· `ask`, `brief`, `weekly_brief`, `project_status`, `decisions`, `risks`, `next_actions`, `stalled`（生成系 — LLM を回す）· `context`, `corpus_status`, `events`, `config_get`（構造化 / 自己診断）· `remember`, `forget`, `classify_repo`, `sync`（書き込み / 保守）。

既定の wiki 優先モード（`BORING_VECTOR=off`）では、グラフ・新しさ順・イベント DB を要するツールは `BORING_VECTOR=on` にするまで JSON-RPC `-32603` を返す: `neighbors`, `claims`, `corpus_status`, `events`, `brief`, `weekly_brief`, `project_status`, `decisions`, `risks`, `next_actions`, `stalled`。残りはなしで動く: `recall`, `ask`, `context`, `remember`, `forget`, `sync`, `config_get`, `classify_repo`, `code_index_status`, `code_search`, `code_symbol`（コード系 3 つは有効な `code_index` ソースと事前の `code-sync` が必要）。

## Status

以下の数字にはすべて、それを出したコマンドがある。

- 想起の回帰フロア: 18 文書のフィクスチャコーパスでゴールデンクエリ 22/22、MRR 1.000（`make eval`）。配線のフロアであって品質の主張ではない — 18 文書には近接競合がなく、本番にはある。
- 想起の精度: LLM 判定で関連 6 / 判定 24。その判定器を校正する人手監査が下限 30 に達していないため、精度の数字はまだ出さない（`make peek`, `label_core.py`）。
- 注入したノートは使われるか: 事前登録した測定（注入ノートのプロンプト当たり uptake 対 同一プールの対照群、`docs/PRD.md` §2）が `agents/shared/verdict_core.py` に登録された窓で走っている。`make peek` が進み具合を示す。サンプル下限（セッション 20、注入プロンプト 200）を満たすまで判定は引用しない。
- 埋め込み: `bge-m3` が MacBook Pro M5 Pro 48 GB のローカル Ollama でテキスト当たり平均 0.105 s（`make bench-embed`）。RAM 階層ごとの蒸留モデル対とレイテンシ: `make bench-llm-tier TIER=16gb|32gb`。結果は `docs/reports/llm-pair-matrix.md`。
- 配達: `/health` が `build_sha` を報告し、エンジン・ホスト CLI バイナリ・インストール済みフックスクリプトがチェックアウトより古ければ `make doctor` が失敗する。

## Non-goals

- クラウドなし、チーム共有メモリなし、社内ナレッジベースの取り込みなし — vault は一人のセッション経験だ。
- 対話型 UI 製品はやらない。`make peek` は読み取り専用のループバックページで、そこが境界。
- Windows は未対応: `hooks/` が後方互換のためシンボリックリンクを使う。macOS と Linux で検証済み。
