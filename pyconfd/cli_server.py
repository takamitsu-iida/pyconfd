"""
CLI サーバー

ConfD の CLI エージェントに相当する TCP ベースの対話式 CLI サーバーです。
telnet でポートに接続するとシェルが起動します。

対応スタイル:
  C-style (Cisco XR/IOS 風, デフォルト)
  J-style (Juniper Junos 風)

C-style オペレーショナルモード コマンド:
  show running-config [path]    running 設定を表示
  show candidate-config [path]  candidate 設定を表示
  configure [terminal]          コンフィグモードに移行
  exit / quit                   切断
  help / ?                      ヘルプ表示

C-style コンフィグモード コマンド:
  show [path]                   candidate 設定を表示
  set <path> <value>            リーフ値をセット
  no <path>                     ノードを削除
  commit                        candidate を running に適用
  abort / discard               candidate の変更を破棄
  exit / end                    コンフィグモードを抜ける
  help / ?                      ヘルプ表示

J-style オペレーショナルモード コマンド:
  show configuration [path]     running 設定を表示
  configure                     コンフィグモードに移行
  exit / quit                   切断

J-style コンフィグモード コマンド:
  show [path]                   candidate 設定を表示
  set <path> <value>            リーフ値をセット
  delete <path>                 ノードを削除
  commit                        candidate を running に適用
  rollback / discard            candidate の変更を破棄
  exit                          一つ上のモードへ
  quit                          コンフィグモードを抜ける
  help / ?                      ヘルプ表示
"""

import json
import logging
import re
import select
import socket
import threading
from typing import Optional

from .cdb import CDB
from .maapi import MAAPI

log = logging.getLogger(__name__)

# Telnet IAC (Interpret As Command) バイト定数
IAC  = bytes([255])
DONT = bytes([254])
DO   = bytes([253])
WONT = bytes([252])
WILL = bytes([251])
SB   = bytes([250])   # サブネゴシエーション開始
SE   = bytes([240])   # サブネゴシエーション終了
GA   = bytes([249])   # Go Ahead
# オプション番号
OPT_ECHO       = bytes([1])
OPT_SGA        = bytes([3])   # Suppress Go Ahead
OPT_TTYPE      = bytes([24])  # Terminal Type
OPT_NAWS       = bytes([31])  # Negotiate About Window Size
OPT_LINEMODE   = bytes([34])

# CLI スタイル定数
STYLE_C = "c"   # Cisco C/I 風
STYLE_J = "j"   # Juniper J 風

CRLF = b"\r\n"


# ---------------------------------------------------------------------------
# ヘルパー: 設定ツリーを CLI テキストに変換
# ---------------------------------------------------------------------------

def _format_tree(tree, indent: int = 0, path_prefix: str = "", style: str = "j") -> str:
    """
    CDB の dict ツリーを設定テキスト形式に変換する。
    style="c" (Cisco IOS): セミコロンなし、インデントのみ
    style="j" (Juniper): セミコロンあり（デフォルト）
    """
    lines = []
    pad = "  " * indent
    suffix = "" if style == "c" else ";"

    if isinstance(tree, dict):
        for key, val in tree.items():
            if isinstance(val, dict):
                lines.append(f"{pad}{key} {{")
                lines.append(_format_tree(val, indent + 1, style=style))
                lines.append(f"{pad}}}")
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        # リストのキーを探してラベルにする
                        label = _list_label(item)
                        lines.append(f"{pad}{key}{' ' + label if label else ''} {{")
                        lines.append(_format_tree(item, indent + 1, style=style))
                        lines.append(f"{pad}}}")
                    else:
                        lines.append(f"{pad}{key} {item}{suffix}")
            else:
                lines.append(f"{pad}{key} {val}{suffix}")
    elif isinstance(tree, list):
        for item in tree:
            lines.append(_format_tree(item, indent, style=style))
    else:
        lines.append(f"{pad}{tree}{suffix}")
    return "\n".join(lines)


def _list_label(entry: dict) -> str:
    """リストエントリから代表的なキー値を取り出してラベル文字列を作る"""
    # 一般的なキー名候補
    for k in ("name", "id", "net", "address", "key", "prefix"):
        if k in entry:
            return str(entry[k])
    # 最初の文字列値を使う
    for v in entry.values():
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            return str(v)
    return ""


def _subtree(tree: dict, path: str) -> object:
    """
    '/' 区切りのパスで tree を辿って部分ツリーを返す。
    パスが空文字や '/' の場合はツリー全体を返す。
    キー述語 (例: subnet[net=1.2.3.4][mask=255.0.0.0]) にも対応。
    """
    if not path or path == "/":
        return tree
    parts = [p for p in path.strip("/").split("/") if p]
    node = tree
    for part in parts:
        if not isinstance(node, dict):
            return None
        # キー述語を解析（例: subnet[net=1.2.3.4][mask=255.0.0.0] → name=subnet, keys={...}）
        name = part.split("[", 1)[0]
        if name not in node:
            return None
        child = node[name]
        # list の場合、キー述語でエントリを検索
        if isinstance(child, list):
            keys_str = part[len(name):]  # "[net=...][mask=...]"
            if not keys_str:
                return None  # list だがキー述語がない
            # キー述語を解析
            import re
            key_pairs = re.findall(r"\[(\w+)=([^\]]+)\]", keys_str)
            if not key_pairs:
                return None
            # リストエントリを検索
            for entry in child:
                if all(str(entry.get(k)) == str(v) for k, v in key_pairs):
                    node = entry
                    break
            else:
                return None  # マッチするエントリがない
        else:
            node = child
    return node


