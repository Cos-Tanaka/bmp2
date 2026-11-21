# 改修計画書：実績登録時の SQLite ロックエラー対策と Backlog との差異解消

- 作成日: 2026-11-21（同日改訂: コードとの照合レビューの指摘を反映。タイムアウト配分・apiKey 露出範囲・WAL 下のバックアップ/ロールバック・日付条件の書き換え等）
- 対象: `backend/app.py`（SQLite 接続・インデックス・実績追加/削除の処理順・保守作業取込）。フロントエンドの変更は原則なし
- 方針: **SQLite のまま修正する**（DB 移行はしない。根拠は §3）
- 作業場所: **Windows PC の Docker 上にある検証環境で修正・検証し、本番（社内の Ubuntu サーバー。ホスト・ユーザー・配置先は公開リポジトリのため記載しない）には検証完了後に別途反映する**。本計画書はリポジトリの `docs/` に置き、作業用ファイル（本番 DB コピー・検証スクリプト）は `C:\temp` に置く。本番 DB・本番 Backlog への書き込みを伴う検証はしない
- 調査（§2・§3）は本番環境で読み取りのみで実施済み

## 1. 背景・事象

実績工数を登録すると画面にエラーが出て BPM の実績履歴（SQLite）に残らない。一方、Backlog の `actualHours` には加算されているため、BPM と Backlog の工数に差異が出ている。

## 2. 調査結果（2026-11-21、本番ログより）

### 2.1 エラーの内容

- `sqlite3.OperationalError: database is locked` が直近 5 日間で 44 件（バックエンドログ）
- 発生箇所は `with get_db() as conn:` の終了時（＝コミット）。INSERT 自体は通っており、コミットに必要な排他ロックを 5 秒（Python sqlite3 の既定タイムアウト）待って失敗している

### 2.2 原因

| # | 原因 | 根拠 |
|---|---|---|
| 1 | 日別実績画面（`worklog-matrix.html`）が 60 秒ごとに絞り込みなしの `GET /api/worklog/search` を呼ぶ。このクエリは親案件ごとの相関サブクエリ（`parent_title` 取得）をインデックスなしで回すため **約 8.7 秒**かかり、その間 SQLite の共有ロックを保持する | 実測 8.1〜8.7 秒。直近 24h で 4,523 回呼び出し。`app.py` `api_worklog_search` |
| 2 | `journal_mode=delete`（既定）では、読み取り中の共有ロックがあるとコミットが待たされる | `PRAGMA journal_mode` = `delete` |
| 3 | 失敗時刻と検索の実行区間が一致 | 例: 検索開始 01:02:18.9 → 実績登録失敗 01:02:26.8 → 検索終了 01:02:27（UTC、nginx ログと `t=` パラメータから算出） |
| 4 | `worklog` テーブル 32,152 行のうち有効行は 3,197 行。**29,102 行は保守作業（`FIX-MAINT`）の論理削除済み行**で、保守工数取込のたびに増える。検索の遅さを増幅している | `SELECT parent_key, count(*) ...` |
| 5 | `get_db()` で開いた接続を閉じていない（`with` はコミット/ロールバックのみで close しない）。プロセス内に 9 本の接続が残存 | `/proc/<pid>/fd`。ただし CPython では関数を抜けて参照がなくなった接続は自動で close されるため、ロックエラーの主因ではない（明示 close は作法として行う） |

### 2.3 Backlog との差異が出る理由

| API | 処理順 | 失敗時の結果 |
|---|---|---|
| `POST /api/worklog/<key>`（実績追加） | ① Backlog の `actualHours` を加算 → ② SQLite に INSERT | ② だけ失敗し、**Backlog にだけ加算が残る**。ユーザーが再試行すると、そのたびに Backlog だけ加算される |
| `DELETE /api/worklog/entry/<id>`（履歴削除） | ① SQLite を論理削除（未コミット）→ ② Backlog から減算 → ③ コミット | ③ だけ失敗し、**Backlog からだけ減算される**。再試行のたびに減算される |
| `PUT /api/fixed-worklog` / `POST /api/fixed-worklog/maintenance-import` | SQLite のみ | 登録されないだけで、Backlog との差異は出ない |

### 2.4 失敗した書き込みリクエスト（nginx ログ、時刻は UTC）

実績追加（Backlog だけ加算された可能性あり）:

