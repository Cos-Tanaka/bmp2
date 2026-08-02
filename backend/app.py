"""
BPM バックエンド API
Backlog API からデータを取得し、フロントエンド向けに整形して返す。
"""
import os
import re
import json
import time
import sqlite3
import logging
import datetime
import threading
from functools import wraps
from flask import Flask, jsonify, abort, request
import requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── タイムゾーン ───────────────────────────────────
JST = datetime.timezone(datetime.timedelta(hours=9))


def now_jst_iso() -> str:
    """JST の壁時計時刻を naive ISO 文字列で返す（SQLite date() 集計と整合）。"""
    return datetime.datetime.now(JST).replace(tzinfo=None).isoformat(timespec="seconds")


# ── 設定 ──────────────────────────────────────────
SPACE      = os.environ["BACKLOG_SPACE"]          # 例: yourspace.backlog.com
API_KEY    = os.environ["BACKLOG_API_KEY"]
PROJECT    = os.environ["BACKLOG_PROJECT_KEY"]
CACHE_TTL  = int(os.environ.get("CACHE_TTL", 60))
BASE_URL   = f"https://{SPACE}/api/v2"
DB_PATH    = os.environ.get("DB_PATH", "/app/data/worklog.db")

# ── シンプルインメモリキャッシュ ────────────────────
# project_id / custom_fields など「めったに変わらない」値の短期キャッシュに使う。
# issues 一覧は下記の常時ウォームなスナップショット層で別管理する。
_cache: dict = {}

# ── issues スナップショット層 ───────────────────────
# Backlog 全件取得は 1 回 ~20 秒かかるため、リクエスト契機で取りに行かず、
# バックグラウンドスレッドが CACHE_TTL ごとに先回りして取得・保持する。
# /api/issues は常にこのスナップショットを即返すので、ユーザーが取得を待つことはない。
# スナップショットはディスクにも永続化し、再起動直後も「多少古いが即表示」を実現する。
SNAPSHOT_PATH   = os.path.join(os.path.dirname(DB_PATH), "issues_snapshot.json")
_snapshot: dict = {"data": None, "ts": 0.0}
_snapshot_lock  = threading.Lock()   # _snapshot の読み書き保護（短時間）
_refresh_lock   = threading.Lock()   # 同時多重取得を防ぐ（Backlog取得中は 1 本に絞る）

# ── スナップショットの局所更新用 生データ ─────────────
# build_response() の入力（親・子の生 JSON とカスタム属性のフィールドID）をそのまま保持する。
# 1 課題だけの更新（実績工数の入力、モーダルからの編集）は、この生データの該当課題を
# 差し替えて build_response() を回し直すだけで済むため、Backlog 全件取得(~20秒) が要らない。
# 集計・health の計算式は build_response()/format_issue() を再利用するので二重実装にならない。
# 生データはメモリのみ（ディスクには整形後のスナップショットだけを永続化する）。
_raw: dict = {"parents": None, "children_map": None, "field_ids": None}
_raw_lock  = threading.Lock()

# 全件取得の実行中に更新された課題を、取得結果へ取り込み直すためのバッファ。
# 全件取得は ~20 秒かかるので、その最中の更新は「取得済みの古い値」で上書きされ得る。
# 取得開始後に更新された課題はここから復元し、入力した値が一瞬巻き戻るのを防ぐ。
_recent_patches: dict = {}            # {issueKey: (更新時刻, 課題JSON)}
_RECENT_PATCH_TTL = 300               # 秒。これより古い記録は破棄する


def _save_snapshot(data: list, ts: float) -> None:
    """スナップショットをディスクへアトミックに保存する。"""
    try:
        os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
        tmp = SNAPSHOT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": ts, "data": data}, f, ensure_ascii=False)
        os.replace(tmp, SNAPSHOT_PATH)
    except Exception:
        log.exception("Failed to persist issues snapshot")


