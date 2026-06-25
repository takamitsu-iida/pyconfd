# examples/recipe — 料理レシピ管理デモ

ネットワークやITインフラとは無関係な「料理レシピ」を題材にして、
pyconfd の主要機能と YANG モデルの概念を学習できるチュートリアルです。

---

## ディレクトリ構成

```
examples/recipe/
├── demo.py                総合デモスクリプト
├── recipe_subscriber.py   CDB サブスクリプション + recipes.md 自動生成
├── recipe.yang            レシピ管理の YANG モデル
└── confd-cdb/
    └── running.json       CDB 永続化ファイル (初期レシピ入り)
```

---

## `recipe.yang` — YANG データモデル

```
module recipe
  └─ container recipes
       └─ list recipe              (key: "name")
            ├─ leaf name           (string)
            ├─ leaf cuisine        (enumeration: japanese/italian/chinese/french/other)
            ├─ leaf difficulty     (enumeration: easy/medium/hard)
            ├─ leaf servings       (uint32, range 1..20, default 2)
            ├─ leaf prep-time      (uint32, 分)
            ├─ leaf cook-time      (uint32, 分)
            ├─ leaf calories       (uint32, kcal/人前)
            ├─ leaf description    (string)
            ├─ container ingredients
            │    └─ list ingredient  (key: "name")
            │         ├─ leaf name    (string)
            │         ├─ leaf amount  (string)
            │         └─ leaf optional (boolean, default false)
            └─ container steps
                 └─ list step      (key: "order")
                      ├─ leaf order       (uint32, range 1..100)
                      └─ leaf instruction (string)
```

### このモデルで学べる YANG の主要機能

| YANG 機能 | どこで使われているか |
|---|---|
| `typedef` による型再利用 | `difficulty`、`cuisine-type` |
| `enumeration` | `difficulty`、`cuisine` |
| `range` 制約 | `servings` (1..20)、`step/order` (1..100) |
| `default` 値 | `servings`、`difficulty`、`cuisine`、`optional` |
| `boolean` | `ingredient/optional` |
| ネストした `list` | `recipe` の中に `ingredient`、`step` |
| 複数の `list` キー | (dhcpd 例と対比: こちらは単一キー) |

---

## デモスクリプトの説明

### `demo.py` — 総合デモスクリプト

#### 実行方法

```bash
cd examples/recipe
python demo.py
```

#### 動作ステップ

| ステップ | 内容 |
|---|---|
| 1 | `recipe.yang` を YANG パーサーで読み込む |
| 2 | CDB を初期化する |
| 3 | `/recipes` への CDB サブスクリプションを登録する |
| 4 | MAAPI トランザクションでレシピ3件を書き込む |
| 5 | CDB の running データストアをダンプして内容を確認する |
| 6 | YANG バリデーション制約の説明を表示する |
| 7 | candidate → commit のトランザクション動作を示す |
| 8 | NETCONF サーバーを起動する (TCP 2022 / SSH 830) |
| 9 | CLI サーバーを起動する (SSH 2222) |
| 10 | Python 内蔵クライアントで NETCONF `get-config` をテストする |

---

### `recipe_subscriber.py` — CDB サブスクリプション + Markdown 生成

CDB の `/recipes` 以下が変更されるたびに `recipes.md` を再生成します。

#### 実行方法

```bash
cd examples/recipe
python recipe_subscriber.py
```

別ターミナルで `demo.py` を実行するか、NETCONF や CLI でレシピを変更すると、
`recipes.md` が自動的に更新されます。

---

## NETCONF 接続例

### ncclient (Python)

```python
from ncclient import manager

with manager.connect(
    host="127.0.0.1", port=830,
    username="admin", password="admin",
    hostkey_verify=False,
) as m:
    reply = m.get_config(source="running")
    print(reply)
```

### CLI

```bash
ssh -p 2222 admin@localhost
# パスワード: admin
```

---

## pyconfd チュートリアルとしてのポイント

このデモは「なぜ YANG + NETCONF + CDB を使うのか」を、
馴染みやすいドメイン（料理レシピ）で示します。

- **YANG のバリデーション** — `difficulty` に `"unknown"` を入れようとしても
  YANG モデルには `easy / medium / hard` しかないことがすぐ分かる
- **型安全な設定管理** — JSON の `"servings": 99` は YANG の `range "1..20"` 制約に違反する
- **トランザクション** — 複数レシピをまとめて追加・削除して一度に commit できる
- **サブスクリプション** — レシピが変わると自動で `recipes.md` が更新される副作用
- **NETCONF** — curl や REST ではなく XML + RPC でデータを取得・変更する
