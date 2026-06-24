# examples/hosts — ネットワークホスト管理デモ

ConfD の `examples.confd/intro/python/6-config` (hst.yang) を参考にした
ネットワークホスト設定管理のサンプルです。
各ホストが複数のネットワークインターフェースを持つ構成を pyconfd で管理します。

---

## ディレクトリ構成

```
examples/hosts/
├── demo.py               総合デモスクリプト
├── hosts_subscriber.py   CDB サブスクリプション + hosts.conf 自動生成
├── hosts.yang            ホスト・インターフェースの YANG モデル
└── confd-cdb/
    └── running.json      CDB 永続化ファイル (demo.py 実行後に生成)
```

---

## `hosts.yang` — YANG データモデル

ConfD の `hst.yang` をベースに、`tailf:` 拡張を除いて pyconfd 向けに作成したモデルです。

```
module hosts
  └─ container hosts
       └─ list host                   (key: "name")
            ├─ leaf name              (string)
            ├─ leaf domain            (string)
            ├─ leaf defgw             (ipv4-address)
            └─ container interfaces
                 └─ list interface    (key: "name")
                      ├─ leaf name    (string)
                      ├─ leaf ip      (ipv4-address)
                      ├─ leaf mask    (ipv4-address)
                      └─ leaf enabled (boolean, default true)
```

ConfD 版との主な違い:

| ConfD hst.yang | hosts.yang |
|---|---|
| `tailf:callpoint` でデータプロバイダを登録 | CDB に直接保持 (プロバイダ不要) |
| `max-elements 64` 制限あり | 制限なし |
| `tailf-common` import | 依存なし |

---

## `demo.py` — 総合デモスクリプト

### 実行方法

```bash
cd examples/hosts
python demo.py
```

### 動作ステップ

#### ステップ 1: YANG パーサー

```python
yang_root = load_yang("hosts.yang")
```

`hosts.yang` をパースして `YangNode` ツリーを構築し、
`/hosts/host` のキー一覧と子ノードを表示します。

#### ステップ 2: CDB 初期化

```python
cdb = CDB(db_dir="confd-cdb")
```

`confd-cdb/running.json` があれば自動ロードします。

#### ステップ 3: CDB サブスクリプション登録

```python
cdb.subscribe("/hosts", on_hosts_changed)
```

`/hosts` 以下の変更があるたびに `on_hosts_changed(changed_paths)` が呼ばれます。

#### ステップ 4: MAAPI トランザクションでホストを登録

2 台のホストを複数のトランザクションに分けて登録します。

```python
# buzz: 2 インターフェース (両方 enabled)
with maapi.start_write_trans() as t:
    t.create("/hosts/host", {"name": "buzz"})
    t.set("/hosts/host[name=buzz]/domain", "tail-f.com")
    t.set("/hosts/host[name=buzz]/defgw",  "192.168.1.1")

with maapi.start_write_trans() as t:
    t.create("/hosts/host[name=buzz]/interfaces/interface", {"name": "eth0"})
    t.set("/hosts/host[name=buzz]/interfaces/interface[name=eth0]/ip",   "192.168.1.61")
    ...

# woody: 1 インターフェース (enabled=False)
```

登録後、CDB から読み直して全ホストの情報をコンソールに表示します。

**実行結果の例:**

```
  登録済みホスト:
    Host       buzz  domain=tail-f.com  defgw=192.168.1.1
         iface:    eth0     192.168.1.61    255.255.255.0  enabled=True
         iface:    eth1       10.77.1.44      255.255.0.0  enabled=True
    Host      woody  domain=tail-f.com  defgw=10.0.0.1
         iface:    eth0        10.0.0.55        255.0.0.0  enabled=False
```

#### ステップ 5: CDB ダンプ

```python
print(cdb.dump("running"))
```

`running` データストア全体を JSON で表示します。

#### ステップ 6/6b: NETCONF・CLI サーバー起動

```python
netconf_srv = NetconfServer(cdb, host="127.0.0.1", port=2022)
cli_srv     = CLIServer(cdb, host="127.0.0.1", port=2023,
                        style="c", hostname="router", schema=yang_root)
```

