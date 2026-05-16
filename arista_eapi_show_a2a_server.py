#!/usr/bin/env python3
# Copyright (c) 2026 hidemi-k
# Licensed under the MIT License.
"""
Arista cEOS eAPI Show A2A Server — 参照系 (port:8002)
======================================================
pyeapi (https/443) で show コマンドを実行し、
オペレーショナル状態（<state>相当）を返す A2A サーバ。

役割分担:
  task_decompose_a2a_server.py　 　 (port:8000) … ルーティングハブ
  arista_netconf_rag_a2a_server.py  (port:8001) … 設定変更 (NETCONF edit-config)
  arista_eapi_show_a2a_server.py    (port:8002) … 状態参照 (eAPI show コマンド) ← 本ファイル
  xdp_a2a_server.py                 (port:8003) … セキュリティ (XDP/eBPF)
  arista_anta_verify_a2a_server.py  (port:8004) … 検証 (ANTA Snapshot)

なぜ eAPI か:
  Arista cEOS の NETCONF では <state/>サブツリーフィルターが 0件を返す（実機確認済み）。
  オペレーショナルデータは eAPI show コマンド経由でのみ確実に取得できる。

起動:
    python arista_eapi_show_a2a_server.py

環境変数:
    A2A_PORT       : ポート番号（デフォルト: 8002）
    A2A_PUBLIC_URL : 外部公開URL（デフォルト: http://localhost:8002）
    FAISS_PATH     : faiss_db のパス（デフォルト: ./faiss_db/arista_eapi）
    SASE_CONFIG    : config.ini のパス
    EAPI_HOST      : デバイスIP（デフォルト: 172.20.100.31）
    EAPI_PORT_NUM  : eAPI ポート番号（デフォルト: 443）
    EAPI_TRANSPORT : http or https（デフォルト: https）
    EAPI_USER      : ユーザー名（デフォルト: admin）
    EAPI_PASS      : パスワード（デフォルト: admin）

リクエスト形式（JSON）:
    {
        "query":   "インターフェースの状態を確認してください",
        "device_ip":   "172.20.100.31",   # 省略可（環境変数 EAPI_HOST を使用）
        "username":    "admin",            # 省略可
        "password":    "admin",            # 省略可
        "port":        443,                # 省略可
        "transport":   "https"             # 省略可
    }
    または単純なテキスト（環境変数の接続設定を使用）

レスポンス形式（JSON）:
    {
        "query":          "...",
        "cmd":            "show interfaces",
        "status":         "success",
        "result":         { ... },          # pyeapi からの生レスポンス
        "summary":        get_msg("eapi_success"),
        "scope_note":     get_msg("scope_note_eapi")
    }

[アーキテクチャ]
  A2AStarletteApplication
    └─ DefaultRequestHandler
         └─ AristaEapiShowExecutor (AgentExecutor)
               ├─ eAPI RAG: show コマンド生成（FAISS + LLM）
               └─ pyeapi.client.Node.run_commands() 実行（https/443）

[接続確認済みパラメータ]
  transport = https, port = 443  （HTTP/80 は shutdown 実機確認済み）
  SSL証明書: 自己署名（urllib3 警告を無効化済み）
"""

import os
import re
import json
import logging
import configparser
import urllib3
from typing import Any, Dict, List, Optional

import uvicorn
# ── 多言語対応 ────────────────────────────────────────────────────────────
from i18n import get_msg, locale_from_request, LOCALE

import pyeapi

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
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_core.output_parsers import StrOutputParser

# ── LLM ファクトリ（Groq Primary / Azure Fallback 共通モジュール） ─────────────
from llm_factory import build_llm_with_fallback, log_llm_config, LLM_PROVIDER_NAME

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── 設定 ──────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.getenv(
    "SASE_CONFIG",
    os.path.join(BASE_DIR, "./config.ini")
)
GROQ_BASE_URL  = "https://api.groq.com/openai/v1"   # ログ用に残す
DEFAULT_MODEL  = "llama-3.3-70b-versatile"
FAISS_PATH     = os.getenv("FAISS_PATH",
                            os.path.join(BASE_DIR, "faiss_db", "arista_eapi"))

A2A_HOST       = os.getenv("A2A_HOST", "0.0.0.0")
A2A_PORT       = int(os.getenv("A2A_PORT",   "8002"))
SDIFF_PORT     = int(os.getenv("SDIFF_PORT", "8009"))  # session-diff REST API 専用ポート
A2A_PUBLIC_URL = os.getenv("A2A_PUBLIC_URL", f"http://localhost:{A2A_PORT}")

# デフォルト接続設定（実機確認済みパラメータ）
DEFAULT_EAPI_HOST      = os.getenv("EAPI_HOST",      "172.20.100.31")
DEFAULT_EAPI_PORT      = int(os.getenv("EAPI_PORT_NUM", "443"))
DEFAULT_EAPI_TRANSPORT = os.getenv("EAPI_TRANSPORT", "https")
DEFAULT_EAPI_USER      = os.getenv("EAPI_USER",      "admin")
DEFAULT_EAPI_PASS      = os.getenv("EAPI_PASS",      "admin")


def _init_llm():
    """LLM インスタンスを構築する（llm_factory 経由 Groq→Azure 自動切り替え）。"""
    return build_llm_with_fallback()


def _init_retriever():
    if not os.path.exists(FAISS_PATH):
        logger.warning(f"eAPI faiss_db が見つかりません: {FAISS_PATH}")
        logger.warning("RAG なしで動作します（LLM の知識で show コマンドを生成）")
        return None
    logger.info(f"eAPI FAISS ロード中: {FAISS_PATH}")
    embedding   = HuggingFaceEmbeddings(model_name="BAAI/bge-large-en-v1.5")
    vectorstore = FAISS.load_local(
        FAISS_PATH, embedding, allow_dangerous_deserialization=True)
    logger.info("eAPI FAISS ロード完了")
    return vectorstore.as_retriever(search_kwargs={"k": 5})


