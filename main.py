import os, re
from typing import Any, Dict, List
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from twilio.rest import Client

# Load .env next to this script; do NOT override inline env vars
load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=True)

# Twilio creds: use API key if present, else SID+Token
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
    raise RuntimeError("Twilio creds missing. Set (TWILIO_API_KEY_SID & TWILIO_API_KEY_SECRET & TWILIO_ACCOUNT_SID) "
                       "or (TWILIO_ACCOUNT_SID & TWILIO_AUTH_TOKEN).")

def send_whatsapp(body: str) -> str:
    if not FROM_WHATSAPP or not TO_WHATSAPP:
        raise RuntimeError("Missing WhatsApp numbers. Set TWILIO_WHATSAPP_FROM and WHATSAPP_TO in .env.")
    to_num = TO_WHATSAPP if TO_WHATSAPP.startswith("whatsapp:") else "whatsapp:" + TO_WHATSAPP
    client = get_twilio_client()
    msg = client.messages.create(from_=FROM_WHATSAPP, to=to_num, body=body)
    return msg.sid

# WTT endpoints
APPSETTING_URL = "https://wtt-website-api-prod-3-frontdoor-bddnb2haduafdze9.a01.azurefd.net/api/cms/GetAppSetting/completed_results_page_event_id"
STATIC_ROOT    = "https://wtt-web-frontdoor-withoutcache-cqakg0andqf5hchn.a01.azurefd.net/websitestaticapifiles"
LIVE_API       = "https://wtt-website-live-events-api-prod-cmfzgabgbzhphabb.eastasia-01.azurewebsites.net/api/cms/GetOfficialResult"
GET_EVENT      = "https://wtt-website-live-events-api-prod-cmfzgabgbzhphabb.eastasia-01.azurewebsites.net/api/cms/GetEvent"
TAKE_TRY       = [200, 100, 50, 20, 10]

def _utc_now_iso_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def _headers(no_cache=False) -> Dict[str, str]:
    h = {"Accept": "application/json, text/plain, */*", "Origin": "https://www.worldtabletennis.com",
         "Referer": "https://www.worldtabletennis.com/", "User-Agent": "Mozilla/5.0"}
    if no_cache: h["Cache-Control"] = "no-cache"; h["Pragma"] = "no-cache"
    return h

def get_latest_completed_event_ids() -> List[str]:
    r = requests.get(APPSETTING_URL, params={"qc": _utc_now_iso_ms()}, headers=_headers(), timeout=20)
    r.raise_for_status()
    raw = str(r.json().get("value","")).strip()
    ids, seen = [], set()
    for part in raw.split(","):
        eid = "".join(ch for ch in part.strip() if ch.isdigit())
        if eid and eid not in seen: seen.add(eid); ids.append(eid)
    return ids

def _static_url(eid: str, take: int) -> str:
    return f"{STATIC_ROOT}/{eid}/{eid}_take_{take}_official_results.json"

def _fetch_json(url: str, no_cache=True) -> Any:
    r = requests.get(url, headers=_headers(no_cache=no_cache), timeout=30)
    r.raise_for_status()
    return r.json()

