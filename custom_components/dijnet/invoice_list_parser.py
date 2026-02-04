"""Module for parsing invoice list from Dijnet szamla_list_uj page."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

_LOGGER = logging.getLogger(__name__)


def parse_invoice_list_from_js(page_bytes: bytes) -> list[dict[str, Any]]:
    """
    Parse invoice list from JavaScript pushSz() calls in the HTML page.

    This function extracts invoice data from the szamla_list_uj page by parsing
    the JavaScript pushSz({...}) calls that populate the invoice table data.

    Args:
      page_bytes:
        The raw HTML page content as bytes (typically ISO-8859-2 encoded).

    Returns:
      A list of dictionaries containing invoice data. Each dictionary contains
      keys like 'bdt', 'fdt', 'szn', 'oss', 'egy', 'dst', 'rid', 'gid', 'tid'.
      Returns an empty list if no pushSz calls are found or parsing fails.
    """
    try:
        # Decode the HTML as ISO-8859-2
        page_html = page_bytes.decode("iso-8859-2")
    except UnicodeDecodeError:
        _LOGGER.exception("Failed to decode page as ISO-8859-2")
        return []

    # Find all pushSz({...}); calls using regex
    # The pattern looks for pushSz( followed by { ... } and ending with );
    pattern = r"pushSz\s*\(\s*(\{[^}]+\})\s*\)"
    matches = re.findall(pattern, page_html)

    if not matches:
        _LOGGER.debug("No pushSz calls found in the page")
        return []

    invoices = []
    for i, match in enumerate(matches):
        try:
            # Parse the JSON object
            invoice_data = json.loads(match)
            invoices.append(invoice_data)
        except json.JSONDecodeError:
            _LOGGER.warning("Failed to parse pushSz call %d: %s", i, match)
            continue

    _LOGGER.debug("Successfully parsed %d invoices from pushSz calls", len(invoices))
    return invoices


def format_dijnet_date(date_int: int) -> str:
    """
    Convert Dijnet date format (YYYYMMDD as integer) to ISO-8601 date string (YYYY-MM-DD).

    Args:
      date_int:
        The date as an integer in YYYYMMDD format (e.g., 20260121).

    Returns:
      The date as an ISO-8601 formatted string (e.g., "2026-01-21").
    """
    date_str = str(date_int)
    if len(date_str) != 8:  # noqa: PLR2004
        msg = f"Invalid date format: {date_int}. Expected YYYYMMDD (8 digits)."
        raise ValueError(msg)

    year = date_str[0:4]
    month = date_str[4:6]
    day = date_str[6:8]

    return f"{year}-{month}-{day}"
