import os
import json
import requests
import sys
from datetime import datetime
from twilio.rest import Client

# =========================
# CONFIGURATION
# =========================
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
FROM_WHATSAPP = os.getenv("TWILIO_WHATSAPP_FROM") 
TO_WHATSAPP = os.getenv("WHATSAPP_TO")            

DB_FILE = "wtt_complete_database.json"
HISTORY_FILE = "processed_ids.json" # New file to track IDs
SCORE_URL_TEMPLATE = "https://wtt-web-frontdoor-withoutcache-cqakg0andqf5hchn.a01.azurefd.net/websitestaticapifiles/{eid}/{eid}_take_10_official_results.json"

def get_headers():
    return {
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://worldtabletennis.com",
        "Referer": "https://worldtabletennis.com/"
    }

# =========================
# HELPER: SCORE FLIPPERS
# =========================
def flip_score(s):
    if s and "-" in s:
        parts = s.split("-")
        if len(parts) == 2: return f"{parts[1]}-{parts[0]}"
    return s

def flip_games(g_str):
    if not g_str: return ""
    clean_g = g_str.replace(",0-0", "").replace("0-0,", "").replace("0-0", "")
    games = [g for g in clean_g.split(",") if g]
    flipped = []
    for g in games:
        if "-" in g:
            p = g.split("-")
            flipped.append(f"{p[1]}-{p[0]}" if len(p) == 2 else g)
        else:
            flipped.append(g)
    return ",".join(flipped)

def clean_games_normal(g_str):
    return g_str.replace(",0-0", "").replace("0-0,", "").strip(",")

# =========================
# 1. DATABASE LOOKUP
# =========================
def get_active_events():
    print(f"📂 Reading database: {DB_FILE}...")
    active_events = {} 
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            events = json.load(f)
        today = datetime.now().strftime("%Y-%m-%d")
        print(f"📅 Server Date: {today}")
        for event in events:
            s_date = (event.get("StartDateTime") or "2099-01-01")[:10]
            e_date = (event.get("EndDateTime") or "2099-01-01")[:10]
            if s_date <= today <= e_date:
                eid = event.get("EventId")
                name = event.get("EventName")
                city = event.get("City")
                if eid not in active_events:
                    active_events[eid] = f"{name} ({city})"
                    print(f"   ✅ ACTIVE: [{eid}] {name}")
        return active_events
    except Exception as e:
        print(f"❌ Error reading DB: {e}")
        return {}

# =========================
# 2. FETCH & FILTER MATCHES
# =========================
def fetch_and_filter(active_events, processed_ids):
    new_messages = []
    new_ids = []
    
    for eid, event_name in active_events.items():
        url = SCORE_URL_TEMPLATE.format(eid=eid)
        try:
            r = requests.get(url, headers=get_headers(), timeout=10)
            if r.status_code != 200: continue

            matches = r.json()
            
            # Process Oldest -> Newest
            for m in reversed(matches):
                mc = m.get("match_card", {})
                match_id = mc.get("documentCode")
                
                # CRITICAL: Skip if we have already processed this ID
                if not match_id or match_id in processed_ids:
                    continue
                
                # If new, add to our list
                new_ids.append(match_id)

                # Extract info
                sub_event = mc.get("subEventName", "Match")
                raw_desc = mc.get("subEventDescription", "")
                round_info = raw_desc.replace(sub_event + " - ", "").replace(sub_event, "").strip()
                if not round_info: round_info = raw_desc
                clean_round = round_info.split(" - Match")[0]

                comps = mc.get("competitiors", [])
                p1_name = comps[0].get("competitiorName", "Unknown") if len(comps) > 0 else "Unknown"
                p1_org = comps[0].get("competitiorOrg", "") if len(comps) > 0 else ""
                p2_name = comps[1].get("competitiorName", "Unknown") if len(comps) > 1 else "Unknown"
                p2_org = comps[1].get("competitiorOrg", "") if len(comps) > 1 else ""

                raw_score = mc.get("resultOverallScores", "0-0")
                raw_games = mc.get("resultsGameScores", "")

                # India Logic
                swap_needed = ("IND" in p2_org) and ("IND" not in p1_org)
                if swap_needed:
                    primary_name = p2_name; primary_org = p2_org
                    opp_name = p1_name; opp_org = p1_org
                    final_score = flip_score(raw_score)
                    final_games = flip_games(raw_games)
                else:
                    primary_name = p1_name; primary_org = p1_org
                    opp_name = p2_name; opp_org = p2_org
                    final_score = raw_score
                    final_games = clean_games_normal(raw_games)

                try:
                    s1, s2 = map(int, final_score.split("-"))
                    verb = "defeated" if s1 > s2 else "lost to" if s1 < s2 else "vs"
                except:
                    verb = "vs"

                if "IND" in primary_org: primary_name = f"_{primary_name}_"
                if "IND" in opp_org: opp_name = f"_{opp_name}_"
                
                msg_block = (
                    f"*{event_name}*\n"
                    f"*{sub_event} | {clean_round}*\n"
                    f"{primary_name} ({primary_org}) {verb} {opp_name} ({opp_org}), {final_score} ({final_games})"
                )
                
                if "IND" in primary_org or "IND" in opp_org:
                    msg_block = "🇮🇳 " + msg_block 

                new_messages.append(msg_block)

        except Exception:
            pass
            
    return new_messages, new_ids

# =========================
# 3. TWILIO SENDER
# =========================
def send_whatsapp(body):
    if not TWILIO_SID or not TWILIO_TOKEN:
        print("❌ Twilio credentials missing.")
        return

    client = Client(TWILIO_SID, TWILIO_TOKEN)
    try:
        msg = client.messages.create(from_=FROM_WHATSAPP, body=body, to=TO_WHATSAPP)
        print(f"✅ Message Sent: {msg.sid}")
    except Exception as e:
        print(f"❌ Twilio Error: {e}")

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    print("🚀 Starting Bot (ID-Based De-duplication)...")
    
    # 1. Load Processed IDs
    processed_ids = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                processed_ids = json.load(f)
        except:
            processed_ids = []
            
    active_events = get_active_events()
    if not active_events:
        print("⚠️ No events today.")
        sys.exit(0)
        
    # 2. Get ONLY new matches
    messages, new_ids = fetch_and_filter(active_events, processed_ids)
    
    if messages:
        print(f"⚡ Found {len(messages)} NEW matches!")
        final_message = "\n\n".join(messages)
        send_whatsapp(final_message)
        
        # 3. Save updated IDs to file
        processed_ids.extend(new_ids)
        # Keep file size small (last 200 matches)
        processed_ids = processed_ids[-200:] 
        
        with open(HISTORY_FILE, 'w') as f:
            json.dump(processed_ids, f)
        print("💾 Updated history file.")
    else:
        print("💤 No new matches found.")