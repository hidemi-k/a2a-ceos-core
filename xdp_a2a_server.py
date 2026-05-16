#!/usr/bin/env python3
# Copyright (c) 2026 hidemi-k
# Licensed under the MIT License.
"""
XDP Firewall A2A Server — セキュリティ操作 (port:8003)
=======================================================
Go IPS Server (port:8080) の REST API を A2A プロトコルで公開する。
sase_agent.py の FW ツール群をそのまま A2A に移植。

役割分担:
  task_decompose_a2a_server.py　　  (port:8000) … ルーティングハブ
  arista_netconf_rag_a2a_server.py  (port:8001) … 設定変更 (NETCONF edit-config)
  arista_eapi_show_a2a_server.py    (port:8002) … 状態参照 (eAPI show コマンド)
  xdp_a2a_server.py                 (port:8003) … セキュリティ (XDP/eBPF) ← 本ファイル
  arista_anta_verify_a2a_server.py  (port:8004) … 検証 (ANTA Snapshot)

起動:
    python xdp_a2a_server.py

環境変数:
    A2A_PORT       : ポート番号（デフォルト: 8003）
    A2A_PUBLIC_URL : 外部公開URL（デフォルト: http://localhost:8003）
    XDP_API_URL    : Go IPS Server URL（デフォルト: http://localhost:8080）
    SASE_CONFIG    : config.ini のパス
    GROQ_API_KEY   : Groq API キー

リクエスト形式（JSON）:
    {
        "query": "10.0.1.30 をブロックして",
        "action": "block",           # 省略可（LLM が判断）
        "ip":     "10.0.1.30",       # 省略可（LLM が判断）
        "proto":  "tcp",             # 省略可
        "port":   22,                # 省略可
        "limit":  10000              # qos/set 時のみ
    }
    または単純なテキスト

レスポンス形式（JSON）:
    {
        "query":   "...",
        "action":  "block" | "unblock" | "qos_set" | "stats" | "top" |
                   "drop_list" | "qos_list" | "qos_get" | "info" | "analyze",
        "status":  "success" | "error" | "blocked",
        "result":  { ... },         # Go IPS Server からの応答
        "analysis": "..."           # analyze アクション時の LLM 解析結果
    }

[アーキテクチャ]
  A2AStarletteApplication
    └─ DefaultRequestHandler
         └─ XdpFirewallExecutor (AgentExecutor)
               ├─ LLM: アクション判別 & 解析
               └─ Go IPS REST API (:8080) 呼び出し
"""

import json
import logging
import os
import re
import configparser
import sys
from typing import Any, Dict, Optional

import httpx
import uvicorn

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
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

# ── LLM ファクトリ（Groq Primary / Azure Fallback 共通モジュール） ─────────────
from llm_factory import build_llm_with_fallback, log_llm_config, LLM_PROVIDER_NAME

# ── 多言語対応 ────────────────────────────────────────────────────────────────
from i18n import get_msg, locale_from_request, LOCALE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("xdp_a2a_server")

# ── 設定 ──────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.getenv(
    "SASE_CONFIG",
    os.path.join(BASE_DIR, "./config.ini"),
)
GROQ_BASE_URL  = "https://api.groq.com/openai/v1"
DEFAULT_MODEL  = "llama-3.3-70b-versatile"

A2A_HOST       = os.getenv("A2A_HOST",       "0.0.0.0")
A2A_PORT       = int(os.getenv("A2A_PORT",   "8003"))
A2A_PUBLIC_URL = os.getenv("A2A_PUBLIC_URL", f"http://localhost:{A2A_PORT}")

XDP_API_URL    = os.getenv("XDP_API_URL",    "http://localhost:8080")
HTTP_TIMEOUT   = float(os.getenv("HTTP_TIMEOUT", "30"))


def _init_llm():
    """LLM インスタンスを構築する（llm_factory 経由 Groq→Azure 自動切り替え）。"""
    return build_llm_with_fallback()


