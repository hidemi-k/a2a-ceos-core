📘 English | 🇯🇵 [日本語はこちら](README.ja.md)

## 🔥 Why it matters

- **OSS that unifies NETCONF / eAPI / ANTA / XDP via A2A and operates Arista cEOS safely with natural language**
- **Junos-equivalent `commit check` diff reproduced on cEOS** — Dry-run → diff → approval → NETCONF deploy → ANTA Post-Check, fully automated
- Agents integrated via A2A protocol. Groq → Azure OpenAI automatic fallback guarantees production reliability
- XDP/eBPF AI-controlled with Human-in-the-loop — existing C/Go assets integrated without modification
- Validated on real hardware via Containerlab (cEOS 4.36.0F). XDP security demo confirmed working

---

## Overview

| | |
|---|---|
| **Unified** | NETCONF / eAPI / ANTA / XDP integrated via A2A — operated with a single natural-language sentence |
| **Safe** | Dry-run → +/- diff → human approval → NETCONF deploy → ANTA auto Post-Check |
| **Proven** | Validated on Containerlab (cEOS 4.36.0F). XDP security demo available |
| **AI** | A2A Hub classifies intent via LLM → delegates to specialist agents (NETCONF / eAPI / XDP / ANTA) |
| **Microsoft** | Azure Container Apps + Azure VM + Azure OpenAI + Microsoft Agent Framework |

---

## Demo & Screenshots

