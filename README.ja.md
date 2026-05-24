🇯🇵 日本語 | 📘 [English](README.md)

## 🔥 このプロジェクトが特別な理由（Why it matters）

- **NETCONF / eAPI / eAPI Config / ANTA / XDP を A2A で統合し、Arista cEOS を自然言語 1 文で安全に操作できる OSS**
- **Junos の commit check 相当の diff 機能を cEOS で再現**——Dry-run → diff → 承認 → NETCONF / eAPI Config デプロイ → ANTA Post-Check の完全自動化
- **スナップショット差分 RAG によるマルチエージェント障害診断**——正常時との差分を根拠に 5 専門エージェントが連携して診断レポートを生成
- A2A プロトコルでエージェントを統合。Groq → Azure OpenAI の自動フォールバックで本番稼働を保証
- XDP/eBPF を AI 制御化（Human-in-the-loop）——既存 C/Go 資産を無改修で AI 統合
- Containerlab（cEOS 4.36.0F）で実機検証済み。

---

## 概要

| | |
|---|---|
| **統合** | NETCONF / eAPI / eAPI Config / ANTA / XDP を A2A で統合し、自然言語 1 文で操作 |
| **安全** | Dry-run → +/- diff → 人間承認 → NETCONF / eAPI Config デプロイ → ANTA 自動 Post-Check |
| **診断** | スナップショット差分 RAG → 5 専門エージェント連携による障害診断（Self-Correction 付き） |
| **実証** | Containerlab（cEOS 4.36.0F）で実機検証済み。XDP セキュリティデモあり |
| **AI** | A2A Hub が LLM で分類 → 専門エージェント（NETCONF / eAPI / eAPI Config / XDP / ANTA / Diagnose）に委譲 |
| **Microsoft** | Azure Container Apps + Azure VM + Azure OpenAI + Microsoft Agent Framework |

---

## デモ動画・スクリーンショット

