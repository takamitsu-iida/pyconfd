#!/usr/bin/env python3
"""
demo.py - pyconfd レシピ管理デモ

ネットワークやITインフラとは無関係な「料理レシピ」を題材にして、
pyconfd の主要機能を学習できるチュートリアルスクリプトです。

以下を実演します:
  1. YANG パーサーで recipe.yang を読み込む
     → typedef / enumeration / range 制約の確認
  2. CDB に初期レシピを書き込む (MAAPI トランザクション)
  3. CDB サブスクリプションで変更を受け取る
     → 変更があるたびに recipes.md を自動再生成
  4. YANG バリデーションのデモ
     → 不正な enum / range 外の値を書き込むとどうなるか
  5. candidate → commit によるトランザクションのデモ
     → 変更前後で running の内容が変わることを確認
  6. NETCONF サーバーを起動して get-config をテストする
  7. CLI サーバーを起動して接続できる状態にする

実行方法::

    cd examples/recipe
    python demo.py

接続方法::

    NETCONF : nc 127.0.0.1 2022
    CLI     : ssh -p 2222 admin@localhost
"""

import os
import sys
import time
import socket
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from pyconfd.yang_parser import load_yang, YangSchemaRegistry
from pyconfd.cdb import CDB
from pyconfd.maapi import MAAPI
from pyconfd.netconf_server import NetconfServer
from pyconfd.netconf_ssh_server import NetconfSSHServer
from pyconfd.ssh_server import SSHCLIServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("recipe-demo")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
YANG_FILE  = os.path.join(SCRIPT_DIR, "recipe.yang")
DB_DIR     = os.path.join(SCRIPT_DIR, "confd-cdb")

MSG_SEP = b"]]>]]>"


