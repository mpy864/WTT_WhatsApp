#!/usr/bin/env python3
import os, re, sys
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

# =========================
# Config / Env
# =========================
SCRIPT_VERSION = "no-eid-v7-excel-mandatory"

# Load .env next to this script; DO NOT override env vars (so GitHub Secrets win)
load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=False)

# Twilio (prefer API key; fallback to SID+Token)
ACCOUNT_SID    = os.getenv("TWILIO_ACCOUNT_SID", "")
AUTH_TOKEN     = os.getenv("TWILIO_AUTH_TOKEN", "")
API_KEY_SID    = os.getenv("TWILIO_API_KEY_SID", "")
API_KEY_SECRET = os.getenv("TWILIO_API_KEY_SECRET", "")

FROM_WHATSAPP  = os.getenv("TWILIO_WHATSAPP_FROM", "")
TO_WHATSAPP    = os.getenv("WHATSAPP_TO", "")

# Mandatory Excel (EventId → {EventType, Country})
EVENTS_XLSX = os.getenv("EVENTS_XLSX", "TT_Events_2021-2025.xlsx")

# =========================
# WTT endpoints
# =========================
APPSETTING_URL = "https://wtt-website-api-prod-3-frontdoor-bddnb2haduafdze9.a01.azurefd.net/api/cms/GetAppSetting/completed_results_page_event_id"
STATIC_ROOT    = "https://wtt-web-frontdoor-withoutcache-cqakg0andqf5hchn.a01.azurefd.net/websitestaticapifiles"
LIVE_API       = "https://wtt-website-live-events-api-prod-cmfzgabgbzhphabb.eastasia-01.azurewebsites.net/api/cms/GetOfficialResult"
TAKE_TRY       = [200, 100, 50, 20, 10]

# =========================
# HTTP helpers
# =========================
def _utc_now_iso_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def _headers(no_cache=False) -> Dict[str, str]:
    h = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.worldtabletennis.com",
        "Referer": "https://www.worldtabletennis.com/",
        "User-Agent": "Mozilla/5.0",
    }
    if no_cache:
        h["Cache-Control"] = "no-cache"
        h["Pragma"] = "no-cache"
    return h

# =========================
# Twilio
# =========================
def _get_twilio_client() -> Client:
    if API_KEY_SID and API_KEY_SECRET and ACCOUNT_SID:
        return Client(API_KEY_SID, API_KEY_SECRET, account_sid=ACCOUNT_SID)
    if ACCOUNT_SID and AUTH_TOKEN:
        return Client(ACCOUNT_SID, AUTH_TOKEN)
    raise RuntimeError(
        "Twilio creds missing. Set (TWILIO_API_KEY_SID,TWILIO_API_KEY_SECRET,TWILIO_ACCOUNT_SID) "
        "or (TWILIO_ACCOUNT_SID,TWILIO_AUTH_TOKEN)."
    )

def send_whatsapp(body: str) -> Optional[str]:
    if not FROM_WHATSAPP or not TO_WHATSAPP:
        raise RuntimeError("Missing WhatsApp numbers. Set TWILIO_WHATSAPP_FROM and WHATSAPP_TO.")
    to_num = TO_WHATSAPP if TO_WHATSAPP.startswith("whatsapp:") else "whatsapp:" + TO_WHATSAPP
    client = _get_twilio_client()
    try:
        msg = client.messages.create(from_=FROM_WHATSAPP, to=to_num, body=body)
        return msg.sid
    except TwilioRestException as e:
        if getattr(e, "status", None) == 429:
            print("Twilio daily cap reached (429); skipping send.")
            return None
        print(f"Twilio error ({getattr(e, 'status', 'n/a')}): {e}")
        raise

# =========================
# Excel mapping (MANDATORY)
# =========================
def _norm(s: str) -> str:
    return re.sub(r"[\s_]+", "", s.strip().lower())

def load_event_idx_strict(xlsx_path: str) -> Dict[str, Dict[str, str]]:
    try:
        import pandas as pd
    except Exception:
        raise RuntimeError("pandas is required for Excel mapping. Install pandas/openpyxl and retry.")

    p = Path(xlsx_path)
    if not p.exists():
        raise FileNotFoundError(f"Excel file not found: {p.resolve()}")

    df = pd.read_excel(p)
    if df.empty:
        raise RuntimeError("Excel mapping is empty.")

    cols = list(df.columns)
    norm = [_norm(c) for c in cols]
    cmap: Dict[str, str] = {}

    for i, c in enumerate(norm):
        if c in ("eventid", "event_id", "id"):
            cmap["EventId"] = cols[i]
        elif c in ("eventtype", "type", "event_category", "category"):
            cmap["EventType"] = cols[i]
        elif c in ("country", "hostcountry", "nation"):
            cmap["Country"] = cols[i]

    if "EventId" not in cmap:
        raise RuntimeError("Excel must contain an EventId column.")

    def to_eid(x) -> str:
        try:
            if isinstance(x, float) and x.is_integer():
                return str(int(x))
            return str(x).strip()
        except Exception:
            return str(x)

    idx: Dict[str, Dict[str, str]] = {}
    for _, r in df.iterrows():
        eid = to_eid(r[cmap["EventId"]])
        ety = str(r.get(cmap.get("EventType", ""), "") or "")
        cty = str(r.get(cmap.get("Country", ""), "") or "")
        if eid:
            idx[eid] = {"EventType": ety, "Country": cty}

    if not idx:
        raise RuntimeError("No valid rows found in Excel mapping.")
    return idx

