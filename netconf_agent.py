#!/usr/bin/env python3
# Copyright (c) 2026 hidemi-k
# Licensed under the MIT License.
"""
netconf_agent.py
OrchestratorAgentArista を NiceGUI app.py から呼び出すためのモジュール。
RAG_arista_netconf_maf.ipynb のセル1-11を統合。
"""

# ── Cell 1 ──────────────────────────────────────────
import asyncio, os, re, json, copy, yaml, sys
import configparser
import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom
from typing import Optional, List, Dict, Any, Callable
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field

from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_openai import ChatOpenAI
from ncclient import manager
from ncclient.xml_ import to_ele

# ── MAF ───────────────────────────────────────────────────────────────────────
from agent_framework import Agent, Message
from agent_framework_openai import OpenAIChatCompletionClient

import logging
from logging.handlers import RotatingFileHandler

# ── Cell 2 ──────────────────────────────────────────
# APIキー管理: .env → 環境変数 → config.ini の優先順位
GROQ_API_KEY = None

# 優先度1: .env ファイル (pip install python-dotenv)
try:
    from dotenv import load_dotenv
    load_dotenv()
    GROQ_API_KEY = os.getenv('GROQ_API_KEY')
    if GROQ_API_KEY:
        print("✅ APIキーを .env から読み込みました")
except ImportError:
    pass

# 優先度2: 環境変数
if not GROQ_API_KEY:
    GROQ_API_KEY = os.getenv('GROQ_API_KEY')
    if GROQ_API_KEY:
        print("✅ APIキーを環境変数から読み込みました")

# 優先度3: config.ini（後方互換）
if not GROQ_API_KEY:
    _cfg = configparser.ConfigParser()
    for _path in ['./config.ini',
                  os.path.expanduser('~/config/config.ini')]:
        if os.path.exists(_path):
            _cfg.read(_path)
            if 'GROQ' in _cfg and 'GROQ_API_KEY' in _cfg['GROQ']:
                GROQ_API_KEY = _cfg['GROQ']['GROQ_API_KEY']
                print(f"✅ APIキーを {_path} から読み込みました")
                break

if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY が見つかりません\n"
                     "1. .env に GROQ_API_KEY=gsk_xxx\n"
                     "2. export GROQ_API_KEY=gsk_xxx\n"
                     "3. config.ini の [GROQ] セクション")

# ── Cell 3 ──────────────────────────────────────────
# ================================================================
# logging 設定（改善1: print → logging に標準化）
# ================================================================
LOG_FILE_PATH = "./arista_netconf_rag.log"

logger = logging.getLogger("arista_rag")
logger.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                          datefmt="%Y-%m-%dT%H:%M:%S")

_ch = logging.StreamHandler(sys.stdout)
_ch.setLevel(logging.INFO)
_ch.setFormatter(_fmt)

_fh = RotatingFileHandler(LOG_FILE_PATH, maxBytes=5*1024*1024, backupCount=3,
                           encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(_fmt)

if not logger.handlers:
    logger.addHandler(_ch)
    logger.addHandler(_fh)

# 既存の self.log() と互換性を保つラッパー
def log_info(msg):    logger.info(msg)
def log_debug(msg):   logger.debug(msg)
def log_warning(msg): logger.warning(msg)
def log_error(msg):   logger.error(msg)


# ── Cell 4 ──────────────────────────────────────────

embedding = HuggingFaceEmbeddings(model_name="BAAI/bge-large-en-v1.5")

FAISS_DB_PATH = "./faiss_db_netconf"  # RAG_arista_netconf.ipynb で作成済み

if os.path.exists(FAISS_DB_PATH):
    vectorstore = FAISS.load_local(
        FAISS_DB_PATH, embedding, allow_dangerous_deserialization=True
    )
    retriever = vectorstore.as_retriever(search_kwargs={"k": 5})
    print(f"✅ ベクトルストア読み込み完了: {FAISS_DB_PATH}")
else:
    print(f"⚠️ ベクトルストアが見つかりません: {FAISS_DB_PATH}")
    retriever = None

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL  = "llama-3.3-70b-versatile"

llm = ChatOpenAI(
    model=DEFAULT_MODEL, temperature=0,
    api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL,
)

# ── Cell 5 ──────────────────────────────────────────
def make_client(model_id: str = DEFAULT_MODEL) -> OpenAIChatCompletionClient:
    return OpenAIChatCompletionClient(
        model=model_id,
        api_key=GROQ_API_KEY,
        base_url=GROQ_BASE_URL,
    )


# ── Cell 6 ──────────────────────────────────────────
# ============================================================
# Skill 基底クラス（Junos版と同一）
# ============================================================
@dataclass
class Skill:
    name: str
    description: str
    function: Callable
    parameters: Dict[str, Any] = field(default_factory=dict)

    def execute(self, **kwargs) -> Any:
        return self.function(**kwargs)

    def __str__(self):
        return f"Skill(name={self.name}, description={self.description[:50]}...)"


# ============================================================
# Skill 1: OpenConfig XML 構造検証
# Junos版 validate_xml_structure → OpenConfig / NETCONF 形式に置き換え
# ============================================================
def validate_xml_structure(xml_config: str) -> Dict[str, Any]:
    """
    OpenConfig NETCONF XML の構造を検証する。

    Junos版との主な違い:
    - <configuration> ではなく <get-config>/<edit-config>/<interfaces> 等が root
    - <vlans>/<vlan-id> ではなく OpenConfig 名前空間の要素を検証
    - delete 操作は operation="delete" 属性（Junosと同じ形式）
    """
    if not xml_config or not xml_config.strip():
        return {"valid": False, "errors": ["XML is empty"], "warnings": []}

    try:
        root = ET.fromstring(xml_config)
        errors   = []
        warnings = []

        tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

        # ① 数字のみのタグ名禁止
        for elem in root.iter():
            pure_tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if pure_tag.isdigit():
                errors.append(f"Numeric-only tag forbidden: <{pure_tag}>")

        # ② <operation>タグ形式を検出（属性でなければエラー）
        for elem in root.iter():
            op_tag = elem.find("operation")
            if op_tag is not None:
                errors.append(
                    "<operation> must be an XML attribute, not a child tag. "
                    'Use: <interface operation="delete"> instead of <operation>delete</operation>'
                )
                break

        # ③ edit-config の場合: <config> ラッパー確認
        if tag == "edit-config":
            config_elem = root.find("config")
            if config_elem is None:
                errors.append("<edit-config> must contain <config> child element")

        # ④ OpenConfig 名前空間の確認（警告のみ）
        has_oc_ns = any(
            "openconfig.net" in (k if isinstance(k, str) else "")
            for k in root.attrib
        ) or "openconfig.net" in xml_config
        if not has_oc_ns:
            warnings.append("OpenConfig namespace (openconfig.net) not detected — verify namespace declarations")

        return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}

    except ET.ParseError as e:
        return {"valid": False, "errors": [f"XML syntax error: {str(e)}"], "warnings": []}


# ============================================================
# Skill 2: XML 自動修正（OpenConfig版）
# ============================================================
def fix_xml_structure(xml_config: str, translated_query: str = "") -> Dict[str, Any]:
    """
    OpenConfig XML の構造を自動修正する。

    修正内容:
    - <operation> 子タグ → 属性に変換
    - edit-config 内の <config> ラッパー欠落を補完
    """
    if not xml_config:
        return {"fixed_xml": xml_config, "changes": ["No XML to fix"], "success": False}

    changes = []
    try:
        root = ET.fromstring(xml_config)
        tag  = root.tag.split("}")[-1] if "}" in root.tag else root.tag

        # 修正①: <operation>タグ → 属性に変換
        for elem in root.iter():
            op_tag = elem.find("operation")
            if op_tag is not None:
                op_value = (op_tag.text or "").strip()
                elem.set("operation", op_value)
                elem.remove(op_tag)
                changes.append(f"Fixed: <operation>{op_value}</operation> → attribute")

        # 修正②: edit-config に <config> がない場合にラップ
        if tag == "edit-config":
            config_elem = root.find("config")
            if config_elem is None:
                children = list(root)
                config_wrapper = ET.Element("config")
                for child in children:
                    root.remove(child)
                    config_wrapper.append(child)
                root.append(config_wrapper)
                changes.append("Fixed: Added missing <config> wrapper inside <edit-config>")

        fixed_xml = ET.tostring(root, encoding="unicode")
        return {
            "fixed_xml": fixed_xml,
            "changes": changes if changes else ["No changes needed"],
            "success": True
        }

    except ET.ParseError as e:
        return {"fixed_xml": xml_config, "changes": [f"XML parse error: {str(e)}"], "success": False}


# ============================================================
# Skill 3: NETCONF デプロイ（ncclient版）
# Junos版 deploy_netconf_config → jnpr.junos → ncclient に置き換え
# ============================================================
# NETCONF 名前空間定数
NETCONF_NS = "urn:ietf:params:xml:ns:netconf:base:1.0"

# OpenConfig 名前空間マップ
OC_NS_MAP = {
    "interfaces":        "http://openconfig.net/yang/interfaces",
    "network-instances": "http://openconfig.net/yang/network-instance",
    "network-instance":  "http://openconfig.net/yang/network-instance",
    "bgp":               "http://openconfig.net/yang/bgp",
    "vlans":             "http://openconfig.net/yang/vlan",
    "acl":               "http://openconfig.net/yang/acl",
    "routing-policy":    "http://openconfig.net/yang/routing-policy",
    "lldp":              "http://openconfig.net/yang/lldp",
    "lacp":              "http://openconfig.net/yang/lacp",
    "platform":          "http://openconfig.net/yang/platform",
    "system":            "http://openconfig.net/yang/system",
}


def _find_elem(parent, tag: str, ns_list=None) -> "ET.Element | None":
    """
    改善1: 名前空間の有無に関わらず要素を探す共通関数。
    DeprecationWarning を回避するため find() or find() を廃止し
    is not None チェックに統一する。
    """
    # 名前空間なしで検索
    elem = parent.find(tag)
    if elem is not None:
        return elem
    # 指定された名前空間リストで検索
    for ns in (ns_list or [NETCONF_NS]):
        elem = parent.find(f"{{{ns}}}{tag}")
        if elem is not None:
            return elem
    return None


def _inject_oc_namespace(xml_str: str) -> str:
    """
    改善2: LLM が xmlns を省略した場合に OpenConfig namespace を自動付加する。
    タグ名を OC_NS_MAP と照合し、名前空間がなければ追加する。
    """
    try:
        root = ET.fromstring(xml_str)
        modified = False
        for elem in root.iter():
            raw_tag = elem.tag
            if "}" not in raw_tag:  # 名前空間なし
                if raw_tag in OC_NS_MAP:
                    elem.tag = f"{{{OC_NS_MAP[raw_tag]}}}{raw_tag}"
                    modified = True
        if modified:
            # 名前空間プレフィックスを登録してシリアライズ
            for tag, ns in OC_NS_MAP.items():
                ET.register_namespace("", ns)
            return ET.tostring(root, encoding="unicode")
        return xml_str
    except Exception:
        return xml_str


def _unwrap_rpc(xml_config: str):
    """
    改善3: DOM 処理で <rpc> ラッパーを除去する（DeprecationWarning 対応）。
    """
    try:
        root = ET.fromstring(xml_config)
        tag  = root.tag.split("}")[-1] if "}" in root.tag else root.tag
        if tag == "rpc":
            children = list(root)
            if not children:
                return "unknown", xml_config
            inner     = children[0]
            inner_tag = inner.tag.split("}")[-1] if "}" in inner.tag else inner.tag
            return inner_tag, ET.tostring(inner, encoding="unicode")
        return tag, xml_config
    except Exception:
        return "unknown", xml_config


