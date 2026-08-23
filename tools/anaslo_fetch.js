/*
 * アナスロ(ana-slo.com)の日別データをまとめて取得し、CSVとしてダウンロードするスクリプト。
 *
 * ana-slo.com はCloudflareのbot対策が入っているため、curl や Python の requests から
 * 直接叩くと 403 が返る。一方、自分がブラウザで開いているページの中から fetch すれば
 * 通常の閲覧と同じ扱いになるので、この方法なら素直に取得できる。
 *
 * 使い方:
 *   1. Chromeで https://ana-slo.com/ の任意のページを開く
 *   2. F12 → Console を開く
 *   3. 下のCONFIGを書き換えて、このファイルの中身を丸ごと貼り付けてEnter
 *   4. 取得が終わるとCSVが自動でダウンロードされる
 *   5. そのCSVを tools/anaslo.py agg で集計する
 *
 * 出力CSVの列は tools/anaslo.py の CSV_FIELDS と同じ。
 * サーバーに負荷をかけないよう、1日分ごとに間隔(既定3秒)を空けて順番に取得する。
 */
(async () => {
  const CONFIG = {
    store: '楽園大宮店',   // 店舗名(アナスロのページ表記どおりに)
    days: 30,              // 何日分さかのぼるか
    endDate: '',           // 起点日 'YYYY-MM-DD'(空なら昨日から)
    delayMs: 3000,         // 1日分ごとの待ち時間(ミリ秒)。短くしすぎない
  };

  if (location.hostname !== 'ana-slo.com') {
    console.error('ana-slo.com のページを開いた状態で実行してください。');
    return;
  }

  const FIELDS = ['date', 'store_name', 'machine_number', 'machine_name',
                  'games', 'diff', 'bb', 'rb', 'art',
                  'total_rate', 'bb_rate', 'rb_rate', 'art_rate'];

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const toInt = (text) => {
    const cleaned = String(text).replace(/[,+\s]/g, '');
    const value = parseInt(cleaned, 10);
    return Number.isNaN(value) ? '' : value;
  };

  // '1/152.7' → 152.7。当たり0回のときの '1/0.0' は算出不能なので空にする
  const toRate = (text) => {
    const match = String(text).match(/1\s*\/\s*([\d,.]+)/);
    if (!match) return '';
    const value = parseFloat(match[1].replace(/,/g, ''));
    return value > 0 ? value : '';
  };

  const stripTags = (fragment) => {
    const div = document.createElement('div');
    div.innerHTML = fragment;
    return (div.textContent || '').replace(/ /g, ' ').trim();
  };

  const dayUrl = (dateStr) =>
    `${location.origin}/${dateStr}-${encodeURIComponent(CONFIG.store)}-data/`;

  const dateList = () => {
    const end = CONFIG.endDate ? new Date(`${CONFIG.endDate}T00:00:00`) : new Date(Date.now() - 86400000);
    const dates = [];
    for (let i = 0; i < CONFIG.days; i += 1) {
      const d = new Date(end.getTime() - i * 86400000);
      const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
      dates.push(iso);
    }
    return dates.reverse();
  };

  // 9MB近いHTMLをまるごとDOM化すると重いので、全台データの表だけ切り出してから解析する
  const parseDay = (htmlText, dateStr) => {
    const start = htmlText.indexOf('<table id="all_data_table"');
    if (start === -1) return [];
    const end = htmlText.indexOf('</table>', start);
    const table = htmlText.slice(start, end);

    const rows = [];
    for (const rowMatch of table.matchAll(/<tr>([\s\S]*?)<\/tr>/g)) {
      const cells = [...rowMatch[1].matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)].map((m) => stripTags(m[1]));
      if (cells.length < 11) continue;
      const number = toInt(cells[1]);
      if (number === '') continue;
      rows.push({
        date: dateStr,
        store_name: CONFIG.store,
        machine_number: number,
        machine_name: cells[0],
        games: toInt(cells[2]),
        diff: toInt(cells[3]),
        bb: toInt(cells[4]),
        rb: toInt(cells[5]),
        art: toInt(cells[6]),
        total_rate: toRate(cells[7]),
        bb_rate: toRate(cells[8]),
        rb_rate: toRate(cells[9]),
        art_rate: toRate(cells[10]),
      });
    }
    return rows;
  };

  const collected = [];
  const skipped = [];
  const dates = dateList();
  console.log(`${CONFIG.store} / ${dates[0]} 〜 ${dates[dates.length - 1]} の ${dates.length}日分を取得します`);

  for (let i = 0; i < dates.length; i += 1) {
    const dateStr = dates[i];
    try {
      const response = await fetch(dayUrl(dateStr), { credentials: 'include' });
      if (!response.ok) {
        // 休業日や未掲載の日は404になる。そこで止めず次の日へ進む
        skipped.push(`${dateStr}(HTTP ${response.status})`);
      } else {
        const text = await response.text();
        if (text.includes('Just a moment')) {
          console.error('Cloudflareの確認画面が返りました。ページを開き直してからやり直してください。');
          break;
        }
        const rows = parseDay(text, dateStr);
        if (rows.length === 0) {
          skipped.push(`${dateStr}(データなし)`);
        } else {
          collected.push(...rows);
          const total = rows.reduce((sum, r) => sum + (r.diff || 0), 0);
          console.log(`[${i + 1}/${dates.length}] ${dateStr}: ${rows.length}台 / 総差枚 ${total.toLocaleString()}`);
        }
      }
    } catch (e) {
      skipped.push(`${dateStr}(${e.message})`);
    }
    if (i < dates.length - 1) await sleep(CONFIG.delayMs);
  }

  if (collected.length === 0) {
    console.error('1件も取得できませんでした。店舗名の表記とCONFIGを確認してください。');
    return;
  }

  const escape = (value) => {
    const text = value === null || value === undefined ? '' : String(value);
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const csv = [FIELDS.join(',')]
    .concat(collected.map((row) => FIELDS.map((f) => escape(row[f])).join(',')))
    .join('\n');

  // Excelで文字化けしないようBOMを付ける
  const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8;' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `anaslo_${CONFIG.store}_${dates[0]}_${dates[dates.length - 1]}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();

  console.log(`完了: ${collected.length}行 / ${new Set(collected.map((r) => r.date)).size}日分`);
  if (skipped.length) console.warn('取得できなかった日:', skipped.join(', '));
  window.anasloRows = collected;  // 追加で加工したいとき用にコンソールへ残しておく
})();
