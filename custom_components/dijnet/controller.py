"""Module for Dijnet controller."""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from os import makedirs, path, remove
from typing import TYPE_CHECKING, Any, Self

import anyio
import pytz
import yaml
from homeassistant.util import Throttle, slugify
from pyquery import PyQuery

from .const import ATTR_TOTAL_AMOUNT, DATA_CONTROLLER, DOMAIN
from .dijnet_session import DijnetSession
from .invoice_list_parser import format_dijnet_date, parse_invoice_list_from_js

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

MIN_DATE = "1990-01-01"
DATE_FORMAT = "%Y.%m.%d"
MIN_TIME_BETWEEN_UPDATES = timedelta(hours=3)
MIN_TIME_BETWEEN_ISSUER_UPDATES = timedelta(days=1)
PAID_INVOICES_FILENAME = ".dijnet_paid_invoices_{0}.yaml"
REGISTRY_FILENAME = ".dijnet_registry_{0}.yaml"
ATTR_REGISTRY_NEXT_QUERY_DATE = "next_query_date"

ATTR_PROVIDER = "provider"
ATTR_DISPLAY_NAME = "display_name"
ATTR_INVOICE_NO = "invoice_no"
ATTR_ISSUANCE_DATE = "issuance_date"
ATTR_DEADLINE = "deadline"
ATTR_AMOUNT = "amount"
PAID_KEY = "paid"
ATTR_PAID_AT = "paid_at"

TZ = pytz.timezone("Europe/Budapest")


def convert_deadline_to_date(deadline_str: str) -> date:
    """
    Convert deadline value to date.

    Args:
      deadline_str:
        The deadline string in DATE_FORMAT (%Y.%m.%d).

    Returns:
      The converted date object.
    """
    return datetime.strptime(deadline_str, DATE_FORMAT).replace(tzinfo=TZ).date()


