/*
 * 設定判別の計算だけを集めたファイル。DOMには一切触らない。
 *
 * 【なぜブラウザ側で計算するのか】
 * 判別は「小役を1つ入れるたびに事後確率がどう動くか」を見ながら使うもので、
 * 1回ごとにサーバーへ往復すると打ちながら使えない。計算自体は軽いので全部手元でやる。
 *
 * 【なぜ画面の描画と分けているのか】
 * 数値の正しさが結果に直結する部分なので、表示の都合で書き換えられないよう隔離している。
 * この中の関数は入力を受けて数値を返すだけで、色やHTMLは judge.js 側の責任。
 */

const NUM_SETTINGS = 6;

// ホールの設定移行マトリクス（行: 前日設定, 列: 本日設定 / 各行の合計=1.0）
// 過去ログからの自動学習は、学習の入力になる「前日の設定」自体が推定値でノイズを含むため
// 現実的なサンプル数では収束しない。既定値をそのまま使う方が正確になる。
const DEFAULT_TRANSITION_MATRIX = [
  [0.70, 0.15, 0.05, 0.05, 0.03, 0.02],
  [0.20, 0.60, 0.10, 0.05, 0.03, 0.02],
  [0.30, 0.30, 0.30, 0.05, 0.03, 0.02],
  [0.40, 0.30, 0.10, 0.15, 0.03, 0.02],
  [0.50, 0.30, 0.00, 0.00, 0.20, 0.00],
  [0.60, 0.30, 0.00, 0.00, 0.00, 0.10]
];

/**
 * 前日データと朝一挙動から本日の事前確率を生成する。
 * 同時分布 P(前日=j, 本日=i) を保持することで、据え置き観測を正しくベイズ更新できる。
 */
function generateTodayPriorWithMorning(prevDayPosteriors, transitionMatrix, morningObservation = 'UNKNOWN') {
  const normalizedPrev = prevDayPosteriors.map(p => p / 100);

  const jointMatrix = Array.from({ length: NUM_SETTINGS }, () => new Array(NUM_SETTINGS).fill(0));
  for (let j = 0; j < NUM_SETTINGS; j++) {
    for (let i = 0; i < NUM_SETTINGS; i++) {
      jointMatrix[j][i] = normalizedPrev[j] * transitionMatrix[j][i];
    }
  }

  const todayPrior = new Array(NUM_SETTINGS).fill(0);

  if (morningObservation === 'STAY') {
    // 据え置き濃厚: 前日=j かつ 本日=j (i===j) の要素のみを残す
    for (let i = 0; i < NUM_SETTINGS; i++) {
      todayPrior[i] = jointMatrix[i][i];
    }
  } else if (morningObservation === 'RESET') {
    // リセット濃厚: 前日≠本日の要素 (i!==j) を集計
    for (let i = 0; i < NUM_SETTINGS; i++) {
      for (let j = 0; j < NUM_SETTINGS; j++) {
        if (i !== j) todayPrior[i] += jointMatrix[j][i];
      }
    }
  } else {
    for (let i = 0; i < NUM_SETTINGS; i++) {
      for (let j = 0; j < NUM_SETTINGS; j++) {
        todayPrior[i] += jointMatrix[j][i];
      }
    }
  }

  const total = todayPrior.reduce((a, b) => a + b, 0);
  return total === 0 ? new Array(NUM_SETTINGS).fill(1 / NUM_SETTINGS) : todayPrior.map(v => v / total);
}

/**
 * 多要素ベイズ推定の本体。
 * @param {Array<number>} prior 事前確率 [設定1..設定6]（和が1）
 * @param {Array<Object>} observations 二項観測 {n, k, p[6]} または多項観測 {kind:'categorical', counts, optionProbs}
 * @param {Array<boolean>} confirmations 確定演出フラグ（falseの設定を候補から外す）
 * @returns {Array<number>} 事後確率 [%] x6
 */
