"""
SSH CLI サーバー

asyncssh を使った暗号化 CLI サーバーです。
cli_server.py の CLISession のすべての CLI ロジックを再利用し、
トランスポート層だけを SSH (asyncssh) に差し替えています。

接続方法::
    ssh -p 2222 admin@localhost

ホスト鍵:
    host_key_path で指定したファイルから RSA 鍵を読み込みます。
    ファイルが存在しない場合は起動時に自動生成して保存します。

認証:
    パスワード認証: users パラメータに {"username": "password"} の dict を渡します。
    デフォルトは {"admin": "admin"} です。
    本番環境では適切なパスワードまたは公開鍵認証に変更してください。
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
from .cli_server import STYLE_C, CLISession

log = logging.getLogger(__name__)

_DEFAULT_HOST_KEY_PATH = "pyconfd_host_key"


# ---------------------------------------------------------------------------
# asyncio ↔ threading ブリッジ: socketpair を使った I/O アダプター
# ---------------------------------------------------------------------------

class _ChannelAdapter:
    """
    asyncssh の SSHServerProcess を CLISession が期待する
    socket.socket 互換インターフェースに変換するアダプター。

    socketpair を使うことで select.select() が正しく機能する。
    asyncio 側のリーダータスクが process.stdin からデータを読み、
    socketpair の write 端に転送する。
    CLISession は socketpair の read 端を通常の blocking recv() で読む。
    """

    def __init__(self, process, loop: asyncio.AbstractEventLoop):
        self._process = process
        self._loop = loop
        # read_sock: CLISession 側 (select/recv), write_sock: asyncio リーダータスク側
        self._read_sock, self._write_sock = socket.socketpair()
        # __init__ は asyncio コルーチン内から呼ばれるので ensure_future を使う
        asyncio.ensure_future(self._reader_task())

    async def _reader_task(self):
        """asyncssh stdin からデータを読み、socketpair の write 端に転送する。"""
        try:
            while True:
                data = await self._process.stdin.read(256)
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
        """select.select() に渡す fd を返す。"""
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
# SSH CLI サーバー
# ---------------------------------------------------------------------------

class SSHCLIServer:
    """
    asyncssh ベースの SSH CLI サーバー。

    使用例::

        cdb = CDB()
        server = SSHCLIServer(cdb, port=2222)
        server.start()
        # ssh -p 2222 admin@localhost で接続

    Parameters
    ----------
    cdb : CDB
        バックエンドの設定データベース
    host : str
        リッスンアドレス (デフォルト "127.0.0.1")
    port : int
        リッスンポート (デフォルト 2222)
    style : str
        CLI スタイル: "c" (Cisco C/I 風, デフォルト) または "j" (Juniper J 風)
    hostname : str
        プロンプトに表示するホスト名
    users : dict
        認証ユーザー辞書 {"username": "password"}
    host_key_path : str
        RSA ホスト鍵ファイルのパス。存在しない場合は自動生成する。
    schema : optional
        YangNode ルート (補完・ナビに使用)
    """

    def __init__(
        self,
        cdb: CDB,
        host: str = "127.0.0.1",
        port: int = 2222,
        style: str = STYLE_C,
        hostname: str = "pyconfd",
        users: Optional[dict] = None,
        host_key_path: str = _DEFAULT_HOST_KEY_PATH,
        schema=None,
    ):
        self._cdb = cdb
        self._host = host
        self._port = port
        self._style = style
        self._hostname = hostname
        self._users = users if users is not None else {"admin": "admin"}
        self._host_key_path = host_key_path
        self._schema = schema
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._active_adapters: set = set()
        self._adapters_lock = threading.Lock()

    def start(self):
        """バックグラウンドスレッドで asyncio ループを起動してサーバーを開始する。"""
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="ssh-cli-server"
        )
        self._thread.start()
        log.info(
            "SSH CLI サーバー起動: %s:%d (スタイル=%s)",
            self._host, self._port, self._style.upper(),
        )

    def stop(self):
        """サーバーを停止し、接続中のすべての SSH セッションを強制切断する。"""
        # 接続中セッションの socketpair を閉じて CLISession スレッドをアンブロック
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
        log.info("SSH CLI サーバー停止")

    def _run_loop(self):
        """専用 asyncio ループを作成して SSH サーバーを実行する。"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception:
            log.exception("SSH CLI サーバー asyncio ループ例外")
        finally:
            self._loop.close()

    async def _serve(self):
        host_key = self._load_or_generate_host_key()

        cdb = self._cdb
        style = self._style
        hostname = self._hostname
        schema = self._schema
        users = self._users
        loop = asyncio.get_event_loop()
        active_adapters = self._active_adapters
        adapters_lock = self._adapters_lock

        async def handle_client(process):
            """process_factory として asyncssh から呼ばれるコルーチン。"""
            username = process.get_extra_info("username") or "admin"
            conn_adapter = _ChannelAdapter(process, loop)
            with adapters_lock:
                active_adapters.add(conn_adapter)
            try:
                sess = CLISession(
                    conn=conn_adapter,
                    addr=process.get_extra_info("peername", ("?", 0)),
                    cdb=cdb,
                    style=style,
                    hostname=hostname,
                    username=username,
                    schema=schema,
                    use_telnet=False,
                )
                # CLISession.run() はブロッキングなので executor (スレッド) で実行
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
            encoding=None,       # バイナリモード: stdin は bytes, stdout は bytes
            line_editor=False,   # CLISession が行編集を担当する
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
