# pyconfd パッケージ — 実装解説

このディレクトリには pyconfd の全コンポーネントが含まれています。
各モジュールが何を担当し、内部でどう動作するかをソースコードレベルで解説します。

---

## モジュール一覧

| ファイル | 役割 |
|---|---|
| `__init__.py` | パッケージ初期化・公開 API のエクスポート |
| `cdb.py` | 設定データベース (CDB) 本体 |
| `maapi.py` | トランザクション付き管理 API (MAAPI) |
| `netconf_server.py` | NETCONF サーバー (RFC 6241) |
| `cli_server.py` | CLI サーバー (Telnet ベース) |
| `yang_parser.py` | YANG 1.0/1.1 パーサー |

---

## `__init__.py` — パッケージ初期化

```python
from .netconf_server import NetconfServer
from .cli_server import CLIServer
from .maapi import MAAPI, Transaction
from .cdb import CDB
```

`pyconfd` をインポートしたとき、外部から使う 5 つのクラスだけを `__all__` でエクスポートします。
内部の低レベル関数 (`_parse_path` など) は公開しません。

---

## `cdb.py` — 設定データベース (CDB)

### 概要

ConfD の CDB に相当する **インメモリ設定データストア**です。
データは Python の `dict` ツリーとして保持し、JSON ファイルへのパーシストをサポートします。

### データストアの種類

| 名前 | 用途 |
|---|---|
| `running` | 現在稼働中の設定 (永続化対象) |
| `candidate` | 編集中の設定。`commit()` で `running` に反映 |
| `startup` | 起動時設定 (オプション) |
| `operational` | 運用データ (読み取り専用想定) |

### パス表記

`/container/list[key=val]/leaf` という XPath ライクな文字列でノードを指定します。

```
/dhcp/subnets/subnet[net=192.168.0.0][mask=255.255.255.0]/range
```

#### `_parse_path(path)` — パスのパース

`/` 区切りの文字列を `(name, keys_dict)` のリストに変換します。
述語 `[k=v]` は正規表現 `_RE_PRED` で抽出します。

```
"/dhcp/subnet[net=1.2.3.4]/range"
→ [('dhcp', None), ('subnet', {'net': '1.2.3.4'}), ('range', None)]
```

#### `_navigate(tree, parts, create=False)` — ツリーの走査

パース済みパーツを受け取り、ツリーを再帰的に辿って `(親ノード, 最終キー)` を返します。
`create=True` のとき、存在しない中間ノードを自動作成します。
リストノードは `_find_list_entry()` でキー照合して目的のエントリを特定します。

### `CDB` クラスの主要メソッド

#### 読み書き

| メソッド | 動作 |
|---|---|
| `get(path, datastore)` | 指定パスの値を返す。見つからなければ `KeyError` |
| `set(path, value, datastore)` | 指定パスに値をセットし、`_pending_changes` にジャーナルを追記 |
| `delete(path, datastore)` | 指定パスのノードを削除。リストエントリも対応 |
| `exists(path, datastore)` | パスの存在確認。内部で `get()` を呼び例外をキャッチ |
| `get_list(path, datastore)` | リストノードをそのまま返す |
| `subtree(path, datastore)` | 指定パス以下の dict ツリーを `deepcopy` して返す |

#### トランザクション

```
start_transaction()
    ↓ candidate = deepcopy(running)
set() / delete()  ←→ _pending_changes に蓄積
    ↓
commit()
    running = deepcopy(candidate)
    JSONファイルを保存 (アトミック: tmp→replace)
    サブスクライバに通知
```

`abort()` は `candidate` を `running` のコピーで上書きして変更を捨てます。

#### ファイルパーシスト

`_save(datastore)` はまず `.tmp` ファイルに書き出してから `os.replace()` で差し替えます。
これにより書き込み途中のクラッシュでもファイルが壊れません。

#### サブスクリプション

```python
cdb.subscribe("/dhcp", callback)
```

