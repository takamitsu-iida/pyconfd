"""
シナリオベースの NETCONF モックレスポンス定義 (Wiremock スタイル)

YAML または JSON ファイルに「このリクエストパターン → この固定応答」を定義します。
NETCONF サーバーは CDB に問い合わせる前にシナリオをチェックし、
マッチした場合は固定 XML を返します。

使用例::

    from pyconfd.scenario import ScenarioMatcher
    matcher = ScenarioMatcher.from_file("mock-scenarios.yaml")
    server = NetconfSSHServer(cdb, port=830, scenario_matcher=matcher)

シナリオファイルフォーマット (YAML):

    scenarios:
      - name: "get-config running"
        match:
          operation: get-config     # get / get-config / edit-config / validate / *
          source: running           # running / candidate (省略可: 任意にマッチ)
          filter_tag: dhcp         # filter XML にこのタグが含まれる場合のみマッチ (省略可)
        response:
          body: |                   # インライン XML (<rpc-reply> の中身)
            <data>...</data>

      - name: "edit-config always ok"
        match:
          operation: edit-config
        response:
          ok: true                  # <ok/> を返す

      - name: "get from file"
        match:
          operation: get
        response:
          file: responses/get.xml  # ファイルから読み込む (シナリオファイルからの相対パス)

      - name: "無効化済みシナリオ"
        disabled: true             # true にするとこのシナリオはロードされない
        match:
          operation: get-config
        response:
          ok: true

サポートフォーマット:
    .yaml / .yml  -- PyYAML が必要 (pip install pyyaml)
    .json         -- 標準ライブラリのみで動作
"""

import json
import logging
import os
from typing import List, Optional

log = logging.getLogger(__name__)

try:
    import yaml as _yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


class ScenarioMatcher:
    """
    YAML/JSON ファイルに定義された固定応答シナリオを管理するクラス。

    NETCONF リクエストがシナリオにマッチした場合、CDB の代わりに
    固定 XML を返すことで、テスト・CI 環境で実機なしに動作検証できます。

    マッチング優先順位: シナリオファイル内の定義順。最初にマッチしたものが使われる。
    """

    def __init__(self, scenarios: list, base_dir: str = "."):
        # disabled: true のシナリオは読み込まない
        self._scenarios = [s for s in scenarios if not s.get("disabled")]
        self._base_dir = base_dir

    # ---- ファクトリ ----

    @classmethod
    def from_file(cls, path: str) -> "ScenarioMatcher":
        """
        YAML または JSON ファイルからシナリオを読み込む。

        Parameters
        ----------
        path : str
            シナリオファイルのパス (.yaml / .yml / .json)
        """
        base_dir = os.path.dirname(os.path.abspath(path))
        ext = os.path.splitext(path)[1].lower()

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        if ext in (".yaml", ".yml"):
            if not _YAML_AVAILABLE:
                raise ImportError(
                    "YAML シナリオファイルの読み込みには PyYAML が必要です:\n"
                    "  pip install pyyaml"
                )
            data = _yaml.safe_load(content)
        else:
            data = json.loads(content)

        scenarios = data.get("scenarios", []) if data else []
        log.info(
            "ScenarioMatcher: %d シナリオを読み込みました (%s)",
            len(scenarios), path,
        )
        return cls(scenarios, base_dir)

    # ---- マッチング ----

    def match(
        self,
        operation: str,
        source: Optional[str] = None,
        filter_elem=None,
    ) -> Optional[str]:
        """
        リクエストにマッチするシナリオの応答 XML ボディを返す。
        最初にマッチしたシナリオが使われる。マッチなしなら None を返す。

        Parameters
        ----------
        operation : str
            NETCONF オペレーション名 ("get", "get-config", "edit-config" など)
        source : str, optional
            データストア名 ("running", "candidate" など)
        filter_elem : ET.Element, optional
            <filter> 要素

        Returns
        -------
        str or None
            <rpc-reply> に挿入する XML ボディ文字列。
            None の場合は通常の CDB 処理にフォールバックする。
        """
        for scenario in self._scenarios:
            if self._matches(scenario, operation, source, filter_elem):
                name = scenario.get("name", "(無名)")
                log.debug("ScenarioMatcher: '%s' にマッチ (op=%s)", name, operation)
                return self._build_response(scenario.get("response", {}))
        return None

    def _matches(
        self,
        scenario: dict,
        operation: str,
        source: Optional[str],
        filter_elem,
    ) -> bool:
        m = scenario.get("match", {})

        # operation チェック (* はワイルドカード)
        m_op = m.get("operation", "*")
        if m_op != "*" and m_op != operation:
            return False

        # source チェック (省略時は任意にマッチ)
        m_src = m.get("source")
        if m_src and source and m_src != source:
            return False

        # filter_tag チェック
        #   省略時  → 常にマッチ
        #   指定あり かつ filter なし → マッチしない
        #   指定あり かつ filter あり → タグが含まれるかチェック
        m_tag = m.get("filter_tag")
        if m_tag is not None:
            if filter_elem is None:
                return False
            if not self._filter_has_tag(filter_elem, m_tag):
                return False

        return True

    @staticmethod
    def _filter_has_tag(filter_elem, tag: str) -> bool:
        """filter 要素のいずれかのローカル名が tag と一致するか確認する。"""
        for elem in filter_elem.iter():
            local = elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag
            if ":" in local:
                local = local.split(":", 1)[1]
            if local == tag:
                return True
        return False

    def _build_response(self, response: dict) -> str:
        """response 定義から <rpc-reply> に挿入する XML ボディを生成する。"""
        if response.get("ok"):
            return "  <ok/>"
        if "body" in response:
            body = response["body"]
            return body if isinstance(body, str) else str(body)
        if "file" in response:
            fpath = os.path.join(self._base_dir, response["file"])
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except OSError as exc:
                log.error("ScenarioMatcher: レスポンスファイル読み込みエラー: %s", exc)
                return "  <ok/>"
        return "  <ok/>"

    # ---- ユーティリティ ----

    @property
    def scenario_count(self) -> int:
        """ロード済み（disabled でない）シナリオ数を返す。"""
        return len(self._scenarios)

    def scenario_names(self) -> List[str]:
        """ロード済みシナリオ名のリストを返す。"""
        return [s.get("name", "(無名)") for s in self._scenarios]

    def __len__(self) -> int:
        return len(self._scenarios)

    def __repr__(self) -> str:
        return f"ScenarioMatcher({len(self._scenarios)} scenarios)"
