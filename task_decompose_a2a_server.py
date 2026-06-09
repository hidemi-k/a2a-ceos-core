#!/usr/bin/env python3
# Copyright (c) 2026 hidemi-k
# Licensed under the MIT License.
"""
task_decompose A2A Hub Server — A2A + FastAPI 統合版 (port:8000)
====================================================================
A2A エンドポイント（エージェント間通信）:
  POST /              JSON-RPC message/send
  GET  /.well-known/agent.json  Agent Card

FastAPI エンドポイント（NiceGUI UI 向け）:
  GET  /healthz              死活監視
  POST /execute              Dry-run（A2A Hub 経由、deploy=False）
  POST /deploy/{trace_id}    実機投入（A2A Hub 経由、deploy=True）
  GET  /diff/{trace_id}      差分・結果取得
  WS   /ws/updates           ログストリーミング

起動:
  python task_decompose_a2a_server.py

環境変数:
  A2A_PORT       : このサーバのポート（デフォルト: 8000）
  NETCONF_A2A_URL: NETCONF サーバURL（デフォルト: http://localhost:8001）
  EAPI_A2A_URL   : eAPI サーバURL（デフォルト: http://localhost:8002）
  HTTP_TIMEOUT   : 転送タイムアウト秒（デフォルト: 120）
"""

import asyncio, json, logging, os, re, sys, uuid, configparser
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
import uvicorn
# ── 多言語対応 ────────────────────────────────────────────────────────────
from i18n import get_msg, locale_from_request, LOCALE

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from starlette.routing import Route, WebSocketRoute

# A2A SDK
from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue_v2 import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.routes.rest_routes import create_rest_routes
from a2a.types import (
    AgentCard, AgentCapabilities, AgentSkill,
)
from a2a.utils.errors import UnsupportedOperationError

# LangChain
from langchain_openai import ChatOpenAI

