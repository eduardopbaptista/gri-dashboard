#!/usr/bin/env python3
"""
GRI Dashboard Sync — v3
Puxa dados do Culkin Analytics (Salesforce) e atualiza gri_global_hub.html.
Usa curl via subprocess para evitar problemas de SSL do LibreSSL no macOS.

Uso:
    python3 sync_dashboard.py [caminho/para/gri_global_hub.html]
"""

import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from difflib import SequenceMatcher
from unicodedata import normalize

# ── Configuração ──────────────────────────────────────────────────────────────

API_KEY  = "3becff7d26aacf2cac438789d75cf39d8e99c9306a2a0c34bc51711bbd41dc36"
BASE_URL = "https://culkin.mygri.com/api/mcp/analytics"

EVENTS = {
    "mx_re":  {"id": "a02Np00001HD3NaIAL", "name": "Mexico GRI Real Estate 2026",        "marker": "MX_RE"},
    "hosp":   {"id": "a02Np00001HZx30IAD", "name": "GRI Hospitality MX & CA 2026",       "marker": "HOSP"},
    "andean": {"id": "a02Np00001HCyPWIA1", "name": "Andean & Central America GRI 2026",  "marker": "ANDEAN"},
}

SESSION_TYPES = ("'Talkshow'", "'Discussion'", "'Keynote'")

def today_str() -> str:
    return date.today().isoformat()


# ── HTTP via curl ─────────────────────────────────────────────────────────────

def call_analytics(view_name: str, where: str = "", limit: int = 300) -> list:
    """Chama a API Culkin Analytics via curl e retorna lista de rows."""
    arguments = {"view_name": view_name, "limit": limit}
    if where:
        arguments["where"] = where  # FIX: parâmetro correto é "where", não "where_clause"

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "query_analytics_view",
            "arguments": arguments,
        },
    }

    cmd = [
        "curl", "-s", "-X", "POST",
        BASE_URL,
        "-H", "Content-Type: application/json",
        "-H", f"X-Api-Key: {API_KEY}",
        "-d", json.dumps(payload),
        "--max-time", "60",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=65)
        if result.returncode != 0:
            print(f"  ❌ curl erro (code {result.returncode}): {result.stderr[:200]}")
            return []
    except subprocess.TimeoutExpired:
        print("  ❌ Timeout ao chamar a API.")
        return []
    except FileNotFoundError:
        print("  ❌ curl não encontrado no sistema.")
        return []

    raw = result.stdout.strip()
    if not raw:
        print("  ❌ Resposta vazia da API.")
        return []

    try:
        resp = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  ❌ Resposta não é JSON válido: {e}\n     Início: {raw[:200]}")
        return []

    # Extrai rows do formato MCP
    try:
        content = resp.get("result", {}).get("content", [])
        if not content:
            err = resp.get("error") or resp.get("result", {}).get("error")
            if err:
                print(f"  ❌ Erro da API: {err}")
            return []
        text_block = next((c for c in content if c.get("type") == "text"), None)
        if not text_block:
            return []
        data = json.loads(text_block["text"])
        rows = data if isinstance(data, list) else data.get("rows", data.get("data", []))
        return rows
    except Exception as e:
        print(f"  ❌ Erro ao parsear resposta: {e}")
        return []


# ── Fetch de dados do evento ──────────────────────────────────────────────────

def fetch_event_data(event_id: str) -> tuple:
    """Retorna (cochair_rows, wl_rows, declined_rows) para um evento."""
    types_clause = f"sessiontype IN ({', '.join(SESSION_TYPES)})"
    base_filter  = (
        f"eventid = '{event_id}' "
        f"AND {types_clause} "
        f"AND sessiontitle NOT LIKE '%Some of the%' "
        f"AND session_position__c = 'Co-chair'"
    )

    print("    → Buscando co-chairs (Confirmed + Tentative)...")
    cochair_rows = call_analytics(
        "mv_wishlist_snapshot",
        where=f"{base_filter} AND session_status__c IN ('Confirmed', 'Tentative')",
        limit=300,
    )

    print("    → Buscando wishlist pipeline...")
    wl_rows = call_analytics(
        "mv_wishlist_snapshot",
        where=f"{base_filter} AND session_status__c = 'Wish List'",
        limit=300,
    )

    print("    → Buscando declined...")
    declined_rows = call_analytics(
        "mv_wishlist_snapshot",
        where=f"{base_filter} AND session_status__c = 'Declined'",
        limit=300,
    )

    return cochair_rows, wl_rows, declined_rows


# ── Agregação de sessões ──────────────────────────────────────────────────────

def normalize_str(s: str) -> str:
    """Normaliza string: remove acentos, pontuação especial (¿¡), lowercase."""
    s = normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[¿¡!?]", "", s)
    return s.lower().strip()