CLI サーバーに `schema=yang_root` を渡しているため、
YANG モデルに基づいたコンテキスト移動と前方一致ナビゲーションが有効になります。

#### ステップ 7: NETCONF get-config テスト

`demo.py` 自身がインライン NETCONF クライアントとして動作します。

```
TCP 接続 → <hello> 交換 → <get-config><source><running/></source></get-config> 送信
→ XML レスポンスを表示 → <close-session/>
```

**get-config レスポンス例 (整形):**

```xml
<rpc-reply message-id="1" xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <data>
    <hosts>
      <host>
        <name>buzz</name>
        <domain>tail-f.com</domain>
        <defgw>192.168.1.1</defgw>
        <interfaces>
          <interface>
            <name>eth0</name>
            <ip>192.168.1.61</ip>
            <mask>255.255.255.0</mask>
            <enabled>True</enabled>
          </interface>
          ...
        </interfaces>
      </host>
      ...
    </hosts>
  </data>
</rpc-reply>
```

#### ステップ 8: 待機

サーバーを起動したまま Ctrl+C を待ちます。

### サーバー起動後の操作

| プロトコル | 接続先 | コマンド例 |
|---|---|---|
| NETCONF | `127.0.0.1:2022` | `nc 127.0.0.1 2022` |
| CLI (Telnet) | `127.0.0.1:2023` | `telnet 127.0.0.1 2023` |

**CLI 操作例:**

```
router> show running-config          ← running 設定を表示
router> configure
router(config)# hosts                ← hosts コンテナに移動
router(config-hosts)# host buzz      ← buzz ホストのコンテキストへ
router(config-hosts-host-buzz)# defgw 192.168.1.254   ← 値を変更
router(config-hosts-host-buzz)# commit
router(config-hosts-host-buzz)# exit
```

---

## `hosts_subscriber.py` — CDB サブスクライバ

### 実行方法

```bash
# ターミナル A: サブスクライバを先に起動
cd examples/hosts
python hosts_subscriber.py

# ターミナル B: demo.py でホストを登録
python demo.py
```

### 動作の概要

`/hosts` 以下の変更を監視し、コミットのたびに `hosts.conf` を自動生成します。

#### 起動時の処理

1. `CDB(db_dir="confd-cdb")` で既存設定をロード
2. `cdb.subscribe("/hosts", on_hosts_changed)` で変更コールバックを登録
3. `write_hosts_conf(cdb)` を呼んで起動時に一度ファイルを生成
4. `while True: time.sleep(1)` で変更通知を待機

#### `write_hosts_conf(cdb)` — hosts.conf 生成

CDB から全ホストのインターフェースを読み取り、UNIX の `/etc/hosts` ライクな形式に変換します。

- `enabled=False` のインターフェースはスキップ
- ドメインがある場合は `<name>.<domain>` の FQDN を生成

**生成される `hosts.conf` の例:**

```
# hosts.conf — pyconfd hosts_subscriber.py が自動生成
# フォーマット: <IP アドレス>  <FQDN>  <ホスト名>

127.0.0.1   localhost

192.168.1.61       buzz.tail-f.com                buzz  # eth0
10.77.1.44         buzz.tail-f.com                buzz  # eth1
```

> `woody` の `eth0` は `enabled=False` のため出力されません。

#### `on_hosts_changed(changed_paths)` — 変更コールバック

`cdb.subscribe()` に登録されたコールバックで、`demo.py` 等が `commit()` するたびに呼ばれます。
変更パスをコンソールに表示してから `write_hosts_conf()` を再実行します。

---

## dhcpd デモとの比較

| 項目 | dhcpd デモ | hosts デモ |
|---|---|---|
| モデルの特徴 | シングルキーリスト (`list subnet { key "net mask"; }`) | ネストしたリスト (`list host` → `list interface`) |
| 出力ファイル | `dhcpd.conf` (ISC DHCPd 形式) | `hosts.conf` (/etc/hosts 形式) |
| YANG 参考元 | オリジナル | ConfD `hst.yang` |
| enabled フィルタ | なし | `enabled=False` のエントリを除外 |