# ═══════════════════════════════════════════════════════════════════════════════
# Go IPS REST API クライアント
# ═══════════════════════════════════════════════════════════════════════════════

class XdpApiClient:
    """Go IPS Server (port:8080) への HTTP クライアント。"""

    def __init__(self, base_url: str = XDP_API_URL):
        self.base = base_url.rstrip("/")

    def _get(self, path: str, params: Dict = None) -> Any:
        url = f"{self.base}{path}"
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            try:
                return resp.json()
            except Exception:
                return resp.text

    def get_info(self) -> Any:
        return self._get("/info")

    def get_stats(self) -> Any:
        return self._get("/stats")

    def get_top(self) -> Any:
        return self._get("/top")

    def qos_list(self) -> Any:
        return self._get("/qos/list")

    def qos_get(self, ip: str) -> Any:
        return self._get("/qos/get", params={"ip": ip})

    def qos_set(self, ip: str, limit: int) -> Any:
        return self._get("/qos/set", params={"ip": ip, "limit": limit})

    def drop_list(self) -> Any:
        return self._get("/drop/list")

    def drop_block(self, ip: str, proto: str, port: int) -> Any:
        return self._get("/drop/block",
                         params={"ip": ip, "proto": proto, "port": port})

    def drop_unblock(self, ip: str, proto: str, port: int) -> Any:
        return self._get("/drop/unblock",
                         params={"ip": ip, "proto": proto, "port": port})


# ═══════════════════════════════════════════════════════════════════════════════
# LLM: アクション判別プロンプト
# ═══════════════════════════════════════════════════════════════════════════════

ACTION_CLASSIFY_TEMPLATE = """\
あなたは XDP Firewall の操作インテント分類器です。
ユーザーの自然言語クエリから実行すべきアクションと必要なパラメータを抽出し、
以下の JSON のみを返してください（```json ブロック不要）。

アクション一覧:
  block      : /drop/block  — IP+proto+port を完全遮断
  unblock    : /drop/unblock — ブロックルール解除
  qos_set    : /qos/set     — 帯域制限設定
  drop_list  : /drop/list   — 現在のブロックルール一覧
  qos_list   : /qos/list    — 現在のQoSポリシー一覧
  qos_get    : /qos/get     — 特定IPのQoSポリシー確認
  stats      : /stats       — 全フロー統計
  top        : /top         — 上位10フロー統計
  info       : /info        — エージェント情報
  analyze    : 統計取得 + LLM 解析（ブロック提案）

出力 JSON 形式:
{{
  "action": "<上記のいずれか>",
  "ip":     "<IPv4 or null>",
  "proto":  "<tcp|udp|icmp or null>",
  "port":   <整数 or null>,
  "limit":  <整数(B/s) or null>
}}

クエリ: {query}
"""

