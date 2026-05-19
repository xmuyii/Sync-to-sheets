"""
sync_to_sheets.py — Sync Supabase weekly_points to Google Sheet (Clean & Simple)

This script:
1. Fetches weekly leaderboard from Supabase  
2. Authenticates with Google Sheets API (via gspread + service account)
3. Updates a Google Sheet with clean, formatted rankings
4. Can be triggered via webhook from Railway or scheduled to run hourly/daily

Setup:
  1. Install dependencies:
     pip install gspread google-auth-oauthlib supabase python-dotenv

  2. Create Google Service Account:
     - Go to Google Cloud Console → Create Project
     - Enable Google Sheets API
     - Create Service Account → Generate JSON key
     - Place key in .env as GOOGLE_CREDENTIALS_PATH

  3. Share your Google Sheet with the service account email
     - Find email in the JSON key file (looks like: xxx@xxx.iam.gserviceaccount.com)
     - Share the sheet with this email
     - WhatsAuto will monitor this sheet and send WhatsApp messages based on its contents

  4. Add to .env:
     GOOGLE_SHEET_ID=your_sheet_id_from_url
     GOOGLE_SHEET_NAME=Leaderboard  (or whatever sheet tab name)
     GOOGLE_CREDENTIALS_PATH=./google-credentials.json
     SUPABASE_URL=your_url
     SUPABASE_KEY=your_key

  5. Test: python sync_to_sheets.py

  6. Schedule on Railway:
     - Set this script to run hourly via Railway cron job
     - Or expose as webhook for manual/event-triggered syncs
"""

import os
import sys
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict
import json

from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

# Load environment variables
load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────

SUPABASE_URL = os.getenv('SUPABASE_URL', '').rstrip('/')
SUPABASE_KEY = os.getenv('SUPABASE_KEY', '')
GOOGLE_SHEET_ID = os.getenv('GOOGLE_SHEET_ID', '')
GOOGLE_SHEET_NAME = os.getenv('GOOGLE_SHEET_NAME', 'Leaderboard')
GOOGLE_CREDENTIALS_PATH = os.getenv('GOOGLE_CREDENTIALS_PATH', './google-credentials.json')

SCOPES = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']

# ── Logging Setup ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('sync_to_sheets.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ── Validation ─────────────────────────────────────────────────────────────

def get_google_credentials():
    """Load Google credentials from environment variable or file.
    
    Priority:
    1. GOOGLE_CREDENTIALS_JSON env var (for Railway)
    2. GOOGLE_CREDENTIALS_PATH file (for local development)
    """
    # Try environment variable first (Railway deployment)
    creds_json_str = os.getenv('GOOGLE_CREDENTIALS_JSON')
    if creds_json_str:
        try:
            creds_dict = json.loads(creds_json_str)
            logger.info("✅ Loaded credentials from GOOGLE_CREDENTIALS_JSON env variable")
            return Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        except Exception as e:
            logger.error(f"❌ Failed to parse GOOGLE_CREDENTIALS_JSON: {e}")
            raise
    
    # Fall back to file (local development)
    if os.path.exists(GOOGLE_CREDENTIALS_PATH):
        try:
            logger.info(f"✅ Loaded credentials from file: {GOOGLE_CREDENTIALS_PATH}")
            return Credentials.from_service_account_file(GOOGLE_CREDENTIALS_PATH, scopes=SCOPES)
        except Exception as e:
            logger.error(f"❌ Failed to load credentials from file: {e}")
            raise
    
    # Neither found
    raise FileNotFoundError(
        f"Google credentials not found!\n"
        f"  - Env var GOOGLE_CREDENTIALS_JSON not set\n"
        f"  - File {GOOGLE_CREDENTIALS_PATH} not found\n"
        f"Set one of these to authenticate with Google Sheets."
    )


def validate_config():
    """Check if all required config is present."""
    errors = []
    
    if not SUPABASE_URL or 'supabase.co' not in SUPABASE_URL:
        errors.append("❌ SUPABASE_URL not configured")
    if not SUPABASE_KEY:
        errors.append("❌ SUPABASE_KEY not configured")
    if not GOOGLE_SHEET_ID:
        errors.append("❌ GOOGLE_SHEET_ID not configured")
    
    # Check for credentials (env var or file)
    has_creds_env = os.getenv('GOOGLE_CREDENTIALS_JSON') is not None
    has_creds_file = os.path.exists(GOOGLE_CREDENTIALS_PATH)
    if not has_creds_env and not has_creds_file:
        errors.append(
            f"❌ Google credentials not found!\n"
            f"   Set GOOGLE_CREDENTIALS_JSON env var (Railway) OR\n"
            f"   Place file at {GOOGLE_CREDENTIALS_PATH} (local)"
        )
    
    if errors:
        msg = "\n".join(errors)
        logger.error(msg)
        print(msg)
        sys.exit(1)
    
    logger.info("✅ Configuration validated")
    print("✅ Configuration validated")


