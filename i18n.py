# Copyright (c) 2026 hidemi-k
# Licensed under the MIT License.
"""
i18n.py — 多言語対応モジュール（全サーバ共通）
=============================================
使い方:
    from i18n import get_msg, set_locale, LOCALE

    # デフォルトは "ja"
    msg = get_msg("success")           # "✅ 成功"
    msg = get_msg("success", "en")     # "✅ Success"

    # リクエストから locale を受け取る場合
    locale = params.get("locale", LOCALE)
    msg = get_msg("deploy_done", locale)

追加方法:
    MESSAGES の "ja" / "en" に同じキーを追加するだけ。
    キーが "en" に存在しない場合は "ja" にフォールバック。
"""

import os

# デフォルトロケール（環境変数 LOCALE で上書き可能）
LOCALE: str = os.getenv("LOCALE", "ja")

MESSAGES: dict = {
    # ── 共通 ──────────────────────────────────────────────────────────────
    "ja": {
        # 汎用ステータス
        "success":          "✅ 成功",
        "failure":          "❌ 失敗",
        "no_changes":       "変更なし（既に最新）",
        "dry_run":          "🔍 ドライラン完了",
        "blocked":          "🚫 ブロック",
        "skipped":          "⏭️ スキップ",
        "not_implemented":  "未実装",
        "error":            "エラーが発生しました",

        # デプロイフロー
        "deploy_start":     "🚀 デプロイ開始...",
        "deploy_done":      "✅ デプロイ完了",
        "deploy_failed":    "❌ デプロイ失敗",
        "deploy_success":   "edit-config 投入完了",
        "dryrun_note":      "(ドライラン — 実機未投入)",

        # 冪等性
        "idempotent_vlan":   "冪等性チェック: VLAN は既に設定済み",
        "idempotent_absent": "冪等性チェック: VLAN は既に存在しません",
        "idempotent_intf":   "冪等性チェック: 設定は既に最新です",
        "idempotent_skip":   "変更不要",

        # タスク集約
        "all_success":      "✅ 全 {n} タスク成功",
        "all_failure":      "❌ 全 {n} タスク失敗",
        "partial_failure":  "⚠️ {ok}成功/{ng}失敗",
        "report_header":    "📊 Arista NETCONF RAG 実行レポート",

        # audit
        "audit_confirmed":  "✅ Confirmed: {type} {target} {op}d",
        "audit_failed":     "❌ Audit failed: {type} {target} not confirmed after {op}",
        "audit_scope_note": (
            "audit は NETCONF get_config (<config>ツリー限定)。"
            "<state>フィルターは cEOS で NETCONF 取得不可（実機確認済み）。"
            "オペレーショナル状態は eAPI サーバ (port:8002) で確認。"
            "Security 操作（block/unblock/qos）は XDP A2A Agent (port:8003) 経由で"
            "Hub (port:8000) の /execute → /deploy 2段階フローで実行。"
            "監視データ（pps/stats/top）は Go IPS API (port:8080) から直接取得。"
        ),

        # eAPI
        "eapi_success":     "✅ 取得成功",
        "eapi_blocked":     "変更系コマンドが検出されました。show コマンドのみ許可されます。",
        "eapi_parse_fail":  "JSONパース失敗",
        "eapi_conn_error":  "eAPI 接続エラー",
        "scope_note_eapi":  "operational-state (eAPI show)",

        # Hub / Router
        # XDP Firewall Agent (xdp_a2a_server / Go IPS API)
        "xdp_block_plan":    "IP {ip}:{port} [{proto}] を XDP ブロックする計画を作成しました。",
        "xdp_unblock_plan":  "IP {ip}:{port} [{proto}] の XDP ブロックを解除する計画を作成しました。",
        "xdp_qos_plan":      "IP {ip} に QoS 制限 {limit}KB/s を設定する計画を作成しました。",
        "xdp_block_ok":      "✅ XDP ブロック完了: {ip}:{port} [{proto}]",
        "xdp_unblock_ok":    "✅ XDP ブロック解除完了: {ip}:{port} [{proto}]",
        "xdp_qos_ok":        "✅ QoS 設定完了: {ip} → {limit}KB/s",
        "xdp_err_block":     "block には ip/proto/port が必要です",
        "xdp_need_proto_port": "プロトコル（tcp/udp/icmp）とポート番号を指定してください。\n例: \"10.0.0.1 を tcp/80 でブロックして\"",
        "xdp_need_proto":      "プロトコル（tcp/udp/icmp）を指定してください。\n例: \"10.0.0.1 を tcp/80 でブロックして\"",
        "xdp_need_port":       "ポート番号を指定してください。\n例: \"10.0.0.1 を tcp/80 でブロックして\"",
        "xdp_err_unblock":   "unblock には ip/proto/port が必要です",
        "xdp_err_qos_set":   "qos_set には ip/limit が必要です",
        "xdp_err_qos_get":   "qos_get には ip が必要です",
        "xdp_conn_error":    "Go IPS Server ({url}) に接続できません: {e}",
        "xdp_stats_error":   "統計取得エラー: {e}",
        "xdp_deploy_ok":     "XDP deploy 完了: {status}",
        "xdp_deploy_failed": "XDP deploy エラー: {e}",
        "hub_conn_error":   "転送先サーバに接続できません",
        "hub_timeout":      "タイムアウト",
        "read_deploy_deny": "参照系クエリは /deploy に渡せません。/execute の result を参照してください。",
        "trace_not_found":  "trace_id が見つかりません。先に /execute を実行してください。",

        # WebSocket
        "ws_connected":     "📡 接続完了",
        "ws_empty":         "メッセージが空です。",
        "cancel_unsupported": "キャンセルはサポートされていません。",

        # ロールバック
        "rollback_success": "Candidate config discarded",
        "rollback_failed":  "Rollback failed",

        # Security タブ / XDP ルーティング (task_decompose)
        "security_route":       "security",
        "xdp_deploy_start":     "🛡️ XDP Firewall へ適用中...",
        "xdp_plan_created":     "計画を作成しました。承認後に実行されます。",
        "sec_exec_confirm":     "⚠️ 実行確認",
        "sec_exec_label":       "以下のコマンドを XDP Firewall に送信します。",
        "sec_exec_warn":        "この操作は即時反映されます。",
        "sec_exec_btn":         "実行する",
        "sec_cancel_btn":       "キャンセル",
        "sec_exec_done":        "✅ 実行完了: {path} ({status})",
        "sec_exec_warn_status": "⚠️ {path}: {message}",
        "sec_exec_error":       "❌ エラー: {error}",

        # Chat タブ Security 提案アクション
        "chat_ai_propose":      "⚠️ AI 提案アクション（確認後に実行）",
        "chat_sec_analysis":    "AI セキュリティ解析",
        "chat_analyzing":       "⟳ AI がセキュリティ脅威を解析中...",
        "chat_exec_btn":        "実行",

        # RAW FLOW STATISTICS テーブル
        "stats_table_title":    "RAW FLOW STATISTICS",
        "stats_no_data":        "データなし",

        # analyze キーワード判定（task_decompose security route）
        "analyze_keywords":     "分析,解析,analyze,ai解析,提案",

        # ── ANTA Snapshot 事後検証 (port:8004) ──────────────────────────────
        # action 名
        "anta_action_snapshot":   "スナップショット取得",
        "anta_action_verify":     "ANTA テスト実行",
        "anta_action_compare":    "スナップショット比較",
        "anta_action_post_check": "事後検証 (Post-Check)",

        # 実行ステータス
        "anta_running":     "⟳ ANTA テスト実行中...",
        "anta_done":        "✅ ANTA テスト完了",
        "anta_failed":      "❌ ANTA テスト失敗",
        "anta_error":       "⚠️ ANTA 実行エラー",
        "anta_not_installed": "❌ ANTA ライブラリ未インストール (pip install anta)",

        # スナップショット
        "anta_snap_taken":  "✅ スナップショット取得完了 (ID: {snap_id})",
        "anta_snap_notfound": "❌ スナップショット '{snap_id}' が見つかりません",
        "anta_snap_required": "snapshot_id が必要です。先に action=snapshot を実行してください。",

        # 比較結果
        "anta_no_sideeffect":  "✅ 副作用なし — 設定変更による意図しない影響は検出されませんでした",
        "anta_sideeffect":     "⚠️ {n} 件の副作用を検出 — 設定変更の影響を確認してください",
        "anta_new_failure":    "⚠️ {test}: {before} → {after}",
        "anta_resolved":       "✅ 解決: {test} ({before} → {after})",
        "anta_still_failing":  "🔴 継続失敗: {test}",
        "anta_new_test_fail":  "🔴 [新規テスト失敗] {test}",

        # カテゴリ表示名
        "anta_cat_interface":    "インターフェース検証",
        "anta_cat_system":       "システム状態確認",
        "anta_cat_routing":      "ルーティングテーブル確認",
        "anta_cat_bgp":          "BGP セッション確認",
        "anta_cat_connectivity": "疎通確認 (LLDP)",
        "anta_cat_mlag":         "MLAG 状態確認",
        "anta_cat_vlan":         "VLAN 確認",
        "anta_cat_stp":          "STP 確認",

        # スコープノート
        "anta_scope_note": (
            "ANTA v1.8.0 公式テスト (事後検証 / Post-Check)。"
            "anta.catalog.AntaCatalog / anta.runner.main() / ResultManager を使用。"
            "verify → ANTA A2A (port:8004)。"
            "CNV (Continuous Network Verification) フロー: "
            "snapshot (before) → NETCONF deploy → post_check (compare)。"
        ),
    },

    # ── English ───────────────────────────────────────────────────────────
    "en": {
        # Generic status
        "success":          "✅ Success",
        "failure":          "❌ Failed",
        "no_changes":       "No changes (already up to date)",
        "dry_run":          "🔍 Dry-run complete",
        "blocked":          "🚫 Blocked",
        "skipped":          "⏭️ Skipped",
        "not_implemented":  "Not implemented",
        "error":            "An error occurred",

        # Deploy flow
        "deploy_start":     "🚀 Deploying...",
        "deploy_done":      "✅ Deployment complete",
        "deploy_failed":    "❌ Deployment failed",
        "deploy_success":   "edit-config deployed",
        "dryrun_note":      "(Dry-run — not deployed to device)",

        # Idempotency
        "idempotent_vlan":   "Idempotency check: VLAN already configured",
        "idempotent_absent": "Idempotency check: VLAN already absent",
        "idempotent_intf":   "Idempotency check: config already up to date",
        "idempotent_skip":   "No change required",

        # Task aggregation
        "all_success":      "✅ All {n} task(s) succeeded",
        "all_failure":      "❌ All {n} task(s) failed",
        "partial_failure":  "⚠️ {ok} succeeded / {ng} failed",
        "report_header":    "📊 Arista NETCONF RAG Execution Report",

        # Audit
        "audit_confirmed":  "✅ Confirmed: {type} {target} {op}d",
        "audit_failed":     "❌ Audit failed: {type} {target} not confirmed after {op}",
        "audit_scope_note": (
            "Audit uses NETCONF get_config (<config> tree only). "
            "<state> filter returns 0 results on cEOS (confirmed on device). "
            "Check operational state via eAPI server (port:8002). "
            "Security operations (block/unblock/qos) run through XDP A2A Agent (port:8003) "
            "via Hub (port:8000) /execute → /deploy two-phase flow. "
            "Monitoring data (pps/stats/top) fetched directly from Go IPS API (port:8080)."
        ),

        # eAPI
        "eapi_success":     "✅ Retrieved successfully",
        "eapi_blocked":     "Write command detected. Only show commands are permitted.",
        "eapi_parse_fail":  "JSON parse failed",
        "eapi_conn_error":  "eAPI connection error",
        "scope_note_eapi":  "operational-state (eAPI show)",

        # Hub / Router
        # XDP Firewall Agent (xdp_a2a_server / Go IPS API)
        "xdp_block_plan":    "Created plan to XDP-block IP {ip}:{port} [{proto}].",
        "xdp_unblock_plan":  "Created plan to XDP-unblock IP {ip}:{port} [{proto}].",
        "xdp_qos_plan":      "Created plan to apply QoS limit {limit}KB/s to IP {ip}.",
        "xdp_block_ok":      "✅ XDP block complete: {ip}:{port} [{proto}]",
        "xdp_unblock_ok":    "✅ XDP unblock complete: {ip}:{port} [{proto}]",
        "xdp_qos_ok":        "✅ QoS configured: {ip} → {limit}KB/s",
        "xdp_err_block":     "block requires ip/proto/port",
        "xdp_need_proto_port": "Please specify protocol (tcp/udp/icmp) and port number.\nExample: \"Block 10.0.0.1 tcp/80\"",
        "xdp_need_proto":      "Please specify protocol (tcp/udp/icmp).\nExample: \"Block 10.0.0.1 tcp/80\"",
        "xdp_need_port":       "Please specify port number.\nExample: \"Block 10.0.0.1 tcp/80\"",
        "xdp_err_unblock":   "unblock requires ip/proto/port",
        "xdp_err_qos_set":   "qos_set requires ip/limit",
        "xdp_err_qos_get":   "qos_get requires ip",
        "xdp_conn_error":    "Cannot connect to Go IPS Server ({url}): {e}",
        "xdp_stats_error":   "Stats retrieval error: {e}",
        "xdp_deploy_ok":     "XDP deploy complete: {status}",
        "xdp_deploy_failed": "XDP deploy error: {e}",
        "hub_conn_error":   "Cannot connect to downstream server",
        "hub_timeout":      "Timeout",
        "read_deploy_deny": "Read-only queries cannot be sent to /deploy. See /execute result.",
        "trace_not_found":  "trace_id not found. Run /execute first.",

        # WebSocket
        "ws_connected":     "📡 Connected",
        "ws_empty":         "Empty message.",
        "cancel_unsupported": "Cancel is not supported.",

        # Rollback
        "rollback_success": "Candidate config discarded",
        "rollback_failed":  "Rollback failed",

        # Security tab / XDP routing (task_decompose)
        "security_route":       "security",
        "xdp_deploy_start":     "🛡️ Applying to XDP Firewall...",
        "xdp_plan_created":     "Plan created. Will execute after approval.",
        "sec_exec_confirm":     "⚠️ Confirm Execution",
        "sec_exec_label":       "The following command will be sent to XDP Firewall.",
        "sec_exec_warn":        "This operation takes effect immediately.",
        "sec_exec_btn":         "Execute",
        "sec_cancel_btn":       "Cancel",
        "sec_exec_done":        "✅ Done: {path} ({status})",
        "sec_exec_warn_status": "⚠️ {path}: {message}",
        "sec_exec_error":       "❌ Error: {error}",

        # Chat tab Security proposal actions
        "chat_ai_propose":      "⚠️ AI Proposed Actions (confirm before executing)",
        "chat_sec_analysis":    "AI Security Analysis",
        "chat_analyzing":       "⟳ AI is analyzing security threats...",
        "chat_exec_btn":        "Execute",

        # RAW FLOW STATISTICS table
        "stats_table_title":    "RAW FLOW STATISTICS",
        "stats_no_data":        "No data",

        # analyze keyword detection (task_decompose security route)
        "analyze_keywords":     "analyze,analysis,ai analysis,proposal",

        # ── ANTA Snapshot Post-Check (port:8004) ─────────────────────────────
        # action names
        "anta_action_snapshot":   "Snapshot capture",
        "anta_action_verify":     "ANTA test run",
        "anta_action_compare":    "Snapshot comparison",
        "anta_action_post_check": "Post-Check (CNV)",

        # run status
        "anta_running":     "⟳ Running ANTA tests...",
        "anta_done":        "✅ ANTA tests complete",
        "anta_failed":      "❌ ANTA tests failed",
        "anta_error":       "⚠️ ANTA execution error",
        "anta_not_installed": "❌ ANTA library not installed (pip install anta)",

        # snapshot
        "anta_snap_taken":    "✅ Snapshot captured (ID: {snap_id})",
        "anta_snap_notfound": "❌ Snapshot '{snap_id}' not found",
        "anta_snap_required": "snapshot_id is required. Run action=snapshot first.",

        # diff results
        "anta_no_sideeffect":  "✅ No side-effects — no unintended impact from the config change",
        "anta_sideeffect":     "⚠️ {n} side-effect(s) detected — review the config change impact",
        "anta_new_failure":    "⚠️ {test}: {before} → {after}",
        "anta_resolved":       "✅ Resolved: {test} ({before} → {after})",
        "anta_still_failing":  "🔴 Still failing: {test}",
        "anta_new_test_fail":  "🔴 [New test failure] {test}",

        # category display names
        "anta_cat_interface":    "Interface verification",
        "anta_cat_system":       "System health check",
        "anta_cat_routing":      "Routing table check",
        "anta_cat_bgp":          "BGP session check",
        "anta_cat_connectivity": "Connectivity check (LLDP)",
        "anta_cat_mlag":         "MLAG status check",
        "anta_cat_vlan":         "VLAN check",
        "anta_cat_stp":          "STP check",

        # scope note
        "anta_scope_note": (
            "ANTA v1.8.0 official tests (Post-Check / CNV). "
            "Uses anta.catalog.AntaCatalog / anta.runner.main() / ResultManager. "
            "verify → ANTA A2A (port:8004). "
            "CNV flow: snapshot (before) → NETCONF deploy → post_check (compare)."
        ),
    },
}


def get_msg(key: str, locale: str = None, **kwargs) -> str:
    """
    ロケールに対応するメッセージを返す。

    Args:
        key:    MESSAGES のキー
        locale: "ja" | "en"（省略時は LOCALE 環境変数 or "ja"）
        **kwargs: フォーマット変数（例: n=3, ok=2, ng=1）

    Returns:
        メッセージ文字列。キーが存在しない場合はキー自体を返す。

    Examples:
        get_msg("all_success", n=2)         → "✅ 全 2 タスク成功"
        get_msg("all_success", "en", n=2)   → "✅ All 2 task(s) succeeded"
        get_msg("audit_confirmed", type="vlan", target="101", op="configure")
    """
    loc  = locale or LOCALE
    msgs = MESSAGES.get(loc) or MESSAGES["ja"]
    tmpl = msgs.get(key) or MESSAGES["ja"].get(key, key)
    if kwargs:
        try:
            return tmpl.format(**kwargs)
        except (KeyError, IndexError):
            return tmpl
    return tmpl


def locale_from_request(params: dict, default: str = None) -> str:
    """
    リクエストパラメータから locale を取得する。
    params に "locale" キーがなければ LOCALE 環境変数を使用。
    """
    return params.get("locale") or default or LOCALE
