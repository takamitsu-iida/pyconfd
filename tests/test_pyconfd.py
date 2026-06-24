"""
pyconfd テストスイート
"""

import os
import sys
import time
import socket
import threading
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pyconfd.yang_parser import YangParser, NodeType
from pyconfd.cdb import CDB, _parse_path
from pyconfd.maapi import MAAPI, Transaction, TransactionError
from pyconfd.netconf_server import NetconfServer

# ---------------------------------------------------------------------------
# YANG パーサーテスト
# ---------------------------------------------------------------------------

SIMPLE_YANG = """
module test {
  namespace "http://example.com/test";
  prefix t;

  container config {
    leaf hostname {
      type string;
      default "localhost";
    }
    leaf port {
      type uint16;
      mandatory true;
    }
    list interface {
      key name;
      leaf name { type string; }
      leaf ip   { type string; }
    }
  }
}
"""


class TestYangParser:
    def test_parse_module(self):
        root = YangParser().parse(SIMPLE_YANG)
        assert root.node_type == NodeType.MODULE
        assert root.name == "test"
        assert root.namespace == "http://example.com/test"

    def test_container(self):
        root = YangParser().parse(SIMPLE_YANG)
        cfg = root.get_child("config")
        assert cfg is not None
        assert cfg.node_type == NodeType.CONTAINER

    def test_leaf(self):
        root = YangParser().parse(SIMPLE_YANG)
        cfg = root.get_child("config")
        hostname = cfg.get_child("hostname")
        assert hostname is not None
        assert hostname.node_type == NodeType.LEAF
        assert hostname.default == "localhost"

    def test_list(self):
        root = YangParser().parse(SIMPLE_YANG)
        cfg = root.get_child("config")
        iface = cfg.get_child("interface")
        assert iface is not None
        assert iface.node_type == NodeType.LIST
        assert "name" in iface.keys

    def test_find_path(self):
        root = YangParser().parse(SIMPLE_YANG)
        node = root.find_path("/config/hostname")
        assert node is not None
        assert node.name == "hostname"

    def test_data_children(self):
        root = YangParser().parse(SIMPLE_YANG)
        cfg = root.get_child("config")
        children = cfg.data_children()
        names = [c.name for c in children]
        assert "hostname" in names
        assert "port" in names
        assert "interface" in names


# ---------------------------------------------------------------------------
# パスパーサーテスト
# ---------------------------------------------------------------------------

class TestParsePath:
    def test_simple(self):
        parts = _parse_path("/dhcp/default-lease-time")
        assert parts == [("dhcp", None), ("default-lease-time", None)]

    def test_with_key(self):
        parts = _parse_path("/dhcp/subnets/subnet[net=192.168.1.0][mask=255.255.255.0]")
        assert parts[0] == ("dhcp", None)
        assert parts[2][0] == "subnet"
        assert parts[2][1] == {"net": "192.168.1.0", "mask": "255.255.255.0"}


# ---------------------------------------------------------------------------
# CDB テスト
# ---------------------------------------------------------------------------

@pytest.fixture
def cdb(tmp_path):
    return CDB(db_dir=str(tmp_path / "cdb"))


class TestCDB:
    def test_set_get(self, cdb):
        cdb.set("/dhcp/default-lease-time", 600)
        cdb.commit()
        assert cdb.get("/dhcp/default-lease-time") == 600

    def test_nested(self, cdb):
        cdb.set("/dhcp/subnets/net", "192.168.1.0")
        cdb.commit()
        assert cdb.get("/dhcp/subnets/net") == "192.168.1.0"

    def test_not_found(self, cdb):
        with pytest.raises(KeyError):
            cdb.get("/nonexistent/path")

    def test_exists(self, cdb):
        cdb.set("/dhcp/port", 67)
        cdb.commit()
        assert cdb.exists("/dhcp/port")
        assert not cdb.exists("/dhcp/nonexistent")

    def test_delete(self, cdb):
        cdb.set("/dhcp/port", 67)
        cdb.commit()
        cdb.delete("/dhcp/port", datastore="candidate")
        cdb.commit()
        assert not cdb.exists("/dhcp/port")

    def test_abort(self, cdb):
        cdb.set("/dhcp/port", 67)
        cdb.commit()
        cdb.start_transaction()
        cdb.set("/dhcp/port", 99)
        cdb.abort()
        assert cdb.get("/dhcp/port") == 67

    def test_subscription(self, cdb):
        received = []
        cdb.subscribe("/dhcp", lambda paths: received.extend(paths))

        cdb.set("/dhcp/default-lease-time", 300)
        cdb.commit()
        time.sleep(0.05)
        assert any("/dhcp/default-lease-time" in p for p in received)

    def test_list_operations(self, cdb):
        cdb.start_transaction()
        tree = cdb._stores["candidate"]
        tree.setdefault("dhcp", {}).setdefault("subnets", {})["subnet"] = []
        tree["dhcp"]["subnets"]["subnet"].append({"net": "10.0.0.0", "mask": "255.0.0.0"})
        tree["dhcp"]["subnets"]["subnet"].append({"net": "192.168.1.0", "mask": "255.255.255.0"})
        cdb.commit()

        assert cdb.num_instances("/dhcp/subnets/subnet") == 2

    def test_persist_reload(self, tmp_path):
        db_dir = str(tmp_path / "persist")
        c1 = CDB(db_dir=db_dir)
        c1.set("/app/key", "hello")
        c1.commit()

        c2 = CDB(db_dir=db_dir)
        assert c2.get("/app/key") == "hello"


