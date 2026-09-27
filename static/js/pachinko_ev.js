/*
 * パチンコの回転率・ボーダー・期待値の計算だけを集めたファイル。DOMには一切触らない。
 *
 * 回転率計算(/pachinko/calc)と、AI分析のパチンコ期待値・振り返り(/pachinko/review)の両方が使う。
 * 同じ式を2つの画面に書くと片方だけ直して数字が食い違うので、ここに1つだけ置く。
 * サーバー側(common.py の「パチンコの稼働記録と振り返り」)にも同じ式があるが、
 * あちらはAIに渡す数字を保存するためのもので、画面の表示はこのファイルが正。
 *
 * 【期待値の考え方】
 * 交換率Xでのボーダー B_X は「1,000円で B_X 回まわせば収支がトントン」になる回転率。
 * 当たりから戻ってくる出玉の価値は回転率に関係なく1回転あたり一定なので、
 *   1回転あたりの戻り(円) = 1000 / B_X
 *   1回転あたりのコスト(円) = 1000 / R            (現金で打つとき。R は実際の回転率)
 *                          = 1000 / R × 貸玉/X    (持ち玉で打つとき。玉を交換率の値段で使っている)
 * の差を1回転あたりの期待値とする。持ち玉は換金ギャップがかからないぶん、非等価ほど得になる。
 * 実際には大当り中の止め打ちや出玉の削りで上下するが、打つか・続けるかを決めるには足りる。
 */
(function (root) {
  'use strict';

  const DEFAULT_LEND = 250; // 4円パチンコの1,000円あたりの貸玉数

  function num(value) {
    const n = parseFloat(value);
    return isFinite(n) ? n : 0;
  }

  /*
   * 交換率(1,000円分を何玉で交換するか)でのボーダーを、登録済みの点を直線で結んで概算する。
   * 範囲の外は端の2点の傾きで伸ばす。実際は厳密な直線ではないが、店ごとの細かい差を見るには足りる。
   * borderPoints は [{key: 'border_equiv', balls: 250}, ...](pachinko_info.BORDER_POINTS)。
   */
  function borderAt(machine, balls, borderPoints) {
    const points = borderPoints
      .map(function (p) { return [p.balls, machine[p.key]]; })
      .filter(function (p) { return p[1]; });
    if (!points.length) return null;
    if (points.length === 1) return points[0][1];
    let i = 0;
    while (i < points.length - 2 && balls > points[i + 1][0]) i++;
    const a = points[i], b = points[i + 1];
    return a[1] + (b[1] - a[1]) * (balls - a[0]) / (b[0] - a[0]);
  }

  /*
   * 1回転あたりの期待値(円)。
   * cashRatio は現金で打った割合(0〜1)。残りは持ち玉。exchangeBalls / lend は1,000円あたりの玉数。
   */
  function evPerSpin(rate, border, exchangeBalls, lend, cashRatio) {
    if (!(rate > 0) || !(border > 0)) return null;
    const cash = cashRatio === undefined ? 1 : Math.min(1, Math.max(0, cashRatio));
    const ballCostFactor = exchangeBalls > 0 ? (lend || DEFAULT_LEND) / exchangeBalls : 1;
    const cost = 1000 / rate * (cash + (1 - cash) * ballCostFactor);
    return 1000 / border - cost;
  }

  /*
   * 区間(大当り・台移動で区切った単位)ごとの回転率と、ボーダーに届いていなかった割合。
   * 割合は「時間」で出す。打っていた時間の長さがそのまま損の大きさになるため。
   * 時刻が無い区間(古いデータ)が混ざるときは回転数の割合で代わりにする。
   * segments: [{spins, investK, startedAt?, endedAt?}]
   */
  function segmentSummary(segments, border) {
    let spins = 0, investK = 0, belowSpins = 0, totalMs = 0, belowMs = 0, timed = true;
    const rows = segments.map(function (s) {
      const rate = s.investK > 0 ? s.spins / s.investK : null;
      const ms = s.startedAt && s.endedAt ? Math.max(0, new Date(s.endedAt) - new Date(s.startedAt)) : null;
      const below = border > 0 && rate !== null && rate < border;
      spins += s.spins;
      investK += s.investK;
      if (below) belowSpins += s.spins;
      if (ms === null) {
        if (s.spins || s.investK) timed = false;
      } else {
        totalMs += ms;
        if (below) belowMs += ms;
      }
      return { spins: s.spins, investK: s.investK, rate: rate, below: below, minutes: ms === null ? null : ms / 60000 };
    });
    let belowRatio = null, basis = null;
    if (timed && totalMs > 0) {
      belowRatio = belowMs / totalMs; basis = 'time';
    } else if (spins > 0) {
      belowRatio = belowSpins / spins; basis = 'spins';
    }
    return {
      rows: rows,
      spins: spins,
      investK: investK,
      rate: investK > 0 ? spins / investK : null,
      minutes: timed && totalMs > 0 ? totalMs / 60000 : null,
      belowRatio: border > 0 ? belowRatio : null,
      belowBasis: basis,
    };
  }

  root.PachinkoEV = {
    DEFAULT_LEND: DEFAULT_LEND,
    num: num,
    borderAt: borderAt,
    evPerSpin: evPerSpin,
    segmentSummary: segmentSummary,
  };
})(window);