# ── LLM ファクトリ（Groq Primary / Azure Fallback 共通モジュール） ─────────────
from llm_factory import (
    build_llm, build_llm_with_fallback,
    invoke_with_fallback, log_llm_config,
    LLM_PROVIDER_NAME,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("task_decompose_hub_v2")

# ── 設定 ──────────────────────────────────────────────────────────────────────
VERSION    = "1.4.0"
BUILD_DATE = "2026-05-16"

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.getenv("SASE_CONFIG",
                         os.path.join(BASE_DIR, "./config.ini"))
GROQ_BASE_URL  = "https://api.groq.com/openai/v1"   # フォールバックログ用に残す
DEFAULT_MODEL  = "llama-3.3-70b-versatile"

A2A_HOST       = os.getenv("A2A_HOST",       "0.0.0.0")
A2A_PORT       = int(os.getenv("A2A_PORT",   "8000"))
A2A_PUBLIC_URL = os.getenv("A2A_PUBLIC_URL", f"http://localhost:{A2A_PORT}")

NETCONF_A2A_URL    = os.getenv("NETCONF_A2A_URL",    "http://localhost:8001")
EAPI_A2A_URL       = os.getenv("EAPI_A2A_URL",       "http://localhost:8002")
EAPI_SDIFF_URL     = os.getenv("EAPI_SDIFF_URL",     "http://localhost:8009")  # session-diff REST
XDP_A2A_URL        = os.getenv("XDP_A2A_URL",        "http://localhost:8003")
ANTA_A2A_URL       = os.getenv("ANTA_A2A_URL",       "http://localhost:8004")  # ANTA Snapshot 検証
EAPI_CONFIG_A2A_URL = os.getenv("EAPI_CONFIG_A2A_URL", "http://localhost:8006")  # eAPI configure session
HTTP_TIMEOUT       = float(os.getenv("HTTP_TIMEOUT", "120"))


# LLM インスタンス（Groq Primary / Azure Fallback 自動切り替え）
# classify_query() 等で llm.invoke() の代わりに invoke_with_fallback(llm, prompt) を使う
llm = build_llm_with_fallback()

# ── トレース結果ストア ────────────────────────────────────────────────────────
_trace_store: Dict[str, Dict] = {}
_ws_clients:  Dict[str, list] = {}


# ═══════════════════════════════════════════════════════════════════════════════
# クエリ分類（ルーター）
# ═══════════════════════════════════════════════════════════════════════════════

READ_KEYWORDS = [
    "確認","表示","一覧","状態","参照","見せ","教え","show","get",
    "list","check","status","display","バージョン","version",
    "見たい","知りたい","調べたい","確かめたい",  # ★ 追加(2026-05-20): 参照意図の口語表現
    "acl",   # ★ 追加(2026-05-20): show ip access-lists は eAPI 参照コマンド
    # ★ 削除(2026-05-20 解決策B): "情報" を除去
    #   理由: 「セキュリティの統計情報」「XDP情報」等、あらゆる文脈で使われるため
    #         単語単体では read/security を決定できない → LLM fallback に委ねる
    #   影響: 「バージョン情報を確認して」等は「確認」「バージョン」が残るため影響なし
    #         「セキュリティの統計情報を調べて」はLLM fallbackに到達→正しくsecurityへ
]
WRITE_KEYWORDS = [
    "設定","変更","追加","削除","適用","投入","作成","修正",
    "configure","set","add","delete","remove","update","apply","create",
]

# ── Security キーワード設計方針 ────────────────────────────────────────────────
# 【必須キーワード】これが含まれる場合のみ security に分類する。
#   XDP/ファイアウォール操作に固有の用語に限定し、
#   NTP・BGP・インターフェース等の一般デバイス参照と混同しない。
#
# 【削除した旧キーワード】
#   "統計","フロー","パケット","stats","flow" — 汎用的すぎる（NTP状態参照等と衝突）
#   "情報" — READ_KEYWORDS と重複
#   "ips"  — "tips" 等の誤マッチリスク
#
# 【移動先】
#   "脅威","攻撃","flood","syn flood" → SECURITY_CONTEXT_KEYWORDS（後述）
#   これらは単独ではなく、SECURITY_REQUIRED との組み合わせで security 判定する。
# ──────────────────────────────────────────────────────────────────────────────

# XDP/FW に明示的に言及しているキーワード（これだけで security 確定）
# ★ 2026-05-20 修正: READ_OVERRIDE 方式導入（方針B）
#   「BGPのACLを確認して」→ acl が SECURITY_REQUIRED にヒットして security 誤判定
#   していた問題を修正。acl/security/qos 等は参照目的でも使われる語なので除去し、
#   READ_OVERRIDE_WORDS + READ動詞の AND 条件で read に倒す仕組みを追加。
#   XDP固有の操作系ワード（ブロック・遮断・xdp・ebpf等）のみここに残す。
SECURITY_REQUIRED = [
    # 日本語: XDP/FW操作を示す明示的な操作系単語
    "ブロック", "遮断",
    # XDP/eBPF 固有（Arista デバイス設定とは別ドメイン）
    "xdp", "ebpf", "firewall",
    # ブロック操作の英語表現（drop単体は除外 → "drop list" "drop/block" のみ）
    "block", "drop list", "drop/block", "drop/unblock",
    "ブロックリスト",
    # QoS帯域制限（操作系のみ残す）
    # ★ "qos" 単体は除去 → "QoSの設定を確認" が eAPI に正しく流れるように
    "帯域制限", "rate limit", "rate-limit",
    # ★ 除去済みキーワード（READ_OVERRIDE_WORDS に移動）:
    #   "acl"        → show ip access-lists は eAPI 参照
    #   "security"   → セキュリティポリシー確認は eAPI 参照
    #   "セキュリティ" → 同上
    #   "qos"        → QoS設定確認は eAPI 参照
    #   "ファイアウォール" → FWルール表示は eAPI 参照の場合がある
]

# READ優先上書きワード:
#   SECURITY_REQUIRED にヒットしても、このワード + READ動詞がある場合は read に倒す。
#   「BGPのACLを確認して」「QoSの設定を見せて」「セキュリティポリシーを表示」等が対象。
#   XDP固有ワード（xdp/block/遮断）は SECURITY_REQUIRED に残してあるので上書きしない。
READ_OVERRIDE_WORDS = [
    "acl",           # show ip access-lists → eAPI 参照
    "security",      # セキュリティポリシーを確認 → eAPI 参照
    "セキュリティ",  # 同上（日本語）
    "qos",           # QoSの設定を確認 → eAPI 参照
    "ファイアウォール",  # ファイアウォールのルールを表示 → eAPI 参照
]

# 脅威・攻撃系キーワード（SECURITY_REQUIRED との AND 条件で security 判定）
# 単独では read に倒す（例: "攻撃を調べて" → eAPI で確認、"攻撃をブロック" → security）
SECURITY_CONTEXT_KEYWORDS = [
    "脅威", "攻撃", "flood", "syn flood", "syn-flood",
    "統計", "フロー", "パケット", "stats", "flow",
]

# ANTA Snapshot 検証キーワード（security/read/write より先にチェック）
VERIFY_KEYWORDS = [
    "anta","snapshot","スナップショット","事後","post_check","post-check",
    "post check","verify","検証","事後検証","副作用","影響確認",
    "anta テスト","anta test","ネットワーク検証","network verify",
]


def classify_query(query: str) -> str:
    """
    クエリを eapi_config / verify / security / write / read の5種に分類する。

    判定ロジック:
      0. VXLAN/EVPN × 設定変更 → eapi_config（NETCONF非対応のため最優先）
      1. VERIFY_KEYWORDS   → verify （ANTA検証）
      2. SECURITY_REQUIRED → security（XDP/FW操作の明示キーワード）
      3. READ + SECURITY_CONTEXT の両方 → read を優先
         （例: "フローの状態を確認" → read, "フローをブロック" → security）
      4. READ / WRITE キーワードで判定
      5. いずれも該当しない → LLMフォールバック（改善プロンプト）
    """
    q = query.lower()

    # 0. VXLAN/EVPN × 設定変更 → eapi_config（最優先）
    #    NETCONF/OpenConfig では設定不可のため eAPI configure session で処理する
    #
    #    ★ 参照系動詞（READ_KEYWORDS）が含まれる場合は eapi_config へ送らず read に倒す。
    #       例: "VXLANの設定を確認して" → "設定"(write_verb) + "確認"(read_verb)
    #           → 参照系が優先 → eapi_show へ
    #       例: "VXLAN VNI 100 に vni 10000 を設定して" → write_verb のみ → eapi_config
    _has_vxlan_evpn = any(k in q for k in [
        "vxlan", "evpn", "vni", "vtep", "route-target",
        "ルートターゲット", "mac flooding", "mac フラッディング",
        "address-family evpn", "evpn アドレスファミリー",
    ])
    # ★ BGP network advertise / redistribute も eapi_config へ
    # NETCONF の BGP YANG は複数ツリーをまたぐ複雑な操作のため CLI 方式が確実
    _has_bgp_cli = (
        any(k in q for k in ["bgp", "ルータ bgp", "router bgp"])
        and any(k in q for k in [
            "network ", "redistribute", "advertise", "アドバタイズ",
            "bgp network", "bgp advertise",
        ])
    )
    _has_write_verb = any(k in q for k in [
        "設定", "変更", "追加", "削除", "適用", "投入", "作成", "修正",
        "configure", "set", "add", "delete", "remove", "update", "apply", "create",
    ])
    _has_read_verb = any(k in q for k in READ_KEYWORDS)
    if (_has_vxlan_evpn and _has_write_verb and not _has_read_verb):
        return "eapi_config"
    # BGP network/redistribute/advertise は write_verb なしでも eapi_config へ
    # （「redistribute connected して」の「して」は write_verb にヒットしないため）
    if _has_bgp_cli and not _has_read_verb:
        return "eapi_config"
    # VXLAN/EVPN + 参照系動詞 → read（設定変更ではなく状態確認）
    # 例: "VXLANの設定を確認して" → read（eapi_show へ）
    # ※ mixed に落ちないよう、ここで明示的に read を返す
    if _has_vxlan_evpn and _has_read_verb:
        return "read"

    # 1. ANTA 検証を最優先
    if any(k in q for k in VERIFY_KEYWORDS):
        return "verify"

    # 2. XDP/FW 明示キーワードがあれば security 確定
    #    ★ READ_OVERRIDE_WORDS + READ動詞がある場合は read を優先（方針B）
    #    例: "BGPのACLを確認して" → acl がヒットするが "確認" もある → read
    #    例: "10.0.1.30をブロックして" → ブロックがヒット、READ_OVERRIDE にない → security
    #
    #    ★ ただし XDP固有ワード（xdp/ebpf/firewall）が含まれる場合は
    #       READ_OVERRIDE を無効にする（2026-05-21 修正）
    #    例: "XDPでQoSの設定を確認して"
    #       → xdp（SECURITY_REQUIRED）+ qos（READ_OVERRIDE）+ 確認（READ）
    #       → xdp が明示されているので override 無効 → security
    #    例: "QoSの設定を確認して"
    #       → qos（READ_OVERRIDE）+ 確認（READ）、xdp なし → override 有効 → read
    _XDP_EXPLICIT = ["xdp", "ebpf", "firewall", "ファイアウォール"]
    if any(k in q for k in SECURITY_REQUIRED):
        has_xdp_explicit = any(k in q for k in _XDP_EXPLICIT)
        override = any(k in q for k in READ_OVERRIDE_WORDS)
        has_read_verb = any(k in q for k in READ_KEYWORDS)
        # XDP固有ワードが明示されている場合は override を無効化
        if override and has_read_verb and not has_xdp_explicit:
            return "read"
        return "security"

    # 3. セキュリティ系曖昧語 × XDP文脈語 の AND → security（step3より前に判定）
    #    ★ 解決策B (2026-05-20): 「セキュリティの統計を見せて」等を正しく security へ
    #    「セキュリティ」単体は曖昧（デバイス設定参照でも使われる）だが、
    #    「統計/アラート/異常/監視/フロー」と組み合わさると XDP 文脈と判断できる。
    #    これを READ_KEYWORDS チェックより先に評価することで、
    #    「見せ」「確認」等の READ 動詞があっても security に正しく流れる。
    _sec_ambiguous   = ["セキュリティ", "security"]
    _sec_ctx_combine = ["統計", "stats", "アラート", "alert", "異常", "監視", "monitor",
                        "脅威", "フロー", "flow", "攻撃", "attack"]
    if (any(k in q for k in _sec_ambiguous)
            and any(k in q for k in _sec_ctx_combine)):
        return "security"

    has_read  = any(k in q for k in READ_KEYWORDS)
    has_write = any(k in q for k in WRITE_KEYWORDS)

    if has_read and not has_write:  return "read"
    if has_write and not has_read:  return "write"
    if has_read and has_write:      return "mixed"  # 参照+変更の混在 → UI で警告

    # 4. 脅威系のみ（READ/WRITE どちらもなし）→ security
    if any(k in q for k in SECURITY_CONTEXT_KEYWORDS):
        return "security"

    # 5. LLM フォールバック（改善プロンプト）
    # ★ 解決策B (2026-05-20): 改善プロンプト
    #   旧プロンプトの問題: security の定義が「操作系のみ」だったため
    #   「セキュリティの統計情報を調べて」等の「参照系 XDP クエリ」を
    #   LLM が read と誤判定していた。
    #   改善: security に「XDP に関するあらゆる参照・分析・統計」も含むと明示。
    #   また「セキュリティ＋統計/監視/アラート → security」の判定例を追加。
    result = invoke_with_fallback(
        llm,
        "あなたは Arista ネットワーク管理システムのルーター AI です。\n"
        "以下のクエリを 5 種類のルートのうち 1 つに分類し、一単語のみで回答してください。\n\n"
        "【ルート定義】\n"
        "  eapi_config : VXLAN/EVPN の設定変更（NETCONF/OpenConfig 非対応）\n"
        "                例: VXLAN VNI 設定 / EVPN RD/RT 設定 / VTEP 設定 /\n"
        "                    address-family evpn 有効化 / MAC フラッディング追加\n"
        "  read     : Arista cEOS デバイスの状態参照（show コマンド相当）\n"
        "             例: BGP 状態確認 / インターフェース確認 / ACL 表示 /\n"
        "                 ルーティングテーブル / NTP 状態 / VLAN 一覧\n"
        "  write    : Arista cEOS デバイスへの設定変更（VXLAN/EVPN 以外）\n"
        "             例: VLAN 作成 / インターフェース設定 / BGP neighbor 追加\n"
        "  security : XDP/eBPF ファイアウォールに関するあらゆる操作・参照・分析\n"
        "             例: IP ブロック / 遮断 / QoS 帯域制限 / フロー統計参照 /\n"
        "                 セキュリティ統計情報 / セキュリティアラート /\n"
        "                 セキュリティ脅威分析 / XDP 監視 / 攻撃検知\n"
        "  verify   : ANTA によるネットワーク検証・スナップショット\n"
        "             例: 事後検証 / post-check / anta テスト / 副作用確認\n\n"
        "【判定ルール（重要）】\n"
        "  - 「VXLAN」「EVPN」「VNI」「VTEP」「route-target」+「設定/変更/追加」→ eapi_config\n"
        "  - 「セキュリティ」+「統計/監視/アラート/分析/フロー」→ security\n"
        "  - 「セキュリティポリシー」「ACL」「FW ルール」等デバイス設定の参照 → read\n"
        "  - 「ブロック」「遮断」「xdp」「ebpf」単独 → security\n"
        "  - 「show」で始まる CLI コマンド → read\n"
        "  - NTP / BGP / OSPF / インターフェース / ルート の参照 → read\n\n"
        f"クエリ: {query}\n"
        "回答（eapi_config / read / write / security / verify のいずれか一単語のみ）:"
    ).strip().lower()

    if "eapi_config" in result: return "eapi_config"
    if "write"       in result: return "write"
    if "security"    in result: return "security"
    if "verify"      in result: return "verify"
    return "read"


# ═══════════════════════════════════════════════════════════════════════════════
# A2A 通信ユーティリティ
# ═══════════════════════════════════════════════════════════════════════════════

def _make_a2a_request(payload: dict, msg_id: str = None) -> dict:
    # v1.1.0: jsonrpc ラッパーなし、REST 形式で直接送信
    mid = msg_id or f"hub-{datetime.now().strftime('%H%M%S%f')}"
    return {
        "message": {
            "role": "ROLE_USER",
            "parts": [{"text": json.dumps(payload, ensure_ascii=False)}],
            "messageId": mid,
        },
        "configuration": {"returnImmediately": False},
    }


# v1.1.0: A2A-Version ヘッダーが必須
_A2A_HEADERS = {"A2A-Version": "1.0", "Content-Type": "application/json"}


def _extract_text(a2a_resp: dict) -> dict:
    """
    A2A v1.1.0 レスポンスからテキストを取り出し JSON として返す。

    【#1774 対応】
    message.parts（会話・要約）と artifacts（最終成果物）を両方ハンドリングする。
    Artifact が存在する場合は _artifact_{name} キーとして parsed にマージし、
    "result" という名前の Artifact は parsed["result"] を上書きする（成果物が正）。

    既知の Artifact 名:
      anta_report  → parsed["_artifact_anta_report"]  （ANTA テストレポート全体）
      xdp_log      → parsed["_artifact_xdp_log"]      （XDP 統計・分析ログ）
      report       → parsed["_artifact_report"]        （診断レポート全体）
      diff         → parsed["_artifact_diff"]          （差分テキスト）
    """
    try:
        # ── 1. Artifact を先に収集 ─────────────────────────────────────────────
        artifact_map: dict = {}
        for art in a2a_resp.get("artifacts", []):
            if not isinstance(art, dict):
                continue
            art_name = art.get("name", "")
            for part in art.get("parts", []):
                text = part.get("text") if isinstance(part, dict) else None
                if text is None:
                    continue
                try:
                    artifact_map[art_name] = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    artifact_map[art_name] = {"_raw_text": text}
                break  # 各 Artifact の parts 先頭のみ使用

        # ── 2. message.parts から会話テキスト（要約）を取得 ───────────────────
        parts = (
            a2a_resp.get("message", {}).get("parts", [])
            or a2a_resp.get("result", {}).get("parts", [])
            or a2a_resp.get("result", {}).get("message", {}).get("parts", [])
        )
        parsed: dict = {}
        for part in parts:
            text = part.get("text") if isinstance(part, dict) else None
            if text is None:
                continue
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                parsed = {"_raw_text": text}
                break
            # Hub 経由のネスト展開（後方互換）
            inner = parsed.get("result")
            if isinstance(inner, dict) and "message" in inner:
                parsed["result"] = _extract_text(inner)
            break

        if not parsed and not artifact_map:
            return {"_raw_a2a": a2a_resp}

        # ── 3. Artifact を parsed にマージ（Artifact が正の成果物）─────────────
        for art_name, art_data in artifact_map.items():
            parsed[f"_artifact_{art_name}"] = art_data
        if "result" in artifact_map:
            parsed["result"] = artifact_map["result"]

        return parsed

    except Exception as e:
        return {"_parse_error": str(e)}


async def _forward(target_url: str, payload: dict) -> dict:
    """
    ダウンストリームの A2A サーバへリクエストを転送し、レスポンスを返す。
    _extract_text() が Artifact を _artifact_* キーとして展開済みの dict を返す。
    """
    # v1.1.0: /message:send エンドポイント + A2A-Version ヘッダー必須
    a2a_req  = _make_a2a_request(payload)
    endpoint = target_url.rstrip("/") + "/message:send"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(endpoint, json=a2a_req, headers=_A2A_HEADERS)
        resp.raise_for_status()
        return _extract_text(resp.json())


async def _push_log(trace_id: str, message: str):
    for ws in _ws_clients.get(trace_id, []):
        try:
            await ws.send_text(json.dumps({"trace_id": trace_id, "log": message}))
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# A2A AgentExecutor（A2A プロトコル経由のルーティング）
# ═══════════════════════════════════════════════════════════════════════════════

class TaskDecomposeExecutor(AgentExecutor):
    def _parse_request(self, text: str) -> dict:
        text = text.strip()
        try:
            p = json.loads(text)
            if isinstance(p, dict) and "query" in p:
                return p
        except json.JSONDecodeError:
            pass
        return {"query": text}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        from a2a.types.a2a_pb2 import Part as _Part, Message as _Message, Role as _Role
        import uuid as _uuid

        def _make_message(text):
            msg = _Message()
            msg.role = _Role.ROLE_AGENT
            msg.message_id = str(_uuid.uuid4())
            if context.task_id:
                msg.task_id = context.task_id
            if context.context_id:
                msg.context_id = context.context_id
            msg.parts.append(_Part(text=text))
            return msg

        async def _send_text(text):
            await event_queue.enqueue_event(_make_message(text))

        raw_text = "".join(
            part.text for part in context.message.parts
            if part.HasField("text")
        )
        if not raw_text.strip():
            await _send_text(get_msg("ws_empty"))
            return

        params    = self._parse_request(raw_text)
        query     = params.get("query", raw_text)
        device_ip = params.get("device_ip")
        username  = params.get("username")
        password  = params.get("password")
        # port はルート別に使い分け（NETCONF:830 / eAPI:各サーバデフォルト）
        port      = params.get("port")   # None の場合は転送先サーバのデフォルトを使用
        deploy    = params.get("deploy", False)

        logger.info(f"[A2A] 受信: {query[:80]} deploy={deploy}")

        # action フィールドが明示されている場合は classify_query より優先
        _action = params.get("action", "")
        if _action in ("verify", "snapshot", "post_check", "compare"):
            route = "verify"
        elif _action in ("block", "unblock", "qos_set", "qos_list", "qos_get",
                         "drop_list", "stats", "top", "info", "analyze"):
            route = "security"
        else:
            route = classify_query(query)

        if route == "security":
            xdp_payload = {"query": query}
            try:
                inner = await _forward(XDP_A2A_URL, xdp_payload)
                result = {"query": query, "route": "security",
                          "routed_to": XDP_A2A_URL, "status": "success",
                          "result": inner}
            except httpx.ConnectError as e:
                result = {"query": query, "route": "security",
                          "routed_to": XDP_A2A_URL,
                          "status": "error", "message": f"XDP A2A 接続エラー: {e}"}
            except Exception as e:
                result = {"query": query, "route": "security",
                          "routed_to": XDP_A2A_URL,
                          "status": "error", "message": str(e)}
        elif route == "verify":
            anta_payload = {
                "query":    query,
                "action":   params.get("action", "verify"),
                "snapshot_id": params.get("snapshot_id", ""),
                "tests":    params.get("tests"),
                "device_ip": device_ip or "",
                "username":  username or "",
                "password":  password or "",
            }
            try:
                inner = await _forward(ANTA_A2A_URL, anta_payload)
                result = {"query": query, "route": "verify",
                          "routed_to": ANTA_A2A_URL, "status": "success",
                          "result": inner}
            except httpx.ConnectError as e:
                result = {"query": query, "route": "verify",
                          "routed_to": ANTA_A2A_URL,
                          "status": "error",
                          "message": f"ANTA A2A 接続エラー: {e} (port:8004 未起動の可能性)"}
            except Exception as e:
                result = {"query": query, "route": "verify", "routed_to": ANTA_A2A_URL,
                          "status": "error", "message": str(e)}
        else:
            target_url = NETCONF_A2A_URL if route == "write" else EAPI_A2A_URL
            forward_payload = {
                "query": query, "device_ip": device_ip or "",
                "username": username or "", "password": password or "",
                "deploy": deploy,
            }
            # NETCONF(write) のみ port を転送。read は eAPI サーバのデフォルト(443)を使用
            if route == "write" and port:
                forward_payload["port"] = port
            elif route == "write":
                forward_payload["port"] = "830"
            try:
                inner = await _forward(target_url, forward_payload)
                result = {"query": query, "route": route,
                          "routed_to": target_url, "status": "success",
                          "result": inner}
            except httpx.ConnectError as e:
                result = {"query": query, "route": route, "routed_to": target_url,
                          "status": "error", "message": f"接続エラー: {e}"}
            except Exception as e:
                result = {"query": query, "route": route, "routed_to": target_url,
                          "status": "error", "message": str(e)}

        await _send_text(json.dumps(result, ensure_ascii=False, indent=2))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise UnsupportedOperationError(get_msg("cancel_unsupported"))


# ═══════════════════════════════════════════════════════════════════════════════
# FastAPI アプリ（NiceGUI UI 向け REST + WebSocket）
# ═══════════════════════════════════════════════════════════════════════════════

rest_app = FastAPI(
    title="Arista Network Agent Hub API",
    version=VERSION,
    description="task_decompose A2A Hub + NiceGUI 向け REST API",
)
rest_app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_methods=["*"], allow_headers=["*"],
)


