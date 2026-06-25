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
| **NETCONF サーバー (TCP)** | RFC 6241 準拠の生 TCP サーバー (ポート 2022)。既存ツールとの相互接続や内部テストに使用 |
| **NETCONF SSH サーバー** | RFC 6242 準拠の SSH トランスポートサーバー (ポート 830)。ncclient / Ansible から標準接続可能 |
| **CLI サーバー** | SSH ベースの対話式 CLI (ポート 2222)。C-style / J-style を選択可能 |

## ディレクトリ構成

```
pyconfd/
├── pyconfd/
│   ├── __init__.py             # パッケージエントリポイント
│   ├── yang_parser.py          # YANG パーサー / YangSchemaRegistry
│   ├── cdb.py                  # 設定データベース (CDB)
│   ├── maapi.py                # 管理 API (MAAPI)
│   ├── netconf_server.py       # NETCONF TCP サーバー
│   ├── netconf_ssh_server.py   # NETCONF SSH サーバー (RFC 6242)
│   ├── ssh_server.py           # SSH CLI サーバー
│   └── cli_server.py           # CLI コアロジック
├── examples/
│   ├── dhcpd/
│   │   ├── dhcpd.yang           # サンプル YANG モデル (DHCP サーバー設定)
│   │   ├── demo.py              # 総合デモスクリプト
│   │   └── dhcpd_subscriber.py  # CDB サブスクリプションのデモ
│   ├── hosts/
│   │   ├── hosts.yang           # サンプル YANG モデル (ネットワークホスト管理)
│   │   ├── demo.py              # 総合デモスクリプト
│   │   └── hosts_subscriber.py  # CDB サブスクリプションのデモ
│   └── recipe/
│       ├── recipe.yang          # サンプル YANG モデル (料理レシピ管理)
│       ├── demo.py              # 総合デモスクリプト (YANG チュートリアル向け)
│       └── recipe_subscriber.py # CDB サブスクリプション + recipes.md 自動生成
├── tests/
│   └── test_pyconfd.py      # テストスイート
└── pyproject.toml
```

## 動作要件

- Python 3.9 以上
- `asyncssh` — SSH CLI サーバーおよび NETCONF SSH サーバーに必要
- テスト実行には `pytest` が必要

```bash
pip install asyncssh
```

## クイックスタート

### インストール

```bash
git clone https://github.com/takamitsu-iida/pyconfd.git
cd pyconfd
pip install -e .
```

### デモを実行する

#### dhcpd — DHCP サーバー設定管理

YANG パーサー・CDB・MAAPI・NETCONF サーバーをまとめて確認できます。

```bash
cd examples/dhcpd
python demo.py
```

#### hosts — ネットワークホスト管理

ConfD の公式サンプル (`hst.yang`) を参考にしたホスト・インターフェース管理デモです。

```bash
cd examples/hosts
python demo.py
```

#### recipe — 料理レシピ管理 (YANG チュートリアル)

ネットワーク知識不要の「料理レシピ」を題材に、YANG の主要機能を学べるチュートリアルです。
`typedef`・`enumeration`・`range` 制約・ネストした `list`・`boolean` の使い方を確認できます。

```bash
cd examples/recipe
python demo.py
```

レシピ変更時に `recipes.md` を自動生成するサブスクライバーを単独で起動することもできます。

```bash
cd examples/recipe
python recipe_subscriber.py
```

別ターミナルから NETCONF や CLI で接続して動作を確認します。

```bash
# NETCONF (生TCP) を手動で試す
nc 127.0.0.1 2022

# NETCONF SSH に ncclient で接続する
python3 -c "
from ncclient import manager
with manager.connect(host='127.0.0.1', port=830,
                     username='admin', password='admin',
                     hostkey_verify=False) as m:
    print(m.get_config(source='running'))
"

# CLI に SSH で接続する
ssh -p 2222 admin@localhost
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

### YANG ディレクトリ一括ロード

ディレクトリ内のすべての `.yang` ファイルを一度に読み込み、`YangSchemaRegistry` に登録できます。
レジストリは NETCONF `<hello>` の capability 告知にも使われます。

```python
from pyconfd.yang_parser import YangSchemaRegistry

# ディレクトリ内の .yang を全て読み込む
registry = YangSchemaRegistry.from_dir("./yang-modules")

# サブディレクトリも再帰的に探索する場合
registry = YangSchemaRegistry.from_dir("./yang-modules", recursive=True)

# 個別モジュールを追加
from pyconfd.yang_parser import load_yang
registry.add(load_yang("extra.yang"))

# モジュール名で検索
mod = registry.get("dhcpd")

# NETCONF capability URI 一覧 (hello で告知する内容)
for uri in registry.capability_uris():
    print(uri)
# → urn:ietf:params:xml:ns:yang:dhcpd?module=dhcpd&revision=2024-01-01
```

### NETCONF サーバー (TCP)

```python
from pyconfd.cdb import CDB
from pyconfd.netconf_server import NetconfServer

cdb = CDB()
server = NetconfServer(cdb, host="127.0.0.1", port=2022)
server.start()   # バックグラウンドスレッドで起動
```

### NETCONF SSH サーバー (RFC 6242)

ncclient や Ansible の `ansible_connection: netconf` から標準 SSH 接続できます。

```python
from pyconfd.cdb import CDB
from pyconfd.netconf_ssh_server import NetconfSSHServer
from pyconfd.yang_parser import YangSchemaRegistry

cdb = CDB()
registry = YangSchemaRegistry.from_dir("./yang-modules")

