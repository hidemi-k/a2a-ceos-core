#!/usr/bin/env python3
# Copyright (c) 2026 hidemi-k
# Licensed under the MIT License.
"""
arista_eapi_config_a2a_server.py — eAPI configure session 設定変更 (port:8006)
==============================================================================

【位置づけ】
  OpenConfig 非対応、かつベンダーネイティブ YANG も RAG に未定義の設定変更を扱う。
  arista_netconf_rag_a2a_server.py (8001) が NETCONF で対応できない設定を補完する。

  対象機能（実機実証済み）:
    ✅ VXLAN VNI 設定    : interface Vxlan1 / vxlan vlan <VLAN> vni <VNI>
    ✅ EVPN RD/RT 設定  : router bgp / vlan <VLAN> / rd auto / route-target
    ✅ EVPN AF 有効化    : router bgp / address-family evpn / neighbor activate
    ✅ VTEP ソースIF設定 : interface Vxlan1 / vxlan source-interface <IF>
    ✅ MAC フラッディング: interface Vxlan1 / vxlan flood vtep add <IP>
    ✅ その他 EOS CLI でのみ設定可能な機能全般

【処理フロー（NETCONF と同じ 2段階実行）】
  Phase1 deploy=False:
    1. 自然言語 → LLM が CLI コマンド列を生成（JSON）
    2. 安全ガード: _FORBIDDEN_CLI / High Risk チェック
    3. configure session <session> → CLI 投入 → end（破棄）
    4. show session-config named <session> diffs → diff 取得
    5. プラン（diff + CLI コマンド列）を返す

  Phase2 deploy=True:
    1. Phase1 で生成した CLI コマンド列を再実行
    2. configure session <session> → CLI 投入 → commit
    3. running-config に反映

  Phase3 persist=True（オプション）:
    write memory → startup-config に永続化

【A2A エンドポイント】
  POST /                        JSON-RPC message/send
  GET  /.well-known/agent.json  Agent Card

【リクエスト形式】
  自然言語:
    "VXLAN VNI 100 に vni 10000 を設定して"

  JSON（詳細指定）:
    {
      "query":   "VXLAN VNI 100 に vni 10000 を設定して",
      "deploy":  false,           # 省略時: false（dry-run）
      "persist": false,           # 省略時: false（write memory しない）
      "cmds":    ["interface Vxlan1", "vxlan vlan 100 vni 10000"]  # 省略時: LLM 生成
    }

【レスポンス形式】
  {
    "status":   "plan" | "success" | "error" | "blocked",
    "query":    "...",
    "cmds":     ["interface Vxlan1", "vxlan vlan 100 vni 10000"],
    "diff":     "--- system:/running-config\\n+++ session:...",
    "message":  "...",
    "deploy":   false,
    "persist":  false,
    "session":  "eapi_config_<timestamp>"
  }

【環境変数】
  A2A_PORT       : このサーバのポート（デフォルト: 8006）
  EAPI_HOST      : デバイスIP（デフォルト: 172.20.100.31）
  EAPI_PORT  : eAPI ポート番号（デフォルト: 443）
  EAPI_TRANSPORT : eAPI トランスポート（デフォルト: https）
  EAPI_USER      : eAPI ユーザー名（デフォルト: admin）
  EAPI_PASS      : eAPI パスワード（デフォルト: admin）

【curl テスト例】
  # Phase1: dry-run（diffs 確認）
  curl -s -X POST http://localhost:8006/ \\
    -H "Content-Type: application/json" \\
    -d '{
      "jsonrpc":"2.0","id":"1","method":"message/send",
      "params":{"message":{"role":"user",
        "parts":[{"text":"VXLAN VNI 100 に vni 10000 を設定して"}],
        "messageId":"test-001"}}
    }' | python3 -m json.tool

  # Phase2: commit（deploy=true）
  curl -s -X POST http://localhost:8006/ \\
    -H "Content-Type: application/json" \\
    -d '{
      "jsonrpc":"2.0","id":"2","method":"message/send",
      "params":{"message":{"role":"user",
        "parts":[{"text":"{\\"query\\":\\"VXLAN VNI 100 に vni 10000 を設定して\\",\\"deploy\\":true,\\"cmds\\":[\\"interface Vxlan1\\",\\"vxlan vlan 100 vni 10000\\"]}"}],
        "messageId":"test-002"}}
    }' | python3 -m json.tool

起動:
  python arista_eapi_config_a2a_server.py
"""

