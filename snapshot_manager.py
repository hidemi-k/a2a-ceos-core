#!/usr/bin/env python3
# Copyright (c) 2026 hidemi-k
# Licensed under the MIT License.
"""
snapshot_manager.py — eAPI スナップショット管理モジュール
=========================================================
diagnose_a2a_server.py から import して使用する。

【設計方針】
  - 追加ライブラリ不要（Python 標準ライブラリのみ: json / pathlib / os）
  - コマンドごとに「最新の正常状態」を上書き保存（常に1件が正解）
  - インメモリキャッシュ併用でディスクI/Oを最小化
  - ファイル名: {host}__{safe_cmd}.json（固定名・上書き）

【環境変数】
  SNAPSHOT_STORE : 保存ディレクトリ（デフォルト: ./eapi_snapshots）

【使い方】
  from snapshot_manager import SnapshotManager

  mgr = SnapshotManager()

  # 正常時に保存（cronジョブや手動スナップショット取得時）
  mgr.save(host="172.20.100.31", command="show interfaces", output="...")

  # 障害診断時に読み込み
  snap = mgr.load(host="172.20.100.31", command="show interfaces")
  # snap = {"host": ..., "command": ..., "output": ..., "captured_at": ...}
  # snap = None（スナップショット未取得の場合）
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("snapshot_manager")

# デフォルト保存先（環境変数で上書き可能）
_DEFAULT_STORE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "eapi_snapshots"
)


class SnapshotManager:
    """
    eAPI コマンド出力（CLIテキスト）を正常時スナップショットとして管理する。

    保存戦略:
      - インメモリキャッシュ（_cache）に常に保持 → 再読み込み不要
      - ディスク（JSONファイル）に上書き保存 → プロセス再起動後も復元可能
      - ファイル名は固定（コマンドごとに1ファイル）→ 最新の正解が常に1件

    anta_verify_a2a_server.py の _save_snapshot / _load_snapshot パターンを
    diagnose_a2a_server.py のユースケース（CLIテキスト上書き管理）向けに改変。
    """

    def __init__(self, snapshot_dir: Optional[str] = None):
        self._dir   = Path(snapshot_dir or os.getenv("SNAPSHOT_STORE", _DEFAULT_STORE))
        self._cache: Dict[str, Dict] = {}
        self._dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"[SnapshotManager] 保存先: {self._dir}")

    # ─────────────────────────────────────────────────────────────────────────
    # 内部: ファイルパス生成
    # ─────────────────────────────────────────────────────────────────────────

    def _cache_key(self, host: str, command: str) -> str:
        """インメモリキャッシュのキー"""
        return f"{host}::{command}"

    def _file_path(self, host: str, command: str) -> Path:
        """
        ディスク上のファイルパス。
        コマンド文字列のスペース・スラッシュをアンダースコアに変換。
        例: "show ip bgp summary" → "172.20.100.31__show_ip_bgp_summary.json"
        """
        safe_cmd  = command.strip().replace(" ", "_").replace("/", "-")
        safe_host = host.replace(".", "_").replace(":", "-")
        return self._dir / f"{safe_host}__{safe_cmd}.json"

    # ─────────────────────────────────────────────────────────────────────────
    # 保存
    # ─────────────────────────────────────────────────────────────────────────

    def save(self, host: str, command: str, output: str) -> None:
        """
        正常時の eAPI CLI 出力を上書き保存する。

        Args:
            host    : eAPI 接続先ホスト（IPアドレスまたはホスト名）
            command : show コマンド文字列（例: "show interfaces"）
            output  : encoding="text" で取得した CLI テキスト出力
        """
        data = {
            "host":        host,
            "command":     command,
            "output":      output,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
        }

        # メモリキャッシュに保存
        key = self._cache_key(host, command)
        self._cache[key] = data

        # ディスクに上書き保存
        path = self._file_path(host, command)
        try:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(f"[SnapshotManager] 保存完了: {path.name} ({len(output)}文字)")
        except OSError as e:
            logger.warning(f"[SnapshotManager] ディスク保存失敗（メモリのみ継続）: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # 読み込み
    # ─────────────────────────────────────────────────────────────────────────

    def load(self, host: str, command: str) -> Optional[Dict]:
        """
        スナップショットを読み込む。

        Returns:
            {"host", "command", "output", "captured_at"} の辞書、
            または None（スナップショット未取得）
        """
        key = self._cache_key(host, command)

        # メモリキャッシュヒット
        if key in self._cache:
            return self._cache[key]

        # ディスクから復元
        path = self._file_path(host, command)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._cache[key] = data   # 次回はメモリから
                logger.info(f"[SnapshotManager] ディスクから復元: {path.name}")
                return data
            except (OSError, json.JSONDecodeError) as e:
                logger.error(f"[SnapshotManager] 読み込みエラー: {path.name} → {e}")

        return None

    # ─────────────────────────────────────────────────────────────────────────
    # 一覧・削除
    # ─────────────────────────────────────────────────────────────────────────

    def list_commands(self, host: str) -> List[Dict]:
        """
        指定ホストの保存済みスナップショット一覧を返す。

        Returns:
            [{"command": str, "captured_at": str}, ...] （captured_at 降順）
        """
        items: List[Dict] = []
        safe_host = host.replace(".", "_").replace(":", "-")

        for path in sorted(self._dir.glob(f"{safe_host}__*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                items.append({
                    "command":     data.get("command", ""),
                    "captured_at": data.get("captured_at", ""),
                    "output_len":  len(data.get("output", "")),
                })
            except Exception:
                pass
        return items

    def delete(self, host: str, command: str) -> bool:
        """
        指定コマンドのスナップショットを削除する。

        Returns:
            True: 削除成功、False: 対象なし
        """
        key  = self._cache_key(host, command)
        path = self._file_path(host, command)

        self._cache.pop(key, None)

        if path.exists():
            path.unlink()
            logger.info(f"[SnapshotManager] 削除: {path.name}")
            return True
        return False

    def clear_host(self, host: str) -> int:
        """指定ホストの全スナップショットを削除して削除件数を返す。"""
        safe_host = host.replace(".", "_").replace(":", "-")
        count     = 0
        for path in list(self._dir.glob(f"{safe_host}__*.json")):
            # キャッシュからも削除
            for key in [k for k in self._cache if k.startswith(f"{host}::")]:
                self._cache.pop(key, None)
            path.unlink()
            count += 1
        logger.info(f"[SnapshotManager] {host} のスナップショット {count} 件削除")
        return count
