#!/usr/bin/env python3
"""
demo.py - pyconfd ホスト管理デモ

ConfD の examples.confd/intro/python/6-config (hst.yang) を参考にした
ネットワークホスト設定管理のデモスクリプトです。

以下を実演します:
  1. YANG パーサーで hosts.yang を読み込む
  2. MAAPI トランザクションでホスト・インターフェースを登録する
  3. CDB サブスクリプションで変更を受け取る
  4. NETCONF サーバーを起動して get-config をテストする
  5. CLI サーバーを起動して Telnet 接続できる状態にする

実行方法::

    cd examples/hosts
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

from pyconfd.yang_parser import load_yang
from pyconfd.cdb import CDB
from pyconfd.maapi import MAAPI
from pyconfd.netconf_server import NetconfServer
from pyconfd.ssh_server import SSHCLIServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("hosts-demo")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
YANG_FILE  = os.path.join(SCRIPT_DIR, "hosts.yang")
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
    step("1. YANG パーサー: hosts.yang を読み込む")
    yang_root = load_yang(YANG_FILE)
    print(f"  モジュール名  : {yang_root.name}")
    print(f"  名前空間      : {yang_root.namespace}")

    hosts_node = yang_root.find_path("/hosts")
    if hosts_node:
        print("  /hosts 配下のノード:")
        for child in hosts_node.data_children():
            print(f"    - {child.node_type.value}: {child.name}")
        host_node = hosts_node.get_child("host")
        if host_node:
            print(f"  /hosts/host のキー   : {host_node.keys}")
            print(f"  /hosts/host の子ノード:")
            for child in host_node.data_children():
                print(f"    - {child.node_type.value}: {child.name}")

    # ------------------------------------------------------------------
    # 2. CDB 初期化
    # ------------------------------------------------------------------
    step("2. CDB 初期化")
    os.makedirs(DB_DIR, exist_ok=True)
    cdb = CDB(db_dir=DB_DIR)
    print(f"  DB ディレクトリ: {DB_DIR}")

    # ------------------------------------------------------------------
    # 3. CDB サブスクリプション
    # ------------------------------------------------------------------
    step("3. CDB サブスクリプション登録")
    received_changes = []

    def on_hosts_changed(paths):
        log.info("[CDB subscriber] 変更通知: %s", paths)
        received_changes.extend(paths)

    cdb.subscribe("/hosts", on_hosts_changed)
    print("  /hosts へのサブスクリプションを登録しました")

    # ------------------------------------------------------------------
    # 4. MAAPI: ホストエントリを登録する
    # ------------------------------------------------------------------
    step("4. MAAPI: ホスト・インターフェースをトランザクションで登録する")
    maapi = MAAPI(cdb)

    # ---- buzz ホスト ----
    with maapi.start_write_trans() as t:
        t.create("/hosts/host", {"name": "buzz"})
        t.set("/hosts/host[name=buzz]/domain", "tail-f.com")
        t.set("/hosts/host[name=buzz]/defgw",  "192.168.1.1")

    with maapi.start_write_trans() as t:
        t.create("/hosts/host[name=buzz]/interfaces/interface", {"name": "eth0"})
        t.set("/hosts/host[name=buzz]/interfaces/interface[name=eth0]/ip",      "192.168.1.61")
        t.set("/hosts/host[name=buzz]/interfaces/interface[name=eth0]/mask",    "255.255.255.0")
        t.set("/hosts/host[name=buzz]/interfaces/interface[name=eth0]/enabled", True)

        t.create("/hosts/host[name=buzz]/interfaces/interface", {"name": "eth1"})
        t.set("/hosts/host[name=buzz]/interfaces/interface[name=eth1]/ip",      "10.77.1.44")
        t.set("/hosts/host[name=buzz]/interfaces/interface[name=eth1]/mask",    "255.255.0.0")
        t.set("/hosts/host[name=buzz]/interfaces/interface[name=eth1]/enabled", True)

    # ---- woody ホスト ----
    with maapi.start_write_trans() as t:
        t.create("/hosts/host", {"name": "woody"})
        t.set("/hosts/host[name=woody]/domain", "tail-f.com")
        t.set("/hosts/host[name=woody]/defgw",  "10.0.0.1")

    with maapi.start_write_trans() as t:
        t.create("/hosts/host[name=woody]/interfaces/interface", {"name": "eth0"})
        t.set("/hosts/host[name=woody]/interfaces/interface[name=eth0]/ip",      "10.0.0.55")
        t.set("/hosts/host[name=woody]/interfaces/interface[name=eth0]/mask",    "255.0.0.0")
        t.set("/hosts/host[name=woody]/interfaces/interface[name=eth0]/enabled", False)

    time.sleep(0.1)  # サブスクライバへの通知待ち

    # ---- 登録結果の確認 ----
    print("\n  登録済みホスト:")
    try:
        host_list = cdb.get("/hosts/host")
        if isinstance(host_list, list):
            for h in host_list:
                name   = h.get("name", "")
                domain = h.get("domain", "")
                defgw  = h.get("defgw", "")
                ifaces = h.get("interfaces", {}).get("interface", [])
                if not isinstance(ifaces, list):
                    ifaces = [ifaces]
                print(f"    Host {name:>10}  domain={domain}  defgw={defgw}")
                for iface in ifaces:
                    ifname  = iface.get("name", "")
                    ip      = iface.get("ip", "")
                    mask    = iface.get("mask", "")
                    enabled = iface.get("enabled", True)
                    print(f"         iface: {ifname:>7}  {ip:>15}  {mask:>15}  enabled={enabled}")
    except KeyError:
        print("    (データなし)")

    print(f"\n  サブスクライバが受信したパス: {received_changes}")

    # ------------------------------------------------------------------
    # 5. CDB ダンプ
    # ------------------------------------------------------------------
    step("5. CDB 内容ダンプ (running)")
    print(cdb.dump("running"))

    # ------------------------------------------------------------------
    # 6. NETCONF サーバー起動
    # ------------------------------------------------------------------
    step("6. NETCONF サーバー起動 (TCP port 2022)")
    netconf_srv = NetconfServer(cdb, host="127.0.0.1", port=2022)
    netconf_srv.start()
    time.sleep(0.2)

    # ------------------------------------------------------------------
    # 6b. CLI サーバー起動
    # ------------------------------------------------------------------
    step("6b. CLI サーバー起動 (SSH port 2222, C-style)")
    cli_srv = SSHCLIServer(
        cdb,
        host="127.0.0.1",
        port=2222,
        style="c",
        hostname="router",
        users={"admin": "admin"},
        schema=yang_root,
    )
    cli_srv.start()
    time.sleep(0.2)

    # ------------------------------------------------------------------
    # 7. NETCONF クライアントでテスト
    # ------------------------------------------------------------------
    step("7. NETCONF get-config テスト")
    _netconf_get_config_test()

    # ------------------------------------------------------------------
    # 8. 終了
    # ------------------------------------------------------------------
    step("完了 — サーバー待機中")
    print("  接続方法:")
    print("    NETCONF : nc 127.0.0.1 2022")
    print("    CLI     : ssh -p 2222 admin@localhost")
    print()
    print("  CLI 操作例 (C-style):")
    print("    $ ssh -p 2222 admin@localhost")
    print("    router> show running-config")
    print("    router> configure")
    print("    router(config)# hosts host[name=buzz] defgw 192.168.1.254")
    print("    router(config)# commit")
    print()
    print("  Ctrl+C で終了します。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    netconf_srv.stop()
    cli_srv.stop()
    print("終了しました。")


# ---------------------------------------------------------------------------
# NETCONF テストクライアント
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


def _netconf_get_config_test():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(("127.0.0.1", 2022))

        hello = _recv_msg(s)
        log.info("[NETCONF client] サーバー hello 受信 (%d 文字)", len(hello))

        s.sendall(HELLO_XML + MSG_SEP)
        s.sendall(GET_CONFIG_XML + MSG_SEP)

        reply = _recv_msg(s)
        print("\n  <get-config> レスポンス:")
        if len(reply) > 1000:
            print("  " + reply[:1000] + "\n  ... (省略)")
        else:
            print("  " + reply)

        s.sendall(CLOSE_XML + MSG_SEP)
        s.close()
    except Exception as e:
        log.error("[NETCONF client] エラー: %s", e)


if __name__ == "__main__":
    main()