function calculateBayesianPosteriors(prior, observations, confirmations = null) {
  if (!Array.isArray(prior) || prior.length !== NUM_SETTINGS) return new Array(NUM_SETTINGS).fill(0);

  // 数万Gの観測でも尤度が桁落ちしないよう、確率そのものではなく対数で持ち回る
  const logPosteriors = new Array(NUM_SETTINGS).fill(0);

  for (let s = 0; s < NUM_SETTINGS; s++) {
    if ((confirmations && !confirmations[s]) || prior[s] <= 0) {
      logPosteriors[s] = -Infinity;
    } else {
      logPosteriors[s] = Math.log(prior[s]);
    }
  }

  for (const obs of observations) {
    // 多項観測（終了画面など、複数の選択肢のうちどれが出たか）
    // 対数尤度: Σ k_j * ln(p_j)  ※出現率は設定ごとに正規化してから使う
    if (obs.kind === 'categorical') {
      const counts = obs.counts;
      const optionProbs = obs.optionProbs;
      if (!Array.isArray(counts) || !Array.isArray(optionProbs)) continue;
      if (counts.length !== optionProbs.length || counts.length < 2) continue;
      if (counts.every(c => !(c > 0))) continue;

      for (let s = 0; s < NUM_SETTINGS; s++) {
        if (logPosteriors[s] === -Infinity) continue;

        const total = optionProbs.reduce((sum, arr) => sum + (arr[s] || 0), 0);
        if (!(total > 0)) { logPosteriors[s] = -Infinity; continue; }

        for (let j = 0; j < counts.length; j++) {
          if (!(counts[j] > 0)) continue;
          const prob = (optionProbs[j][s] || 0) / total;
          if (!(prob > 0)) { logPosteriors[s] = -Infinity; break; }
          logPosteriors[s] += counts[j] * Math.log(prob);
        }
      }
      continue;
    }

    // 二項観測の対数尤度: k*ln(p) + (n-k)*ln(1-p)  ※nCkは設定間で共通のため省略
    const { n, k, p } = obs;
    if (k > n || !Array.isArray(p) || p.length !== NUM_SETTINGS) continue;

    for (let s = 0; s < NUM_SETTINGS; s++) {
      if (logPosteriors[s] === -Infinity) continue;
      const prob = p[s];
      if (prob <= 0 || prob >= 1) {
        logPosteriors[s] = -Infinity;
        continue;
      }
      logPosteriors[s] += k * Math.log(prob) + (n - k) * Math.log(1 - prob);
    }
  }

  // Log-Sum-Expトリックでアンダーフローを回避してから正規化する
  const maxLog = Math.max(...logPosteriors.filter(v => v !== -Infinity));
  if (maxLog === -Infinity) return new Array(NUM_SETTINGS).fill(0);

  const unnormalized = logPosteriors.map(logP => logP === -Infinity ? 0 : Math.exp(logP - maxLog));
  const totalSum = unnormalized.reduce((sum, val) => sum + val, 0);

  return unnormalized.map(val => Number(((val / totalSum) * 100).toFixed(2)));
}

/**
 * 店舗条件・持ち玉比率を加味した設定別の実質時給。
 * 非等価のホールでは現金投資分にギャップ損が乗るため、機械割だけでは判断を誤る。
 */
function calculateShopAdjustedHourlyRates(basePayouts, shopConfig) {
  const {
    rentalRate = 20,
    exchangeRate = 20,
    gamesPerHour = 800,
    holdingRatio = 0.70
  } = shopConfig;

  const cashGapPerMedal = Math.max(0, rentalRate - exchangeRate);
  const cashGapLossPerGame = 3 * (1 - holdingRatio) * cashGapPerMedal;

  const rates = [];
  for (let s = 0; s < NUM_SETTINGS; s++) {
    const medalGainPerGame = 3 * ((basePayouts[s] / 100) - 1);
    const baseValuePerGame = medalGainPerGame * exchangeRate;
    rates.push(Math.round((baseValuePerGame - cashGapLossPerGame) * gamesPerHour));
  }
  return rates;
}