| 日時 | 課題 | 日時 | 課題 |
|---|---|---|---|
| 11/17 00:07:57 | V2A9-2516 | 11/18 08:24:03 | V2A9-2138 |
| 11/17 04:42:33 | V2A9-2502 | 11/18 09:18:33 | V2A9-1455 |
| 11/17 06:14:56 / 06:18:55 / 06:19:17 | V2A9-2575（3 回） | 11/18 10:27:56 / 10:28:13 | V2A9-2374（2 回） |
| 11/17 08:52:40 | V2A9-1553 | 11/19 00:08:27 | V2A9-2408 |
| 11/17 09:48:34 / 09:48:53 | V2A9-2189（2 回） | 11/19 00:25:19 | V2A9-2539 |
| 11/18 00:17:54 | V2A9-2142 | 11/19 00:27:20 | V2A9-2464 |
| 11/18 01:41:11 | V2A9-2288 | 11/19 07:08:34 | V2A9-1407 |
| 11/18 01:43:14 / 01:44:00 | V2A9-2374（2 回） | 11/19 09:12:24 | V2A9-1461 |
| 11/18 04:14:22 | V2A9-2122 | 11/19 23:25:10 / 23:25:34 | V2A9-1654（2 回） |
| 11/18 08:22:59 | V2A9-2142 | 11/20 00:12:55 | V2A9-2606 |
| 11/18 08:23:56 | V2A9-2138 | 11/20 04:00:55 | V2A9-2149 |
| | | 11/20 09:12:56 | V2A9-2537 |
| | | 11/21 01:02:26 | V2A9-2517 |
| | | 11/21 01:06:49 | V2A9-1503 |

履歴削除（Backlog からだけ減算された可能性あり）: entry 31412 を 11/19 09:07:23 / 09:07:34 / 09:09:25 の 3 回。

固定作業登録・保守取込の失敗（差異なし。再登録が必要な可能性あり）: `PUT /api/fixed-worklog` 6 回、`maintenance-import` 4 回。

※ 登録時間（hours）は nginx ログに残らないため、差異の量は §5.5 の突合で確定する。nginx ログの保持期間より前の失敗は追えない。

## 3. SQLite のままとする根拠

| 項目 | 実測 | 評価 |
|---|---|---|
| データ量 | 32,152 行 / 4 MB（整理後 3,603 行 / 608 KB） | SQLite の想定範囲を大きく下回る |
| 書き込み | 7 日で 341 件、ピーク 1 時間 40 件 | WAL なら毎秒数百件以上に耐える |
| 書き込みプロセス | gunicorn 1 プロセス（4 スレッド） | 単一プロセスからの書き込みは SQLite の得意領域 |
| 利用者 | クライアント 12 台/日 | 問題なし |
| 検索の改善余地 | インデックス 1 本で 8.1 秒 → **0.024 秒**（DB コピーで検証、結果一致を確認） | ロックの原因自体が消える |

バックエンドの複数プロセス化・複数台構成・他システムからの直接書き込みが必要になった時点で、改めて移行を検討する。

## 4. 修正内容

### 4.1 接続設定（`get_db()`）

- `sqlite3.connect(DB_PATH, timeout=15)`（ロック待ちを 5 秒 → 15 秒。30 秒にしない理由は下の「タイムアウト配分」）
- `PRAGMA busy_timeout` は `timeout` で設定されるので個別指定は不要
- `init_db()` の**最初の文**で `PRAGMA journal_mode=WAL` を設定（DB ファイルに永続化される）。トランザクション中は変更できないため、CREATE TABLE 等より前に置く。WAL では読み取りが書き込みのコミットを妨げない
- `PRAGMA synchronous=NORMAL`（WAL での推奨値）は**接続ごとの設定で DB に永続化されない**ため、`init_db()` ではなく `get_db()` の中で毎回実行する
- `get_db()` を **close まで行うコンテキストマネージャ**に変更する（`contextlib.contextmanager` で `try: yield conn; conn.commit() except: conn.rollback(); raise finally: conn.close()`）。既存の呼び出し側の `with get_db() as conn:` はそのまま動く形にする。`with` 内での `return`（`api_worklog_delete` の 409 など）はコミット、`abort()` は例外なのでロールバックになる点も従来と同じ
- WAL では `worklog.db-wal` / `worklog.db-shm` が同じディレクトリにできる。`bpm-data` は名前付きボリューム（Linux ファイルシステム上）なので追加の設定は不要。バックアップ・復元時の扱いは §5.1・§6 を参照

**タイムアウト配分**: 前段の nginx は `proxy_read_timeout 30s`（`frontend/nginx.conf`）。1 リクエストの最悪時間が 30 秒を超えると、画面には 504 が出る一方でバックエンドは処理を続けてコミットまで成功し、利用者の再試行で Backlog・BPM の両方に二重計上される。実績追加の最悪時間を 30 秒未満に収める:

| 区間 | 修正前 | 修正後 |
|---|---|---|
| SQLite ロック待ち | 5 秒 | 15 秒（`SQLITE_TIMEOUT`） |
| Backlog GET + PATCH（実績追加・削除） | 各 15 秒 | 2 回合わせて最大 10 秒（`WORKLOG_BACKLOG_BUDGET`）。かつリクエスト開始から 25 秒（`WORKLOG_DEADLINE`）以内 ※ |
| Backlog 呼び出し（その他） | 15 秒 | 15 秒のまま（`BACKLOG_TIMEOUT`） |