`commit()` 後に `_notify_subscribers()` が呼ばれ、変更パスが登録済みプレフィックスに前方一致するとき、コールバックが `changed_paths: list[str]` を受け取って実行されます。
コールバックは `_lock` 外で実行するため、コールバック内で再度 CDB を操作できます。

---

## `maapi.py` — 管理 API (MAAPI)

### 概要

ConfD の MAAPI に相当する **トランザクション付き CDB アクセス層**です。
`CDB` を直接操作する代わりに `MAAPI` を使うことで、トランザクションのライフサイクルを安全に管理できます。

### `Transaction` クラス

`MAAPI.start_write_trans()` が返す書き込みトランザクションです。

#### コンテキストマネージャー

```python
with maapi.start_write_trans() as t:
    t.set("/dhcp/default-lease-time", 600)
    # __exit__ で自動 commit
```

- 例外なく `__exit__` に到達した場合 → `commit()` を自動実行
- 例外が発生した場合 → `abort()` を自動実行

#### 書き込みトランザクションの初期化

コンストラクタで `writable=True` のとき `cdb.start_transaction()` を呼び、
`candidate` を `running` のコピーで初期化します。

#### `create(path, keys)` — リストエントリの作成

`_navigate()` でリストの親ノードまで辿り、キー重複チェックしてからエントリを追加します。
内部実装は `cdb.py` の低レベル関数を直接利用します。

### `MAAPI` クラス

`Transaction` のファクトリーと、トランザクションなしの簡易アクセスを提供します。

| メソッド | 動作 |
|---|---|
| `start_write_trans()` | 書き込みトランザクションを返す |
| `start_read_trans()` | 読み取り専用トランザクションを返す |
| `get(path)` | `running` から直接値を取得 (トランザクションなし) |
| `set(path, value)` | `start_write_trans()` を使い即時コミット |
| `subscribe(prefix, cb)` | `cdb.subscribe()` への委譲 |

---

## `netconf_server.py` — NETCONF サーバー

### 概要

RFC 6241 に準拠した **TCP ベースの NETCONF サーバー**です。
各クライアント接続は `NetconfSession` インスタンスが独立したスレッドで処理します。

### フレーミング

| バージョン | 区切り方式 |
|---|---|
| NETCONF 1.0 | `]]>]]>` (6 バイト固定区切り) |
| NETCONF 1.1 | チャンクフレーミング (`#<N>\n...\n##\n`) |

クライアントの `<hello>` に `base:1.1` ケーパビリティが含まれる場合、
`_use_chunked = True` に切り替えてチャンクモードで通信します。

### セッション確立フロー

```
TCP 接続
  └─ _send_hello()          サーバー側 <hello> 送信 (capabilities + session-id)
  └─ _recv_client_hello()   クライアント側 <hello> 受信 (1.1 対応確認)
  └─ メインループ:
       _recv_message() → _handle_message() → 各オペレーションハンドラー
```

### 対応 NETCONF オペレーション

| オペレーション | 実装クラス/メソッド | 動作 |
|---|---|---|
| `<get>` | `_op_get` | `running` + `operational` をマージして XML で返す |
| `<get-config>` | `_op_get_config` | 指定 datastore の設定を XML で返す |
| `<edit-config>` | `_op_edit_config` | `<config>` 要素を `_xml_to_dict()` で dict 化して CDB にマージ |
| `<commit>` | `_op_commit` | `cdb.commit()` を呼ぶ |
| `<discard-changes>` | `_op_discard_changes` | `cdb.abort()` を呼ぶ |
| `<lock>` / `<unlock>` | `_op_lock` / `_op_unlock` | スタブ (常に `<ok/>` を返す) |
| `<close-session>` | `_op_close_session` | `<ok/>` を返してループを終了 |
| `<kill-session>` | `_op_kill_session` | スタブ |
| `<validate>` | `_op_validate` | スタブ |

### XML ↔ dict 変換

- **`_dict_to_xml(d, tag, ns)`** — Python の `dict`/`list`/スカラーを XML 文字列に再帰変換します。
- **`_xml_to_dict(elem)`** — `xml.etree.ElementTree` の `Element` を `dict` に変換します。同名の要素が複数ある場合はリスト化されます。