📹 **Demo video coming soon** (replace `VIDEO_ID` once published)
[![Demo](https://img.youtube.com/vi/VIDEO_ID/0.jpg)](https://www.youtube.com/watch?v=VIDEO_ID)

Nothing touches the device until you press **Approve**. Hit **Cancel** and the session is discarded with zero impact.

<!-- Screenshots (uncomment when ready)
![Diff tab](docs/screenshots/diff_tab.png)
![ANTA Post-Check](docs/screenshots/anta_postcheck.png)
![Security tab](docs/screenshots/security_tab.png)
-->

---

## ✨ Features

- 🔄 **Natural language → NETCONF XML → Dry-run → diff → approval → deploy** (Junos-equivalent pre-diff on cEOS)
- 🔍 **eAPI + RAG high-speed show / state query** (natural language → appropriate show command selected automatically)
- ✅ **ANTA auto Post-Check** (~340 ms / 11 tests after deploy — zero side-effects verified automatically)
- 🛡️ **XDP/eBPF AI control** (Human-in-the-loop — AI proposes, human approves before XDP rule is applied)
- ⚡ **Groq → Azure OpenAI automatic fallback** (shared across all 5 servers; swap LLM with a single file change)

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│            Azure Container Apps                      │
│  ┌────────────────────────────────────────────────┐  │
│  │  app_a2a.py (NiceGUI Web UI / port:8088)       │  │
│  │  · Natural language input → REST POST /execute │  │
│  │  · Dry-run → Diff review → Approve & Deploy    │  │
│  │  · ANTA Verify tab / Security tab              │  │
│  │  · i18n support (Japanese / English)           │  │
│  └────────────────────┬───────────────────────────┘  │
└───────────────────────│─────────────────────────────┘
                        │ HTTP
┌───────────────────────▼─────────────────────────────┐
│                   Azure VM                           │
│                                                      │
│  ┌─────────────────────────────────────────────┐    │
│  │  task_decompose_a2a_server.py  :8000         │    │
│  │  A2A Hub / LLM Router                        │    │
│  │  write  → :8001  / read    → :8002           │    │
│  │  security → :8003 / verify → :8004           │    │
│  └────┬──────────┬──────────┬──────────┬───────┘    │
│       │          │          │          │             │
│  ┌────▼──┐  ┌────▼──┐  ┌───▼───┐  ┌───▼───┐        │
│  │:8001  │  │:8002  │  │:8003  │  │:8004  │        │
│  │NETCONF│  │eAPI   │  │XDP    │  │ANTA   │        │
│  │RAG    │  │Show + │  │Firewall│  │Verify │        │
│  │(cfg)  │  │Diff   │  │(eBPF) │  │(test) │        │
│  └───┬───┘  └───┬───┘  └───┬───┘  └───┬───┘        │
│      │          │          │          │             │
│  ┌───▼──────────▼──┐  ┌────▼──────────▼──────┐      │
│  │  Arista cEOS    │  │ Go IPS REST API :8080 │      │
│  │  (NETCONF/eAPI) │  │ (ips-maf eBPF/XDP)   │      │
│  └─────────────────┘  └──────────────────────┘      │
└─────────────────────────────────────────────────────┘
```

### Azure components

| Component | Azure service | Role |
|---|---|---|
| Web UI | Azure Container Apps | NiceGUI frontend (port:8088) |
| A2A Hub | Azure VM | LLM router + REST API (port:8000) |
| NETCONF Agent | Azure VM | Config change + RAG (port:8001) |
| eAPI Agent | Azure VM | State query + Diff engine (port:8002) |
| XDP Agent | Azure VM | Security control (port:8003) |
| ANTA Agent | Azure VM | Post-verification (port:8004) |
| Go IPS | Azure VM | eBPF/XDP REST API (port:8080) |
| LLM Primary | Groq | llama-3.3-70b-versatile (low-latency inference) |
| LLM Fallback | Azure OpenAI | gpt-4.1-mini (private endpoint) |
| Agent framework | **Microsoft Agent Framework** | LLM client layer for NETCONF Agent |

---

## A2A Hub routing flow

```
Natural language query
      │
      ▼
┌─────────────────────────────────────┐
│    classify_query()                  │
│                                     │
│  ① VERIFY_KEYWORDS match?          │
│     → "verify"  ──────────────────▶ ANTA Agent   :8004
│                                     │
│  ② SECURITY_REQUIRED match?        │
│     → "security" ─────────────────▶ XDP Agent    :8003
│                                     │
│  ③ READ_KEYWORDS only?             │
│     → "read"   ────────────────────▶ eAPI Agent  :8002
│                                     │
│  ④ WRITE_KEYWORDS only?            │
│     → "write"  ────────────────────▶ NETCONF Agent :8001
│                                     │
│  ⑤ Ambiguous → LLM fallback       │
└─────────────────────────────────────┘
```

---

## File structure

| File | Role |
|------|------|
| `app_a2a.py` | NiceGUI Web UI (frontend) |
| `task_decompose_a2a_server.py` | A2A Hub / LLM router (port:8000) |
| `arista_netconf_rag_a2a_server.py` | NETCONF Agent / RAG (port:8001) |
| `arista_eapi_show_a2a_server.py` | eAPI Agent / Diff engine (port:8002) |
| `xdp_a2a_server.py` | XDP Agent / Security control (port:8003) |
| `arista_anta_verify_a2a_server.py` | ANTA Agent / Post-verification (port:8004) |
| `llm_factory.py` | Shared LLM factory (Groq Primary / Azure OpenAI Fallback) |
| `i18n.py` | Internationalization (Japanese / English) |
| `config.ini.example` | Configuration file sample |
| `.env.example` | Environment variable sample |

---

## Setup

### Requirements

- Python 3.11+
- Arista cEOS (Containerlab recommended)
- Azure VM / Azure Container Apps
- Groq API key (or Azure OpenAI endpoint)

### Install

```bash
git clone https://github.com/hidemi-k/maf-a2a-ceos.git
cd maf-a2a-ceos
pip install -r requirements.txt
```

> **Note**
> `a2a-sdk` is pinned to `0.3.23`. Version `1.0.x` removed `a2a.server.apps`, which breaks all A2A servers in this project.
> `agent-framework` is pinned to `1.4.0`. From `agent-framework-a2a 1.0.0b260514` onward, `a2a-sdk 1.0.x` is required, causing a version conflict.
> Do not upgrade these packages when running `pip install`.

### Configure

```bash
# Environment variables
cp .env.example .env
# Edit .env — set API keys, device IP, etc.

# Config file
cp config.ini.example config.ini
# Edit config.ini
```

### Start

```bash
# Launch A2A agents (on Azure VM)
python task_decompose_a2a_server.py &      # A2A Hub       :8000
python arista_netconf_rag_a2a_server.py &  # NETCONF Agent :8001
python arista_eapi_show_a2a_server.py &    # eAPI Agent    :8002
python xdp_a2a_server.py &                # XDP Agent     :8003
python arista_anta_verify_a2a_server.py &  # ANTA Agent    :8004

# Launch Web UI (on Azure Container Apps)
python app_a2a.py
```

---

## Lab environment: Containerlab

```
Azure VM (172.20.100.0/24 — clab-mgmt)
│
├── ceos1  (Arista cEOS 4.36.0F)   172.20.100.31
│     ├── eth1 ─── 10.0.20.3/24 ──── linux1:eth1 (10.0.20.150)  FRRouting BGP peer
│     └── eth2 ─── 10.0.3.3/24  ──── kali1:eth2  (10.0.3.150)   Kali Linux (attacker)
│
├── linux1 (Alpine + FRRouting)     172.20.100.3
│     BGP AS 65002 — neighbor 10.0.20.3 (ceos1 AS 65001)
│
└── kali1  (custom Kali Linux)      172.20.100.150
      Used as the attack source for the XDP security demo
```

A single `clab deploy` command brings up all 3 nodes with eAPI (HTTPS/443), NETCONF (SSH/830), and gNMI (:6030 — Arista's default; IANA standard is 9339) fully operational on cEOS.

---

## Why NETCONF/OpenConfig?

After comparing CLI, eAPI, pyeapi, NETCONF, and RESTCONF as the configuration interface, this project adopts **NETCONF (OpenConfig)**.

| Criterion | CLI string | eAPI JSON | **NETCONF / OpenConfig (adopted)** |
|---|---|---|---|
| LLM compatibility | ❌ Low (unstructured) | ⚠️ Medium | ✅ **High (rich YANG schema)** |
| Pre-deploy validation | None | Field-name check | **Schema validation (type · required · enum)** |
| Idempotency | △ Command-dependent | ✅ | **✅ Controlled via `operation` attribute** |
| Pre-diff | None | None | **✅ Reproduced via `configure session`** |
| Multi-vendor expansion | ❌ | ❌ Arista-only | **✅ Easy Juniper/Cisco extension** |

eAPI is an Arista-proprietary API, making multi-vendor deployment difficult. NETCONF is a standard protocol, so extending to Juniper/Cisco requires only swapping the RAG template layer (FAISS index). YANG-schema-based XML also gives LLMs higher generation accuracy than fragmented CLI commands, and `edit-config` idempotency automatically skips duplicate configuration.

---

## Tech stack

| Category | Technology |
|---------|------------|
| A2A Protocol | google/a2a-sdk (Python) |
| Agent framework | **Microsoft Agent Framework** (`agent_framework_openai`) |
| LLM (Primary) | Groq llama-3.3-70b-versatile |
| LLM (Fallback) | Azure OpenAI gpt-4.1-mini |
| RAG | FAISS + LangChain (BAAI/bge-large-en-v1.5) |
| NETCONF | ncclient + OpenConfig |
| eAPI | pyeapi (HTTPS) |
| Network Testing | ANTA (Arista Network Test Automation) |
| Security | XDP/eBPF + Go IPS REST API |
| Web Framework | FastAPI + Starlette + NiceGUI |
| Container | Azure Container Apps |
| VM | Azure Virtual Machines |
| Lab environment | Containerlab + Arista cEOS 4.36.0F |
| i18n | i18n.py (Japanese / English) |
| OSS stack | All components except Azure infrastructure and LLM APIs are OSS or free tools |

---

## 🧠 Technical deep-dive

<details>
<summary>Click to expand</summary>

### eAPI 3-stage hybrid parse strategy

Frequent commands are formatted instantly by a structured parser. Unsupported commands fall back through a 3-stage pipeline.

```
① Structured parse (show vlan / show ip bgp summary / show ip bgp neighbors, etc.)
       ↓ returns None
② Re-fetch with encoding="text" → LLM formatting (12,000 char limit)
       ↓ text format unsupported
③ JSON LLM parse (8,000 char limit — last resort)
```

Commands with large JSON responses such as `show ip bgp neighbors` (~50 KB) are handled via ② to avoid the character limit. The `parse_method` field (`"structured"` / `"text+llm"` / `"json+llm(fallback)"`) in the response enables path tracing for debugging.

### Arista cEOS BGP neighbor delete constraint (verified on real hardware)

`nc:operation="delete"` on `<neighbor>` does **not** work on cEOS — it returns a `data does not exist` error even when the neighbor is visible via gNMI. The correct deletion method is to use `nc:operation="replace"` on `<neighbors>` and **list all neighbors to keep** (omitting the one to delete).

```xml
<!-- ❌ Does not work -->
<neighbor nc:operation="delete">
  <neighbor-address>10.0.20.153</neighbor-address>
</neighbor>

<!-- ✅ Correct method -->
<neighbors nc:operation="replace">
  <neighbor><neighbor-address>10.0.20.150</neighbor-address>...</neighbor>
  <!-- neighbor to delete is simply omitted here -->
</neighbors>
```

The NETCONF Agent implements a dedicated BGP delete flow that automatically fetches the current neighbor list and generates the replace XML — LLM and RAG are bypassed entirely for this operation.

### AI change summary (natural-language translation of diff)

The LLM translates the +/- diff computed by EOS into natural language. EOS handles the diff calculation; the LLM only handles "translation". Input size is a few to tens of lines — no risk of context overflow.

Three processing patterns depending on operation type:

- **① VLAN / Interface** (session diff available): EOS +/- diff → LLM translates to natural language
- **② BGP delete** (`nc:operation="replace"` method): Dedicated logic generates summary directly from confirmed data (no LLM)  
  Note: passing the replace XML to the LLM causes it to misinterpret deletion as addition
- **③ BGP add / others** (session diff skipped): Generated XML → LLM reads intent

The AI change summary is displayed in the Diff tab, allowing operators to review changes before approval even when +/- diff is unavailable.

### Hybrid transaction method (how diff is reproduced)

```
① NETCONF Agent : Generate XML via RAG
② eAPI Agent   : _cmds_from_xml() converts XML → EOS CLI command list
                  → inject CLI commands into configure session
                  → show session-config diffs  ← obtain +/- diff (computed by EOS)
                  → abort (session discarded — zero impact on device)
  Note: cEOS configure session only accepts CLI commands, not OpenConfig XML directly.
  XML→CLI conversion is implemented in the app layer. VLAN/Interface are supported;
  BGP session diff is skipped and covered by AI change summary instead.
③ Hub           : Return generated XML + +/- diff + AI change summary to UI
④ Operator      : Review "+ hostname new-sw1" format and AI summary in Diff tab
⑤ Approve → NETCONF: Push validated XML via edit-config
⑥ Verify applied config : get_config audit
⑦ ANTA Post-Check : Automated verification of zero side effects (compared against the Before Snapshot)
```

### RAG knowledge sources

| RAG | Knowledge source | Purpose |
|-----|-----------------|---------|
| NETCONF | OpenConfig YANG + Arista YANG + gNMI capabilities (128 + 146 models) + verified XML templates | Generate XML config templates |
| eAPI | `eapi_documentation.json` (2,051 commands) | Select show commands + interpret response fields |

The NETCONF RAG includes **real-hardware-verified XML templates** as a 4th knowledge source alongside YANG models. This allows the LLM to avoid patterns that are valid per the YANG schema but fail on actual cEOS hardware (e.g., BGP neighbor deletion via replace).

### Microsoft Agent Framework

`agent_framework_openai.OpenAIChatCompletionClient` is used as the LLM client for the NETCONF Agent. It abstracts Groq and Azure OpenAI behind a single interface and automatically falls back on failure.

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

## References

**Agent protocol**
- [A2A Protocol (Google)](https://github.com/google/A2A)
- [Microsoft Agent Framework](https://github.com/microsoft/agent-framework)

**Lab environment**
- [Containerlab — cEOS](https://containerlab.dev/manual/kinds/ceos/)
- [gnmic — gNMI CLI Client](https://gnmic.openconfig.net/)

**Network automation**
- [Arista eAPI Python Library (pyeapi)](https://github.com/arista-eosplus/pyeapi)
- [ANTA — Arista Network Test Automation](https://anta.arista.com/)
- [ncclient — NETCONF Python Client](https://ncclient.readthedocs.io/)

**RAG knowledge sources (YANG models)**
- [aristanetworks/yang — Arista YANG Models](https://github.com/aristanetworks/yang)
- [openconfig/public — OpenConfig YANG Models](https://github.com/openconfig/public)

**Security**
- [XDP IPS (ips-maf)](https://github.com/hidemi-k/maf-ebpf-sase/tree/main/ips-maf)

**RAG embedding model**
- [BAAI/bge-large-en-v1.5](https://huggingface.co/BAAI/bge-large-en-v1.5)
