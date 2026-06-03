📘 English | 🇯🇵 [日本語はこちら](README.ja.md)

## 🔥 Why it matters

- **OSS that unifies NETCONF / eAPI / eAPI Config / ANTA / XDP via A2A and operates Arista cEOS safely with natural language**
- **Junos-equivalent `commit check` diff reproduced on cEOS** — Dry-run → diff → approval → NETCONF / eAPI Config deploy → ANTA Post-Check, fully automated
- **Snapshot-diff RAG powered multi-agent fault diagnosis** — 5 specialist agents collaborate using normal-state diff as evidence, with automatic Self-Correction
- Agents integrated via A2A protocol. Groq → Azure OpenAI automatic fallback guarantees production reliability
- XDP/eBPF AI-controlled with Human-in-the-loop — existing C/Go assets integrated without modification
- Validated on real hardware via Containerlab (cEOS 4.36.0F). 

---

## Overview

| | |
|---|---|
| **Unified** | NETCONF / eAPI / eAPI Config / ANTA / XDP integrated via A2A — operated with a single natural-language sentence |
| **Safe** | Dry-run → +/- diff → human approval → NETCONF / eAPI Config deploy → ANTA auto Post-Check |
| **Diagnosis** | Snapshot-diff RAG → 5-agent fault diagnosis with Self-Correction |
| **Proven** | Validated on Containerlab (cEOS 4.36.0F). XDP security demo available |
| **AI** | A2A Hub classifies intent via LLM → delegates to specialist agents (NETCONF / eAPI / eAPI Config / XDP / ANTA / Diagnose) |
| **Microsoft** | Azure Container Apps + Azure VM + Azure OpenAI + Microsoft Agent Framework |

---

## Demo

