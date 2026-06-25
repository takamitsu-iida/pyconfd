#!/usr/bin/env python3
"""
YANG モデル可視化 Web ダッシュボード

指定ディレクトリの .yang ファイルを読み込み、ブラウザでインタラクティブな
ツリー表示を提供します。

使用方法::

    # カレントディレクトリの .yang を読み込む
    cd examples/yang-viz
    python yang_viz_server.py --yang-dir ../hosts

    # 複数ディレクトリを一括ロード
    python yang_viz_server.py --yang-dir ../hosts --yang-dir ../dhcpd --yang-dir ../recipe

    # ブラウザで確認
    open http://127.0.0.1:8080/
"""

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from pyconfd.yang_parser import NodeType, YangNode, YangSchemaRegistry


def _node_to_dict(node: YangNode, path: str = "") -> dict:
    """YangNode を JSON シリアライズ可能な dict に変換する"""
    current_path = f"{path}/{node.name}" if path else node.name

    result: dict = {
        "id": current_path,
        "name": node.name,
        "type": node.node_type.value,
        "description": node.description or "",
    }

    if node.node_type in (NodeType.LEAF, NodeType.LEAF_LIST):
        result["data_type"] = node.data_type
        result["mandatory"] = node.mandatory
        if node.default is not None:
            result["default"] = node.default

    if node.node_type == NodeType.LIST:
        result["keys"] = node.keys

    if node.node_type == NodeType.TYPEDEF:
        enums = node.properties.get("enum", [])
        if enums:
            result["enum"] = enums if isinstance(enums, list) else [enums]

    if node.node_type in (NodeType.MODULE, NodeType.SUBMODULE):
        result["namespace"] = node.namespace
        result["prefix"] = node.prefix
        revision = node.properties.get("revision", "")
        if revision:
            result["revision"] = revision

    result["children"] = [_node_to_dict(child, current_path) for child in node.children]
    return result


class YangVizHandler(BaseHTTPRequestHandler):
    registry: YangSchemaRegistry = None
    static_dir: Path = None

    def log_message(self, fmt, *args):  # noqa: D102
        print(f"  {self.command} {self.path}")

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._serve_file("index.html", "text/html; charset=utf-8")
        elif path == "/api/schema":
            self._serve_json(self._build_schema_response())
        elif path.startswith("/api/schema/"):
            module_name = path[len("/api/schema/"):]
            node = self.registry.get(module_name)
            if node is None:
                self._send_404()
            else:
                self._serve_json(_node_to_dict(node))
        else:
            self._send_404()

    def _send_404(self):
        self.send_response(404)
        self.end_headers()

    def _serve_file(self, filename: str, content_type: str):
        file_path = self.static_dir / filename
        if not file_path.exists():
            self._send_404()
            return
        content = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_json(self, data: dict):
        content = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _build_schema_response(self) -> dict:
        modules = {}
        for name, node in self.registry._modules.items():
            modules[name] = _node_to_dict(node)
        return {"modules": modules}


def main():
    parser = argparse.ArgumentParser(
        description="YANG モデル可視化 Web ダッシュボード",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--yang-dir",
        action="append",
        dest="yang_dirs",
        default=None,
        metavar="DIR",
        help="YANG ファイルのディレクトリ (複数指定可能、デフォルト: カレントディレクトリ)",
    )
    parser.add_argument(
        "--port", type=int, default=8080,
        help="HTTP ポート番号 (デフォルト: 8080)",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="バインドアドレス (デフォルト: 127.0.0.1)",
    )
    args = parser.parse_args()

    if not args.yang_dirs:
        args.yang_dirs = ["."]

    registry = YangSchemaRegistry()
    for d in args.yang_dirs:
        yang_dir = Path(d).resolve()
        if not yang_dir.exists():
            print(f"警告: ディレクトリが存在しません: {yang_dir}", file=sys.stderr)
            continue
        sub = YangSchemaRegistry.from_dir(str(yang_dir))
        for name, node in sub._modules.items():
            registry.add(node)
            print(f"  ロード完了: {name}  ({yang_dir})")

    modules = list(registry._modules.keys())
    if not modules:
        print("警告: YANG ファイルが見つかりませんでした。", file=sys.stderr)
    else:
        print(f"読み込んだモジュール: {', '.join(modules)}")

    YangVizHandler.registry = registry
    YangVizHandler.static_dir = Path(__file__).parent

    server = HTTPServer((args.host, args.port), YangVizHandler)
    print(f"\nダッシュボード起動: http://{args.host}:{args.port}/")
    print("Ctrl+C で停止します。\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました。")


if __name__ == "__main__":
    main()
