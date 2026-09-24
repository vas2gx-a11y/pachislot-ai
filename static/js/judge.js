/*
 * 設定判別ページの画面まわり。計算は bayes.js にあり、こちらはその入出力だけを扱う。
 *
 * 【localStorageに置いているもの】
 *   店舗条件（貸出・交換・時速・持ち玉比率）と、判別結果のログ。
 *   どちらも「その端末で打った人の持ち物」で、他人と共有する意味がない。
 *   機種スペックはサーバー（SQLite）が正で、ここには持たない。
 *   ログを残しておくと、次に同じ台に座ったとき前回の推定を事前確率として引き継げる。
 */

const STORE_KEY = 'pachislot_judge_v1';
const DEFAULT_SHOP = { rental: 46, exchange: 52, gamesPerHour: 800, holdingRatio: 70 };

// 判別力のレベルと表示色の対応。色の判断はここだけに置く（bayes.js は色を持たない）
const LEVEL_COLORS = {
  strong: '#059669',
  medium: '#2563eb',
  weak: '#d97706',
  none: '#dc2626'
};

const MACHINES = Array.isArray(window.JUDGE_MACHINES) ? window.JUDGE_MACHINES : [];

// 機械割が未登録の機種でも時給を出せるようにするための代替値（あくまで目安）
const FALLBACK_PAYOUTS = [97, 98, 99.5, 102, 105, 109];

let state = { shop: { ...DEFAULT_SHOP }, logs: [] };
let lastResult = null;


/* ============================================================
 * 保存
 * ========================================================== */

function loadState() {
  let saved = null;
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (raw) saved = JSON.parse(raw);
  } catch (e) {
    console.warn('保存データの読み込みに失敗しました:', e);
  }
  state.shop = (saved && saved.shop) ? { ...DEFAULT_SHOP, ...saved.shop } : { ...DEFAULT_SHOP };
  state.logs = (saved && Array.isArray(saved.logs)) ? saved.logs : [];
}

function saveState() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(state));
  } catch (e) {
    // プライベートモードや容量超過で保存できないことがある。計算自体は続けられるので止めない。
    console.warn('保存に失敗しました:', e);
    alert('データの保存に失敗しました。ブラウザの設定（プライベートモード等）をご確認ください。');
  }
}


/* ============================================================
 * 小物
 * ========================================================== */