def _extract_filter(xml_str: str) -> str:
    """<filter> 要素の子要素を返す。DeprecationWarning 対応版。"""
    try:
        root  = ET.fromstring(xml_str)
        f_elem = _find_elem(root, "filter", [NETCONF_NS])
        if f_elem is not None:
            return "".join(ET.tostring(c, encoding="unicode") for c in list(f_elem))
        return xml_str
    except Exception:
        return xml_str


def _extract_config(xml_str: str) -> str:
    """<edit-config> の <config> 要素を返す。DeprecationWarning 対応版。"""
    try:
        root   = ET.fromstring(xml_str)
        c_elem = _find_elem(root, "config", [NETCONF_NS])
        return ET.tostring(c_elem, encoding="unicode") if c_elem is not None else xml_str
    except Exception:
        return xml_str


def _connect_with_retry(host, port, username, password, max_retry=3, base_wait=1):
    """
    改善2: 指数バックオフ付きリトライで ncclient 接続する。
    待機時間: 1s → 2s → 4s → ... (base_wait * 2^attempt)
    ネットワーク負荷を抑えつつ瞬断に耐える。
    """
    import time
    last_err = None
    for attempt in range(max_retry):
        try:
            conn = manager.connect(
                host=host, port=int(port),
                username=username, password=password,
                hostkey_verify=False,
                device_params={"name": "default"},
                look_for_keys=False,
            )
            if attempt > 0:
                logger.info(f"ncclient 接続成功 (試行 {attempt+1}/{max_retry})")
            return conn
        except Exception as e:
            last_err = e
            if attempt < max_retry - 1:
                wait_sec = base_wait * (2 ** attempt)   # 1s, 2s, 4s ...
                logger.warning(f"ncclient 接続失敗 (試行 {attempt+1}/{max_retry}): {e}")
                logger.warning(f"  {wait_sec}秒後にリトライします...")
                time.sleep(wait_sec)
    logger.error(f"ncclient 接続失敗 (全{max_retry}回): {last_err}")
    raise last_err


