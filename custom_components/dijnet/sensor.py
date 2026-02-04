"""Support for Dijnet."""

import logging
from datetime import date, datetime
from typing import Self

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.sensor import (
    PLATFORM_SCHEMA,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import CONF_DOWNLOAD_DIR, DOMAIN
from .controller import DijnetController, InvoiceIssuer, get_controller

_LOGGER = logging.getLogger(__name__)


def _parse_deadline(raw_deadline: date | datetime | str | None) -> date | None:
    """Convert deadline value to date."""
    if raw_deadline is None:
        return None

    if isinstance(raw_deadline, datetime):
        return raw_deadline.date()

    if isinstance(raw_deadline, date):
        return raw_deadline

    if isinstance(raw_deadline, str):
        for normalized_deadline in (raw_deadline, raw_deadline.replace(".", "-")):
            try:
                return date.fromisoformat(normalized_deadline)
            except ValueError:
                continue
        _LOGGER.debug(
            "Unrecognized deadline format (tried YYYY-MM-DD and YYYY.MM.DD): %s", raw_deadline
        )

    return None


PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_DOWNLOAD_DIR, default=""): cv.string,
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,  # noqa: ARG001
    discovery_info: DiscoveryInfoType = None,  # noqa: ARG001
) -> None:
    """Import yaml config and initiates config flow for Dijnet integration."""
    # Check if entry config exists and skips import if it does.
    if hass.config_entries.async_entries(DOMAIN):
        _LOGGER.warning(
            "Setting up Dijnet integration from yaml is deprecated."
            "Please remove configuration from yaml."
        )
        return

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data=config,
        )
    )


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> bool:
    """
    Setup of Dijnet sensors for the specified config_entry.

    Args:
      hass:
        The Home Assistant instance.
      config_entry:
        The config entry which is used to create sensors.
      async_add_entities:
        The callback which can be used to add new entities to Home Assistant.

    Returns:
      The value indicates whether the setup succeeded.
    """
    _LOGGER.info("Setting up Dijnet sensors.")

    controller = get_controller(hass, config_entry.data[CONF_USERNAME])

    for registered_invoice_issuer in await controller.get_issuers():
        sensors = []
        for provider in registered_invoice_issuer.providers:
            sensors.append(
                InvoiceAmountSensor(
                    controller, config_entry.entry_id, registered_invoice_issuer, provider
                )
            )
            sensors.append(
                InvoiceDeadlineSensor(
                    controller, config_entry.entry_id, registered_invoice_issuer, provider
                )
            )
        async_add_entities(sensors)
        _LOGGER.debug("Sensors added (%s)", registered_invoice_issuer)

    _LOGGER.info("Setting up Dijnet sensors completed.")
    return True


class InvoiceAmountSensor(SensorEntity):
    """Represents an invoice amount sensor."""

    def __init__(
        self: Self,
        controller: DijnetController,
        config_entry_id: str,
        invoice_issuer: InvoiceIssuer,
        provider: str,
    ) -> None:
        """
        Initializes a new instance of `InvoiceAmountSensor` class.

        Args:
          controller:
            The Dijnet controller.
          config_entry_id:
            The unique id of the config entry.
          invoice_issuer:
            The invoice issuer.
          provider:
            The invoice provider.
        """
        self._controller = controller
        self._invoice_issuer = invoice_issuer
        self._attr_unique_id = (
            f"{config_entry_id}_{invoice_issuer.issuer}_"
            f"{invoice_issuer.issuer_id}_{provider}_amount"
        )
        self._provider = provider
        self.entity_description = SensorEntityDescription(
            key="invoice_amount",
            device_class=SensorDeviceClass.MONETARY,
            native_unit_of_measurement="Ft",
            name=f"Dijnet - {provider} fizetendő összeg",
        )

    @property
    def device_info(self: Self) -> DeviceInfo:
        """Returns the device information."""
        return DeviceInfo(
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://dijnet.hu/",
            manufacturer="Dijnet Zrt",
            identifiers={
                (DOMAIN, self._invoice_issuer.issuer + "|" + self._invoice_issuer.issuer_id)
            },
            name=self._invoice_issuer.display_name,
        )

    async def async_update(self: Self) -> None:
        """Called when the entity should update its state."""
        invoices = [
            invoice
            for invoice in await self._controller.get_unpaid_invoices()
            if invoice.display_name == self._invoice_issuer.display_name
            and invoice.provider == self._provider
        ]
        self._attr_native_value = sum([invoice.amount for invoice in invoices])

        # Find the earliest deadline among unpaid invoices
        deadlines = [_parse_deadline(invoice.deadline) for invoice in invoices]
        deadlines = [deadline for deadline in deadlines if deadline is not None]
        next_payment_deadline = min(deadlines) if deadlines else None

        self._attr_extra_state_attributes = {
            "unpaid_invoices": [invoice.to_dictionary() for invoice in invoices],
            "next_payment_deadline": next_payment_deadline.isoformat()
            if next_payment_deadline
            else None,
        }


class InvoiceDeadlineSensor(SensorEntity):
    """Represents an invoice payment deadline sensor."""

    def __init__(
        self: Self,
        controller: DijnetController,
        config_entry_id: str,
        invoice_issuer: InvoiceIssuer,
        provider: str,
    ) -> None:
        """
        Initializes a new instance of `InvoiceDeadlineSensor` class.

        Args:
          controller:
            The Dijnet controller.
          config_entry_id:
            The unique id of the config entry.
          invoice_issuer:
            The invoice issuer.
          provider:
            The invoice provider.
        """
        self._controller = controller
        self._invoice_issuer = invoice_issuer
        self._attr_unique_id = (
            f"{config_entry_id}_{invoice_issuer.issuer}_"
            f"{invoice_issuer.issuer_id}_{provider}_deadline"
        )
        self._provider = provider
        self.entity_description = SensorEntityDescription(
            key="invoice_deadline",
            device_class=SensorDeviceClass.DATE,
            name=f"Dijnet - {provider} fizetési határidő",
        )

    @property
    def device_info(self: Self) -> DeviceInfo:
        """Returns the device information."""
        return DeviceInfo(
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://dijnet.hu/",
            manufacturer="Dijnet Zrt",
            identifiers={
                (DOMAIN, self._invoice_issuer.issuer + "|" + self._invoice_issuer.issuer_id)
            },
            name=self._invoice_issuer.display_name,
        )

    async def async_update(self: Self) -> None:
        """Called when the entity should update its state."""
        invoices = [
            invoice
            for invoice in await self._controller.get_unpaid_invoices()
            if invoice.display_name == self._invoice_issuer.display_name
            and invoice.provider == self._provider
        ]

        # Set the state to the earliest deadline, or None if no unpaid invoices
        deadlines = [_parse_deadline(invoice.deadline) for invoice in invoices]
        deadlines = [deadline for deadline in deadlines if deadline is not None]
        self._attr_native_value = min(deadlines) if deadlines else None