※ `requests` の `timeout=8` のような単一値は「接続」と「受信の途切れ」それぞれの上限で、呼び出し全体の上限ではない（接続 8 秒 + 応答待ち 8 秒で 16 秒かかり得る）。そのため固定値ではなく期限を決め、各呼び出しの直前に残り時間から `(connect, read)` を計算する（`_deadline_timeout`）。残りが 1 秒未満なら Backlog を呼ばずに 503 で失敗させる（Backlog・SQLite とも未変更）。合計はロック待ち 15 + Backlog 10 = 25 秒 < 30 秒。
ロックの保持時間（Backlog 10 秒）を他の書き込みのロック待ち（15 秒）より短くしているので、実績登録 1 件の Backlog 応答が遅くても、固定作業登録・保守取込・カレンダー取込が `database is locked` にならない。

### 4.2 インデックス（`init_db()`）

- `CREATE INDEX IF NOT EXISTS idx_worklog_parent ON worklog(parent_key, added_at, id)` — `api_worklog_search` の `parent_title` サブクエリ用（効果の大半はこれ）
- `CREATE INDEX IF NOT EXISTS idx_worklog_name_day ON worklog(name, added_at)` — 固定作業・保守取込用。ただし現状の条件 `date(added_at) = ?` は関数を通すため `added_at` 部分にインデックスが効かない。次のように**範囲条件へ書き換えた上で**入れる。効果がなければ入れない（§5.3 で実測して判断）
  - `date(added_at) = ?` → `added_at >= ? AND added_at < ?`（当日 `YYYY-MM-DD` と翌日 `YYYY-MM-DD` を渡す）。`added_at` は常に `YYYY-MM-DDTHH:MM:SS` 形式なので文字列比較で日付範囲になる
  - 対象: `_get_fixed_hours`、`api_fixed_worklog_put` の置換、`api_fixed_worklog_maintenance_import` の置換。`api_worklog_search` の `date` 絞り込みも同様に書き換えてよい（GROUP BY / SELECT の `date(added_at)` はそのまま）
  - 前提確認: 既存行がすべてこの形式であること（`SELECT count(*) FROM worklog WHERE added_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T*'` が 0）を本番 DB コピーで確認する

### 4.3 実績追加・削除の処理順（差異の再発防止）

**実績追加 `api_worklog_add`**:

1. SQLite で `BEGIN IMMEDIATE`（書き込みロックを先に確保）→ INSERT
2. Backlog の `actualHours` を取得・加算
3. Backlog 成功 → SQLite コミット / Backlog 失敗 → SQLite ロールバック
4. コミット後（`with` の外）で `reflect_issue_change(updated)`

こうするとロックが取れない場合は Backlog に触る前に失敗する。Backlog の PATCH 成功後にコミットが失敗する余地は残るが、書き込みロックを確保済みなのでコミットの失敗は実質なくなる。

- 実装: 今の `add_worklog()` は自前で接続を開いてコミットまで行うため、この順序では使えない。`api_worklog_add` の中で 1 つの `with get_db() as conn:` に INSERT と Backlog 呼び出しをまとめる（`add_worklog()` の呼び出し元は `api_worklog_add` だけなので削除する）。Python 3.12 の既定（`isolation_level=""`）のまま、`conn.execute("BEGIN IMMEDIATE")` を明示的に実行すれば暗黙の BEGIN は重ならない。
- なお効果の中心は WAL 化と「INSERT を Backlog より先に行う」ことで、`BEGIN IMMEDIATE` は最初の文が書き込みである限り実質同じ。意図を明示するために入れる。
- Backlog API の呼び出し中（GET + PATCH で最大 10 秒、§4.1）は書き込みロックを保持し続ける。WAL なので読み取りは妨げない。他の書き込みのロック待ちは 15 秒なので、保持中の 1 件が終わるまで待てる。実績登録の頻度（ピーク 1 時間 40 件）なら許容範囲。
- PATCH 成功後に SQLite のコミットが失敗した場合（ディスクフル等）は Backlog だけが更新済みになるため、`commit_after_backlog` が「Backlog 反映済み・BPM 未保存」の警告ログを課題キー・時間付きで出す。
- 副次効果: Backlog の「取得 → 加算 → 更新」は原子的でなく、同じ課題へ同時に加算すると後の更新が先の更新を上書きする。書き込みロックを持ったまま行うことで、BPM からの同時加算は直列化され、この取りこぼしも防げる（Backlog 画面からの直接編集との競合は防げない）。
- **残る差異**: Backlog 側で PATCH が適用されたのに応答がタイムアウト（`requests.Timeout`）した場合、SQLite はロールバックされ Backlog にだけ加算が残る。完全には防げないため、PATCH の失敗のうち Backlog 未変更と言い切れないもの（応答待ちのタイムアウト、接続断、5xx、応答の解析失敗など）は課題キー・時間・名前を `log.warning` で「Backlog 反映状態不明」として出し、§5.5 の突合で拾えるようにする。Backlog 未変更と言い切れるもの（GET の失敗、PATCH の `ConnectTimeout`、4xx、期限切れで呼ばなかった場合）は「Backlog 未反映」として区別して出す。
- 代替案: ロックを持たずに Backlog を先に更新し、SQLite が失敗したら Backlog を元の値に戻す（補償処理）。戻す処理自体が失敗し得るので、上記を採用する。