def retry_operation(func, max_retries=3, backoff_factor=2):
    """Retry wrapper with exponential backoff."""
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = backoff_factor ** attempt
                logger.warning(f"Attempt {attempt + 1} failed ({type(e).__name__}), retrying in {wait_time}s...")
                time.sleep(wait_time)
            else:
                raise


def get_credentials():
    """Load Google credentials from environment variable or file."""
    creds_json_str = os.getenv('GOOGLE_CREDENTIALS_JSON')
    
    if creds_json_str:
        # Load from environment variable
        creds_dict = json.loads(creds_json_str)
        return Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    elif os.path.exists(GOOGLE_CREDENTIALS_PATH):
        # Fall back to file if it exists
        return Credentials.from_service_account_file(GOOGLE_CREDENTIALS_PATH, scopes=SCOPES)
    else:
        raise FileNotFoundError(f"Google credentials not found in env var or {GOOGLE_CREDENTIALS_PATH}")


def get_gspread_client():
    """Authenticate with Google Sheets using gspread."""
    try:
        creds = get_google_credentials()
        return gspread.authorize(creds)
    except Exception as e:
        logger.error(f"Could not authenticate with Google Sheets: {e}")
        print(f"Could not authenticate with Google Sheets: {e}")
        sys.exit(1)


def get_fusion_weekly_leaderboard() -> List[Dict]:
    """Fetch Fusion weekly leaderboard from Supabase.
    
    Tries fusion_weekly_points first. If all values are NULL/0 (columns not yet
    populated), falls back to the shared weekly_points column so the sheet always
    shows real data.
    """
    def _fetch():
        from supabase import create_client
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

        # --- Try game-specific column first ---
        r = supabase.table('players').select(
            'user_id, username, fusion_weekly_points, weekly_points, level'
        ).order('fusion_weekly_points', desc=True).limit(10).execute()

        results = []
        for p in (r.data or []):
            pts = int(p.get('fusion_weekly_points') or 0)
            if pts > 0:
                results.append({
                    'rank': len(results) + 1,
                    'username': p.get('username', 'Unknown'),
                    'user_id': p['user_id'],
                    'points': pts,
                    'level': p.get('level', 1),
                    'source': 'fusion_weekly_points',
                })

        if results:
            logger.info(f"✅ Got {len(results)} players from fusion_weekly_points")
            return results

        # --- Fallback: shared weekly_points ---
        logger.warning("⚠️  fusion_weekly_points empty or all NULL — falling back to weekly_points")
        r2 = supabase.table('players').select(
            'user_id, username, weekly_points, level'
        ).gt('weekly_points', 0).order('weekly_points', desc=True).limit(10).execute()

        for p in (r2.data or []):
            pts = int(p.get('weekly_points') or 0)
            if pts > 0:
                results.append({
                    'rank': len(results) + 1,
                    'username': p.get('username', 'Unknown'),
                    'user_id': p['user_id'],
                    'points': pts,
                    'level': p.get('level', 1),
                    'source': 'weekly_points_fallback',
                })

        logger.info(f"✅ Fallback returned {len(results)} players from weekly_points")
        return results

    try:
        return retry_operation(_fetch, max_retries=3)
    except Exception as e:
        logger.error(f"❌ Error fetching Fusion weekly leaderboard: {e}")
        print(f"❌ Error fetching Fusion weekly leaderboard: {e}")
        return []


