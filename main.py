import os, re
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

# ---- version tag (visible in logs) ----
VERSION_TAG = "no-eid-v3"
print("SCRIPT_VERSION:", VERSION_TAG)

# ---- .env near script; allow CI env to override ----
load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=False)

# ---- Twilio config ----
ACCOUNT_SID    = os.getenv("TWILIO_ACCOUNT_SID", "")
AUTH_TOKEN     = os.getenv("TWILIO_AUTH_TOKEN", "")
API_KEY_SID    = os.getenv("TWILIO_API_KEY_SID", "")
API_KEY_SECRET = os.getenv("TWILIO_API_KEY_SECRET", "")
FROM_WHATSAPP  = os.getenv("TWILIO_WHATSAPP_FROM", "")
TO_WHATSAPP    = os.getenv("WHATSAPP_TO", "")

def get_twilio_client() -> Client:
    if API_KEY_SID and API_KEY_SECRET and ACCOUNT_SID:
        return Client(API_KEY_SID, API_KEY_SECRET, account_sid=ACCOUNT_SID)
    if ACCOUNT_SID and AUTH_TOKEN:
        return Client(ACCOUNT_SID, AUTH_TOKEN)
    raise RuntimeError("Twilio creds missing.")

def send_whatsapp(body: str) -> Optional[str]:
    if not FROM_WHATSAPP or not TO_WHATSAPP:
        print("Missing WhatsApp numbers; skipping send.")
        return None
    to_num = TO_WHATSAPP if TO_WHATSAPP.startswith("whatsapp:") else "whatsapp:" + TO_WHATSAPP
    try:
        client = get_twilio_client()
        msg = client.messages.create(from_=FROM_WHATSAPP, to=to_num, body=body)
        return msg.sid
    except TwilioRestException as e:
        if e.status == 429:
            print("Twilio daily cap reached (429); skipping send.")
            return None
        raise

# ---- WTT endpoints ----
APPSETTING_URL = "https://wtt-website-api-prod-3-frontdoor-bddnb2haduafdze9.a01.azurefd.net/api/cms/GetAppSetting/completed_results_page_event_id"
STATIC_ROOT    = "https://wtt-web-frontdoor-withoutcache-cqakg0andqf5hchn.a01.azurefd.net/websitestaticapifiles"
LIVE_API       = "https://wtt-website-live-events-api-prod-cmfzgabgbzhphabb.eastasia-01.azurewebsites.net/api/cms/GetOfficialResult"
GET_EVENT      = "https://wtt-website-live-events-api-prod-cmfzgabgbzhphabb.eastasia-01.azurewebsites.net/api/cms/GetEvent"
TAKE_TRY       = [200, 100, 50, 20, 10]

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
        h["Cache-Control"] = "no-cache"; h["Pragma"] = "no-cache"
    return h

def get_latest_completed_event_ids() -> List[str]:
    r = requests.get(APPSETTING_URL, params={"qc": _utc_now_iso_ms()}, headers=_headers(), timeout=20)
    r.raise_for_status()
    raw = str(r.json().get("value", "")).strip()
    ids, seen = [], set()
    for part in raw.split(","):
        eid = "".join(ch for ch in part.strip() if ch.isdigit())
        if eid and eid not in seen:
            seen.add(eid); ids.append(eid)
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
                headers=_headers(), timeout=30
            )
            r.raise_for_status()
            return r.json()
        except Exception:
            continue
    raise RuntimeError(f"No payload for Event {eid}")

def parse_matches(payload: Any) -> List[Dict[str, str]]:
    items = payload if isinstance(payload, list) else payload.get("matches", []) if isinstance(payload, dict) else []
    out: List[Dict[str, str]] = []
    for it in items:
        mc = it.get("match_card") or {}
        sub_event = mc.get("subEventName") or it.get("subEventType") or ""
        desc = mc.get("subEventDescription") or ""
        m = re.search(r"(R\s*\d+|R\d+|QF|SF|F(inal)?)", desc, flags=re.I)
        round_name = m.group(1).upper().replace("FINAL", "F") if m else ""
        home, away = {}, {}
        for c in mc.get("competitiors") or []:
            if c.get("competitorType") == "H":
                home = {"name": c.get("competitiorName", ""), "org": (c.get("competitiorOrg") or "").strip()}
            elif c.get("competitorType") == "A":
                away = {"name": c.get("competitiorName", ""), "org": (c.get("competitiorOrg") or "").strip()}
        h_name, h_org = home.get("name",""), home.get("org","")
        a_name, a_org = away.get("name",""), away.get("org","")
        game_scores = mc.get("resultsGameScores") or mc.get("gameScores") or ""
        overall     = mc.get("resultOverallScores") or mc.get("overallScores") or ""
        win = ""
        m2 = re.match(r"\s*(\d+)\s*[-:]\s*(\d+)\s*$", overall) if overall else None
        if m2:
            aa, bb = int(m2.group(1)), int(m2.group(2))
            win = h_name if aa > bb else a_name if bb > aa else ""
        out.append({
            "SubEventName": sub_event, "Round": round_name,
            "Comp1": h_name, "Comp1_Nation": h_org,
            "Comp2": a_name, "Comp2_Nation": a_org,
            "Match_Score": overall, "Games_Score": game_scores, "Winner": win
        })
    return out