**履歴削除 `api_worklog_delete`**: 現状も「SQLite 更新 → Backlog → コミット」の順。`BEGIN IMMEDIATE` で書き込みロックを先に取る点、Backlog 呼び出しの期限（§4.1）、PATCH 失敗・コミット失敗の警告ログを実績追加と揃える。

### 4.4 保守作業取込の肥大化対策

- `api_fixed_worklog_maintenance_import` の置換を **論理削除 → 物理削除**（`DELETE`）に変える。保守作業はスプレッドシートが正であり、BPM 側に削除履歴を持つ意味がないため
- あわせて、現状の「人数 × 日数」回（30 人 × 31 日なら約 900 回）の UPDATE を、1 回の DELETE にまとめて書き込みロックの保持時間を短くする: `DELETE FROM worklog WHERE parent_key = 'FIX-MAINT' AND name IN (<シート上の氏名>) AND added_at >= <day_cols の最小日> AND added_at < <day_cols の最大日の翌日>`。シートの日付列は月内で連続しているため、置換範囲は従来の (氏名, 日付) ごとの置換と同じになる（日付列が連続していることを `_parse_maintenance_csv` で確認し、連続していなければ従来どおり日ごとに削除する）
- 既存の論理削除済み `FIX-MAINT` 行（約 29,100 行）を削除し、`VACUUM` する作業を本番反映手順に含める（§6）
- `PUT /api/fixed-worklog`（手入力の固定作業）は論理削除のまま（件数が少なく、履歴として意味がある）

### 4.5 API キーの露出（ログと画面のエラー応答）

`backlog_get` / `backlog_patch` は apiKey を URL のクエリで送っているため、例外メッセージに `?apiKey=...` を含む URL が入る。露出経路は 2 つある:

| 経路 | 例外 | 出力先 |
|---|---|---|
| `except requests.HTTPError` → `log.error(..., e)` | HTTPError（`... for url: https://.../issues/X?apiKey=...`） | ログ |
| `except Exception` → `log.exception(...)` と `abort(500, description=str(e))` | `requests.Timeout` / `ConnectionError`（`Max retries exceeded with url: /api/v2/...?apiKey=...`） | ログ（トレースバック込み）と**ブラウザへの 500 応答本文** |

対策:

- `apiKey=[^&\s'"]+` を `apiKey=***` に置換する関数 `_redact()` を用意する
- ロガーに logging.Filter を付け、メッセージ・引数・例外テキストをまとめて伏せる（`log.exception` のトレースバックも対象。`record.exc_text` を整形後に置換するため、Filter ではなく Formatter の `formatException` を上書きする方法でもよい）
- `abort(500, description=str(e))` / `abort(502, ...)` の description も `_redact(str(e))` を通す
- 根本対策（apiKey をヘッダで送る）は Backlog の API キー認証がクエリ指定のみのため取らない

### 4.6 変更しないもの

- 日別実績画面の 60 秒ポーリング（クエリが 0.02 秒になれば問題ない）
- フロントエンドのエラー表示
- DB スキーマ（インデックス追加のみ）

## 5. 検証環境（Windows PC の Docker）での作業手順

### 5.1 環境準備

1. 作業開始前に、検証環境のソースと DB の状態を確認する。ソースのフォルダ、ブランチ、`.env` の接続先（Backlog のスペース・プロジェクト）、`bpm-data` ボリュームの `worklog.db` の行数を確認する。
   - ブランチ: 検証環境（`C:\Cos-Tanaka\bpm2`）のローカル `main` は 94843c6 で止まっており、3f7c085 は `origin/main` にしかない。`git fetch` してから `git switch -c fix/sqlite-lock origin/main` で作る
   - 現在のチェックアウト（`feature/gantt-drag-dates`）には `data/calendar/work_calendar_2026.csv` の削除が未コミットで残っている。切り替える前に、意図した削除かを確認して扱い（コミット / `git restore` で戻す）を決める
