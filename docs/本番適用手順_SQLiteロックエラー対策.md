# 本番適用手順：SQLite ロックエラー対策（PR #31・#32・#33）

[改修計画書_SQLiteロックエラー対策.md](改修計画書_SQLiteロックエラー対策.md) の改修を、本番環境（Linux）に適用する手順。計画書 §6 を、実際に打つコマンドの順に並べ直したもの。

## 0. 概要

### 適用するもの

| PR | 内容 | 本番で必要な操作 |
|---|---|---|
| #31 | `backend/app.py`：SQLite の WAL 化・インデックス・実績登録の処理順・期限付きタイムアウト・API キーのマスク・保守取込の物理削除 | **バックエンドのリビルド**（再起動だけでは反映されない） |
| #32 | 計画書への検証結果の追記 | なし（ドキュメントのみ） |
| #33 | `docker-compose.yml` の `version:` 行の削除 | なし。ただし Compose V2 が前提（§1） |

### 注意すること

- **再起動（`docker compose restart`）では反映されない。** `backend/Dockerfile` は `COPY . .` で `app.py` をイメージに焼き込むため、`docker compose up -d --build backend` で作り直す
- **DB は起動時に WAL モードに切り替わる。** 旧コードでもそのまま読めるが、DB ファイルだけをコピーして戻すと壊れる。必ず §3 のバックアップを先に取り、戻すときは §8 の手順に従う
- フロントエンド・`.env.example`・`requirements.txt` は変わっていない。`.env` の追加・変更は不要
- 作業時間の目安: 30 分（リビルドの時間はサーバーによる）。画面が止まるのはコンテナの入れ替え中の数秒だけ
- 実施は利用者の少ない時間帯に。§6 の物理削除と `VACUUM` は書き込みを数秒止める

### 表記

- コマンドは本番サーバーのリポジトリのディレクトリ（`docker-compose.yml` がある場所）で実行する
- `YYYYMMDD` は実施日に置き換える（例: `20261125`）

## 1. 事前確認（読み取りのみ。いつ実施してもよい）

### 1.1 Docker Compose が V2 であること

```bash
docker compose version
```

- `Docker Compose version v2.x` 以上が表示されれば OK
- **エラーになり、`docker-compose`（ハイフン付き、V1）しか無い場合は中止する。** V1 は `version:` の無い compose ファイルを最も古い形式として読もうとするため、PR #33 を取り込むと起動できなくなるおそれがある。Compose V2 を入れるか、PR #33 を戻してから進める

### 1.2 現在のコミットと作業ツリー

```bash
git log -1 --format='%h %ci %s'
git status --short
git fetch origin
git log --oneline HEAD..origin/main
```

- `git status --short` が空であること（本番で直接ファイルを書き換えていないこと）。何か出たら中身を確認し、適用前に扱いを決める
- `HEAD..origin/main` に、これから取り込むコミットが並ぶ。#31・#32・#33 以外の未適用の変更が混ざっていないかを確認する（混ざっていれば、その変更の反映手順も合わせて確認する）

### 1.3 ディスクの空き

```bash
df -h .
docker system df
docker compose exec backend ls -la /app/data
```

- バックアップで `worklog.db` と同じサイズがもう 1 つ必要になる。ボリュームの空きが DB サイズの 3 倍以上あれば十分（`VACUUM` も一時的に同程度を使う）

### 1.4 現状の記録（適用後の比較用）

```bash
docker compose exec -T backend python - <<'EOF'
import sqlite3, time
c = sqlite3.connect('file:/app/data/worklog.db?mode=ro', uri=True)
q = lambda s: c.execute(s).fetchall()
print("journal_mode", q("PRAGMA journal_mode"))
print("indexes", [r[0] for r in q("select name from sqlite_master where type='index' and name not like 'sqlite_%'")])
print("worklog total/active/deleted", q("select count(*), sum(deleted_at is null), sum(deleted_at is not null) from worklog"))
print("active hours", q("select round(sum(hours), 2) from worklog where deleted_at is null"))
print("FIX-MAINT deleted", q("select count(*) from worklog where parent_key='FIX-MAINT' and deleted_at is not null"))
EOF
curl -s -o /dev/null -w 'search %{http_code} %{time_total}s\n' http://localhost:3031/api/worklog/search
```

- 出力を控えておく。`active hours`（有効行の合計時間）は適用後も同じ値になるはず
- `search` の時間が修正前の値（計画書 §2 では 8 秒前後）

## 2. `added_at` の形式チェック（計画書 V13。読み取りのみ）

PR #31 は日付の絞り込みを `added_at >= 'YYYY-MM-DD' AND added_at < 翌日` の範囲条件に変えている。`added_at` が `YYYY-MM-DDTHH:MM:SS` 以外の形式の行があると、その行が絞り込みから漏れる。