# --------- Event metadata (Excel → API → fallback) ----------
EVENTS_XLSX = os.getenv("EVENTS_XLSX", "TT_Events_2021-2025.xlsx")

def _try_load_event_idx() -> Dict[str, Dict[str, str]]:
    """
    Returns: {eid: {"EventName":..., "EventType":..., "Country":...}}
    Uses pandas if available; otherwise skip silently.
    """
    idx: Dict[str, Dict[str, str]] = {}
    p = Path(EVENTS_XLSX)
    if not p.exists():
        return idx
    try:
        import pandas as pd
        df = pd.read_excel(p)
        def norm(s): return re.sub(r"[^a-z0-9]+", "", str(s).strip().lower())
        cols = {norm(c): c for c in df.columns}
        idcol = cols.get("eventid") or cols.get("id")
        if not idcol:
            return idx
        namecol = cols.get("eventname") or cols.get("name") or cols.get("title") or None
        typecol = cols.get("eventtype") or cols.get("type") or None
        ctrycol = cols.get("country")  or cols.get("hostcountry") or None
        for _, r in df.iterrows():
            raw_id = r.get(idcol, "")
            if isinstance(raw_id, float) and raw_id.is_integer():
                eid = str(int(raw_id))
            else:
                eid = str(raw_id).strip()
            if not eid:
                continue
            idx[eid] = {
                "EventName": str(r.get(namecol, "") or ""),
                "EventType": str(r.get(typecol, "") or ""),
                "Country":   str(r.get(ctrycol, "") or ""),
            }
    except Exception as e:
        print("Excel load skipped:", e)
    return idx

EVENT_IDX = _try_load_event_idx()

def get_event_meta_api(eid: str) -> Tuple[str, str, str]:
    """
    Returns (name, type, country) from GET_EVENT if available; else empty strings.
    """
    try:
        r = requests.get(GET_EVENT, params={"EventId": eid}, headers=_headers(), timeout=20)
        r.raise_for_status()
        data = r.json()
        # Flexible shapes
        obj = data.get("event", data) if isinstance(data, dict) else {}
        name = str(obj.get("eventName") or obj.get("name") or obj.get("title") or "") if isinstance(obj, dict) else ""
        etyp = str(obj.get("eventType") or obj.get("type") or "") if isinstance(obj, dict) else ""
        ctry = str(obj.get("country") or obj.get("hostCountry") or "") if isinstance(obj, dict) else ""
        return (name, etyp, ctry)
    except Exception:
        return ("", "", "")

def build_header(eid: str) -> str:
    # 1) Excel
    meta = EVENT_IDX.get(eid, {})
    name = (meta.get("EventName") or "").strip()
    etyp = (meta.get("EventType") or "").strip()
    ctry = (meta.get("Country") or "").strip()
    # 2) API if name missing
    if not name:
        n2, t2, c2 = get_event_meta_api(eid)
        name = name or n2
        etyp = etyp or t2
        ctry = ctry or c2
    # 3) Compose (no EventID ever)
    if name:
        return f"*{name}*"
    if etyp or ctry:
        return f"*{etyp} | {ctry}*".strip(" *|")
    return "*WTT Update*"

# --------- Formatter for India-only view ----------
def build_india_block(eid: str) -> str:
    payload = get_payload_static_or_live(eid)
    matches = parse_matches(payload)
    lines: List[str] = [build_header(eid)]
    india = [m for m in matches if m["Comp1_Nation"] == "IND" or m["Comp2_Nation"] == "IND"]
    if not india:
        lines.append("(No Indian matches found)")
        return "\n".join(lines)
    for m in india:
        lines.append(f"{m['SubEventName']} {m['Round']}".strip())
        comp_ind = m["Comp1"] if m["Comp1_Nation"] == "IND" else m["Comp2"]
        opp      = m["Comp2"] if m["Comp1_Nation"] == "IND" else m["Comp1"]
        opp_nat  = m["Comp2_Nation"] if m["Comp1_Nation"] == "IND" else m["Comp1_Nation"]
        opp_with = f"{opp} ({opp_nat})" if opp else f"({opp_nat})"
        if m["Winner"] == comp_ind:
            phr = f"Defeated {opp_with} by"
        elif m["Winner"] == opp:
            phr = f"Lost to {opp_with} by"
        else:
            phr = f"vs {opp_with}"
        lines.append(f"{comp_ind} {phr} ({m['Match_Score']}) ({m['Games_Score']})")
        lines.append("")
    return "\n".join(lines).strip()

# --------- Main ---------
def main():
    print("FROM:", repr(FROM_WHATSAPP), "TO:", repr(TO_WHATSAPP))
    eids = get_latest_completed_event_ids()
    if not eids:
        print("No completed events.")
        return
    blocks = []
    for eid in eids:
        try:
            blocks.append(build_india_block(eid))
        except Exception as e:
            # Even on error, keep a neutral header (no EventID)
            blocks.append("*WTT Update*\n(Error: " + str(e) + ")")
        blocks.append("-" * 60)
    msg = "\n".join(blocks).strip()
    print("==== MESSAGE PREVIEW ====\n" + msg + "\n=========================")
    sid = send_whatsapp(msg)
    print("Sent via Twilio SID:", sid)

if __name__ == "__main__":
    main()