def _load_snapshot() -> None:
    """起動時にディスクのスナップショットを読み込む（あれば即表示に使える）。"""
    try:
        with open(SNAPSHOT_PATH, encoding="utf-8") as f:
            payload = json.load(f)
        with _snapshot_lock:
            _snapshot["data"] = payload.get("data")
            _snapshot["ts"]   = payload.get("ts", 0.0)
        age = int(time.time() - _snapshot["ts"])
        log.info("Loaded issues snapshot from disk (%d parents, age=%ds)",
                 len(_snapshot["data"] or []), age)
    except FileNotFoundError:
        log.info("No issues snapshot on disk yet (cold start)")
    except Exception:
        log.exception("Failed to load issues snapshot")


def _publish_snapshot(data: list) -> float:
    """整形済み一覧をスナップショット（メモリ＋ディスク）として公開する。"""
    ts = time.time()
    with _snapshot_lock:
        _snapshot["data"] = data
        _snapshot["ts"]   = ts
    _save_snapshot(data, ts)
    return ts


def _rebuild_locked() -> list:
    """_raw_lock 取得済みの前提で、生データからフロント向け一覧を組み立て直す。"""
    return build_response(_raw["parents"], _raw["children_map"], *_raw["field_ids"])


def _replace_issue(parents: list, children_map: dict, issue: dict) -> bool:
    """生データ内の同一 issueKey の課題を差し替える。見つからなければ False。"""
    key = issue.get("issueKey")
    for kids in children_map.values():
        for i, c in enumerate(kids):
            if c.get("issueKey") == key:
                kids[i] = issue
                return True
    # 親側。親一覧は「種別=00.案件 かつ 状態≠完了」で絞り込み済みのため、
    # 条件から外れる更新（完了化など）が一覧から消えるのは次の全件更新時になる。
    for i, p in enumerate(parents):
        if p.get("issueKey") == key:
            parents[i] = issue
            return True
    return False


def _merge_recent_patches(parents: list, children_map: dict, since: float) -> None:
    """全件取得の開始後に更新された課題を、取得結果へ復元する。ついでに古い記録を捨てる。"""
    cutoff = time.time() - _RECENT_PATCH_TTL
    for key, (ts, issue) in list(_recent_patches.items()):
        if ts < cutoff:
            _recent_patches.pop(key, None)
            continue
        if ts >= since:
            _replace_issue(parents, children_map, issue)


def refresh_snapshot() -> list:
    """Backlog から最新を取得してスナップショット（メモリ＋ディスク）を更新する。

    _refresh_lock で同時取得を 1 本に絞る。取得中に来た別スレッドはロック解放後、
    更新済みの最新スナップショットをそのまま受け取る。
    """
    with _refresh_lock:
        started = time.time()
        parents, children_map, field_ids = _fetch_raw()
        with _raw_lock:
            _merge_recent_patches(parents, children_map, started)
            _raw["parents"]      = parents
            _raw["children_map"] = children_map
            _raw["field_ids"]    = field_ids
            data = _rebuild_locked()
            # 公開まで _raw_lock を握ったままにして、局所更新との公開順が入れ替わらないようにする
            ts = _publish_snapshot(data)
        log.info("Snapshot refreshed in %.1fs (%d parents)", ts - started, len(data))
        return data


def refresh_snapshot_async() -> None:
    """全件更新をバックグラウンドで走らせる（呼び出し元のレスポンスを待たせない）。"""
    def run():
        try:
            refresh_snapshot()
        except Exception:
            log.exception("Async snapshot refresh failed")
    threading.Thread(target=run, name="snapshot-refresh-async", daemon=True).start()


def apply_issue_update(issue: dict) -> bool:
    """更新後の課題 1 件をスナップショットへ反映する。Backlog へのアクセスは発生しない。

    生データが未取得（起動直後など）か、対象が生データに含まれない場合は False を返す。
    """
    key = issue.get("issueKey")
    if not key:
        return False
    # 全件取得の実行中でも取り込めるよう、まずバッファへ記録する
    _recent_patches[key] = (time.time(), issue)
    with _raw_lock:
        if _raw["parents"] is None:
            return False
        if not _replace_issue(_raw["parents"], _raw["children_map"], issue):
            return False
        data = _rebuild_locked()
        # 再構築と公開を _raw_lock 内で完結させ、更新が同時に来ても公開順が逆転しないようにする
        _publish_snapshot(data)
    return True