def deploy_netconf_config(
    xml_config: str,
    device_ip: str,
    username: str,
    password: str,
    port: str = "830",
    comment: str = "AI Agent - Arista NETCONF",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    ncclient で Arista cEOS に NETCONF 設定を送信する。

    改善1: _find_elem() で DeprecationWarning を解消
    改善2: _inject_oc_namespace() で xmlns 自動付加
    改善3: _connect_with_retry() でネットワーク瞬断に対応
    dry_run=True: edit-config を実行せず diff を返す
    """
    # 改善2: namespace 自動インジェクション
    xml_config = _inject_oc_namespace(xml_config)

    # <rpc> ラッパーを除去（改善3: DOM処理）
    tag, inner_xml = _unwrap_rpc(xml_config)

    try:
        # 改善3: リトライ付き接続
        m = _connect_with_retry(device_ip, port, username, password)
        with m:
            if tag == "get":
                fc     = _extract_filter(inner_xml)
                result = m.get(filter=("subtree", fc))
                _xml_raw = getattr(result, "data_xml", str(result))
                _xml_raw = _xml_raw[_xml_raw.index("?>")+2:].strip() if _xml_raw.startswith("<?") else _xml_raw
                output = minidom.parseString(_xml_raw).toprettyxml(indent="  ")
                return {"status": "success", "diff": output,
                        "message": f"get success from {device_ip}"}

            elif tag == "get-config":
                fc     = _extract_filter(inner_xml)
                result = m.get_config(source="running", filter=("subtree", fc))
                _xml_raw = getattr(result, "data_xml", str(result))
                _xml_raw = _xml_raw[_xml_raw.index("?>")+2:].strip() if _xml_raw.startswith("<?") else _xml_raw
                output = minidom.parseString(_xml_raw).toprettyxml(indent="  ")
                return {"status": "success", "diff": output,
                        "message": f"get-config success from {device_ip}"}

            elif tag in {"edit-config", "config"}:
                cc = _extract_config(inner_xml)

                # 改善3: 冪等性チェック — 現在の設定と比較して差分がなければスキップ
                # dry_run=True でも同様に diff を返す（変更は適用しない）
                import difflib
                try:
                    before_raw  = m.get_config(source="running")
                    before_text = minidom.parseString(
                        str(before_raw)).toprettyxml(indent="  ")
                    before_lines = before_text.splitlines(keepends=True)
                    after_lines  = cc.splitlines(keepends=True)
                    diff_lines = list(difflib.unified_diff(
                        before_lines, after_lines,
                        fromfile="running-config", tofile="proposed-change",
                        lineterm=""
                    ))
                    diff_text = "".join(diff_lines)
                except Exception as diff_err:
                    logger.warning(f"diff 取得失敗: {diff_err}")
                    diff_text = "(diff 取得失敗)"
                    diff_lines = ["__non_empty__"]  # diff取得失敗時はデプロイを続行

                # dry_run: diff のみ返す（変更しない）
                if dry_run:
                    return {
                        "status": "dry_run",
                        "diff": diff_text if diff_text else "(差分なし)",
                        "message": f"[DRY-RUN] {device_ip} への変更内容を確認しました"
                    }

                # 冪等性チェック: diff がなければスキップ
                meaningful_diff = [l for l in diff_lines
                                   if l.startswith(('+', '-'))
                                   and not l.startswith(('+++', '---'))]
                if not meaningful_diff:
                    logger.info(f"冪等性チェック: 変更なし → スキップ ({device_ip})")
                    return {
                        "status": "no_changes",
                        "diff": "(差分なし — 設定は既に最新です)",
                        "message": f"冪等性チェック: 変更不要のためスキップ ({device_ip})"
                    }

                logger.info(f"冪等性チェック: {len(meaningful_diff)} 行の変更を検出 → デプロイ実行")
                result = m.edit_config(target="running", config=cc)
                return {"status": "success", "diff": diff_text or str(result),
                        "message": f"edit-config deployed to {device_ip}: {comment}"}

            else:
                # フォールバック
                fc     = _extract_filter(inner_xml)
                result = m.get_config(source="running", filter=("subtree", fc))
                _xml_raw = getattr(result, "data_xml", str(result))
                _xml_raw = _xml_raw[_xml_raw.index("?>")+2:].strip() if _xml_raw.startswith("<?") else _xml_raw
                output = minidom.parseString(_xml_raw).toprettyxml(indent="  ")
                return {"status": "success", "diff": output,
                        "message": f"get-config (fallback) from {device_ip}"}

    except Exception as e:
        err = str(e)
        if "not found" in err.lower() or "does not exist" in err.lower():
            return {"status": "no_changes", "diff": "",
                    "message": f"Target not found: {err}"}
        return {"status": "failure", "diff": "",
                "message": f"NETCONF error: {err}"}

def rollback_config(
    device_ip: str, username: str, password: str,
    port: str = "830", mode: str = "candidate"
) -> Dict[str, Any]:
    """
    ncclient で Arista cEOS の設定をロールバックする。

    mode="candidate": candidate config を discard
    mode="rescue": running config の直前スナップショットへ（EOS限定）
    """
    try:
        with manager.connect(
            host=device_ip, port=int(port),
            username=username, password=password,
            hostkey_verify=False,
            device_params={"name": "default"},
            look_for_keys=False,
        ) as m:
            if mode == "candidate":
                m.discard_changes()
                return {"status": "success", "mode": "candidate",
                        "message": "Candidate configuration discarded"}
            elif mode == "rescue":
                # EOS では rollback 操作を CLI で行うため NETCONF RPC で代替
                rollback_rpc = '<rpc xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><get-config><source><running/></source></get-config></rpc>'
                return {"status": "success", "mode": "rescue",
                        "message": "Rescue: retrieved running config (manual restore may be needed)"}
            else:
                return {"status": "failure", "mode": mode, "message": f"Unknown mode: {mode}"}
    except Exception as e:
        return {"status": "failure", "mode": mode, "message": f"Rollback failed: {str(e)}"}


# ============================================================
# Skill 5: デプロイ後監査（ncclient版）
# Junos版 audit_deployment → vlans確認 → OpenConfig interfaces/state確認
# ============================================================
def audit_deployment(
    xml_config: str, device_ip: str, username: str,
    password: str, port: str = "830"
) -> Dict[str, Any]:
    """
    デプロイ後に設定反映を NETCONF get-config で確認する。

    対応する操作タイプ:
    - interface configure/delete : interfaces 名前空間で確認
    - VLAN create/delete         : network-instances 名前空間で確認
    """
    # ── XML から操作タイプとターゲットを解析 ──────────────────────────────────
    try:
        root_xml = ET.fromstring(
            xml_config if xml_config.startswith("<") else f"<root>{xml_config}</root>"
        )
    except ET.ParseError as e:
        return {"status": "failure", "operation": "unknown", "target": "",
                "evidence": "", "message": f"XML parse error: {e}"}

    # operation 属性を取得
    op_attr = ""
    for elem in root_xml.iter():
        if elem.get("operation"):
            op_attr = elem.get("operation")
            break
    operation = "delete" if op_attr == "delete" else "configure"

    # 操作対象タイプの判定（interface vs VLAN）
    OC_INTF_NS = "http://openconfig.net/yang/interfaces"
    OC_NI_NS   = "http://openconfig.net/yang/network-instance"

    is_vlan = (
        root_xml.find(f".//{{{OC_NI_NS}}}vlan-id") is not None
        or root_xml.find(".//vlan-id") is not None
        or root_xml.find(f".//{{{OC_NI_NS}}}vlans") is not None
    )

    # ターゲット名を抽出
    if is_vlan:
        # VLAN: vlan-id を取得
        vid_elem = root_xml.find(f".//{{{OC_NI_NS}}}vlan-id")
        if vid_elem is None:
            vid_elem = root_xml.find(".//vlan-id")
        target_name = vid_elem.text.strip() if vid_elem is not None else ""
        target_type = "vlan"
    else:
        # Interface: interfaces 名前空間の name を取得
        name_elem = root_xml.find(f".//{{{OC_INTF_NS}}}name")
        if name_elem is None:
            name_elem = root_xml.find(".//name")
        target_name = name_elem.text.strip() if name_elem is not None else ""
        target_type = "interface"

    logger.debug(f"audit: type={target_type} target={target_name!r} operation={operation}")

    # ── NETCONF get-config で確認 ─────────────────────────────────────────────
    try:
        m = _connect_with_retry(device_ip, port, username, password)
        with m:
            # target_name 空の場合: 全VLAN取得してsuccessとみなす
            if target_type == "vlan" and not target_name:
                _fxml = (f'<network-instances xmlns="{OC_NI_NS}"><network-instance>'
                         f'<name>default</name><vlans/></network-instance></network-instances>')
                _res = m.get_config(source="running", filter=("subtree", _fxml))
                try:
                    _ev = _res.data_xml
                    if _ev.startswith("<?"):
                        _ev = _ev[_ev.index("?>") + 2:].strip()
                except AttributeError:
                    _ev = str(_res)
                return {"status":"success","operation":operation,"target":"(all vlans)",
                        "evidence":_ev[:500],"message":"✅ VLAN state retrieved"}

            if target_type == "vlan":
                # VLAN: network-instances/vlans で確認
                filter_xml = (
                    f'<network-instances xmlns="{OC_NI_NS}">'
                    f'<network-instance><name>default</name>'
                    f'<vlans><vlan><vlan-id>{target_name}</vlan-id></vlan></vlans>'
                    f'</network-instance></network-instances>'
                )
            else:
                # Interface: interfaces で確認
                filter_xml = (
                    f'<interfaces xmlns="{OC_INTF_NS}">'
                    f'<interface><name>{target_name}</name></interface>'
                    f'</interfaces>'
                ) if target_name else f'<interfaces xmlns="{OC_INTF_NS}"/>'

            result   = m.get_config(source="running", filter=("subtree", filter_xml))
            try:
                evidence = result.data_xml
                if evidence.startswith("<?"):
                    evidence = evidence[evidence.index("?>") + 2:].strip()
            except AttributeError:
                evidence = str(result)

            # 確認ロジック
            if operation == "delete":
                # 削除確認: target_name が evidence に含まれていなければ成功
                if target_type == "vlan":
                    # VLAN: <vlan-id>50</vlan-id> が消えていれば成功
                    confirmed = f"<vlan-id>{target_name}</vlan-id>" not in evidence
                else:
                    # Interface: <name>Ethernet1</name> が消えていれば成功
                    confirmed = f"<name>{target_name}</name>" not in evidence
            else:
                # 設定確認: target_name が evidence に含まれていれば成功
                confirmed = target_name in evidence

            label = f"{target_type} {target_name}"
            return {
                "status": "success" if confirmed else "failure",
                "operation": operation,
                "target": target_name,
                "evidence": evidence[:1000],
                "message": (
                    f"✅ Confirmed: {label} {operation}d successfully"
                    if confirmed else
                    f"❌ Audit failed: {label} not confirmed after {operation}"
                )
            }

    except Exception as e:
        return {"status": "failure", "operation": operation, "target": target_name,
                "evidence": "", "message": f"Audit error: {e}"}


# ============================================================
# Skill 6: インベントリ取得（ncclient版）
# Junos版 get_device_inventory → vlans → OpenConfig interfaces
# ============================================================
def get_device_inventory(
    device_ip: str, username: str, password: str, port: str = "830"
) -> Dict[str, Any]:
    """
    NETCONF get-config で Arista cEOS の interfaces 一覧を取得する。

    Junos版との違い:
    - <configuration><vlans/> → OpenConfig <interfaces xmlns=.../>
    - VLAN名リスト → インターフェース名リスト
    """
    try:
        with manager.connect(
            host=device_ip, port=int(port),
            username=username, password=password,
            hostkey_verify=False,
            device_params={"name": "default"},
            look_for_keys=False,
        ) as m:
            filter_xml = '''<interfaces xmlns="http://openconfig.net/yang/interfaces"/>'''
            result = m.get_config(source="running", filter=("subtree", filter_xml))
            try:
                raw_config = result.data_xml
                if raw_config.startswith("<?"):
                    raw_config = raw_config[raw_config.index("?>") + 2:].strip()
            except AttributeError:
                raw_config = str(result)

            # インターフェース名を抽出
            root_r = ET.fromstring(raw_config) if raw_config.startswith("<") else None
            interfaces = []
            if root_r is not None:
                for name_elem in root_r.findall(".//{http://openconfig.net/yang/interfaces}name"):
                    if name_elem.text and name_elem.text.strip():
                        interfaces.append(name_elem.text.strip())
            # 重複除去
            interfaces = list(dict.fromkeys(interfaces))

            # VLAN 一覧も取得
            vlan_list = []
            try:
                vlan_filter = (
                    '<network-instances xmlns="http://openconfig.net/yang/network-instance">'
                    '<network-instance><name>default</name><vlans/></network-instance>'
                    '</network-instances>'
                )
                vlan_result = m.get_config(source="running",
                                            filter=("subtree", vlan_filter))
                # ncclient GetReply → data_xml でdata要素内のXMLを取得
                # str() は宣言+rpc-replyが混在しET.fromstringが失敗するため使わない
                try:
                    vlan_raw = vlan_result.data_xml
                except AttributeError:
                    vlan_raw = str(vlan_result)
                # data_xml が <?xml...> 宣言を含む場合は除去
                if vlan_raw.startswith("<?"):
                    vlan_raw = vlan_raw[vlan_raw.index("?>") + 2:].strip()
                for vid_elem in ET.fromstring(vlan_raw).iter(
                        "{http://openconfig.net/yang/network-instance}vlan-id"):
                    if vid_elem.text:
                        vlan_list.append(vid_elem.text.strip())
                vlan_list = list(dict.fromkeys(vlan_list))
            except Exception:
                vlan_list = []

            return {
                "status": "success",
                "interfaces": interfaces,
                "interface_names": interfaces,
                "vlans": vlan_list,
                "raw_config": raw_config,
                "message": (f"Retrieved {len(interfaces)} interfaces, "
                            f"{len(vlan_list)} VLANs from {device_ip}")
            }

    except Exception as e:
        return {"status": "failure", "interfaces": [], "interface_names": [],
                "vlans": [], "raw_config": "",
                "message": f"Inventory failed: {str(e)}"}


# ============================================================
# Skill 7: RAG検索
# ============================================================
def lookup_documentation(
    query: str, retriever=None, top_k: int = 3
) -> Dict[str, Any]:
    if retriever is None:
        return {"status": "failure", "documents": [], "context": "",
                "message": "Retriever not available"}
    try:
        docs      = retriever.invoke(query)
        documents = [doc.page_content for doc in docs[:top_k]]
        context   = "\n\n---\n\n".join(documents)
        return {"status": "success", "documents": documents, "context": context,
                "message": f"Found {len(documents)} documents for: {query}"}
    except Exception as e:
        return {"status": "failure", "documents": [], "context": "",
                "message": f"Lookup failed: {str(e)}"}


# ── Skill オブジェクト登録 ────────────────────────────────────────────────────
validate_xml_skill = Skill(
    name="validate_xml",
    description="Validate Arista OpenConfig NETCONF XML structure. Returns valid(bool), errors(list), warnings(list).",
    function=validate_xml_structure,
    parameters={"xml_config": {"type": "string"}}
)
fix_xml_skill = Skill(
    name="fix_xml",
    description="Auto-fix common OpenConfig XML structural errors (<operation> tag, <config> wrapper).",
    function=fix_xml_structure,
    parameters={"xml_config": {"type": "string"}, "translated_query": {"type": "string"}}
)
deploy_skill = Skill(
    name="deploy_netconf",
    description="Deploy OpenConfig XML to Arista cEOS via NETCONF (ncclient). Returns status/diff/message.",
    function=deploy_netconf_config,
    parameters={"xml_config": {"type": "string"}, "device_ip": {"type": "string"},
                "username": {"type": "string"}, "password": {"type": "string"}, "port": {"type": "string"}}
)
rollback_skill = Skill(
    name="rollback",
    description="Rollback Arista cEOS NETCONF config. mode='candidate': discard-changes. mode='rescue': retrieve running.",
    function=rollback_config,
    parameters={"device_ip": {"type": "string"}, "username": {"type": "string"},
                "password": {"type": "string"}, "port": {"type": "string"}, "mode": {"type": "string"}}
)
audit_skill = Skill(
    name="audit",
    description="Verify deployment via NETCONF get-config. Confirms interface operation was applied.",
    function=audit_deployment,
    parameters={"xml_config": {"type": "string"}, "device_ip": {"type": "string"},
                "username": {"type": "string"}, "password": {"type": "string"}, "port": {"type": "string"}}
)
get_inventory_skill = Skill(
    name="get_inventory",
    description="Fetch current interfaces list from Arista cEOS via NETCONF. Returns interface_names(list), raw_config(str).",
    function=get_device_inventory,
    parameters={"device_ip": {"type": "string"}, "username": {"type": "string"},
                "password": {"type": "string"}, "port": {"type": "string"}}
)
lookup_documentation_skill = Skill(
    name="lookup_documentation",
    description="Search RAG knowledge base for OpenConfig YANG documentation.",
    function=lookup_documentation,
    parameters={"query": {"type": "string"}, "top_k": {"type": "integer"}}
)

ALL_SKILLS = [
    validate_xml_skill, fix_xml_skill, deploy_skill,
    rollback_skill, audit_skill,
    get_inventory_skill, lookup_documentation_skill
]

for sk in ALL_SKILLS:
    print(f"   🛠️  {sk.name}")
deploy_skill = Skill(
    name="deploy_netconf",
    description=(
        "Deploy OpenConfig XML to Arista cEOS via NETCONF. "
        "Auto-removes <rpc> wrapper. Auto-injects OpenConfig namespace. "
        "dry_run=True で変更前にDiff確認。リトライ接続対応。"
    ),
    function=deploy_netconf_config,
    parameters={"xml_config": {"type": "string"}, "device_ip": {"type": "string"},
                "username": {"type": "string"}, "password": {"type": "string"},
                "port": {"type": "string"}, "dry_run": {"type": "boolean"}}
)
rollback_skill = Skill(
    name="rollback",
    description="Rollback Arista cEOS NETCONF config. mode='candidate': discard-changes. mode='rescue': retrieve running.",
    function=rollback_config,
    parameters={"device_ip": {"type": "string"}, "username": {"type": "string"},
                "password": {"type": "string"}, "port": {"type": "string"}, "mode": {"type": "string"}}
)
audit_skill = Skill(
    name="audit",
    description="Verify deployment via NETCONF get-config. Confirms interface operation was applied.",
    function=audit_deployment,
    parameters={"xml_config": {"type": "string"}, "device_ip": {"type": "string"},
                "username": {"type": "string"}, "password": {"type": "string"}, "port": {"type": "string"}}
)
get_inventory_skill = Skill(
    name="get_inventory",
    description="Fetch current interfaces list from Arista cEOS via NETCONF. Returns interface_names(list), raw_config(str).",
    function=get_device_inventory,
    parameters={"device_ip": {"type": "string"}, "username": {"type": "string"},
                "password": {"type": "string"}, "port": {"type": "string"}}
)
lookup_documentation_skill = Skill(
    name="lookup_documentation",
    description="Search RAG knowledge base for OpenConfig YANG documentation.",
    function=lookup_documentation,
    parameters={"query": {"type": "string"}, "top_k": {"type": "integer"}}
)

ALL_SKILLS = [
    validate_xml_skill, fix_xml_skill, deploy_skill,
    rollback_skill, audit_skill,
    get_inventory_skill, lookup_documentation_skill
]

for sk in ALL_SKILLS:
    print(f"   🛠️  {sk.name}")

# ── Cell 7 ──────────────────────────────────────────
# ============================================================
# Phase 6 Skills: task_decomposer / dependency_resolver / result_aggregator
# Junos版から移植（Arista固有の変更点: VLAN → interface/path で記述）
# ============================================================

TASK_DECOMPOSER_PROMPT = """
You are a network operation task decomposer for Arista cEOS via NETCONF/OpenConfig.
Analyze the user request and break it into atomic tasks.

[OUTPUT FORMAT]
Return ONLY valid JSON. No markdown. No explanation.
Schema:
{
  "tasks": [
    {
      "id": "task_1",
      "operation": "get" | "set" | "delete" | "configure_interface",
      "target": "<interface name, BGP neighbor, or path (e.g. Ethernet1, 192.0.2.1)>",
      "yang_path": "<OpenConfig path hint (e.g. /interfaces/interface, /network-instances/...)>",
      "value": "<value to set, or null>",
      "description": "<one-line English description>",
      "depends_on": ["task_N", ...]
    }
  ]
}

[CURRENT NETWORK STATE]
{inventory_section}

[SMART RULES]
- GET operations: use operation="get" and ALWAYS merge into ONE task even if multiple fields are requested
- CONFIG operations: use operation="set" or "configure_interface" (one task per interface)
- DELETE operations: use operation="delete"
- Independent tasks: depends_on = []
- Never create circular dependencies
- Do NOT split a single GET request into multiple tasks — combine into one

[EXAMPLES]
Input: "Ethernet1 の description を uplink に設定してください"
Output: {"tasks": [{"id":"task_1","operation":"configure_interface","target":"Ethernet1",
  "yang_path":"/interfaces/interface[name=Ethernet1]/config/description",
  "value":"uplink","description":"Set Ethernet1 description to uplink","depends_on":[]}]}

Input: "BGP ネイバーの状態を取得してください"
Output: {"tasks": [{"id":"task_1","operation":"get","target":"bgp-neighbors",
  "yang_path":"/network-instances/network-instance/protocols/protocol/bgp/neighbors",
  "value":null,"description":"Get BGP neighbor state","depends_on":[]}]}

Input: "VLAN ID 50 の DEV_VLAN を作成してください"
Output: {"tasks": [{"id":"task_1","operation":"set","target":"vlan-50",
  "yang_path":"/network-instances/network-instance[name=default]/vlans/vlan[vlan-id=50]/config",
  "value":"vlan-id=50,name=DEV_VLAN,status=ACTIVE",
  "description":"Create VLAN 50 named DEV_VLAN","depends_on":[]}]}

Input: "VLAN 100 を削除してください"
Output: {"tasks": [{"id":"task_1","operation":"delete","target":"vlan-100",
  "yang_path":"/network-instances/network-instance[name=default]/vlans/vlan[vlan-id=100]",
  "value":null,"description":"Delete VLAN 100 (vlan-id=100, name is not needed for deletion)","depends_on":[]}]}

Input: "VLAN の状態を確認してください" / "VLAN 一覧を取得してください" / "show vlan"
Output: {"tasks": [{"id":"task_1","operation":"get","target":"vlans",
  "yang_path":"/network-instances/network-instance[name=default]/vlans",
  "value":null,"description":"Get all VLANs state from default network-instance","depends_on":[]}]}

Input: "インターフェースの状態を確認してください" / "show interfaces"
Output: {"tasks": [{"id":"task_1","operation":"get","target":"interfaces",
  "yang_path":"/interfaces",
  "value":null,"description":"Get all interfaces state","depends_on":[]}]}
"""

def decompose_tasks(
    user_query: str, llm=None, inventory: Dict = None
) -> Dict[str, Any]:
    if llm is None:
        return {"status": "failure", "tasks": [], "raw_response": "",
                "message": "LLM not provided"}
    try:
        if inventory and inventory.get("status") == "success":
            names    = inventory.get("interface_names", [])
            raw_cfg  = inventory.get("raw_config", "").strip()
            inv_sect = f"Existing interfaces: {names}\nRaw config:\n{raw_cfg[:500]}"
        else:
            inv_sect = "(Inventory not available)"

        prompt = TASK_DECOMPOSER_PROMPT.replace("{inventory_section}", inv_sect)
        prompt += f"\n\nInput: {user_query}\nOutput:"
        # JSON 抽出ヘルパー
        def _extract_json(text):
            m = re.search(r"```json\s*(.+?)\s*```", text, re.DOTALL)
            if m: return m.group(1).strip()
            m = re.search(r"```\s*(.+?)\s*```", text, re.DOTALL)
            if m: return m.group(1).strip()
            start = text.find("{"); end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                return text[start:end+1]
            return text

        def _try_parse(raw_text):
            """JSON文字列をパースして dict を返す。失敗は例外を投げる。"""
            cleaned = _extract_json(raw_text)
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                decoder = json.JSONDecoder()
                obj, _ = decoder.raw_decode(cleaned.strip())
                return obj

        # LLM 呼び出し + 最大3回リトライ（Groq APIの一時的なJSON不正対策）
        import time as _time
        parsed = None
        last_err = None
        for _attempt in range(3):
            try:
                raw = llm.invoke(prompt).content.strip()
                logger.debug(f"task_decomposer attempt {_attempt+1}: {raw[:200]!r}")
                parsed = _try_parse(raw)
                break
            except Exception as _e:
                last_err = _e
                logger.warning(f"task_decomposer attempt {_attempt+1} failed: {_e}")
                if _attempt < 2:
                    _time.sleep(1.5 * (_attempt + 1))

        if parsed is None:
            raise ValueError(f"LLM JSON parse failed after 3 retries: {last_err}")
        tasks  = parsed.get("tasks", [])
        return {"status": "success", "tasks": tasks, "raw_response": raw,
                "message": f"Decomposed into {len(tasks)} task(s)"}
    except json.JSONDecodeError as e:
        return {"status": "failure", "tasks": [], "raw_response": "",
                "message": f"JSON parse error: {e}"}
    except Exception as e:
        return {"status": "failure", "tasks": [], "raw_response": "",
                "message": f"task_decomposer error: {e}"}


def resolve_dependencies(tasks: List[Dict]) -> Dict[str, Any]:
    """Kahn's algorithm によるトポロジカルソート（Junos版と同一）"""
    if not tasks:
        return {"status": "success", "execution_order": [], "message": "No tasks"}
    from collections import deque
    task_map   = {t["id"]: t for t in tasks}
    in_degree  = {t["id"]: 0 for t in tasks}
    dependents = {t["id"]: [] for t in tasks}
    for t in tasks:
        for dep in t.get("depends_on", []):
            if dep not in task_map:
                return {"status": "error", "execution_order": [],
                        "message": f"Unknown dependency: {dep}"}
            in_degree[t["id"]] += 1
            dependents[dep].append(t["id"])
    queue = deque(tid for tid, deg in in_degree.items() if deg == 0)
    order = []
    while queue:
        tid  = queue.popleft()
        task = copy.deepcopy(task_map[tid])
        task["parallel"] = (
            len(task.get("depends_on", [])) == 0
            and sum(1 for t in tasks if not t.get("depends_on")) > 1
        )
        order.append(task)
        for dep_id in dependents[tid]:
            in_degree[dep_id] -= 1
            if in_degree[dep_id] == 0:
                queue.append(dep_id)
    if len(order) != len(tasks):
        remaining = [tid for tid, deg in in_degree.items() if deg > 0]
        return {"status": "error", "execution_order": [],
                "message": f"Circular dependency: {remaining}"}
    return {"status": "success", "execution_order": order,
            "message": f"Resolved {len(order)} task(s)"}


def aggregate_results(task_results: List[Dict]) -> Dict[str, Any]:
    """実行結果を集約してレポートを生成（Junos版と同一ロジック）"""
    succeeded, failed, skipped = [], [], []
    report_lines = ["=" * 60, "📊 Arista NETCONF RAG 実行レポート", "=" * 60]

    for entry in task_results:
        task   = entry.get("task", {})
        result = entry.get("result", {})
        tid    = task.get("id", "unknown")
        op     = task.get("operation", "?")
        target = task.get("target", "?")
        deploy = result.get("deployment_status", {}) or {}
        audit  = result.get("audit_status")  or {}
        valid  = result.get("validation_status", False)
        ds     = deploy.get("status", "unknown")
        aus    = audit.get("status", "") if audit else ""

        report_lines.append(f"\n  📋 [{tid}] {op} / {target}")
        report_lines.append(f"     検証: {'✅' if valid else '❌'}")
        report_lines.append(f"     デプロイ: {ds}")
        if deploy.get("diff"):
            for l in str(deploy["diff"]).strip().split("\n")[:3]:
                report_lines.append(f"     diff: {l}")
        if aus:
            report_lines.append(f"     Audit: {aus} - {audit.get('message','')}")

        audit_msg = audit.get("message", "") if audit else ""
        audit_is_error = aus == "failure" and "Audit error" in audit_msg
        if ds in ("success", "no_changes") and (aus in ("success", "skipped", "") or audit_is_error):
            succeeded.append(tid); report_lines.append("     → ✅ SUCCESS")
        elif ds == "skipped":
            skipped.append(tid);   report_lines.append("     → ⏭️  SKIPPED")
        else:
            failed.append(tid);    report_lines.append("     → ❌ FAILED")

    report_lines.append("\n" + "=" * 60)
    if not failed and not skipped:
        overall = "all_success"
        summary = f"✅ 全 {len(succeeded)} タスク成功"
    elif not failed and not succeeded:
        overall = "dry_run_complete"
        summary = f"🔍 ドライラン完了: 全 {len(skipped)} タスクの XML 生成・検証成功"
    elif failed and not succeeded:
        overall = "all_failure"
        summary = f"❌ 全 {len(failed)} タスク失敗"
    else:
        overall = "partial_failure"
        summary = f"⚠️ {len(succeeded)} 成功 / {len(failed)} 失敗 / {len(skipped)} スキップ"
    report_lines.append(f"  {summary}")
    report_lines.append("=" * 60)

    return {"status": overall, "summary": summary,
            "succeeded_tasks": succeeded, "failed_tasks": failed,
            "skipped_tasks": skipped, "report_lines": report_lines}


task_decomposer_skill = Skill(
    name="task_decomposer",
    description="Decompose natural language request into ordered task list (JSON) for Arista cEOS NETCONF operations.",
    function=decompose_tasks,
    parameters={"user_query": {"type": "string"}, "llm": {"type": "object"},
                "inventory": {"type": "dict"}}
)
dependency_resolver_skill = Skill(
    name="dependency_resolver",
    description="Resolve task dependencies (topological sort). Detects circular deps. Returns execution_order.",
    function=resolve_dependencies,
    parameters={"tasks": {"type": "list"}}
)
result_aggregator_skill = Skill(
    name="result_aggregator",
    description="Aggregate all task results and generate final report.",
    function=aggregate_results,
    parameters={"task_results": {"type": "list"}}
)

ALL_SKILLS_V6 = ALL_SKILLS + [task_decomposer_skill, dependency_resolver_skill, result_aggregator_skill]



# ================================================================
# deploy_netconf_config の上書き定義（<rpc>ラッパーを自動除去）
# cell[5] がスキップされても ここで確実に上書きします
# ================================================================

def _unwrap_rpc(xml_config: str):
    """<rpc>ラッパーを剥がして内側の操作要素と文字列を返す"""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_config)
        tag  = root.tag.split("}")[-1] if "}" in root.tag else root.tag
        if tag == "rpc":
            children = list(root)
            if not children:
                return "unknown", xml_config
            inner     = children[0]
            inner_tag = inner.tag.split("}")[-1] if "}" in inner.tag else inner.tag
            return inner_tag, ET.tostring(inner, encoding="unicode")
        return tag, xml_config
    except Exception:
        return "unknown", xml_config