📹 **Demo video** (2 min 45 sec)
[![Demo Video](https://img.youtube.com/vi/tjJTGXttZ-s/maxresdefault.jpg)](https://www.youtube.com/watch?v=tjJTGXttZ-s)

---

## ✨ Features

- 🔄 **Natural language → NETCONF XML → Dry-run → diff → approval → deploy** (Junos-equivalent pre-diff on cEOS)
- 🔍 **eAPI + RAG high-speed show / state query** (natural language → appropriate show command selected automatically)
- 🛠️ **eAPI Config (NETCONF-unsupported area config change)** — Covers VXLAN/EVPN and BGP network/redistribute via eAPI configure session. Two-layer safety guard + Phase1 dry-run / Phase2 commit flow
- 🔎 **Snapshot-diff RAG fault diagnosis** — Saves normal-state eAPI output and injects Unified Diff into LLM context at diagnosis time. Five specialist agents (flow routing / L2 / L3 / consistency check / report) collaborate with automatic Self-Correction
- ✅ **ANTA auto Post-Check** (~340 ms / 11 tests after deploy — zero side-effects verified automatically)
- 🛡️ **XDP/eBPF AI control** (Human-in-the-loop — AI proposes, human approves before XDP rule is applied)
- ⚡ **Groq → Azure OpenAI automatic fallback** (shared across all servers; swap LLM with a single file change)

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│            Azure Container Apps                      │
│  ┌────────────────────────────────────────────────┐  │
│  │  app_a2a.py (NiceGUI Web UI / port:8080)       │  │
│  │  · Natural language input → REST POST /execute │  │
│  │  · Dry-run → Diff review → Approve & Deploy    │  │
│  │  · ANTA Verify / Diagnose / Security tab       │  │
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
│  │  write  → :8001  / read      → :8002         │    │
│  │  security → :8003 / verify   → :8004         │    │
│  │  eapi_config → :8006 (VXLAN/EVPN・BGP CLI)  │    │
│  └────┬──────────┬──────────┬──────────┬───┬───┘    │
│       │          │          │          │   │         │
│  ┌────▼──┐  ┌────▼──┐  ┌───▼───┐  ┌───▼─┐ │        │
│  │:8001  │  │:8002  │  │:8003  │  │:8004│ │        │
│  │NETCONF│  │eAPI   │  │XDP    │  │ANTA │ │        │
│  │RAG    │  │Show + │  │Firewall│  │Verify│ │        │
│  │(cfg)  │  │Diff   │  │(eBPF) │  │(test)│ │        │
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

> **Communication path note**
> - Security tab real-time display (Top Traffic / Drop List / QoS List) polls Go IPS (:8080) **directly** from the Web UI — bypassing the A2A Hub.
> - Diagnose tab sends A2A requests **directly** from the Web UI to Diagnose Agent (:8005) — bypassing the A2A Hub.
> - Verify tab Before Snapshot / Post-Check buttons send A2A requests **directly** from the Web UI to ANTA Agent (:8004) — bypassing the A2A Hub. (`verify` keyword queries from Chat go through the Hub.)
> - Security operations and VXLAN/EVPN config changes and BGP network/redistribute triggered via chat go through Hub → respective agent as usual.

### Azure components

| Component | Azure service | Role |
|---|---|---|
| Web UI | Azure Container Apps | NiceGUI frontend (port:8088) |
| A2A Hub | Azure VM | LLM router + REST API (port:8000) |
| NETCONF Agent | Azure VM | Config change + RAG (port:8001) |
| eAPI Agent | Azure VM | State query + Diff engine (port:8002) |
| XDP Agent | Azure VM | Security control (port:8003) |
| ANTA Agent | Azure VM | Post-verification (port:8004) |
| Diagnose Agent | Azure VM | Fault diagnosis (5-agent collaboration) (port:8005) |
| eAPI Config Agent | Azure VM | VXLAN/EVPN and BGP network/redistribute — NETCONF-unsupported areas via eAPI configure session (port:8006) |
| Go IPS | Azure VM | eBPF/XDP REST API (port:8080). Attaches XDP/eBPF to ceos1 eth2 via `-iface eth2` |
| LLM Primary | Groq | llama-3.3-70b-versatile (low-latency inference) |
| LLM Fallback | Azure OpenAI | gpt-4.1-mini (private endpoint) |
| Agent framework | **Microsoft Agent Framework** | LLM client for NETCONF Agent + 5-agent framework for Diagnose Agent (6 Agent instances total) |

---

## A2A Hub routing flow

```
Natural language query
      │
      ▼
┌─────────────────────────────────────────────────────┐
│    classify_query()                                  │
│                                                     │
│  ⓪ VXLAN/EVPN × config change (no read verb)?     │
│     → "eapi_config" ──────────────────────────────▶ eAPI Config Agent :8006
│                                                     │
│     VXLAN/EVPN × read verb present?                │
│     → "read"   ───────────────────────────────────▶ eAPI Agent :8002
│                                                     │
│     BGP network / redistribute / advertise?         │
│     → "eapi_config" ──────────────────────────────▶ eAPI Config Agent :8006
│                                                     │
│  ① VERIFY_KEYWORDS match?                          │
│     → "verify"  ──────────────────────────────────▶ ANTA Agent   :8004
│                                                     │
│  ② SECURITY_REQUIRED match?                        │
│     → "security" ─────────────────────────────────▶ XDP Agent    :8003
│                                                     │
│  ③ READ_KEYWORDS only?                             │
│     → "read"   ────────────────────────────────────▶ eAPI Agent  :8002
│                                                     │
│  ④ WRITE_KEYWORDS only?                            │
│     → "write"  ────────────────────────────────────▶ NETCONF Agent :8001
│                                                     │
│  ④ read + write mixed? → "mixed"                  │
│     → execute read only + show warning bubble      │
│                                                     │
│  ⑤ Ambiguous → LLM fallback                       │
└─────────────────────────────────────────────────────┘

Note: Diagnose Agent (:8005) is called directly from the Web UI, bypassing the Hub.
Note: Verify tab (Before Snapshot / Post-Check) also calls ANTA Agent (:8004) directly from the Web UI, bypassing the Hub. Only `verify` keyword queries from Chat go through the Hub.
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
| `diagnose_a2a_server.py` | Diagnose Agent / Fault diagnosis (port:8005) |
| `arista_eapi_config_a2a_server.py` | eAPI Config Agent / VXLAN/EVPN and BGP network/redistribute — NETCONF-unsupported areas (port:8006) |
| `snapshot_manager.py` | Snapshot manager (for Diagnose Agent) |
| `diff_engine.py` | Diff extraction engine (for Diagnose Agent) |
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
git clone https://github.com/hidemi-k/a2a-ceos-core.git
cd a2a-ceos-core
pip install -r requirements.txt
```

> **Note**
> See `requirements.txt` for pinned versions of `a2a-sdk` and `agent-framework`.

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
python task_decompose_a2a_server.py &       # A2A Hub        :8000
python arista_netconf_rag_a2a_server.py &   # NETCONF Agent  :8001
python arista_eapi_show_a2a_server.py &     # eAPI Agent     :8002
python xdp_a2a_server.py &                 # XDP Agent      :8003
python arista_anta_verify_a2a_server.py &   # ANTA Agent     :8004
python diagnose_a2a_server.py &             # Diagnose Agent :8005
python arista_eapi_config_a2a_server.py &   # eAPI Config    :8006

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
│                                      ↑ Go IPS attaches XDP/eBPF to eth2 (-iface eth2)
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

eAPI is an Arista-proprietary API, making multi-vendor deployment difficult. NETCONF is a standard protocol, so future Juniper support will be added as microservices such as `a2a-junos-read` and `a2a-junos-write`. The A2A Hub can incorporate new agents into the existing flow simply by updating the routing logic. YANG-schema-based XML also gives LLMs higher generation accuracy than fragmented CLI commands, and `edit-config` idempotency automatically skips duplicate configuration.

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

`agent_framework_openai.OpenAIChatCompletionClient` is used in 2 servers: NETCONF Agent (XML generation) and Diagnose Agent (5-agent construction). It abstracts Groq and Azure OpenAI behind a single interface and automatically falls back on failure. 6 Agent instances in total are built with Microsoft Agent Framework (NETCONF Agent: 1, Diagnose Agent: 5).

```python
from agent_framework import Agent
from agent_framework_openai import OpenAIChatCompletionClient

client = OpenAIChatCompletionClient(
    model    = GROQ_MODEL,
    api_key  = _GROQ_KEY,
    base_url = GROQ_BASE_URL,
)
# client is the first positional argument (changed in MAF 1.7.0)
agent = Agent(client, name="AgentName", instructions="...")
```

### eAPI configure session limit handling (eAPI Config Agent)

If sessions are left open after dry-run, the `configure session` limit is reached. Phase1 always calls `abort` immediately after fetching `show session-config diffs` to discard the session. Phase2 (commit) generates a new timestamped session name (`eapi_config_YYYYMMDD_HHMMSS`) and never reuses the Phase1 session name.

```
Phase1 (dry-run): configure session → show diffs → abort  ← session discarded immediately
Phase2 (commit):  new session name  → configure session → commit
```

### Snapshot-diff RAG (Diagnose Agent)

Normal-state eAPI output is saved as JSON (`snapshot_manager.py`). At diagnosis time, `difflib` generates a Unified Diff which is injected into the LLM context (`diff_engine.py`). Implemented with no vector DB and no extra libraries (Python standard library only).

Five specialist agents (flow routing / L2 analysis / L3 analysis / consistency check / diagnostic report) collaborate. When the consistency-check agent detects a conflict, the L3 agent automatically re-analyzes (Self-Correction).

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