def reflect_issue_change(issue: dict) -> None:
    """課題の更新をダッシュボードへ即時反映する（更新系エンドポイントの共通後処理）。

    生データからの再構築で済むのが通常経路。それが使えないときだけ、
    全件取得をバックグラウンドに投げる（レスポンスは待たせない）。
    """
    try:
        if apply_issue_update(issue):
            return
        log.info("Local snapshot update skipped (raw cache unavailable); refreshing in background")
    except Exception:
        log.exception("Local snapshot update failed; refreshing in background")
    refresh_snapshot_async()


def _snapshot_refresher() -> None:
    """CACHE_TTL ごとにスナップショットを先回り更新するバックグラウンドループ。"""
    # 起動直後、ディスクに何も無ければ最初の 1 回をここで取得（リクエストは待たせない）
    if _snapshot["data"] is None:
        try:
            refresh_snapshot()
        except Exception:
            log.exception("Initial snapshot refresh failed")
    while True:
        time.sleep(CACHE_TTL)
        try:
            refresh_snapshot()
        except Exception:
            # 取得失敗時は直前のスナップショットを保持したまま次サイクルへ
            log.exception("Background snapshot refresh failed")

# ── SQLite（実績工数の日々入力履歴）──────────────────
def get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS worklog (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_key   TEXT NOT NULL,
                parent_title TEXT NOT NULL,
                child_key    TEXT NOT NULL,
                name         TEXT NOT NULL,
                added_at     TEXT NOT NULL,
                hours        REAL NOT NULL,
                deleted_at   TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_worklog_child ON worklog(child_key)")
        # 既存DBへのマイグレーション: 論理削除カラムが無ければ追加
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(worklog)").fetchall()]
        if "deleted_at" not in cols:
            conn.execute("ALTER TABLE worklog ADD COLUMN deleted_at TEXT")


def add_worklog(parent_key, parent_title, child_key, name, hours, added_at=None) -> dict:
    added_at = added_at or now_jst_iso()
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO worklog (parent_key, parent_title, child_key, name, added_at, hours)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (parent_key, parent_title, child_key, name, added_at, hours),
        )
        return {"id": cur.lastrowid, "name": name, "added_at": added_at, "hours": hours}


def get_worklog(child_key: str) -> list:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, added_at, hours FROM worklog"
            " WHERE child_key = ? AND deleted_at IS NULL ORDER BY added_at DESC, id DESC",
            (child_key,),
        ).fetchall()
    return [{"id": r["id"], "name": r["name"], "added_at": r["added_at"], "hours": r["hours"]} for r in rows]


init_db()

def cached(key: str, ttl: int):
    """関数の戻り値を ttl 秒キャッシュするデコレータ"""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            entry = _cache.get(key)
            if entry and time.time() - entry["ts"] < ttl:
                return entry["data"]
            data = fn(*args, **kwargs)
            _cache[key] = {"ts": time.time(), "data": data}
            return data
        return wrapper
    return decorator


# ── Backlog API ヘルパー ────────────────────────────
def backlog_get(path: str, params: dict = None) -> list | dict:
    """Backlog REST API を GET 呼び出し"""
    p = params or {}
    p["apiKey"] = API_KEY
    url = f"{BASE_URL}{path}"
    resp = requests.get(url, params=p, timeout=15)
    resp.raise_for_status()
    return resp.json()


