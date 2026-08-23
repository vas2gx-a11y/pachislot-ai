from flask import Blueprint, render_template, redirect, url_for, abort

import navigation

nav_bp = Blueprint("nav", __name__, url_prefix="/c")


@nav_bp.route("/<key>")
def category(key):
    """
    カテゴリのトップページ。配下の機能を一覧で出す。

    配下が1つしかないカテゴリは、わざわざ中継ページを挟む意味がないので
    その機能へそのまま飛ばす(クリック数を増やさないため)。
    """
    target = navigation.find_category(key)
    if not target:
        abort(404)

    items = navigation.visible_items(target)
    if len(items) == 1:
        return redirect(url_for(items[0]["endpoint"]))

    return render_template("category.html", category=target, items=items)
