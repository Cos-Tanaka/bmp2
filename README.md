# BPM / 進捗監視盤

Backlog の親課題・子課題から予実進捗をリアルタイムで可視化するダッシュボード。

## 構成

```
bpm/
├── docker-compose.yml
├── .env.example          # 環境変数テンプレート
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app.py            # Flask API（Backlog API 取得・整形）
└── frontend/
    ├── Dockerfile
    ├── nginx.conf         # リバースプロキシ設定
    └── html/
        └── index.html     # ダッシュボード画面
```

## セットアップ

### 1. 環境変数の設定

```bash
cp .env.example .env
vi .env
```

| 変数名 | 説明 | 例 |
|---|---|---|
| `BACKLOG_SPACE` | スペース URL | `yourspace.backlog.com` |
| `BACKLOG_API_KEY` | Backlog API キー | Backlog > 個人設定 > API で発行 |
| `BACKLOG_PROJECT_KEY` | プロジェクトキー | `ASPIT` |
| `CACHE_TTL` | キャッシュ秒数（デフォルト60） | `60` |
| `PROGRESS_FIELD_NAME` | 進捗率カスタム属性名（省略可） | `進捗率` |

### 2. 起動

```bash
docker compose up -d
```

ブラウザで `http://サーバIP` を開く。

### 3. ログ確認

```bash
docker compose logs -f backend    # Flask ログ
docker compose logs -f frontend   # nginx ログ
```

### 4. 停止

```bash
docker compose down
```

## API エンドポイント

| パス | 説明 |
|---|---|
| `GET /api/issues` | 親課題＋子課題の進捗データ（JSON） |
| `GET /api/health` | バックエンド死活確認 |

## 健全性ロジック

| 判定 | 条件 |
|---|---|
| 🔴 危険 | 期限超過 OR 実績工数 > 予定工数 × 120% |
| 🟡 注意 | 期限まで3日以内 OR 実績工数 > 予定工数 |
| 🟢 正常 | 上記以外 |

## データ更新

- ブラウザが **60秒ごと** に `/api/issues` をポーリング
- Flask 側でも同 TTL でキャッシュ（Backlog API の呼び出し回数を抑制）
- 手動更新は「↻ 更新」ボタン

## カスタマイズ

- ポーリング間隔: `index.html` の `POLL_MS`（ミリ秒）
- キャッシュ TTL: `.env` の `CACHE_TTL`（秒）
- 進捗率カスタム属性名: `.env` の `PROGRESS_FIELD_NAME`