def backlog_patch(path: str, data: dict) -> dict:
    """Backlog REST API を PATCH 呼び出し（フォームエンコード）"""
    url = f"{BASE_URL}{path}"
    resp = requests.patch(url, params={"apiKey": API_KEY}, data=data, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_issues_all(extra_params: dict = None) -> list:
    """
    Backlog の issues エンドポイントは 1 回最大 100 件。
    offset を進めて全件取得する。
    """
    params = {
        "projectId[]": get_project_id(),
        "count": 100,
        "offset": 0,
    }
    if extra_params:
        params.update(extra_params)

    issues = []
    while True:
        batch = backlog_get("/issues", params)
        if not batch:
            break
        issues.extend(batch)
        if len(batch) < 100:
            break
        params["offset"] += 100
    return issues


@cached("project_id", ttl=3600)
def get_project_id() -> int:
    proj = backlog_get(f"/projects/{PROJECT}")
    return proj["id"]


@cached("custom_fields", ttl=3600)
def get_custom_fields() -> dict:
    """カスタム属性一覧を取得し {name: id} マップを返す"""
    fields = backlog_get(f"/projects/{PROJECT}/customFields")
    return {f["name"]: f["id"] for f in fields}


@cached("project_users", ttl=3600)
def get_project_users() -> list:
    """プロジェクトメンバー一覧を返す（担当者プルダウン用）"""
    users = backlog_get(f"/projects/{PROJECT}/users")
    return [{"id": u["id"], "name": u["name"]} for u in users]


@cached("field_options", ttl=3600)
def get_field_options() -> dict:
    """
    編集対象（進捗率・チェック状態）の単一選択リストの選択肢を返す。
    { "progress": {"fieldId": int, "items": [{"id", "name"}, ...]},
      "checkStatus": {...} }
    """
    progress_name     = os.environ.get("PROGRESS_FIELD_NAME", "進捗率")
    check_status_name = os.environ.get("CHECK_STATUS_FIELD_NAME", "チェック状態")
    targets = {progress_name: "progress", check_status_name: "checkStatus"}

    fields = backlog_get(f"/projects/{PROJECT}/customFields")
    result: dict = {}
    for f in fields:
        key = targets.get(f["name"])
        if not key:
            continue
        result[key] = {
            "fieldId": f["id"],
            "items": [{"id": it["id"], "name": it["name"]} for it in (f.get("items") or [])],
        }
    return result


def extract_custom_value(issue: dict, field_id: int):
    """課題からカスタム属性値を取り出す"""
    for cf in issue.get("customFields", []):
        if cf.get("id") == field_id:
            return cf.get("value")
    return None


# ── データ整形 ─────────────────────────────────────
def calc_health(issue: dict, planned: float, actual: float) -> str:
    """
    健全性ロジック:
      🔴 危険  : 期限超過 OR 実績工数 > 予定工数 × 1.2
      🟡 注意  : 期限まで 3 日以内 OR 実績工数 > 予定工数
      🟢 正常  : 上記以外
    """
    today = datetime.datetime.now(JST).date()

    due_str = issue.get("dueDate")
    due = None
    if due_str:
        due = datetime.date.fromisoformat(due_str[:10])

    if due and due < today:
        return "red"
    if planned > 0 and actual > planned * 1.2:
        return "red"
    if due and (due - today).days <= 3:
        return "yellow"
    if planned > 0 and actual > planned:
        return "yellow"
    return "green"


def format_issue(issue: dict, progress_field_id: int | None, check_status_field_id: int | None) -> dict:
    """子課題 1 件をフロントエンド向けに整形"""
    progress = None
    if progress_field_id:
        val = extract_custom_value(issue, progress_field_id)
        if val is not None:
            # 辞書型（ラジオボタンやリスト形式 of カスタムフィールド）の場合
            if isinstance(val, dict):
                val = val.get("name")
            # リスト型（複数選択形式）の場合
            elif isinstance(val, list) and val:
                val = val[0].get("name") if isinstance(val[0], dict) else val[0]
            
            # 数値または数字文字列から整数値を抽出
            if isinstance(val, (int, float)):
                progress = int(val)
            elif isinstance(val, str):
                import re
                match = re.search(r'([\d.]+)', val)
                if match:
                    try:
                        progress = int(float(match.group(1)))
                    except ValueError:
                        pass

    check_status = None
    if check_status_field_id:
        val = extract_custom_value(issue, check_status_field_id)
        if val is not None:
            if isinstance(val, dict):
                check_status = val.get("name")
            elif isinstance(val, list) and val:
                check_status = val[0].get("name") if isinstance(val[0], dict) else val[0]
            else:
                check_status = str(val)

    planned_h = issue.get("estimatedHours") or 0
    actual_h  = issue.get("actualHours")    or 0

    if progress is not None:
        progress_pct = min(progress, 100)
    elif planned_h > 0:
        progress_pct = min(round(actual_h / planned_h * 100), 100)
    else:
        progress_pct = 0
    remaining_h = round((planned_h / 0.8) * (100 - progress_pct) / 100, 1)

    return {
        "id":          issue["issueKey"],
        "title":       issue["summary"],
        "assignee":    (issue.get("assignee") or {}).get("name", "未割当"),
        "assigneeId":  (issue.get("assignee") or {}).get("id"),
        "start":       (issue.get("startDate") or "")[:10],
        "due":         (issue.get("dueDate")   or "")[:10],
        "plannedH":    planned_h,
        "actualH":     actual_h,
        "remainingH":  remaining_h,
        "progress":    progress,
        "status":      issue["status"]["name"],
        "statusId":    issue["status"]["id"],
        "checkStatus": check_status,
        "issueType":   issue.get("issueType", {}).get("name", ""),
        "url":         f"https://{SPACE}/view/{issue['issueKey']}",
    }


KINTONE_BASE_URL = "https://aspit.cybozu.com/k/70/show#record="


def build_response(parents: list, children_map: dict, progress_field_id: int | None, status_field_id: int | None, check_status_field_id: int | None, kintone_field_id: int | None) -> list:
    result = []
    for p in parents:
        kids_raw = children_map.get(p["id"], [])
        kids = [format_issue(c, progress_field_id, check_status_field_id) for c in kids_raw]

        custom_status = None
        if status_field_id:
            val = extract_custom_value(p, status_field_id)
            if val is not None:
                if isinstance(val, dict):
                    custom_status = val.get("name")
                elif isinstance(val, list) and val:
                    custom_status = val[0].get("name") if isinstance(val[0], dict) else val[0]
                else:
                    custom_status = str(val)

        # 親の種別が「00.案件」かつカスタムステータスに応じて集計対象の子課題種別を判定
        import re
        status_num = None
        if p.get("issueType", {}).get("name") == "00.案件" and custom_status:
            match = re.match(r'^(\d+)', custom_status)
            if match:
                status_num = int(match.group(1))

        is_parent_dev    = status_num is not None and 40 <= status_num <= 49
        is_parent_design = status_num is not None and 20 <= status_num <= 29

        # 開発中 fallback: 番号がなくても文字列に「開発」を含む場合
        if not is_parent_dev and custom_status and "開発" in custom_status:
            is_parent_dev = True

        if is_parent_dev:
            # 子課題の種別が 01〜07 で始まるもののみを集計
            target_prefixes = ("01", "02", "03", "04", "05", "06", "07")
            planned_total   = 0
            actual_total    = 0
            remaining_total = 0
            for k in kids:
                if k.get("issueType", "")[:2] in target_prefixes:
                    planned_total   += k["plannedH"]
                    actual_total    += k["actualH"]
                    remaining_total += k["remainingH"]
        elif is_parent_design:
            # 子課題の種別が 32 で始まるもののみを集計
            target_prefixes = ("32",)
            planned_total   = 0
            actual_total    = 0
            remaining_total = 0
            for k in kids:
                if k.get("issueType", "")[:2] in target_prefixes:
                    planned_total   += k["plannedH"]
                    actual_total    += k["actualH"]
                    remaining_total += k["remainingH"]
        else:
            # 通常はすべての子課題を合計
            planned_total   = sum(k["plannedH"]   for k in kids)
            actual_total    = sum(k["actualH"]    for k in kids)
            remaining_total = sum(k["remainingH"] for k in kids)

        health = calc_health(p, planned_total, actual_total)

        # Kintone番号（テキスト）から数値部分のみ抽出してリンクURLを生成
        kintone_url = None
        if kintone_field_id:
            kval = extract_custom_value(p, kintone_field_id)
            if kval:
                m = re.search(r"\d+", str(kval))
                if m:
                    kintone_url = KINTONE_BASE_URL + m.group(0)

        result.append({
            "id":           p["issueKey"],
            "title":        p["summary"],
            "assignee":     (p.get("assignee") or {}).get("name", "未割当"),
            "start":        (p.get("startDate") or "")[:10],
            "due":          (p.get("dueDate")   or "")[:10],
            "plannedH":     planned_total,
            "actualH":      actual_total,
            "remainingH":   round(remaining_total, 1),
            "health":       health,
            "status":       p["status"]["name"],
            "statusId":     p["status"]["id"],
            "customStatus": custom_status,
            "url":          f"https://{SPACE}/view/{p['issueKey']}",
            "kintoneUrl":   kintone_url,
            "children":     kids,
        })
    return result


# ── エンドポイント ──────────────────────────────────
@app.route("/api/issues")
def api_issues():
    """
    親課題＋子課題の進捗データを返す。
    常時ウォームなスナップショットを即返す（Backlog 取得はバックグラウンドで先回り実施）。
    スナップショットが未生成の初回のみ、同期取得してから返す。
    """
    try:
        with _snapshot_lock:
            data = _snapshot["data"]
        if data is None:
            # ディスクにもメモリにも無い真のコールドスタート時のみ同期取得
            data = refresh_snapshot()
        return jsonify(data)
    except requests.HTTPError as e:
        log.error("Backlog API error: %s", e)
        abort(502, description=f"Backlog API error: {e.response.status_code}")
    except Exception as e:
        log.exception("Unexpected error")
        abort(500, description=str(e))


def _fetch_raw() -> tuple:
    """Backlog から全課題を取得し、build_response() の入力を組み立てて返す。

    戻り値: (parents, children_map, field_ids)
    field_ids は build_response() の第3引数以降にそのまま展開できる並び。
    整形（build_response）を分けているのは、スナップショットの局所更新で
    「取得済みの生データから組み立て直す」経路を使えるようにするため。
    """
    log.info("Fetching issues from Backlog (project=%s)", PROJECT)

    custom_fields = get_custom_fields()
    # 進捗率フィールド名は「進捗率」と仮定（実際の名称に合わせて .env 等で変更）
    progress_field_name = os.environ.get("PROGRESS_FIELD_NAME", "進捗率")
    progress_field_id   = custom_fields.get(progress_field_name)
    # ステータスフィールド名は「ステータス」と仮定
    status_field_name   = os.environ.get("STATUS_FIELD_NAME", "ステータス")
    status_field_id     = custom_fields.get(status_field_name)
    # チェック状態フィールド名
    check_status_field_name = os.environ.get("CHECK_STATUS_FIELD_NAME", "チェック状態")
    check_status_field_id   = custom_fields.get(check_status_field_name)
    # Kintone番号フィールド
    kintone_field_name      = os.environ.get("KINTONE_FIELD_NAME", "Kintone番号")
    kintone_field_id        = custom_fields.get(kintone_field_name)

    all_issues = fetch_issues_all()

    # 親課題と子課題に分類（親課題は課題種別が「00.案件」かつ状態が「完了」以外のもののみに限定）
    parents      = [i for i in all_issues if i.get("parentIssueId") is None and i.get("issueType", {}).get("name") == "00.案件" and i.get("status", {}).get("name") != "完了"]
    children_raw = [i for i in all_issues if i.get("parentIssueId") is not None]

    # children_map: parentIssueId -> [child, ...]
    children_map: dict = {}
    for c in children_raw:
        pid = c["parentIssueId"]
        children_map.setdefault(pid, []).append(c)

    field_ids = (progress_field_id, status_field_id, check_status_field_id, kintone_field_id)
    return parents, children_map, field_ids


@app.route("/api/field-options")
def api_field_options():
    """進捗率・チェック状態プルダウンの選択肢を返す。"""
    try:
        return jsonify(get_field_options())
    except requests.HTTPError as e:
        log.error("Backlog API error: %s", e)
        abort(502, description=f"Backlog API error: {e.response.status_code}")
    except Exception as e:
        log.exception("Unexpected error")
        abort(500, description=str(e))


@app.route("/api/project-users")
def api_project_users():
    """担当者プルダウン用のプロジェクトメンバー一覧を返す。"""
    try:
        return jsonify(get_project_users())
    except requests.HTTPError as e:
        log.error("Backlog API error: %s", e)
        abort(502, description=f"Backlog API error: {e.response.status_code}")
    except Exception as e:
        log.exception("Unexpected error")
        abort(500, description=str(e))


@app.route("/api/issue/<key>", methods=["PATCH"])
def api_update_issue(key: str):
    """
    子課題の担当者・進捗率・チェック状態・開始日・期限日を更新する。
    リクエストボディ(JSON): {
      "assigneeId": int|""?, "progressItemId": int?, "checkStatusItemId": int?,
      "startDate": "YYYY-MM-DD"|""?, "dueDate": "YYYY-MM-DD"|""?
    }
    単一選択リストは項目ID(customField_<id>)、日付は標準フィールドで更新する。
    assigneeId は空文字で「未割当」にクリアできる（Backlog 側の挙動を実課題で確認済み）。
    """
    body = request.get_json(silent=True) or {}
    options = get_field_options()

    data: dict = {}
    if "assigneeId" in body:
        data["assigneeId"] = body["assigneeId"] or ""
    if body.get("progressItemId") is not None and "progress" in options:
        data[f"customField_{options['progress']['fieldId']}"] = body["progressItemId"]
    if body.get("checkStatusItemId") is not None and "checkStatus" in options:
        data[f"customField_{options['checkStatus']['fieldId']}"] = body["checkStatusItemId"]
    # 日付（標準フィールド）。空文字はクリア指示として許容
    if "startDate" in body:
        data["startDate"] = body["startDate"] or ""
    if "dueDate" in body:
        data["dueDate"] = body["dueDate"] or ""

    if not data:
        abort(400, description="更新対象がありません")

    try:
        updated = backlog_patch(f"/issues/{key}", data)
        # 更新後の課題（PATCH のレスポンス）でスナップショットを局所更新し、即時反映する
        reflect_issue_change(updated)
        return jsonify({"ok": True, "issueKey": updated.get("issueKey", key)})
    except requests.HTTPError as e:
        log.error("Backlog update error: %s", e)
        abort(502, description=f"Backlog API error: {e.response.status_code}")
    except Exception as e:
        log.exception("Unexpected error")
        abort(500, description=str(e))


@app.route("/api/worklog/<key>", methods=["GET"])
def api_worklog_list(key: str):
    """子課題の実績工数 入力履歴（名前・追加日時・追加工数）を返す。"""
    try:
        return jsonify(get_worklog(key))
    except Exception as e:
        log.exception("Unexpected error")
        abort(500, description=str(e))


@app.route("/api/worklog/<key>", methods=["POST"])
def api_worklog_add(key: str):
    """
    子課題の実績工数を日々入力する。
    body(JSON): { "hours": number, "name": str, "addedAt": str?, "parentKey": str, "parentTitle": str }
    - バリデーション: hours/name は必須、hours は数値のみ、addedAt は ISO 形式（省略時はサーバー現在時刻）
    - Backlog の actualHours に加算し、SQLite に履歴を保存する
    """
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    parent_key   = (body.get("parentKey") or "").strip()
    parent_title = (body.get("parentTitle") or "").strip()
    added_at_raw = (body.get("addedAt") or "").strip()

    # ── バリデーション ──
    if not name:
        abort(400, description="名前は必須です")
    try:
        hours = float(body.get("hours"))
    except (TypeError, ValueError):
        abort(400, description="追加工数(h) は数値で入力してください")
    if hours <= 0:
        abort(400, description="追加工数(h) は 0 より大きい数値で入力してください")
    added_at = None
    if added_at_raw:
        try:
            dt = datetime.datetime.fromisoformat(added_at_raw)
        except ValueError:
            abort(400, description="日時の形式が不正です")
        # tz 付きは JST に変換し、DB の naive JST 文字列形式に揃える
        if dt.tzinfo is not None:
            dt = dt.astimezone(JST).replace(tzinfo=None)
        added_at = dt.isoformat(timespec="seconds")

    try:
        # Backlog の現在の実績工数を取得して加算
        issue = backlog_get(f"/issues/{key}")
        current = issue.get("actualHours") or 0
        new_total = round(current + hours, 2)
        updated = backlog_patch(f"/issues/{key}", {"actualHours": new_total})

        # SQLite に履歴保存
        entry = add_worklog(parent_key, parent_title, key, name, hours, added_at)

        # ダッシュボードへ即時反映（Backlog 全件取得は挟まない）
        reflect_issue_change(updated)
        return jsonify({"ok": True, "actualHours": new_total, "entry": entry})
    except requests.HTTPError as e:
        log.error("Backlog update error: %s", e)
        abort(502, description=f"Backlog API error: {e.response.status_code}")
    except Exception as e:
        log.exception("Unexpected error")
        abort(500, description=str(e))


@app.route("/api/worklog/entry/<int:entry_id>", methods=["DELETE"])
def api_worklog_delete(entry_id: int):
    """
    入力履歴を論理削除（deleted_at を記録）し、子課題の実績工数から減算する。
    減算結果が 0 未満になる場合は 0 とする。
    Backlog の更新に失敗した場合は論理削除もロールバックする。
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT child_key, hours FROM worklog WHERE id = ? AND deleted_at IS NULL",
            (entry_id,),
        ).fetchone()
    if row is None:
        abort(404, description="対象の履歴が見つかりません（削除済みの可能性があります）")

    try:
        with get_db() as conn:
            # 論理削除（同時操作で既に削除済みなら中断）
            cur = conn.execute(
                "UPDATE worklog SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                (now_jst_iso(), entry_id),
            )
            if cur.rowcount == 0:
                return jsonify({"error": "対象の履歴は既に削除されています"}), 409

            # Backlog の実績工数から減算（0 未満は 0 に丸め）。失敗時は例外 → 論理削除もロールバック
            issue = backlog_get(f"/issues/{row['child_key']}")
            current = issue.get("actualHours") or 0
            new_total = max(0, round(current - row["hours"], 2))
            updated = backlog_patch(f"/issues/{row['child_key']}", {"actualHours": new_total})

        # ダッシュボードへ即時反映（Backlog 全件取得は挟まない）
        reflect_issue_change(updated)
        return jsonify({"ok": True, "actualHours": new_total})
    except requests.HTTPError as e:
        log.error("Backlog update error: %s", e)
        abort(502, description=f"Backlog API error: {e.response.status_code}")
    except Exception as e:
        log.exception("Unexpected error")
        abort(500, description=str(e))


@app.route("/api/worklog/names", methods=["GET"])
def api_worklog_names():
    """SQLite に登録された名前を重複なく返す（検索条件のリスト用）。"""
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT name FROM worklog WHERE deleted_at IS NULL ORDER BY name"
            ).fetchall()
        return jsonify([r["name"] for r in rows])
    except Exception as e:
        log.exception("Unexpected error")
        abort(500, description=str(e))


@app.route("/api/worklog/search", methods=["GET"])
def api_worklog_search():
    """
    実施工数の検索。検索条件: name（任意）, date（任意・単一日付 YYYY-MM-DD）。
    親案件・子案件・追加日ごとに工数を集計し、時間で返す。
    """
    name = (request.args.get("name") or "").strip()
    date = (request.args.get("date") or "").strip()
    try:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT w1.parent_key,
                       (SELECT w2.parent_title FROM worklog w2
                          WHERE w2.parent_key = w1.parent_key
                          ORDER BY w2.added_at DESC, w2.id DESC LIMIT 1) AS parent_title,
                       w1.name,
                       date(w1.added_at) AS day,
                       SUM(w1.hours) AS total_hours
                FROM worklog w1
                WHERE w1.deleted_at IS NULL
                  AND (? = '' OR w1.name = ?)
                  AND (? = '' OR date(w1.added_at) = ?)
                GROUP BY w1.parent_key, w1.name, date(w1.added_at)
                ORDER BY day DESC, w1.parent_key, w1.name
                """,
                (name, name, date, date),
            ).fetchall()
        result = [{
            "parentKey":   r["parent_key"],
            "parentTitle": r["parent_title"],
            "parentUrl":   f"https://{SPACE}/view/{r['parent_key']}",
            "name":        r["name"],
            "day":         r["day"],
            "hours":       round(r["total_hours"], 2),
        } for r in rows]
        return jsonify(result)
    except Exception as e:
        log.exception("Unexpected error")
        abort(500, description=str(e))


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "project": PROJECT, "space": SPACE})


@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(502)
@app.errorhandler(500)
def handle_error(e):
    return jsonify({"error": str(e.description)}), e.code


# ── スナップショット層の起動 ─────────────────────────
# 全関数定義後に実行する。ディスクの前回スナップショットを読み込み（あれば即表示可）、
# バックグラウンド更新スレッドを 1 本起動する。
# gunicorn は --workers 1 前提（キャッシュ／更新スレッドを 1 プロセスに集約するため）。
_load_snapshot()
threading.Thread(target=_snapshot_refresher, name="snapshot-refresher", daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
