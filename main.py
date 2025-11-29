import os
import json
import requests
import sys
from datetime import datetime
from twilio.rest import Client

# =========================
# CONFIGURATION
# =========================
# GitHub Secrets (Must be set in your Repo Settings)
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
FROM_WHATSAPP = os.getenv("TWILIO_WHATSAPP_FROM") # e.g., 'whatsapp:+14155238886'
TO_WHATSAPP = os.getenv("WHATSAPP_TO")            # e.g., 'whatsapp:+919876543210'

# Local Files (These must exist in your Repo)
DB_FILE = "wtt_complete_database.json"
STATE_FILE = "last_message_state.txt"

# WTT Data Source
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
        if len(parts) == 2:
            return f"{parts[1]}-{parts[0]}"
    return s

def flip_games(g_str):
    if not g_str: return ""
    clean_g = g_str.replace(",0-0", "").replace("0-0,", "").replace("0-0", "")
    games = [g for g in clean_g.split(",") if g]
    flipped = []
    for g in games:
        if "-" in g:
            p = g.split("-")
            if len(p) == 2:
                flipped.append(f"{p[1]}-{p[0]}")
            else:
                flipped.append(g)
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
# 2. FETCH AND FORMAT MATCHES
# =========================
def fetch_latest_results(active_events):
    messages = []
    
    for eid, event_name in active_events.items():
        url = SCORE_URL_TEMPLATE.format(eid=eid)
        try:
            r = requests.get(url, headers=get_headers(), timeout=10)
            if r.status_code != 200: continue

            matches = r.json()
            
            # Process oldest -> newest
            for m in reversed(matches):
                mc = m.get("match_card", {})
                
                # Extract basic info
                sub_event = mc.get("subEventName", "Match")
                raw_desc = mc.get("subEventDescription", "")
                round_info = raw_desc.replace(sub_event + " - ", "").replace(sub_event, "").strip()
                if not round_info: round_info = raw_desc
                clean_round = round_info.split(" - Match")[0]

                # Player Data
                comps = mc.get("competitiors", [])
                p1_name = comps[0].get("competitiorName", "Unknown") if len(comps) > 0 else "Unknown"
                p1_org = comps[0].get("competitiorOrg", "") if len(comps) > 0 else ""
                
                p2_name = comps[1].get("competitiorName", "Unknown") if len(comps) > 1 else "Unknown"
                p2_org = comps[1].get("competitiorOrg", "") if len(comps) > 1 else ""

                raw_score = mc.get("resultOverallScores", "0-0")
                raw_games = mc.get("resultsGameScores", "")

                # === INDIA LOGIC ===
                # Swap if India is Player 2 but not Player 1
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

                # Outcome Verb
                try:
                    s1, s2 = map(int, final_score.split("-"))
                    verb = "defeated" if s1 > s2 else "lost to" if s1 < s2 else "vs"
                except:
                    verb = "vs"

                # Formatting
                # Bold Indian Names for WhatsApp (*Name*)
                if "IND" in primary_org: primary_name = f"*{primary_name}*"
                if "IND" in opp_org: opp_name = f"*{opp_name}*"
                
                # Construct Message Block
                msg_block = (
                    f"_{event_name}_\n"
                    f"🏆 {sub_event} | {clean_round}\n"
                    f"{primary_name} ({primary_org}) {verb} {opp_name} ({opp_org})\n"
                    f"🔢 {final_score} ({final_games})"
                )
                
                # Check if India is involved to prioritize
                if "IND" in primary_org or "IND" in opp_org:
                    msg_block = "🇮🇳 *INDIA UPDATE* 🇮🇳\n" + msg_block

                messages.append(msg_block)

        except Exception:
            pass
            
    return messages

# =========================
# 3. TWILIO SENDER
# =========================
def send_whatsapp(body):
    if not TWILIO_SID or not TWILIO_TOKEN:
        print("❌ Twilio credentials missing in GitHub Secrets.")
        return

    client = Client(TWILIO_SID, TWILIO_TOKEN)
    
    try:
        msg = client.messages.create(
            from_=FROM_WHATSAPP,
            body=body,
            to=TO_WHATSAPP
        )
        print(f"✅ Sent WhatsApp Message SID: {msg.sid}")
    except Exception as e:
        print(f"❌ Twilio Error: {e}")

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    print("🚀 Starting GitHub WTT Bot...")
    
    # 1. Get Active Events
    active_events = get_active_events()
    if not active_events:
        print("⚠️ No events today. Exiting.")
        sys.exit(0)
        
    # 2. Fetch All Current Matches
    all_results = fetch_latest_results(active_events)
    if not all_results:
        print("⚠️ No match results found.")
        sys.exit(0)
        
    # 3. Build Final Message String
    # We join the last 5 matches to avoid spamming too much data
    final_message = "\n\n".join(all_results[-5:])
    
    # 4. State Management (Prevent Duplicate Sends)
    # We read the last sent message from file
    last_sent = ""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            last_sent = f.read()
            
    # 5. Compare and Send
    if final_message != last_sent:
        print("⚡ New results detected! Sending WhatsApp...")
        send_whatsapp(final_message)
        
        # Save new state
        with open(STATE_FILE, 'w') as f:
            f.write(final_message)
    else:
        print("💤 No change in results. Skipping send.")