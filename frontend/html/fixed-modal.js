// 固定作業の工数登録モーダル（一覧・ガント・日別実績 共通）
// 各ページは起動時に FixedModal.init({ onSaved? }) を一度呼び、ボタンから openFixedModal() を呼ぶ。
// 登録は SQLite のみ（Backlog には連動しない）。同じ担当者・日付・区分は上書きされ、0 で取り消しになる。
(function () {
  // Backlog に存在しない固定の作業区分。実績はこのキーを parent_key として worklog テーブルに登録する
  // （Backlog の課題キーとは衝突しない形式）。日別実績の表はこの定義を行の並びにも使う。
  const CATEGORIES = [
    { key: 'FIX-MAINT',   title: '保守作業' },
    { key: 'FIX-MGMT',    title: '管理業務' },
    { key: 'FIX-EDU',     title: '開発環境構築／カリキュラム受講' },
    { key: 'FIX-IMPROVE', title: '業務改善／品質改善作業' },
    { key: 'FIX-HATPJ',   title: 'HATPJ業務' },
    { key: 'FIX-OTHER',   title: 'ASPIT外作業' },
  ];
  // 登録モーダルで入力できる区分（保守作業はスプレッドシート取り込み予定のため対象外）
  const INPUT_KEYS = ['FIX-MGMT', 'FIX-EDU', 'FIX-IMPROVE', 'FIX-HATPJ', 'FIX-OTHER'];
  const NAME_KEY = 'bpm2-fixed-name';
  const INDEX = Object.fromEntries(CATEGORIES.map((c, i) => [c.key, i]));

  let opts = {};
  let loadSeq = 0;   // 担当者・日付を続けて変えたとき、古い読み込み結果で上書きしないための連番

  const MODAL_HTML = `
<div class="fx-overlay" id="fxModal" onclick="if(event.target===this)closeFixedModal()">
  <div class="fx-modal" role="dialog" aria-labelledby="fxTitle">
    <div class="fx-head">
      <div class="fx-title" id="fxTitle">✏️ 固定作業の工数登録</div>
      <button class="fx-close" onclick="closeFixedModal()" aria-label="閉じる">✕</button>
    </div>
    <div class="fx-body">
      <div class="fx-row">
        <label for="fxName">担当者</label>
        <input type="text" id="fxName" list="fxNameList" placeholder="例: 田中(弘)" autocomplete="off"
               onchange="loadFixedHours()">
        <datalist id="fxNameList"></datalist>
        <span class="unit"></span>
      </div>
      <div class="fx-row">
        <label for="fxDate">日付</label>
        <input type="date" id="fxDate" onchange="loadFixedHours()">
        <span class="unit"></span>
      </div>
      <div class="fx-sep"></div>
      <div id="fxHourRows" style="display:flex;flex-direction:column;gap:10px"></div>
      <div class="fx-sep"></div>
      <div class="fx-row">
        <label>合計</label>
        <span class="fx-total" id="fxTotal">0</span>
        <span class="unit">h</span>
      </div>
      <div class="fx-msg" id="fxMsg"></div>
    </div>
    <div class="fx-foot">
      <button class="fx-btn" onclick="closeFixedModal()">閉じる</button>
      <button class="fx-btn primary" id="fxSave" onclick="saveFixedHours()">登録</button>
    </div>
  </div>
</div>`;

  function escHtml(str) {
    return String(str ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function todayIso() {
    const t = new Date();
    return `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, '0')}-${String(t.getDate()).padStart(2, '0')}`;
  }

  function init(options = {}) {
    opts = options;
    if (document.getElementById('fxModal')) return;
    document.body.insertAdjacentHTML('beforeend', MODAL_HTML);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && document.getElementById('fxModal').classList.contains('open')) close();
    });
  }

  // 担当者候補は実績入力で使われた名前（/api/worklog/names）。取得に失敗しても手入力はできる。
  async function loadNameList() {
    try {
      const res = await fetch('/api/worklog/names?t=' + Date.now());
      if (!res.ok) return;
      const names = await res.json();
      document.getElementById('fxNameList').innerHTML =
        names.map(n => `<option value="${escHtml(n)}">`).join('');
    } catch (e) {}
  }

  function open() {
    document.getElementById('fxHourRows').innerHTML = INPUT_KEYS.map(k => {
      const c = CATEGORIES[INDEX[k]];
      return `<div class="fx-row">
          <label for="fx_${k}">${escHtml(c.title)}</label>
          <input type="number" id="fx_${k}" data-key="${k}" min="0" max="24" step="0.25" placeholder="0"
                 oninput="updateFixedTotal()">
          <span class="unit">h</span>
        </div>`;
    }).join('');
    loadNameList();
    let saved = '';
    try { saved = localStorage.getItem(NAME_KEY) || ''; } catch (e) {}
    document.getElementById('fxName').value = saved;
    document.getElementById('fxDate').value = todayIso();
    setMsg('');
    document.getElementById('fxModal').classList.add('open');
    document.getElementById(saved ? 'fx_' + INPUT_KEYS[0] : 'fxName').focus();
    loadHours();
  }
  function close() {
    document.getElementById('fxModal').classList.remove('open');
  }
  function setMsg(text, cls = '') {
    const el = document.getElementById('fxMsg');
    el.textContent = text;
    el.className = 'fx-msg ' + cls;
  }
  function inputs() {
    return INPUT_KEYS.map(k => document.getElementById('fx_' + k));
  }
  function updateTotal() {
    const sum = inputs().reduce((a, el) => a + (Number(el.value) || 0), 0);
    document.getElementById('fxTotal').textContent = String(Math.round(sum * 100) / 100);
  }

  // 担当者・日付に登録済みの値を入力欄に読み込む（再登録が上書きになることが分かるように）
  async function loadHours() {
    const name = document.getElementById('fxName').value.trim();
    const date = document.getElementById('fxDate').value;
    const seq = ++loadSeq;
    inputs().forEach(el => { el.value = ''; });
    updateTotal();
    setMsg('');
    if (!name || !date) return;
    try {
      const res = await fetch(`/api/fixed-worklog?name=${encodeURIComponent(name)}&date=${encodeURIComponent(date)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      if (seq !== loadSeq) return;
      inputs().forEach(el => { el.value = data[el.dataset.key] ? String(data[el.dataset.key]) : ''; });
      updateTotal();
      if (Object.keys(data).length) setMsg('登録済みの工数を表示しています。変更して「登録」で上書きします。');
    } catch (e) {
      if (seq === loadSeq) setMsg('登録済み工数の読み込みに失敗しました: ' + e.message, 'error');
    }
  }

  async function save() {
    const name = document.getElementById('fxName').value.trim();
    const date = document.getElementById('fxDate').value;
    if (!name) { setMsg('担当者を入力してください', 'error'); return; }
    if (!date) { setMsg('日付を入力してください', 'error'); return; }

    const hours = {};
    for (const el of inputs()) {
      const label = CATEGORIES[INDEX[el.dataset.key]].title;
      const raw = el.value.trim();
      const h = raw === '' ? 0 : Number(raw);
      if (!Number.isFinite(h) || h < 0 || h > 24) { setMsg(`${label} は 0〜24 時間で入力してください`, 'error'); el.focus(); return; }
      if (Math.abs(h * 4 - Math.round(h * 4)) > 1e-9) { setMsg(`${label} は 0.25 時間単位で入力してください`, 'error'); el.focus(); return; }
      hours[el.dataset.key] = h;
    }

    const btn = document.getElementById('fxSave');
    btn.disabled = true;
    setMsg('登録中...');
    try {
      const res = await fetch('/api/fixed-worklog', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, date, hours }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      try { localStorage.setItem(NAME_KEY, name); } catch (e) {}
      if (opts.onSaved) await opts.onSaved(date);
      const total = Object.values(data.hours || {}).reduce((a, b) => a + b, 0);
      setMsg(`${date} ${name} の工数を登録しました（合計 ${Math.round(total * 100) / 100} h）`, 'ok');
    } catch (e) {
      setMsg('登録に失敗しました: ' + e.message, 'error');
    } finally {
      btn.disabled = false;
    }
  }

  window.FixedModal = { init, open, close, CATEGORIES, INPUT_KEYS, INDEX };
  // 注入した HTML の inline onclick/onchange から呼べるようにグローバルにも公開する
  Object.assign(window, {
    openFixedModal: open,
    closeFixedModal: close,
    loadFixedHours: loadHours,
    saveFixedHours: save,
    updateFixedTotal: updateTotal,
  });
})();