# ═══════════════════════════════════════════════════════════════════════════════
# eAPI RAG: show コマンド生成
# ═══════════════════════════════════════════════════════════════════════════════

EAPI_READ_TEMPLATE = """
あなたは Arista EOS eAPI に特化したネットワークエンジニアリングアシスタントです。
以下のコンテキスト（eAPI仕様書）を参考にしつつ、
あなた自身の Arista EOS の知識も活用して、
ユーザーの質問に対する「参照・確認のための show コマンド」を生成してください。

【重要な制約】
- 生成するコマンドは必ず "show" で始まる参照コマンドのみ
- 設定変更コマンド (configure, interface, ip address, no 等) は絶対に含めない
- コンテキストに該当情報がなくても Arista EOS の標準 show コマンドを使用すること
- 回答は必ず以下の JSON のみ。```json ブロックで囲むこと。説明文は不要。

【BGP コマンドの制約】
- BGP の状態確認には必ず "show ip bgp summary" を使用すること
- "show bgp summary" は使用禁止（EOS ネイティブ形式は JSON キーが異なるため）
- 理由: 社内は Cisco 機器ベースのため Cisco 互換形式（show ip bgp summary）に統一する

```json
{{
  "jsonrpc": "2.0",
  "method": "runCmds",
  "params": {{
    "format": "json",
    "version": 1,
    "cmds": ["<show コマンド>"]
  }},
  "id": "eapi-show"
}}
```

コンテキスト:
{context}

質問:
{question}
"""

# 安全ガード: 変更系コマンドを含む場合はブロック
_FORBIDDEN_PREFIXES = [
    "configure", "interface", "ip ", "no ", "shutdown",
    "vlan ", "router ", "enable",
]


def _build_read_chain(retriever, llm):
    """eAPI RAG チェーンを構築する。retriever が None の場合は LLM のみで生成。"""
    if retriever is None:
        # RAG なし: LLM の知識のみで show コマンドを生成
        prompt = ChatPromptTemplate.from_template(
            EAPI_READ_TEMPLATE.replace("{context}", "(コンテキストなし — EOS標準コマンドを使用)")
        )
        return (
            {"context": lambda _: "(コンテキストなし)", "question": RunnablePassthrough()}
            | prompt | llm | StrOutputParser()
        )
    return (
        {"context": retriever, "question": RunnablePassthrough()}
        | ChatPromptTemplate.from_template(EAPI_READ_TEMPLATE)
        | llm | StrOutputParser()
    )


def _parse_payload(response: str) -> Optional[Dict]:
    m = re.search(r"```json\s*(.*?)\s*```", response, re.DOTALL)
    if m:
        raw = m.group(1).strip()
    else:
        brace = response.find("{")
        raw = response[brace:].strip() if brace != -1 else response.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _is_error_payload(p: Dict) -> bool:
    return "error" in p and "jsonrpc" not in p


def _is_read_only(p: Dict) -> bool:
    for cmd in p.get("params", {}).get("cmds", []):
        if any(str(cmd).strip().lower().startswith(w) for w in _FORBIDDEN_PREFIXES):
            return False
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# pyeapi 実行（接続確認済みパラメータ）
# ═══════════════════════════════════════════════════════════════════════════════

def _eapi_node(host: str, port: int, transport: str,
               username: str, password: str) -> "pyeapi.client.Node":
    """
    pyeapi.connect() → Node 経由で接続する。
    実機確認済み: transport=https, port=443（HTTP/80はshutdown）
    SSL証明書: 自己署名（urllib3 警告は起動時に無効化済み）
    """
    conn = pyeapi.connect(
        transport=transport, host=host,
        username=username, password=password, port=port,
    )
    return pyeapi.client.Node(conn)


# ═══════════════════════════════════════════════════════════════════════════════
# configure session diff（ハイブリッド・トランザクション方式）
#
# cEOS は NETCONF 経由での diff 取得が困難だが、eAPI 経由では
# configure session → load → show session-config diffs → abort
# で Junos の cu.diff() と同等の +/- diff が取得できる（PDF 調査結果より）。
# ═══════════════════════════════════════════════════════════════════════════════

SESSION_NAME = "nicegui_preview"  # 常に同一セッション名を使う（abort で確実にクリア）