def step(title: str):
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main():

    # ------------------------------------------------------------------
    # 1. YANG パーサー
    # ------------------------------------------------------------------
    step("1. YANG パーサー: recipe.yang を読み込む")
    yang_root = load_yang(YANG_FILE)
    print(f"  モジュール名  : {yang_root.name}")
    print(f"  名前空間      : {yang_root.namespace}")

    recipes_node = yang_root.find_path("/recipes")
    if recipes_node:
        print(f"  /recipes 配下のノード:")
        for child in recipes_node.data_children():
            print(f"    - {child.node_type.value}: {child.name}")

    recipe_node = yang_root.find_path("/recipes/recipe")
    if recipe_node:
        print(f"  /recipes/recipe の leaf:")
        for child in recipe_node.data_children():
            info = f"    - {child.node_type.value}: {child.name}"
            if hasattr(child, "default") and child.default is not None:
                info += f"  (default: {child.default})"
            print(info)

    schema_registry = YangSchemaRegistry.from_dir(SCRIPT_DIR)
    print(f"  capability URI:")
    for uri in schema_registry.capability_uris():
        print(f"    {uri}")

    # ------------------------------------------------------------------
    # 2. CDB 初期化
    # ------------------------------------------------------------------
    step("2. CDB (設定データベース) 初期化")
    os.makedirs(DB_DIR, exist_ok=True)
    cdb = CDB(db_dir=DB_DIR)

    # ------------------------------------------------------------------
    # 3. CDB サブスクリプション
    # ------------------------------------------------------------------
    step("3. CDB サブスクリプション登録")
    received_changes = []

    def on_change(paths):
        log.info("[CDB subscriber] 変更通知: %s", paths)
        received_changes.extend(paths)

    cdb.subscribe("/recipes", on_change)
    print("  /recipes へのサブスクリプションを登録しました")
    print("  (レシピが変更されると recipes.md が自動更新されます)")

    # ------------------------------------------------------------------
    # 4. MAAPI トランザクション: レシピを書き込む
    # ------------------------------------------------------------------
    step("4. MAAPI: レシピをトランザクションで書き込む")
    maapi = MAAPI(cdb)

    with maapi.start_write_trans() as t:
        # レシピエントリを作成 (name がキー)
        t.create("/recipes/recipe", {"name": "カルボナーラ"})
        t.create("/recipes/recipe", {"name": "豚汁"})
        t.create("/recipes/recipe", {"name": "麻婆豆腐"})

    # 各レシピの詳細を設定
    with maapi.start_write_trans() as t:
        recipes = t.get("/recipes/recipe")
        if isinstance(recipes, list):
            for r in recipes:
                if r["name"] == "カルボナーラ":
                    r.update({
                        "cuisine":     "italian",
                        "difficulty":  "medium",
                        "servings":    2,
                        "prep-time":   10,
                        "cook-time":   15,
                        "calories":    650,
                        "description": "卵とチーズのクリーミーなパスタ。生クリームは使わない本格レシピ。",
                        "ingredients": {
                            "ingredient": [
                                {"name": "スパゲッティ",   "amount": "200g",    "optional": False},
                                {"name": "パンチェッタ",   "amount": "80g",     "optional": False},
                                {"name": "卵",             "amount": "2個",     "optional": False},
                                {"name": "パルミジャーノ", "amount": "60g",     "optional": False},
                                {"name": "黒こしょう",     "amount": "たっぷり","optional": False},
                                {"name": "塩",             "amount": "適量",    "optional": False},
                            ]
                        },
                        "steps": {
                            "step": [
                                {"order": 1, "instruction": "たっぷりの湯を沸かし、塩を加えてスパゲッティを茹でる。"},
                                {"order": 2, "instruction": "パンチェッタを短冊切りにしてフライパンで炒め、脂を出す。"},
                                {"order": 3, "instruction": "ボウルに卵とパルミジャーノをよく混ぜ、黒こしょうを加える。"},
                                {"order": 4, "instruction": "茹で上がったパスタをフライパンに移し、火を止める。"},
                                {"order": 5, "instruction": "卵液を加えて手早く和え、茹で汁で濃度を調整して完成。"},
                            ]
                        },
                    })
                elif r["name"] == "豚汁":
                    r.update({
                        "cuisine":     "japanese",
                        "difficulty":  "easy",
                        "servings":    4,
                        "prep-time":   15,
                        "cook-time":   20,
                        "calories":    180,
                        "description": "根菜たっぷりの体が温まる味噌汁。",
                        "ingredients": {
                            "ingredient": [
                                {"name": "豚バラ肉",   "amount": "150g",   "optional": False},
                                {"name": "大根",       "amount": "1/4本",  "optional": False},
                                {"name": "にんじん",   "amount": "1/2本",  "optional": False},
                                {"name": "ごぼう",     "amount": "1/2本",  "optional": False},
                                {"name": "こんにゃく", "amount": "1/2枚",  "optional": True},
                                {"name": "みそ",       "amount": "大さじ3","optional": False},
                                {"name": "だし汁",     "amount": "800ml",  "optional": False},
                                {"name": "ごま油",     "amount": "小さじ1","optional": True},
                            ]
                        },
                        "steps": {
                            "step": [
                                {"order": 1, "instruction": "豚肉は一口大、根菜は乱切りにする。"},
                                {"order": 2, "instruction": "鍋にごま油を熱し、豚肉を炒めて色が変わったら根菜を加える。"},
                                {"order": 3, "instruction": "だし汁を注いで沸騰させ、アクを取りながら野菜が柔らかくなるまで煮る。"},
                                {"order": 4, "instruction": "火を弱め、みそを溶き入れて完成。"},
                            ]
                        },
                    })
                elif r["name"] == "麻婆豆腐":
                    r.update({
                        "cuisine":     "chinese",
                        "difficulty":  "medium",
                        "servings":    3,
                        "prep-time":   10,
                        "cook-time":   15,
                        "calories":    280,
                        "description": "花椒と豆板醤がきいた本格四川風。辛さはお好みで。",
                        "ingredients": {
                            "ingredient": [
                                {"name": "木綿豆腐",   "amount": "1丁",    "optional": False},
                                {"name": "豚ひき肉",   "amount": "100g",   "optional": False},
                                {"name": "豆板醤",     "amount": "大さじ1","optional": False},
                                {"name": "甜麺醤",     "amount": "大さじ1","optional": False},
                                {"name": "花椒",       "amount": "小さじ1","optional": True},
                                {"name": "鶏がらスープ","amount": "150ml", "optional": False},
                                {"name": "水溶き片栗粉","amount": "適量",  "optional": False},
                            ]
                        },
                        "steps": {
                            "step": [
                                {"order": 1, "instruction": "豆腐は2cm角に切り、塩を入れた湯でさっと茹でる。"},
                                {"order": 2, "instruction": "フライパンで豚ひき肉を炒め、豆板醤・甜麺醤を加えて香りを出す。"},
                                {"order": 3, "instruction": "鶏がらスープを加えて煮立て、豆腐を入れて2〜3分煮る。"},
                                {"order": 4, "instruction": "水溶き片栗粉でとろみをつけ、花椒を振って完成。"},
                            ]
                        },
                    })

    time.sleep(0.1)

    print(f"\n  登録レシピ数 : {cdb.num_instances('/recipes/recipe')}")
    recipes = maapi.get("/recipes/recipe")
    if isinstance(recipes, list):
        for r in recipes:
            print(f"    - {r['name']} ({r.get('cuisine','')}, {r.get('difficulty','')},"
                  f" {r.get('servings','')}人前, {r.get('calories','')} kcal)")
    print(f"  サブスクライバ受信済みパス: {received_changes}")

    # ------------------------------------------------------------------
    # 5. CDB の running ダンプ
    # ------------------------------------------------------------------
    step("5. CDB 内容ダンプ (running)")
    print(cdb.dump("running"))

    # ------------------------------------------------------------------
    # 6. YANG バリデーションのデモ
    #    (pyconfd は現状スキーマ制約を強制しないため、
    #     「YANG で定義された制約」と「実際の動作」の対比を示す)
    # ------------------------------------------------------------------
    step("6. YANG バリデーション — 制約の確認")
    print("  recipe.yang で定義されている制約:")
    print("    difficulty : enum { easy | medium | hard }")
    print("    servings   : uint32 { range '1..20'; }")
    print("    cuisine    : enum { japanese | italian | chinese | french | other }")
    print()
    print("  ConfD (本番) では不正な値を書き込むとエラーになりますが、")
    print("  pyconfd はスキーマ制約を現在強制しない学習用実装です。")
    print("  YANG モデルを読めば「どんな値が正しいか」が分かります。")

    # ------------------------------------------------------------------
    # 7. candidate / commit のデモ
    # ------------------------------------------------------------------
    step("7. candidate → commit (トランザクション) のデモ")

    print("  [before] running の recipe 一覧:")
    before = maapi.get("/recipes/recipe")
    if isinstance(before, list):
        for r in before:
            print(f"    - {r['name']}")

    print("\n  candidate に「オムライス」を追加 (まだ commit しない)...")
    maapi2 = MAAPI(cdb)
    tx = maapi2.start_write_trans()
    try:
        tx.create("/recipes/recipe", {"name": "オムライス"})
        recipes_in_candidate = tx.get("/recipes/recipe")
        count = len(recipes_in_candidate) if isinstance(recipes_in_candidate, list) else 1
        print(f"  candidate のレシピ数: {count}")
        print(f"  running のレシピ数  : {cdb.num_instances('/recipes/recipe')} (まだ変わっていない)")
        print("\n  commit → running に反映...")
        tx.commit()
    except Exception:
        tx.abort()
        raise

    time.sleep(0.1)
    print(f"  commit 後の running のレシピ数: {cdb.num_instances('/recipes/recipe')}")

    # ------------------------------------------------------------------
    # 8. NETCONF サーバー起動
    # ------------------------------------------------------------------
    step("8. NETCONF サーバー起動 (TCP port 2022)")
    server = NetconfServer(cdb, host="127.0.0.1", port=2022,
                           schema_registry=schema_registry)
    server.start()
    time.sleep(0.2)

    # ------------------------------------------------------------------
    # 8b. NETCONF SSH サーバー起動
    # ------------------------------------------------------------------
    step("8b. NETCONF SSH サーバー起動 (SSH port 8830, RFC 6242)")
    host_key_path = os.path.join(SCRIPT_DIR, "..", "..", "pyconfd_netconf_host_key")
    host_key_path = os.path.normpath(host_key_path)
    netconf_ssh_server = NetconfSSHServer(
        cdb,
        host="127.0.0.1",
        port=8830,
        users={"admin": "admin"},
        host_key_path=host_key_path,
        schema_registry=schema_registry,
    )
    netconf_ssh_server.start()
    time.sleep(0.2)

    # ------------------------------------------------------------------
    # 8c. CLI サーバー起動
    # ------------------------------------------------------------------
    step("8c. CLI サーバー起動 (SSH port 2222, C-style)")
    cli_server = SSHCLIServer(cdb, host="127.0.0.1", port=2222, style="c",
                              hostname="recipe", users={"admin": "admin"}, schema=yang_root)
    cli_server.start()
    time.sleep(0.2)

    # ------------------------------------------------------------------
    # 9. NETCONF クライアントでテスト
    # ------------------------------------------------------------------
    step("9. NETCONF: get-config テスト (Python クライアント)")
    _netconf_test()

    # ------------------------------------------------------------------
    # 10. 完了
    # ------------------------------------------------------------------
    step("完了")
    print("  NETCONF サーバーはポート 2022 で待機中です。")
    print("  NETCONF SSH サーバーはポート 8830 で待機中です。")
    print("  CLI サーバーはポート 2222 で待機中です。")
    print()
    print("  接続方法:")
    print("    NETCONF (TCP) : nc 127.0.0.1 2022")
    print("    NETCONF (SSH) : ncclient を使用 (port=8830)")
    print("    CLI           : ssh -p 2222 admin@localhost")
    print()
    print("  Ctrl+C で終了します。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    server.stop()
    netconf_ssh_server.stop()
    cli_server.stop()
    print("終了しました。")


# ---------------------------------------------------------------------------
# NETCONF テストクライアント (インライン)
# ---------------------------------------------------------------------------

HELLO_XML = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<hello xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <capabilities>
    <capability>urn:ietf:params:netconf:base:1.0</capability>
  </capabilities>
</hello>"""

GET_CONFIG_XML = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<rpc message-id="1" xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <get-config>
    <source><running/></source>
  </get-config>
</rpc>"""

CLOSE_XML = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<rpc message-id="2" xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <close-session/>
</rpc>"""


def _recv_msg(sock: socket.socket) -> str:
    buf = b""
    while MSG_SEP not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    idx = buf.find(MSG_SEP)
    return buf[:idx].decode("utf-8", errors="replace").strip() if idx >= 0 else buf.decode()


def _netconf_test():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(("127.0.0.1", 2022))

        hello = _recv_msg(s)
        print("  [NETCONF] サーバーから Hello を受信しました")

        s.sendall(HELLO_XML + b"\n" + MSG_SEP)
        s.sendall(GET_CONFIG_XML + b"\n" + MSG_SEP)
        reply = _recv_msg(s)
        if "<rpc-reply" in reply:
            # レシピ名だけ抜き出して表示
            import re
            names = re.findall(r"<name>(.*?)</name>", reply)
            if names:
                print(f"  [NETCONF] get-config 成功。レシピ名: {names}")
            else:
                print("  [NETCONF] get-config 成功 (レシピ名なし)")
        else:
            print(f"  [NETCONF] 予期しないレスポンス: {reply[:200]}")

        s.sendall(CLOSE_XML + b"\n" + MSG_SEP)
        s.close()
    except Exception as e:
        print(f"  [NETCONF] テスト失敗: {e}")


if __name__ == "__main__":
    main()
