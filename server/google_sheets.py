"""
Google Sheets integration for DTF Monitor.
When a job is completed, finds the matching order in the sheet and updates
STATUS, MAKINA (machine), and MAKINACI (operator) columns.
"""

import re
import os
import logging
import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

# ── Config ──
CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "second-broker-485916-c9-54a88fbe41b8.json")
SHEET_ID = "1OKFZOdpFrvRrwIALOCw9vAd2gG2zckqngz-9rXrOBAw"
WORKSHEET_NAME = "May"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column positions (1-indexed)
COL_ORDER_NO = 3   # C
COL_STATUS = 10     # J
COL_MAKINACI = 12   # L
COL_MAKINA = 13     # M

STATUS_VALUE = "Basildi"


def _get_client():
    """Create an authorized gspread client."""
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def extract_order_code(filename: str) -> str | None:
    """
    Extract order code from filename.
    The code is the letters+numbers portion right before '(N x)'.
    Examples:
        '67--IN3300 (1 x) ...' -> 'IN3300'
        '129-4-C15963 (2 x) ...' -> 'C15963'
        '7--DWC1518 (1 x) ...' -> 'DWC1518'
        '94-1-MC2096 (3 x) ...' -> 'MC2096'
    """
    # Find the part before (N x), then extract the last alphanumeric token
    m = re.search(r'([A-Za-z]{1,4}\d+)\s*\(\d+\s*x\)', filename)
    if m:
        return m.group(1).upper()
    return None


def update_order_in_sheet(order_code: str, machine_name: str, operator: str = ""):
    """
    Find the order in the Google Sheet by order code and update STATUS, MAKINA, MAKINACI.
    ORDER NO column may have '#' prefix (e.g. '#C5835'), so we strip it for comparison.
    """
    try:
        client = _get_client()
        spreadsheet = client.open_by_key(SHEET_ID)

        # Find the worksheet by name
        try:
            worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            logger.error(f"Worksheet '{WORKSHEET_NAME}' not found")
            return False

        # Get all ORDER NO values (column C)
        order_col = worksheet.col_values(COL_ORDER_NO)

        # Search for matching order code
        matched_rows = []
        for idx, cell_value in enumerate(order_col):
            # Strip '#' prefix and whitespace for comparison
            clean = cell_value.strip().lstrip('#').strip().upper()
            if clean == order_code:
                matched_rows.append(idx + 1)  # 1-indexed row

        if not matched_rows:
            logger.warning(f"Order code '{order_code}' not found in sheet")
            return False

        # Update all matching rows
        for row in matched_rows:
            worksheet.update_cell(row, COL_STATUS, STATUS_VALUE)
            worksheet.update_cell(row, COL_MAKINA, machine_name)
            if operator:
                worksheet.update_cell(row, COL_MAKINACI, operator)

        logger.info(f"Updated {len(matched_rows)} row(s) for order '{order_code}' in sheet")
        return True

    except Exception as e:
        logger.error(f"Google Sheets update failed for '{order_code}': {e}")
        return False


def update_orders_for_jobs(jobs: list[dict], machine_name: str, operator: str = ""):
    """
    Process multiple completed jobs — extract order codes and update sheet.
    Called when jobs are completed (via Print or Done button).
    """
    for job in jobs:
        filename = job.get("filename", "")
        order_code = extract_order_code(filename)
        if order_code:
            update_order_in_sheet(order_code, machine_name, operator)
        else:
            logger.warning(f"Could not extract order code from: {filename}")
