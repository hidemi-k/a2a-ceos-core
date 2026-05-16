#!/usr/bin/env python3
# Copyright (c) 2026 hidemi-k
# Licensed under the MIT License.
"""
Arista cEOS NETCONF RAG A2A Server  (設定変更系)
======================================================
Junosベースの netconf_rag_a2a_server.py の構造を踏襲し、
ncclient / OpenConfig に差し替えたArista専用版。

起動:
    python arista_netconf_rag_a2a_server.py

環境変数:
    FAISS_PATH     : faiss_db のパス（デフォルト: ./faiss_db/arista_netconf）
    A2A_PORT       : ポート番号（デフォルト: 8001）
    A2A_PUBLIC_URL : 外部公開URL（デフォルト: http://localhost:8001）
    SASE_CONFIG    : config.ini のパス

リクエスト形式（JSON）:
    {
        "query":     "Ethernet1 の description を uplink-to-core に設定してください",
        "device_ip": "172.20.100.31",
        "username":  "admin",
        "password":  "admin",
        "port":      "830",
        "deploy":    true
    }
    または単純なテキスト（deploy=False で XML 生成のみ）

レスポンス形式（JSON）:
    {
        "query":             "...",
        "translated_query":  "...",
        "tasks":             [...],
        "final_xml":         "...",
        "validation_status": true,
        "deployment_status": {"status": "success", ...},
        "audit_config":      {"status": "confirmed", "scope": "config-tree-only", ...},
        "summary":           "✅ 成功"
    }

[アーキテクチャ]
  A2AStarletteApplication
    └─ DefaultRequestHandler
         └─ AristaNetconfRagExecutor (AgentExecutor)
               └─ OrchestratorAgentArista
                     ├─ get_inventory (NETCONF)
                     ├─ task_decomposer (LLM)
                     ├─ dependency_resolver (Kahn)
                     └─ [タスク毎] NetconfRagWorkerArista
                           ├─ XMLGenerator (MAF Agent)
                           ├─ XMLReviewer  (MAF Agent)
                           ├─ validate_xml (Skill)
                           ├─ fix_xml      (Skill)
                           ├─ deploy_netconf (Skill, 冪等性チェック付き)
                           ├─ audit        (Skill, config-tree-only)
                           └─ rollback     (Skill)

[スコープ注記]
  audit は NETCONF get_config (<config>ツリー限定) で確認する。
  <state>フィルターは Arista cEOS で 0件（実機確認済み）のため NETCONF では取得不可。
  オペレーショナル状態の確認は arista_eapi_show_a2a_server.py (port:8002) が担当する。
"""

import os
import re
import copy
import json
import logging
import configparser
import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field as dc_field
from datetime import datetime

import uvicorn
# ── 多言語対応 ────────────────────────────────────────────────────────────
from i18n import get_msg, locale_from_request, LOCALE


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

# ── LLM ファクトリ（Groq Primary / Azure Fallback 共通モジュール） ─────────────
from llm_factory import (
    build_llm_with_fallback, build_autogen_client,
    log_llm_config, LLM_PROVIDER_NAME,
)

# MAF
from agent_framework import Agent, Message
from agent_framework_openai import OpenAIChatCompletionClient

# ncclient
from ncclient import manager

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
                            os.path.join(BASE_DIR, "faiss_db", "arista_netconf"))
A2A_HOST       = os.getenv("A2A_HOST", "0.0.0.0")
A2A_PORT       = int(os.getenv("A2A_PORT", "8001"))
A2A_PUBLIC_URL = os.getenv("A2A_PUBLIC_URL", f"http://localhost:{A2A_PORT}")
AUDIT_LOG_PATH = os.getenv("AUDIT_LOG_PATH",
                            os.path.join(BASE_DIR, "audit_log_arista.jsonl"))

# OpenConfig 名前空間定数
OC_INTF_NS = "http://openconfig.net/yang/interfaces"
OC_NI_NS   = "http://openconfig.net/yang/network-instance"
OC_NS_MAP  = {
    "interfaces":        OC_INTF_NS,
    "network-instances": OC_NI_NS,
    "network-instance":  OC_NI_NS,
    "bgp":               "http://openconfig.net/yang/bgp",
}
NETCONF_NS = "urn:ietf:params:xml:ns:netconf:base:1.0"



# ── 起動時初期化（1回だけ） ───────────────────────────────────────────────────
def _init_retriever():
    if not os.path.exists(FAISS_PATH):
        logger.warning(f"faiss_db が見つかりません: {FAISS_PATH}")
        return None
    logger.info(f"FAISS ロード中: {FAISS_PATH}")
    embedding   = HuggingFaceEmbeddings(model_name="BAAI/bge-large-en-v1.5")
    vectorstore = FAISS.load_local(
        FAISS_PATH, embedding, allow_dangerous_deserialization=True)
    logger.info("FAISS ロード完了")
    return vectorstore.as_retriever(search_kwargs={"k": 5})


def _init_llm():
    """LLM インスタンスを構築する（llm_factory 経由 Groq→Azure 自動切り替え）。"""
    return build_llm_with_fallback()


def make_client():
    """AutoGen クライアントを構築する（llm_factory 経由 Groq→Azure 自動切り替え）。"""
    return build_autogen_client()


# ═══════════════════════════════════════════════════════════════════════════════
# Skills
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Skill:
    name: str
    description: str
    function: Callable
    parameters: Dict[str, Any] = dc_field(default_factory=dict)

    def execute(self, **kwargs) -> Any:
        return self.function(**kwargs)


# ── NETCONF ユーティリティ ────────────────────────────────────────────────────

def _connect_with_retry(host, port, username, password, max_retry=3, base_wait=1):
    import time
    last_err = None
    for attempt in range(max_retry):
        try:
            return manager.connect(
                host=host, port=int(port), username=username, password=password,
                hostkey_verify=False, device_params={"name": "default"},
                look_for_keys=False,
            )
        except Exception as e:
            last_err = e
            if attempt < max_retry - 1:
                time.sleep(base_wait * (2 ** attempt))
    raise last_err


def _unwrap_rpc(xml_str: str):
    try:
        root = ET.fromstring(xml_str)
        tag  = root.tag.split("}")[-1] if "}" in root.tag else root.tag
        if tag == "rpc" and list(root):
            inner = list(root)[0]
            itag  = inner.tag.split("}")[-1] if "}" in inner.tag else inner.tag
            return itag, ET.tostring(inner, encoding="unicode")
        return tag, xml_str
    except Exception:
        return "unknown", xml_str


def _inject_oc_ns(xml_str: str) -> str:
    try:
        root = ET.fromstring(xml_str)
        modified = False
        for elem in root.iter():
            if "}" not in elem.tag and elem.tag in OC_NS_MAP:
                elem.tag = "{" + OC_NS_MAP[elem.tag] + "}" + elem.tag
                modified = True
        if modified:
            for tag, ns in OC_NS_MAP.items():
                ET.register_namespace("", ns)
            return ET.tostring(root, encoding="unicode")
        return xml_str
    except Exception:
        return xml_str


def _filter_content(xml_str: str) -> str:
    try:
        root = ET.fromstring(xml_str)
        tag  = root.tag.split("}")[-1] if "}" in root.tag else root.tag
        if tag == "get":
            for child in list(root):
                ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if ctag == "filter":
                    kids = list(child)
                    return "".join(ET.tostring(c, encoding="unicode")
                                   for c in kids) if kids else xml_str
        if tag == "filter":
            kids = list(root)
            return "".join(ET.tostring(c, encoding="unicode")
                           for c in kids) if kids else xml_str
        return xml_str
    except Exception:
        return xml_str


def _config_content(xml_str: str) -> str:
    try:
        root = ET.fromstring(xml_str)
        for ns in [NETCONF_NS, ""]:
            c = root.find(f"{{{ns}}}config") if ns else root.find("config")
            if c is not None:
                return ET.tostring(c, encoding="unicode")
        return xml_str
    except Exception:
        return xml_str


# ── Skill 1: validate_xml ────────────────────────────────────────────────────

