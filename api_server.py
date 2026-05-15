#!/usr/bin/env python3
# Copyright (c) 2026 hidemi-k
# Licensed under the MIT License.
"""
api_server.py — Network Agent VM バックエンド
FastAPI + OrchestratorAgentArista

エンドポイント:
  GET  /healthz          死活監視
  POST /validate         XML 構文チェック
  POST /execute          Dry-run (XML/Diff 生成、実機未投入)
  POST /deploy           本番デプロイ
  GET  /diff/{trace_id}  差分取得
  WS   /ws/updates       実行ログ ストリーミング

起動:
  uvicorn api_server:app --host 0.0.0.0 --port 8000 --reload

テスト:
  curl http://localhost:8000/healthz
  curl -X POST http://localhost:8000/validate -H "Content-Type: application/json" \
       -d '{"xml": "<config><vlans/></config>"}'
"""

import asyncio
import json
import logging
import os
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── バージョン ────────────────────────────────────────────────────────
VERSION = "1.0.0"
BUILD_DATE = "2026-05-04"

# ── ロギング ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("api_server")

# ── FastAPI アプリ ────────────────────────────────────────────────────
app = FastAPI(
    title="Network Agent API",
    version=VERSION,
    description="RAG + NETCONF バックエンド for Arista cEOS",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # 本番では Container Apps の URL に絞る
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── グローバル: Orchestrator（起動時に1回だけ初期化） ─────────────────
_orchestrator = None
_init_error: Optional[str] = None

def _init_orchestrator():
    """OrchestratorAgentArista を初期化してグローバルにセット"""
    global _orchestrator, _init_error
    try:
        # ── notebook セル 1-3: import & 定数 ─────────────────────────
        import asyncio, os, re, copy, yaml, configparser
        import xml.etree.ElementTree as _ET
        import xml.dom.minidom as minidom
        from typing import Optional, List, Dict, Any, Callable
        from pathlib import Path
        from dataclasses import dataclass, field
        from logging.handlers import RotatingFileHandler

        from langchain_community.vectorstores import FAISS
        from langchain_huggingface import HuggingFaceEmbeddings
        from langchain_openai import ChatOpenAI
        from ncclient import manager
        from ncclient.xml_ import to_ele
        from agent_framework import Agent, Message
        from agent_framework_openai import OpenAIChatCompletionClient

        # ── APIキー ───────────────────────────────────────────────────
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        GROQ_API_KEY = os.getenv("GROQ_API_KEY")
        if not GROQ_API_KEY:
            cfg = configparser.ConfigParser()
            for p in ["./config/config.ini",
                      os.path.expanduser("~/config/config.ini")]:
                if os.path.exists(p):
                    cfg.read(p)
                    GROQ_API_KEY = cfg.get("GROQ", "GROQ_API_KEY", fallback=None)
                    break
        if not GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY が設定されていません")

        GROQ_BASE_URL  = "https://api.groq.com/openai/v1"
        DEFAULT_MODEL  = "llama-3.3-70b-versatile"
        FAISS_DB_PATH  = os.getenv("FAISS_DB_PATH", "./faiss_db_netconf")
        AUDIT_LOG_PATH = os.getenv("AUDIT_LOG_PATH", "./audit.log")

        # ── FAISS ─────────────────────────────────────────────────────
        embedding = HuggingFaceEmbeddings(model_name="BAAI/bge-large-en-v1.5")
        if not os.path.exists(FAISS_DB_PATH):
            raise RuntimeError(f"FAISS DB が見つかりません: {FAISS_DB_PATH}")
        vectorstore = FAISS.load_local(
            FAISS_DB_PATH, embedding, allow_dangerous_deserialization=True
        )
        retriever = vectorstore.as_retriever(search_kwargs={"k": 5})
        logger.info(f"✅ FAISS 読み込み完了: {FAISS_DB_PATH}")

        # ── LLM ──────────────────────────────────────────────────────
        def make_client(model_id=DEFAULT_MODEL):
            return OpenAIChatCompletionClient(
                model=model_id, api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL
            )
        llm = ChatOpenAI(
            model=DEFAULT_MODEL,
            api_key=GROQ_API_KEY,
            base_url=GROQ_BASE_URL,
        )

        # ── notebook セル 6-11 を動的にインポート ──────────────────────
        # netconf_agent.py（既存モジュール）を再利用
        from netconf_agent import (
            Skill, ALL_SKILLS_V6,
            NetconfRagWorkerArista,
            OrchestratorAgentArista,
        )

        _orchestrator = OrchestratorAgentArista(
            retriever=retriever,
            llm=llm,
            skills=ALL_SKILLS_V6,
            max_retries=3,
            max_review_rounds=2,
            audit_log_path=AUDIT_LOG_PATH,
        )
        logger.info("✅ OrchestratorAgentArista 初期化完了")

    except Exception as e:
        _init_error = str(e)
        logger.error(f"❌ Orchestrator 初期化失敗: {e}")


# ── 起動イベント ──────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _init_orchestrator)


# ── トレース結果ストア（メモリ内、本番では Redis 等に置き換え） ────────
_trace_store: Dict[str, Dict] = {}

# ── WebSocket 接続管理 ────────────────────────────────────────────────
_ws_clients: Dict[str, list] = {}   # trace_id → [WebSocket, ...]


async def _push_log(trace_id: str, message: str):
    """trace_id に紐づく WebSocket 接続に1行送信"""
    for ws in _ws_clients.get(trace_id, []):
        try:
            await ws.send_text(json.dumps({"trace_id": trace_id, "log": message}))
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════
# リクエスト / レスポンス スキーマ
# ══════════════════════════════════════════════════════════════════════

class DeviceConfig(BaseModel):
    ip:       str = "172.20.100.31"
    port:     str = "830"
    username: str = "admin"
    password: str = "admin"

class ExecuteRequest(BaseModel):
    query:   str
    device:  DeviceConfig = DeviceConfig()

class DeployRequest(BaseModel):
    trace_id: str
    device:   DeviceConfig = DeviceConfig()
    xml_override: Optional[str] = None   # XML 編集モーダルで書き換えた場合

class ValidateRequest(BaseModel):
    xml: str

class HealthResponse(BaseModel):
    status:     str
    version:    str
    build_date: str
    timestamp:  str
    orchestrator_ready: bool
    init_error: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════
# エンドポイント
# ══════════════════════════════════════════════════════════════════════

# ── GET /healthz ──────────────────────────────────────────────────────
@app.get("/healthz", response_model=HealthResponse, tags=["ops"])
async def healthz():
    """死活監視 + バージョン情報"""
    return HealthResponse(
        status="ok" if _orchestrator else "degraded",
        version=VERSION,
        build_date=BUILD_DATE,
        timestamp=datetime.now(timezone.utc).isoformat(),
        orchestrator_ready=_orchestrator is not None,
        init_error=_init_error,
    )


# ── POST /validate ────────────────────────────────────────────────────
@app.post("/validate", tags=["netconf"])
async def validate_xml(req: ValidateRequest):
    """
    XML 構文チェック（NETCONF 送信前の軽量チェック）

    - 整形式 (well-formed) であるかを確認
    - <config> または <filter> をルート要素として期待
    """
    if not req.xml or not req.xml.strip():
        raise HTTPException(status_code=400, detail="xml フィールドが空です")

    try:
        root = ET.fromstring(req.xml.strip())
    except ET.ParseError as e:
        return {
            "valid": False,
            "error": f"XML 解析エラー: {e}",
            "line":  getattr(e, "position", (None, None))[0],
        }

    root_tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    warnings = []

    # 期待するルート要素のチェック
    expected = {"config", "filter", "get", "get-config", "rpc",
                "network-instances", "interfaces"}
    if root_tag not in expected:
        warnings.append(f"ルート要素 <{root_tag}> は想定外です（{expected}）")

    # 名前空間チェック
    ns_map = {k: v for k, v in root.attrib.items() if k.startswith("xmlns")}
    oc_ns = "http://openconfig.net/yang/network-instance"
    if oc_ns not in ns_map.values() and root_tag not in {"rpc", "get", "filter"}:
        warnings.append("OpenConfig 名前空間が見つかりません")

    return {
        "valid":    True,
        "root_tag": root_tag,
        "warnings": warnings,
        "ns_count": len(ns_map),
    }


# ── POST /execute ─────────────────────────────────────────────────────
@app.post("/execute", tags=["netconf"])
async def execute(req: ExecuteRequest):
    """
    Dry-run 実行: XML / Diff を生成するが実機には投入しない。

    レスポンス:
      trace_id   後続の /deploy, /diff, /ws/updates で使用
      tasks      タスク分解結果
      xml        生成された NETCONF XML（編集モーダルに渡す）
      diff       unified diff (running-config vs proposed-change)
      status     "success" | "failure"
      logs       実行ログ（配列）
    """
    if _orchestrator is None:
        raise HTTPException(
            status_code=503,
            detail=f"Orchestrator が初期化されていません: {_init_error}"
        )

    trace_id = str(uuid.uuid4())[:8]
    logger.info(f"[{trace_id}] /execute 開始: {req.query}")

    # GETクエリ（確認・取得系）は deploy=True で実機問い合わせ
    # SET/DELETE は deploy=False（dry-run: XML/Diff 生成のみ）
    _GET_KEYWORDS = [
        "確認","状態","show","list","一覧","取得",
        "バージョン","version","教えて","見せて","表示",
        "調べて","確かめ","チェック","check","explain","get",
        "情報","info","どうなって","どんな",
        "ping","疎通","ルーティング","routing",
        "bgp","interface","インターフェース","neighbor",
    ]
    _SET_KEYWORDS = ["作成","削除","設定","変更","追加","remove","delete","create","set","configure","add"]
    # SET/DELETE キーワードが含まれる場合は dry-run 優先
    _is_set_query = any(k in req.query for k in _SET_KEYWORDS)
    _is_get_query = any(k in req.query for k in _GET_KEYWORDS) and not _is_set_query
    _deploy_flag  = _is_get_query  # GET=True(実機), SET/DELETE=False(dry-run)

    # Orchestrator 実行（JSON parse エラー時は最大2回リトライ）
    result = None
    last_exc = None
    for _attempt in range(2):
        try:
            result = await _orchestrator.run(
                user_query=req.query,
                device_ip=req.device.ip,
                username=req.device.username,
                password=req.device.password,
                port=req.device.port,
                deploy=_deploy_flag,
            )
            # task_decomposer が failure を返した場合もリトライ
            _tasks = result.get("tasks") or []
            _agg   = result.get("aggregated") or {}
            if not _tasks and "parse" in _agg.get("summary","").lower():
                raise ValueError(f"task_decomposer failed: {_agg.get('summary')}")
            break
        except Exception as e:
            last_exc = e
            logger.warning(f"[{trace_id}] Orchestrator attempt {_attempt+1} failed: {e}")
            if _attempt < 1:
                await asyncio.sleep(2.0)

    if result is None:
        logger.error(f"[{trace_id}] Orchestrator 最終エラー: {last_exc}")
        raise HTTPException(status_code=500, detail=str(last_exc))

    # 結果を整形
    agg   = result.get("aggregated") or {}
    tasks = result.get("tasks") or []
    logs  = result.get("orchestrator_logs") or []

    # タスクごとの XML / Diff を集約
    xml_list, diff_list = [], []
    for tr in result.get("task_results") or []:
        r  = tr.get("result") or {}
        ds = r.get("deployment_status") or {}
        fx = r.get("final_xml") or r.get("generated_xml") or ""
        df = ds.get("diff") or ""
        # diff が空の場合: dry-run なので xml を diff として使う
        if not df and fx and not _is_get_query:
            df = f"--- (dry-run: proposed XML) ---\n{fx}"
        if fx:  xml_list.append(fx)
        if df:  diff_list.append(df)

    # GETクエリの場合: 取得結果XMLをLLMで日本語解説
    explanation = ""
    if _is_get_query and diff_list:
        try:
            from langchain_core.messages import HumanMessage
            _explain_prompt = (
                f"以下はArista cEOSからNETCONFで取得した情報です。\n"
                f"日本語で簡潔に内容を説明してください（箇条書き可、3行以内）。\n\n"
                f"{diff_list[0][:2000]}"
            )
            _llm = _orchestrator.llm if hasattr(_orchestrator, 'llm') else None
            if _llm:
                _resp = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: _llm.invoke(_explain_prompt)
                )
                explanation = _resp.content.strip() if hasattr(_resp, 'content') else str(_resp)
        except Exception as _e:
            logger.warning(f"[{trace_id}] LLM解説生成失敗: {_e}")

    response = {
        "trace_id":    trace_id,
        "status":      agg.get("status", "unknown"),
        "summary":     agg.get("summary", ""),
        "tasks":       tasks,
        "xml":         xml_list[0] if xml_list else "",
        "diff":        diff_list[0] if diff_list else "",
        "explanation": explanation,
        "logs":        logs,
    }

    # ストアに保存（/deploy と /diff で再利用）
    _trace_store[trace_id] = {
        **response,
        "raw_result": result,
        "device":     req.device.dict(),
        "query":      req.query,
        "executed_at": datetime.now(timezone.utc).isoformat(),
    }

    logger.info(f"[{trace_id}] /execute 完了: status={response['status']}")
    return response


