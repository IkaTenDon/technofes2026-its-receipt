from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from tests.helpers import reset_registry_cache
from timiniprint.transport.bluetooth import BluetoothDiscovery, BluetoothScanResult
from timiniprint.transport.bluetooth.types import DeviceInfo, DeviceTransport
from timiniprint.devices import PrinterCatalog, BluetoothTarget


class BluetoothDiscoveryLogicalDeviceTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_registry_cache()
        self.catalog = PrinterCatalog.load()
        self.discovery = BluetoothDiscovery(self.catalog)

    def test_unique_classic_and_ble_are_merged_into_one_logical_device(self) -> None:
        devices = [
            DeviceInfo(
                name="X6H-ABCD",
                address="AA:BB:CC:DD:EE:01",
                paired=True,
                transport=DeviceTransport.CLASSIC,
            ),
            DeviceInfo(
                name="X6H-ABCD",
                address="F4B3C8E3-C284-9C3A-C549-D786345CB553",
                paired=None,
                transport=DeviceTransport.BLE,
            ),
        ]

        resolved = self.discovery.devices_from_scan(devices)

        self.assertEqual(len(resolved), 1)
        item = resolved[0]
        self.assertIsInstance(item.transport_target, BluetoothTarget)
        self.assertIsNotNone(item.transport_target.classic_endpoint)
        self.assertIsNotNone(item.transport_target.ble_endpoint)
        self.assertEqual(item.transport_badge, "[classic+ble]")
        self.assertEqual(item.profile_key, "x6h")

    def test_ambiguous_group_is_not_merged(self) -> None:
        devices = [
            DeviceInfo(name="X6H-ABCD", address="AA:BB:CC:DD:EE:01", paired=True, transport=DeviceTransport.CLASSIC),
            DeviceInfo(name="X6H-ABCD", address="AA:BB:CC:DD:EE:02", paired=True, transport=DeviceTransport.CLASSIC),
            DeviceInfo(name="X6H-ABCD", address="F4B3C8E3-C284-9C3A-C549-D786345CB553", paired=None, transport=DeviceTransport.BLE),
        ]

        resolved = self.discovery.devices_from_scan(devices)

        self.assertEqual(len(resolved), 3)
        self.assertTrue(
            all(
                not isinstance(item.transport_target, BluetoothTarget)
                or item.transport_target.classic_endpoint is None
                or item.transport_target.ble_endpoint is None
                for item in resolved
            )
        )

    def test_resolve_by_classic_or_ble_address_returns_same_logical_device(self) -> None:
        device = self.discovery.devices_from_scan(
            [
                DeviceInfo(
                    name="X6H-ABCD",
                    address="AA:BB:CC:DD:EE:01",
                    paired=True,
                    transport=DeviceTransport.CLASSIC,
                ),
                DeviceInfo(
                    name="X6H-ABCD",
                    address="F4B3C8E3-C284-9C3A-C549-D786345CB553",
                    paired=None,
                    transport=DeviceTransport.BLE,
                ),
            ]
        )[0]

        with patch.object(
            self.discovery,
            "scan_report",
            AsyncMock(return_value=BluetoothScanResult(devices=[device], failures=[])),
        ):
            by_classic = _run(self.discovery.resolve_device("AA:BB:CC:DD:EE:01"))
            by_ble = _run(self.discovery.resolve_device("F4B3C8E3-C284-9C3A-C549-D786345CB553"))

        self.assertEqual(by_classic, device)
        self.assertEqual(by_ble, device)

    def test_scan_does_not_retry_ble_when_device_is_already_merged(self) -> None:
        classic = DeviceInfo(name="X6H-ABCD", address="AA:BB:CC:DD:EE:02", paired=True, transport=DeviceTransport.CLASSIC)
        ble = DeviceInfo(name="X6H-ABCD", address="F4B3C8E3-C284-9C3A-C549-D786345CB553", paired=None, transport=DeviceTransport.BLE)

        with patch(
            "timiniprint.transport.bluetooth.discovery.SppBackend.scan_with_failures",
            AsyncMock(return_value=([classic, ble], [])),
        ) as backend_scan:
            result = _run(
                self.discovery.scan_report(
                    include_classic=True,
                    include_ble=True,
                )
            )

        self.assertEqual(result.failures, [])
        self.assertEqual(backend_scan.await_count, 1)
        self.assertEqual(len(result.devices), 1)
        self.assertEqual(result.devices[0].transport_badge, "[classic+ble]")


def _run(coro):
    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
