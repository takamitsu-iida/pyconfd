# examples/dhcpd — DHCPd デモ

pyconfd の各コンポーネントを DHCPd 設定管理に見立てた動作確認用のサンプルです。

---

## ディレクトリ構成

```
examples/dhcpd/
├── demo.py               総合デモスクリプト
├── dhcpd_subscriber.py   CDB サブスクリプション + dhcpd.conf 自動生成
├── dhcpd.yang            DHCP 設定の YANG モデル
└── confd-cdb/
    └── running.json      CDB の永続化ファイル (初期値入り)
```

---

## `dhcpd.yang` — YANG データモデル

DHCPd サーバーの設定を定義したスキーマです。

```
module dhcpd
  └─ container dhcp
       ├─ leaf default-lease-time   (uint32, default 600)
       ├─ leaf max-lease-time       (uint32, default 7200)
       ├─ leaf log-facility         (enumeration: kern/mail/local7)
       └─ container subnets
            └─ list subnet          (key: "net mask")
                 ├─ leaf net            (ipv4-address)
                 ├─ leaf mask           (ipv4-address)
                 ├─ container range
                 │    ├─ leaf low-addr  (ipv4-address)
                 │    └─ leaf hi-addr   (ipv4-address)
                 ├─ leaf routers        (string)
                 └─ leaf max-lease-time (uint32)
```

`list subnet` のキーは `net` と `mask` の 2 つです。
CLI でサブネットコンテキストに入る際は両方の値が必要になります。

---

## `confd-cdb/running.json` — 初期設定データ

CDB の `running` データストアの永続化ファイルです。
`CDB(db_dir="confd-cdb")` を初期化すると自動的に読み込まれます。
`demo.py` または `dhcpd_subscriber.py` を実行すると、このファイルが上書き更新されます。

---

## `demo.py` — 総合デモスクリプト

### 実行方法

```bash
cd examples/dhcpd
python demo.py
```

### 動作ステップ

`demo.py` は以下のステップを順番に実行します。

#### ステップ 1: YANG パーサー

```python
yang_root = load_yang("dhcpd.yang")
```

`dhcpd.yang` をパースして `YangNode` ツリーを構築し、
`/dhcp` 配下のノード一覧を標準出力に表示します。
このツリーは後続の CLI サーバーにスキーマとして渡されます。

#### ステップ 2: CDB 初期化

```python
cdb = CDB(db_dir="confd-cdb")
```

`confd-cdb/running.json` が存在すれば自動ロードします。

#### ステップ 3: CDB サブスクリプション登録

```python
cdb.subscribe("/dhcp", on_change)
```

`/dhcp` 以下のパスに変更があった場合に `on_change(changed_paths)` が呼ばれるよう登録します。
コールバック内では変更されたパスのリストをログに出力します。

#### ステップ 4: MAAPI トランザクションで設定を書き込む

```python
with maapi.start_write_trans() as t:
    t.set("/dhcp/default-lease-time", 600)
    t.create("/dhcp/subnets/subnet", {"net": "192.168.1.0", "mask": "255.255.255.0"})
```

`with` ブロックを抜けると自動的に `commit()` が呼ばれ、
`running.json` が更新され、サブスクリプションコールバックが実行されます。

続けて 2 つ目のトランザクションで各サブネットの詳細 (`range`, `routers` など) を設定します。

#### ステップ 5: CDB ダンプ

```python
print(cdb.dump("running"))
```

`running` データストアの全内容を JSON 文字列として表示します。

#### ステップ 6: NETCONF サーバー起動

```python
server = NetconfServer(cdb, host="127.0.0.1", port=2022)
server.start()
```

TCP ポート 2022 で NETCONF サーバーをバックグラウンドスレッドで起動します。

#### ステップ 6b: CLI サーバー起動

```python
cli_server = CLIServer(cdb, host="127.0.0.1", port=2023, style="c",
                       hostname="pyconfd", schema=yang_root)
cli_server.start()
```

TCP ポート 2023 で Cisco IOS ライク CLI サーバーを起動します。
`schema=yang_root` を渡すことで、コンテキスト移動とノード種別の判別に YANG スキーマを活用します。

#### ステップ 7: NETCONF クライアントテスト (`_netconf_test`)

`demo.py` 自身がインライン NETCONF クライアントとして動作し、以下のシーケンスを実行します。

```
TCP 接続 (127.0.0.1:2022)
  → サーバー <hello> 受信
  → クライアント <hello> 送信
  → <get-config><source><running/></source></get-config> 送信
  → レスポンス (XML) を標準出力に表示
  → <close-session/> 送信
```

#### ステップ 8: 待機・終了

サーバーを起動したまま Ctrl+C を待ちます。
終了時に `server.stop()` と `cli_server.stop()` を呼んでサーバーを停止します。

### サーバー起動後の接続方法

| プロトコル | 接続先 | コマンド例 |
|---|---|---|
| NETCONF | `127.0.0.1:2022` | `nc 127.0.0.1 2022` |
| CLI (Telnet) | `127.0.0.1:2023` | `telnet 127.0.0.1 2023` |

CLI 接続後の操作例:

```
pyconfd> show running-config
pyconfd> configure
pyconfd(config)# dhcp default-lease-time 1200
pyconfd(config)# commit
pyconfd(config)# exit
```

---

## `dhcpd_subscriber.py` — CDB サブスクライバ

### 実行方法

```bash
# ターミナル A
python dhcpd_subscriber.py

# ターミナル B (demo.py などで設定を変更)
python demo.py
```

### 動作の概要

`dhcpd_subscriber.py` は CDB の変更を監視し、
`/dhcp` 以下が変更されるたびに `dhcpd.conf` を自動生成します。

#### 起動時の処理

1. `CDB(db_dir="confd-cdb")` で既存の設定をロード
2. `cdb.subscribe("/dhcp", on_dhcp_changed)` で変更コールバックを登録
3. `write_dhcpd_conf(cdb)` を呼んで起動時に一度 `dhcpd.conf` を生成
4. `while True: time.sleep(1)` で変更通知を待機

#### `write_dhcpd_conf(cdb)` — 設定ファイル生成

CDB の `running` データストアから以下の値を読み取り、ISC DHCPd の設定ファイル形式に変換します。

```python
default_lease = cdb.get("/dhcp/default-lease-time")   # グローバル設定
subnets       = cdb.get("/dhcp/subnets/subnet")        # サブネットリスト
```

生成される `dhcpd.conf` の例:

```
default-lease-time 600;
max-lease-time 7200;
log-facility local7;

subnet 192.168.1.0 netmask 255.255.255.0 {
  range 192.168.1.10 192.168.1.100;
  option routers 192.168.1.1;
  max-lease-time 3600;
}

subnet 10.0.0.0 netmask 255.0.0.0 {
  range 10.0.0.100 10.0.0.200;
  option routers 10.0.0.1;
}
```

#### `on_dhcp_changed(changed_paths)` — 変更コールバック

`cdb.subscribe()` に登録されたコールバックです。
変更されたパスのリストを受け取り、`write_dhcpd_conf()` を再実行します。
このコールバックは `demo.py` や他のプロセスが同じ `confd-cdb/` ディレクトリを使って
設定を変更・コミットするたびに呼び出されます。

> **注意**: `dhcpd_subscriber.py` は CDB ファイルを共有しているだけであり、
> `demo.py` とプロセス間通信は行いません。
> 同じ `confd-cdb/running.json` を読み書きすることで設定を共有します。
> ただし、複数プロセスが同時に書き込む場合のファイルロックは実装されていないため、
> 本番用途では排他制御の追加が必要です。
