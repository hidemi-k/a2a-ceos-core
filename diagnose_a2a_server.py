#!/usr/bin/env python3
# Copyright (c) 2026 hidemi-k
# Licensed under the MIT License.
"""
diagnose A2A Server — マルチエージェント障害診断 (port:8005)
=============================================================
ノートブック network_diagnostic_agent.ipynb を A2A サーバ化したもの。

【アーキテクチャ】
  NetworkDiagnosticExecutor (AgentExecutor)
    └─ NetworkDiagnosticSystem（5エージェント連携）
         ├─ ① フロー遷移判断エージェント（l2 / l3 / full を LLM が選択）
         ├─ ② L2 分析エージェント（インターフェース・MAC 異常特定）
         ├─ ③ L3 分析エージェント（ルーティング・ARP・BGP 異常特定）
         ├─ ④ 整合性チェックエージェント（L2/L3 矛盾検出・Self-Correction）
         └─ ⑤ 診断レポートエージェント（根本原因・影響範囲・推奨アクション）

【コマンド実行】
  Netmiko（SSH）ではなく pyeapi（eAPI/HTTPS）を使用する。
  既存の arista_eapi_show_a2a_server.py と同じ接続設定を流用。
  encoding="text" でCLI出力テキストを取得し LLM に渡す。

【A2A エンドポイント】
  POST /                        JSON-RPC message/send
  GET  /.well-known/agent.json  Agent Card

【リクエスト形式（JSON または自然言語）】
  自然言語:
    "BGPが不安定です。調査してください。"

  JSON（オプション指定時）:
    {
      "query":      "BGPが不安定です。",
      "vendor_key": "arista_ceos",   # 省略時: arista_ceos
      "flow":       "l3"             # 省略時: LLM が自動判定
    }

【レスポンス形式】
  {
    "status":            "success" | "error",
    "vendor_key":        "arista_ceos",
    "flow":              "l2" | "l3" | "full",
    "commands_executed": {"show interfaces": {"output": "...", "status": "ok"}, ...},
    "l2_analysis":       "...",
    "l3_analysis":       "...",
    "consistency":       "...",
    "report":            "...",
    "self_correction_triggered": true | false,
    "elapsed_seconds":   18.3
  }

【環境変数】
  A2A_PORT       : このサーバのポート（デフォルト: 8005）
  EAPI_HOST      : デバイスIP（デフォルト: 172.20.100.31）
  EAPI_PORT  : eAPI ポート番号（デフォルト: 443）
  EAPI_TRANSPORT : eAPI トランスポート（デフォルト: https）
  EAPI_USER      : eAPI ユーザー名（デフォルト: admin）
  EAPI_PASS      : eAPI パスワード（デフォルト: admin）

【curl テスト例】
  # 自然言語クエリ
  curl -s -X POST http://localhost:8005/ \\
    -H "Content-Type: application/json" \\
    -d '{
      "jsonrpc": "2.0", "id": "1", "method": "message/send",
      "params": {
        "message": {
          "role": "user",
          "parts": [{"text": "BGPが不安定です。調査してください。"}],
          "messageId": "test-001"
        }
      }
    }' | python3 -m json.tool

  # JSON クエリ（vendor_key・flow 指定）
  curl -s -X POST http://localhost:8005/ \\
    -H "Content-Type: application/json" \\
    -d '{
      "jsonrpc": "2.0", "id": "2", "method": "message/send",
      "params": {
        "message": {
          "role": "user",
          "parts": [{"text": "{\"query\": \"インターフェースを確認して\", \"vendor_key\": \"arista_ceos\", \"flow\": \"l2\"}"}],
          "messageId": "test-002"
        }
      }
    }' | python3 -m json.tool

起動:
  python diagnose_a2a_server.py
"""

import asyncio
import json
import logging
import os
import sys
import time
import warnings
from datetime import datetime
from typing import Any, Dict, List, Optional

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import pyeapi
import uvicorn

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

# agent-framework（ノートブックと同じ MAF クライアント）
from agent_framework import Agent, Message
from agent_framework_openai import OpenAIChatCompletionClient

# 共通モジュール（既存 A2A サーバと同仕様）
from i18n import get_msg, locale_from_request, LOCALE
from llm_factory import (
    build_autogen_client,
    log_llm_config,
    LLM_PROVIDER_NAME,
)

# ── v2.0.0: スナップショット差分RAG ──────────────────────────────────────────
from snapshot_manager import SnapshotManager
from diff_engine import extract_diff_summary, summarize_diff_briefly

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("diagnose_a2a")

# ── 設定 ──────────────────────────────────────────────────────────────────────
VERSION    = "2.0.0"
BUILD_DATE = "2026-05-23"

A2A_HOST       = os.getenv("A2A_HOST",       "0.0.0.0")
A2A_PORT       = int(os.getenv("A2A_PORT",   "8005"))
A2A_PUBLIC_URL = os.getenv("A2A_PUBLIC_URL", f"http://localhost:{A2A_PORT}")

# eAPI 接続設定（arista_eapi_show_a2a_server.py と同じ環境変数）
DEFAULT_EAPI_HOST      = os.getenv("EAPI_HOST",      "172.20.100.31")
DEFAULT_EAPI_PORT      = int(os.getenv("EAPI_PORT", "443"))
DEFAULT_EAPI_TRANSPORT = os.getenv("EAPI_TRANSPORT", "https")
DEFAULT_EAPI_USER      = os.getenv("EAPI_USER",      "admin")
DEFAULT_EAPI_PASS      = os.getenv("EAPI_PASS",      "admin")