def _extract_config_values(xml_str: str) -> dict:
    """
    OpenConfig <config> ツリーから設定値を抽出する。
    インターフェース (openconfig-interfaces) と
    VLAN (openconfig-network-instance) の両方に対応。

    返すキー:
      インターフェース: target_type="interface", interface_name, description, enabled, mtu
      VLAN:           target_type="vlan",      vlan_id, vlan_name
    """
    result = {}
    if not xml_str:
        return result
    OC_NI   = "http://openconfig.net/yang/network-instance"
    OC_INTF = "http://openconfig.net/yang/interfaces"
    try:
        root = ET.fromstring(
            xml_str if xml_str.strip().startswith("<")
            else f"<root>{xml_str}</root>"
        )

        # ── VLAN (network-instances) の検出 ──────────────────────────────
        # <network-instances> または {OC_NI}network-instances が存在するか
        has_ni = (
            root.find(f".//{{{OC_NI}}}network-instances") is not None
            or root.find(".//network-instances") is not None
        )
        if has_ni:
            result["target_type"] = "vlan"
            # vlan-id を探す（namespace あり/なし 両対応）
            for tag in [f"{{{OC_NI}}}vlan-id", "vlan-id"]:
                # <vlan> 直下の vlan-id（キー要素）を優先
                for vlan in (list(root.iter(f"{{{OC_NI}}}vlan"))
                             or list(root.iter("vlan"))):
                    vid = vlan.find(tag)
                    if vid is not None and vid.text:
                        result["vlan_id"] = vid.text.strip()
                        break
                if "vlan_id" in result:
                    break
                # フォールバック: どこでも vlan-id を探す
                for elem in root.iter():
                    local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                    if local == "vlan-id" and elem.text:
                        result["vlan_id"] = elem.text.strip()
                        break
                break

            # vlan name を <config> 内から探す
            # VLAN name: {OC_NI}config 内の {OC_NI}name を探す
            # namespace つきで iter し、find も namespace つきで行う
            for config in root.iter(f"{{{OC_NI}}}config"):
                name_elem = config.find(f"{{{OC_NI}}}name")
                if name_elem is None:
                    # namespace なし要素として再検索
                    for child in config:
                        local = (child.tag.split("}")[-1]
                                 if "}" in child.tag else child.tag)
                        if local == "name" and child.text:
                            name_elem = child
                            break
                if name_elem is not None and name_elem.text:
                    n = name_elem.text.strip()
                    if not any(n.startswith(p) for p in
                               ("Ethernet","Management","Loopback","Port",
                                "default")):
                        result["vlan_name"] = n
                        break
            return result

        # ── インターフェース (openconfig-interfaces) の検出 ───────────────
        result["target_type"] = "interface"
        # インターフェース名（Ethernet/Management/... で始まる <name>）
        for elem in root.iter():
            local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if local == "name" and elem.text:
                t = elem.text.strip()
                if any(t.startswith(p) for p in
                       ("Ethernet","Management","Loopback","Vlan","Port")):
                    result["interface_name"] = t
                    break

        # 設定値フィールド（<config> コンテナ内を優先）
        config_fields = {"description": None, "enabled": None, "mtu": None}
        for elem in root.iter():
            local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if local == "config":
                for child in elem:
                    clocal = (child.tag.split("}")[-1]
                              if "}" in child.tag else child.tag)
                    if clocal in config_fields and child.text:
                        config_fields[clocal] = child.text.strip()
                break
        if all(v is None for v in config_fields.values()):
            for elem in root.iter():
                local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if local in config_fields and elem.text:
                    config_fields[local] = elem.text.strip()
        result.update({k: v for k, v in config_fields.items() if v is not None})

    except Exception as e:
        logger.debug(f"_extract_config_values parse error: {e}")
    return result


def _values_match(proposed: dict, current: dict) -> bool:
    """
    proposed と current の設定値が一致するか確認する。
    target_type に応じて比較キーを変える。
      interface: description / enabled / mtu を比較
      vlan:      vlan_id / vlan_name を比較
    """
    if not proposed or not current:
        return False
    t = proposed.get("target_type", "interface")
    if t == "vlan":
        # vlan_id が一致し、vlan_name も一致（proposed に vlan_name がある場合）
        if proposed.get("vlan_id") != current.get("vlan_id"):
            return False
        if "vlan_name" in proposed and proposed.get("vlan_name") != current.get("vlan_name"):
            return False
        # vlan_id が一致していれば設定済み
        return bool(proposed.get("vlan_id")) and bool(current.get("vlan_id"))
    # インターフェース
    exclude = {"interface_name", "target_type"}
    compare_keys = [k for k in proposed if k not in exclude]
    if not compare_keys:
        return False
    return all(proposed.get(k) == current.get(k) for k in compare_keys)


def validate_xml_structure(xml_config: str) -> Dict[str, Any]:
    if not xml_config or not xml_config.strip():
        return {"valid": False, "errors": ["XML is empty"], "warnings": []}
    try:
        root = ET.fromstring(xml_config)
        errors, warnings = [], []
        tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
        for elem in root.iter():
            pt = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if pt.isdigit():
                errors.append(f"Numeric-only tag forbidden: <{pt}>")
            if elem.find("operation") is not None:
                errors.append("<operation> must be XML attribute, not child tag")
                break
        if tag == "edit-config" and root.find("config") is None:
            errors.append("<edit-config> must contain <config>")
        if "openconfig.net" not in xml_config:
            warnings.append("OpenConfig namespace not detected")
        # VLAN固有チェック: network-instances 構造の検証
        OC_NI = "http://openconfig.net/yang/network-instance"
        ni_elems = list(root.iter(f"{{{OC_NI}}}network-instances"))
        if ni_elems:
            # <network-instance><name>default</name> の存在確認
            ni_name_found = any(
                e.text and e.text.strip() == "default"
                for e in root.iter(f"{{{OC_NI}}}name")
            )
            if not ni_name_found:
                errors.append(
                    "VLAN config: <network-instance><name>default</name> is required"
                )
            # Junos形式の検出（<vlans><vlan><name>TEXT</name> でvlan-idなし）
            for vlan in root.iter(f"{{{OC_NI}}}vlan"):
                vid = vlan.find(f"{{{OC_NI}}}vlan-id")
                if vid is None:
                    vid = vlan.find("vlan-id")
                if vid is None and vlan.get("operation") != "delete":
                    errors.append(
                        "VLAN config: <vlan-id> is required inside <vlan>"
                    )
        return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}
    except ET.ParseError as e:
        return {"valid": False, "errors": [f"XML syntax error: {e}"], "warnings": []}


# ── Skill 2: fix_xml ─────────────────────────────────────────────────────────
def fix_xml_structure(xml_config: str, translated_query: str = "") -> Dict[str, Any]:
    if not xml_config:
        return {"fixed_xml": xml_config, "changes": ["No XML"], "success": False}
    changes = []
    try:
        root = ET.fromstring(xml_config)
        tag  = root.tag.split("}")[-1] if "}" in root.tag else root.tag
        for elem in root.iter():
            op_tag = elem.find("operation")
            if op_tag is not None:
                elem.set("operation", (op_tag.text or "").strip())
                elem.remove(op_tag)
                changes.append("Fixed: <operation> tag → attribute")
        if tag == "edit-config" and root.find("config") is None:
            wrapper = ET.Element("config")
            for child in list(root):
                root.remove(child)
                wrapper.append(child)
            root.append(wrapper)
            changes.append("Fixed: Added <config> wrapper")
        return {
            "fixed_xml": ET.tostring(root, encoding="unicode"),
            "changes": changes or ["No changes needed"],
            "success": True,
        }
    except ET.ParseError as e:
        return {"fixed_xml": xml_config, "changes": [f"Parse error: {e}"], "success": False}