function esc(str) {
  return String(str == null ? '' : str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function el(id) { return document.getElementById(id); }

function numVal(id, fallback = 0) {
  const node = el(id);
  if (!node) return fallback;
  const v = parseFloat(node.value);
  return Number.isFinite(v) ? v : fallback;
}

function findMachine(id) {
  return MACHINES.find(m => String(m.id) === String(id)) || null;
}

function selectedMachine() {
  return findMachine(el('st-model-select').value);
}

function todayString() {
  return new Date().toISOString().split('T')[0];
}


/* ============================================================
 * 店舗条件
 * ========================================================== */

function getShopConfig() {
  const s = state.shop;
  // 入力は「1,000円で何枚」だが、計算では「1枚あたり何円」を使う
  return {
    rentalRate: 1000 / (s.rental || 46),
    exchangeRate: 1000 / (s.exchange || 52),
    gamesPerHour: s.gamesPerHour || 800,
    holdingRatio: (s.holdingRatio ?? 70) / 100
  };
}

function renderShopConfigInputs() {
  el('shop-rental').value = state.shop.rental;
  el('shop-exchange').value = state.shop.exchange;
  el('shop-gph').value = state.shop.gamesPerHour;
  el('shop-holding').value = state.shop.holdingRatio;
  el('shop-holding-label').innerText = state.shop.holdingRatio + '%';
  renderShopGapInfo();
}

function onHoldingInput() {
  el('shop-holding-label').innerText = el('shop-holding').value + '%';
}

function onShopConfigChange() {
  const rental = numVal('shop-rental', 46);
  const exchange = numVal('shop-exchange', 52);
  const gph = numVal('shop-gph', 800);

  if (!(rental > 0) || !(exchange > 0) || !(gph > 0)) {
    alert('貸出枚数・交換枚数・時速は0より大きい数値で入力してください。');
    renderShopConfigInputs();
    return;
  }

  state.shop = {
    rental, exchange, gamesPerHour: gph,
    holdingRatio: parseInt(el('shop-holding').value, 10)
  };
  saveState();
  renderShopGapInfo();
}

function renderShopGapInfo() {
  const c = getShopConfig();
  const gap = Math.max(0, c.rentalRate - c.exchangeRate);
  const target = el('shop-gap-info');

  if (gap <= 0) {
    target.innerHTML = `貸出 ${c.rentalRate.toFixed(2)}円/枚、交換 ${c.exchangeRate.toFixed(2)}円/枚 → <strong>等価</strong>（ギャップ損なし）`;
    return;
  }

  const lossPerHour = Math.round(3 * (1 - c.holdingRatio) * gap * c.gamesPerHour);
  target.innerHTML =
    `貸出 ${c.rentalRate.toFixed(2)}円/枚、交換 ${c.exchangeRate.toFixed(2)}円/枚 → ギャップ <strong>${gap.toFixed(2)}円/枚</strong><br>` +
    `現金投資分のギャップ損は <strong>約${lossPerHour.toLocaleString()}円/h</strong>（持ち玉比率${Math.round(c.holdingRatio * 100)}%時）。<br>` +
    `<span class="text-red-600">持ち玉比率の指定が結果を大きく左右します。</span>`;
}


/* ============================================================
 * 事前確率
 * ========================================================== */

function onPriorModeChange() {
  const mode = el('prior-mode').value;
  el('prior-shop-box').classList.toggle('hidden', mode !== 'shop');
  el('prior-prevday-box').classList.toggle('hidden', mode !== 'prevday');
  el('prior-prevcount-box').classList.toggle('hidden', mode !== 'prevcount');
  el('prior-lastsession-box').classList.toggle('hidden', mode !== 'lastsession');
  if (mode === 'prevcount') renderPrevCountInputs();
  renderPriorPreview();
}

function renderMorningSignalChips(containerId) {
  const box = el(containerId);
  if (!box) return;
  box.innerHTML = MORNING_SIGNALS.map(sig =>
    `<span class="chip chip-toggle" data-signal="${esc(sig.id)}">${esc(sig.name)}</span>`
  ).join('');
  box.querySelectorAll('.chip-toggle').forEach(chip => {
    chip.addEventListener('click', () => {
      chip.classList.toggle('selected');
      renderPriorPreview();
    });
  });
}

function selectedMorningSignals(containerId) {
  return [...document.querySelectorAll(`#${containerId} .chip-toggle.selected`)].map(c => c.dataset.signal);
}

function renderStayProbability(boxId, stayProb) {
  const box = el(boxId);
  if (!box) return;

  if (stayProb === null) { box.classList.add('hidden'); return; }

  box.classList.remove('hidden');
  const stayPct = stayProb * 100;
  const resetPct = 100 - stayPct;
  box.innerHTML = `
    <div class="text-xs text-gray-500 mb-1">朝一挙動からの判定</div>
    <div class="flex h-6 rounded-md overflow-hidden text-[0.65rem] font-bold text-white">
      <div style="width:${stayPct.toFixed(1)}%; background:#059669;" class="flex items-center justify-center">
        ${stayPct >= 20 ? '据え置き ' + stayPct.toFixed(1) + '%' : ''}
      </div>
      <div style="width:${resetPct.toFixed(1)}%; background:#dc2626;" class="flex items-center justify-center">
        ${resetPct >= 20 ? 'リセット ' + resetPct.toFixed(1) + '%' : ''}
      </div>
    </div>`;
}

// 前日の観測回数の入力欄。機種の判別要素にあわせて作る。
function renderPrevCountInputs() {
  const m = selectedMachine();
  const wrap = el('prevcount-inputs');
  if (!m) { wrap.innerHTML = ''; return; }

  // 母数を個別入力する要素は、前日の総回転数から逆算できないので対象外
  const items = (m.judgeItems || []).filter(it => denomBaseOf(it) !== 'custom');
  if (items.length === 0) {
    wrap.innerHTML = '<div class="hint">この機種には毎G試行する判別要素が登録されていないため、逆算できません。</div>';
    return;
  }

  wrap.innerHTML = items.map(item => {
    const idx = m.judgeItems.indexOf(item);
    return `
      <div class="obs-row mb-2">
        <div class="flex-1">
          <label class="label">${esc(item.name)}</label>
          <input type="number" class="input input-sm prevcount-obs" data-idx="${idx}" min="0" value="0">
        </div>
        <div class="flex-1 text-[0.65rem] text-gray-400 pb-2 leading-tight">
          ${item.denominators.map(d => '1/' + d).join(' · ')}
        </div>
      </div>`;
  }).join('');

  wrap.querySelectorAll('.prevcount-obs').forEach(input => {
    input.addEventListener('change', renderPriorPreview);
  });
}

// 前日の実測値から前日の設定を推定し、移行マトリクスを通して本日の事前確率にする
function buildPriorFromPrevCounts() {
  const m = selectedMachine();
  const games = numVal('prevcount-games', 0);
  if (!m || !(games > 0)) return null;

  const observations = [];
  document.querySelectorAll('.prevcount-obs').forEach(input => {
    const idx = parseInt(input.dataset.idx, 10);
    const item = m.judgeItems[idx];
    const k = parseFloat(input.value);
    if (!item || !Number.isFinite(k) || k <= 0 || k > games) return;
    observations.push({ n: games, k: k, p: item.denominators.map(d => 1 / d) });
  });

  if (observations.length === 0) return null;

  // 前日の推定は、その日の情報だけで行う（均等な事前確率から出発する）
  const prevPosteriors = calculateBayesianPosteriors(new Array(6).fill(1 / 6), observations, null);
  if (prevPosteriors.every(p => p === 0)) return null;

  const signals = selectedMorningSignals('morning-signals-count');
  const blended = morningBlendedPrior(prevPosteriors, DEFAULT_TRANSITION_MATRIX, signals);
  return { prevPosteriors, todayPrior: blended.prior, stayProb: blended.stayProb };
}

// 同じ店舗・台番号・機種で最後に保存された判別結果を探す
function findLastSession() {
  const store = (el('prior-store').value || '').trim();
  const unit = (el('prior-unit').value || '').trim();
  const machine = selectedMachine();
  if (!store || !unit || !machine) return null;

  const today = todayString();

  return state.logs
    .filter(l => Array.isArray(l.posteriors) && l.posteriors.length === 6)
    .filter(l => l.store === store && l.unitNumber === unit && l.model === machine.name)
    .filter(l => l.date < today)   // 当日の記録は「前日データ」にならない
    .sort((a, b) => (a.date < b.date ? 1 : -1))[0] || null;
}

function shopBaselinePrior() {
  const vals = [...document.querySelectorAll('.prior-shop-input')].map(x => Math.max(0, parseFloat(x.value) || 0));
  const sum = vals.reduce((a, b) => a + b, 0);
  return sum > 0 ? vals.map(v => v / sum) : new Array(6).fill(1 / 6);
}

function buildPrior() {
  const mode = el('prior-mode').value;

  if (mode === 'flat') return new Array(6).fill(1 / 6);
  if (mode === 'shop') return shopBaselinePrior();

  // 逆算・引き継ぎは材料が揃わないことがある。その場合はホール配分に落とす。
  if (mode === 'prevcount') {
    const r = buildPriorFromPrevCounts();
    return r ? r.todayPrior : shopBaselinePrior();
  }

  if (mode === 'lastsession') {
    const last = findLastSession();
    if (!last) return shopBaselinePrior();
    const signals = selectedMorningSignals('morning-signals-last');
    return morningBlendedPrior(last.posteriors, DEFAULT_TRANSITION_MATRIX, signals).prior;
  }

  // prevday: 前日の推定設定を手入力する
  const prev = [...document.querySelectorAll('.prior-prev-input')].map(x => Math.max(0, parseFloat(x.value) || 0));
  const sum = prev.reduce((a, b) => a + b, 0);
  const normalizedPrev = sum > 0 ? prev.map(v => (v / sum) * 100) : new Array(6).fill(100 / 6);
  return generateTodayPriorWithMorning(normalizedPrev, DEFAULT_TRANSITION_MATRIX, el('morning-obs').value);
}

function renderPriorPreview() {
  const mode = el('prior-mode').value;

  if (mode === 'prevcount') {
    const infoEl = el('prior-prevcount-info');
    const r = buildPriorFromPrevCounts();
    if (!r) {
      infoEl.innerHTML = '前日の総回転数と観測回数を入力してください。<br>入力がない場合は「ホール配分を仮定」の値を使います。';
      renderStayProbability('stay-prob-box', stayProbabilityFrom(selectedMorningSignals('morning-signals-count')));
    } else {
      const top = r.prevPosteriors.indexOf(Math.max(...r.prevPosteriors)) + 1;
      const prevHigh = r.prevPosteriors.slice(3).reduce((a, b) => a + b, 0);
      infoEl.innerHTML =
        `<strong>前日の推定</strong>：最有力は設定${top}／設定4以上 ${prevHigh.toFixed(1)}%<br>` +
        `<span class="text-gray-400">${r.prevPosteriors.map(p => p.toFixed(1) + '%').join(' / ')}</span><br>` +
        `<span class="text-amber-600">↓ 設定移行マトリクスを適用</span>`;
      renderStayProbability('stay-prob-box', r.stayProb);
    }
  }

  if (mode === 'lastsession') {
    const infoEl = el('prior-lastsession-info');
    const last = findLastSession();
    if (!last) {
      infoEl.innerHTML = '該当する記録が見つかりません。店舗名・台番号・機種が一致する過去の保存が必要です。' +
        '<br>見つからない場合は「ホール配分を仮定」の値を使います。';
      renderStayProbability('stay-prob-box-last', null);
    } else {
      renderStayProbability('stay-prob-box-last', stayProbabilityFrom(selectedMorningSignals('morning-signals-last')));
      const top = last.posteriors.indexOf(Math.max(...last.posteriors)) + 1;
      infoEl.innerHTML = `<strong>${esc(last.date)}</strong> の記録を引き継ぎます` +
        `（${esc(last.totalGames || '?')}G / 最有力は設定${top}）<br>` +
        `前回の推定: ${last.posteriors.map(p => p.toFixed(1) + '%').join(' / ')}`;
    }
  }

  el('prior-preview').innerHTML = settingBarsHtml(buildPrior().map(p => p * 100), () => '#18181b');
}

/** 設定1〜6の横棒。値は%で渡す。 */
function settingBarsHtml(values, colorOf, extraOf) {
  const maxV = Math.max(...values, 0);
  return values.map((v, i) => `
    <div class="flex items-center gap-2 py-0.5">
      <span class="w-12 shrink-0 text-xs text-gray-500">設定${i + 1}</span>
      <div class="bar-bg"><div class="bar" style="width:${maxV > 0 ? (v / maxV * 100).toFixed(1) : 0}%; background:${colorOf(i)};"></div></div>
      <span class="w-12 shrink-0 text-right text-xs font-mono">${v.toFixed(1)}%</span>
      ${extraOf ? extraOf(i) : ''}
    </div>`).join('');
}


/* ============================================================
 * 観測データの入力欄
 * ========================================================== */

// 判別要素が使う母数の種類。成功率型は必ず個別入力。
function denomBaseOf(item) {
  if ((item.type || 'koyaku') === 'ratio') return 'custom';
  const base = item.denomBase || 'total';
  return ['total', 'normal', 'custom'].includes(base) ? base : 'total';
}

// その要素の母数として使うゲーム数を返す
function denominatorGamesFor(item, totalGames, normalGames) {
  if (denomBaseOf(item) === 'normal') {
    return (normalGames > 0) ? normalGames : totalGames;
  }
  return totalGames;
}

function renderObservationInputs() {
  const m = selectedMachine();
  const wrap = el('obs-inputs');
  const games = numVal('st-games', 0);
  const normalGames = numVal('st-normal-games', 0);

  const needsNormal = !!(m && (m.judgeItems || []).some(it => denomBaseOf(it) === 'normal'));
  el('st-normal-games-box').classList.toggle('hidden', !needsNormal);
  el('st-normal-games-note').classList.toggle('hidden', !(needsNormal && !(normalGames > 0)));

  if (!m) {
    wrap.innerHTML = '';
    renderCategoricalInputs(null);
    renderConfirmChips(null);
    renderHintChips();
    return;
  }

  const items = m.judgeItems || [];

  if (items.length === 0) {
    wrap.innerHTML = '<div class="hint">この機種には判別要素が登録されていません。' +
      '機種情報JSON（machine_data/）の setting_estimation.probabilities に小役やボーナスの設定別確率を書くと推測できます。</div>';
  } else {
    // 判別に効く順に並べる。効かない要素は折りたたんで、入力する箇所を絞る。
    const ranked = items.map((item, idx) => ({
      item, idx,
      value: dailyInformationValue('binomial', item.denominators.map(d => 1 / d))
    })).sort((a, b) => b.value - a.value);

    const primary = ranked.filter(r => {
      const power = estimateDiscriminationPower(r.item.denominators);
      return !power || ['strong', 'medium'].includes(power.level);
    });
    const secondary = ranked.filter(r => !primary.includes(r));

    let html = '';
    if (primary.length > 0) {
      html += '<div class="text-xs font-bold text-emerald-600 mt-3 mb-1.5">よく効く要素（優先して入力）</div>';
      html += primary.map(r => observationRowHtml(r.item, r.idx, games, normalGames)).join('');
    }

    if (secondary.length > 0) {
      html += `
        <div class="mt-3">
          <button type="button" class="btn btn-ghost btn-sm w-full" id="secondary-obs-toggle">
            <span id="secondary-obs-label">▸ 効きにくい要素も入力する（${secondary.length}件）</span>
          </button>
          <div id="secondary-obs" class="hidden mt-2">
            <div class="hint">ここにある要素は、1日の稼働では収束しません。入力しても結果はあまり動きません。</div>
            ${secondary.map(r => observationRowHtml(r.item, r.idx, games, normalGames)).join('')}
          </div>
        </div>`;
    }

    wrap.innerHTML = html;

    const toggle = el('secondary-obs-toggle');
    if (toggle) toggle.addEventListener('click', toggleSecondaryObs);
  }

  renderCategoricalInputs(m);
  renderConfirmChips(m);
  renderHintChips();
}

function toggleSecondaryObs() {
  const box = el('secondary-obs');
  const label = el('secondary-obs-label');
  if (!box) return;
  const hidden = box.classList.toggle('hidden');
  label.innerText = label.innerText.replace(hidden ? '▾' : '▸', hidden ? '▸' : '▾');
}

function observationRowHtml(item, idx, games, normalGames) {
  const power = estimateDiscriminationPower(item.denominators);
  const isRatio = denomBaseOf(item) === 'custom';
  const usesNormal = denomBaseOf(item) === 'normal';
  const baseGames = denominatorGamesFor(item, games, normalGames);
  const enough = power && Number.isFinite(power.requiredGames) && baseGames >= power.requiredGames;

  let badge = '';
  if (power && isRatio) {
    badge = `<span style="color:${LEVEL_COLORS[power.level]}" class="font-bold">${esc(power.label)}</span>` +
      `<span class="text-gray-400">／設定1と6を見分けるには約${
        Number.isFinite(power.requiredGames) ? power.requiredGames.toLocaleString() : '−'}回の試行が必要</span>`;
  } else if (power) {
    badge = `<span style="color:${LEVEL_COLORS[power.level]}" class="font-bold">${esc(power.label)}</span>` +
      `<span class="text-gray-400">／収束目安 ${Number.isFinite(power.requiredGames) ? '約' + power.requiredGames.toLocaleString() + 'G' : '判別不可'}</span>` +
      (games > 0
        ? `<span style="color:${enough ? '#059669' : '#d97706'}">／現在${baseGames.toLocaleString()}G ${enough ? '✓ 到達' : '△ 不足'}</span>`
        : '') +
      (Number.isFinite(power.perEventBits)
        ? `<br><span class="text-gray-400">1回の成立で ${power.perEventBits.toFixed(2)} bit 動く（分母が大きい要素ほど1回は重いが、成立回数は少ない）</span>`
        : '');
  }

  const baseNote = isRatio ? ''
    : `<span class="${usesNormal && !(normalGames > 0) ? 'text-red-600' : 'text-gray-400'}">` +
      `母数: ${usesNormal ? '通常時' : '総回転数'} ${baseGames.toLocaleString()}G` +
      `${usesNormal && !(normalGames > 0) ? '（通常時未入力のため総回転数で代用中）' : ''}</span>`;

  return `
    <div class="obs-row">
      ${isRatio ? `
      <div class="flex-1">
        <label class="label">${esc(item.name)} の試行回数</label>
        <input type="number" class="input input-sm obs-trials" data-idx="${idx}" min="0" value="0">
      </div>` : ''}
      <div class="flex-1">
        <label class="label">${esc(item.name)} の${isRatio ? '成功回数' : '回数'}</label>
        <input type="number" class="input input-sm obs-count" data-idx="${idx}" min="0" value="0">
      </div>
      <div class="flex-1 pb-2 text-[0.65rem] text-gray-400 leading-tight">
        理論値（設定1→6）<br>${item.denominators.map(d => '1/' + d).join(' · ')}
      </div>
    </div>
    <div class="text-[0.65rem] leading-relaxed mb-3 -mt-1">${badge}${baseNote ? '<br>' + baseNote : ''}</div>`;
}

// 終了画面・トロフィーなど、選択肢のうちどれが出たかを回数で入力する
function renderCategoricalInputs(m) {
  const wrap = el('cat-inputs');
  const groups = (m && m.categoricalGroups) || [];

  if (groups.length === 0) { wrap.innerHTML = ''; return; }

  wrap.innerHTML = groups.map((g, gi) => {
    const power = estimateCategoricalPower(g.options);
    const badge = power
      ? `<span style="color:${LEVEL_COLORS[power.level]}" class="font-bold">${esc(power.label)}</span>` +
        `<span class="text-gray-400">／設定1と6を見分ける目安 ${
          Number.isFinite(power.requiredObservations) ? '約' + power.requiredObservations + '回' : '判別不可'}</span>`
      : '';

    const rows = g.options.map((o, oi) => `
      <div class="obs-row">
        <div class="flex-1">
          <label class="label">${esc(o.name)}</label>
          <input type="number" class="input input-sm cat-count" data-group="${gi}" data-option="${oi}" min="0" value="0">
        </div>
        <div class="flex-1 pb-2 text-[0.65rem] text-gray-400 leading-tight">
          ${o.probabilities.map(p => (p * 100).toFixed(0) + '%').join(' · ')}
        </div>
      </div>`).join('');

    return `
      <div class="mt-3">
        <div class="text-xs font-bold text-emerald-600 mb-1.5">最も効く要素（少ない観測で絞り込める）</div>
        <div class="label font-bold text-gray-900">${esc(g.name)}（出た回数）</div>
        <div class="text-[0.65rem] leading-relaxed mb-1.5">${badge}</div>
        ${rows}
      </div>`;
  }).join('');
}

// 確定演出: 該当しない設定を候補から外す（択一）
const CONFIRM_PRESETS = [
  { label: '設定2以上濃厚', flags: [false, true, true, true, true, true] },
  { label: '設定3以上濃厚', flags: [false, false, true, true, true, true] },
  { label: '設定4以上濃厚', flags: [false, false, false, true, true, true] },
  { label: '設定5以上濃厚', flags: [false, false, false, false, true, true] },
  { label: '設定6濃厚',     flags: [false, false, false, false, false, true] }
];

// 示唆演出: 確率を倍率で緩やかに補正する（複数選択可）
const HINT_PRESETS = [
  { label: '奇数設定示唆（弱）', weights: [1.5, 1, 1.5, 1, 1.5, 1] },
  { label: '奇数設定示唆（強）', weights: [3, 1, 3, 1, 3, 1] },
  { label: '偶数設定示唆（弱）', weights: [1, 1.5, 1, 1.5, 1, 1.5] },
  { label: '偶数設定示唆（強）', weights: [1, 3, 1, 3, 1, 3] },
  { label: '高設定示唆（弱）',   weights: [1, 1, 1.3, 1.8, 2.2, 2.5] },
  { label: '高設定示唆（強）',   weights: [1, 1, 1.5, 3, 4, 5] }
];

function confirmChipHtml(flags, label, title) {
  return `<span class="chip chip-toggle" data-flags="${esc(JSON.stringify(flags))}"${
    title ? ` title="${esc(title)}"` : ''}>${label}</span>`;
}

function renderConfirmChips(machine) {
  const box = el('confirm-chips');
  const own = (machine && Array.isArray(machine.confirmations)) ? machine.confirmations : [];

  const generic = `
    <div class="w-full">
      <div class="text-xs text-gray-500 mb-1">汎用</div>
      <div class="flex flex-wrap gap-1.5">
        ${CONFIRM_PRESETS.map(c => confirmChipHtml(c.flags, esc(c.label))).join('')}
      </div>
    </div>`;

  // 機種固有の演出があればそちらを先に出す。無ければ汎用のプリセットだけ。
  if (own.length > 0) {
    const byGroup = {};
    own.forEach(c => { (byGroup[c.group] = byGroup[c.group] || []).push(c); });

    box.innerHTML = Object.keys(byGroup).map(group => `
      <div class="w-full mb-1.5">
        <div class="text-xs text-gray-500 mb-1">${esc(group)}</div>
        <div class="flex flex-wrap gap-1.5">
          ${byGroup[group].map(c => {
            const remain = c.flags.map((f, i) => f ? (i + 1) : null).filter(Boolean).join('・');
            return confirmChipHtml(
              c.flags,
              `${esc(c.name)}<span class="opacity-70">（${remain}）</span>`,
              `設定${remain} が残ります`
            );
          }).join('')}
        </div>
      </div>`).join('') + generic;
  } else {
    box.innerHTML = generic;
  }

  // 確定演出は最も強い1つだけを適用する（択一）
  box.querySelectorAll('.chip-toggle').forEach(chip => {
    chip.addEventListener('click', () => {
      const wasSelected = chip.classList.contains('selected');
      box.querySelectorAll('.chip-toggle').forEach(c => c.classList.remove('selected'));
      if (!wasSelected) chip.classList.add('selected');
    });
  });
}

function renderHintChips() {
  const box = el('hint-chips');
  box.innerHTML = HINT_PRESETS.map(c =>
    `<span class="chip chip-toggle" data-weights="${esc(JSON.stringify(c.weights))}">${esc(c.label)}</span>`
  ).join('');
  // 示唆演出は同時に複数発生しうるため複数選択を許可する
  box.querySelectorAll('.chip-toggle').forEach(chip => {
    chip.addEventListener('click', () => chip.classList.toggle('selected'));
  });
}

function selectedConfirmationFlags() {
  const sel = document.querySelector('#confirm-chips .chip-toggle.selected');
  if (!sel) return null;
  try {
    return JSON.parse(sel.dataset.flags);
  } catch (e) {
    return null;
  }
}

// 選んだ確定演出に、機種の非搭載設定（設定1が無い機種など）の除外を重ねる。
// 非搭載の設定は演出を見る前から候補外なので、確定演出と同じ仕組みで常に外しておく。
function getConfirmations() {
  const selected = selectedConfirmationFlags();
  const m = selectedMachine();
  const available = (m && Array.isArray(m.availableSettings) && m.availableSettings.length === 6)
    ? m.availableSettings : null;
  if (!available || available.every(Boolean)) return selected;
  return available.map((ok, i) => ok && (!selected || !!selected[i]));
}

// 選択された示唆演出の倍率を掛け合わせ、事前確率を補正する
function applyHintWeights(prior) {
  const chips = [...document.querySelectorAll('#hint-chips .chip-toggle.selected')];
  if (chips.length === 0) return prior;

  let weighted = prior.slice();
  chips.forEach(chip => {
    let w;
    try { w = JSON.parse(chip.dataset.weights); } catch (e) { return; }
    if (!Array.isArray(w) || w.length !== 6) return;
    weighted = weighted.map((v, i) => v * w[i]);
  });

  const sum = weighted.reduce((a, b) => a + b, 0);
  return sum > 0 ? weighted.map(v => v / sum) : prior;
}


/* ============================================================
 * 判別の実行
 * ========================================================== */

function calcSettingBayes() {
  const m = selectedMachine();
  const games = numVal('st-games', NaN);

  if (!m) { alert('機種を選択してください。'); return; }
  if (!(games > 0)) { alert('総回転数は0より大きい数値で入力してください。'); return; }

  const normalGames = numVal('st-normal-games', 0);
  if (normalGames > games) {
    alert('通常時のゲーム数が総回転数を超えています。入力を確認してください。');
    return;
  }

  const observations = [];
  const summaries = [];
  const weakItems = [];
  let usedNormalFallback = false;

  document.querySelectorAll('.obs-count').forEach(input => {
    const idx = parseInt(input.dataset.idx, 10);
    const item = m.judgeItems[idx];
    const k = parseFloat(input.value);
    const isRatio = denomBaseOf(item) === 'custom';

    input.classList.remove('invalid');
    if (!Number.isFinite(k) || k < 0) return;

    // 要素ごとに指定された母数を使う。通常時が未入力なら総回転数で代用し、後で警告する。
    let n = denominatorGamesFor(item, games, normalGames);
    if (denomBaseOf(item) === 'normal' && !(normalGames > 0) && k > 0) {
      usedNormalFallback = true;
    }

    if (isRatio) {
      const trialsInput = document.querySelector(`.obs-trials[data-idx="${idx}"]`);
      n = trialsInput ? parseFloat(trialsInput.value) : NaN;
      if (trialsInput) trialsInput.classList.remove('invalid');

      if (!Number.isFinite(n) || n <= 0) {
        if (k > 0 && trialsInput) {
          trialsInput.classList.add('invalid');
          summaries.push(`<span class="text-red-600">${esc(item.name)}: 試行回数が未入力のため除外しました</span>`);
        }
        return;
      }
    }

    if (k === 0) return;

    if (k > n) {
      input.classList.add('invalid');
      summaries.push(`<span class="text-red-600">${esc(item.name)}: ${k}回 は${
        isRatio ? `試行回数 ${n}回` : `総回転数 ${games}G`}を超えているため除外しました</span>`);
      return;
    }

    const power = estimateDiscriminationPower(item.denominators);
    if (power && (!Number.isFinite(power.requiredGames) || n < power.requiredGames)) {
      weakItems.push(esc(item.name));
    }

    observations.push({ n: n, k: k, p: item.denominators.map(d => 1 / d), label: item.name });
    const baseLabel = isRatio ? '回' : (denomBaseOf(item) === 'normal' ? 'G(通常時)' : 'G');
    summaries.push(`${esc(item.name)}: ${k}回 / ${n}${baseLabel} = <strong>1/${(n / k).toFixed(2)}</strong>` +
      `（理論 1/${item.denominators[0]}〜1/${item.denominators[5]}）`);
  });

  // 選択肢型（終了画面など）の観測を集める
  (m.categoricalGroups || []).forEach((g, gi) => {
    const counts = g.options.map((o, oi) => {
      const node = document.querySelector(`.cat-count[data-group="${gi}"][data-option="${oi}"]`);
      const v = node ? parseFloat(node.value) : 0;
      return Number.isFinite(v) && v > 0 ? v : 0;
    });

    const total = counts.reduce((a, b) => a + b, 0);
    if (total === 0) return;

    observations.push({
      kind: 'categorical',
      counts: counts,
      optionProbs: g.options.map(o => o.probabilities),
      label: g.name
    });

    const detail = g.options
      .map((o, oi) => counts[oi] > 0 ? `${esc(o.name)} ${counts[oi]}回` : null)
      .filter(Boolean).join(' / ');
    summaries.push(`${esc(g.name)}: ${detail}（計${total}回）`);

    const power = estimateCategoricalPower(g.options);
    if (power && Number.isFinite(power.requiredObservations) && total < power.requiredObservations) {
      weakItems.push(esc(g.name));
    }
  });

  const confirmations = getConfirmations();
  const confirmChip = document.querySelector('#confirm-chips .chip-toggle.selected');
  if (confirmChip) summaries.push(`確定演出: <strong>${esc(confirmChip.innerText)}</strong>`);

  const hintChips = [...document.querySelectorAll('#hint-chips .chip-toggle.selected')];
  if (hintChips.length > 0) {
    summaries.push(`示唆演出: <strong>${hintChips.map(c => esc(c.innerText)).join(' / ')}</strong>`);
  }

  // 非搭載設定の除外だけでは何も観測していないので、選んだ確定演出の有無で見る
  if (observations.length === 0 && !confirmChip && hintChips.length === 0) {
    alert('判別要素の回数を1つ以上入力するか、確定演出・示唆演出を選択してください。');
    return;
  }

  if (usedNormalFallback) {
    summaries.push('<span class="text-red-600">※ 「通常時のみ」を母数とする要素があるのに通常時ゲーム数が未入力です。' +
      '総回転数で代用したため、AT消化が長いほど設定を過小評価します。</span>');
  }

  // 収束に足りていない要素は結果がノイズに振られやすいので明示する
  if (weakItems.length > 0) {
    summaries.push(`<span class="text-amber-600">※ ${weakItems.join('、')} は現在の回転数では収束していません。` +
      `結果が振れやすい点にご注意ください。</span>`);
  }

  const prior = applyHintWeights(buildPrior());
  const posteriors = calculateBayesianPosteriors(prior, observations, confirmations);

  if (posteriors.every(p => p === 0)) {
    alert('入力された条件では、すべての設定が否定されました（確定演出と事前確率が矛盾しています）。条件を見直してください。');
    return;
  }

  const rates = calculateShopAdjustedHourlyRates(m.payouts || FALLBACK_PAYOUTS, getShopConfig());
  const decision = evaluateDecisionAlert(posteriors, rates);

  renderResult(m, prior, posteriors, observations, rates, decision, summaries, games, normalGames);
  lastResult = { machine: m.name, posteriors, decision, totalGames: games };
}

function renderResult(m, prior, posteriors, observations, rates, decision, summaries, games, normalGames) {
  el('st-result-box').classList.remove('hidden');

  const payoutNote = m.payouts ? '' :
    '<br><span class="text-amber-600">※ この機種は機械割が未登録のため、時給は一般的な目安値で計算しています。</span>';

  el('st-obs-summary').innerHTML = summaries.join('<br>') +
    `<br><span class="text-gray-400">事前確率: ${esc(el('prior-mode').selectedOptions[0].text)}</span>` +
    payoutNote;

  el('setting-bars').innerHTML = settingBarsHtml(
    posteriors,
    i => (i >= 4 ? '#059669' : (i >= 2 ? '#d97706' : '#dc2626')),
    i => `<span class="w-16 shrink-0 text-right text-xs font-mono" style="color:${rates[i] >= 0 ? '#059669' : '#dc2626'}">` +
         `${rates[i] >= 0 ? '+' : ''}${rates[i].toLocaleString()}</span>`
  );

  el('st-high-prob').innerText = decision.highSettingProb + '%';
  const hourlyEl = el('st-hourly');
  hourlyEl.innerText = (decision.expectedHourlyRate >= 0 ? '+' : '') + decision.expectedHourlyRate.toLocaleString() + '円';
  hourlyEl.style.color = decision.expectedHourlyRate >= 0 ? '#059669' : '#dc2626';

  const alertBox = el('st-alert');
  alertBox.className = 'mt-3 rounded-lg border p-3 text-sm ' + {
    success: 'bg-emerald-50 border-emerald-200 text-emerald-800',
    warning: 'bg-amber-50 border-amber-200 text-amber-800',
    danger: 'bg-red-50 border-red-200 text-red-800'
  }[decision.tone];
  el('st-alert-title').innerText = decision.title;
  el('st-alert-msg').innerText = decision.message;

  renderPrecisionAnalysis(m, posteriors, prior, observations);
}

/**
 * 何をあと何回観測すれば絞り込めるかを提示する。
 * 期待情報利得が大きい要素ほど、1回の観測で分布が動く。
 */
function renderPrecisionAnalysis(m, posteriors, prior, observations) {
  const target = el('st-precision');
  const post = posteriors.map(p => p / 100);
  const H = entropyBits(post);
  const maxH = Math.log2(6);           // 6択で完全に未知のときのエントロピー
  const narrowed = (1 - H / maxH) * 100;

  const candidates = [];

  (m.categoricalGroups || []).forEach(g => {
    const gain = expectedGainCategorical(post, g.options.map(o => o.probabilities));
    candidates.push({ name: g.name, unit: '1回', gain });
  });

  (m.judgeItems || []).forEach(item => {
    if (denomBaseOf(item) === 'custom') return;   // 母数が別なので単純比較できない
    const gain = expectedGainBinomial(post, item.denominators.map(d => 1 / d), 1000);
    candidates.push({ name: item.name, unit: '1,000G', gain });
  });

  if (candidates.length === 0) { target.innerHTML = ''; return; }

  candidates.sort((a, b) => b.gain - a.gain);
  const best = candidates[0];

  // 目標: 最有力設定の確率が80%に達する状態。必要な情報量から観測回数を概算する。
  const targetEntropy = entropyBits(normalizeDist([0.8, 0.04, 0.04, 0.04, 0.04, 0.04]));
  const needBits = Math.max(0, H - targetEntropy);
  const needCount = best.gain > 0 ? Math.ceil(needBits / best.gain) : Infinity;

  const rows = candidates.map(c => {
    const share = best.gain > 0 ? (c.gain / best.gain) * 100 : 0;
    return `<div class="flex items-center gap-2 py-0.5">
      <span class="flex-1 truncate text-xs text-gray-500">${esc(c.name)}</span>
      <span class="w-14 shrink-0 text-right text-[0.65rem] text-gray-400">${esc(c.unit)}</span>
      <div class="bar-bg" style="max-width:80px;"><div class="bar" style="width:${share.toFixed(0)}%; background:#18181b;"></div></div>
      <span class="w-16 shrink-0 text-right text-[0.65rem] font-mono">${c.gain.toFixed(3)} bit</span>
    </div>`;
  }).join('');

  target.innerHTML =
    '<div class="text-sm font-bold mb-1.5">📈 精度を上げるには</div>' +
    `<div class="text-xs text-gray-500 mb-2">絞り込み度 <strong class="text-gray-900">${narrowed.toFixed(1)}%</strong>` +
    `（残り不確実性 ${H.toFixed(2)} / ${maxH.toFixed(2)} bit）</div>` +
    '<div class="text-[0.65rem] text-gray-400 mb-1">1回あたりの情報量が多い順</div>' +
    rows +
    (Number.isFinite(needCount) && needCount > 0
      ? `<div class="text-xs mt-2 text-amber-600">→ <strong>${esc(best.name)}</strong> をあと約 ${needCount.toLocaleString()} ` +
        `${best.unit === '1,000G' ? '×1,000G' : '回'} 観測すると、最有力設定の確率が80%程度まで上がる見込みです。</div>`
      : '<div class="text-xs mt-2 text-emerald-600">→ すでに十分絞り込めています。</div>') +
    renderContributionRows(prior, observations);
}

/**
 * 各観測を1つ外したときに結果がどれだけ変わるかで、寄与の大きさを見る。
 */
function renderContributionRows(prior, observations) {
  if (observations.length < 2) return '';

  const confirmations = getConfirmations();
  const full = calculateBayesianPosteriors(prior, observations, confirmations);
  const fullHigh = full.slice(3).reduce((a, b) => a + b, 0);

  const rows = observations.map((obs, i) => {
    const without = observations.filter((_, j) => j !== i);
    const p = calculateBayesianPosteriors(prior, without, confirmations);
    return { label: obs.label || `観測${i + 1}`, diff: fullHigh - p.slice(3).reduce((a, b) => a + b, 0) };
  });

  const maxAbs = Math.max(...rows.map(r => Math.abs(r.diff)), 1);
  const body = rows.map(r => {
    const color = r.diff >= 0 ? '#059669' : '#dc2626';
    return `<div class="flex items-center gap-2 py-0.5">
      <span class="flex-1 truncate text-xs text-gray-500">${esc(r.label)}</span>
      <div class="bar-bg" style="max-width:80px;"><div class="bar" style="width:${(Math.abs(r.diff) / maxAbs * 100).toFixed(0)}%; background:${color};"></div></div>
      <span class="w-16 shrink-0 text-right text-[0.65rem] font-mono" style="color:${color}">${r.diff >= 0 ? '+' : ''}${r.diff.toFixed(1)}pt</span>
    </div>`;
  }).join('');

  return '<div class="text-[0.65rem] text-gray-400 mt-3 mb-1">各観測が「設定4以上」の判定に与えている影響</div>' + body;
}


/* ============================================================
 * 判別ログ（この台の前回記録として引き継ぐためのもの）
 * ========================================================== */

function saveJudgeLog() {
  if (!lastResult) { alert('先に設定推測を実行してください。'); return; }

  const store = (el('st-save-store').value || '').trim();
  const unit = (el('st-save-unit').value || '').trim();

  state.logs.unshift({
    id: 'x' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7),
    date: todayString(),
    store: store || '(店舗未入力)',
    unitNumber: unit || null,
    model: lastResult.machine,
    // 次回この台を打つときに事前確率として引き継ぐため、推定結果ごと残す
    posteriors: lastResult.posteriors,
    totalGames: lastResult.totalGames,
    note: `設定4以上 ${lastResult.decision.highSettingProb}% ／ 推定時給 ${lastResult.decision.expectedHourlyRate.toLocaleString()}円/h ／ ${lastResult.decision.status}`
  });
  saveState();
  renderJudgeLogs();

  alert(store && unit
    ? `推測結果を保存しました。\n次回この台（${store} ${unit}番）を打つときに「この台の前回記録から」で引き継げます。`
    : '推測結果を保存しました。\n店舗名と台番号を入れて保存すると、次回この台の事前確率として引き継げます。');
}

function renderJudgeLogs() {
  const box = el('judge-log-list');
  if (!box) return;

  if (state.logs.length === 0) {
    box.innerHTML = '<div class="hint">まだ保存された判別結果はありません。</div>';
    return;
  }

  box.innerHTML = state.logs.slice(0, 20).map(l => {
    const top = l.posteriors.indexOf(Math.max(...l.posteriors)) + 1;
    return `<div class="panel flex items-start justify-between gap-3">
      <div class="min-w-0">
        <div class="text-sm font-bold truncate">${esc(l.model)}</div>
        <div class="text-xs text-gray-500">${esc(l.date)}／${esc(l.store)}${l.unitNumber ? ' ' + esc(l.unitNumber) + '番' : ''}${l.totalGames ? '／' + l.totalGames.toLocaleString() + 'G' : ''}</div>
        <div class="text-xs text-gray-400 mt-1">最有力 設定${top}／${esc(l.note || '')}</div>
      </div>
      <button type="button" class="btn btn-ghost btn-sm shrink-0" data-delete-log="${esc(l.id)}">削除</button>
    </div>`;
  }).join('');

  box.querySelectorAll('[data-delete-log]').forEach(btn => {
    btn.addEventListener('click', () => {
      state.logs = state.logs.filter(l => l.id !== btn.dataset.deleteLog);
      saveState();
      renderJudgeLogs();
      renderPriorPreview();
    });
  });
}


/* ============================================================
 * 初期化
 * ========================================================== */

function initJudgePage() {
  // 判別スペックが1件も無いときは案内だけのページになり、入力欄そのものが存在しない
  if (!el('st-model-select')) return;

  loadState();
  renderShopConfigInputs();
  renderMorningSignalChips('morning-signals-count');
  renderMorningSignalChips('morning-signals-last');

  // 機種を切り替えると、判別要素も前日入力欄も事前確率も全部作り直す必要がある
  el('st-model-select').addEventListener('change', () => {
    renderObservationInputs();
    renderPrevCountInputs();
    renderPriorPreview();
  });

  el('prior-mode').addEventListener('change', onPriorModeChange);
  el('morning-obs').addEventListener('change', renderPriorPreview);
  el('prevcount-games').addEventListener('change', renderPriorPreview);
  el('prior-store').addEventListener('input', renderPriorPreview);
  el('prior-unit').addEventListener('input', renderPriorPreview);
  document.querySelectorAll('.prior-shop-input, .prior-prev-input').forEach(input => {
    input.addEventListener('change', renderPriorPreview);
  });

  el('st-games').addEventListener('change', renderObservationInputs);
  el('st-normal-games').addEventListener('change', renderObservationInputs);
  el('shop-rental').addEventListener('change', onShopConfigChange);
  el('shop-exchange').addEventListener('change', onShopConfigChange);
  el('shop-gph').addEventListener('change', onShopConfigChange);
  el('shop-holding').addEventListener('input', onHoldingInput);
  el('shop-holding').addEventListener('change', onShopConfigChange);

  el('st-run').addEventListener('click', calcSettingBayes);
  el('st-save').addEventListener('click', saveJudgeLog);

  onPriorModeChange();
  renderObservationInputs();
  renderPrevCountInputs();
  renderPriorPreview();
  renderJudgeLogs();
}

document.addEventListener('DOMContentLoaded', initJudgePage);
