"""
MAAPI (Management Agent API)

ConfD の MAAPI に相当する Python 内部 API です。
CDB へのトランザクション付きアクセスを提供します。

使用例::

    maapi = MAAPI(cdb)
    with maapi.start_write_trans() as t:
        t.set("/dhcp/default-lease-time", "PT600S")
        t.commit()
"""

import copy
import threading
from typing import Any, List, Optional, Tuple

from .cdb import CDB


class TransactionError(Exception):
    """トランザクション操作エラー"""


class Transaction:
    """
    1つの読み書きトランザクション。
    MAAPI.start_write_trans() / start_read_trans() で生成される。
    """

    def __init__(self, cdb: "CDB", writable: bool = True):
        self._cdb = cdb
        self._writable = writable
        self._active = True
        # 書き込みトランザクション: candidate を running のコピーで初期化
        if writable:
            cdb.start_transaction()

    # ---- コンテキストマネージャー ----

    def __enter__(self) -> "Transaction":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._active:
            if exc_type is None and self._writable:
                # 例外がなければ自動コミット
                self.commit()
            else:
                self.abort()
        return False

    # ---- 読み取り ----

    def get(self, path: str) -> Any:
        """running データストアから値を取得"""
        self._check_active()
        if self._writable:
            return self._cdb.get(path, datastore="candidate")
        return self._cdb.get(path, datastore="running")

    def exists(self, path: str) -> bool:
        self._check_active()
        ds = "candidate" if self._writable else "running"
        return self._cdb.exists(path, datastore=ds)

    def get_list(self, path: str) -> list:
        self._check_active()
        ds = "candidate" if self._writable else "running"
        return self._cdb.get_list(path, datastore=ds)

    def num_instances(self, path: str) -> int:
        self._check_active()
        ds = "candidate" if self._writable else "running"
        return self._cdb.num_instances(path, datastore=ds)

    # ---- 書き込み ----

    def set(self, path: str, value: Any):
        """candidate に値をセット"""
        self._check_active()
        if not self._writable:
            raise TransactionError("読み取り専用トランザクションには書き込めません")
        self._cdb.set(path, value, datastore="candidate")

    def delete(self, path: str):
        """candidate からノードを削除"""
        self._check_active()
        if not self._writable:
            raise TransactionError("読み取り専用トランザクションには書き込めません")
        self._cdb.delete(path, datastore="candidate")

    def create(self, path: str, keys: dict):
        """
        リストエントリを作成する。
        例: t.create('/dhcp/subnets/subnet', {'net': '10.0.0.0', 'mask': '255.0.0.0'})
        """
        self._check_active()
        if not self._writable:
            raise TransactionError("読み取り専用トランザクションには書き込めません")
        tree = self._cdb._stores["candidate"]
        from .cdb import _parse_path, _navigate, _find_list_entry

        parts = _parse_path(path)
        if not parts:
            raise ValueError("ルートパスへの create はできません")

        parent, list_name = _navigate(tree, parts, create=True)
        if not isinstance(parent, dict):
            raise ValueError(f"'{path}' の親が dict ではありません")
        lst = parent.setdefault(list_name, [])
        if not isinstance(lst, list):
            raise ValueError(f"'{path}' がリストではありません")
        if _find_list_entry(lst, keys) is None:
            lst.append(dict(keys))

    # ---- コミット/アボート ----

    def commit(self) -> List[Tuple[str, str, Any]]:
        """変更を running に反映"""
        self._check_active()
        self._active = False
        return self._cdb.commit()

    def abort(self):
        """変更を破棄"""
        if self._active:
            self._active = False
            self._cdb.abort()

    def _check_active(self):
        if not self._active:
            raise TransactionError("トランザクションはすでに終了しています")


class MAAPI:
    """
    ConfD MAAPI 互換クラス。

    CDB へのトランザクション付き読み書きを行います。

    使用例::

        cdb = CDB()
        m = MAAPI(cdb)
        with m.start_write_trans() as t:
            t.set("/dhcp/default-lease-time", "PT600S")
            # __exit__ で自動 commit
    """

    def __init__(self, cdb: CDB):
        self._cdb = cdb
        self._lock = threading.Lock()

    def start_write_trans(self) -> Transaction:
        """書き込みトランザクションを開始する"""
        return Transaction(self._cdb, writable=True)

    def start_read_trans(self) -> Transaction:
        """読み取り専用トランザクションを開始する"""
        return Transaction(self._cdb, writable=False)

    # ---- ショートカット API ----

    def get(self, path: str) -> Any:
        """running データストアから直接値を取得 (トランザクションなし)"""
        return self._cdb.get(path, datastore="running")

    def set(self, path: str, value: Any):
        """
        running データストアへ直接セット (即時コミット)。
        シンプルな設定変更に使用。
        """
        with self.start_write_trans() as t:
            t.set(path, value)

    def exists(self, path: str) -> bool:
        return self._cdb.exists(path, datastore="running")

    def dump(self) -> str:
        return self._cdb.dump("running")

    # ---- サブスクリプション ----

    def subscribe(self, path_prefix: str, callback):
        """
        設定変更のサブスクリプションを登録する。
        callback(changed_paths: list[str]) が呼ばれる。
        """
        self._cdb.subscribe(path_prefix, callback)