def _cmds_from_xml(xml_str: str) -> list[str]:
    """
    NETCONF XML (OpenConfig) から EOS CLI コマンド列を生成する。

    実際の XML 構造（NETCONFサーバ生成）:
      <config>
        <network-instances xmlns="...">
          <network-instance>
            <name>default</name>          ← "default" (数字でない)
            <vlans>
              <vlan>
                <vlan-id>106</vlan-id>    ← ★ VLAN ID はここ
                <config>
                  <vlan-id>106</vlan-id>
                  <name>DEV6_VLAN</name>  ← ★ VLAN name はここ
                </config>
              </vlan>
            </vlans>
          </network-instance>
        </network-instances>
      </config>

      <interfaces xmlns="...">
        <interface>
          <name>Ethernet1</name>
          <config>
            <description>uplink-to-core</description>
          </config>
        </interface>
      </interfaces>

    戦略:
      - VLAN: <vlan-id> 要素を直接 iter で探す（親の <name> が数字かどうかに依存しない）
      - Interface: <interface> 内の <name> と <description> を探す
    """
    import xml.etree.ElementTree as ET

    def _local(tag: str) -> str:
        return tag.split("}")[-1] if "}" in tag else tag

    try:
        root = ET.fromstring(xml_str.strip())
    except ET.ParseError:
        return []

    cmds: list[str] = []
    seen_vlans: set = set()  # 重複防止

    # ── VLAN: <vlan-id> 要素を直接検索 ──────────────────────────────────────
    # <vlan> 要素の中にある <vlan-id> を探し、同じ <vlan> 内の <config><name> を取得
    for vlan_el in root.iter():
        if _local(vlan_el.tag) != "vlan":
            continue
        # operation="delete" 属性を確認（削除操作）
        # <vlan operation="delete"> または <vlan><config operation="delete">
        is_delete = False
        vlan_attrs = {_local(k): v for k, v in vlan_el.attrib.items()}
        if vlan_attrs.get("operation") == "delete":
            is_delete = True

        # <vlan-id> (直接の子)
        vid = ""
        for child in vlan_el:
            if _local(child.tag) == "vlan-id":
                vid = (child.text or "").strip()
                break
        if not vid or not vid.isdigit():
            continue
        if vid in seen_vlans:
            continue
        seen_vlans.add(vid)

        if is_delete:
            # 削除: no vlan {id}
            cmds.append(f"no vlan {vid}")
        else:
            # 追加/更新: vlan {id} + name
            # <config><name> から VLAN name を取得
            vlan_name = ""
            for child in vlan_el:
                if _local(child.tag) == "config":
                    for sub in child:
                        if _local(sub.tag) == "name":
                            vlan_name = (sub.text or "").strip()
                            break
                    break

            cmds.append(f"vlan {vid}")
            if vlan_name:
                cmds.append(f"   name {vlan_name}")

    # ── Interface: <interface> 内の <name> と <config><description> ──────────
    seen_intfs: set = set()
    for intf_el in root.iter():
        if _local(intf_el.tag) != "interface":
            continue
        intf_name = ""
        for child in intf_el:
            if _local(child.tag) == "name":
                intf_name = (child.text or "").strip()
                break
        if not intf_name or intf_name in seen_intfs:
            continue

        desc_val = ""
        for child in intf_el:
            if _local(child.tag) == "config":
                for sub in child:
                    if _local(sub.tag) == "description":
                        desc_val = (sub.text or "").strip()
                        break
                break

        if desc_val:
            seen_intfs.add(intf_name)
            cmds.extend([
                f"interface {intf_name}",
                f"   description {desc_val}",
            ])

    return cmds


    return cmds


def session_diff(
    xml_str:   str,
    host:      str,
    port:      int,
    transport: str,
    username:  str,
    password:  str,
) -> dict:
    """
    cEOS の configure session を使い、XML 相当設定の +/- diff を取得する。

    フロー:
      1. XML → CLI コマンド列に変換
      2. configure session {SESSION_NAME}
      3. 変換したCLIコマンドを投入
      4. show session-config diffs
      5. abort（running-config は一切変更しない）

    戻り値:
      {
        "status":      "success" | "skipped" | "error",
        "diff_text":   "+vlan 100\n+ name DEV_VLAN\n...",
        "diff_lines":  [{"op": "+"|"-"|" ", "text": "..."}, ...],
        "cmds":        ["vlan 100", "  name DEV_VLAN"],
        "message":     str,
      }

    - diff_text が空 (no changes) でも status="success" で返す
    - CLI 変換できない XML は status="skipped" で返す（エラーではない）
    - pyeapi 接続失敗は status="error"
    """
    cmds = _cmds_from_xml(xml_str)
    if not cmds:
        return {
            "status":     "skipped",
            "diff_text":  "",
            "diff_lines": [],
            "cmds":       [],
            "message":    "session diff: XML→CLI 変換対象外のため スキップ",
        }

    try:
        node = _eapi_node(host, port, transport, username, password)

        # ── 全コマンドを1回の run_commands() にまとめる ────────────────────
        # pyeapi は呼び出しごとにコンテキストがリセットされるため、
        # configure session → 設定投入 → show diffs → abort を
        # 必ず1回の run_commands() で実行しなければならない。
        #
        # run_commands() は encoding="text" のとき全コマンドをテキストで返す。
        # encoding 混在は不可のため、設定コマンド（出力なし）の結果は無視する。
        #
        # コマンド列:
        #   1. configure session <NAME>  ← セッション開始
        #   2. <config cmds...>          ← 設定投入（出力なし）
        #   3. show session-config diffs ← diff 取得
        #   4. abort                     ← セッション破棄（running-config 変更なし）
        all_cmds = (
            [f"configure session {SESSION_NAME}"]
            + cmds
            + ["show session-config diffs", "abort"]
        )

        result_list = node.run_commands(all_cmds, encoding="text")

        # show session-config diffs の結果は末尾から2番目（abort の前）
        # インデックス = len(cmds) + 1  (configure session=0, cmds=1..N, show=N+1, abort=N+2)
        diff_idx = len(cmds) + 1
        raw_diff = ""
        if isinstance(result_list, list) and len(result_list) > diff_idx:
            entry = result_list[diff_idx]
            if isinstance(entry, dict):
                raw_diff = entry.get("output", "")
            else:
                raw_diff = str(entry)

        # diff テキストをパース（+/-/空白 の行ごとに分類）
        diff_lines = []
        for line in raw_diff.splitlines():
            if line.startswith("+"):
                diff_lines.append({"op": "+", "text": line[1:].rstrip()})
            elif line.startswith("-"):
                diff_lines.append({"op": "-", "text": line[1:].rstrip()})
            elif line.strip():
                diff_lines.append({"op": " ", "text": line.rstrip()})

        return {
            "status":     "success",
            "diff_text":  raw_diff,
            "diff_lines": diff_lines,
            "cmds":       cmds,
            "message":    f"session diff 取得成功 ({len(diff_lines)} 行)",
        }

    except pyeapi.eapilib.ConnectionError as e:
        return {
            "status":     "error",
            "diff_text":  "",
            "diff_lines": [],
            "cmds":       cmds,
            "message":    f"eAPI 接続エラー: {e}",
        }
    except Exception as e:
        return {
            "status":     "error",
            "diff_text":  "",
            "diff_lines": [],
            "cmds":       cmds,
            "message":    f"session diff エラー: {e}",
        }



