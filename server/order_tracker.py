"""
Order Tracker integration for DTF Monitor.

Drop-in replacement for google_sheets.py: when a job is completed (Print/Done),
it calls the self-hosted Order Tracker API to mark the matching order printed and
record the machine + operator — no Google Sheets involved.

Config via environment variables (with sensible defaults):
    ORDER_TRACKER_URL      base URL of the Order Tracker app
    ORDER_TRACKER_API_KEY  the INTEGRATION_API_KEY configured on the server
"""

import re
import os
import json
import logging
from typing import Optional
import requests

logger = logging.getLogger(__name__)

# ── Config ──
# Read from environment first, then fall back to a local (gitignored) ot_config.json
# next to this file: {"url": "...", "apiKey": "..."}
_DEFAULT_URL = "https://order-tracker-production-5a11.up.railway.app"
_cfg = {}
_cfg_path = os.path.join(os.path.dirname(__file__), "ot_config.json")
try:
    with open(_cfg_path, "r", encoding="utf-8") as f:
        _cfg = json.load(f)
except Exception:
    _cfg = {}

API_URL = (os.environ.get("ORDER_TRACKER_URL") or _cfg.get("url") or _DEFAULT_URL).rstrip("/")
API_KEY = os.environ.get("ORDER_TRACKER_API_KEY") or _cfg.get("apiKey", "")
TIMEOUT = 10


def extract_order_code(filename: str) -> Optional[str]:
    """
    Extract order code from filename — the letters+digits token right before '(N x)'.
        '67--IN3300 (1 x) ...'  -> 'IN3300'
        '129-4-C15963 (2 x) ...' -> 'C15963'
        '7--DWC1518 (1 x) ...'  -> 'DWC1518'
    The 'x' in the copies token is case-insensitive and optional: UV files use
    '(1X)', DTF '(1x)', and some files just '(1)'.
    """
    m = re.search(r'([A-Za-z]{1,4}\d+)\s*\(\d+\s*[xX]?\)', filename)
    if m:
        return m.group(1).upper()
    return None


def update_order(order_code: str, machine_name: str, operator: str = "") -> bool:
    """Mark one order printed in the Order Tracker app."""
    if not API_KEY:
        logger.error("ORDER_TRACKER_API_KEY not set; skipping order update")
        return False
    try:
        resp = requests.post(
            f"{API_URL}/integrations/print",
            headers={"X-Api-Key": API_KEY, "Content-Type": "application/json"},
            json={"orderCode": order_code, "machine": machine_name, "operator": operator},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            logger.info(f"Order '{order_code}' updated ({data.get('updated', 0)} row(s))")
            return True
        if resp.status_code == 404:
            logger.warning(f"Order code '{order_code}' not found in Order Tracker")
            return False
        logger.error(f"Order Tracker update failed for '{order_code}': {resp.status_code} {resp.text[:200]}")
        return False
    except Exception as e:
        logger.error(f"Order Tracker request failed for '{order_code}': {e}")
        return False


def get_order_status(order_code: str) -> Optional[dict]:
    """Look up an order's print status in Order Tracker (for the duplicate-print
    warning). Returns the JSON dict, or None on error / no key."""
    if not API_KEY:
        return None
    try:
        resp = requests.get(
            f"{API_URL}/integrations/order-status",
            headers={"X-Api-Key": API_KEY},
            params={"code": order_code},
            timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception as e:
        logger.error(f"Order status check failed for '{order_code}': {e}")
        return None


def update_sheet(order_code: str, part: int, total: int, copies: int, stage: str,
                 machine_name: str = "", operator: str = "", file_name: str = "",
                 printed_count: Optional[int] = None, urgent: bool = False) -> dict:
    """Per-sheet progress from the agent queue: stage = 'downloaded' | 'ripped' | 'printed'.
    Order Tracker keeps one row per sheet and rolls the order up itself
    ("Downloaded 1/5" → "RIP'd 1/5" → "Printed 3/5" → "Printed"). Returns {ok, message}."""
    if not API_KEY:
        logger.error("ORDER_TRACKER_API_KEY not set; skipping sheet update")
        return {"ok": False, "message": "Order Tracker API key not configured"}
    body = {
        "orderCode": order_code, "stage": stage,
        "printStatus": {"printed": "Printed", "ripped": "RIP'd"}.get(stage, "Downloaded"),
        "part": part, "total": total, "copies": copies,
        "machine": machine_name, "operator": operator, "fileName": file_name,
    }
    if printed_count is not None:
        body["printedCount"] = printed_count
    if urgent:
        body["urgent"] = True  # "++" file name → OT flags the order urgent
    try:
        resp = requests.post(
            f"{API_URL}/integrations/print",
            headers={"X-Api-Key": API_KEY, "Content-Type": "application/json"},
            json=body, timeout=TIMEOUT,
        )
        if resp.status_code == 200:
            logger.info(f"Sheet {order_code} #{part}/{total} -> {stage} ({machine_name}/{operator})")
            return {"ok": True, "message": "ok"}
        if resp.status_code == 404:
            logger.warning(f"Sheet update: order '{order_code}' not found in Order Tracker")
            return {"ok": False, "message": f"order {order_code} not found in Order Tracker"}
        logger.error(f"Sheet update failed for '{order_code}': {resp.status_code} {resp.text[:200]}")
        return {"ok": False, "message": f"Order Tracker {resp.status_code}"}
    except Exception as e:
        logger.error(f"Sheet update request failed for '{order_code}': {e}")
        return {"ok": False, "message": str(e)[:120]}


def reset_order(order_code: str) -> dict:
    """Test/reset: drop every sheet the floor reported for this order (OT DELETE /integrations/print)."""
    if not API_KEY:
        return {"ok": False, "message": "Order Tracker API key not configured"}
    try:
        resp = requests.delete(f"{API_URL}/integrations/print", params={"orderCode": order_code},
                               headers={"X-Api-Key": API_KEY}, timeout=TIMEOUT)
        if resp.status_code == 200:
            return {"ok": True, "message": resp.json().get("sheetsDeleted", 0)}
        return {"ok": False, "message": f"Order Tracker {resp.status_code}"}
    except Exception as e:
        return {"ok": False, "message": str(e)[:120]}


def update_orders_for_jobs(jobs: list[dict], machine_name: str, operator: str = ""):
    """
    Process completed jobs — extract order codes and mark them printed.
    Same signature as the old google_sheets.update_orders_for_jobs, so server.py
    only needs to change the import.
    """
    for job in jobs:
        filename = job.get("filename", "")
        order_code = extract_order_code(filename)
        if order_code:
            update_order(order_code, machine_name, operator)
        else:
            logger.warning(f"Could not extract order code from: {filename}")
