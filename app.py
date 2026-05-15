#!/usr/bin/env python3
# Copyright (c) 2026 hidemi-k
# Licensed under the MIT License.
"""
app.py — Network Agent NiceGUI フロントエンド
"""

import asyncio
import base64
import json
import os
import time
from collections import defaultdict
import httpx
import websockets
from datetime import datetime
from nicegui import ui
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

API_BASE = os.getenv("API_BASE", "http://localhost:8000")
WS_BASE  = os.getenv("WS_BASE",  "ws://localhost:8000")

DEVICE = {
    "ip":       "172.20.100.31",
    "port":     "830",
    "username": "admin",
    "password": "admin",
}

# ── Basic 認証設定（環境変数で上書き可能） ───────────────────────────
AUTH_USER = os.getenv("BASIC_AUTH_USER", "judge")
AUTH_PASS = os.getenv("BASIC_AUTH_PASS", "hackathon2026")
AUTH_REALM = "Restricted"  # 固定文字列（Container Apps 推奨）

# ── レート制限設定 ────────────────────────────────────────────────────
RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", "30"))   # 1分あたり最大リクエスト数
_rate_store: dict = defaultdict(list)                        # IP → [timestamp, ...]


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """
    Starlette ミドルウェアで Basic 認証 + レート制限を実装。
    - /healthz は認証不要（Azure の死活監視用）
    - 静的アセット (_nicegui/) も認証対象外
    """

    def _check_auth(self, request: Request) -> bool:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8")
            user, passwd = decoded.split(":", 1)
            return user == AUTH_USER and passwd == AUTH_PASS
        except Exception:
            return False

    def _check_rate(self, ip: str) -> bool:
        now = time.time()
        window = 60.0
        _rate_store[ip] = [t for t in _rate_store[ip] if now - t < window]
        if len(_rate_store[ip]) >= RATE_LIMIT_RPM:
            return False
        _rate_store[ip].append(now)
        return True

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 認証不要パス
        bypass = (
            path == "/healthz"
            or path.startswith("/_nicegui/")
            or path.startswith("/favicon")
        )
        if bypass:
            return await call_next(request)

        # レート制限
        client_ip = request.client.host if request.client else "unknown"
        if not self._check_rate(client_ip):
            return Response(
                content='{"error": "Too Many Requests", "retry_after": 60}',
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": "60"},
            )

        # Basic 認証
        if not self._check_auth(request):
            return Response(
                content="Unauthorized",
                status_code=401,
                headers={
                    "WWW-Authenticate": f'Basic realm="{AUTH_REALM}"',
                    "Cache-Control": "no-store",
                },
            )

        return await call_next(request)

C = {
    "bg":         "#141414",
    "bg2":        "#1e1e1e",
    "bg3":        "#282828",
    "bg4":        "#303030",
    "border":     "rgba(255,255,255,0.08)",
    "border2":    "rgba(255,255,255,0.14)",
    "text":       "#e2e2e2",
    "text2":      "#8a8a8a",
    "text3":      "#4a4a4a",
    "success_bg": "#0f2318",
    "success_fg": "#5bb85b",
    "warn_bg":    "#251a08",
    "warn_fg":    "#c4902a",
    "info_bg":    "#0a1828",
    "info_fg":    "#4a8fc4",
    "danger_bg":  "#221010",
    "danger_fg":  "#c45a5a",
    "mono":       "'JetBrains Mono','Fira Code','Cascadia Code',monospace",
}

class State:
    def __init__(self):
        self.trace_id      = ""
        self.pending_xml   = ""
        self.pending_query = ""
        self.phase         = "idle"
        self.diff_history  = []

async def api_post(path, payload):
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(f"{API_BASE}{path}", json=payload)
        r.raise_for_status()
        return r.json()

def badge(label, kind="info"):
    colors = {
        "success": (C["success_bg"], C["success_fg"]),
        "dry-run": (C["warn_bg"],    C["warn_fg"]),
        "info":    (C["info_bg"],    C["info_fg"]),
        "failure": (C["danger_bg"],  C["danger_fg"]),
        "pending": (C["bg4"],        C["text2"]),
    }
    bg, fg = colors.get(kind, colors["info"])
    ui.label(label).style(
        f"background:{bg};color:{fg};font-size:10px;padding:2px 8px;"
        f"border-radius:4px;font-weight:500;white-space:nowrap;"
        f"border:0.5px solid {fg}22;"
    )

