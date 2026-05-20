#!/usr/bin/env python3
# Copyright (c) 2026 hidemi-k
# Licensed under the MIT License.
"""
app_a2a.py — Network Agent NiceGUI フロントエンド (A2A版)
=========================================================
A2A化前の app.py の UI レイアウトを継承し、
接続先を task_decompose_a2a_server.py (FastAPI/A2A Hub, port:8000) に変更。

変更点（A2A化）:
  POST /execute → {"query":..., "device":DEVICE, "locale":LOCALE}
  POST /deploy/{trace_id} → {"device":DEVICE, "snapshot_id":snap_id}
  POST /validate → {"xml":...}
  GET  /diff/{trace_id}

自動 Post-Check（deploy 後の ANTA 自動実行）:
  Before Snapshot 取得済みの場合、/deploy 完了直後に Hub が
  ANTA post_check を asyncio.create_task でバックグラウンド実行する。
  結果は _push_log（WebSocket）でリアルタイム通知 → UI チャットに表示。
  snap_id 未取得時はスキップ（既存フローに影響なし）。

  WRITE系レスポンス:  result.task_summaries でタスク結果表示
  READ系レスポンス:   result.formatted / result.cmds を使用
  is_read 判定:       result.is_read フラグ

多言語対応:
  i18n.py の get_msg() を使用
  環境変数 LOCALE=en で英語化
"""

import asyncio
import base64
import json
import os
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime

import httpx
from nicegui import ui
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# ── 多言語対応 ────────────────────────────────────────────────────────────────
# i18n.py は import できても UIキーは存在しないため、
# L() は専用の _UI_MSG を参照する独立実装にする
try:
    from i18n import LOCALE as _i18n_locale
    LOCALE = os.getenv("LOCALE", _i18n_locale)
except ImportError:
    LOCALE = os.getenv("LOCALE", "ja")

# get_msg スタブ（後方互換、UI では L() を使う）
_UI_MSG = {
    "ja": {
        "send": "送信", "approve_deploy": "承認 & デプロイ",
        "edit_xml": "XML 編集", "dry_run": "dry-run",
        "dry_run_active": "dry-run 有効",
        "waiting": "● 待機中", "executing": "⟳ 実行中...",
        "await_confirm": "◉ 承認待ち", "deploying": "⟳ デプロイ中...",
        "executing_msg": "⟳ Orchestrator 実行中...",
        "deploying_msg": "⟳ デプロイ中...",
        "approve_title": "デプロイ承認の確認",
        "snapshot_done": "スナップショット（自動取得済み）",
        "rollback_title": "ロールバック手順",
        "chk1": "バックアップの取得を確認しました（自動チェック不可）",
        "chk2": "ロールバック手順を理解しています",
        "chk1_warn": "⚠️ バックアップ確認チェックが必要です",
        "chk2_warn": "⚠️ ロールバック理解チェックが必要です",
        "deploy_btn": "デプロイ実行",
        "cancel": "キャンセル",
        "xml_edit_title": "XML 編集",
        "xml_edit_hint": "編集後「保存して再 Dry-run」を押すと差分が更新されます。",
        "xml_validate": "構文チェック",
        "xml_save": "保存して再 Dry-run",
        "xml_ok": "✅ XML 構文チェック OK",
        "diff_history": "Diff / History",
        "diff_empty": "操作を実行すると差分履歴が表示されます。",
        "diff_refresh": "更新",
        "diff_label": "① 設定差分",
        "reasoning_label": "② AI の判断根拠 (Reasoning)",
        "logs_label": "③ 技術ログ (NETCONF / Raw)",
        "confirm_btn": "✓  内容を確認しました。本番適用します",
        "deploy_diff": "Deploy Diff (Audit)",
        "hint_input": "例: BGPの状態を調べて",
        "hint_guide": "自然言語でネットワーク操作を入力してください。Dry-run → Diff 確認 → 承認 の順で実行されます。",
        "diff_tab_confirm": "Diff / Historyタブで確認",
        "error_title": "エラーが発生しました",
        "deploy_fail": "デプロイ失敗",
        "approve_detail": "右の「承認 & デプロイ」または「XML 編集」で続行してください。",
        "no_diff": "(差分なし)",
        "audit_scope": "audit: config-tree-only (<state>はNETCONFで取得不可)",
        # ── Verify タブ (ANTA) ──────────────────────────────────────────────
        "verify_tab_title":    "ANTA Network Verify",
        "verify_snap_btn":     "📸 Before Snapshot",
        "verify_snap_hint":    "設定変更「前」に押す — 現在状態を記録します",
        "verify_now_btn":      "▶ Verify Now",
        "verify_now_hint":     "現在の状態をANTAでテスト（単発ヘルスチェック）",
        "verify_post_btn":     "🔍 Post-Check",
        "verify_post_hint":    "設定変更「後」に押す — Before と比較して副作用を検出",
        "verify_post_need_snap": "⚠️ 先に [📸 Before Snapshot] を実行してください",
        "verify_snap_taken":   "✅ スナップショット取得完了",
        "verify_snap_id":      "snap_id",
        "verify_running":      "⟳ ANTA テスト実行中...",
        "verify_cat_label":    "テストカテゴリ",
        "verify_cat_all":      "すべて選択",
        "verify_cat_clear":    "クリア",
        "verify_result_label": "テスト結果",
        "verify_passed":       "✅ 成功",
        "verify_failed":       "❌ 失敗",
        "verify_skipped":      "⏭️ スキップ",
        "verify_no_sideeffect": "✅ 副作用なし",
        "verify_sideeffect":   "⚠️ 副作用を検出",
        "verify_new_failures": "新規 failure（副作用）",
        "verify_resolved":     "解決（改善）",
        "verify_still_fail":   "継続失敗（変更と無関係）",
        "verify_refresh":      "更新",
        "verify_engine":       "エンジン",
        "verify_ignored":      "除外 I/F",
        # ── CNV 自動 Post-Check ──────────────────────────────────────────────
        "cnv_running":         "⟳ 自動 Post-Check 実行中...",
        "cnv_done_ok":         "✅ 自動 Post-Check: 副作用なし",
        "cnv_done_ng":         "⚠️ 自動 Post-Check: 副作用を検出",
        "cnv_skipped":         "（Before Snapshot 未取得 — Post-Check スキップ）",
        "cnv_error":           "⚠️ 自動 Post-Check 失敗",
        "cnv_label":           "自動 Post-Check",
        "cnv_new_issues":      "新規 failure（変更の副作用）",
    },
    "en": {
        "send": "Send", "approve_deploy": "Approve & Deploy",
        "edit_xml": "Edit XML", "dry_run": "dry-run",
        "dry_run_active": "dry-run enabled",
        "waiting": "● Idle", "executing": "⟳ Running...",
        "await_confirm": "◉ Awaiting approval", "deploying": "⟳ Deploying...",
        "executing_msg": "⟳ Running Orchestrator...",
        "deploying_msg": "⟳ Deploying...",
        "approve_title": "Confirm Deployment",
        "snapshot_done": "Snapshot (auto-captured)",
        "rollback_title": "Rollback procedure",
        "chk1": "Confirmed backup was taken (manual check required)",
        "chk2": "I understand the rollback procedure",
        "chk1_warn": "⚠️ Please confirm backup",
        "chk2_warn": "⚠️ Please confirm rollback procedure",
        "deploy_btn": "Deploy",
        "cancel": "Cancel",
        "xml_edit_title": "Edit XML",
        "xml_edit_hint": "Press 'Save & Re-Dry-run' after editing to update the diff.",
        "xml_validate": "Validate",
        "xml_save": "Save & Re-Dry-run",
        "xml_ok": "✅ XML syntax OK",
        "diff_history": "Diff / History",
        "diff_empty": "Diff history will appear after operations.",
        "diff_refresh": "Refresh",
        "diff_label": "① Config Diff",
        "reasoning_label": "② AI Reasoning",
        "logs_label": "③ Tech Logs (NETCONF / Raw)",
        "confirm_btn": "✓  Reviewed. Apply to production",
        "deploy_diff": "Deploy Diff (Audit)",
        "hint_input": "e.g. Create VLAN 200 named MGMT_VLAN",
        "hint_guide": "Enter network operations in natural language. Flow: Dry-run → Diff → Approve.",
        "diff_tab_confirm": "View in Diff / History tab",
        "error_title": "An error occurred",
        "deploy_fail": "Deployment failed",
        "approve_detail": "Click 'Approve & Deploy' or 'Edit XML' to proceed.",
        "no_diff": "(no diff)",
        "audit_scope": "audit: config-tree-only (<state> unavailable via NETCONF)",
        # ── Verify tab (ANTA) ────────────────────────────────────────────────
        "verify_tab_title":    "ANTA Network Verify",
        "verify_snap_btn":     "📸 Before Snapshot",
        "verify_snap_hint":    "Press before config change — captures current state",
        "verify_now_btn":      "▶ Verify Now",
        "verify_now_hint":     "Run ANTA tests immediately (single health check)",
        "verify_post_btn":     "🔍 Post-Check",
        "verify_post_hint":    "Press after config change — compares with Before snapshot",
        "verify_post_need_snap": "⚠️ Please run [📸 Before Snapshot] first",
        "verify_snap_taken":   "✅ Snapshot captured",
        "verify_snap_id":      "snap_id",
        "verify_running":      "⟳ Running ANTA tests...",
        "verify_cat_label":    "Test categories",
        "verify_cat_all":      "Select all",
        "verify_cat_clear":    "Clear",
        "verify_result_label": "Test results",
        "verify_passed":       "✅ Passed",
        "verify_failed":       "❌ Failed",
        "verify_skipped":      "⏭️ Skipped",
        "verify_no_sideeffect": "✅ No side-effects",
        "verify_sideeffect":   "⚠️ Side-effects detected",
        "verify_new_failures": "New failures (side-effects)",
        "verify_resolved":     "Resolved (improvements)",
        "verify_still_fail":   "Still failing (pre-existing)",
        "verify_refresh":      "Refresh",
        "verify_engine":       "Engine",
        "verify_ignored":      "Ignored I/F",
        # ── CNV auto Post-Check ──────────────────────────────────────────────
        "cnv_running":         "⟳ Auto Post-Check running...",
        "cnv_done_ok":         "✅ Auto Post-Check: No side-effects",
        "cnv_done_ng":         "⚠️ Auto Post-Check: Side-effects detected",
        "cnv_skipped":         "(Before Snapshot not taken — Post-Check skipped)",
        "cnv_error":           "⚠️ Auto Post-Check failed",
        "cnv_label":           "Auto Post-Check",
        "cnv_new_issues":      "New failures (side-effects of change)",
    },
}


def get_msg(key, locale=None, **kw):
        loc = locale or LOCALE
        tmpl = _UI_MSG.get(loc, _UI_MSG["ja"]).get(key) or _UI_MSG["ja"].get(key, key)
        return tmpl.format(**kw) if kw else tmpl
def L(key, **kw):
    """UI専用ロケール関数。必ず内部 _UI_MSG を参照する。"""
    msgs = _UI_MSG.get(LOCALE) or _UI_MSG["ja"]
    tmpl = msgs.get(key) or _UI_MSG["ja"].get(key, key)
    return tmpl.format(**kw) if kw else tmpl

# ── 接続設定 ──────────────────────────────────────────────────────────────────
API_BASE    = os.getenv("API_BASE",    "http://localhost:8000")
WS_BASE     = os.getenv("WS_BASE",     "ws://localhost:8000")
XDP_URL     = os.getenv("XDP_URL",     "http://localhost:8003")  # XDP A2A Server
XDP_API_URL = os.getenv("XDP_API_URL", "http://localhost:8080")  # Go IPS Server
ANTA_URL    = os.getenv("ANTA_URL",    "http://localhost:8004")  # ANTA Verify A2A Server

DEVICE = {
    "ip":       os.getenv("DEVICE_IP",   "172.20.100.31"),
    "port":     os.getenv("DEVICE_PORT", "830"),
    "username": os.getenv("DEVICE_USER", "admin"),
    "password": os.getenv("DEVICE_PASS", "admin"),
}

# ── Basic 認証 + レート制限 ────────────────────────────────────────────────────
# 複数ユーザー対応: 環境変数をカンマ区切りでリスト化
# 例: BASIC_AUTH_USER=admin,judge2  BASIC_AUTH_PASS=secure2026!,hackathon
AUTH_USER      = [u.strip() for u in os.getenv("BASIC_AUTH_USER", "judge").split(",")]
AUTH_PASS      = [p.strip() for p in os.getenv("BASIC_AUTH_PASS", "hackathon2026").split(",")]
AUTH_REALM     = "Restricted"
RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "30"))
_rate_store: dict = defaultdict(list)

# ユーザー数とパスワード数が一致しない場合は警告
if len(AUTH_USER) != len(AUTH_PASS):
    import logging as _logging
    _logging.getLogger(__name__).warning(
        f"[Auth] BASIC_AUTH_USER ({len(AUTH_USER)}件) と "
        f"BASIC_AUTH_PASS ({len(AUTH_PASS)}件) の件数が一致しません。"
        f"短い方に合わせて動作します。"
    )


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def _check_auth(self, request: Request) -> bool:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            user, passwd = decoded.split(":", 1)
            # 複数ユーザー対応: いずれかのユーザー/パスワードペアに一致すれば OK
            for u, p in zip(AUTH_USER, AUTH_PASS):
                if user == u and passwd == p:
                    return True
            return False
        except Exception:
            return False

    def _check_rate(self, ip: str) -> bool:
        now = time.time()
        _rate_store[ip] = [t for t in _rate_store[ip] if now - t < 60.0]
        if len(_rate_store[ip]) >= RATE_LIMIT_RPM:
            return False
        _rate_store[ip].append(now)
        return True

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        bypass = (
            path == "/healthz"
            or path.startswith("/_nicegui/")
            or path.startswith("/favicon")
        )
        if bypass:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        if not self._check_rate(client_ip):
            return Response(
                content='{"error":"Too Many Requests","retry_after":60}',
                status_code=429, media_type="application/json",
                headers={"Retry-After": "60"},
            )
        if not self._check_auth(request):
            return Response(
                content="Unauthorized", status_code=401,
                headers={
                    "WWW-Authenticate": f'Basic realm="{AUTH_REALM}"',
                    "Cache-Control": "no-store",
                },
            )
        return await call_next(request)


# ── カラーテーマ（app.py 継承） ───────────────────────────────────────────────
C = {
    "bg":         "#141414",
    "bg2":        "#1e1e1e",
    "bg3":        "#282828",
    "bg4":        "#303030",
    "border":     "rgba(255,255,255,0.08)",
    "border2":    "rgba(255,255,255,0.14)",
    "text":       "#e2e2e2",
    "text1":      "#e2e2e2",
    "text2":      "#8a8a8a",
    "text3":      "#4a4a4a",
    "primary":    "#4a8fc4",   # AI 変更要約の強調色（info_fg と同色）
    "success_bg": "#0f2318",
    "success_fg": "#5bb85b",
    "warn_bg":    "#251a08",
    "warn_fg":    "#c4902a",
    "info_bg":    "#0a1828",
    "info_fg":    "#4a8fc4",
    "danger_bg":  "#221010",
    "danger_fg":  "#c45a5a",
    "mono":       "'JetBrains Mono','Fira Code','Cascadia Code',monospace",
}


# ── State ─────────────────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.trace_id      = ""
        self.pending_xml   = ""
        self.pending_query = ""
        self.phase         = "idle"
        self.diff_history: list = []
        # Chat Sticky Footer の現在の exec_tags を保持する。
        # 1件実行後に残りを render_chat_sticky() で再描画するために使用。
        self.sticky_tags:  list = []



# ── Security タブ State ───────────────────────────────────────────────────────
class SecurityState:
    MAX_HISTORY  = 5
    CHART_POINTS = 100  # 3秒×100点=300秒=5分分
    PPS_FLOOD_THR = 10000

    def __init__(self):
        self.top_stats:   list  = []
        self.drop_list:   dict  = {}
        self.qos_list:    dict  = {}
        self.threat_log:  list  = []
        self.pps_history:  list  = []
        self.syn_history:  list  = []
        self.time_labels:  list  = []   # グラフ横軸用の時刻ラベル
        self.prev_syn:    int   = 0
        self.drop_history: list  = []   # 総DROP数の時系列
        self.stats_ts:    str   = ""    # 統計テーブル用タイムスタンプ
        self.analysis:    str   = ""
        self.exec_tags:   list  = []
        self.last_event_ip: str = ""
        self.polling:     bool  = False

    @property
    def active_blocks(self) -> int:
        return len(self.drop_list)

    @property
    def qos_mitigated(self) -> int:
        return sum(1 for v in self.qos_list.values()
                   if isinstance(v, dict) and v.get("limit_bytes_per_sec") == 10000)

    @property
    def unique_ips(self) -> int:
        return len({s.get("ip") for s in self.top_stats})

    @property
    def current_pps(self) -> int:
        return sum(s.get("stats", {}).get("packets", 0) for s in self.top_stats)


_sec_state = SecurityState()


# ── Verify タブ State (ANTA) ──────────────────────────────────────────────────
class VerifyState:
    """ANTA Verify タブの状態を保持するクラス。"""
    # 利用可能なテストカテゴリ（表示名 / API キー のペア）
    ALL_CATS = [
        ("Interface",    "interface"),
        ("System",       "system"),
        ("Routing",      "routing"),
        ("BGP",          "bgp"),
        ("MLAG",         "mlag"),
        ("VLAN",         "vlan"),
        ("STP",          "stp"),
    ]

    def __init__(self):
        # 選択中カテゴリ（デフォルト: Interface + System + Routing）
        self.selected_cats: list = ["interface", "system", "routing", "bgp"]
        # before スナップショット ID（空 = 未取得）
        self.snap_id:       str  = ""
        self.snap_ts:       str  = ""   # スナップショット取得時刻
        # 直近の verify / post_check 結果
        self.last_result:   dict = {}
        # 実行中フラグ
        self.running:       bool = False


_verify_state = VerifyState()


async def xdp_get(path: str, params: dict = None):
    """Go IPS Server への GET。レスポンスが空・非 JSON でも安全に返す。"""
    url = f"{XDP_API_URL}{path}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        text = r.text.strip()
        if not text:
            return {"status": "ok", "message": "empty response"}
        try:
            return r.json()
        except Exception:
            return {"status": "ok", "message": text[:200]}


def _xdp_extract_text_as_json(a2a_resp: dict) -> dict:
    """A2A レスポンスから JSON を抽出する。arista_a2a_client_test_v5 準拠。"""
    try:
        result_obj = a2a_resp.get("result", {})
        parts = result_obj.get("parts", [])
        if not parts:
            parts = result_obj.get("message", {}).get("parts", [])
        for part in parts:
            if part.get("kind") == "text":
                text = part["text"]
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    return {"_raw_text": text}
                inner = parsed.get("result")
                if (isinstance(inner, dict)
                        and inner.get("jsonrpc") == "2.0"
                        and "result" in inner):
                    parsed["result"] = _xdp_extract_text_as_json(inner)
                return parsed
        return {"status": "error", "message": "parts empty",
                "_raw": str(a2a_resp)[:300]}
    except Exception as e:
        return {"status": "error", "message": f"parse error: {e}"}