def get_payload_static_or_live(eid: str) -> Any:
    for take in TAKE_TRY:
        try: return _fetch_json(_static_url(eid, take))
        except requests.HTTPError as he:
            if he.response is not None and he.response.status_code == 404: continue
        except Exception: continue
    for take in TAKE_TRY:
        try:
            r = requests.get(LIVE_API,
                params={"EventId": eid, "include_match_card":"true", "take": str(take), "languageCode":"en"},
                headers=_headers(), timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception: continue
    raise RuntimeError(f"No payload available for Event {eid}")

def parse_matches(payload: Any) -> List[Dict[str, str]]:
    items = payload if isinstance(payload, list) else payload.get("matches", []) if isinstance(payload, dict) else []
    out: List[Dict[str, str]] = []
    for it in items:
        mc = it.get("match_card") or {}
        sub_event = mc.get("subEventName") or it.get("subEventType") or ""
        desc = mc.get("subEventDescription") or ""
        m = re.search(r"(R\s*\d+|R\d+|QF|SF|F(inal)?)", desc, flags=re.I)
        round_name = m.group(1).upper().replace("FINAL","F") if m else ""
        home, away = {}, {}
        for c in mc.get("competitiors") or []:
            if c.get("competitorType") == "H":
                home = {"name": c.get("competitiorName",""), "org": (c.get("competitiorOrg") or "").strip()}
            elif c.get("competitorType") == "A":
                away = {"name": c.get("competitiorName",""), "org": (c.get("competitiorOrg") or "").strip()}
        h_name, h_org = home.get("name",""), home.get("org","")
        a_name, a_org = away.get("name",""), away.get("org","")
        game_scores = mc.get("resultsGameScores") or mc.get("gameScores") or ""
        overall     = mc.get("resultOverallScores") or mc.get("overallScores") or ""
        win = ""
        m2 = re.match(r"\s*(\d+)\s*[-:]\s*(\d+)\s*$", overall) if overall else None
        if m2:
            aa, bb = int(m2.group(1)), int(m2.group(2))
            win = h_name if aa>bb else a_name if bb>aa else ""
        out.append({"SubEventName": sub_event, "Round": round_name,
                    "Comp1": h_name, "Comp1_Nation": h_org,
                    "Comp2": a_name, "Comp2_Nation": a_org,
                    "Match_Score": overall, "Games_Score": game_scores, "Winner": win})
    return out

def get_event_name(eid: str) -> str:
    try:
        r = requests.get(GET_EVENT, params={"EventId": eid}, headers=_headers(), timeout=20)
        r.raise_for_status()
        data = r.json()
        for k in ("eventName","name","title"):
            if isinstance(data, dict) and data.get(k): return str(data[k])
        if isinstance(data, dict) and isinstance(data.get("event"), dict):
            ev = data["event"]
            for k in ("eventName","name","title"):
                if ev.get(k): return str(ev[k])
    except Exception: pass
    return f"Event {eid}"

def build_india_block(eid: str) -> str:
    payload = get_payload_static_or_live(eid)
    matches = parse_matches(payload)
    event_name = get_event_name(eid)
    lines: List[str] = [event_name]
    india = [m for m in matches if m["Comp1_Nation"]=="IND" or m["Comp2_Nation"]=="IND"]
    if not india:
        lines.append("(No Indian matches found)")
        return "\n".join(lines)
    for m in india:
        lines.append(f"{m['SubEventName']} {m['Round']}".strip())
        comp_ind = m["Comp1"] if m["Comp1_Nation"]=="IND" else m["Comp2"]
        opp      = m["Comp2"] if m["Comp1_Nation"]=="IND" else m["Comp1"]
        opp_nat  = m["Comp2_Nation"] if m["Comp1_Nation"]=="IND" else m["Comp1_Nation"]
        opp_with = f"{opp} ({opp_nat})" if opp else f"({opp_nat})"
        if m["Winner"] == comp_ind: phr = f"Defeated {opp_with} by"
        elif m["Winner"] == opp:    phr = f"Lost to {opp_with} by"
        else:                       phr = f"vs {opp_with}"
        lines.append(f"{comp_ind} {phr} ({m['Match_Score']}) ({m['Games_Score']})")
        lines.append("")
    return "\n".join(lines).strip()

def main():
    print("FROM:", repr(FROM_WHATSAPP), "TO:", repr(TO_WHATSAPP))
    eids = get_latest_completed_event_ids()
    if not eids:
        print("No completed events."); return
    blocks = []
    for eid in eids:
        try: blocks.append(build_india_block(eid))
        except Exception as e: blocks.append(f"Event {eid}\n(Error: {e})")
        blocks.append("-"*60)
    msg = "\n".join(blocks).strip()
    print("==== MESSAGE PREVIEW ====\n" + msg + "\n=========================")
    sid = send_whatsapp(msg)
    print("Sent via Twilio SID:", sid)

if __name__ == "__main__":
    main()