/**
 * 継続・撤退の判断。設定4以上の確率ではなく、店舗条件込みの期待時給で判定する。
 */
function evaluateDecisionAlert(posteriors, hourlyRatesBySetting, thresholdConfig = { target: 2000, minAllowable: 1000 }) {
  let expectedHourlyRate = 0;
  for (let i = 0; i < NUM_SETTINGS; i++) {
    expectedHourlyRate += (posteriors[i] / 100) * hourlyRatesBySetting[i];
  }
  expectedHourlyRate = Math.round(expectedHourlyRate);

  const highSettingProb = Number((posteriors.slice(3).reduce((a, b) => a + b, 0)).toFixed(1));

  const formattedRate = expectedHourlyRate >= 0
    ? `＋${expectedHourlyRate.toLocaleString()}円/h`
    : `－${Math.abs(expectedHourlyRate).toLocaleString()}円/h`;

  let status, tone, title, message;

  if (expectedHourlyRate >= thresholdConfig.target) {
    status = 'CONTINUE';
    tone = 'success';
    title = '【続行】目標時給クリア';
    message = `推定時給は ${formattedRate} です（設定4以上: ${highSettingProb}%）。そのまま継続してください。`;
  } else if (expectedHourlyRate >= thresholdConfig.minAllowable) {
    status = 'CAUTION';
    tone = 'warning';
    title = '【様子見】ボーダー付近';
    message = `推定時給が ${formattedRate} に低下（設定4以上: ${highSettingProb}%）。目標（${thresholdConfig.target.toLocaleString()}円/h）を下回っています。次回大当り等のヤメ時を警戒してください。`;
  } else {
    status = 'QUIT';
    tone = 'danger';
    title = '【ヤメ推奨】期待値割り込み';
    const reasonText = highSettingProb < 30.0
      ? `低設定濃厚（設定4以上: ${highSettingProb}%）`
      : `非等価ギャップ等のため時給効率が不十分（設定4以上: ${highSettingProb}%）`;
    message = `推定時給が ${formattedRate}（許容ライン: ${thresholdConfig.minAllowable.toLocaleString()}円/h未満）です。${reasonText}のため撤退を推奨します。`;
  }

  return { status, tone, title, message, expectedHourlyRate, highSettingProb };
}

/**
 * 判別要素の「効き」を評価する。
 * 設定1と設定6のベルヌーイ分布のKLダイバージェンスから、
 * 両者を見分けられるようになるまでの目安試行数を推定する。
 * （分散を無視した期待値ベースの近似。目安G数での正答率は実測で約9割）
 */
function estimateDiscriminationPower(denominators) {
  if (!Array.isArray(denominators) || denominators.length !== NUM_SETTINGS) return null;

  const p1 = 1 / denominators[0];
  const p6 = 1 / denominators[5];
  if (!(p1 > 0 && p1 < 1 && p6 > 0 && p6 < 1)) return null;

  if (Math.abs(p1 - p6) < 1e-12) {
    return { requiredGames: Infinity, level: 'none', label: '設定差なし' };
  }

  const kl = p6 * Math.log(p6 / p1) + (1 - p6) * Math.log((1 - p6) / (1 - p1));
  if (!(kl > 0)) return { requiredGames: Infinity, level: 'none', label: '判別不可' };

  // オッズ19:1（確度95%）に到達する期待試行数
  const requiredGames = Math.ceil(Math.log(19) / kl);

  // 1回成立したときに設定6と設定1の尤度比がどれだけ動くか。
  // 分母の大きい要素ほどこの値は大きくなるが、そのぶん成立回数自体が少ないため、
  // 1日の稼働で得られる合計の情報量とは別物になる（ボーナスが強く見える理由）。
  const perEventBits = Math.log2(p6 / p1);

  let level, label;
  if (requiredGames <= 3000)       { level = 'strong'; label = '判別力 強'; }
  else if (requiredGames <= 8000)  { level = 'medium'; label = '判別力 中'; }
  else if (requiredGames <= 20000) { level = 'weak';   label = '判別力 弱'; }
  else                             { level = 'none';   label = 'ノイズになりやすい'; }

  return { requiredGames, level, label, perEventBits };
}