def get_fusion_alltime_leaderboard() -> List[Dict]:
    """Fetch Fusion all-time leaderboard from Supabase.

    Tries fusion_all_time_points first. Falls back to all_time_points when the
    game-specific column is empty (columns not yet populated via award_word_score).
    """
    def _fetch():
        from supabase import create_client
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

        # --- Try game-specific column first ---
        r = supabase.table('players').select(
            'user_id, username, fusion_all_time_points, all_time_points, level'
        ).order('fusion_all_time_points', desc=True).limit(10).execute()

        results = []
        for p in (r.data or []):
            pts = int(p.get('fusion_all_time_points') or 0)
            if pts > 0:
                results.append({
                    'rank': len(results) + 1,
                    'username': p.get('username', 'Unknown'),
                    'user_id': p['user_id'],
                    'points': pts,
                    'level': p.get('level', 1),
                    'source': 'fusion_all_time_points',
                })

        if results:
            logger.info(f"Got {len(results)} players from fusion_all_time_points")
            return results

        # --- Fallback: shared all_time_points ---
        logger.warning("fusion_all_time_points empty — falling back to all_time_points")
        r2 = supabase.table('players').select(
            'user_id, username, all_time_points, level'
        ).gt('all_time_points', 0).order('all_time_points', desc=True).limit(10).execute()

        for p in (r2.data or []):
            pts = int(p.get('all_time_points') or 0)
            if pts > 0:
                results.append({
                    'rank': len(results) + 1,
                    'username': p.get('username', 'Unknown'),
                    'user_id': p['user_id'],
                    'points': pts,
                    'level': p.get('level', 1),
                    'source': 'all_time_fallback',
                })

        logger.info(f"Fallback returned {len(results)} players from all_time_points")
        return results

    try:
        return retry_operation(_fetch, max_retries=3)
    except Exception as e:
        logger.error(f"❌ Error fetching Fusion all-time leaderboard: {e}")
        print(f"❌ Error fetching Fusion all-time leaderboard: {e}")
        return []


