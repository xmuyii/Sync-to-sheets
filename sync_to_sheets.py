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
from datetime import datetime, timedelta
from typing import List, Dict

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

def validate_config():
    """Check if all required config is present."""
    errors = []
    
    if not SUPABASE_URL or 'supabase.co' not in SUPABASE_URL:
        errors.append("❌ SUPABASE_URL not configured")
    if not SUPABASE_KEY:
        errors.append("❌ SUPABASE_KEY not configured")
    if not GOOGLE_SHEET_ID:
        errors.append("❌ GOOGLE_SHEET_ID not configured")
    if not os.path.exists(GOOGLE_CREDENTIALS_PATH):
        errors.append(f"❌ Google credentials file not found: {GOOGLE_CREDENTIALS_PATH}")
    
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


def get_gspread_client():
    """Authenticate with Google Sheets using gspread."""
    try:
        creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_PATH, scopes=SCOPES)
        return gspread.authorize(creds)
    except Exception as e:
        logger.error(f"❌ Could not authenticate with Google Sheets: {e}")
        print(f"❌ Could not authenticate with Google Sheets: {e}")
        sys.exit(1)


def get_weekly_leaderboard() -> List[Dict]:
    """Fetch weekly leaderboard from Supabase with retry logic."""
    def _fetch():
        from supabase import create_client
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        
        # Get current week key (ISO date of the Sunday that starts this week)
        today = datetime.utcnow()
        days_since_sunday = (today.weekday() + 1) % 7
        sunday = today - timedelta(days=days_since_sunday)
        this_week = sunday.date().isoformat()
        
        # Fetch all players with weekly_points (with timeout)
        r = supabase.table('players').select(
            'user_id, username, weekly_points, week_start'
        ).order('weekly_points', desc=True).limit(100).execute()
        
        results = []
        for p in (r.data or []):
            pts = int(p.get('weekly_points') or 0)
            player_week = p.get('week_start', '')
            player_week_date = player_week.split('T')[0] if player_week else ''
            
            # Only include if their week matches current week and points > 0
            if player_week_date == this_week and pts > 0:
                results.append({
                    'rank': len(results) + 1,
                    'username': p.get('username', 'Unknown'),
                    'user_id': p['user_id'],
                    'points': pts,
                })
        
        return results
    
    try:
        return retry_operation(_fetch, max_retries=3)
    except Exception as e:
        logger.error(f"❌ Error fetching leaderboard from Supabase: {e}", exc_info=True)
        print(f"❌ Error fetching leaderboard from Supabase: {e}")
        return []


def update_google_sheet(leaderboard_data):
    """
    Updates Google Sheet with leaderboard data.
    WhatsAuto will monitor this sheet and send WhatsApp messages based on its contents.
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
            sheet = spreadsheet.add_worksheet(title=GOOGLE_SHEET_NAME, rows=100, cols=3)
        
        # Format the leaderboard for WhatsAuto
        top_10_weekly = leaderboard_data[:10]
        divider_line = "━━━━━━━━━━━━━━━━━━━━"
        
        # Build the message that WhatsAuto will read and send to WhatsApp
        weekly_msg = "📊 *CHECKMATE HQ LIVE WEEKLY TOP 10*\n"
        weekly_msg += divider_line + "\n"
        for idx, player in enumerate(top_10_weekly, 1):
            username = player.get("username", "Operative")
            pts = player.get("points", 0)
            weekly_msg += f"{idx:02d}. 👤 {username} ➔ {pts:,} pts\n"
        weekly_msg += divider_line + "\n"
        weekly_msg += "💬 Type *!play* in the group chat to claim your spot!\n"
        weekly_msg += f"⏰ Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
        
        # WhatsAuto trigger keywords and their responses
        whatsauto_layout = [
            ["Trigger", "Response", "Status"],  # Headers
            ["leaderboard", weekly_msg, "Active"],
            ["weekly", weekly_msg, "Active"],
            ["scores", weekly_msg, "Active"],
            ["rank", weekly_msg, "Active"],
        ]
        
        # Clear and update the sheet
        sheet.clear()
        sheet.update('A1', whatsauto_layout)
        
        # Also add raw data rows for reference
        sheet.append_row(["", "", ""])  # Blank row separator
        sheet.append_row(["Raw Leaderboard Data", "", ""])
        sheet.append_row(["Rank", "Username", "Points"])
        
        for idx, player in enumerate(leaderboard_data[:20], 1):
            sheet.append_row([
                str(idx),
                player.get("username", "Unknown"),
                str(player.get("points", 0))
            ])
        
        logger.info(f"✅ Google Sheet updated successfully with {len(leaderboard_data)} players")
        print(f"✅ Google Sheet updated successfully!")
        print(f"   Sheet: {GOOGLE_SHEET_NAME}")
        print(f"   Players synced: {len(leaderboard_data)}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Failed to update Google Sheet: {e}", exc_info=True)
        print(f"❌ Failed to update Google Sheet: {e}")
        return False

def main():
    """Main sync function."""
    try:
        header = "🔄 SYNCING SUPABASE WEEKLY POINTS TO GOOGLE SHEET"
        print("\n" + "="*70)
        print(header)
        print("="*70 + "\n")
        logger.info(header)
        
        # Validate config
        validate_config()
        
        # Fetch leaderboard
        print("\n📊 Fetching leaderboard from Supabase...")
        logger.info("Fetching leaderboard from Supabase...")
        leaderboard = get_weekly_leaderboard()
        
        if not leaderboard:
            logger.warning("⚠️  No leaderboard data found!")
            print("⚠️  No leaderboard data found!")
            return False
        
        msg = f"Found {len(leaderboard)} players"
        print(f"   {msg}")
        logger.info(msg)
        
        # Show top 5
        print("\n   Top 5:")
        for p in leaderboard[:5]:
            print(f"      #{p['rank']} {p['username']:20} {p['points']:>6} pts")
        
        # Update Google Sheet
        print("\n📝 Updating Google Sheet...")
        logger.info("Updating Google Sheet...")
        update_google_sheet(leaderboard)
        
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
    main()