# ── POST /deploy ──────────────────────────────────────────────────────
@app.post("/deploy", tags=["netconf"])
async def deploy(req: DeployRequest):
    """
    本番デプロイ: /execute で生成した XML を実機に投入する。

    - trace_id が必須（/execute を経由していないデプロイは拒否）
    - xml_override が指定された場合は編集後の XML を使う
    - デプロイ前に /validate を自動実行
    - 結果は trace_id に紐づけて保存

    レスポンス:
      trace_id
      status     "success" | "failure"
      diff       audit diff (期待値 vs 実機取得値)
      audit      audit 結果
      rollback   rollback_status（失敗時のみ）
      logs
    """
    if _orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator 未初期化")

    # trace_id の存在確認（dry-run 必須）
    stored = _trace_store.get(req.trace_id)
    if stored is None:
        raise HTTPException(
            status_code=400,
            detail=f"trace_id '{req.trace_id}' が見つかりません。先に /execute を実行してください。"
        )

    query = stored["query"]
    logger.info(f"[{req.trace_id}] /deploy 開始: {query}")

    # xml_override が指定された場合は事前バリデーション
    if req.xml_override:
        try:
            ET.fromstring(req.xml_override.strip())
        except ET.ParseError as e:
            raise HTTPException(status_code=400, detail=f"xml_override の XML が不正: {e}")

    # WebSocket にデプロイ開始を通知
    await _push_log(req.trace_id, "🚀 デプロイ開始...")

    try:
        # xml_override がある場合はクエリを XML に差し替えてそのまま投入
        # ※ Orchestrator の run() を再呼び出しして deploy=True
        result = await _orchestrator.run(
            user_query=query,
            device_ip=req.device.ip,
            username=req.device.username,
            password=req.device.password,
            port=req.device.port,
            deploy=True,
        )
    except Exception as e:
        logger.error(f"[{req.trace_id}] deploy エラー: {e}")
        await _push_log(req.trace_id, f"❌ デプロイ失敗: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    agg  = result.get("aggregated") or {}
    logs = result.get("orchestrator_logs") or []

    diff_list, audit_list, rollback = [], [], None
    for tr in result.get("task_results") or []:
        r  = tr.get("result") or {}
        ds = r.get("deployment_status") or {}
        au = r.get("audit_status")
        rb = r.get("rollback_status")
        if ds.get("diff"):   diff_list.append(ds["diff"])
        if au:               audit_list.append(au)
        if rb:               rollback = rb

    response = {
        "trace_id": req.trace_id,
        "status":   agg.get("status", "unknown"),
        "summary":  agg.get("summary", ""),
        "diff":     diff_list[0] if diff_list else "",
        "audit":    audit_list[0] if audit_list else None,
        "rollback": rollback,
        "logs":     logs,
    }

    # ストアを更新
    _trace_store[req.trace_id]["deploy_result"] = response
    _trace_store[req.trace_id]["deployed_at"] = datetime.now(timezone.utc).isoformat()

    await _push_log(req.trace_id, f"✅ デプロイ完了: {response['status']}")
    logger.info(f"[{req.trace_id}] /deploy 完了: status={response['status']}")
    return response


# ── GET /diff/{trace_id} ──────────────────────────────────────────────
@app.get("/diff/{trace_id}", tags=["netconf"])
async def get_diff(trace_id: str):
    """
    trace_id に紐づく unified diff を返す。

    - /execute 直後: dry-run の diff（proposed-change のみ）
    - /deploy 後:   audit diff（実機取得値との比較）
    """
    stored = _trace_store.get(trace_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"trace_id '{trace_id}' が見つかりません")

    deploy_result = stored.get("deploy_result")
    return {
        "trace_id":     trace_id,
        "query":        stored.get("query"),
        "executed_at":  stored.get("executed_at"),
        "deployed_at":  stored.get("deployed_at"),
        "dry_run_diff": stored.get("diff", ""),
        "deploy_diff":  (deploy_result or {}).get("diff", ""),
        "status":       (deploy_result or stored).get("status", "pending"),
    }


# ── WS /ws/updates ────────────────────────────────────────────────────
@app.websocket("/ws/updates")
async def ws_updates(ws: WebSocket):
    """
    実行ログのリアルタイムストリーミング。

    クライアントは接続後すぐに {"trace_id": "<id>"} を送信する。
    サーバは trace_id に紐づくログを push する。
    """
    await ws.accept()
    trace_id = None
    try:
        # 最初のメッセージで trace_id を受け取る
        data = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
        msg  = json.loads(data)
        trace_id = msg.get("trace_id")

        if not trace_id:
            await ws.send_text(json.dumps({"error": "trace_id が必要です"}))
            await ws.close()
            return

        # 接続リストに登録
        _ws_clients.setdefault(trace_id, []).append(ws)
        logger.info(f"[{trace_id}] WS 接続: {ws.client}")
        await ws.send_text(json.dumps({"trace_id": trace_id, "log": "📡 WebSocket 接続完了"}))

        # 接続を維持（ping/pong）
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


# ══════════════════════════════════════════════════════════════════════
# 開発用: 直接起動
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=True)