# ── 解析プロンプト（analyze アクション時）────────────────────────────────────
ANALYZE_TEMPLATE = """\
あなたは高度なネットワークセキュリティ運用エンジニアです。
XDP Firewall の通信統計を分析し、異常を検知して対処を提案します。

【現在のブロックリスト】
{drop_list}

【防御効果（前回比 dropped_packets 増加量）】
{diff_info}

【現在のQoSポリシー（自動ミティゲーション含む）】
{qos_list}

【通信統計（上位10フロー）】
{stats}

【ブロック済み判定ルール（最優先・必ず最初に確認すること）】
以下のいずれかに該当するフローは【防御済み（対応不要）】です:
  条件A: ブロックリストに該当フローが存在する
         キー形式: "IP:PORT [proto]"（例: "10.0.3.150:161 [tcp]"）
         フローの ip, port, protocol で "{{ip}}:{{port}} [{{protocol}}]" を構築し
         ブロックリストのキーと完全一致するか確認すること。
         条件Aに該当するフローは dropped_packets が 0 でも防御済みです。
  条件B: dropped_packets > 0

防御済みフローには絶対に [EXEC: ...] を書かないこと。
ただし、条件Aに該当するフローがあっても他のフローの評価を省略しないこと。
統計内の全フローを必ず最後まで評価してから回答すること。

【RSTパケットの解釈（重要）】
XDP 統計には攻撃元パケットとターゲット応答パケットが混在する場合があります。

  SYN Flood の典型パターン:
    - 攻撃元が SYN を大量送信
    - ターゲットが SYN に対して RST または RST/ACK を返す
    - 統計上: syn_packets ≒ rst_packets、ack_packets = 0 となる

  「≒」の定義: rst_packets が syn_packets の 30%〜100% の範囲
  例: syn=34237, rst=12128 → rst/syn=35% → SYN Flood の兆候

  よって「SYN が大量 かつ ACK=0 かつ RST が SYN の30%以上」は SYN Flood です。
  RST はターゲットからの拒否応答のため攻撃継続中と判断してください。

  ポートスキャンとの違い:
    - ポートスキャン: 複数ポートに少量ずつ SYN+RST
    - SYN Flood:     単一ポートに大量の SYN（RST はターゲット応答）

【判定の優先順位】

1. 【防御済み（対応不要）】
   - 条件A または 条件B に該当
   - [EXEC: ...] は絶対に書かない

2. 【異常あり・未対策（要アクション）】
   dropped_packets=0 かつ 条件A に非該当 かつ 以下のいずれかに該当:

   (a) SYN Flood（単一ポートへの大量 SYN・TCP のみ）
       - syn_packets > 1000 かつ ack_packets = 0
       - かつ rst_packets=0（ターゲット無応答）
         または rst_packets が syn_packets の 30% 以上（ターゲット応答）
       → [EXEC: /drop/block?...] を1件提案

   (b) ポートスキャン
       - syn_packets と rst_packets が共に大量、複数ポートに分散
       → [EXEC: /drop/block?...] を提案

   (c) その他の Flood
       - パケットサイズが固定でパケット数が異常に多い
       → [EXEC: /drop/block?...] または [EXEC: /qos/set?...] を提案

   (d) ハーフオープン接続（TCP のみ）
       - ack_packets / (syn_packets + 1) < 0.5
       - SYN Flood と同時判定の場合は SYN Flood を優先し [EXEC: ...] は1件のみ

   (e) qos_list に当該IPが存在（limit_bytes_per_sec=10000）
       - 自動ミティゲーション適用中。攻撃継続時は [EXEC: /drop/block?...] で完全遮断を提案可
       - [EXEC: /qos/set?...] は提案しない（ミティゲーション中は不要）

   icmp / udp フロー:
       - ack/syn=0 が正常。packets < 10000 の icmp/udp は正常とみなし [EXEC: ...] 禁止。

3. 【正常】上記のいずれにも該当しない

【厳守事項】
- 条件A または 条件B に該当するフローには絶対に [EXEC: ...] を書かない
- icmp / udp で packets < 10000 のフローには絶対に [EXEC: ...] を書かない
- [EXEC: ...] に書く IP・proto・port は統計の実際の値のみ使用する
- <IP> <PROTO> <PORT> などプレースホルダーをそのまま出力しない
- 同一フロー（同じ IP+proto+port）への二重ブロック提案をしない
- rst_packets=0 のフローに「RST が大量」と書かない
- [EXEC: ...] は必ず半角角括弧 [ ] で記述する（全角【】や括弧なしは使わない）
- 統計内の全フローを必ず最後まで評価してから回答する

【[EXEC: ...] を書く場所のルール（最重要）】
  [EXEC: ...] は「提案する」という肯定的な文脈にのみ記述すること。
  「提案しない」「対応不要」「防御済み」という文脈には絶対に書かない。

  ✅ 正しい例:
    * 10.0.1.30 [tcp] は SYN Flood を検知。[EXEC: /drop/block?ip=10.0.1.30&proto=tcp&port=80]

  ❌ 誤った例（絶対禁止）:
    * 10.0.1.30 [tcp] は防御済みのため、[EXEC: /drop/block?...] は提案しません。
    * 10.0.1.30 [icmp] は正常のため、[EXEC: /drop/block?...] は提案しません。

  「提案しない」と言いたい場合は [EXEC: ...] を一切書かずにテキストのみで説明すること:
  ✅ 正しい例:
    * 10.0.1.30 [tcp] は防御済みのため、ブロック提案はありません。
    * 10.0.1.30 [icmp] は正常トラフィックのため、対応は不要です。

【[EXEC: ...] の記述形式】
  [EXEC: /drop/block?ip=<実際のIP>&proto=<実際のproto>&port=<実際のport>]
  [EXEC: /qos/set?ip=<実際のIP>&limit=<bytes_per_sec>]
  ※ 同一フローへの [EXEC: ...] は1件のみ（重複禁止）
"""


