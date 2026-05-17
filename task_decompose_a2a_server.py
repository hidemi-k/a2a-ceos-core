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
  python task_decompose_a2a_server_v2.py

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
from a2a.server.apps import A2AStarletteApplication
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.utils import new_agent_text_message
from a2a.types import (
    AgentCard, AgentCapabilities, AgentSkill, UnsupportedOperationError,
)

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

NETCONF_A2A_URL = os.getenv("NETCONF_A2A_URL", "http://localhost:8001")
EAPI_A2A_URL    = os.getenv("EAPI_A2A_URL",    "http://localhost:8002")
EAPI_SDIFF_URL  = os.getenv("EAPI_SDIFF_URL",  "http://localhost:8009")  # session-diff REST
XDP_A2A_URL     = os.getenv("XDP_A2A_URL",     "http://localhost:8003")
ANTA_A2A_URL    = os.getenv("ANTA_A2A_URL",    "http://localhost:8004")  # ANTA Snapshot 検証
HTTP_TIMEOUT    = float(os.getenv("HTTP_TIMEOUT", "120"))


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
    "list","check","status","display","バージョン","version","情報",
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
SECURITY_REQUIRED = [
    # 日本語: XDP/FW操作を示す明示的な単語
    "ブロック", "遮断", "セキュリティ", "ファイアウォール",
    "xdp", "ebpf", "firewall", "acl",
    # ブロック操作の英語表現（drop単体は除外 → "drop list" "drop/block" のみ）
    "block", "drop list", "drop/block", "drop/unblock",
    "ブロックリスト",
    # QoS帯域制限（XDP専用機能）
    "qos", "帯域制限", "rate limit", "rate-limit",
    # security という単語（英語クエリ対応）
    "security",
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
    クエリを verify / security / write / read の4種に分類する。

    判定ロジック:
      1. VERIFY_KEYWORDS   → verify （ANTA検証は最優先）
      2. SECURITY_REQUIRED → security（XDP/FW操作の明示キーワード）
      3. READ + SECURITY_CONTEXT の両方 → read を優先
         （例: "フローの状態を確認" → read, "フローをブロック" → security）
      4. READ / WRITE キーワードで判定
      5. いずれも該当しない → LLMフォールバック（改善プロンプト）
    """
    q = query.lower()

    # 1. ANTA 検証を最優先
    if any(k in q for k in VERIFY_KEYWORDS):
        return "verify"

    # 2. XDP/FW 明示キーワードがあれば security 確定
    if any(k in q for k in SECURITY_REQUIRED):
        return "security"

    # 3. READ キーワードがある場合は、脅威系キーワードがあっても read を優先
    #    （"フローの状態を確認" / "パケット統計を見せて" → read）
    has_read  = any(k in q for k in READ_KEYWORDS)
    has_write = any(k in q for k in WRITE_KEYWORDS)

    if has_read and not has_write:  return "read"
    if has_write and not has_read:  return "write"
    if has_read and has_write:      return "read"   # 競合時は参照を優先

    # 4. 脅威系のみ（READ/WRITE どちらもなし）→ security
    if any(k in q for k in SECURITY_CONTEXT_KEYWORDS):
        return "security"

    # 5. LLM フォールバック（改善プロンプト）
    result = invoke_with_fallback(
        llm,
        "あなたはネットワーク操作の分類器です。\n"
        "以下の質問を read / write / security / verify のいずれか一単語で分類してください。\n\n"
        "【分類基準】\n"
        "  read     = デバイスの状態参照・確認（NTP/BGP/インターフェース/ルーティング等）\n"
        "  write    = デバイスへの設定変更・追加・削除\n"
        "  security = XDP/eBPF Firewall操作・ブロック・遮断・QoS帯域制限\n"
        "  verify   = ANTA/スナップショット/事後検証\n\n"
        "【重要】NTP・BGP・OSPF・インターフェース・ルート等のデバイス参照は必ず read。\n"
        "security に分類するのは XDP/ファイアウォール/ブロック/遮断 の操作のみ。\n\n"
        f"質問: {query}\n"
        "回答（一単語のみ）:"
    ).strip().lower()

    if "write"    in result: return "write"
    if "security" in result: return "security"
    if "verify"   in result: return "verify"
    return "read"


# ═══════════════════════════════════════════════════════════════════════════════
# A2A 通信ユーティリティ
# ═══════════════════════════════════════════════════════════════════════════════

def _make_a2a_request(payload: dict, msg_id: str = None) -> dict:
    mid = msg_id or f"hub-{datetime.now().strftime('%H%M%S%f')}"
    return {
        "jsonrpc": "2.0", "id": mid, "method": "message/send",
        "params": {"message": {
            "role": "user",
            "parts": [{"kind": "text",
                       "text": json.dumps(payload, ensure_ascii=False)}],
            "messageId": mid,
        }},
    }


def _extract_text(a2a_resp: dict) -> dict:
    """A2A レスポンスからテキストを取り出し JSON として返す。ネスト展開付き。"""
    try:
        parts = a2a_resp.get("result", {}).get("parts", [])
        if not parts:
            parts = a2a_resp.get("result", {}).get("message", {}).get("parts", [])
        for part in parts:
            if part.get("kind") == "text":
                try:
                    parsed = json.loads(part["text"])
                except json.JSONDecodeError:
                    return {"_raw_text": part["text"]}
                # Hub 経由のネスト展開
                inner = parsed.get("result")
                if (isinstance(inner, dict)
                        and inner.get("jsonrpc") == "2.0"
                        and "result" in inner):
                    parsed["result"] = _extract_text(inner)
                return parsed
        return {"_raw_a2a": a2a_resp}
    except Exception as e:
        return {"_parse_error": str(e)}


async def _forward(target_url: str, payload: dict) -> dict:
    a2a_req = _make_a2a_request(payload)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(target_url, json=a2a_req)
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
        raw_text = "".join(
            part.root.text for part in context.message.parts
            if hasattr(part.root, "text")
        )
        if not raw_text.strip():
            await event_queue.enqueue_event(new_agent_text_message(get_msg("ws_empty")))
            return

        params    = self._parse_request(raw_text)
        query     = params.get("query", raw_text)
        device_ip = params.get("device_ip")
        username  = params.get("username")
        password  = params.get("password")
        port      = params.get("port", "830")
        deploy    = params.get("deploy", False)

        logger.info(f"[A2A] 受信: {query[:80]} deploy={deploy}")
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
                "port": port, "deploy": deploy,
            }
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

        await event_queue.enqueue_event(
            new_agent_text_message(json.dumps(result, ensure_ascii=False, indent=2)))

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
                            ("anta",    ANTA_A2A_URL)]:
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
            "write":    NETCONF_A2A_URL,
            "read":     EAPI_A2A_URL,
            "security": XDP_A2A_URL,
            "verify":   ANTA_A2A_URL,
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


# ═══════════════════════════════════════════════════════════════════════════════
# 混合クエリ分割（read + write が同一クエリに共存する場合）
# ═══════════════════════════════════════════════════════════════════════════════

def split_mixed_query(query: str) -> list:
    """
    混合クエリ（read + write が混在）を LLM でサブクエリ配列に分割する。

    例:
      "VLANの状態を確認して、VLAN ID 103 の DEV3_VLAN を作成して"
      → [{"query": "VLANの状態を確認して",           "route": "read"},
         {"query": "VLAN ID 103 の DEV3_VLAN を作成して", "route": "write"}]

    分割不要（単一ルート）の場合は空リスト [] を返す。
    LLM パース失敗時も空リストを返し、呼び出し元が通常フローへフォールバックする。
    """
    result = invoke_with_fallback(
        llm,
        "あなたはネットワーク操作の分類器です。\n"
        "以下のクエリに read（参照）と write（設定変更）の両方の操作が含まれる場合、\n"
        "それぞれを独立したサブクエリに分割し、JSON 配列のみで返してください。\n"
        "単一操作のみの場合は空配列 [] を返してください。\n\n"
        "【出力形式】JSON 配列のみ。前後の説明文・コードブロック記号は不要。\n"
        '例: [{"query": "VLANの状態を確認して", "route": "read"}, '
        '{"query": "VLAN ID 103 の DEV3_VLAN を作成して", "route": "write"}]\n\n'
        "【分類基準】\n"
        "  read  = デバイスの状態参照・確認（show コマンド相当）\n"
        "  write = デバイスへの設定変更・追加・削除\n\n"
        f"クエリ: {query}\n"
        "回答（JSON のみ）:"
    ).strip()

    try:
        cleaned = re.sub(r"```(?:json)?|```", "", result).strip()
        parsed  = json.loads(cleaned)
        if isinstance(parsed, list) and len(parsed) >= 2:
            logger.info(f"split_mixed_query: {len(parsed)} サブクエリに分割")
            return parsed
    except Exception as e:
        logger.warning(f"split_mixed_query parse error: {e} / raw={result[:200]}")
    return []


async def _execute_mixed(
    trace_id: str,
    sub_queries: list,
    req,           # ExecuteRequest
) -> "Response":
    """
    混合クエリのサブクエリを順次実行してマージしたレスポンスを返す。

    実行順序:
      read サブクエリ  → eAPI A2A (8002)  に deploy=True  で即時実行
      write サブクエリ → NETCONF A2A (8001) に deploy=False で dry-run

    /deploy フローとの互換:
      write サブクエリの final_xml / session_diff を trace_store に保存するため、
      承認 → /deploy/{trace_id} → 実機投入 の既存フローがそのまま使える。

    複数 write サブクエリがある場合は最後の XML を採用する（将来的にはマージ）。
    """
    from fastapi.responses import Response

    EAPI_DEFAULT_PORT = int(os.getenv("EAPI_PORT_NUM", "443"))
    EAPI_DIFF_PORT    = int(os.getenv("EAPI_PORT_NUM", "443"))
    EAPI_TRANSPORT    = os.getenv("EAPI_TRANSPORT", "https")

    results         = []
    xml_out         = ""
    session_diff    = {}
    overall_status  = "success"

    for i, sq in enumerate(sub_queries):
        sub_q     = sq.get("query", "")
        sub_route = sq.get("route", classify_query(sub_q))
        is_read   = (sub_route == "read")
        logger.info(
            f"[{trace_id}] 混合サブクエリ {i+1}/{len(sub_queries)}: "
            f"route={sub_route} query={sub_q[:60]!r}"
        )

        payload = {
            "query":     sub_q,
            "device_ip": req.device.ip,
            "username":  req.device.username,
            "password":  req.device.password,
            "port":      str(EAPI_DEFAULT_PORT) if is_read else req.device.port,
            "deploy":    is_read,
        }
        target_url = EAPI_A2A_URL if is_read else NETCONF_A2A_URL

        sub_result: dict = {}
        for attempt in range(2):
            try:
                sub_result = await _forward(target_url, payload)
                break
            except Exception as e:
                logger.warning(
                    f"[{trace_id}] サブクエリ {i+1} attempt {attempt+1} 失敗: {e}"
                )
                if attempt < 1:
                    await asyncio.sleep(2.0)
                else:
                    sub_result = {"status": "error", "summary": str(e)}
                    overall_status = "partial_error"

        # read 結果を safe 形式に整形
        if is_read:
            sub_safe = {
                "query":          sub_result.get("query", sub_q),
                "cmds":           sub_result.get("cmds", []),
                "status":         sub_result.get("status", "unknown"),
                "summary":        sub_result.get("summary", ""),
                "formatted_text": sub_result.get("formatted_text", ""),
                "formatted":      sub_result.get("formatted", ""),
            }
        else:
            sub_safe = sub_result

        results.append({
            "index":          i + 1,
            "query":          sub_q,
            "route":          sub_route,
            "status":         sub_result.get("status", "unknown"),
            "summary":        sub_result.get("summary", ""),
            "formatted_text": sub_result.get("formatted_text", ""),
            "formatted":      sub_result.get("formatted", ""),
            "result":         sub_safe,
        })

        # write サブクエリの XML を収集（最後の write を採用）
        if not is_read:
            raw = sub_result.get("result", sub_result)
            _xml = (
                sub_result.get("final_xml", "")
                or sub_result.get("generated_xml", "")
                or raw.get("final_xml", "")
                or raw.get("generated_xml", "")
            )
            if not _xml:
                for ts in sub_result.get("task_summaries", []):
                    _fx = ts.get("final_xml") or ts.get("generated_xml", "")
                    if _fx:
                        _xml = _fx
                        break
            if _xml:
                xml_out = _xml

            # session diff（write サブクエリの XML がある場合のみ）
            if xml_out:
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
                            session_diff = diff_resp.json()
                        else:
                            session_diff = {
                                "status": "error",
                                "message": f"HTTP {diff_resp.status_code}",
                                "diff_lines": [], "diff_text": "",
                            }
                except Exception as e:
                    logger.warning(f"[{trace_id}] 混合 session diff スキップ: {e}")
                    session_diff = {
                        "status": "skipped",
                        "message": f"session diff 取得失敗: {e}",
                        "diff_lines": [], "diff_text": "",
                    }

    has_write = any(sq.get("route") == "write" for sq in sub_queries)
    response = {
        "trace_id":       trace_id,
        "route":          "mixed",
        "is_read":        not has_write,  # write があれば deploy 可能
        "status":         overall_status,
        "summary":        f"混合クエリ {len(sub_queries)} 件実行完了",
        "task_summaries": results,        # UI の既存 task_summaries 表示と互換
        "xml":            xml_out,
        "session_diff":   session_diff,
        "result":         {"task_summaries": results},
    }

    _trace_store[trace_id] = {
        **response,
        "device":      req.device.model_dump(),
        "query":       req.query,
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(
        f"[{trace_id}] 混合クエリ完了: status={overall_status} "
        f"sub={len(sub_queries)} has_write={has_write}"
    )
    return Response(
        content=json.dumps(response, ensure_ascii=True),
        media_type="application/json",
    )


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

    # ── 混合クエリ検出: classify_query が read を返したが WRITE_KEYWORDS も含む場合 ──
    # 例: "VLANの状態を確認して、VLAN 103 を作成して"
    #     → classify_query は read/write 競合で "read" を返すが、
    #       WRITE_KEYWORDS が含まれるため LLM で分割を試みる
    if route == "read":
        _q_lower = req.query.lower()
        if any(k in _q_lower for k in WRITE_KEYWORDS):
            _sub_queries = split_mixed_query(req.query)
            if _sub_queries:
                logger.info(
                    f"[{trace_id}] 混合クエリ検出: "
                    f"{len(_sub_queries)} サブクエリに分割して順次実行"
                )
                return await _execute_mixed(trace_id, _sub_queries, req)

    is_read = (route == "read")
    # eAPI と NETCONF でポートを使い分ける
    # eAPI: HTTPS/443（実機確認済み。NETCONF port 830 を渡すと SSL エラーになる）
    # NETCONF: req.device.port をそのまま使う（デフォルト 830）
    EAPI_DEFAULT_PORT = int(os.getenv("EAPI_PORT_NUM", "443"))
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
                                         f"http://localhost:{os.getenv('EAPI_PORT','8002')}")
            EAPI_DIFF_PORT  = int(os.getenv("EAPI_PORT_NUM", "443"))
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

    response = {
        "trace_id":     trace_id,
        "route":        route,
        "is_read":      is_read,
        "status":       result.get("status") or result.get("overall_status", "unknown"),
        "summary":      result.get("summary", ""),
        "xml":          xml_out,
        "session_diff": session_diff_result,   # ★ 事前 diff（新規追加）
        "result":       safe_result,
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

    # ── mixed ルート: dry-run で生成済みの XML を NETCONF に直接投入 ────────────
    # /execute(_execute_mixed) が生成した final_xml を使い、
    # クエリ全体を NETCONF に再送しない（task_decomposer の再分解を避ける）。
    if stored.get("route") == "mixed":
        xml_to_deploy = stored.get("xml", "")
        if not xml_to_deploy:
            raise HTTPException(
                status_code=400,
                detail="混合クエリの dry-run XML が見つかりません。先に /execute を実行してください。",
            )

        # write サブクエリのクエリ文字列を復元（task_summaries から）
        write_query = " / ".join(
            s.get("query", "")
            for s in stored.get("task_summaries", [])
            if s.get("route") == "write"
        ) or query

        mixed_payload = {
            "query":     write_query,
            "xml":       xml_to_deploy,   # dry-run 済み XML を直接渡す
            "device_ip": req.device.ip,
            "username":  req.device.username,
            "password":  req.device.password,
            "port":      req.device.port,
            "deploy":    True,
        }
        logger.info(
            f"[{trace_id}] mixed /deploy: XML {len(xml_to_deploy)} chars → NETCONF 8001"
        )
        try:
            result = await _forward(NETCONF_A2A_URL, mixed_payload)
        except Exception as e:
            logger.error(f"[{trace_id}] mixed deploy エラー: {e}")
            await _push_log(trace_id, get_msg("deploy_failed") + f": {e}")
            raise HTTPException(status_code=500, detail=str(e))

        response = {
            "trace_id": trace_id,
            "route":    "mixed",
            "status":   result.get("overall_status", result.get("status", "unknown")),
            "summary":  result.get("summary", ""),
            "result":   result,
            "audit_scope_note": get_msg("audit_scope_note"),
        }
        _trace_store[trace_id]["deploy_result"] = response
        _trace_store[trace_id]["deployed_at"]   = datetime.now(timezone.utc).isoformat()

        await _push_log(trace_id, get_msg("deploy_done") + f": {response['status']}")
        logger.info(f"[{trace_id}] mixed /deploy 完了: status={response['status']}")

        snap_id  = req.snapshot_id.strip()
        _ok_statuses = ("success", "all_success", "dry_run", "no_changes")
        deploy_ok = any(s in response["status"] for s in _ok_statuses)
        if snap_id and deploy_ok:
            asyncio.create_task(_auto_post_check(trace_id, snap_id, req.device))
            response["anta_post_check"] = "running"
        else:
            response["anta_post_check"] = "skipped"

        from fastapi.responses import Response as _MR
        return _MR(
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
        url=A2A_PUBLIC_URL,
        version=VERSION,
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        capabilities=AgentCapabilities(streaming=False),
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
    executor        = TaskDecomposeExecutor()
    request_handler = DefaultRequestHandler(
        agent_executor=executor, task_store=InMemoryTaskStore())
    a2a_app = A2AStarletteApplication(
        agent_card=build_agent_card(), http_handler=request_handler).build()

    # A2A ルートを REST アプリにマウント
    # A2A: POST / と GET /.well-known/agent.json
    # REST: /healthz /execute /deploy /diff /ws
    # → A2A の / は REST の /a2a/ 以下にマウントし、
    #   /.well-known/ は REST に直接追加する方式が最もシンプル
    # ここでは uvicorn で REST アプリをメインとし、
    # A2A エンドポイントを REST アプリに追加する

    from starlette.applications import Starlette
    from starlette.routing import Mount

    # A2A ルートを /a2a プレフィックスなしで REST に統合
    # /.well-known/agent.json と POST / は A2A が使う
    # /healthz /execute 等は FastAPI が使う
    # → 両方を Starlette の routes にマージ

    from starlette.middleware.cors import CORSMiddleware as StarletteCORSMiddleware

    combined = Starlette(routes=[
        # A2A: POST / (JSON-RPC)
        Route("/",          endpoint=a2a_app, methods=["POST"]),
        # A2A: Agent Card
        Route("/.well-known/agent.json",
              endpoint=a2a_app, methods=["GET"]),
        # REST: FastAPI を /api 以下にマウント
        Mount("/api", app=rest_app),
        # REST: / 以下を FastAPI に直接マウント（/healthz /execute 等）
        Mount("/", app=rest_app),
    ])

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
    logger.info(f"  NETCONF A2A (write): {NETCONF_A2A_URL}")
    logger.info(f"  eAPI A2A    (read) : {EAPI_A2A_URL}")
    logger.info(f"  XDP A2A  (security): {XDP_A2A_URL}")
    logger.info(f"  ANTA A2A  (verify) : {ANTA_A2A_URL}  ← NEW")
    logger.info(f"  Routes: write→8001 / read→8002 / security→8003 / verify→8004")
    log_llm_config("Hub")
    logger.info("=" * 64)

    uvicorn.run(combined, host=A2A_HOST, port=A2A_PORT)


if __name__ == "__main__":
    main()
