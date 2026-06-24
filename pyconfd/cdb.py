"""
CDB (Configuration DataBase)

ConfD CDB と同等のインメモリ・データストアです。

データストア:
  - running    : 現在稼働中の設定
  - candidate  : 編集中の設定 (commit で running に反映)
  - startup    : 起動設定 (オプション)
  - operational: 運用データ

データは Python dict ツリーで管理し、JSON ファイルにパーシストします。
パス表記: '/container/list[key=val]/leaf'
"""

import copy
import json
import os
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# パス操作
# ---------------------------------------------------------------------------

_RE_PRED = re.compile(r"\[(\w+)=([^\]]+)\]")


def _parse_path(path: str) -> List[Tuple[str, Optional[Dict[str, str]]]]:
    """
    '/dhcp/subnets/subnet[net=1.2.3.4][mask=255.0.0.0]/range' を
    [('dhcp', None), ('subnets', None), ('subnet', {'net':'1.2.3.4', 'mask':'255.0.0.0'}), ('range', None)]
    に変換する
    """
    result = []
    for seg in path.strip("/").split("/"):
        if not seg:
            continue
        m = _RE_PRED.search(seg)
        if m:
            name = seg[: m.start()]
            keys: Dict[str, str] = {}
            for mk in _RE_PRED.finditer(seg):
                v = mk.group(2).strip("'\"")
                keys[mk.group(1)] = v
            result.append((name, keys))
        else:
            result.append((seg, None))
    return result


def _navigate(tree: Any, parts: List[Tuple[str, Optional[Dict[str, str]]]], create: bool = False) -> Tuple[Any, str]:
    """
    parts で指定されたパスをたどり、(parent_node, last_key) を返す。
    create=True の場合、途中の dict がなければ作成する。
    list ノードは [ {'_key_leaf': val, ...}, ... ] の形式で管理する。
    """
    node = tree
    for idx, (name, keys) in enumerate(parts[:-1]):
        if isinstance(node, dict):
            if name not in node:
                if not create:
                    raise KeyError(f"パス要素 '{name}' が見つかりません")
                node[name] = {} if keys is None else []
            child = node[name]
        else:
            raise KeyError(f"'{name}' に到達できません (親がリストまたはスカラー)")

        if keys is not None:
            # リストエントリを探す
            if not isinstance(child, list):
                if create:
                    node[name] = []
                    child = node[name]
                else:
                    raise KeyError(f"'{name}' はリストではありません")
            entry = _find_list_entry(child, keys)
            if entry is None:
                if not create:
                    raise KeyError(f"リストエントリ {name}{keys} が見つかりません")
                entry = dict(keys)
                child.append(entry)
            node = entry
        else:
            node = child

    return node, parts[-1][0]


def _find_list_entry(lst: list, keys: Dict[str, str]) -> Optional[dict]:
    for entry in lst:
        if all(str(entry.get(k)) == str(v) for k, v in keys.items()):
            return entry
    return None


# ---------------------------------------------------------------------------
# メインCDBクラス
# ---------------------------------------------------------------------------