def build_session_summary(cochair_rows, wl_rows, declined_rows) -> tuple:
    """Agrupa rows por sessão e conta co-chairs únicos."""
    sessions    = {}
    seen_keys   = set()  # (sessionid, contactid) para dedup

    def add_rows(rows, status):
        for r in rows:
            sid  = r.get("sessionid", "")
            title= r.get("sessiontitle", "")
            cid  = r.get("contactid", "")
            name = r.get("contact", "")
            if not sid:
                continue
            if sid not in sessions:
                sessions[sid] = {
                    "title": title,
                    "confirmed": 0, "tentative": 0, "wl": 0, "declined": 0,
                    "confirmed_names": [], "tentative_names": [],
                }
            key = (sid, cid)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            if status == "Confirmed":
                sessions[sid]["confirmed"] += 1
                if name:
                    sessions[sid]["confirmed_names"].append(name)
            elif status == "Tentative":
                sessions[sid]["tentative"] += 1
                if name:
                    sessions[sid]["tentative_names"].append(name)
            elif status == "Wish List":
                sessions[sid]["wl"] += 1
            elif status == "Declined":
                sessions[sid]["declined"] += 1

    for r in cochair_rows:
        add_rows([r], r.get("session_status__c", ""))
    add_rows(wl_rows, "Wish List")
    add_rows(declined_rows, "Declined")

    total_conf = sum(v["confirmed"] for v in sessions.values())
    total_tent = sum(v["tentative"] for v in sessions.values())
    total_wl   = sum(v["wl"]        for v in sessions.values())

    return sessions, total_conf, total_tent, total_wl


# ── Match fuzzy de sessões ────────────────────────────────────────────────────

def fuzzy_score(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_str(a), normalize_str(b)).ratio()


def match_and_update_sessions(html_sessions: list, sf_sessions: dict) -> list:
    """Faz match fuzzy entre sessões do HTML e do SF e atualiza campos."""
    updated   = []
    used_sids = set()

    for hs in html_sessions:
        html_title = hs.get("title", "")
        best_sid   = None
        best_score = 0.0

        for sid, ss in sf_sessions.items():
            if sid in used_sids:
                continue
            score = fuzzy_score(html_title, ss["title"])
            if score > best_score:
                best_score = score
                best_sid   = sid

        if best_sid and best_score >= 0.40:
            ss = sf_sessions[best_sid]
            used_sids.add(best_sid)

            hs["confirmed"] = ss["confirmed"]
            hs["tentative"] = ss["tentative"]
            hs["wl"]        = ss["wl"]
            hs["declined"]  = ss.get("declined", hs.get("declined", 0))
            if ss["confirmed_names"]:
                hs["confirmed_names"] = ss["confirmed_names"]
            if ss["tentative_names"]:
                hs["tentative_names"] = ss["tentative_names"]

            match_pct = f"{best_score:.0%}"
            print(f"      ✓ [{match_pct}] '{ss['title'][:60]}'")
        else:
            score_str = f"{best_score:.0%}" if best_sid else "—"
            print(f"      ⚠ Sem match ({score_str}): '{html_title[:60]}'")

        updated.append(hs)

    return updated


# ── Patching do HTML ──────────────────────────────────────────────────────────

def extract_sessions_block(html: str, marker: str) -> tuple:
    start_tag = f"/*%%SYNC_{marker}_SESSIONS_START%%*/"
    end_tag   = f"/*%%SYNC_{marker}_SESSIONS_END%%*/"
    s = html.find(start_tag)
    e = html.find(end_tag)
    if s == -1 or e == -1:
        return None, -1, -1
    content_start = s + len(start_tag)
    content       = html[content_start:e]
    m = re.search(r"const\s+SESSIONS\s*=\s*(\[[\s\S]*?\]);", content)
    if not m:
        return None, -1, -1
    return m.group(1), content_start + m.start(1), content_start + m.end(1)


def patch_html(html: str, event_key: str, sf_sessions: dict,
               total_conf: int, total_tent: int, total_wl: int) -> str:
    marker = EVENTS[event_key]["marker"]
    json_str, idx_start, idx_end = extract_sessions_block(html, marker)
    if json_str is None:
        print(f"  ⚠ Marcador SYNC_{marker}_SESSIONS não encontrado — pulando.")
        return html
    try:
        html_sessions = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"  ❌ Erro ao parsear SESSIONS: {e}")
        return html
    print(f"  Match: {len(html_sessions)} sessões HTML ↔ {len(sf_sessions)} sessões SF...")
    updated_sessions = match_and_update_sessions(html_sessions, sf_sessions)
    new_json = json.dumps(updated_sessions, ensure_ascii=False, separators=(",", ":"))
    html = html[:idx_start] + new_json + html[idx_end:]
    return html