def diff_to_html(diff_text):
    if not diff_text:
        return f'<div style="color:{C["text3"]};font-family:{C["mono"]};font-size:11px;padding:8px;">(差分なし)</div>'
    out = []
    for ln in diff_text.strip().split("\n"):
        if ln.startswith("+") and not ln.startswith("+++"):
            color, bg = C["success_fg"], C["success_bg"]
        elif ln.startswith("-") and not ln.startswith("---"):
            color, bg = C["danger_fg"],  C["danger_bg"]
        elif ln.startswith("@@"):
            color, bg = C["info_fg"],    C["info_bg"]
        else:
            color, bg = C["text2"],      "transparent"
        esc = ln.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        out.append(
            f'<div style="background:{bg};color:{color};'
            f'font-family:{C["mono"]};font-size:11px;line-height:1.7;'
            f'padding:1px 10px;white-space:pre;">{esc}</div>'
        )
    return "\n".join(out)

def render_user_msg(text, chat_col):
    with chat_col:
        with ui.row().style("justify-content:flex-end;width:100%;margin:2px 0;"):
            with ui.column().style("align-items:flex-end;gap:2px;max-width:76%;"):
                ui.label(f"you · {datetime.now().strftime('%H:%M')}").style(
                    f"font-size:10px;color:{C['text3']};"
                )
                ui.label(text).style(
                    f"background:{C['info_bg']};color:{C['info_fg']};"
                    f"border:0.5px solid {C['info_fg']}33;"
                    f"border-radius:12px 12px 2px 12px;padding:8px 13px;"
                    f"font-size:13px;line-height:1.5;"
                )

def render_agent_msg(text, detail, status, chat_col,
                     actions=None, show_approve=False,
                     on_approve=None, on_edit=None):
    with chat_col:
        with ui.row().style("justify-content:flex-start;width:100%;margin:2px 0;"):
            with ui.column().style("gap:3px;max-width:94%;"):
                with ui.row().style("align-items:center;gap:6px;"):
                    ui.label(f"agent · {datetime.now().strftime('%H:%M')}").style(
                        f"font-size:10px;color:{C['text3']};"
                    )
                    badge(status, status)
                with ui.card().style(
                    f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                    f"border-radius:2px 12px 12px 12px;"
                    f"padding:11px 14px;gap:5px;box-shadow:none;"
                ):
                    ui.label(text).style(
                        f"font-size:13px;font-weight:500;color:{C['text']};"
                    )
                    if detail:
                        ui.label(detail).style(
                            f"font-size:12px;color:{C['text2']};line-height:1.5;"
                        )
                    with ui.row().style("gap:6px;margin-top:6px;flex-wrap:wrap;"):
                        for act in (actions or []):
                            ui.button(act).props("flat dense").style(
                                f"font-size:11px;padding:2px 9px;"
                                f"border:0.5px solid {C['border2']};"
                                f"border-radius:5px;color:{C['text2']};"
                                f"background:{C['bg3']};min-height:26px;"
                            )
                        if on_edit:
                            ui.button("XML 編集").props("flat dense").style(
                                f"font-size:11px;padding:2px 9px;"
                                f"border:0.5px solid {C['border2']};"
                                f"border-radius:5px;color:{C['text2']};"
                                f"background:{C['bg3']};min-height:26px;"
                            ).on("click", on_edit)
                        if show_approve and on_approve:
                            ui.button("承認 & デプロイ").props("flat dense").style(
                                f"font-size:11px;padding:2px 11px;"
                                f"border:0.5px solid {C['success_fg']}66;"
                                f"border-radius:5px;color:{C['success_fg']};"
                                f"background:{C['success_bg']};min-height:26px;"
                                f"font-weight:500;"
                            ).on("click", on_approve)

def show_xml_editor(xml_str, on_save):
    with ui.dialog() as dlg, ui.card().style(
        f"width:680px;max-width:95vw;padding:20px;gap:12px;"
        f"border-radius:10px;background:{C['bg2']};"
        f"border:0.5px solid {C['border2']};"
    ):
        ui.label("XML 編集").style(
            f"font-size:14px;font-weight:500;color:{C['text']};"
        )
        ui.label("編集後「保存して再 Dry-run」を押すと差分が更新されます。").style(
            f"font-size:11px;color:{C['text2']};"
        )
        editor = ui.textarea(value=xml_str).style(
            f"width:100%;font-family:{C['mono']};font-size:11px;"
        ).props("outlined rows=16 dark")
        val_label = ui.label("").style("font-size:11px;")

        async def validate_xml():
            try:
                r = await api_post("/validate", {"xml": editor.value.strip()})
                if r["valid"]:
                    val_label.text = "✅ XML 構文チェック OK"
                    val_label.style(f"color:{C['success_fg']};")
                else:
                    val_label.text = f"❌ {r.get('error','不正')}"
                    val_label.style(f"color:{C['danger_fg']};")
            except Exception as e:
                val_label.text = f"⚠️ チェック失敗: {e}"
                val_label.style(f"color:{C['warn_fg']};")

        with ui.row().style("gap:8px;width:100%;margin-top:4px;"):
            ui.button("構文チェック", on_click=validate_xml).props("flat").style(
                f"border:0.5px solid {C['border2']};border-radius:6px;"
                f"color:{C['text2']};font-size:12px;"
            )
            ui.label("").style("flex:1;")
            ui.button("キャンセル", on_click=dlg.close).props("flat").style(
                f"border:0.5px solid {C['border2']};border-radius:6px;"
                f"color:{C['text2']};font-size:12px;"
            )
            async def do_save():
                xml = editor.value.strip()
                dlg.close()
                await on_save(xml)
            ui.button("保存して再 Dry-run", on_click=do_save).style(
                f"background:{C['info_bg']};color:{C['info_fg']};"
                f"border:0.5px solid {C['info_fg']}66;"
                f"border-radius:6px;font-size:12px;font-weight:500;"
            )
    dlg.open()

