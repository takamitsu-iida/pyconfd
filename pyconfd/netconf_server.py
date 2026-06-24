"""
NETCONF サーバー (RFC 6241)

ConfD の NETCONF インターフェースに相当する TCP ベースの NETCONF サーバーです。

対応オペレーション:
  <hello>
  <get>
  <get-config>
  <edit-config>
  <commit>
  <discard-changes>
  <lock> / <unlock>  (スタブ)
  <close-session>
  <kill-session>  (スタブ)
  <validate>  (スタブ)

フレーミング: NETCONF 1.0 (メッセージ区切り ]]>]]>) および
              NETCONF 1.1 (チャンク: #<N>\\n ... ##\\n)
"""

import logging
import re
import select
import socket
import threading
import traceback
import xml.etree.ElementTree as ET
from typing import Dict, Optional

from .cdb import CDB
from .maapi import MAAPI

log = logging.getLogger(__name__)

# NETCONF 名前空間
NS_BASE_1_0 = "urn:ietf:params:netconf:base:1.0"
NS_BASE_1_1 = "urn:ietf:params:netconf:base:1.1"
NS_MSGS     = "urn:ietf:params:xml:ns:netconf:base:1.0"

MSG_SEP = b"]]>]]>"  # NETCONF 1.0 区切り

# デフォルトの <capabilities> リスト
BASE_CAPS = [
    "urn:ietf:params:netconf:base:1.0",
    "urn:ietf:params:netconf:base:1.1",
    "urn:ietf:params:netconf:capability:writable-running:1.0",
    "urn:ietf:params:netconf:capability:candidate:1.0",
    "urn:ietf:params:netconf:capability:rollback-on-error:1.0",
    "urn:ietf:params:netconf:capability:validate:1.1",
]


# ---------------------------------------------------------------------------
# XML ヘルパー
# ---------------------------------------------------------------------------

def _tag(local: str) -> str:
    return f"{{{NS_MSGS}}}{local}"


def _rpc_reply(message_id: str, body: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<rpc-reply message-id="{message_id}" '
        f'xmlns="{NS_MSGS}">\n'
        f'{body}\n'
        f'</rpc-reply>'
    )


def _ok_reply(message_id: str) -> str:
    return _rpc_reply(message_id, "  <ok/>")


def _error_reply(message_id: str, error_type: str, tag: str, msg: str) -> str:
    body = (
        f'  <rpc-error>\n'
        f'    <error-type>{error_type}</error-type>\n'
        f'    <error-tag>{tag}</error-tag>\n'
        f'    <error-severity>error</error-severity>\n'
        f'    <error-message xml:lang="en">{_esc(msg)}</error-message>\n'
        f'  </rpc-error>'
    )
    return _rpc_reply(message_id, body)


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )


def _dict_to_xml(d, tag: str, ns: str = "") -> str:
    """dict/list/scalar を XML 文字列に変換する"""
    ns_attr = f' xmlns="{ns}"' if ns else ""
    if isinstance(d, dict):
        inner = "".join(_dict_to_xml(v, k) for k, v in d.items())
        return f"<{tag}{ns_attr}>{inner}</{tag}>"
    elif isinstance(d, list):
        return "".join(_dict_to_xml(item, tag) for item in d)
    else:
        return f"<{tag}{ns_attr}>{_esc(str(d))}</{tag}>"


def _xml_to_dict(elem) -> dict:
    """ET.Element を dict に変換する"""
    result = {}
    for child in elem:
        local = child.tag.split("}", 1)[-1] if "}" in child.tag else child.tag
        val = _xml_to_dict(child) if len(child) else (child.text or "")
        if local in result:
            existing = result[local]
            if not isinstance(existing, list):
                result[local] = [existing]
            result[local].append(val)
        else:
            result[local] = val
    return result


# ---------------------------------------------------------------------------
# セッションハンドラー
# ---------------------------------------------------------------------------