📹 **動画は近日公開予定です**（公開後は `VIDEO_ID` を差し替えてください）
[![Demo](https://img.youtube.com/vi/VIDEO_ID/0.jpg)](https://www.youtube.com/watch?v=VIDEO_ID)

承認ボタンを押すまで実機には一切触れません。Cancel すれば設定は破棄されます。

<!-- スクリーンショット（準備でき次第、以下を有効化）
![Diff タブ](docs/screenshots/diff_tab.png)
![ANTA Post-Check](docs/screenshots/anta_postcheck.png)
![Security タブ](docs/screenshots/security_tab.png)
-->

---

## ✨ 主な機能

- 🔄 **自然言語 → NETCONF XML → Dry-run → diff → 承認 → デプロイ**（Junos 相当の事前 diff を cEOS で再現）
- 🔍 **eAPI + RAG による高速 show / 状態参照**（自然言語クエリ → 適切な show コマンドを自動選択）
- 🛠️ **eAPI Config（NETCONF 非対応領域の設定変更）** — VXLAN/EVPN・BGP network/redistribute など OpenConfig 非対応領域を eAPI configure session で補完。2 層の安全ガード + Phase1 dry-run / Phase2 commit の 2 段階フロー
- 🔎 **スナップショット差分 RAG 障害診断** — 正常時 eAPI 出力を保存し、障害時に Unified Diff を LLM コンテキストに注入。5 専門エージェント（フロー判断 / L2 / L3 / 整合性チェック / レポート）が連携し Self-Correction で矛盾を自動検出
- ✅ **ANTA 自動 Post-Check**（デプロイ後 ~340ms / 11 tests で副作用ゼロを自動確認）
- 🛡️ **XDP/eBPF の AI 制御**（Human-in-the-loop — AI が提案、人間が承認して XDP ルール適用）
- ⚡ **Groq → Azure OpenAI の自動フォールバック**（全サーバで共通。1 ファイルで LLM を差し替え可能）

---

## アーキテクチャ

```
┌─────────────────────────────────────────────────────┐
│            Azure Container Apps                      │
│  ┌────────────────────────────────────────────────┐  │
│  │  app_a2a.py (NiceGUI Web UI / port:8080)       │  │
│  │  ・自然言語入力 → REST POST /execute           │  │
│  │  ・Dry-run → Diff 確認 → 承認デプロイ          │  │
│  │  ・ANTA Verify / Diagnose / Security タブ      │  │
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
│  │  write  → :8001  / read      → :8002         │    │
│  │  security → :8003 / verify   → :8004         │    │
│  │  eapi_config → :8006（VXLAN/EVPN・BGP CLI） │    │
│  └────┬──────────┬──────────┬──────────┬───┬───┘    │
│       │          │          │          │   │         │
│  ┌────▼──┐  ┌────▼──┐  ┌───▼───┐  ┌───▼─┐ │        │
│  │:8001  │  │:8002  │  │:8003  │  │:8004│ │        │
│  │NETCONF│  │eAPI   │  │XDP    │  │ANTA │ │        │
│  │RAG    │  │Show + │  │Firewall│  │Verify│ │        │
│  │(設定) │  │Diff   │  │(eBPF) │  │(検証)│ │        │
│  └───┬───┘  └───┬───┘  └───┬───┘  └──┬──┘ │        │
│      │          │          │         │    │         │
│  ┌───▼──────────▼──┐  ┌────▼─────────▼──┐ │        │
│  │  Arista cEOS    │  │ Go IPS REST API  │ │        │
│  │  (NETCONF/eAPI) │  │ :8080 (eBPF/XDP) │ │        │
│  └─────────────────┘  └─────────────────┘ │        │
│                                            │        │
│  ┌─────────────┐  ┌──────────────────────┐ │        │
│  │:8005        │  │:8006                 │◀┘        │
│  │Diagnose     │  │eAPI Config           │          │
│  │(5 agents)   │  │(VXLAN/EVPN・BGP CLI)  │          │
│  └──────┬──────┘  └──────────┬───────────┘          │
│         │                    │                      │
│  ┌──────▼────────────────────▼──┐                   │
│  │  Arista cEOS (eAPI / HTTPS)  │                   │
│  └──────────────────────────────┘                   │
└─────────────────────────────────────────────────────┘
```

> **通信経路の補足**
> - Security タブのリアルタイム表示（Top Traffic・Drop List・QoS List）は A2A Hub を経由せず、Web UI から Go IPS（:8080）へ直接 GET します。
> - Diagnose タブは A2A Hub を経由せず、Web UI から Diagnose Agent（:8005）へ直接 A2A リクエストを送ります。
> - チャット入力からのセキュリティ操作・VXLAN/EVPN 設定変更・BGP network/redistribute は Hub → 各エージェントの順に処理されます。

### Azure 構成

| コンポーネント | Azure サービス | 役割 |
|---|---|---|
| Web UI | Azure Container Apps | NiceGUI フロントエンド (port:8088) |
| A2A Hub | Azure VM | LLM ルーター + REST API (port:8000) |
| NETCONF Agent | Azure VM | 設定変更・RAG (port:8001) |
| eAPI Agent | Azure VM | 状態参照 + Diff エンジン (port:8002) |
| XDP Agent | Azure VM | セキュリティ制御 (port:8003) |
| ANTA Agent | Azure VM | 事後検証 (port:8004) |
| Diagnose Agent | Azure VM | 障害診断（5 専門エージェント連携）(port:8005) |
| eAPI Config Agent | Azure VM | VXLAN/EVPN・BGP network/redistribute など NETCONF 非対応領域の設定変更（eAPI configure session）(port:8006) |
| Go IPS | Azure VM | eBPF/XDP REST API (port:8080)。`-iface eth2` で ceos1 の eth2 に XDP/eBPF をアタッチ |
| LLM Primary | Groq | llama-3.3-70b-versatile（高速推論） |
| LLM Fallback | Azure OpenAI | gpt-4.1-mini（プライベートエンドポイント） |
| エージェント基盤 | **Microsoft Agent Framework** | NETCONF Agent の LLM クライアント層 ＋ Diagnose Agent の 5 専門エージェント構築基盤（合計 6 Agent インスタンス） |

---

## A2A Hub のルーティングフロー

```
自然言語クエリ
      │
      ▼
┌─────────────────────────────────────────────────┐
│    classify_query()                              │
│                                                 │
│  ⓪ VXLAN/EVPN × 設定変更（参照動詞なし）？    │
│     → "eapi_config" ──────────────────────────▶ eAPI Config Agent :8006
│                                                 │
│     VXLAN/EVPN × 参照動詞あり？               │
│     → "read"   ───────────────────────────────▶ eAPI Agent :8002
│                                                 │
│     BGP network / redistribute / advertise？    │
│     → "eapi_config" ──────────────────────────▶ eAPI Config Agent :8006
│                                                 │
│  ① VERIFY_KEYWORDS に一致？                    │
│     → "verify"  ──────────────────────────────▶ ANTA Agent :8004
│                                                 │
│  ② SECURITY_REQUIRED に一致？                  │
│     → "security" ─────────────────────────────▶ XDP Agent  :8003
│                                                 │
│  ③ READ_KEYWORDS のみ？                        │
│     → "read"   ────────────────────────────────▶ eAPI Agent :8002
│                                                 │
│  ④ WRITE_KEYWORDS のみ？                       │
│     → "write"  ────────────────────────────────▶ NETCONF Agent :8001
│                                                 │
│  ④ 参照 + 変更の混在（mixed）？               │
│     → 参照のみ実行 ＋ 警告バブル表示          │
│                                                 │
│  ⑤ 判定不能 → LLM フォールバック              │
└─────────────────────────────────────────────────┘

※ Diagnose Agent（:8005）は Hub を経由せず Web UI から直接呼び出します。
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
| `diagnose_a2a_server.py` | Diagnose Agent / 障害診断（port:8005）|
| `arista_eapi_config_a2a_server.py` | eAPI Config Agent / VXLAN/EVPN・BGP network/redistribute など NETCONF 非対応領域の設定変更（port:8006）|
| `snapshot_manager.py` | スナップショット管理（Diagnose Agent 用）|
| `diff_engine.py` | 差分抽出エンジン（Diagnose Agent 用）|
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
git clone https://github.com/hidemi-k/a2a-ceos-core.git
cd a2a-ceos-core
pip install -r requirements.txt
```

> **Note**
> `a2a-sdk` は `0.3.23` に固定しています。`1.0.x` で `a2a.server.apps` が廃止され
> 動作しなくなるためです。`pip install` 時にバージョンを上げないでください。
> `agent-framework` は `1.4.0` に固定しています。`agent-framework-a2a 1.0.0b260514` 以降は
> `a2a-sdk 1.0.x` が必要となりバージョン競合が発生するためです。

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
python task_decompose_a2a_server.py &       # A2A Hub        :8000
python arista_netconf_rag_a2a_server.py &   # NETCONF Agent  :8001
python arista_eapi_show_a2a_server.py &     # eAPI Agent     :8002
python xdp_a2a_server.py &                 # XDP Agent      :8003
python arista_anta_verify_a2a_server.py &   # ANTA Agent     :8004
python diagnose_a2a_server.py &             # Diagnose Agent :8005
python arista_eapi_config_a2a_server.py &   # eAPI Config    :8006

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
│                                      ↑ Go IPS が eth2 に XDP/eBPF をアタッチ（-iface eth2）
│
├── linux1 (Alpine + FRRouting)     172.20.100.3
│     BGP AS 65002 — neighbor 10.0.20.3 (ceos1 AS 65001)
│
└── kali1  (Kali Linux カスタム)    172.20.100.150
      XDP セキュリティデモの攻撃元として使用
```

`clab deploy` 1 コマンドで 3 ノードが起動し、cEOS の eAPI（HTTPS/443）・NETCONF（SSH/830）・gNMI（:6030、Arista 独自デフォルト。IANA 標準は 9339）がすべて利用可能になります。

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

eAPI は Arista 専用 API のためマルチベンダー展開が困難ですが、NETCONF は標準プロトコルのため Juniper/Cisco への拡張が容易です。また YANG スキーマに基づく XML は断片的な CLI コマンドより LLM の生成精度が高く、`edit-config` の冪等性により重複設定を自動スキップできます。将来の Juniper 対応は、`a2a-junos-read`・`a2a-junos-write` のように必要な機能だけをマイクロサービスとして追加する方針で進めます。A2A Hub のルーティングロジックを変えるだけで、新しいエージェントを既存フローに組み込めます。

---

## 技術スタック

| カテゴリ | 技術 |
|---------|------|
| A2A Protocol | google/a2a-sdk (Python) |
| エージェント基盤 | **Microsoft Agent Framework**（`agent_framework_openai`） |
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
| OSS 構成 | Azure インフラおよび LLM API 以外はすべて OSS・無償ツールで構成 |

---

## 🧠 技術的な詳細

<details>
<summary>クリックして展開</summary>

### eAPI 3段階ハイブリッド・パース方式

頻出コマンドは構造化パーサーで即時整形し、未対応コマンドは段階的にフォールバックします。

```
① structured パース（show vlan / show ip bgp summary 等）
       ↓ None の場合
② encoding="text" で CLI テキストを再取得 → LLM 整形（12000字）
       ↓ text 非対応コマンドの場合
③ JSON LLM パース（8000字制限・最終手段）
```

`show ip bgp neighbors` のように JSON が大きいコマンド（~50KB）は ② の text フォーマットで取得することで制限を回避します。サーバログの `parse_method` フィールド（`"structured"` / `"text+llm"` / `"json+llm(fallback)"`）でパスの追跡が可能です。

### Arista cEOS の BGP neighbor 削除制約（実機確認済み）

`nc:operation="delete"` on `<neighbor>` は cEOS では動作しません（`data does not exist` エラー）。正しい削除方法は `<neighbors>` レベルで `nc:operation="replace"` を使い、**残したい neighbor を全列挙**する方式です。

```xml
<!-- ❌ 動作しない -->
<neighbor nc:operation="delete">
  <neighbor-address>10.0.20.153</neighbor-address>
</neighbor>

<!-- ✅ 正しい方法 -->
<neighbors nc:operation="replace">
  <neighbor><neighbor-address>10.0.20.150</neighbor-address>...</neighbor>
  <!-- 削除したい neighbor はここに含めない -->
</neighbors>
```

本システムでは NETCONF Agent に BGP 削除専用ロジックを実装し、現在の neighbor 一覧を自動取得して replace XML を生成します。

### AI 変更要約（diff の自然言語翻訳）

EOS が計算した +/- diff を LLM が自然言語に翻訳します。diff の計算は EOS がすでに行っており、LLM は「翻訳」のみを担います。入力サイズは数行〜数十行と小さく、コンテキスト超過の心配がありません。

操作タイプに応じて 3 パターンで処理します。

- **① VLAN / Interface**（session diff あり）: EOS の +/- diff → LLM が日本語に翻訳
- **② BGP 削除**（`nc:operation="replace"` 方式）: 専用ロジックが確定情報から直接生成（LLM 不使用）  
  ※ XML を LLM に渡すと replace の構造を「追加」と誤解するため
- **③ BGP 追加 / その他**（session diff スキップ）: 生成 XML → LLM が意図を読み取る

AI 変更要約は UI の Diff タブに表示され、+/- diff が取れない操作でも承認前に変更内容を確認できます。

### ハイブリッド・トランザクション方式（diff 再現の仕組み）

```
① NETCONF Agent : RAG で XML 生成
② eAPI Agent   : _cmds_from_xml() で XML → EOS CLI コマンド列に変換
                  → configure session に CLI コマンドを投入
                  → show session-config diffs  ← +/- diff を取得（EOS が計算）
                  → abort（session 破棄、実機には傷なし）
  ※ cEOS の configure session は CLI コマンドしか受け付けないため、
     XML → CLI 変換をアプリ側で実装。VLAN・Interface は変換対応済み、
     BGP は session diff をスキップし AI 変更要約で補完。
③ Hub           : UI へ「生成 XML」＋「+/- diff」＋「AI 変更要約」を返す
④ オペレーター  : Diff タブで "+ hostname new-sw1" 形式と AI 要約を確認
⑤ 承認 → NETCONF: 検証済み XML を edit-config で投入
⑥ 反映確認      : get_config audit
⑦ ANTA Post-Check  : 副作用ゼロを自動検証（Before Snapshot と比較）
```

### RAG 知識ソース

| RAG | 知識ソース | 用途 |
|-----|-----------|------|
| NETCONF 用 | OpenConfig YANG + Arista YANG + gNMI capabilities（128 + 146 モデル）+ 実機検証済み XML テンプレート | XML 設定テンプレートの生成 |
| eAPI 用 | `eapi_documentation.json`（2,051 コマンド） | show コマンドの選択・フィールド解釈 |

NETCONF 用 RAG には YANG モデルだけでなく、**実機検証で確認した動作確認済み XML テンプレート**を第4の知識ソースとして追加しています。「YANG モデル上は正しいが実機では動かない」ケース（BGP 削除の replace 方式など）を LLM が回避できます。

### Microsoft Agent Framework

NETCONF Agent と Diagnose Agent の 2 サーバで `agent_framework_openai.OpenAIChatCompletionClient` を採用。Groq と Azure OpenAI を同一インタフェースで抽象化し、障害時に自動フォールバックします。合計 6 つの Agent インスタンスを Microsoft Agent Framework で構築しています（NETCONF Agent: 1、Diagnose Agent: 5）。

```python
from agent_framework_openai import OpenAIChatCompletionClient

client = OpenAIChatCompletionClient(
    model   = GROQ_MODEL,
    api_key = _GROQ_KEY,
    base_url= GROQ_BASE_URL,
)
```

### eAPI configure session 上限問題への対処（eAPI Config Agent）

dry-run 後にセッションが残存すると `configure session` の上限に達します。Phase1 では `show session-config diffs` 取得直後に `abort` でセッションを即時破棄します。Phase2（commit）ではタイムスタンプ付きの新規セッション名（`eapi_config_YYYYMMDD_HHMMSS`）を生成し、Phase1 のセッション名を再利用しません。

```
Phase1（dry-run）: configure session → show diffs → abort  ← セッション即時破棄
Phase2（commit）: 新規セッション名で configure session → commit
```

### スナップショット差分 RAG（Diagnose Agent）

正常時の eAPI 出力を JSON ファイルとして保存（`snapshot_manager.py`）し、障害時に `difflib` で Unified Diff を生成して LLM コンテキストに注入（`diff_engine.py`）します。ベクトル DB なし・追加ライブラリなし（Python 標準ライブラリのみ）で実装しています。

5 専門エージェント（フロー判断 / L2 分析 / L3 分析 / 整合性チェック / 診断レポート）が連携し、整合性チェックエージェントが矛盾を検出すると L3 エージェントが自動的に再分析します（Self-Correction）。

</details>

---

## 参考リンク

**エージェント・プロトコル**
- [A2A Protocol (Google)](https://github.com/google/A2A)
- [Microsoft Agent Framework](https://github.com/microsoft/agent-framework)

**検証環境**
- [Containerlab — cEOS](https://containerlab.dev/manual/kinds/ceos/)
- [gnmic — gNMI CLI Client](https://gnmic.openconfig.net/)

**ネットワーク自動化**
- [Arista eAPI Python Library (pyeapi)](https://github.com/arista-eosplus/pyeapi)
- [ANTA — Arista Network Test Automation](https://anta.arista.com/)
- [ncclient — NETCONF Python Client](https://ncclient.readthedocs.io/)

**RAG 知識ソース（YANG モデル）**
- [aristanetworks/yang — Arista YANG Models](https://github.com/aristanetworks/yang)
- [openconfig/public — OpenConfig YANG Models](https://github.com/openconfig/public)

**セキュリティ**
- [XDP IPS (ips-maf)](https://github.com/hidemi-k/maf-ebpf-sase/tree/main/ips-maf)

**RAG 埋め込みモデル**
- [BAAI/bge-large-en-v1.5](https://huggingface.co/BAAI/bge-large-en-v1.5)