# ── スキーマ ─────────────────────────────────────────────────────────────────
class DeviceConfig(BaseModel):
    ip:       str = "172.20.100.31"
    port:     str = "830"
    username: str = "admin"
    password: str = "admin"

class ExecuteRequest(BaseModel):
    query:  str
    device: DeviceConfig = DeviceConfig()

class DeployRequest(BaseModel):
    device:      DeviceConfig = DeviceConfig()
    snapshot_id: str          = ""  # CNV: Before Snapshot ID（空文字 = Post-Check スキップ）


# ── GET /healthz ──────────────────────────────────────────────────────────────
@rest_app.get("/healthz", tags=["ops"])
async def healthz():
    servers = {}
    async with httpx.AsyncClient(timeout=5) as client:
        for name, url in [("netconf", NETCONF_A2A_URL),
                            ("eapi",    EAPI_A2A_URL),
                            ("xdp",     XDP_A2A_URL),
                            ("anta",    ANTA_A2A_URL),
                            ("eapi_config", EAPI_CONFIG_A2A_URL)]:
            try:
                r = await client.get(f"{url}/.well-known/agent.json")
                servers[name] = {"status": "ok", "name": r.json().get("name")}
            except Exception as e:
                servers[name] = {"status": "error", "message": str(e)[:60]}
    return {
        "status":     "ok",
        "version":    VERSION,
        "build_date": BUILD_DATE,
        "timestamp":  datetime.now(timezone.utc).isoformat(),
        "hub_port":   A2A_PORT,
        "downstream": servers,
        "routes": {
            "write":       NETCONF_A2A_URL,
            "read":        EAPI_A2A_URL,
            "security":    XDP_A2A_URL,
            "verify":      ANTA_A2A_URL,
            "eapi_config": EAPI_CONFIG_A2A_URL,
        },
    }