import json
import logging
import os
import re
import sys
import time
import urllib3
from datetime import datetime
from typing import Any, Dict, List, Optional

import pyeapi
import uvicorn

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# A2A SDK（他のサーバと同じ構成）
from a2a.server.apps import A2AStarletteApplication
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.utils import new_agent_text_message
from a2a.types import (
    AgentCard, AgentCapabilities, AgentSkill, UnsupportedOperationError,
)

# ── LLM ファクトリ（xdp_a2a_server.py と同じ import）────────────────────────
from llm_factory import (
    build_llm,
    invoke_with_fallback,
    log_llm_config,
    LLM_PROVIDER_NAME,
)

# ── 多言語対応（xdp_a2a_server.py と同じ import）────────────────────────────
from i18n import get_msg, locale_from_request, LOCALE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("eapi_config_a2a")

# ── 設定 ──────────────────────────────────────────────────────────────────────
VERSION    = "1.0.0"
BUILD_DATE = "2026-05-23"

A2A_HOST       = os.getenv("A2A_HOST",       "0.0.0.0")
A2A_PORT       = int(os.getenv("A2A_PORT",   "8006"))
A2A_PUBLIC_URL = os.getenv("A2A_PUBLIC_URL", f"http://localhost:{A2A_PORT}")

# eAPI 接続設定（arista_eapi_show_a2a_server.py と同じ環境変数）
EAPI_HOST      = os.getenv("EAPI_HOST",      "172.20.100.31")
EAPI_PORT      = int(os.getenv("EAPI_PORT", "443"))
EAPI_TRANSPORT = os.getenv("EAPI_TRANSPORT", "https")
EAPI_USER      = os.getenv("EAPI_USER",      "admin")
EAPI_PASS      = os.getenv("EAPI_PASS",      "admin")

# configure session の名前プレフィックス
SESSION_PREFIX = "eapi_config"


# ═══════════════════════════════════════════════════════════════════════════════
# 安全ガード
# ═══════════════════════════════════════════════════════════════════════════════

# 絶対に実行させないコマンド（先頭一致 / 完全一致）
# NETCONF の DANGEROUS_PATTERNS に相当
_FORBIDDEN_CLI: List[str] = [
    "reload",           # 再起動
    "write erase",      # 設定初期化
    "no aaa",           # 認証設定削除
    "no username",      # ユーザー削除
    "no management api",# eAPI 無効化（自分自身を切断）
    "no management api http-commands",
    "shutdown",         # インターフェースシャットダウン（Management は特に危険）
    "format",           # フラッシュフォーマット
    "show",             # 参照コマンドはここでは不要
    "enable",           # configure session 内では不要（自動付与）
    "configure",        # configure session 自体も不要（自動付与）
    "commit",           # commit は自動付与（LLM は出力禁止）
    "end",              # end も自動付与
    "abort",            # abort も自動付与
    "write memory",     # persist フラグで制御（LLM は出力禁止）
    "copy running",     # 同上
]

# High Risk パターン（blocking ではなく警告を返す）
# NETCONF の HIGH_RISK_PATTERNS に相当
_HIGH_RISK_PATTERNS: List[tuple] = [
    (r"Management0",        "mgmt_intf",   "管理インターフェース変更は要確認"),
    (r"no interface",       "intf_delete", "インターフェース削除は要確認"),
    (r"autonomous-system",  "bgp_as",      "BGP Local AS 変更は要確認"),
    (r"no router bgp",      "bgp_delete",  "BGP 設定削除は要確認"),
]


def _check_forbidden(cmds: List[str]) -> Optional[str]:
    """
    禁止コマンドが含まれているか確認する。
    含まれていれば理由文字列を返す。安全なら None を返す。
    """
    for cmd in cmds:
        c = cmd.strip().lower()
        for forbidden in _FORBIDDEN_CLI:
            if c == forbidden.lower() or c.startswith(forbidden.lower() + " "):
                return f"禁止コマンド検出: '{cmd}'"
    return None