# ---------------------------------------------------------------------------
# CLI セッション
# ---------------------------------------------------------------------------

class CLISession:
    """1クライアント接続を処理する CLI セッション"""

    _next_session_id = 1
    _id_lock = threading.Lock()

    def __init__(
        self,
        conn: socket.socket,
        addr,
        cdb: CDB,
        style: str = STYLE_C,
        hostname: str = "pyconfd",
        username: str = "admin",
        schema=None,
        use_telnet: bool = True,
    ):
        with CLISession._id_lock:
            self.session_id = CLISession._next_session_id
            CLISession._next_session_id += 1

        self._conn = conn
        self._addr = addr
        self._cdb = cdb
        self._maapi = MAAPI(cdb)
        self._style = style
        self._hostname = hostname
        self._username = username
        self._schema = schema          # YangNode ルート (補完・ナビに使用)
        self._use_telnet = use_telnet  # False の場合は Telnet ネゴシエーションをスキップ
        self._active = True
        self._in_config = False        # True: コンフィグモード
        self._config_path: list = []   # Cisco 階層コンテキスト (セグメントのリスト)
        self._buf = b""
        self._history: list = []       # コマンド履歴
        self._history_max: int = 100   # 最大履歴数

    # ---- パブリック エントリポイント ----

    def run(self):
        try:
            if self._use_telnet:
                self._negotiate_telnet()
            self._send_banner()
            while self._active:
                prompt = self._make_prompt()
                self._write(prompt.encode())
                line = self._read_line(prompt)
                if line is None:
                    break
                line = line.strip()
                if not line:
                    continue
                self._dispatch(line)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        except Exception:
            log.exception("CLIセッション %d 例外", self.session_id)
        finally:
            try:
                self._conn.close()
            except OSError:
                pass
            log.info("CLIセッション %d 切断 (%s)", self.session_id, self._addr)

    # ---- Telnet ネゴシエーション ----

    def _negotiate_telnet(self):
        """
        基本的な Telnet オプション交渉を行う。
        サーバー側から文字エコーを引き受け (WILL ECHO)、
        SGA (Suppress Go Ahead) を有効化する。
        """
        # RFC 1184 (LINEMODE) に従い文字単位モードを確立する。
        #
        # WILL ECHO    : サーバーがエコーを担当
        # WILL SGA     : サーバーが Go Ahead を抑制
        # DO SGA       : クライアントにも SGA を要求
        # DO LINEMODE  : LINEMODE オプションを要求
        #
        # その後クライアントが WILL LINEMODE を返したら
        # IAC SB LINEMODE MODE 0 IAC SE を送って文字単位モード (MODE 0) にする。
        # MODE 0 では Ctrl-P/N などがクライアント側でローカル処理されず
        # そのままサーバーに届く。
        self._conn.sendall(
            IAC + WILL + OPT_ECHO    +
            IAC + WILL + OPT_SGA     +
            IAC + DO   + OPT_SGA     +
            IAC + DO   + OPT_LINEMODE
        )
        import select as _select
        deadline_chunks = 5
        while deadline_chunks > 0:
            r, _, _ = _select.select([self._conn], [], [], 0.2)
            if not r:
                break
            data = self._conn.recv(256)
            if not data:
                break
            # IAC WILL LINEMODE (ff fb 22) → MODE 0 サブネゴシエーションを送る
            # MODE 0 = character-at-a-time (ローカル行編集なし)
            if bytes([255, 251, 34]) in data:
                self._conn.sendall(
                    IAC + SB + OPT_LINEMODE + bytes([1, 0]) + IAC + SE
                )
            self._buf += data
            deadline_chunks -= 1
        self._buf = self._strip_iac(self._buf)

    # ---- バナー ----

    def _send_banner(self):
        banner = (
            "\r\n"
            "pyconfd CLI\r\n"
            f"スタイル: {'C (Cisco XR)' if self._style == STYLE_C else 'J (Juniper)'}\r\n"
            "help または ? でコマンド一覧を表示します。\r\n"
            "\r\n"
        )
        self._write(banner.encode())

    # ---- プロンプト生成 ----

    def _make_prompt(self) -> str:
        h = self._hostname
        u = self._username
        if self._style == STYLE_C:
            if self._in_config:
                if self._config_path:
                    ctx = "-".join(self._config_path)
                    return f"{u}@{h}(config-{ctx})# "
                return f"{u}@{h}(config)# "
            return f"{u}@{h}> "
        else:  # J-style
            if self._in_config:
                return f"[edit]\r\n{u}@{h}% "
            return f"{u}@{h}> "

    # ---- コマンド ディスパッチ ----

    def _dispatch(self, line: str):
        tokens = line.split()
        cmd = tokens[0].lower()

        if self._in_config:
            self._dispatch_config(cmd, tokens, line)
        else:
            self._dispatch_oper(cmd, tokens, line)

    def _dispatch_oper(self, cmd: str, tokens: list, line: str):
        """オペレーショナルモードのコマンド処理 (前方一致によるコマンド解決をサポート)"""
        oper_cmds = ["configure", "show", "exit", "quit", "logout", "help"]
        resolved = self._resolve_cmd(cmd, oper_cmds) or cmd

        if resolved in ("exit", "quit", "logout"):
            self._write(b"\r\nGoodbye.\r\n")
            self._active = False

        elif resolved == "configure":
            self._enter_config()

        elif resolved == "show":
            self._cmd_show_oper(tokens)

        elif resolved == "help" or cmd == "?":
            self._write(self._help_oper().encode())

        else:
            self._write(f"% Unknown command: {line}\r\n".encode())

    def _dispatch_config(self, cmd: str, tokens: list, line: str):
        """コンフィグモードのコマンド処理"""
        if self._style == STYLE_C:
            self._dispatch_config_ios(cmd, tokens, line)
        else:
            self._dispatch_config_juniper(cmd, tokens, line)

    def _dispatch_config_ios(self, cmd: str, tokens: list, line: str):
        """
        Cisco IOS ライク コンフィグモード:
          - コンテキスト移動: <node-name> [key...]
          - リーフ設定: <leaf-name> <value>
          - no <leaf-name>  : リーフ削除
          - exit            : 一段上に戻る (トップなら exec へ)
          - end             : 即座に exec に戻る
          - show            : 現コンテキスト以下の candidate を表示
          - do <cmd>        : oper コマンドの実行
          - commit          : commit
        """
        keyword_cmds = ["show", "no", "commit", "abort", "discard",
                        "exit", "end", "do", "help"]
        resolved = self._resolve_cmd(cmd, keyword_cmds)

        if resolved == "end":
            self._config_path.clear()
            self._leave_config()
            return

        if resolved == "exit":
            if self._config_path:
                self._config_path.pop()
                self._write(b"")
            else:
                self._leave_config()
            return

        if resolved == "show":
            self._cmd_show_config_ios(tokens)
            return

        if resolved == "no":
            self._cmd_no_ios(tokens[1:])
            return

        if resolved == "commit":
            self._cmd_commit(tokens)
            return

        if resolved in ("abort", "discard"):
            self._cmd_discard()
            return

        if resolved == "do":
            if len(tokens) > 1:
                self._dispatch_oper(tokens[1].lower(), tokens[1:], " ".join(tokens[1:]))
            else:
                self._write(b"% Syntax: do <oper-command>\r\n")
            return

        if resolved == "help" or cmd == "?":
            self._write(self._help_config().encode())
            return

        # ---- コンテキスト移動 / リーフ設定 ----
        self._ios_navigate_or_set(tokens)

    def _ios_navigate_or_set(self, tokens: list):
        """
        入力トークンをもとに:
          1. コンテキスト内の子ノード名に一致 → そのコンテキストに移動
          2. リーフ名 + 値 → candidate に書き込み
        パスは現在の _config_path からの相対パス。
        """
        if not tokens:
            return
        node_name = tokens[0]
        value_tokens = tokens[1:]

        current_abs = "/" + "/".join(self._config_path) if self._config_path else ""

        # candidate ツリーで現コンテキストのサブツリーを取得
        cand = self._cdb._stores["candidate"]
        ctx_tree = _subtree(cand, current_abs) if current_abs else cand

        # スキーマで子ノードの種別を確認
        schema_node = self._schema_node_at(self._config_path)

        if schema_node is not None:
            child_schema = schema_node.get_child(node_name)
            if child_schema is None:
                # 前方一致
                matches = [c for c in schema_node.children if c.name.startswith(node_name)]
                if len(matches) == 1:
                    child_schema = matches[0]
                    node_name = child_schema.name
            if child_schema is not None:
                from pyconfd.yang_parser import NodeType as NT
                # container / list → コンテキスト移動
                if child_schema.node_type in (NT.CONTAINER, NT.LIST):
                    if child_schema.node_type == NT.LIST:
                        # キー値でリストエントリを特定する。
                        # Cisco ライクに部分キー入力でもサブモードへ入れるようにし、
                        # 足りないキーは後続の leaf 設定で埋められるようにする。
                        keys = child_schema.keys
                        if not value_tokens:
                            self._write(f"% キー値が必要です: {' '.join(keys)}\r\n".encode())
                            return
                        if len(value_tokens) > len(keys):
                            self._write(f"% キー値が多すぎます: {' '.join(keys)}\r\n".encode())
                            return
                        # リストエントリが存在しなければ作成
                        key_pred = "".join(
                            f"[{k}={v}]" for k, v in zip(keys, value_tokens)
                        )
                        abs_path = (current_abs + "/" + node_name + key_pred).lstrip("/")
                        try:
                            self._cdb.set(
                                "/" + abs_path + "/" + keys[0],
                                _coerce_value(value_tokens[0]),
                                datastore="candidate",
                            )
                        except Exception:
                            pass
                        # コンテキストセグメントはキー付きで積む
                        seg = node_name + key_pred
                    else:
                        seg = node_name
                    self._config_path.append(seg)
                    return
                # leaf / leaf-list → 値をセット
                else:
                    if not value_tokens:
                        self._write("% 値が必要です\r\n".encode())
                        return
                    value = _coerce_value(" ".join(value_tokens))
                    path = (current_abs + "/" + node_name).lstrip("/")
                    try:
                        self._cdb.set("/" + path, value, datastore="candidate")
                        self._normalize_current_list_context()
                    except Exception as e:
                        self._write(f"% Error: {e}\r\n".encode())
                    return

        # スキーマなし / 不明 → フォールバック: leaf として設定を試みる
        if value_tokens:
            path = ((current_abs + "/" + node_name) if current_abs
                    else "/" + node_name)
            try:
                self._cdb.set(path, _coerce_value(" ".join(value_tokens)),
                              datastore="candidate")
            except Exception as e:
                self._write(f"% Error: {e}\r\n".encode())
        else:
            self._write(f"% Unknown command: {tokens[0]}\r\n".encode())

    def _cmd_no_ios(self, args: list):
        """no <leaf-name> [key...]  : 現コンテキスト相対でリーフ/ノードを削除"""
        if not args:
            self._write(b"% Syntax: no <name>\r\n")
            return
        current_abs = "/" + "/".join(self._config_path) if self._config_path else ""
        path = (current_abs + "/" + "/".join(args)).lstrip("/")
        try:
            self._cdb.delete("/" + path, datastore="candidate")
        except Exception as e:
            self._write(f"% Error: {e}\r\n".encode())

    def _cmd_show_config_ios(self, tokens: list):
        """show [running-config] : 現コンテキスト以下の candidate を表示"""
        current_abs = "/" + "/".join(self._config_path) if self._config_path else ""
        # 'show running-config' は running を表示
        if len(tokens) >= 2 and tokens[1].lower() in ("running-config", "running"):
            ds = "running"
            path = current_abs
        else:
            ds = "candidate"
            path = current_abs
        # 追加パスがあれば結合
        extra = tokens[2:] if len(tokens) >= 3 else (tokens[1:] if len(tokens) >= 2
                and tokens[1].lower() not in ("running-config", "running") else [])
        if extra:
            path = (path + "/" + "/".join(extra)).rstrip("/")
        self._show_datastore(ds, path)

    def _dispatch_config_juniper(self, cmd: str, tokens: list, line: str):
        """Juniper J-style コンフィグモードのコマンド処理"""
        config_cmds = ["show", "set", "delete", "commit", "rollback", "discard",
                       "exit", "quit", "do", "help"]
        resolved = self._resolve_cmd(cmd, config_cmds) or cmd

        if resolved in ("exit", "quit"):
            self._leave_config()
        elif resolved == "show":
            self._cmd_show_config(tokens)
        elif resolved == "set":
            self._cmd_set(tokens)
        elif resolved == "delete":
            self._cmd_delete(tokens[1:])
        elif resolved == "commit":
            self._cmd_commit(tokens)
        elif resolved in ("rollback", "discard"):
            self._cmd_discard()
        elif resolved == "do":
            if len(tokens) > 1:
                self._dispatch_oper(tokens[1].lower(), tokens[1:], " ".join(tokens[1:]))
            else:
                self._write(b"% Syntax: do <oper-command>\r\n")
        elif resolved == "help" or cmd == "?":
            self._write(self._help_config().encode())
        else:
            self._write(f"% Unknown command: {line}\r\n".encode())

    # ---- コンフィグモード遷移 ----

    def _enter_config(self):
        self._in_config = True
        self._config_path.clear()
        # CDB でトランザクション開始 (candidate を running のコピーにリセット)
        self._cdb.start_transaction()
        self._write(b"\r\nEntering configuration mode.\r\n")

    def _leave_config(self):
        self._in_config = False
        self._config_path.clear()
        # uncommitted な変更は破棄
        try:
            self._cdb.abort_transaction()
        except Exception:
            pass
        self._write(b"\r\nLeaving configuration mode.\r\n")

    # ---- show コマンド (オペレーショナルモード) ----

    def _cmd_show_oper(self, tokens: list):
        if len(tokens) < 2:
            self._write(b"% Incomplete command. Use 'show running-config' or 'show candidate-config'.\r\n")
            return

        sub = tokens[1].lower()
        if sub in ("running-config", "running", "configuration") and self._style == STYLE_C:
            path = "/".join(tokens[2:]) if len(tokens) > 2 else ""
            self._show_datastore("running", path)
        elif sub == "configuration" and self._style == STYLE_J:
            path = "/".join(tokens[2:]) if len(tokens) > 2 else ""
            self._show_datastore("running", path)
        elif sub in ("candidate-config", "candidate"):
            path = "/".join(tokens[2:]) if len(tokens) > 2 else ""
            self._show_datastore("candidate", path)
        elif sub == "running-config" or sub == "running":
            path = "/".join(tokens[2:]) if len(tokens) > 2 else ""
            self._show_datastore("running", path)
        else:
            self._write(f"% Unknown show target: {' '.join(tokens[1:])}\r\n".encode())

    # ---- show コマンド (コンフィグモード, J-style) ----

    def _cmd_show_config(self, tokens: list):
        # 'show' だけ, または 'show <path>'
        if len(tokens) == 1:
            path = ""
        elif tokens[1].lower() in ("configuration", "running-config", "running"):
            path = "/".join(tokens[2:]) if len(tokens) > 2 else ""
        else:
            # 'show /dhcp/...' や 'show dhcp' のようなパス指定
            path = "/".join(tokens[1:])
        self._show_datastore("candidate", path)

    def _show_datastore(self, datastore: str, path: str = ""):
        try:
            tree = self._cdb._stores[datastore]
            if path:
                subtree = _subtree(tree, path)
                if subtree is None:
                    self._write(f"% Path not found: {path}\r\n".encode())
                    return
                label = path.strip("/").split("/")[-1] if path else ""
                formatted = _format_tree({label: subtree} if label else subtree, style=self._style)
            else:
                formatted = _format_tree(tree, style=self._style)
            if not formatted.strip():
                self._write(b"(empty)\r\n")
            else:
                output = formatted.replace("\n", "\r\n") + "\r\n"
                self._write(output.encode())
        except Exception as e:
            self._write(f"% Error: {e}\r\n".encode())

    # ---- set コマンド ----

    def _cmd_set(self, tokens: list):
        # set <path> <value>
        if len(tokens) < 3:
            self._write(b"% Syntax: set <path> <value>\r\n")
            return
        path = tokens[1]
        value = " ".join(tokens[2:])
        # 数値変換を試みる
        value = _coerce_value(value)
        try:
            self._cdb.set(path, value, datastore="candidate")
            self._write(b"[ok]\r\n")
        except Exception as e:
            self._write(f"% Error: {e}\r\n".encode())

    # ---- no / delete コマンド ----

    def _cmd_delete(self, path_tokens: list):
        if not path_tokens:
            self._write(b"% Syntax: no <path>  (or: delete <path>)\r\n")
            return
        path = path_tokens[0]
        try:
            self._cdb.delete(path, datastore="candidate")
            self._write(b"[ok]\r\n")
        except Exception as e:
            self._write(f"% Error: {e}\r\n".encode())

    # ---- commit コマンド ----

    def _cmd_commit(self, tokens: list):
        sub = tokens[1].lower() if len(tokens) > 1 else ""
        if sub == "check":
            # バリデーションのみ (現実装では常に OK)
            self._write(b"Validation OK.\r\n[ok]\r\n")
            return
        try:
            self._cdb.commit()
            self._write(b"Commit complete.\r\n[ok]\r\n")
            if sub == "and-quit":
                self._leave_config()
        except Exception as e:
            self._write(f"% Commit failed: {e}\r\n".encode())

    # ---- discard / abort コマンド ----

    def _cmd_discard(self):
        try:
            self._cdb.abort_transaction()
            self._cdb.start_transaction()  # 新しいトランザクションを開始
            self._write(b"Changes discarded.\r\n[ok]\r\n")
        except Exception as e:
            self._write(f"% Error: {e}\r\n".encode())

    # ---- ヘルプテキスト ----

    def _help_oper(self) -> str:
        if self._style == STYLE_C:
            return (
                "\r\nオペレーショナルモード コマンド:\r\n"
                "  show running-config [path]    running 設定を表示\r\n"
                "  show candidate-config [path]  candidate 設定を表示\r\n"
                "  configure [terminal]          コンフィグモードに移行\r\n"
                "  exit / quit                   切断\r\n"
                "  help / ?                      このヘルプを表示\r\n\r\n"
            )
        else:
            return (
                "\r\nオペレーショナルモード コマンド:\r\n"
                "  show configuration [path]     running 設定を表示\r\n"
                "  configure                     コンフィグモードに移行\r\n"
                "  exit / quit                   切断\r\n"
                "  help / ?                      このヘルプを表示\r\n\r\n"
            )

    def _help_config(self) -> str:
        if self._style == STYLE_C:
            return (
                "\r\nコンフィグモード コマンド (Cisco IOS ライク):\r\n"
                "  <node>                        コンテキストに移動 (container/list)\r\n"
                "  <leaf> <value>                リーフ値をセット\r\n"
                "  no <leaf>                     リーフ/ノードを削除\r\n"
                "  show [running-config]         現コンテキストの設定を表示\r\n"
                "  commit [check|and-quit]       candidate を running に適用\r\n"
                "  abort / discard               変更を破棄\r\n"
                "  do <oper-cmd>                 operational コマンドを実行\r\n"
                "  exit                          一段上へ戻る\r\n"
                "  end                           exec モードへ戻る\r\n"
                "  help / ?                      このヘルプを表示\r\n\r\n"
            )
        else:
            return (
                "\r\nコンフィグモード コマンド:\r\n"
                "  show [path]                   candidate 設定を表示\r\n"
                "  set <path> <value>            リーフ値をセット\r\n"
                "  delete <path>                 ノードを削除\r\n"
                "  commit                        candidate を running に適用\r\n"
                "  rollback / discard            変更を破棄\r\n"
                "  exit                          コンフィグモードを抜ける\r\n"
                "  quit                          コンフィグモードを抜ける\r\n"
                "  help / ?                      このヘルプを表示\r\n\r\n"
            )

    # ---- I/O ヘルパー ----

    def _write(self, data: bytes):
        try:
            self._conn.sendall(data)
        except OSError:
            self._active = False

    # ---- スキーマナビゲーション ----

    def _schema_node_at(self, path_segs: list):
        """_config_path のセグメントリストをたどり、対応する YangNode を返す"""
        if self._schema is None:
            return None
        node = self._schema
        for seg in path_segs:
            # キー述語 (例: subnet[net=1.2.3.4][mask=255.255.255.0]) を除去
            name = seg.split("[")[0]
            child = node.get_child(name)
            if child is None:
                return None
            node = child
        return node

    @staticmethod
    def _parse_path_segment(seg: str):
        """'subnet[net=1.1.1.0][mask=255.255.255.0]' を (name, {key: val}) に分解する"""
        name = seg.split("[", 1)[0]
        preds = {}
        for k, v in re.findall(r"\[(\w+)=([^\]]+)\]", seg):
            preds[k] = v
        return name, preds

    def _normalize_current_list_context(self):
        """現在コンテキストが list の場合、揃った key で述語を正規化する。"""
        if not self._in_config or not self._config_path:
            return

        node = self._schema_node_at(self._config_path)
        if node is None:
            return

        from pyconfd.yang_parser import NodeType as NT
        if node.node_type != NT.LIST:
            return

        seg_name, pred_map = self._parse_path_segment(self._config_path[-1])
        abs_path = "/" + "/".join(self._config_path)

        merged = dict(pred_map)
        for k in node.keys:
            if k in merged:
                continue
            try:
                v = self._cdb.get(abs_path + "/" + k, datastore="candidate")
                if v is not None:
                    merged[k] = str(v)
            except Exception:
                pass

        if all(k in merged for k in node.keys):
            key_pred = "".join(f"[{k}={merged[k]}]" for k in node.keys)
            self._config_path[-1] = seg_name + key_pred

    # ---- コマンド解決・補完ヘルパー ----

    def _resolve_cmd(self, word: str, candidates: list) -> Optional[str]:
        """前方一致でコマンド名を解決する (完全一致 > 一意な前方一致 > None)"""
        w = word.lower()
        if w in candidates:
            return w
        matches = [c for c in candidates if c.startswith(w)]
        return matches[0] if len(matches) == 1 else None

    def _get_keyword_commands(self) -> list:
        """現在モードの固定キーワードコマンド一覧を返す"""
        if self._in_config:
            if self._style == STYLE_C:
                return ["show", "no", "commit", "abort", "discard",
                        "exit", "end", "do", "help"]
            else:
                return ["show", "set", "delete", "commit", "rollback", "discard",
                        "exit", "quit", "do", "help"]
        return ["show", "configure", "exit", "quit", "logout", "help"]

    def _get_context_children(self) -> list:
        """C-style config: 現コンテキストの子ノード名一覧を返す"""
        node = self._schema_node_at(self._config_path)
        if node is None:
            return []
        return [c.name for c in node.children]

    def _get_commands(self) -> list:
        """補完・ヘルプ用コマンド一覧 (キーワード + コンテキスト子ノード)"""
        kw = self._get_keyword_commands()
        if self._in_config and self._style == STYLE_C:
            ctx = self._get_context_children()
            # キーワードと重複しないものを追加
            return kw + [c for c in ctx if c not in kw]
        return kw

    def _show_subcommands(self) -> list:
        """show コマンドのサブコマンド候補を返す"""
        if self._style == STYLE_C:
            return ["running-config", "candidate-config"]
        return ["configuration", "candidate-config"]

    def _complete(self, text: str) -> list:
        """Tab 補完の候補リストを返す"""
        tokens = text.split()
        kw_cmds = self._get_keyword_commands()
        all_cmds = self._get_commands()
        # コマンド名の補完
        if not tokens or (len(tokens) == 1 and not text.endswith(" ")):
            word = tokens[0] if tokens else ""
            return sorted(c for c in all_cmds if c.startswith(word))
        # show サブコマンドの補完
        resolved0 = self._resolve_cmd(tokens[0], kw_cmds)
        if resolved0 == "show":
            if len(tokens) == 1 and text.endswith(" "):
                return sorted(self._show_subcommands())
            if len(tokens) == 2 and not text.endswith(" "):
                word = tokens[1]
                return sorted(s for s in self._show_subcommands() if s.startswith(word))
        return []

    def _inline_help(self, text: str) -> str:
        """? キー押下時のインラインヘルプテキストを返す"""
        tokens = text.split()
        kw_cmds = self._get_keyword_commands()
        all_cmds = self._get_commands()
        if not tokens or (len(tokens) == 1 and not text.endswith(" ")):
            word = tokens[0] if tokens else ""
            matches = [(c, self._cmd_description(c)) for c in all_cmds if c.startswith(word)]
        elif len(tokens) == 1 and text.endswith(" "):
            resolved = self._resolve_cmd(tokens[0], kw_cmds)
            if resolved == "show":
                matches = [(s, "") for s in self._show_subcommands()]
            elif resolved == "commit":
                matches = [("check", "バリデーションのみ"),
                           ("and-quit", "コミット後にコンフィグモードを抜ける"),
                           ("<CR>", "確定")]
            elif resolved in ("no", "set", "delete"):
                ctx = self._get_context_children()
                matches = [(c, "") for c in ctx] or [("<name>", "ノード名")]
            else:
                matches = [("<CR>", "確定")]
        else:
            matches = [("<CR>", "確定")]
        if not matches:
            return "  % No matches\r\n\r\n"
        return "".join(f"  {c:<24} {desc}\r\n" for c, desc in matches) + "\r\n"

    def _cmd_description(self, cmd: str) -> str:
        """コマンドの1行説明を返す"""
        # コンテキスト子ノードはスキーマの description を使う
        node = self._schema_node_at(self._config_path)
        if node is not None:
            child = node.get_child(cmd)
            if child is not None and child.description:
                return child.description[:50]
        return {
            "show":      "設定を表示する",
            "configure": "コンフィグモードに移行する",
            "set":       "リーフ値をセットする",
            "no":        "ノードを削除する",
            "delete":    "ノードを削除する",
            "commit":    "candidate を running に適用する",
            "abort":     "変更を破棄する",
            "discard":   "変更を破棄する",
            "rollback":  "変更を破棄する",
            "exit":      "一段上へ戻る / 切断する",
            "end":       "exec モードへ戻る",
            "quit":      "切断する",
            "logout":    "切断する",
            "do":        "config モードから operational コマンドを実行する",
            "help":      "ヘルプを表示する",
        }.get(cmd, "")

    def _read_line(self, prompt: str = "") -> Optional[str]:
        """
        Telnet から 1 行読み込む。
        機能:
          - 文字を受信するたびに即時エコーバック
          - ← → キーでカーソル移動 (挿入モード)
          - ↑ / Ctrl+P で履歴を遡る
          - ↓ / Ctrl+N で履歴を進む
          - Tab キーでコマンド補完
          - Ctrl+A / Ctrl+E で行頭 / 行末移動
          - Ctrl+U / Ctrl+K で行の全 / 部分消去
          - Ctrl+C で現在の入力をキャンセル
          - Ctrl+D で exit と同等 (oper モードは切断、config モードはモード脱出)
          - ? でインラインヘルプ表示
        """
        # マルチラインプロンプトでも再描画が正しく動くよう最終行のみ取り出す
        if "\r\n" in prompt:
            prompt_line = prompt.rsplit("\r\n", 1)[1]
        elif "\n" in prompt:
            prompt_line = prompt.rsplit("\n", 1)[1]
        else:
            prompt_line = prompt

        line_chars: list = []  # 現在の入力行 (文字のリスト)
        cursor: int = 0        # カーソル位置 (0 = 先頭)
        hist_idx: int = len(self._history)
        saved_line: list = []  # 履歴ナビ中に保存した現在行
        esc_state: int = 0     # 0=通常, 1=ESC受信, 2=CSI (ESC[) 受信
        csi_buf: str = ""      # CSI パラメータ蓄積

        def redraw() -> None:
            """プロンプト最終行 + 入力行を再描画し、カーソルを正しい位置に移動する"""
            line_str = "".join(line_chars)
            out = b"\r" + prompt_line.encode("utf-8")
            out += line_str.encode("utf-8")
            out += b"\x1b[K"  # カーソル位置から行末までをクリア
            if cursor < len(line_chars):
                back = len(line_chars) - cursor
                out += f"\x1b[{back}D".encode()
            self._write(out)

        def next_byte() -> Optional[int]:
            """バッファから 1 バイト取り出す。バッファが空なら recv する。"""
            while not self._buf:
                if not self._active:
                    return None
                try:
                    r, _, _ = select.select([self._conn], [], [], 1.0)
                    if not r:
                        continue
                    chunk = self._conn.recv(256)
                    if not chunk:
                        return None
                    self._buf += self._strip_iac(chunk) if self._use_telnet else chunk
                except (OSError, ValueError):
                    return None
            b = self._buf[0]
            self._buf = self._buf[1:]
            return b

        while True:
            b = next_byte()
            if b is None:
                return None

            # ---- ESC シーケンス処理 ----
            if esc_state == 1:
                if b == 0x5b:  # [ → CSI
                    esc_state = 2
                    csi_buf = ""
                else:
                    esc_state = 0
                continue

            if esc_state == 2:
                if 0x40 <= b <= 0x7e:  # Final byte
                    esc_state = 0
                    if b == 0x41:  # ↑ Up: 履歴を遡る
                        if hist_idx > 0:
                            if hist_idx == len(self._history):
                                saved_line = line_chars[:]
                            hist_idx -= 1
                            line_chars[:] = list(self._history[hist_idx])
                            cursor = len(line_chars)
                            redraw()
                    elif b == 0x42:  # ↓ Down: 履歴を進む
                        if hist_idx < len(self._history):
                            hist_idx += 1
                            if hist_idx == len(self._history):
                                line_chars[:] = saved_line
                            else:
                                line_chars[:] = list(self._history[hist_idx])
                            cursor = len(line_chars)
                            redraw()
                    elif b == 0x43:  # → Right
                        if cursor < len(line_chars):
                            cursor += 1
                            self._write(b"\x1b[C")
                    elif b == 0x44:  # ← Left
                        if cursor > 0:
                            cursor -= 1
                            self._write(b"\x1b[D")
                    elif b == 0x48:  # Home
                        if cursor > 0:
                            self._write(f"\x1b[{cursor}D".encode())
                            cursor = 0
                    elif b == 0x46:  # End
                        if cursor < len(line_chars):
                            fwd = len(line_chars) - cursor
                            self._write(f"\x1b[{fwd}C".encode())
                            cursor = len(line_chars)
                    elif b == 0x7e:  # ~ : Delete キー (ESC [ 3 ~)
                        if csi_buf == "3" and cursor < len(line_chars):
                            line_chars.pop(cursor)
                            redraw()
                    csi_buf = ""
                elif 0x30 <= b <= 0x3f:  # Parameter byte
                    csi_buf += chr(b)
                else:
                    esc_state = 0
                    csi_buf = ""
                continue

            # ---- 通常文字処理 ----
            if b == 0x1b:  # ESC
                esc_state = 1

            elif b in (0x0d, 0x0a):  # CR / LF → 行確定
                if b == 0x0d and self._buf and self._buf[0] in (0, 0x0a):
                    self._buf = self._buf[1:]
                self._write(CRLF)
                text = "".join(line_chars)
                if text.strip():
                    self._history.append(text)
                    if len(self._history) > self._history_max:
                        self._history.pop(0)
                return text

            elif b in (0x08, 0x7f):  # Backspace / DEL
                if cursor > 0:
                    cursor -= 1
                    line_chars.pop(cursor)
                    redraw()

            elif b == 0x01:  # Ctrl+A: 行頭へ
                if cursor > 0:
                    self._write(f"\x1b[{cursor}D".encode())
                    cursor = 0

            elif b == 0x05:  # Ctrl+E: 行末へ
                if cursor < len(line_chars):
                    fwd = len(line_chars) - cursor
                    self._write(f"\x1b[{fwd}C".encode())
                    cursor = len(line_chars)

            elif b == 0x10:  # Ctrl+P: 履歴を遡る (↑ と同等)
                if hist_idx > 0:
                    if hist_idx == len(self._history):
                        saved_line = line_chars[:]
                    hist_idx -= 1
                    line_chars[:] = list(self._history[hist_idx])
                    cursor = len(line_chars)
                    redraw()

            elif b == 0x0e:  # Ctrl+N: 履歴を進む (↓ と同等)
                if hist_idx < len(self._history):
                    hist_idx += 1
                    if hist_idx == len(self._history):
                        line_chars[:] = saved_line
                    else:
                        line_chars[:] = list(self._history[hist_idx])
                    cursor = len(line_chars)
                    redraw()

            elif b == 0x15:  # Ctrl+U: 行を全消去
                line_chars.clear()
                cursor = 0
                redraw()

            elif b == 0x0b:  # Ctrl+K: カーソル以降を削除
                if cursor < len(line_chars):
                    del line_chars[cursor:]
                    redraw()

            elif b == 0x03:  # Ctrl+C: 入力をキャンセル
                self._write(b"\r\n")
                return ""

            elif b == 0x04:  # Ctrl+D: exit と同等
                self._write(b"\r\n")
                return "exit"

            elif b == 0x09:  # Tab: 補完
                text_so_far = "".join(line_chars[:cursor])
                completions = self._complete(text_so_far)
                if len(completions) == 1:
                    comp = completions[0]
                    if not comp.endswith(" "):
                        comp += " "
                    # カーソルより前の「最後の単語の開始位置」を求め、
                    # その手前のプレフィックス部分は保持する
                    last_space = text_so_far.rfind(" ")
                    prefix = text_so_far[:last_space + 1]  # "show " など (末尾スペース含む)
                    tail = line_chars[cursor:]
                    line_chars[:] = list(prefix + comp) + tail
                    cursor = len(prefix) + len(comp)
                    redraw()
                elif len(completions) > 1:
                    self._write(b"\r\n  " + "  ".join(completions).encode() + b"\r\n")
                    self._write(prompt_line.encode())
                    self._write("".join(line_chars).encode())
                    if cursor < len(line_chars):
                        back = len(line_chars) - cursor
                        self._write(f"\x1b[{back}D".encode())

            elif b == ord("?") and cursor == len(line_chars):  # ? : インラインヘルプ
                text_so_far = "".join(line_chars)
                self._write(b"?\r\n")
                self._write(self._inline_help(text_so_far).encode())
                self._write(prompt_line.encode())
                self._write("".join(line_chars).encode())

            elif b >= 0x20:  # 印字可能文字 (文字挿入)
                ch = chr(b)
                line_chars.insert(cursor, ch)
                cursor += 1
                if cursor == len(line_chars):
                    self._write(bytes([b]))  # 行末なら 1 文字エコーで十分
                else:
                    redraw()  # 行中挿入は再描画が必要

    @staticmethod
    def _strip_iac(data: bytes) -> bytes:
        """
        Telnet IAC シーケンスをバイト列から取り除く。
        3バイト (IAC <cmd> <opt>) および
        IAC SB ... IAC SE サブネゴシエーションを除去する。
        """
        result = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if b == 255:  # IAC
                if i + 1 >= len(data):
                    # 不完全 IAC: バッファに残す
                    result.extend(data[i:])
                    break
                cmd = data[i + 1]
                if cmd == 250:  # SB - サブネゴシエーション
                    # IAC SE まで読み飛ばす
                    end = data.find(bytes([255, 240]), i + 2)
                    if end == -1:
                        result.extend(data[i:])
                        break
                    i = end + 2
                elif cmd in (251, 252, 253, 254):  # WILL/WONT/DO/DONT
                    i += 3  # 3バイトスキップ
                elif cmd == 255:  # IAC IAC (エスケープされた 0xFF)
                    result.append(255)
                    i += 2
                else:
                    i += 2  # 2バイトスキップ
            else:
                result.append(b)
                i += 1
        return bytes(result)