# ── POST /validate ────────────────────────────────────────────────────────────
class ValidateRequest(BaseModel):
    xml: str

@rest_app.post("/validate", tags=["ops"])
async def validate_xml(req: ValidateRequest):
    """
    XML 構文チェック。ET.fromstring() で parse を試み、
    成功なら valid=True、失敗なら valid=False + エラーメッセージを返す。
    Hub 側で完結するため、バックエンドサーバへの通信は不要。
    """
    import xml.etree.ElementTree as ET
    xml_str = req.xml.strip()
    if not xml_str:
        return {"valid": False, "message": "XML が空です"}
    try:
        ET.fromstring(xml_str)
        return {"valid": True, "message": "✅ XML 構文チェック OK"}
    except ET.ParseError as e:
        return {"valid": False, "message": f"❌ XML 構文エラー: {e}"}


# ── POST /execute ─────────────────────────────────────────────────────────────
@rest_app.post("/execute", tags=["netconf"])
async def execute(req: ExecuteRequest):
    """
    Dry-run 実行。A2A Hub 経由でルーティングし、deploy=False で XML/結果を返す。
    変更系: NETCONF XML を生成して返す（実機未投入）
    参照系: eAPI show を即時実行して結果を返す
    """
    trace_id = str(uuid.uuid4())[:8]
    route    = classify_query(req.query)
    logger.info(f"[{trace_id}] /execute: {req.query!r} route={route}")

    # ── VLAN名のみ削除ガード ──────────────────────────────────────────────────
    # ネットワーク運用の原則: VLAN操作はVLAN IDで行う（名前は一意でないため危険）。
    # 削除系クエリで VLAN IDの数値が含まれず、VLAN名のみ指定されている場合は
    # バックエンドに転送せず即エラーを返す。
    _delete_verbs    = ["削除", "delete", "remove", "消し", "消す", "なくし"]
    _q_lower         = req.query.lower()
    _has_delete      = any(k in _q_lower for k in _delete_verbs)
    _has_vlan_kw     = "vlan" in _q_lower
    _has_vlan_id     = bool(re.search(r'\b\d+\b', req.query))
    if route == "write" and _has_delete and _has_vlan_kw and not _has_vlan_id:
        msg = (
            "VLAN削除にはVLAN IDの指定が必要です。\n"
            "VLAN名からの自動解決は行いません（名前は一意でないため危険）。\n"
            "例: 'VLAN 102 を削除して' のようにVLAN IDで指定してください。"
        )
        logger.warning(f"[{trace_id}] VLAN名のみ削除をブロック: {req.query!r}")
        error_response = {
            "trace_id":       trace_id,
            "route":          "write",
            "is_read":        False,
            "status":         "blocked",
            "overall_status": "blocked",
            "summary":        msg,
            "xml":            "",
            "session_diff":   {},
            "task_summaries": [{
                "task_id":       "task_1",
                "operation":     "delete_vlan",
                "target":        req.query,
                "deploy_status": "blocked",
                "audit_message": msg,
            }],
        }
        _trace_store[trace_id] = {
            **error_response,
            "device":      req.device.model_dump(),
            "query":       req.query,
            "is_read":     False,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }
        from fastapi.responses import Response as _VlanErrResp
        return _VlanErrResp(
            content=json.dumps(error_response, ensure_ascii=True),
            media_type="application/json",
        )

    is_read = (route == "read")
    # eAPI と NETCONF でポートを使い分ける
    # eAPI: HTTPS/443（実機確認済み。NETCONF port 830 を渡すと SSL エラーになる）
    # NETCONF: req.device.port をそのまま使う（デフォルト 830）
    EAPI_DEFAULT_PORT = int(os.getenv("EAPI_PORT", "443"))
    payload = {
        "query":     req.query,
        "device_ip": req.device.ip,
        "username":  req.device.username,
        "password":  req.device.password,
        "port":      str(EAPI_DEFAULT_PORT) if is_read else req.device.port,
        "deploy":    is_read,   # 参照系のみ即時実行
    }

    if route == "security":
        # analyze クエリは action="analyze" を付与して送る
        _analyze_keywords = ["分析", "解析", "analyze", "ai解析", "提案"]
        _is_analyze = any(k in req.query.lower() for k in _analyze_keywords)
        xdp_payload = {"query": req.query, "deploy": False}
        if _is_analyze:
            xdp_payload["action"] = "analyze"

        try:
            xdp_result = await _forward(XDP_A2A_URL, xdp_payload)
        except httpx.ConnectError as e:
            raise HTTPException(status_code=503,
                detail=f"XDP A2A Server ({XDP_A2A_URL}) に接続できません: {e}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        # analyze アクション時は exec_tags / analysis をトップレベルに引き上げて
        # UI（app_a2a.py）が直接参照しやすくする
        # ── #1774: _artifact_* キーをトップレベルに透過伝播 ─────────────────────
        _artifact_passthrough = {k: v for k, v in xdp_result.items()
                                 if k.startswith("_artifact_")}
        response = {
            "trace_id":  trace_id,
            "route":     "security",
            "is_read":   False,
            "status":    xdp_result.get("status", "unknown"),
            "summary":   xdp_result.get("summary", ""),
            "routed_to": XDP_A2A_URL,
            "result":    xdp_result,
            # analyze 時の追加フィールド（非 analyze 時は空）
            "analysis":  xdp_result.get("analysis", ""),
            "exec_tags": xdp_result.get("exec_tags", []),
            # security 操作では xml/session_diff は不要
            "xml":          "",
            "session_diff": {},
            **_artifact_passthrough,   # _artifact_xdp_log 等を透過
        }
        _trace_store[trace_id] = {
            **response,
            "device":      req.device.model_dump(),
            "query":       req.query,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }
        from fastapi.responses import Response
        return Response(
            content=json.dumps(response, ensure_ascii=True),
            media_type="application/json",
        )

    # ── mixed ルート: 参照+変更の混在クエリ → eAPI で参照だけ実行して警告 ───────
    # 設定変更は実行せず参照結果のみ返す。UI 側で警告バブルを追加表示する。
    if route == "mixed":
        _warn_msg = (
            "⚠️ 参照と設定変更が混在しています。\n"
            "参照結果のみ表示しました。設定変更は別途入力してください。\n"
            "例: まず「VLANの状態を確認して」→ 次に「VLAN ID 103 の DEV3_VLAN を作成して」"
        )
        logger.info(f"[{trace_id}] mixed クエリ検出: 参照のみ実行")
        mixed_payload = {
            "query":     req.query,
            "device_ip": req.device.ip,
            "username":  req.device.username,
            "password":  req.device.password,
            "port":      str(EAPI_DEFAULT_PORT),
            "deploy":    True,   # eAPI は即時実行
        }
        try:
            mixed_result = await _forward(EAPI_A2A_URL, mixed_payload)
        except Exception as e:
            mixed_result = {"status": "error", "message": str(e)}

        response = {
            "trace_id":      trace_id,
            "route":         "read",   # UI は read として処理
            "is_read":       True,
            "status":        mixed_result.get("status", "unknown"),
            "summary":       mixed_result.get("summary", ""),
            "result":        mixed_result,
            "formatted":     mixed_result.get("formatted_text",
                             mixed_result.get("formatted", "")),
            "xml":           "",
            "session_diff":  {},
            "mixed_warning": _warn_msg,   # ★ UI が警告バブルを表示するフラグ
        }
        _trace_store[trace_id] = {
            **response,
            "device":      req.device.model_dump(),
            "query":       req.query,
            "is_read":     True,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }
        from fastapi.responses import Response as _MixedResp
        return _MixedResp(
            content=json.dumps(response, ensure_ascii=True),
            media_type="application/json",
        )

    # ── eapi_config ルート: VXLAN/EVPN 設定変更 (8006) dry-run ─────────────────
    if route == "eapi_config":
        eapi_cfg_payload = {
            "query":  req.query,
            "deploy": False,   # dry-run（Phase1）
        }
        try:
            eapi_cfg_result = await _forward(EAPI_CONFIG_A2A_URL, eapi_cfg_payload)
        except httpx.ConnectError as e:
            raise HTTPException(status_code=503,
                detail=f"eAPI Config Server ({EAPI_CONFIG_A2A_URL}) に接続できません: {e}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        # eAPI config の diff を diff_history 互換の session_diff 形式に変換する
        raw_diff = eapi_cfg_result.get("diff", "")
        diff_lines = []
        for line in raw_diff.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                diff_lines.append({"op": "+", "text": line[1:].lstrip()})
            elif line.startswith("-") and not line.startswith("---"):
                diff_lines.append({"op": "-", "text": line[1:].lstrip()})
            elif line.startswith("@@"):
                diff_lines.append({"op": "@@", "text": line})

        session_diff = {
            "status":     "ok" if raw_diff else "no_change",
            "diff_lines": diff_lines,
            "diff_text":  raw_diff,
            "message":    eapi_cfg_result.get("message", ""),
        }
        # AI 要約（LLMによるdiff解釈）を生成する
        if raw_diff:
            try:
                ai_summary = invoke_with_fallback(
                    llm,
                    "以下は Arista cEOS の設定変更差分（configure session diffs）です。\n"
                    "この差分を日本語で2〜3文に要約してください。\n"
                    "変更の意図と影響を具体的に述べてください。\n\n"
                    f"差分:\n{raw_diff[:1500]}\n\n要約:"
                ).strip()
                session_diff["ai_summary"] = ai_summary
            except Exception:
                session_diff["ai_summary"] = ""

        cmds = eapi_cfg_result.get("cmds", [])
        status = eapi_cfg_result.get("status", "unknown")

        response = {
            "trace_id":     trace_id,
            "route":        "eapi_config",
            "is_read":      False,
            "status":       status,   # "plan" | "blocked" | "error"
            "summary":      (
                f"eAPI Config dry-run: {len(cmds)}件のコマンドを計画しました"
                if status == "plan" else eapi_cfg_result.get("message", "")
            ),
            "xml":          "",       # NETCONF XML は不要
            "session_diff": session_diff,
            "routed_to":    EAPI_CONFIG_A2A_URL,
            "result":       eapi_cfg_result,
            # eapi_config 専用フィールド（UI が参照）
            "eapi_cmds":    cmds,
            "eapi_diff":    raw_diff,
            "eapi_session": eapi_cfg_result.get("session", ""),
            "eapi_warning": eapi_cfg_result.get("warning", ""),
        }
        _trace_store[trace_id] = {
            **response,
            "device":      req.device.model_dump(),
            "query":       req.query,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }
        from fastapi.responses import Response as _ECResp
        return _ECResp(
            content=json.dumps(response, ensure_ascii=True),
            media_type="application/json",
        )

    # ── verify ルート: ANTA Snapshot 検証 (8004) ─────────────────────────────
    if route == "verify":
        anta_payload = {
            "query":       req.query,
            "action":      "verify",
            "device_ip":   req.device.ip,
            "username":    req.device.username,
            "password":    req.device.password,
        }
        try:
            anta_result = await _forward(ANTA_A2A_URL, anta_payload)
        except httpx.ConnectError as e:
            raise HTTPException(status_code=503,
                detail=f"ANTA A2A Server ({ANTA_A2A_URL}) に接続できません: {e}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        # ── #1774: _artifact_* キーをトップレベルに透過伝播 ─────────────────────
        _artifact_passthrough = {k: v for k, v in anta_result.items()
                                 if k.startswith("_artifact_")}
        response = {
            "trace_id":       trace_id,
            "route":          "verify",
            "is_read":        True,
            "status":         anta_result.get("status", "unknown"),
            "summary":        anta_result.get("summary", ""),
            "routed_to":      ANTA_A2A_URL,
            "result":         anta_result,
            "xml":            "",
            "session_diff":   {},
            "snapshot_id":    anta_result.get("snapshot_id", ""),
            "tests_total":    anta_result.get("tests_total", 0),
            "tests_passed":   anta_result.get("tests_passed", 0),
            "tests_failed":   anta_result.get("tests_failed", 0),
            **_artifact_passthrough,   # _artifact_anta_report 等を透過
        }
        _trace_store[trace_id] = {
            **response,
            "device":      req.device.model_dump(),
            "query":       req.query,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }
        from fastapi.responses import Response as _Resp
        return _Resp(
            content=json.dumps(response, ensure_ascii=True),
            media_type="application/json",
        )

    target_url = EAPI_A2A_URL if is_read else NETCONF_A2A_URL

    result = None
    for attempt in range(2):
        try:
            result = await _forward(target_url, payload)
            break
        except Exception as e:
            logger.warning(f"[{trace_id}] attempt {attempt+1} failed: {e}")
            if attempt < 1:
                await asyncio.sleep(2.0)

    if result is None:
        raise HTTPException(status_code=500, detail=get_msg("hub_conn_error"))

    # ── 変更系: NETCONF dry-run 結果 + session diff（事前 diff） ────────────
    xml_out      = ""
    session_diff_result = {}

    if not is_read:
        # NETCONFサーバの response_payload は _extract_text() で展開されて result に入る。
        # final_xml / generated_xml を以下の優先順で取得する:
        #   1. result 直下（今回追加）
        #   2. result["result"] のネスト内（旧形式互換）
        #   3. task_summaries の先頭タスクの final_xml
        raw = result.get("result", result)
        xml_out = (
            result.get("final_xml", "")           # ★ NETCONFサーバが直接返す（今回修正）
            or result.get("generated_xml", "")
            or raw.get("final_xml", "")
            or raw.get("generated_xml", "")
        )
        # task_summaries からも探す（フォールバック）
        if not xml_out:
            for ts in result.get("task_summaries", []):
                _fx = ts.get("final_xml") or ts.get("generated_xml", "")
                if _fx:
                    xml_out = _fx
                    break

        # ── ★ session diff（ハイブリッド・トランザクション方式）────────────
        # NETCONF XML が取れた場合、eAPI の configure session で
        # cEOS がデバイス自身で計算した +/- diff を取得する。
        # running-config は変更しない（abort 保証）。
        if xml_out:
            EAPI_DIRECT_URL = os.getenv("EAPI_DIRECT_URL",
                                         f"http://localhost:{os.getenv('EAPI_A2A_PORT','8002')}")
            EAPI_DIFF_PORT  = int(os.getenv("EAPI_PORT", "443"))
            EAPI_TRANSPORT  = os.getenv("EAPI_TRANSPORT", "https")

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    diff_resp = await client.post(
                        f"{EAPI_SDIFF_URL}/session-diff",
                        json={
                            "xml_str":   xml_out,
                            "device_ip": req.device.ip,
                            "port":      EAPI_DIFF_PORT,
                            "transport": EAPI_TRANSPORT,
                            "username":  req.device.username,
                            "password":  req.device.password,
                        },
                        timeout=30.0,
                    )
                    if diff_resp.status_code == 200:
                        session_diff_result = diff_resp.json()
                        logger.info(
                            f"[{trace_id}] session diff: "
                            f"status={session_diff_result.get('status')} "
                            f"lines={len(session_diff_result.get('diff_lines', []))}"
                        )
                        # NETCONF サーバが ai_summary を生成済みの場合（BGP 削除等）は
                        # eAPI session-diff の ai_summary より優先して上書きする
                        netconf_ai_summary = result.get("ai_summary", "")
                        if netconf_ai_summary:
                            session_diff_result["ai_summary"] = netconf_ai_summary
                            logger.info(f"[{trace_id}] ai_summary: NETCONF サーバから注入")
                    else:
                        logger.warning(
                            f"[{trace_id}] session diff HTTP {diff_resp.status_code}"
                        )
                        session_diff_result = {
                            "status": "error",
                            "message": f"HTTP {diff_resp.status_code}",
                            "diff_lines": [], "diff_text": "",
                        }
            except Exception as e:
                # session diff の失敗は /execute 全体の失敗にしない
                logger.warning(f"[{trace_id}] session diff スキップ: {e}")
                session_diff_result = {
                    "status": "skipped",
                    "message": f"session diff 取得失敗（eAPI 接続不可）: {e}",
                    "diff_lines": [], "diff_text": "",
                }
        else:
            # XML が取れなかった場合（task_summaries のみ）
            session_diff_result = {
                "status":  "skipped",
                "message": "NETCONF dry-run が XML を生成しませんでした",
                "diff_lines": [], "diff_text": "",
            }

    # READ 系: raw_result（大量データ・制御文字含む）を除外して返す
    if is_read:
        safe_result = {
            "query":          result.get("query", req.query),
            "cmds":           result.get("cmds", []),
            "status":         result.get("status", "unknown"),
            "summary":        result.get("summary", ""),
            "scope_note":     result.get("scope_note", "operational-state (eAPI show)"),
            "parse_method":   result.get("parse_method", ""),      # structured / llm
            "formatted_text": result.get("formatted_text", ""),    # ハイブリッドパース結果
            "formatted":      result.get("formatted", ""),         # 後方互換
        }
    else:
        safe_result = result

    # ── #1774: _artifact_* キーをトップレベルに透過伝播 ─────────────────────────
    _artifact_passthrough = {k: v for k, v in result.items()
                             if k.startswith("_artifact_")}
    response = {
        "trace_id":     trace_id,
        "route":        route,
        "is_read":      is_read,
        "status":       result.get("status") or result.get("overall_status", "unknown"),
        "summary":      result.get("summary", ""),
        "xml":          xml_out,
        "session_diff": session_diff_result,   # ★ 事前 diff（新規追加）
        "result":       safe_result,
        **_artifact_passthrough,   # _artifact_report / _artifact_diff 等を透過
    }

    _trace_store[trace_id] = {
        **response,
        "device":      req.device.model_dump(),
        "query":       req.query,
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(f"[{trace_id}] /execute 完了: status={response['status']}")
    # json.dumps(ensure_ascii=True) で全文字をエスケープして返す
    # ensure_ascii=False だと formatted 内の実際の \n (0x0A) がそのまま出力され
    # curl/shell での JSON パースエラーになる
    from fastapi.responses import Response
    return Response(
        content=json.dumps(response, ensure_ascii=True),
        media_type="application/json",
    )


# ── CNV 自動 Post-Check ───────────────────────────────────────────────────────
async def _auto_post_check(trace_id: str, snap_id: str, device: DeviceConfig) -> None:
    """
    NETCONF デプロイ完了後に ANTA post_check を非同期で自動実行する（CNV）。

    asyncio.create_task() で呼び出し、/deploy のレスポンスタイムに影響しない。
    結果は _trace_store[trace_id]["anta_post_check_result"] に追記し、
    GET /diff/{trace_id} で UI がポーリング取得できる形にする。
    完了・失敗は _push_log（WebSocket）でリアルタイム通知する。

    snap_id なし:  呼び出し側でガードするが、念のため内部でも早期 return。
    ANTA 障害時:   デプロイ成功扱いを維持し、エラー情報を trace_store に記録。
    """
    if not snap_id:
        return

    logger.info(f"[{trace_id}] CNV: 自動 Post-Check 開始 snap_id={snap_id!r}")
    await _push_log(trace_id, f"CNV: ANTA Post-Check 開始 (snap_id={snap_id})")

    anta_payload = {
        "action":      "post_check",
        "query":       "CNV 自動 Post-Check（deploy 後）",
        "snapshot_id": snap_id,
        "device_ip":   device.ip,
        "username":    device.username,
        "password":    device.password,
        "tests":       None,   # None = ANTA サーバ側のデフォルトカテゴリを使用
    }

    try:
        result = await _forward(ANTA_A2A_URL, anta_payload)
        # ★ deploy_result の中に書き込む（UI は /diff の deploy_result 配下をポーリング）
        _trace_store[trace_id].setdefault("deploy_result", {})["anta_post_check_result"] = result
        _trace_store[trace_id].setdefault("deploy_result", {})["anta_post_check_status"] = result.get("status", "unknown")
        summary = result.get("summary", "")
        logger.info(
            f"[{trace_id}] CNV: Post-Check 完了 "
            f"status={result.get('status')} summary={summary[:80]}"
        )
        await _push_log(trace_id, f"CNV: Post-Check 完了 — {summary}")
    except httpx.ConnectError as e:
        msg = f"ANTA A2A Server ({ANTA_A2A_URL}) に接続できません: {e}"
        logger.warning(f"[{trace_id}] CNV: {msg}")
        _trace_store[trace_id].setdefault("deploy_result", {})["anta_post_check_result"] = {
            "status": "error", "message": msg, "new_issues": [],
        }
        _trace_store[trace_id].setdefault("deploy_result", {})["anta_post_check_status"] = "error"
        await _push_log(trace_id, f"CNV: Post-Check 失敗 — {msg}")
    except Exception as e:
        msg = f"Post-Check 例外: {type(e).__name__}: {e}"
        logger.warning(f"[{trace_id}] CNV: {msg}")
        _trace_store[trace_id].setdefault("deploy_result", {})["anta_post_check_result"] = {
            "status": "error", "message": msg, "new_issues": [],
        }
        _trace_store[trace_id].setdefault("deploy_result", {})["anta_post_check_status"] = "error"
        await _push_log(trace_id, f"CNV: Post-Check 失敗 — {msg}")


# ── POST /deploy/{trace_id} ───────────────────────────────────────────────────
@rest_app.post("/deploy/{trace_id}", tags=["netconf"])
async def deploy(trace_id: str, req: DeployRequest):
    """
    本番デプロイ。/execute で生成した trace_id を使い、deploy=True で実機投入。
    参照系クエリの trace_id は拒否。
    """
    stored = _trace_store.get(trace_id)
    if stored is None:
        raise HTTPException(
            status_code=400,
            detail=f"trace_id '{trace_id}' が見つかりません。先に /execute を実行してください。",
        )
    if stored.get("is_read"):
        raise HTTPException(
            status_code=400,
            detail=get_msg("read_deploy_deny"),
        )

    query = stored["query"]
    logger.info(f"[{trace_id}] /deploy 開始: {query!r}")
    await _push_log(trace_id, get_msg("deploy_start"))

    payload = {
        "query":     query,
        "device_ip": req.device.ip,
        "username":  req.device.username,
        "password":  req.device.password,
        "port":      req.device.port,
        "deploy":    True,
    }


    # ── eapi_config ルート: eAPI configure session commit (8006) ─────────────
    if stored.get("route") == "eapi_config":
        # Phase1 で生成した CLI コマンドを Phase2 で再利用（LLM 再呼び出し不要）
        stored_cmds = stored.get("eapi_cmds", [])
        eapi_cfg_deploy_payload = {
            "query":  query,
            "deploy": True,
            "cmds":   stored_cmds,  # Phase1 生成済みコマンドを指定
        }
        try:
            eapi_cfg_result = await _forward(EAPI_CONFIG_A2A_URL, eapi_cfg_deploy_payload)
        except httpx.ConnectError as e:
            logger.error(f"[{trace_id}] eAPI Config deploy エラー: {e}")
            await _push_log(trace_id, f"eAPI Config deploy 失敗: {e}")
            raise HTTPException(status_code=503,
                detail=f"eAPI Config Server ({EAPI_CONFIG_A2A_URL}) に接続できません: {e}")
        except Exception as e:
            logger.error(f"[{trace_id}] eAPI Config deploy エラー: {e}")
            await _push_log(trace_id, get_msg("deploy_failed") + f": {e}")
            raise HTTPException(status_code=500, detail=str(e))

        status = eapi_cfg_result.get("status", "unknown")
        response = {
            "trace_id":   trace_id,
            "route":      "eapi_config",
            "status":     status,
            "summary":    eapi_cfg_result.get("message", ""),
            "result":     eapi_cfg_result,
            "eapi_cmds":  eapi_cfg_result.get("cmds", stored_cmds),
            "eapi_session": eapi_cfg_result.get("session", ""),
            "audit_scope_note": "eAPI configure session commit — running-config に反映済み",
            "task_summaries": [
                {
                    "task_id":       "eapi_config",
                    "operation":     "eapi_configure_session",
                    "target":        "running-config",
                    "deploy_status": "success" if status == "success" else status,
                    "audit_message": eapi_cfg_result.get("message", "")[:120],
                    "audit_scope":   f"cmds: {stored_cmds}",
                }
            ],
        }
        _trace_store[trace_id]["deploy_result"] = response
        _trace_store[trace_id]["deployed_at"]   = datetime.now(timezone.utc).isoformat()
        await _push_log(trace_id, f"eAPI Config commit 完了: {status}")
        logger.info(f"[{trace_id}] eAPI Config /deploy 完了: status={status}")

        # ── CNV 自動 Post-Check（eAPI Config も NETCONF と同じく実行）────────
        snap_id   = req.snapshot_id.strip()
        deploy_ok = (status == "success")
        if snap_id and deploy_ok:
            asyncio.create_task(_auto_post_check(trace_id, snap_id, req.device))
            response["anta_post_check"] = "running"
            logger.info(f"[{trace_id}] CNV(eAPI Config): Post-Check タスク起動 snap_id={snap_id!r}")
        else:
            response["anta_post_check"] = "skipped"

        from fastapi.responses import Response as _ECFR
        return _ECFR(
            content=json.dumps(response, ensure_ascii=True),
            media_type="application/json",
        )

    # ── security ルートは XDP A2A (8003) へ deploy=True で再送 ────────────────
    if stored.get("route") == "security":
        xdp_deploy_payload = {
            **stored.get("result", {}).get("classified", {}),
            "query":  query,
            "deploy": True,
        }
        # classified が空の場合は query のみで再送（XDP側でLLMが再分類）
        if not xdp_deploy_payload.get("action"):
            xdp_deploy_payload = {"query": query, "deploy": True}

        try:
            xdp_result = await _forward(XDP_A2A_URL, xdp_deploy_payload)
        except Exception as e:
            logger.error(f"[{trace_id}] XDP deploy エラー: {e}")
            await _push_log(trace_id, get_msg("deploy_failed") + f": {e}")
            raise HTTPException(status_code=500, detail=str(e))

        response = {
            "trace_id":  trace_id,
            "route":     "security",
            "status":    xdp_result.get("status", "unknown"),
            "summary":   xdp_result.get("summary") or xdp_result.get("message", ""),
            "result":    xdp_result,
            "analysis":  xdp_result.get("analysis", ""),
            "exec_tags": xdp_result.get("exec_tags", []),
            "audit_scope_note": "XDP Firewall (Go API) へ直接反映済み",
        }
        _trace_store[trace_id]["deploy_result"] = response
        _trace_store[trace_id]["deployed_at"]   = datetime.now(timezone.utc).isoformat()
        await _push_log(trace_id, f"XDP deploy 完了: {response['status']}")
        logger.info(f"[{trace_id}] XDP /deploy 完了: status={response['status']}")
        from fastapi.responses import Response as FR
        return FR(
            content=json.dumps(response, ensure_ascii=True),
            media_type="application/json",
        )

    try:
        result = await _forward(NETCONF_A2A_URL, payload)
    except Exception as e:
        logger.error(f"[{trace_id}] deploy エラー: {e}")
        await _push_log(trace_id, get_msg("deploy_failed") + f": {e}")
        raise HTTPException(status_code=500, detail=str(e))

    response = {
        "trace_id": trace_id,
        "status":   result.get("overall_status", result.get("status", "unknown")),
        "summary":  result.get("summary", ""),
        "result":   result,
        "audit_scope_note": get_msg("audit_scope_note"),
    }

    _trace_store[trace_id]["deploy_result"] = response
    _trace_store[trace_id]["deployed_at"]   = datetime.now(timezone.utc).isoformat()

    await _push_log(trace_id, get_msg("deploy_done") + f": {response['status']}")
    logger.info(f"[{trace_id}] /deploy 完了: status={response['status']}")

    # ── CNV 自動 Post-Check ────────────────────────────────────────────────────
    # snapshot_id が渡された場合のみ実行（空文字 = スキップ）。
    # deploy が success 系のときのみ実行し、失敗デプロイには後追いしない。
    # asyncio.create_task で非同期起動するため /deploy の応答は即時返却される。
    snap_id = req.snapshot_id.strip()
    _ok_statuses = ("success", "all_success", "dry_run", "no_changes")
    deploy_ok = any(s in response["status"] for s in _ok_statuses)

    if snap_id and deploy_ok:
        asyncio.create_task(_auto_post_check(trace_id, snap_id, req.device))
        response["anta_post_check"] = "running"   # UI ポーリングの起動トリガー
        logger.info(f"[{trace_id}] CNV: Post-Check タスク起動 snap_id={snap_id!r}")
    else:
        response["anta_post_check"] = "skipped"
        if not snap_id:
            logger.info(f"[{trace_id}] CNV: snapshot_id 未指定のため Post-Check スキップ")
        else:
            logger.info(
                f"[{trace_id}] CNV: deploy status={response['status']} のため Post-Check スキップ"
            )
    # ── CNV ここまで ───────────────────────────────────────────────────────────

    from fastapi.responses import Response
    return Response(
        content=json.dumps(response, ensure_ascii=True),
        media_type="application/json",
    )


# ── GET /diff/{trace_id} ──────────────────────────────────────────────────────
@rest_app.get("/diff/{trace_id}", tags=["netconf"])
async def get_diff(trace_id: str):
    stored = _trace_store.get(trace_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"trace_id '{trace_id}' が見つかりません")
    deploy_result = stored.get("deploy_result")
    return {
        "trace_id":    trace_id,
        "query":       stored.get("query"),
        "is_read":     stored.get("is_read"),
        "executed_at": stored.get("executed_at"),
        "deployed_at": stored.get("deployed_at"),
        "execute_result": stored.get("result"),
        "deploy_result":  deploy_result,
        "status": (deploy_result or stored).get("status", "pending"),
    }


# ── WS /ws/updates ────────────────────────────────────────────────────────────
@rest_app.websocket("/ws/updates")
async def ws_updates(ws: WebSocket):
    await ws.accept()
    trace_id = None
    try:
        data     = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
        msg      = json.loads(data)
        trace_id = msg.get("trace_id")
        if not trace_id:
            await ws.send_text(json.dumps({"error": "trace_id が必要です"}))
            await ws.close(); return
        _ws_clients.setdefault(trace_id, []).append(ws)
        await ws.send_text(json.dumps({"trace_id": trace_id, "log": get_msg("ws_connected")}))
        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"ping": True}))
    except WebSocketDisconnect:
        logger.info(f"[{trace_id}] WS 切断")
    except Exception as e:
        logger.warning(f"[{trace_id}] WS エラー: {e}")
    finally:
        if trace_id and ws in _ws_clients.get(trace_id, []):
            _ws_clients[trace_id].remove(ws)



# ═══════════════════════════════════════════════════════════════════════════════
# ANTA Snapshot REST エンドポイント（Hub 経由 shortcut）
# ═══════════════════════════════════════════════════════════════════════════════

class AntaSnapshotRequest(BaseModel):
    device: DeviceConfig = DeviceConfig()
    query:  str = "事前スナップショットを取得"
    tests:  Optional[List[str]] = None

    class Config:
        arbitrary_types_allowed = True


class AntaPostCheckRequest(BaseModel):
    device:      DeviceConfig = DeviceConfig()
    snapshot_id: str = ""
    query:       str = "事後検証を実行"
    tests:       Optional[List[str]] = None

    class Config:
        arbitrary_types_allowed = True


from typing import Optional as _Opt, List as _List


@rest_app.post("/anta/snapshot", tags=["anta"])
async def anta_snapshot(req: AntaSnapshotRequest):
    """
    ANTA 事前スナップショット取得（Before）。
    設定変更前に呼び出し、返却の snapshot_id を /anta/post_check に渡す。
    """
    trace_id = str(uuid.uuid4())[:8]
    logger.info(f"[{trace_id}] /anta/snapshot: {req.query!r}")

    anta_payload = {
        "action":    "snapshot",
        "query":     req.query,
        "device_ip": req.device.ip,
        "username":  req.device.username,
        "password":  req.device.password,
        "tests":     req.tests,
    }
    try:
        result = await _forward(ANTA_A2A_URL, anta_payload)
    except httpx.ConnectError as e:
        raise HTTPException(status_code=503,
            detail=f"ANTA A2A Server ({ANTA_A2A_URL}) に接続できません: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # ── #1774: _artifact_* キーをトップレベルに透過伝播 ─────────────────────────
    _artifact_passthrough = {k: v for k, v in result.items()
                             if k.startswith("_artifact_")}
    response = {
        "trace_id":    trace_id,
        "route":       "verify",
        "action":      "snapshot",
        "status":      result.get("status", "unknown"),
        "summary":     result.get("summary", ""),
        "snapshot_id": result.get("snapshot_id", ""),
        "timestamp":   result.get("timestamp", ""),
        "categories":  result.get("categories", []),
        "result":      result,
        **_artifact_passthrough,
    }
    from fastapi.responses import Response as _FResp
    return _FResp(
        content=json.dumps(response, ensure_ascii=True),
        media_type="application/json",
    )


@rest_app.post("/anta/post_check", tags=["anta"])
async def anta_post_check(req: AntaPostCheckRequest):
    """
    ANTA 事後検証（Post-Check）。
    設定変更後に呼び出し、snapshot_id の before と現在の after を比較する。
    snapshot_id を省略した場合は verify のみ実行。
    """
    trace_id = str(uuid.uuid4())[:8]
    logger.info(
        f"[{trace_id}] /anta/post_check: {req.query!r} "
        f"snapshot_id={req.snapshot_id!r}"
    )

    anta_payload = {
        "action":      "post_check",
        "query":       req.query,
        "snapshot_id": req.snapshot_id,
        "device_ip":   req.device.ip,
        "username":    req.device.username,
        "password":    req.device.password,
        "tests":       req.tests,
    }
    try:
        result = await _forward(ANTA_A2A_URL, anta_payload)
    except httpx.ConnectError as e:
        raise HTTPException(status_code=503,
            detail=f"ANTA A2A Server ({ANTA_A2A_URL}) に接続できません: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # ── #1774: _artifact_* キーをトップレベルに透過伝播 ─────────────────────────
    _artifact_passthrough = {k: v for k, v in result.items()
                             if k.startswith("_artifact_")}
    response = {
        "trace_id":       trace_id,
        "route":          "verify",
        "action":         "post_check",
        "status":         result.get("status", "unknown"),
        "summary":        result.get("summary", ""),
        "before_snap_id": req.snapshot_id,
        "after_snap_id":  result.get("after_snap_id", ""),
        "new_issues":     result.get("new_issues", []),
        "diff":           result.get("diff", {}),
        "result":         result,
        **_artifact_passthrough,
    }
    from fastapi.responses import Response as _FResp2
    return _FResp2(
        content=json.dumps(response, ensure_ascii=True),
        media_type="application/json",
    )


@rest_app.get("/anta/snapshots", tags=["anta"])
async def anta_list_snapshots():
    """ANTA サーバのスナップショット一覧を取得する。"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{ANTA_A2A_URL}/snapshots")
            return r.json()
    except Exception as e:
        raise HTTPException(status_code=503,
            detail=f"ANTA A2A Server ({ANTA_A2A_URL}) に接続できません: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# A2A + FastAPI をマウントして起動
# ═══════════════════════════════════════════════════════════════════════════════

def build_agent_card() -> AgentCard:
    from a2a.types.a2a_pb2 import AgentInterface
    iface = AgentInterface()
    iface.url = A2A_PUBLIC_URL
    iface.protocol_version = "1.0"
    return AgentCard(
        name="task_decompose A2A Hub v2",
        description=(
            "A2A ルーティングハブ + NiceGUI 向け REST API。\n"
            "  write    → NETCONF A2A  (8001)\n"
            "  read     → eAPI A2A    (8002)\n"
            "  security → XDP A2A    (8003)\n"
            "  verify   → ANTA A2A   (8004) ← NEW\n"
            "REST: /healthz /execute /deploy/{trace_id} /diff/{trace_id} "
            "/anta/snapshot /anta/post_check WS:/ws/updates"
        ),
        supported_interfaces=[iface],
        version=VERSION,
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(),
        skills=[
            AgentSkill(
                id="task_route",
                name="クエリ分類 & A2A ルーティング",
                description=(
                    "write→NETCONF:8001 / read→eAPI:8002 / "
                    "security→XDP:8003 / verify→ANTA:8004 に自動転送"
                ),
                tags=["routing", "hub", "netconf", "eapi", "anta", "verify"],
                examples=[
                    "インターフェースの状態を確認してください",
                    "VLAN 101 を作成してください",
                    "ANTAで事後検証してください",
                ],
            ),
            AgentSkill(
                id="rest_execute",
                name="REST /execute (dry-run)",
                description="NiceGUI から POST /execute で dry-run を実行",
                tags=["rest", "dryrun", "nicegui"],
                examples=['POST /execute {"query": "...", "device": {...}}'],
            ),
            AgentSkill(
                id="security_route",
                name="セキュリティ操作ルーティング",
                description=(
                    "XDP/eBPF Firewall 操作を XDP A2A (8003) に転送する。\n"
                    "操作例: ブロック/遮断/QoS帯域制限/セキュリティ分析/統計参照。\n"
                    "block/unblock/qos_set は人間確認後に実行。"
                ),
                tags=["security", "xdp", "ebpf", "block", "firewall", "ips"],
                examples=[
                    "10.0.1.30 をブロックして",
                    "セキュリティ上の異常を分析して",
                    "QoSポリシー一覧を見せて",
                    "ブロックリストを確認して",
                ],
            ),
            AgentSkill(
                id="anta_verify_route",
                name="ANTA Snapshot 事後検証ルーティング",
                description=(
                    "ANTA Snapshot 検証を ANTA A2A (8004) に転送する。\n"
                    "action: snapshot / verify / compare / post_check\n"
                    "設定変更前後の副作用（ルート消失・エラー増加）を自動検出。\n"
                    "REST: POST /anta/snapshot  POST /anta/post_check"
                ),
                tags=["anta", "snapshot", "verify", "post-check", "compare"],
                examples=[
                    "ANTAでインターフェースを検証して",
                    "事後検証を実行してください",
                    '{"action":"snapshot","query":"設定変更前のスナップショットを取得"}',
                ],
            ),
        ],
    )


def main():
    agent_card      = build_agent_card()
    executor        = TaskDecomposeExecutor()
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )
    # v1.1.0: rest_app に A2A ルートを追加して一本化
    add_a2a_routes_to_fastapi(
        rest_app,
        agent_card_routes=create_agent_card_routes(agent_card),
        jsonrpc_routes=create_jsonrpc_routes(request_handler, rpc_url="/"),
        rest_routes=create_rest_routes(request_handler),
    )
    combined = rest_app

    logger.info("=" * 64)
    logger.info("task_decompose A2A Hub v2 起動 (ANTA Snapshot 対応)")
    logger.info("=" * 64)
    logger.info(f"  Port               : {A2A_PORT}")
    logger.info(f"  A2A endpoint       : POST {A2A_PUBLIC_URL}/")
    logger.info(f"  Agent Card         : {A2A_PUBLIC_URL}/.well-known/agent.json")
    logger.info(f"  REST /healthz      : {A2A_PUBLIC_URL}/healthz")
    logger.info(f"  REST /execute      : {A2A_PUBLIC_URL}/execute")
    logger.info(f"  REST /anta/snapshot: {A2A_PUBLIC_URL}/anta/snapshot")
    logger.info(f"  REST /anta/post_check: {A2A_PUBLIC_URL}/anta/post_check")
    logger.info(f"  NETCONF A2A (write)     : {NETCONF_A2A_URL}")
    logger.info(f"  eAPI A2A    (read)      : {EAPI_A2A_URL}")
    logger.info(f"  XDP A2A  (security)     : {XDP_A2A_URL}")
    logger.info(f"  ANTA A2A  (verify)      : {ANTA_A2A_URL}  ← NEW")
    logger.info(f"  eAPI Config (eapi_config): {EAPI_CONFIG_A2A_URL}  ← NEW")
    logger.info(f"  Routes: write→8001 / read→8002 / security→8003 / verify→8004 / eapi_config→8006")
    log_llm_config("Hub")
    logger.info("=" * 64)

    uvicorn.run(combined, host=A2A_HOST, port=A2A_PORT)


if __name__ == "__main__":
    main()
