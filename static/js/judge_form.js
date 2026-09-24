/*
 * 判別スペック登録フォームの行の増減。
 *
 * 判別力の目安表示は bayes.js の estimateDiscriminationPower をそのまま呼んでいる。
 * 登録画面と判別画面で別々に実装すると、片方だけ直したときに
 * 「登録時は判別力 強と出たのに判別画面では弱」というずれが起きるため。
 */

const LEVEL_COLORS = {
  strong: '#059669',
  medium: '#2563eb',
  weak: '#d97706',
  none: '#dc2626'
};

// よくある機種構成の雛形。要素名・種別・母数だけを入れる。
// 確率は機種ごとに違うので、ここで埋めると誤登録の元になる。
const JUDGE_TEMPLATES = {
  'a-type': [
    { name: 'ブドウ', type: 'koyaku', base: 'total' },
    { name: 'REG', type: 'bonus', base: 'total' },
    { name: 'BIG', type: 'bonus', base: 'total' }
  ],
  'standard-at': [
    { name: 'AT初当り', type: 'bonus', base: 'normal' },
    { name: 'AT直撃', type: 'bonus', base: 'normal' },
    { name: '弱スイカ', type: 'koyaku', base: 'normal' }
  ],
  'cz-type': [
    { name: 'CZ初当り', type: 'bonus', base: 'normal' },
    { name: 'CZ突破率', type: 'ratio', base: 'custom' }
  ]
};

function settingInputsHtml(namePrefix, extraClass) {
  let html = '';
  for (let i = 1; i <= 6; i++) {
    html += `<div>
      <label class="label text-[0.7rem]">設定${i}</label>
      <input type="number" step="0.01" min="0" class="input input-sm ${extraClass}" name="${namePrefix}${i}[]" value="">
    </div>`;
  }
  return `<div class="grid grid-cols-3 md:grid-cols-6 gap-1.5 mt-2">${html}</div>`;
}


/* ---- 判別要素 ---- */

function judgeRowHtml() {
  return `
    <div class="judge-row panel mb-2">
      <div class="grid grid-cols-1 md:grid-cols-4 gap-2 items-end">
        <div>
          <label class="label text-[0.7rem]">要素名</label>
          <input type="text" name="judge_name[]" class="input input-sm ji-name" value="" placeholder="例: ブドウ">
        </div>
        <div>
          <label class="label text-[0.7rem]">種別</label>
          <select name="judge_type[]" class="input input-sm ji-type">
            <option value="koyaku">カウント小役（毎G試行）</option>
            <option value="bonus">初当り・ボーナス（毎G試行）</option>
            <option value="ratio">成功率（母数が別）</option>
          </select>
        </div>
        <div>
          <label class="label text-[0.7rem]">確率を割る母数</label>
          <select name="judge_base[]" class="input input-sm ji-base">
            <option value="total">総回転数</option>
            <option value="normal">通常時のみ（AT消化を除く）</option>
            <option value="custom">試行回数を個別入力</option>
          </select>
        </div>
        <button type="button" class="btn btn-sm btn-ghost text-red-600 judge-remove">削除</button>
      </div>
      ${settingInputsHtml('judge_denom', 'ji-denom')}
      <div class="ji-power text-[0.65rem] leading-relaxed mt-1.5"></div>
    </div>`;
}

function updateJudgePowerHint(node) {
  const row = node && node.closest('.judge-row');
  if (!row) return;
  const target = row.querySelector('.ji-power');
  if (!target) return;

  const denominators = [...row.querySelectorAll('.ji-denom')].map(x => parseFloat(x.value));
  if (denominators.some(d => !Number.isFinite(d) || d <= 1)) {
    target.innerHTML = '<span class="text-gray-400">設定1〜6の確率を入力すると判別力を表示します</span>';
    return;
  }

  const power = estimateDiscriminationPower(denominators);
  if (!power) { target.innerHTML = ''; return; }

  // 「登録はできたが1日では収束しない」要素にその場で気づけるようにする
  const advice = {
    none: ' — 1日では収束しません。判別要素から外すことを検討してください。',
    weak: ' — 補助的な扱いに留めてください。',
    strong: ' — 軸として有効です。'
  }[power.level] || '';

  target.innerHTML =
    `<span class="font-bold" style="color:${LEVEL_COLORS[power.level]}">${power.label}</span>` +
    `<span class="text-gray-400">／設定1と6を見分ける目安 ` +
    `${Number.isFinite(power.requiredGames) ? '約' + power.requiredGames.toLocaleString() + '回' : '判別不可'}${advice}</span>` +
    (Number.isFinite(power.perEventBits)
      ? `<br><span class="text-gray-400">1回の成立で ${power.perEventBits.toFixed(2)} bit 動きます</span>`
      : '');
}


/* ---- 選択肢型（終了画面・トロフィー） ---- */

function catOptionHtml(groupIndex) {
  return `
    <div class="cat-option panel bg-white mb-2">
      <input type="hidden" name="cat_group_idx[]" value="${groupIndex}" class="cat-idx">
      <div class="flex gap-2 items-end">
        <div class="flex-1">
          <label class="label text-[0.7rem]">選択肢名</label>
          <input type="text" name="cat_opt_name[]" class="input input-sm" value="" placeholder="例: カサンドラ">
        </div>
        <button type="button" class="btn btn-sm btn-ghost text-red-600 cat-option-remove">削除</button>
      </div>
      ${settingInputsHtml('cat_p', '')}
    </div>`;
}