class NetconfSession:
    """1クライアント接続を管理するセッション"""

    _next_session_id = 1
    _id_lock = threading.Lock()

    def __init__(self, conn: socket.socket, addr, cdb: CDB, extra_caps=None):
        with NetconfSession._id_lock:
            self.session_id = NetconfSession._next_session_id
            NetconfSession._next_session_id += 1

        self._conn = conn
        self._addr = addr
        self._cdb = cdb
        self._maapi = MAAPI(cdb)
        self._buf = b""
        self._locked: Optional[str] = None  # ロックしているデータストア
        self._use_chunked = False            # NETCONF 1.1 チャンクモード
        self._caps = list(BASE_CAPS) + (extra_caps or [])
        self._active = True
        self._trans_open = False

    def run(self):
        try:
            self._send_hello()
            self._recv_client_hello()
            while self._active:
                msg = self._recv_message()
                if msg is None:
                    break
                self._handle_message(msg)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        except Exception:
            log.exception("セッション %d 例外", self.session_id)
        finally:
            try:
                self._conn.close()
            except OSError:
                pass
            log.info("セッション %d 切断 (%s)", self.session_id, self._addr)

    # ---- hello ----

    def _send_hello(self):
        caps_xml = "\n".join(
            f"    <capability>{c}</capability>" for c in self._caps
        )
        hello = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<hello xmlns="{NS_MSGS}">\n'
            f'  <capabilities>\n'
            f'{caps_xml}\n'
            f'  </capabilities>\n'
            f'  <session-id>{self.session_id}</session-id>\n'
            f'</hello>'
        )
        self._send_raw(hello.encode())

    def _recv_client_hello(self):
        msg = self._recv_message(initial=True)
        if msg is None:
            return
        try:
            root = ET.fromstring(msg)
            local = root.tag.split("}", 1)[-1] if "}" in root.tag else root.tag
            if local != "hello":
                return
            for cap in root.iter():
                cap_local = cap.tag.split("}", 1)[-1] if "}" in cap.tag else cap.tag
                if cap_local == "capability" and cap.text:
                    if "base:1.1" in cap.text:
                        self._use_chunked = True
        except ET.ParseError:
            pass

    # ---- メッセージ送受信 ----

    def _send_raw(self, data: bytes):
        if self._use_chunked:
            chunk = f"\n#{len(data)}\n".encode() + data + b"\n##\n"
            self._conn.sendall(chunk)
        else:
            self._conn.sendall(data + MSG_SEP)

    def _send_reply(self, xml_str: str):
        self._send_raw(xml_str.encode("utf-8"))

    def _recv_message(self, initial: bool = False) -> Optional[str]:
        """1つの NETCONF メッセージを受信して返す"""
        if not initial and self._use_chunked:
            return self._recv_chunked()
        return self._recv_framed()

    def _recv_framed(self) -> Optional[str]:
        """NETCONF 1.0: ]]>]]> 区切りでメッセージを受信"""
        while MSG_SEP not in self._buf:
            try:
                chunk = self._conn.recv(4096)
            except OSError:
                return None
            if not chunk:
                return None
            self._buf += chunk
        idx = self._buf.index(MSG_SEP)
        msg = self._buf[:idx].decode("utf-8", errors="replace")
        self._buf = self._buf[idx + len(MSG_SEP):]
        return msg.strip()

    def _recv_chunked(self) -> Optional[str]:
        """NETCONF 1.1: チャンクフレーミングでメッセージを受信"""
        result = b""
        while True:
            # ヘッダー行: #<N>
            line = self._read_line()
            if line is None:
                return None
            line = line.strip()
            if line == b"##":
                break
            if line.startswith(b"#"):
                try:
                    size = int(line[1:])
                except ValueError:
                    return None
                result += self._read_exact(size)
        return result.decode("utf-8", errors="replace").strip()

    def _read_line(self) -> Optional[bytes]:
        while b"\n" not in self._buf:
            try:
                chunk = self._conn.recv(4096)
            except OSError:
                return None
            if not chunk:
                return None
            self._buf += chunk
        idx = self._buf.index(b"\n")
        line = self._buf[: idx + 1]
        self._buf = self._buf[idx + 1:]
        return line

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                chunk = self._conn.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            self._buf += chunk
        data = self._buf[:n]
        self._buf = self._buf[n:]
        return data

    # ---- RPC ディスパッチ ----

    def _handle_message(self, msg: str):
        try:
            root = ET.fromstring(msg)
        except ET.ParseError as e:
            log.warning("XML パースエラー: %s", e)
            return

        local = root.tag.split("}", 1)[-1] if "}" in root.tag else root.tag
        if local != "rpc":
            return

        msg_id = root.attrib.get("message-id", "")
        # <rpc> の最初の子要素がオペレーション
        op_elem = next(iter(root), None)
        if op_elem is None:
            self._send_reply(_error_reply(msg_id, "rpc", "missing-element", "操作なし"))
            return

        op = op_elem.tag.split("}", 1)[-1] if "}" in op_elem.tag else op_elem.tag
        handlers = {
            "get":             self._op_get,
            "get-config":      self._op_get_config,
            "edit-config":     self._op_edit_config,
            "commit":          self._op_commit,
            "discard-changes": self._op_discard_changes,
            "lock":            self._op_lock,
            "unlock":          self._op_unlock,
            "close-session":   self._op_close_session,
            "kill-session":    self._op_kill_session,
            "validate":        self._op_validate,
        }
        handler = handlers.get(op)
        if handler:
            handler(msg_id, op_elem)
        else:
            self._send_reply(
                _error_reply(msg_id, "rpc", "operation-not-supported", f"未対応: {op}")
            )

    # ---- 各オペレーション ----

    def _op_get_config(self, msg_id: str, elem):
        source = "running"
        src_elem = elem.find(".//{%s}source" % NS_MSGS)
        if src_elem is None:
            src_elem = elem.find(".//source")
        if src_elem is not None:
            for child in src_elem:
                src_local = child.tag.split("}", 1)[-1] if "}" in child.tag else child.tag
                source = src_local
                break

        data = self._cdb.subtree("/", datastore=source)
        data_xml = _dict_to_xml(data, "data", ns=NS_MSGS)
        self._send_reply(_rpc_reply(msg_id, f"  {data_xml}"))

    def _op_get(self, msg_id: str, elem):
        # operational + running を合成して返す
        data = self._cdb.subtree("/", datastore="running")
        op_data = self._cdb.subtree("/", datastore="operational")
        # 単純マージ
        data.update(op_data)
        data_xml = _dict_to_xml(data, "data", ns=NS_MSGS)
        self._send_reply(_rpc_reply(msg_id, f"  {data_xml}"))

    def _op_edit_config(self, msg_id: str, elem):
        # ターゲット取得
        target = "candidate"
        tgt_elem = elem.find(".//{%s}target" % NS_MSGS)
        if tgt_elem is None:
            tgt_elem = elem.find(".//target")
        if tgt_elem is not None:
            for child in tgt_elem:
                tgt_local = child.tag.split("}", 1)[-1] if "}" in child.tag else child.tag
                target = tgt_local
                break

        config_elem = elem.find(".//{%s}config" % NS_MSGS)
        if config_elem is None:
            config_elem = elem.find(".//config")
        if config_elem is None:
            self._send_reply(
                _error_reply(msg_id, "rpc", "missing-element", "<config> 要素がありません")
            )
            return

        try:
            if target in ("running", "candidate"):
                self._cdb.start_transaction()
                self._apply_edit(config_elem, "candidate")
                # running ターゲットは即時コミット、candidate は <commit> を待つ
                self._cdb.commit()
            self._send_reply(_ok_reply(msg_id))
        except Exception as e:
            self._cdb.abort()
            self._send_reply(
                _error_reply(msg_id, "application", "operation-failed", str(e))
            )

    def _apply_edit(self, config_elem, target: str):
        """<config> 以下の要素を CDB に適用する"""
        config_dict = _xml_to_dict(config_elem)
        self._merge_into(config_dict, "/", target)

    def _merge_into(self, d: dict, prefix: str, datastore: str):
        for key, val in d.items():
            path = f"{prefix.rstrip('/')}/{key}"
            if isinstance(val, dict):
                self._merge_into(val, path, datastore)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        self._merge_into(item, path, datastore)
                    else:
                        self._cdb.set(path, item, datastore=datastore)
            else:
                self._cdb.set(path, val, datastore=datastore)

    def _op_commit(self, msg_id: str, elem):
        try:
            self._cdb.commit()
            self._send_reply(_ok_reply(msg_id))
        except Exception as e:
            self._send_reply(
                _error_reply(msg_id, "application", "operation-failed", str(e))
            )

    def _op_discard_changes(self, msg_id: str, elem):
        self._cdb.abort()
        self._send_reply(_ok_reply(msg_id))

    def _op_lock(self, msg_id: str, elem):
        # ロック機構の簡易スタブ
        self._send_reply(_ok_reply(msg_id))

    def _op_unlock(self, msg_id: str, elem):
        self._send_reply(_ok_reply(msg_id))

    def _op_close_session(self, msg_id: str, elem):
        self._send_reply(_ok_reply(msg_id))
        self._active = False

    def _op_kill_session(self, msg_id: str, elem):
        self._send_reply(_ok_reply(msg_id))

    def _op_validate(self, msg_id: str, elem):
        self._send_reply(_ok_reply(msg_id))