def _format_interfaces(result_list: List[Dict]) -> Optional[str]:
    """
    eAPI show コマンドの結果を人間が読みやすいテキスト形式に変換する（structured パース）。

    対応コマンド:
      show interfaces / show interfaces description → テーブル形式
      show version                                   → キー:値形式
      show vlan                                      → テーブル形式
      show ip route / show lldp neighbors            → テーブル形式
      その他（show interfaces counters 等）           → None を返す（LLM パースへ委譲）
    """
    lines = []
    for res in result_list:
        # ── show interfaces ──────────────────────────────────────────────
        # show interfaces counters も "interfaces" キーを持つが、
        # interfaceStatus / lineProtocolStatus を持たないため厳密に区別する。
        # 先頭インターフェースに interfaceStatus があれば show interfaces と判定。
        if "interfaces" in res and any(
            "interfaceStatus" in info
            for info in res["interfaces"].values()
        ):
            # ヘッダー幅: Description は切り捨てなし（[:18] を撤廃）
            lines.append(f"{'Interface':<16} {'Status':<12} {'Proto':<8} Description")
            lines.append("-" * 70)
            for intf, info in res["interfaces"].items():
                desc = info.get('description', '')   # ← 切り捨てなし
                lines.append(
                    f"  {intf:<14} "
                    f"{info.get('interfaceStatus', '?'):<12} "
                    f"{info.get('lineProtocolStatus', '?'):<8} "
                    f"{desc}"
                )

        # ── show interfaces description ───────────────────────────────────
        elif "interfaceDescriptions" in res:
            lines.append(f"{'Interface':<16} {'Status':<12} Description")
            lines.append("-" * 60)
            for intf, info in res["interfaceDescriptions"].items():
                desc = info.get('description', '')   # ← 切り捨てなし
                lines.append(
                    f"  {intf:<14} "
                    f"{info.get('interfaceStatus', '?'):<12} "
                    f"{desc}"
                )

        # ── show version ──────────────────────────────────────────────────
        elif "version" in res:
            lines.append(f"  EOS Version  : {res.get('version', '?')}")
            lines.append(f"  Model        : {res.get('modelName', '?')}")
            lines.append(f"  Serial       : {res.get('serialNumber', '?')}")
            lines.append(f"  System MAC   : {res.get('systemMacAddress', '?')}")
            lines.append(f"  Uptime       : {res.get('uptime', '?'):.0f}s" if isinstance(res.get('uptime'), float) else f"  Uptime       : {res.get('uptime', '?')}")
            lines.append(f"  Mem Total    : {res.get('memTotal', '?')} kB")

        # ── show vlan ─────────────────────────────────────────────────────
        elif "vlans" in res:
            lines.append(f"{'VLAN':<6} {'Name':<20} Status")
            lines.append("-" * 38)
            for vid, vinfo in sorted(res["vlans"].items(), key=lambda x: int(x[0])):
                name   = vinfo.get("name", "")[:18]
                status = vinfo.get("status", "?")
                lines.append(f"  {vid:<4} {name:<20} {status}")

        # ── show lldp neighbors ───────────────────────────────────────────
        elif "lldpNeighbors" in res:
            lines.append(f"{'ローカルIF':<18} {'ネイバーデバイス':<26} {'ネイバーIF':<20}")
            lines.append("-" * 62)
            for nb in res["lldpNeighbors"]:
                lines.append(
                    f"  {nb.get('port',''):<16} "
                    f"{nb.get('neighborDevice',''):<24} "
                    f"{nb.get('neighborPort','')}"
                )

        # ── show ip bgp summary (Cisco 互換形式・統一コマンド) ──────────────
        # JSON 構造（実機 show ip bgp summary 確認済み）:
        #   vrfs.default.routerId   : "1.1.1.1"
        #   vrfs.default.asn        : "65001"        ← Local AS
        #   vrfs.default.peers
        #     [peer_ip].asn         : "65002"        ← Remote AS（実機確認済み）
        #     [peer_ip].peerState   : "Estab"        ← 実機確認済み
        #     [peer_ip].upDownTime  : "00:37:29"     ← 実機確認済み
        #     [peer_ip].pfxRcd / prefixReceived / nlrisReceived  ← フォールバック
        #     [peer_ip].msgRcvd / msgSent / inq / outq
        #
        # 注意: show bgp summary（EOS ネイティブ）は BGP コマンド正規化で
        #       show ip bgp summary に自動変換済みのためここには来ない。
        #       ただし念のため peers に peerAsn がある場合も同じ表示形式で処理する。
        elif "vrfs" in res and any(
            "peers" in vrf or "routerId" in vrf
            for vrf in res["vrfs"].values()
        ):
            def _bgp_val(d: dict, *keys: str, default: str = "?") -> str:
                """
                複数キー候補をフォールバックで検索して文字列で返す。
                EOS バージョン差異・コマンド差異を吸収する。
                """
                for k in keys:
                    v = d.get(k)
                    if v is not None:
                        return str(v)
                return default

            def _updown_str(val) -> str:
                """
                upDownTime を人間が読める Up/Down 表示に変換する。

                EOS バージョンによって2種類の値が返る:
                  (A) float / int: Unix エポック秒（セッション確立時刻）
                      → 現在時刻との差分を計算して経過時間に変換する
                      例: 1778750714.371786 → "01:20:44"
                  (B) str: すでに "HH:MM:SS" 形式
                      → そのまま使用する
                      例: "00:37:29" → "00:37:29"

                表示形式（Cisco IOS 互換）:
                  < 1日  : HH:MM:SS
                  >= 1日 : Xd HH:MM:SS
                """
                if val is None:
                    return "?"
                # (A) 数値（エポック秒）の判定
                # 文字列でも float 変換できれば数値扱い（"00:37:29" は変換不可）
                try:
                    epoch = float(val)
                    import time as _time
                    elapsed  = max(0.0, _time.time() - epoch)
                    total_s  = int(elapsed)
                    days     = total_s // 86400
                    hours    = (total_s % 86400) // 3600
                    minutes  = (total_s % 3600)  // 60
                    seconds  = total_s % 60
                    if days > 0:
                        return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
                    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                except (ValueError, TypeError):
                    # (B) 文字列（"HH:MM:SS"）はそのまま使用
                    return str(val)

            def _pfx_rcvd(peer: dict) -> str:
                """
                受信プレフィックス数を取得する。
                  show ip bgp summary : pfxRcd | prefixReceived
                  show bgp summary    : ipv4Unicast.nlrisReceived（フォールバック）
                """
                # show ip bgp summary のキー（実機確認済み優先）
                for k in ("pfxRcd", "prefixReceived", "prefixRcvd"):
                    v = peer.get(k)
                    if v is not None:
                        return str(v)
                # show bgp summary のネスト構造（フォールバック）
                for afi_key in ("ipv4Unicast", "ipv6Unicast"):
                    afi = peer.get(afi_key, {})
                    for k in ("nlrisReceived", "nlrisAccepted"):
                        v = afi.get(k)
                        if v is not None:
                            return str(v)
                return "?"

            def _pfx_adv(peer: dict) -> str:
                """送信プレフィックス数を取得する。"""
                for k in ("pfxSnt", "prefixAdvertised", "prefixSent"):
                    v = peer.get(k)
                    if v is not None:
                        return str(v)
                for afi_key in ("ipv4Unicast", "ipv6Unicast"):
                    afi = peer.get(afi_key, {})
                    v = afi.get("nlrisAdvertised")
                    if v is not None:
                        return str(v)
                return "?"

            for vrf_name, vrf in res["vrfs"].items():
                router_id = vrf.get("routerId", "?")
                local_as  = _bgp_val(vrf, "asn", "localAs")
                lines.append(f"BGP summary (VRF: {vrf_name})")
                lines.append(
                    f"  Router-ID : {router_id}  "
                    f"Local AS  : {local_as}"
                )
                peers = vrf.get("peers", {})
                if not peers:
                    lines.append("  (BGP ピアなし / BGP 未設定)")
                    continue

                # ヘッダー（Cisco IOS の show ip bgp summary に合わせた列構成）
                lines.append("")
                lines.append(
                    f"  {'Neighbor':<16} {'V':<3} {'AS':<8} "
                    f"{'Up/Down':<12} {'State':<12} "
                    f"{'PfxRcd':>7} {'PfxAdv':>7}"
                )
                lines.append("  " + "-" * 72)

                for peer_ip, peer in peers.items():
                    # Remote AS: asn（show ip bgp summary）/ peerAsn（show bgp summary）
                    peer_as  = _bgp_val(peer, "asn", "peerAsn", "peerAs", "remoteAs")
                    version  = _bgp_val(peer, "version",  default="4")
                    state    = _bgp_val(peer, "peerState", "state")
                    updown   = _updown_str(peer.get("upDownTime") or peer.get("upDown"))
                    pfx_rcvd = _pfx_rcvd(peer)
                    pfx_adv  = _pfx_adv(peer)
                    lines.append(
                        f"  {peer_ip:<16} {version:<3} {peer_as:<8} "
                        f"{updown:<12} {state:<12} "
                        f"{pfx_rcvd:>7} {pfx_adv:>7}"
                    )

        # ── show ip route ─────────────────────────────────────────────────
        elif "vrfs" in res:
            for vrf_name, vrf in res["vrfs"].items():
                lines.append(f"VRF: {vrf_name}")
                for prefix, route_info in vrf.get("routes", {}).items():
                    via = ""
                    for entry in route_info.get("routeAction", [{}]):
                        via = entry.get("nexthopAddr", "")
                    intf = route_info.get("routeLeakedFrom", "")
                    lines.append(f"  {prefix:<22} via {via} {intf}".rstrip())

        # ── show ntp status ───────────────────────────────────────────────
        # eAPI 実機確認済みキー: status, server, stratum, pollingInterval,
        #   maxEstimatedError, referenceClock, timeSinceLastSync 等
        elif "status" in res and "stratum" in res:
            status_val  = res.get("status", "?")
            server_val  = res.get("server", res.get("referenceClock", "?"))
            stratum_val = res.get("stratum", "?")
            poll_val    = res.get("pollingInterval", "?")
            err_val     = res.get("maxEstimatedError", "?")
            sync_val    = res.get("timeSinceLastSync", "?")

            # status の絵文字マップ
            _STATUS_ICON = {
                "synchronised": "✅",
                "synchronized": "✅",
                "unsynchronised": "⚠️",
                "unsynchronized": "⚠️",
            }
            icon = _STATUS_ICON.get(str(status_val).lower(), "❓")

            lines.append(f"NTP Status")
            lines.append("-" * 42)
            lines.append(f"  {icon} Status          : {status_val}")
            lines.append(f"  Server           : {server_val}")
            lines.append(f"  Stratum          : {stratum_val}")
            lines.append(f"  Poll Interval    : {poll_val} s")
            if err_val != "?":
                lines.append(f"  Max Est. Error   : {err_val} ms")
            if sync_val != "?":
                lines.append(f"  Since Last Sync  : {sync_val} s")

        # ── show ntp associations ─────────────────────────────────────────
        # eAPI 実機確認済みキー: peers[ip].condition, peerIpAddr,
        #   refid, stratumLevel, peerType, lastReceived,
        #   pollInterval, reachabilityHistory
        elif "peers" in res:
            peers = res.get("peers", {})
            lines.append(f"NTP Associations")
            lines.append("-" * 72)
            lines.append(
                f"  {'Peer IP':<18} {'Condition':<12} {'Stratum':<8} "
                f"{'RefID':<10} {'Poll':>5} {'Reachable'}"
            )
            lines.append("  " + "-" * 68)
            for peer_ip, peer_info in peers.items():
                condition   = peer_info.get("condition", "?")
                stratum_lvl = peer_info.get("stratumLevel", "?")
                refid       = peer_info.get("refid", "?")
                poll        = peer_info.get("pollInterval", "?")
                reach_hist  = peer_info.get("reachabilityHistory", [])
                # 直近8回の到達性を "✓✗" 形式で表示
                reach_str   = "".join(
                    "✓" if r else "✗" for r in reach_hist[:8]
                ) if reach_hist else "?"
                # sys.peer / sys.peerなど condition の絵文字
                _COND_ICON = {
                    "sys.peer": "★", "sys peer": "★",
                    "candidate": "○", "reject": "✗",
                    "outlier": "△", "falseticker": "✗",
                }
                cond_icon = _COND_ICON.get(str(condition).lower(), "")
                cond_str  = f"{cond_icon}{condition}" if cond_icon else condition
                lines.append(
                    f"  {peer_ip:<18} {cond_str:<12} {str(stratum_lvl):<8} "
                    f"{str(refid):<10} {str(poll):>5}s  {reach_str}"
                )

        # ── その他: structured パース未対応 → None を返して LLM パースへ委譲 ──
        else:
            return None

    return "\n".join(lines) if lines else None