# ═══════════════════════════════════════════════════════════════════════════════
# VENDOR_COMMANDS — 手順書YAML辞書（Arista cEOS 拡充版）
#
# 設計方針（ノートブックから継承）:
#   - VENDOR_KEY は運用チームが機種ごとに自由に命名する
#   - eAPI（pyeapi）は encoding="text" でCLI出力テキストを取得する
#   - purpose は LLM に渡すプロンプト文脈（何を見ればよいか）
#   - recovery_hints は OS 固有のCLIコマンド例（レポートに注入）
#   - 新機種追加: このブロックにエントリを追加するだけ。コードは変更不要
# ═══════════════════════════════════════════════════════════════════════════════

VENDOR_COMMANDS: Dict[str, Dict] = {

    # ── Arista cEOS（eAPI/HTTPS 接続）─────────────────────────────────────
    # 既存の arista_eapi_show_a2a_server.py と同じ pyeapi 接続を使用
    # encoding="text" でCLI出力をテキストとして取得し LLM に渡す
    "arista_ceos": {
        "os_label": "Arista cEOS (eAPI/HTTPS)",
        "l2": [
            {
                "cmd": "show interfaces",
                "purpose": (
                    "物理インターフェースの Up/Down 状態・エラーカウンタを確認する。"
                    "output が空の場合はインターフェースが存在しないか eAPI エラー。"
                    "注目点: interfaceStatus=notconnect/errdisabled、"
                    "inputErrors/outputErrors が非ゼロ、resets が増加傾向。"
                ),
            },
            {
                "cmd": "show mac address-table",
                "purpose": (
                    "MAC アドレステーブルの学習状態を確認する。"
                    "output が空の場合は L2 通信が一切発生していないか、"
                    "スイッチング機能が無効な可能性がある。"
                    "注目点: 特定 VLAN のエントリ欠落、エントリ数が異常に少ない。"
                ),
            },
            {
                "cmd": "show vlan",
                "purpose": (
                    "設定済み VLAN の一覧と状態を確認する。"
                    "注目点: 期待する VLAN ID が存在しない、Status が active でない。"
                ),
            },
        ],
        "l3": [
            {
                "cmd": "show ip bgp summary",
                "purpose": (
                    "BGP ピアの状態・受信ルート数を確認する。"
                    "output が空またはヘッダーのみの場合は BGP が未設定。"
                    "注目点: State が Idle/Active/Connect（Established でない）、"
                    "MsgRcvd=0 または Up/Down が短い（フラッピング疑い）。"
                ),
            },
            {
                "cmd": "show ip route",
                "purpose": (
                    "ルーティングテーブル全体を確認する。"
                    "output が空の場合はルーティングが未設定か全ルートが消失。"
                    "注目点: デフォルトルート(0.0.0.0/0)の欠落、"
                    "特定プレフィックスの消失、next-hop の到達性。"
                ),
            },
            {
                "cmd": "show arp",
                "purpose": (
                    "ARP テーブルの解決状態を確認する。"
                    "output が空の場合は直接接続セグメントへのトラフィックがない状態。"
                    "注目点: 通信できるはずの IP が ARP テーブルにない（到達性問題）、"
                    "Incomplete エントリ（ARP 未解決）。"
                ),
            },
            {
                "cmd": "show ip ospf neighbor",
                "purpose": (
                    "OSPF ネイバーの確立状態を確認する。"
                    "output が空またはネイバーなしの場合は OSPF 未設定または隣接未確立。"
                    "注目点: State が FULL でない（2-way/ExStart/Exchange/Loading）、"
                    "ネイバーが期待数より少ない。"
                ),
            },
            {
                "cmd": "show vxlan vni",
                "purpose": (
                    "VXLAN VNI マッピング（VLAN ↔ VNI）の設定状態を確認する。"
                    "注目点: 期待する VLAN に VNI が割り当てられていない、"
                    "Source が static でない（EVPN 動的学習の確認）。"
                ),
            },
            {
                "cmd": "show vxlan flood vtep",
                "purpose": (
                    "VXLAN フラッディング先 VTEP の一覧を確認する。"
                    "注目点: 期待する VTEP IP が登録されていない（BUM トラフィック転送不可）。"
                ),
            },
            {
                "cmd": "show bgp evpn summary",
                "purpose": (
                    "EVPN BGP ピアの状態・NLRI 受信数を確認する。"
                    "注目点: ピアが Established でない、NLRI 受信数が 0（MAC/IP 経路未学習）。"
                ),
            },
        ],
        "recovery_hints": {
            "interface_down":  "interface {ifname} → no shutdown",
            "interface_error": "show interfaces {ifname} counters → clear counters {ifname}",
            "bgp_peer_down":   "show ip bgp neighbors {peer} で詳細確認 → clear ip bgp {peer} soft",
            "bgp_flapping":    "show ip bgp neighbors {peer} | inc Timer → BGP タイマー調整 (timers bgp 10 30)",
            "route_missing":   "show ip route {prefix} → 再配送設定・ルートマップを確認",
            "arp_incomplete":  "ping {ip} source {src_if} → clear arp-cache でARPキャッシュをクリア",
            "ospf_down":       "show ip ospf interface → インターフェースの OSPF 設定確認",
            "vxlan_vni_missing": "interface Vxlan1 → vxlan vlan {vlan} vni {vni} で VNI マッピングを確認",
            "evpn_peer_down":  "show bgp evpn neighbors {peer} detail → clear bgp evpn {peer} soft",
        },
    },

    # ── Juniper EX シリーズ（SSH/Netmiko 接続用 — 将来拡張用に保持）─────────
    "juniper_ex": {
        "os_label": "Juniper EX Series (L2 Switch)",
        "l2": [
            {
                "cmd": "show interfaces",
                "purpose": "物理インターフェースの Up/Down 状態・エラーカウンタを確認",
            },
            {
                "cmd": "show ethernet-switching table",
                "purpose": "MAC アドレステーブルの学習状態・エントリ欠落を確認",
            },
        ],
        "l3": [
            {
                "cmd": "show route",
                "purpose": "ルーティングテーブル・Inactive ルートを確認",
            },
            {
                "cmd": "show arp",
                "purpose": "ARP テーブルの解決状態・エントリ欠落を確認",
            },
        ],
        "recovery_hints": {
            "interface_down": "delete interfaces {ifname} disable  または  set interfaces {ifname} enable",
            "bgp_peer_down":  "show bgp neighbor {peer} detail → clear bgp neighbor {peer}",
            "route_inactive": "show route {prefix} detail で詳細確認",
            "arp_missing":    "clear arp  でARPキャッシュをクリア後、再確認",
        },
    },

    # ── 追加機種はここに追記するだけ（コードは変更不要）─────────────────────
    # "cisco_ios":    {...},
    # "cisco_nxos":  {...},
    # "juniper_mx":  {...},
}