def _parse_action_json(text: str) -> Dict:
    """LLM 出力から action JSON を抽出する。"""
    # ```json ... ``` ブロックを除去
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```", "", text)
    brace = text.find("{")
    if brace != -1:
        text = text[brace:]
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return {"action": "top"}


def _extract_exec_tags(text: str, qos_list: Dict = None, top_stats: list = None, drop_list: Dict = None):
    """[EXEC: /path?params] タグを全件抽出して (path, params_dict) のリストを返す。

    改善点:
      1. 同一 (path, ip, proto, port) の重複エントリを除去する。
      2. qos_list が渡された場合、自動ミティゲーション適用中(limit=10000)の IP への
         /qos/set 提案をフィルタリングする（ミティゲーション中は不要）。
    """
    if qos_list is None:
        qos_list = {}
    if top_stats is None:
        top_stats = []
    if drop_list is None:
        drop_list = {}

    # 半角[EXEC:...]・全角【EXEC:...】・括弧なし EXEC:... の3パターンに対応
    pattern1 = r"(?:\[|【)EXEC:\s*((?:/drop/block|/drop/unblock|/qos/set)\?[^\]\s<>】]{5,})(?:\]|】)"
    pattern2 = r"(?<!\[)(?<!【)EXEC:\s*((?:/drop/block|/drop/unblock|/qos/set)\?[^\s<>】\]]{5,})"
    raw_cmds = re.findall(pattern1, text)
    for m in re.findall(pattern2, text):
        if m not in raw_cmds:
            raw_cmds.append(m)

    raw_results = []
    for cmd in raw_cmds:
        # ip= が実際の IPv4 形式かチェック（プレースホルダーを除去）
        if not re.search(r"ip=\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", cmd):
            logger.debug(f"exec_tag フィルタ: プレースホルダー除去 ({cmd[:60]})")
            continue
        if "?" in cmd:
            path, qs = cmd.split("?", 1)
        else:
            path, qs = cmd, ""
        params = {}
        for pair in qs.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                # port / limit は int に変換（型安全）
                if k in ("port", "limit"):
                    try:
                        v = int(v)
                    except ValueError:
                        pass
                params[k] = v
        raw_results.append((path, params))

    # ── フィルタリング & 重複除去 ────────────────────────────────────────────
    seen: set = set()
    results = []
    for path, params in raw_results:
        ip    = params.get("ip", "")
        proto = params.get("proto", "")
        port  = params.get("port", "")

        # DROP_LIST に既に存在するフローへの /drop/block は除外（防御済み）
        # Go IPS の drop_list キー形式: "IP:PORT [proto]"
        if path == "/drop/block" and drop_list:
            block_key = f"{ip}:{port} [{proto}]"
            if block_key in drop_list:
                logger.debug(
                    f"exec_tag フィルタ: DROP_LIST 済みフローへの block 提案を除外 ({block_key})"
                )
                continue

        # 自動ミティゲーション適用中 IP への /qos/set は除外
        if path == "/qos/set" and ip in qos_list:
            qos_entry = qos_list.get(ip, {})
            if isinstance(qos_entry, dict) and qos_entry.get("limit_bytes_per_sec") == 10000:
                logger.debug(f"exec_tag フィルタ: ミティゲーション中 IP への qos_set を除外 ({ip})")
                continue

        # /drop/block で proto が icmp/udp の場合、top_stats でパケット数を確認して
        # 通常量（packets < 10000）なら誤検知として除外する
        # （ICMP/UDP は ack/syn=0 が正常のため、ハーフオープン判定が誤適用されやすい）
        if path == "/drop/block" and proto in ("icmp", "udp") and top_stats:
            flow_pkts = next(
                (f.get("stats", {}).get("packets", 0)
                 for f in top_stats
                 if f.get("ip") == ip
                 and f.get("protocol") == proto
                 and str(f.get("port", "")) == str(port)),
                0,
            )
            if flow_pkts < 10000:
                logger.debug(
                    f"exec_tag フィルタ: 通常量の {proto} フローへの block 提案を除外 "
                    f"({ip}:{port} packets={flow_pkts})"
                )
                continue

        # 同一 (path, ip, proto, port) の重複を除去
        dedup_key = (path, ip, proto, port)
        if dedup_key in seen:
            logger.debug(f"exec_tag フィルタ: 重複エントリを除去 {dedup_key}")
            continue
        seen.add(dedup_key)
        results.append((path, params))

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# A2A AgentExecutor
# ═══════════════════════════════════════════════════════════════════════════════

class XdpFirewallExecutor(AgentExecutor):
    """
    XDP Firewall 操作を A2A プロトコルで公開するアダプタ。

    フロー:
      1. リクエストを受信（テキスト or JSON）
      2. LLM でアクション判別
      3. Go IPS REST API を呼び出し
      4. analyze アクションは追加で LLM 解析を実行
      5. 結果を JSON で返却
    """

    # 破壊的操作（block/unblock/qos_set）は task_decompose から
    # 人間確認付きで呼ばれることを前提とする。
    # A2A レイヤーでは操作を実行するだけでよい。
    DESTRUCTIVE_ACTIONS = {"block", "unblock", "qos_set"}

    def __init__(self, llm: ChatOpenAI):
        self._llm        = llm
        self._client     = XdpApiClient()
        self._prev_stats: dict = {}   # 前回の dropped_packets 記録（diff_info 用）
        self._classify_chain = (
            ChatPromptTemplate.from_template(ACTION_CLASSIFY_TEMPLATE)
            | llm
            | StrOutputParser()
        )
        self._analyze_chain = (
            ChatPromptTemplate.from_template(ANALYZE_TEMPLATE)
            | llm
            | StrOutputParser()
        )
        logger.info("XdpFirewallExecutor 初期化完了")

    # ── リクエスト解析 ────────────────────────────────────────────────────────
    def _parse_request(self, text: str) -> Dict:
        text = text.strip()
        try:
            p = json.loads(text)
            if isinstance(p, dict) and "query" in p:
                return p
        except json.JSONDecodeError:
            pass
        return {"query": text}

    # ── アクション判別 ────────────────────────────────────────────────────────
    def _classify(self, params: Dict) -> Dict:
        """params に action が明示されていれば使い、なければ LLM で判別。"""
        if "action" in params and params["action"]:
            return params  # 呼び出し元が action を明示

        query = params.get("query", "")
        raw   = self._classify_chain.invoke({"query": query})
        logger.info(f"LLM classify: {raw[:200]}")
        classified = _parse_action_json(raw)

        # 呼び出し元の明示パラメータで上書き（LLM 誤抽出を防ぐ）
        for key in ("ip", "proto", "port", "limit"):
            if params.get(key) is not None:
                classified[key] = params[key]

        return classified

    # ── Go IPS API 呼び出し ───────────────────────────────────────────────────
    def _execute_action(self, classified: Dict) -> Dict:
        action = classified.get("action", "top")
        ip     = classified.get("ip")
        proto  = classified.get("proto")
        port   = classified.get("port")
        limit  = classified.get("limit")

        try:
            if action == "block":
                if not (ip and proto and port):
                    return {"status": "error",
                            "message": get_msg("xdp_err_block")}
                result = self._client.drop_block(ip, proto, int(port))
                return {"status": "success", "action": action,
                        "ip": ip, "proto": proto, "port": port, "result": result}

            elif action == "unblock":
                if not (ip and proto and port):
                    return {"status": "error",
                            "message": get_msg("xdp_err_unblock")}
                result = self._client.drop_unblock(ip, proto, int(port))
                return {"status": "success", "action": action,
                        "ip": ip, "proto": proto, "port": port, "result": result}

            elif action == "qos_set":
                if not (ip and limit):
                    return {"status": "error",
                            "message": get_msg("xdp_err_qos_set")}
                result = self._client.qos_set(ip, int(limit))
                return {"status": "success", "action": action,
                        "ip": ip, "limit": int(limit), "result": result}

            elif action == "qos_get":
                if not ip:
                    return {"status": "error",
                            "message": get_msg("xdp_err_qos_get")}
                result = self._client.qos_get(ip)
                return {"status": "success", "action": action, "result": result}

            elif action == "drop_list":
                result = self._client.drop_list()
                return {"status": "success", "action": action, "result": result}

            elif action == "qos_list":
                result = self._client.qos_list()
                return {"status": "success", "action": action, "result": result}

            elif action == "stats":
                result = self._client.get_stats()
                return {"status": "success", "action": action, "result": result}

            elif action == "top":
                result = self._client.get_top()
                return {"status": "success", "action": action, "result": result}

            elif action == "info":
                result = self._client.get_info()
                return {"status": "success", "action": action, "result": result}

            elif action == "analyze":
                return self._execute_analyze()

            else:
                # 不明アクション → top にフォールバック
                result = self._client.get_top()
                return {"status": "success", "action": "top",
                        "note": f"不明なアクション '{action}' → top にフォールバック",
                        "result": result}

        except httpx.ConnectError as e:
            return {"status": "error",
                    "message": get_msg("xdp_conn_error", url=XDP_API_URL, e=e),
                    "hint": "XDP_API_URL 環境変数と Go IPS Server の起動を確認してください"}
        except httpx.HTTPStatusError as e:
            return {"status": "error",
                    "message": f"HTTP エラー {e.response.status_code}: {e}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ── analyze アクション ────────────────────────────────────────────────────
    def _execute_analyze(self) -> Dict:
        """統計取得 → LLM 解析 → 提案を返す。"""
        try:
            top_raw      = self._client.get_top()
            drop_list_raw = self._client.drop_list()
            qos_list_raw  = self._client.qos_list()
        except Exception as e:
            return {"status": "error", "action": "analyze",
                    "message": get_msg("xdp_stats_error", e=e)}

        stats_json    = json.dumps(top_raw,      ensure_ascii=False, indent=2)
        drop_list_str = json.dumps(drop_list_raw, ensure_ascii=False, indent=2)
        qos_list_str  = json.dumps(qos_list_raw,  ensure_ascii=False, indent=2)

        # 前回比 dropped_packets 増加量を計算（sase_agent の diff_info 相当）
        diff_reports = []
        for s in top_raw:
            key       = f"{s.get('ip')}-{s.get('protocol')}-{s.get('port')}"
            curr_drop = s.get("stats", {}).get("dropped_packets", 0)
            if key in self._prev_stats:
                diff = curr_drop - self._prev_stats[key]
                if diff > 0:
                    diff_reports.append(f"{key}:+{diff}")
            self._prev_stats[key] = curr_drop
        diff_info_str = ", ".join(diff_reports) if diff_reports else "（変化なし）"

        analysis = self._analyze_chain.invoke({
            "stats":     stats_json,
            "drop_list": drop_list_str,
            "qos_list":  qos_list_str,
            "diff_info": diff_info_str,
        })
        logger.info(f"analyze 結果: {analysis[:200]}")

        # [EXEC: ...] タグを抽出（重複除去 & ミティゲーション中IP の qos_set を除外）
        exec_tags = _extract_exec_tags(analysis, qos_list=qos_list_raw, top_stats=top_raw, drop_list=drop_list_raw)

        return {
            "status":     "success",
            "action":     "analyze",
            "analysis":   analysis,
            "exec_tags":  [{"path": p, "params": q} for p, q in exec_tags],
            "top_stats":  top_raw,
            "drop_list":  drop_list_raw,
            "qos_list":   qos_list_raw,
        }

    # ── A2A エントリポイント ──────────────────────────────────────────────────
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        raw_text = ""
        for part in context.message.parts:
            if hasattr(part.root, "text"):
                raw_text += part.root.text

        if not raw_text.strip():
            await event_queue.enqueue_event(
                new_agent_text_message(json.dumps(
                    {"status": "error", "message": get_msg("ws_empty")},
                    ensure_ascii=True)))
            return

        params     = self._parse_request(raw_text)
        query      = params.get("query", raw_text)
        locale     = locale_from_request(params)   # ja / en
        # deploy フラグ: Hub から deploy=True が渡された場合のみ Go API へ書き込む
        deploy     = params.get("deploy", False)
        if isinstance(deploy, str):
            deploy = deploy.lower() in ("true", "1", "yes")
        logger.info(f"受信: {query[:80]} deploy={deploy}")

        try:
            classified = self._classify(params)
            logger.info(f"アクション判別: {classified}")
            action = classified.get("action", "top")

            # ── 書き込みアクションは deploy フラグで 2 段階実行 ──────────────
            WRITE_ACTIONS = {"block", "unblock", "qos_set"}
            if action in WRITE_ACTIONS and not deploy:
                # 計画フェーズ: Go API には書き込まず、実行プランだけ返す
                ip    = classified.get("ip", "?")
                proto = classified.get("proto", "?")
                port  = classified.get("port", "?")
                limit = classified.get("limit", "?")
                if action == "block":
                    plan_msg = get_msg("xdp_block_plan", ip=ip, port=port, proto=proto)
                elif action == "unblock":
                    plan_msg = get_msg("xdp_unblock_plan", ip=ip, port=port, proto=proto)
                else:
                    plan_msg = get_msg("xdp_qos_plan", ip=ip, limit=limit)

                result = {
                    "status":     "plan",
                    "action":     action,
                    "deploy":     False,
                    "message":    plan_msg,
                    "classified": classified,
                    "query":      query,
                }
                logger.info(f"計画フェーズ: {plan_msg}")
            else:
                # 実行フェーズ (読み取り系は deploy 不問、書き込み系は deploy=True のみ)
                result = self._execute_action(classified)
                result["query"]  = query
                result["deploy"] = deploy
                logger.info(f"完了: action={result.get('action')} "
                            f"status={result.get('status')}")

            await event_queue.enqueue_event(
                new_agent_text_message(
                    json.dumps(result, ensure_ascii=True, indent=2)))

        except Exception as e:
            logger.error(f"executor エラー: {e}", exc_info=True)
            await event_queue.enqueue_event(
                new_agent_text_message(json.dumps(
                    {"status": "error", "query": query, "message": str(e)},
                    ensure_ascii=True)))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise UnsupportedOperationError(get_msg("cancel_unsupported"))


# ── Agent Card ─────────────────────────────────────────────────────────────────
def build_agent_card() -> AgentCard:
    return AgentCard(
        name="XDP Firewall A2A Agent",
        description=(
            "Go IPS Server (port:8080) の XDP Firewall 操作を A2A プロトコルで公開する。\n"
            "操作: block/unblock/qos_set/drop_list/qos_list/qos_get/stats/top/info/analyze\n"
            "⚠️ block/unblock/qos_set は task_decompose Hub 経由で人間確認後に実行すること。\n"
            f"IPS エンドポイント: {XDP_API_URL}"
        ),
        url=A2A_PUBLIC_URL,
        version="1.0.0",
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="xdp_analyze",
                name="セキュリティ分析",
                description=(
                    "通信統計を取得して LLM が異常を解析し、"
                    "SYN Flood・ポートスキャン等の攻撃を検知して対処を提案する。"
                ),
                tags=["xdp", "analyze", "security", "syn-flood", "ips"],
                examples=[
                    "通信状況を分析して",
                    "セキュリティ上の異常を確認して",
                    '{"query":"状況を分析して","action":"analyze"}',
                ],
            ),
            AgentSkill(
                id="xdp_block",
                name="フロー遮断",
                description=(
                    "/drop/block で L4 レベル（IP+proto+port）のフローを即時遮断。\n"
                    "⚠️ 人間確認後に task_decompose Hub から呼び出すこと。"
                ),
                tags=["xdp", "block", "firewall", "drop"],
                examples=[
                    '{"query":"block 10.0.1.30 tcp 22","action":"block","ip":"10.0.1.30","proto":"tcp","port":22}',
                    "10.0.1.30 の TCP 22 番をブロックして",
                ],
            ),
            AgentSkill(
                id="xdp_qos",
                name="QoS 帯域制限 & ミティゲーション確認",
                description=(
                    "/qos/set で IP 単位の帯域制限を設定。\n"
                    "/qos/list で自動ミティゲーション（Go Agent）の適用状況を確認。\n"
                    "/qos/get で特定 IP の QoS ポリシーを確認。"
                ),
                tags=["xdp", "qos", "rate-limit", "mitigation"],
                examples=[
                    "QoSポリシー一覧を見せて",
                    "10.0.1.30 の帯域を 1KB/s に制限して",
                    '{"query":"qos list","action":"qos_list"}',
                ],
            ),
            AgentSkill(
                id="xdp_stats",
                name="通信統計参照",
                description=(
                    "/top (上位10フロー) / /stats (全フロー) / /info (エージェント情報) を参照。"
                ),
                tags=["xdp", "stats", "top", "info", "monitor"],
                examples=[
                    "上位フローを見せて",
                    "全フロー統計を取得して",
                    "ブロックリストを確認して",
                    '{"query":"top flows","action":"top"}',
                ],
            ),
        ],
    )


# ── サーバ起動 ─────────────────────────────────────────────────────────────────
def main():
    llm          = _init_llm()
    agent_card   = build_agent_card()
    executor     = XdpFirewallExecutor(llm=llm)
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )
    a2a_app = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    ).build()

    logger.info("=" * 60)
    logger.info("XDP Firewall A2A Server 起動")
    logger.info("=" * 60)
    logger.info(f"  Agent Card : {A2A_PUBLIC_URL}/.well-known/agent.json")
    logger.info(f"  A2A endpoint: {A2A_PUBLIC_URL}/   (port:{A2A_PORT})")
    logger.info(f"  Go IPS URL  : {XDP_API_URL}")
    log_llm_config("XDP")
    logger.info(f"  Locale      : {LOCALE}")
    logger.info("  スコープ     : block/unblock/qos/stats/analyze")
    logger.info("  ⚠️  block系は task_decompose Hub 経由で人間確認後に使用")
    logger.info("=" * 60)

    uvicorn.run(a2a_app, host=A2A_HOST, port=A2A_PORT, log_level="info")


if __name__ == "__main__":
    main()
