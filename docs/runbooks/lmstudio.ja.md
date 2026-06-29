# LM Studio ランブック

## 目的

LM Studio を ohmyboring のローカル OpenAI-compatible バックエンドとして使います。エンジンはそのまま `/v1/chat/completions` と `/v1/embeddings` を呼び、provider のブートストラップとモデル id だけを変えます。

## 前提条件

- LM Studio がインストール済み。
- LM Studio Developer モードでローカルサーバーが有効。
- chat モデル 1 つと embedding モデル 1 つがロード済み。
- `jq`、`curl`、Docker、`make` が使える。

## 設定

`boring.json` の provider を `lmstudio` にし、Docker ランタイムでは `host.docker.internal` を使います:

```json
{
  "llm": {
    "provider": "lmstudio",
    "base_url": "http://host.docker.internal:1234/v1",
    "model": "<正確な chat model id>",
    "embed_model": "<正確な embedding model id>",
    "embed_dim": 768,
    "api_key_env": "BORING_LLM_API_KEY",
    "bootstrap": "manual"
  }
}
```

LM Studio が返す id をそのまま使います:

```bash
curl -s http://localhost:1234/v1/models | jq -r '.data[].id'
```

LM Studio CLI がインストールされていれば、ホストでも同じ状態を確認できます:

```bash
~/.lmstudio/bin/lms ls
~/.lmstudio/bin/lms ps
```

`text-embedding-nomic-embed-text-v1.5` のような embedding モデルだけでは足りません。ohmyboring には `/v1/chat/completions` 用の chat モデル 1 つと `/v1/embeddings` 用の embedding モデル 1 つが必要で、設定した chat モデルがなければ `make verify-llm` は失敗するのが正常です。

## 検証

```bash
make verify-llm
make up
make doctor
```

期待結果:

- `make verify-llm` が provider スクリプトを見つけ、`/v1/models` に到達し、設定した 2 つのモデル id を確認します。
- `make doctor` がエンジン正常と write door open を報告します。
- Hermes/Codex 取り込みが有効なら、`make doctor` が Codex ワーカー状態も表示します。

## Embedding 次元

Embedding モデルの次元は保存形式の契約です。よく使う値:

| モデル | `embed_dim` |
| --- | ---: |
| `bge-m3` | 1024 |
| `nomic-embed-text` / `text-embedding-nomic-embed-text-v1.5` | 768 |
| `text-embedding-3-small` | 1536 |

`llm.embed_model` を変えるときは `llm.embed_dim` も合わせ、vector モードを信頼する前に `make reset` を実行します。wiki-first recall は Markdown を直接読みますが、vector search、claims、graph、status、brief は vector store の形に依存します。

## トラブルシュート

| 症状 | 確認 |
| --- | --- |
| `/v1/models` が空 | LM Studio ローカルサーバーを起動し、アプリでモデルをロードします。 |
| `/v1/models` に embedding モデルだけが出る | chat モデルをダウンロードしてロードし、`llm.model` に正確な id を設定します。 |
| `make verify-llm` がモデルを見つけない | `/v1/models` の正確な id をコピーします。表示名だけでは足りません。 |
| Docker が LM Studio に届かない | `boring.json` では `localhost` ではなく `http://host.docker.internal:1234/v1` を使います。 |
| ホスト上のベンチマークが LM Studio に届かない | `scripts/bench-llm.py --base-url` では `http://localhost:1234/v1` を使います。 |
| embedding upsert が失敗 | `llm.embed_dim` が embedding モデルと合っていません。次元を修正し、vector DB を resetします。 |