/**
 * 選択肢型（多項）要素の判別力。
 * 1回の観測あたりの情報量が大きいため、必要な観測「回数」で示す。
 */
function estimateCategoricalPower(options) {
  if (!Array.isArray(options) || options.length < 2) return null;

  const normAt = (s) => {
    const total = options.reduce((sum, o) => sum + (o.probabilities[s] || 0), 0);
    return total > 0 ? options.map(o => (o.probabilities[s] || 0) / total) : null;
  };

  const p1 = normAt(0), p6 = normAt(5);
  if (!p1 || !p6) return null;

  let kl = 0;
  for (let j = 0; j < options.length; j++) {
    if (p6[j] > 0 && p1[j] > 0) kl += p6[j] * Math.log(p6[j] / p1[j]);
    // 設定1で出ない選択肢が1つでもあれば、1回出た時点で設定1を否定できる
    else if (p6[j] > 0 && p1[j] === 0) return { requiredObservations: 1, level: 'strong', label: '判別力 強' };
  }

  if (!(kl > 0)) return { requiredObservations: Infinity, level: 'none', label: '設定差なし' };

  const requiredObservations = Math.ceil(Math.log(19) / kl);
  let level, label;
  if (requiredObservations <= 5)       { level = 'strong'; label = '判別力 強'; }
  else if (requiredObservations <= 15) { level = 'medium'; label = '判別力 中'; }
  else if (requiredObservations <= 40) { level = 'weak';   label = '判別力 弱'; }
  else                                 { level = 'none';   label = 'ノイズになりやすい'; }

  return { requiredObservations, level, label };
}


/* ---- 朝一挙動から据え置き確率を求める ---- */

// 朝一に観測できる挙動と、その出現しやすさ（据え置き時 / リセット時）。
// 機種によって使える挙動もガックンの有無も変わるため、あくまで汎用の目安。
const MORNING_SIGNALS = [
  { id: 'gakkun',       name: 'ガックンした',           stay: 0.05, reset: 0.85 },
  { id: 'no_gakkun',    name: 'ガックンしなかった',     stay: 0.95, reset: 0.15 },
  { id: 'early_chance', name: '朝一から高確・CZ',       stay: 0.30, reset: 0.70 },
  { id: 'short_ceil',   name: '天井が短縮されていた',   stay: 0.05, reset: 0.95 },
  { id: 'carried',      name: '前日の状態を引き継いだ', stay: 0.90, reset: 0.10 }
];

/**
 * 観測した朝一挙動から、据え置きである確率を求める。
 * 各挙動が独立に起きると仮定した単純なベイズ更新なので、
 * 矛盾する挙動（ガックンした + しなかった）を両方選んでも破綻せず中間に落ち着く。
 */
function stayProbabilityFrom(selectedIds, priorStay = 0.5) {
  if (!Array.isArray(selectedIds) || selectedIds.length === 0) return null;

  let logStay = Math.log(priorStay);
  let logReset = Math.log(1 - priorStay);

  selectedIds.forEach(id => {
    const sig = MORNING_SIGNALS.find(x => x.id === id);
    if (!sig) return;
    logStay += Math.log(sig.stay);
    logReset += Math.log(sig.reset);
  });

  const mx = Math.max(logStay, logReset);
  const es = Math.exp(logStay - mx);
  const er = Math.exp(logReset - mx);
  return es / (es + er);
}

/**
 * 据え置き確率を使って、据え置き時とリセット時の事前確率を混ぜる。
 * 挙動を何も観測していない場合は、移行マトリクスをそのまま適用した分布を使う
 * （据え置き率は行列に織り込まれているため、50:50で混ぜるのとは別物になる）。
 */