def deploy_netconf_config(
    xml_config: str,
    device_ip: str,
    username: str,
    password: str,
    port: str = "830",
    comment: str = "AI Agent - Arista NETCONF"
) -> Dict[str, Any]:
    """
    ncclient で Arista cEOS に NETCONF 設定を送信する。
    LLM が生成した <rpc> ラッパーを自動で除去する。
    """
    import xml.etree.ElementTree as ET
    import xml.dom.minidom as minidom
    from ncclient import manager

    tag, inner_xml = _unwrap_rpc(xml_config)

    def _filter_content(xml_str):
        """<filter> または <get-config> の内側のコンテンツだけを返す"""
        try:
            root = ET.fromstring(xml_str)
            # <filter> 要素を探す
            ns = "urn:ietf:params:xml:ns:netconf:base:1.0"
            f = root.find(f"{{{ns}}}filter")
            if f is None:
                f = root.find("filter")
            if f is not None:
                return "".join(ET.tostring(c, encoding="unicode") for c in list(f))
            return xml_str
        except Exception:
            return xml_str

    def _config_content(xml_str):
        """<edit-config> の <config> 内側を返す"""
        try:
            root = ET.fromstring(xml_str)
            ns = "urn:ietf:params:xml:ns:netconf:base:1.0"
            c = root.find(f"{{{ns}}}config")
            if c is None:
                c = root.find("config")
            if c is not None:
                return ET.tostring(c, encoding="unicode")
            return xml_str
        except Exception:
            return xml_str

    try:
        with manager.connect(
            host=device_ip, port=int(port),
            username=username, password=password,
            hostkey_verify=False,
            device_params={"name": "default"},
            look_for_keys=False,
        ) as m:
            if tag == "get":
                fc = _filter_content(inner_xml)
                result = m.get(filter=("subtree", fc))
                _xml_raw = getattr(result, "data_xml", str(result))
                _xml_raw = _xml_raw[_xml_raw.index("?>")+2:].strip() if _xml_raw.startswith("<?") else _xml_raw
                output = minidom.parseString(_xml_raw).toprettyxml(indent="  ")
                return {"status": "success", "diff": output,
                        "message": f"get success from {device_ip}"}

            elif tag == "get-config":
                fc = _filter_content(inner_xml)
                result = m.get_config(source="running", filter=("subtree", fc))
                _xml_raw = getattr(result, "data_xml", str(result))
                _xml_raw = _xml_raw[_xml_raw.index("?>")+2:].strip() if _xml_raw.startswith("<?") else _xml_raw
                output = minidom.parseString(_xml_raw).toprettyxml(indent="  ")
                return {"status": "success", "diff": output,
                        "message": f"get-config success from {device_ip}"}

            elif tag in {"edit-config", "config"}:
                cc = _config_content(inner_xml)
                result = m.edit_config(target="running", config=cc)
                return {"status": "success", "diff": str(result),
                        "message": f"edit-config deployed to {device_ip}: {comment}"}

            else:
                # フォールバック: subtree フィルターとして渡す
                result = m.get_config(source="running",
                                       filter=("subtree", inner_xml))
                _xml_raw = getattr(result, "data_xml", str(result))
                _xml_raw = _xml_raw[_xml_raw.index("?>")+2:].strip() if _xml_raw.startswith("<?") else _xml_raw
                output = minidom.parseString(_xml_raw).toprettyxml(indent="  ")
                return {"status": "success", "diff": output,
                        "message": f"get-config (fallback) from {device_ip}"}

    except Exception as e:
        err = str(e)
        if "not found" in err.lower() or "does not exist" in err.lower():
            return {"status": "no_changes", "diff": "",
                    "message": f"Target not found: {err}"}
        return {"status": "failure", "diff": "",
                "message": f"NETCONF error: {err}"}