# ---------------------------------------------------------------------------
# CLI サーバー
# ---------------------------------------------------------------------------

class CLIServer:
    """
    TCP ポートで CLI セッションを受け付けるサーバー。

    使用例::

        cdb = CDB()
        server = CLIServer(cdb, port=2023)
        server.start()
        # telnet localhost 2023 で接続

    Parameters
    ----------
    cdb : CDB
        バックエンドの設定データベース
    host : str
        リッスンアドレス (デフォルト "127.0.0.1")
    port : int
        リッスンポート (デフォルト 2023)
    style : str
        CLI スタイル: "c" (Cisco C/I 風, デフォルト) または "j" (Juniper J 風)
    hostname : str
        プロンプトに表示するホスト名
    username : str
        セッションのユーザー名 (将来的には認証で置き換え)
    """

    def __init__(
        self,
        cdb: CDB,
        host: str = "127.0.0.1",
        port: int = 2023,
        style: str = STYLE_C,
        hostname: str = "pyconfd",
        username: str = "admin",
        schema=None,
    ):
        self._cdb = cdb
        self._host = host
        self._port = port
        self._style = style
        self._hostname = hostname
        self._username = username
        self._schema = schema
        self._server_sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self):
        """バックグラウンドスレッドでサーバーを起動する"""
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind((self._host, self._port))
        self._server_sock.listen(10)
        self._running = True
        self._thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="cli-accept"
        )
        self._thread.start()
        log.info(
            "CLI サーバー起動: %s:%d (スタイル=%s)",
            self._host, self._port, self._style.upper()
        )

    def stop(self):
        """サーバーを停止する"""
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
        log.info("CLI サーバー停止")

    def _accept_loop(self):
        while self._running:
            try:
                rlist, _, _ = select.select([self._server_sock], [], [], 1.0)
                if not rlist:
                    continue
                conn, addr = self._server_sock.accept()
                log.info("CLI 接続: %s", addr)
                sess = CLISession(
                    conn, addr, self._cdb,
                    style=self._style,
                    hostname=self._hostname,
                    username=self._username,
                    schema=self._schema,
                )
                t = threading.Thread(
                    target=sess.run, daemon=True,
                    name=f"cli-session-{sess.session_id}",
                )
                t.start()
            except OSError:
                break
            except Exception:
                log.exception("CLI accept loop 例外")


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _coerce_value(s: str):
    """文字列を適切な Python 型に変換する (int → float → str の順で試みる)"""
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    # 真偽値
    if s.lower() in ("true", "yes"):
        return True
    if s.lower() in ("false", "no"):
        return False
    return s
