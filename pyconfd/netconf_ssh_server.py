"""
NETCONF SSH サーバー (RFC 6242)

asyncssh を使って既存の NetconfSession を SSH トランスポートでラップします。
標準の NETCONF ポート 830 (RFC 6242) で動作し、ncclient や Ansible の
netconf_get / netconf_config モジュールから直接接続できます。

接続方法::
    ncclient: manager.connect(host="127.0.0.1", port=830, ...)
    ansible:  ansible_connection: netconf, ansible_port: 830

ホスト鍵:
    host_key_path で指定したファイルから RSA 鍵を読み込みます。
    ファイルが存在しない場合は起動時に自動生成して保存します。

認証:
    パスワード認証: users パラメータに {"username": "password"} の dict を渡します。
    デフォルトは {"admin": "admin"} です。
    本番環境では適切なパスワードまたは公開鍵認証に変更してください。

ncclient 接続例::

    from ncclient import manager
    with manager.connect(
        host="127.0.0.1",
        port=830,
        username="admin",
        password="admin",
        hostkey_verify=False,
    ) as m:
        cfg = m.get_config(source="running")
        print(cfg)
"""

import asyncio
import hmac
import logging
import os
import socket
import threading
from typing import Optional

import asyncssh

from .cdb import CDB
from .netconf_server import NetconfSession

log = logging.getLogger(__name__)

_DEFAULT_HOST_KEY_PATH = "pyconfd_netconf_host_key"
_NETCONF_SUBSYSTEM = "netconf"


# ---------------------------------------------------------------------------
# asyncio ↔ threading ブリッジ: socketpair を使った I/O アダプター
# ---------------------------------------------------------------------------

class _ChannelAdapter:
    """
    asyncssh の SSHServerProcess を NetconfSession が期待する
    socket.socket 互換インターフェースに変換するアダプター。

    ssh_server.py の _ChannelAdapter と同じ構造で、
    NETCONF セッション向けに調整しています。
    """

    def __init__(self, process, loop: asyncio.AbstractEventLoop):
        self._process = process
        self._loop = loop
        # read_sock: NetconfSession 側 (recv), write_sock: asyncio リーダータスク側
        self._read_sock, self._write_sock = socket.socketpair()
        asyncio.ensure_future(self._reader_task())

    async def _reader_task(self):
        """asyncssh stdin からデータを読み、socketpair の write 端に転送する。"""
        try:
            while True:
                data = await self._process.stdin.read(4096)
                if not data:
                    break
                if isinstance(data, str):
                    data = data.encode("utf-8", errors="replace")
                self._write_sock.sendall(data)
        except Exception:
            pass
        finally:
            try:
                self._write_sock.close()
            except OSError:
                pass

    def sendall(self, data: bytes):
        """スレッドから呼ばれる書き込み。asyncio スレッドセーフに stdout.write() を呼ぶ。"""
        if not isinstance(data, (bytes, bytearray)):
            data = str(data).encode("utf-8", errors="replace")
        self._loop.call_soon_threadsafe(self._process.stdout.write, bytes(data))

    def recv(self, bufsize: int) -> bytes:
        """ブロッキング recv。socketpair の read 端から読む。"""
        try:
            return self._read_sock.recv(bufsize)
        except OSError:
            return b""

    def fileno(self):
        return self._read_sock.fileno()

    def close(self):
        try:
            self._read_sock.close()
        except OSError:
            pass
        self._loop.call_soon_threadsafe(self._process.close)


# ---------------------------------------------------------------------------
# asyncssh ServerInterface: 認証
# ---------------------------------------------------------------------------

class _SSHServerInterface(asyncssh.SSHServer):
    """パスワード認証を実装した SSH サーバーインターフェース。"""

    _users: dict = {"admin": "admin"}

    def password_auth_supported(self):
        return True

    def validate_password(self, username: str, password: str) -> bool:
        expected = self._users.get(username)
        if expected is None:
            return False
        # 定数時間比較でタイミング攻撃を防ぐ
        return hmac.compare_digest(expected, password)


# ---------------------------------------------------------------------------
# NETCONF SSH サーバー
# ---------------------------------------------------------------------------