def _check_high_risk(cmds: List[str]) -> List[Dict]:
    """
    High Risk パターンを確認する。リスト（空なら安全）を返す。
    """
    risks = []
    all_cmds = "\n".join(cmds)
    for pat, code, msg in _HIGH_RISK_PATTERNS:
        if re.search(pat, all_cmds, re.IGNORECASE):
            risks.append({"code": code, "message": msg})
    return risks


# ═══════════════════════════════════════════════════════════════════════════════
# eAPI configure session エンジン
# ═══════════════════════════════════════════════════════════════════════════════

def _eapi_node() -> "pyeapi.client.Node":
    """
    pyeapi Node を返す。arista_eapi_show_a2a_server.py と同じ接続方式。
    """
    conn = pyeapi.connect(
        transport=EAPI_TRANSPORT,
        host=EAPI_HOST,
        username=EAPI_USER,
        password=EAPI_PASS,
        port=EAPI_PORT,
    )
    return pyeapi.client.Node(conn)


def _run_session(
    session_name: str,
    cmds: List[str],
    commit: bool = False,
) -> Dict[str, Any]:
    """
    eAPI configure session を実行する。

    Args:
        session_name : セッション名（一意である必要がある）
        cmds         : configure session 内で実行する CLI コマンドリスト
        commit       : True → commit して running-config に反映
                       False → end で破棄（dry-run）

    Returns:
        {
            "diff":    str,   # show session-config named <session> diffs の出力
            "status":  "ok" | "error",
            "message": str,
        }
    """
    # configure session 内のコマンド列を構築
    # enable → configure session → <cmds> → end or commit → show diffs
    terminal_cmd = "commit" if commit else "end"

    # diffs は end/commit の後は取れないため end 前に取得（dry-run のみ）
    # commit 時は diffs 不要（commit で確定するため）
    if commit:
        full_cmds = (
            ["enable", f"configure session {session_name}"]
            + cmds
            + ["commit"]
        )
    else:
        # dry-run:
        #   ① diffs を abort の前に取得（セッション存在中に取得）
        #   ② abort でセッションを完全破棄（pending リストから削除）
        #   ※ end の後に show diffs → セッションが残存して上限エラーになる
        full_cmds = (
            ["enable", f"configure session {session_name}"]
            + cmds
            + [
                f"show session-config named {session_name} diffs",
                f"configure session {session_name} abort",
            ]
        )

    try:
        node   = _eapi_node()
        result = node.run_commands(full_cmds, encoding="text")

        if commit:
            logger.info(f"[{session_name}] commit 完了")
            return {
                "diff":    "",
                "status":  "ok",
                "message": f"configure session '{session_name}' を commit しました。",
            }
        else:
            # dry-run: 最後のコマンド（show diffs）の出力を取得
            diff_output = ""
            # full_cmds の構成:
            #   [enable, configure session, ...cmds..., show diffs, abort]
            # diffs は末尾から2番目のコマンドの出力（abort の1つ前）
            if result and len(result) >= 2:
                # show diffs の出力（abort の直前）
                diff_result = result[-2] if len(result) >= 2 else result[-1]
                diff_output = diff_result.get("output", "") if isinstance(diff_result, dict) else ""
            elif result:
                diff_output = result[-1].get("output", "") if isinstance(result[-1], dict) else ""
            logger.info(f"[{session_name}] dry-run 完了, diff={len(diff_output)}文字")
            return {
                "diff":    diff_output.strip(),
                "status":  "ok",
                "message": f"dry-run 完了（セッション '{session_name}' は破棄済み）",
            }

    except pyeapi.eapilib.CommandError as e:
        logger.error(f"[{session_name}] eAPI コマンドエラー: {e}")
        return {
            "diff":    "",
            "status":  "error",
            "message": f"コマンドエラー: {str(e)[:300]}",
        }
    except pyeapi.eapilib.ConnectionError as e:
        logger.error(f"[{session_name}] eAPI 接続エラー: {e}")
        return {
            "diff":    "",
            "status":  "error",
            "message": f"接続エラー: {str(e)[:200]}",
        }
    except Exception as e:
        logger.error(f"[{session_name}] 予期せぬエラー: {e}")
        return {
            "diff":    "",
            "status":  "error",
            "message": f"{type(e).__name__}: {str(e)[:200]}",
        }


