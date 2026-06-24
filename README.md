# pyconfd

Cisco ConfD と同等の機能を Python で実装したライブラリです。
ConfD はオープンソースではないため、その主要機能を標準ライブラリのみで再実装しています。

## 機能

| 機能 | 説明 |
|------|------|
| **YANG パーサー** | YANG 1.0/1.1 モジュールを読み込みスキーマツリーを構築 |
| **CDB** | running / candidate / startup / operational の4データストア。JSON ファイルにパーシスト |
| **CDB サブスクリプション** | パスプレフィックスで変更通知を受け取るコールバック機構 |
| **MAAPI** | トランザクション付きの設定読み書き API |
| **NETCONF サーバー** | RFC 6241 準拠の TCP サーバー (ポート 2022) |
| **CLI サーバー** | Telnet ベースの対話式 CLI (ポート 2023)。C-style / J-style を選択可能 |

## ディレクトリ構成

```
pyconfd/
├── pyconfd/
│   ├── __init__.py          # パッケージエントリポイント
│   ├── yang_parser.py       # YANG パーサー
│   ├── cdb.py               # 設定データベース (CDB)
│   ├── maapi.py             # 管理 API (MAAPI)
│   ├── netconf_server.py    # NETCONF TCP サーバー
│   └── cli_server.py        # CLI TCP サーバー (Telnet)
├── examples/
│   └── dhcpd/
│       ├── dhcpd.yang           # サンプル YANG モデル (DHCP サーバー設定)
│       ├── demo.py              # 総合デモスクリプト
│       └── dhcpd_subscriber.py  # CDB サブスクリプションのデモ
├── tests/
│   └── test_pyconfd.py      # テストスイート
└── pyproject.toml
```

## 動作要件

- Python 3.9 以上
- 外部ライブラリ不要 (標準ライブラリのみ)
- テスト実行には `pytest` が必要

## クイックスタート

### インストール

```bash
git clone https://github.com/your-org/pyconfd.git
cd pyconfd
pip install -e .
```

### デモを実行する

YANG パーサー・CDB・MAAPI・NETCONF サーバーをまとめて確認できます。

```bash
cd examples/dhcpd
python demo.py
```

別ターミナルから NETCONF や CLI で接続することも可能です。

```bash
# NETCONF hello + get-config を手動で試す
nc 127.0.0.1 2022

# CLI に Telnet で接続する
telnet 127.0.0.1 2023
```

### テストを実行する

```bash
pip install pytest
python -m pytest tests/ -v
```

## 各コンポーネントの使い方

### YANG パーサー

```python
from pyconfd.yang_parser import load_yang

root = load_yang("dhcpd.yang")
print(root.name)           # "dhcpd"
print(root.namespace)      # "http://example.com/ns/dhcpd"

node = root.find_path("/dhcp/default-lease-time")
print(node.data_type)      # "uint32"
print(node.default)        # "600"
```

### CDB (設定データベース)

```python
from pyconfd.cdb import CDB

cdb = CDB(db_dir="./confd-cdb")

# 書き込み (candidate → commit で running に反映)
cdb.set("/dhcp/default-lease-time", 600)
cdb.commit()

# 読み取り
val = cdb.get("/dhcp/default-lease-time")  # 600

# サブスクリプション
def on_change(changed_paths):
    print("変更:", changed_paths)

cdb.subscribe("/dhcp", on_change)
```

### MAAPI (トランザクション API)

```python
from pyconfd.cdb import CDB
from pyconfd.maapi import MAAPI

cdb = CDB()
maapi = MAAPI(cdb)

# with ブロックを抜けると自動コミット
with maapi.start_write_trans() as t:
    t.set("/dhcp/default-lease-time", 300)
    t.set("/dhcp/max-lease-time", 3600)
    t.create("/dhcp/subnets/subnet", {"net": "192.168.1.0", "mask": "255.255.255.0"})

# 例外が発生した場合は自動アボート
```

