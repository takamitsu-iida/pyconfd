#!/usr/bin/env python3
"""
demo.py - pyconfd 総合デモスクリプト

以下を実演します:
  1. YANG パーサーで dhcpd.yang を読み込む
  2. CDB に設定値を書き込む (MAAPI トランザクション)
  3. CDB サブスクリプションで変更を受け取る
  4. NETCONF サーバーを起動し、get-config / edit-config をテストする
  5. 標準の netconf-console コマンドも使える形で終了

実行方法::

    cd examples/dhcpd
    python demo.py
"""

import os
import sys
import time
import socket
import threading
import logging

# パッケージパスの設定
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from pyconfd.yang_parser import load_yang
from pyconfd.cdb import CDB
from pyconfd.maapi import MAAPI
from pyconfd.netconf_server import NetconfServer
from pyconfd.ssh_server import SSHCLIServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("demo")

# ---------------------------------------------------------------------------
# セットアップ
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
YANG_FILE  = os.path.join(SCRIPT_DIR, "dhcpd.yang")
DB_DIR     = os.path.join(SCRIPT_DIR, "confd-cdb")


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
    step("1. YANG パーサー: dhcpd.yang を読み込む")
    yang_root = load_yang(YANG_FILE)
    print(f"  モジュール名  : {yang_root.name}")
    print(f"  名前空間      : {yang_root.namespace}")
    dhcp_node = yang_root.find_path("/dhcp")
    if dhcp_node:
        print(f"  /dhcp 配下のノード:")
        for child in dhcp_node.data_children():
            print(f"    - {child.node_type.value}: {child.name}")

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

    cdb.subscribe("/dhcp", on_change)
    print("  /dhcp へのサブスクリプションを登録しました")

    # ------------------------------------------------------------------
    # 4. MAAPI トランザクション: 設定を書き込む
    # ------------------------------------------------------------------
    step("4. MAAPI: 設定値をトランザクションで書き込む")
    maapi = MAAPI(cdb)

    with maapi.start_write_trans() as t:
        t.set("/dhcp/default-lease-time", 600)
        t.set("/dhcp/max-lease-time",     7200)
        t.set("/dhcp/log-facility",       "local7")
        # リストエントリを作成
        t.create("/dhcp/subnets/subnet", {"net": "192.168.1.0", "mask": "255.255.255.0"})
        t.create("/dhcp/subnets/subnet", {"net": "10.0.0.0", "mask": "255.0.0.0"})

    # サブネット詳細を設定
    with maapi.start_write_trans() as t:
        subnets = t.get("/dhcp/subnets/subnet")
        if isinstance(subnets, list) and len(subnets) > 0:
            subnets[0].update({
                "range": {"low-addr": "192.168.1.10", "hi-addr": "192.168.1.100"},
                "routers": "192.168.1.1",
                "max-lease-time": 3600,
            })
            subnets[1].update({
                "range": {"low-addr": "10.0.0.100", "hi-addr": "10.0.0.200"},
                "routers": "10.0.0.1",
            })

    time.sleep(0.1)  # サブスクライバへの通知待ち

    print(f"\n  書き込んだ設定の確認:")
    print(f"    default-lease-time : {maapi.get('/dhcp/default-lease-time')}")
    print(f"    max-lease-time     : {maapi.get('/dhcp/max-lease-time')}")
    print(f"    log-facility       : {maapi.get('/dhcp/log-facility')}")
    print(f"    サブネット数        : {cdb.num_instances('/dhcp/subnets/subnet')}")
    print(f"  サブスクライバ受信済みパス: {received_changes}")

    # ------------------------------------------------------------------
    # 5. CDB ダンプ
    # ------------------------------------------------------------------
    step("5. CDB 内容ダンプ (running)")
    print(cdb.dump("running"))

    # ------------------------------------------------------------------
    # 6. NETCONF サーバー起動
    # ------------------------------------------------------------------
    step("6. NETCONF サーバー起動 (TCP port 2022)")
    server = NetconfServer(cdb, host="127.0.0.1", port=2022)
    server.start()
    time.sleep(0.2)

    # ------------------------------------------------------------------
    # 6b. CLI サーバー起動
    # ------------------------------------------------------------------
    step("6b. CLI サーバー起動 (SSH port 2222, C-style)")
    cli_server = SSHCLIServer(cdb, host="127.0.0.1", port=2222, style="c", hostname="pyconfd",
                              users={"admin": "admin"}, schema=yang_root)
    cli_server.start()
    time.sleep(0.2)

    # ------------------------------------------------------------------
    # 7. NETCONF クライアントでテスト
    # ------------------------------------------------------------------
    step("7. NETCONF: get-config テスト (Python クライアント)")
    _netconf_test()

    # ------------------------------------------------------------------
    # 8. 終了
    # ------------------------------------------------------------------
    step("完了")
    print("  NETCONF サーバーはポート 2022 で待機中です。")
    print("  CLI サーバーはポート 2222 で待機中です。")
    print()
    print("  接続方法:")
    print("    NETCONF : netcat 127.0.0.1 2022")
    print("    CLI     : ssh -p 2222 admin@localhost")
    print()
    print("  Ctrl+C で終了します。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    server.stop()
    cli_server.stop()
    print("終了しました。")


# ---------------------------------------------------------------------------
# NETCONF テストクライアント (インライン)
# ---------------------------------------------------------------------------

MSG_SEP = b"]]>]]>"

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

        # サーバーの hello 受信
        hello = _recv_msg(s)
        log.info("[NETCONF client] サーバー hello 受信 (%d 文字)", len(hello))

        # クライアントの hello 送信
        s.sendall(HELLO_XML + MSG_SEP)

        # get-config 送信
        s.sendall(GET_CONFIG_XML + MSG_SEP)
        reply = _recv_msg(s)
        print("\n  <get-config> レスポンス:")
        # 短く表示
        if len(reply) > 800:
            print("  " + reply[:800] + "\n  ... (省略)")
        else:
            print("  " + reply)

        # close-session
        s.sendall(CLOSE_XML + MSG_SEP)
        s.close()
    except Exception as e:
        log.error("[NETCONF client] エラー: %s", e)


if __name__ == "__main__":
    main()