```bash
docker compose exec -T backend python - <<'EOF'
import sqlite3
c = sqlite3.connect('file:/app/data/worklog.db?mode=ro', uri=True)
rows = c.execute("select id, parent_key, name, added_at from worklog where added_at not glob '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]'").fetchall()
print(len(rows))
for r in rows[:20]: print(r)
EOF
```

- **0 件なら次へ進む**
- 1 件以上なら中止し、該当行の扱い（形式を直すか）を決めてから再開する

## 3. バックアップ（必須）

### 3.1 SQLite のバックアップ API で取る

ファイルのコピーではなく、SQLite のバックアップ API を使う（書き込み中でも整合した状態で取れる）。この時点のコンテナは旧コードのままでよい。

```bash
docker compose exec backend python -c "import sqlite3; s=sqlite3.connect('/app/data/worklog.db'); d=sqlite3.connect('/app/data/worklog.db.bak-YYYYMMDD'); s.backup(d); d.close(); s.close()"
docker compose exec backend ls -la /app/data
```

- `worklog.db.bak-YYYYMMDD` が `worklog.db` とほぼ同じサイズでできていること

### 3.2 ホスト側にも取り出す

バックアップは同じ `bpm-data` ボリュームの中にあるので、ボリューム障害に備えてホストにもコピーする。

```bash
mkdir -p ~/bpm-backup
docker compose cp backend:/app/data/worklog.db.bak-YYYYMMDD ~/bpm-backup/
ls -la ~/bpm-backup/
```

### 3.3 バックアップが開けることを確認

```bash
docker compose exec -T backend python -c "import sqlite3; c=sqlite3.connect('file:/app/data/worklog.db.bak-YYYYMMDD?mode=ro', uri=True); print(c.execute('pragma integrity_check').fetchone(), c.execute('select count(*) from worklog').fetchone())"
```

- `('ok',)` と、§1.4 の `total` と同じ行数が出ること

### 3.4 ロールバック用に旧イメージを残す

```bash
docker image tag $(docker compose images -q backend) bpm2-backend:before-sqlite-lock
docker image ls | grep bpm
```

- イメージ名はプロジェクト名（ディレクトリ名）によって `bpm2-backend` と違う場合がある。`docker image ls` の表示で確認して読み替える

## 4. コードの取り込み

```bash
git pull origin main
git log -1 --format='%h %s'
```

- 最新が `Merge pull request #33 …`（`23d60e1`）以降になっていること
- `frontend/html` と `nginx.conf` は変わっていないので、フロントエンドは何もしなくてよい

## 5. バックエンドのリビルドと起動

```bash
docker compose up -d --build backend
docker compose ps
docker compose logs --tail 50 backend
```

- イメージのビルドが終わってからコンテナが入れ替わる。画面が止まるのは入れ替えの数秒だけ
- 起動時の `init_db()` で、WAL への切り替えとインデックス 3 本（`idx_worklog_child` / `idx_worklog_parent` / `idx_worklog_name_day`）の作成が自動で行われる
- ログに `Traceback` や `Error` が出ていないこと

### 5.1 起動直後の確認

```bash
docker compose exec -T backend sha256sum /app/app.py
sha256sum backend/app.py
docker compose exec -T backend python - <<'EOF'
import sqlite3
c = sqlite3.connect('file:/app/data/worklog.db?mode=ro', uri=True)
q = lambda s: c.execute(s).fetchall()
print("journal_mode", q("PRAGMA journal_mode"))
print("indexes", [r[0] for r in q("select name from sqlite_master where type='index' and name not like 'sqlite_%'")])
print("active hours", q("select round(sum(hours), 2) from worklog where deleted_at is null"))
EOF
docker compose exec backend ls -la /app/data
curl -s -o /dev/null -w 'search %{http_code} %{time_total}s\n' http://localhost:3031/api/worklog/search
curl -s -o /dev/null -w 'issues %{http_code} %{time_total}s\n' http://localhost:3031/api/issues
```

合格の条件:

- 2 つの `sha256sum` が一致する（新しいコードで動いている）
- `journal_mode` が `wal`、インデックスが 3 本、`/app/data` に `worklog.db-wal` と `worklog.db-shm` がある
- `active hours` が §1.4 と同じ
- `search` が 200 で、0.1 秒前後（§1.4 の値から大きく短くなる）。`issues` が 200

## 6. 論理削除済み `FIX-MAINT` 行の物理削除と `VACUUM`

保守取込が論理削除で溜めてきた行（計画書 §2.2 の推定で約 29,100 行）を消して、DB を小さくする。新しいコードは保守取込で物理削除するので、この操作は今回 1 回だけでよい。

**書き込みを数秒止めるので、利用者の少ない時間帯に行う。** `VACUUM` は他の接続がある間は失敗することがあるので、失敗したら少し待って `VACUUM` だけやり直す。

```bash
docker compose exec backend python -c "import sqlite3; c=sqlite3.connect('/app/data/worklog.db', timeout=30); n=c.execute(\"DELETE FROM worklog WHERE parent_key='FIX-MAINT' AND deleted_at IS NOT NULL\").rowcount; c.commit(); c.execute('VACUUM'); c.close(); print(n)"
docker compose exec backend ls -la /app/data
```