2. 検証環境の DB を本番 DB のコピーに差し替える（V1・V2 を本番と同じデータ量で再現するため）。差し替える前に、検証環境の既存 DB を `C:\temp` にバックアップする:
   - コピーはファイルの単純コピーではなく **SQLite のバックアップ API** で取る。書き込み中のファイルをそのままコピーすると壊れたコピーになり得る（WAL 化後は `-wal` にしかないコミット済みデータが欠ける）
   - 本番側（読み取りのみ）: `docker compose exec backend python -c "import sqlite3; s=sqlite3.connect('/app/data/worklog.db'); d=sqlite3.connect('/tmp/worklog_prod_copy.db'); s.backup(d); d.close(); s.close()"` → `docker compose cp backend:/tmp/worklog_prod_copy.db /tmp/worklog_prod_copy.db`
   - Windows 側: `scp <ユーザー>@<本番ホスト>:/tmp/worklog_prod_copy.db C:\temp\worklog_prod_copy.db` → `docker compose stop backend` → `docker compose cp C:\temp\worklog_prod_copy.db backend:/app/data/worklog.db`（停止中のコンテナにも cp できる。検証環境側に `worklog.db-wal` / `-shm` があれば先に削除）→ `docker compose start backend`
   - 取得後、本番のホストとコンテナ内の `/tmp/worklog_prod_copy.db` は削除する
3. バックエンドを変更したら `docker compose up -d --build backend` で反映する。検証スクリプト（§5.3）はコンテナ内で実行する（`docker compose exec backend python /tmp/verify.py` など。スクリプトは `docker compose cp` で入れる）

### 5.2 本番 Backlog に書き込まないための制約

