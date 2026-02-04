"""Tests for invoice_list_parser module."""

from custom_components.dijnet.invoice_list_parser import (
    format_dijnet_date,
    parse_invoice_list_from_js,
)


def test_parse_invoice_list_from_js_with_multiple_invoices() -> None:
    """Test parsing multiple invoices from pushSz calls."""
    # Sample HTML with multiple pushSz calls (similar to the actual page)
    html_content = """<!DOCTYPE html>
<html>
<head>
<meta content="text/html; charset=ISO-8859-2" http-equiv="content-type">
</head>
<body>
<script>
pushSz({"bdt":20260121,"tid":666991321,"gid":-1638659138,"oss":4281,"szn":"FCSM Zrt.","egy":4281,"rid":0,"dst":"Rendezetlen","fdt":20260215});
pushSz({"bdt":20260121,"tid":666991321,"gid":-1638633213,"oss":2659,"szn":"FV Zrt.","egy":2659,"rid":1,"dst":"Rendezetlen","fdt":20260215});
pushSz({"bdt":20260115,"tid":-1633646386,"gid":-1633646386,"oss":1298,"szn":"Fejérvíz Zrt.","egy":1298,"rid":2,"dst":"Csoportos beszedés","fdt":20260317});
</script>
</body>
</html>"""

    page_bytes = html_content.encode("iso-8859-2")
    invoices = parse_invoice_list_from_js(page_bytes)

    assert len(invoices) == 3  # noqa: PLR2004

    # Check first invoice
    assert invoices[0]["bdt"] == 20260121  # noqa: PLR2004
    assert invoices[0]["szn"] == "FCSM Zrt."
    assert invoices[0]["oss"] == 4281  # noqa: PLR2004
    assert invoices[0]["egy"] == 4281  # noqa: PLR2004
    assert invoices[0]["dst"] == "Rendezetlen"
    assert invoices[0]["fdt"] == 20260215  # noqa: PLR2004
    assert invoices[0]["rid"] == 0

    # Check second invoice
    assert invoices[1]["szn"] == "FV Zrt."
    assert invoices[1]["oss"] == 2659  # noqa: PLR2004

    # Check third invoice (Csoportos beszedés)
    assert invoices[2]["szn"] == "Fejérvíz Zrt."
    assert invoices[2]["dst"] == "Csoportos beszedés"
    assert invoices[2]["oss"] == 1298  # noqa: PLR2004


def test_parse_invoice_list_from_js_no_pushsz() -> None:
    """Test parsing page with no pushSz calls."""
    html_content = """<!DOCTYPE html>
<html>
<head>
<meta content="text/html; charset=ISO-8859-2" http-equiv="content-type">
</head>
<body>
<p>No invoices here</p>
</body>
</html>"""

    page_bytes = html_content.encode("iso-8859-2")
    invoices = parse_invoice_list_from_js(page_bytes)

    assert len(invoices) == 0


def test_parse_invoice_list_from_js_with_special_characters() -> None:
    """Test parsing invoices with Hungarian special characters."""
    html_content = """<!DOCTYPE html>
<html>
<head>
<meta content="text/html; charset=ISO-8859-2" http-equiv="content-type">
</head>
<body>
<script>
pushSz({"bdt":20260101,"tid":123,"gid":456,"oss":1000,"szn":"Vízmű Zrt.","egy":1000,"rid":0,"dst":"Rendezetlen","fdt":20260201});
pushSz({"bdt":20260102,"tid":789,"gid":101,"oss":2000,"szn":"Áramszolgáltató Kft.","egy":2000,"rid":1,"dst":"Csoportos beszedés","fdt":20260202});
</script>
</body>
</html>"""

    page_bytes = html_content.encode("iso-8859-2")
    invoices = parse_invoice_list_from_js(page_bytes)

    assert len(invoices) == 2  # noqa: PLR2004
    assert invoices[0]["szn"] == "Vízmű Zrt."
    assert invoices[1]["szn"] == "Áramszolgáltató Kft."


def test_parse_invoice_list_from_js_invalid_json() -> None:
    """Test parsing page with invalid JSON in pushSz call."""
    html_content = """<!DOCTYPE html>
<html>
<body>
<script>
pushSz({"bdt":20260121,"szn":"Valid Inc.","oss":1000,"egy":1000,"rid":0,"dst":"Rendezetlen","fdt":20260215});
pushSz({invalid json here});
pushSz({"bdt":20260122,"szn":"Another Valid Inc.","oss":2000,"egy":2000,"rid":1,"dst":"Rendezetlen","fdt":20260216});
</script>
</body>
</html>"""

    page_bytes = html_content.encode("iso-8859-2")
    invoices = parse_invoice_list_from_js(page_bytes)

    # Should parse the two valid ones, skip the invalid one
    assert len(invoices) == 2  # noqa: PLR2004
    assert invoices[0]["szn"] == "Valid Inc."
    assert invoices[1]["szn"] == "Another Valid Inc."


def test_format_dijnet_date() -> None:
    """Test converting Dijnet date format to ISO-8601."""
    assert format_dijnet_date(20260121) == "2026-01-21"
    assert format_dijnet_date(20260215) == "2026-02-15"
    assert format_dijnet_date(20260317) == "2026-03-17"
    assert format_dijnet_date(19900101) == "1990-01-01"
    assert format_dijnet_date(20991231) == "2099-12-31"


def test_format_dijnet_date_invalid() -> None:
    """Test format_dijnet_date with invalid input."""
    try:
        format_dijnet_date(123)  # Too short
        assert False, "Should have raised ValueError"  # noqa: B011, PT015
    except ValueError as e:
        assert "Invalid date format" in str(e)

    try:
        format_dijnet_date(202601211)  # Too long (9 digits)
        assert False, "Should have raised ValueError"  # noqa: B011, PT015
    except ValueError as e:
        assert "Invalid date format" in str(e)


if __name__ == "__main__":
    # Run all tests
    test_parse_invoice_list_from_js_with_multiple_invoices()
    test_parse_invoice_list_from_js_no_pushsz()
    test_parse_invoice_list_from_js_with_special_characters()
    test_parse_invoice_list_from_js_invalid_json()
    test_format_dijnet_date()
    test_format_dijnet_date_invalid()
    print("All tests passed!")