# ═══════════════════════════════════════════════════════════════════════════════
# LLM パース: task_decompose A2A Server (8000) に raw データを送ってテキスト整形
# ═══════════════════════════════════════════════════════════════════════════════

# task_decompose Hub の URL（直接 LLM 整形を依頼するために使用）
TASK_DECOMPOSE_URL = os.getenv("TASK_DECOMPOSE_URL", "http://localhost:8000")

LLM_PARSE_PROMPT_TEMPLATE = """
あなたは Arista EOS のネットワークエンジニアリングアシスタントです。
以下の eAPI show コマンドの実行結果（JSON）を、
ネットワークエンジニアが読みやすい日本語テキスト形式に整形してください。

【整形ルール】
- インターフェース名、カウンタ値、レート等を表形式またはリスト形式で表示
- 数値は単位（bps, pps, packets, errors 等）を付けて表示
- 不要な内部フィールドは省略し、重要な情報を優先
- 日本語で簡潔に、ただし数値は正確に
- ```json や ``` ブロックで囲まないこと（プレーンテキストで返す）

【BGP summary の場合の整形指示】
show ip bgp summary の JSON は vrfs > {vrf名} > peers の構造を持つ。
以下の形式で表示すること:
  BGP summary (VRF: default)
    Router-ID : {routerId}  Local AS : {asn}

    Neighbor        AS      State        Up/Down    PfxRcvd
    -------------------------------------------------------
    {peer_ip}       {AS}    {peerState}  {upDownTime}  {pfxRcd/prefixReceived}

  ※ peers が空の場合は「BGP ピアなし」と表示する。
  ※ AS 番号は peers の各エントリが持つ asn または peerAs フィールドから取得する。

実行コマンド: {cmds}

eAPI 実行結果 (JSON):
{raw_json}
"""