def update_google_sheet(fusion_weekly_lb, fusion_alltime_lb):
    """
    Updates Google Sheet with both Fusion weekly and all-time leaderboards.
    WhatsAuto will monitor this sheet and send WhatsApp messages based on trigger keywords.
    
    - "weekly" → Fusion Weekly leaderboard
    - "alltime" or "rank" → Fusion All-Time leaderboard
    """
    try:
        # Initialize gspread client
        client = get_gspread_client()
        
        # Open the workbook and get the specific sheet
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        
        try:
            sheet = spreadsheet.worksheet(GOOGLE_SHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            # Create sheet if it doesn't exist
            sheet = spreadsheet.add_worksheet(title=GOOGLE_SHEET_NAME, rows=200, cols=3)
        
        # Clean, premium styling lines
        divider_line = "─────────────────────"
        # Display clean time format (14:30 WAT / UTC)
        timestamp = datetime.now(timezone.utc).strftime('%H:%M %Z')
        
        # ─── Format Premium Weekly Leaderboard ───
        top_10_weekly = fusion_weekly_lb[:10]
        weekly_msg = "⚡ *FUSION WEEKLY SCOREBOARD*\n"
        weekly_msg += f" {divider_line}\n\n"
        
        if not top_10_weekly:
            weekly_msg += "   _No active players yet this week._\n"
        else:
            for idx, player in enumerate(top_10_weekly, 1):
                username = player.get("username", "Operative")
                # Fallbacks to handle variable score naming variations
                pts = player.get("points") or player.get("fusion_weekly_points") or 0
                
                # Assign premium ranking badges
                if idx == 1: medal = "🥇"
                elif idx == 2: medal = "🥈"
                elif idx == 3: medal = "🥉"
                else: medal = f"*{idx:02d}.*"
                
                weekly_msg += f"{medal}  *{username}* ➔  `{pts:,}` pts\n"
                
        weekly_msg += f"\n {divider_line}\n"
        weekly_msg += "🎮 Type *!play* in the Telegram group chat!\n"
        weekly_msg += f"⏳ _Updated: Today at {timestamp}_"
        
        # ─── Format Premium All-Time Leaderboard ───
        top_10_alltime = fusion_alltime_lb[:10]
        alltime_msg = "👑 *FUSION ALL-TIME HALL OF FAME*\n"
        alltime_msg += f" {divider_line}\n\n"
        
        if not top_10_alltime:
            alltime_msg += "   _No entries logged in the system._\n"
        else:
            for idx, player in enumerate(top_10_alltime, 1):
                username = player.get("username", "Operative")
                pts = player.get("points") or player.get("fusion_all_time_points") or 0
                
                if idx == 1: medal = "🔱"
                elif idx == 2: medal = "✨"
                elif idx == 3: medal = "⭐️"
                else: medal = f"*{idx:02d}.*"
                
                alltime_msg += f"{medal}  *{username}* ➔  `{pts:,}` pts\n"
                
        alltime_msg += f"\n {divider_line}\n"
        alltime_msg += "🔗 Global Server: https://t.me/checkmateHQ\n"
        alltime_msg += f"📡 _Live Synced Session • {timestamp}_"
        
        # ─── WhatsAuto trigger keywords and their responses ───
        whatsauto_layout = [
            ["Trigger", "Response", "Status"],  # Headers
            ["leaderboard", weekly_msg, "Active"],
            ["weekly", weekly_msg, "Active"],
            ["fusion_weekly", weekly_msg, "Active"],
            ["alltime", alltime_msg, "Active"],
            ["rank", alltime_msg, "Active"],
            ["fusion_alltime", alltime_msg, "Active"],
        ]
        
        # Clear and update the sheet
        sheet.clear()
        sheet.update('A1', whatsauto_layout)
        
        # ─── Add raw Fusion Weekly data for reference ───
        sheet.append_row(["", "", ""])  # Blank separator
        sheet.append_row(["FUSION WEEKLY - Raw Data", "", ""])
        sheet.append_row(["Rank", "Username", "Points"])
        for idx, player in enumerate(fusion_weekly_lb[:15], 1):
            sheet.append_row([
                str(idx),
                player.get("username", "Unknown"),
                str(player.get("points", 0))
            ])
        
        # ─── Add raw Fusion All-Time data for reference ───
        sheet.append_row(["", "", ""])  # Blank separator
        sheet.append_row(["FUSION ALL-TIME - Raw Data", "", ""])
        sheet.append_row(["Rank", "Username", "Points"])
        for idx, player in enumerate(fusion_alltime_lb[:15], 1):
            sheet.append_row([
                str(idx),
                player.get("username", "Unknown"),
                str(player.get("points", 0))
            ])
        
        logger.info(f"✅ Google Sheet updated: {len(fusion_weekly_lb)} weekly + {len(fusion_alltime_lb)} all-time players")
        print(f"✅ Google Sheet updated successfully!")
        print(f"   Fusion Weekly: {len(fusion_weekly_lb)} players")
        print(f"   Fusion All-Time: {len(fusion_alltime_lb)} players")
        return True
        
    except Exception as e:
        logger.error(f"❌ Failed to update Google Sheet: {e}", exc_info=True)
        print(f"❌ Failed to update Google Sheet: {e}")
        return False

def main():
    """Main sync function."""
    try:
        header = "🔄 SYNCING SUPABASE FUSION LEADERBOARDS TO GOOGLE SHEET"
        print("\n" + "="*70)
        print(header)
        print("="*70 + "\n")
        logger.info(header)
        
        # Validate config
        validate_config()
        
        # Fetch both Fusion leaderboards
        print("\n📊 Fetching Fusion leaderboards from Supabase...")
        logger.info("Fetching Fusion leaderboards from Supabase...")
        
        fusion_weekly = get_fusion_weekly_leaderboard()
        fusion_alltime = get_fusion_alltime_leaderboard()
        
        if not fusion_weekly and not fusion_alltime:
            logger.warning("⚠️  No leaderboard data found!")
            print("⚠️  No leaderboard data found!")
            return False
        
        print(f"\n   Fusion Weekly: {len(fusion_weekly)} players")
        print(f"   Fusion All-Time: {len(fusion_alltime)} players")
        logger.info(f"Found {len(fusion_weekly)} weekly players, {len(fusion_alltime)} all-time players")
        
        # Show top 10 weekly
        if fusion_weekly:
            print("\n   Top 10 Weekly:")
            for p in fusion_weekly[:10]:
                print(f"      #{p['rank']} {p['username']:20} {p['points']:>6} pts")
        
        # Show top 10 all-time
        if fusion_alltime:
            print("\n   Top 10 All-Time:")
            for p in fusion_alltime[:10]:
                print(f"      #{p['rank']} {p['username']:20} {p['points']:>6} pts")
        
        # Update Google Sheet
        print("\n📝 Updating Google Sheet...")
        logger.info("Updating Google Sheet...")
        update_google_sheet(fusion_weekly, fusion_alltime)
        
        print("\n" + "="*70)
        print("✅ SYNC COMPLETE!")
        print("   WhatsAuto will now send the leaderboard to WhatsApp")
        print("="*70 + "\n")
        logger.info("✅ SYNC COMPLETE!")
        return True
        
    except Exception as e:
        logger.critical(f"Critical error in main: {e}", exc_info=True)
        print(f"❌ Critical error: {e}")
        return False

if __name__ == '__main__':
    import time
    import sys
    
    # Check if running as a scheduled worker (Railway) or one-time script
    if len(sys.argv) > 1 and sys.argv[1] == '--daemon':
        # Run as daemon: sync every 60 minutes
        print("\n🔄 Starting sync daemon (runs every 60 minutes)...\n")
        while True:
            try:
                main()
                time.sleep(3600)  # 60 minutes
            except KeyboardInterrupt:
                print("\n✋ Daemon stopped")
                break
            except Exception as e:
                print(f"❌ Daemon error: {e}")
                time.sleep(60)  # Wait 1 min before retrying
    else:
        # Run once and exit
        main()
