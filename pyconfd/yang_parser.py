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

YangSchemaRegistry:
  ディレクトリ内の .yang ファイルを一括ロードし、モジュール名で検索できます。
  NETCONF <hello> の capability 告知にも使用します。
"""

import glob
import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Any

log = logging.getLogger(__name__)


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
                # 'enum' ステートメントは複数あるためリストに追記する
                if keyword == "enum":
                    enum_list = parent.properties.get("enum", [])
                    if not isinstance(enum_list, list):
                        enum_list = [enum_list] if enum_list else []
                    enum_list.append(arg or "")
                    parent.properties["enum"] = enum_list
                else:
                    parent.properties[keyword] = arg or ""
            return node

        if p == "{":
            self._next()   # '{' を消費
            target = node if node is not None else parent
            # node がない場合 (revision など) は引数をプロパティとして親に保存する
            # ただし 'enum' は後でリストに追記するためここでは書かない
            if node is None and parent is not None and keyword is not None and arg is not None:
                if keyword != "enum":
                    parent.properties[keyword] = arg
            while self._peek() not in ("}", None):
                self._parse_stmt(target)
            self._next()   # '}' を消費
            # ブロック付き enum ステートメント (enum <name> { ... }) の場合もリストに追記
            if node is None and keyword == "enum" and parent is not None:
                enum_list = parent.properties.get("enum", [])
                if not isinstance(enum_list, list):
                    enum_list = [enum_list] if enum_list else []
                if arg and arg not in enum_list:
                    enum_list.append(arg)
                parent.properties["enum"] = enum_list
            return node

        # 引数もブロックもない場合
        if node is None and parent is not None and keyword is not None:
            parent.properties[keyword] = arg or ""
        return node


def load_yang(path: str) -> YangNode:
    """YANG ファイルを読み込み、モジュールのルートノードを返す"""
    return YangParser().parse_file(path)


# ---------------------------------------------------------------------------
# YangSchemaRegistry — 複数 YANG モジュールの管理
# ---------------------------------------------------------------------------

class YangSchemaRegistry:
    """
    複数の YANG モジュールを保持し、モジュール名・namespace で検索できるレジストリ。

    NETCONF <hello> の capability 告知にも利用できます。
    capability URI の形式は RFC 6020 section 5.6.4 に準拠します::

        <namespace>?module=<name>&revision=<date>

    使用例::

        registry = YangSchemaRegistry.from_dir("./yang-modules")
        mod = registry.get("dhcpd")
        for uri in registry.capability_uris():
            print(uri)
    """

    def __init__(self):
        # モジュール名 → YangNode のマッピング
        self._modules: Dict[str, YangNode] = {}

    # ---- ファクトリ ----

    @classmethod
    def from_dir(cls, directory: str, recursive: bool = False) -> "YangSchemaRegistry":
        """
        ディレクトリ内の全 .yang ファイルを読み込んでレジストリを構築する。

        Parameters
        ----------
        directory : str
            .yang ファイルを探すディレクトリのパス
        recursive : bool
            True のとき、サブディレクトリも再帰的に探索する (デフォルト False)
        """
        registry = cls()
        pattern = os.path.join(directory, "**", "*.yang") if recursive else os.path.join(directory, "*.yang")
        paths = sorted(glob.glob(pattern, recursive=recursive))
        if not paths:
            log.warning("YangSchemaRegistry: .yang ファイルが見つかりません: %s", directory)
        for path in paths:
            try:
                node = load_yang(path)
                registry.add(node)
                log.debug("YangSchemaRegistry: ロード成功 %s (%s)", node.name, path)
            except Exception as exc:
                log.warning("YangSchemaRegistry: %s のロードに失敗しました: %s", path, exc)
        return registry

    # ---- 操作 ----

    def add(self, module: YangNode) -> None:
        """パース済みの YangNode (module) をレジストリに追加する。"""
        if module.node_type not in (NodeType.MODULE, NodeType.SUBMODULE):
            raise ValueError(f"YangNode は module または submodule である必要があります: {module.node_type}")
        self._modules[module.name] = module

    def get(self, module_name: str) -> Optional[YangNode]:
        """モジュール名で YangNode を返す。存在しない場合は None。"""
        return self._modules.get(module_name)

    def all_modules(self) -> List[YangNode]:
        """登録済みの全モジュールを返す。"""
        return list(self._modules.values())

    def capability_uris(self) -> List[str]:
        """
        NETCONF <hello> に載せる capability URI のリストを返す。
        各モジュールの namespace + ?module=name[&revision=date] の形式 (RFC 6020 sec 5.6.4)。
        """
        uris = []
        for mod in self._modules.values():
            ns = mod.namespace
            if not ns:
                continue
            revision = mod.properties.get("revision", "")
            # revision ステートメントは "date { ... }" という子ノードになる場合もある
            if not revision:
                for child in mod.children:
                    if child.name and re.match(r"\d{4}-\d{2}-\d{2}", child.name):
                        revision = child.name
                        break
            uri = f"{ns}?module={mod.name}"
            if revision:
                uri += f"&revision={revision}"
            uris.append(uri)
        return uris

    def __len__(self) -> int:
        return len(self._modules)

    def __contains__(self, module_name: str) -> bool:
        return module_name in self._modules

    def __repr__(self) -> str:
        names = ", ".join(self._modules.keys())
        return f"YangSchemaRegistry([{names}])"


# ---------------------------------------------------------------------------
# 型バリデーション
# ---------------------------------------------------------------------------

import ipaddress as _ipaddress

# YANG 組み込み整数型の範囲
_INT_RANGES: Dict[str, tuple] = {
    "int8":   (-128, 127),
    "int16":  (-32768, 32767),
    "int32":  (-2147483648, 2147483647),
    "int64":  (-9223372036854775808, 9223372036854775807),
    "uint8":  (0, 255),
    "uint16": (0, 65535),
    "uint32": (0, 4294967295),
    "uint64": (0, 18446744073709551615),
}


def _resolve_typedef(type_name: str, module_node: Optional[YangNode]) -> Optional[YangNode]:
    """
    module_node の直下にある typedef を名前で検索して返す。
    prefix 付き (prefix:name) の場合は prefix を除去して検索する。
    """
    if module_node is None:
        return None
    if ":" in type_name:
        type_name = type_name.split(":", 1)[1]
    for child in module_node.children:
        if child.node_type == NodeType.TYPEDEF and child.name == type_name:
            return child
    return None


def _get_enum_values(node: YangNode) -> List[str]:
    """
    YangNode (typedef または leaf) から有効な enum 値のリストを返す。
    パーサーは properties["enum"] にリストで格納している。
    """
    raw = node.properties.get("enum", [])
    if isinstance(raw, list):
        return [str(v) for v in raw]
    if raw:
        return [str(raw)]
    return []


def _collect_enum_values(node: YangNode) -> List[str]:
    """
    leaf の type が enumeration のとき、その enum 値リストを返す。
    """
    return _get_enum_values(node)


def _parse_range_constraint(range_str: str) -> List[tuple]:
    """
    YANG range 文字列 (例: "1..20 | 50..100" または "1..max") を
    (min, max) タプルのリストに変換する。
    """
    segments = []
    for part in range_str.split("|"):
        part = part.strip()
        if ".." in part:
            lo_s, hi_s = part.split("..", 1)
            lo_s, hi_s = lo_s.strip(), hi_s.strip()
            lo = None if lo_s == "min" else int(lo_s)
            hi = None if hi_s == "max" else int(hi_s)
            segments.append((lo, hi))
        else:
            v = int(part.strip())
            segments.append((v, v))
    return segments


def validate_value(value: Any, leaf_node: YangNode,
                   module_node: Optional[YangNode] = None) -> Optional[str]:
    """
    value が leaf_node の YANG 型制約を満たすかを検査する。

    Parameters
    ----------
    value       : セットしようとしている値 (Python 型)
    leaf_node   : 対象の YangNode (LEAF または LEAF_LIST)
    module_node : モジュールルートの YangNode (typedef 解決に使用)

    Returns
    -------
    None        : バリデーション成功
    str         : エラーメッセージ
    """
    type_name: str = leaf_node.properties.get("type", "string")

    # --- typedef 解決 ---
    typedef_node = _resolve_typedef(type_name, module_node)
    if typedef_node is not None:
        # typedef の実体型を取得
        actual_type = typedef_node.properties.get("type", "string")
        # enumeration の enum 値は typedef の properties["enum"] から収集
        if actual_type == "enumeration":
            enum_vals = _get_enum_values(typedef_node)
            str_value = str(value)
            if str_value not in enum_vals:
                return f"値 {str_value!r} は型 {type_name} に含まれません (有効値: {', '.join(enum_vals)})"
            return None
        # typedef に range 制約がある場合
        range_str = typedef_node.properties.get("range", "")
        return _validate_builtin(actual_type, value, range_str, type_name)

    # --- enumeration (直接定義) ---
    if type_name == "enumeration":
        enum_vals = _collect_enum_values(leaf_node)
        if not enum_vals:
            # enum 値を properties["enum_*"] から取得しようとする
            enum_vals = [k[5:] for k in leaf_node.properties if k.startswith("enum_")]
        str_value = str(value)
        if str_value not in enum_vals:
            return f"値 {str_value!r} は enumeration に含まれません (有効値: {', '.join(enum_vals)})"
        return None

    # --- range 制約 (leafレベルの type ブロック) ---
    range_str = leaf_node.properties.get("range", "")
    return _validate_builtin(type_name, value, range_str, type_name)


def _validate_builtin(type_name: str, value: Any,
                      range_str: str, display_name: str) -> Optional[str]:
    """
    YANG 組み込み型および ietf-inet-types の型チェックを行う。
    """
    str_value = str(value) if not isinstance(value, str) else value

    # --- 整数型 ---
    if type_name in _INT_RANGES:
        lo, hi = _INT_RANGES[type_name]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            try:
                int_val = int(str_value)
            except (ValueError, TypeError):
                return f"値 {str_value!r} は {display_name} に変換できません (整数が必要です)"
        else:
            int_val = int(value)
        if not (lo <= int_val <= hi):
            return (f"値 {int_val} は {display_name} の範囲外です "
                    f"({lo}..{hi})")
        if range_str:
            return _check_range_constraint(int_val, range_str, display_name)
        return None

    # --- decimal64 ---
    if type_name == "decimal64":
        try:
            float(str_value)
        except (ValueError, TypeError):
            return f"値 {str_value!r} は {display_name} に変換できません (小数が必要です)"
        return None

    # --- boolean ---
    if type_name == "boolean":
        if str_value.lower() not in ("true", "false"):
            return f"値 {str_value!r} は {display_name} に変換できません (true または false が必要です)"
        return None

    # --- empty ---
    if type_name == "empty":
        if str_value not in ("", "empty"):
            return f"値 {str_value!r} は {display_name} に変換できません (empty 型は値なし)"
        return None

    # --- inet:ipv4-address ---
    if type_name in ("inet:ipv4-address", "ipv4-address"):
        try:
            _ipaddress.IPv4Address(str_value)
        except ValueError:
            return f"値 {str_value!r} は有効な IPv4 アドレスではありません"
        return None

    # --- inet:ipv6-address ---
    if type_name in ("inet:ipv6-address", "ipv6-address"):
        try:
            _ipaddress.IPv6Address(str_value)
        except ValueError:
            return f"値 {str_value!r} は有効な IPv6 アドレスではありません"
        return None

    # --- inet:ip-address ---
    if type_name in ("inet:ip-address", "ip-address"):
        try:
            _ipaddress.ip_address(str_value)
        except ValueError:
            return f"値 {str_value!r} は有効な IP アドレスではありません"
        return None

    # --- inet:ipv4-prefix ---
    if type_name in ("inet:ipv4-prefix", "ipv4-prefix"):
        try:
            _ipaddress.IPv4Network(str_value, strict=False)
        except ValueError:
            return f"値 {str_value!r} は有効な IPv4 プレフィックス (例: 192.168.1.0/24) ではありません"
        return None

    # --- inet:ipv6-prefix ---
    if type_name in ("inet:ipv6-prefix", "ipv6-prefix"):
        try:
            _ipaddress.IPv6Network(str_value, strict=False)
        except ValueError:
            return f"値 {str_value!r} は有効な IPv6 プレフィックス ではありません"
        return None

    # --- inet:port-number ---
    if type_name in ("inet:port-number", "port-number"):
        try:
            port = int(str_value)
            if not (0 <= port <= 65535):
                raise ValueError
        except (ValueError, TypeError):
            return f"値 {str_value!r} は有効なポート番号 (0..65535) ではありません"
        return None

    # --- string (range_str があれば長さ制約として扱う) ---
    if type_name == "string":
        if range_str:
            return _check_range_constraint(len(str_value), range_str, display_name)
        return None

    # 未知の型はチェックしない (将来の拡張に対して寛容に)
    return None


def _check_range_constraint(int_val: int, range_str: str,
                             display_name: str) -> Optional[str]:
    """range 制約 (例: '1..20 | 50..100') に対して int_val を検査する"""
    try:
        segments = _parse_range_constraint(range_str)
    except (ValueError, TypeError):
        return None  # パース失敗は無視
    for lo, hi in segments:
        lo_ok = (lo is None) or (int_val >= lo)
        hi_ok = (hi is None) or (int_val <= hi)
        if lo_ok and hi_ok:
            return None
    ranges_desc = " | ".join(
        f"{lo if lo is not None else 'min'}..{hi if hi is not None else 'max'}"
        for lo, hi in segments
    )
    return f"値 {int_val} は {display_name} の range 制約外です ({ranges_desc})"