async def _llm_format_raw(cmds: List[str], raw_result: List[Dict], llm: ChatOpenAI) -> str:
    """
    structured パース未対応のコマンド結果を LLM で整形する。
    task_decompose_a2a_server の LLM インスタンスを直接呼ぶのではなく、
    同じ LLM（GROQ）を使ってローカルで整形する（A2A 通信不要）。
    """
    try:
        # raw_result が大きすぎる場合は先頭部分のみ渡す
        raw_json = json.dumps(raw_result, ensure_ascii=False, indent=2)
        if len(raw_json) > 8000:
            raw_json = raw_json[:8000] + "\n... (省略)"

        prompt = LLM_PARSE_PROMPT_TEMPLATE.format(
            cmds=cmds,
            raw_json=raw_json,
        )
        response = llm.invoke(prompt)
        text = response.content.strip() if hasattr(response, "content") else str(response).strip()
        return text if text else json.dumps(raw_result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"LLM パース失敗: {e}")
        # フォールバック: raw JSON をそのまま返す
        return json.dumps(raw_result, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════════════
# A2A AgentExecutor
# ═══════════════════════════════════════════════════════════════════════════════

class AristaEapiShowExecutor(AgentExecutor):
    """
    eAPI RAG + pyeapi show コマンドを A2A プロトコルで公開するアダプタ。

    フロー:
      1. リクエストを受信（テキスト or JSON）
      2. eAPI RAG で show コマンドを生成（FAISS + LLM）
      3. 安全ガード（show コマンドのみ許可）
      4. pyeapi https/443 で実機に送信
      5. 結果を JSON で返却
    """

    def __init__(self, retriever, llm):
        self._retriever = retriever
        self._llm       = llm
        logger.info("AristaEapiShowExecutor 初期化完了（ハイブリッドパース有効）")

    def _parse_request(self, text: str) -> dict:
        text = text.strip()
        try:
            params = json.loads(text)
            if isinstance(params, dict) and "query" in params:
                return params
        except json.JSONDecodeError:
            pass
        return {"query": text}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        raw_text = ""
        for part in context.message.parts:
            if hasattr(part.root, "text"):
                raw_text += part.root.text

        if not raw_text.strip():
            await event_queue.enqueue_event(
                new_agent_text_message("メッセージが空です。"))
            return

        params    = self._parse_request(raw_text)
        query     = params.get("query", raw_text)
        host      = params.get("device_ip",  DEFAULT_EAPI_HOST)
        port      = int(params.get("port",   DEFAULT_EAPI_PORT))
        transport = params.get("transport",  DEFAULT_EAPI_TRANSPORT)
        username  = params.get("username",   DEFAULT_EAPI_USER)
        password  = params.get("password",   DEFAULT_EAPI_PASS)

        logger.info(f"受信: {query[:80]}... ({transport}://{host}:{port})")

        try:
            # ① eAPI RAG で show コマンド生成
            chain    = _build_read_chain(self._retriever, self._llm)
            response = chain.invoke(query)
            logger.info(f"RAG生成: {response[:200]}")

            # ② JSON パース
            payload = _parse_payload(response)
            if payload is None:
                await event_queue.enqueue_event(
                    new_agent_text_message(json.dumps({
                        "status": "error",
                        "message": get_msg("eapi_parse_fail"),
                        "raw_response": response[:500],
                    }, ensure_ascii=True, indent=2)))
                return

            if _is_error_payload(payload):
                await event_queue.enqueue_event(
                    new_agent_text_message(json.dumps({
                        "status": "error",
                        "message": f"RAGエラー応答: {payload.get('error')}",
                    }, ensure_ascii=True, indent=2)))
                return

            # ③ 安全ガード
            if not _is_read_only(payload):
                await event_queue.enqueue_event(
                    new_agent_text_message(json.dumps({
                        "status": "blocked",
                        "message": get_msg("eapi_blocked"),
                        "payload": payload,
                    }, ensure_ascii=True, indent=2)))
                return

            cmds = payload.get("params", {}).get("cmds", [])

            # ── コマンド正規化 ────────────────────────────────────────────────
            # LLM が生成する曖昧・不完全コマンドを cEOS で有効な形式に変換する。
            #
            # 【BGP】
            #   "show bgp summary" → "show ip bgp summary"
            #   理由: EOS ネイティブ形式は JSON キーが異なり structured パース不可。
            #
            # 【NTP】
            #   "show ntp"         → "show ntp status"  （不完全コマンドエラー回避）
            #   "show ntp detail"  → "show ntp status"
            #   "show ntp peers"   → "show ntp associations"
            #   "show ntp servers" → "show ntp associations"
            #   理由: cEOS は "show ntp" 単体が invalid command になる（実機確認済み）。
            #         "状態" クエリ → show ntp status、"一覧/ピア" クエリ → show ntp associations。
            # ─────────────────────────────────────────────────────────────────
            normalized_cmds = []
            for cmd in cmds:
                c = str(cmd).strip().lower()

                # BGP 正規化
                if c == "show bgp summary":
                    logger.info("コマンド正規化: 'show bgp summary' → 'show ip bgp summary'")
                    normalized_cmds.append("show ip bgp summary")

                # NTP 正規化
                elif c == "show ntp" or c == "show ntp detail":
                    logger.info(f"コマンド正規化: '{cmd}' → 'show ntp status'")
                    normalized_cmds.append("show ntp status")
                elif c in ("show ntp peers", "show ntp servers",
                           "show ntp peer", "show ntp server"):
                    logger.info(f"コマンド正規化: '{cmd}' → 'show ntp associations'")
                    normalized_cmds.append("show ntp associations")

                else:
                    normalized_cmds.append(cmd)
            cmds = normalized_cmds

            logger.info(f"実行コマンド: {cmds}")

            # ④ pyeapi 実行（https/443 確定済み）
            node       = _eapi_node(host, port, transport, username, password)
            raw_result = node.run_commands(cmds, encoding="json")

            # ⑤ ハイブリッドパース:
            #    structured パース（既定フォーマット対応コマンド）→ None なら LLM パース
            structured_text = _format_interfaces(raw_result)

            if structured_text is not None:
                # 既定フォーマット対応コマンド: structured パース結果を使用
                parse_method   = "structured"
                formatted_text = structured_text
                logger.info(f"パース方式: structured ({cmds})")
            else:
                # 未対応コマンド（show interfaces counters 等）:
                # raw データを LLM に送って整形させる
                logger.info(f"パース方式: LLM ({cmds}) — structured パース未対応")
                formatted_text = await _llm_format_raw(cmds, raw_result, self._llm)
                parse_method   = "llm"

            response_payload = {
                "query":          query,
                "cmds":           cmds,
                "status":         "success",
                "summary":        get_msg("eapi_success"),
                "scope_note":     get_msg("scope_note_eapi"),
                "parse_method":   parse_method,   # structured / llm
                "formatted_text": formatted_text, # ハイブリッドパース結果（常に文字列）
                # 後方互換: "formatted" キーも同じ値で残す
                "formatted":      formatted_text,
                "raw_result":     raw_result,
                "connection": {
                    "transport": transport,
                    "host":      host,
                    "port":      port,
                },
            }

            logger.info(f"完了: cmds={cmds}")
            # ensure_ascii=True: 日本語・制御文字を全て \uXXXX / \n にエスケープ
            await event_queue.enqueue_event(
                new_agent_text_message(
                    json.dumps(response_payload, ensure_ascii=True)))

        except pyeapi.eapilib.ConnectionError as e:
            logger.error(f"eAPI 接続エラー: {e}")
            await event_queue.enqueue_event(
                new_agent_text_message(json.dumps({
                    "status":  "error",
                    "message": f"eAPI 接続エラー: {e}",
                    "hint": (
                        f"接続先: {transport}://{host}:{port} "
                        "確認事項: HTTPS/443 が稼働しているか "
                        "(HTTP/80 は shutdown 実機確認済み)"
                    ),
                }, ensure_ascii=True, indent=2)))

        except Exception as e:
            logger.error(f"eAPI 実行エラー: {e}", exc_info=True)
            await event_queue.enqueue_event(
                new_agent_text_message(json.dumps({
                    "status":  "error",
                    "message": get_msg("error") + f": {e}",
                }, ensure_ascii=True, indent=2)))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise UnsupportedOperationError("キャンセルはサポートされていません。")


# ═══════════════════════════════════════════════════════════════════════════════
# FastAPI REST エンドポイント（Hub から直接呼び出し用）
#   POST /session-diff  → session_diff() を実行して JSON 返却
# ═══════════════════════════════════════════════════════════════════════════════

from fastapi import FastAPI as _FastAPI
from fastapi.middleware.cors import CORSMiddleware as _CORSMiddleware
from pydantic import BaseModel as _BaseModel

_rest = _FastAPI(title="eAPI session-diff endpoint")
_rest.add_middleware(
    _CORSMiddleware, allow_origins=["*"],
    allow_methods=["*"], allow_headers=["*"],
)


class _SessionDiffRequest(_BaseModel):
    xml_str:   str
    device_ip: str  = DEFAULT_EAPI_HOST
    port:      int  = DEFAULT_EAPI_PORT
    transport: str  = DEFAULT_EAPI_TRANSPORT
    username:  str  = DEFAULT_EAPI_USER
    password:  str  = DEFAULT_EAPI_PASS


@_rest.post("/session-diff")
async def api_session_diff(req: _SessionDiffRequest):
    """
    NETCONF XML を受け取り、configure session で +/- diff を取得して返す。
    running-config は変更しない（abort で確実にロールバック）。
    """
    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: session_diff(
            xml_str=req.xml_str,
            host=req.device_ip,
            port=req.port,
            transport=req.transport,
            username=req.username,
            password=req.password,
        )
    )
    return result