function catGroupHtml(groupIndex) {
  return `
    <div class="cat-group panel mb-2" data-group-index="${groupIndex}">
      <div class="flex gap-2 items-end mb-2">
        <div class="flex-1">
          <label class="label text-[0.7rem]">判別グループ名</label>
          <input type="text" name="cat_group_name[]" class="input input-sm" value="" placeholder="例: AT終了画面">
        </div>
        <button type="button" class="btn btn-sm btn-ghost text-red-600 cat-group-remove">グループを削除</button>
      </div>
      <div class="cat-options">${catOptionHtml(groupIndex)}${catOptionHtml(groupIndex)}</div>
      <button type="button" class="btn btn-sm btn-ghost cat-option-add">＋ 選択肢を追加</button>
    </div>`;
}

// グループを削除すると添字がずれるため、対応付けを毎回振り直す
function reindexCatGroups() {
  document.querySelectorAll('#cat-groups .cat-group').forEach((group, gi) => {
    group.dataset.groupIndex = gi;
    group.querySelectorAll('.cat-idx').forEach(input => { input.value = gi; });
  });
}


/* ---- 確定・否定演出 ---- */

function confRowHtml(index) {
  let boxes = '';
  for (let i = 1; i <= 6; i++) {
    boxes += `<label class="inline-flex items-center gap-1 text-xs">
      <input type="checkbox" name="conf_s${i}[]" value="${index}"><span>設定${i}</span></label>`;
  }
  return `
    <div class="conf-row panel mb-2">
      <div class="grid grid-cols-1 md:grid-cols-3 gap-2 items-end">
        <div>
          <label class="label text-[0.7rem]">グループ</label>
          <input type="text" name="conf_group[]" class="input input-sm" value="" placeholder="例: ST終了画面">
        </div>
        <div>
          <label class="label text-[0.7rem]">名称</label>
          <input type="text" name="conf_name[]" class="input input-sm" value="" placeholder="例: 極">
        </div>
        <button type="button" class="btn btn-sm btn-ghost text-red-600 conf-remove">削除</button>
      </div>
      <label class="label text-[0.7rem] mt-2">候補として残る設定</label>
      <div class="flex flex-wrap gap-3">${boxes}</div>
    </div>`;
}

// 行を消すと添字がずれるので、チェックボックスのvalue（＝何行目か）を振り直す
function reindexConfRows() {
  document.querySelectorAll('#conf-items .conf-row').forEach((row, idx) => {
    row.querySelectorAll('input[type="checkbox"]').forEach(cb => { cb.value = idx; });
  });
}


/* ---- 初期化 ---- */

document.addEventListener('DOMContentLoaded', function () {
  const judgeItems = document.getElementById('judge-items');
  const catGroups = document.getElementById('cat-groups');
  const confItems = document.getElementById('conf-items');
  if (!judgeItems) return;

  // 確率を入力したらその場で判別力を更新する
  judgeItems.addEventListener('input', function (e) {
    if (e.target.classList.contains('ji-denom')) updateJudgePowerHint(e.target);
  });
  judgeItems.addEventListener('click', function (e) {
    if (e.target.classList.contains('judge-remove')) e.target.closest('.judge-row').remove();
  });
  document.getElementById('judge-add').addEventListener('click', function () {
    judgeItems.insertAdjacentHTML('beforeend', judgeRowHtml());
  });

  // 既存行の判別力を初期表示する
  judgeItems.querySelectorAll('.judge-row').forEach(row => {
    updateJudgePowerHint(row.querySelector('.ji-denom'));
  });

  document.querySelectorAll('[data-template]').forEach(btn => {
    btn.addEventListener('click', function () {
      (JUDGE_TEMPLATES[btn.dataset.template] || []).forEach(row => {
        judgeItems.insertAdjacentHTML('beforeend', judgeRowHtml());
        const added = judgeItems.querySelector('.judge-row:last-child');
        added.querySelector('.ji-name').value = row.name;
        added.querySelector('.ji-type').value = row.type;
        added.querySelector('.ji-base').value = row.base;
        updateJudgePowerHint(added.querySelector('.ji-denom'));
      });
    });
  });

  document.getElementById('cat-group-add').addEventListener('click', function () {
    catGroups.insertAdjacentHTML('beforeend', catGroupHtml(catGroups.querySelectorAll('.cat-group').length));
    reindexCatGroups();
  });

  catGroups.addEventListener('click', function (e) {
    if (e.target.classList.contains('cat-group-remove')) {
      e.target.closest('.cat-group').remove();
      reindexCatGroups();
    } else if (e.target.classList.contains('cat-option-remove')) {
      e.target.closest('.cat-option').remove();
      reindexCatGroups();
    } else if (e.target.classList.contains('cat-option-add')) {
      const group = e.target.closest('.cat-group');
      group.querySelector('.cat-options')
        .insertAdjacentHTML('beforeend', catOptionHtml(group.dataset.groupIndex));
      reindexCatGroups();
    }
  });

  document.getElementById('conf-add').addEventListener('click', function () {
    confItems.insertAdjacentHTML('beforeend', confRowHtml(confItems.querySelectorAll('.conf-row').length));
    reindexConfRows();
  });

  confItems.addEventListener('click', function (e) {
    if (e.target.classList.contains('conf-remove')) {
      e.target.closest('.conf-row').remove();
      reindexConfRows();
    }
  });

  reindexCatGroups();
  reindexConfRows();
});