def _write_memory() -> Dict[str, Any]:
    """
    write memory を実行して startup-config に永続化する。
    """
    try:
        node   = _eapi_node()
        result = node.run_commands(["enable", "write memory"], encoding="text")
        output = result[-1].get("output", "") if result else ""
        logger.info(f"write memory 完了: {output.strip()[:80]}")
        return {
            "status":  "ok",
            "message": f"write memory 完了: {output.strip()[:80] or 'Copy complete.'}",
        }
    except Exception as e:
        logger.error(f"write memory エラー: {e}")
        return {
            "status":  "error",
            "message": f"write memory エラー: {str(e)[:200]}",
        }


# ═══════════════════════════════════════════════════════════════════════════════
# LLM CLI コマンド生成
# ═══════════════════════════════════════════════════════════════════════════════


def _extract_evpn_bgp_as(running_config: str) -> list:
    """
    running-config から address-family evpn を持つ BGP プロセスの AS 番号を抽出する。
    レビュアー指摘（複数 BGP プロセス環境での誤 AS 選択リスク）への対応。
    """
    evpn_as_list = []
    current_as   = None
    in_bgp       = False
    has_evpn     = False

    for line in running_config.splitlines():
        m = re.match(r"^router bgp (\d+)\s*$", line)
        if m:
            # 直前の BGP ブロックを確定
            if in_bgp and has_evpn and current_as:
                evpn_as_list.append(current_as)
            current_as = int(m.group(1))
            in_bgp     = True
            has_evpn   = False
            continue
        # BGP ブロック終了（インデントなしの行）
        if in_bgp and line and not line[0].isspace():
            if has_evpn and current_as:
                evpn_as_list.append(current_as)
            in_bgp = False; has_evpn = False; current_as = None
        # address-family evpn 検出
        if in_bgp and re.search(r"address-family\s+evpn", line):
            has_evpn = True

    # ファイル末尾のブロック
    if in_bgp and has_evpn and current_as:
        evpn_as_list.append(current_as)
    return evpn_as_list


def _fetch_device_context() -> str:
    """
    デバイスから現在の設定情報を取得してプロンプト用コンテキスト文字列を返す。

    【レビュアー指摘対応】複数 BGP プロセス環境での誤 AS 選択リスクを解消する。
    AS 選択の優先順位:
      1. address-family evpn を持つ BGP プロセス（最優先・EVPN 用と確定できる）
      2. VXLAN（Vxlan1）が存在する場合は default VRF の BGP を採用
      3. 複数候補が残る場合はプロンプトに候補リストを注入してユーザ確認を促す
      4. VRF 内 BGP（vrf X）・外部向け eBGP は除外済み（running-config で識別）
    """
    try:
        conn = pyeapi.connect(
            transport=EAPI_TRANSPORT, host=EAPI_HOST,
            username=EAPI_USER, password=EAPI_PASS, port=EAPI_PORT,
        )
        node = pyeapi.client.Node(conn)

        # running-config は text 形式のみ対応。bgp summary/version は json 形式で取得。
        # ※ show running-config は encoding="json" では "unconverted command" エラーになる
        text_results = node.run_commands([
            "show running-config section router bgp",  # BGP 設定全体（text のみ）
        ], encoding="text")
        json_results = node.run_commands([
            "show ip bgp summary",   # default VRF の AS/RouterID
            "show version",          # EOS バージョン
        ], encoding="json")

        bgp_running = (text_results[0].get("output", "")
                       if text_results and isinstance(text_results[0], dict) else "")
        bgp_summary = json_results[0] if json_results else {}
        ver_info    = json_results[1] if len(json_results) > 1 else {}

        lines = ["【現在のデバイス設定】（必ずこれを参照してコマンドを生成すること）"]

        # ── Step1: EVPN AF を持つ BGP プロセスを最優先 ──────────────────────
        evpn_as_list = _extract_evpn_bgp_as(bgp_running)

        if len(evpn_as_list) == 1:
            # 候補が1つ → 確定
            chosen_as = evpn_as_list[0]
            lines.append(
                f"- BGP Local AS番号（EVPN 用・確定）: {chosen_as}"
                " ← router bgp コマンドには必ずこの番号を使うこと"
            )
            logger.info(f"AS選択: EVPN AF 保持プロセス AS={chosen_as}（確定）")

        elif len(evpn_as_list) > 1:
            # 複数 EVPN プロセス → ユーザ確認を促す
            lines.append(
                f"- BGP AS番号の候補（複数）: {evpn_as_list}"
                " ← EVPN 設定に使う AS をユーザに確認してください"
            )
            logger.warning(f"AS選択: 複数 EVPN プロセス候補 {evpn_as_list}（要確認）")

        else:
            # Step2: EVPN AF なし → default VRF の AS を採用（VXLAN 存在時）
            default_vrf = bgp_summary.get("vrfs", {}).get("default", {})
            local_as    = default_vrf.get("asn", "")
            router_id   = default_vrf.get("routerId", "")
            if local_as:
                lines.append(
                    f"- BGP Local AS番号（default VRF）: {local_as}"
                    " ← EVPN 設定をする場合はこの AS に address-family evpn を追加すること"
                )
                logger.info(f"AS選択: EVPN AFなし → default VRF AS={local_as}")
            if router_id:
                lines.append(f"- BGP Router ID: {router_id}")

        # ── EOS バージョン情報 ──────────────────────────────────────────────
        eos_ver = ver_info.get("version", "") if isinstance(ver_info, dict) else ""
        model   = ver_info.get("modelName", "") if isinstance(ver_info, dict) else ""
        if eos_ver:
            lines.append(f"- EOS バージョン: {eos_ver}")
        if model:
            lines.append(f"- 機種: {model}")

        ctx = "\n".join(lines)
        logger.info(f"デバイスコンテキスト取得完了 ({len(lines)-1}項目)")
        return ctx

    except Exception as e:
        logger.warning(f"デバイスコンテキスト取得失敗（続行）: {e}")
        return ""