@_rest.get("/healthz")
async def _healthz():
    return {"status": "ok", "service": "arista-eapi-session-diff"}



# ── Agent Card ─────────────────────────────────────────────────────────────────
def build_agent_card() -> AgentCard:
    return AgentCard(
        name="Arista cEOS eAPI Show Agent",
        description=(
            "Arista cEOS のオペレーショナル状態（<state>相当）を"
            "eAPI show コマンド経由で参照する A2A サーバ。\n"
            "NETCONF では <state/>フィルターが 0件を返すため（実機確認済み）、"
            "show 系の参照はすべてこのサーバが担当する。\n"
            "接続: pyeapi https/443（自己署名証明書）\n"
            "⚠️ 設定変更は port:8001 の NETCONF サーバが担当。"
        ),
        url=A2A_PUBLIC_URL,
        version="1.0.0",
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="arista_eapi_show_interfaces",
                name="インターフェース状態参照",
                description=(
                    "show interfaces / show interfaces description / "
                    "show ip interface brief 等を実行し、"
                    "インターフェースのオペレーショナル状態を返す。"
                ),
                tags=["eapi", "arista", "show", "interfaces",
                      "operational", "state"],
                examples=[
                    "インターフェースの状態を確認してください",
                    "show interfaces description",
                    '{"query":"インターフェース一覧","device_ip":"172.20.100.31",'
                    '"username":"admin","password":"admin"}',
                ],
            ),
            AgentSkill(
                id="arista_eapi_show_general",
                name="汎用 show コマンド参照",
                description=(
                    "show version / show lldp neighbors / show ip route 等、"
                    "任意の show コマンドを RAG で生成して実行する。\n"
                    "BGP: show ip bgp summary を使用（Cisco 互換形式に統一）。\n"
                    "'show bgp summary' は自動的に 'show ip bgp summary' に置換される。"
                ),
                tags=["eapi", "arista", "show", "version",
                      "lldp", "routing", "operational"],
                examples=[
                    "show version",
                    "LLDPネイバーを確認してください",
                    '{"query":"show lldp neighbors","device_ip":"172.20.100.31"}',
                ],
            ),
        ],
    )