DEFAULT_VENDOR_KEY = "arista_ceos"


# ═══════════════════════════════════════════════════════════════════════════════
# eAPI コマンド実行エンジン（pyeapi ベース）
# arista_eapi_show_a2a_server.py の _eapi_node() と同じ接続方式を使用
# ═══════════════════════════════════════════════════════════════════════════════

def _eapi_node(
    host: str, port: int, transport: str, username: str, password: str
) -> "pyeapi.client.Node":
    """
    pyeapi.connect() → Node 経由で接続する。
    arista_eapi_show_a2a_server.py と同じ実装。
    """
    conn = pyeapi.connect(
        transport=transport, host=host,
        username=username, password=password, port=port,
    )
    return pyeapi.client.Node(conn)


def run_command_eapi(
    command: str,
    host: str      = DEFAULT_EAPI_HOST,
    port: int      = DEFAULT_EAPI_PORT,
    transport: str = DEFAULT_EAPI_TRANSPORT,
    username: str  = DEFAULT_EAPI_USER,
    password: str  = DEFAULT_EAPI_PASS,
) -> Dict[str, str]:
    """
    1コマンドを eAPI（pyeapi）で実行してテキスト出力を返す。

    ノートブックの run_command() と同じ戻り値形式:
      {"command": str, "output": str, "status": "ok" | "error" | "empty"}

    encoding="text" で CLI 出力をテキストとして取得する。
    空出力は status="empty" で返す（LLM への誤った補完を防ぐため）。
    """
    try:
        node = _eapi_node(host, port, transport, username, password)
        # encoding="text" で CLI テキスト出力を取得
        result = node.run_commands([command], encoding="text")
        output = result[0].get("output", "") if result else ""

        if not output or not output.strip():
            return {
                "command": command,
                "output":  "",
                "status":  "empty",
                "message": f"'{command}' の出力は空です（設定なし、または対象なし）",
            }
        return {"command": command, "output": output, "status": "ok"}

    except pyeapi.eapilib.CommandError as e:
        logger.warning(f"eAPI コマンドエラー: {command!r} → {e}")
        return {
            "command": command,
            "output":  "",
            "status":  "error",
            "message": f"コマンドエラー: {str(e)[:200]}",
        }
    except pyeapi.eapilib.ConnectionError as e:
        logger.error(f"eAPI 接続エラー: {e}")
        return {
            "command": command,
            "output":  "",
            "status":  "error",
            "message": f"接続エラー: {str(e)[:200]}",
        }
    except Exception as e:
        logger.error(f"eAPI 予期せぬエラー: {command!r} → {e}")
        return {
            "command": command,
            "output":  "",
            "status":  "error",
            "message": f"{type(e).__name__}: {str(e)[:200]}",
        }


def run_commands_eapi(
    commands: List[str], **eapi_kwargs
) -> Dict[str, Dict]:
    """
    複数コマンドをまとめて実行して辞書で返す。
    ノートブックの run_commands() と同じ戻り値形式。
    """
    results = {}
    for cmd in commands:
        logger.info(f"  eAPI実行: {cmd}")
        results[cmd] = run_command_eapi(cmd, **eapi_kwargs)
        status = results[cmd]["status"]
        logger.info(f"  → {status}")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# NetworkDiagnosticSystem — 5エージェント連携（ノートブックから A2A サーバ化）
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_text(response) -> str:
    """Agent レスポンスからテキストを抽出（ノートブックの extract_text と同じ）。"""
    if hasattr(response, "text"):
        return str(response.text)
    if hasattr(response, "messages") and response.messages:
        for msg in response.messages:
            if hasattr(msg, "content"):
                c = msg.content
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    for block in c:
                        if hasattr(block, "text"):
                            return str(block.text)
    return str(response)


