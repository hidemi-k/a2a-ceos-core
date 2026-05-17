# Arista cEOS Sentinel

🇯🇵 日本語 | 📘 [English](README.md)

> **Arista cEOS を自然言語で安全に操作できる、A2A ネットワーク自動化プラットフォーム。**
> NETCONF・eAPI・ANTA・XDP を統合し、Dry-run → diff → 承認 → ANTA Post-Check まで一気通貫で実行できます。

**Microsoft Agent Hackathon powered by Tokyo Electron Device** 参加作品
Azure Container Apps + Azure VM + Azure OpenAI + **Microsoft Agent Framework v1.4.0** 構成。

---

## 🔥 このプロジェクトが特別な理由（Why it matters）

- **NETCONF / eAPI / ANTA / XDP を A2A で統合し、Arista cEOS を自然言語 1 文で安全に操作できる世界初の OSS**
- **Junos の commit check 相当の diff 機能を cEOS で再現**——Dry-run → diff → 承認 → NETCONF デプロイ → ANTA Post-Check の完全自動化
- A2A プロトコルで 4 エージェントを統合。Groq → Azure OpenAI の自動フォールバックで本番稼働を保証
- XDP/eBPF を AI 制御化（Human-in-the-loop）——既存 C/Go 資産を無改修で AI 統合
- Containerlab（cEOS 4.36.0F）で実機検証済み。XDP セキュリティデモも動作確認済み

---

## 概要（30秒で分かる）

| | |
|---|---|
| **統合** | NETCONF / eAPI / ANTA / XDP を A2A で統合し、自然言語 1 文で操作 |
| **安全** | Dry-run → +/- diff → 人間承認 → NETCONF デプロイ → ANTA 自動 Post-Check |
| **実証** | Containerlab（cEOS 4.36.0F）で実機検証済み。XDP セキュリティデモあり |
| **AI** | A2A Hub が LLM で分類 → 専門エージェント（NETCONF / eAPI / XDP / ANTA）に委譲 |
| **Microsoft** | Azure Container Apps + Azure VM + Azure OpenAI + Microsoft Agent Framework v1.4.0 |

---

## デモ動画・スクリーンショット