server = NetconfSSHServer(
    cdb,
    host="127.0.0.1",
    port=830,
    users={"admin": "admin"},
    schema_registry=registry,   # capability を自動告知
)
server.start()
```

```python
# ncclient から接続する例
from ncclient import manager
with manager.connect(
    host="127.0.0.1", port=830,
    username="admin", password="admin",
    hostkey_verify=False,
) as m:
    cfg = m.get_config(source="running")
    print(cfg)
```

対応 NETCONF オペレーション:

| オペレーション | 説明 |
|----------------|------|
| `<get>` | running + operational を返す。subtree フィルター対応 |
| `<get-config>` | 指定データストアの設定を返す。subtree フィルター対応 |
| `<edit-config>` | 設定を編集する |
| `<commit>` | candidate を running に反映する |
| `<discard-changes>` | candidate への変更を破棄する |
| `<lock>` / `<unlock>` | ロック (スタブ) |
| `<close-session>` | セッションを切断する |
| `<validate>` | バリデーション (スタブ) |

#### subtree フィルター (RFC 6241 section 6.4)

`<get>` / `<get-config>` の `<filter type="subtree">` に対応しています。

| フィルター種別 | XML 例 | 動作 |
|----------------|--------|------|
| フィルターなし | `<get-config><source><running/></source></get-config>` | 全設定を返す |
| 選択ノード | `<filter><dhcp/></filter>` | `dhcp` コンテナ全体を返す |
| 包含ノード | `<filter><dhcp><default-lease-time/></dhcp></filter>` | 指定リーフのみ返す |
| 内容マッチ | `<filter><dhcp><subnets><subnet><net>192.168.1.0</net></subnet></subnets></dhcp></filter>` | キーが一致するリストエントリのみ返す |
| 空フィルター | `<filter/>` | 空の `<data/>` を返す |

```python
# ncclient での subtree フィルター使用例
from ncclient import manager
from ncclient.xml_ import to_ele

with manager.connect(host="127.0.0.1", port=830,
                     username="admin", password="admin",
                     hostkey_verify=False) as m:

    # dhcp コンテナ全体を取得
    filt = to_ele("<filter><dhcp xmlns='http://example.com/ns/dhcpd'/></filter>")
    print(m.get_config(source="running", filter=filt))

    # 特定サブネットのみ取得 (内容マッチ)
    filt = to_ele("""
    <filter>
      <dhcp xmlns='http://example.com/ns/dhcpd'>
        <subnets><subnet><net>192.168.1.0</net></subnet></subnets>
      </dhcp>
    </filter>""")
    print(m.get_config(source="running", filter=filt))
```

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
| 実装言語 | C / Erlang | Python |
| NETCONF transport | SSH (RFC 6242) | SSH (RFC 6242) / 生 TCP |
| CLI | Cisco XR / Juniper スタイル | C-style / J-style (SSH) |
| `.fxs` コンパイル | 必要 (`confdc`) | 不要 (実行時パース) |
| YANG 対応範囲 | 完全 | 主要構文のみ |
| スケーラビリティ | 商用グレード | プロトタイプ相当 |
| モック応答定義 | なし | YAML/JSON シナリオファイル対応 |

## モックシナリオ定義 (Wiremock スタイル)

`ScenarioMatcher` を使うと、YAML/JSON ファイルに「このリクエスト → この固定応答」を
定義できます。CDB を参照せずに固定 XML を返すため、CI テストや Ansible Playbook の
動作確認を実機・ConfD なしで行えます。

### シナリオファイル例 (YAML)

```yaml
scenarios:
  # get-config running で dhcp フィルターが来たら固定 XML を返す
  - name: "mock: get-config running filter=dhcp"
    match:
      operation: get-config
      source: running
      filter_tag: dhcp        # filter に <dhcp> タグが含まれる場合のみマッチ
    response:
      body: |
        <data xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
          <dhcp xmlns="http://example.com/ns/dhcpd">
            <default-lease-time>600</default-lease-time>
          </dhcp>
        </data>

  # edit-config は常に <ok/> を返す (書き込みを無視するモック)
  - name: "mock: edit-config always ok"
    match:
      operation: edit-config
    response:
      ok: true

  # レスポンスを外部ファイルから読み込む例
  - name: "mock: get from file"
    match:
      operation: get
    response:
      file: responses/custom_get.xml   # シナリオファイルからの相対パス

  # disabled: true で一時無効化
  - name: "unused scenario"
    disabled: true
    match:
      operation: validate
    response:
      ok: true
```

### match フィールド一覧

| フィールド | 説明 | 省略時 |
|---|---|---|
| `operation` | `get` / `get-config` / `edit-config` / `validate` / `*` | `*`（任意） |
| `source` | `running` / `candidate` | 任意にマッチ |
| `filter_tag` | `<filter>` 内に指定タグが含まれる場合のみマッチ | 常にマッチ |

### Python からの使い方

```python
from pyconfd import CDB, NetconfSSHServer, ScenarioMatcher

cdb = CDB()
matcher = ScenarioMatcher.from_file("mock-scenarios.yaml")
print(matcher.scenario_names())

server = NetconfSSHServer(
    cdb,
    port=830,
    scenario_matcher=matcher,   # マッチしたら固定応答、しなければ CDB にフォールバック
)
server.start()
```

> **PyYAML について**: `.yaml` / `.yml` ファイルの読み込みには `pip install pyyaml` が必要です。
> `.json` 形式であれば標準ライブラリのみで動作します。