# ---------------------------------------------------------------------------
# MAAPI テスト
# ---------------------------------------------------------------------------

class TestMAAPI:
    def test_write_trans(self, cdb):
        m = MAAPI(cdb)
        with m.start_write_trans() as t:
            t.set("/app/value", 42)
        assert m.get("/app/value") == 42

    def test_read_trans(self, cdb):
        m = MAAPI(cdb)
        with m.start_write_trans() as t:
            t.set("/app/value", 99)
        with m.start_read_trans() as t:
            assert t.get("/app/value") == 99

    def test_read_trans_no_write(self, cdb):
        m = MAAPI(cdb)
        with m.start_read_trans() as t:
            with pytest.raises(TransactionError):
                t.set("/app/value", 1)

    def test_abort_on_exception(self, cdb):
        m = MAAPI(cdb)
        with m.start_write_trans() as t:
            t.set("/app/value", 10)
        try:
            with m.start_write_trans() as t:
                t.set("/app/value", 20)
                raise RuntimeError("abort!")
        except RuntimeError:
            pass
        assert m.get("/app/value") == 10

    def test_double_commit_error(self, cdb):
        m = MAAPI(cdb)
        t = m.start_write_trans()
        t.set("/app/val", 1)
        t.commit()
        with pytest.raises(TransactionError):
            t.commit()


# ---------------------------------------------------------------------------
# NETCONF サーバーテスト
# ---------------------------------------------------------------------------

MSG_SEP = b"]]>]]>"


def recv_msg(s):
    buf = b""
    s.settimeout(5)
    while MSG_SEP not in buf:
        chunk = s.recv(8192)
        if not chunk:
            break
        buf += chunk
    idx = buf.find(MSG_SEP)
    return buf[:idx].decode() if idx >= 0 else buf.decode()


def send_msg(s, xml: str):
    s.sendall(xml.encode() + MSG_SEP)


@pytest.fixture
def netconf_server(tmp_path):
    cdb = CDB(db_dir=str(tmp_path / "cdb"))
    # 初期データ
    cdb.set("/dhcp/default-lease-time", 600)
    cdb.commit()
    srv = NetconfServer(cdb, host="127.0.0.1", port=0)
    # ポート 0 はカーネルに割り当てさせる
    import socket as _socket
    srv._server_sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    srv._server_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    srv._server_sock.bind(("127.0.0.1", 0))
    srv._port = srv._server_sock.getsockname()[1]
    srv._server_sock.listen(10)
    srv._running = True
    srv._thread = threading.Thread(target=srv._accept_loop, daemon=True)
    srv._thread.start()
    yield srv
    srv.stop()


HELLO = """\
<?xml version="1.0" encoding="UTF-8"?>
<hello xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <capabilities>
    <capability>urn:ietf:params:netconf:base:1.0</capability>
  </capabilities>
</hello>"""

GET_CONFIG = """\
<?xml version="1.0" encoding="UTF-8"?>
<rpc message-id="1" xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <get-config>
    <source><running/></source>
  </get-config>
</rpc>"""

CLOSE_SESSION = """\
<?xml version="1.0" encoding="UTF-8"?>
<rpc message-id="9" xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <close-session/>
</rpc>"""


class TestNetconfServer:
    def _connect(self, srv):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("127.0.0.1", srv._port))
        # サーバーの hello 受信
        hello = recv_msg(s)
        assert "<hello" in hello
        assert "<session-id>" in hello
        # クライアントの hello 送信
        send_msg(s, HELLO)
        return s

    def test_hello(self, netconf_server):
        s = self._connect(netconf_server)
        send_msg(s, CLOSE_SESSION)
        s.close()

    def test_get_config(self, netconf_server):
        s = self._connect(netconf_server)
        send_msg(s, GET_CONFIG)
        reply = recv_msg(s)
        assert "<rpc-reply" in reply
        assert "default-lease-time" in reply
        send_msg(s, CLOSE_SESSION)
        s.close()

    def test_edit_config_and_commit(self, netconf_server):
        s = self._connect(netconf_server)
        edit = """\
<?xml version="1.0" encoding="UTF-8"?>
<rpc message-id="2" xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <edit-config>
    <target><running/></target>
    <config>
      <dhcp>
        <max-lease-time>9999</max-lease-time>
      </dhcp>
    </config>
  </edit-config>
</rpc>"""
        send_msg(s, edit)
        reply = recv_msg(s)
        assert "<ok/>" in reply

        # 確認
        send_msg(s, GET_CONFIG)
        reply2 = recv_msg(s)
        assert "9999" in reply2

        send_msg(s, CLOSE_SESSION)
        s.close()
