#!/usr/bin/env python3
"""
hosts_subscriber.py

CDB サブスクリプションのデモ。
ホスト設定が変更されるたびに hosts.conf (UNIX /etc/hosts ライク) を再生成します。

実行方法::

    # ターミナル A: サブスクライバを起動
    python hosts_subscriber.py

    # ターミナル B: demo.py を実行してホストを登録
    python demo.py

demo.py がコミットするたびに hosts.conf が自動更新されます。
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from pyconfd.cdb import CDB

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR     = os.path.join(SCRIPT_DIR, "confd-cdb")
OUT_FILE   = os.path.join(SCRIPT_DIR, "hosts.conf")


def write_hosts_conf(cdb: CDB):
    """CDB の設定から /etc/hosts ライクなファイルを生成する"""
    lines = [
        "# hosts.conf — pyconfd hosts_subscriber.py が自動生成",
        "# フォーマット: <IP アドレス>  <FQDN>  <ホスト名>",
        "",
        "127.0.0.1   localhost",
        "",
    ]

    try:
        host_list = cdb.get("/hosts/host")
    except KeyError:
        host_list = []

    if not isinstance(host_list, list):
        host_list = [host_list] if host_list else []

    for host in host_list:
        name   = host.get("name", "")
        domain = host.get("domain", "")
        ifaces = host.get("interfaces", {}).get("interface", [])
        if not isinstance(ifaces, list):
            ifaces = [ifaces] if ifaces else []

        # 有効なインターフェースの IP を全て出力
        for iface in ifaces:
            enabled = iface.get("enabled", True)
            # 文字列 "false" / bool False の両方を考慮
            if str(enabled).lower() == "false":
                continue
            ip   = iface.get("ip", "")
            ifname = iface.get("name", "")
            if ip:
                fqdn = f"{name}.{domain}" if domain else name
                lines.append(f"{ip:<18} {fqdn:<30} {name}  # {ifname}")

    lines.append("")
    content = "\n".join(lines)

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"[subscriber] hosts.conf を更新しました:")
    print(content)


def on_hosts_changed(changed_paths):
    print(f"[subscriber] 変更を検知: {changed_paths}")
    write_hosts_conf(cdb)


if __name__ == "__main__":
    os.makedirs(DB_DIR, exist_ok=True)
    cdb = CDB(db_dir=DB_DIR)

    # /hosts 以下の変更を購読
    cdb.subscribe("/hosts", on_hosts_changed)

    # 起動時に一度生成
    write_hosts_conf(cdb)

    print("[subscriber] 変更を待機中... (Ctrl+C で終了)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[subscriber] 終了")