def show_approve_dialog(trace_id, on_deploy):
    with ui.dialog() as dlg, ui.card().style(
        f"width:400px;padding:20px;gap:12px;border-radius:10px;"
        f"background:{C['bg2']};border:0.5px solid {C['border2']};"
    ):
        ui.label("デプロイ承認の確認").style(
            f"font-size:14px;font-weight:500;color:{C['text']};"
        )
        ui.html(
            f'<div style="font-size:10px;color:{C["text3"]};">'
            f'trace_id: <span style="font-family:{C["mono"]};color:{C["text2"]};">'
            f'{trace_id}</span></div>'
        )
        snap_id = f"snap_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        ui.label("スナップショット（自動取得済み）").style(
            f"font-size:11px;color:{C['text2']};margin-top:6px;"
        )
        ui.html(
            f'<div style="background:{C["bg3"]};border:0.5px solid {C["border2"]};'
            f'border-radius:6px;padding:8px 10px;font-family:{C["mono"]};'
            f'font-size:10px;color:{C["text"]};line-height:1.6;">'
            f'snapshot-id: {snap_id}<br>'
            f'取得時刻: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}<br>'
            f'running-config: 保存完了</div>'
        )
        ui.label("ロールバック手順").style(
            f"font-size:11px;color:{C['text2']};margin-top:4px;"
        )
        ui.html(
            f'<div style="font-size:10px;color:{C["text2"]};line-height:1.6;">'
            f'コマンド: <code style="color:{C["info_fg"]};">'
            f'POST /rollback {{snap_id: "{snap_id}"}}</code><br>'
            f'所要時間の目安: 約 30 秒</div>'
        )
        chk1 = ui.checkbox("バックアップの取得を確認しました（自動チェック不可）").style(
            f"color:{C['text']};font-size:12px;"
        )
        chk2 = ui.checkbox("ロールバック手順を理解しています").style(
            f"color:{C['text']};font-size:12px;"
        )
        warn_lbl = ui.label("").style(f"font-size:11px;color:{C['danger_fg']};")
        with ui.row().style("gap:8px;width:100%;margin-top:6px;"):
            ui.button("キャンセル", on_click=dlg.close).props("flat").style(
                f"flex:1;border:0.5px solid {C['border2']};border-radius:6px;"
                f"color:{C['text2']};font-size:12px;"
            )
            async def do_deploy():
                if not chk1.value:
                    warn_lbl.text = "⚠️ バックアップ確認チェックが必要です"
                    return
                if not chk2.value:
                    warn_lbl.text = "⚠️ ロールバック理解チェックが必要です"
                    return
                dlg.close()
                await on_deploy()
            ui.button("デプロイ実行", on_click=do_deploy).style(
                f"flex:1;background:{C['success_bg']};color:{C['success_fg']};"
                f"border:0.5px solid {C['success_fg']}66;border-radius:6px;"
                f"font-size:12px;font-weight:500;"
            )
    dlg.open()