def _safe_pyquery_from_bytes(
    page_bytes: bytes, encoding: str = "iso-8859-2", context: str = ""
) -> PyQuery | None:
    """
    Safely create a PyQuery object from bytes with validation and error handling.

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


class InvoiceIssuer:
    """Represents an invoice issuer."""

    def __init__(
        self: Self, issuer_id: str, issuer_name: str, display_name: str, providers: list[str]
    ) -> None:
        """
        Initialize a new instance of InvoiceIssuer class.

        Args:
          issuer_id:
            The registration ID at the invoice issuer.
          issuer_name:
            The name of the invoice issuer.
          display_name:
            The display name of the registration.
          providers:
            The list of providers belongs to issuer.
        """
        self._issuer_id = issuer_id
        self._issuer_name = issuer_name
        self._display_name = display_name
        self._providers = providers

    def __str__(self: Self) -> str:
        """Returns the string representation of the class."""
        return f"{self.issuer} - {self.issuer_id} - {self.display_name} - {self.providers}"

    @property
    def issuer_id(self: Self) -> str:
        """Gets the invoice issuer id."""
        return self._issuer_id

    @property
    def display_name(self: Self) -> str:
        """Gets the display name."""
        return self._display_name

    @property
    def issuer(self: Self) -> str:
        """Gets the invoice issuer name."""
        return self._issuer_name

    @property
    def providers(self: Self) -> list[str]:
        """Gets the list of providers belongs to the issuer"""
        return self._providers


class Invoice:
    """Represents an invoice."""

    def __init__(  # noqa: PLR0913
        self: Self,
        provider: str,
        display_name: str,
        invoice_no: str,
        issuance_date: datetime,
        amount: int,
        deadline: datetime,
        total_amount: int | None = None,
    ):
        """
        Initialize a new instance of Invoice class.

        Args:
          provider:
            The provider.
          display_name:
            The display name.
          invoice_no:
            The invoice number.
          issuance_date:
            The issuance date.
          amount:
            The invoice payable amount.
          deadline:
            The deadline.
          total_amount:
            The invoice total amount (optional, defaults to amount if not provided).
        """
        self._provider = provider
        self._display_name = display_name
        self._invoice_no = invoice_no
        self._issuance_date = issuance_date
        self._amount = amount
        self._deadline = deadline
        self._total_amount = total_amount

    @property
    def provider(self: Self) -> str:
        """Gets the provider."""
        return self._provider

    @property
    def display_name(self: Self) -> str:
        """Gets the display name."""
        return self._display_name

    @property
    def invoice_no(self: Self) -> str:
        """Gets the invoice number."""
        return self._invoice_no

    @property
    def issuance_date(self: Self) -> datetime:
        """Gets the issuance date."""
        return self._issuance_date

    @property
    def amount(self: Self) -> int:
        """Gets the payable amount."""
        return self._amount

    @property
    def total_amount(self: Self) -> int:
        """Gets the invoice total amount (falls back to amount if not set)."""
        return self._total_amount if self._total_amount is not None else self._amount

    @property
    def deadline(self: Self) -> datetime:
        """Gets the deadline."""
        return self._deadline

    def __eq__(self: Self, obj: object):
        """Implements the equality operator."""
        return (
            isinstance(obj, Invoice)
            and obj.provider == self.provider
            and obj.invoice_no == self.invoice_no
        )

    def __hash__(self: Self) -> int:
        """Implements hash so Invoice is hashable (based on provider and invoice_no)."""
        return hash((self.provider, self.invoice_no))

    def to_dictionary(self: Self) -> dict[str, Any]:
        """
        Converts the paid invoice to a dictionary.

        Returns:
          The dictionary contains information of paid invoice.
        """
        return {
            ATTR_PROVIDER: self._provider,
            ATTR_DISPLAY_NAME: self.display_name,
            ATTR_INVOICE_NO: self.invoice_no,
            ATTR_ISSUANCE_DATE: self.issuance_date,
            ATTR_AMOUNT: self.amount,
            ATTR_DEADLINE: self.deadline,
            ATTR_TOTAL_AMOUNT: self.total_amount,
        }

    def __str__(self: Self):
        """Returns the string representation of the class."""
        return self.to_dictionary().__str__()


class PaidInvoice(Invoice):
    """Represents a paid invoice."""

    def __init__(  # noqa: PLR0913
        self: Self,
        provider: str,
        display_name: str,
        invoice_no: str,
        issuance_date: datetime,
        amount: int,
        deadline: datetime,
        paid_at: datetime,
        total_amount: int | None = None,
    ) -> None:
        """
        Initialize a new instance of Invoice class.

        Args:
          provider:
            The provider.
          display_name:
            The display name.
          invoice_no:
            The invoice number.
          issuance_date:
            The issuance date.
          amount:
            The invoice payable amount.
          deadline:
            The deadline.
          paid_at:
            The date of payment.
          total_amount:
            The invoice total amount (optional, defaults to amount if not provided).
        """
        super().__init__(
            provider, display_name, invoice_no, issuance_date, amount, deadline, total_amount
        )
        self._paid_at = paid_at

    @property
    def paid_at(self: Self) -> datetime:
        """Gets the date of payment."""
        return self._paid_at

    @staticmethod
    def from_dictionary(dictionary: dict[str, Any]) -> PaidInvoice:
        """
        Converts a dictionary to PaidInvoice instance.

        Args:
          dictionary:
            The dictionary to convert.

        Returns:
            The converted paid invoice.
        """
        return PaidInvoice(
            dictionary[ATTR_PROVIDER],
            dictionary[ATTR_DISPLAY_NAME],
            dictionary[ATTR_INVOICE_NO],
            dictionary[ATTR_ISSUANCE_DATE].date().isoformat()
            if isinstance(dictionary[ATTR_ISSUANCE_DATE], datetime)
            else dictionary[ATTR_ISSUANCE_DATE],
            dictionary[ATTR_AMOUNT],
            dictionary[ATTR_DEADLINE].date().isoformat()
            if isinstance(dictionary[ATTR_DEADLINE], datetime)
            else dictionary[ATTR_DEADLINE],
            dictionary[ATTR_PAID_AT],
            dictionary.get(ATTR_TOTAL_AMOUNT),
        )

    def to_dictionary(self: Self) -> dict[str, Any]:
        """
        Converts the paid invoice to a dictionary.

        Returns:
          The dictionary contains information of paid invoice.
        """
        res = super().to_dictionary()
        res[ATTR_PAID_AT] = self.paid_at

        return res


class DijnetController:
    """Responsible for providing data from Dijnet website."""

    def __init__(
        self: Self,
        username: str,
        password: str,
        download_dir: str | None = None,
        encashment_reported_as_paid_after_deadline: bool = False,
    ) -> None:
        """
        Initialize a new instance of DijnetController class.

        Args:
          username:
            The registered username.
          password:
            The password for user.
          download_dir:
            Optional download directory. If set then the invoice
            files are downloaded to that location.
          encashment_reported_as_paid_after_deadline:
            The value indicates whether the encashment
            should be reported as paid after deadline,
        """
        self._username = username
        self._password = password
        self._download_dir = download_dir
        self._encashment_reported_as_paid_after_deadline = (
            encashment_reported_as_paid_after_deadline
        )
        self._registry: dict[str, str] = None
        self._unpaid_invoices: list[Invoice] = []
        self._paid_invoices: list[Invoice] = []
        self._issuers: list[InvoiceIssuer] = []
        self._remove_old_files()

    def _remove_old_files(self: Self) -> None:
        """
        Removes the old registry and paid invoices files,
        because they could be corrupted if multiple accounts handled.
        """
        # remove old registry and paid invoice files (they might be corrupted)
        if path.exists(".dijnet_paid_invoices.yaml"):
            try:
                remove(".dijnet_paid_invoices.yaml")
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Failed to remove .dijnet_paid_invoices.yaml file")

        if path.exists(".dijnet_registry.yaml"):
            try:
                remove(".dijnet_registry.yaml")
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Failed to remove .dijnet_registry.yaml file")

    async def get_unpaid_invoices(self: Self) -> list[Invoice]:
        """
        Gets the list of unpaid invoices.

        Returns:
          The list of unpaid invoices.
        """
        await self.update_invoices()
        return self._unpaid_invoices

    async def get_paid_invoices(self: Self) -> list[Invoice]:
        """
        Gets the list of paid invoices.

        Returns:
          The list of paid invoices.
        """
        await self.update_invoices()
        return self._paid_invoices

    async def get_issuers(self: Self) -> list[InvoiceIssuer]:
        """
        Gets the list of registered invoice issuers.

        Returns:
          The list of registered invoice issuers.
        """
        await self.update_registered_issuers()
        return self._issuers

    @Throttle(MIN_TIME_BETWEEN_ISSUER_UPDATES)
    async def update_registered_issuers(self: Self) -> None:  # noqa: C901
        """Updates the registered issuers list."""
        issuers: list[InvoiceIssuer] = []

        _LOGGER.debug("Updating issuers.")

        async with DijnetSession() as session:
            await session.get_root_page()

            if not await session.post_login(self._username, self._password):
                return

            await session.get_main_page()

            search_page = await session.get_invoice_search_page()

            # Try to extract ropts with improved regex (handles CDATA, spacing, minification)
            match = re.search(
                r"var\s+ropts\s*=\s*(\[.*?\]);", search_page.decode("iso-8859-2"), re.DOTALL
            )
            raw_providers: list[Any] = []
            provider_alias_mapping: dict[str, list[str]] = {}

            if match:
                providers_json = match.group(1)
                try:
                    raw_providers = json.loads(providers_json)
                    _LOGGER.debug(
                        "Successfully extracted %d providers from ropts", len(raw_providers)
                    )
                except json.JSONDecodeError as e:
                    _LOGGER.warning("Failed to parse providers JSON from search page: %s", e)
            else:
                _LOGGER.warning(
                    "Failed to extract providers JSON from search page. "
                    "This may be due to a website update. Attempting fallback method."
                )

            # Fallback: Extract provider mappings from invoice list page
            if not raw_providers:
                _LOGGER.info("Using fallback method to extract provider mappings from invoice list")
                try:
                    invoice_list_page = await session.get_invoice_list_page()
                    invoice_list_pyquery = PyQuery(
                        invoice_list_page.decode("iso-8859-2").encode("utf-8")
                    )
                    for row in invoice_list_pyquery.find(".table > tbody > tr").items():
                        provider_name = row.children("td:nth-child(1)").text()
                        alias = row.children("td:nth-child(2)").text()
                        if provider_name and alias:
                            # Store mapping from alias to provider name
                            if alias not in provider_alias_mapping:
                                provider_alias_mapping[alias] = []
                            if provider_name not in provider_alias_mapping[alias]:
                                provider_alias_mapping[alias].append(provider_name)
                    _LOGGER.debug(
                        "Extracted %d provider-alias mappings from invoice list",
                        len(provider_alias_mapping),
                    )
                except Exception:
                    _LOGGER.exception("Fallback method also failed")

            await session.get_new_providers_page()

            invoice_providers_response = await session.get_registered_providers_page()

            invoice_providers_response_pquery = PyQuery(
                invoice_providers_response.decode("iso-8859-2").encode("utf-8")
            )
            for row in invoice_providers_response_pquery.find(".table > tbody > tr").items():
                issuer_name = row.children("td:nth-child(1)").text()
                issuer_id = row.children("td:nth-child(2)").text()
                display_name = row.children("td:nth-child(3)").text() or issuer_id

                # First try to get providers from ropts
                providers = [
                    raw_provider["szlaszolgnev"]
                    for raw_provider in raw_providers
                    if (raw_provider["alias"] or raw_provider["aliasnev"]) == display_name
                ]

                # If no providers found from ropts, use fallback mapping
                if not providers and display_name in provider_alias_mapping:
                    providers = provider_alias_mapping[display_name]
                    _LOGGER.debug("Using fallback mapping for %s: %s", display_name, providers)

                issuer = InvoiceIssuer(issuer_id, issuer_name, display_name, providers)
                issuers.append(issuer)
                _LOGGER.debug("Issuer found (%s)", issuer)

            self._issuers = issuers

    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    async def update_invoices(self: Self) -> None:  # noqa: PLR0912, PLR0915, C901
        """Updates the invoice lists."""
        _LOGGER.debug("Updating invoices.")

        if self._registry is None:
            await self._initialize_registry_and_unpaid_invoices()

        async with DijnetSession() as session:
            await session.get_root_page()

            if not await session.post_login(self._username, self._password):
                return

            from_date = self._registry[ATTR_REGISTRY_NEXT_QUERY_DATE]
            to_date = datetime.now(TZ).date().isoformat()

            await session.get_main_page()

            # First, load the current unpaid invoices from szamla_list_uj
            # This is more reliable for current balance tracking
            _LOGGER.debug("Loading current unpaid invoices from szamla_list_uj")
            current_unpaid_invoices: list[Invoice] = []
            try:
                invoice_list_page = await session.get_invoice_list_page()

                # Try to parse invoices from JavaScript pushSz calls first
                js_invoice_data = parse_invoice_list_from_js(invoice_list_page)

                if js_invoice_data:
                    _LOGGER.debug(
                        "Using JavaScript-based parser, found %d pushSz calls", len(js_invoice_data)
                    )
                    for invoice_data in js_invoice_data:
                        invoice = self._create_invoice_from_js_data(invoice_data)
                        if invoice:
                            current_unpaid_invoices.append(invoice)

                    _LOGGER.debug(
                        "Parsed %d current unpaid invoices from JavaScript data",
                        len(current_unpaid_invoices),
                    )
                else:
                    # Fallback to DOM-based parsing if no pushSz calls found
                    _LOGGER.debug("No pushSz calls found, falling back to DOM-based parsing")
                    invoice_list_pyquery = PyQuery(
                        invoice_list_page.decode("iso-8859-2").encode("utf-8")
                    )
                    for row in invoice_list_pyquery.find(".table > tbody > tr").items():
                        # Parse unpaid invoices from the current list
                        # These should all be unpaid based on the page name "Fizetendő számlák"
                        try:
                            invoice = self._create_invoice_from_row(row)
                            current_unpaid_invoices.append(invoice)
                        except Exception as e:  # noqa: BLE001
                            _LOGGER.warning("Failed to parse invoice from list page: %s", e)

                    _LOGGER.debug("Found %d current unpaid invoices", len(current_unpaid_invoices))
            except Exception as e:  # noqa: BLE001
                _LOGGER.warning("Failed to load current unpaid invoices from list page: %s", e)

            # Now also search for invoices in the specified date range
            # This helps track paid invoices and historical data
            search_page = await session.get_invoice_search_page()
            search_page_pyquery = PyQuery(search_page.decode("iso-8859-2").encode("utf-8"))

            # Extract vfw_token - look for any hidden input with name vfw_token
            vfw_token = None
            try:
                vfw_token_elements = search_page_pyquery.find("input[name=vfw_token]")
                if vfw_token_elements:
                    vfw_token = next(vfw_token_elements.items()).val()
                    _LOGGER.debug("Successfully extracted vfw_token")
                else:
                    _LOGGER.warning(
                        "Failed to extract vfw_token: No input element with name "
                        "'vfw_token' found. Will use only current unpaid invoices "
                        "from list page."
                    )
                    # Continue with just the current unpaid invoices
                    vfw_token = None
            except (StopIteration, AttributeError) as e:
                _LOGGER.warning("Failed to extract vfw_token: %s. Using only current invoices.", e)
                vfw_token = None

            possible_new_paid_invoices: list[PaidInvoice] = []
            possible_new_unpaid_invoices: list[Invoice] = []

            # Only do search if we have vfw_token
            if vfw_token:
                search_result = await session.post_search_invoice(
                    "", "", vfw_token, from_date, to_date
                )

                invoices_pyquery = PyQuery(search_result.decode("iso-8859-2").encode("utf-8"))
                index = 0
                for row in invoices_pyquery.find(".table > tbody > tr").items():
                    invoice: Invoice = None
                    is_paid: bool | None = self._is_invoice_paid(row)
                    if is_paid is None:
                        _LOGGER.error(
                            "Failed to determine invoice state. State column text: %s",
                            row.children("td:nth-child(8)").text(),
                        )
                        continue

                    if is_paid:
                        await session.get_invoice_page(index)
                        invoice_history_page = await session.get_invoice_history_page()

                        # Safely parse the invoice history page with validation
                        invoice_history_page_response_pyquery = _safe_pyquery_from_bytes(
                            invoice_history_page, encoding="iso-8859-2", context="invoice_history"
                        )

                        # Try to find payment info in the history page
                        payment_info_found = False
                        if invoice_history_page_response_pyquery is not None:
                            for history_row in invoice_history_page_response_pyquery.find(
                                ".table tr"
                            ).items():
                                payment_text = history_row.children("td:nth-child(4)").text()
                                is_successful_payment = payment_text == "**Sikeres fizetés**"
                                if is_successful_payment:
                                    paid_at = (
                                        datetime.strptime(
                                            history_row.children("td:nth-child(1)").text(),
                                            DATE_FORMAT,
                                        )
                                        .replace(tzinfo=TZ)
                                        .date()
                                        .isoformat()
                                    )
                                    invoice = self._create_invoice_from_row(row, paid_at)
                                    possible_new_paid_invoices.append(invoice)
                                    payment_info_found = True
                                    break

                        # Fallback: payment info not found in history, use deadline as paid_at
                        if not payment_info_found:
                            if invoice_history_page_response_pyquery is None:
                                _LOGGER.debug(
                                    "Invoice history page could not be parsed, "
                                    "using fallback date for paid invoice"
                                )
                            paid_at = (
                                datetime.strptime(
                                    row.children("td:nth-child(6)").text(), DATE_FORMAT
                                )
                                .replace(tzinfo=TZ)
                                .date()
                                .isoformat()
                            )
                            invoice = self._create_invoice_from_row(row, paid_at)
                            possible_new_paid_invoices.append(invoice)

                    else:
                        invoice = self._create_invoice_from_row(row)
                        possible_new_unpaid_invoices.append(invoice)

                    if self._download_dir != "":
                        directory = path.join(self._download_dir, slugify(invoice.provider))
                        makedirs(directory, exist_ok=True)
                        if not isinstance(invoice, PaidInvoice):
                            await session.get_invoice_page(index)

                        invoice_download_page = await session.get_invoice_download_page()

                        unpaid_invoice_download_page_response_pyquery = PyQuery(
                            invoice_download_page.decode("iso-8859-2").encode("utf-8")
                        )

                        for downloadable_link in unpaid_invoice_download_page_response_pyquery.find(
                            "#content_bs a[href*=szamla_pdf], a[href*=szamla_xml]"
                        ).items():
                            href = downloadable_link.attr("href")
                            extension = href.split("?")[0].split("_")[-1]
                            name = href.split("?")[0][:-4]
                            filename = (
                                slugify(
                                    f"{datetime.fromisoformat(invoice.issuance_date).strftime('%Y%m%d')}_{invoice.invoice_no}_{name}"
                                )
                                + f".{extension}"
                            )
                            download_url = f"https://www.dijnet.hu/ekonto/control/{href}"
                            _LOGGER.debug("Downloadable file found (%s).", download_url)

                            full_path = anyio.Path(directory) / filename

                            if await full_path.exists():
                                _LOGGER.debug("File already downloaded (%s)", full_path)
                            else:
                                _LOGGER.info(
                                    "Downloading file (%s -> %s).", download_url, full_path
                                )
                                file_content = await session.download(download_url)
                                async with await anyio.open_file(full_path, "wb") as file:
                                    await file.write(file_content)

                    index += 1
                    await session.get_invoice_list_page()

            # Add current unpaid invoices from the list page
            # These are more reliable for current balance
            for current_invoice in current_unpaid_invoices:
                if current_invoice not in possible_new_unpaid_invoices:
                    possible_new_unpaid_invoices.append(current_invoice)

            paid_invoices = self._paid_invoices.copy()
            unpaid_invoices = self._unpaid_invoices.copy()
            new_paid_invoices: list[PaidInvoice] = []
            for possible_new_paid_invoice in possible_new_paid_invoices:
                already_exists = False
                for paid_invoice in paid_invoices:
                    if paid_invoice == possible_new_paid_invoice:
                        already_exists = True
                        break

                if not already_exists:
                    paid_invoices.append(possible_new_paid_invoice)
                    new_paid_invoices.append(possible_new_paid_invoice)
                    for unpaid_invoice in unpaid_invoices:
                        if unpaid_invoice == possible_new_paid_invoice:
                            unpaid_invoices.remove(unpaid_invoice)
                            break

            for possible_new_unpaid_invoice in possible_new_unpaid_invoices:
                already_exists = False
                for unpaid_invoice in unpaid_invoices:
                    if unpaid_invoice == possible_new_unpaid_invoice:
                        already_exists = True
                        break

                if not already_exists:
                    unpaid_invoices.append(possible_new_unpaid_invoice)

            if len(new_paid_invoices) > 0:
                async with await anyio.open_file(
                    get_paid_invoices_filename(self._username), "a"
                ) as file:
                    await file.write("\n")
                    await file.write(
                        yaml.dump(
                            [x.to_dictionary() for x in new_paid_invoices],
                            default_flow_style=False,
                        )
                    )

            next_query_date = (
                (datetime.fromisoformat(to_date) - timedelta(days=31)).date().isoformat()
            )

            for unpaid_invoice in unpaid_invoices:
                next_query_date = min(next_query_date, unpaid_invoice.issuance_date)

            registry = {ATTR_REGISTRY_NEXT_QUERY_DATE: next_query_date}

            async with await anyio.open_file(get_registry_filename(self._username), "w") as file:
                await file.write(yaml.dump(registry, default_flow_style=False))

            self._registry = registry
            self._unpaid_invoices = unpaid_invoices
            self._paid_invoices = paid_invoices

    def _create_invoice_from_row(
        self: Self, row: PyQuery, paid_at: datetime | None = None
    ) -> Invoice:
        provider = row.children("td:nth-child(1)").text()
        display_name = row.children("td:nth-child(2)").text()
        invoice_no = row.children("td:nth-child(3)").text()
        issuance_date = (
            datetime.strptime(row.children("td:nth-child(4)").text(), DATE_FORMAT)
            .replace(tzinfo=TZ)
            .date()
            .isoformat()
        )
        total_amount = int(re.sub(r"[^0-9\-]+", "", row.children("td:nth-child(5)").text()))
        amount = int(re.sub(r"[^0-9\-]+", "", row.children("td:nth-child(7)").text()))
        deadline = convert_deadline_to_date(row.children("td:nth-child(6)").text()).isoformat()

        invoice: Invoice = None
        if paid_at:
            invoice = PaidInvoice(
                provider,
                display_name,
                invoice_no,
                issuance_date,
                amount,
                deadline,
                paid_at,
                total_amount,
            )
        else:
            invoice = Invoice(
                provider,
                display_name,
                invoice_no,
                issuance_date,
                amount,
                deadline,
                total_amount,
            )

        _LOGGER.info("Invoice created. %s", invoice)

        return invoice

    def _create_invoice_from_js_data(self: Self, invoice_data: dict[str, Any]) -> Invoice | None:
        """
        Create an Invoice object from JavaScript pushSz data.

        Args:
          invoice_data:
            The invoice data dictionary from pushSz call with keys like
            'bdt', 'fdt', 'szn', 'oss', 'egy', 'dst', 'rid', 'gid', 'tid'.

        Returns:
          An Invoice object, or None if the invoice should be skipped
          (e.g., if it's a "Csoportos beszedés" invoice and the configuration
          says to treat them as paid after deadline has passed).
        """
        try:
            # Extract fields from JavaScript data
            provider = invoice_data.get("szn", "Unknown")
            # Use provider as display_name since we don't have separate display_name in JS data
            display_name = provider
            # Use gid (group ID) as invoice number since we don't have invoice_no in JS data
            # If gid is negative or missing, use tid or a combination
            gid = invoice_data.get("gid", 0)
            tid = invoice_data.get("tid", 0)
            invoice_no = f"GID{gid}_TID{tid}"

            # Convert dates from YYYYMMDD to ISO-8601
            issuance_date = format_dijnet_date(invoice_data["bdt"])
            deadline = format_dijnet_date(invoice_data["fdt"])

            # Get amounts
            total_amount = invoice_data.get("oss", 0)
            amount = invoice_data.get("egy", total_amount)

            # Check status to determine if should be included
            status = invoice_data.get("dst", "")

            # Handle "Csoportos beszedés" (direct debit/collection) invoices
            is_collection = "Csoportos beszedés" in status or "Beszedés alatt" in status
            if is_collection and self._encashment_reported_as_paid_after_deadline:
                # Check if deadline has passed
                deadline_date = datetime.strptime(deadline, "%Y-%m-%d").replace(tzinfo=TZ).date()
                if deadline_date < datetime.now(tz=TZ).date():
                    # Don't include in unpaid list (treated as paid after deadline)
                    _LOGGER.debug(
                        "Skipping collection invoice %s (deadline passed): %s",
                        invoice_no,
                        provider,
                    )
                    return None

            invoice = Invoice(
                provider,
                display_name,
                invoice_no,
                issuance_date,
                amount,
                deadline,
                total_amount,
            )

            _LOGGER.info("Invoice created from JS data. %s", invoice)

        except (KeyError, ValueError) as e:
            _LOGGER.warning("Failed to create invoice from JS data: %s. Data: %s", e, invoice_data)
            return None
        else:
            return invoice

    def _is_invoice_paid(self: Self, row: PyQuery) -> bool | None:
        state_text = row.children("td:nth-child(8)").text()

        paid_states: list[str] = ["Rendezett", "Fizetve"]
        unpaid_states: list[str] = [
            "Tovább a fizetéshez",
            "Rendezetlen",
            "Mobiltelefonra küldve",
            "Internetbanknak átadva",
        ]

        if any(state in state_text for state in paid_states):
            return True

        if any(state in state_text for state in unpaid_states):
            return False

        collection: bool = "Csoportos beszedés" in state_text or "Beszedés alatt" in state_text
        if collection:
            if self._encashment_reported_as_paid_after_deadline:
                deadline = convert_deadline_to_date(row.children("td:nth-child(6)").text())
                return deadline < datetime.now(tz=TZ).date()
            return False
        return None

    async def _initialize_registry_and_unpaid_invoices(self: Self) -> None:
        paid_invoices = None
        registry = None
        paid_invoices_filename = get_paid_invoices_filename(self._username)
        registry_filename = get_registry_filename(self._username)
        try:
            _LOGGER.debug('Loading registry from "%s"', registry_filename)
            async with await anyio.open_file(registry_filename) as file:
                registry_file_content = await file.read()
                registry = yaml.safe_load(registry_file_content)

            if isinstance(registry[ATTR_REGISTRY_NEXT_QUERY_DATE], datetime):
                registry[ATTR_REGISTRY_NEXT_QUERY_DATE] = (
                    registry[ATTR_REGISTRY_NEXT_QUERY_DATE].date().isoformat()
                )
            elif isinstance(registry[ATTR_REGISTRY_NEXT_QUERY_DATE], date):
                registry[ATTR_REGISTRY_NEXT_QUERY_DATE] = registry[
                    ATTR_REGISTRY_NEXT_QUERY_DATE
                ].isoformat()

            paid_invoices = []
            _LOGGER.debug('Loading invoices from "%s"', paid_invoices_filename)
            async with await anyio.open_file(paid_invoices_filename) as file:
                paid_invoices_file_content = await file.read()
                data = yaml.safe_load(paid_invoices_file_content)
                for paid_invoice_dict in data:
                    try:
                        paid_invoices.append(PaidInvoice.from_dictionary(paid_invoice_dict))
                    except Exception as exception:  # noqa: BLE001
                        _LOGGER.warning("Invalid paid invoice data: %s", exception)
        except FileNotFoundError:
            _LOGGER.debug('"%s" or "%s" not found.', paid_invoices_filename, registry_filename)
            paid_invoices = []
            registry = {ATTR_REGISTRY_NEXT_QUERY_DATE: MIN_DATE}

        self._paid_invoices = paid_invoices
        self._registry = registry


def set_controller(hass: HomeAssistant, user_name: str, controller: DijnetController) -> None:
    """
    Sets the controller instance for the specified username in Home Assistant data container.

    Args:
      hass:
        The Home Assistant instance.
      user_name:
        The registered username.
      controller:
        The controller instance to set.
    """
    hass.data[DOMAIN][DATA_CONTROLLER][user_name] = controller


def get_controller(hass: HomeAssistant, user_name: str) -> DijnetController:
    """
    Gets the controller instance for the specified username from Home Assistant data container.

    Args:
      hass:
        The Home Assistant instance.
      user_name:
        The registered username.

    Returns:
      The controller associated to the specified username.
    """
    return hass.data[DOMAIN][DATA_CONTROLLER].get(user_name)


def is_controller_exists(hass: HomeAssistant, user_name: str) -> bool:
    """
    Gets the value indicates whether a controller associated to the specified
    username in Home Assistant data container.

    Args:
      hass:
        The Home Assistant instance.
      user_name:
        The registered username.

    Returns:
      The value indicates whether a controller associated to the specified
      username in Home Assistant data container.

    """
    return user_name in hass.data[DOMAIN][DATA_CONTROLLER]


def get_paid_invoices_filename(username: str) -> str:
    """Gets the paid invoices filename."""
    return PAID_INVOICES_FILENAME.format(slugify(username))


def get_registry_filename(username: str) -> str:
    """Gets the registry filename."""
    return REGISTRY_FILENAME.format(slugify(username))