- 表示される削除件数が、§1.4 の `FIX-MAINT deleted` と同じであること
- `VACUUM` だけやり直す場合:

```bash
docker compose exec backend python -c "import sqlite3; c=sqlite3.connect('/app/data/worklog.db', timeout=30); c.execute('VACUUM'); c.close(); print('ok')"
```

- 実施後に §5.1 の `active hours` をもう一度確認し、変わっていないこと（消すのは論理削除済みの行だけ）

## 7. 動作確認

### 7.1 画面（読み取り）

ブラウザで本番の URL を開き、次の画面が表示されることを確認する。

- 一覧（`/`）
- ガント（📅 ガント）
- 月別工数（📊 月別工数）
- 日別実績（🗓 日別実績）: 当月の合計が §1.4 前と変わっていないこと
- 実施工数確認（`worklog.html`）: 日付を 1 日指定して検索できること

### 7.2 実績登録（書き込み）

**テスト用の登録はしない。** 本番の Backlog の実績工数が変わるため、実際の業務での入力で確認する（利用者に、いつもどおり実績を入力してもらう）。

入力があったら確認する:

```bash
docker compose logs --since 30m backend | grep -E "database is locked|Backlog 未反映|Backlog 反映状態不明|反映済み・BPM 未保存|Traceback"
```

- 何も出なければ OK
- `database is locked` が出たら §8 のロールバックを検討する
- `Backlog 反映状態不明` / `Backlog 反映済み・BPM 未保存` が出たら、その課題キーと時間を控えて §9 の突合の対象にする

### 7.3 環境状態の記録

[環境状態.md](環境状態.md) の「本番環境」の節を、確認コマンドの結果で更新してコミットする（稼働コミット、Compose のバージョン、`journal_mode`、インデックス、行数、バックアップのファイル名）。

## 8. ロールバック

### 8.1 コードだけを戻す（DB はそのまま使う）

新しいコードに問題があり、DB は壊れていない場合。WAL モードの DB は旧コードでもそのまま読めるので、DB は戻さない（バックアップ以降に登録された実績を失わずに済む）。

```bash
docker compose stop backend
docker image tag bpm2-backend:before-sqlite-lock bpm2-backend:latest
docker compose up -d --no-build backend
docker compose exec -T backend sha256sum /app/app.py
```

- ハッシュが新しい `backend/app.py` と違う（旧コードで動いている）ことを確認する
- リポジトリは新しいままなので、次に `--build` すると新しいコードに戻る。原因が分かるまでは `git checkout <適用前のコミット> -- backend/` などで戻しておくか、`--build` しないこと
- §6 を実施済みなら、論理削除済みの `FIX-MAINT` 行は戻らないが、旧コードの動作には影響しない（保守取込はスプレッドシートが正なので、再取込で揃う）

### 8.2 DB もバックアップに戻す

DB の中身に問題がある場合だけ。**バックアップ以降に登録された実績は失われる**ので、戻す前に失われる範囲を確認する。

1. バックエンドを止めて、接続をすべて閉じる

   ```bash
   docker compose stop backend
   docker volume ls | grep bpm-data
   ```

2. ボリューム内の `worklog.db-wal` と `worklog.db-shm` を**削除してから**、バックアップを `worklog.db` に戻す。新しい DB の `-wal` が残っていると、戻した DB にそれが適用されて壊れる。停止中は `docker compose exec` で入れないので、別のコンテナからボリュームを操作する（`<ボリューム名>` は上の `docker volume ls` の表示）

   ```bash
   docker run --rm -v <ボリューム名>:/data alpine sh -c "rm -f /data/worklog.db-wal /data/worklog.db-shm && cp /data/worklog.db.bak-YYYYMMDD /data/worklog.db && ls -la /data"
   ```

3. 旧イメージで起動する（§8.1 の `docker image tag` と `up -d --no-build`）

## 9. 適用後のフォロー

- **差異の突合**: 計画書 §5.5 の手順で、既存の Backlog と BPM の実績工数の差を一覧にする。§7.2 の警告ログに出た課題も同じ方法で確認する。補正は自動では行わず、担当者と相談して手作業で行う
- **警告ログの確認**: Backlog の応答が遅い日は「Backlog 反映状態不明」が出やすい（計画書 §9.3 の「気付いた点」）。適用後しばらくは、週に 1 回程度 §7.2 のコマンドでログを確認する
- **バックアップの削除**: 1〜2 週間問題がなければ、ボリューム内の `worklog.db.bak-YYYYMMDD` と旧イメージのタグ `bpm2-backend:before-sqlite-lock` を削除してよい（ホスト側の `~/bpm-backup/` は残す）

## 実施記録

| 日付 | 実施者 | 結果 | 備考 |
|---|---|---|---|
| | | | |
