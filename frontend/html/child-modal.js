// 子課題詳細モーダル（担当者・進捗・日付・チェック状態の編集 + 実績工数入力パネル）。
// index.html（一覧）と gantt.html（ガント）から共通で読み込む。
// 利用側は起動時に ChildModal.init({ findChild, onDataChanged, getStandaloneWorklogId }) を呼び、
// 子課題タイトルの onclick から ChildModal.open(id)（= グローバルの openChildModal(id)）で開く。
//
// 実績工数入力パネルは、このモーダル内('dw' プレフィックス)と、一覧画面が独自に持つ
// 単独モーダル('w' プレフィックス、index.html にそのまま残す)の二箇所で共用する。
// 'w' 側の対象子課題IDは opts.getStandaloneWorklogId() 経由で取得する（未指定なら 'w' は使わない）。
(function () {
  const MODAL_HTML = `
<div class="modal-overlay" id="childModal" onclick="if(event.target===this)closeChildModal()">
  <div class="modal" role="dialog" aria-modal="true">
    <div class="modal-head">
      <div>
        <span class="m-key" id="mKey"></span>
        <div class="m-title" id="mTitle"></div>
      </div>
      <button class="modal-close" onclick="closeChildModal()" aria-label="閉じる">✕</button>
    </div>
    <div class="modal-body">
      <div class="m-grid" id="mGrid"></div>
    </div>
    <div class="modal-foot">
      <span class="m-msg" id="mMsg"></span>
      <button class="m-save" id="mSave" onclick="saveChildEdits()">保存</button>
      <a class="m-link" id="mLink" href="#" target="_blank">Backlog で開く ↗</a>
    </div>
    <div class="modal-body" style="padding-top:16px">
      <div class="w-history" style="margin-top:0;border-top:none;padding-top:0">
        <div class="w-history-title">実績工数を入力</div>
        <div class="w-form" style="margin-bottom:16px">
          <div class="m-field">
            <span class="m-label">追加工数 (h) <span style="color:var(--red)">*</span></span>
            <input type="number" step="0.25" min="0" class="m-select" id="dwHours" placeholder="例: 1.5">
          </div>
          <div class="m-field">
            <span class="m-label">名前 <span style="color:var(--red)">*</span></span>
            <input type="text" class="m-select" id="dwName" placeholder="入力者名">
          </div>
          <div class="m-field">
            <span class="m-label">日時 <span style="color:var(--red)">*</span></span>
            <input type="datetime-local" class="m-select" id="dwDate">
          </div>
          <div class="w-form-foot">
            <span class="m-msg" id="dwMsg"></span>
            <button class="m-save" id="dwAdd" onclick="addWorklog('dw')">追加</button>
          </div>
        </div>
        <div class="w-history-title">入力履歴</div>
        <div id="dwHistory"></div>
      </div>
    </div>
  </div>
</div>`;

  let opts = { findChild: null, onDataChanged: null, getStandaloneWorklogId: null };
  let modalChildId = null;          // 現在モーダルで開いている子課題ID
  let modalOriginal = {};           // モーダル表示時の初期選択値（変更検知用）
  let fieldOptions = null;          // 進捗率・チェック状態の選択肢
  let projectUsers = null;          // 担当者プルダウンの選択肢 [{id, name}]

  const WORKLOG_NAME_KEY = 'bpm2-worklog-name';
  const worklogRowsByPrefix = { w: [], dw: [] };   // 表示中の履歴（削除時の確認メッセージ用）

  function escHtml(s) {
    return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  async function notifyChanged() {
    if (opts.onDataChanged) await opts.onDataChanged();
  }

  function findChild(childId) {
    return opts.findChild ? opts.findChild(childId) : null;
  }

  // prefix が対象とする子課題IDを返す（'dw' は詳細モーダル、'w' は各ページの単独モーダル）
  function worklogTargetId(prefix) {
    if (prefix === 'dw') return modalChildId;
    return opts.getStandaloneWorklogId ? opts.getStandaloneWorklogId() : null;
  }

  // ── 編集用フィールド選択肢の取得 ──────────────────
  async function loadFieldOptions() {
    try {
      const res = await fetch('/api/field-options');
      if (res.ok) fieldOptions = await res.json();
    } catch (e) { /* 選択肢取得失敗時はモーダルで閲覧のみ */ }
  }

  async function loadProjectUsers() {
    try {
      const res = await fetch('/api/project-users');
      if (res.ok) projectUsers = await res.json();
    } catch (e) { /* 取得失敗時は担当者を閲覧表示にフォールバック */ }
  }

  // 選択肢を <option> 文字列に変換。現在値(name一致)を選択状態にする
  function optionsHTML(items, currentName) {
    return items.map(it =>
      `<option value="${it.id}"${String(it.name) === String(currentName) ? ' selected' : ''}>${escHtml(it.name)}</option>`
    ).join('');
  }

  function editFieldHTML(label, selectId, items, currentName) {
    if (!items) {
      // 選択肢が取れない場合は閲覧表示にフォールバック
      return `<div class="m-field">
        <span class="m-label">${label}</span>
        <span class="m-value">${escHtml(currentName || '—')}</span>
      </div>`;
    }
    return `<div class="m-field">
      <span class="m-label">${label}</span>
      <select class="m-select" id="${selectId}">${optionsHTML(items, currentName)}</select>
    </div>`;
  }

  // 担当者選択肢を <option> 文字列に変換。現在値(id一致)を選択状態にする。先頭に「未割当」を含む
  function assigneeOptionsHTML(users, currentId) {
    const none = currentId == null || currentId === '' ? ' selected' : '';
    return `<option value=""${none}>未割当</option>` + users.map(u =>
      `<option value="${u.id}"${String(u.id) === String(currentId) ? ' selected' : ''}>${escHtml(u.name)}</option>`
    ).join('');
  }

  function openChildModal(childId) {
    const found = findChild(childId);
    if (!found) return;
    const { child: c, parent: p } = found;
    modalChildId = childId;

    document.getElementById('mKey').textContent = c.id;
    document.getElementById('mTitle').textContent = c.title;
    document.getElementById('mLink').href = c.url;
    setModalMsg('', '');

    const progItems  = fieldOptions && fieldOptions.progress    ? fieldOptions.progress.items    : null;
    const checkItems = fieldOptions && fieldOptions.checkStatus ? fieldOptions.checkStatus.items : null;

    const diff = c.actualH - c.plannedH;
    // 親案件・工数（閲覧専用。予定/実績/差分/残工数は工数入力後に refreshChildModalHours() で更新するため id を持たせる）
    const readonly =
      `<div class="m-field full">
        <span class="m-label">親案件</span>
        <span class="m-value">${escHtml(p.id)} ${escHtml(p.title)}</span>
      </div>` +
      [
        ['予定工数', 'mPlannedH', c.plannedH + ' h'],
        ['実績工数', 'mActualH', c.actualH + ' h'],
        ['差分', 'mDiffH', (diff >= 0 ? '+' : '') + diff + ' h'],
        ['残工数', 'mRemainingH', c.remainingH != null ? c.remainingH + ' h' : '—'],
      ].map(([label, id, val]) =>
        `<div class="m-field">
          <span class="m-label">${label}</span>
          <span class="m-value mono" id="${id}">${val}</span>
        </div>`
      ).join('') +
      `<div class="m-field">
        <span class="m-label">状態</span>
        <span class="m-value">${escHtml(c.status || '—')}</span>
      </div>`;

    // 担当者（プルダウン取得済みなら編集可能、失敗時は閲覧表示にフォールバック）
    const assigneeField = projectUsers
      ? `<div class="m-field">
          <span class="m-label">担当者</span>
          <select class="m-select" id="mAssignee">${assigneeOptionsHTML(projectUsers, c.assigneeId)}</select>
        </div>`
      : `<div class="m-field">
          <span class="m-label">担当者</span>
          <span class="m-value">${escHtml(c.assignee || '未割当')}</span>
        </div>`;

    // 編集可能フィールド（開始日・期限日・進捗率・チェック状態）
    const editable =
      `<div class="m-field">
        <span class="m-label">開始日</span>
        <input type="date" class="m-select" id="mStart" value="${c.start || ''}">
      </div>` +
      `<div class="m-field">
        <span class="m-label">期限日</span>
        <input type="date" class="m-select" id="mDue" value="${c.due || ''}">
      </div>` +
      editFieldHTML('進捗率', 'mProgress', progItems, c.progress != null ? c.progress : '') +
      editFieldHTML('チェック状態', 'mCheckStatus', checkItems, c.checkStatus || '');

    document.getElementById('mGrid').innerHTML = assigneeField + readonly + editable;

    // 変更検知用に初期値を保持
    const progEl     = document.getElementById('mProgress');
    const checkEl    = document.getElementById('mCheckStatus');
    const assigneeEl = document.getElementById('mAssignee');
    modalOriginal = {
      progress:    progEl     ? progEl.value     : null,
      checkStatus: checkEl    ? checkEl.value    : null,
      assignee:    assigneeEl ? assigneeEl.value : null,
      start:       c.start || '',
      due:         c.due   || '',
    };

    document.getElementById('childModal').classList.add('open');
    mountWorklogPanel('dw', childId);
  }

  function setModalMsg(text, cls) {
    const el = document.getElementById('mMsg');
    el.textContent = text;
    el.className = 'm-msg' + (cls ? ' ' + cls : '');
  }

  async function saveChildEdits() {
    if (!modalChildId) return;
    const progEl     = document.getElementById('mProgress');
    const checkEl    = document.getElementById('mCheckStatus');
    const startEl    = document.getElementById('mStart');
    const dueEl      = document.getElementById('mDue');
    const assigneeEl = document.getElementById('mAssignee');
    const payload = {};
    // 変更があった項目だけを送る（同値だと Backlog が "No comment content" で弾くため）
    if (progEl     && progEl.value     !== modalOriginal.progress)    payload.progressItemId    = parseInt(progEl.value, 10);
    if (checkEl    && checkEl.value    !== modalOriginal.checkStatus) payload.checkStatusItemId = parseInt(checkEl.value, 10);
    if (startEl    && startEl.value    !== modalOriginal.start)       payload.startDate = startEl.value;
    if (dueEl      && dueEl.value      !== modalOriginal.due)         payload.dueDate   = dueEl.value;
    // 担当者: 空文字は「未割当」へのクリア指示（Backlog 側の挙動を実課題で確認済み）
    if (assigneeEl && assigneeEl.value !== modalOriginal.assignee)    payload.assigneeId = assigneeEl.value ? parseInt(assigneeEl.value, 10) : '';

    if (Object.keys(payload).length === 0) {
      setModalMsg('変更がありません', '');
      return;
    }

    if (startEl && dueEl && startEl.value && dueEl.value && startEl.value > dueEl.value) {
      setModalMsg('開始日が期限日より後になっています', 'err');
      return;
    }

    const btn = document.getElementById('mSave');
    btn.disabled = true;
    setModalMsg('保存中...', '');
    try {
      const res = await fetch('/api/issue/' + encodeURIComponent(modalChildId), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try { const j = await res.json(); if (j.error) detail = j.error; } catch (e) {}
        throw new Error(detail);
      }
      setModalMsg('保存しました', 'ok');
      // 保存後の値を初期値として更新（連続保存での誤再送を防止）
      if (progEl)     modalOriginal.progress    = progEl.value;
      if (checkEl)    modalOriginal.checkStatus = checkEl.value;
      if (startEl)    modalOriginal.start       = startEl.value;
      if (dueEl)      modalOriginal.due         = dueEl.value;
      if (assigneeEl) modalOriginal.assignee    = assigneeEl.value;
      await notifyChanged();   // 呼び出し元ページ（テーブル/ガント）を最新化
    } catch (e) {
      setModalMsg('保存失敗: ' + e.message, 'err');
    } finally {
      btn.disabled = false;
    }
  }

  function closeChildModal() {
    modalChildId = null;
    const el = document.getElementById('childModal');
    if (el) el.classList.remove('open');
  }

  // ── 実績工数 日々入力レイヤー ─────────────────────
  // フォームを初期値にリセットし、入力履歴を読み込む（独立モーダル・詳細モーダル共通）
  async function mountWorklogPanel(prefix, childId) {
    document.getElementById(prefix + 'Hours').value = '';
    // 名前は前回入力をブラウザから復元
    document.getElementById(prefix + 'Name').value = localStorage.getItem(WORKLOG_NAME_KEY) || '';
    document.getElementById(prefix + 'Date').value = nowLocalDatetime();
    setWorklogMsg(prefix, '', '');
    document.getElementById(prefix + 'History').innerHTML = '<div class="w-hist-empty">読込中...</div>';
    await loadWorklogHistory(prefix, childId);
  }

  function nowLocalDatetime() {
    const d = new Date();
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function fmtDateTime(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return d.toLocaleString('ja-JP', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  }

  async function loadWorklogHistory(prefix, childId) {
    const box = document.getElementById(prefix + 'History');
    try {
      const res = await fetch('/api/worklog/' + encodeURIComponent(childId));
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const rows = await res.json();
      worklogRowsByPrefix[prefix] = rows;
      if (!rows.length) {
        box.innerHTML = '<div class="w-hist-empty">履歴はありません</div>';
        return;
      }
      box.innerHTML = rows.map(r =>
        `<div class="w-hist-row">
          <span class="w-hist-name">${escHtml(r.name)}</span>
          <span class="w-hist-date">${fmtDateTime(r.added_at)}</span>
          <span class="w-hist-hours">+${r.hours}h</span>
          <button class="w-hist-del" onclick="deleteWorklog('${prefix}',${r.id})" title="この履歴を削除" aria-label="削除">🗑</button>
        </div>`
      ).join('');
    } catch (e) {
      box.innerHTML = `<div class="w-hist-empty">履歴の取得に失敗しました (${escHtml(e.message)})</div>`;
    }
  }

  async function deleteWorklog(prefix, entryId) {
    const childId = worklogTargetId(prefix);
    if (!childId) return;
    const r = worklogRowsByPrefix[prefix].find(x => x.id === entryId);
    if (!r) return;
    const ok = confirm(
      `この入力履歴を削除しますか？\n\n` +
      `${r.name}  ${fmtDateTime(r.added_at)}  +${r.hours}h\n\n` +
      `子課題の実績工数から ${r.hours}h 減算されます（0 未満になる場合は 0 になります）。`
    );
    if (!ok) return;

    document.querySelectorAll(`#${prefix}History .w-hist-del`).forEach(b => b.disabled = true);
    setWorklogMsg(prefix, '削除中...', '');
    try {
      const res = await fetch('/api/worklog/entry/' + entryId, { method: 'DELETE' });
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try { const j = await res.json(); if (j.error) detail = j.error; } catch (e) {}
        throw new Error(detail);
      }
      setWorklogMsg(prefix, '削除しました', 'ok');
    } catch (e) {
      setWorklogMsg(prefix, '削除失敗: ' + e.message, 'err');
    } finally {
      await loadWorklogHistory(prefix, childId);   // 成否に関わらず履歴を最新化（ボタンも再有効化）
      await notifyChanged();                        // 呼び出し元ページを最新化
      if (prefix === 'dw') refreshChildModalHours();
    }
  }

  function setWorklogMsg(prefix, text, cls) {
    const el = document.getElementById(prefix + 'Msg');
    el.textContent = text;
    el.className = 'm-msg' + (cls ? ' ' + cls : '');
  }

  async function addWorklog(prefix) {
    const childId = worklogTargetId(prefix);
    if (!childId) return;
    const found = findChild(childId);
    if (!found) return;
    const { parent: p } = found;

    const hoursRaw = document.getElementById(prefix + 'Hours').value.trim();
    const name = document.getElementById(prefix + 'Name').value.trim();
    const addedAt = document.getElementById(prefix + 'Date').value;

    // 入力された名前はブラウザに保持（次回表示時に復元）
    if (name) { try { localStorage.setItem(WORKLOG_NAME_KEY, name); } catch (e) {} }

    // バリデーション: 全項目必須・追加工数は数値のみ
    if (!hoursRaw && !name) { setWorklogMsg(prefix, '追加工数(h) と 名前 は必須です', 'err'); return; }
    if (!name) { setWorklogMsg(prefix, '名前は必須です', 'err'); return; }
    if (!hoursRaw) { setWorklogMsg(prefix, '追加工数(h) は必須です', 'err'); return; }
    const hours = Number(hoursRaw);
    if (!Number.isFinite(hours)) { setWorklogMsg(prefix, '追加工数(h) は数値で入力してください', 'err'); return; }
    if (hours <= 0) { setWorklogMsg(prefix, '追加工数(h) は 0 より大きい数値で入力してください', 'err'); return; }
    if (!addedAt) { setWorklogMsg(prefix, '日時は必須です', 'err'); return; }

    const btn = document.getElementById(prefix + 'Add');
    btn.disabled = true;
    setWorklogMsg(prefix, '追加中...', '');
    try {
      const res = await fetch('/api/worklog/' + encodeURIComponent(childId), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hours, name, addedAt, parentKey: p.id, parentTitle: p.title }),
      });
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try { const j = await res.json(); if (j.error) detail = j.error; } catch (e) {}
        throw new Error(detail);
      }
      setWorklogMsg(prefix, '追加しました', 'ok');
      document.getElementById(prefix + 'Hours').value = '';
      document.getElementById(prefix + 'Date').value = nowLocalDatetime();
      await loadWorklogHistory(prefix, childId);   // 履歴更新
      await notifyChanged();                        // 呼び出し元ページを最新化
      if (prefix === 'dw') refreshChildModalHours();
    } catch (e) {
      setWorklogMsg(prefix, '追加失敗: ' + e.message, 'err');
    } finally {
      btn.disabled = false;
    }
  }

  // 工数入力後、詳細モーダル内の予定/実績/差分/残工数だけを更新する
  // （モーダル全体を再描画すると編集中の進捗率・日付が失われるため、対象要素のみ差し替える）
  function refreshChildModalHours() {
    if (!modalChildId) return;
    const found = findChild(modalChildId);
    if (!found) return;
    const { child: c } = found;
    const diff = c.actualH - c.plannedH;
    document.getElementById('mPlannedH').textContent   = c.plannedH + ' h';
    document.getElementById('mActualH').textContent    = c.actualH + ' h';
    document.getElementById('mDiffH').textContent      = (diff >= 0 ? '+' : '') + diff + ' h';
    document.getElementById('mRemainingH').textContent = c.remainingH != null ? c.remainingH + ' h' : '—';
  }

  function injectModalHTML() {
    if (document.getElementById('childModal')) return;
    document.body.insertAdjacentHTML('beforeend', MODAL_HTML);
  }

  function init(o) {
    opts = Object.assign({}, opts, o || {});
    injectModalHTML();
    loadFieldOptions();
    loadProjectUsers();
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') closeChildModal();
    });
  }

  window.ChildModal = { init, open: openChildModal };
  // 既存の onclick 属性（注入 HTML・履歴行の動的 HTML・呼び出し元ページ）から
  // 従来どおりの関数名で呼べるようにグローバルへエイリアスする。
  window.openChildModal      = openChildModal;
  window.closeChildModal     = closeChildModal;
  window.saveChildEdits      = saveChildEdits;
  window.mountWorklogPanel   = mountWorklogPanel;
  window.loadWorklogHistory  = loadWorklogHistory;
  window.addWorklog          = addWorklog;
  window.deleteWorklog       = deleteWorklog;
  window.setWorklogMsg       = setWorklogMsg;
  window.worklogTargetId     = worklogTargetId;
})();
