#!/usr/bin/env python3
# Copyright (c) 2026 hidemi-k
# Licensed under the MIT License.
"""
diff_engine.py — eAPI CLIテキスト差分抽出モジュール
====================================================
diagnose_a2a_server.py から import して使用する。

【設計方針】
  - 追加ライブラリ不要（Python 標準ライブラリのみ: difflib）
  - CLIテキスト（encoding="text" 出力）に特化した行単位 Unified Diff
  - LLM に渡す差分テキストを適切なトークン量にキャップ
  - 差分なし・スナップショットなしの両方を明示的に返す

【使い方】
  from diff_engine import extract_diff_summary, has_diff

  summary = extract_diff_summary(snap_output, current_output)
  # → "--- 正常時スナップショット\n+++ 現在の状態\n@@ ... @@\n-  up\n+  down\n..."

  if has_diff(snap_output, current_output):
      # 差分あり → LLMに優先的に渡す
"""

import difflib
import logging
from typing import Optional

logger = logging.getLogger("diff_engine")

# LLM に渡す差分テキストの最大行数（過大なトークン消費を防止）
_MAX_DIFF_LINES = 200


def extract_diff_summary(
    snapshot_output: str,
    current_output: str,
    context_lines: int = 3,
    max_lines: int = _MAX_DIFF_LINES,
) -> str:
    """
    正常時スナップショットと現在の CLI テキスト出力を行単位で比較し、
    LLM に渡す Unified Diff 形式のサマリーテキストを返す。

    Args:
        snapshot_output : 正常時のeAPI CLIテキスト（SnapshotManager.load()["output"]）
        current_output  : 現在のeAPI CLIテキスト（run_command_eapi()["output"]）
        context_lines   : 差分前後に含めるコンテキスト行数（デフォルト: 3）
        max_lines       : 出力の最大行数（デフォルト: 200行）

    Returns:
        差分テキスト（Unified Diff 形式）、
        または差分なしを示すメッセージ文字列
    """
    if not snapshot_output and not current_output:
        return "（正常時スナップショット・現在の出力ともに空）"

    if not snapshot_output:
        return "（正常時スナップショットが空のため差分比較不可）"

    if not current_output:
        return "（現在の出力が空 — コマンド実行エラーまたは設定なし）"

    snap_lines    = snapshot_output.splitlines(keepends=True)
    current_lines = current_output.splitlines(keepends=True)

    diff = list(difflib.unified_diff(
        snap_lines,
        current_lines,
        fromfile="正常時スナップショット",
        tofile="現在の状態",
        lineterm="",
        n=context_lines,
    ))

    if not diff:
        return "（差分なし: 現在の状態は正常時スナップショットと完全に一致しています）"

    # 行数キャップ
    truncated = False
    if len(diff) > max_lines:
        diff      = diff[:max_lines]
        truncated = True

    result = "\n".join(diff)
    if truncated:
        result += (
            f"\n\n（差分が {max_lines} 行を超えたため以降を省略しました。"
            f"上記の差分から異常を判断してください）"
        )

    logger.debug(
        f"[DiffEngine] 差分行数: {len(diff)}行"
        f"{'（省略あり）' if truncated else ''}"
    )
    return result


def has_diff(snapshot_output: str, current_output: str) -> bool:
    """
    差分が存在するかどうかを高速に判定する（テキスト全体の比較）。

    extract_diff_summary() より軽量（difflib.unified_diff を生成せず比較のみ）。
    """
    return snapshot_output.strip() != current_output.strip()


def summarize_diff_briefly(
    snapshot_output: str,
    current_output: str,
) -> str:
    """
    差分の要点を短くまとめる（追加行数・削除行数のカウント）。

    LLM プロンプトの前置きとして使用する。

    Returns:
        例: "差分あり: +12行 / -8行（計20行の変化）"
        例: "差分なし"
    """
    if not has_diff(snapshot_output, current_output):
        return "差分なし"

    snap_lines    = snapshot_output.splitlines()
    current_lines = current_output.splitlines()

    diff = list(difflib.unified_diff(snap_lines, current_lines, lineterm=""))

    added   = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))

    return f"差分あり: +{added}行 / -{removed}行（計{added + removed}行の変化）"