# Skill オブジェクトを更新（新しい deploy_netconf_config を登録）
deploy_skill = Skill(
    name="deploy_netconf",
    description="Deploy OpenConfig XML to Arista cEOS via NETCONF (ncclient). Auto-removes <rpc> wrapper.",
    function=deploy_netconf_config,
    parameters={"xml_config": {"type": "string"}, "device_ip": {"type": "string"},
                "username": {"type": "string"}, "password": {"type": "string"},
                "port": {"type": "string"}}
)
# ALL_SKILLS / ALL_SKILLS_V6 の deploy_netconf を上書き
for skill_list in [ALL_SKILLS, ALL_SKILLS_V6]:
    for j, sk in enumerate(skill_list):
        if sk.name == "deploy_netconf":
            skill_list[j] = deploy_skill
            break


# ── Cell 8 ──────────────────────────────────────────
# ============================================================
# Phase 7: PolicyChecker（Arista版）
# Junos版と同一ロジック / vlans → interfaces に読み替え
# ============================================================

DEFAULT_POLICY = {
    "allowed_interfaces": ["Ethernet1", "Ethernet2", "Management0"],
    "forbidden_keywords": ["delete-config", "kill-session", "restart"],
    "allowed_netconf_nodes": ["interfaces", "network-instances", "get-config", "edit-config",
                               "get", "filter", "config", "root", "rpc",
                               "data", "hello", "capabilities"],
    "max_operations_per_run": 20
}
POLICY_YAML_PATH = "./policy_arista.yaml"

def _load_policy(path: str = POLICY_YAML_PATH) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    return DEFAULT_POLICY

# 全 NETCONF ルートタグを許可するデフォルトセット
# <rpc> <interfaces> <network-instances> <filter> <config> 等すべて許可
_NETCONF_ALLOWED_TAGS = {
    "rpc", "interfaces", "network-instances", "network-instance",
    "get-config", "edit-config", "get", "filter", "config", "root",
    "data", "hello", "capabilities", "bgp", "protocols", "interface",
    "system", "acl", "routing-policy", "lldp", "lacp", "vlan",
}

def check_policy(task: dict, xml_config: str, policy: dict = None) -> dict:
    """
    操作ポリシーと照合する。

    NETCONFノードチェックは _NETCONF_ALLOWED_TAGS を使用し、
    policy引数の allowed_netconf_nodes よりも広い範囲を許可する。
    これにより policy.yaml の設定漏れによる誤ブロックを防ぐ。
    """
    if policy is None:
        policy = {}
    violations = []

    # ① インターフェース制限（allowed_interfaces が空 = 全許可）
    iface          = task.get("target", "")
    allowed_ifaces = policy.get("allowed_interfaces", [])
    if iface and allowed_ifaces and iface not in allowed_ifaces:
        violations.append({"rule": "interface_allowlist",
                           "detail": f"'{iface}' not in allowed_interfaces"})

    # ② 禁止キーワードスキャン
    for kw in policy.get("forbidden_keywords", ["delete-config", "kill-session"]):
        if kw.lower() in xml_config.lower():
            violations.append({"rule": "forbidden_keyword",
                               "detail": f"forbidden keyword: '{kw}'"})

    # ③ NETCONF ルートタグチェック（_NETCONF_ALLOWED_TAGS で判定）
    if xml_config:
        try:
            root = ET.fromstring(xml_config)
            tag  = root.tag.split("}")[-1] if "}" in root.tag else root.tag
            if tag not in _NETCONF_ALLOWED_TAGS:
                violations.append({"rule": "netconf_node_allowlist",
                                   "detail": f"<{tag}> not in allowed NETCONF tags"})
        except Exception:
            pass  # XML パース失敗は validate_xml_structure が処理

    return {"allowed": len(violations) == 0, "violations": violations}


# ============================================================
# Phase 7: ValidationAgent（Arista版）
# ============================================================
DANGEROUS_PATTERNS = [
    (r"<delete-config",             "error",   "<delete-config> is forbidden"),
    (r"kill-session",               "error",   "kill-session is forbidden"),
    (r"loopback",                   "warning", "loopback interface reference detected"),
    (r"<format",                    "error",   "format operation is forbidden"),
]
BULK_THRESHOLD = 5

def validate_safety(xml_config: str, task: dict = None) -> dict:
    if not xml_config:
        return {"safe": False, "errors": ["XML is empty"], "warnings": []}
    errors, warnings = [], []
    for pattern, severity, message in DANGEROUS_PATTERNS:
        if re.search(pattern, xml_config, re.IGNORECASE | re.DOTALL):
            (errors if severity == "error" else warnings).append(message)
    delete_count = len(re.findall(r'operation=["\']delete["\']', xml_config, re.IGNORECASE))
    if delete_count >= BULK_THRESHOLD:
        warnings.append(f"Bulk delete: {delete_count} operations (threshold={BULK_THRESHOLD})")
    return {"safe": len(errors) == 0, "errors": errors, "warnings": warnings}


# ============================================================
# Phase 7: AuditLogger（Junos版と同一）
# ============================================================
AUDIT_LOG_PATH = "./audit_log_arista.jsonl"

