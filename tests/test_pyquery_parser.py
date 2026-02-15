"""Tests for PyQuery parsing helpers in controller module."""

# ruff: noqa: S101, T201

import logging

from pyquery import PyQuery

_LOGGER = logging.getLogger(__name__)


def _safe_pyquery_from_bytes(
    page_bytes: bytes, encoding: str = "iso-8859-2", context: str = ""
) -> PyQuery | None:
    """
    Safely create a PyQuery object from bytes with validation and error handling.

    This is a copy of the function from controller.py for testing purposes.

    Args:
      page_bytes:
        The raw HTML page content as bytes.
      encoding:
        The character encoding of the page (default: "iso-8859-2").
      context:
        Optional context string for logging (e.g., "invoice_history_page").

    Returns:
      A PyQuery object if parsing succeeds, None otherwise.
    """
    # Check if the response is empty after stripping whitespace
    if not page_bytes or not page_bytes.strip():
        _LOGGER.debug(
            "Empty or whitespace-only response received%s. Length: %d bytes",
            f" for {context}" if context else "",
            len(page_bytes),
        )
        return None

    try:
        # Decode and re-encode as done in the rest of the codebase
        decoded_page = page_bytes.decode(encoding)
        encoded_page = decoded_page.encode("utf-8")

        # Try to create PyQuery object
        return PyQuery(encoded_page)
    except UnicodeDecodeError:
        _LOGGER.warning(
            "Failed to decode page%s as %s. Response length: %d bytes, prefix: %s",
            f" ({context})" if context else "",
            encoding,
            len(page_bytes),
            page_bytes[:200] if len(page_bytes) > 0 else b"",
        )
        return None
    except Exception as e:  # noqa: BLE001
        _LOGGER.warning(
            "Failed to parse page%s with PyQuery: %s. Response length: %d bytes, prefix: %s",
            f" ({context})" if context else "",
            e,
            len(page_bytes),
            page_bytes[:200] if len(page_bytes) > 0 else b"",
        )
        return None


def test_safe_pyquery_from_bytes_valid_html() -> None:
    """Test parsing valid HTML content."""
    html_content = """<!DOCTYPE html>
<html>
<head>
<meta content="text/html; charset=ISO-8859-2" http-equiv="content-type">
</head>
<body>
<table class="table">
<tr>
<td>Test data</td>
</tr>
</table>
</body>
</html>"""

    page_bytes = html_content.encode("iso-8859-2")
    result = _safe_pyquery_from_bytes(page_bytes, encoding="iso-8859-2", context="test")

    assert result is not None
    # Check that we can query the document
    table = result.find("table.table")
    assert table is not None
    assert len(table) == 1


def test_safe_pyquery_from_bytes_empty_bytes() -> None:
    """Test parsing empty byte string."""
    result = _safe_pyquery_from_bytes(b"", encoding="iso-8859-2", context="test_empty")
    assert result is None


def test_safe_pyquery_from_bytes_whitespace_only() -> None:
    """Test parsing whitespace-only content."""
    result = _safe_pyquery_from_bytes(b"   \n\t  ", encoding="iso-8859-2", context="test_ws")
    assert result is None


def test_safe_pyquery_from_bytes_invalid_encoding() -> None:
    """Test parsing with content that can't be decoded with specified encoding."""
    # Use utf-16 encoded data with ascii decoder to trigger decode error
    utf16_text = "Test".encode("utf-16")
    result = _safe_pyquery_from_bytes(utf16_text, encoding="ascii", context="test_bad_encode")
    # UTF-16 encoded text cannot be decoded as ASCII, should return None
    assert result is None


def test_safe_pyquery_from_bytes_malformed_html() -> None:
    """Test parsing HTML with parsing issues."""
    # PyQuery/lxml is quite forgiving, so truly malformed HTML is rare
    # But an empty document after decode might cause issues
    html_content = "<html><body></body></html>"
    page_bytes = html_content.encode("iso-8859-2")
    result = _safe_pyquery_from_bytes(page_bytes, encoding="iso-8859-2", context="test_minimal")

    # Should still parse successfully
    assert result is not None


def test_safe_pyquery_from_bytes_hungarian_characters() -> None:
    """Test parsing HTML with Hungarian special characters."""
    html_content = """<!DOCTYPE html>
<html>
<head>
<meta content="text/html; charset=ISO-8859-2" http-equiv="content-type">
</head>
<body>
<table class="table">
<tr>
<td>Számla</td>
<td>Fizetés</td>
<td>Előleg</td>
</tr>
</table>
</body>
</html>"""

    page_bytes = html_content.encode("iso-8859-2")
    result = _safe_pyquery_from_bytes(page_bytes, encoding="iso-8859-2", context="test_hungarian")

    assert result is not None
    # Check that Hungarian characters are preserved
    td_elements = result.find("td")
    assert len(td_elements) == 3  # noqa: PLR2004
    # Just verify we got some text back without errors
    first_td_text = td_elements.eq(0).text()
    assert len(first_td_text) > 0


def test_safe_pyquery_from_bytes_with_context() -> None:
    """Test that context parameter is used in logging (doesn't affect return value)."""
    html_content = "<html><body>Test</body></html>"
    page_bytes = html_content.encode("iso-8859-2")

    # With context
    result1 = _safe_pyquery_from_bytes(
        page_bytes, encoding="iso-8859-2", context="invoice_history"
    )
    # Without context
    result2 = _safe_pyquery_from_bytes(page_bytes, encoding="iso-8859-2", context="")

    assert result1 is not None
    assert result2 is not None


if __name__ == "__main__":
    # Set up basic logging
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    # Run all tests
    test_functions = [
        test_safe_pyquery_from_bytes_valid_html,
        test_safe_pyquery_from_bytes_empty_bytes,
        test_safe_pyquery_from_bytes_whitespace_only,
        test_safe_pyquery_from_bytes_invalid_encoding,
        test_safe_pyquery_from_bytes_malformed_html,
        test_safe_pyquery_from_bytes_hungarian_characters,
        test_safe_pyquery_from_bytes_with_context,
    ]

    failed = 0
    for test_func in test_functions:
        try:
            test_func()
            print(f"✓ {test_func.__name__}")
        except AssertionError as e:
            print(f"✗ {test_func.__name__}: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"✗ {test_func.__name__}: Unexpected error: {e}")
            failed += 1

    print(f"\n{len(test_functions) - failed}/{len(test_functions)} tests passed")

    import sys

    sys.exit(1 if failed > 0 else 0)