# =========================
# WTT fetch & parse
# =========================
def get_latest_completed_event_ids() -> List[str]:
    r = requests.get(APPSETTING_URL, params={"qc": _utc_now_iso_ms()}, headers=_headers(), timeout=20)
    r.raise_for_status()
    raw = str(r.json().get("value", "")).strip()
    ids, seen = [], set()
    for part in raw.split(","):
        eid = "".join(ch for ch in part.strip() if ch.isdigit())
        if eid and eid not in seen:
            seen.add(eid)
            ids.append(eid)
    return ids

def _static_url(eid: str, take: int) -> str:
    return f"{STATIC_ROOT}/{eid}/{eid}_take_{take}_official_results.json"

def _fetch_json(url: str, no_cache=True) -> Any:
    r = requests.get(url, headers=_headers(no_cache=no_cache), timeout=30)
    r.raise_for_status()
    return r.json()

def get_payload_static_or_live(eid: str) -> Any:
    for take in TAKE_TRY:
        try:
            return _fetch_json(_static_url(eid, take))
        except requests.HTTPError as he:
            if he.response is not None and he.response.status_code == 404:
                continue
        except Exception:
            continue
    for take in TAKE_TRY:
        try:
            r = requests.get(
                LIVE_API,
                params={"EventId": eid, "include_match_card": "true", "take": str(take), "languageCode": "en"},
                headers=_headers(),
                timeout=30,
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            continue
    raise RuntimeError(f"No payload available for Event {eid}")

# ----- Round parser -----
ROUND_LABELS = {"QF": "Quarterfinal", "SF": "Semifinal", "F": "Final"}

def pretty_round(desc: str) -> str:
    if not desc:
        return ""
    t = desc.strip()
    tl = t.lower()
    if re.search(r"\bsemi[-\s]?finals?\b", tl):
        return "Semifinal"
    if re.search(r"\bquarter[-\s]?finals?\b", tl):
        return "Quarterfinal"
    if re.search(r"\bfinal\b", tl):
        return "Final"
    m = re.search(r"\bround\s+of\s+(\d{1,3})\b", tl)
    if m:
        return f"R{m.group(1)}"
    m = re.search(r"\bR\s*(\d{1,3})\b", t, flags=re.I)
    if m:
        return f"R{m.group(1)}"
    m = re.search(r"\b(QF|SF|F)\b", t.upper())
    if m:
        return ROUND_LABELS.get(m.group(1), m.group(1))
    return ""

def _has_ind(org: str) -> bool:
    if not org:
        return False
    tokens = re.split(r"[/\s,-]+", org.strip().upper())
    return "IND" in tokens

def parse_matches(payload: Any) -> List[Dict[str, str]]:
    items = payload if isinstance(payload, list) else payload.get("matches", []) if isinstance(payload, dict) else []
    out: List[Dict[str, str]] = []

    for it in items:
        mc = it.get("match_card") or {}
        sub_event = mc.get("subEventName") or it.get("subEventType") or ""
        desc = mc.get("subEventDescription") or ""
        round_name = pretty_round(desc)

        home, away = {}, {}
        for c in mc.get("competitiors") or []:
            if c.get("competitorType") == "H":
                home = {"name": c.get("competitiorName", ""), "org": (c.get("competitiorOrg") or "").strip()}
            elif c.get("competitorType") == "A":
                away = {"name": c.get("competitiorName", ""), "org": (c.get("competitiorOrg") or "").strip()}

        h_name, h_org = home.get("name", ""), home.get("org", "")
        a_name, a_org = away.get("name", ""), away.get("org", "")

        game_scores = mc.get("resultsGameScores") or mc.get("gameScores") or ""
        overall     = mc.get("resultOverallScores") or mc.get("overallScores") or ""

        winner = ""
        m2 = re.match(r"\s*(\d+)\s*[-:]\s*(\d+)\s*$", overall) if overall else None
        if m2:
            aa, bb = int(m2.group(1)), int(m2.group(2))
            winner = h_name if aa > bb else a_name if bb > aa else ""

        out.append({
            "SubEventName": sub_event,
            "Round": round_name,
            "Comp1": h_name, "Comp1_Nation": h_org,
            "Comp2": a_name, "Comp2_Nation": a_org,
            "Match_Score": overall,
            "Games_Score": game_scores,
            "Winner": winner,
        })
    return out

# =========================
# Header from Excel (STRICT)
# =========================
def build_header_strict(eid: str, event_idx: Dict[str, Dict[str, str]]) -> str:
    if eid not in event_idx:
        raise KeyError(f"EventId {eid} not found in Excel mapping ({EVENTS_XLSX}).")
    etyp = (event_idx[eid].get("EventType") or "").strip()
    ctry = (event_idx[eid].get("Country") or "").strip()
    if not (etyp or ctry):
        raise ValueError(f"EventId {eid} has empty EventType/Country in Excel.")
    parts = [p for p in (etyp, ctry) if p]
    return f"*{' | '.join(parts)}*"

# =========================
# Build message for one event (India-only)
# =========================
def build_india_block(eid: str, event_idx: Dict[str, Dict[str, str]]) -> str:
    header = build_header_strict(eid, event_idx)  # raises if missing
    payload = get_payload_static_or_live(eid)
    matches = parse_matches(payload)

    lines: List[str] = [header]

    india: List[Dict[str, str]] = []
    for m in matches:
        if _has_ind(m["Comp1_Nation"]) or _has_ind(m["Comp2_Nation"]):
            india.append(m)

    if not india:
        lines.append("(No Indian matches found)")
        return "\n".join(lines)

    def flip_overall(s: str) -> str:
        m = re.match(r"\s*(\d+)\s*[-:]\s*(\d+)\s*$", s or "")
        return f"{m.group(2)}-{m.group(1)}" if m else (s or "")

    def flip_games(gs: str) -> str:
        parts = [p.strip() for p in (gs or "").split(",") if p.strip()]
        out = []
        for p in parts:
            m = re.match(r"(\d+)\s*[-:]\s*(\d+)$", p)
            out.append(f"{m.group(2)}-{m.group(1)}" if m else p)
        return ",".join(out)

    for m in india:
        # Row 2
        lines.append(f"{m['SubEventName']} {m['Round']}".strip())

        # Row 3
        ind1 = _has_ind(m["Comp1_Nation"])
        comp_ind = m["Comp1"] if ind1 else m["Comp2"]
        opp      = m["Comp2"] if ind1 else m["Comp1"]
        opp_nat  = m["Comp2_Nation"] if ind1 else m["Comp1_Nation"]
        opp_with = f"{opp} ({opp_nat})" if opp else f"({opp_nat})"

        if m["Winner"] == comp_ind:
            phr = f"defeated {opp_with} by"
            overall, games = m["Match_Score"], m["Games_Score"]
        elif m["Winner"] == opp:
            phr = f"lost to {opp_with} by"
            overall, games = flip_overall(m["Match_Score"]), flip_games(m["Games_Score"])
        else:
            phr = f"vs {opp_with}"
            overall, games = m["Match_Score"], m["Games_Score"]

        lines.append(f"{comp_ind} {phr} ({overall}) ({games})")
        lines.append("")

    return "\n".join(lines).rstrip()

# =========================
# Main
# =========================
def main():
    print(f"SCRIPT_VERSION: {SCRIPT_VERSION}")
    print("FROM:", repr(FROM_WHATSAPP), "TO:", repr(TO_WHATSAPP))

    # Load Excel STRICTLY
    try:
        event_idx = load_event_idx_strict(EVENTS_XLSX)
    except Exception as e:
        print(f"ERROR: Excel mapping required but not usable: {e}")
        sys.exit(2)

    eids = get_latest_completed_event_ids()
    if not eids:
        print("No completed events.")
        sys.exit(0)

    # Ensure all EventIds present in Excel
    missing = [eid for eid in eids if eid not in event_idx]
    if missing:
        print(f"ERROR: EventIds not found in Excel {EVENTS_XLSX}: {', '.join(missing)}")
        sys.exit(3)

    blocks: List[str] = []
    for eid in eids:
        try:
            blocks.append(build_india_block(eid, event_idx))
        except Exception as e:
            print(f"ERROR building block for {eid}: {e}")
            sys.exit(4)
        blocks.append("-" * 60)

    msg = "\n".join(blocks).strip()
    print("==== MESSAGE PREVIEW ====\n" + msg + "\n=========================")

    sid = send_whatsapp(msg)
    if sid:
        print("Sent via Twilio SID:", sid)

if __name__ == "__main__":
    main()
