#!/usr/bin/env python3
"""
GRI Dashboard Sync — v5
Puxa dados do Culkin Analytics (Salesforce) e atualiza gri_global_hub.html.
Usa curl via subprocess para evitar problemas de SSL do LibreSSL no macOS.
v5: auto-descoberta de eventos LatAm + europeus via mv_streamlit_opportunities.
     Não requer IDs fixos no código — basta configurar business_units/division_codes.
     API key via variável de ambiente CULKIN_API_KEY.

Uso:
    CULKIN_API_KEY=<key> python3 sync_dashboard.py [caminho/para/gri_global_hub.html]
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

API_KEY  = os.environ.get("CULKIN_API_KEY", "")
BASE_URL = "https://culkin.mygri.com/api/mcp/analytics"

# Mapeamento keyword → marcador HTML existente para eventos LatAm conhecidos.
# Adicione linhas aqui quando criar novos cards no dashboard (sem precisar de IDs do SF).
LATAM_KNOWN_EVENTS = {
    "MX_RE":  {"keywords": ["mexico gri real estate"],                   "dash_id": "dash-mexico"},
    "HOSP":   {"keywords": ["hospitality mexico", "hospitality central"], "dash_id": "dash-hospitality"},
    "ANDEAN": {"keywords": ["andean"],                                    "dash_id": "dash-andean"},
}

# Critérios de auto-descoberta para eventos LatAm (Open Events confirmados de 2026)
LATAM_DISCOVERY = {
    "event_type":   "Open Event",
    "event_status": "Confirmed",
    "event_year":   "2026",
    "business_units": [
        "Mexico", "Andean", "Colombia", "Peru", "Chile",
        "Brazil Real Estate", "Central America", "Pan Latam RE",
    ],
}

# Critérios de auto-descoberta para eventos europeus
EUROPE_DISCOVERY = {
    "event_type":             "Open Event",
    "event_status":           "Confirmed",
    "event_year":             "2026",
    "division_codes":         ["Europe - SWE", "Europe - NCEE"],
    # Exclui business_units não-europeus que aparecem nestas divisions
    "exclude_business_units": ["Brazil Real Estate", "India RE", "GCC"],
}

SESSION_TYPES = ("'Talkshow'", "'Discussion'", "'Keynote'")

MONTHS_NUM = {
    "January":"01","February":"02","March":"03","April":"04",
    "May":"05","June":"06","July":"07","August":"08",
    "September":"09","October":"10","November":"11","December":"12",
}
MONTHS_ABBR = {
    "January":"Jan","February":"Feb","March":"Mar","April":"Apr",
    "May":"May","June":"Jun","July":"Jul","August":"Aug",
    "September":"Sep","October":"Oct","November":"Nov","December":"Dec",
}


def today_str() -> str:
    return date.today().isoformat()


# ── HTTP via curl ─────────────────────────────────────────────────────────────

def call_analytics(view_name: str, where: str = "", limit: int = 300) -> list:
    """Chama a API Culkin Analytics via curl e retorna lista de rows."""
    arguments = {"view_name": view_name, "limit": limit}
    if where:
        arguments["where"] = where

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


# ── Helpers de nomenclatura ───────────────────────────────────────────────────

def name_to_marker(name: str) -> str:
    """Converte nome de evento em marcador uppercase.
    Ex: 'GRI Living Assets Europe 2026' → 'EU_LIVING_ASSETS'
    """
    stop = {"gri", "2026", "europe", "edition", "the", "and", "open", "event", "&"}
    words = re.sub(r"[^a-zA-Z0-9\s]", " ", name).split()
    words = [w.upper() for w in words if w.lower() not in stop and len(w) > 1]
    return "EU_" + "_".join(words[:3])


def name_to_dash_id(name: str) -> str:
    """Converte nome de evento em ID de div HTML.
    Ex: 'GRI Living Assets Europe 2026' → 'dash-eu-living-assets'
    """
    stop = {"gri", "2026", "edition", "the", "and", "&"}
    words = re.sub(r"[^a-zA-Z0-9\s]", " ", name.lower()).split()
    words = [w for w in words if w not in stop and len(w) > 1]
    return "dash-" + "-".join(words[:4])


def parse_event_date(date_str: str) -> tuple:
    """Converte string de data em (display, iso).
    Ex: '9 - 10 September 2026' → ('Sep 9, 2026', '2026-09-09')
    """
    # Remove a parte do range (ex: "9 - 10" → "9")
    date_str = re.sub(r"(\d+)\s*-\s*\d+\s+", r"\1 ", date_str.strip())
    m = re.match(r"(\d+)\s+(\w+)\s+(\d{4})", date_str)
    if m:
        day, month_name, year = m.groups()
        abbr = MONTHS_ABBR.get(month_name, month_name[:3])
        num  = MONTHS_NUM.get(month_name, "01")
        display = f"{abbr} {int(day)}, {year}"
        iso     = f"{year}-{num}-{day.zfill(2)}"
        return display, iso
    return date_str, ""


def pick_emoji(name: str, business_unit: str) -> str:
    """Escolhe emoji representativo com base no nome/business unit do evento."""
    n, bu = name.lower(), business_unit.lower()
    if "espana" in bu or "ibero" in n or "spain" in n: return "🇪🇸"
    if "portugal" in bu or "portugal" in n:             return "🇵🇹"
    if "deutsche" in bu or "german" in n:               return "🇩🇪"
    if "hospitality" in n or "hotel" in n:              return "🏨"
    if "living" in n or "residential" in n:             return "🏠"
    if "credit" in n or "debt" in n:                    return "💳"
    if "data centre" in n or "commercial" in n:         return "🏢"
    if "logistics" in n or "industrial" in n:           return "🏭"
    if "retail" in n:                                   return "🛍️"
    if "infrastructure" in n:                           return "🔧"
    return "🇪🇺"


# ── Auto-descoberta de eventos europeus ──────────────────────────────────────

def discover_latam_events() -> list:
    """Consulta mv_streamlit_opportunities e retorna eventos LatAm únicos (Open Events 2026 Confirmed)."""
    cfg    = LATAM_DISCOVERY
    bus_str = "', '".join(cfg["business_units"])
    where = (
        f"event_type = '{cfg['event_type']}' "
        f"AND event_status = '{cfg['event_status']}' "
        f"AND event_year = '{cfg['event_year']}' "
        f"AND business_unit IN ('{bus_str}')"
    )
    rows = call_analytics("mv_streamlit_opportunities", where=where, limit=500)

    seen = {}
    for r in rows:
        eid = r.get("sf_event_id", "")
        if not eid or eid in seen:
            continue
        seen[eid] = {
            "id":            eid,
            "name":          r.get("event_name", ""),
            "date":          r.get("event_date", ""),
            "division_code": r.get("division_code", ""),
            "business_unit": r.get("business_unit", ""),
        }
    return list(seen.values())


def assign_latam_marker(ev_name: str) -> tuple:
    """Faz match do nome do evento com um marcador HTML existente (LATAM_KNOWN_EVENTS).
    Retorna (marker, dash_id) para eventos conhecidos, ou (None, None) para eventos novos.
    """
    name_lower = ev_name.lower()
    for marker, cfg in LATAM_KNOWN_EVENTS.items():
        if any(kw in name_lower for kw in cfg["keywords"]):
            return marker, cfg["dash_id"]
    return None, None


def discover_europe_events() -> list:
    """Consulta mv_streamlit_opportunities e retorna eventos europeus únicos."""
    cfg       = EUROPE_DISCOVERY
    div_codes = "', '".join(cfg["division_codes"])
    excl_bus  = "', '".join(cfg["exclude_business_units"])
    where = (
        f"event_type = '{cfg['event_type']}' "
        f"AND event_status = '{cfg['event_status']}' "
        f"AND event_year = '{cfg['event_year']}' "
        f"AND division_code IN ('{div_codes}') "
        f"AND business_unit NOT IN ('{excl_bus}')"
    )
    rows = call_analytics("mv_streamlit_opportunities", where=where, limit=1000)

    seen = {}
    for r in rows:
        eid = r.get("sf_event_id", "")
        if not eid or eid in seen:
            continue
        seen[eid] = {
            "id":            eid,
            "name":          r.get("event_name", ""),
            "date":          r.get("event_date", ""),
            "division_code": r.get("division_code", ""),
            "business_unit": r.get("business_unit", ""),
        }
    return list(seen.values())


# ── Geração de HTML para novos eventos ───────────────────────────────────────

def generate_event_hub_card(ev_info: dict, marker: str, dash_id: str) -> str:
    """Gera o objeto JS para inserção no array EVENTS do hub."""
    name         = ev_info["name"].replace("'", "\\'")
    date_display, date_iso = parse_event_date(ev_info.get("date", ""))
    emoji        = pick_emoji(ev_info["name"], ev_info.get("business_unit", ""))
    return (
        f"{{emoji:'{emoji}',name:'{name}',"
        f"date:'{date_display}',location:'(Venue TBD)',"
        f"accentColor:'#185FA5',coChairTarget:0,coChairConf:0,coChairTent:0,"
        f"eventDate:'{date_iso}',dashId:'{dash_id}',available:true,"
        f"lastUpdate:'{today_str()}'}}"
    )


def generate_dash_div(ev_info: dict, marker: str, dash_id: str) -> str:
    """Gera div HTML mínimo para o dashboard do evento, com marcadores de sync."""
    name = ev_info["name"]
    sm   = f"/*%%SYNC_{marker}_SESSIONS_START%%*/"
    em   = f"/*%%SYNC_{marker}_SESSIONS_END%%*/"
    cm   = f"/*%%SYNC_{marker}_COUNTRY_START%%*/"
    cem  = f"/*%%SYNC_{marker}_COUNTRY_END%%*/"
    return (
        f'\n<!-- AUTO-GENERATED: {name} -->\n'
        f'<div id="{dash_id}" class="dashboard hidden"'
        f' data-sf-event-id="{ev_info["id"]}">\n'
        f'  <script>\n'
        f'    {sm}const SESSIONS = [];{em}\n'
        f'    {cm}const COUNTRY_DATA = {{}};{cem}\n'
        f'  </script>\n'
        f'</div>'
    )


def inject_hub_card(html: str, card_js: str, event_name: str) -> str:
    """Injeta novo card no array EVENTS do hub (dentro dos marcadores HUB_EVENTS)."""
    start_tag = "/*%%SYNC_HUB_EVENTS_START%%*/"
    end_tag   = "/*%%SYNC_HUB_EVENTS_END%%*/"
    s = html.find(start_tag)
    e = html.find(end_tag)
    if s == -1 or e == -1:
        print(f"  ⚠ Marcador HUB_EVENTS não encontrado — não foi possível injetar '{event_name}'.")
        return html
    block      = html[s + len(start_tag):e]
    safe_name  = event_name.replace("'", "\\'")
    if f"name:'{safe_name}'" in block:
        return html  # já presente
    last_obj = block.rfind("}")
    if last_obj == -1:
        return html
    block = block[:last_obj + 1] + f",\n    {card_js}" + block[last_obj + 1:]
    print(f"  ✓ Hub card injetado: '{event_name}'")
    return html[:s + len(start_tag)] + block + html[e:]


def inject_dash_div(html: str, dash_div: str, dash_id: str) -> str:
    """Injeta div do dashboard antes de </body> se não existir ainda."""
    if f'id="{dash_id}"' in html:
        return html
    idx = html.rfind("</body>")
    if idx == -1:
        return html
    return html[:idx] + dash_div + "\n" + html[idx:]


# ── Fetch de dados do evento ──────────────────────────────────────────────────

def fetch_event_data(event_id: str) -> tuple:
    types_clause = f"sessiontype IN ({', '.join(SESSION_TYPES)})"
    base_filter  = (
        f"eventid = '{event_id}' "
        f"AND {types_clause} "
        f"AND sessiontitle NOT LIKE '%Some of the%' "
        f"AND session_position__c IN ('Co-chair', 'Moderator', 'Keynote', 'Provocateur')"
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
    s = normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[¿¡!?]", "", s)
    return s.lower().strip()


def build_session_summary(cochair_rows, wl_rows, declined_rows) -> tuple:
    sessions  = {}
    seen_keys = set()

    def add_rows(rows, status):
        for r in rows:
            sid   = r.get("sessionid", "")
            title = r.get("sessiontitle", "")
            cid   = r.get("contactid", "")
            name  = r.get("contact", "")
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
                if name: sessions[sid]["confirmed_names"].append(name)
            elif status == "Tentative":
                sessions[sid]["tentative"] += 1
                if name: sessions[sid]["tentative_names"].append(name)
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
            if ss["confirmed_names"]: hs["confirmed_names"] = ss["confirmed_names"]
            if ss["tentative_names"]: hs["tentative_names"] = ss["tentative_names"]
            print(f"      ✓ [{best_score:.0%}] '{ss['title'][:60]}'")
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
    content = html[content_start:e]
    m = re.search(r"const\s+SESSIONS\s*=\s*(\[[\s\S]*?\]);", content)
    if not m:
        return None, -1, -1
    return m.group(1), content_start + m.start(1), content_start + m.end(1)


def patch_html(html: str, marker: str, sf_sessions: dict,
               total_conf: int, total_tent: int, total_wl: int) -> str:
    """Atualiza o bloco SESSIONS de um evento no HTML via marcadores."""
    json_str, idx_start, idx_end = extract_sessions_block(html, marker)
    if json_str is None:
        print(f"  ⚠ Marcador SYNC_{marker}_SESSIONS não encontrado — pulando.")
        return html
    try:
        html_sessions = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"  ❌ Erro ao parsear SESSIONS: {e}")
        return html
    if not html_sessions:
        print(f"  ℹ SESSIONS vazio — aguardando definição manual das sessões.")
        return html
    print(f"  Match: {len(html_sessions)} sessões HTML ↔ {len(sf_sessions)} sessões SF...")
    updated_sessions = match_and_update_sessions(html_sessions, sf_sessions)
    new_json = json.dumps(updated_sessions, ensure_ascii=False, separators=(",", ":"))
    html = html[:idx_start] + new_json + html[idx_end:]
    return html


def add_sync_markers(html: str) -> str:
    """Injeta marcadores de sync nos eventos LatAm se ainda não existirem."""
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
            if html[i] == "[":   depth += 1
            elif html[i] == "]":
                depth -= 1
                if depth == 0: break
            i += 1
        array_end = i + 1
        semicolon = html.find(";", array_end)
        stmt_end  = semicolon + 1 if semicolon != -1 and semicolon < array_end + 5 else array_end
        end_tag = f"/*%%SYNC_{marker}_SESSIONS_END%%*/"
        html = (
            html[:const_start]
            + start_tag + "const SESSIONS = " + html[open_bracket:stmt_end]
            + end_tag + html[stmt_end:]
        )
        print(f"  ✓ Marcador adicionado para {marker}")
    return html


# ── Patch do bloco hub events ─────────────────────────────────────────────────

def patch_hub_events(html: str, totals: dict, event_names: dict) -> str:
    """Atualiza coChairConf, coChairTent e lastUpdate no array EVENTS do hub."""
    start_tag = "/*%%SYNC_HUB_EVENTS_START%%*/"
    end_tag   = "/*%%SYNC_HUB_EVENTS_END%%*/"
    s = html.find(start_tag)
    e = html.find(end_tag)
    if s == -1 or e == -1:
        print("  ⚠ Marcador HUB_EVENTS não encontrado — pulando.")
        return html
    block = html[s + len(start_tag):e]
    for key, (total_conf, total_tent) in totals.items():
        name = event_names.get(key, "")
        if not name:
            continue
        safe_name = name.replace("'", "\\'")
        name_idx  = block.find(f"name:'{safe_name}'")
        if name_idx == -1:
            print(f"  ⚠ Evento '{name}' não encontrado no hub block.")
            continue
        obj_end = block.find("},", name_idx)
        if obj_end == -1: obj_end = block.find("}", name_idx)
        if obj_end == -1: continue
        obj = block[name_idx:obj_end + 1]
        obj = re.sub(r"coChairConf:\d+",      f"coChairConf:{total_conf}",   obj)
        obj = re.sub(r"coChairTent:\d+",      f"coChairTent:{total_tent}",   obj)
        obj = re.sub(r"lastUpdate:'[\d-]+'",  f"lastUpdate:'{today_str()}'", obj)
        block = block[:name_idx] + obj + block[obj_end + 1:]
        print(f"  ✓ Hub card '{name}': {total_conf}C / {total_tent}T")
    return html[:s + len(start_tag)] + block + html[e:]


# ── Country Data ─────────────────────────────────────────────────────────────

def fetch_country_data(event_name: str) -> dict:
    """Busca co-chairs confirmados por país no mv_members_future_events."""
    print(f"    → Buscando co-chairs por país (mv_members_future_events)...")
    safe_name = event_name.replace("'", "''")
    rows = call_analytics(
        "mv_members_future_events",
        where=f"event_name = '{safe_name}' AND session_status = 'Co-chair - Confirmed'",
        limit=500,
    )
    if not rows:
        print("    ℹ Sem dados de país retornados.")
        return {}
    from collections import Counter
    counts = Counter()
    for r in rows:
        country = (r.get("mailingcountry") or "").strip()
        if country:
            counts[country] += 1
    result = dict(counts.most_common())
    print(f"    País breakdown: { {k: v for k, v in result.items()} }")
    return result


def patch_country_data(html: str, marker: str, country_data: dict) -> str:
    """Atualiza COUNTRY_DATA entre os marcadores SYNC_{marker}_COUNTRY."""
    start_tag = f"/*%%SYNC_{marker}_COUNTRY_START%%*/"
    end_tag   = f"/*%%SYNC_{marker}_COUNTRY_END%%*/"
    s = html.find(start_tag)
    e = html.find(end_tag)
    if s == -1 or e == -1:
        print(f"  ⚠ Marcador SYNC_{marker}_COUNTRY não encontrado — pulando country data.")
        return html
    new_block = (
        f"{start_tag}\n"
        f"const COUNTRY_DATA = {json.dumps(country_data, ensure_ascii=False)};\n"
        f"{end_tag}"
    )
    html = html[:s] + new_block + html[e + len(end_tag):]
    print(f"  ✓ COUNTRY_DATA atualizado para {marker}: {country_data}")
    return html


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

    # ── 1. Marcadores de sync para eventos LatAm conhecidos ─────────────────
    print("📌 Verificando marcadores de sync...")
    html = add_sync_markers(html)

    # ── 2. Auto-descoberta de eventos LatAm ──────────────────────────────────
    print("\n🔍 Auto-descoberta de eventos LatAm...")
    latam_events = discover_latam_events()
    print(f"   {len(latam_events)} evento(s) LatAm encontrado(s) no SF.")

    # ── 3. Auto-descoberta de eventos europeus ───────────────────────────────
    print("\n🔍 Auto-descoberta de eventos europeus...")
    europe_events = discover_europe_events()
    print(f"   {len(europe_events)} evento(s) europeu(s) encontrado(s) no SF.")

    # Monta dict unificado: LatAm + Europa (sem duplicatas por SF ID)
    all_events  = {}
    seen_ids    = set()

    for ev_info in latam_events:
        eid = ev_info["id"]
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        marker, dash_id = assign_latam_marker(ev_info["name"])
        if marker:
            # Evento LatAm conhecido — usa marcador/dash_id do HTML existente
            key = marker.lower()
            all_events[key] = {
                "id":      eid,
                "name":    ev_info["name"],
                "marker":  marker,
                "dash_id": dash_id,
                "auto":    False,
            }
        else:
            # Evento LatAm novo — gera marcador e div automaticamente
            marker  = name_to_marker(ev_info["name"]).replace("EU_", "LAT_")
            dash_id = name_to_dash_id(ev_info["name"])
            key     = marker.lower()
            all_events[key] = {
                "id":      eid,
                "name":    ev_info["name"],
                "marker":  marker,
                "dash_id": dash_id,
                "auto":    True,
                "ev_info": ev_info,
            }

    for ev_info in europe_events:
        eid = ev_info["id"]
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        marker  = name_to_marker(ev_info["name"])
        dash_id = name_to_dash_id(ev_info["name"])
        key     = marker.lower()
        all_events[key] = {
            "id":      eid,
            "name":    ev_info["name"],
            "marker":  marker,
            "dash_id": dash_id,
            "auto":    True,
            "ev_info": ev_info,
        }

    # ── 4. Injeta HTML para eventos novos (não presentes no arquivo ainda) ───
    new_count = 0
    for key, ev in all_events.items():
        if not ev.get("auto"):
            continue
        dash_id = ev["dash_id"]
        marker  = ev["marker"]
        if f'id="{dash_id}"' in html:
            continue  # já existe no HTML
        print(f"\n🆕 Novo evento detectado: {ev['name']}")
        card_js  = generate_event_hub_card(ev["ev_info"], marker, dash_id)
        dash_div = generate_dash_div(ev["ev_info"], marker, dash_id)
        html = inject_hub_card(html, card_js, ev["name"])
        html = inject_dash_div(html, dash_div, dash_id)
        new_count += 1

    if new_count:
        print(f"\n   ✓ {new_count} novo(s) evento(s) injetado(s) no HTML.")
    else:
        print("   ✓ Nenhum evento novo — HTML já está atualizado.")

    # ── 5. Sync de dados de co-chairs para todos os eventos ──────────────────
    hub_totals  = {}
    event_names = {}

    for key, ev in all_events.items():
        print(f"\n📊 {ev['name']}")
        print(f"   SF ID: {ev['id']}")
        cochair_rows, wl_rows, declined_rows = fetch_event_data(ev["id"])
        if not cochair_rows and not wl_rows:
            print("   ℹ Sem dados retornados — pulando.")
            hub_totals[key]  = (0, 0)
            event_names[key] = ev["name"]
            continue
        sf_sessions, total_conf, total_tent, total_wl = build_session_summary(
            cochair_rows, wl_rows, declined_rows
        )
        print(f"   Resumo: {total_conf} confirmed · {total_tent} tentative · {total_wl} wishlist")
        print(f"   Sessões SF: {len(sf_sessions)}")
        html = patch_html(html, ev["marker"], sf_sessions, total_conf, total_tent, total_wl)
        hub_totals[key]  = (total_conf, total_tent)
        event_names[key] = ev["name"]

    # ── 6. Atualiza co-chairs por país (todos os eventos) ────────────────────
    # Qualquer evento com marcador /*%%SYNC_{MARKER}_COUNTRY_START%%*/ no HTML
    # terá seus dados de país atualizados automaticamente.
    print("\n🌍 Atualizando co-chairs por país...")
    for key, ev in all_events.items():
        country_marker = ev["marker"]
        start_tag = f"/*%%SYNC_{country_marker}_COUNTRY_START%%*/"
        if start_tag not in html:
            continue  # evento sem marcador de país — pula silenciosamente
        print(f"  {ev['name']}")
        country_data = fetch_country_data(ev["name"])
        if country_data:
            html = patch_country_data(html, country_marker, country_data)

    # ── 7. Atualiza hub cards (totais) ───────────────────────────────────────
    print("\n🃏 Atualizando hub cards...")
    html = patch_hub_events(html, hub_totals, event_names)

    # ── 8. Salva com backup ──────────────────────────────────────────────────
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