def add_sync_markers(html: str) -> str:
    SIGNATURES = {
        "MX_RE":  'const SESSIONS = [{"stream":"Opening","title":"Inversiones Inmob',
        "HOSP":   'const SESSIONS = [{"stream":"Opening","title":"Mercado Hotelero en M',
        "ANDEAN": 'const SESSIONS = [{"stream":"Opening","title":"Apertura',
    }
    for marker, sig in SIGNATURES.items():
        start_tag = f"/*%%SYNC_{marker}_SESSIONS_START%%*/"
        if start_tag in html:
            continue
        idx = html.find(sig)
        if idx == -1:
            print(f"  ⚠ Assinatura para {marker} não encontrada.")
            continue
        const_start  = html.rfind("const SESSIONS", 0, idx + len(sig))
        if const_start == -1:
            continue
        open_bracket = html.find("[", const_start)
        if open_bracket == -1:
            continue
        depth = 0
        i = open_bracket
        while i < len(html):
            if html[i] == "[":
                depth += 1
            elif html[i] == "]":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        array_end = i + 1
        semicolon = html.find(";", array_end)
        stmt_end  = semicolon + 1 if semicolon != -1 and semicolon < array_end + 5 else array_end
        end_tag = f"/*%%SYNC_{marker}_SESSIONS_END%%*/"
        html = (
            html[:const_start]
            + start_tag
            + "const SESSIONS = "
            + html[open_bracket:stmt_end]
            + end_tag
            + html[stmt_end:]
        )
        print(f"  ✓ Marcador adicionado para {marker}")
    return html


# ── Patch do bloco hub events ─────────────────────────────────────────────────

HUB_EVENT_NAMES = {
    "hosp":   "GRI Hospitality MX & CA 2026",
    "mx_re":  "Mexico GRI Real Estate 2026",
    "andean": "Andean & Central America GRI 2026",
}

def patch_hub_events(html: str, totals: dict) -> str:
    start_tag = "/*%%SYNC_HUB_EVENTS_START%%*/"
    end_tag   = "/*%%SYNC_HUB_EVENTS_END%%*/"
    s = html.find(start_tag)
    e = html.find(end_tag)
    if s == -1 or e == -1:
        print("  ⚠ Marcador HUB_EVENTS não encontrado — pulando.")
        return html
    block = html[s + len(start_tag):e]
    for key, (total_conf, total_tent) in totals.items():
        name = HUB_EVENT_NAMES.get(key, "")
        if not name:
            continue
        name_idx = block.find(f"name:'{name}'")
        if name_idx == -1:
            print(f"  ⚠ Evento '{name}' não encontrado no hub block.")
            continue
        obj_end = block.find("},", name_idx)
        if obj_end == -1:
            obj_end = block.find("}", name_idx)
        if obj_end == -1:
            continue
        obj = block[name_idx:obj_end + 1]
        obj = re.sub(r"coChairConf:\d+", f"coChairConf:{total_conf}", obj)
        obj = re.sub(r"coChairTent:\d+", f"coChairTent:{total_tent}", obj)
        obj = re.sub(r"lastUpdate:'[\d-]+'", f"lastUpdate:'{today_str()}'", obj)
        block = block[:name_idx] + obj + block[obj_end + 1:]
        print(f"  ✓ Hub card '{name}': {total_conf}C / {total_tent}T")
    return html[:s + len(start_tag)] + block + html[e:]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) > 1:
        html_path = sys.argv[1]
    else:
        candidates = [
            "gri_global_hub.html",
            os.path.expanduser("~/Downloads/gri_global_hub.html"),
        ]
        html_path = next((p for p in candidates if os.path.exists(p)), None)
        if not html_path:
            print("❌ Arquivo HTML não encontrado. Passe o caminho como argumento:")
            print("   python3 sync_dashboard.py caminho/para/gri_global_hub.html")
            sys.exit(1)

    print(f"\n🔄 GRI Dashboard Sync — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print(f"   Arquivo: {html_path}\n")

    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    print("📌 Verificando marcadores de sync...")
    html = add_sync_markers(html)

    hub_totals = {}

    for key, ev in EVENTS.items():
        print(f"\n📊 {ev['name']}")
        print(f"   SF ID: {ev['id']}")

        cochair_rows, wl_rows, declined_rows = fetch_event_data(ev["id"])

        if not cochair_rows and not wl_rows:
            print("   ℹ Sem dados retornados — pulando.")
            hub_totals[key] = (0, 0)
            continue

        sf_sessions, total_conf, total_tent, total_wl = build_session_summary(
            cochair_rows, wl_rows, declined_rows
        )

        print(f"   Resumo: {total_conf} confirmed · {total_tent} tentative · {total_wl} wishlist")
        print(f"   Sessões SF: {len(sf_sessions)}")

        html = patch_html(html, key, sf_sessions, total_conf, total_tent, total_wl)
        hub_totals[key] = (total_conf, total_tent)

    print("\n🃏 Atualizando hub cards...")
    html = patch_hub_events(html, hub_totals)

    # Backup antes de sobrescrever
    backup_path = html_path.replace(".html", f"_backup_{date.today().isoformat()}.html")
    if not os.path.exists(backup_path):
        with open(html_path, "r", encoding="utf-8") as f:
            original = f.read()
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(original)
        print(f"\n💾 Backup salvo em: {backup_path}")

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅ Dashboard atualizado com sucesso!")
    print(f"   {html_path}")
    print(f"   Atualizado em: {datetime.now().strftime('%d/%m/%Y às %H:%M')}\n")


if __name__ == "__main__":
    main()
