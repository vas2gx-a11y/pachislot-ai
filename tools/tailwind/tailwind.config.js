// 画面のCSSを事前に作るための設定（作り方は tools/tailwind/build.sh）。
// 以前は base.html で Tailwind の Play CDN を読み、ページを開くたびにブラウザでCSSを生成していた。
// スマホだとその間ずっと画面が崩れるので、使っているクラスだけを先に static/css/tailwind.css に書き出す。
module.exports = {
  // クラス名はテンプレート・判別のJS・Python(アイコンやナビの定義)に直書きされている。
  // 文字列を組み立てて作るクラス名は拾えないので、クラス名は省略せず書くこと。
  content: {
    relative: true,
    files: [
      "../../templates/**/*.html",
      "../../static/js/**/*.js",
      "../../*.py",
      "../../routes/**/*.py",
    ],
  },
  theme: {
    extend: {
      fontFamily: {
        sans: ['"Inter"', '"Noto Sans JP"', "sans-serif"],
        display: ['"Inter"', '"Noto Sans JP"', "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "monospace"],
      },
    },
  },
};
