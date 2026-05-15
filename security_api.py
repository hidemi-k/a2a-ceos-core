#!/usr/bin/env python3
# Copyright (c) 2026 hidemi-k
# Licensed under the MIT License.
"""
security_api.py — Security FastAPI Router
==========================================
api_server.py に include_router で追加するセキュリティ系エンドポイント。

NiceGUI (app.py) からのポーリング・操作を受け付け、
A2A Security Agent (:8002) または Go Agent (:8080) に転送する。

エンドポイント:
  GET  /security/stats           統計サマリ（pps差分・フロー数・SYNスパイク）
  GET  /security/top             上位フロー一覧（UI チャート用）
  GET  /security/drop/list       DROP_LIST（XDP_DROP 中ルール）
  GET  /security/qos/list        QOS_MAP（レート制限中 IP）
  GET  /security/info            XDP デバイス情報
  POST /security/drop/block      手動ブロック（Human-in-the-loop）
  POST /security/drop/unblock    遮断解除（Human-in-the-loop）
  POST /security/agent/query     Security A2A Agent への自然言語クエリ転送
  WS   /ws/security              攻撃検知イベントのリアルタイム push

api_server.py への組み込み:
  from security_api import security_router, start_security_monitor
  app.include_router(security_router)
  # startup イベント内で:
  asyncio.create_task(start_security_monitor())

依存:
  pip install httpx
  sase_agent.py（同ディレクトリに存在すること）
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

logger = logging.getLogger("security_api")

# ── 環境変数 ──────────────────────────────────────────────────────────────────
SASE_API_URL    = os.getenv("SASE_API_URL",    "http://localhost:8080")
SECURITY_A2A_URL = os.getenv("SECURITY_A2A_URL", "http://localhost:8002")

# Human-in-the-loop: block/unblock は confirmed=true が必要
REQUIRE_CONFIRM = os.getenv("SECURITY_REQUIRE_CONFIRM", "true").lower() == "true"

# 監視ポーリング間隔（秒）
MONITOR_INTERVAL = int(os.getenv("SECURITY_MONITOR_INTERVAL", "3"))

# ── FastAPI ルーター ──────────────────────────────────────────────────────────
security_router = APIRouter(prefix="/security", tags=["security"])

# ── WebSocket 接続管理 ────────────────────────────────────────────────────────
_security_ws_clients: List[WebSocket] = []

# ── pps 差分計算用キャッシュ ──────────────────────────────────────────────────
_prev_packets: Dict[str, int] = {}   # "ip:port:proto" → packets
_prev_check_ts: float = 0.0

# ── 攻撃検知ログ（最新 50 件、UI の threat log 用）─────────────────────────
_threat_log: List[Dict] = []         # [{time, ip, kind, desc, status}, ...]
MAX_THREAT_LOG = 50

# ── Reasoning ログ（最新 5 件、UI の Reasoning セクション用）────────────────
_reasoning_log: List[Dict] = []
MAX_REASONING_LOG = 5


# ══════════════════════════════════════════════════════════════════════════════
# 内部ユーティリティ
# ══════════════════════════════════════════════════════════════════════════════

async def _go_get(path: str, params: dict = None) -> Any:
    """Go Agent への GET リクエスト（非同期）"""
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{SASE_API_URL}{path}", params=params)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return r.text


async def _go_get_text(path: str, params: dict = None) -> str:
    """Go Agent への GET（テキストレスポンス）"""
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{SASE_API_URL}{path}", params=params)
        r.raise_for_status()
        return r.text


def _push_threat(ip: str, kind: str, desc: str, status: str):
    """脅威ログに追記（最新 MAX_THREAT_LOG 件を保持）"""
    entry = {
        "time":   datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "ip":     ip,
        "kind":   kind,
        "desc":   desc,
        "status": status,
    }
    _threat_log.append(entry)
    if len(_threat_log) > MAX_THREAT_LOG:
        _threat_log.pop(0)
    return entry


def _push_reasoning(ip: str, steps: List[str], action: str):
    """Reasoning ログに追記"""
    entry = {
        "time":   datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "ip":     ip,
        "steps":  steps,
        "action": action,
    }
    _reasoning_log.append(entry)
    if len(_reasoning_log) > MAX_REASONING_LOG:
        _reasoning_log.pop(0)
    return entry


async def _broadcast_security_event(event: dict):
    """全 WebSocket クライアントにイベントを push"""
    dead = []
    for ws in _security_ws_clients:
        try:
            await ws.send_text(json.dumps(event, ensure_ascii=False))
        except Exception:
            dead.append(ws)
    for ws in dead:
        _security_ws_clients.remove(ws)


def _build_stats_summary(stats: list) -> dict:
    """
    /stats の生データから UI 向け集計値を算出。
    pps は前回差分 / 経過時間で計算。
    """
    global _prev_packets, _prev_check_ts

    now = time.time()
    elapsed = max(1.0, now - _prev_check_ts) if _prev_check_ts else MONITOR_INTERVAL
    _prev_check_ts = now

    total_pkts = 0
    total_prev = 0
    syn_spike_ips: List[str] = []
    total_bytes = 0
    drop_count_pkts = 0

    for flow in stats:
        s   = flow.get("stats", {})
        key = f"{flow['ip']}:{flow['port']}:{flow['protocol']}"
        pkts = s.get("packets", 0)
        total_pkts += pkts
        total_prev += _prev_packets.get(key, pkts)
        _prev_packets[key] = pkts

        total_bytes    += s.get("bytes", 0)
        drop_count_pkts += s.get("dropped_packets", 0)

        # SYN スパイク簡易判定（api_spec.py 準拠）
        syn = s.get("syn_packets", 0)
        ack = s.get("ack_packets", 0)
        rst = s.get("rst_packets", 0)
        ack_ratio = ack / (syn + 1)
        rst_ratio = rst / (syn + 1)
        is_spike = syn > 1000 and ack_ratio < 0.1 and (rst_ratio >= 0.3 or rst == 0)
        if is_spike and flow["ip"] not in syn_spike_ips:
            syn_spike_ips.append(flow["ip"])

    pps = int(max(0, total_pkts - total_prev) / elapsed)

    return {
        "pps":              pps,
        "active_flows":     len(stats),
        "total_bytes":      total_bytes,
        "dropped_packets":  drop_count_pkts,
        "syn_spike_ips":    syn_spike_ips,
        "syn_spike_count":  len(syn_spike_ips),
        "timestamp":        datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# リクエスト / レスポンス スキーマ
# ══════════════════════════════════════════════════════════════════════════════

class BlockRequest(BaseModel):
    ip:        str
    proto:     str = "tcp"
    port:      int
    confirmed: bool = False          # Human-in-the-loop

class UnblockRequest(BaseModel):
    ip:        str
    proto:     str = "tcp"
    port:      int
    confirmed: bool = False

class AgentQueryRequest(BaseModel):
    query:     str
    skill:     str = "fw_analyze"    # fw_analyze / fw_block / fw_unblock / fw_status / fw_mitigate
    confirmed: bool = False
    # fw_block / fw_unblock / fw_mitigate 用
    ip:        Optional[str] = None
    proto:     Optional[str] = "tcp"
    port:      Optional[int] = None
    limit:     Optional[int] = None  # fw_mitigate 用


# ══════════════════════════════════════════════════════════════════════════════
# REST エンドポイント
# ══════════════════════════════════════════════════════════════════════════════

@security_router.get("/stats", summary="統計サマリ（pps・フロー数・SYNスパイク）")
async def get_stats():
    """
    Go Agent /stats を取得してUI向け集計値に変換して返す。
    VM側で差分計算を完結させることで UI は描画のみに専念できる。
    """
    try:
        stats = await _go_get("/stats")
        if not isinstance(stats, list):
            stats = []
    except Exception as e:
        logger.warning("Go Agent /stats 取得失敗: %s", e)
        stats = []

    summary = _build_stats_summary(stats)

    # DROP_LIST の数も付加
    try:
        drop_list = await _go_get("/drop/list")
        drop_count = len(drop_list) if isinstance(drop_list, dict) else 0
    except Exception:
        drop_count = 0

    try:
        qos_list = await _go_get("/qos/list")
        qos_count = len(qos_list) if isinstance(qos_list, dict) else 0
    except Exception:
        qos_count = 0

    return {
        **summary,
        "drop_rule_count": drop_count,
        "qos_rule_count":  qos_count,
        "threat_log":      list(reversed(_threat_log[-10:])),   # 最新10件
        "reasoning_log":   list(reversed(_reasoning_log[-3:])),  # 最新3件
    }


@security_router.get("/top", summary="上位フロー一覧（チャート用）")
async def get_top():
    """Go Agent /top を返す。UIチャートのデータソース。"""
    try:
        return await _go_get("/top")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Go Agent /top 取得失敗: {e}")


@security_router.get("/drop/list", summary="DROP_LIST（XDP_DROP 中ルール）")
async def get_drop_list():
    try:
        return await _go_get("/drop/list")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Go Agent /drop/list 取得失敗: {e}")


@security_router.get("/qos/list", summary="QOS_MAP（レート制限中 IP）")
async def get_qos_list():
    try:
        return await _go_get("/qos/list")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Go Agent /qos/list 取得失敗: {e}")


@security_router.get("/info", summary="XDP デバイス情報")
async def get_info():
    try:
        return await _go_get("/info")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Go Agent /info 取得失敗: {e}")


@security_router.post("/drop/block", summary="手動ブロック（Human-in-the-loop）")
async def drop_block(req: BlockRequest):
    """
    Human-in-the-loop: confirmed=true が必要。
    confirmed=false の場合は pending_confirmation を返す。
    """
    if REQUIRE_CONFIRM and not req.confirmed:
        return {
            "status":  "pending_confirmation",
            "message": f"{req.ip}:{req.port}/{req.proto} を XDP_DROP で遮断します。confirmed=true で再送してください",
        }
    try:
        result = await _go_get_text(
            "/drop/block",
            {"ip": req.ip, "proto": req.proto, "port": req.port},
        )
        entry = _push_threat(
            req.ip,
            "手動ブロック",
            f"{req.proto}:{req.port} 手動遮断",
            "XDP_DROP",
        )
        await _broadcast_security_event({"type": "block", **entry})
        return {"status": "executed", "result": result}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ブロック失敗: {e}")


@security_router.post("/drop/unblock", summary="遮断解除（Human-in-the-loop）")
async def drop_unblock(req: UnblockRequest):
    if REQUIRE_CONFIRM and not req.confirmed:
        return {
            "status":  "pending_confirmation",
            "message": f"{req.ip}:{req.port}/{req.proto} の遮断を解除します。confirmed=true で再送してください",
        }
    try:
        result = await _go_get_text(
            "/drop/unblock",
            {"ip": req.ip, "proto": req.proto, "port": req.port},
        )
        entry = _push_threat(req.ip, "遮断解除", f"{req.proto}:{req.port} 解除", "Restored")
        await _broadcast_security_event({"type": "unblock", **entry})
        return {"status": "executed", "result": result}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"解除失敗: {e}")


@security_router.post("/agent/query", summary="Security A2A Agent への自然言語クエリ")
async def agent_query(req: AgentQueryRequest):
    """
    Security A2A Agent (:8002) に JSON で転送して結果を返す。
    A2A Agent が未起動の場合は Go Agent に直接フォールバック。
    """
    payload = {
        "skill":     req.skill,
        "query":     req.query,
        "confirmed": req.confirmed,
    }
    if req.ip:    payload["ip"]    = req.ip
    if req.port:  payload["port"]  = req.port
    if req.proto: payload["proto"] = req.proto
    if req.limit: payload["limit"] = req.limit

    # A2A Agent に転送を試みる
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{SECURITY_A2A_URL}/",
                json={
                    "jsonrpc": "2.0",
                    "method": "tasks/send",
                    "id": 1,
                    "params": {
                        "message": {
                            "role": "user",
                            "parts": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
                        }
                    }
                },
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
            a2a_resp = r.json()

        # A2A レスポンスからテキスト部分を抽出
        text = ""
        try:
            parts = (
                a2a_resp.get("result", {})
                .get("status", {})
                .get("message", {})
                .get("parts", [])
            )
            for part in parts:
                if part.get("type") == "text":
                    text += part.get("text", "")
        except Exception:
            text = str(a2a_resp)

        # Reasoning ログを保存（fw_analyze の場合）
        try:
            result_data = json.loads(text)
            if result_data.get("skill") == "fw_analyze" and result_data.get("reasoning"):
                _push_reasoning(
                    ip=payload.get("ip", "複数IP"),
                    steps=_parse_reasoning_steps(result_data["reasoning"]),
                    action=str(result_data.get("proposed_actions", [])),
                )
            # 実行済みアクションを脅威ログに記録
            for act in result_data.get("executed_actions", []):
                _parse_and_log_action(act.get("action", ""), act.get("result", ""))
        except Exception:
            pass

        await _broadcast_security_event({
            "type":    "agent_response",
            "skill":   req.skill,
            "summary": text[:200],
        })

        return {"status": "ok", "response": text}

    except (httpx.ConnectError, httpx.TimeoutException) as e:
        # A2A Agent 未起動時のフォールバック: fw_status を直接返す
        logger.warning("Security A2A Agent 未起動、フォールバック: %s", e)
        try:
            drop = await _go_get("/drop/list")
            qos  = await _go_get("/qos/list")
            return {
                "status":   "fallback",
                "message":  "Security A2A Agent に接続できませんでした。Go Agent から直接取得しました。",
                "drop_list": drop,
                "qos_list":  qos,
            }
        except Exception as e2:
            raise HTTPException(status_code=502, detail=f"Go Agent フォールバックも失敗: {e2}")


def _parse_reasoning_steps(reasoning_text: str) -> List[str]:
    """LLM の reasoning テキストを箇条書きステップに分割（最大 5 件）"""
    lines = [
        ln.strip().lstrip("・-*0123456789. ")
        for ln in reasoning_text.split("\n")
        if ln.strip() and len(ln.strip()) > 10
    ]
    return lines[:5]


def _parse_and_log_action(action_str: str, result_str: str):
    """実行済みアクション文字列から IP を抽出して脅威ログに追記"""
    ip_m = re.search(r"ip=(\d+\.\d+\.\d+\.\d+)", action_str)
    if not ip_m:
        return
    ip = ip_m.group(1)
    kind   = "XDP_DROP" if "drop_block" in action_str else "QoS"
    status = "executed" if "error" not in result_str.lower() else "error"
    _push_threat(ip, f"AI自動{kind}", action_str[:60], status)


# ══════════════════════════════════════════════════════════════════════════════
# WebSocket: リアルタイム イベント push
# ══════════════════════════════════════════════════════════════════════════════

@security_router.websocket("/ws/security")
async def ws_security(ws: WebSocket):
    """
    攻撃検知・回復・手動操作イベントをリアルタイムで push するチャネル。
    app.py の ui.timer に代わる「イベント駆動」更新源として使用可能。
    接続後は ping を 20 秒ごとに送信して死活を維持する。
    """
    await ws.accept()
    _security_ws_clients.append(ws)
    logger.info("Security WS 接続: %s (合計%d)", ws.client, len(_security_ws_clients))

    try:
        # 接続直後に現在の状態をスナップショットとして push
        await ws.send_text(json.dumps({
            "type":    "connected",
            "message": "Security WebSocket 接続完了",
            "threat_log": list(reversed(_threat_log[-5:])),
        }, ensure_ascii=False))

        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=20.0)
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"type": "ping"}))

    except WebSocketDisconnect:
        logger.info("Security WS 切断: %s", ws.client)
    except Exception as e:
        logger.warning("Security WS エラー: %s", e)
    finally:
        if ws in _security_ws_clients:
            _security_ws_clients.remove(ws)


# ══════════════════════════════════════════════════════════════════════════════
# バックグラウンド監視タスク
# ══════════════════════════════════════════════════════════════════════════════

async def start_security_monitor():
    """
    MONITOR_INTERVAL 秒ごとに Go Agent をポーリングし、
    攻撃検知・回復イベントを WebSocket クライアントに push する。

    api_server.py の startup イベントで以下のように起動する:
        asyncio.create_task(start_security_monitor())

    検知ロジック:
      - SYN スパイク（syn_packets > 1000, ack_ratio < 0.1）
      - QoS 自動ミティゲーション適用・解除（Go Agent のログを QOS_MAP で検知）
      - DROP_LIST の増減（手動ブロック / 解除）
    """
    logger.info("Security monitor 開始 (interval=%ds)", MONITOR_INTERVAL)

    prev_drop_keys: set = set()
    prev_qos_keys:  set = set()
    prev_syn_spikes: set = set()

    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        try:
            # ── 1. SYN スパイク検知 ────────────────────────────────────
            try:
                stats = await _go_get("/stats")
                if not isinstance(stats, list):
                    stats = []
            except Exception:
                stats = []

            current_spikes: set = set()
            for flow in stats:
                s   = flow.get("stats", {})
                syn = s.get("syn_packets", 0)
                ack = s.get("ack_packets", 0)
                rst = s.get("rst_packets", 0)
                ack_ratio = ack / (syn + 1)
                rst_ratio = rst / (syn + 1)
                if syn > 1000 and ack_ratio < 0.1 and (rst_ratio >= 0.3 or rst == 0):
                    current_spikes.add(flow["ip"])

            # 新規スパイク
            for ip in current_spikes - prev_syn_spikes:
                entry = _push_threat(ip, "SYNスパイク",
                                     "大量SYN検知 → 自動ミティゲーション候補", "Detected")
                await _broadcast_security_event({"type": "syn_spike", **entry})
                # Chat タブへの通知イベント（app.py 側で購読して自動投稿）
                await _broadcast_security_event({
                    "type":    "chat_alert",
                    "message": f"🚨 {ip} から SYN スパイクを検知しました。Security タブを確認してください。",
                })
                logger.warning("[monitor] SYN スパイク検知: %s", ip)

            # スパイク解消（回復）
            for ip in prev_syn_spikes - current_spikes:
                entry = _push_threat(ip, "回復", "SYN スパイク解消", "Restored")
                await _broadcast_security_event({"type": "syn_resolved", **entry})

            prev_syn_spikes = current_spikes

            # ── 2. QoS ミティゲーション適用・解除 ────────────────────
            try:
                qos_list = await _go_get("/qos/list")
                if not isinstance(qos_list, dict):
                    qos_list = {}
            except Exception:
                qos_list = {}

            current_qos_keys = set(qos_list.keys())

            # 新規ミティゲーション
            for ip in current_qos_keys - prev_qos_keys:
                info = qos_list.get(ip, {})
                limit = info.get("limit_bytes_per_sec", 10000)
                entry = _push_threat(
                    ip, "QoS ミティゲーション",
                    f"レート制限 {limit} B/s 適用",
                    "Mitigated",
                )
                await _broadcast_security_event({"type": "qos_applied", **entry})

            # ミティゲーション解除
            for ip in prev_qos_keys - current_qos_keys:
                entry = _push_threat(ip, "QoS 解除", "レート制限 解除 (Go Agent 自動回復)", "Restored")
                await _broadcast_security_event({"type": "qos_removed", **entry})
                await _broadcast_security_event({
                    "type":    "chat_alert",
                    "message": f"✅ {ip} の QoS ミティゲーションが解除されました（2分間安定）。",
                })

            prev_qos_keys = current_qos_keys

            # ── 3. DROP_LIST 変化検知 ─────────────────────────────────
            try:
                drop_list = await _go_get("/drop/list")
                if not isinstance(drop_list, dict):
                    drop_list = {}
            except Exception:
                drop_list = {}

            current_drop_keys = set(drop_list.keys())

            for key in current_drop_keys - prev_drop_keys:
                ip = key.split(":")[0]
                entry = _push_threat(ip, "DROP_LIST追加", f"{key} 遮断開始", "XDP_DROP")
                await _broadcast_security_event({"type": "drop_added", **entry})

            for key in prev_drop_keys - current_drop_keys:
                ip = key.split(":")[0]
                entry = _push_threat(ip, "DROP_LIST削除", f"{key} 遮断解除", "Restored")
                await _broadcast_security_event({"type": "drop_removed", **entry})

            prev_drop_keys = current_drop_keys

        except Exception as e:
            logger.error("Security monitor エラー: %s", e)