# ── Skill 3: deploy_netconf（冪等性チェック付き） ────────────────────────────
def deploy_netconf_config(
    xml_config: str, device_ip: str, username: str, password: str,
    port: str = "830", comment: str = "Arista A2A NETCONF Agent",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    冪等性チェック: running-config との diff が空ならスキップ。
    <rpc>ラッパー自動除去 / OC名前空間自動付加。

    スコープ注記:
      get/get-config の場合は <config> ツリーのデータを返す。
      <state> ツリーは cEOS で NETCONF 取得不可（実機確認済み）。
    """
    xml_config = _inject_oc_ns(xml_config)
    tag, inner = _unwrap_rpc(xml_config)
    try:
        with _connect_with_retry(device_ip, port, username, password) as m:
            if tag == "get":
                result = m.get(filter=("subtree", _filter_content(inner)))
                output = minidom.parseString(result.data_xml).toprettyxml(indent="  ")
                return {"status": "success", "diff": output,
                        "scope": "config-tree-only",
                        "message": f"get success from {device_ip}"}

            elif tag == "get-config":
                result = m.get_config(source="running",
                                       filter=("subtree", _filter_content(inner)))
                output = minidom.parseString(result.data_xml).toprettyxml(indent="  ")
                return {"status": "success", "diff": output,
                        "scope": "config-tree-only",
                        "message": f"get-config success from {device_ip}"}

            elif tag in {"edit-config", "config"}:
                cc = _config_content(inner)

                if dry_run:
                    return {"status": "dry_run",
                            "diff": get_msg("dryrun_note"),
                            "message": f"[DRY-RUN] {device_ip}"}

                # ── 冪等性チェック（値ベース比較） ────────────────────────
                # running-config 全体ではなく対象インターフェースの
                # <config>ツリーを取り出して設定値を抽出・比較する。
                # LLM生成XMLと running-config は名前空間/インデントが
                # 一致しないため、テキスト diff は使わない。
                try:
                    proposed_vals = _extract_config_values(cc)
                    if proposed_vals:
                        target_type = proposed_vals.get("target_type", "interface")

                        if target_type == "vlan":
                            # VLAN: network-instances で対象 vlan-id だけ取得
                            vlan_id    = proposed_vals.get("vlan_id", "")
                            is_delete  = 'operation="delete"' in cc or "delete" in cc
                            flt = (
                                '<network-instances xmlns="' + OC_NI_NS + '">'
                                '<network-instance><name>default</name>'
                                '<vlans><vlan><vlan-id>' + vlan_id + '</vlan-id>'
                                '</vlan></vlans></network-instance>'
                                '</network-instances>'
                                if vlan_id else
                                '<network-instances xmlns="' + OC_NI_NS + '"/>'
                            )
                            cur_result   = m.get_config(source="running",
                                                        filter=("subtree", flt))
                            current_vals = _extract_config_values(cur_result.data_xml)
                            vlan_exists  = bool(current_vals.get("vlan_id"))
                            if is_delete:
                                # 削除: 既に存在しない → no_changes
                                if not vlan_exists:
                                    return {
                                        "status": "no_changes",
                                        "diff":   get_msg("idempotent_absent"),
                                        "message": (
                                            f"冪等性チェック: 変更不要 ({device_ip})"
                                            f" vlan_id={vlan_id} (already absent)"
                                        ),
                                    }
                            else:
                                # 作成: 既に同じ値で存在する → no_changes
                                if _values_match(proposed_vals, current_vals):
                                    return {
                                        "status": "no_changes",
                                        "diff":   get_msg("idempotent_vlan"),
                                        "message": (
                                            f"冪等性チェック: 変更不要 ({device_ip})"
                                            f" vlan_id={vlan_id}"
                                        ),
                                    }
                        else:
                            # インターフェース: 対象intf の configツリーだけ取得
                            intf_name = proposed_vals.get("interface_name", "")
                            flt = (
                                '<interfaces xmlns="' + OC_INTF_NS + '">'
                                '<interface><name>' + intf_name + '</name></interface>'
                                '</interfaces>'
                                if intf_name else
                                '<interfaces xmlns="' + OC_INTF_NS + '"/>'
                            )
                            cur_result   = m.get_config(source="running",
                                                        filter=("subtree", flt))
                            current_vals = _extract_config_values(cur_result.data_xml)
                            if _values_match(proposed_vals, current_vals):
                                return {
                                    "status": "no_changes",
                                    "diff":   get_msg("idempotent_intf"),
                                    "message": (
                                        f"冪等性チェック: 変更不要 ({device_ip})"
                                        f" interface={intf_name}"
                                    ),
                                }
                except Exception as ie:
                    logger.warning(f"冪等性チェック失敗（続行）: {ie}")

                result = m.edit_config(target="running", config=cc)
                return {"status": "success",
                        "diff": f"edit-config 投入済み: {device_ip}",
                        "message": f"edit-config deployed to {device_ip}: {comment}"}

            else:
                result = m.get_config(source="running",
                                       filter=("subtree", inner))
                output = minidom.parseString(result.data_xml).toprettyxml(indent="  ")
                return {"status": "success", "diff": output,
                        "scope": "config-tree-only",
                        "message": f"get-config(fallback) from {device_ip}"}

    except Exception as e:
        err = str(e)
        if "not found" in err.lower() or "does not exist" in err.lower():
            return {"status": "no_changes", "diff": "",
                    "message": f"Target not found: {err}"}
        return {"status": "failure", "diff": "",
                "message": f"NETCONF error: {err}"}


# ── Skill 4: rollback ────────────────────────────────────────────────────────
def rollback_config(device_ip, username, password, port="830", mode="candidate"):
    try:
        with _connect_with_retry(device_ip, port, username, password) as m:
            if mode == "candidate":
                m.discard_changes()
                return {"status": "success", "mode": "candidate",
                        "message": "Candidate config discarded"}
            return {"status": "success", "mode": mode,
                    "message": "Rescue (manual restore may be needed)"}
    except Exception as e:
        return {"status": "failure", "mode": mode,
                "message": f"Rollback failed: {e}"}


# ── Skill 5: audit（config-tree-only） ───────────────────────────────────────
def audit_deployment(xml_config, device_ip, username, password, port="830"):
    """
    デプロイ後に NETCONF get_config (<config>ツリー) で確認する。

    ⚠️ scope = config-tree-only
    <state>フィルターは Arista cEOS で 0件（実機確認済み）のため確認不可。
    オペレーショナル状態の確認は eAPI サーバ (port:8002) で行うこと。

    修正済みの問題:
      - 名前空間付き vlan-id の抽出（iter で全要素を走査）
      - VLAN削除確認は target_name が evidence に含まれないことで判定
        （他VLANの <vlan-id> タグ存在に影響されない）
    """
    try:
        root_xml = ET.fromstring(
            xml_config if xml_config.startswith("<") else f"<root>{xml_config}</root>"
        )
    except ET.ParseError as e:
        return {"status": "failure", "scope": "config-tree-only",
                "message": f"XML parse error: {e}"}

    op_attr = next(
        (elem.get("operation") for elem in root_xml.iter() if elem.get("operation")),
        ""
    )
    operation = "delete" if op_attr == "delete" else "configure"

    is_vlan = any(
        root_xml.find(f".//{{{OC_NI_NS}}}{t}") is not None
        for t in ["vlan-id", "vlans"]
    )

    if is_vlan:
        # 修正1: iter() で名前空間あり/なし 両対応で vlan-id を確実に取得
        target_name = ""
        for elem in root_xml.iter():
            local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if local == "vlan-id" and elem.text and elem.text.strip().isdigit():
                target_name = elem.text.strip()
                break
        target_type = "vlan"
    else:
        # インターフェース名: Ethernet/Management 等で始まる <name> を取得
        target_name = ""
        for elem in root_xml.iter():
            local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if local == "name" and elem.text:
                t = elem.text.strip()
                if any(t.startswith(p) for p in
                       ("Ethernet", "Management", "Loopback", "Vlan", "Port")):
                    target_name = t
                    break
        target_type = "interface"

    try:
        with _connect_with_retry(device_ip, port, username, password) as m:
            if is_vlan:
                flt = (
                    '<network-instances xmlns="' + OC_NI_NS + '">'
                    '<network-instance><name>default</name>'
                    '<vlans><vlan><vlan-id>' + target_name + '</vlan-id>'
                    '</vlan></vlans></network-instance></network-instances>'
                ) if target_name else (
                    '<network-instances xmlns="' + OC_NI_NS + '"/>'
                )
            else:
                flt = (
                    '<interfaces xmlns="' + OC_INTF_NS + '">'
                    '<interface><name>' + target_name + '</name></interface>'
                    '</interfaces>'
                ) if target_name else (
                    '<interfaces xmlns="' + OC_INTF_NS + '"/>'
                )

            result   = m.get_config(source="running", filter=("subtree", flt))
            evidence = result.data_xml

            if operation == "delete":
                # 修正2: target_name（数値ID）が evidence に含まれないことで判定
                # 他のVLANの <vlan-id> タグに影響されない
                if is_vlan and target_name:
                    # XML として解析して対象 vlan-id の存在を確認
                    try:
                        ev_root = ET.fromstring(evidence)
                        still_exists = any(
                            elem.text and elem.text.strip() == target_name
                            for elem in ev_root.iter()
                            if (elem.tag.split("}")[-1] if "}" in elem.tag
                                else elem.tag) == "vlan-id"
                        )
                        confirmed = not still_exists
                    except ET.ParseError:
                        confirmed = target_name not in evidence
                else:
                    confirmed = not evidence.strip() or target_name not in evidence
            else:
                confirmed = bool(target_name) and target_name in evidence

            return {
                "status":    "success" if confirmed else "failure",
                "scope":     "config-tree-only",
                "operation": operation,
                "target":    target_name,
                "message": (
                    get_msg("audit_confirmed", type=target_type, target=target_name, op=operation)
                    if confirmed else
                    get_msg("audit_failed", type=target_type, target=target_name, op=operation)
                ),
            }
    except Exception as e:
        return {"status": "failure", "scope": "config-tree-only",
                "message": f"Audit error: {e}"}


# ── Skill 6: get_inventory ───────────────────────────────────────────────────
def get_device_inventory(device_ip, username, password, port="830"):
    try:
        with _connect_with_retry(device_ip, port, username, password) as m:
            result = m.get_config(
                source="running",
                filter=("subtree", f'<interfaces xmlns="{OC_INTF_NS}"/>'),
            )
            root_r = ET.fromstring(result.data_xml)
            intfs  = list(dict.fromkeys(
                e.text.strip() for e in root_r.iter()
                if e.tag.split("}")[-1] == "name" and e.text
                and any(e.text.strip().startswith(p)
                        for p in ("Ethernet", "Management", "Loopback", "Vlan", "Port"))
            ))
            return {
                "status": "success",
                "interfaces": intfs, "interface_names": intfs,
                "vlans": [], "raw_config": result.data_xml,
                "message": f"Retrieved {len(intfs)} interfaces from {device_ip}",
            }
    except Exception as e:
        return {"status": "failure", "interfaces": [], "interface_names": [],
                "vlans": [], "raw_config": "",
                "message": f"Inventory failed: {e}"}


# ── Skill 7: lookup_documentation ───────────────────────────────────────────
def lookup_documentation(query, retriever=None, top_k=3):
    if retriever is None:
        return {"status": "failure", "documents": [], "context": "",
                "message": "No retriever"}
    try:
        docs = retriever.invoke(query)[:top_k]
        docs_text = [d.page_content for d in docs]
        return {"status": "success", "documents": docs_text,
                "context": "\n\n---\n\n".join(docs_text),
                "message": f"{len(docs_text)} docs retrieved"}
    except Exception as e:
        return {"status": "failure", "documents": [], "context": "",
                "message": str(e)}


# ── Skill 登録 ────────────────────────────────────────────────────────────────
ALL_SKILLS = [
    Skill("validate_xml",    "Validate OpenConfig NETCONF XML",
          validate_xml_structure,  {"xml_config": "str"}),
    Skill("fix_xml",         "Auto-fix OpenConfig XML structure",
          fix_xml_structure,       {"xml_config": "str"}),
    Skill("deploy_netconf",  "Deploy via ncclient with idempotency check",
          deploy_netconf_config,
          {"xml_config": "str", "device_ip": "str",
           "username": "str", "password": "str", "port": "str"}),
    Skill("rollback",        "Rollback NETCONF config",
          rollback_config,
          {"device_ip": "str", "username": "str",
           "password": "str", "port": "str"}),
    Skill("audit",           "Audit deployment (config-tree-only)",
          audit_deployment,
          {"xml_config": "str", "device_ip": "str",
           "username": "str", "password": "str", "port": "str"}),
    Skill("get_inventory",   "Get device interface inventory",
          get_device_inventory,
          {"device_ip": "str", "username": "str",
           "password": "str", "port": "str"}),
    Skill("lookup_documentation", "RAG document search",
          lookup_documentation,   {"query": "str", "retriever": "obj"}),
]


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 6 Skills（タスク分解・依存解決・集約）
# ═══════════════════════════════════════════════════════════════════════════════

TASK_DECOMPOSER_PROMPT = """
You are a network operation task decomposer for Arista cEOS via NETCONF/OpenConfig.
Analyze the user request and break it into atomic tasks.

[OUTPUT FORMAT] Return ONLY valid JSON. No markdown. No explanation.
Schema:
{
  "tasks": [{
    "id": "task_1",
    "operation": "get" | "set" | "delete" | "configure_interface",
    "target": "<interface name or path>",
    "yang_path": "<OpenConfig path hint>",
    "value": "<value to set, or null>",
    "description": "<one-line English description>",
    "depends_on": []
  }]
}

[SMART RULES]
- GET: merge multiple fields into ONE task
- CONFIG: one task per interface
- DELETE: operation="delete"
- Never create circular dependencies

[EXAMPLES]
Input: "Ethernet1 の description を uplink-to-core に設定してください"
Output: {"tasks":[{"id":"task_1","operation":"configure_interface","target":"Ethernet1",
  "yang_path":"/interfaces/interface[name=Ethernet1]/config/description",
  "value":"uplink-to-core","description":"Set Ethernet1 description to uplink-to-core",
  "depends_on":[]}]}

Input: "インターフェースの状態を確認してください"
Output: {"tasks":[{"id":"task_1","operation":"get","target":"interfaces",
  "yang_path":"/interfaces","value":null,
  "description":"Get all interfaces state","depends_on":[]}]}

Input: "VLAN ID 101 の DEV1_VLAN を作成してください"
Output: {"tasks":[{"id":"task_1","operation":"create_vlan","target":"101",
  "yang_path":"/network-instances/network-instance[name=default]/vlans/vlan[vlan-id=101]",
  "value":"DEV1_VLAN","description":"Create VLAN 101 named DEV1_VLAN","depends_on":[]}]}

Input: "VLAN 101 を削除してください"
Output: {"tasks":[{"id":"task_1","operation":"delete_vlan","target":"101",
  "yang_path":"/network-instances/network-instance[name=default]/vlans/vlan[vlan-id=101]",
  "value":null,"description":"Delete VLAN 101","depends_on":[]}]}
"""


def decompose_tasks(user_query, llm=None, inventory=None):
    if llm is None:
        return {"status": "failure", "tasks": [], "message": "No LLM"}
    try:
        inv_sect = (
            f"Existing interfaces: {inventory.get('interface_names', [])}\n"
            f"Raw config:\n{inventory.get('raw_config', '')[:500]}"
        ) if inventory and inventory.get("status") == "success" else "(not available)"

        prompt = TASK_DECOMPOSER_PROMPT + f"\n\nCurrent network state:\n{inv_sect}"
        prompt += f"\n\nInput: {user_query}\nOutput:"
        raw    = llm.invoke(prompt).content.strip()
        m      = re.search(r"```json\s*(.+?)\s*```", raw, re.DOTALL)
        if m:
            raw = m.group(1).strip()
        parsed = json.loads(raw)
        tasks  = parsed.get("tasks", [])
        return {"status": "success", "tasks": tasks,
                "message": f"{len(tasks)} task(s)"}
    except Exception as e:
        return {"status": "failure", "tasks": [], "message": str(e)}


def resolve_dependencies(tasks):
    if not tasks:
        return {"status": "success", "execution_order": [], "message": "No tasks"}
    from collections import deque
    task_map  = {t["id"]: t for t in tasks}
    in_degree = {t["id"]: 0 for t in tasks}
    deps_of   = {t["id"]: [] for t in tasks}
    for t in tasks:
        for dep in t.get("depends_on", []):
            if dep not in task_map:
                return {"status": "error", "execution_order": [],
                        "message": f"Unknown dep: {dep}"}
            in_degree[t["id"]] += 1
            deps_of[dep].append(t["id"])
    queue = deque(tid for tid, deg in in_degree.items() if deg == 0)
    order = []
    while queue:
        tid  = queue.popleft()
        task = copy.deepcopy(task_map[tid])
        order.append(task)
        for did in deps_of[tid]:
            in_degree[did] -= 1
            if in_degree[did] == 0:
                queue.append(did)
    if len(order) != len(tasks):
        return {"status": "error", "execution_order": [],
                "message": "Circular dependency detected"}
    return {"status": "success", "execution_order": order,
            "message": f"{len(order)} task(s)"}


def aggregate_results(task_results):
    succeeded, failed, skipped = [], [], []
    lines = ["=" * 60, get_msg("report_header"), "=" * 60]
    for entry in task_results:
        task   = entry.get("task", {})
        result = entry.get("result", {})
        tid    = task.get("id", "?")
        deploy = result.get("deployment_status", {}) or {}
        audit  = result.get("audit_status") or {}
        ds     = deploy.get("status", "unknown")
        aus    = audit.get("status", "") if audit else ""
        lines.append(f"\n  [{tid}] {task.get('operation')}/{task.get('target')}"
                     f"  deploy={ds}  audit={aus or '-'}")
        if ds in ("success", "no_changes") and aus in ("success", "skipped", ""):
            succeeded.append(tid); lines.append("     → ✅ SUCCESS")
        elif ds == "skipped":
            skipped.append(tid);   lines.append("     → ⏭️  SKIPPED")
        else:
            failed.append(tid);    lines.append("     → ❌ FAILED")
    lines.append("\n" + "=" * 60)
    if not failed and not skipped:
        overall = "all_success";    summary = get_msg("all_success", n=len(succeeded))
    elif not failed and not succeeded:
        overall = "dry_run";         summary = get_msg("dry_run")
    elif failed and not succeeded:
        overall = "all_failure";     summary = get_msg("all_failure", n=len(failed))
    else:
        overall = "partial_failure"; summary = get_msg("partial_failure", ok=len(succeeded), ng=len(failed))
    lines.extend([f"  {summary}", "=" * 60])
    return {
        "status": overall, "summary": summary,
        "succeeded_tasks": succeeded, "failed_tasks": failed,
        "skipped_tasks": skipped, "report_lines": lines,
    }


ALL_SKILLS_V6 = ALL_SKILLS + [
    Skill("task_decomposer",    "Decompose request into atomic tasks",
          decompose_tasks,    {}),
    Skill("dependency_resolver", "Resolve task dependencies (Kahn algorithm)",
          resolve_dependencies, {}),
    Skill("result_aggregator",  "Aggregate task results into report",
          aggregate_results,  {}),
]


# ═══════════════════════════════════════════════════════════════════════════════
# AuditLogger
# ═══════════════════════════════════════════════════════════════════════════════

class AuditLogger:
    def __init__(self, log_path=AUDIT_LOG_PATH):
        self.log_path    = log_path
        self._session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._entries    = []

    def record(self, task, worker_result, policy_result=None,
               safety_result=None, deploy_method="ncclient"):
        deploy = worker_result.get("deployment_status") or {}
        audit  = worker_result.get("audit_status") or {}
        entry  = {
            "session_id":     self._session_id,
            "timestamp":      datetime.now().isoformat(),
            "task_id":        task.get("id"),
            "operation":      task.get("operation"),
            "target":         task.get("target", ""),
            "policy_allowed": (policy_result or {}).get("allowed"),
            "safety_passed":  (safety_result or {}).get("safe"),
            "deploy_method":  deploy_method,
            "deploy_status":  deploy.get("status", "unknown"),
            "diff":           deploy.get("diff", "")[:500],
            "audit_status":   audit.get("status", ""),
            "audit_scope":    audit.get("scope", "config-tree-only"),
        }
        self._entries.append(entry)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"AuditLogger write error: {e}")

    def record_blocked(self, task, reason, violations=None):
        entry = {
            "session_id": self._session_id, "timestamp": datetime.now().isoformat(),
            "task_id":    task.get("id"),   "operation":  task.get("operation"),
            "target":     task.get("target", ""),
            "deploy_status":   "blocked",
            "deploy_message":  reason,
            "policy_violations": violations or [],
        }
        self._entries.append(entry)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"AuditLogger write error: {e}")

    def summary(self):
        total   = len(self._entries)
        success = sum(1 for e in self._entries if e.get("deploy_status") == "success")
        blocked = sum(1 for e in self._entries if e.get("deploy_status") == "blocked")
        failed  = sum(1 for e in self._entries
                      if e.get("deploy_status") in ("failure", "rolled_back"))
        return {"total": total, "success": success, "blocked": blocked,
                "failed": failed, "skipped": total - success - blocked - failed}


# ═══════════════════════════════════════════════════════════════════════════════
# ValidationAgent / PolicyChecker
# ═══════════════════════════════════════════════════════════════════════════════

DANGEROUS_PATTERNS = [
    (r"<delete-config", "error",   "<delete-config> is forbidden"),
    (r"kill-session",   "error",   "kill-session is forbidden"),
    (r"<format",        "error",   "format operation is forbidden"),
]
_ALLOWED_TAGS = {
    "rpc", "interfaces", "network-instances", "network-instance",
    "get-config", "edit-config", "get", "filter", "config", "root",
    "data", "hello", "interface", "bgp", "system", "vlan",
}
ARISTA_POLICY = {
    "allowed_interfaces": [],
    "forbidden_keywords": ["delete-config", "kill-session", "restart"],
}


def validate_safety(xml_config, task=None):
    errors, warnings = [], []
    for pat, sev, msg in DANGEROUS_PATTERNS:
        if re.search(pat, xml_config, re.IGNORECASE):
            (errors if sev == "error" else warnings).append(msg)
    return {"safe": len(errors) == 0, "errors": errors, "warnings": warnings}


def check_policy(task, xml_config, policy=None):
    policy     = policy or ARISTA_POLICY
    violations = []
    iface      = task.get("target", "")
    allowed    = policy.get("allowed_interfaces", [])
    if iface and allowed and iface not in allowed:
        violations.append({"rule": "interface_allowlist",
                           "detail": f"'{iface}' not in allowed_interfaces"})
    for kw in policy.get("forbidden_keywords", []):
        if kw.lower() in xml_config.lower():
            violations.append({"rule": "forbidden_keyword",
                               "detail": f"'{kw}' found"})
    try:
        root = ET.fromstring(xml_config)
        tag  = root.tag.split("}")[-1] if "}" in root.tag else root.tag
        if tag not in _ALLOWED_TAGS:
            violations.append({"rule": "netconf_tag",
                               "detail": f"<{tag}> not in allowed tags"})
    except Exception:
        pass
    return {"allowed": len(violations) == 0, "violations": violations}


# ═══════════════════════════════════════════════════════════════════════════════
# NetconfRagWorkerArista（MAF Agent）
# ═══════════════════════════════════════════════════════════════════════════════

GENERATOR_INSTRUCTIONS = """
You are a dedicated Arista EOS OpenConfig NETCONF XML generator.
OUTPUT ONLY RAW XML — no explanation, no markdown code blocks.

[OPERATION TYPES]
- GET (read): Use filter with OpenConfig namespace
  Example: <interfaces xmlns="http://openconfig.net/yang/interfaces">
             <interface><name>Ethernet1</name></interface>
           </interfaces>

- SET/CONFIGURE: Use <config> with edit-config structure
  Example: <config>
             <interfaces xmlns="http://openconfig.net/yang/interfaces">
               <interface>
                 <name>Ethernet1</name>
                 <config><description>uplink-to-core</description></config>
               </interface>
             </interfaces>
           </config>

- DELETE: Add operation="delete" attribute
  Example: <config>
             <interfaces xmlns="http://openconfig.net/yang/interfaces">
               <interface operation="delete"><name>Ethernet1</name></interface>
             </interfaces>
           </config>

VLAN CREATE/UPDATE: Use network-instances namespace with this EXACT structure:
```
<config>
  <network-instances xmlns="http://openconfig.net/yang/network-instance">
    <network-instance>
      <name>default</name>
      <vlans>
        <vlan>
          <vlan-id>101</vlan-id>
          <config>
            <vlan-id>101</vlan-id>
            <name>DEV1_VLAN</name>
          </config>
        </vlan>
      </vlans>
    </network-instance>
  </network-instances>
</config>
```

VLAN DELETE: Add operation="delete" on the <vlan> element:
```
<config>
  <network-instances xmlns="http://openconfig.net/yang/network-instance">
    <network-instance>
      <name>default</name>
      <vlans>
        <vlan operation="delete">
          <vlan-id>101</vlan-id>
        </vlan>
      </vlans>
    </network-instance>
  </network-instances>
</config>
```

CRITICAL VLAN RULES:
- ALWAYS wrap in <network-instance><name>default</name>...</network-instance>
- vlan-id MUST appear BOTH as element key AND inside <config>
- NEVER use Junos-style <vlans><vlan><name>VLAN_NAME</name> structure
- NEVER omit the <network-instance><name>default</name> wrapper

If you cannot generate XML, output ONLY: <filter/>
"""

REVIEWER_INSTRUCTIONS = """
You are an XML intent validator. Check ONLY:
1. Operation type match (get/set/delete)
2. Target match (interface name, etc.)
3. XML parsability (well-formed)

First word MUST be exactly one of: APPROVE, IMPROVE, REJECT
"""


class NetconfRagWorkerArista:
    def __init__(self, retriever, llm, skills=None,
                 max_retries=3, max_review_rounds=2, log_callback=None):
        self.retriever         = retriever
        self.llm               = llm
        self.max_retries       = max_retries
        self.max_review_rounds = max_review_rounds
        self.log_callback      = log_callback
        self.skill_execution_log: List[Dict] = []
        self.conversation_history: List[Message] = []
        self.skills = {sk.name: sk for sk in (skills or ALL_SKILLS)}
        self.xml_generator = Agent(
            name="XMLGenerator", client=make_client(),
            instructions=GENERATOR_INSTRUCTIONS)
        self.xml_reviewer = Agent(
            name="XMLReviewer", client=make_client(),
            instructions=REVIEWER_INSTRUCTIONS)

    def log(self, msg: str):
        if self.log_callback:
            self.log_callback(msg)
        else:
            logger.info(f"[Worker] {msg}")

    def _run_skill(self, name, **kwargs):
        if name == "lookup_documentation" and "retriever" not in kwargs:
            kwargs["retriever"] = self.retriever
        if name not in self.skills:
            return None
        try:
            result = self.skills[name].execute(**kwargs)
            self.skill_execution_log.append({
                "skill": name,
                "timestamp": datetime.now().isoformat(),
                "result_summary": str(result)[:200],
            })
            return result
        except Exception as e:
            self.log(f"[Skill:{name}] Error: {e}")
            return None

    def _extract_xml(self, text: str) -> str:
        candidates = []
        for m in re.finditer(r"```(?:xml)?\s*(.*?)\s*```", text, re.DOTALL):
            candidates.append(m.group(1).strip())
        m = re.search(r"(<[a-zA-Z][^>]*>.*)", text, re.DOTALL)
        if m:
            candidates.append(m.group(1).strip())

        def try_parse(s):
            s = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;)([a-zA-Z])",
                       r"&amp;\1", s)
            fm = re.match(r"<([a-zA-Z][a-zA-Z0-9_:-]*)", s)
            if fm:
                close = f"</{fm.group(1)}>"
                if close in s:
                    s = s[:s.rindex(close) + len(close)]
            try:
                ET.fromstring(s); return s
            except Exception:
                pass
            for em in re.finditer(r"</[a-zA-Z][a-zA-Z0-9_:-]*>", s):
                try:
                    ET.fromstring(s[:em.end()]); return s[:em.end()]
                except Exception:
                    continue
            return None

        for c in candidates:
            r = try_parse(c)
            if r:
                return r
        return ""

    async def _run_skill_loop(self, xml_config, translated_query,
                               device_info=None, deploy=False):
        current_xml = xml_config

        # validate
        val = self._run_skill("validate_xml", xml_config=current_xml)
        if val is None:
            return {"final_xml": current_xml, "valid": False,
                    "deployment_status": {"status": "skipped", "diff": "",
                                          "message": "validate failed"}}
        # fix if needed
        if not val["valid"]:
            for _ in range(3):
                fix = self._run_skill("fix_xml", xml_config=current_xml,
                                      translated_query=translated_query)
                if fix and fix["success"]:
                    current_xml = fix["fixed_xml"]
                val = self._run_skill("validate_xml", xml_config=current_xml)
                if val and val["valid"]:
                    break

        if not val or not val["valid"]:
            return {"final_xml": current_xml, "valid": False,
                    "deployment_status": {"status": "skipped", "diff": "",
                                          "message": "Validation failed"}}

        # GET判定
        write_inds = ['edit-config', 'operation="delete"',
                      'operation="merge"', 'operation="replace"']
        is_read = (not any(w in current_xml for w in write_inds)
                   and "<config>" not in current_xml)

        if deploy and device_info:
            dep = self._run_skill(
                "deploy_netconf", xml_config=current_xml,
                device_ip=device_info["ip"], username=device_info["username"],
                password=device_info["password"],
                port=device_info.get("port", "830"),
            )
            if not dep or dep["status"] == "failure":
                rb = None if is_read else self._run_skill(
                    "rollback", device_ip=device_info["ip"],
                    username=device_info["username"],
                    password=device_info["password"],
                    port=device_info.get("port", "830"), mode="candidate",
                )
                return {"final_xml": current_xml, "valid": True,
                        "deployment_status": dep or {"status": "failure", "diff": "",
                                                     "message": "deploy error"},
                        "audit_status": None, "rollback_status": rb}

            if is_read:
                return {"final_xml": current_xml, "valid": True,
                        "deployment_status": dep,
                        "audit_status": None, "rollback_status": None}

            # audit（config-tree-only）
            aud = self._run_skill(
                "audit", xml_config=current_xml,
                device_ip=device_info["ip"], username=device_info["username"],
                password=device_info["password"],
                port=device_info.get("port", "830"),
            )
            if not aud or aud["status"] == "failure":
                rb = self._run_skill(
                    "rollback", device_ip=device_info["ip"],
                    username=device_info["username"],
                    password=device_info["password"],
                    port=device_info.get("port", "830"), mode="rescue",
                )
                return {"final_xml": current_xml, "valid": True,
                        "deployment_status": dep, "audit_status": aud,
                        "rollback_status": rb}
            return {"final_xml": current_xml, "valid": True,
                    "deployment_status": dep, "audit_status": aud,
                    "rollback_status": None}

        return {"final_xml": current_xml, "valid": True,
                "deployment_status": {"status": "skipped", "diff": "",
                                      "message": "deploy=False"}}

    async def run(self, user_query, device_ip=None, username=None,
                  password=None, port="830", deploy=False,
                  inventory_info=None):
        result = {
            "user_query": user_query, "translated_query": "",
            "generated_xml": "", "final_xml": "",
            "validation_status": False, "deployment_status": {},
            "skill_execution_log": [],
        }
        try:
            # 翻訳
            ratio = sum(1 for c in user_query if ord(c) < 128) / max(len(user_query), 1)
            translated = (
                user_query if ratio >= 0.8
                else self.llm.invoke(
                    "Translate only the following into English, "
                    "without any additional text: " + user_query
                ).content.strip()
            )
            result["translated_query"] = translated

            # RAG
            docs    = self.retriever.invoke(translated) if self.retriever else []
            context = "\n\n---\n\n".join(d.page_content for d in docs)

            # XMLGenerator
            inv_sec = ""
            if inventory_info and inventory_info.get("status") == "success":
                inv_sec = (
                    f"\n### Device interfaces: "
                    f"{inventory_info.get('interface_names', [])}\n"
                )
            gen_prompt = (
                f"Generate OpenConfig NETCONF XML for Arista cEOS.\n{inv_sec}"
                f"### Context:\n{context}\n\n"
                f"### Request:\n{translated}\n\nOnly XML."
            )

            raw_xml   = ""
            review_ok = False
            for attempt in range(self.max_retries):
                gen_resp = await self.xml_generator.run(gen_prompt)
                raw      = gen_resp.text if hasattr(gen_resp, "text") else str(gen_resp)
                xml      = self._extract_xml(raw)
                if not xml:
                    continue
                try:
                    ET.fromstring(xml)
                except ET.ParseError:
                    continue
                result["generated_xml"] = xml

                # XMLReviewer
                for rnd in range(self.max_review_rounds):
                    rev_resp = await self.xml_reviewer.run(
                        f"Review XML:\n{xml}\nUser request: {translated}\n"
                        "Check: operation type + target + parsability. "
                        "First word: APPROVE/IMPROVE/REJECT."
                    )
                    rev_text = (rev_resp.text if hasattr(rev_resp, "text")
                                else str(rev_resp))
                    if "APPROVE" in rev_text.upper():
                        raw_xml = xml; review_ok = True; break
                    elif ("IMPROVE" in rev_text.upper()
                          and rnd < self.max_review_rounds - 1):
                        imp_resp = await self.xml_generator.run(
                            f"Context:\n{context}\nRequest:{translated}\n"
                            f"Previous:\n{xml}\nFeedback:{rev_text}\n"
                            "Improve XML. ONLY XML."
                        )
                        imp_xml = self._extract_xml(
                            imp_resp.text if hasattr(imp_resp, "text")
                            else str(imp_resp)
                        )
                        if imp_xml:
                            try:
                                ET.fromstring(imp_xml); xml = imp_xml
                            except ET.ParseError:
                                pass
                    else:
                        break
                if review_ok:
                    break

            if not raw_xml or not review_ok:
                return result

            device_info = (
                {"ip": device_ip, "username": username,
                 "password": password, "port": port}
                if deploy and all([device_ip, username, password]) else None
            )
            skill_result = await self._run_skill_loop(
                raw_xml, translated, device_info, deploy
            )
            result.update({
                "final_xml":         skill_result["final_xml"],
                "validation_status": skill_result["valid"],
                "deployment_status": skill_result["deployment_status"],
                "audit_status":      skill_result.get("audit_status"),
                "rollback_status":   skill_result.get("rollback_status"),
                "skill_execution_log": self.skill_execution_log,
            })
        except Exception as e:
            logger.error(f"Worker error: {e}", exc_info=True)
            result["error"] = str(e)
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# OrchestratorAgentArista
# ═══════════════════════════════════════════════════════════════════════════════

class OrchestratorAgentArista:
    def __init__(self, retriever, llm, skills=None,
                 max_retries=3, max_review_rounds=2,
                 policy=None, audit_log_path=AUDIT_LOG_PATH,
                 log_callback=None):
        self.retriever         = retriever
        self.llm               = llm
        self.max_retries       = max_retries
        self.max_review_rounds = max_review_rounds
        self.policy            = policy or ARISTA_POLICY
        self.audit_logger      = AuditLogger(log_path=audit_log_path)
        self.log_callback      = log_callback
        self.skills            = {sk.name: sk for sk in (skills or ALL_SKILLS_V6)}

    def log(self, msg: str):
        if self.log_callback:
            self.log_callback(msg)
        else:
            logger.info(f"[Orchestrator] {msg}")

    def _run_skill(self, name, **kwargs):
        if name not in self.skills:
            return None
        try:
            return self.skills[name].execute(**kwargs)
        except Exception as e:
            self.log(f"[Skill:{name}] Error: {e}")
            return None

    def _build_worker_query(self, task):
        op     = task.get("operation", "")
        target = task.get("target", "")
        value  = task.get("value", "")
        desc   = task.get("description", "")
        if desc:
            return desc
        if op == "create_vlan":
            return (f"Create VLAN ID {target}"
                    f"{' named ' + value if value else ''} "
                    f"using OpenConfig network-instances YANG.")
        if op == "delete_vlan":
            return f"Delete VLAN ID {target} using OpenConfig network-instances YANG."
        if op == "delete":
            return f"Delete configuration for {target} via NETCONF."
        if op in ("set", "configure_interface"):
            return f"Configure {target}{' to ' + value if value else ''} via NETCONF."
        if op == "get":
            return f"Get current state of {target} via NETCONF."
        return f"{op} {target}"

    async def _dispatch_task(self, task, idx, total,
                              device_ip, username, password, port, deploy):
        tid = task.get("id", "?")

        if task.get("operation") == "skip":
            skipped = {
                "validation_status": True,
                "deployment_status": {"status": "no_changes", "diff": "",
                                      "message": "skipped"},
                "audit_status": None, "rollback_status": None,
            }
            self.audit_logger.record_blocked(task, "skipped")
            return skipped

        worker = NetconfRagWorkerArista(
            retriever=self.retriever, llm=self.llm, skills=ALL_SKILLS,
            max_retries=self.max_retries,
            max_review_rounds=self.max_review_rounds,
            log_callback=self.log_callback,
        )
        try:
            gen_result = await worker.run(
                user_query=self._build_worker_query(task),
                device_ip=device_ip, username=username,
                password=password, port=port, deploy=False,
            )
        except Exception as e:
            self.audit_logger.record_blocked(task, f"XML generation failed: {e}")
            return {
                "validation_status": False,
                "deployment_status": {"status": "failure", "diff": "",
                                      "message": str(e)},
                "audit_status": None, "rollback_status": None,
            }

        xml_config = gen_result.get("final_xml") or gen_result.get("generated_xml", "")
        if not xml_config:
            self.audit_logger.record_blocked(task, "Empty XML")
            return {
                "validation_status": False,
                "deployment_status": {"status": "failure", "diff": "",
                                      "message": "Empty XML"},
                "audit_status": None, "rollback_status": None,
            }

        # ValidationAgent
        safety = validate_safety(xml_config, task=task)
        if not safety["safe"]:
            msg = f"ValidationAgent BLOCK: {safety['errors']}"
            self.audit_logger.record_blocked(task, msg, violations=safety["errors"])
            return {
                "validation_status": False,
                "deployment_status": {"status": "blocked", "diff": "", "message": msg},
                "audit_status": None, "rollback_status": None,
            }

        # PolicyChecker
        policy_res = check_policy(task, xml_config, policy=self.policy)
        if not policy_res["allowed"]:
            msg = f"PolicyChecker BLOCK: {policy_res['violations']}"
            self.audit_logger.record_blocked(
                task, msg, violations=policy_res["violations"])
            return {
                "validation_status": False,
                "deployment_status": {"status": "blocked", "diff": "", "message": msg},
                "audit_status": None, "rollback_status": None,
            }

        # Skill ループ
        device_info = (
            {"ip": device_ip, "username": username,
             "password": password, "port": port}
            if deploy and all([device_ip, username, password]) else None
        )
        skill_result = await worker._run_skill_loop(
            xml_config=xml_config,
            translated_query=gen_result.get("translated_query",
                                             self._build_worker_query(task)),
            device_info=device_info, deploy=deploy,
        )
        worker_result = {
            "generated_xml":      xml_config,
            "final_xml":          skill_result["final_xml"],
            "validation_status":  skill_result["valid"],
            "deployment_status":  skill_result["deployment_status"],
            "audit_status":       skill_result.get("audit_status"),
            "rollback_status":    skill_result.get("rollback_status"),
            "skill_execution_log": worker.skill_execution_log,
        }
        self.audit_logger.record(
            task, worker_result,
            policy_result=policy_res, safety_result=safety,
            deploy_method="ncclient",
        )
        return worker_result

    async def run(self, user_query, device_ip=None, username=None,
                  password=None, port="830", deploy=False):
        self.log("\n" + "=" * 70)
        self.log("🎼 OrchestratorAgentArista 起動")
        self.log("=" * 70)
        self.log(f"Query: {user_query}  deploy={deploy}")

        result = {
            "user_query": user_query, "tasks": [], "execution_order": [],
            "task_results": [], "aggregated": {},
        }

        # Step 0: get_inventory
        inventory = None
        if all([device_ip, username, password]):
            self.log("\n[Step 0] get_inventory")
            inv = self._run_skill(
                "get_inventory", device_ip=device_ip,
                username=username, password=password, port=port,
            )
            if inv and inv.get("status") == "success":
                inventory = inv
                self.log(f"   interfaces: {inv.get('interface_names', [])}")

        # Step 1: task_decomposer
        self.log("\n[Step 1] task_decomposer")
        decomp = self._run_skill(
            "task_decomposer", user_query=user_query,
            llm=self.llm, inventory=inventory,
        )
        if not decomp or decomp["status"] != "success":
            self.log(f"❌ task_decomposer 失敗: {(decomp or {}).get('message')}")
            result["aggregated"] = {"status": "all_failure",
                                    "summary": "task_decomposer failed"}
            return result
        tasks = decomp["tasks"]; result["tasks"] = tasks
        self.log(f"   → {len(tasks)} タスク: {[t['id'] for t in tasks]}")

        # Step 2: dependency_resolver
        self.log("\n[Step 2] dependency_resolver")
        resolve = self._run_skill("dependency_resolver", tasks=tasks)
        if not resolve or resolve["status"] != "success":
            self.log(f"❌ dependency_resolver 失敗")
            result["aggregated"] = {"status": "all_failure",
                                    "summary": "dependency_resolver failed"}
            return result
        exec_order = resolve["execution_order"]
        result["execution_order"] = exec_order
        self.log(f"   → 実行順: {[t['id'] for t in exec_order]}")

        # Step 3: Worker ディスパッチ
        self.log(f"\n[Step 3] Worker ディスパッチ ({len(exec_order)} タスク)")
        task_results = []
        for idx, task in enumerate(exec_order, 1):
            tid = task["id"]
            self.log(f"\n  [{idx}/{len(exec_order)}] {tid}: "
                     f"{task.get('operation')}/{task.get('target')}")
            wr  = await self._dispatch_task(
                task, idx, len(exec_order),
                device_ip, username, password, port, deploy,
            )
            ds  = (wr.get("deployment_status") or {}).get("status", "?")
            aus = ((wr.get("audit_status") or {}).get("status", ""))
            ok  = (ds in ("success", "no_changes", "skipped", "blocked")
                   and aus in ("success", "skipped", ""))
            self.log(f"  {'✅' if ok else '❌'} {tid} → deploy={ds} audit={aus or '-'}")
            task_results.append({"task": task, "result": wr})
            if not ok and ds not in ("blocked", "no_changes"):
                self.log(f"\n  🛑 {tid} 失敗 → 残タスク中断")
                for rem in exec_order[idx:]:
                    task_results.append({
                        "task": rem,
                        "result": {
                            "validation_status": False,
                            "deployment_status": {
                                "status": "skipped", "diff": "",
                                "message": f"Skipped due to failure of {tid}",
                            },
                            "audit_status": None, "rollback_status": None,
                        },
                    })
                break

        result["task_results"] = task_results

        # Step 4: result_aggregator
        self.log("\n[Step 4] result_aggregator")
        agg = self._run_skill("result_aggregator", task_results=task_results)
        result["aggregated"] = agg or {}
        if agg:
            self.log(f"\n{agg['summary']}")

        self.log("\n" + "=" * 70)
        self.log("🎼 OrchestratorAgentArista 完了")
        self.log("=" * 70)
        return result


# ═══════════════════════════════════════════════════════════════════════════════
# A2A AgentExecutor（Junosの NetconfRagExecutor と同一パターン）
# ═══════════════════════════════════════════════════════════════════════════════

class AristaNetconfRagExecutor(AgentExecutor):
    """
    OrchestratorAgentArista を A2A プロトコルで公開するアダプタ。

    リクエスト形式:
      テキスト形式: "Ethernet1 の description を uplink に設定してください"
        → deploy=False で実行（XML生成・検証のみ）

      JSON形式: {
          "query":     "...",
          "device_ip": "172.20.100.31",
          "username":  "admin",
          "password":  "admin",
          "port":      "830",
          "deploy":    true
        }
        → 指定パラメータで実機投入
    """

    def __init__(self, retriever, llm):
        self._retriever = retriever
        self._llm       = llm
        logger.info("AristaNetconfRagExecutor 初期化完了")

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
        device_ip = params.get("device_ip")
        username  = params.get("username")
        password  = params.get("password")
        port      = params.get("port", "830")
        deploy    = params.get("deploy", False)

        logger.info(f"受信: {query[:80]}... deploy={deploy}")

        def log_callback(msg: str):
            logger.info(f"[orchestrator] {msg}")

        try:
            orchestrator = OrchestratorAgentArista(
                retriever=self._retriever, llm=self._llm,
                skills=ALL_SKILLS_V6, log_callback=log_callback,
            )
            result = await orchestrator.run(
                user_query=query, device_ip=device_ip,
                username=username, password=password,
                port=port, deploy=deploy,
            )

            agg = result.get("aggregated", {})

            # タスク毎の結果を整形
            task_summaries = []
            for entry in result.get("task_results", []):
                task   = entry["task"]
                res    = entry["result"]
                deploy_st = (res.get("deployment_status") or {})
                audit_st  = (res.get("audit_status") or {})
                task_summaries.append({
                    "task_id":        task.get("id"),
                    "operation":      task.get("operation"),
                    "target":         task.get("target"),
                    "deploy_status":  deploy_st.get("status"),
                    "deploy_message": deploy_st.get("message", "")[:200],
                    "audit_status":   audit_st.get("status", ""),
                    "audit_scope":    audit_st.get("scope", "config-tree-only"),
                    "audit_message":  audit_st.get("message", "")[:200],
                    # ★ session diff 用: タスクが生成した最終 XML を含める
                    "final_xml":      res.get("final_xml", ""),
                    "generated_xml":  res.get("generated_xml", ""),
                })

            # ★ session diff 用: 全タスクの XML を結合して返す
            # dry-run 時は task_summaries[*].final_xml に入っている
            all_xmls = [
                ts.get("final_xml") or ts.get("generated_xml", "")
                for ts in task_summaries
                if ts.get("final_xml") or ts.get("generated_xml")
            ]
            # 複数タスクは改行区切りで結合（session diff は先頭タスクを使う）
            combined_xml = "\n".join(all_xmls)

            response_payload = {
                "query":            query,
                "tasks_detected":   len(result.get("tasks", [])),
                "task_summaries":   task_summaries,
                "overall_status":   agg.get("status", "unknown"),
                "summary":          agg.get("summary", ""),
                "audit_scope_note": (
                    get_msg("audit_scope_note")
                ),
                # ★ Hub の session diff 呼び出しで使う
                "final_xml":        combined_xml,
                "generated_xml":    combined_xml,
            }

            logger.info(f"完了: {agg.get('summary', '')}")
            await event_queue.enqueue_event(
                new_agent_text_message(
                    json.dumps(response_payload, ensure_ascii=False, indent=2)))

        except Exception as e:
            logger.error(f"Orchestrator エラー: {e}", exc_info=True)
            await event_queue.enqueue_event(
                new_agent_text_message(f"エラーが発生しました: {e}"))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise UnsupportedOperationError("キャンセルはサポートされていません。")


# ── Agent Card ─────────────────────────────────────────────────────────────────
def build_agent_card() -> AgentCard:
    return AgentCard(
        name="Arista cEOS NETCONF RAG Agent",
        description=(
            "自然言語の指示を OpenConfig NETCONF XML に変換し、"
            "Arista cEOS に設定を投入するマルチエージェント A2A サーバ。\n"
            "OrchestratorAgentArista による "
            "タスク分解 → 冪等性チェック → ValidationAgent → PolicyChecker "
            "→ deploy(ncclient) → audit(config-tree-only) → rollback "
            "のフルサイクルをサポート。\n"
            "⚠️ オペレーショナル状態の確認は port:8002 の eAPI サーバが担当。"
        ),
        url=A2A_PUBLIC_URL,
        version="1.0.0",
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="arista_netconf_write",
                name="Arista NETCONF 設定変更（MAFマルチエージェント）",
                description=(
                    "自然言語 → task_decomposer → XMLGenerator/Reviewer(MAF Agent) "
                    "→ validate → fix → deploy(ncclient, 冪等性チェック) "
                    "→ audit(config-tree-only) → rollback"
                ),
                tags=["netconf", "arista", "openconfig", "rag",
                      "maf", "idempotency", "audit"],
                examples=[
                    "Ethernet1 の description を uplink-to-core に設定してください",
                    '{"query":"Ethernet2の説明をkali1-LINKに設定","device_ip":'
                    '"172.20.100.31","username":"admin","password":"admin","deploy":true}',
                ],
            ),
            AgentSkill(
                id="arista_netconf_dryrun",
                name="Arista NETCONF ドライラン（XML生成・検証のみ）",
                description=(
                    "deploy=false でテキスト or JSON を送信すると "
                    "XML 生成・validate・fix のみ実行し、実機投入しません。"
                ),
                tags=["netconf", "arista", "dryrun", "xml", "validate"],
                examples=[
                    "Ethernet1 の description を test に設定してください",
                    '{"query":"Ethernet1のdescriptionをtestに設定","deploy":false}',
                ],
            ),
        ],
    )


# ── サーバ起動 ─────────────────────────────────────────────────────────────────
def main():
    retriever = _init_retriever()
    llm       = _init_llm()

    agent_card      = build_agent_card()
    executor        = AristaNetconfRagExecutor(retriever=retriever, llm=llm)
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
    )
    app = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    ).build()

    logger.info("=" * 60)
    logger.info("Arista cEOS NETCONF RAG A2A Server 起動")
    logger.info("=" * 60)
    logger.info(f"  Agent Card  : {A2A_PUBLIC_URL}/.well-known/agent.json")
    logger.info(f"  A2A endpoint: {A2A_PUBLIC_URL}/")
    logger.info(f"  FAISS_PATH  : {FAISS_PATH}")
    logger.info(f"  RAG         : {'有効' if retriever else '無効（faiss_db なし）'}")
    logger.info(f"  AUDIT_LOG   : {AUDIT_LOG_PATH}")
    logger.info(f"  Port        : {A2A_PORT}  (eAPI show サーバは 8002)")
    logger.info("=" * 60)

    uvicorn.run(app, host=A2A_HOST, port=A2A_PORT)


if __name__ == "__main__":
    main()