# ---------------------------------------------------------------------------
# サーバー
# ---------------------------------------------------------------------------

class NetconfServer:
    """
    ConfD 互換 NETCONF TCP サーバー

    使用例::

        cdb = CDB("./confd-cdb")
        srv = NetconfServer(cdb, host="127.0.0.1", port=2022)
        srv.start()          # バックグラウンドスレッドで起動
        ...
        srv.stop()
    """

    def __init__(
        self,
        cdb: CDB,
        host: str = "127.0.0.1",
        port: int = 2022,
        extra_caps=None,
    ):
        self._cdb = cdb
        self._host = host
        self._port = port
        self._extra_caps = extra_caps or []
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
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="netconf-accept")
        self._thread.start()
        log.info("NETCONF サーバー起動: %s:%d", self._host, self._port)

    def stop(self):
        """サーバーを停止する"""
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
        log.info("NETCONF サーバー停止")

    def _accept_loop(self):
        while self._running:
            try:
                rlist, _, _ = select.select([self._server_sock], [], [], 1.0)
                if not rlist:
                    continue
                conn, addr = self._server_sock.accept()
                log.info("NETCONF 接続: %s", addr)
                sess = NetconfSession(conn, addr, self._cdb, self._extra_caps)
                t = threading.Thread(
                    target=sess.run, daemon=True,
                    name=f"netconf-session-{sess.session_id}"
                )
                t.start()
            except OSError:
                break
            except Exception:
                log.exception("accept ループ例外")

    @property
    def address(self):
        return (self._host, self._port)