class NetconfSSHServer:
    """
    asyncssh ベースの NETCONF SSH サーバー (RFC 6242)。

    既存の NetconfSession をバックエンドとして使用し、
    SSH トランスポートを追加します。
    ncclient や Ansible から標準の NETCONF クライアントとして接続できます。

    使用例::

        cdb = CDB("./confd-cdb")
        server = NetconfSSHServer(cdb, port=830)
        server.start()
        # ncclient / ansible から 127.0.0.1:830 に接続

    Parameters
    ----------
    cdb : CDB
        バックエンドの設定データベース
    host : str
        リッスンアドレス (デフォルト "127.0.0.1")
    port : int
        リッスンポート (デフォルト 830, RFC 6242 標準ポート)
    users : dict
        認証ユーザー辞書 {"username": "password"}
        デフォルトは {"admin": "admin"}
    host_key_path : str
        RSA ホスト鍵ファイルのパス。存在しない場合は自動生成する。
    extra_caps : list
        追加 NETCONF capability URI のリスト
    """

    def __init__(
        self,
        cdb: CDB,
        host: str = "127.0.0.1",
        port: int = 830,
        users: Optional[dict] = None,
        host_key_path: str = _DEFAULT_HOST_KEY_PATH,
        extra_caps: Optional[list] = None,
        schema_registry=None,
        scenario_matcher=None,
    ):
        self._cdb = cdb
        self._host = host
        self._port = port
        self._users = users if users is not None else {"admin": "admin"}
        self._host_key_path = host_key_path
        self._extra_caps = extra_caps or []
        self._schema_registry = schema_registry
        self._scenario_matcher = scenario_matcher
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._active_adapters: set = set()
        self._adapters_lock = threading.Lock()

    def start(self):
        """バックグラウンドスレッドで asyncio ループを起動してサーバーを開始する。"""
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="netconf-ssh-server"
        )
        self._thread.start()
        log.info("NETCONF SSH サーバー起動: %s:%d", self._host, self._port)

    def stop(self):
        """サーバーを停止し、接続中のすべての SSH セッションを強制切断する。"""
        with self._adapters_lock:
            for adapter in list(self._active_adapters):
                try:
                    adapter._read_sock.close()
                except OSError:
                    pass
                try:
                    adapter._write_sock.close()
                except OSError:
                    pass
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.close)
        log.info("NETCONF SSH サーバー停止")

    def _run_loop(self):
        """専用 asyncio ループを作成して SSH サーバーを実行する。"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception:
            log.exception("NETCONF SSH サーバー asyncio ループ例外")
        finally:
            self._loop.close()

    async def _serve(self):
        host_key = self._load_or_generate_host_key()

        cdb = self._cdb
        extra_caps = self._extra_caps
        schema_registry = self._schema_registry
        scenario_matcher = self._scenario_matcher
        users = self._users
        loop = asyncio.get_event_loop()
        active_adapters = self._active_adapters
        adapters_lock = self._adapters_lock

        async def handle_client(process):
            """process_factory として asyncssh から呼ばれるコルーチン。"""
            peer = process.get_extra_info("peername", ("?", 0))
            conn_adapter = _ChannelAdapter(process, loop)
            with adapters_lock:
                active_adapters.add(conn_adapter)
            try:
                sess = NetconfSession(
                    conn=conn_adapter,
                    addr=peer,
                    cdb=cdb,
                    extra_caps=extra_caps,
                    schema_registry=schema_registry,
                    scenario_matcher=scenario_matcher,
                )
                # NetconfSession.run() はブロッキングなので executor (スレッド) で実行
                await loop.run_in_executor(None, sess.run)
            finally:
                with adapters_lock:
                    active_adapters.discard(conn_adapter)
                process.close()

        class _ServerInterface(_SSHServerInterface):
            _users = users

        self._server = await asyncssh.create_server(
            _ServerInterface,
            self._host,
            self._port,
            server_host_keys=[host_key],
            process_factory=handle_client,
            encoding=None,      # バイナリモード
            line_editor=False,
        )
        async with self._server:
            await self._server.wait_closed()

    def _load_or_generate_host_key(self) -> asyncssh.SSHKey:
        """ホスト鍵を読み込む。ファイルが存在しない場合は RSA 鍵を生成して保存する。"""
        if os.path.exists(self._host_key_path):
            key = asyncssh.read_private_key(self._host_key_path)
            log.info("SSH ホスト鍵を読み込みました: %s", self._host_key_path)
        else:
            key = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
            key.write_private_key(self._host_key_path)
            os.chmod(self._host_key_path, 0o600)
            log.info("SSH ホスト鍵を生成しました: %s", self._host_key_path)
        return key

    @property
    def address(self):
        return (self._host, self._port)
