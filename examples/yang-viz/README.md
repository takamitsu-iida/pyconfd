# YANG Model Visualizer

pyconfd の YANG パーサーを使って `.yang` ファイルをブラウザ上でインタラクティブに可視化する Web ダッシュボードです。

## スクリーンショット

```
┌──────────────────────────────────────────────────────────────────┐
│ YANG Visualizer  [hosts] [dhcpd] [recipe]    全展開 全折畳 画面合わせ 🔍 │
├────────────────────────────────────────────────┬─────────────────┤
│                                                │ ノード詳細       │
│  ● hosts (module)                              │ ────────────    │
│  └─● hosts (container)                         │ interface       │
│    └─● host (list) ──── key: name              │ [list]          │
│      ├─● name  (leaf: string)                  │                 │
│      ├─● domain (leaf: string)                 │ 型: list        │
│      ├─● defgw  (leaf: ipv4-address)           │ key: name       │
│      └─● interfaces (container)                │ path: hosts/... │
│        └─● interface (list) ─ key: name        │                 │
│          ├─● name    (leaf: string)             │                 │
│          ├─● ip      (leaf: ipv4-address)       │                 │
│          ├─● mask    (leaf: ipv4-address)       │                 │
│          └─● enabled (leaf: boolean)            │                 │
├────────────────────────────────────────────────┴─────────────────┤
│ ● container  ● list  ● leaf  ● leaf-list  ● choice  ● typedef   │
└──────────────────────────────────────────────────────────────────┘
```

## ファイル構成

```
examples/yang-viz/
├── yang_viz_server.py   # Python HTTP サーバー (標準ライブラリのみ)
├── index.html           # D3.js v7 フロントエンド
└── README.md            # このファイル
```

## 動作要件

- Python 3.9 以上
- 外部ライブラリ不要（標準ライブラリのみ）
- フロントエンドは CDN から D3.js v7 を読み込む（インターネット接続が必要）

## 使い方

### 基本

```bash
cd examples/yang-viz

# 単一ディレクトリの .yang ファイルを読み込む
python yang_viz_server.py --yang-dir ../hosts
```

ブラウザで `http://127.0.0.1:8080/` を開きます。

### 複数ディレクトリを一括ロード

```bash
python yang_viz_server.py \
    --yang-dir ../hosts \
    --yang-dir ../dhcpd \
    --yang-dir ../recipe
```

### オプション

| オプション | デフォルト | 説明 |
|------------|-----------|------|
| `--yang-dir DIR` | `.` | YANG ファイルのディレクトリ（複数指定可） |
| `--port PORT` | `8080` | HTTP ポート番号 |
| `--host HOST` | `127.0.0.1` | バインドアドレス |

```bash
# ポートを変えて起動
python yang_viz_server.py --yang-dir ../hosts --port 9090

# 全インターフェースでリッスン (LAN からのアクセスを許可する場合)
python yang_viz_server.py --yang-dir ../hosts --host 0.0.0.0
```

## API

ダッシュボードは以下の REST API を内部で使用しています。`curl` や `jq` で直接叩くことも可能です。

| エンドポイント | 説明 |
|---------------|------|
| `GET /` | ダッシュボード HTML |
| `GET /api/schema` | 全モジュールのスキーマを JSON で返す |
| `GET /api/schema/<module>` | 指定モジュールのスキーマを JSON で返す |

```bash
# 全モジュール一覧
curl http://127.0.0.1:8080/api/schema | python3 -m json.tool | head -20

# 特定モジュールのスキーマ
curl http://127.0.0.1:8080/api/schema/hosts | python3 -m json.tool
```

## 画面の使い方

### ツリー操作

| 操作 | 動作 |
|------|------|
| ノードをクリック | 子ノードを展開 / 折り畳み |
| 「全展開」ボタン | すべてのノードを展開 |
| 「全折畳」ボタン | ルート直下だけ残して折り畳み |
| 「画面合わせ」ボタン | ツリー全体が収まるようズーム調整 |
| マウスホイール | ズームイン / ズームアウト |
| ドラッグ | パン（画面移動） |

### ノード詳細

ノードをクリックすると右側の詳細パネルに以下の情報が表示されます。

- ノード名・種別バッジ
- description（説明文）
- type（データ型）
- key（リストのキー）
- mandatory / default
- namespace / prefix / revision（module ノード）
- スキーマパス

### ノード検索

右上の検索ボックスにキーワードを入力すると、**名前・description・型** でフィルタリングされます。一致したノードは黄色でハイライトされ、祖先ノードが自動的に展開されます。

## ノードの色

| 色 | ノード種別 |
|----|-----------|
| 青紫 | module / submodule |
| 青 | container |
| 緑 | list |
| 黄 | leaf |
| 赤 | leaf-list |
| 紫 | choice / case |
| シアン | grouping / uses |
| テキスト色 | typedef |
| 橙 | rpc |
| ピンク | notification |

## 自分の YANG ファイルを使う

任意のディレクトリに `.yang` ファイルを置いて `--yang-dir` で指定するだけで動作します。

```bash
# 例: プロジェクトの yang/ ディレクトリを可視化
python examples/yang-viz/yang_viz_server.py --yang-dir /path/to/your/yang
```