function morningBlendedPrior(prevPosteriors, matrix, selectedIds) {
  const stayProb = stayProbabilityFrom(selectedIds);
  if (stayProb === null) {
    return { prior: generateTodayPriorWithMorning(prevPosteriors, matrix, 'UNKNOWN'), stayProb: null };
  }

  const stay = generateTodayPriorWithMorning(prevPosteriors, matrix, 'STAY');
  const reset = generateTodayPriorWithMorning(prevPosteriors, matrix, 'RESET');
  return {
    prior: stay.map((v, i) => v * stayProb + reset[i] * (1 - stayProb)),
    stayProb: stayProb
  };
}


/* ---- 情報量：次に何を観測すべきかを評価する ---- */

// 1日の稼働で観測できるおおよその量。単位の違う要素を同じ土俵で比べるために使う。
const DAILY_GAMES = 6000;
const DAILY_CATEGORICAL_OBSERVATIONS = 8;

function entropyBits(dist) {
  return -dist.reduce((a, v) => a + (v > 0 ? v * Math.log2(v) : 0), 0);
}

function normalizeDist(arr) {
  const s = arr.reduce((a, b) => a + b, 0);
  return s > 0 ? arr.map(v => v / s) : arr.slice();
}

/** 選択肢型を1回観測したときの期待情報利得（bit） */
function expectedGainCategorical(post, optionProbs) {
  const H0 = entropyBits(post);
  let expected = 0;

  for (let j = 0; j < optionProbs.length; j++) {
    let pj = 0;
    const updated = new Array(NUM_SETTINGS).fill(0);

    for (let s = 0; s < NUM_SETTINGS; s++) {
      const total = optionProbs.reduce((a, arr) => a + (arr[s] || 0), 0);
      if (!(total > 0)) continue;
      const p = (optionProbs[j][s] || 0) / total;
      pj += post[s] * p;
      updated[s] = post[s] * p;
    }

    if (pj <= 0) continue;
    expected += pj * entropyBits(normalizeDist(updated));
  }
  return Math.max(0, H0 - expected);
}

/** 二項型をnG（n回）観測したときの期待情報利得（bit）。成立回数ごとに場合分けして期待値を取る。 */
function expectedGainBinomial(post, ps, n) {
  if (!(n > 0)) return 0;
  const H0 = entropyBits(post);

  const maxP = Math.max(...ps);
  const maxK = Math.min(n, Math.max(20, Math.ceil(n * maxP * 3)));

  // 二項係数の対数を漸化式で更新して O(maxK) に抑える
  let logC = 0;
  let expected = 0;

  for (let k = 0; k <= maxK; k++) {
    if (k > 0) logC += Math.log(n - k + 1) - Math.log(k);

    let pk = 0;
    const updated = new Array(NUM_SETTINGS).fill(0);
    for (let s = 0; s < NUM_SETTINGS; s++) {
      if (post[s] <= 0) continue;
      const lp = logC + k * Math.log(ps[s]) + (n - k) * Math.log(1 - ps[s]);
      const p = Math.exp(lp);
      pk += post[s] * p;
      updated[s] = post[s] * p;
    }

    if (pk <= 1e-12) continue;
    expected += pk * entropyBits(normalizeDist(updated));
  }
  return Math.max(0, H0 - expected);
}

/**
 * 判別要素の優先度（bit/日）。事前確率が均等な状態で評価する。
 * 「1回のインパクト」ではなく「1日で得られる合計の情報量」で並べるための指標。
 */
function dailyInformationValue(kind, payload) {
  const flat = new Array(NUM_SETTINGS).fill(1 / NUM_SETTINGS);
  try {
    if (kind === 'categorical') {
      return expectedGainCategorical(flat, payload) * DAILY_CATEGORICAL_OBSERVATIONS;
    }
    return expectedGainBinomial(flat, payload, DAILY_GAMES);
  } catch (e) {
    return 0;
  }
}