# ── サーバ起動 ─────────────────────────────────────────────────────────────────
def main():
    retriever = _init_retriever()
    llm       = _init_llm()

    agent_card      = build_agent_card()
    executor        = AristaEapiShowExecutor(retriever=retriever, llm=llm)
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )
    a2a_app = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    ).build()

    logger.info("=" * 60)
    logger.info("Arista cEOS eAPI Show A2A Server 起動")
    logger.info("=" * 60)
    logger.info(f"  Agent Card      : {A2A_PUBLIC_URL}/.well-known/agent.json")
    logger.info(f"  A2A endpoint    : {A2A_PUBLIC_URL}/   (port:{A2A_PORT})")
    logger.info(f"  session-diff    : http://{A2A_HOST}:{SDIFF_PORT}/session-diff  (NEW)")
    logger.info(f"  FAISS_PATH      : {FAISS_PATH}")
    logger.info(f"  RAG             : {'有効' if retriever else '無効（LLM知識のみ）'}")
    logger.info(f"  eAPI 接続       : {DEFAULT_EAPI_TRANSPORT}://"
                f"{DEFAULT_EAPI_HOST}:{DEFAULT_EAPI_PORT}")
    logger.info(f"  A2A Port        : {A2A_PORT}  /  REST Port: {SDIFF_PORT}")
    log_llm_config("eAPI")
    logger.info("  スコープ         : show + session diff（設定変更不可、abort 保証）")
    logger.info("=" * 60)

    # A2A サーバ（8002）と session-diff REST サーバ（8009）を asyncio で同時起動
    import asyncio as _asyncio
    import uvicorn as _uvicorn

    cfg_a2a = _uvicorn.Config(
        app=a2a_app, host=A2A_HOST, port=A2A_PORT, log_level="info"
    )
    cfg_rest = _uvicorn.Config(
        app=_rest, host=A2A_HOST, port=SDIFF_PORT, log_level="info"
    )

    async def _serve_both():
        srv_a2a  = _uvicorn.Server(cfg_a2a)
        srv_rest = _uvicorn.Server(cfg_rest)
        # 両サーバを並行起動
        await _asyncio.gather(
            srv_a2a.serve(),
            srv_rest.serve(),
        )

    _asyncio.run(_serve_both())


if __name__ == "__main__":
    main()