### `NetconfServer` クラス

```python
srv = NetconfServer(cdb, host="127.0.0.1", port=2022)
srv.start()   # daemon スレッドでアクセプトループを起動
srv.stop()    # ソケットをクローズしてループを終了
```

`_accept_loop()` は `select()` で 1 秒タイムアウトをかけてポーリングし、
接続があれば `NetconfSession` を生成してデーモンスレッドに渡します。

---

## `cli_server.py` — CLI サーバー

### 概要

Telnet プロトコルを使った **対話式 CLI サーバー**です。
Cisco IOS ライク (C スタイル) と Juniper Junos ライク (J スタイル) の 2 種類の UI を持ちます。

### Telnet ネゴシエーション

`_negotiate_telnet()` で以下の 3 つのオプションを交換します。

| コマンド | 意味 |
|---|---|
| `IAC WILL ECHO` | サーバーがキー入力をエコーバック |
| `IAC WILL SGA` | Go Ahead を抑制 (文字単位通信を実現) |
| `IAC DO SGA` | クライアントにも SGA を要求 |

クライアントからの IAC 応答は `_strip_iac()` で除去してから本文バッファに格納します。

### CLI モードの構造

```
オペレーショナルモード (_in_config = False)
  └─ show running-config / show candidate-config
  └─ configure → コンフィグモードへ移行

コンフィグモード (_in_config = True)
  └─ C スタイル: set / no / commit / abort / exit / end
  └─ J スタイル: set / delete / commit / rollback / exit / quit
```

### コマンドディスパッチ

`_dispatch()` が `_in_config` フラグで C/J スタイルのハンドラーに振り分けます。
`_resolve_cmd()` によって前方一致でコマンドを解決します (`conf` → `configure` など)。

### YANG スキーマを使ったナビゲーション支援

CLI サーバーは `CLIServer(cdb, schema=load_yang("dhcpd.yang"))` のように
YANG パーサーが生成した `YangNode` ツリーをオプションで受け取ります。
`schema` は `CLISession._schema` フィールドに保持され、コマンド入力のたびに参照されます。

> **注意**: `schema=None` (デフォルト) でも動作します。その場合はスキーマチェックを
> スキップし、入力値を無条件に leaf として CDB へ書き込むフォールバック動作になります。

#### `_schema_node_at(path_segs)` — 現在コンテキストの YangNode を取得

`_config_path` のセグメントリスト (例: `["dhcp", "subnet[net=10.0.0.0]"]`) を辿り、
対応する `YangNode` を返します。セグメントのキー述語 `[k=v]` は `split("[")[0]` で除去してから
`YangNode.get_child(name)` で子ノードを検索します。

#### `_resolve_cmd()` — キーワードコマンドの前方一致解決

固定キーワード (`show`, `commit`, `exit` など) に対して前方一致で解決します。

```
"conf"  → "configure"   (一意な前方一致)
"com"   → "commit"      (一意な前方一致)
"s"     → None          (show / set など複数候補があれば解決失敗)
```

完全一致が優先され、一意に絞れない場合は `None` を返します。

### C スタイル: コンテキスト移動 (`_ios_navigate_or_set`)

Cisco IOS と同様に、コンテキストを積み上げて階層的に設定します。

```
(config)# interface eth0        → _config_path = ["interface", "eth0"]
(config-interface-eth0)# ip address 10.0.0.1
```

入力トークンが届くと以下の順序で処理されます。

1. `_schema_node_at(_config_path)` で現在コンテキストの `YangNode` を取得
2. `schema_node.get_child(node_name)` で子ノードを完全一致検索
3. 見つからない場合は `c.name.startswith(node_name)` による**前方一致**で再検索
   (一意に絞れた場合のみ採用)
4. 子ノードの `node_type` で処理を分岐:
   - `CONTAINER` / `LIST` → `_config_path` にセグメントを追加して**コンテキスト移動**
   - `LEAF` / `LEAF_LIST` → `cdb.set()` で candidate に値を書き込み