CLI_GENERATOR_PROMPT = """\
あなたは Arista EOS の設定変更専門エージェントです。
ユーザーの要求を読み、Arista EOS の configure session 内で実行する CLI コマンドリストを生成してください。

【出力形式】
JSON 配列のみを出力してください。説明文・コードブロック記号（```）は一切出力禁止です。
例: ["interface Vxlan1", "vxlan vlan 100 vni 10000"]

【重要な制約】
- show コマンドは禁止（参照は別サーバが担当）
- enable, configure session, commit, end, write memory は自動付与するため出力禁止
- 危険なコマンド（reload, write erase, no aaa, format 等）は絶対に出力禁止
- configure session 内で有効な設定コマンドのみ出力すること

【Arista EOS の主な対象機能】
  VXLAN/EVPN 設定（OpenConfig では設定不可）:
    interface Vxlan1
      vxlan source-interface Loopback0
      vxlan udp-port 4789
      vxlan vlan <VLAN> vni <VNI>
      vxlan flood vtep add <VTEP-IP>
    router bgp <ASN>
      vlan <VLAN>
        rd auto
        route-target both <RT>
        redistribute learned
      address-family evpn
        neighbor <PEER> activate

  その他 CLI のみで設定可能な機能も対応する。

  BGP network advertise / redistribute（NETCONF では複数YANGツリーをまたぐため CLI 方式で対応）:
    router bgp <ASN>
      network <PREFIX>/<LEN>          ← 例: network 192.168.1.0/24
      redistribute connected          ← connected route を BGP に再配布
      redistribute static             ← static route を BGP に再配布
      neighbor <PEER-IP> route-map <MAP-NAME> out  ← route-map 適用

  【BGP network の注意点】
  - ASN は省略しない。デバイスコンテキストから取得するか、クエリに明示された値を使う
  - "network" コマンドは "router bgp <ASN>" の配下に記述する
  - 例: ["router bgp 65001", "network 192.168.1.0/24"]

{device_context}

ユーザーの要求: {query}
"""


def _generate_cli_cmds(llm, query: str) -> List[str]:
    """
    LLM を使って自然言語から CLI コマンドリストを生成する。
    invoke_with_fallback を使用（llm_factory の Groq → Azure フォールバック）。
    実行前にデバイスから BGP AS 番号等を取得してプロンプトに注入する。
    """
    device_context = _fetch_device_context()
    prompt = CLI_GENERATOR_PROMPT.format(
        query=query,
        device_context=device_context,
    )
    raw    = invoke_with_fallback(llm, prompt)

    # JSON 配列を抽出
    # LLM が ```json ... ``` で囲む場合にも対応
    raw = raw.strip()
    raw = re.sub(r"```(?:json)?", "", raw, flags=re.IGNORECASE).strip("` \n")

    try:
        cmds = json.loads(raw)
        if isinstance(cmds, list) and all(isinstance(c, str) for c in cmds):
            logger.info(f"LLM 生成コマンド ({len(cmds)}件): {cmds}")
            return cmds
        logger.warning(f"LLM 出力が配列でない: {raw[:100]}")
        return []
    except json.JSONDecodeError:
        logger.warning(f"LLM 出力の JSON パース失敗: {raw[:100]}")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# A2A AgentExecutor