### NETCONF サーバー

```python
from pyconfd.cdb import CDB
from pyconfd.netconf_server import NetconfServer

cdb = CDB()
server = NetconfServer(cdb, host="127.0.0.1", port=2022)
server.start()   # バックグラウンドスレッドで起動
```

対応 NETCONF オペレーション:

| オペレーション | 説明 |
|----------------|------|
| `<get>` | running + operational を返す |
| `<get-config>` | 指定データストアの設定を返す |
| `<edit-config>` | 設定を編集する |
| `<commit>` | candidate を running に反映する |
| `<discard-changes>` | candidate への変更を破棄する |
| `<lock>` / `<unlock>` | ロック (スタブ) |
| `<close-session>` | セッションを切断する |
| `<validate>` | バリデーション (スタブ) |

### CLI サーバー

```python
from pyconfd.cdb import CDB
from pyconfd.cli_server import CLIServer

cdb = CDB()
cli = CLIServer(
    cdb,
    host="127.0.0.1",
    port=2023,
    style="c",          # "c" = Cisco XR 風 / "j" = Juniper 風
    hostname="myrouter",
    username="admin",
)
cli.start()   # バックグラウンドスレッドで起動
```

接続するには:

```bash
telnet 127.0.0.1 2023
```

#### C-style コマンド

**オペレーショナルモード** (`admin@myrouter> `)

| コマンド | 説明 |
|----------|------|
| `show running-config [path]` | running 設定を表示 |
| `show candidate-config [path]` | candidate 設定を表示 |
| `configure [terminal]` | コンフィグモードに移行 |
| `exit` / `quit` | 切断 |
| `help` / `?` | ヘルプ表示 |

**コンフィグモード** (`admin@myrouter(config)# `)

| コマンド | 説明 |
|----------|------|
| `show [path]` | candidate 設定を表示 |
| `set <path> <value>` | リーフ値をセット |
| `no <path>` | ノードを削除 |
| `commit [check\|and-quit]` | candidate を running に反映 |
| `abort` / `discard` | 変更を破棄 |
| `exit` / `end` | コンフィグモードを抜ける |

#### J-style コマンド

**オペレーショナルモード** (`admin@myrouter> `)

| コマンド | 説明 |
|----------|------|
| `show configuration [path]` | running 設定を表示 |
| `configure` | コンフィグモードに移行 |
| `exit` / `quit` | 切断 |

**コンフィグモード** (`[edit]\nadmin@myrouter% `)

| コマンド | 説明 |
|----------|------|
| `show [path]` | candidate 設定を表示 |
| `set <path> <value>` | リーフ値をセット |
| `delete <path>` | ノードを削除 |
| `commit` | candidate を running に反映 |
| `rollback` / `discard` | 変更を破棄 |
| `exit` / `quit` | コンフィグモードを抜ける |

#### セッション例 (C-style)

```
admin@myrouter> show running-config
dhcp {
  default-lease-time 600;
  max-lease-time 7200;
}
admin@myrouter> configure terminal

Entering configuration mode.
admin@myrouter(config)# set /dhcp/default-lease-time 999
[ok]
admin@myrouter(config)# commit
Commit complete.
[ok]
admin@myrouter(config)# end

Leaving configuration mode.
admin@myrouter>
```

## ConfD との主な違い

| 項目 | ConfD | pyconfd |
|------|-------|---------|
| ライセンス | 商用 (Cisco) | MIT |
| 実装言語 | C / Erlang | Python |
| NETCONF transport | SSH / TCP | TCP のみ |
| CLI | Cisco XR / Juniper スタイル | C-style / J-style (Telnet) |
| `.fxs` コンパイル | 必要 (`confdc`) | 不要 (実行時パース) |
| YANG 対応範囲 | 完全 | 主要構文のみ |
| スケーラビリティ | 商用グレード | プロトタイプ相当 |

## ライセンス

MIT License