5. スキーマが `None` またはノードが見つからない場合 → **フォールバック**: leaf として書き込みを試みる

#### LIST エントリのコンテキスト移動

`list` ノードに移動する際、YANG の `key` ステートメントからキー名の一覧を取得し、
入力値でキー述語を組み立てて `_config_path` に積みます。

```python
# YANG: list subnet { key "net mask"; ... }
# 入力: subnet 10.0.0.0 255.255.255.0
keys = child_schema.keys          # → ["net", "mask"]
key_pred = "[net=10.0.0.0][mask=255.255.255.0]"
_config_path.append("subnet[net=10.0.0.0][mask=255.255.255.0]")
```

キー値が揃っていない場合は `_normalize_current_list_context()` が
CDB から不足キーを補って述語を正規化します。

### 設定ツリーのテキスト化 (`_format_tree`)

CDB から取得した `dict` ツリーを C スタイル (インデントのみ) または J スタイル (末尾セミコロン付き) のテキストに再帰変換します。
リストエントリは `_list_label()` で代表キー (`name`, `id`, `net` など) をラベルとして抽出します。

---

## `yang_parser.py` — YANG パーサー

### 概要

YANG 1.0/1.1 ファイルを読み込み、`YangNode` オブジェクトのツリーを構築します。
ConfD の `.fxs` コンパイル済みスキーマの代わりに、Python オブジェクトとしてスキーマを保持します。

### `YangNode` — スキーマノード

`@dataclass` で定義されており、以下のフィールドを持ちます。

| フィールド | 内容 |
|---|---|
| `node_type` | `NodeType` enum (MODULE, CONTAINER, LIST, LEAF など) |
| `name` | ノード名 (モジュール名・コンテナ名・リーフ名など) |
| `parent` | 親ノードへの参照 |
| `children` | 子ノードのリスト |
| `properties` | YANG ステートメントのプロパティ dict (`type`, `key`, `mandatory` など) |

`namespace`, `prefix`, `data_type`, `keys`, `mandatory`, `default`, `description` は `properties` dict を参照するプロパティとして実装されています。

### `_tokenize(text)` — トークナイザー

YANG テキストを文字単位で走査し、以下のルールでトークンリストを生成します。

- `//` 行コメント・`/* */` ブロックコメントをスキップ
- `{`, `}`, `;` を単独トークンとして切り出し
- `"..."` ダブルクォート文字列: エスケープシーケンス (`\n`, `\t`, `\"`, `\\`) を展開
- `'...'` シングルクォート文字列: エスケープなし
- その他の連続する非空白文字: 識別子・非引用値

### `YangParser._parse_stmt(parent)` — 再帰パーサー

トークンリストを消費しながら再帰的に `YangNode` ツリーを構築します。

```
keyword [arg] ;          → プロパティとして parent.properties[keyword] = arg
keyword [arg] { ... }   → 子ノード (YangNode) として parent.children に追加
```

`_NODE_TYPES` dict でキーワードを `NodeType` に変換します。
認識できないキーワードはノードを生成せず、親ノードの `properties` に文字列として格納します。

### 公開 API

```python
from pyconfd.yang_parser import load_yang

module = load_yang("dhcpd.yang")
leaf_node = module.find_path("/dhcp/default-lease-time")
print(leaf_node.data_type)   # "uint32"
```

`load_yang(path)` は `YangParser().parse_file(path)` への薄いラッパーです。

---

## コンポーネント間の依存関係

```
yang_parser.py
    ↑ (スキーマ参照)
cli_server.py ──────┐
                    ├── maapi.py ── cdb.py
netconf_server.py ──┘
```

- `cdb.py` は他モジュールに依存しない独立したコア層
- `maapi.py` は `cdb.py` のみに依存
- `netconf_server.py` / `cli_server.py` は `cdb.py` と `maapi.py` を利用
- `cli_server.py` はオプションで `yang_parser.py` のスキーマを参照して補完・ナビゲーションを行う
