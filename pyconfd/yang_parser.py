"""
YANG 1.0/1.1 基本パーサー

YANG ファイルを読み込み、スキーマツリー(YangNodeオブジェクト)を構築します。
ConfD の .fxs ファイルを生成する代わりに、Python 内でスキーマを表現します。

対応する YANG 構文:
  module / submodule, namespace, prefix, import
  container, list, leaf, leaf-list
  choice, case, grouping, uses
  rpc, input, output, notification
  typedef, type, key, mandatory, default, description
  コメント (// および /* */)
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any


class NodeType(Enum):
    MODULE       = "module"
    SUBMODULE    = "submodule"
    CONTAINER    = "container"
    LIST         = "list"
    LEAF         = "leaf"
    LEAF_LIST    = "leaf-list"
    CHOICE       = "choice"
    CASE         = "case"
    GROUPING     = "grouping"
    USES         = "uses"
    TYPEDEF      = "typedef"
    AUGMENT      = "augment"
    RPC          = "rpc"
    INPUT        = "input"
    OUTPUT       = "output"
    NOTIFICATION = "notification"
    ANYXML       = "anyxml"
    ANYDATA      = "anydata"


@dataclass
class YangNode:
    """YANG スキーマノード"""

    node_type: NodeType
    name: str
    parent: Optional["YangNode"] = field(default=None, repr=False)
    children: List["YangNode"] = field(default_factory=list)
    properties: Dict[str, Any] = field(default_factory=dict)

    # ---- 便利プロパティ ----

    @property
    def namespace(self) -> str:
        return self.properties.get("namespace", "")

    @property
    def prefix(self) -> str:
        return self.properties.get("prefix", "")

    @property
    def data_type(self) -> str:
        return self.properties.get("type", "string")

    @property
    def keys(self) -> List[str]:
        return self.properties.get("key", "").split()

    @property
    def mandatory(self) -> bool:
        return self.properties.get("mandatory", "false") == "true"

    @property
    def default(self) -> Optional[str]:
        return self.properties.get("default")

    @property
    def description(self) -> str:
        return self.properties.get("description", "")

    # ---- 検索 ----

    def get_child(self, name: str) -> Optional["YangNode"]:
        """名前でサブノードを検索"""
        for child in self.children:
            if child.name == name:
                return child
        return None

    def find_path(self, path: str) -> Optional["YangNode"]:
        """'/container/list/leaf' 形式のパスでノードを検索"""
        node: Optional[YangNode] = self
        for part in path.strip("/").split("/"):
            if not part:
                continue
            if ":" in part:          # prefix:name → name だけ使用
                part = part.split(":", 1)[1]
            if node is None:
                return None
            node = node.get_child(part)
        return node

    def data_children(self) -> List["YangNode"]:
        """データノードである子だけを返す"""
        data_types = {
            NodeType.CONTAINER, NodeType.LIST, NodeType.LEAF,
            NodeType.LEAF_LIST, NodeType.CHOICE, NodeType.ANYXML,
            NodeType.ANYDATA,
        }
        return [c for c in self.children if c.node_type in data_types]

    def __repr__(self) -> str:
        return f"YangNode({self.node_type.value}, {self.name!r})"


# ---------------------------------------------------------------------------
# トークナイザー
# ---------------------------------------------------------------------------

_RE_PRED = re.compile(r"\[([^=\]]+)=['\"]?([^'\"\\]]*)['\"]?\]")


def _tokenize(text: str):
    """YANG テキストをトークンのリストにする"""
    tokens = []
    i = 0
    n = len(text)

    while i < n:
        c = text[i]

        # 空白スキップ
        if c.isspace():
            i += 1
            continue

        # 行コメント
        if text[i: i + 2] == "//":
            while i < n and text[i] != "\n":
                i += 1
            continue

        # ブロックコメント
        if text[i: i + 2] == "/*":
            end = text.find("*/", i + 2)
            i = (end + 2) if end != -1 else n
            continue

        # 1文字トークン
        if c in "{};\n":
            if c not in ("\n",):
                tokens.append(c)
            i += 1
            continue

        # ダブルクォート文字列
        if c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    esc = text[i + 1]
                    buf.append(
                        {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(esc, esc)
                    )
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            tokens.append("".join(buf))
            i += 1
            continue

        # シングルクォート文字列
        if c == "'":
            i += 1
            j = text.find("'", i)
            if j == -1:
                j = n
            tokens.append(text[i:j])
            i = j + 1
            continue

        # 識別子 / 非引用値
        j = i
        while j < n and not text[j].isspace() and text[j] not in "{};\n\"'":
            j += 1
        if j > i:
            tokens.append(text[i:j])
        i = max(j, i + 1)

    return tokens


# ---------------------------------------------------------------------------
# パーサー
# ---------------------------------------------------------------------------

_NODE_TYPES: Dict[str, NodeType] = {
    "module":       NodeType.MODULE,
    "submodule":    NodeType.SUBMODULE,
    "container":    NodeType.CONTAINER,
    "list":         NodeType.LIST,
    "leaf":         NodeType.LEAF,
    "leaf-list":    NodeType.LEAF_LIST,
    "choice":       NodeType.CHOICE,
    "case":         NodeType.CASE,
    "grouping":     NodeType.GROUPING,
    "uses":         NodeType.USES,
    "typedef":      NodeType.TYPEDEF,
    "augment":      NodeType.AUGMENT,
    "rpc":          NodeType.RPC,
    "input":        NodeType.INPUT,
    "output":       NodeType.OUTPUT,
    "notification": NodeType.NOTIFICATION,
    "anyxml":       NodeType.ANYXML,
    "anydata":      NodeType.ANYDATA,
}


class YangParser:
    """YANG モジュールテキストを YangNode ツリーにパース"""

    def __init__(self):
        self._tokens: List[str] = []
        self._pos: int = 0

    # ---- 公開API ----

    def parse(self, text: str) -> YangNode:
        self._tokens = _tokenize(text)
        self._pos = 0
        top = self._peek()
        if top not in ("module", "submodule"):
            raise ValueError(f"YANG ファイルは 'module' か 'submodule' で始まる必要があります (got {top!r})")
        root = self._parse_stmt(None)
        if root is None:
            raise ValueError("パース失敗: ルートノードが見つかりません")
        return root

    def parse_file(self, path: str) -> YangNode:
        with open(path, "r", encoding="utf-8") as f:
            return self.parse(f.read())

    # ---- 内部メソッド ----

    def _peek(self, offset: int = 0) -> Optional[str]:
        idx = self._pos + offset
        return self._tokens[idx] if idx < len(self._tokens) else None

    def _next(self) -> Optional[str]:
        if self._pos < len(self._tokens):
            tok = self._tokens[self._pos]
            self._pos += 1
            return tok
        return None

    def _parse_stmt(self, parent: Optional[YangNode]) -> Optional[YangNode]:
        """1つの YANG ステートメントをパースする"""
        keyword = self._next()
        if keyword is None:
            return None

        # オプション引数の読み取り
        arg: Optional[str] = None
        p = self._peek()
        if p not in ("{", ";", None):
            arg = self._next()
            p = self._peek()

        # ノードタイプの判定
        node_type = _NODE_TYPES.get(keyword)
        if node_type is not None:
            node = YangNode(node_type=node_type, name=arg or keyword, parent=parent)
            if parent is not None:
                parent.children.append(node)
        else:
            node = None

        if p == ";":
            self._next()
            if node is None and parent is not None and keyword is not None:
                parent.properties[keyword] = arg or ""
            return node

        if p == "{":
            self._next()   # '{' を消費
            target = node if node is not None else parent
            while self._peek() not in ("}", None):
                self._parse_stmt(target)
            self._next()   # '}' を消費
            return node

        # 引数もブロックもない場合
        if node is None and parent is not None and keyword is not None:
            parent.properties[keyword] = arg or ""
        return node


def load_yang(path: str) -> YangNode:
    """YANG ファイルを読み込み、モジュールのルートノードを返す"""
    return YangParser().parse_file(path)