class CDB:
    """
    ConfD CDB 互換データストア

    使用例::

        cdb = CDB(db_dir="/var/confd/cdb")
        cdb.set("/dhcp/default-lease-time", "PT600S")
        val = cdb.get("/dhcp/default-lease-time")
        cdb.commit()
    """

    DATASTORES = ("running", "candidate", "startup", "operational")

    def __init__(self, db_dir: str = "./confd-cdb"):
        self._db_dir = db_dir
        os.makedirs(db_dir, exist_ok=True)

        self._lock = threading.RLock()
        self._stores: Dict[str, dict] = {ds: {} for ds in self.DATASTORES}

        # サブスクリプション: path_prefix -> list of callbacks
        self._subscriptions: Dict[str, List[Callable]] = {}

        # 変更ジャーナル (candidate への変更を追跡)
        self._pending_changes: List[Tuple[str, str, Any]] = []  # (op, path, value)

        self._load_all()

    # ---- パーシスト ----

    def _store_file(self, datastore: str) -> str:
        return os.path.join(self._db_dir, f"{datastore}.json")

    def _load_all(self):
        for ds in self.DATASTORES:
            path = self._store_file(ds)
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        self._stores[ds] = json.load(f)
                except (json.JSONDecodeError, OSError):
                    self._stores[ds] = {}

    def _save(self, datastore: str):
        path = self._store_file(datastore)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._stores[datastore], f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    # ---- 基本アクセス ----

    def get(self, path: str, datastore: str = "running") -> Any:
        """path で示すノードの値を返す"""
        with self._lock:
            tree = self._stores[datastore]
            parts = _parse_path(path)
            if not parts:
                return tree
            parent, key = _navigate(tree, parts)
            if isinstance(parent, dict) and key in parent:
                return parent[key]
            raise KeyError(f"'{path}' が見つかりません (datastore={datastore})")

    def set(self, path: str, value: Any, datastore: str = "candidate"):
        """path で示すノードに値をセットする"""
        with self._lock:
            tree = self._stores[datastore]
            parts = _parse_path(path)
            if not parts:
                raise ValueError("ルートパスへの set はできません")
            parent, key = _navigate(tree, parts, create=True)
            if isinstance(parent, dict):
                parent[key] = value
            elif isinstance(parent, list):
                # 末尾にエントリを追加 (create 系)
                parent.append({key: value})
            self._pending_changes.append(("set", path, value))

    def delete(self, path: str, datastore: str = "candidate"):
        """path で示すノードを削除する"""
        with self._lock:
            tree = self._stores[datastore]
            parts = _parse_path(path)
            parent, key = _navigate(tree, parts)

            name, keys = parts[-1]
            if keys is not None:
                # リストエントリの削除
                lst = parent if isinstance(parent, list) else parent.get(key, [])
                if isinstance(parent, dict):
                    lst = parent.get(name, [])
                    entry = _find_list_entry(lst, keys)
                    if entry is not None:
                        lst.remove(entry)
                    else:
                        raise KeyError(f"'{path}' が見つかりません")
            elif isinstance(parent, dict) and key in parent:
                del parent[key]
            else:
                raise KeyError(f"'{path}' が見つかりません")
            self._pending_changes.append(("delete", path, None))

    def exists(self, path: str, datastore: str = "running") -> bool:
        """パスが存在するかどうか"""
        try:
            self.get(path, datastore=datastore)
            return True
        except KeyError:
            return False

    def get_list(self, path: str, datastore: str = "running") -> list:
        """リストノードをそのまま返す"""
        val = self.get(path, datastore=datastore)
        if isinstance(val, list):
            return val
        return [val]

    def num_instances(self, path: str, datastore: str = "running") -> int:
        """リストのエントリ数を返す"""
        try:
            lst = self.get(path, datastore=datastore)
            if isinstance(lst, list):
                return len(lst)
            return 0
        except KeyError:
            return 0

    def subtree(self, path: str = "/", datastore: str = "running") -> dict:
        """path 以下のサブツリーを dict で返す (XML 生成などに使用)"""
        if path in ("/", ""):
            return copy.deepcopy(self._stores[datastore])
        return copy.deepcopy(self.get(path, datastore=datastore))

    # ---- トランザクション操作 ----

    def start_transaction(self):
        """candidate を running のコピーで初期化"""
        with self._lock:
            self._stores["candidate"] = copy.deepcopy(self._stores["running"])
            self._pending_changes.clear()

    def commit(self) -> List[Tuple[str, str, Any]]:
        """
        candidate を running に反映し、変更済みパスのサブスクライバを通知する。
        Returns: コミットされた変更のリスト
        """
        with self._lock:
            changes = list(self._pending_changes)
            self._stores["running"] = copy.deepcopy(self._stores["candidate"])
            self._pending_changes.clear()
            self._save("running")

        # サブスクライバへの通知 (ロック外で実行)
        changed_paths = {c[1] for c in changes}
        self._notify_subscribers(changed_paths)
        return changes

    def abort(self):
        """candidate を捨て、running に戻す"""
        with self._lock:
            self._stores["candidate"] = copy.deepcopy(self._stores["running"])
            self._pending_changes.clear()

    # ---- サブスクリプション ----

    def subscribe(self, path_prefix: str, callback: Callable[[List[str]], None]):
        """
        指定パスプレフィックス配下の変更を受け取るコールバックを登録する。

        callback(changed_paths: list[str]) が呼ばれる。
        """
        with self._lock:
            self._subscriptions.setdefault(path_prefix, []).append(callback)

    def _notify_subscribers(self, changed_paths: set):
        for prefix, callbacks in list(self._subscriptions.items()):
            matched = [p for p in changed_paths if p.startswith(prefix)]
            if matched:
                for cb in callbacks:
                    try:
                        cb(matched)
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).exception(
                            "CDB サブスクライバ例外: %s", e
                        )

    # ---- デバッグ ----

    def dump(self, datastore: str = "running") -> str:
        """データストアの内容を JSON 文字列で返す"""
        return json.dumps(self._stores[datastore], indent=2, ensure_ascii=False)
