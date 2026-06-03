#!/usr/bin/env python3
# Copyright (c) 2026 hidemi-k
# Licensed under the MIT License.
"""
Arista ANTA Snapshot Verify A2A Server — 検証系 (port:8004)
===============================================================
公式 ANTA (Arista Network Test Automation) ライブラリを使い、
Hub からの「テスト依頼」を受けて設定変更後の事後検証（Post-Check）を
JSON-RPC で返す A2A サーバ。

【公式 ANTA API への完全移行】
  anta.catalog.AntaCatalog.from_dict()
  anta.runner.main()
  anta.result_manager.ResultManager

役割分担:
  task_decompose_a2a_server.py　 　 (port:8000) … ルーティングハブ
  arista_netconf_rag_a2a_server.py  (port:8001) … 設定変更 (NETCONF edit-config)
  arista_eapi_show_a2a_server.py    (port:8002) … 状態参照 (eAPI show コマンド)
  xdp_a2a_server.py                 (port:8003) … セキュリティ (XDP/eBPF)
  arista_anta_verify_a2a_server.py  (port:8004) … 検証 (ANTA Snapshot) ← 本ファイル

3つの核心的メリット:
  ① 検証の「オンデマンド・マイクロサービス化」
     Hub は「Ethernet1 の物理状態は健全か？」という高レベルな問いを投げるだけ。
  ② LLM への「事実（Grounding）」の提供
     ANTA 公式の Success/Failure という構造化された判定結果を返す。
  ③ 状態の変化に対する「イベント駆動」のトリガー
     XDP が異常検知 → Hub → ANTA で周辺インターフェース全件テスト。

アーキテクチャ:
  A2AStarletteApplication
    └─ DefaultRequestHandler
         └─ AristaAntaVerifyExecutor (AgentExecutor)
               ├─ クエリ→カタログ変換 : _build_catalog_from_categories()
               │    AntaCatalog.from_dict(test_specs)
               ├─ ANTA テスト実行    : await anta_run(manager, inventory, catalog)
               ├─ 結果整形           : ResultManager → results_list
               ├─ スナップショット保存: JSON (before/after)
               └─ before/after 比較 : _compare_snapshots()

起動:
    python arista_anta_verify_a2a_server.py

依存パッケージ:
    pip install anta nest_asyncio uvicorn fastapi a2a-sdk

環境変数:
    A2A_PORT       : ポート番号（デフォルト: 8004）
    A2A_PUBLIC_URL : 外部公開URL（デフォルト: http://localhost:8004）
    EAPI_HOST      : デバイスIP（デフォルト: 172.20.100.31）
    EAPI_PORT  : eAPI ポート番号（デフォルト: 443）
    EAPI_USER      : ユーザー名（デフォルト: admin）
    EAPI_PASS      : パスワード（デフォルト: admin）
    EAPI_INSECURE  : 自己署名証明書を許可 "true"/"false"（デフォルト: true）
    SNAPSHOT_STORE : スナップショット保存パス（デフォルト: ./anta_snapshots）
    LOCALE         : 言語 "ja"/"en"（デフォルト: ja）

リクエスト形式（JSON）:
    {
        "query":       "インターフェースの健全性を確認してください",
        "action":      "snapshot" | "verify" | "compare" | "post_check",
        "snapshot_id": "snap_xxxx",          # compare/post_check 時 (省略可)
        "tests":       ["interface", "system", "routing", "bgp",
                        "connectivity", "mlag", "vlan", "stp", "all"],
        "device_ip":   "172.20.100.31",      # 省略可（環境変数 EAPI_HOST を使用）
        "username":    "admin",              # 省略可
        "password":    "admin",              # 省略可
        "port":        443,                  # 省略可
        "locale":      "ja"                  # 省略可 ("ja" | "en")
    }

action の意味:
    snapshot   : ANTA テストを実行してその結果を JSON で保存（before 用途）
    verify     : ANTA テストを即時実行して Success/Failure を返す（単発確認）
    compare    : snapshot_id の before と現在の after を ANTA で比較（副作用検出）
    post_check : verify + compare を一括実行（NETCONF deploy 後の事後検証）

【ANTA v1.8.0 ハマりポイント - ノートブック実機確認済み】
  - anta.tests.bgp         → anta.tests.routing.bgp  （パスが異なる）
  - anta.tests.routing     → ネスト辞書形式 {"generic":[...], "bgp":[...]}
  - VerifyRoutingTableSize → minimum/maximum 両方必須（片方だけで ValidationError）
  - VerifyBGPPeersHealth   → address_families=[{afi,safi,vrf}] 必須
  - AntaCatalog.parse()    → ファイル読み込み（from_yaml は非存在）
  - AsyncEOSDevice         → .host/.hostname プロパティ非公開
  - asyncio イベントループ → nest_asyncio.apply() 必須（A2A サーバ内部との競合解消）
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import uvicorn

# nest_asyncio: A2A サーバの既存 asyncio ループ内で
# anta_run() (内部で asyncio を使用) を呼べるようにする
import nest_asyncio
nest_asyncio.apply()

# ── 多言語対応 ────────────────────────────────────────────────────────────────
from i18n import get_msg, locale_from_request, LOCALE

# ── ANTA 公式ライブラリ ────────────────────────────────────────────────────────
# ノートブック(anta_ceos_complete.ipynb) Step 3〜6 で確認済みの import
try:
    from anta.catalog import AntaCatalog
    from anta.device import AsyncEOSDevice
    from anta.inventory import AntaInventory
    from anta.runner import main as anta_run
    from anta.result_manager import ResultManager
    ANTA_AVAILABLE    = True
    _anta_import_error = ""
except ImportError as _e:
    ANTA_AVAILABLE    = False
    _anta_import_error = str(_e)
    # ダミー定義（起動エラーを防ぐ）
    AntaCatalog = AntaInventory = ResultManager = None
    AsyncEOSDevice = anta_run = None

# ── A2A SDK ───────────────────────────────────────────────────────────────────
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

# ── FastAPI (REST ヘルス + スナップショット管理) ──────────────────────────────
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("anta_verify_a2a")

# ── 設定 ──────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))

A2A_HOST       = os.getenv("A2A_HOST",       "0.0.0.0")
A2A_PORT       = int(os.getenv("A2A_PORT",   "8004"))
A2A_PUBLIC_URL = os.getenv("A2A_PUBLIC_URL", f"http://localhost:{A2A_PORT}")

DEFAULT_EAPI_HOST     = os.getenv("EAPI_HOST",      "172.20.100.31")
DEFAULT_EAPI_PORT     = int(os.getenv("EAPI_PORT", "443"))
DEFAULT_EAPI_USER     = os.getenv("EAPI_USER",      "admin")
DEFAULT_EAPI_PASS     = os.getenv("EAPI_PASS",      "admin")
# cEOS 自己署名証明書: insecure=True が必要（実機確認済み）
DEFAULT_EAPI_INSECURE = os.getenv("EAPI_INSECURE", "true").lower() == "true"

SNAPSHOT_STORE = os.getenv("SNAPSHOT_STORE",
                             os.path.join(BASE_DIR, "anta_snapshots"))

SCOPE_NOTE = "ANTA v1.8.0 公式テスト (事後検証 / Post-Check)"

# インメモリ スナップショットキャッシュ
_snapshot_cache: Dict[str, Dict] = {}

# ═══════════════════════════════════════════════════════════════════════════════
# Arista ベストプラクティス: Management インターフェース除外
#
# Management0 は cEOS の管理インターフェース（eAPI/NETCONF/SSH 用）。
# カーネルが受け取れないパケット（送信先ポート不一致など）を
# ソフトウェア的に廃棄するため、inDiscards カウンターが常に増加する。
# これは cEOS の正常な動作であり、データプレーンの問題ではない。
#
# Arista ベストプラクティス:
#   「管理インターフェース（Management0/Management1）はデータプレーンの
#    監視対象から除外する」
#   参照: https://anta.arista.com/stable/api/tests.interfaces/
#
# ANTA v1.8.0 では ignored_interfaces パラメータで除外できる。
# None（パラメータなし）ではなく辞書形式で渡す点に注意。
# ═══════════════════════════════════════════════════════════════════════════════

# 監視対象外インターフェース（環境変数で上書き可能）
_IGNORED_INTERFACES: List[str] = [
    iface.strip()
    for iface in os.getenv(
        "ANTA_IGNORED_INTERFACES",
        "Management0,",   # デフォルト: 管理 I/F を除外
    ).split(",")
    if iface.strip()
]


# ═══════════════════════════════════════════════════════════════════════════════
# ANTA テストカタログ構築
# ノートブック Step 3 の test_specs 形式を動的に生成する
# ═══════════════════════════════════════════════════════════════════════════════

def _build_catalog_specs(categories: List[str]) -> Dict[str, Any]:
    """
    カテゴリリストから ANTA test_specs 辞書を構築する。

    【重要: ノートブック実機確認済みの書式ルール】
    1. anta.tests.routing は ネスト辞書形式:
       {"anta.tests.routing": {"generic": [...], "bgp": [...]}}
    2. VerifyRoutingTableSize: minimum と maximum は両方必須
    3. VerifyBGPPeersHealth: address_families=[{afi, safi, vrf}] は必須
    4. パラメータ不要なテストは None を渡す
    5. anta.tests.bgp は存在しない → anta.tests.routing.bgp を使う
    """
    specs: Dict[str, Any] = {}
    routing_specs: Dict[str, List] = {}   # anta.tests.routing 用ネスト辞書

    for cat in categories:

        # ── anta.tests.interfaces ─────────────────────────────────────────────
        if cat == "interface":
            specs["anta.tests.interfaces"] = [
                # ── VerifyInterfaceErrors ────────────────────────────────────
                # CRC / InputError / OutputError がゼロであること。
                # ignored_interfaces: Management0 を除外する。
                #   理由: 管理 I/F は eAPI/NETCONF/SSH トラフィックを処理する際に
                #         カーネルが受け取れないパケットを廃棄するため、
                #         inErrors カウンターが増加することがある（cEOS 正常動作）。
                {
                    "VerifyInterfaceErrors": {
                        "ignored_interfaces": _IGNORED_INTERFACES,
                    }
                },

                # ── VerifyInterfaceErrDisabled ───────────────────────────────
                # err-disabled になっているポートがないこと。
                # Management0 では err-disabled は通常発生しないため
                # ignored_interfaces 不要だが、念のため除外する。
                {
                    "VerifyInterfaceErrDisabled": {
                        "ignored_interfaces": _IGNORED_INTERFACES,
                    }
                },

                # ── VerifyInterfaceDiscards ──────────────────────────────────
                # パケット廃棄カウンター（inDiscards/outDiscards）がゼロであること。
                # ignored_interfaces: Management0 を必ず除外する。
                #   理由: Management0 の inDiscards は eAPI/NETCONF/SSH の
                #         管理トラフィック処理で常にカウントされる（cEOS 正常動作）。
                #         Arista ベストプラクティス: 管理 I/F は監視対象外。
                {
                    "VerifyInterfaceDiscards": {
                        "ignored_interfaces": _IGNORED_INTERFACES,
                    }
                },

                # ── VerifyInterfaceUtilization ───────────────────────────────
                # 帯域使用率が閾値（デフォルト 75%）以内であること。
                # ignored_interfaces: Management0 を除外する。
                #   理由: 管理トラフィックの帯域使用率はデータプレーンと
                #         切り分けて評価すべきであるため。
                {
                    "VerifyInterfaceUtilization": {
                        "ignored_interfaces": _IGNORED_INTERFACES,
                    }
                },
            ]

        # ── anta.tests.system ─────────────────────────────────────────────────
        elif cat == "system":
            specs["anta.tests.system"] = [
                {"VerifyUptime": {"minimum": 60}},
                # 最低 60 秒以上 Uptime があること
                {"VerifyNTP":               None},
                # NTP が同期していること
                {"VerifyCPUUtilization":    None},
                # CPU 使用率が閾値内であること
                {"VerifyMemoryUtilization": None},
                # メモリ使用率が閾値内であること
                {"VerifyReloadCause":       None},
                # 再起動原因が正常であること（予期しないリロードを検出）
            ]

        # ── anta.tests.routing.generic ────────────────────────────────────────
        elif cat == "routing":
            # ネスト辞書形式: {"anta.tests.routing": {"generic": [...]}}
            routing_specs.setdefault("generic", []).extend([
                {
                    "VerifyRoutingTableSize": {
                        "minimum": 0,
                        "maximum": 100000,
                        # ★ minimum/maximum は両方必須（実機確認済み）
                    }
                },
                # VerifyRoutingProtocolModel は model パラメータが必須のため除外
                # (ValidationError を避けるため None では渡せない)
            ])

        # ── anta.tests.routing.bgp ────────────────────────────────────────────
        elif cat == "bgp":
            # ★ anta.tests.bgp は存在しない → routing.bgp を使う（実機確認済み）
            routing_specs.setdefault("bgp", []).append(
                {
                    "VerifyBGPPeersHealth": {
                        "address_families": [
                            {
                                "afi":  "ipv4",
                                "safi": "unicast",
                                "vrf":  "default",
                                # ★ address_families は必須パラメータ（実機確認済み）
                            }
                        ]
                    }
                }
            )

        # ── anta.tests.connectivity ───────────────────────────────────────────
        elif cat == "connectivity":
            # VerifyLLDPNeighbors は neighbors パラメータ（期待ネイバーリスト）が必須
            # → 環境に依存するため None では渡せない → 除外してデータ収集のみ可能なテストへ
            # VerifyReachability も hosts パラメータが必須 → 除外
            # connectivity カテゴリは現状ノートブック確認済みテストが
            # すべて必須パラメータを要求するため、routing の status チェックに含める
            # ※ 将来的には inventory.yaml からネイバーリストを動的に生成する
            pass  # 必須パラメータ不足のためスキップ（ログに記録）

        # ── anta.tests.mlag ───────────────────────────────────────────────────
        elif cat == "mlag":
            specs["anta.tests.mlag"] = [
                {"VerifyMlagStatus":     None},
                # MLAG が active / connected 状態であること
                {"VerifyMlagInterfaces": None},
                # MLAG インターフェースが正常であること
            ]

        # ── anta.tests.vlan ───────────────────────────────────────────────────
        elif cat == "vlan":
            # VerifyVlanInternalPolicy: policy/start_vlan_id/end_vlan_id が必須
            # VerifyVlanStatus: vlans が必須
            # → デバイス固有パラメータが必要なためデフォルトカタログからは除外
            pass

        # ── anta.tests.stp ────────────────────────────────────────────────────
        elif cat == "stp":
            # VerifySTPMode: vlans が必須
            # VerifySTPBlockedPorts: vlans が必須
            # → デバイス固有パラメータが必要なためデフォルトカタログからは除外
            pass

    # routing サブモジュールをネスト辞書形式でセット
    if routing_specs:
        specs["anta.tests.routing"] = routing_specs

    return specs


# ── クエリキーワード → カテゴリ ────────────────────────────────────────────────
_KEYWORD_MAP: Dict[str, List[str]] = {
    # インターフェース
    "インターフェース": ["interface"], "interface":  ["interface"],
    "intf":             ["interface"], "エラー":     ["interface"],
    "error":            ["interface"], "カウンター": ["interface"],
    "counter":          ["interface"], "errdisabled":["interface"],
    "帯域":             ["interface"], "utilization":["interface"],
    # システム
    "システム": ["system"], "system":  ["system"], "cpu":     ["system"],
    "memory":   ["system"], "メモリ":  ["system"], "ntp":     ["system"],
    "uptime":   ["system"], "起動":    ["system"], "version": ["system"],
    "バージョン": ["system"],
    # ルーティング
    "ルート":   ["routing"], "routing": ["routing"],
    "route":    ["routing"], "ルーティング": ["routing"],
    # BGP
    "bgp": ["bgp", "routing"],
    # 疎通
    "lldp":     ["connectivity"], "ネイバー": ["connectivity"],
    "neighbor": ["connectivity"], "疎通":     ["connectivity"],
    # MLAG
    "mlag": ["mlag"], "スタック": ["mlag"],
    # VLAN
    "vlan": ["vlan"],
    # STP
    "stp": ["stp"], "spanning": ["stp"], "スパニング": ["stp"],
    # 一括
    "全体": ["interface", "system", "routing", "bgp", "connectivity"],
    "全件": ["interface", "system", "routing", "bgp", "connectivity"],
    "all":  ["interface", "system", "routing", "bgp",
             "connectivity", "mlag", "vlan", "stp"],
    # post_check / snapshot / 事後検証 のデフォルトセット
    # → BGP を含めて 11 テスト（interface×4 + system×5 + routing×1 + bgp×1）
    "post_check":  ["interface", "system", "routing", "bgp"],
    "post-check":  ["interface", "system", "routing", "bgp"],
    "snapshot":    ["interface", "system", "routing", "bgp"],
    "事後":        ["interface", "system", "routing", "bgp"],
    "スナップ":    ["interface", "system", "routing", "bgp"],
    # 「anta」「verify」「検証」単体キーワードも bgp を含む標準セットへ
    "anta":        ["interface", "system", "routing", "bgp"],
    "verify":      ["interface", "system", "routing", "bgp"],
    "検証":        ["interface", "system", "routing", "bgp"],
}

# デフォルト: キーワードに何もヒットしない場合も BGP を含む標準 4 カテゴリ
_DEFAULT_CATS = ["bgp", "interface", "routing", "system"]
_ALL_CATS     = ["interface", "system", "routing", "bgp",
                 "connectivity", "mlag", "vlan", "stp"]


def _infer_categories(query: str, tests_param: Optional[List[str]]) -> List[str]:
    """クエリテキストまたは明示的な tests パラメータからカテゴリリストを決定する。"""
    if tests_param:
        if "all" in tests_param:
            return list(_ALL_CATS)
        valid = [t for t in tests_param if t in _ALL_CATS]
        return valid if valid else list(_DEFAULT_CATS)

    q    = query.lower()
    cats: set = set()
    for kw, cat_list in _KEYWORD_MAP.items():
        if kw in q:
            cats.update(cat_list)

    return sorted(cats) if cats else list(_DEFAULT_CATS)


# ═══════════════════════════════════════════════════════════════════════════════
# ANTA テスト実行（公式 API）
# ノートブック Step 3〜6 の実装に忠実に対応
# ═══════════════════════════════════════════════════════════════════════════════

async def _run_anta_tests(
    host: str,
    port: int,
    username: str,
    password: str,
    insecure: bool,
    categories: List[str],
    device_name: str = "ceos-01",
) -> Dict:
    """
    公式 ANTA API を使ってテストを実行し、raw 結果辞書を返す。

    対応する ノートブック Step:
      Step 3: AntaCatalog.from_dict(test_specs)
      Step 4: AsyncEOSDevice + AntaInventory.add_device()
      Step 5: await anta_run(manager, inventory, catalog)
      Step 6: ResultManager.results → results_list
    """
    if not ANTA_AVAILABLE:
        return {
            "status":  "error",
            "message": f"ANTA ライブラリがインストールされていません。",
            "detail":  _anta_import_error,
            "hint":    "pip install anta nest_asyncio",
        }

    # ── Step 3: カタログ構築 ─────────────────────────────────────────────────
    test_specs = _build_catalog_specs(categories)
    if not test_specs:
        return {
            "status":  "error",
            "message": f"有効なカテゴリがありません: {categories}",
        }

    try:
        catalog = AntaCatalog.from_dict(test_specs)
    except Exception as e:
        err_msg = str(e)
        logger.error(f"AntaCatalog.from_dict エラー: {err_msg}")
        return {
            "status":  "error",
            "message": f"カタログ構築エラー: {err_msg}",
            "hint": (
                "VerifyRoutingProtocolModel → model パラメータが必須。"
                "VerifyLLDPNeighbors / VerifyReachability → neighbors/hosts が必須。"
                "None では渡せないテストは test_specs から除外してください。"
            ),
            "test_specs_used": str(list(test_specs.keys())),
        }

    n_tests = len(catalog.tests)
    logger.info(
        f"[ANTA] カタログ構築完了: {n_tests} テスト "
        f"categories={categories}"
    )

    # ── Step 4: デバイス & インベントリ ──────────────────────────────────────
    # ノートブック確認済み: AsyncEOSDevice には .host/.hostname プロパティが
    # 公開されていないため、host IP は DEVICE_CONFIG 辞書で別途管理する
    try:
        device = AsyncEOSDevice(
            name=device_name,
            host=host,
            username=username,
            password=password,
            port=port,
            insecure=insecure,   # cEOS 自己署名証明書 → True（実機確認済み）
        )
    except Exception as e:
        logger.error(f"AsyncEOSDevice 作成エラー: {e}")
        return {
            "status":  "error",
            "message": f"デバイス接続設定エラー: {e}",
        }

    inventory = AntaInventory()
    inventory.add_device(device)

    logger.info(f"[ANTA] インベントリ: {len(inventory)} 台 ({device_name} @ {host}:{port})")

    # ── Step 5: テスト実行 ───────────────────────────────────────────────────
    manager = ResultManager()
    try:
        logger.info(
            f"[ANTA] テスト実行開始: {host}:{port} "
            f"({n_tests} テスト, insecure={insecure})"
        )
        await anta_run(
            manager=manager,
            inventory=inventory,
            catalog=catalog,
        )
        logger.info(f"[ANTA] テスト完了: {len(manager.results)} 件の結果")
    except Exception as e:
        logger.error(f"[ANTA] anta_run エラー: {e}", exc_info=True)
        return {
            "status":  "error",
            "message": f"ANTA 実行エラー: {e}",
            "hint": (
                f"接続先: https://{host}:{port}  "
                "eAPI (HTTPS) が有効か確認してください。"
                "自己署名証明書の場合は EAPI_INSECURE=true を設定してください。"
            ),
        }

    # ── Step 6: 結果整形 ──────────────────────────────────────────────────────
    results_list = []
    for r in manager.results:
        status_str = str(r.result)            # "success"/"failure"/"error"/"skipped"
        messages   = list(r.messages) if r.messages else []
        results_list.append({
            "test":     r.test,              # テストクラス名
            "device":   device_name,
            "result":   status_str,
            "messages": messages,
        })

    return {
        "results":      results_list,
        "device_name":  device_name,
        "host":         host,
        "port":         port,
        "categories":   categories,
        "catalog_size": n_tests,
    }


def _build_summary(raw: Dict, locale: str) -> Dict:
    """
    _run_anta_tests() の raw 結果に i18n サマリーを付与して返す。
    get_msg() を使って ja/en を切り替える。
    """
    if raw.get("status") == "error":
        return raw

    results_list = raw.get("results", [])
    passed  = sum(1 for r in results_list if r["result"] == "success")
    failed  = sum(1 for r in results_list if r["result"] == "failure")
    errored = sum(1 for r in results_list if r["result"] == "error")
    skipped = sum(1 for r in results_list if r["result"] == "skipped")
    total   = len(results_list)
    ng      = failed + errored

    if total == 0:
        status  = "error"
        # i18n: "エラーが発生しました" / "An error occurred"
        summary = get_msg("error", locale) + " (テスト結果なし)"
    elif ng == 0 and skipped == 0:
        status  = "success"
        # i18n: "✅ 全 N タスク成功" / "✅ All N task(s) succeeded"
        summary = get_msg("all_success", locale, n=total)
    elif ng == 0 and skipped > 0:
        status  = "success"
        summary = (
            get_msg("all_success", locale, n=passed)
            + f" ({get_msg('skipped', locale)}: {skipped})"
        )
    elif passed == 0:
        status  = "failure"
        # i18n: "❌ 全 N タスク失敗" / "❌ All N task(s) failed"
        summary = get_msg("all_failure", locale, n=total)
    else:
        status  = "partial_failure"
        # i18n: "⚠️ N成功/M失敗" / "⚠️ N succeeded / M failed"
        summary = get_msg("partial_failure", locale, ok=passed, ng=ng)

    return {
        **raw,
        "status":        status,
        "summary":       summary,
        "tests_total":   total,
        "tests_passed":  passed,
        "tests_failed":  ng,
        "tests_skipped": skipped,
        "engine":        "anta-official",
        "scope_note":    SCOPE_NOTE,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# スナップショット管理
# before スナップショット = ANTA テスト結果 JSON をそのまま保存
# ═══════════════════════════════════════════════════════════════════════════════

def _new_snap_id() -> str:
    ts  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    uid = str(uuid.uuid4())[:6]
    return f"snap_{ts}_{uid}"


def _save_snapshot(snap_id: str, data: Dict) -> None:
    _snapshot_cache[snap_id] = data
    try:
        os.makedirs(SNAPSHOT_STORE, exist_ok=True)
        path = os.path.join(SNAPSHOT_STORE, f"{snap_id}.json")
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        logger.info(f"[ANTA] スナップショット保存: {path}")
    except Exception as e:
        logger.warning(f"[ANTA] スナップショットファイル保存失敗（メモリのみ）: {e}")


def _load_snapshot(snap_id: str) -> Optional[Dict]:
    if snap_id in _snapshot_cache:
        return _snapshot_cache[snap_id]
    path = os.path.join(SNAPSHOT_STORE, f"{snap_id}.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fp:
                data = json.load(fp)
            _snapshot_cache[snap_id] = data
            return data
        except Exception as e:
            logger.error(f"[ANTA] スナップショット読み込みエラー: {e}")
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# before / after 比較
# ANTA の results_list（test 名 + result 文字列）を突き合わせる
# ═══════════════════════════════════════════════════════════════════════════════

def _compare_snapshots(before: Dict, after: Dict, locale: str) -> Dict:
    """
    before / after それぞれの ANTA results_list を比較し、
    副作用（設定変更後に新たに failure になったテスト）を検出する。

    分類:
      new_failures  : before=success → after=failure/error  ← 副作用
      resolved      : before=failure → after=success        ← 改善
      still_failing : before=failure → after=failure        ← 既存不良（今回の変更無関係）
      new_tests     : before に存在しない after の failure   ← 新カテゴリ由来
    """
    def _ok(r: str) -> bool:
        return r in ("success", "skipped")

    before_map: Dict[str, str] = {
        r["test"]: r["result"] for r in before.get("results", [])
    }
    after_map: Dict[str, str] = {
        r["test"]: r["result"] for r in after.get("results", [])
    }
    # after の messages を引けるようにする
    after_messages: Dict[str, List[str]] = {
        r["test"]: r.get("messages", []) for r in after.get("results", [])
    }

    new_failures:  List[Dict] = []
    resolved:      List[Dict] = []
    still_failing: List[Dict] = []
    new_tests:     List[str]  = []

    for test in sorted(set(before_map) | set(after_map)):
        b = before_map.get(test)
        a = after_map.get(test)
        msgs = after_messages.get(test, [])

        if b is None:
            # after にのみ存在（新カテゴリのテスト）
            if not _ok(a or ""):
                new_tests.append(test)
            continue

        if a is None:
            continue

        if _ok(b) and not _ok(a):
            new_failures.append({
                "test":          test,
                "before_result": b,
                "after_result":  a,
                "messages":      msgs,
            })
        elif not _ok(b) and _ok(a):
            resolved.append({
                "test":          test,
                "before_result": b,
                "after_result":  a,
            })
        elif not _ok(b) and not _ok(a):
            still_failing.append({
                "test":          test,
                "before_result": b,
                "after_result":  a,
                "messages":      msgs,
            })

    # 人間向けの new_issues リスト
    new_issues: List[str] = []
    for f in new_failures:
        detail = " / ".join(f["messages"]) if f["messages"] else ""
        line   = f"⚠️  {f['test']}: {f['before_result']} → {f['after_result']}"
        if detail:
            line += f"  ({detail})"
        new_issues.append(line)
    for t in new_tests:
        msgs = " / ".join(after_messages.get(t, []))
        new_issues.append(f"🔴 [新規失敗] {t}" + (f"  ({msgs})" if msgs else ""))

    # タイムスタンプ
    b_ts = before.get("timestamp", "")[:19]
    a_ts = after.get("timestamp",  "")[:19]

    n_issues = len(new_failures) + len(new_tests)
    if n_issues == 0:
        summary = (
            f"✅ 副作用なし — 設定変更による意図しない影響は検出されませんでした "
            f"(before:{b_ts} → after:{a_ts})"
        )
    else:
        summary = (
            f"⚠️  {n_issues} 件の副作用を検出 — 設定変更の影響を確認してください "
            f"(before:{b_ts} → after:{a_ts})"
        )

    return {
        "new_failures":   new_failures,
        "resolved":       resolved,
        "still_failing":  still_failing,
        "new_tests":      new_tests,
        "new_issues":     new_issues,
        "summary":        summary,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# A2A AgentExecutor
# ═══════════════════════════════════════════════════════════════════════════════

class AristaAntaVerifyExecutor(AgentExecutor):
    """公式 ANTA ライブラリを使った事後検証 AgentExecutor"""

    # ── リクエストパース ──────────────────────────────────────────────────────
    def _parse_request(self, text: str) -> Dict:
        text = text.strip()
        try:
            p = json.loads(text)
            if isinstance(p, dict):
                return p
        except json.JSONDecodeError:
            pass
        return {"query": text, "action": "verify"}

    # ── メインエントリ ────────────────────────────────────────────────────────
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # v1.1.0: Message を直接 enqueue する
        from a2a.types.a2a_pb2 import Part as _Part, Message as _Message, Role as _Role
        import uuid as _uuid

        def _make_message(text: str) -> "_Message":
            msg = _Message()
            msg.role = _Role.ROLE_AGENT
            msg.message_id = str(_uuid.uuid4())
            if context.task_id:
                msg.task_id = context.task_id
            if context.context_id:
                msg.context_id = context.context_id
            part = _Part(text=text)
            msg.parts.append(part)
            return msg

        async def _send_text(text: str) -> None:
            await event_queue.enqueue_event(_make_message(text))

        raw_text = "".join(
            part.text for part in context.message.parts
            if part.HasField("text")
        )
        if not raw_text.strip():
            await _send_text(json.dumps({
                    "status":  "error",
                    "message": get_msg("ws_empty"),
                }, ensure_ascii=True))
            return

        params = self._parse_request(raw_text)
        locale = locale_from_request(params)

        query   = params.get("query",  raw_text)
        action  = params.get("action", "verify")
        snap_id = params.get("snapshot_id", "")
        tests   = params.get("tests")   # None or list[str]

        host     = params.get("device_ip", DEFAULT_EAPI_HOST) or DEFAULT_EAPI_HOST
        port     = int(params.get("port",  DEFAULT_EAPI_PORT))
        username = params.get("username",  DEFAULT_EAPI_USER) or DEFAULT_EAPI_USER
        password = params.get("password",  DEFAULT_EAPI_PASS) or DEFAULT_EAPI_PASS
        insecure = DEFAULT_EAPI_INSECURE

        categories = _infer_categories(query, tests)
        logger.info(
            f"[ANTA] action={action!r} categories={categories} "
            f"snap_id={snap_id!r} host={host} locale={locale}"
        )

        try:
            result = await self._dispatch(
                action, query, snap_id, categories,
                host, port, username, password, insecure, locale,
            )
        except Exception as e:
            logger.error(f"[ANTA] executor 例外: {e}", exc_info=True)
            result = {
                "status":  "error",
                "message": get_msg("error", locale) + f": {e}",
            }

        await _send_text(json.dumps(result, ensure_ascii=True))

    # ── action ディスパッチ ───────────────────────────────────────────────────
    async def _dispatch(
        self, action: str, query: str, snap_id: str,
        categories: List[str],
        host: str, port: int, username: str, password: str,
        insecure: bool, locale: str,
    ) -> Dict:

        if action == "snapshot":
            return await self._do_snapshot(
                query, categories, host, port, username, password, insecure, locale)

        elif action == "verify":
            return await self._do_verify(
                query, categories, host, port, username, password, insecure, locale)

        elif action == "compare":
            return await self._do_compare(
                snap_id, categories,
                host, port, username, password, insecure, locale)

        elif action == "post_check":
            return await self._do_post_check(
                snap_id, query, categories,
                host, port, username, password, insecure, locale)

        else:
            return {
                "status":  "error",
                "message": (
                    f"未知の action: {action!r}。"
                    "snapshot / verify / compare / post_check を指定してください。"
                ),
            }

    # ── snapshot ─────────────────────────────────────────────────────────────
    async def _do_snapshot(
        self, query: str, categories: List[str],
        host: str, port: int, username: str, password: str,
        insecure: bool, locale: str,
    ) -> Dict:
        """
        ANTA テストを実行してその結果を JSON スナップショットとして保存する。
        設定変更「前」に呼び出す（before スナップショット）。
        返却される snapshot_id を compare / post_check に渡す。
        """
        raw = await _run_anta_tests(
            host, port, username, password, insecure, categories)
        if raw.get("status") == "error":
            return raw

        snap_id   = _new_snap_id()
        timestamp = datetime.now(timezone.utc).isoformat()

        snap_data = {
            **raw,
            "timestamp":   timestamp,
            "action":      "snapshot",
            "snapshot_id": snap_id,
        }
        _save_snapshot(snap_id, snap_data)

        summarized = _build_summary(raw, locale)
        n = len(raw.get("results", []))
        return {
            **summarized,
            "action":      "snapshot",
            "snapshot_id": snap_id,
            "timestamp":   timestamp,
            # スナップショット取得時は結果 summary を上書きして分かりやすくする
            "summary": (
                f"✅ スナップショット取得完了: {n} テスト実行 "
                f"(ID: {snap_id})"
            ),
        }

    # ── verify ───────────────────────────────────────────────────────────────
    async def _do_verify(
        self, query: str, categories: List[str],
        host: str, port: int, username: str, password: str,
        insecure: bool, locale: str,
    ) -> Dict:
        """
        ANTA テストを即時実行して Success/Failure を返す。
        スナップショット比較なし（単発ヘルスチェック）。
        LLM への「事実（Grounding）」提供に使用する。
        """
        raw = await _run_anta_tests(
            host, port, username, password, insecure, categories)
        if raw.get("status") == "error":
            return raw

        result = _build_summary(raw, locale)
        return {
            **result,
            "action":    "verify",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ── compare ──────────────────────────────────────────────────────────────
    async def _do_compare(
        self, snap_id: str, categories: List[str],
        host: str, port: int, username: str, password: str,
        insecure: bool, locale: str,
    ) -> Dict:
        """
        before スナップショット(snap_id)と現在の after を ANTA で実行・比較する。
        新たな failure（副作用）を検出して返す。
        """
        if not snap_id:
            return {
                "status":  "error",
                "message": (
                    "snapshot_id が必要です。"
                    "先に action=snapshot を実行して snapshot_id を取得してください。"
                ),
            }

        before = _load_snapshot(snap_id)
        if before is None:
            return {
                "status":  "error",
                "message": f"スナップショット '{snap_id}' が見つかりません。",
            }

        # after テスト実行（before と同じカテゴリを使う）
        after_cats = before.get("categories", categories)
        raw_after  = await _run_anta_tests(
            host, port, username, password, insecure, after_cats)
        if raw_after.get("status") == "error":
            return raw_after

        after_id  = _new_snap_id()
        timestamp = datetime.now(timezone.utc).isoformat()
        after_data = {
            **raw_after,
            "timestamp":   timestamp,
            "action":      "compare_after",
            "snapshot_id": after_id,
        }
        _save_snapshot(after_id, after_data)

        # 差分比較（ANTA 結果ベース）
        diff   = _compare_snapshots(before, after_data, locale)
        status = (
            "failure" if (diff["new_failures"] or diff["new_tests"])
            else "success"
        )
        after_summarized = _build_summary(raw_after, locale)

        return {
            "action":         "compare",
            "status":         status,
            "summary":        diff["summary"],
            "before_snap_id": snap_id,
            "after_snap_id":  after_id,
            "diff":           diff,
            "new_issues":     diff["new_issues"],
            "after_verify":   after_summarized,
            "engine":         "anta-official",
            "scope_note":     SCOPE_NOTE,
        }

    # ── post_check ───────────────────────────────────────────────────────────
    async def _do_post_check(
        self, snap_id: str, query: str, categories: List[str],
        host: str, port: int, username: str, password: str,
        insecure: bool, locale: str,
    ) -> Dict:
        """
        NETCONF/eAPI 設定変更後の事後検証（CNV フロー）。
        snap_id あり → compare（before/after 副作用検出）
        snap_id なし → verify のみ（単発確認）
        """
        if snap_id:
            result = await self._do_compare(
                snap_id, categories,
                host, port, username, password, insecure, locale,
            )
            return {
                **result,
                "action": "post_check",
                "note": (
                    "post_check: before スナップショットと現在の ANTA テスト結果を比較しました。"
                    " new_issues に設定変更の副作用が列挙されています。"
                ),
            }
        else:
            result = await self._do_verify(
                query, categories,
                host, port, username, password, insecure, locale,
            )
            return {
                **result,
                "action": "post_check",
                "note": (
                    "snapshot_id が未指定のため verify のみ実行しました。"
                    " before/after 比較には事前に action=snapshot を実行してください。"
                ),
            }

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise UnsupportedOperationError(get_msg("cancel_unsupported"))


# ═══════════════════════════════════════════════════════════════════════════════
# FastAPI REST エンドポイント
# ═══════════════════════════════════════════════════════════════════════════════

rest_app = FastAPI(
    title="ANTA Snapshot Verify A2A Server",
    description="Arista 公式 ANTA v1.8.0 を使った事後検証 A2A サーバ (port:8004)",
    version="2.0.0",
)
rest_app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_methods=["*"], allow_headers=["*"],
)


@rest_app.get("/healthz")
async def healthz():
    snap_mem  = len(_snapshot_cache)
    snap_disk = 0
    if os.path.isdir(SNAPSHOT_STORE):
        snap_disk = sum(1 for f in os.listdir(SNAPSHOT_STORE)
                        if f.endswith(".json"))
    return {
        "status":             "ok",
        "service":            "arista-anta-verify-a2a",
        "version":            "2.0.0",
        "port":               A2A_PORT,
        "anta_available":     ANTA_AVAILABLE,
        "anta_library":       "official" if ANTA_AVAILABLE else "unavailable",
        "anta_import_error":  _anta_import_error if not ANTA_AVAILABLE else "",
        "engine":             "anta-official" if ANTA_AVAILABLE else "none",
        "ignored_interfaces": _IGNORED_INTERFACES,
        "snapshots_memory":   snap_mem,
        "snapshots_disk":     snap_disk,
        "snapshot_store":     SNAPSHOT_STORE,
        "eapi_default":       f"https://{DEFAULT_EAPI_HOST}:{DEFAULT_EAPI_PORT}",
        "eapi_insecure":      DEFAULT_EAPI_INSECURE,
        "locale":             LOCALE,
        "timestamp":          datetime.now(timezone.utc).isoformat(),
    }


@rest_app.get("/snapshots")
async def list_snapshots():
    """保存済みスナップショット一覧（メモリ + ディスク）"""
    items: List[Dict] = []

    seen = set()
    for sid, snap in _snapshot_cache.items():
        seen.add(sid)
        items.append({
            "snapshot_id": sid,
            "timestamp":   snap.get("timestamp", ""),
            "host":        snap.get("host", ""),
            "categories":  snap.get("categories", []),
            "tests_total": snap.get("tests_total", len(snap.get("results", []))),
            "status":      snap.get("status", ""),
            "source":      "memory",
        })

    if os.path.isdir(SNAPSHOT_STORE):
        for fname in sorted(os.listdir(SNAPSHOT_STORE), reverse=True):
            if not fname.endswith(".json"):
                continue
            sid = fname[:-5]
            if sid in seen:
                continue
            snap = _load_snapshot(sid)
            if snap:
                items.append({
                    "snapshot_id": sid,
                    "timestamp":   snap.get("timestamp", ""),
                    "host":        snap.get("host", ""),
                    "categories":  snap.get("categories", []),
                    "tests_total": snap.get("tests_total", len(snap.get("results", []))),
                    "status":      snap.get("status", ""),
                    "source":      "disk",
                })

    items.sort(key=lambda x: x["timestamp"], reverse=True)
    return {"snapshots": items, "total": len(items)}


@rest_app.get("/snapshots/{snapshot_id}")
async def get_snapshot(snapshot_id: str):
    """指定スナップショットの詳細"""
    snap = _load_snapshot(snapshot_id)
    if snap is None:
        raise HTTPException(
            status_code=404,
            detail=f"snapshot '{snapshot_id}' が見つかりません",
        )
    return {
        "snapshot_id": snapshot_id,
        "timestamp":   snap.get("timestamp"),
        "host":        snap.get("host"),
        "categories":  snap.get("categories"),
        "tests_total": snap.get("tests_total", len(snap.get("results", []))),
        "status":      snap.get("status"),
        "engine":      snap.get("engine"),
        "results":     snap.get("results", []),
    }


# ── Agent Card ─────────────────────────────────────────────────────────────────
def build_agent_card() -> AgentCard:
    lib_status = "✅ 利用可能" if ANTA_AVAILABLE else "❌ 未インストール (pip install anta)"
    from a2a.types.a2a_pb2 import AgentInterface
    iface = AgentInterface()
    iface.url = A2A_PUBLIC_URL
    iface.protocol_version = "1.0"
    return AgentCard(
        name="Arista ANTA Snapshot Verify Agent",
        description=(
            "公式 ANTA (Arista Network Test Automation) v1.8.0 を使った\n"
            "事後検証（Post-Check）A2A サーバ。\n\n"
            f"ANTA ライブラリ: {lib_status}\n"
            "エンジン: anta-official\n"
            "  (anta.catalog.AntaCatalog / anta.runner.main / anta.result_manager)\n\n"
            "action:\n"
            "  snapshot   : ANTA テストを実行して JSON として保存（before 用途）\n"
            "  verify     : ANTA テスト即時実行（単発ヘルスチェック / LLM Grounding）\n"
            "  compare    : before/after 比較（副作用・新規 failure 検出）\n"
            "  post_check : verify + compare 一括（CNV フロー）\n\n"
            "カテゴリ: interface/system/routing/bgp/connectivity/mlag/vlan/stp/all\n\n"
            "ANTA v1.8.0 注意点（ノートブック実機確認済み）:\n"
            "  routing は ネスト辞書形式 / VerifyRoutingTableSize は min+max 両方必須\n"
            "  VerifyBGPPeersHealth は address_families 必須 / AntaCatalog.parse() 使用"
        ),
        supported_interfaces=[iface],
        version="2.0.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(),
        skills=[
            AgentSkill(
                id="anta_snapshot",
                name="スナップショット取得 Before",
                description=(
                    "設定変更「前」の状態を ANTA で取得して保存する。\n"
                    "返却の snapshot_id を compare / post_check に渡す。"
                ),
                tags=["anta", "snapshot", "before", "pre-check"],
                examples=[
                    '{"action":"snapshot","query":"設定変更前のスナップショット"}',
                    '{"action":"snapshot","tests":["interface","system","routing"]}',
                    '{"action":"snapshot","device_ip":"172.20.100.31",'
                    '"username":"admin","password":"admin"}',
                ],
            ),
            AgentSkill(
                id="anta_verify",
                name="ANTA テスト即時実行 Verify",
                description=(
                    "ANTA 公式テストを即時実行して Success/Failure を返す。\n"
                    "LLM への Grounding（動かしようのない事実）として使用する。"
                ),
                tags=["anta", "verify", "health", "grounding", "liveness"],
                examples=[
                    '{"action":"verify","query":"インターフェースの健全性を確認して"}',
                    '{"action":"verify","tests":["interface","system"]}',
                    '{"action":"verify","tests":["all"]}',
                ],
            ),
            AgentSkill(
                id="anta_post_check",
                name="事後検証 Post-Check (CNV)",
                description=(
                    "NETCONF/eAPI 設定変更後に before と after を比較して\n"
                    "副作用（新規 failure）を自動検出する。\n"
                    "Arista CNV (Continuous Network Verification) の実装形。"
                ),
                tags=["anta", "post-check", "cnv", "side-effect"],
                examples=[
                    '{"action":"post_check","snapshot_id":"snap_20260510_153000_abc123"}',
                    '{"action":"post_check","query":"設定変更後の事後検証",'
                    '"snapshot_id":"snap_20260510_153000_abc123"}',
                ],
            ),
            AgentSkill(
                id="anta_compare",
                name="スナップショット比較 Compare",
                description=(
                    "before スナップショットと現在の after を ANTA で比較する。\n"
                    "new_failures / resolved / still_failing の3分類で返す。"
                ),
                tags=["anta", "compare", "diff", "before-after"],
                examples=[
                    '{"action":"compare","snapshot_id":"snap_20260510_153000_abc123"}',
                    '{"action":"compare","snapshot_id":"snap_xxx",'
                    '"tests":["interface","routing"]}',
                ],
            ),
        ],
    )


# ── サーバ起動 ─────────────────────────────────────────────────────────────────
def main():
    os.makedirs(SNAPSHOT_STORE, exist_ok=True)

    if not ANTA_AVAILABLE:
        logger.error("=" * 64)
        logger.error("⚠️  ANTA ライブラリが見つかりません")
        logger.error(f"   エラー: {_anta_import_error}")
        logger.error("   インストール: pip install anta nest_asyncio")
        logger.error("   サーバは起動しますが全リクエストがエラーを返します")
        logger.error("=" * 64)
    else:
        logger.info("✅ ANTA ライブラリ確認済み (公式 API 使用)")

    agent_card      = build_agent_card()
    executor        = AristaAntaVerifyExecutor()
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,           # v1.1.0: agent_card が必須引数に変更
    )
    # v1.1.0: A2AStarletteApplication 廃止 → rest_app に A2A ルートを追加
    add_a2a_routes_to_fastapi(
        rest_app,
        agent_card_routes=create_agent_card_routes(agent_card),
        jsonrpc_routes=create_jsonrpc_routes(request_handler, rpc_url="/"),
        rest_routes=create_rest_routes(request_handler),
    )

    logger.info("=" * 64)
    logger.info("Arista ANTA Snapshot Verify A2A Server v2.0.0 起動")
    logger.info("  ★ 公式 ANTA ライブラリ使用 (anta.runner.main / AntaCatalog)")
    logger.info("=" * 64)
    logger.info(f"  Agent Card      : {A2A_PUBLIC_URL}/.well-known/agent.json")
    logger.info(f"  A2A endpoint    : {A2A_PUBLIC_URL}/  (port:{A2A_PORT})")
    logger.info(f"  REST /healthz   : http://{A2A_HOST}:{A2A_PORT}/healthz")
    logger.info(f"  REST /snapshots : http://{A2A_HOST}:{A2A_PORT}/snapshots")
    logger.info(f"  ANTA available  : {ANTA_AVAILABLE}")
    logger.info(f"  Engine          : anta-official")
    logger.info(f"  Snapshot store  : {SNAPSHOT_STORE}")
    logger.info(f"  eAPI default    : https://{DEFAULT_EAPI_HOST}:{DEFAULT_EAPI_PORT}")
    logger.info(f"  eAPI insecure   : {DEFAULT_EAPI_INSECURE}  (cEOS 自己署名証明書対応)")
    logger.info(f"  Locale          : {LOCALE}")
    logger.info(f"  ignored_intfs   : {_IGNORED_INTERFACES}  (管理I/F除外 ベストプラクティス)")
    logger.info("  actions         : snapshot / verify / compare / post_check")
    logger.info("  categories      : interface / system / routing / bgp /")
    logger.info("                    connectivity / mlag / vlan / stp / all")
    logger.info("  nest_asyncio    : applied (A2A + anta_run 競合解消)")
    logger.info("=" * 64)

    # v1.1.0: A2A ルートは rest_app に追加済みのため直接起動
    uvicorn.run(rest_app, host=A2A_HOST, port=A2A_PORT)


if __name__ == "__main__":
    main()
