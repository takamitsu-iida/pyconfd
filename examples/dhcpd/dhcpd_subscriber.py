#!/usr/bin/env python3
"""
dhcpd_subscriber.py

CDB サブスクリプションのデモ。
DHCP 設定が変更されたときに dhcpd.conf を再生成します。

実行方法::

    python dhcpd_subscriber.py

別ターミナルで maapi_demo.py を実行して設定を変更すると、
このスクリプトが変更を検知して dhcpd.conf を書き出します。
"""

import os
import sys
import time

# パッケージパスの設定
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from pyconfd.cdb import CDB

DB_DIR = os.path.join(os.path.dirname(__file__), "confd-cdb")
OUT_FILE = os.path.join(os.path.dirname(__file__), "dhcpd.conf")


def write_dhcpd_conf(cdb: CDB):
    """CDB の設定から dhcpd.conf を生成する"""
    try:
        default_lease = cdb.get("/dhcp/default-lease-time")
        max_lease     = cdb.get("/dhcp/max-lease-time")
        log_facility  = cdb.get("/dhcp/log-facility")
    except KeyError:
        print("[subscriber] /dhcp 設定がまだありません。スキップ。")
        return

    lines = [
        f"default-lease-time {default_lease};",
        f"max-lease-time {max_lease};",
        f"log-facility {log_facility};",
        "",
    ]

    n = cdb.num_instances("/dhcp/subnets/subnet")
    for i in range(n):
        subnets = cdb.get("/dhcp/subnets/subnet")
        if isinstance(subnets, list):
            subnet = subnets[i]
        else:
            subnet = subnets
        net  = subnet.get("net", "")
        mask = subnet.get("mask", "")
        lines.append(f"subnet {net} netmask {mask} {{")
        rng = subnet.get("range", {})
        if rng:
            low = rng.get("low-addr", "")
            hi  = rng.get("hi-addr", "")
            if low:
                lines.append(f"  range {low} {hi};")
        routers = subnet.get("routers", "")
        if routers:
            lines.append(f"  option routers {routers};")
        ml = subnet.get("max-lease-time", "")
        if ml:
            lines.append(f"  max-lease-time {ml};")
        lines.append("}")
        lines.append("")

    content = "\n".join(lines)
    with open(OUT_FILE, "w") as f:
        f.write(content)
    print(f"[subscriber] dhcpd.conf を更新しました:\n{content}")


def on_dhcp_changed(changed_paths):
    print(f"[subscriber] 変更を検知: {changed_paths}")
    write_dhcpd_conf(cdb)


if __name__ == "__main__":
    os.makedirs(DB_DIR, exist_ok=True)
    cdb = CDB(db_dir=DB_DIR)

    # /dhcp 以下の変更を購読
    cdb.subscribe("/dhcp", on_dhcp_changed)

    # 起動時に一度生成
    write_dhcpd_conf(cdb)

    print("[subscriber] 変更を待機中... (Ctrl+C で終了)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[subscriber] 終了")
