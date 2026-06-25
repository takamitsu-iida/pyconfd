#!/usr/bin/env python3
"""
recipe_subscriber.py

CDB サブスクリプションのデモ。
レシピが追加・変更・削除されたときに recipes.md (Markdown) を再生成します。

実行方法::

    cd examples/recipe
    python recipe_subscriber.py

別ターミナルで demo.py を実行して設定を変更すると、
このスクリプトが変更を検知して recipes.md を書き出します。
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from pyconfd.cdb import CDB

DB_DIR   = os.path.join(os.path.dirname(__file__), "confd-cdb")
OUT_FILE = os.path.join(os.path.dirname(__file__), "recipes.md")

# 難易度の日本語ラベル
DIFFICULTY_LABEL = {"easy": "⭐ 簡単", "medium": "⭐⭐ 普通", "hard": "⭐⭐⭐ 難しい"}
CUISINE_LABEL    = {
    "japanese": "🍱 和食",
    "italian":  "🍝 イタリアン",
    "chinese":  "🥢 中華",
    "french":   "🥐 フレンチ",
    "other":    "🍽️ その他",
}


def write_recipes_md(cdb: "CDB") -> None:
    """CDB のレシピデータから Markdown ファイルを生成する"""
    recipes = cdb.get("/recipes/recipe")
    if not recipes:
        content = "# レシピ集\n\nレシピがまだ登録されていません。\n"
        with open(OUT_FILE, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[subscriber] recipes.md を更新しました (レシピなし)")
        return

    if not isinstance(recipes, list):
        recipes = [recipes]

    lines = ["# レシピ集\n"]

    for r in recipes:
        name        = r.get("name", "(無題)")
        cuisine     = CUISINE_LABEL.get(r.get("cuisine", "other"), r.get("cuisine", ""))
        difficulty  = DIFFICULTY_LABEL.get(r.get("difficulty", "easy"), r.get("difficulty", ""))
        servings    = r.get("servings", "")
        prep_time   = r.get("prep-time", "")
        cook_time   = r.get("cook-time", "")
        calories    = r.get("calories", "")
        description = r.get("description", "")

        lines.append(f"## {name}\n")
        if description:
            lines.append(f"{description}\n")

        lines.append("| 項目 | 内容 |")
        lines.append("|------|------|")
        if cuisine:
            lines.append(f"| ジャンル | {cuisine} |")
        if difficulty:
            lines.append(f"| 難易度 | {difficulty} |")
        if servings:
            lines.append(f"| 分量 | {servings} 人前 |")
        if prep_time:
            lines.append(f"| 下準備 | {prep_time} 分 |")
        if cook_time:
            lines.append(f"| 調理時間 | {cook_time} 分 |")
        if calories:
            lines.append(f"| カロリー | {calories} kcal/人前 |")
        lines.append("")

        # 材料
        ingredients = r.get("ingredients", {}).get("ingredient", [])
        if not isinstance(ingredients, list):
            ingredients = [ingredients]
        if ingredients:
            lines.append("### 材料\n")
            lines.append("| 材料 | 分量 | 備考 |")
            lines.append("|------|------|------|")
            for ing in ingredients:
                ing_name   = ing.get("name", "")
                amount     = ing.get("amount", "")
                optional   = "任意" if ing.get("optional", False) else ""
                lines.append(f"| {ing_name} | {amount} | {optional} |")
            lines.append("")

        # 手順
        steps = r.get("steps", {}).get("step", [])
        if not isinstance(steps, list):
            steps = [steps]
        if steps:
            # order で並び替え
            steps = sorted(steps, key=lambda s: int(s.get("order", 0)))
            lines.append("### 手順\n")
            for s in steps:
                order       = s.get("order", "")
                instruction = s.get("instruction", "")
                lines.append(f"{order}. {instruction}")
            lines.append("")

        lines.append("---\n")

    content = "\n".join(lines)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[subscriber] recipes.md を更新しました ({len(recipes)} レシピ)")
    print(content)


def on_recipes_changed(changed_paths):
    print(f"[subscriber] 変更を検知: {changed_paths}")
    write_recipes_md(cdb)


if __name__ == "__main__":
    os.makedirs(DB_DIR, exist_ok=True)
    cdb = CDB(db_dir=DB_DIR)

    # /recipes 以下の変更を購読
    cdb.subscribe("/recipes", on_recipes_changed)

    # 起動時に一度生成
    write_recipes_md(cdb)

    print("[subscriber] 変更を待機中... (Ctrl+C で終了)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[subscriber] 終了")