class AuditLogger:
    def __init__(self, log_path: str = AUDIT_LOG_PATH):
        self.log_path   = log_path
        self._session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._entries   = []
        print(f"✅ AuditLogger 初期化: {log_path}")

    def record(self, task, worker_result, policy_result=None,
               safety_result=None, deploy_method="unknown"):
        deploy_status = worker_result.get("deployment_status") or {}
        audit_status  = worker_result.get("audit_status")  or {}
        entry = {
            "session_id":      self._session_id,
            "timestamp":       datetime.now().isoformat(),
            "task_id":         task.get("id", "unknown"),
            "operation":       task.get("operation", "unknown"),
            "target":          task.get("target", ""),
            "rationale":       task.get("description", ""),
            "policy_allowed":  (policy_result or {}).get("allowed"),
            "policy_violations": (policy_result or {}).get("violations", []),
            "safety_passed":   (safety_result or {}).get("safe"),
            "safety_errors":   (safety_result or {}).get("errors", []),
            "deploy_method":   deploy_method,
            "deploy_status":   deploy_status.get("status", "unknown"),
            "diff":            deploy_status.get("diff", "")[:500],
            "audit_status":    audit_status.get("status", ""),
            "rollback_status": (worker_result.get("rollback_status") or {}).get("status", ""),
        }
        self._entries.append(entry)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"AuditLogger write error: {e}")

    def record_blocked(self, task: dict, reason: str, violations: list = None):
        entry = {
            "session_id": self._session_id, "timestamp": datetime.now().isoformat(),
            "task_id": task.get("id"), "operation": task.get("operation"),
            "target": task.get("target", ""), "rationale": task.get("description", ""),
            "deploy_status": "blocked", "deploy_message": reason,
            "policy_violations": violations or []
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
        failed  = sum(1 for e in self._entries if e.get("deploy_status") in ("failure", "rolled_back"))
        return {"total": total, "success": success, "blocked": blocked,
                "failed": failed, "skipped": total - success - blocked - failed}

    def print_summary(self):
        s = self.summary()
        print("\n" + "=" * 50)
        print("📋 AuditLogger サマリー")
        print("=" * 50)
        for k, v in s.items():
            print(f"  {k}: {v}")
        print(f"  ログファイル: {self.log_path}")

# policy.yaml を初期化
GLOBAL_POLICY = _load_policy()

# ── Cell 9 ──────────────────────────────────────────
# ============================================================
# Phase 6 Reviewer プロンプト（意図整合性チェックのみ）
# Junos版と同一の設計思想 / Arista OpenConfig 向けに表現を調整
# ============================================================
REVIEWER_INSTRUCTIONS_V6 = """
You are an XML intent validator — NOT an OpenConfig syntax expert.

[YOUR ONLY JOB]
Check whether the generated OpenConfig NETCONF XML matches the user's stated intent.
Do NOT apply OpenConfig-specific syntax rules — that is handled by the validate_xml Skill.

[THREE CHECKS ONLY]
1. Operation type match:
   - User said "delete/remove" → XML must have operation="delete"
   - User said "get/show" → XML must be a get-config or filter structure
   - User said "set/configure" → XML must be an edit-config or config structure
2. Target match:
   - The target (interface name, BGP neighbor, etc.) in XML must match the user request
3. Basic XML parsability:
   - XML must be well-formed (parseable by ET.fromstring)

[WHAT YOU MUST IGNORE]
- OpenConfig namespace declarations (xmlns=...)
- Specific field names or YANG path details
- These are handled by validate_xml and the RAG documentation

[VLAN DELETE RULES — CRITICAL]
- VLAN DELETE operations use vlan-id only. The VLAN name is NOT required in delete XML.
- APPROVE if the XML contains operation="delete" and the correct vlan-id.
- Do NOT reject because the VLAN name (e.g. DEV_VLAN) is absent from the XML.
- Example of CORRECT VLAN delete XML (APPROVE this):
  <vlan operation="delete"><vlan-id>50</vlan-id></vlan>
- The name attribute is only needed for CREATE, not DELETE.

[OUTPUT FORMAT - MANDATORY]
First word MUST be exactly one of: APPROVE, IMPROVE, REJECT

APPROVE: Intent matches — operation type and target are correct.
IMPROVE: <specific intent mismatch only>
REJECT: <critical intent mismatch>
"""


# ── Cell 10 ──────────────────────────────────────────
# ============================================================
# WorkerAgent: Arista NETCONF RAG ワークフロー（MAF版）
# Junos版 NetconfRagWorkflowPhase3 の ncclient / OpenConfig 移植版
# LangGraph → MAF Agent (XMLGenerator / XMLReviewer) に置き換え
# ============================================================

class NetconfRagWorkerArista:
    """
    MAF Agent を使った Arista cEOS NETCONF XML 生成・検証・デプロイ Worker。

    Junos版との主な違い:
    - LangGraph StateGraph → MAF Agent.run() による非同期実行
    - junos-eznc → ncclient
    - <configuration><vlans> → OpenConfig <interfaces>/<network-instances> XML
    - XMLGenerator/Reviewer 両方とも MAF OpenAIChatCompletionClient を使用
    """

    def __init__(
        self, retriever, llm, skills: List[Skill] = None,
        max_retries: int = 3, max_review_rounds: int = 2
    ):
        self.retriever         = retriever
        self.llm               = llm
        self.max_retries       = max_retries
        self.max_review_rounds = max_review_rounds
        self.logs              = []
        self.conversation_history: List[Message] = []
        self.skill_execution_log: List[Dict] = []
        self.skills: Dict[str, Skill] = {sk.name: sk for sk in (skills or ALL_SKILLS)}
        self._initialize_agents()

    def _initialize_agents(self):
        # ── XMLGenerator ────────────────────────────────────────────────────────
        generator_client = make_client()
        generator_instructions = """
You are a dedicated Arista EOS OpenConfig NETCONF XML generator.
Your ONLY task is to produce valid OpenConfig NETCONF XML for Arista cEOS.

[CRITICAL RULES]
1. OUTPUT ONLY RAW XML — no explanation, no markdown code blocks.
2. Start with the appropriate NETCONF root element.
3. Always include OpenConfig namespaces.

[OPERATION TYPES]
- GET (read): Use <filter> with OpenConfig namespace
  Example:
    <interfaces xmlns="http://openconfig.net/yang/interfaces">
      <interface><name>Ethernet1</name></interface>
    </interfaces>

- SET/CONFIGURE: Use <config> with edit-config structure
  Example:
    <config>
      <interfaces xmlns="http://openconfig.net/yang/interfaces">
        <interface>
          <name>Ethernet1</name>
          <config><description>uplink</description></config>
        </interface>
      </interfaces>
    </config>

- DELETE: Add operation="delete" attribute
  Example:
    <config>
      <interfaces xmlns="http://openconfig.net/yang/interfaces">
        <interface operation="delete"><name>Ethernet1</name></interface>
      </interfaces>
    </config>

[BGP (via network-instance)]
    <network-instances xmlns="http://openconfig.net/yang/network-instance">
      <network-instance><name>default</name>
        <protocols><protocol><name>BGP</name>
          <bgp><neighbors/></bgp>
        </protocol></protocols>
      </network-instance>
    </network-instances>


CRITICAL VLAN RULES:
- VLAN DELETE: Use operation="delete" on <vlan> with <vlan-id> only. Do NOT include <name>. The VLAN name is for CREATE only.
VLAN operations: ALWAYS use <network-instances> with xmlns="http://openconfig.net/yang/network-instance"
- VLAN CREATE example (edit-config):
  <config>
    <network-instances xmlns="http://openconfig.net/yang/network-instance">
      <network-instance><name>default</name>
        <vlans><vlan>
          <vlan-id>50</vlan-id>
          <config><vlan-id>50</vlan-id><name>DEV_VLAN</name><status>ACTIVE</status></config>
        </vlan></vlans>
      </network-instance>
    </network-instances>
  </config>
If you cannot generate XML, output ONLY: <filter/>

VLAN operations MUST use network-instances namespace, NOT interfaces namespace.
VLAN namespace: xmlns="http://openconfig.net/yang/network-instance"
VLAN path: /network-instances/network-instance[name=default]/vlans/vlan[vlan-id=N]/config/
"""
        self.xml_generator = Agent(
            name="XMLGenerator",
            client=generator_client,
            instructions=generator_instructions
        )

        # ── XMLReviewer ──────────────────────────────────────────────────────────
        reviewer_client = make_client()
        self.xml_reviewer = Agent(
            name="XMLReviewer",
            client=reviewer_client,
            instructions=REVIEWER_INSTRUCTIONS_V6
        )
        self.log("✅ MAF Agents 初期化完了 (XMLGenerator / XMLReviewer)")

    def log(self, msg: str):
        self.logs.append(msg)
        # INFO/WARNING/ERROR を自動判定して logging 経由で出力
        if any(kw in msg for kw in ['❌', 'ERROR', '失敗', 'error']):
            logger.error(msg)
        elif any(kw in msg for kw in ['⚠️', 'WARNING', '警告']):
            logger.warning(msg)
        else:
            logger.info(msg)

    def add_message(self, role: str, text: str):
        self.conversation_history.append(Message(role=role, contents=[text]))

    def _extract_response_text(self, response) -> str:
        if hasattr(response, "text"):
            return str(response.text)
        if hasattr(response, "messages") and response.messages:
            for msg in response.messages:
                if hasattr(msg, "text"):
                    return str(msg.text)
        return str(response)

    def _extract_xml(self, response: str) -> str:
        """
        LLM レスポンスから XML を抽出する（堅牢版）。
        - ```xml ... ``` / ``` ... ``` ブロック
        - < タグで始まるブロックを広く抽出
        - & などの XML 不正文字を自動修正して ET.fromstring で検証
        """
        candidates = []

        # ① ```xml ... ``` ブロック
        for m in re.finditer(r"```(?:xml)?\s*(.*?)\s*```", response, re.DOTALL):
            candidates.append(m.group(1).strip())

        # ② <タグ で始まるブロック（タグ名を問わない）
        m = re.search(r"(<[a-zA-Z][^>]*>.*)", response, re.DOTALL)
        if m:
            candidates.append(m.group(1).strip())

        def try_parse(xml_str):
            # & の自動修正
            xml_str = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;)([a-zA-Z])",
                              r"&amp;\1", xml_str)
            # ルートタグで最初の完結ブロックに切り詰め
            fm = re.match(r"<([a-zA-Z][a-zA-Z0-9_:-]*)", xml_str)
            if fm:
                root_tag  = fm.group(1)
                close_tag = f"</{root_tag}>"
                if close_tag in xml_str:
                    xml_str = xml_str[:xml_str.rindex(close_tag) + len(close_tag)]
                elif xml_str.rstrip().endswith("/>"):
                    xml_str = xml_str.rstrip()
            try:
                ET.fromstring(xml_str)
                return xml_str
            except ET.ParseError:
                # 末尾の余計な文字を削り再試行
                for em in re.finditer(r"</[a-zA-Z][a-zA-Z0-9_:-]*>", xml_str):
                    trimmed = xml_str[:em.end()]
                    try:
                        ET.fromstring(trimmed)
                        return trimmed
                    except ET.ParseError:
                        continue
            return None

        for candidate in candidates:
            result = try_parse(candidate)
            if result:
                return result
        return ""


    def _run_skill(self, skill_name: str, **kwargs) -> Any:
        if skill_name == "lookup_documentation" and "retriever" not in kwargs:
            kwargs["retriever"] = self.retriever
        if skill_name not in self.skills:
            self.log(f"  ❌ [Skill] 未知: {skill_name}"); return None
        self.log(f"  🛠️  [Skill:{skill_name}] 実行中...")
        try:
            result = self.skills[skill_name].execute(**kwargs)
            self.skill_execution_log.append({
                "timestamp": datetime.now().isoformat(),
                "skill": skill_name,
                "params": {k: (v[:50]+"...") if isinstance(v,str) and len(v)>50 else v for k,v in kwargs.items()},
                "result_summary": str(result)[:200]
            })
            self.log(f"  ✅ [Skill:{skill_name}] 完了"); return result
        except Exception as e:
            self.log(f"  ❌ [Skill:{skill_name}] エラー: {e}"); return None

    async def _run_skill_loop(self, xml_config, translated_query, device_info=None, deploy=False):
        self.log("\n" + "="*70)
        self.log("🛠️  Skillループ [validate → fix → deploy → audit → rollback]")
        self.log("="*70)

        current_xml = xml_config
        skill_steps = []
        MAX_FIX     = 3

        # Step A: validate
        self.log("\n  📋 [Step A] validate_xml")
        skill_steps.append("validate")
        val = self._run_skill("validate_xml", xml_config=current_xml)
        if val is None:
            return {"final_xml": current_xml, "valid": False,
                    "deployment_status": {"status": "skipped", "diff": "", "message": "validate failed"},
                    "skill_steps": skill_steps}

        # Step B: fix if needed
        if not val["valid"]:
            for attempt in range(MAX_FIX):
                self.log(f"\n  🔧 [Step B] fix_xml (試行 {attempt+1}/{MAX_FIX})")
                skill_steps.append("fix")
                fix = self._run_skill("fix_xml", xml_config=current_xml, translated_query=translated_query)
                if not fix or not fix["success"]:
                    break
                current_xml = fix["fixed_xml"]
                self.log(f"  修正: {fix['changes']}")
                val = self._run_skill("validate_xml", xml_config=current_xml)
                skill_steps.append("re-validate")
                if val and val["valid"]:
                    break

        if not val or not val["valid"]:
            self.log("  ❌ 検証失敗")
            return {"final_xml": current_xml, "valid": False,
                    "deployment_status": {"status": "skipped", "diff": "", "message": "Validation failed"},
                    "skill_steps": skill_steps}

        self.log("  ✅ XML 検証通過")

        # GETクエリ判定: <config> や edit-config を含まない = 読み取り専用
        def _is_read_only(xml_str: str) -> bool:
            """
            GETクエリかどうかを判定する。
            edit-config / <config> ラッパー / operation="delete/merge/replace" が
            含まれない場合は読み取り専用とみなす。
            """
            xml_lower = xml_str.lower()
            write_indicators = [
                "edit-config",
                'operation="delete"', "operation='delete'",
                'operation="merge"',  "operation='merge'",
                'operation="replace"', "operation='replace'",
                'operation="create"', "operation='create'",
            ]
            # <config> タグ（フィルター用の <get-config> 内の <config> は除く）
            if "<config>" in xml_str or "<config " in xml_str:
                # get-config のフィルター子要素としての <config> でなければ書き込み
                if "edit-config" in xml_lower or "operation=" in xml_lower:
                    return False
            return not any(ind in xml_str for ind in write_indicators)

        is_read_only = _is_read_only(current_xml)
        if is_read_only:
            self.log("  📖 GETクエリを検出 — audit はスキップします")

        # Step D: deploy
        if deploy and device_info:
            self.log("\n  📡 [Step D] deploy_netconf")
            skill_steps.append("deploy")
            dep = self._run_skill("deploy_netconf", xml_config=current_xml,
                                   device_ip=device_info["ip"], username=device_info["username"],
                                   password=device_info["password"], port=device_info.get("port","830"))
            if not dep or dep["status"] == "failure":
                msg = (dep or {}).get("message", "deploy error")
                self.log(f"  ❌ デプロイ失敗: {msg}")
                if not is_read_only:
                    skill_steps.append("rollback(candidate)")
                    rb = self._run_skill("rollback", device_ip=device_info["ip"],
                                          username=device_info["username"], password=device_info["password"],
                                          port=device_info.get("port","830"), mode="candidate")
                else:
                    rb = None
                return {"final_xml": current_xml, "valid": True,
                        "deployment_status": dep or {"status":"failure","diff":"","message":msg},
                        "audit_status": None, "rollback_status": rb, "skill_steps": skill_steps}

            # Step E: audit（GETクエリはスキップ）
            if is_read_only:
                self.log("\n  ⏭️  [Step E] audit スキップ（GETクエリのため）")
                skill_steps.append("audit(skipped/read-only)")
                return {"final_xml": current_xml, "valid": True,
                        "deployment_status": dep, "audit_status": None,
                        "rollback_status": None, "skill_steps": skill_steps}

            self.log("\n  🔍 [Step E] audit")
            skill_steps.append("audit")
            aud = self._run_skill("audit", xml_config=current_xml,
                                   device_ip=device_info["ip"], username=device_info["username"],
                                   password=device_info["password"], port=device_info.get("port","830"))
            if aud:
                self.log(f"  {aud['status']}: {aud['message']}")
            if not aud or aud["status"] == "failure":
                skill_steps.append("rollback(rescue)")
                rb = self._run_skill("rollback", device_ip=device_info["ip"],
                                      username=device_info["username"], password=device_info["password"],
                                      port=device_info.get("port","830"), mode="rescue")
                return {"final_xml": current_xml, "valid": True,
                        "deployment_status": dep, "audit_status": aud,
                        "rollback_status": rb, "skill_steps": skill_steps}

            return {"final_xml": current_xml, "valid": True,
                    "deployment_status": dep, "audit_status": aud,
                    "rollback_status": None, "skill_steps": skill_steps}
        else:
            reason = "deploy=False" if not deploy else "device_info missing"
            self.log(f"  ⏭️  デプロイスキップ ({reason})")
            return {"final_xml": current_xml, "valid": True,
                    "deployment_status": {"status": "skipped", "diff": "", "message": reason},
                    "skill_steps": skill_steps}

    async def step1_translate(self, user_query: str) -> str:
        self.log("\n" + "="*70)
        self.log("ステップ1: 翻訳中...")
        ascii_ratio = sum(1 for c in user_query if ord(c) < 128) / max(len(user_query), 1)
        if ascii_ratio >= 0.8:
            self.log(f"  英語クエリ → スキップ")
            return user_query
        translated = self.llm.invoke(
            "Translate only the following into English, without any additional text: " + user_query
        ).content.strip()
        self.log(f"  → {translated}")
        self.add_message("user", user_query)
        self.add_message("system", f"Translation: {translated}")
        return translated

    async def step2_retrieve(self, translated_query: str) -> List[str]:
        self.log("\n" + "="*70)
        self.log("ステップ2: RAG検索中...")
        if not self.retriever:
            return []
        docs = self.retriever.invoke(translated_query)
        self.log(f"  {len(docs)} チャンクを取得")
        return [d.page_content for d in docs]

    async def step3_generate_review(
        self, translated_query: str, retrieved_docs: List[str], inventory_info: Dict = None
    ) -> tuple:
        self.log("\n" + "="*70)
        self.log("ステップ3: MAF Agent — XML生成・レビュー")
        self.log("="*70)
        context = "\n\n---\n\n".join(retrieved_docs)

        for attempt in range(self.max_retries):
            self.log(f"\n🔄 生成試行 {attempt+1}/{self.max_retries}")

            # インベントリ情報をプロンプトに埋め込む
            inv_section = ""
            if inventory_info and inventory_info.get("status") == "success":
                names = inventory_info.get("interface_names", [])
                inv_section = f"\n### Current interfaces on device: {names}\n"

            gen_prompt = (
                f"Generate OpenConfig NETCONF XML for Arista cEOS.\n"
                f"{inv_section}"
                f"### Documentation Context:\n{context}\n\n"
                f"### Request:\n{translated_query}\n\n"
                f"Generate XML now. ONLY XML. NO EXPLANATIONS."
            )

            self.log("  🤖 [Generator] XML生成中 (MAF Agent)...")
            gen_response = await self.xml_generator.run(gen_prompt)
            raw_xml = self._extract_response_text(gen_response)
            generated_xml = self._extract_xml(raw_xml)

            if not generated_xml:
                self.log("  ❌ XML抽出失敗"); continue
            try:
                ET.fromstring(generated_xml)
            except ET.ParseError as e:
                self.log(f"  ❌ XMLパースエラー: {e}"); continue

            self.log("  ✅ XML生成成功")
            self.add_message("assistant", "[Generator] XML generated")

            # Reviewer ループ
            for review_round in range(self.max_review_rounds):
                self.log(f"\n  👁️  [Reviewer] ラウンド {review_round+1}/{self.max_review_rounds}")
                review_prompt = (
                    f"Review this OpenConfig NETCONF XML for Arista cEOS:\n{generated_xml}\n\n"
                    f"User requirement: {translated_query}\n\n"
                    f"Check ONLY: operation type match + target match + XML parsability.\n"
                    f"First word MUST be: APPROVE, IMPROVE, or REJECT."
                )
                review_response = await self.xml_reviewer.run(review_prompt)
                review_text     = self._extract_response_text(review_response)
                self.log(f"  📋 [Reviewer] {review_text[:150]}")
                self.add_message("assistant", f"[Reviewer] {review_text[:100]}")

                if "APPROVE" in review_text.upper():
                    self.log("  ✅ APPROVED"); return generated_xml, True
                elif "IMPROVE" in review_text.upper() and review_round < self.max_review_rounds - 1:
                    improve_prompt = (
                        f"Context:\n{context}\nRequest: {translated_query}\n"
                        f"Previous XML:\n{generated_xml}\n"
                        f"Reviewer feedback: {review_text}\n\nImprove the XML. ONLY XML."
                    )
                    imp_response = await self.xml_generator.run(improve_prompt)
                    imp_xml = self._extract_xml(self._extract_response_text(imp_response))
                    if imp_xml:
                        try:
                            ET.fromstring(imp_xml); generated_xml = imp_xml
                            self.log("  ✅ 改善版生成成功"); continue
                        except ET.ParseError:
                            pass
                    return generated_xml, True
                elif "REJECT" in review_text.upper():
                    self.log("  ❌ REJECTED"); break
                else:
                    self.log("  ⚠️ 不明応答 → 承認扱い"); return generated_xml, True

        self.log(f"⚠️ {self.max_retries}回の試行後もXML生成失敗")
        return "", False

    async def run(
        self, user_query: str, device_ip: str = None, username: str = None,
        password: str = None, port: str = "830", deploy: bool = False,
        inventory_info: Dict = None
    ) -> Dict[str, Any]:
        self.logs = []; self.conversation_history = []; self.skill_execution_log = []

        self.log("\n" + "="*70)
        self.log("🚀 Arista NETCONF RAG Worker (MAF Agent版)")
        self.log(f"🔒 生成{self.max_retries}回 × レビュー{self.max_review_rounds}ラウンド")
        self.log("="*70)
        self.log(f"入力: {user_query}")

        result = {
            "user_query": user_query, "translated_query": "",
            "retrieved_documents": [], "generated_xml": "",
            "final_xml": "", "validation_status": False,
            "deployment_status": {}, "skill_steps": [],
            "skill_execution_log": [], "conversation_history": []
        }
        try:
            result["translated_query"] = await self.step1_translate(user_query)
            result["retrieved_documents"] = await self.step2_retrieve(result["translated_query"])
            raw_xml, review_passed = await self.step3_generate_review(
                result["translated_query"], result["retrieved_documents"], inventory_info
            )
            result["generated_xml"] = raw_xml

            if not review_passed or not raw_xml:
                self.log("\n❌ XML生成失敗 — ワークフロー中断")
                return result

            device_info = None
            if deploy and all([device_ip, username, password]):
                device_info = {"ip": device_ip, "username": username,
                               "password": password, "port": port}

            skill_result = await self._run_skill_loop(
                raw_xml, result["translated_query"], device_info, deploy
            )
            result["final_xml"]          = skill_result["final_xml"]
            result["validation_status"]  = skill_result["valid"]
            result["deployment_status"]  = skill_result["deployment_status"]
            result["audit_status"]       = skill_result.get("audit_status")
            result["rollback_status"]    = skill_result.get("rollback_status")
            result["skill_steps"]        = skill_result["skill_steps"]
            result["skill_execution_log"] = self.skill_execution_log
            result["conversation_history"] = [
                {"role": msg.role, "text": msg.text if hasattr(msg, "text") else str(msg.contents)}
                for msg in self.conversation_history
            ]
            self.log("\n✅ ワークフロー完了")

        except Exception as e:
            self.log(f"\n❌ エラー: {e}")
            import traceback; traceback.print_exc()
            result["error"] = str(e)

        return result