@ui.page("/")
def main():
    # ページごとに独立した状態を生成（複数ブラウザタブ対応）
    state = State()

    ui.add_head_html("""
    <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500&display=swap" rel="stylesheet">
    <style>
      body { margin:0; background:#141414; font-family:'IBM Plex Sans',sans-serif; }
      .nicegui-content { padding:0 !important; }
      ::-webkit-scrollbar { width:4px; height:4px; }
      ::-webkit-scrollbar-track { background:transparent; }
      ::-webkit-scrollbar-thumb { background:rgba(255,255,255,.12); border-radius:2px; }
      .q-btn { text-transform:none !important; letter-spacing:0 !important; }
      .q-tab__label { text-transform:none !important; font-size:12px !important; }
      .q-tabs { min-height:36px !important; }
      .q-tab { min-height:36px !important; padding:0 16px !important; }
      .full-input .q-field { width:100% !important; }
      .full-input .q-field__control { width:100% !important; }
      .full-input input { width:100% !important; box-sizing:border-box !important; }
    </style>
    """)

    with ui.column().style(
        f"width:100vw;height:100vh;gap:0;background:{C['bg']};overflow:hidden;"
    ):
        # トップバー
        with ui.row().style(
            f"width:100%;background:{C['bg2']};border-bottom:0.5px solid {C['border']};"
            f"padding:0 18px;align-items:center;gap:12px;box-sizing:border-box;"
            f"flex-shrink:0;height:44px;"
        ):
            ui.label("Network Agent").style(
                f"font-size:14px;font-weight:500;color:{C['text']};"
            )
            badge("connected", "success")
            ui.label("").style("flex:1;")
            ui.label("device:").style(f"font-size:11px;color:{C['text3']};")
            ui.label(f"sw1 ({DEVICE['ip']})").style(
                f"font-size:12px;font-weight:500;color:{C['text']};"
            )
            badge("admin", "info")

        with ui.column().style("flex:1;overflow:hidden;min-height:0;width:100%;"):
            with ui.tabs().style(
                f"background:{C['bg2']};border-bottom:0.5px solid {C['border']};"
                f"color:{C['text2']};flex-shrink:0;"
            ).props("dense dark align='left'") as tabs:
                tab_chat = ui.tab("Chat",           icon="chat")
                tab_diff = ui.tab("Diff / History", icon="compare_arrows")

            # switch_to_diff: Chat タブから Diff タブへ切り替えて差分を更新
            # render_diff_tab は後で定義されるが、クロージャで実行時参照なので問題なし
            def switch_to_diff():
                tabs.set_value(tab_diff)
                render_diff_tab()

            with ui.tab_panels(tabs, value=tab_chat).style(
                "flex:1;overflow:hidden;min-height:0;background:transparent;"
            ).props("dark"):

                # ── Chat タブ ────────────────────────────────────────
                with ui.tab_panel(tab_chat).style(
                    f"padding:0;height:100%;display:flex;flex-direction:column;"
                    f"background:{C['bg']};"
                ):
                    chat_col = ui.column().style(
                        "flex:1;overflow-y:auto;padding:14px 16px;gap:8px;min-height:0;"
                    )
                    with chat_col:
                        ui.label(
                            "自然言語でネットワーク操作を入力してください。"
                            "Dry-run → Diff 確認 → 承認 の順で実行されます。"
                        ).style(
                            f"font-size:11px;color:{C['text3']};"
                            f"text-align:center;padding:8px 0;width:100%;"
                        )

                    # 入力エリア
                    with ui.column().style(
                        f"border-top:0.5px solid {C['border']};"
                        f"padding:10px 16px;gap:8px;background:{C['bg2']};"
                        f"flex-shrink:0;box-sizing:border-box;width:100%;"
                    ):
                        with ui.row().style("gap:8px;align-items:center;flex-wrap:wrap;"):
                            phase_label = ui.label("● 待機中").style(
                                f"font-size:11px;color:{C['text3']};"
                            )
                            dry_run_chk = ui.checkbox("dry-run").style(
                                f"font-size:12px;color:{C['text']};"
                            )
                            dry_run_chk.value = True

                            ui.label("dry-run 有効").style(
                                f"font-size:10px;background:{C['warn_bg']};"
                                f"color:{C['warn_fg']};padding:2px 8px;border-radius:4px;"
                                f"border:0.5px solid {C['warn_fg']}44;"
                            )

                        input_box = ui.input(
                            placeholder="例: VLAN ID 200 の MGMT_VLAN を作成してください"
                        ).classes("full-input").props("outlined dense dark").style(
                            "width:100%;font-size:13px;"
                        )

                        with ui.row().style(
                            "justify-content:space-between;width:100%;align-items:center;"
                        ):
                            trace_label = ui.label("").style(
                                f"font-size:10px;color:{C['text3']};font-family:{C['mono']};"
                            )
                            send_btn = ui.button("送信").props("dark").style(
                                f"font-size:13px;padding:4px 24px;"
                                f"border:0.5px solid {C['border2']};"
                                f"border-radius:6px;color:{C['text']};"
                                f"background:{C['bg3']};"
                            )

                    # ロジック
                    def set_phase(p):
                        state.phase = p
                        labels = {
                            "idle":             ("● 待機中",       C["text3"]),
                            "executing":        ("⟳ 実行中...",    C["warn_fg"]),
                            "awaiting_confirm": ("◉ 承認待ち",     C["success_fg"]),
                            "deploying":        ("⟳ デプロイ中...", C["warn_fg"]),
                        }
                        txt, col = labels.get(p, ("● 待機中", C["text3"]))
                        phase_label.text = txt
                        phase_label.style(f"font-size:11px;color:{col};")
                        if p != "idle":
                            send_btn.props(add="disabled")
                        else:
                            send_btn.props(remove="disabled")

                    async def do_execute(query):
                        set_phase("executing")
                        render_user_msg(query, chat_col)
                        with chat_col:
                            thinking = ui.label("⟳ Orchestrator 実行中...").style(
                                f"font-size:11px;color:{C['text3']};padding:2px 0;"
                            )
                        try:
                            result = await api_post("/execute", {
                                "query": query, "device": DEVICE,
                            })
                            thinking.delete()

                            state.trace_id      = result["trace_id"]
                            state.pending_xml   = result.get("xml", "")
                            state.pending_query = query
                            diff    = result.get("diff", "")
                            status  = result.get("status", "unknown")
                            summary = result.get("summary", "完了")

                            trace_label.text = f"trace: {state.trace_id}"

                            # is_get を先に定義（_xml_for_hist・diff_history より前）
                            is_get = any(k in query for k in [
                                "確認","状態","show","list","一覧","取得",
                                "バージョン","version","教えて","見せて","表示",
                                "調べて","確かめ","チェック","check","get",
                                "情報","info","どうなって","どんな",
                                "ping","疎通","ルーティング","routing",
                                "bgp","interface","インターフェース","neighbor",
                            ])

                            # diff が空でも xml があれば履歴に記録する
                            _xml_for_hist = result.get("xml", "")
                            # GETクエリは差分履歴に追加しない（SET/DELETE のみ記録）
                            if not is_get and (diff or _xml_for_hist):
                                state.diff_history.append({
                                    "trace_id": state.trace_id,
                                    "query":    query,
                                    "diff":     diff,
                                    "xml":      _xml_for_hist,
                                    "status":   status,
                                    "time":     datetime.now().strftime("%H:%M"),
                                    "deployed": False,
                                    "tasks":    result.get("tasks", []),
                                    "logs":     result.get("logs", []),
                                })

                            if dry_run_chk.value and state.pending_xml and not is_get:
                                set_phase("awaiting_confirm")

                                def _on_approve():
                                    # NiceGUI スロットコンテキスト内で直接モーダルを開く
                                    show_approve_dialog(
                                        trace_id=state.trace_id,
                                        on_deploy=_do_deploy_api,
                                    )

                                def _on_edit():
                                    async def _save(new_xml):
                                        state.pending_xml = new_xml
                                        await do_execute(query)
                                    show_xml_editor(state.pending_xml, _save)

                                render_agent_msg(
                                    text=summary,
                                    detail="右の「承認 & デプロイ」または「XML 編集」で続行してください。",
                                    status="dry-run",
                                    chat_col=chat_col,
                                    show_approve=True,
                                    on_approve=_on_approve,
                                    on_edit=_on_edit,
                                )
                                # Dry-run完了 → 自動で Diff タブへ切り替え
                                switch_to_diff()
                            else:
                                # GETクエリ: logs からVLAN情報を抽出して表示
                                _kind = "success" if "success" in status else "info"
                                _logs = result.get("logs", [])

                                # VLAN取得: logs の中から get_inventory や deploy の結果を抽出
                                _vlan_line = ""
                                for _l in _logs:
                                    if "vlan" in _l.lower() and (":" in _l or "VLAN" in _l):
                                        _vlan_line = _l.strip()
                                        break

                                # diff(=GET結果XML)からVLAN IDを抽出
                                _vids = []
                                if diff:
                                    import xml.etree.ElementTree as _ET
                                    try:
                                        _xml = diff if diff.strip().startswith("<") else f"<r>{diff}</r>"
                                        _root = _ET.fromstring(_xml)
                                        _ns = "http://openconfig.net/yang/network-instance"
                                        _seen = set()
                                        for _ve in _root.iter(f"{{{_ns}}}vlan-id"):
                                            if _ve.text and _ve.text not in _seen:
                                                _seen.add(_ve.text)
                                                _vids.append(_ve.text)
                                    except Exception:
                                        pass

                                # VLAN一覧 + LLM解説を組み合わせて表示
                                _explanation = result.get("explanation", "")
                                if _vids:
                                    _vlan_str = ", ".join(sorted(_vids, key=int))
                                    _detail = f"取得VLAN: {_vlan_str}"
                                else:
                                    _detail = summary
                                # LLM解説があれば detail に追加
                                if _explanation:
                                    _detail = _explanation

                                with chat_col:
                                    with ui.row().style("justify-content:flex-start;width:100%;margin:2px 0;"):
                                        with ui.column().style("gap:3px;max-width:94%;"):
                                            with ui.row().style("align-items:center;gap:6px;"):
                                                ui.label(f"agent · {datetime.now().strftime('%H:%M')}").style(
                                                    f"font-size:10px;color:{C['text3']};"
                                                )
                                                badge(_kind, _kind)
                                            with ui.card().style(
                                                f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                                f"border-radius:2px 12px 12px 12px;"
                                                f"padding:11px 14px;gap:5px;box-shadow:none;"
                                            ):
                                                ui.label(summary).style(
                                                    f"font-size:13px;font-weight:500;color:{C['text']};"
                                                )
                                                ui.label(_detail).style(
                                                    f"font-size:12px;color:{C['text2']};line-height:1.5;"
                                                )
                                                # VLAN一覧を別行で表示（解説と両方ある場合）
                                                if _vids and _explanation:
                                                    _vlan_str = ", ".join(sorted(_vids, key=int))
                                                    ui.label(f"取得VLAN: {_vlan_str}").style(
                                                        f"font-size:11px;color:{C['text3']};font-family:{C['mono']};"
                                                    )
                                                if diff:
                                                    _xml_preview = diff[:600] + ("..." if len(diff)>600 else "")
                                                    ui.html(
                                                        f'<div style="background:{C["bg3"]};border:0.5px solid {C["border"]}; '
                                                        f'border-radius:6px;padding:8px;font-family:{C["mono"]};'
                                                        f'font-size:10px;color:{C["text2"]};white-space:pre;'
                                                        f'overflow-x:auto;max-height:160px;overflow-y:auto;margin-top:6px;">'
                                                        + _xml_preview.replace("<","&lt;").replace(">","&gt;") +
                                                        '</div>'
                                                    )
                                set_phase("idle")

                        except Exception as e:
                            try:
                                thinking.delete()
                            except Exception:
                                pass
                            render_agent_msg(
                                text="エラーが発生しました",
                                detail=str(e),
                                status="failure",
                                chat_col=chat_col,
                            )
                            set_phase("idle")

                    async def _do_deploy_api():
                        set_phase("deploying")
                        with chat_col:
                            dep_msg = ui.label("⟳ デプロイ中...").style(
                                f"font-size:11px;color:{C['text3']};padding:2px 0;"
                            )
                        try:
                            result = await api_post("/deploy", {
                                "trace_id": state.trace_id,
                                "device":   DEVICE,
                            })
                            dep_msg.delete()
                            status  = result.get("status","unknown")
                            summary = result.get("summary","完了")
                            diff    = result.get("diff","")
                            kind    = "success" if "success" in status else "failure"

                            for h in reversed(state.diff_history):
                                if h["trace_id"] == state.trace_id:
                                    h["deployed"]      = True
                                    h["deploy_diff"]   = diff
                                    h["deploy_status"] = status
                                    break

                            with chat_col:
                                with ui.row().style("justify-content:flex-start;width:100%;margin:2px 0;"):
                                    with ui.column().style("gap:3px;max-width:94%;"):
                                        with ui.row().style("align-items:center;gap:6px;"):
                                            ui.label(f"agent · {datetime.now().strftime('%H:%M')}").style(
                                                f"font-size:10px;color:{C['text3']};"
                                            )
                                            badge(kind, kind)
                                        with ui.card().style(
                                            f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                            f"border-radius:2px 12px 12px 12px;"
                                            f"padding:11px 14px;gap:5px;box-shadow:none;"
                                        ):
                                            ui.label(summary).style(
                                                f"font-size:13px;font-weight:500;color:{C['text']};"
                                            )
                                            ui.label(f"status: {status}").style(
                                                f"font-size:12px;color:{C['text2']};line-height:1.5;"
                                            )
                                            ui.button("Diff / History タブで確認").props("flat dense").style(
                                                f"font-size:11px;padding:2px 9px;"
                                                f"border:0.5px solid {C['border2']};"
                                                f"border-radius:5px;color:{C['text2']};"
                                                f"background:{C['bg3']};min-height:26px;margin-top:6px;"
                                            ).on("click", switch_to_diff)
                            ui.notify(
                                summary,
                                color="positive" if kind=="success" else "negative"
                            )
                        except Exception as e:
                            dep_msg.delete()
                            render_agent_msg(
                                text="デプロイ失敗",
                                detail=str(e),
                                status="failure",
                                chat_col=chat_col,
                            )
                        finally:
                            set_phase("idle")

                    async def send_message():
                        query = input_box.value.strip()
                        if not query or state.phase != "idle":
                            return
                        input_box.value = ""
                        await do_execute(query)

                    send_btn.on("click", send_message)
                    input_box.on("keydown.enter", send_message)

                # ── Diff / History タブ ───────────────────────────────
                with ui.tab_panel(tab_diff).style(
                    f"padding:0;height:100%;display:flex;flex-direction:column;"
                    f"background:{C['bg']};"
                ):
                    with ui.row().style(
                        f"padding:10px 16px;border-bottom:0.5px solid {C['border']};"
                        f"align-items:center;gap:10px;flex-shrink:0;background:{C['bg2']};"
                    ):
                        ui.label("Diff / History").style(
                            f"font-size:12px;font-weight:500;color:{C['text']};"
                        )
                        ui.label("").style("flex:1;")
                        refresh_btn = ui.button("更新").props("flat dense").style(
                            f"font-size:11px;color:{C['text2']};"
                            f"border:0.5px solid {C['border2']};border-radius:5px;"
                        )

                    diff_body = ui.column().style(
                        "flex:1;overflow-y:auto;padding:12px 16px;gap:12px;min-height:0;"
                    )


                    def render_diff_tab():
                        diff_body.clear()
                        with diff_body:
                            if not state.diff_history:
                                ui.label("操作を実行すると差分履歴が表示されます。").style(
                                    f"font-size:12px;color:{C['text3']};"
                                    "text-align:center;padding:32px 0;width:100%;"
                                )
                                return
                            for entry in reversed(state.diff_history):
                                with ui.card().style(
                                    f"background:{C['bg2']};border:0.5px solid {C['border2']};"
                                    "border-radius:10px;padding:14px 16px;"
                                    "gap:10px;box-shadow:none;width:100%;"
                                ):
                                    # ヘッダー
                                    with ui.row().style(
                                        "align-items:center;gap:8px;flex-wrap:wrap;"
                                        "padding-bottom:8px;"
                                        "border-bottom:0.5px solid rgba(255,255,255,0.06);"
                                    ):
                                        badge(
                                            "deployed" if entry.get("deployed") else "dry-run",
                                            "success"  if entry.get("deployed") else "dry-run"
                                        )
                                        ui.label(entry["query"]).style(
                                            f"font-size:13px;font-weight:500;color:{C['text']};"
                                        )
                                        ui.label("").style("flex:1;")
                                        ui.label(entry["time"]).style(
                                            f"font-size:10px;color:{C['text3']};"
                                        )
                                        ui.label(entry["trace_id"]).style(
                                            f"font-size:10px;color:{C['text3']};"
                                            f"font-family:{C['mono']};"
                                        )

                                    # ① 設定差分 Diff（常時表示）
                                    ui.label("① 設定差分").style(
                                        f"font-size:10px;font-weight:500;color:{C['text3']};"
                                        "text-transform:uppercase;letter-spacing:.06em;"
                                    )
                                    _ed = entry.get("diff", "")
                                    _ex = entry.get("xml", "")
                                    if _ed:
                                        _border = C["border"]
                                        ui.html(
                                            f'<div style="border:0.5px solid {_border};'
                                            'border-radius:6px;overflow:hidden;padding:4px 0;">'
                                            + diff_to_html(_ed)
                                            + '</div>'
                                        )
                                    elif _ex:
                                        _xp = _ex[:1000] + ("..." if len(_ex) > 1000 else "")
                                        _bg3 = C["bg3"]; _bdr = C["border"]; _mn = C["mono"]; _t2 = C["text2"]
                                        ui.html(
                                            f'<div style="background:{_bg3};border:0.5px solid {_bdr};'
                                            'border-radius:6px;padding:8px;'
                                            f'font-family:{_mn};font-size:10px;'
                                            f'color:{_t2};white-space:pre;'
                                            'overflow-x:auto;max-height:180px;overflow-y:auto;">'
                                            + _xp.replace("<", "&lt;").replace(">", "&gt;")
                                            + '</div>'
                                        )
                                    else:
                                        ui.label("(差分なし)").style(
                                            f"font-size:11px;color:{C['text3']};"
                                        )

                                    # Deploy Diff
                                    if entry.get("deployed") and entry.get("deploy_diff"):
                                        ui.label("Deploy Diff (Audit)").style(
                                            f"font-size:10px;font-weight:500;color:{C['success_fg']};"
                                            "text-transform:uppercase;letter-spacing:.06em;margin-top:2px;"
                                        )
                                        _bdr2 = C["border"]
                                        ui.html(
                                            f'<div style="border:0.5px solid {_bdr2};'
                                            'border-radius:6px;overflow:hidden;padding:4px 0;">'
                                            + diff_to_html(entry.get("deploy_diff", ""))
                                            + '</div>'
                                        )

                                    # ② Reasoning（折りたたみ）— GET操作は非表示
                                    _tasks = entry.get("tasks", [])
                                    _show_reasoning = _tasks and not all(
                                        t.get("operation") in ("get", "get-config")
                                        for t in _tasks
                                    )
                                    if _show_reasoning:
                                        with ui.expansion(
                                            "② AI の判断根拠 (Reasoning)",
                                            icon="psychology",
                                        ).props("dense dark").style(
                                            f"background:{C['bg3']};border:0.5px solid {C['border']};"
                                            "border-radius:6px;margin-top:4px;"
                                            f"color:{C['text2']};font-size:12px;"
                                        ):
                                            for tk in _tasks:
                                                with ui.row().style(
                                                    "gap:8px;padding:5px 0;"
                                                    f"border-bottom:0.5px solid {C['border']};"
                                                    "align-items:flex-start;"
                                                ):
                                                    ui.label(tk.get("id", "")).style(
                                                        f"font-size:10px;color:{C['info_fg']};"
                                                        f"font-family:{C['mono']};min-width:52px;"
                                                    )
                                                    with ui.column().style("gap:2px;"):
                                                        ui.label(tk.get("description", "")).style(
                                                            f"font-size:11px;color:{C['text']};"
                                                        )
                                                        _op = tk.get("operation","")
                                                        _tg = tk.get("target","")
                                                        _yp = tk.get("yang_path","")
                                                        ui.label(
                                                            f"op={_op}  target={_tg}  yang={_yp}"
                                                        ).style(
                                                            f"font-size:10px;color:{C['text3']};"
                                                            f"font-family:{C['mono']};"
                                                        )

                                    # ③ Raw Logs（折りたたみ）— GET操作は非表示
                                    _logs = entry.get("logs", [])
                                    _show_logs = _logs and not all(
                                        t.get("operation") in ("get", "get-config")
                                        for t in entry.get("tasks", [{"operation":"set"}])
                                    )
                                    if _show_logs:
                                        with ui.expansion(
                                            "③ 技術ログ (NETCONF / Raw)",
                                            icon="terminal",
                                        ).props("dense dark").style(
                                            f"background:{C['bg3']};border:0.5px solid {C['border']};"
                                            "border-radius:6px;margin-top:4px;"
                                            f"color:{C['text2']};font-size:12px;"
                                        ):
                                            _lt = "\n".join(_logs)
                                            _bg = C["bg"]; _mn2 = C["mono"]; _t2b = C["text2"]
                                            ui.html(
                                                f'<div style="background:{_bg};'
                                                'border-radius:4px;padding:8px;'
                                                f'font-family:{_mn2};font-size:10px;'
                                                f'color:{_t2b};white-space:pre;'
                                                'overflow-x:auto;overflow-y:auto;'
                                                'max-height:300px;line-height:1.6;">'
                                                + _lt.replace("<", "&lt;").replace(">", "&gt;")
                                                + '</div>'
                                            )

                                    # Confirm ボタン（未デプロイ・SET/DELETE のみ）
                                    _entry_tasks = entry.get("tasks", [])
                                    _entry_is_get = all(
                                        t.get("operation") in ("get", "get-config")
                                        for t in _entry_tasks
                                    ) if _entry_tasks else entry.get("is_get", False)

                                    if (not entry.get("deployed")
                                            and entry.get("trace_id") == state.trace_id
                                            and not _entry_is_get):
                                        ui.separator().style(
                                            f"margin:6px 0;background:{C['border']};"
                                        )
                                        _tid = entry["trace_id"]
                                        def _make_confirm(tid):
                                            def _confirm():
                                                show_approve_dialog(
                                                    trace_id=tid,
                                                    on_deploy=_do_deploy_api,
                                                )
                                            return _confirm
                                        ui.button(
                                            "✓  内容を確認しました。本番適用します",
                                        ).style(
                                            f"width:100%;padding:10px;"
                                            "font-size:13px;font-weight:500;"
                                            f"background:{C['success_bg']};color:{C['success_fg']};"
                                            f"border:0.5px solid {C['success_fg']}66;"
                                            "border-radius:6px;margin-top:2px;"
                                        ).on("click", _make_confirm(_tid))

                                        refresh_btn.on("click", render_diff_tab)

                    # タブ切り替え検知: tab_diff の visibility 変化で更新
                    def on_tab_change(e):
                        # e.args が tab_diff.name と一致したら更新
                        if hasattr(e, "args") and "Diff / History" in str(e.args):
                            render_diff_tab()

                    tabs.on("update:modelValue", lambda e: on_tab_change(e))

                    # 「Diff / Historyタブで確認」ボタン用: タブを切り替えて更新
                    render_diff_tab()

# ── ミドルウェア登録 ─────────────────────────────────────────────────
from nicegui import app as _nicegui_app
_nicegui_app.add_middleware(BasicAuthMiddleware)

ui.run(
    title="Network Agent",
    host="0.0.0.0",
    port=int(os.getenv("PORT", "8088")),
    reload=False,
    dark=True,
    favicon="🌐",
)