async def xdp_a2a_post(payload: dict) -> dict:
    """XDP A2A Server (port:8003) へ A2A リクエストを送信して結果を返す。"""
    mid = f"ui-{datetime.now().strftime('%H%M%S%f')}"
    req = {
        "jsonrpc": "2.0", "id": mid, "method": "message/send",
        "params": {"message": {
            "role": "user",
            "parts": [{"kind": "text",
                       "text": json.dumps(payload, ensure_ascii=False)}],
            "messageId": mid,
        }},
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(XDP_URL, json=req)
            resp.raise_for_status()
            data = resp.json()
        return _xdp_extract_text_as_json(data)
    except httpx.ConnectError:
        return {"status": "error",
                "message": f"XDP A2A Server ({XDP_URL}) に接続できません"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


async def anta_a2a_post(payload: dict) -> dict:
    """
    ANTA Verify A2A Server (port:8004) へ A2A リクエストを送信して結果を返す。
    xdp_a2a_post と同じ A2A JSON-RPC パターンを使用する。

    payload 例:
        {"action": "snapshot", "query": "...", "device_ip": "...", ...}
        {"action": "verify",   "tests": ["interface","system"], ...}
        {"action": "post_check", "snapshot_id": "snap_xxx", ...}
    """
    mid = f"anta-ui-{datetime.now().strftime('%H%M%S%f')}"
    req = {
        "jsonrpc": "2.0", "id": mid, "method": "message/send",
        "params": {"message": {
            "role": "user",
            "parts": [{"kind": "text",
                       "text": json.dumps(payload, ensure_ascii=False)}],
            "messageId": mid,
        }},
    }
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:  # ANTA は時間がかかる
            resp = await client.post(ANTA_URL, json=req)
            resp.raise_for_status()
            data = resp.json()
        return _xdp_extract_text_as_json(data)  # A2A レスポンス展開は共通
    except httpx.ConnectError:
        return {"status": "error",
                "message": f"ANTA Verify Server ({ANTA_URL}) に接続できません"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _classify_threat(flow: dict, drop_list: dict, qos_list: dict):
    ip    = flow.get("ip", "")
    port  = flow.get("port", 0)
    proto = flow.get("protocol", "")
    stats = flow.get("stats", {})
    syn   = stats.get("syn_packets", 0)
    ack   = stats.get("ack_packets", 0)
    pkts  = stats.get("packets", 0)
    drops = stats.get("dropped_packets", 0)
    block_key = f"{ip}:{port} [{proto}]"
    if block_key in drop_list or drops > 0:
        return None
    if proto == "tcp" and syn > 1000 and ack == 0:
        action = "Mitigated" if ip in qos_list else "XDP_DROP"
        return {"kind": "SYNスパイク",
                "detail": f"syn={syn:,} ack=0 → SYN Flood検知",
                "action": action, "ip": ip, "port": port, "proto": proto}
    if proto == "tcp" and syn > 0 and (ack / (syn + 1)) < 0.5:
        return {"kind": "ポートスキャン疑い",
                "detail": f"ack/syn={ack/(syn+1):.2f} ハーフオープン",
                "action": "XDP_DROP", "ip": ip, "port": port, "proto": proto}
    # icmp / udp は ack/syn=0 が正常。drop/block は提案せず脅威ログのみ記録
    if proto in ("icmp", "udp") and pkts > SecurityState.PPS_FLOOD_THR:
        return {"kind": "異常フロー",
                "detail": f"port={port} {proto.upper()} flood packets={pkts:,}",
                "action": "QoS", "ip": ip, "port": port, "proto": proto,
                "no_block": True}
    return None


async def _poll_security():
    global _sec_state
    while _sec_state.polling:
        try:
            top  = await xdp_get("/top")
            drl  = await xdp_get("/drop/list")
            qosl = await xdp_get("/qos/list")
            _sec_state.top_stats = top  if isinstance(top,  list) else []
            _sec_state.drop_list = drl  if isinstance(drl,  dict) else {}
            _sec_state.qos_list  = qosl if isinstance(qosl, dict) else {}

            pps = _sec_state.current_pps
            _sec_state.pps_history.append(pps)
            if len(_sec_state.pps_history) > SecurityState.CHART_POINTS:
                _sec_state.pps_history.pop(0)
            _sec_state.time_labels.append(datetime.now().strftime("%H:%M:%S"))
            if len(_sec_state.time_labels) > SecurityState.CHART_POINTS:
                _sec_state.time_labels.pop(0)

            total_syn = sum(s.get("stats", {}).get("syn_packets", 0)
                            for s in _sec_state.top_stats)
            syn_delta = max(0, total_syn - _sec_state.prev_syn)
            _sec_state.prev_syn = total_syn
            _sec_state.syn_history.append(syn_delta)
            if len(_sec_state.syn_history) > SecurityState.CHART_POINTS:
                _sec_state.syn_history.pop(0)

            total_drop = sum(
                s.get("stats", {}).get("dropped_packets", 0)
                for s in _sec_state.top_stats
            )
            _sec_state.drop_history.append(total_drop)
            if len(_sec_state.drop_history) > SecurityState.CHART_POINTS:
                _sec_state.drop_history.pop(0)
            _sec_state.stats_ts = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

            for flow in _sec_state.top_stats:
                threat = _classify_threat(
                    flow, _sec_state.drop_list, _sec_state.qos_list)
                if threat:
                    entry = {
                        "time":   datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "ip":     threat["ip"],
                        "kind":   threat["kind"],
                        "detail": threat["detail"],
                        "action": threat["action"],
                    }
                    if not _sec_state.threat_log or (
                        _sec_state.threat_log[-1]["ip"]   != entry["ip"] or
                        _sec_state.threat_log[-1]["kind"] != entry["kind"]
                    ):
                        _sec_state.threat_log.append(entry)
                        _sec_state.last_event_ip = entry["ip"]
                        if len(_sec_state.threat_log) > SecurityState.MAX_HISTORY:
                            _sec_state.threat_log.pop(0)
        except Exception:
            pass
        await asyncio.sleep(3)


# ── API ユーティリティ ─────────────────────────────────────────────────────────
async def api_post(path: str, payload: dict):
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(f"{API_BASE}{path}", json=payload)
        r.raise_for_status()
        return r.json()


async def api_get(path: str):
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{API_BASE}{path}")
        r.raise_for_status()
        return r.json()


# ── A2A Hub へのリクエスト構築 ────────────────────────────────────────────────
def _exec_payload(query: str) -> dict:
    """POST /execute 用ペイロード"""
    return {"query": query, "device": DEVICE, "locale": LOCALE}


def _deploy_payload() -> dict:
    """POST /deploy/{trace_id} 用ペイロード。
    Before Snapshot 取得済みなら snapshot_id を渡す → Hub が自動 Post-Check を実行。
    未取得（空文字）なら Hub 側でスキップされ、既存フローに影響しない。
    """
    return {
        "device":      DEVICE,
        "snapshot_id": _verify_state.snap_id,  # CNV: Before Snapshot ID（未取得なら ""）
    }


# ── レスポンス解析（A2A Hub v3 形式） ────────────────────────────────────────
def _parse_execute_result(result: dict) -> dict:
    """
    /execute レスポンスを UI 表示用に正規化する。

    Hub /execute が返す実際の構造:
      {
        "trace_id":  str,
        "is_read":   bool,
        "status":    str,   ← result.get("status") or result.get("overall_status")
        "summary":   str,
        "xml":       str,   ← WRITE dry-run時は "" (NETCONFサーバはdeploy前にXMLを返さない)
        "result":    {      ← NETCONFサーバの response_payload がそのまま入る
          "task_summaries": [...],
          "overall_status": "dry_run" | "all_success" | ...,
          "summary":        str,
          "tasks_detected": int,
          "audit_scope_note": str,
        }
      }

    【重要】dry-run判定:
      NETCONFサーバは deploy=False のとき XML を含まない。
      result["xml"] == "" でも overall_status == "dry_run" かつ
      task_summaries が存在すれば dry-run 成功 → 承認待ちへ遷移する。
    """
    is_read     = result.get("is_read", False)
    is_security = result.get("route", "") == "security"
    is_verify   = result.get("route", "") == "verify"   # ANTA 事後検証ルート
    # Hub は result.get("status") or result.get("overall_status") で設定する
    status  = result.get("status", "unknown")
    summary = result.get("summary", "")
    xml_out = result.get("xml", "")

    # [修正1核心] バックエンドのレスポンスは result["result"] にネストされている
    inner = result.get("result", {}) or {}

    # ── Verify 系 (ANTA A2A): 事後検証結果を Chat に表示 ──────────────────────
    if is_verify:
        # Hub /execute verify ルートのレスポンス構造:
        #   response["route"]   = "verify"
        #   response["result"]  = ANTA A2A の応答全体
        #   response["status"]  = "success" | "partial_failure" | "failure" | "error"
        #   response["summary"] = "✅ 全 10 タスク成功" など
        # inner (= response["result"]) には ANTA の results/tests_total 等が入っている
        anta_results  = inner.get("results",       [])
        tests_total   = inner.get("tests_total",   len(anta_results))
        tests_passed  = inner.get("tests_passed",  0)
        tests_failed  = inner.get("tests_failed",  0)
        engine        = inner.get("engine",        "anta-official")
        snap_id       = inner.get("snapshot_id",   "")
        new_issues    = inner.get("new_issues",    [])
        diff          = inner.get("diff",          {}) or {}

        # テスト結果を読みやすいテキストに整形
        ICON = {"success": "✅", "failure": "❌", "error": "🔴", "skipped": "⏭️"}
        lines = []
        for r in anta_results[:15]:   # 最大15件表示
            icon = ICON.get(r.get("result", ""), "❓")
            msgs = " / ".join(r.get("messages", []))[:60]
            lines.append(
                f"{icon} {r.get('test','')} "
                + (f"— {msgs}" if msgs else "")
            )
        if len(anta_results) > 15:
            lines.append(f"… 他 {len(anta_results) - 15} 件")

        formatted = "\n".join(lines) if lines else summary

        # 副作用 (post_check 時)
        _pc_summary = ""
        if new_issues:
            _pc_summary = "⚠️ 副作用検出:\n" + "\n".join(new_issues[:5])
        elif diff and inner.get("action") in ("post_check", "compare"):
            _pc_summary = "✅ 副作用なし"

        if _pc_summary:
            formatted = _pc_summary + "\n\n" + formatted

        _meta = f"engine:{engine}"
        if snap_id:
            _meta += f"  snap:{snap_id[:20]}…"
        if tests_total:
            _meta += f"  ({tests_passed}/{tests_total} passed)"

        return {
            "is_read":        False,
            "is_security":    False,
            "is_verify":      True,
            "is_dry_run":     False,
            "status":         status,
            "summary":        summary or inner.get("summary", ""),
            "cmds":           [],
            "formatted":      formatted,
            "meta":           _meta,
            "exec_tags":      [],
            "xml":            "",
            "tasks":          [],
            "task_summaries": [],
            "diff":           "",
            "logs":           [],
        }

    if is_security:
        # Security 系 (XDP A2A): result["result"] に XDP A2A の応答が入っている
        # task_decompose /execute security ルートのレスポンス構造:
        #   response["result"]    = xdp_result (XDP A2A の応答全体)
        #   response["analysis"]  = xdp_result["analysis"]  (引き上げ済み)
        #   response["exec_tags"] = xdp_result["exec_tags"] (引き上げ済み)
        #   inner (= response["result"]) にも analysis/exec_tags が入っている
        action    = inner.get("action", "")
        # analysis / exec_tags: トップレベル(result直下)を優先、なければ inner から取得
        _analysis  = result.get("analysis", "") or inner.get("analysis", "")
        _exec_tags = result.get("exec_tags", []) or inner.get("exec_tags", [])
        _message   = inner.get("message", "")
        _raw_text  = inner.get("_raw_text", "")
        xdp_data   = inner.get("result", {})

        # action ごとに読みやすい形式に整形
        if _analysis:
            # analyze アクション: LLM 解析テキストをそのまま表示
            formatted = _analysis
        elif _message:
            formatted = _message
        elif _raw_text:
            formatted = _raw_text
        elif xdp_data:
            # stats / top / drop_list / qos_list / info など:
            # JSON を整形して表示
            try:
                formatted = json.dumps(xdp_data, ensure_ascii=False, indent=2)
            except Exception:
                formatted = str(xdp_data)[:1000]
        else:
            # フォールバック: inner 全体を表示
            try:
                formatted = json.dumps(inner, ensure_ascii=False, indent=2)
            except Exception:
                formatted = str(inner)[:500]

        # summary: action 名を含めたわかりやすい見出し
        _action_labels = {
            "stats":     "全フロー統計",
            "top":       "上位フロー統計 (Top 10)",
            "drop_list": "ブロックリスト",
            "qos_list":  "QoS ポリシー一覧",
            "qos_get":   "QoS ポリシー",
            "info":      "エージェント情報",
            "analyze":   "AI セキュリティ解析",
            "block":     "ブロック実行結果",
            "unblock":   "ブロック解除結果",
            "qos_set":   "QoS 設定結果",
        }
        _label = _action_labels.get(action, "Security クエリ実行完了")
        _summary = summary or _label

        return {
            "is_read":        False,
            "is_security":    True,
            "is_dry_run":     False,
            "status":         status,
            "summary":        _summary,
            "cmds":           [],
            "formatted":      formatted,
            "exec_tags":      _exec_tags,   # analyze 時の提案アクション
            "xml":            "",
            "tasks":          [],
            "task_summaries": [],
            "diff":           "",
            "logs":           [],
        }

    # ── mixed ルート: read + write の混合クエリ ────────────────────────────────
    # _execute_mixed() が返す task_summaries には read/write 両方のサブクエリ結果が入る。
    # read サブクエリの formatted_text を先頭に表示し、
    # write サブクエリの task_summaries を承認待ちフローに渡す。
    if result.get("route") == "mixed":
        # task_summaries はトップレベルに直接入っている（Hub が設定）
        all_tasks = result.get("task_summaries", []) or inner.get("task_summaries", [])

        # read サブクエリの結果を formatted テキストとして結合
        read_lines = []
        for sq in all_tasks:
            if sq.get("route") == "read":
                _ft = sq.get("formatted_text", "") or sq.get("formatted", "")
                _sm = sq.get("summary", "")
                _q  = sq.get("query", "")
                if _ft:
                    read_lines.append(f"▶ {_q}\n{_ft}")
                elif _sm:
                    read_lines.append(f"▶ {_q}\n{_sm}")
        formatted_read = "\n\n".join(read_lines)

        # write サブクエリの task_summaries を NETCONF 形式に変換
        # Hub の _execute_mixed は write サブクエリの result（NETCONF応答）を格納している
        write_task_summaries = []
        write_overall = "success"
        for sq in all_tasks:
            if sq.get("route") == "write":
                _r = sq.get("result", {}) or {}
                _ts = _r.get("task_summaries", [])
                write_task_summaries.extend(_ts)
                _st = _r.get("overall_status", _r.get("status", ""))
                if "fail" in _st.lower() or "error" in _st.lower():
                    write_overall = _st

        is_dry_run = any(
            ts.get("deploy_status") == "skipped"
            for ts in write_task_summaries
        )

        sd = result.get("session_diff", {}) or {}

        return {
            "is_read":        False,
            "is_mixed":       True,           # mixed フラグ（UI 側判定用）
            "is_dry_run":     is_dry_run,
            "status":         status or write_overall,
            "summary":        summary,
            "cmds":           [],
            "formatted":      formatted_read,  # read サブクエリ結果を表示
            "xml":            xml_out,
            "tasks":          [],
            "task_summaries": write_task_summaries,
            "session_diff":   sd,
            "diff":           sd.get("diff_text", ""),
            "logs":           [],
        }

    if is_read:
        # READ 系 (eAPI): safe_result に cmds/formatted が入っている
        cmds      = inner.get("cmds", [])
        formatted = inner.get("formatted", "")
        return {
            "is_read":        True,
            "is_dry_run":     False,
            "status":         status,
            "summary":        summary or inner.get("summary", ""),
            "cmds":           cmds,
            "formatted":      formatted,
            "xml":            "",
            "tasks":          [],
            "task_summaries": [],
            "diff":           "",
            "logs":           [],
        }
    else:
        # WRITE 系 (NETCONF):
        # task_summaries は inner (= NETCONFサーバの response_payload) に入っている
        task_summaries = inner.get("task_summaries", [])
        tasks          = inner.get("tasks", []) or result.get("tasks", [])
        _summary       = summary or inner.get("summary", "")

        # overall_status を inner からも補完 ("dry_run" / "all_success" / ...)
        overall_status = (
            status
            or inner.get("overall_status", "")
            or inner.get("status", "unknown")
        )

        # dry-run 判定:
        #   deploy=False のとき NETCONFサーバは overall_status="dry_run" を返す
        #   xml は空だが、task_summaries があれば承認待ちへ遷移できる
        #   ★ blocked タスクのみの場合は dry-run ではなく failure 扱い
        non_blocked_tasks = [
            ts for ts in task_summaries
            if ts.get("deploy_status") != "blocked"
        ]
        all_blocked = bool(task_summaries) and len(non_blocked_tasks) == 0

        is_dry_run = (
            not all_blocked
            and (
                "dry_run" in overall_status.lower()
                or any(
                    ts.get("deploy_status") == "skipped"
                    for ts in non_blocked_tasks
                )
            )
        )

        # xml が空でも is_dry_run フラグで承認待ちへ遷移できるようにする
        # ★ session_diff: Hub が取得した +/- diff（新規）
        sd = result.get("session_diff", {}) or {}

        return {
            "is_read":        False,
            "is_dry_run":     is_dry_run,
            "status":         overall_status,
            "summary":        _summary,
            "cmds":           [],
            "formatted":      "",
            "xml":            xml_out,
            "tasks":          tasks,
            "task_summaries": task_summaries,
            "session_diff":   sd,          # ★ {status, diff_lines, diff_text, cmds}
            "diff":           sd.get("diff_text", ""),
            "logs":           [],
        }


def _parse_deploy_result(result: dict) -> dict:
    """
    POST /deploy/{trace_id} レスポンスを正規化する。

    Hub /deploy レスポンス:
      {
        "trace_id": str,
        "status":   str,   ← overall_status を Hub が引き上げ
        "summary":  str,
        "result":   {      ← NETCONFサーバの response_payload（A2A経由でネスト）
          "task_summaries":  [...],
          "overall_status":  str,
          "summary":         str,
          "audit_scope_note": str,
        },
        "audit_scope_note": str,
      }

    [注意] Hub _forward() → _extract_text() 経由のため、
    result["result"] が NETCONFサーバの response_payload になる。
    """
    # Hub トップレベルのステータス
    status  = result.get("status", "unknown")
    summary = result.get("summary", "")

    # NETCONFサーバのレスポンス（ネスト）
    inner = result.get("result", {}) or {}

    # task_summaries は inner に入っている
    task_summaries = inner.get("task_summaries", [])

    # status/summary の補完（inner の overall_status を fallback）
    if not status or status == "unknown":
        status = inner.get("overall_status", inner.get("status", "unknown"))
    if not summary:
        summary = inner.get("summary", "")

    # badge 用: success 系ステータスの正規化
    _ok = ("all_success", "success", "no_changes")
    status_kind = "success" if any(s in status for s in _ok) else "failure"

    # audit diff を task_summaries から生成
    audit_lines = []
    for ts in task_summaries:
        audit_msg   = ts.get("audit_message", "")
        audit_scope = ts.get("audit_scope", "")
        if audit_msg:
            audit_lines.append(f"[{ts.get('task_id','?')}] {audit_msg}")
        if audit_scope:
            audit_lines.append(f"  scope: {audit_scope}")
    deploy_diff = "\n".join(audit_lines)

    return {
        "status":         status,
        "status_kind":    status_kind,
        "summary":        summary,
        "task_summaries": task_summaries,
        "deploy_diff":    deploy_diff,
    }


# ── UI パーツ ──────────────────────────────────────────────────────────────────
def badge(label: str, kind: str = "info"):
    colors = {
        "success":  (C["success_bg"], C["success_fg"]),
        "deployed": (C["success_bg"], C["success_fg"]),  # Diffタブ用
        "dry-run":  (C["warn_bg"],    C["warn_fg"]),
        "info":     (C["info_bg"],    C["info_fg"]),
        "failure":  (C["danger_bg"],  C["danger_fg"]),
        "pending":  (C["bg4"],        C["text2"]),
    }
    bg, fg = colors.get(kind, colors["info"])
    ui.label(label).style(
        f"background:{bg};color:{fg};font-size:10px;padding:2px 8px;"
        f"border-radius:4px;font-weight:500;"
        f"border:0.5px solid {fg}22;"
    )


def _style_exec_tags(escaped_html: str, C: dict, exec_tags: list = None) -> str:
    """
    HTML エスケープ済みテキスト内の [EXEC: ...] タグを処理する。

    【仕様変更】
    exec_tags リストを受け取り、実際に提案されたタグのみをコードブロック風に変換する。
    「提案しない」という文脈でLLMが誤出力した [EXEC: ...] は、
    テキストごと除去して不自然な表示を防ぐ。

    具体的には：
      - exec_tags に含まれる path+params のタグ → コードブロック風に変換（▶ /drop/block?...）
      - exec_tags に含まれない [EXEC: ...] タグ → タグ部分のみ削除（「提案しない」文脈の誤出力）

    変換例（exec_tagsに含まれるタグ）:
      [EXEC: /drop/block?ip=10.0.3.150&proto=tcp&port=80]
        → <code style="...">▶ /drop/block?ip=10.0.3.150&amp;proto=tcp&amp;port=80</code>

    除去例（exec_tagsに含まれないタグ）:
      「...のため、[EXEC: /drop/block?ip=10.0.3.150&proto=tcp&port=80] は提案しません。」
        → 「...のため、 は提案しません。」
        → さらにクリーンアップ → 「...のため、対応は不要です。」に近い自然な文になる
    """
    import re as _re

    # exec_tags から「提案済みタグ」のキーセットを構築
    # キー: "/drop/block?ip=X&proto=Y&port=Z" の形式で正規化
    approved_keys: set = set()
    if exec_tags:
        for tag in exec_tags:
            path   = tag.get("path", "")
            params = tag.get("params", {})
            qs     = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            approved_keys.add(f"{path}?{qs}" if qs else path)

    def _replace_exec(m):
        raw_content = m.group(1).strip()
        # HTMLエスケープを戻してパラメータ比較（&amp; → &）
        content_plain = raw_content.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")

        # exec_tags が未指定（旧来の呼び出し）→ 全件コードブロックに変換
        if exec_tags is None:
            return (
                f'<code style="background:{C["bg4"]};color:{C["text3"]};'
                f'font-family:monospace;font-size:10px;padding:1px 7px;'
                f'border-radius:4px;border:0.5px solid {C["border2"]};'
                f'white-space:nowrap;display:inline-block;margin:1px 0;">'
                f'▶ {content_plain}</code>'
            )

        # approved_keys との照合（パラメータ順序の差異を吸収するため部分一致も確認）
        is_approved = False
        for key in approved_keys:
            # path 部分が一致し、かつ全パラメータが含まれているか確認
            if "?" in content_plain and "?" in key:
                cp_path, cp_qs = content_plain.split("?", 1)
                k_path,  k_qs  = key.split("?", 1)
                if cp_path == k_path:
                    cp_params = set(cp_qs.split("&"))
                    k_params  = set(k_qs.split("&"))
                    if cp_params == k_params:
                        is_approved = True
                        break
            elif content_plain == key:
                is_approved = True
                break

        if is_approved:
            # 提案済みタグ → コードブロック風に変換
            return (
                f'<code style="background:{C["bg4"]};color:{C["text3"]};'
                f'font-family:monospace;font-size:10px;padding:1px 7px;'
                f'border-radius:4px;border:0.5px solid {C["border2"]};'
                f'white-space:nowrap;display:inline-block;margin:1px 0;">'
                f'▶ {content_plain}</code>'
            )
        else:
            # 「提案しない」文脈の誤出力 → タグ部分のみ削除
            return ""

    result = _re.sub(r'\[EXEC:\s*([^\]]+)\]', _replace_exec, escaped_html)

    # タグ削除後に残る不自然なフレーズをクリーンアップ
    # 例: "、 は提案しません。" → "。"
    #     "、  は提案しません" が複数連続する場合も対応
    result = _re.sub(r'[、，,]\s*は提案しません[。.]?', '。', result)
    result = _re.sub(r'\s+は提案しません[。.]?', '。', result)
    # 文末の重複句点を整理
    result = _re.sub(r'。{2,}', '。', result)

    return result


def diff_to_html(diff_text: str) -> str:
    if not diff_text:
        return (
            f'<div style="color:{C["text3"]};font-family:{C["mono"]};'
            f'font-size:11px;padding:8px;">{L("no_diff")}</div>'
        )
    out = []
    for ln in diff_text.strip().split("\n"):
        if ln.startswith("+") and not ln.startswith("+++"):
            color, bg = C["success_fg"], C["success_bg"]
        elif ln.startswith("-") and not ln.startswith("---"):
            color, bg = C["danger_fg"],  C["danger_bg"]
        elif ln.startswith("@@"):
            color, bg = C["info_fg"],    C["info_bg"]
        else:
            color, bg = C["text2"],      "transparent"
        esc = ln.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        out.append(
            f'<div style="background:{bg};color:{color};'
            f'font-family:{C["mono"]};font-size:11px;line-height:1.7;'
            f'padding:1px 10px;white-space:pre-wrap;">{esc}</div>'
        )
    return "\n".join(out)


def render_user_msg(text: str, chat_col):
    with chat_col:
        with ui.row().style("justify-content:flex-end;width:100%;margin:2px 0;"):
            with ui.column().style("align-items:flex-end;gap:2px;max-width:76%;"):
                ui.label(f"you · {datetime.now().strftime('%Y-%m-%d %H:%M')}").style(
                    f"font-size:10px;color:{C['text3']};"
                )
                ui.label(text).style(
                    f"background:{C['info_bg']};color:{C['info_fg']};"
                    f"border:0.5px solid {C['info_fg']}33;"
                    f"border-radius:12px 12px 2px 12px;padding:8px 13px;"
                    f"font-size:13px;line-height:1.5;"
                )


def render_agent_msg(text, detail, status, chat_col,
                     show_approve=False, on_approve=None, on_edit=None):
    with chat_col:
        with ui.row().style("justify-content:flex-start;width:100%;margin:2px 0;"):
            with ui.column().style("gap:3px;max-width:min(94%, 100%);"):
                with ui.row().style("align-items:center;gap:6px;"):
                    ui.label(f"agent · {datetime.now().strftime('%Y-%m-%d %H:%M')}").style(
                        f"font-size:10px;color:{C['text3']};"
                    )
                    badge(status, status)
                with ui.card().style(
                    f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                    f"border-radius:2px 12px 12px 12px;"
                    f"padding:11px 14px;gap:5px;box-shadow:none;"
                ):
                    ui.label(text).style(
                        f"font-size:13px;font-weight:500;color:{C['text']};"
                    )
                    if detail:
                        ui.label(detail).style(
                            f"font-size:12px;color:{C['text2']};line-height:1.5;"
                        )
                    with ui.row().style("gap:6px;margin-top:6px;flex-wrap:wrap;"):
                        if on_edit:
                            ui.button(L("edit_xml")).props("flat dense").style(
                                f"font-size:11px;padding:2px 9px;"
                                f"border:0.5px solid {C['border2']};"
                                f"border-radius:5px;color:{C['text2']};"
                                f"background:{C['bg3']};min-height:26px;"
                            ).on("click", on_edit)
                        if show_approve and on_approve:
                            ui.button(L("approve_deploy")).props("flat dense").style(
                                f"font-size:11px;padding:2px 11px;"
                                f"border:0.5px solid {C['success_fg']}66;"
                                f"border-radius:5px;color:{C['success_fg']};"
                                f"background:{C['success_bg']};min-height:26px;"
                                f"font-weight:500;"
                            ).on("click", on_approve)


def show_xml_editor(xml_str: str, on_save):
    with ui.dialog() as dlg, ui.card().style(
        f"width:680px;max-width:95vw;padding:20px;gap:12px;"
        f"border-radius:10px;background:{C['bg2']};"
        f"border:0.5px solid {C['border2']};"
    ):
        ui.label(L("xml_edit_title")).style(
            f"font-size:14px;font-weight:500;color:{C['text']};"
        )
        ui.label(L("xml_edit_hint")).style(f"font-size:11px;color:{C['text2']};")
        editor = ui.textarea(value=xml_str).style(
            f"width:100%;font-family:{C['mono']};font-size:11px;"
        ).props("outlined rows=16 dark")
        val_label = ui.label("").style("font-size:11px;")

        async def validate_xml():
            try:
                r = await api_post("/validate", {"xml": editor.value.strip()})
                if r.get("valid"):
                    val_label.text = r.get("message", L("xml_ok"))
                    val_label.style(f"color:{C['success_fg']};")
                else:
                    # Hub は "message" キーでエラー内容を返す
                    val_label.text = r.get("message") or f"❌ {r.get('error','XML 構文エラー')}"
                    val_label.style(f"color:{C['danger_fg']};")
            except Exception as e:
                val_label.text = f"⚠️ {e}"
                val_label.style(f"color:{C['warn_fg']};")

        with ui.row().style("gap:8px;width:100%;margin-top:4px;"):
            ui.button(L("xml_validate"), on_click=validate_xml).props("flat").style(
                f"border:0.5px solid {C['border2']};border-radius:6px;"
                f"color:{C['text2']};font-size:12px;"
            )
            ui.label("").style("flex:1;")
            ui.button(L("cancel"), on_click=dlg.close).props("flat").style(
                f"border:0.5px solid {C['border2']};border-radius:6px;"
                f"color:{C['text2']};font-size:12px;"
            )
            async def do_save():
                xml = editor.value.strip()
                dlg.close()
                await on_save(xml)
            ui.button(L("xml_save"), on_click=do_save).style(
                f"background:{C['info_bg']};color:{C['info_fg']};"
                f"border:0.5px solid {C['info_fg']}66;"
                f"border-radius:6px;font-size:12px;font-weight:500;"
            )
    dlg.open()


def show_approve_dialog(trace_id: str, on_deploy):
    with ui.dialog() as dlg, ui.card().style(
        f"width:420px;padding:20px;gap:12px;border-radius:10px;"
        f"background:{C['bg2']};border:0.5px solid {C['border2']};"
    ):
        ui.label(L("approve_title")).style(
            f"font-size:14px;font-weight:500;color:{C['text']};"
        )
        ui.html(
            f'<div style="font-size:10px;color:{C["text3"]};">'
            f'trace_id: <span style="font-family:{C["mono"]};color:{C["text2"]};">'
            f"{trace_id}</span></div>"
        )
        snap_id = f"snap_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        ui.label(L("snapshot_done")).style(f"font-size:11px;color:{C['text2']};margin-top:6px;")
        ui.html(
            f'<div style="background:{C["bg3"]};border:0.5px solid {C["border2"]};'
            f'border-radius:6px;padding:8px 10px;font-family:{C["mono"]};'
            f'font-size:10px;color:{C["text"]};line-height:1.6;">'
            f"snapshot-id: {snap_id}<br>"
            f"取得時刻: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}<br>"
            f"running-config: 保存完了</div>"
        )
        ui.label(L("rollback_title")).style(f"font-size:11px;color:{C['text2']};margin-top:4px;")
        ui.html(
            f'<div style="font-size:10px;color:{C["text2"]};line-height:1.6;">'
            f'コマンド: <code style="color:{C["info_fg"]};">'
            f'POST /rollback {{snap_id: "{snap_id}"}}</code><br>'
            f"所要時間の目安: 約 30 秒</div>"
        )
        chk1 = ui.checkbox(L("chk1")).style(f"color:{C['text']};font-size:12px;")
        chk2 = ui.checkbox(L("chk2")).style(f"color:{C['text']};font-size:12px;")
        warn_lbl = ui.label("").style(f"font-size:11px;color:{C['danger_fg']};")
        with ui.row().style("gap:8px;width:100%;margin-top:6px;"):
            ui.button(L("cancel"), on_click=dlg.close).props("flat").style(
                f"flex:1;border:0.5px solid {C['border2']};border-radius:6px;"
                f"color:{C['text2']};font-size:12px;"
            )
            async def do_deploy():
                if not chk1.value:
                    warn_lbl.text = L("chk1_warn"); return
                if not chk2.value:
                    warn_lbl.text = L("chk2_warn"); return
                dlg.close()
                await on_deploy()
            ui.button(L("deploy_btn"), on_click=do_deploy).style(
                f"flex:1;background:{C['success_bg']};color:{C['success_fg']};"
                f"border:0.5px solid {C['success_fg']}66;border-radius:6px;"
                f"font-size:12px;font-weight:500;"
            )
    dlg.open()

def show_xdp_exec_dialog(tag_path: str, tag_params: dict, on_confirm):
    """XDP Firewall 実行確認ダイアログ（モジュールレベル定義・Securityタブと同パターン）"""
    _qs    = "&".join(f"{k}={v}" for k, v in tag_params.items())
    _label = f"{tag_path}?{_qs}"
    with ui.dialog() as dlg, ui.card().style(
        f"background:{C['bg2']};border:1px solid {C['danger_fg']}44;"
        "border-radius:10px;padding:20px 24px;min-width:340px;gap:12px;"
    ):
        ui.label("⚠️ 実行確認").style(
            f"font-size:14px;font-weight:700;color:{C['danger_fg']};"
        )
        ui.label("以下のコマンドを XDP Firewall に送信します。").style(
            f"font-size:12px;color:{C['text2']};"
        )
        ui.label(_label).style(
            f"font-size:11px;color:{C['danger_fg']};"
            f"font-family:{C['mono']};padding:6px 10px;"
            f"background:{C['danger_bg']};border-radius:5px;"
            "word-break:break-all;"
        )
        ui.label("この操作は即時反映されます。").style(
            f"font-size:11px;color:{C['warn_fg']};"
        )
        with ui.row().style("gap:8px;justify-content:flex-end;width:100%;"):
            ui.button("キャンセル").props("flat dense").style(
                f"font-size:12px;color:{C['text2']};"
            ).on("click", dlg.close)
            async def _do_confirm():
                dlg.close()
                await on_confirm()
            ui.button("実行する", icon="play_arrow").style(
                f"font-size:12px;font-weight:600;"
                f"background:{C['danger_bg']};color:{C['danger_fg']};"
                f"border:1px solid {C['danger_fg']}66;"
                "border-radius:5px;padding:4px 14px;"
            ).on("click", _do_confirm)
    dlg.open()


# ── メインページ ───────────────────────────────────────────────────────────────
@ui.page("/")
def main():
    state = State()

    ui.add_head_html("""
    <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500&display=swap" rel="stylesheet">
    <style>
      *, *::before, *::after { box-sizing: border-box; }
      body { margin:0; background:#141414; font-family:'IBM Plex Sans',sans-serif; }
      .nicegui-content { padding:0 !important; max-width:none !important; width:100% !important; }
      .q-page          { min-height:100vh !important; width:100% !important; }
      .q-tab-panel     { padding:0 !important; width:100% !important; }
      .q-tab-panels    { background:transparent !important; width:100% !important; }
      ::-webkit-scrollbar { width:4px; height:4px; }
      ::-webkit-scrollbar-track { background:transparent; }
      ::-webkit-scrollbar-thumb { background:rgba(255,255,255,.12); border-radius:2px; }
      .q-btn { text-transform:none !important; letter-spacing:0 !important; }
      .q-tab__label { text-transform:none !important; font-size:12px !important; }
      .q-tabs { min-height:36px !important; }
      .q-tab { min-height:36px !important; padding:0 16px !important; }
      .full-input .q-field { width:100% !important; }
      .full-input .q-field__control { width:100% !important; }
      .full-input input { width:100% !important; }
      /* コードブロック: 横スクロールのみ、テキストは折り返さない */
      .code-block { white-space:pre; overflow-x:auto; overflow-y:auto; word-break:normal; max-width:100%; }

    </style>
    """)

    with ui.column().style(
        f"width:100vw;height:100vh;gap:0;background:{C['bg']};overflow:hidden;"
    ):
        # ── トップバー ────────────────────────────────────────────────────────
        with ui.row().style(
            f"width:100%;background:{C['bg2']};border-bottom:0.5px solid {C['border']};"
            f"padding:0 12px;align-items:center;gap:8px;box-sizing:border-box;"
            f"flex-shrink:0;height:44px;"
        ):
            ui.label("Network Agent").style(
                f"font-size:14px;font-weight:500;color:{C['text']};flex-shrink:0;"
            )
            badge("connected", "success")
            ui.label("").style("flex:1;min-width:0;")
            # 右側バッジ群: 幅不足時は overflow:hidden で隠れるだけでレイアウト崩れない
            with ui.row().style(
                "align-items:center;gap:6px;flex-shrink:1;min-width:0;overflow:hidden;"
            ):
                ui.label("device:").style(
                    f"font-size:11px;color:{C['text3']};flex-shrink:0;"
                )
                ui.label(f"cEOS ({DEVICE['ip']})").style(
                    f"font-size:12px;font-weight:500;color:{C['text']};flex-shrink:0;"
                )
                badge(DEVICE["username"], "info")
                badge(f"LOCALE:{LOCALE}", "pending")

        with ui.column().style("flex:1;overflow:hidden;min-height:0;width:100%;"):
            with ui.tabs().style(
                f"background:{C['bg2']};border-bottom:0.5px solid {C['border']};"
                f"color:{C['text2']};flex-shrink:0;"
            ).props("dense dark align='left'") as tabs:
                tab_chat     = ui.tab("Chat",            icon="chat")
                tab_diff     = ui.tab(L("diff_history"), icon="compare_arrows")
                tab_verify   = ui.tab("Verify",          icon="verified").style(
                    f"color:{C['info_fg']};"
                )
                tab_security = ui.tab("Security",        icon="security").style(
                    f"color:{C['danger_fg']};"
                )


            def switch_to_diff():
                tabs.set_value(tab_diff)
                render_diff_tab()

            with ui.tab_panels(tabs, value=tab_chat).style(
                "flex:1;overflow:hidden;min-height:0;width:100%;background:transparent;"
            ).props("dark"):

                # ── Chat タブ ────────────────────────────────────────────────
                with ui.tab_panel(tab_chat).style(
                    f"padding:0;height:100%;display:flex;flex-direction:column;width:100%;"
                    f"background:{C['bg']};"
                ):
                    # ui.scroll_area: NiceGUI ネイティブの .scroll_to() で確実に最下部へ移動
                    # overflow-y:auto の CSS 制御より安定（タブ切り替えのタイミング問題を回避）
                    chat_scroll = ui.scroll_area().style(
                        "flex:1;min-height:0;width:100%;"
                    )
                    with chat_scroll:
                        chat_col = ui.column().style(
                            "padding:14px 16px;gap:8px;width:100%;"
                        )
                        with chat_col:
                            ui.label(L("hint_guide")).style(
                                f"font-size:11px;color:{C['text3']};"
                                f"text-align:center;padding:8px 0;width:100%;"
                            )

                    # ── スクロールヘルパー ────────────────────────────────────
                    async def scroll_to_bottom():
                        """
                        Chat を最新メッセージ（最下部）にスクロールする。
                        asyncio.sleep(0.1) でDOM描画の完了を待ってからスクロール。
                        （タブ切り替え直後やメッセージ追加直後は描画が未完了なため）
                        """
                        await asyncio.sleep(0.1)
                        chat_scroll.scroll_to(percent=1.0)

                    # ── Chat タブ用 Sticky Footer（AI 提案アクション）──────
                    chat_sticky = ui.element("div").style(
                        f"position:sticky;bottom:0;left:0;right:0;z-index:100;"
                        f"background:{C['bg2']}dd;backdrop-filter:blur(8px);"
                        f"border-top:1px solid {C['danger_fg']}44;"
                        "padding:8px 14px;display:none;"
                    )

                    def render_chat_sticky(exec_tags: list):
                        chat_sticky.clear()
                        if not exec_tags:
                            chat_sticky.style(add="display:none;")
                            chat_sticky.style(remove="display:flex;")
                            return
                        chat_sticky.style(remove="display:none;")
                        chat_sticky.style(add="display:flex;align-items:center;flex-wrap:wrap;gap:8px;")
                        with chat_sticky:
                            ui.label("⚠️ AI 提案アクション（確認後に実行）").style(
                                f"font-size:10px;color:{C['warn_fg']};font-weight:600;"
                                "margin-right:4px;flex-shrink:0;align-self:center;"
                            )
                            def _make_chat_sticky_handler(tag_path, tag_params):
                                async def _exec_action():
                                    _qs    = "&".join(f"{k}={v}" for k, v in tag_params.items())
                                    _label = f"{tag_path}?{_qs}"
                                    with ui.dialog() as dlg, ui.card().style(
                                        f"background:{C['bg2']};"
                                        f"border:1px solid {C['danger_fg']}44;"
                                        "border-radius:10px;padding:20px 24px;"
                                        "min-width:340px;gap:12px;"
                                    ):
                                        ui.label("⚠️ 実行確認").style(
                                            f"font-size:14px;font-weight:700;color:{C['danger_fg']};"
                                        )
                                        ui.label("以下のコマンドを XDP Firewall に送信します。").style(
                                            f"font-size:12px;color:{C['text2']};"
                                        )
                                        ui.label(_label).style(
                                            f"font-size:11px;color:{C['danger_fg']};"
                                            f"font-family:{C['mono']};padding:6px 10px;"
                                            f"background:{C['danger_bg']};"
                                            "border-radius:5px;word-break:break-all;"
                                        )
                                        ui.label("この操作は即時反映されます。").style(
                                            f"font-size:11px;color:{C['warn_fg']};"
                                        )
                                        with ui.row().style("gap:8px;justify-content:flex-end;width:100%;"):
                                            ui.button("キャンセル").props("flat dense").style(
                                                f"font-size:12px;color:{C['text2']};"
                                            ).on("click", dlg.close)
                                            def _make_do_cs(p, pr, d):
                                                async def _do():
                                                    d.close()
                                                    try:
                                                        _qs_str = "&".join(f"{k}={v}" for k, v in pr.items())
                                                        _res = await xdp_a2a_post({
                                                            "query":  f"{p}?{_qs_str}",
                                                            "deploy": True,
                                                            **pr,
                                                        })
                                                        _st = _res.get("status", "unknown")
                                                        if _st == "success":
                                                            ui.notify(f"✅ 実行完了: {p} ({_st})", type="positive", timeout=3000)
                                                        else:
                                                            ui.notify(f"⚠️ {p}: {_res.get('message', _st)}", type="warning", timeout=5000)
                                                        # 案X: 実行済みの1件だけ除外して残りを再描画
                                                        # render_chat_sticky([]) で全件消去していたバグを修正
                                                        _remaining = [
                                                            t for t in state.sticky_tags
                                                            if not (
                                                                t.get("path")   == p
                                                                and t.get("params") == pr
                                                            )
                                                        ]
                                                        state.sticky_tags = _remaining
                                                        render_chat_sticky(_remaining)
                                                    except Exception as ex:
                                                        ui.notify(f"❌ エラー: {ex}", type="negative", timeout=5000)
                                                return _do
                                            ui.button("実行する", icon="play_arrow").style(
                                                f"font-size:12px;font-weight:600;"
                                                f"background:{C['danger_bg']};color:{C['danger_fg']};"
                                                f"border:1px solid {C['danger_fg']}66;"
                                                "border-radius:5px;padding:4px 14px;"
                                            ).on("click", _make_do_cs(tag_path, tag_params, dlg))
                                    dlg.open()
                                return _exec_action

                            for _ct in exec_tags:
                                _ct_path   = _ct.get("path", "")
                                _ct_params = _ct.get("params", {})
                                _ct_qs     = "&".join(f"{k}={v}" for k, v in _ct_params.items())
                                with ui.element("div").style(
                                    "display:flex;align-items:center;gap:6px;"
                                ):
                                    ui.label(f"{_ct_path}?{_ct_qs}").style(
                                        f"font-size:10px;color:{C['danger_fg']};"
                                        f"font-family:{C['mono']};padding:3px 7px;"
                                        f"background:{C['danger_bg']};border-radius:4px;"
                                    )
                                    ui.button("実行", icon="play_arrow").props("flat dense").style(
                                        f"font-size:10px;color:{C['danger_fg']};"
                                        f"border:0.5px solid {C['danger_fg']}66;"
                                        "border-radius:4px;padding:1px 8px;flex-shrink:0;"
                                    ).on("click", _make_chat_sticky_handler(_ct_path, _ct_params))

                    # 入力エリア
                    with ui.column().style(
                        f"border-top:0.5px solid {C['border']};"
                        f"padding:10px 16px;gap:8px;background:{C['bg2']};"
                        f"flex-shrink:0;box-sizing:border-box;width:100%;"
                    ):
                        with ui.row().style("gap:8px;align-items:center;flex-wrap:wrap;"):
                            phase_label = ui.label(L("waiting")).style(
                                f"font-size:11px;color:{C['text3']};"
                            )
                            dry_run_chk = ui.checkbox(L("dry_run")).style(
                                f"font-size:12px;color:{C['text']};"
                            )
                            dry_run_chk.value = True
                            ui.label(L("dry_run_active")).style(
                                f"font-size:10px;background:{C['warn_bg']};"
                                f"color:{C['warn_fg']};padding:2px 8px;border-radius:4px;"
                                f"border:0.5px solid {C['warn_fg']}44;"
                            )

                        input_box = ui.input(
                            placeholder=L("hint_input")
                        ).classes("full-input").props("outlined dense dark").style(
                            "width:100%;font-size:13px;"
                        )

                        with ui.row().style(
                            "justify-content:space-between;width:100%;align-items:center;"
                        ):
                            trace_label = ui.label("").style(
                                f"font-size:10px;color:{C['text3']};font-family:{C['mono']};"
                            )
                            send_btn = ui.button(L("send")).props("dark").style(
                                f"font-size:13px;padding:4px 24px;"
                                f"border:0.5px solid {C['border2']};"
                                f"border-radius:6px;color:{C['text']};"
                                f"background:{C['bg3']};"
                            )

                    # ── ロジック ─────────────────────────────────────────────
                    def set_phase(p: str):
                        state.phase = p
                        labels = {
                            "idle":             (L("waiting"),       C["text3"]),
                            "executing":        (L("executing"),     C["warn_fg"]),
                            "awaiting_confirm": (L("await_confirm"), C["success_fg"]),
                            "deploying":        (L("deploying"),     C["warn_fg"]),
                        }
                        txt, col = labels.get(p, (L("waiting"), C["text3"]))
                        phase_label.text = txt
                        phase_label.style(f"font-size:11px;color:{col};")
                        if p != "idle":
                            send_btn.props(add="disabled")
                        else:
                            send_btn.props(remove="disabled")

                    async def do_execute(query: str):
                        set_phase("executing")
                        render_user_msg(query, chat_col)
                        asyncio.create_task(scroll_to_bottom())  # ユーザーメッセージ表示直後にスクロール
                        with chat_col:
                            thinking = ui.label(L("executing_msg")).style(
                                f"font-size:11px;color:{C['text3']};padding:2px 0;"
                            )
                        try:
                            # ── POST /execute → A2A Hub ──────────────────────
                            raw = await api_post("/execute", _exec_payload(query))
                            thinking.delete()

                            p = _parse_execute_result(raw)
                            state.trace_id      = raw.get("trace_id", "")
                            state.pending_xml   = p["xml"]
                            state.pending_query = query
                            trace_label.text    = f"trace: {state.trace_id}"

                            is_read     = p["is_read"]
                            is_security = p.get("is_security", False)
                            is_verify   = p.get("is_verify",   False)   # ANTA 事後検証
                            is_mixed    = p.get("is_mixed",    False)   # 混合クエリ
                            is_dry_run  = p["is_dry_run"]   # ★ dry_run フラグ
                            status      = p["status"]
                            summary     = p["summary"]

                            # 差分履歴に追加（WRITE系のみ。Security/READ/Verify系は除外）
                            if not is_read and not is_security and not is_verify and state.trace_id:
                                state.diff_history.append({
                                    "trace_id":       state.trace_id,
                                    "query":          query,
                                    "diff":           p["diff"],
                                    "xml":            p["xml"],
                                    "session_diff":   p.get("session_diff", {}),
                                    "status":         status,
                                    "time":           datetime.now().strftime("%Y-%m-%d %H:%M"),
                                    "deployed":       False,
                                    "tasks":          p["tasks"],
                                    "task_summaries": p["task_summaries"],
                                    "is_read":        False,
                                })

                            # ── WRITE 系: dry-run → 承認待ち ─────────────────
                            # [修正] NETCONFサーバはdeploy=Falseのとき xml を返さない。
                            # 承認待ち遷移条件を is_dry_run フラグ + task_summaries の
                            # 存在で判定する（pending_xml の空チェックは廃止）。
                            has_tasks = bool(p["task_summaries"]) and any(
                                ts.get("deploy_status") != "blocked"
                                for ts in p["task_summaries"]
                            )
                            if (not is_read
                                    and not is_security
                                    and not is_verify      # ← Verify は承認フロー不要
                                    and dry_run_chk.value
                                    and (is_dry_run or has_tasks)):

                                # ── mixed: read サブクエリ結果を先に表示 ──────
                                if is_mixed and p.get("formatted"):
                                    _mixed_fmt = p["formatted"]
                                    _mixed_esc = (
                                        _mixed_fmt
                                        .replace("&", "&amp;")
                                        .replace("<", "&lt;")
                                        .replace(">", "&gt;")
                                    )
                                    with chat_col:
                                        with ui.row().style(
                                            "justify-content:flex-start;width:100%;margin:2px 0;"
                                        ):
                                            with ui.column().style("gap:3px;max-width:min(94%, 100%);"):
                                                with ui.row().style("align-items:center;gap:6px;"):
                                                    ui.label(
                                                        f"agent · {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                                                    ).style(f"font-size:10px;color:{C['text3']};")
                                                    badge("success", "success")
                                                with ui.card().style(
                                                    f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                                    f"border-radius:2px 12px 12px 12px;"
                                                    f"padding:11px 14px;gap:6px;box-shadow:none;"
                                                ):
                                                    ui.html(
                                                        f'<div style="'
                                                        f'background:{C["bg3"]};'
                                                        f'border:0.5px solid {C["border"]};'
                                                        f'border-radius:6px;'
                                                        f'padding:10px 12px;margin-top:4px;'
                                                        f'font-family:{C["mono"]};'
                                                        f'font-size:11px;line-height:1.7;'
                                                        f'color:{C["text2"]};'
                                                        f'white-space:pre-wrap;'
                                                        f'overflow-y:auto;max-height:320px;">'
                                                        + _mixed_esc + "</div>"
                                                    )

                                set_phase("awaiting_confirm")

                                def _on_approve():
                                    show_approve_dialog(
                                        trace_id=state.trace_id,
                                        on_deploy=_do_deploy_api,
                                    )

                                def _on_edit():
                                    async def _save(new_xml):
                                        state.pending_xml = new_xml
                                        await do_execute(query)
                                    show_xml_editor(state.pending_xml, _save)

                                # session diff サマリーをバブルに表示
                                # ヘッダー行 (-- system:/ / ++ session:/) を除いた実変更行数
                                _sd_b       = p.get("session_diff", {}) or {}
                                _sd_lines_b = _sd_b.get("diff_lines", [])
                                _adds_b = sum(
                                    1 for d in _sd_lines_b
                                    if d.get("op") == "+"
                                    and "session-config" not in d.get("text","")
                                    and not d.get("text","").startswith("++ ")
                                )
                                _dels_b = sum(
                                    1 for d in _sd_lines_b
                                    if d.get("op") == "-"
                                    and "running-config" not in d.get("text","")
                                    and not d.get("text","").startswith("-- ")
                                )
                                if _sd_lines_b and (_adds_b or _dels_b):
                                    _detail_b = (
                                        f"cEOS session diff: +{_adds_b} / -{_dels_b} 行 — "
                                        "Diff / History タブで差分を確認してください。"
                                    )
                                elif _sd_lines_b:
                                    _detail_b = (
                                        "cEOS session diff: 差分なし（既に設定済み）— "
                                        "Diff / History タブで確認してください。"
                                    )
                                else:
                                    _detail_b = L("approve_detail")

                                render_agent_msg(
                                    text=summary,
                                    detail=_detail_b,
                                    status="dry-run",
                                    chat_col=chat_col,
                                    show_approve=True,
                                    on_approve=_on_approve,
                                    on_edit=_on_edit,
                                )
                                switch_to_diff()

                            # ── Verify 系: ANTA テスト結果を Chat に表示 ────
                            elif is_verify:
                                _kind     = (
                                    "success" if status == "success"
                                    else "failure" if status in ("failure", "error")
                                    else "info"   # partial_failure
                                )
                                formatted = p["formatted"]
                                _meta     = p.get("meta", "")

                                with chat_col:
                                    with ui.row().style(
                                        "justify-content:flex-start;width:100%;margin:2px 0;"
                                    ):
                                        with ui.column().style(
                                            "gap:3px;max-width:min(94%, 100%);"
                                        ):
                                            with ui.row().style(
                                                "align-items:center;gap:6px;"
                                            ):
                                                ui.label(
                                                    f"agent · {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                                                ).style(
                                                    f"font-size:10px;color:{C['text3']};"
                                                )
                                                badge(_kind, _kind)
                                                badge("ANTA", "info")
                                            with ui.card().style(
                                                f"background:{C['bg2']};"
                                                f"border:0.5px solid {C['border2']};"
                                                "border-radius:2px 12px 12px 12px;"
                                                "padding:11px 14px;gap:6px;box-shadow:none;"
                                            ):
                                                ui.label(summary).style(
                                                    f"font-size:13px;font-weight:500;"
                                                    f"color:{C['text']};"
                                                )
                                                if _meta:
                                                    ui.label(_meta).style(
                                                        f"font-size:10px;"
                                                        f"color:{C['text3']};"
                                                        f"font-family:{C['mono']};"
                                                    )
                                                if formatted:
                                                    _esc = (
                                                        formatted
                                                        .replace("&", "&amp;")
                                                        .replace("<", "&lt;")
                                                        .replace(">", "&gt;")
                                                    )
                                                    ui.html(
                                                        f'<div style="'
                                                        f'background:{C["bg3"]};'
                                                        f'border:0.5px solid {C["border"]};'
                                                        f'border-radius:6px;'
                                                        f'padding:10px 12px;margin-top:4px;'
                                                        f'font-family:{C["mono"]};'
                                                        f'font-size:11px;line-height:1.7;'
                                                        f'color:{C["text2"]};'
                                                        f'white-space:pre-wrap;'
                                                        f'overflow-y:auto;max-height:320px;">'
                                                        + _esc + "</div>"
                                                    )
                                                # Verify タブへ誘導するボタン
                                                ui.button(
                                                    "🔬 Verify タブで詳細確認",
                                                ).props("flat dense").style(
                                                    f"font-size:11px;padding:2px 9px;"
                                                    f"border:0.5px solid {C['info_fg']}44;"
                                                    f"border-radius:5px;color:{C['info_fg']};"
                                                    f"background:{C['info_bg']};"
                                                    "min-height:26px;margin-top:4px;"
                                                ).on("click", lambda: tabs.set_value(tab_verify))
                                set_phase("idle")
                                asyncio.create_task(scroll_to_bottom())  # レスポンス表示後にスクロール

                            # ── Security 系: XDP 結果を Chat に表示 ────────
                            elif is_security:
                                _kind      = "success" if "success" in status else "info"
                                formatted  = p["formatted"]
                                _exec_tags = p.get("exec_tags", [])

                                with chat_col:
                                    with ui.row().style(
                                        "justify-content:flex-start;width:100%;margin:2px 0;"
                                    ):
                                        with ui.column().style("gap:3px;max-width:min(94%, 100%);"):
                                            with ui.row().style("align-items:center;gap:6px;"):
                                                ui.label(
                                                    f"agent · {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                                                ).style(f"font-size:10px;color:{C['text3']};")
                                                badge(_kind, _kind)
                                            with ui.card().style(
                                                f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                                f"border-radius:2px 12px 12px 12px;"
                                                f"padding:11px 14px;gap:6px;box-shadow:none;"
                                            ):
                                                ui.label(summary).style(
                                                    f"font-size:13px;font-weight:500;color:{C['text']};"
                                                )
                                                if formatted:
                                                    _escaped = (
                                                        formatted
                                                        .replace("&", "&amp;")
                                                        .replace("<", "&lt;")
                                                        .replace(">", "&gt;")
                                                    )
                                                    ui.html(
                                                        f'<div style="'
                                                        f'background:{C["bg3"]};'
                                                        f'border:0.5px solid {C["border"]};'
                                                        f'border-radius:6px;'
                                                        f'padding:10px 12px;'
                                                        f'margin-top:4px;'
                                                        f'font-family:{C["mono"]};'
                                                        f'font-size:11px;'
                                                        f'line-height:1.7;'
                                                        f'color:{C["text2"]};'
                                                        f'white-space:pre-wrap;'
                                                        f'overflow-y:auto;'
                                                        f'max-height:320px;">'
                                                        + _style_exec_tags(_escaped, C, _exec_tags)
                                                        + '</div>'
                                                    )

                                                # ── AI 提案アクション（exec_tags）────────────
                                                # 【案E】チャットカード内は「証跡テキスト」のみ表示。
                                                # 実行ボタンは画面下部の Sticky Footer にのみ配置する。
                                                # 理由:
                                                #   - チャット履歴に「何を提案されたか」が残る（Audit 証跡）
                                                #   - 実行操作は Sticky Footer に一元化（二重表示・誤操作を防ぐ）
                                                #   - チャットカード内のボタンを消すことで重複を解消
                                                if _exec_tags:
                                                    ui.separator().style(
                                                        f"margin:8px 0;background:{C['border']};"
                                                    )
                                                    ui.label("⚠️ AI 提案アクション（画面下部から実行）").style(
                                                        f"font-size:10px;font-weight:600;"
                                                        f"color:{C['warn_fg']};"
                                                    )
                                                    for _tag in _exec_tags:
                                                        _t_path   = _tag.get("path", "")
                                                        _t_params = _tag.get("params", {})
                                                        _t_qs     = "&".join(f"{k}={v}" for k, v in _t_params.items())
                                                        # ラベルのみ（ボタンなし）: 証跡テキストとしてチャット履歴に永続
                                                        ui.label(f"{_t_path}?{_t_qs}").style(
                                                            f"font-size:10px;color:{C['danger_fg']};"
                                                            f"font-family:{C['mono']};padding:3px 7px;"
                                                            f"background:{C['danger_bg']};border-radius:4px;"
                                                            "margin-top:4px;display:block;"
                                                        )
                                # ── analyze 未実行の場合、自動で AI 解析を追加実行 ─
                                _sec_action = (p.get("exec_tags") is not None
                                               and p.get("formatted", "").startswith("["))
                                # inner の action フィールドで判定
                                _inner_action = ""
                                try:
                                    import json as _json
                                    _r = raw.get("result", {}) or {}
                                    _inner_action = _r.get("action", "")
                                except Exception:
                                    pass
                                if _inner_action not in ("analyze",) and "success" in status:
                                    # 統計系クエリ → 自動で analyze を追加実行して提案表示
                                    with chat_col:
                                        _analyze_thinking = ui.label(
                                            "⟳ AI がセキュリティ脅威を解析中..."
                                        ).style(
                                            f"font-size:11px;color:{C['text3']};padding:2px 0;"
                                        )
                                    try:
                                        _analyze_raw = await api_post(
                                            "/execute",
                                            _exec_payload("状況を分析してセキュリティ脅威の提案をして"),
                                        )
                                        _analyze_p = _parse_execute_result(_analyze_raw)
                                        _a_exec_tags = _analyze_p.get("exec_tags", [])
                                        _a_formatted = _analyze_p.get("formatted", "")
                                        try:
                                            _analyze_thinking.delete()
                                        except Exception:
                                            pass
                                        if _a_formatted or _a_exec_tags:
                                            with chat_col:
                                                with ui.row().style(
                                                    "justify-content:flex-start;width:100%;margin:2px 0;"
                                                ):
                                                    with ui.column().style("gap:3px;max-width:min(94%, 100%);"):
                                                        with ui.row().style("align-items:center;gap:6px;"):
                                                            ui.label(
                                                                f"agent · {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                                                            ).style(f"font-size:10px;color:{C['text3']};")
                                                            badge("info", "info")
                                                        with ui.card().style(
                                                            f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                                            f"border-radius:2px 12px 12px 12px;"
                                                            f"padding:11px 14px;gap:6px;box-shadow:none;"
                                                        ):
                                                            ui.label("AI セキュリティ解析").style(
                                                                f"font-size:13px;font-weight:500;color:{C['text']};"
                                                            )
                                                            if _a_formatted:
                                                                _a_esc = (
                                                                    _a_formatted
                                                                    .replace("&", "&amp;")
                                                                    .replace("<", "&lt;")
                                                                    .replace(">", "&gt;")
                                                                )
                                                                ui.html(
                                                                    f'<div style="'
                                                                    f'background:{C["bg3"]};'
                                                                    f'border:0.5px solid {C["border"]};'
                                                                    f'border-radius:6px;'
                                                                    f'padding:10px 12px;'
                                                                    f'margin-top:4px;'
                                                                    f'font-family:{C["mono"]};'
                                                                    f'font-size:11px;'
                                                                    f'line-height:1.7;'
                                                                    f'color:{C["text2"]};'
                                                                    f'white-space:pre-wrap;'
                                                                    f'overflow-y:auto;'
                                                                    f'max-height:360px;">'
                                                                    + _style_exec_tags(_a_esc, C, _a_exec_tags)
                                                                    + '</div>'
                                                                )
                                                            if _a_exec_tags:
                                                                ui.separator().style(
                                                                    f"margin:8px 0;background:{C['border']};"
                                                                )
                                                                # 【案E】auto-analyze の exec_tags もラベルのみ（証跡テキスト）
                                                                # 実行は画面下部 Sticky Footer に一元化
                                                                ui.label("⚠️ AI 提案アクション（画面下部から実行）").style(
                                                                    f"font-size:10px;font-weight:600;"
                                                                    f"color:{C['warn_fg']};"
                                                                )
                                                                for _tag in _a_exec_tags:
                                                                    _t_path   = _tag.get("path", "")
                                                                    _t_params = _tag.get("params", {})
                                                                    _t_qs     = "&".join(
                                                                        f"{k}={v}" for k, v in _t_params.items()
                                                                    )
                                                                    # ラベルのみ: ボタンなし
                                                                    ui.label(f"{_t_path}?{_t_qs}").style(
                                                                        f"font-size:10px;color:{C['danger_fg']};"
                                                                        f"font-family:{C['mono']};padding:3px 7px;"
                                                                        f"background:{C['danger_bg']};border-radius:4px;"
                                                                        "margin-top:4px;display:block;"
                                                                    )
                                    except Exception:
                                        try:
                                            _analyze_thinking.delete()
                                        except Exception:
                                            pass
                                # ── Chat sticky footer に exec_tags を反映 ──
                                # 【案E】_a_exec_tags（auto-analyze）と _exec_tags（統計付随）を
                                # 結合して全件 Sticky Footer に渡す。
                                # or で上書きしていたため件数が欠落していたバグを修正。
                                try:
                                    _a_tags = locals().get("_a_exec_tags") or []
                                    _s_tags = _exec_tags or []
                                    # 重複を除去しつつ全件結合（_a_tags 優先・順序保持）
                                    _seen_sticky = set()
                                    _sticky_tags = []
                                    for _st in list(_a_tags) + list(_s_tags):
                                        _key = (
                                            _st.get("path", ""),
                                            str(_st.get("params", {}))
                                        )
                                        if _key not in _seen_sticky:
                                            _seen_sticky.add(_key)
                                            _sticky_tags.append(_st)
                                    # state に保存（1件実行後に残りを再描画するため）
                                    state.sticky_tags = _sticky_tags
                                    render_chat_sticky(_sticky_tags)
                                except Exception:
                                    pass
                                set_phase("idle")
                                asyncio.create_task(scroll_to_bottom())  # レスポンス表示後にスクロール

                            # ── READ 系: eAPI show 結果を表示 ──────────────
                            elif is_read:
                                _kind     = "success" if "success" in status else "info"
                                formatted = p["formatted"]
                                cmds      = p["cmds"]

                                with chat_col:
                                    with ui.row().style(
                                        "justify-content:flex-start;width:100%;margin:2px 0;"
                                    ):
                                        with ui.column().style("gap:3px;max-width:min(94%, 100%);"):
                                            with ui.row().style("align-items:center;gap:6px;"):
                                                ui.label(
                                                    f"agent · {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                                                ).style(f"font-size:10px;color:{C['text3']};")
                                                badge(_kind, _kind)
                                            with ui.card().style(
                                                f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                                f"border-radius:2px 12px 12px 12px;"
                                                f"padding:11px 14px;gap:6px;box-shadow:none;"
                                            ):
                                                ui.label(summary).style(
                                                    f"font-size:13px;font-weight:500;color:{C['text']};"
                                                )
                                                if cmds:
                                                    ui.label(
                                                        f"cmd: {', '.join(cmds)}"
                                                    ).style(
                                                        f"font-size:10px;color:{C['text3']};"
                                                        f"font-family:{C['mono']};"
                                                    )
                                                if formatted:
                                                    # white-space:pre-wrap を使う。
                                                    # pre-wrap は改行・空白を保持しつつ、
                                                    # 幅が足りなければ折り返す（カード幅に従う）。
                                                    # テーブルの桁揃えは保持される。
                                                    _escaped = (
                                                        formatted
                                                        .replace("&", "&amp;")
                                                        .replace("<", "&lt;")
                                                        .replace(">", "&gt;")
                                                    )
                                                    ui.html(
                                                        f'<div style="'
                                                        f'background:{C["bg3"]};'
                                                        f'border:0.5px solid {C["border"]};'
                                                        f'border-radius:6px;'
                                                        f'padding:10px 12px;'
                                                        f'margin-top:4px;'
                                                        f'font-family:{C["mono"]};'
                                                        f'font-size:11px;'
                                                        f'line-height:1.7;'
                                                        f'color:{C["text2"]};'
                                                        f'white-space:pre-wrap;'
                                                        f'overflow-y:auto;'
                                                        f'max-height:320px;">'
                                                        + _style_exec_tags(_escaped, C, [])
                                                        + '</div>'
                                                    )
                                set_phase("idle")
                                asyncio.create_task(scroll_to_bottom())  # レスポンス表示後にスクロール

                            # ── WRITE 系 dry-run OFF: そのまま表示 ───────────
                            else:
                                # dry_run / no_changes / skipped も正常扱い
                                _ok_statuses = ("success", "dry_run", "no_changes", "all_success")
                                _kind = "success" if any(s in status for s in _ok_statuses) else "failure"
                                _task_detail = ""
                                for ts in p["task_summaries"]:
                                    ds = ts.get("deploy_status", "")
                                    _task_detail += (
                                        f"[{ts.get('task_id','?')}] "
                                        f"{ts.get('operation','')}/{ts.get('target','')} "
                                        f"→ {ds}\n"
                                    )
                                render_agent_msg(
                                    text=summary,
                                    detail=_task_detail.strip(),
                                    status=_kind,
                                    chat_col=chat_col,
                                )
                                set_phase("idle")
                                asyncio.create_task(scroll_to_bottom())  # レスポンス表示後にスクロール

                        except Exception as e:
                            try:
                                thinking.delete()
                            except Exception:
                                pass
                            render_agent_msg(
                                text=L("error_title"),
                                detail=str(e),
                                status="failure",
                                chat_col=chat_col,
                            )
                            set_phase("idle")
                            asyncio.create_task(scroll_to_bottom())  # エラー時もスクロール

                    async def _do_deploy_api():
                        set_phase("deploying")
                        with chat_col:
                            dep_msg = ui.label(L("deploying_msg")).style(
                                f"font-size:11px;color:{C['text3']};padding:2px 0;"
                            )
                        try:
                            # ── POST /deploy/{trace_id} → A2A Hub ────────────
                            raw = await api_post(
                                f"/deploy/{state.trace_id}", _deploy_payload()
                            )
                            dep_msg.delete()

                            p       = _parse_deploy_result(raw)
                            status  = p["status"]
                            summary = p["summary"]
                            # [修正] _parse_deploy_result が正規化した kind を使う
                            kind    = p["status_kind"]

                            # 差分履歴を更新
                            for h in reversed(state.diff_history):
                                if h["trace_id"] == state.trace_id:
                                    h["deployed"]         = True
                                    h["deploy_diff"]      = p["deploy_diff"]
                                    h["deploy_status"]    = status
                                    h["task_summaries"]   = p["task_summaries"]
                                    break

                            # タスク結果の詳細を表示
                            task_detail = ""
                            for ts in p["task_summaries"]:
                                ds = ts.get("deploy_status", "")
                                am = ts.get("audit_message", "")
                                task_detail += (
                                    f"[{ts.get('task_id','?')}] "
                                    f"{ts.get('operation','')}/{ts.get('target','')} "
                                    f"deploy:{ds}"
                                )
                                if am:
                                    task_detail += f" | {am}"
                                task_detail += "\n"

                            with chat_col:
                                with ui.row().style(
                                    "justify-content:flex-start;width:100%;margin:2px 0;"
                                ):
                                    with ui.column().style("gap:3px;max-width:min(94%, 100%);"):
                                        with ui.row().style("align-items:center;gap:6px;"):
                                            ui.label(
                                                f"agent · {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                                            ).style(f"font-size:10px;color:{C['text3']};")
                                            badge(kind, kind)
                                        with ui.card().style(
                                            f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                            f"border-radius:2px 12px 12px 12px;"
                                            f"padding:11px 14px;gap:5px;box-shadow:none;"
                                        ):
                                            ui.label(summary).style(
                                                f"font-size:13px;font-weight:500;color:{C['text']};"
                                            )
                                            if task_detail:
                                                ui.html(
                                                    f'<div style="background:{C["bg3"]};'
                                                    f'border-radius:6px;padding:8px;'
                                                    f'font-family:{C["mono"]};font-size:10px;'
                                                    f'color:{C["text2"]};white-space:pre-wrap;'
                                                    f'margin-top:4px;">'
                                                    + task_detail.strip().replace("<","&lt;").replace(">","&gt;")
                                                    + "</div>"
                                                )
                                            ui.label(L("audit_scope")).style(
                                                f"font-size:10px;color:{C['text3']};"
                                            )
                                            ui.button(
                                                L("diff_tab_confirm")
                                            ).props("flat dense").style(
                                                f"font-size:11px;padding:2px 9px;"
                                                f"border:0.5px solid {C['border2']};"
                                                f"border-radius:5px;color:{C['text2']};"
                                                f"background:{C['bg3']};min-height:26px;margin-top:6px;"
                                            ).on("click", switch_to_diff)

                            ui.notify(
                                summary,
                                color="positive" if kind == "success" else "negative",
                            )

                            # ── CNV 自動 Post-Check ────────────────────────────
                            # deploy が成功し、かつ Before Snapshot 取得済みの場合に
                            # バックグラウンドで ANTA post_check を自動実行する。
                            # Hub 側の /deploy が snapshot_id を受け取り
                            # asyncio.create_task で非同期実行 → _push_log で通知済み。
                            # UI 側では「実行中」表示 → WS 完了通知で結果カードを追加する。
                            snap_id_for_cnv = _verify_state.snap_id
                            cnv_status = raw.get("anta_post_check", "skipped")

                            if snap_id_for_cnv and cnv_status != "skipped":
                                # 「実行中」ラベルを表示（WS の通知が来るまでの仮表示）
                                with chat_col:
                                    cnv_pending = ui.label(L("cnv_running")).style(
                                        f"font-size:11px;color:{C['text3']};padding:2px 0;"
                                    )

                                async def _wait_cnv_result(pending_lbl=cnv_pending):
                                    """Hub が非同期実行した Post-Check の完了を待ち、結果を表示する。"""
                                    _max_wait = 120   # 最大 120 秒待機
                                    _interval = 3
                                    _elapsed  = 0
                                    while _elapsed < _max_wait:
                                        await asyncio.sleep(_interval)
                                        _elapsed += _interval
                                        try:
                                            diff_raw = await api_get(
                                                f"/diff/{state.trace_id}"
                                            )
                                            cnv_res = diff_raw.get(
                                                "deploy_result", {}
                                            ).get("anta_post_check_result")
                                            if cnv_res is None:
                                                continue  # まだ実行中
                                        except Exception:
                                            continue

                                        # 完了：仮ラベルを消して結果カードを表示
                                        try:
                                            pending_lbl.delete()
                                        except Exception:
                                            pass

                                        cnv_summary  = cnv_res.get("summary", "")
                                        new_issues   = cnv_res.get("new_issues", [])
                                        cnv_ok       = (
                                            cnv_res.get("status") not in ("failure", "error")
                                            and not new_issues
                                        )
                                        cnv_msg   = L("cnv_done_ok") if cnv_ok else L("cnv_done_ng")
                                        cnv_kind  = "success" if cnv_ok else "failure"

                                        with chat_col:
                                            with ui.row().style(
                                                "justify-content:flex-start;"
                                                "width:100%;margin:2px 0;"
                                            ):
                                                with ui.column().style(
                                                    "gap:3px;max-width:min(94%, 100%);"
                                                ):
                                                    with ui.row().style(
                                                        "align-items:center;gap:6px;"
                                                    ):
                                                        ui.label(
                                                            f"agent · {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                                                        ).style(
                                                            f"font-size:10px;color:{C['text3']};"
                                                        )
                                                        badge(L("cnv_label"), cnv_kind)
                                                    with ui.card().style(
                                                        f"background:{C['bg2']};"
                                                        f"border:0.5px solid {C['border2']};"
                                                        f"border-radius:2px 12px 12px 12px;"
                                                        f"padding:11px 14px;gap:5px;"
                                                        f"box-shadow:none;"
                                                    ):
                                                        ui.label(cnv_msg).style(
                                                            f"font-size:13px;font-weight:500;"
                                                            f"color:{C['text']};"
                                                        )
                                                        if cnv_summary:
                                                            ui.label(cnv_summary).style(
                                                                f"font-size:11px;"
                                                                f"color:{C['text2']};"
                                                                f"line-height:1.5;"
                                                            )
                                                        if new_issues:
                                                            ui.label(
                                                                L("cnv_new_issues")
                                                            ).style(
                                                                f"font-size:10px;"
                                                                f"font-weight:600;"
                                                                f"color:{C['warn_fg']};"
                                                                f"margin-top:4px;"
                                                            )
                                                            for issue in new_issues:
                                                                ui.label(
                                                                    f"  {issue}"
                                                                ).style(
                                                                    f"font-size:10px;"
                                                                    f"color:{C['danger_fg']};"
                                                                    f"font-family:{C['mono']};"
                                                                    f"white-space:pre-wrap;"
                                                                )

                                        # ui.notify() はバックグラウンドタスクから呼べないため除去。
                                        # 結果は上の ui.card() カードで表示済み。
                                        asyncio.create_task(scroll_to_bottom())
                                        return   # 完了

                                    # タイムアウト：仮ラベルを消してエラー表示
                                    try:
                                        pending_lbl.delete()
                                    except Exception:
                                        pass
                                    with chat_col:
                                        ui.label(L("cnv_error")).style(
                                            f"font-size:11px;color:{C['warn_fg']};"
                                            f"padding:2px 0;"
                                        )

                                asyncio.create_task(_wait_cnv_result())

                            elif not snap_id_for_cnv:
                                # Before Snapshot 未取得のためスキップ（情報として小さく表示）
                                with chat_col:
                                    ui.label(L("cnv_skipped")).style(
                                        f"font-size:10px;color:{C['text3']};padding:2px 0;"
                                    )
                            # ── CNV 自動 Post-Check ここまで ───────────────────

                        except Exception as e:
                            try:
                                dep_msg.delete()
                            except Exception:
                                pass
                            render_agent_msg(
                                text=L("deploy_fail"),
                                detail=str(e),
                                status="failure",
                                chat_col=chat_col,
                            )
                        finally:
                            set_phase("idle")
                            asyncio.create_task(scroll_to_bottom())  # レスポンス表示後にスクロール

                    async def send_message():
                        query = input_box.value.strip()
                        if not query or state.phase != "idle":
                            return
                        input_box.value = ""
                        await do_execute(query)

                    send_btn.on("click", send_message)
                    input_box.on("keydown.enter", send_message)


                # ── Diff / History タブ ───────────────────────────────────────
                with ui.tab_panel(tab_diff).style(
                    f"padding:0;height:100%;display:flex;flex-direction:column;width:100%;"
                    f"background:{C['bg']};"
                ):
                    with ui.row().style(
                        f"padding:10px 16px;border-bottom:0.5px solid {C['border']};"
                        f"align-items:center;gap:10px;flex-shrink:0;background:{C['bg2']};"
                    ):
                        ui.label(L("diff_history")).style(
                            f"font-size:12px;font-weight:500;color:{C['text']};"
                        )
                        ui.label("").style("flex:1;")
                        refresh_btn = ui.button(L("diff_refresh")).props("flat dense").style(
                            f"font-size:11px;color:{C['text2']};"
                            f"border:0.5px solid {C['border2']};border-radius:5px;"
                        )

                    diff_body = ui.column().style(
                        "flex:1;overflow-y:auto;padding:12px 16px;gap:12px;min-height:0;"
                    )

                    def render_diff_tab():
                        diff_body.clear()
                        with diff_body:
                            if not state.diff_history:
                                ui.label(L("diff_empty")).style(
                                    f"font-size:12px;color:{C['text3']};"
                                    "text-align:center;padding:32px 0;width:100%;"
                                )
                                return
                            for entry in reversed(state.diff_history):
                                with ui.card().style(
                                    f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                    "border-radius:10px;padding:14px 16px;"
                                    "gap:10px;box-shadow:none;width:100%;"
                                ):
                                    # ヘッダー
                                    with ui.row().style(
                                        "align-items:center;gap:8px;flex-wrap:wrap;"
                                        "padding-bottom:8px;"
                                        "border-bottom:0.5px solid rgba(255,255,255,0.06);"
                                    ):
                                        badge(
                                            "deployed" if entry.get("deployed") else "dry-run",
                                            "success"  if entry.get("deployed") else "dry-run",
                                        )
                                        ui.label(entry["query"]).style(
                                            f"font-size:13px;font-weight:500;color:{C['text']};"
                                        )
                                        ui.label("").style("flex:1;")
                                        ui.label(entry["time"]).style(
                                            f"font-size:10px;color:{C['text3']};"
                                        )
                                        ui.label(entry["trace_id"]).style(
                                            f"font-size:10px;color:{C['text3']};"
                                            f"font-family:{C['mono']};"
                                        )

                                    # ① 設定差分
                                    ui.label(L("diff_label")).style(
                                        f"font-size:10px;font-weight:500;color:{C['text3']};"
                                        "text-transform:uppercase;letter-spacing:.06em;"
                                    )
                                    _sd       = entry.get("session_diff", {}) or {}
                                    _sd_lines = _sd.get("diff_lines", [])
                                    _sd_status= _sd.get("status", "")
                                    _xml      = entry.get("xml", "")
                                    _tss      = entry.get("task_summaries", [])

                                    if _sd_lines:
                                        # ★ session diff (+/- 形式) を Junos 方式で表示
                                        _diff_html = (
                                            f'<div style="background:{C["bg3"]};'
                                            f'border:0.5px solid {C["border"]};'
                                            f'border-radius:6px;padding:10px;'
                                            f'font-family:{C["mono"]};font-size:11px;'
                                            f'overflow-x:auto;max-height:220px;overflow-y:auto;">'
                                        )
                                        # ヘッダー行 (-- system:/ / ++ session:/) の判定
                                        # これらはメタ情報のため薄く表示する
                                        _real_adds = 0
                                        _real_dels = 0
                                        for _dl in _sd_lines:
                                            _op   = _dl.get("op", " ")
                                            _text = _dl.get("text", "").replace("<","&lt;").replace(">","&gt;")
                                            _is_header = (
                                                "system:/running-config" in _text
                                                or "session-config" in _text
                                                or _text.startswith("-- ")
                                                or _text.startswith("++ ")
                                            )
                                            if _is_header:
                                                # ヘッダー行: 薄いグレーで小さく表示
                                                _diff_html += (
                                                    f'<div style="color:{C["text3"]};line-height:1.5;'
                                                    f'font-size:9px;white-space:pre;opacity:0.6;">'
                                                    f'{_op} {_text}</div>'
                                                )
                                            else:
                                                if _op == "+":
                                                    _color = C["success_fg"]
                                                    _real_adds += 1
                                                elif _op == "-":
                                                    _color = C["danger_fg"]
                                                    _real_dels += 1
                                                else:
                                                    _color = C["text3"]
                                                _diff_html += (
                                                    f'<div style="color:{_color};line-height:1.7;'
                                                    f'font-size:11px;font-weight:500;white-space:pre;">'
                                                    f'{_op} {_text}</div>'
                                                )
                                        _diff_html += "</div>"
                                        ui.html(_diff_html)
                                        # 変更サマリーと注記
                                        _summary_txt = f"+{_real_adds} / -{_real_dels} 行"
                                        ui.label(
                                            f"cEOS configure session diff — {_summary_txt}"
                                        ).style(
                                            f"font-size:9px;color:{C['text3']};"
                                            f"font-family:{C['mono']};margin-top:2px;"
                                        )

                                    elif _sd_status == "skipped" and _xml:
                                        # session diff 対象外 → 生成 XML を表示
                                        _xp = _xml[:1200] + ("..." if len(_xml) > 1200 else "")
                                        ui.html(
                                            f'<div style="background:{C["bg3"]};'
                                            f'border:0.5px solid {C["border"]};'
                                            f'border-radius:6px;padding:8px;'
                                            f'font-family:{C["mono"]};font-size:10px;'
                                            f'color:{C["text2"]};white-space:pre-wrap;'
                                            f'overflow-x:auto;max-height:180px;overflow-y:auto;">'
                                            + _xp.replace("<", "&lt;").replace(">", "&gt;")
                                            + f'<div style="color:{C["text3"]};margin-top:6px;">'
                                            f'(session diff スキップ: {_sd.get("message","")[:80]})</div>'
                                            + "</div>"
                                        )

                                    elif _tss:
                                        # session diff もXMLもない → task_summaries を表示
                                        _lines = ""
                                        for ts in _tss:
                                            ds = ts.get("deploy_status", "")
                                            _lines += (
                                                f"[{ts.get('task_id','?')}] "
                                                f"{ts.get('operation','')}/{ts.get('target','')} "
                                                f"→ {ds}" + "\n"
                                            )
                                        ui.html(
                                            f'<div style="background:{C["bg3"]};'
                                            f'border:0.5px solid {C["border"]};'
                                            f'border-radius:6px;padding:8px;'
                                            f'font-family:{C["mono"]};font-size:10px;'
                                            f'color:{C["warn_fg"]};white-space:pre-wrap;'
                                            f'overflow-x:auto;max-height:180px;overflow-y:auto;">'
                                            + _lines.strip().replace("<", "&lt;").replace(">", "&gt;")
                                            + "</div>"
                                        )
                                    else:
                                        ui.label(L("no_diff")).style(
                                            f"font-size:11px;color:{C['text3']};"
                                        )

                                    # ── AI による変更要約 ──────────────────
                                    _ai_summary = _sd.get("ai_summary", "")
                                    if _ai_summary:
                                        ui.html(
                                            f'<div style="margin-top:8px;padding:8px 10px;'
                                            f'background:{C["bg3"]};'
                                            f'border-left:3px solid {C["primary"]};'
                                            f'border-radius:0 6px 6px 0;'
                                            f'font-size:12px;color:{C["text1"]};'
                                            f'line-height:1.6;">'
                                            f'<span style="font-size:10px;font-weight:600;'
                                            f'color:{C["primary"]};letter-spacing:.04em;">'
                                            f'🤖 AI 変更要約</span><br>'
                                            + _ai_summary.replace("<","&lt;").replace(">","&gt;")
                                            + "</div>"
                                        )

                                    # Deploy Diff (Audit 結果)
                                    if entry.get("deployed") and entry.get("deploy_diff"):
                                        ui.label(L("deploy_diff")).style(
                                            f"font-size:10px;font-weight:500;color:{C['success_fg']};"
                                            "text-transform:uppercase;letter-spacing:.06em;margin-top:2px;"
                                        )
                                        # task_summaries から audit 情報を整形表示
                                        for ts in entry.get("task_summaries", []):
                                            ds = ts.get("deploy_status", "")
                                            am = ts.get("audit_message", "")
                                            sc = ts.get("audit_scope", "")
                                            _color = (
                                                C["success_fg"] if ds == "success"
                                                else C["warn_fg"] if ds == "no_changes"
                                                else C["danger_fg"]
                                            )
                                            ui.html(
                                                f'<div style="font-family:{C["mono"]};'
                                                f'font-size:10px;color:{_color};'
                                                f'padding:2px 0;line-height:1.6;">'
                                                f"[{ts.get('task_id','?')}] "
                                                f"{ts.get('operation','')}/{ts.get('target','')} "
                                                f"→ {ds} | {am}<br>"
                                                f'<span style="color:{C["text3"]};">'
                                                f"scope: {sc}</span></div>"
                                            )

                                    # ② AI の判断根拠（Reasoning）
                                    _tasks = entry.get("tasks", [])
                                    if _tasks:
                                        with ui.expansion(
                                            L("reasoning_label"), icon="psychology"
                                        ).props("dense dark").style(
                                            f"background:{C['bg3']};border:0.5px solid {C['border']};"
                                            "border-radius:6px;margin-top:4px;"
                                            f"color:{C['text2']};font-size:12px;"
                                        ):
                                            for tk in _tasks:
                                                with ui.row().style(
                                                    "gap:8px;padding:5px 0;"
                                                    f"border-bottom:0.5px solid {C['border']};"
                                                    "align-items:flex-start;"
                                                ):
                                                    ui.label(tk.get("id", "")).style(
                                                        f"font-size:10px;color:{C['info_fg']};"
                                                        f"font-family:{C['mono']};min-width:52px;"
                                                    )
                                                    with ui.column().style("gap:2px;"):
                                                        ui.label(tk.get("description", "")).style(
                                                            f"font-size:11px;color:{C['text']};"
                                                        )
                                                        ui.label(
                                                            f"op={tk.get('operation','')}  "
                                                            f"target={tk.get('target','')}  "
                                                            f"yang={tk.get('yang_path','')}"
                                                        ).style(
                                                            f"font-size:10px;color:{C['text3']};"
                                                            f"font-family:{C['mono']};"
                                                        )

                                    # ③ タスクサマリー（折りたたみ）
                                    _ts_list = entry.get("task_summaries", [])
                                    if _ts_list:
                                        with ui.expansion(
                                            L("logs_label"), icon="terminal"
                                        ).props("dense dark").style(
                                            f"background:{C['bg3']};border:0.5px solid {C['border']};"
                                            "border-radius:6px;margin-top:4px;"
                                            f"color:{C['text2']};font-size:12px;"
                                        ):
                                            for ts in _ts_list:
                                                ds = ts.get("deploy_status", "")
                                                am = ts.get("audit_message", "")
                                                sc = ts.get("audit_scope", "")
                                                dm = ts.get("deploy_message", "")
                                                _c = (
                                                    C["success_fg"] if ds == "success"
                                                    else C["warn_fg"] if ds == "no_changes"
                                                    else C["danger_fg"]
                                                )
                                                ui.html(
                                                    f'<div style="font-family:{C["mono"]};'
                                                    f'font-size:10px;padding:4px 0;'
                                                    f'border-bottom:0.5px solid {C["border"]};'
                                                    f'line-height:1.7;">'
                                                    f'<span style="color:{C["info_fg"]};">'
                                                    f"[{ts.get('task_id','?')}]</span> "
                                                    f'<span style="color:{_c};">{ds}</span><br>'
                                                    f'<span style="color:{C["text2"]};">deploy: {dm[:80]}</span><br>'
                                                    f'<span style="color:{C["text2"]};">audit:  {am}</span><br>'
                                                    f'<span style="color:{C["text3"]};">scope:  {sc}</span>'
                                                    + "</div>"
                                                )

                                    # 承認ボタン（未デプロイ・WRITE系のみ）
                                    if (not entry.get("deployed")
                                            and entry.get("trace_id") == state.trace_id
                                            and not entry.get("is_read")):
                                        ui.separator().style(
                                            f"margin:6px 0;background:{C['border']};"
                                        )
                                        _tid = entry["trace_id"]
                                        def _make_confirm(tid):
                                            def _confirm():
                                                show_approve_dialog(
                                                    trace_id=tid,
                                                    on_deploy=_do_deploy_api,
                                                )
                                            return _confirm
                                        ui.button(L("confirm_btn")).style(
                                            f"width:100%;padding:10px;"
                                            "font-size:13px;font-weight:500;"
                                            f"background:{C['success_bg']};color:{C['success_fg']};"
                                            f"border:0.5px solid {C['success_fg']}66;"
                                            "border-radius:6px;margin-top:2px;"
                                        ).on("click", _make_confirm(_tid))

                        refresh_btn.on("click", render_diff_tab)

                    def on_tab_change(e):
                        args_str = str(e.args) if hasattr(e, "args") else ""
                        if "Diff" in args_str:
                            render_diff_tab()
                        # Chat タブに戻ったとき → 0.1秒待ってDOM描画完了後に最下部スクロール
                        if "Chat" in args_str:
                            asyncio.create_task(scroll_to_bottom())

                    tabs.on("update:modelValue", lambda e: on_tab_change(e))
                    render_diff_tab()

                # ── Verify タブ (ANTA) ────────────────────────────────────────
                with ui.tab_panel(tab_verify).style(
                    f"padding:0;height:100%;display:flex;flex-direction:column;"
                    f"width:100%;background:{C['bg']};"
                ):
                    # ── ヘッダー ─────────────────────────────────────────────
                    with ui.row().style(
                        f"padding:8px 16px;border-bottom:0.5px solid {C['border']};"
                        f"align-items:center;gap:10px;flex-shrink:0;background:{C['bg2']};"
                    ):
                        ui.icon("verified").style(
                            f"color:{C['info_fg']};font-size:14px;"
                        )
                        ui.label(L("verify_tab_title")).style(
                            f"font-size:12px;font-weight:600;color:{C['text']};"
                        )
                        ui.label("").style("flex:1;")
                        # snap_id バッジ（取得済みのとき表示）
                        verify_snap_badge = ui.label("").style(
                            f"font-size:10px;font-family:{C['mono']};"
                            f"color:{C['success_fg']};display:none;"
                        )

                    # ── スクロール可能なボディ ─────────────────────────────
                    verify_body = ui.column().style(
                        "flex:1;overflow-y:auto;padding:14px 16px;"
                        "gap:14px;min-height:0;width:100%;box-sizing:border-box;"
                    )

                    # ── ボタンハンドラ（render_verify_tab より前に定義必須） ───
                    # NiceGUI: render 関数内で .on("click", fn) と参照するため
                    # fn は render_verify_tab() が呼ばれる前に定義されていなければならない。

                    async def _on_snap():
                        """Step 1: Before Snapshot — 設定変更前の状態を ANTA で記録する。"""
                        if _verify_state.running:
                            return
                        if not _verify_state.selected_cats:
                            ui.notify("カテゴリを1つ以上選択してください", type="warning")
                            return
                        _verify_state.running = True
                        ui.notify(L("verify_running"), timeout=2000)
                        try:
                            result = await anta_a2a_post({
                                "action":    "snapshot",
                                "query":     "設定変更前のスナップショットを取得",
                                "tests":     _verify_state.selected_cats,
                                "device_ip": DEVICE["ip"],
                                "username":  DEVICE["username"],
                                "password":  DEVICE["password"],
                                "locale":    LOCALE,
                            })
                            sid = result.get("snapshot_id", "")
                            if sid:
                                _verify_state.snap_id     = sid
                                _verify_state.snap_ts     = datetime.now().strftime(
                                    "%Y-%m-%d %H:%M:%S"
                                )
                                _verify_state.last_result = result
                                ui.notify(f"✅ snapshot: {sid}", type="positive", timeout=4000)
                                verify_snap_badge.text = f"snap: {sid[:24]}…"
                                verify_snap_badge.style(remove="display:none;")
                                verify_snap_badge.style(add="display:inline;")
                            else:
                                _verify_state.last_result = result
                                ui.notify(
                                    result.get("message", "スナップショット取得失敗"),
                                    type="negative", timeout=5000,
                                )
                        except Exception as e:
                            ui.notify(f"❌ {e}", type="negative", timeout=5000)
                        finally:
                            _verify_state.running = False
                        render_verify_tab()

                    async def _on_verify_now():
                        """Step 2: Verify Now — 現在の状態を ANTA で即時テストする。"""
                        if _verify_state.running:
                            return
                        if not _verify_state.selected_cats:
                            ui.notify("カテゴリを1つ以上選択してください", type="warning")
                            return
                        _verify_state.running = True
                        ui.notify(L("verify_running"), timeout=2000)
                        try:
                            result = await anta_a2a_post({
                                "action":    "verify",
                                "query":     "現在の状態をANTAでテスト",
                                "tests":     _verify_state.selected_cats,
                                "device_ip": DEVICE["ip"],
                                "username":  DEVICE["username"],
                                "password":  DEVICE["password"],
                                "locale":    LOCALE,
                            })
                            _verify_state.last_result = result
                            st     = result.get("status", "")
                            sm     = result.get("summary", "")
                            n_type = "positive" if st == "success" else "warning"
                            ui.notify(sm or st, type=n_type, timeout=4000)
                        except Exception as e:
                            ui.notify(f"❌ {e}", type="negative", timeout=5000)
                        finally:
                            _verify_state.running = False
                        render_verify_tab()

                    async def _on_post_check():
                        """Step 3: Post-Check — before/after を比較して副作用を検出する。"""
                        if _verify_state.running:
                            return
                        if not _verify_state.snap_id:
                            ui.notify(L("verify_post_need_snap"), type="warning", timeout=4000)
                            return
                        if not _verify_state.selected_cats:
                            ui.notify("カテゴリを1つ以上選択してください", type="warning")
                            return
                        _verify_state.running = True
                        ui.notify(L("verify_running"), timeout=2000)
                        try:
                            result = await anta_a2a_post({
                                "action":      "post_check",
                                "query":       "設定変更後の事後検証",
                                "snapshot_id": _verify_state.snap_id,
                                "tests":       _verify_state.selected_cats,
                                "device_ip":   DEVICE["ip"],
                                "username":    DEVICE["username"],
                                "password":    DEVICE["password"],
                                "locale":      LOCALE,
                            })
                            _verify_state.last_result = result
                            n_issues = len(result.get("new_issues", []))
                            if n_issues == 0:
                                ui.notify(L("verify_no_sideeffect"),
                                          type="positive", timeout=4000)
                            else:
                                ui.notify(f"⚠️ {n_issues} 件の副作用を検出",
                                          type="warning", timeout=6000)
                        except Exception as e:
                            ui.notify(f"❌ {e}", type="negative", timeout=5000)
                        finally:
                            _verify_state.running = False
                        render_verify_tab()

                    def render_verify_tab():   # noqa: F811
                        """ボタン接続込みの描画関数（上書き再定義）"""
                        verify_body.clear()
                        with verify_body:
                            # ── カテゴリ選択 ─────────────────────────────────
                            with ui.card().style(
                                f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                "border-radius:8px;padding:12px 14px;gap:8px;"
                                "box-shadow:none;width:100%;box-sizing:border-box;"
                            ):
                                with ui.row().style(
                                    "align-items:center;gap:8px;flex-wrap:wrap;"
                                ):
                                    ui.label(L("verify_cat_label")).style(
                                        f"font-size:11px;font-weight:600;"
                                        f"color:{C['text2']};flex-shrink:0;"
                                    )
                                    ui.label("").style("flex:1;")

                                    def _select_all_2():
                                        _verify_state.selected_cats = [
                                            k for _, k in VerifyState.ALL_CATS
                                        ]
                                        render_verify_tab()

                                    def _clear_all_2():
                                        _verify_state.selected_cats = []
                                        render_verify_tab()

                                    ui.button(L("verify_cat_all")).props(
                                        "flat dense"
                                    ).style(
                                        f"font-size:10px;color:{C['info_fg']};"
                                        f"border:0.5px solid {C['info_fg']}44;"
                                        "border-radius:4px;padding:1px 8px;"
                                    ).on("click", _select_all_2)
                                    ui.button(L("verify_cat_clear")).props(
                                        "flat dense"
                                    ).style(
                                        f"font-size:10px;color:{C['text3']};"
                                        f"border:0.5px solid {C['border']};"
                                        "border-radius:4px;padding:1px 8px;"
                                    ).on("click", _clear_all_2)

                                with ui.row().style(
                                    "align-items:center;gap:6px;flex-wrap:wrap;margin-top:4px;"
                                ):
                                    for _lbl, _key in VerifyState.ALL_CATS:
                                        _sel = _key in _verify_state.selected_cats
                                        _bg  = C["info_bg"]  if _sel else C["bg3"]
                                        _fg  = C["info_fg"]  if _sel else C["text3"]
                                        _bdr = C["info_fg"]  if _sel else C["border"]

                                        def _make_toggle(k):
                                            def _toggle():
                                                if k in _verify_state.selected_cats:
                                                    _verify_state.selected_cats.remove(k)
                                                else:
                                                    _verify_state.selected_cats.append(k)
                                                render_verify_tab()
                                            return _toggle

                                        ui.button(_lbl).props(
                                            "flat dense"
                                        ).style(
                                            f"font-size:11px;font-weight:500;"
                                            f"color:{_fg};background:{_bg};"
                                            f"border:0.5px solid {_bdr}44;"
                                            "border-radius:5px;padding:3px 10px;"
                                            "min-height:26px;"
                                        ).on("click", _make_toggle(_key))

                            # ── Step 1: Before Snapshot ───────────────────────
                            with ui.card().style(
                                f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                "border-radius:8px;padding:12px 14px;gap:6px;"
                                "box-shadow:none;width:100%;box-sizing:border-box;"
                            ):
                                with ui.row().style(
                                    "align-items:center;gap:10px;flex-wrap:wrap;"
                                ):
                                    ui.html(
                                        f'<div style="background:{C["bg4"]};color:{C["text3"]};'
                                        f'font-size:9px;padding:2px 7px;border-radius:4px;'
                                        f'font-weight:700;letter-spacing:.08em;">STEP 1</div>'
                                    )
                                    ui.button(
                                        L("verify_snap_btn")
                                    ).style(
                                        f"font-size:12px;font-weight:600;"
                                        f"background:{C['bg3']};color:{C['text']};"
                                        f"border:0.5px solid {C['border2']};"
                                        "border-radius:6px;padding:4px 14px;min-height:32px;"
                                    ).on("click", _on_snap)  # ← 直接接続
                                    ui.label(L("verify_snap_hint")).style(
                                        f"font-size:11px;color:{C['text3']};"
                                    )
                                if _verify_state.snap_id:
                                    with ui.row().style(
                                        f"align-items:center;gap:6px;margin-top:2px;"
                                        f"background:{C['success_bg']};"
                                        f"border:0.5px solid {C['success_fg']}33;"
                                        "border-radius:6px;padding:5px 10px;"
                                    ):
                                        ui.icon("check_circle").style(
                                            f"color:{C['success_fg']};font-size:14px;"
                                        )
                                        ui.label(L("verify_snap_taken")).style(
                                            f"font-size:11px;color:{C['success_fg']};"
                                            "font-weight:500;"
                                        )
                                        ui.label(_verify_state.snap_id).style(
                                            f"font-size:10px;color:{C['success_fg']}88;"
                                            f"font-family:{C['mono']};"
                                        )
                                        if _verify_state.snap_ts:
                                            ui.label(_verify_state.snap_ts).style(
                                                f"font-size:10px;color:{C['text3']};"
                                            )

                            # ── Step 2: Verify Now ────────────────────────────
                            with ui.card().style(
                                f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                "border-radius:8px;padding:12px 14px;gap:6px;"
                                "box-shadow:none;width:100%;box-sizing:border-box;"
                            ):
                                with ui.row().style(
                                    "align-items:center;gap:10px;flex-wrap:wrap;"
                                ):
                                    ui.html(
                                        f'<div style="background:{C["bg4"]};color:{C["text3"]};'
                                        f'font-size:9px;padding:2px 7px;border-radius:4px;'
                                        f'font-weight:700;letter-spacing:.08em;">STEP 2</div>'
                                    )
                                    ui.button(
                                        L("verify_now_btn")
                                    ).style(
                                        f"font-size:12px;font-weight:600;"
                                        f"background:{C['info_bg']};color:{C['info_fg']};"
                                        f"border:0.5px solid {C['info_fg']}66;"
                                        "border-radius:6px;padding:4px 14px;min-height:32px;"
                                    ).on("click", _on_verify_now)  # ← 直接接続
                                    ui.label(L("verify_now_hint")).style(
                                        f"font-size:11px;color:{C['text3']};"
                                    )

                            # ── Step 3: Post-Check ────────────────────────────
                            _snap_ready = bool(_verify_state.snap_id)
                            with ui.card().style(
                                f"background:{C['bg2']};"
                                f"border:0.5px solid "
                                + (f"{C['success_fg']}44" if _snap_ready
                                   else C['border2'])
                                + ";border-radius:8px;padding:12px 14px;gap:6px;"
                                "box-shadow:none;width:100%;box-sizing:border-box;"
                            ):
                                with ui.row().style(
                                    "align-items:center;gap:10px;flex-wrap:wrap;"
                                ):
                                    ui.html(
                                        f'<div style="background:{C["bg4"]};color:{C["text3"]};'
                                        f'font-size:9px;padding:2px 7px;border-radius:4px;'
                                        f'font-weight:700;letter-spacing:.08em;">STEP 3</div>'
                                    )
                                    ui.button(
                                        L("verify_post_btn")
                                    ).style(
                                        f"font-size:12px;font-weight:600;"
                                        f"background:{C['success_bg'] if _snap_ready else C['bg3']};"
                                        f"color:{C['success_fg'] if _snap_ready else C['text3']};"
                                        f"border:0.5px solid "
                                        + (f"{C['success_fg']}66" if _snap_ready
                                           else C['border'])
                                        + ";border-radius:6px;padding:4px 14px;min-height:32px;"
                                    ).on("click", _on_post_check)  # ← 直接接続
                                    ui.label(L("verify_post_hint")).style(
                                        f"font-size:11px;color:{C['text3']};"
                                    )
                                if not _snap_ready:
                                    ui.label(L("verify_post_need_snap")).style(
                                        f"font-size:10px;color:{C['warn_fg']};"
                                        "margin-top:2px;"
                                    )

                            # ── 結果エリア ────────────────────────────────────
                            res = _verify_state.last_result
                            if res:
                                action  = res.get("action",  "")
                                status  = res.get("status",  "")
                                summary = res.get("summary", "")

                                with ui.row().style(
                                    "align-items:center;gap:8px;flex-wrap:wrap;"
                                ):
                                    _sk = (
                                        "success" if status == "success"
                                        else "failure" if status in ("failure", "error")
                                        else "dry-run"
                                    )
                                    badge(status, _sk)
                                    ui.label(summary).style(
                                        f"font-size:13px;font-weight:500;color:{C['text']};"
                                    )

                                if status == "error":
                                    _msg  = res.get("message", "")
                                    _hint = res.get("hint", "")
                                    if _msg:
                                        ui.label(f"❌ {_msg}").style(
                                            f"font-size:11px;color:{C['danger_fg']};"
                                        )
                                    if _hint:
                                        ui.label(f"💡 {_hint}").style(
                                            f"font-size:10px;color:{C['warn_fg']};"
                                        )
                                else:
                                    # エンジン / snap_id
                                    with ui.row().style(
                                        "align-items:center;gap:8px;flex-wrap:wrap;"
                                    ):
                                        _engine = res.get("engine", "")
                                        if _engine:
                                            ui.label(
                                                f"{L('verify_engine')}: {_engine}"
                                            ).style(
                                                f"font-size:10px;color:{C['text3']};"
                                                f"font-family:{C['mono']};"
                                            )
                                        _sid = (res.get("snapshot_id", "")
                                                or res.get("after_snap_id", ""))
                                        if _sid:
                                            ui.label(
                                                f"{L('verify_snap_id')}: {_sid}"
                                            ).style(
                                                f"font-size:10px;color:{C['text3']};"
                                                f"font-family:{C['mono']};"
                                            )

                                    # Post-Check 副作用表示
                                    diff = res.get("diff", {}) or {}
                                    new_failures  = diff.get("new_failures",  [])
                                    resolved      = diff.get("resolved",      [])
                                    still_failing = diff.get("still_failing", [])
                                    new_issues    = res.get("new_issues", [])

                                    if action in ("post_check", "compare") or new_issues:
                                        _no_side = not new_failures and not new_issues
                                        with ui.card().style(
                                            f"background:"
                                            + (C['success_bg'] if _no_side else C['danger_bg'])
                                            + f";border:0.5px solid "
                                            + (f"{C['success_fg']}44" if _no_side
                                               else f"{C['danger_fg']}44")
                                            + ";border-radius:8px;padding:12px;"
                                            "gap:8px;box-shadow:none;width:100%;"
                                            "box-sizing:border-box;"
                                        ):
                                            ui.label(
                                                L("verify_no_sideeffect") if _no_side
                                                else f"⚠️ {L('verify_sideeffect')} ({len(new_failures)} 件)"
                                            ).style(
                                                f"font-size:12px;font-weight:600;color:"
                                                + (C['success_fg'] if _no_side
                                                   else C['danger_fg'])
                                                + ";"
                                            )
                                            if new_failures:
                                                ui.label(
                                                    f"【{L('verify_new_failures')}】"
                                                ).style(
                                                    f"font-size:10px;font-weight:600;"
                                                    f"color:{C['danger_fg']};margin-top:4px;"
                                                )
                                                for _fi in new_failures:
                                                    _msgs = " / ".join(
                                                        _fi.get("messages", [])
                                                    )[:80]
                                                    ui.html(
                                                        f'<div style="font-family:{C["mono"]};'
                                                        f'font-size:10px;color:{C["danger_fg"]};'
                                                        f'padding:2px 0;line-height:1.6;">'
                                                        f"⚠️ {_fi.get('test','')} "
                                                        f"({_fi.get('before_result','')} → "
                                                        f"{_fi.get('after_result','')})"
                                                        + (f"<br>　{_msgs}" if _msgs else "")
                                                        + "</div>"
                                                    )
                                            if resolved:
                                                ui.label(
                                                    f"【{L('verify_resolved')}】"
                                                ).style(
                                                    f"font-size:10px;font-weight:600;"
                                                    f"color:{C['success_fg']};margin-top:4px;"
                                                )
                                                for _ri in resolved:
                                                    ui.label(
                                                        f"✅ {_ri.get('test','')} "
                                                        f"({_ri.get('before_result','')} → "
                                                        f"{_ri.get('after_result','')})"
                                                    ).style(
                                                        f"font-size:10px;color:{C['success_fg']};"
                                                        f"font-family:{C['mono']};"
                                                    )
                                            if still_failing:
                                                with ui.expansion(
                                                    f"{L('verify_still_fail')} ({len(still_failing)})",
                                                    icon="warning_amber",
                                                ).props("dense dark").style(
                                                    f"background:{C['bg3']};border-radius:6px;"
                                                    f"color:{C['warn_fg']};font-size:10px;"
                                                    "margin-top:4px;"
                                                ):
                                                    for _sf in still_failing:
                                                        ui.label(
                                                            f"🔴 {_sf.get('test','')}"
                                                        ).style(
                                                            f"font-size:10px;"
                                                            f"color:{C['warn_fg']};"
                                                            f"font-family:{C['mono']};"
                                                        )

                                    # テスト結果一覧
                                    results = res.get("results", [])
                                    t_total   = res.get("tests_total",   len(results))
                                    t_passed  = res.get("tests_passed",  0)
                                    t_failed  = res.get("tests_failed",  0)
                                    t_skipped = res.get("tests_skipped", 0)

                                    if results:
                                        with ui.card().style(
                                            f"background:{C['bg2']};"
                                            f"border:0.5px solid {C['border2']};"
                                            "border-radius:8px;padding:12px 14px;gap:4px;"
                                            "box-shadow:none;width:100%;"
                                            "box-sizing:border-box;"
                                        ):
                                            with ui.row().style(
                                                "align-items:center;gap:8px;flex-wrap:wrap;"
                                                "padding-bottom:8px;"
                                                f"border-bottom:0.5px solid {C['border']};"
                                            ):
                                                ui.label(
                                                    L("verify_result_label")
                                                ).style(
                                                    f"font-size:11px;font-weight:600;"
                                                    f"color:{C['text2']};"
                                                )
                                                ui.label("").style("flex:1;")
                                                ui.label(
                                                    f"{L('verify_passed')}:{t_passed}  "
                                                    f"{L('verify_failed')}:{t_failed}  "
                                                    f"{L('verify_skipped')}:{t_skipped}"
                                                    f" / {t_total}"
                                                ).style(
                                                    f"font-size:11px;color:{C['text2']};"
                                                    f"font-family:{C['mono']};"
                                                )
                                            ICON_MAP = {
                                                "success": ("✅", C["success_fg"]),
                                                "failure": ("❌", C["danger_fg"]),
                                                "error":   ("🔴", C["danger_fg"]),
                                                "skipped": ("⏭️", C["text3"]),
                                            }
                                            for _t in results:
                                                _tr = _t.get("result", "")
                                                _ti, _tc = ICON_MAP.get(
                                                    _tr, ("❓", C["text3"]))
                                                _tm = " / ".join(
                                                    _t.get("messages", [])
                                                )[:80]
                                                with ui.row().style(
                                                    "align-items:flex-start;gap:6px;"
                                                    f"padding:4px 0;"
                                                    f"border-bottom:0.5px solid {C['bg3']};"
                                                ):
                                                    ui.label(_ti).style(
                                                        "font-size:12px;flex-shrink:0;"
                                                        "padding-top:1px;"
                                                    )
                                                    with ui.column().style("gap:0;"):
                                                        ui.label(
                                                            _t.get("test", "")
                                                        ).style(
                                                            f"font-size:11px;font-weight:500;"
                                                            f"color:{_tc};"
                                                            f"font-family:{C['mono']};"
                                                        )
                                                        if _tm:
                                                            ui.label(_tm).style(
                                                                f"font-size:10px;"
                                                                f"color:{C['text3']};"
                                                                "line-height:1.4;"
                                                            )

                    # 初回描画
                    render_verify_tab()

                    # タブ切り替え時に再描画
                    def _on_tab_change_verify(e):
                        if hasattr(e, "args") and "Verify" in str(e.args):
                            render_verify_tab()

                    tabs.on("update:modelValue",
                            lambda e: _on_tab_change_verify(e))

                # ── Security タブ ─────────────────────────────────────────────
                with ui.tab_panel(tab_security).style(
                    f"padding:0;height:100%;display:flex;flex-direction:column;"
                    f"width:100%;background:{C['bg']};"
                ):
                    # ヘッダー行
                    with ui.row().style(
                        f"padding:8px 16px;border-bottom:0.5px solid {C['border']};"
                        f"align-items:center;gap:10px;flex-shrink:0;background:{C['bg2']};"
                    ):
                        ui.icon("security").style(f"color:{C['danger_fg']};font-size:14px;")
                        ui.label("XDP Firewall Monitor").style(
                            f"font-size:12px;font-weight:600;color:{C['text']};"
                        )
                        ui.label("").style("flex:1;")
                        analyze_btn = ui.button("AI 解析", icon="psychology").props(
                            "flat dense"
                        ).style(
                            f"font-size:11px;color:{C['info_fg']};"
                            f"border:0.5px solid {C['border2']};border-radius:5px;"
                        )
                        sec_refresh_btn = ui.button("更新", icon="refresh").props(
                            "flat dense"
                        ).style(
                            f"font-size:11px;color:{C['text2']};"
                            f"border:0.5px solid {C['border2']};border-radius:5px;"
                        )

                    sec_body = ui.column().style(
                        "flex:1;overflow-y:auto;padding:10px 14px;"
                        "gap:10px;min-height:0;width:100%;"
                    )

                    def _action_badge(action: str):
                        COLOR_MAP = {
                            "Mitigated": (C["warn_bg"],    C["warn_fg"]),
                            "Restored":  (C["success_bg"], C["success_fg"]),
                            "XDP_DROP":  (C["danger_bg"],  C["danger_fg"]),
                            "QoS":       (C["warn_bg"],    C["warn_fg"]),
                        }
                        bg, fg = COLOR_MAP.get(action, (C["bg4"], C["text2"]))
                        ui.label(action).style(
                            f"background:{bg};color:{fg};font-size:9px;"
                            f"padding:2px 7px;border-radius:3px;font-weight:600;"
                            f"border:0.5px solid {fg}33;flex-shrink:0;"
                        )

                    def _kind_badge(kind: str):
                        KIND_COLOR = {
                            "SYNスパイク":      (C["danger_bg"],  C["danger_fg"]),
                            "ポートスキャン疑い": (C["warn_bg"],    C["warn_fg"]),
                            "異常フロー":        (C["warn_bg"],    C["warn_fg"]),
                            "DROP_LIST":        (C["info_bg"],    C["info_fg"]),
                        }
                        bg, fg = KIND_COLOR.get(kind, (C["bg4"], C["text2"]))
                        ui.label(kind).style(
                            f"background:{bg};color:{fg};font-size:9px;"
                            f"padding:2px 7px;border-radius:3px;font-weight:600;"
                            f"flex-shrink:0;white-space:nowrap;"
                        )

                    def render_security_tab():
                        sec_body.clear()
                        with sec_body:
                            ss = _sec_state

                            # ── KPI カード (4枚) ─────────────────────────────
                            pps_now  = ss.current_pps
                            pps_prev = ss.pps_history[-2] if len(ss.pps_history) >= 2 else 0
                            pps_diff = pps_now - pps_prev

                            with ui.row().style("gap:8px;width:100%;flex-wrap:nowrap;"):
                                def _kpi(title, value, sub, color=None):
                                    fg = color or C["info_fg"]
                                    with ui.card().style(
                                        f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                        "border-radius:8px;padding:10px 14px;flex:1;min-width:0;"
                                        "box-shadow:none;gap:2px;"
                                    ):
                                        ui.label(title).style(
                                            f"font-size:10px;color:{C['text3']};"
                                            "letter-spacing:.04em;"
                                        )
                                        ui.label(str(value)).style(
                                            f"font-size:22px;font-weight:700;color:{fg};"
                                            f"font-family:{C['mono']};line-height:1.2;"
                                        )
                                        ui.label(sub).style(
                                            f"font-size:9px;color:{C['text3']};"
                                        )

                                diff_str = (f"▲ +{pps_diff:,}" if pps_diff > 0
                                            else (f"▼ {pps_diff:,}" if pps_diff < 0 else "— 変化なし"))
                                _kpi("パケット / 秒",
                                     f"{pps_now:,}",
                                     f"{diff_str} vs 前回",
                                     C["info_fg"])
                                _kpi("XDP DROP 中",
                                     ss.active_blocks,
                                     "アクティブブロック",
                                     C["danger_fg"] if ss.active_blocks > 0 else C["text3"])
                                _kpi("SYN スパイク",
                                     ss.qos_mitigated,
                                     "QoS ミティゲーション中",
                                     C["warn_fg"] if ss.qos_mitigated > 0 else C["text3"])
                                _kpi("フロー総数",
                                     ss.unique_ips,
                                     "ユニーク IP",
                                     C["text2"])

                            # ── グラフ 2枚（インラインSVG） ──────────────────
                            with ui.row().style("gap:8px;width:100%;"):
                                def _sparkline(title: str, data: list, color: str,
                                               badge_label: str, badge_color: str,
                                               unit: str = "",
                                               time_labels: list = None):
                                    with ui.card().style(
                                        f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                        "border-radius:8px;padding:10px 14px;flex:1;min-width:0;"
                                        "box-shadow:none;gap:6px;"
                                    ):
                                        with ui.row().style("align-items:center;gap:6px;"):
                                            ui.label(title).style(
                                                f"font-size:11px;font-weight:500;color:{C['text2']};"
                                            )
                                            ui.label("").style("flex:1;")
                                            ui.label(badge_label).style(
                                                f"font-size:9px;color:{badge_color};"
                                                f"background:{C['bg4']};padding:1px 6px;"
                                                "border-radius:3px;"
                                            )
                                        # SVG: Y軸数値 + 横軸時刻ラベル付き
                                        W       = 340
                                        X_LEFT  = 46   # Y軸ラベル幅
                                        X_RIGHT = 4    # 右余白
                                        Y_TOP   = 12   # 上余白
                                        Y_MID   = 18   # 横軸ラベル用下余白
                                        H       = 74 + Y_MID
                                        PLOT_W  = W - X_LEFT - X_RIGHT
                                        PLOT_H  = H - Y_TOP - Y_MID

                                        pts  = data[-SecurityState.CHART_POINTS:] if data else [0]
                                        n    = len(pts)
                                        mx   = max(pts) or 1
                                        step = PLOT_W / max(n - 1, 1)

                                        # 横軸: 5分固定ウィンドウ・絶対時刻 60秒間隔
                                        tlabels = (time_labels or [])[-SecurityState.CHART_POINTS:]
                                        WINDOW_S = 300   # 5分固定
                                        TICK_S   = 60    # 60秒ごとに目盛り
                                        # 最新の time_label から現在時刻を得る
                                        import math as _math
                                        from datetime import datetime as _dt
                                        _now = _dt.now()
                                        _now_ep = _now.timestamp()
                                        # 各データ点の epoch を推定
                                        # tlabels[i] → HH:MM:SS → 当日の datetime
                                        _epochs = []
                                        for _lbl in tlabels:
                                            try:
                                                _t = _dt.strptime(_lbl, "%H:%M:%S").replace(
                                                    year=_now.year, month=_now.month,
                                                    day=_now.day)
                                                _epochs.append(_t.timestamp())
                                            except Exception:
                                                _epochs.append(None)
                                        # tlabels がない場合は等間隔で 3秒ずつ過去に割り当て
                                        if not _epochs:
                                            _epochs = [_now_ep - (n-1-i)*3 for i in range(n)]
                                        # 5分ウィンドウ開始 epoch
                                        _win_start = _now_ep - WINDOW_S
                                        # pts の X 座標を epoch ベースで計算
                                        def _ex(ep):
                                            if ep is None:
                                                return X_LEFT
                                            ratio = (ep - _win_start) / WINDOW_S
                                            return X_LEFT + ratio * PLOT_W
                                        # 60秒ごとの目盛り epoch リスト
                                        _first_tick = _math.ceil(_win_start / TICK_S) * TICK_S
                                        _tick_epochs = []
                                        _t_ep = _first_tick
                                        while _t_ep <= _now_ep:
                                            _tick_epochs.append(_t_ep)
                                            _t_ep += TICK_S

                                        def _fmt(v):
                                            if v >= 1_000_000:
                                                return f"{v/1_000_000:.1f}M"
                                            if v >= 1_000:
                                                return f"{v/1_000:.0f}K"
                                            return str(int(v))

                                        bot_y = Y_TOP + PLOT_H
                                        mid_y = Y_TOP + PLOT_H * 0.5

                                        # epoch ベースで座標計算
                                        _pt_coords = []
                                        for _i, _v in enumerate(pts):
                                            _ep = _epochs[_i] if _i < len(_epochs) else None
                                            _px = _ex(_ep)
                                            _py = Y_TOP + PLOT_H - (_v/mx)*PLOT_H*0.9
                                            _pt_coords.append((_px, _py, _v))
                                        coords = " ".join(
                                            f"{_px:.1f},{_py:.1f}"
                                            for _px, _py, _ in _pt_coords
                                        )
                                        last_x   = _pt_coords[-1][0] if _pt_coords else X_LEFT + PLOT_W
                                        first_x  = _pt_coords[0][0]  if _pt_coords else X_LEFT
                                        fill_pts = (
                                            f"{first_x:.1f},{bot_y} "
                                            + coords
                                            + f" {last_x:.1f},{bot_y}"
                                        )
                                        gid     = abs(hash(title)) % 99999
                                        cur_val = pts[-1] if pts else 0
                                        cur_x   = last_x
                                        cur_y   = Y_TOP + PLOT_H - (cur_val / mx) * PLOT_H * 0.9
                                        unit_str = f" {unit}" if unit else ""

                                        # 横軸ティック & ラベル SVG 断片（絶対時刻 60秒間隔）
                                        x_tick_svg = ""
                                        for _tep in _tick_epochs:
                                            _tx = _ex(_tep)
                                            if _tx < X_LEFT or _tx > X_LEFT + PLOT_W:
                                                continue
                                            _lbl = _dt.fromtimestamp(_tep).strftime("%H:%M:%S")
                                            _anchor = "middle"
                                            if _tx < X_LEFT + 20:
                                                _anchor = "start"
                                            elif _tx > X_LEFT + PLOT_W - 20:
                                                _anchor = "end"
                                            x_tick_svg += (
                                                f'<line x1="{_tx:.1f}" y1="{bot_y}" '
                                                f'x2="{_tx:.1f}" y2="{bot_y+3}" '
                                                f'stroke="#ffffff20" stroke-width="0.5"/>'
                                                f'<text x="{_tx:.1f}" y="{bot_y+10}" '
                                                f'text-anchor="{_anchor}" font-size="7" fill="#4a4a4a">'
                                                f'{_lbl}</text>'
                                            )

                                        svg = (
                                            f'<svg viewBox="0 0 {W} {H}" '
                                            f'xmlns="http://www.w3.org/2000/svg" '
                                            f'style="width:100%;height:{H+10}px;">'
                                            # グラデ
                                            f'<defs><linearGradient id="g{gid}" '
                                            f'x1="0" y1="0" x2="0" y2="1">'
                                            f'<stop offset="0%" stop-color="{color}" stop-opacity="0.30"/>'
                                            f'<stop offset="100%" stop-color="{color}" stop-opacity="0.02"/>'
                                            f'</linearGradient></defs>'
                                            # Y軸グリッド
                                            f'<line x1="{X_LEFT}" y1="{Y_TOP}" x2="{W}" y2="{Y_TOP}" '
                                            f'stroke="#ffffff10" stroke-width="0.5"/>'
                                            f'<line x1="{X_LEFT}" y1="{mid_y:.1f}" x2="{W}" y2="{mid_y:.1f}" '
                                            f'stroke="#ffffff10" stroke-width="0.5"/>'
                                            f'<line x1="{X_LEFT}" y1="{bot_y}" x2="{W}" y2="{bot_y}" '
                                            f'stroke="#ffffff18" stroke-width="0.5"/>'
                                            # Y軸ラベル
                                            f'<text x="{X_LEFT-3}" y="{Y_TOP+3}" '
                                            f'text-anchor="end" font-size="8" fill="#5a5a5a">'
                                            f'{_fmt(mx)}{unit_str}</text>'
                                            f'<text x="{X_LEFT-3}" y="{mid_y+3:.1f}" '
                                            f'text-anchor="end" font-size="8" fill="#5a5a5a">'
                                            f'{_fmt(mx/2)}{unit_str}</text>'
                                            f'<text x="{X_LEFT-3}" y="{bot_y:.1f}" '
                                            f'text-anchor="end" font-size="8" fill="#5a5a5a">0</text>'
                                            # 横軸ティック & 時刻ラベル
                                            + x_tick_svg +
                                            # 塗り & 折れ線
                                            f'<polygon points="{fill_pts}" fill="url(#g{gid})"/>'
                                            f'<polyline points="{coords}" fill="none" '
                                            f'stroke="{color}" stroke-width="1.5" stroke-linejoin="round"/>'
                                            # 現在値ドット & ラベル
                                            f'<circle cx="{cur_x:.1f}" cy="{cur_y:.1f}" r="3" fill="{color}"/>'
                                            f'<text x="{min(cur_x+5, W-4):.1f}" '
                                            f'y="{max(cur_y-3, Y_TOP+6):.1f}" '
                                            f'font-size="9" fill="{color}" font-weight="bold">'
                                            f'{_fmt(cur_val)}{unit_str}</text>'
                                            f'</svg>'
                                        )
                                        ui.html(svg)

                                _sparkline("トラフィック (pps)",
                                           ss.pps_history, C["info_fg"],
                                           "3 sec 更新", C["info_fg"],
                                           unit="pps",
                                           time_labels=ss.time_labels)
                                _sparkline("SYN delta 推移",
                                           ss.syn_history, C["warn_fg"],
                                           "監視中", C["warn_fg"],
                                           unit="pkts",
                                           time_labels=ss.time_labels)
                                _sparkline("総 DROP 数",
                                           ss.drop_history, C["danger_fg"],
                                           "累積", C["danger_fg"],
                                           unit="pkts",
                                           time_labels=ss.time_labels)

                            # ── フロー統計テーブル（ui.html で一括生成）────────
                            with ui.card().style(
                                f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                "border-radius:8px;padding:10px 14px;box-shadow:none;width:100%;"
                            ):
                                # ヘッダー行（タイトル＋タイムスタンプ）
                                with ui.row().style("align-items:center;gap:6px;margin-bottom:8px;"):
                                    ui.label("RAW FLOW STATISTICS").style(
                                        f"font-size:11px;font-weight:600;color:{C['text']};"
                                    )
                                    ui.label("").style("flex:1;")
                                    ui.label(ss.stats_ts or "—").style(
                                        f"font-size:9px;color:{C['text3']};"
                                        f"background:{C['bg3']};padding:1px 6px;"
                                        f"border-radius:3px;font-family:{C['mono']};"
                                    )

                                # テーブルを ui.html で一括生成
                                _mono  = C["mono"]
                                _t2    = C["text2"]
                                _t3    = C["text3"]
                                _txt   = C["text"]
                                _dfg   = C["danger_fg"]
                                _dbg   = C["danger_bg"]
                                _bdr   = C["border"]

                                _th_s  = (f"font-size:9px;font-weight:600;color:{_t3};"
                                          f"font-family:{_mono};padding:3px 6px;"
                                          f"border-bottom:1px solid {_bdr};text-align:left;"
                                          "white-space:nowrap;")
                                _thr_s = (f"font-size:9px;font-weight:600;color:{_t3};"
                                          f"font-family:{_mono};padding:3px 6px;"
                                          f"border-bottom:1px solid {_bdr};text-align:right;"
                                          "white-space:nowrap;")
                                _td_s  = (f"font-size:10px;color:{_t2};"
                                          f"font-family:{_mono};padding:3px 6px;"
                                          "white-space:nowrap;")
                                _tdr_s = (f"font-size:10px;color:{_txt};"
                                          f"font-family:{_mono};padding:3px 6px;"
                                          "text-align:right;white-space:nowrap;")
                                _drop_s= (f"font-size:10px;color:{_dfg};font-weight:600;"
                                          f"font-family:{_mono};padding:3px 6px;"
                                          "text-align:right;white-space:nowrap;")

                                _rows_html = ""
                                if ss.top_stats:
                                    for _flow in ss.top_stats:
                                        _st      = _flow.get("stats", {})
                                        _ip      = _flow.get("ip", "—")
                                        _prot    = _flow.get("protocol", "—")
                                        _port    = _flow.get("port", 0)
                                        _pkts    = _st.get("packets", 0)
                                        _drop    = _st.get("dropped_packets", 0)
                                        _syn     = _st.get("syn_packets", 0)
                                        _rst     = _st.get("rst_packets", 0)
                                        _ack     = _st.get("ack_packets", 0)
                                        _pmin    = _st.get("pkt_min", 0)
                                        _pmax    = _st.get("pkt_max", 0)
                                        _is_drop = _drop > 0
                                        _row_bg  = f"background:{_dbg}22;" if _is_drop else ""
                                        _d_style = _drop_s if _is_drop else _tdr_s
                                        _rows_html += (
                                            f'<tr style="border-bottom:1px solid {_bdr}22;{_row_bg}">'
                                            f'<td style="{_td_s}">{_ip}</td>'
                                            f'<td style="{_td_s}">{_prot}</td>'
                                            f'<td style="{_tdr_s}">{_port}</td>'
                                            f'<td style="{_tdr_s}">{_pkts:,}</td>'
                                            f'<td style="{_d_style}">{_drop:,}</td>'
                                            f'<td style="{_tdr_s}">{_syn:,}</td>'
                                            f'<td style="{_tdr_s}">{_rst:,}</td>'
                                            f'<td style="{_tdr_s}">{_ack:,}</td>'
                                            f'<td style="{_tdr_s}">{_pmin}/{_pmax}</td>'
                                            f'</tr>'
                                        )
                                else:
                                    _rows_html = (
                                        f'<tr><td colspan="9" style="'
                                        f'font-size:10px;color:{_t3};text-align:center;padding:10px;">'
                                        f'データなし</td></tr>'
                                    )

                                ui.html(
                                    f'<table style="width:100%;border-collapse:collapse;">'
                                    f'<thead><tr>'
                                    f'<th style="{_th_s}">IP ADDRESS</th>'
                                    f'<th style="{_th_s}">PROT</th>'
                                    f'<th style="{_thr_s}">PORT</th>'
                                    f'<th style="{_thr_s}">PKTS</th>'
                                    f'<th style="{_thr_s}">DROP</th>'
                                    f'<th style="{_thr_s}">SYN</th>'
                                    f'<th style="{_thr_s}">RST</th>'
                                    f'<th style="{_thr_s}">ACK</th>'
                                    f'<th style="{_thr_s}">MIN/MAX</th>'
                                    f'</tr></thead>'
                                    f'<tbody>{_rows_html}</tbody>'
                                    f'</table>'
                                )

                            # ── 脅威検知ログ ─────────────────────────────────
                            with ui.card().style(
                                f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                "border-radius:8px;padding:10px 14px;box-shadow:none;width:100%;"
                            ):
                                with ui.row().style(
                                    "align-items:center;gap:6px;margin-bottom:8px;"
                                ):
                                    ui.html(
                                        f'<span style="width:8px;height:8px;border-radius:50%;'
                                        f'background:{C["danger_fg"]};display:inline-block;'
                                        f'animation:pulse 1.5s infinite;"></span>'
                                        f'<style>@keyframes pulse{{0%,100%{{opacity:1}}'
                                        f'50%{{opacity:.3}}}}</style>'
                                    )
                                    ui.label("脅威検知ログ").style(
                                        f"font-size:11px;font-weight:600;color:{C['text']};"
                                    )
                                    ui.label("").style("flex:1;")
                                    ui.label(f"最新 {SecurityState.MAX_HISTORY} 件").style(
                                        f"font-size:9px;color:{C['text3']};"
                                        f"background:{C['bg3']};padding:1px 6px;"
                                        "border-radius:3px;"
                                    )

                                if not ss.threat_log:
                                    ui.label("脅威は検知されていません").style(
                                        f"font-size:11px;color:{C['text3']};"
                                        "text-align:center;padding:16px 0;width:100%;"
                                    )
                                else:
                                    for entry in reversed(ss.threat_log):
                                        with ui.row().style(
                                            f"align-items:center;gap:8px;padding:6px 4px;"
                                            f"border-bottom:0.5px solid {C['border']};"
                                            "flex-wrap:nowrap;"
                                        ):
                                            ui.label(entry["time"]).style(
                                                f"font-size:10px;color:{C['text3']};"
                                                f"font-family:{C['mono']};min-width:54px;"
                                            )
                                            ui.label(entry["ip"]).style(
                                                f"font-size:11px;color:{C['danger_fg']};"
                                                f"font-family:{C['mono']};min-width:90px;"
                                            )
                                            _kind_badge(entry["kind"])
                                            ui.label(entry["detail"]).style(
                                                f"font-size:10px;color:{C['text2']};flex:1;"
                                                "overflow:hidden;text-overflow:ellipsis;"
                                                "white-space:nowrap;"
                                            )
                                            _action_badge(entry["action"])

                            # ── 防御ステータス & QoS ─────────────────────────
                            with ui.row().style("gap:8px;width:100%;"):
                                # DROP_LIST
                                with ui.card().style(
                                    f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                    "border-radius:8px;padding:10px 14px;flex:1;min-width:0;"
                                    "box-shadow:none;"
                                ):
                                    with ui.row().style(
                                        "align-items:center;gap:6px;margin-bottom:8px;"
                                    ):
                                        ui.label("防御ステータス (DROP_LIST)").style(
                                            f"font-size:11px;font-weight:600;color:{C['text']};"
                                        )
                                        ui.label("").style("flex:1;")
                                        _col = C["danger_fg"] if ss.drop_list else C["text3"]
                                        _lbl = "XDP_DROP 中" if ss.drop_list else "クリア"
                                        ui.label(_lbl).style(
                                            f"font-size:9px;color:{_col};"
                                            f"background:{C['danger_bg'] if ss.drop_list else C['bg4']};"
                                            "padding:1px 6px;border-radius:3px;font-weight:600;"
                                        )
                                    if not ss.drop_list:
                                        ui.label("ブロックルールなし").style(
                                            f"font-size:10px;color:{C['text3']};"
                                        )
                                    else:
                                        # ヘッダ
                                        with ui.row().style(
                                            f"padding:2px 0;border-bottom:0.5px solid {C['border']};"
                                            "gap:8px;"
                                        ):
                                            for h in ["対象 IP", "Proto", "Port"]:
                                                ui.label(h).style(
                                                    f"font-size:9px;color:{C['text3']};"
                                                    "letter-spacing:.04em;font-weight:500;"
                                                    "min-width:70px;"
                                                )
                                        for key in list(ss.drop_list.keys())[:6]:
                                            # "10.0.2.55:22 [tcp]" を分解
                                            import re as _re
                                            m = _re.match(
                                                r"([\d.]+):(\d+) \[(\w+)\]", key)
                                            if m:
                                                _ip, _port, _proto = (
                                                    m.group(1), m.group(2), m.group(3))
                                            else:
                                                _ip, _port, _proto = key, "-", "-"
                                            with ui.row().style(
                                                "gap:8px;padding:3px 0;"
                                                f"border-bottom:0.5px solid {C['border']};"
                                            ):
                                                ui.label(_ip).style(
                                                    f"font-size:10px;color:{C['danger_fg']};"
                                                    f"font-family:{C['mono']};min-width:70px;"
                                                )
                                                ui.label(_proto).style(
                                                    f"font-size:10px;color:{C['info_fg']};"
                                                    "min-width:70px;"
                                                )
                                                ui.label(_port).style(
                                                    f"font-size:10px;color:{C['text2']};"
                                                    "min-width:70px;"
                                                )

                                # QOS_MAP
                                with ui.card().style(
                                    f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                    "border-radius:8px;padding:10px 14px;flex:1;min-width:0;"
                                    "box-shadow:none;"
                                ):
                                    with ui.row().style(
                                        "align-items:center;gap:6px;margin-bottom:8px;"
                                    ):
                                        ui.label("QoS ポリシー (QOS_MAP)").style(
                                            f"font-size:11px;font-weight:600;color:{C['text']};"
                                        )
                                        ui.label("").style("flex:1;")
                                        _ql = ss.qos_list
                                        _qlbl = "レート制限中" if _ql else "クリア"
                                        _qcol = C["warn_fg"] if _ql else C["text3"]
                                        ui.label(_qlbl).style(
                                            f"font-size:9px;color:{_qcol};"
                                            f"background:{C['warn_bg'] if _ql else C['bg4']};"
                                            "padding:1px 6px;border-radius:3px;font-weight:600;"
                                        )
                                    if not ss.qos_list:
                                        ui.label("QoSポリシーなし").style(
                                            f"font-size:10px;color:{C['text3']};"
                                        )
                                    else:
                                        with ui.row().style(
                                            f"padding:2px 0;border-bottom:0.5px solid {C['border']};"
                                            "gap:4px;"
                                        ):
                                            for h in ["対象 IP", "上限", "残りトークン"]:
                                                ui.label(h).style(
                                                    f"font-size:9px;color:{C['text3']};"
                                                    "letter-spacing:.04em;font-weight:500;"
                                                    "min-width:80px;"
                                                )
                                        for _qip, _qv in list(ss.qos_list.items())[:6]:
                                            _lim = _qv.get("limit_bytes_per_sec", 0)
                                            _tok = _qv.get("tokens", 0)
                                            _lim_str = (f"{_lim//1000}KB/s"
                                                        if _lim >= 1000 else f"{_lim}B/s")
                                            _tok_pct = int(_tok / 10_000_000 * 100)
                                            with ui.row().style(
                                                "gap:4px;padding:3px 0;"
                                                f"border-bottom:0.5px solid {C['border']};"
                                            ):
                                                ui.label(_qip).style(
                                                    f"font-size:10px;color:{C['warn_fg']};"
                                                    f"font-family:{C['mono']};min-width:80px;"
                                                )
                                                ui.label(_lim_str).style(
                                                    f"font-size:10px;color:{C['warn_fg']};"
                                                    "min-width:80px;"
                                                )
                                                ui.label(f"{_tok_pct}%").style(
                                                    f"font-size:10px;color:{C['text2']};"
                                                    "min-width:80px;"
                                                )

                            # ── AI エージェントの判断根拠 ─────────────────────
                            with ui.card().style(
                                f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                "border-radius:8px;padding:10px 14px;box-shadow:none;width:100%;"
                            ):
                                with ui.row().style(
                                    "align-items:center;gap:6px;margin-bottom:8px;"
                                ):
                                    ui.label("AI エージェントの判断根拠").style(
                                        f"font-size:11px;font-weight:600;color:{C['text']};"
                                    )
                                    ui.label("").style("flex:1;")
                                    _ev_ip = ss.last_event_ip
                                    if _ev_ip:
                                        ui.label(f"最新イベント: {_ev_ip}").style(
                                            f"font-size:9px;color:{C['warn_fg']};"
                                            f"background:{C['warn_bg']};padding:1px 6px;"
                                            "border-radius:3px;"
                                        )

                                if not ss.analysis:
                                    ui.label(
                                        "「AI 解析」ボタンを押すと LLM が脅威を分析します。"
                                    ).style(
                                        f"font-size:11px;color:{C['text3']};"
                                        "text-align:center;padding:16px 0;width:100%;"
                                    )
                                else:
                                    # 解析テキストを行ごとにナンバリング表示
                                    lines = [l for l in ss.analysis.splitlines() if l.strip()]
                                    for i, line in enumerate(lines[:20], 1):
                                        # [EXEC: ...] 行はスキップ（exec_tags で別表示）
                                        if "[EXEC:" in line:
                                            continue
                                        with ui.row().style(
                                            "gap:8px;padding:5px 4px;align-items:flex-start;"
                                            f"border-bottom:0.5px solid {C['border']};"
                                        ):
                                            ui.label(str(i)).style(
                                                f"font-size:10px;color:{C['info_fg']};"
                                                f"background:{C['info_bg']};padding:1px 6px;"
                                                "border-radius:3px;font-weight:600;"
                                                "min-width:22px;text-align:center;"
                                            )
                                            ui.label(line.strip()).style(
                                                f"font-size:11px;color:{C['text2']};flex:1;"
                                            )


                    render_security_tab()

                    # ── Sticky 提案アクションフッター ─────────────────────
                    sticky_footer = ui.element("div").style(
                        f"position:sticky;bottom:0;left:0;right:0;z-index:100;"
                        f"background:{C['bg2']}dd;backdrop-filter:blur(8px);"
                        f"border-top:1px solid {C['danger_fg']}44;"
                        "padding:8px 14px;display:none;"
                    )

                    def render_sticky_footer():
                        sticky_footer.clear()
                        ss = _sec_state
                        if not ss.exec_tags:
                            sticky_footer.style(add="display:none;")
                            sticky_footer.style(remove="display:flex;")
                            return
                        sticky_footer.style(remove="display:none;")
                        sticky_footer.style(add="display:flex;")
                        with sticky_footer:
                            ui.label("⚠️ 提案アクション（人間確認後に実行）").style(
                                f"font-size:10px;color:{C['warn_fg']};font-weight:600;"
                                "margin-right:12px;flex-shrink:0;align-self:center;"
                            )
                            def _make_exec_handler_sticky(tag_path, tag_params):
                                async def _exec_action():
                                    _qs = "&".join(
                                        f"{k}={v}" for k, v in tag_params.items())
                                    _label = f"{tag_path}?{_qs}"
                                    with ui.dialog() as dlg, ui.card().style(
                                        f"background:{C['bg2']};"
                                        f"border:1px solid {C['danger_fg']}44;"
                                        "border-radius:10px;padding:20px 24px;"
                                        "min-width:340px;gap:12px;"
                                    ):
                                        ui.label("⚠️ 実行確認").style(
                                            f"font-size:14px;font-weight:700;"
                                            f"color:{C['danger_fg']};"
                                        )
                                        ui.label(
                                            "以下のコマンドを XDP Firewall に送信します。"
                                        ).style(
                                            f"font-size:12px;color:{C['text2']};"
                                        )
                                        ui.label(_label).style(
                                            f"font-size:11px;color:{C['danger_fg']};"  
                                            f"font-family:{C['mono']};padding:6px 10px;"
                                            f"background:{C['danger_bg']};"  
                                            "border-radius:5px;word-break:break-all;"
                                        )
                                        ui.label("この操作は即時反映されます。").style(
                                            f"font-size:11px;color:{C['warn_fg']};"
                                        )
                                        with ui.row().style(
                                            "gap:8px;justify-content:flex-end;width:100%;"
                                        ):
                                            ui.button("キャンセル").props("flat dense").style(
                                                f"font-size:12px;color:{C['text2']};"
                                            ).on("click", dlg.close)
                                            def _make_do_exec_s(p, pr, d):
                                                async def _do():
                                                    d.close()
                                                    try:
                                                        # Hub /execute (deploy=False) → plan
                                                        _q = f"{p}?" + "&".join(
                                                            f"{k}={v}" for k, v in pr.items()
                                                        )
                                                        _ex_raw = await api_post(
                                                            "/execute",
                                                            {**_exec_payload(_q),
                                                             "deploy": False},
                                                        )
                                                        _tid = _ex_raw.get("trace_id", "")
                                                        if not _tid:
                                                            raise ValueError(
                                                                f"trace_id が取得できません: {_ex_raw}"
                                                            )
                                                        # Hub /deploy (deploy=True) → 実行
                                                        _dep_raw = await api_post(
                                                            f"/deploy/{_tid}",
                                                            _deploy_payload(),
                                                        )
                                                        _dep_status = _dep_raw.get(
                                                            "status", "unknown"
                                                        )
                                                        ui.notify(
                                                            f"✅ 実行完了: {p} ({_dep_status})",
                                                            type="positive", timeout=3000
                                                        )
                                                        _sec_state.exec_tags = []
                                                        render_security_tab()
                                                        render_sticky_footer()
                                                    except Exception as ex:
                                                        ui.notify(
                                                            f"❌ エラー: {ex}",
                                                            type="negative", timeout=5000
                                                        )
                                                return _do
                                            ui.button(
                                                "実行する", icon="play_arrow"
                                            ).style(
                                                f"font-size:12px;font-weight:600;"
                                                f"background:{C['danger_bg']};"
                                                f"color:{C['danger_fg']};"
                                                f"border:1px solid {C['danger_fg']}66;"
                                                "border-radius:5px;padding:4px 14px;"
                                            ).on("click",
                                                 _make_do_exec_s(tag_path, tag_params, dlg))
                                    dlg.open()
                                return _exec_action

                            for tag in ss.exec_tags:
                                _t_path   = tag.get("path", "")
                                _t_params = tag.get("params", {})
                                _t_qs     = "&".join(
                                    f"{k}={v}" for k, v in _t_params.items())
                                with ui.element("div").style(
                                    "display:flex;align-items:center;gap:8px;margin-right:8px;"
                                ):
                                    ui.label(f"{_t_path}?{_t_qs}").style(
                                        f"font-size:10px;color:{C['danger_fg']};"
                                        f"font-family:{C['mono']};padding:3px 7px;"
                                        f"background:{C['danger_bg']};border-radius:4px;"
                                    )
                                    ui.button("実行", icon="play_arrow").props(
                                        "flat dense"
                                    ).style(
                                        f"font-size:10px;color:{C['danger_fg']};"
                                        f"border:0.5px solid {C['danger_fg']}66;"
                                        "border-radius:4px;padding:1px 8px;flex-shrink:0;"
                                    ).on("click",
                                         _make_exec_handler_sticky(_t_path, _t_params))

                    render_sticky_footer()

                    # ── AI 解析ボタン ────────────────────────────────────────
                    async def _on_analyze():
                        analyze_btn.props("loading")
                        _sec_state.analysis  = "解析中..."
                        _sec_state.exec_tags = []
                        render_security_tab()
                        try:
                            # task_decompose (8000) /execute 経由で analyze を実行
                            raw = await api_post(
                                "/execute",
                                _exec_payload("状況を分析してセキュリティ脅威の提案をして"),
                            )
                            # /execute → security route → xdp_result がネストされて返る
                            # response["analysis"] / response["exec_tags"] はトップレベルに引き上げ済み
                            analysis  = raw.get("analysis", "")
                            exec_tags = raw.get("exec_tags", [])
                            # フォールバック: result["analysis"] も確認
                            if not analysis:
                                _inner = raw.get("result", {}) or {}
                                analysis  = _inner.get("analysis", "")
                                exec_tags = exec_tags or _inner.get("exec_tags", [])

                            if not analysis:
                                message = raw.get("message", "") or (raw.get("result") or {}).get("message", "")
                                analysis = f"[エラー] {message}" if message else f"[不明] {str(raw)[:300]}"

                            _sec_state.analysis  = analysis
                            # UI 側でも icmp/udp への block 提案を除外
                            _sec_state.exec_tags = [
                                t for t in exec_tags
                                if not (t.get("path") == "/drop/block" and
                                        t.get("params", {}).get("proto")
                                        in ("icmp", "udp"))
                            ]
                        except Exception as e:
                            _sec_state.analysis  = f"[例外] {type(e).__name__}: {e}"
                            _sec_state.exec_tags = []
                        finally:
                            analyze_btn.props(remove="loading")
                        render_security_tab()
                        render_sticky_footer()

                    analyze_btn.on("click", _on_analyze)

                    # ── 更新ボタン ───────────────────────────────────────────
                    async def _on_sec_refresh():
                        sec_refresh_btn.props("loading")
                        try:
                            top  = await xdp_get("/top")
                            drl  = await xdp_get("/drop/list")
                            qosl = await xdp_get("/qos/list")
                            _sec_state.top_stats = top  if isinstance(top,  list) else []
                            _sec_state.drop_list = drl  if isinstance(drl,  dict) else {}
                            _sec_state.qos_list  = qosl if isinstance(qosl, dict) else {}
                        except Exception:
                            pass
                        finally:
                            sec_refresh_btn.props(remove="loading")
                        render_security_tab()
                        render_sticky_footer()

                    sec_refresh_btn.on("click", _on_sec_refresh)

                    # ── タブに切り替えたときポーリング開始 ────────────────────
                    async def _start_polling():
                        if not _sec_state.polling:
                            _sec_state.polling = True
                            asyncio.create_task(_poll_security())

                    def on_tab_change_security(e):
                        if hasattr(e, "args") and "Security" in str(e.args):
                            asyncio.create_task(_start_polling())
                            render_security_tab()

                    tabs.on("update:modelValue", lambda e: on_tab_change_security(e))



# ── /healthz エンドポイント（認証不要、Azure 死活監視用） ─────────────────────
from nicegui import app as _nicegui_app


@_nicegui_app.get("/healthz")
async def healthz():
    return {"status": "ok", "service": "network-agent-a2a", "locale": LOCALE}


# ── ミドルウェア登録 ───────────────────────────────────────────────────────────
_nicegui_app.add_middleware(BasicAuthMiddleware)

ui.run(
    title="Network Agent",
    host="0.0.0.0",
    port=int(os.getenv("PORT", "8088")),  # Go IPS が 8080 を使用するため 8088
    reload=False,
    dark=True,
    favicon="🌐",
)