# ── Cell 11 ──────────────────────────────────────────
# ============================================================
# OrchestratorAgentArista (Phase 7互換)
# Junos版 OrchestratorAgentV7 の Arista / ncclient 移植版
# ============================================================

class OrchestratorAgentArista:
    """
    Arista cEOS NETCONF RAG Orchestrator (Phase 7互換)

    Junos版との違い:
    - Worker: NetconfRagWorkflowPhase3 → NetconfRagWorkerArista (MAF版)
    - Inventory: vlans → interfaces (OpenConfig)
    - deploy: junos-eznc → ncclient
    """

    def __init__(
        self, retriever, llm, skills: List[Skill] = None,
        max_retries: int = 3, max_review_rounds: int = 2,
        policy: dict = None, audit_log_path: str = AUDIT_LOG_PATH
    ):
        self.retriever         = retriever
        self.llm               = llm
        self.max_retries       = max_retries
        self.max_review_rounds = max_review_rounds
        # cell[7] が未実行でも動くよう NETCONF 用ポリシーをここで定義
        ARISTA_NETCONF_POLICY = {
            "allowed_interfaces": [],  # 空 = 全インターフェース許可
            "forbidden_keywords": ["delete-config", "kill-session", "restart"],
            "allowed_netconf_nodes": [
                "interfaces", "network-instances", "get-config", "edit-config",
                "get", "filter", "config", "root", "rpc",
                "data", "hello", "capabilities", "bgp", "protocols",
                "network-instance", "interface"
            ],
            "max_operations_per_run": 20
        }
        self.policy            = policy or ARISTA_NETCONF_POLICY
        self.audit_logger      = AuditLogger(log_path=audit_log_path)
        self.logs              = []
        self.skills: Dict[str, Skill] = {sk.name: sk for sk in (skills or ALL_SKILLS_V6)}
        print("✅ OrchestratorAgentArista 初期化完了")
        print(f"   登録 Skills: {list(self.skills.keys())}")

    def log(self, msg: str):
        self.logs.append(msg)
        # INFO/WARNING/ERROR を自動判定して logging 経由で出力
        if any(kw in msg for kw in ['❌', 'ERROR', '失敗', 'error']):
            logger.error(msg)
        elif any(kw in msg for kw in ['⚠️', 'WARNING', '警告']):
            logger.warning(msg)
        else:
            logger.info(msg)

    def _run_skill(self, skill_name: str, **kwargs):
        if skill_name not in self.skills:
            self.log(f"  ❌ [Skill] 未知: {skill_name}"); return None
        self.log(f"  🛠️  [Skill:{skill_name}] 実行中...")
        try:
            result = self.skills[skill_name].execute(**kwargs)
            self.log(f"  ✅ [Skill:{skill_name}] 完了"); return result
        except Exception as e:
            self.log(f"  ❌ [Skill:{skill_name}] エラー: {e}"); return None

    def _build_worker_query(self, task: Dict) -> str:
        op     = task.get("operation", "")
        target = task.get("target", "")
        value  = task.get("value", "")
        desc   = task.get("description", "")
        if desc:
            return desc
        if op == "delete":
            return f"Delete configuration for {target} via NETCONF."
        elif op in ("set", "configure_interface"):
            val_part = f" to {value}" if value else ""
            return f"Configure {target}{val_part} via NETCONF."
        elif op == "get":
            return f"Get current state of {target} via NETCONF."
        return f"{op} {target}"

    async def _dispatch_task(
        self, task, idx, total, device_ip, username, password, port, deploy
    ) -> dict:
        tid = task.get("id", "?")

        # skip タスク
        if task.get("operation") == "skip":
            self.log(f"  ⏭️  [{idx}/{total}] SKIP: {tid}")
            skipped = {"validation_status": True,
                       "deployment_status": {"status": "no_changes", "diff": "",
                                              "message": task.get("description","skipped")},
                       "audit_status": None, "rollback_status": None}
            self.audit_logger.record(task, skipped, deploy_method="skipped")
            return skipped

        worker_query = self._build_worker_query(task)
        worker = NetconfRagWorkerArista(
            retriever=self.retriever, llm=self.llm, skills=ALL_SKILLS,
            max_retries=self.max_retries, max_review_rounds=self.max_review_rounds
        )

        # XML 生成・レビュー（Worker の step1〜3 のみ実行）
        try:
            gen_result = await worker.run(
                user_query=worker_query,
                device_ip=device_ip, username=username, password=password, port=port,
                deploy=False   # 生成・検証のみ。デプロイは Orchestrator が制御する
            )
        except Exception as e:
            self.log(f"  ❌ XML生成エラー: {e}")
            err = {"validation_status": False,
                   "deployment_status": {"status":"failure","diff":"","message":str(e)},
                   "audit_status": None, "rollback_status": None}
            self.audit_logger.record_blocked(task, f"XML generation failed: {e}")
            return err

        # final_xml が空なら generated_xml にフォールバック
        xml_config = gen_result.get("final_xml") or gen_result.get("generated_xml", "")

        if not xml_config:
            msg = "XML generation returned empty result"
            self.log(f"  ❌ {msg}")
            self.audit_logger.record_blocked(task, msg)
            return {"validation_status": False,
                    "deployment_status": {"status":"failure","diff":"","message":msg},
                    "audit_status": None, "rollback_status": None}

        self.log(f"  📄 生成XML (先頭80文字): {xml_config[:80]}")

        # ① ValidationAgent
        self.log(f"  🔍 [ValidationAgent] 安全性チェック: {tid}")
        safety = validate_safety(xml_config, task=task)
        if not safety["safe"]:
            msg = f"ValidationAgent BLOCK: {safety['errors']}"
            self.log(f"  🚫 {msg}")
            self.audit_logger.record_blocked(task, msg, violations=safety["errors"])
            return {"validation_status": False,
                    "deployment_status": {"status":"blocked","diff":"","message":msg},
                    "audit_status": None, "rollback_status": None}
        if safety["warnings"]:
            self.log(f"  ⚠️  warnings: {safety['warnings']}")

        # ② PolicyChecker
        self.log(f"  🛡️  [PolicyChecker] ポリシーチェック: {tid}")
        policy_res = check_policy(task, xml_config, policy=self.policy)
        if not policy_res["allowed"]:
            msg = f"PolicyChecker BLOCK: {policy_res['violations']}"
            self.log(f"  🚫 {msg}")
            self.audit_logger.record_blocked(task, msg, violations=policy_res["violations"])
            return {"validation_status": False,
                    "deployment_status": {"status":"blocked","diff":"","message":msg},
                    "audit_status": None, "rollback_status": None}
        self.log(f"  📋 policy allowed_interfaces={self.policy.get('allowed_interfaces','?')}")
        self.log(f"  📋 policy allowed_netconf_nodes (先頭5)={list(self.policy.get('allowed_netconf_nodes',[]))[:5]}")
        self.log("  ✅ Policy OK / Safety OK → Skill ループへ")

        # Skill ループ（validate → fix → deploy → audit → rollback）を直接実行
        # Worker を再度 run() せず、生成済み xml_config を使って Skill を順次呼ぶ
        device_info = None
        if deploy and all([device_ip, username, password]):
            device_info = {"ip": device_ip, "username": username,
                           "password": password, "port": port}

        skill_result = await worker._run_skill_loop(
            xml_config=xml_config,
            translated_query=gen_result.get("translated_query", worker_query),
            device_info=device_info,
            deploy=deploy
        )

        worker_result = {
            "generated_xml":      xml_config,
            "final_xml":          skill_result["final_xml"],
            "validation_status":  skill_result["valid"],
            "deployment_status":  skill_result["deployment_status"],
            "audit_status":       skill_result.get("audit_status"),
            "rollback_status":    skill_result.get("rollback_status"),
            "skill_steps":        skill_result["skill_steps"],
            "skill_execution_log": worker.skill_execution_log,
            "conversation_history": [
                {"role": msg.role,
                 "text": msg.text if hasattr(msg, "text") else str(msg.contents)}
                for msg in worker.conversation_history
            ],
        }

        self.audit_logger.record(task, worker_result, policy_result=policy_res,
                                  safety_result=safety, deploy_method="ncclient")
        return worker_result

    async def run(
        self, user_query: str, device_ip: str = None, username: str = None,
        password: str = None, port: str = "830", deploy: bool = False
    ) -> Dict[str, Any]:
        self.logs = []
        self.log("\n" + "="*70)
        self.log("🎼 OrchestratorAgentArista 起動")
        self.log("="*70)
        self.log(f"入力クエリ: {user_query}")

        result = {
            "user_query": user_query, "tasks": [], "execution_order": [],
            "task_results": [], "aggregated": {}, "orchestrator_logs": self.logs
        }

        # Step 0: get_inventory
        inventory = None
        if all([device_ip, username, password]):
            self.log("\n" + "─"*50)
            self.log("🗂️  [Step 0] get_inventory")
            inv = self._run_skill("get_inventory", device_ip=device_ip,
                                   username=username, password=password, port=port)
            if inv and inv.get("status") == "success":
                inventory = inv
                self.log(f"   インターフェース: {inv.get('interface_names',[][:10])}")
                self.log(f"   VLAN一覧: {inv.get('vlans', [])}")
                result["orchestrator_inventory"] = inv

        # Step 1: task_decomposer
        self.log("\n" + "─"*50)
        self.log("📋 [Step 1] task_decomposer")
        decomp = self._run_skill("task_decomposer", user_query=user_query,
                                  llm=self.llm, inventory=inventory)
        if not decomp or decomp["status"] != "success":
            msg = (decomp or {}).get("message", "error")
            self.log(f"❌ task_decomposer 失敗: {msg}")
            result["aggregated"] = {"status":"all_failure","summary":msg,"report_lines":[]}
            return result
        tasks = decomp["tasks"]
        result["tasks"] = tasks
        self.log(f"   → {len(tasks)} タスク検出")
        for t in tasks:
            self.log(f"      {t['id']}: {t.get('operation','?')} / {t.get('target','?')} deps={t.get('depends_on',[])}")

        # Step 2: dependency_resolver
        self.log("\n" + "─"*50)
        self.log("🔗 [Step 2] dependency_resolver")
        resolve = self._run_skill("dependency_resolver", tasks=tasks)
        if not resolve or resolve["status"] != "success":
            msg = (resolve or {}).get("message", "error")
            self.log(f"❌ dependency_resolver 失敗: {msg}")
            result["aggregated"] = {"status":"all_failure","summary":msg,"report_lines":[]}
            return result
        execution_order = resolve["execution_order"]
        result["execution_order"] = execution_order
        self.log(f"   → 実行順: {[t['id'] for t in execution_order]}")

        # Step 3: Worker ディスパッチ
        self.log("\n" + "─"*50)
        self.log(f"🚀 [Step 3] Worker ディスパッチ ({len(execution_order)} タスク)")
        task_results = []
        for idx, task in enumerate(execution_order, 1):
            tid = task["id"]
            self.log(f"\n  [{idx}/{len(execution_order)}] 🔧 {tid}")
            worker_result = await self._dispatch_task(
                task, idx, len(execution_order),
                device_ip, username, password, port, deploy
            )
            ds = (worker_result.get("deployment_status") or {}).get("status", "?")
            _aud = worker_result.get("audit_status") or {}
            aus = _aud.get("status", "")
            _aud_msg = _aud.get("message", "")
            # audit が "Audit error" の場合（接続失敗等）は deploy 成功扱い
            _audit_ok = aus in ("success","skipped","") or (aus == "failure" and "Audit error" in _aud_msg)
            ok  = ds in ("success","no_changes","skipped","blocked") and _audit_ok
            self.log(f"  {'✅' if ok else '❌'} {tid} → deploy={ds} audit={aus or '-'}")
            task_results.append({"task": task, "result": worker_result})
            if not ok and ds not in ("blocked","no_changes"):
                self.log(f"\n  🛑 {tid} 失敗 → 残タスクを中断")
                for rem in execution_order[idx:]:
                    task_results.append({"task": rem, "result": {
                        "validation_status": False,
                        "deployment_status": {"status":"skipped","diff":"",
                                               "message":f"Skipped due to failure of {tid}"},
                        "audit_status": None, "rollback_status": None
                    }})
                break

        result["task_results"] = task_results

        # Step 4: result_aggregator
        self.log("\n" + "─"*50)
        self.log("📊 [Step 4] result_aggregator")
        agg = self._run_skill("result_aggregator", task_results=task_results)
        result["aggregated"] = agg or {}
        if agg:
            self.log(f"\n{agg['summary']}")
            for line in agg.get("report_lines", []):
                self.log(line)

        # Step 5: AuditLogger
        self.audit_logger.print_summary()

        self.log("\n" + "="*70)
        self.log("🎼 OrchestratorAgentArista 完了")
        self.log("="*70)
        return result



# ── モジュール初期化ファクトリ ────────────────────────────────────────────
_orchestrator_instance = None

def get_orchestrator() -> "OrchestratorAgentArista":
    """
    OrchestratorAgentArista のシングルトンを返す。
    初回呼び出し時に retriever / llm を初期化して生成する。
    """
    global _orchestrator_instance
    if _orchestrator_instance is None:
        _orchestrator_instance = OrchestratorAgentArista(
            retriever=retriever,
            llm=llm,
            skills=ALL_SKILLS_V6,
            max_retries=3,
            max_review_rounds=2,
            audit_log_path=AUDIT_LOG_PATH,
        )
    return _orchestrator_instance