📹 **動画は近日公開予定です**（公開後は `VIDEO_ID` を差し替えてください）
[![Demo](https://img.youtube.com/vi/VIDEO_ID/0.jpg)](https://www.youtube.com/watch?v=VIDEO_ID)

UI スクリーンショットは現在準備中です。以下は Diff タブの実際の出力例です。

```diff
+ hostname new-sw1
+ interface Ethernet1
+   description Uplink to core
+   no shutdown
```

承認ボタンを押すまで実機には一切触れません。Cancel すれば設定は破棄されます。

<!-- スクリーンショット（準備でき次第、以下を有効化）
![Diff タブ](docs/screenshots/diff_tab.png)
![ANTA Post-Check](docs/screenshots/anta_postcheck.png)
![Security タブ](docs/screenshots/security_tab.png)
-->

---

## ✨ 主な機能（5行で理解）

- 🔄 **自然言語 → NETCONF XML → Dry-run → diff → 承認 → デプロイ**（Junos 相当の事前 diff を cEOS で再現）
- 🔍 **eAPI + RAG による高速 show / 状態参照**（自然言語クエリ → 適切な show コマンドを自動選択）
- ✅ **ANTA 自動 Post-Check**（デプロイ後 ~340ms / 11 tests で副作用ゼロを自動確認）
- 🛡️ **XDP/eBPF の AI 制御**（Human-in-the-loop — AI が提案、人間が承認して XDP ルール適用）
- ⚡ **Groq → Azure OpenAI の自動フォールバック**（全 5 サーバで共通。1 ファイルで LLM を差し替え可能）

---

## 解決する業務課題

ネットワーク運用現場の「プロトコル地獄」——設定変更は NETCONF、状態確認は eAPI、テスト自動化は ANTA、セキュリティ制御は XDP/eBPF——それぞれ優れた技術でありながら、一貫したワークフローで扱える統合基盤はありませんでした。

| 操作 | 従来の工数 | 本システム | 効果 |
|------|-----------|-----------|------|
| 設定変更 | 20〜30分（XML 手書き） | **約 2 分（自然言語 → Dry-run → 承認）** | **最大 15 倍 速い** |
| 状態確認 | 5 分（eAPI show を手打ち） | **数秒（自然言語 → eAPI + RAG）** | **50 倍 速い** |
| 自動テスト | 5 分（ANTA 手動起動） | **0.3 秒（ANTA 自動 Post-Check）**（実機計測） | **100 倍 速い** |
| セキュリティ対応 | 5〜10 分（XDP を直接操作） | **AI 分析 → 1 クリック承認** | **即時対応** |

---

## アーキテクチャ

```
┌─────────────────────────────────────────────────────┐
│            Azure Container Apps                      │
│  ┌────────────────────────────────────────────────┐  │
│  │  app_a2a.py (NiceGUI Web UI / port:8088)       │  │
│  │  ・自然言語入力 → REST POST /execute           │  │
│  │  ・Dry-run → Diff 確認 → 承認デプロイ          │  │
│  │  ・ANTA Verify タブ / Security タブ            │  │
│  │  ・多言語対応（日本語 / 英語）                  │  │
│  └────────────────────┬───────────────────────────┘  │
└───────────────────────│─────────────────────────────┘
                        │ HTTP
┌───────────────────────▼─────────────────────────────┐
│                   Azure VM                           │
│                                                      │
│  ┌─────────────────────────────────────────────┐    │
│  │  task_decompose_a2a_server.py  :8000         │    │
│  │  A2A Hub / LLM ルーター                      │    │
│  │  write  → :8001  / read    → :8002           │    │
│  │  security → :8003 / verify → :8004           │    │
│  └────┬──────────┬──────────┬──────────┬───────┘    │
│       │          │          │          │             │
│  ┌────▼──┐  ┌────▼──┐  ┌───▼───┐  ┌───▼───┐        │
│  │:8001  │  │:8002  │  │:8003  │  │:8004  │        │
│  │NETCONF│  │eAPI   │  │XDP    │  │ANTA   │        │
│  │RAG    │  │Show + │  │Firewall│  │Verify │        │
│  │(設定) │  │Diff   │  │(eBPF) │  │(検証) │        │
│  └───┬───┘  └───┬───┘  └───┬───┘  └───┬───┘        │
│      │          │          │          │             │
│  ┌───▼──────────▼──┐  ┌────▼──────────▼──────┐      │
│  │  Arista cEOS    │  │ Go IPS REST API :8080 │      │
│  │  (NETCONF/eAPI) │  │ (ips-maf eBPF/XDP)   │      │
│  └─────────────────┘  └──────────────────────┘      │
└─────────────────────────────────────────────────────┘
```

### Azure 構成

| コンポーネント | Azure サービス | 役割 |
|---|---|---|
| Web UI | Azure Container Apps | NiceGUI フロントエンド (port:8088) |
| A2A Hub | Azure VM | LLM ルーター + REST API (port:8000) |
| NETCONF Agent | Azure VM | 設定変更・RAG (port:8001) |
| eAPI Agent | Azure VM | 状態参照 + Diff エンジン (port:8002) |
| XDP Agent | Azure VM | セキュリティ制御 (port:8003) |
| ANTA Agent | Azure VM | 事後検証 (port:8004) |
| Go IPS | Azure VM | eBPF/XDP REST API (port:8080) |
| LLM Primary | Groq | llama-3.3-70b-versatile（高速推論） |
| LLM Fallback | Azure OpenAI | gpt-4.1-mini（プライベートエンドポイント） |
| エージェント基盤 | **Microsoft Agent Framework v1.4.0** | NETCONF Agent の LLM クライアント層 |

---

## A2A Hub のルーティングフロー

```
自然言語クエリ
      │
      ▼
┌─────────────────────────────────────┐
│    classify_query()                  │
│                                     │
│  ① VERIFY_KEYWORDS に一致？        │
│     → "verify"  ──────────────────▶ ANTA Agent :8004
│                                     │
│  ② SECURITY_REQUIRED に一致？      │
│     → "security" ─────────────────▶ XDP Agent  :8003
│                                     │
│  ③ READ_KEYWORDS のみ？            │
│     → "read"   ────────────────────▶ eAPI Agent :8002
│                                     │
│  ④ WRITE_KEYWORDS のみ？           │
│     → "write"  ────────────────────▶ NETCONF Agent :8001
│                                     │
│  ⑤ 判定不能 → LLM フォールバック  │
└─────────────────────────────────────┘
```

---

## ファイル構成

| ファイル | 役割 |
|----------|------|
| `app_a2a.py` | NiceGUI Web UI（フロントエンド）|
| `task_decompose_a2a_server.py` | A2A Hub / LLM ルーター（port:8000）|
| `arista_netconf_rag_a2a_server.py` | NETCONF Agent / RAG（port:8001）|
| `arista_eapi_show_a2a_server.py` | eAPI Agent / Diff エンジン（port:8002）|
| `xdp_a2a_server.py` | XDP Agent / セキュリティ制御（port:8003）|
| `arista_anta_verify_a2a_server.py` | ANTA Agent / 事後検証（port:8004）|
| `llm_factory.py` | LLM 共通ファクトリ（Groq Primary / Azure OpenAI Fallback）|
| `i18n.py` | 多言語対応（日本語 / 英語）|
| `config.ini.example` | 設定ファイルのサンプル |
| `.env.example` | 環境変数のサンプル |

---

## セットアップ

### 必要要件

- Python 3.11+
- Arista cEOS（Containerlab 推奨）
- Azure VM / Azure Container Apps
- Groq API キー（または Azure OpenAI エンドポイント）

### インストール

```bash
git clone https://github.com/hidemi-k/maf-a2a-ceos.git
cd maf-a2a-ceos
pip install -r requirements.txt
```

### 設定

```bash
# 環境変数
cp .env.example .env
# エディタで .env を編集（API キー、デバイス IP 等を設定）

# 設定ファイル
cp config.ini.example config.ini
# エディタで config.ini を編集
```

### 起動

```bash
# A2A エージェント群を起動（Azure VM 上で実行）
python task_decompose_a2a_server.py &      # A2A Hub       :8000
python arista_netconf_rag_a2a_server.py &  # NETCONF Agent :8001
python arista_eapi_show_a2a_server.py &    # eAPI Agent    :8002
python xdp_a2a_server.py &                # XDP Agent     :8003
python arista_anta_verify_a2a_server.py &  # ANTA Agent    :8004

# Web UI を起動（Azure Container Apps 上で実行）
python app_a2a.py
```

---

## 検証環境：Containerlab ネットワーキングラボ

```
Azure VM (172.20.100.0/24 — clab-mgmt)
│
├── ceos1  (Arista cEOS 4.36.0F)   172.20.100.31
│     ├── eth1 ─── 10.0.20.3/24 ──── linux1:eth1 (10.0.20.150)  FRRouting BGP
│     └── eth2 ─── 10.0.3.3/24  ──── kali1:eth2  (10.0.3.150)   Kali Linux（攻撃元）
│
├── linux1 (Alpine + FRRouting)     172.20.100.3
│     BGP AS 65002 — neighbor 10.0.20.3 (ceos1 AS 65001)
│
└── kali1  (Kali Linux カスタム)    172.20.100.150
      XDP セキュリティデモの攻撃元として使用
```

`clab deploy` 1 コマンドで 3 ノードが起動し、cEOS の eAPI（HTTPS/443）・NETCONF（SSH/830）・gNMI（:6030）がすべて利用可能になります。

---

## なぜ NETCONF/OpenConfig を採用したか

設定変更のインタフェースとして CLI・eAPI・pyeapi・NETCONF・RESTCONF を比較した結果、本プロジェクトでは **NETCONF（OpenConfig）** を採用しています。

| 比較項目 | CLI 文字列 | eAPI JSON | **NETCONF / OpenConfig（採用）** |
|---|---|---|---|
| LLM との親和性 | ❌ 低（非構造化） | ⚠️ 中 | ✅ **高（YANG スキーマが豊富）** |
| 投入前検証 | なし | フィールド名照合 | **スキーマ検証（型・必須・enum）** |
| 冪等性 | △ コマンド依存 | ✅ | **✅ operation 属性で制御** |
| 事前 diff | なし | なし | **✅ configure session で再現** |
| マルチベンダー展開 | ❌ | ❌ Arista 専用 | **✅ Juniper/Cisco 対応が容易** |

eAPI は Arista 専用 API のためマルチベンダー展開が困難ですが、NETCONF は標準プロトコルのため Juniper/Cisco への拡張が容易です。また YANG スキーマに基づく XML は断片的な CLI コマンドより LLM の生成精度が高く、`edit-config` の冪等性により重複設定を自動スキップできます。将来の Juniper/Cisco 対応は RAG のテンプレート層（FAISS インデックス）を差し替えるだけで済みます。

---

## 技術スタック

| カテゴリ | 技術 |
|---------|------|
| A2A Protocol | google/a2a-sdk (Python) |
| エージェント基盤 | **Microsoft Agent Framework v1.4.0**（`agent_framework_openai`） |
| LLM Orchestration | LangChain |
| LLM (Primary) | Groq llama-3.3-70b-versatile |
| LLM (Fallback) | Azure OpenAI gpt-4.1-mini |
| RAG | FAISS + LangChain（BAAI/bge-large-en-v1.5） |
| NETCONF | ncclient + OpenConfig |
| eAPI | pyeapi (HTTPS) |
| Network Testing | ANTA (Arista Network Test Automation) |
| Security | XDP/eBPF + Go IPS REST API |
| Web Framework | FastAPI + Starlette + NiceGUI |
| Container | Azure Container Apps |
| VM | Azure Virtual Machines |
| Lab 環境 | Containerlab + Arista cEOS 4.36.0F |
| 多言語 | i18n.py（日本語 / 英語） |

---

## 🧠 技術的な詳細

<details>
<summary>クリックして展開</summary>

### Arista cEOS の NETCONF `<state>` 問題と対処

Arista cEOS では NETCONF の `<state>` フィルターが常に 0 件を返します（Junos とは異なる実装）。本システムでは NETCONF を設定変更専用・eAPI をオペレーショナルデータ取得専用と役割分担することで解決しています。

### eAPI ハイブリッド・パース方式

`show interfaces` 等の頻出コマンドは構造化パーサーで即時整形し、パーサー未対応のコマンドは LLM が自動整形します。`parse_method`（`"structured"` / `"llm"`）フィールドでパスの追跡が可能です。

### ハイブリッド・トランザクション方式（diff 再現の仕組み）

```
① NETCONF Agent : RAG で XML 生成
② eAPI Agent   : configure session → load XML
                  → show session-config diffs  ← +/- diff を取得
                  → abort（session 破棄、実機には傷なし）
③ Hub           : UI へ「生成 XML」＋「人間が読める diff」を返す
④ オペレーター  : Diff タブで "+ hostname new-sw1" 形式を確認
⑤ 承認 → NETCONF: 検証済み XML を edit-config で投入
```

### RAG 知識ソース

| RAG | 知識ソース | 用途 |
|-----|-----------|------|
| NETCONF 用 | OpenConfig YANG + Arista YANG + gNMI capabilities（128 + 146 モデル） | XML 設定テンプレートの生成 |
| eAPI 用 | `eapi_documentation.json`（2,051 コマンド） | show コマンドの選択・フィールド解釈 |

### Microsoft Agent Framework v1.4.0

NETCONF Agent の LLM クライアントとして `agent_framework_openai.OpenAIChatCompletionClient` を採用。Groq と Azure OpenAI を同一インタフェースで抽象化し、障害時に自動フォールバックします。

```python
from agent_framework_openai import OpenAIChatCompletionClient

client = OpenAIChatCompletionClient(
    model   = GROQ_MODEL,
    api_key = _GROQ_KEY,
    base_url= GROQ_BASE_URL,
)
```

</details>

---

## 今後の展望

**マルチデバイス対応（Juniper / Cisco）**
NETCONF（標準プロトコル）を基盤としているため、ベンダー差分は RAG のテンプレート層（FAISS インデックス）で吸収可能です。本プロジェクトの最優先次期ステップです。

**CI/CD パイプライン化**
Azure DevOps / GitHub Actions と連携し、「Pull Request → 承認 → 自動デプロイ → ANTA 自動 Post-Check」のパイプライン化を目指します。

---

## 参考リンク

**エージェント・プロトコル**
- [A2A Protocol (Google)](https://github.com/google/A2A)
- [Microsoft Agent Framework](https://github.com/microsoft/agent-framework)

**検証環境**
- [Containerlab — cEOS](https://containerlab.dev/manual/kinds/ceos/)
- [gnmic — gNMI CLI Client](https://gnmic.openconfig.net/)

**ネットワーク自動化**
- [ANTA — Arista Network Test Automation](https://anta.arista.com/)
- [Arista eAPI Python Library (pyeapi)](https://github.com/arista-eosplus/pyeapi)
- [ncclient — NETCONF Python Client](https://ncclient.readthedocs.io/)

**RAG 知識ソース（YANG モデル）**
- [openconfig/public — OpenConfig YANG Models](https://github.com/openconfig/public)
- [aristanetworks/yang — Arista YANG Models](https://github.com/aristanetworks/yang)

**セキュリティ**
- [XDP IPS (ips-maf)](https://github.com/hidemi-k/maf-ebpf-sase/tree/main/ips-maf)

**RAG 埋め込みモデル**
- [BAAI/bge-large-en-v1.5](https://huggingface.co/BAAI/bge-large-en-v1.5)

---

## 関連記事

[Zenn ハッカソン作品記事（詳細解説）](https://zenn.dev) <!-- URLを入れる -->
