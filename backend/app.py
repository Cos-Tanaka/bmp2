"""
BPM バックエンド API
Backlog API からデータを取得し、フロントエンド向けに整形して返す。
"""
import os
import re
import time
import sqlite3
import logging
import datetime
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
_cache: dict = {}

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
    CACHE_TTL 秒ごとに Backlog API を再取得。
    """
    try:
        data = _get_issues_cached()
        return jsonify(data)
    except requests.HTTPError as e:
        log.error("Backlog API error: %s", e)
        abort(502, description=f"Backlog API error: {e.response.status_code}")
    except Exception as e:
        log.exception("Unexpected error")
        abort(500, description=str(e))


@cached("issues", ttl=CACHE_TTL)
def _get_issues_cached():
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

    return build_response(parents, children_map, progress_field_id, status_field_id, check_status_field_id, kintone_field_id)


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


@app.route("/api/issue/<key>", methods=["PATCH"])
def api_update_issue(key: str):
    """
    子課題の進捗率・チェック状態・開始日・期限日を更新する。
    リクエストボディ(JSON): {
      "progressItemId": int?, "checkStatusItemId": int?,
      "startDate": "YYYY-MM-DD"|""?, "dueDate": "YYYY-MM-DD"|""?
    }
    単一選択リストは項目ID(customField_<id>)、日付は標準フィールドで更新する。
    """
    body = request.get_json(silent=True) or {}
    options = get_field_options()

    data: dict = {}
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
        # ダッシュボード一覧へ即時反映させるためキャッシュ破棄
        _cache.pop("issues", None)
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
        backlog_patch(f"/issues/{key}", {"actualHours": new_total})

        # SQLite に履歴保存
        entry = add_worklog(parent_key, parent_title, key, name, hours, added_at)

        # ダッシュボードへ即時反映
        _cache.pop("issues", None)
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
            backlog_patch(f"/issues/{row['child_key']}", {"actualHours": new_total})

        # ダッシュボードへ即時反映
        _cache.pop("issues", None)
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