# ═══════════════════════════════════════════════════════════════════════════════

class EapiConfigExecutor(AgentExecutor):
    """
    eAPI configure session 設定変更の AgentExecutor。
    xdp_a2a_server.py と同じ _parse_request パターンを踏襲。
    """

    def __init__(self, llm):
        self._llm = llm

    def _parse_request(self, raw_text: str) -> Dict[str, Any]:
        """
        自然言語 or JSON 文字列を解析してパラメータ辞書を返す。
        xdp_a2a_server.py の _parse_request と同じパターン。
        """
        text = raw_text.strip()
        if text.startswith("{"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        return {"query": text}

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:

        # ── メッセージテキスト抽出 ────────────────────────────────────────────
        raw_text = ""
        for part in context.message.parts:
            if hasattr(part.root, "text"):
                raw_text += part.root.text

        if not raw_text.strip():
            await event_queue.enqueue_event(
                new_agent_text_message(json.dumps(
                    {"status": "error", "message": get_msg("ws_empty")},
                    ensure_ascii=False,
                ))
            )
            return

        params  = self._parse_request(raw_text)
        query   = params.get("query", raw_text)
        deploy  = bool(params.get("deploy", False))
        persist = bool(params.get("persist", False))
        # cmds: 既に Phase1 で生成済みのコマンドを Phase2 で再利用
        given_cmds: Optional[List[str]] = params.get("cmds", None)

        logger.info(
            f"受信: {query[:80]} "
            f"deploy={deploy} persist={persist} "
            f"cmds={'指定あり' if given_cmds else 'LLM生成'}"
        )

        session_name = (
            f"{SESSION_PREFIX}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        )

        try:
            # ── CLI コマンド列の確定 ──────────────────────────────────────────
            if given_cmds:
                cmds = given_cmds
                logger.info(f"指定コマンドを使用: {cmds}")
            else:
                logger.info("LLM でコマンドを生成中...")
                cmds = _generate_cli_cmds(self._llm, query)
                if not cmds:
                    await event_queue.enqueue_event(
                        new_agent_text_message(json.dumps({
                            "status":  "error",
                            "query":   query,
                            "message": "LLM がコマンドを生成できませんでした。クエリを具体的に入力してください。",
                        }, ensure_ascii=False))
                    )
                    return

            # ── 安全ガード ────────────────────────────────────────────────────
            forbidden_reason = _check_forbidden(cmds)
            if forbidden_reason:
                logger.warning(f"禁止コマンド検出: {forbidden_reason}")
                await event_queue.enqueue_event(
                    new_agent_text_message(json.dumps({
                        "status":  "blocked",
                        "query":   query,
                        "cmds":    cmds,
                        "message": f"安全ガードによりブロック: {forbidden_reason}",
                    }, ensure_ascii=False))
                )
                return

            high_risks = _check_high_risk(cmds)
            if high_risks and not deploy:
                # High Risk は deploy=False（dry-run）の場合のみ警告して継続
                # deploy=True の場合は警告済みとみなして実行
                risk_msgs = [r["message"] for r in high_risks]
                logger.warning(f"High Risk 検出: {risk_msgs}")

            # ── Phase1: dry-run（configure session → end → diffs）────────────
            if not deploy:
                logger.info(f"Phase1: dry-run [{session_name}]")
                session_result = _run_session(
                    session_name=session_name,
                    cmds=cmds,
                    commit=False,
                )

                if session_result["status"] != "ok":
                    await event_queue.enqueue_event(
                        new_agent_text_message(json.dumps({
                            "status":  "error",
                            "query":   query,
                            "cmds":    cmds,
                            "message": session_result["message"],
                        }, ensure_ascii=False))
                    )
                    return

                diff = session_result["diff"]
                if not diff:
                    diff = "（差分なし — 設定変更は不要か、コマンドが無効の可能性があります）"

                result = {
                    "status":     "plan",
                    "query":      query,
                    "cmds":       cmds,
                    "diff":       diff,
                    "message":    (
                        f"以下の設定変更を計画しました。\n"
                        f"承認して実行するには deploy=true と cmds を指定して再送信してください。\n\n"
                        f"実行コマンド ({len(cmds)}件):\n"
                        + "\n".join(f"  {c}" for c in cmds)
                        + f"\n\n設定差分:\n{diff}"
                    ),
                    "deploy":     False,
                    "persist":    False,
                    "session":    session_name,
                    "high_risks": high_risks,
                }

                if high_risks:
                    result["warning"] = (
                        "⚠️ High Risk 操作が含まれています: "
                        + " / ".join(r["message"] for r in high_risks)
                    )

                logger.info(f"Phase1 完了: diff={len(diff)}文字")

            # ── Phase2: commit（configure session → commit）──────────────────
            else:
                logger.info(f"Phase2: commit [{session_name}]")
                session_result = _run_session(
                    session_name=session_name,
                    cmds=cmds,
                    commit=True,
                )

                if session_result["status"] != "ok":
                    await event_queue.enqueue_event(
                        new_agent_text_message(json.dumps({
                            "status":  "error",
                            "query":   query,
                            "cmds":    cmds,
                            "message": session_result["message"],
                        }, ensure_ascii=False))
                    )
                    return

                result = {
                    "status":  "success",
                    "query":   query,
                    "cmds":    cmds,
                    "message": (
                        f"設定を commit しました（running-config に反映済み）。\n"
                        + ("startup-config への永続化は write memory で別途実行してください。"
                           if not persist else "")
                    ),
                    "deploy":  True,
                    "persist": False,
                    "session": session_name,
                }

                # ── Phase3: write memory（オプション）──────────────────────
                if persist:
                    logger.info("Phase3: write memory")
                    wm_result = _write_memory()
                    result["persist"]        = True
                    result["persist_status"] = wm_result["status"]
                    result["persist_message"] = wm_result["message"]
                    if wm_result["status"] == "ok":
                        result["message"] += f"\n{wm_result['message']}"
                    else:
                        result["message"] += f"\n⚠️ write memory 失敗: {wm_result['message']}"

                logger.info(f"Phase2 完了: persist={persist}")

            # ── レスポンス送信 ────────────────────────────────────────────────
            await event_queue.enqueue_event(
                new_agent_text_message(
                    json.dumps(result, ensure_ascii=False, indent=2)
                )
            )

        except Exception as e:
            logger.error(f"Executor エラー: {e}", exc_info=True)
            await event_queue.enqueue_event(
                new_agent_text_message(json.dumps({
                    "status":  "error",
                    "query":   query,
                    "message": str(e),
                }, ensure_ascii=False))
            )

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        raise UnsupportedOperationError(get_msg("cancel_unsupported"))


# ═══════════════════════════════════════════════════════════════════════════════
# Agent Card
# ═══════════════════════════════════════════════════════════════════════════════

def build_agent_card() -> AgentCard:
    return AgentCard(
        name        = "Arista eAPI Config Agent",
        description = (
            "OpenConfig 非対応、かつベンダーネイティブ YANG も未定義の設定変更を担当。\n"
            "eAPI configure session 経由で CLI を投入し、VXLAN/EVPN 等を設定する。\n"
            "2段階実行: dry-run（diffs 確認）→ 承認 → commit。\n"
            f"対象デバイス: {EAPI_HOST}:{EAPI_PORT} ({EAPI_TRANSPORT})"
        ),
        url                = A2A_PUBLIC_URL,
        version            = VERSION,
        defaultInputModes  = ["text"],
        defaultOutputModes = ["text"],
        capabilities = AgentCapabilities(streaming=False),
        skills = [
            AgentSkill(
                id          = "vxlan_config",
                name        = "VXLAN/EVPN 設定",
                description = (
                    "VXLAN VNI 設定・EVPN RD/RT 設定・BGP EVPN AF 有効化等。\n"
                    "OpenConfig では設定不可な機能を eAPI configure session で実現。\n"
                    "2段階実行: deploy=false で diffs 確認 → deploy=true で commit。"
                ),
                tags     = ["vxlan", "evpn", "vni", "bgp", "configure", "session"],
                examples = [
                    "VXLAN VNI 100 に vni 10000 を設定して",
                    "BGP EVPN アドレスファミリーを有効化して",
                    "interface Vxlan1 に source-interface Loopback0 を設定して",
                    '{"query": "VXLAN VNI 100 vni 10000 を設定して", "deploy": false}',
                ],
            ),
            AgentSkill(
                id          = "eapi_cli_config",
                name        = "EOS CLI 設定変更（汎用）",
                description = (
                    "NETCONF（OpenConfig）では設定できない EOS CLI コマンドを投入する。\n"
                    "LLM が自然言語から configure session 内のコマンドリストを生成。\n"
                    "安全ガード: 禁止コマンド・High Risk 操作を自動検出してブロック/警告。"
                ),
                tags     = ["cli", "configure", "session", "eos", "arista"],
                examples = [
                    "MAC フラッディング VTEP 10.0.20.10 を追加して",
                    "EVPN route-target を 100:100 に設定して",
                    '{"query": "VNI 設定", "deploy": true, "cmds": ["interface Vxlan1", "vxlan vlan 100 vni 10000"]}',
                ],
            ),
        ],
    )


# ═══════════════════════════════════════════════════════════════════════════════
# サーバ起動
# ═══════════════════════════════════════════════════════════════════════════════

def _cleanup_sessions() -> None:
    """
    起動時に残存している eapi_config_* セッションを abort してクリアする。
    前回の dry-run セッションが残っているとセッション上限エラーになるため。
    """
    try:
        conn   = pyeapi.connect(
            transport=EAPI_TRANSPORT, host=EAPI_HOST,
            username=EAPI_USER, password=EAPI_PASS, port=EAPI_PORT,
        )
        node   = pyeapi.client.Node(conn)
        result = node.run_commands(
            ["enable", "show configuration sessions"], encoding="text"
        )
        sessions_output = result[-1].get("output", "") if result else ""

        # eapi_config_ で始まるセッション名を抽出
        import re as _re
        session_names = _re.findall(
            rf"{SESSION_PREFIX}_\S+", sessions_output
        )
        if not session_names:
            logger.info("残存セッション: なし")
            return

        logger.info(f"残存セッションをクリア: {session_names}")
        abort_cmds = ["enable"]
        for sname in session_names:
            abort_cmds.append(f"configure session {sname} abort")
        node.run_commands(abort_cmds, encoding="text")
        logger.info(f"✅ {len(session_names)} セッションを abort しました")

    except Exception as e:
        logger.warning(f"起動時セッションクリア失敗（続行）: {e}")


def main() -> None:
    # 起動時に残存セッションをクリア
    _cleanup_sessions()

    # LLM 初期化（llm_factory: Groq Primary → Azure Fallback）
    llm = build_llm()

    executor    = EapiConfigExecutor(llm=llm)
    agent_card  = build_agent_card()

    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )
    a2a_app = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    ).build()

    logger.info("=" * 60)
    logger.info("Arista eAPI Config A2A Server 起動")
    logger.info("=" * 60)
    logger.info(f"  Agent Card   : {A2A_PUBLIC_URL}/.well-known/agent.json")
    logger.info(f"  A2A endpoint : {A2A_PUBLIC_URL}/   (port:{A2A_PORT})")
    logger.info(f"  eAPI target  : {EAPI_HOST}:{EAPI_PORT} ({EAPI_TRANSPORT})")
    logger.info(f"  Session prefix: {SESSION_PREFIX}_<timestamp>")
    log_llm_config("eAPI-Config")
    logger.info(f"  Locale       : {LOCALE}")
    logger.info(f"  Version      : {VERSION} ({BUILD_DATE})")
    logger.info("  スコープ      : OpenConfig 非対応設定変更専用")
    logger.info(f"  禁止コマンド  : {_FORBIDDEN_CLI[:5]}... ({len(_FORBIDDEN_CLI)}件)")
    logger.info("=" * 60)

    uvicorn.run(a2a_app, host=A2A_HOST, port=A2A_PORT, log_level="info")


if __name__ == "__main__":
    main()