class NetworkDiagnosticSystem:
    """
    マルチレイヤ障害診断エージェントシステム（A2A サーバ版）。

    ノートブック NetworkDiagnosticSystem を A2A サーバ向けに移植。
    LLM クライアントは build_autogen_client()（llm_factory 経由）を使用し、
    他の A2A サーバ（netconf_rag）と同じ MAF ネイティブクライアントを共用する。

    eAPI 接続設定は eapi_kwargs に格納し、run_commands_eapi() に渡す。
    """

    def __init__(self, client: OpenAIChatCompletionClient, eapi_kwargs: Dict):
        self._client      = client
        self._eapi_kwargs = eapi_kwargs
        self._snapshot    = SnapshotManager()   # v2.0.0: スナップショット管理
        self._init_agents()

    def _init_agents(self) -> None:
        """5つの専門エージェントを初期化（instructions はノートブックから移植）。"""
        logger.info("エージェント初期化中...")

        # ① フロー遷移判断エージェント
        self.cmd_agent = Agent(
            self._client,
            name="フロー遷移判断",  # v1.7.0: client が第1位置引数
            instructions=(
                "あなたはネットワーク障害診断のトリアージ専門家です。"
                "ユーザーの状況説明を読み、診断フローを選択してください。"
                "以下のいずれかを **1単語だけ** 出力してください。他の文字は出力禁止です。\n"
                "- l2   : L2層のみ（インターフェース・MAC障害が明確に疑われる場合）\n"
                "- l3   : L3層のみ（ルーティング・到達性・BGP障害が明確に疑われる場合）\n"
                "- full : L2/L3両方（原因不明・複合障害・定期確認の場合）"
            ),
        )

        # ② L2分析エージェント
        self.l2_agent = Agent(
            self._client,
            name="L2分析",
            instructions=(
                "あなたは L2 層（データリンク層）の専門分析エージェントです。\n"
                "提供されたコマンド出力を分析し、以下の観点で異常を特定してください:\n"
                "- インターフェースの Up/Down 状態・errdisabled\n"
                "- インターフェースエラーカウンタ（Input errors / CRC / resets など）\n"
                "- MAC アドレステーブルの不整合（期待されるエントリの欠落など）\n"
                "\n"
                "【スナップショット差分の活用】\n"
                "「正常時スナップショット」と「差分サマリー」が提供されている場合は、\n"
                "差分（+ が現在追加された行、- が正常時から消えた行）を最優先の根拠として使用してください。\n"
                "差分がない項目は正常と判断してよく、分析を簡略化できます。\n"
                "差分がある項目（ステータス変化・カウンタ急増など）を重点的に報告してください。\n"
                "\n"
                "【重要】回答は必ず以下の形式で出力してください:\n"
                "\n"
                "[分析結果]\n"
                "異常点を箇条書きで。正常な場合は「L2 層に異常なし」と述べる。\n"
                "\n"
                "[Evidence]\n"
                "各判断の根拠となった生ログ（差分含む）の該当箇所を必ず引用する。\n"
                "推測ではなく、生ログまたは差分に記載された事実のみを引用すること。\n"
                "\n"
                "日本語で回答してください。"
            ),
        )

        # ③ L3分析エージェント
        self.l3_agent = Agent(
            self._client,
            name="L3分析",
            instructions=(
                "あなたは L3 層（ネットワーク層）の専門分析エージェントです。\n"
                "提供されたコマンド出力を分析し、以下の観点で異常を特定してください:\n"
                "- Inactive ルート・ルートの欠落\n"
                "- ARP エントリの欠落（通信できるはずのホストが ARP にない）\n"
                "- デフォルトルートや静的ルートの異常\n"
                "- BGP ピアの状態・受信ルート数の異常（Active/Idle は障害）\n"
                "- OSPF ネイバーの確立状態\n"
                "\n"
                "【スナップショット差分の活用】\n"
                "「正常時スナップショット」と「差分サマリー」が提供されている場合は、\n"
                "差分（+ が現在追加された行、- が正常時から消えた行）を最優先の根拠として使用してください。\n"
                "差分がない項目は正常と判断してよく、分析を簡略化できます。\n"
                "差分がある項目（BGP State 変化・ルート消失・ARP エントリ消失など）を重点的に報告してください。\n"
                "\n"
                "【重要】回答は必ず以下の形式で出力してください:\n"
                "\n"
                "[分析結果]\n"
                "異常点を箇条書きで。正常な場合は「L3 層に異常なし」と述べる。\n"
                "\n"
                "[Evidence]\n"
                "各判断の根拠となった生ログ（差分含む）の該当箇所を必ず引用する。\n"
                "推測ではなく、生ログまたは差分に記載された事実のみを引用すること。\n"
                "\n"
                "日本語で回答してください。"
            ),
        )

        # ④ 整合性チェックエージェント（Self-Correction）
        self.consistency_agent = Agent(
            self._client,
            name="整合性チェック",
            instructions=(
                "あなたは L2/L3 整合性の専門チェックエージェントです。\n"
                "L2 分析結果と L3 分析結果を突き合わせ、以下のような矛盾を検出してください:\n"
                "- L2 でインターフェースがダウン → L3 でそのインターフェース経由のルートが消失\n"
                "- MAC テーブルにエントリあり → ARP テーブルに対応エントリがない\n"
                "- L3 でルートが消失 → L2 のインターフェースダウンが原因か否か\n"
                "- BGP ピアダウン → 特定サブネットのルートが消失している\n"
                "検出した矛盾・因果関係を箇条書きで。\n"
                "矛盾がなければ「L2/L3 間に整合性の問題なし」と述べてください。\n"
                "日本語で回答してください。"
            ),
        )

        # ⑤ 診断レポートエージェント
        self.report_agent = Agent(
            self._client,
            name="診断レポート",
            instructions=(
                "あなたはネットワーク障害診断の最終レポート作成エージェントです。\n"
                "L2分析・L3分析・整合性チェックの結果と、OS固有の復旧ヒントを統合し、\n"
                "以下の構成でレポートを作成してください:\n\n"
                "【根本原因】\n"
                "  最も根本的な障害原因を1〜2文で断定的に述べる\n\n"
                "【影響範囲】\n"
                "  影響を受けているサービス・通信を箇条書きで列挙\n\n"
                "【推奨アクション】\n"
                "  提供された OS 固有の復旧ヒントを参照しながら、\n"
                "  優先順位順に具体的な手順を箇条書きで列挙\n\n"
                "日本語で回答してください。"
            ),
        )

        logger.info("  ✓ フロー遷移判断 / L2分析 / L3分析 / 整合性チェック / 診断レポート")
        logger.info("✅ 全エージェント初期化完了")

    def _build_context(
        self,
        entries: List[Dict],
        cmd_results: Dict,
        host: str = "",
    ):
        """
        コマンド出力に purpose・スナップショット差分を付与して
        LLM へのコンテキスト文字列を構築する。

        v2.0.0: スナップショットが存在するコマンドは
          「正常時スナップショット」「現在の状態」「差分サマリー」を追加。
          スナップショット未取得のコマンドは従来と同じ動作（後方互換）。

        Returns:
            (context_str: str, snapshot_used: bool)
        """
        sections      = []
        snapshot_used = False

        for entry in entries:
            cmd     = entry["cmd"]
            purpose = entry["purpose"]
            res     = cmd_results.get(cmd, {})
            status  = res.get("status", "error")
            output  = res.get("output", "")

            if status == "ok" and output.strip():
                section = (
                    f"## {cmd}\n"
                    f"（確認目的: {purpose}）\n"
                    f"\n### 現在の状態\n{output}"
                )

                # スナップショット差分を追加（存在する場合のみ）
                if host:
                    snap = self._snapshot.load(host=host, command=cmd)
                    if snap:
                        snapshot_used = True
                        diff_brief = summarize_diff_briefly(snap["output"], output)
                        diff_full  = extract_diff_summary(snap["output"], output)
                        section += (
                            f"\n\n### 正常時スナップショット"
                            f"（取得日時: {snap['captured_at']}）\n"
                            f"{snap['output']}"
                            f"\n\n### 差分サマリー（{diff_brief}）\n"
                            f"{diff_full}"
                        )
                    else:
                        section += (
                            "\n\n（スナップショット未取得のため差分比較なし。"
                            "正常時に mode=snapshot を実行してください）"
                        )

                sections.append(section)

            elif status == "empty":
                sections.append(
                    f"## {cmd}\n"
                    f"（確認目的: {purpose}）\n"
                    f"（出力なし — 該当の設定がされていないか、対象が存在しない可能性があります）"
                )
            else:
                msg = res.get("message", "取得失敗")
                sections.append(
                    f"## {cmd}\n"
                    f"（確認目的: {purpose}）\n"
                    f"（実行エラー: {msg}）"
                )

        return "\n\n".join(sections), snapshot_used

    async def diagnose(
        self,
        user_input:  str,
        vendor_key:  str           = DEFAULT_VENDOR_KEY,
        forced_flow: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        障害診断を実行する（ノートブックの diagnose() を A2A サーバ向けに移植）。

        Args:
            user_input  : ユーザーからの状況説明
            vendor_key  : VENDOR_COMMANDS のキー（デフォルト: arista_ceos）
            forced_flow : "l2" | "l3" | "full" を強制指定（None で LLM 自動判定）

        Returns:
            診断結果辞書
        """
        if vendor_key not in VENDOR_COMMANDS:
            supported = list(VENDOR_COMMANDS.keys())
            return {
                "status":  "error",
                "message": (
                    f"未対応の vendor_key: '{vendor_key}'. "
                    f"対応済み: {supported}"
                ),
            }

        started_at  = datetime.now()
        vendor_cfg  = VENDOR_COMMANDS[vendor_key]
        os_label    = vendor_cfg["os_label"]

        result: Dict[str, Any] = {
            "status":                    "running",
            "vendor_key":                vendor_key,
            "os_label":                  os_label,
            "user_input":                user_input,
            "flow":                      None,
            "snapshot_used":             False,         # v2.0.0: 差分RAG使用フラグ
            "commands_executed":         {},
            "l2_analysis":               None,
            "l3_analysis":               None,
            "l3_analysis_corrected":     None,
            "consistency":               None,
            "report":                    None,
            "self_correction_triggered": False,
            "started_at":                started_at.isoformat(),
        }

        logger.info("=" * 60)
        logger.info(f"障害診断開始 [{os_label}]")
        logger.info(f"  入力: {user_input[:80]}")
        logger.info("=" * 60)

        # ── Step 0: LLM がフロー遷移を判断 ─────────────────────────────────
        if forced_flow and forced_flow in ("l2", "l3", "full", "bgp"):
            flow = forced_flow
            logger.info(f"Step 0: フロー強制指定 → [{flow}]")
        else:
            logger.info("Step 0: フロー遷移判断（LLM）")
            resp = await self.cmd_agent.run(
                f"以下の状況に対して診断フローを選択してください。\n\n{user_input}"
            )
            flow_raw = _extract_text(resp).strip().lower().split()[0]
            flow = flow_raw if flow_raw in ("l2", "l3", "full", "bgp") else "full"
            logger.info(f"  → 選択フロー: [{flow}]")

        result["flow"] = flow

        # ── Step 1: YAML辞書からコマンドを取得して eAPI で実行 ──────────────
        logger.info(f"Step 1: コマンド実行（eAPI / {os_label}）")
        # bgp フロー: VXLAN/EVPN を含む l3 コマンド全体を実行
        l2_entries = vendor_cfg["l2"] if flow in ("l2", "full") else []
        l3_entries = vendor_cfg["l3"] if flow in ("l3", "full", "bgp") else []
        all_entries = l2_entries + l3_entries
        all_cmds    = [e["cmd"] for e in all_entries]

        logger.info(f"  実行コマンド({len(all_cmds)}件): {all_cmds}")
        cmd_results = run_commands_eapi(all_cmds, **self._eapi_kwargs)
        result["commands_executed"] = cmd_results

        # ── Step 2: L2分析 ──────────────────────────────────────────────────
        if l2_entries:
            logger.info(f"Step 2: L2 分析（{os_label}）")
            l2_ctx, l2_snap_used = self._build_context(
                l2_entries, cmd_results, host=self._eapi_kwargs.get("host", "")
            )
            if l2_snap_used:
                result["snapshot_used"] = True
                logger.info("  ✓ スナップショット差分をコンテキストに注入")
            resp = await self.l2_agent.run(
                f"ネットワーク OS: {os_label}\n\n"
                f"以下の L2 コマンド出力を分析してください。\n\n{l2_ctx}"
            )
            result["l2_analysis"] = _extract_text(resp)
            logger.info(f"  L2分析完了（{len(result['l2_analysis'])}文字）")
        else:
            result["l2_analysis"] = "（L2 コマンドは今回のフローでスキップ）"

        # ── Step 3: L3分析 ──────────────────────────────────────────────────
        if l3_entries:
            logger.info(f"Step 3: L3 分析（{os_label}）")
            l3_ctx, l3_snap_used = self._build_context(
                l3_entries, cmd_results, host=self._eapi_kwargs.get("host", "")
            )
            if l3_snap_used:
                result["snapshot_used"] = True
                logger.info("  ✓ スナップショット差分をコンテキストに注入")
            resp = await self.l3_agent.run(
                f"ネットワーク OS: {os_label}\n\n"
                f"以下の L3 コマンド出力を分析してください。\n\n{l3_ctx}"
            )
            result["l3_analysis"] = _extract_text(resp)
            logger.info(f"  L3分析完了（{len(result['l3_analysis'])}文字）")
        else:
            result["l3_analysis"] = "（L3 コマンドは今回のフローでスキップ）"

        # ── Step 4: 整合性チェック（Self-Correction）────────────────────────
        logger.info("Step 4: 整合性チェック（Self-Correction）")
        resp = await self.consistency_agent.run(
            f"【L2 分析結果】\n{result['l2_analysis']}\n\n"
            f"【L3 分析結果】\n{result['l3_analysis']}"
        )
        result["consistency"] = _extract_text(resp)
        logger.info(f"  整合性チェック完了（{len(result['consistency'])}文字）")

        # Self-Correction 発動判定（ノートブックと同じロジック）
        _has_conflict = any(
            kw in result["consistency"]
            for kw in ("矛盾", "因果", "Inactive", "ダウン", "Active", "消失")
        )
        _no_problem = any(
            ph in result["consistency"]
            for ph in ("整合性の問題なし", "問題なし", "異常なし")
        )
        if _has_conflict and not _no_problem:
            logger.info("  🔄 Self-Correction 発動: L3 エージェントが原因を再確認中...")
            result["self_correction_triggered"] = True
            resp2 = await self.l3_agent.run(
                f"整合性チェックで以下の矛盾が指摘されました:\n{result['consistency']}\n\n"
                f"あなたの元の L3 分析: {result['l3_analysis']}\n\n"
                f"ネットワーク OS: {os_label}\n"
                f"指摘を踏まえて、L2 障害と L3 異常の因果関係を明確にして再分析してください。"
            )
            result["l3_analysis_corrected"] = _extract_text(resp2)
            logger.info(f"  L3再分析完了（{len(result['l3_analysis_corrected'])}文字）")

        # ── Step 5: 最終レポート生成 ─────────────────────────────────────────
        logger.info("Step 5: 最終診断レポート生成")
        hints      = vendor_cfg["recovery_hints"]
        hints_text = "\n".join(f"  - {k}: {v}" for k, v in hints.items())

        l3_final = result.get("l3_analysis_corrected") or result["l3_analysis"]
        resp = await self.report_agent.run(
            f"【ユーザー報告】\n{user_input}\n\n"
            f"【ネットワーク OS】\n{os_label}\n\n"
            f"【L2 分析結果】\n{result['l2_analysis']}\n\n"
            f"【L3 分析結果】\n{l3_final}\n\n"
            f"【整合性チェック結果】\n{result['consistency']}\n\n"
            f"【OS 固有の復旧ヒント（推奨アクションのコマンド例に使用すること）】\n"
            f"{hints_text}"
        )
        result["report"] = _extract_text(resp)
        logger.info(f"  レポート生成完了（{len(result['report'])}文字）")

        elapsed = (datetime.now() - started_at).total_seconds()
        result["elapsed_seconds"] = round(elapsed, 1)
        result["status"]          = "success"

        logger.info("=" * 60)
        logger.info(f"診断完了  所要時間: {elapsed:.1f}秒")
        logger.info("=" * 60)

        return result


# ═══════════════════════════════════════════════════════════════════════════════
# A2A AgentExecutor
# ═══════════════════════════════════════════════════════════════════════════════

class NetworkDiagnosticExecutor(AgentExecutor):
    """
    A2A AgentExecutor — NetworkDiagnosticSystem をラップして A2A プロトコルで公開する。

    リクエスト形式（xdp_a2a_server.py と同じ _parse_request パターン）:
      自然言語: "BGPが不安定です。調査してください。"
      JSON: {"query": "...", "vendor_key": "arista_ceos", "flow": "l3"}
    """

    def __init__(self, diag_system: NetworkDiagnosticSystem):
        self._diag = diag_system

    def _parse_request(self, raw_text: str) -> Dict[str, Any]:
        """
        自然言語 or JSON 文字列を解析してパラメータ辞書を返す。
        xdp_a2a_server.py の _parse_request と同じパターン。
        """
        text = raw_text.strip()
        # JSON として解析を試みる
        if text.startswith("{"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        # 自然言語として扱う
        return {"query": text}

    async def _take_snapshot(
        self, vendor_key: str, flow: str
    ) -> Dict[str, Any]:
        """
        v2.0.0: 正常時のeAPI出力をスナップショットとして保存する。

        mode="snapshot" リクエスト時に呼び出す。
        正常時（障害発生前）に cronジョブや手動で実行することを想定。
        """
        if vendor_key not in VENDOR_COMMANDS:
            return {
                "status":  "error",
                "message": f"未対応の vendor_key: '{vendor_key}'",
            }

        vendor_cfg = VENDOR_COMMANDS[vendor_key]
        entries: List[Dict] = []
        if flow in ("l2", "full"):
            entries += vendor_cfg["l2"]
        if flow in ("l3", "full", "bgp"):
            entries += vendor_cfg["l3"]

        cmds        = [e["cmd"] for e in entries]
        host        = self._diag._eapi_kwargs.get("host", DEFAULT_EAPI_HOST)
        captured_at = datetime.now().isoformat(timespec="seconds")

        logger.info(f"スナップショット取得開始: host={host} flow={flow} cmds={cmds}")
        cmd_results = run_commands_eapi(cmds, **self._diag._eapi_kwargs)

        saved:   List[str] = []
        skipped: List[str] = []

        for cmd, res in cmd_results.items():
            if res["status"] == "ok" and res.get("output", "").strip():
                self._diag._snapshot.save(
                    host=host, command=cmd, output=res["output"]
                )
                saved.append(cmd)
                logger.info(f"  ✓ 保存: {cmd}")
            else:
                skipped.append(cmd)
                logger.warning(
                    f"  ✗ スキップ（status={res['status']}）: {cmd}"
                )

        logger.info(
            f"スナップショット取得完了: {len(saved)}件保存 / {len(skipped)}件スキップ"
        )

        return {
            "status":       "success" if saved else "error",
            "mode":         "snapshot",
            "vendor_key":   vendor_key,
            "flow":         flow,
            "host":         host,
            "saved_cmds":   saved,
            "skipped_cmds": skipped,
            "captured_at":  captured_at,
            "message": (
                f"{len(saved)}件のスナップショットを保存しました。"
                if saved else
                "保存できたコマンドがありませんでした。eAPI接続を確認してください。"
            ),
        }

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
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

        raw_text = ""
        for part in context.message.parts:
            if part.HasField("text"):
                raw_text += part.text

        if not raw_text.strip():
            await _send_text(json.dumps(
                    {"status": "error", "message": get_msg("ws_empty")},
                    ensure_ascii=True,
                ))
            return

        params     = self._parse_request(raw_text)
        mode       = params.get("mode", "diagnose")   # v2.0.0: "snapshot" | "diagnose"
        vendor_key = params.get("vendor_key", DEFAULT_VENDOR_KEY)
        flow       = params.get("flow", "full")
        if flow not in ("l2", "l3", "full"):
            flow = "full"

        logger.info(
            f"受信: mode={mode} vendor_key={vendor_key} flow={flow}"
        )

        try:
            # ── スナップショット取得モード（v2.0.0）──────────────────────────
            if mode == "snapshot":
                result = await self._take_snapshot(
                    vendor_key=vendor_key, flow=flow
                )

            # ── 診断モード（従来通り）────────────────────────────────────────
            else:
                query       = params.get("query", raw_text)
                forced_flow = params.get("flow", None)
                if forced_flow and forced_flow not in ("l2", "l3", "full", "bgp"):
                    forced_flow = None

                logger.info(
                    f"  query: {query[:80]} flow={forced_flow or 'auto'}"
                )
                result = await self._diag.diagnose(
                    user_input=query,
                    vendor_key=vendor_key,
                    forced_flow=forced_flow,
                )

            # ── #1774: Message と Artifact を分離して返す ─────────────────────
            if mode == "diagnose" and result.get("status") == "success":
                # Message: 人間が読む要約（report テキスト + status のみ）
                summary_for_message = {
                    "status":    result.get("status"),
                    "vendor_key": result.get("vendor_key"),
                    "flow":      result.get("flow"),
                    "report":    result.get("report", ""),
                    "self_correction_triggered": result.get("self_correction_triggered", False),
                    "elapsed_seconds": result.get("elapsed_seconds", 0),
                }
                await _send_text(
                        json.dumps(summary_for_message, ensure_ascii=False, indent=2))

                # Artifact: 完全な診断データ（commands_executed / l2_analysis 等を含む）
                try:
                    from a2a.types.a2a_pb2 import Artifact as _Artifact
                    art = _Artifact()
                    art.artifact_id = f"diagnose-report-{context.task_id or 'unknown'}"
                    art.name        = "report"
                    art.description = f"ネットワーク診断レポート (flow={result.get('flow','')})"
                    art.parts.append(_Part(
                        text=json.dumps(result, ensure_ascii=False, indent=2)))
                    await event_queue.enqueue_event(art)
                    logger.info(f"[Diagnose] Artifact 送出: report (flow={result.get('flow','')})")
                except Exception as _e:
                    # フォールバック: 完全データを Message で再送
                    logger.debug(f"[Diagnose] Artifact 送出スキップ（SDK非対応）: {_e}")
                    await _send_text(
                            json.dumps(result, ensure_ascii=False, indent=2))
            else:
                # snapshot モード・エラー時はそのまま Message で返す
                await _send_text(
                        json.dumps(result, ensure_ascii=False, indent=2))

        except Exception as e:
            logger.error(f"Executor エラー: {e}", exc_info=True)
            await _send_text(json.dumps(
                    {
                        "status":  "error",
                        "query":   query,
                        "message": str(e),
                    },
                    ensure_ascii=True,
                ))

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        raise UnsupportedOperationError(get_msg("cancel_unsupported"))


# ═══════════════════════════════════════════════════════════════════════════════
# Agent Card
# ═══════════════════════════════════════════════════════════════════════════════

def build_agent_card() -> AgentCard:
    from a2a.types.a2a_pb2 import AgentInterface
    iface = AgentInterface()
    iface.url = A2A_PUBLIC_URL
    iface.protocol_version = "1.0"
    return AgentCard(
        name        = "Network Diagnostic Agent",
        description = (
            "5エージェント連携（L2/L3/整合性/Self-Correction/レポート）による"
            "マルチレイヤ障害診断エージェント。"
            "Arista cEOS の eAPI から show コマンドを取得し、"
            "根本原因・影響範囲・推奨アクションを日本語レポートで返す。"
        ),
        supported_interfaces = [iface],
        version              = VERSION,
        default_input_modes  = ["text"],
        default_output_modes = ["text"],
        capabilities = AgentCapabilities(),
        skills = [
            AgentSkill(
                id          = "diagnose_full",
                name        = "全レイヤ診断 (L2+L3)",
                description = (
                    "原因不明・複合障害・定期確認の場合に使用する。"
                    "L2（インターフェース・MAC）+ L3（ルーティング・BGP・ARP）を全て分析する。"
                ),
                tags        = ["diagnose", "l2", "l3", "full", "障害", "診断"],
                examples    = [
                    "ネットワーク全体の正常性を確認してください。",
                    "社内拠点との通信が断続的に切れています。調査してください。",
                    '{"query": "ネットワークを診断して", "flow": "full"}',
                ],
            ),
            AgentSkill(
                id          = "diagnose_l3",
                name        = "L3 診断 (BGP/ルーティング)",
                description = (
                    "ルーティング・到達性・BGP ピアの障害が疑われる場合に使用する。"
                    "show ip bgp summary / show ip route / show arp / show ip ospf neighbor を実行する。"
                ),
                tags        = ["diagnose", "l3", "bgp", "routing", "arp", "ospf"],
                examples    = [
                    "BGP ピアが不安定です。調査してください。",
                    "特定サブネットに到達できません。ルーティングを確認してください。",
                    '{"query": "BGPを診断して", "flow": "l3"}',
                ],
            ),
            AgentSkill(
                id          = "diagnose_l2",
                name        = "L2 診断 (インターフェース)",
                description = (
                    "インターフェースダウン・MAC 異常が明確に疑われる場合に使用する。"
                    "show interfaces / show mac address-table を実行する。"
                ),
                tags        = ["diagnose", "l2", "interface", "mac"],
                examples    = [
                    "インターフェースがダウンしています。確認してください。",
                    "物理障害が疑われます。L2 を確認してください。",
                    '{"query": "インターフェースを診断して", "flow": "l2"}',
                ],
            ),
        ],
    )


# ═══════════════════════════════════════════════════════════════════════════════
# サーバ起動
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    # MAF ネイティブクライアント（llm_factory 経由: Groq → Azure フォールバック）
    client = build_autogen_client()

    # eAPI 接続設定
    eapi_kwargs = {
        "host":      DEFAULT_EAPI_HOST,
        "port":      DEFAULT_EAPI_PORT,
        "transport": DEFAULT_EAPI_TRANSPORT,
        "username":  DEFAULT_EAPI_USER,
        "password":  DEFAULT_EAPI_PASS,
    }

    diag_system = NetworkDiagnosticSystem(client=client, eapi_kwargs=eapi_kwargs)
    executor    = NetworkDiagnosticExecutor(diag_system=diag_system)
    agent_card  = build_agent_card()

    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )
    from fastapi import FastAPI as _A2AFastAPI
    a2a_app = _A2AFastAPI(title="Network Diagnostic A2A Server")
    add_a2a_routes_to_fastapi(
        a2a_app,
        agent_card_routes=create_agent_card_routes(agent_card),
        jsonrpc_routes=create_jsonrpc_routes(request_handler, rpc_url="/"),
        rest_routes=create_rest_routes(request_handler),
    )

    logger.info("=" * 60)
    logger.info("Network Diagnostic A2A Server 起動  v2.0.0")
    logger.info("  ★ スナップショット差分RAG対応（snapshot_manager / diff_engine）")
    logger.info("=" * 60)
    logger.info(f"  Agent Card      : {A2A_PUBLIC_URL}/.well-known/agent.json")
    logger.info(f"  A2A endpoint    : {A2A_PUBLIC_URL}/   (port:{A2A_PORT})")
    logger.info(f"  eAPI host       : {DEFAULT_EAPI_HOST}:{DEFAULT_EAPI_PORT} ({DEFAULT_EAPI_TRANSPORT})")
    logger.info(f"  Vendor keys     : {list(VENDOR_COMMANDS.keys())}")
    logger.info(f"  Snapshot store  : {diag_system._snapshot._dir}")
    log_llm_config("Diagnose")
    logger.info(f"  Locale          : {LOCALE}")
    logger.info(f"  Version         : {VERSION} ({BUILD_DATE})")
    logger.info("  スコープ         : 障害診断専用 (read-only / eAPI show コマンドのみ)")
    logger.info("  modes           : snapshot（正常時取得）/ diagnose（障害時診断）")
    logger.info("=" * 60)

    uvicorn.run(a2a_app, host=A2A_HOST, port=A2A_PORT, log_level="info")


if __name__ == "__main__":
    main()
