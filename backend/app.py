"""
BPM バックエンド API
Backlog API からデータを取得し、フロントエンド向けに整形して返す。
"""
import os
import time
import logging
from functools import wraps
from flask import Flask, jsonify, abort
import requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── 設定 ──────────────────────────────────────────
SPACE      = os.environ["BACKLOG_SPACE"]          # 例: yourspace.backlog.com
API_KEY    = os.environ["BACKLOG_API_KEY"]
PROJECT    = os.environ["BACKLOG_PROJECT_KEY"]
CACHE_TTL  = int(os.environ.get("CACHE_TTL", 60))
BASE_URL   = f"https://{SPACE}/api/v2"

# ── シンプルインメモリキャッシュ ────────────────────
_cache: dict = {}

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
    import datetime
    today = datetime.date.today()

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
    remaining_h = round(planned_h * (100 - progress_pct) / 100, 1)

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


def build_response(parents: list, children_map: dict, progress_field_id: int | None, status_field_id: int | None, check_status_field_id: int | None) -> list:
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

    all_issues = fetch_issues_all()

    # 親課題と子課題に分類（親課題は課題種別が「00.案件」かつ状態が「完了」以外のもののみに限定）
    parents      = [i for i in all_issues if i.get("parentIssueId") is None and i.get("issueType", {}).get("name") == "00.案件" and i.get("status", {}).get("name") != "完了"]
    children_raw = [i for i in all_issues if i.get("parentIssueId") is not None]

    # children_map: parentIssueId -> [child, ...]
    children_map: dict = {}
    for c in children_raw:
        pid = c["parentIssueId"]
        children_map.setdefault(pid, []).append(c)

    return build_response(parents, children_map, progress_field_id, status_field_id, check_status_field_id)


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "project": PROJECT, "space": SPACE})


@app.errorhandler(502)
@app.errorhandler(500)
def handle_error(e):
    return jsonify({"error": str(e.description)}), e.code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