- 検証環境の `.env` が本番と同じ Backlog スペース・プロジェクトを向いている場合、**実績追加・削除・課題更新の API は本番 Backlog の課題を書き換える**。その場合は検証環境の画面からこれらの操作をしてはいけない
- 書き込みを伴う検証は、Flask のテストクライアント上で `backlog_get` / `backlog_patch` をスタブに差し替えた**検証スクリプト**で行う（§5.3）。スクリプトはリポジトリに含めない（`C:\temp\verify\` に置く）

### 5.3 検証項目

| # | 内容 | 合格基準 |
|---|---|---|
| V1 | 本番 DB コピーで `GET /api/worklog/search`（絞り込みなし）の時間 | 修正前 8 秒前後 → 修正後 0.1 秒未満。結果は修正前と完全一致 |
| V2 | 絞り込みなし検索を別スレッドで連続実行しながら実績追加を 50 回 | ロックエラー 0 件（修正前のコードで同じスクリプトを流し、エラーが再現することも確認） |
| V3 | Backlog スタブが PATCH で例外を投げる場合の実績追加 | SQLite に行が残らない（ロールバック） |
| V3b | Backlog スタブが GET は成功し、PATCH で `requests.Timeout` を投げる場合の実績追加・履歴削除 | SQLite はロールバックされ、課題キー・時間・名前を含む「Backlog 反映状態不明」の警告ログが出る。GET で失敗した場合は別の文言になる |
| V4 | Backlog スタブ成功時の実績追加 | SQLite に 1 行、スタブに加算値が渡る |
| V5 | 履歴削除（Backlog 成功 / 失敗） | 成功時は `deleted_at` が入り減算値が渡る。失敗時は `deleted_at` が NULL のまま |
| V6 | 保守取込（CSV をスタブ）を 2 回連続実行 | 2 回目の後も `FIX-MAINT` の有効行数・合計時間が 1 回目と同じで、削除済み行が増えない。他の月・シートに無い氏名の `FIX-MAINT` 行は変わらない |
| V7 | `PUT /api/fixed-worklog` の再登録・0 クリア、固定作業モーダルの読込（`_get_fixed_hours`） | 従来どおり二重計上しない。日付条件を範囲に書き換えた前後で、本番 DB コピー上の結果が一致する |
| V8 | `journal_mode` / `synchronous` | `journal_mode` は `wal` で再起動後も維持される。`get_db()` で開いた接続の `synchronous` が `1`（NORMAL） |
| V9 | 接続のリーク | 100 リクエスト後もプロセスが開いている `worklog.db` のハンドル数が増え続けない。※ CPython の参照カウントで自動 close されるため修正前でも合格し得る。修正前との比較ではなく、修正後の回帰確認として扱う |
| V10 | 画面（読み取りのみ） | 日別実績・実績一覧・ガント・月別工数が従来どおり表示される |
| V11 | API キーの露出 | スタブで `HTTPError`・`requests.Timeout`・`requests.ConnectionError`（いずれも URL に `apiKey=` を含むメッセージ）を発生させ、ログ（トレースバック含む）と API の 502/500 応答本文の両方で `apiKey=***` になっている |
| V12 | 応答時間の上限 | 書き込みロックを別スレッドで 10 秒以上保持させ、Backlog スタブを各 8 秒待たせた状態で実績追加 → 30 秒未満で応答（成功またはロックエラー）が返る |
| V13 | `added_at` の形式 | 本番 DB コピーで §4.2 の GLOB チェックが 0 件 |

### 5.4 成果物

- `fix/sqlite-lock` ブランチ（`backend/app.py` の修正、`CLAUDE.md` の該当箇所の更新）
- 検証結果（V1〜V13 の結果と計測値）を本計画書の末尾に追記

### 5.5 既存差異の突合（読み取りのみ）

本番 Backlog の読み取り（課題取得）と本番 DB コピーで行う。修正とは独立して実施できる。

1. §2.4 の課題ごとに、Backlog の `actualHours` と SQLite の `SUM(hours) WHERE child_key = ? AND deleted_at IS NULL` を並べる
2. 差がある課題を一覧にする（Backlog に直接入力された工数も差として出るため、差がすべて今回のエラーによるものとは限らない。Backlog の課題の変更履歴で `actualHours` の変化を確認して切り分ける）
3. 修正の反映後に新たな差異が出ていないかは、§4.3 の「Backlog 反映状態不明」警告ログに出た課題を同じ方法で確認する
4. **補正は自動では行わない**。一覧を担当者に確認してもらい、Backlog 側を直すか BPM 側に履歴を追加するかを決めてから、本番で手作業で行う

## 6. 本番反映手順（検証完了後、別途実施）

1. 本番の `worklog.db` をバックアップする。ファイルコピーではなくバックアップ API で取る（§5.1 と同じ方法）:
   `docker compose exec backend python -c "import sqlite3; s=sqlite3.connect('/app/data/worklog.db'); d=sqlite3.connect('/app/data/worklog.db.bak-YYYYMMDD'); s.backup(d); d.close(); s.close()"`
   バックアップは同じ `bpm-data` ボリューム内に置かれる。ボリューム障害に備えてホスト側にも `docker compose cp` で取り出しておく
2. `fix/sqlite-lock` を main にマージして本番に pull
3. `docker compose up -d --build backend`（`init_db()` で WAL 化とインデックス作成が行われる）
4. 削除済み `FIX-MAINT` 行の物理削除と `VACUUM`（利用者の少ない時間帯に。数秒で終わる見込み）。イメージ（`python:3.12-slim`）には sqlite3 コマンドが無いため Python で実行する:
   ```
   docker compose exec backend python -c "import sqlite3; c=sqlite3.connect('/app/data/worklog.db', timeout=30); n=c.execute(\"DELETE FROM worklog WHERE parent_key='FIX-MAINT' AND deleted_at IS NOT NULL\").rowcount; c.commit(); c.execute('VACUUM'); c.close(); print(n)"
   ```
   削除件数が §2.2 の約 29,100 件前後であることを確認する
5. 確認: ログに `database is locked` が出ないこと、`PRAGMA journal_mode` が `wal`、日別実績の表示、実績登録 1 件（実際の業務入力で確認）

ロールバック（WAL 化後の DB を戻すため、手順を守る）:

1. `docker compose stop backend`（接続をすべて閉じる）
2. ボリューム内の `worklog.db-wal` と `worklog.db-shm` を**削除してから**、バックアップを `worklog.db` に戻す。新しい DB の `-wal` が残っていると、戻した DB にそれが適用されて壊れる
   - 停止中はボリュームに `docker compose exec` で入れないため、`docker run --rm -v <プロジェクト名>_bpm-data:/data alpine sh -c "rm -f /data/worklog.db-wal /data/worklog.db-shm && cp /data/worklog.db.bak-YYYYMMDD /data/worklog.db"` などで行う（ボリューム名は `docker volume ls` で確認）
3. 旧イメージ（旧コード）で `docker compose up -d backend`
- 戻すのがコードだけで DB はそのまま使う場合は、上記 2 は不要（WAL モードの DB は旧コードでもそのまま読める）。ただしバックアップ以降に登録された実績を失わないよう、DB を戻すかどうかは状況で判断する

## 7. スコープ外

- PostgreSQL などへの DB 移行
- 既存差異の自動補正（§5.5 の一覧作成まで）
- 日別実績画面のポーリング間隔の変更

## 8. リスクと対策

| リスク | 対策 |
|---|---|
| 検証環境から本番 Backlog を書き換える | 書き込み系は Backlog スタブでのみ検証（§5.2）。検証環境の画面から実績登録・課題更新をしない |
| 本番 DB の個人名・工数データを Windows PC に置く | `C:\temp` と検証環境のボリュームに限定し、検証後に削除するか、検証環境の元の DB に戻す |
| Backlog 呼び出し中に書き込みロックを持つため、他の書き込みが待たされる | WAL で読み取りは影響を受けない。ロック保持は最大 10 秒、他の書き込みのロック待ちは 15 秒なので、1 件分の保持では失敗しない |
| `FIX-MAINT` を物理削除にすると、保守取込の誤操作を戻せない | スプレッドシートが正なので再取込で戻せる。反映前に DB をバックアップする |
| 応答が nginx の 30 秒を超え、画面は 504 なのにバックエンドは成功 → 再試行で二重計上 | ロック待ち 15 秒・Backlog（GET + PATCH）は期限付きで最大 10 秒、合計 25 秒に収める（§4.1、V12） |
| Backlog の PATCH は適用されたが応答がタイムアウト → Backlog にだけ加算が残る | 完全には防げない。警告ログ（§4.3）を出し、§5.5 の突合で拾う |
| WAL 化後に DB ファイルだけをコピー・復元して壊す | バックアップは SQLite のバックアップ API で取り、復元時は `-wal` / `-shm` を削除してから戻す（§5.1・§6） |
| 日付条件の範囲化で、形式の異なる `added_at` が漏れる | 本番 DB コピーで形式を確認し（V13）、書き換え前後の結果一致を確認する（V7） |

## 9. 検証結果（2026-11-21、Windows PC の Docker）

### 9.1 検証方法

- 本番 DB コピーはまだ取得していない（§5.1 の手順 2 は未実施）。代わりに、本番と同規模の**合成 DB** を使った。全 32,550 行（有効 3,300 行、論理削除済み `FIX-MAINT` 29,100 行）、親案件 150 件、担当者 30 名、日付は 2026-05〜11 に散らした
- 旧コード（`origin/main` 3f7c085 の `backend/app.py`）と新コード（`fix/sqlite-lock`）を、同じ DB のコピーで使い捨てのコンテナ（`bpm2-backend` イメージ）を使って実行した。DB はコンテナ内の `/tmp` に置いた
- Backlog は `requests.get` / `requests.patch` をスタブに差し替えた。`BACKLOG_API_KEY` はダミーにしたので、ネットワークへの書き込みは発生しない
- スクリプトは `C:\temp\verify\`（`gen_db.py` / `stub.py` / `bench.py` / `verify.py`）。リポジトリには含めない

### 9.2 結果

| # | 結果 | 計測値・備考 |
|---|---|---|
| V1 | 合格 | 絞り込みなし検索は旧 4.53〜4.84 秒 → 新 0.036〜0.038 秒。結果 2,916 行は完全一致。日付絞り込み（4 日分）も完全一致 |
| V2 | 合格 | 合成 DB では旧コードの検索が 4.7 秒で、旧タイムアウトの 5 秒を下回るためエラーは再現しなかった（本番は 8.7 秒）。そこで削除済み行を倍にした負荷用 DB（61,650 行、旧検索 9.2〜9.4 秒）で再実行した。**旧: 50 回中 8 回が 500（database is locked）。Backlog 25.0h に対し SQLite 21.0h で、本番と同じ差異を再現**。新: 50 回すべて成功、合計 0.1 秒、Backlog と SQLite はどちらも 25.0h |
| V3 | 合格 | PATCH が HTTPError → 502、SQLite に行は残らない |
| V3b | 合格 | PATCH が Timeout → 500、SQLite はロールバックされ、「Backlog 反映状態不明」の警告に課題キー・+1h・名前が出る。GET が Timeout → 「Backlog 未反映」の別文言 |
| V4 | 合格 | 成功時は SQLite に 1 行、Backlog は 2.0 → 3.5 |
| V5 | 合格 | 削除時に Backlog が失敗（HTTPError / Timeout）→ `deleted_at` は NULL のまま。成功 → `deleted_at` が入り減算される。再削除 → 404 |
| V6 | 合格 | 2 回取り込んでも有効行は (414 行, 619.5h) のまま、削除済み行は 29,100 のまま。シートにない氏名・月の行は変わらない。取込 0.011 秒。日付列が飛んでいる CSV では、列にある日だけが置き換わる |
| V7 | 合格 | 再登録しても二重計上されない。0 を送るとクリアされる。`_get_fixed_hours` は範囲条件と `date()` 条件で 460 組すべて一致。不正な日付 → 400 |
| V8 | 合格 | `journal_mode=wal`（DB を開き直しても維持）、`synchronous=1` |
| V9 | 合格 | 100 リクエスト後も、開いたままの `worklog.db` ハンドルは 0 |
| V10 | 合格 | 検証環境の DB を `C:\temp\worklog_local_bak_20261121.db` にバックアップしてから、`docker compose up -d --build backend` で入れ替えた。起動時に WAL 化（`-wal`/`-shm` 生成）とインデックス 3 本を確認した。一覧・ガント・月別工数・日別実績・実施工数確認の各画面を読み取りのみで表示し、API はすべて 200、コンソールエラーなし、バックエンドログにエラーなし。日別実績 2026/09 の合計 525.5h は DB の有効行合計と一致。実施工数確認で日付 2026-09-01 を検索（範囲条件の経路）すると 14 件・30.75h になり、日別実績の 9/1 列と一致 |
| V11 | 合格 | HTTPError / Timeout / ConnectionError のいずれでも、ログ（トレースバック含む）とエラー応答の本文に API キーが出ず、`apiKey=***` になる |
| V12 | 合格 | （7b233b7 時点の固定タイムアウト方式での結果。4e1571f で期限方式に変わったため、現行の結果は §9.3）ロックが 12 秒取れない場合 → 10.0 秒で失敗し、Backlog は変更されない。ロック待ち 9 秒 + Backlog 7.9 秒 × 2 → 24.9 秒で成功（30 秒未満） |
| V13 | 一部 | 検証環境の実 DB（516 行）は形式外 0 件。本番 DB コピーでの確認は未実施 |

### 9.3 再検証（マージ後の main e899854、4e1571f を含む）

§9.2 は 7b233b7 時点のコードでの結果。PR #31 のレビュー対応 4e1571f で、タイムアウトの方式（リクエスト開始から 25 秒の期限と、ロック保持中の Backlog 予算 10 秒から、呼び出しごとに `(connect, read)` を計算）、ロック待ち（15 秒）、警告ログの分類が変わったため、マージ後の main で検証をやり直した。方法は §9.1 と同じ。スタブは `(connect, read)` のタプルを受け取り、遅延が read を超えると `ReadTimeout` を投げるように変更した。

**結果: 機能検証 46/46 合格、V1・V2 合格。**

| # | 結果 | 計測値・備考 |
|---|---|---|
| V1 | 合格 | 絞り込みなし検索 0.033〜0.037 秒（合成 DB）、0.065〜0.070 秒（負荷用 DB）。結果 2,916 行 |
| V2 | 合格 | 合成 DB・負荷用 DB のどちらでも、50 回すべて成功、Backlog と SQLite はどちらも 25.0h |
| V3〜V9・V11 | 合格 | §9.2 と同じ項目・同じ結果 |
| V3c | 合格 | PATCH の失敗の分類: 5xx → 502・「反映状態不明」、4xx → 502・「未反映」、ConnectTimeout → 500・「未反映」。いずれも SQLite に行は残らない |
| V3d | 合格 | PATCH 成功後に SQLite のコミットが失敗 → 500・「Backlog 反映済み・BPM 未保存」の警告（課題キー付き）。SQLite に行は残らず、Backlog は加算済み（想定どおり残る差異で、警告ログから突合する） |
| V3e | 合格 | 期限の残りが 1 秒未満 → Backlog を一度も呼ばずに 503。Backlog・SQLite とも変更なし、「未反映」の警告。入力エラーは従来どおり 400 |
| V12 | 合格 | 定数（ロック待ち 15 / 全体 25 / Backlog 予算 10）。ロックが 17 秒取れない → 15.0 秒で失敗し Backlog 未変更。ロック待ち 14 秒 + Backlog 3 秒 × 2 → 20.1 秒で成功。ロック待ち 14 秒 + Backlog 6 秒 × 2 → 22.1 秒で失敗（状態不明）。Backlog が応答しない → 7.0 秒で失敗（ロック保持は予算内）。タイムアウトは `(connect ≤ 3, read)` のタプルで、合計は残り時間以内 |
| V12e | 合格 | 実績登録が Backlog を待ってロックを持っている間（約 6.6 秒、および Backlog 無応答で約 7 秒）に固定作業を登録 → ロックエラーにならず、待って成功（6.1〜6.6 秒） |
| V10 | 合格 | 検証環境のバックエンドを main で入れ替え、起動と `/api/worklog/search` の応答（0.01 秒）を確認した |

**気付いた点（設計どおりの動作）**: 呼び出しごとに接続用の時間（最大 3 秒）を残り時間から差し引いてから read タイムアウトを決めるため、GET に時間がかかると PATCH の応答待ちが短くなる。例: ロック待ちの後に GET が 4 秒かかると、PATCH の残りは 6 秒でも read は 3 秒になり、4 秒かかる PATCH は打ち切られて「反映状態不明」になる。全体の上限を守るための保守的な配分で、Backlog は通常 1 秒以内に応答するので実運用への影響は小さい。ただし、Backlog が重い日は「反映状態不明」の警告が増え得るので、反映後にログを確認する。

最初の再検証で V12b・V12e が不合格だったのは、テスト側が旧方式の前提（Backlog 4〜4.5 秒 × 2 が予算内に収まる）のままだったため。遅延を 3〜3.3 秒に直して合格した。コードの不具合ではない。

### 9.4 残作業

- 本番 DB コピーでの V1・V13（§5.1 の手順 2。本番サーバーへの接続が必要）
- §5.5 の既存差異の突合
