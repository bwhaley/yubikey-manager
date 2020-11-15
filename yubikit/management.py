# Copyright (c) 2020 Yubico AB
# All rights reserved.
#
#   Redistribution and use in source and binary forms, with or
#   without modification, are permitted provided that the following
#   conditions are met:
#
#    1. Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#    2. Redistributions in binary form must reproduce the above
#       copyright notice, this list of conditions and the following
#       disclaimer in the documentation and/or other materials provided
#       with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from .core import (
    bytes2int,
    int2bytes,
    require_version,
    Version,
    Tlv,
    AID,
    PID,
    TRANSPORT,
    USB_INTERFACE,
    NotSupportedError,
    BadResponseError,
    ApplicationNotAvailableError,
)
from .core.otp import check_crc, OtpConnection, OtpProtocol
from .core.fido import FidoConnection
from .core.smartcard import SmartCardConnection, SmartCardProtocol
from fido2.hid import CAPABILITY

from enum import IntEnum, IntFlag, unique
from dataclasses import dataclass
from typing import Optional, Union, Mapping
import abc
import struct


@unique
class APPLICATION(IntFlag):
    """YubiKey Application identifiers."""

    OTP = 0x01
    U2F = 0x02
    OPENPGP = 0x08
    PIV = 0x10
    OATH = 0x20
    FIDO2 = 0x200

    def __str__(self):
        if self == APPLICATION.U2F:
            return "FIDO U2F"
        elif self == APPLICATION.OPENPGP:
            return "OpenPGP"
        else:
            return self.name


@unique
class FORM_FACTOR(IntEnum):
    """YubiKey device form factors."""

    UNKNOWN = 0x00
    USB_A_KEYCHAIN = 0x01
    USB_A_NANO = 0x02
    USB_C_KEYCHAIN = 0x03
    USB_C_NANO = 0x04
    USB_C_LIGHTNING = 0x05

    def __str__(self):
        if self == FORM_FACTOR.USB_A_KEYCHAIN:
            return "Keychain (USB-A)"
        elif self == FORM_FACTOR.USB_A_NANO:
            return "Nano (USB-A)"
        elif self == FORM_FACTOR.USB_C_KEYCHAIN:
            return "Keychain (USB-C)"
        elif self == FORM_FACTOR.USB_C_NANO:
            return "Nano (USB-C)"
        elif self == FORM_FACTOR.USB_C_LIGHTNING:
            return "Keychain (USB-C, Lightning)"
        elif self == FORM_FACTOR.UNKNOWN:
            return "Unknown"

    @classmethod
    def from_code(cls, code: int) -> "FORM_FACTOR":
        if code and not isinstance(code, int):
            raise ValueError("Invalid form factor code: {}".format(code))
        return cls(code) if code in cls.__members__.values() else cls.UNKNOWN


@unique
class DEVICE_FLAG(IntFlag):
    """Configuration flags."""

    REMOTE_WAKEUP = 0x40
    EJECT = 0x80


TAG_USB_SUPPORTED = 0x01
TAG_SERIAL = 0x02
TAG_USB_ENABLED = 0x03
TAG_FORM_FACTOR = 0x04
TAG_VERSION = 0x05
TAG_AUTO_EJECT_TIMEOUT = 0x06
TAG_CHALRESP_TIMEOUT = 0x07
TAG_DEVICE_FLAGS = 0x08
TAG_APP_VERSIONS = 0x09
TAG_CONFIG_LOCK = 0x0A
TAG_UNLOCK = 0x0B
TAG_REBOOT = 0x0C
TAG_NFC_SUPPORTED = 0x0D
TAG_NFC_ENABLED = 0x0E


@dataclass
class DeviceConfig:
    """Management settings for YubiKey which can be configured by the user."""

    enabled_applications: Mapping[TRANSPORT, APPLICATION]
    auto_eject_timeout: Optional[int]
    challenge_response_timeout: Optional[int]
    device_flags: Optional[DEVICE_FLAG]

    def get_bytes(
        self,
        reboot: bool,
        cur_lock_code: Optional[bytes] = None,
        new_lock_code: Optional[bytes] = None,
    ) -> bytes:
        buf = b""
        if reboot:
            buf += Tlv(TAG_REBOOT)
        if cur_lock_code:
            buf += Tlv(TAG_UNLOCK, cur_lock_code)
        usb_enabled = self.enabled_applications.get(TRANSPORT.USB)
        if usb_enabled is not None:
            buf += Tlv(TAG_USB_ENABLED, int2bytes(usb_enabled, 2))
        nfc_enabled = self.enabled_applications.get(TRANSPORT.NFC)
        if nfc_enabled is not None:
            buf += Tlv(TAG_NFC_ENABLED, int2bytes(nfc_enabled, 2))
        if self.auto_eject_timeout is not None:
            buf += Tlv(TAG_AUTO_EJECT_TIMEOUT, int2bytes(self.auto_eject_timeout, 2))
        if self.challenge_response_timeout is not None:
            buf += Tlv(TAG_CHALRESP_TIMEOUT, int2bytes(self.challenge_response_timeout))
        if self.device_flags is not None:
            buf += Tlv(TAG_DEVICE_FLAGS, int2bytes(self.device_flags))
        if new_lock_code:
            buf += Tlv(TAG_CONFIG_LOCK, new_lock_code)
        if len(buf) > 0xFF:
            raise NotSupportedError("DeviceConfiguration too large")
        return int2bytes(len(buf)) + buf


@dataclass
class DeviceInfo:
    """Information about a YubiKey readable using the ManagementSession."""

    config: DeviceConfig
    serial: Optional[int]
    version: Version
    form_factor: FORM_FACTOR
    supported_applications: Mapping[TRANSPORT, APPLICATION]
    is_locked: bool

    def has_transport(self, transport: TRANSPORT) -> bool:
        return transport in self.supported_applications

    @classmethod
    def parse(cls, encoded: bytes, default_version: Version) -> "DeviceInfo":
        if len(encoded) - 1 != encoded[0]:
            raise BadResponseError("Invalid length")
        data = Tlv.parse_dict(encoded[1:])
        locked = data.get(TAG_CONFIG_LOCK) == b"\1"
        serial = bytes2int(data.get(TAG_SERIAL, b"\0")) or None
        form_factor = FORM_FACTOR.from_code(bytes2int(data.get(TAG_FORM_FACTOR, b"\0")))
        if TAG_VERSION in data:
            version = Version.from_bytes(data[TAG_VERSION])
        else:
            version = default_version
        auto_eject_to = bytes2int(data.get(TAG_AUTO_EJECT_TIMEOUT, b"\0"))
        chal_resp_to = bytes2int(data.get(TAG_CHALRESP_TIMEOUT, b"\0"))
        flags = DEVICE_FLAG(bytes2int(data.get(TAG_DEVICE_FLAGS, b"\0")))

        supported = {}
        enabled = {}

        if version == (4, 2, 4):  # Doesn't report correctly
            supported[TRANSPORT.USB] = APPLICATION(0x3F)
        else:
            supported[TRANSPORT.USB] = APPLICATION(bytes2int(data[TAG_USB_SUPPORTED]))
        if TAG_USB_ENABLED in data:  # From YK 5.0.0
            enabled[TRANSPORT.USB] = APPLICATION(bytes2int(data[TAG_USB_ENABLED]))
        if TAG_NFC_SUPPORTED in data:  # YK with NFC
            supported[TRANSPORT.NFC] = APPLICATION(bytes2int(data[TAG_NFC_SUPPORTED]))
            enabled[TRANSPORT.NFC] = APPLICATION(bytes2int(data[TAG_NFC_ENABLED]))

        return cls(
            DeviceConfig(enabled, auto_eject_to, chal_resp_to, flags),
            serial,
            version,
            form_factor,
            supported,
            locked,
        )


_MODES = [
    USB_INTERFACE.OTP,  # 0x00
    USB_INTERFACE.CCID,  # 0x01
    USB_INTERFACE.OTP | USB_INTERFACE.CCID,  # 0x02
    USB_INTERFACE.FIDO,  # 0x03
    USB_INTERFACE.OTP | USB_INTERFACE.FIDO,  # 0x04
    USB_INTERFACE.FIDO | USB_INTERFACE.CCID,  # 0x05
    USB_INTERFACE.OTP | USB_INTERFACE.FIDO | USB_INTERFACE.CCID,  # 0x06
]


@dataclass(init=False, repr=False)
class Mode:
    """YubiKey USB Mode configuration for use with YubiKey NEO and 4."""

    code: int
    interfaces: USB_INTERFACE

    def __init__(self, interfaces: USB_INTERFACE):
        try:
            self.code = _MODES.index(interfaces)
            self.interfaces = USB_INTERFACE(interfaces)
        except ValueError:
            raise ValueError("Invalid mode!")

    def __repr__(self):
        return "+".join(t.name for t in USB_INTERFACE if t in self.interfaces)

    @classmethod
    def from_code(cls, code: int) -> "Mode":
        code = code & 0b00000111
        return cls(_MODES[code])

    @classmethod
    def from_pid(cls, pid: PID) -> "Mode":
        return cls(PID(pid).get_interfaces())


SLOT_DEVICE_CONFIG = 0x11
SLOT_YK4_CAPABILITIES = 0x13
SLOT_YK4_SET_DEVICE_INFO = 0x15


class _Backend(abc.ABC):
    version: Version

    @abc.abstractmethod
    def close(self) -> None:
        ...

    @abc.abstractmethod
    def set_mode(self, data: bytes) -> None:
        ...

    @abc.abstractmethod
    def read_config(self) -> bytes:
        ...

    @abc.abstractmethod
    def write_config(self, config: bytes) -> None:
        ...


class _ManagementOtpBackend(_Backend):
    def __init__(self, otp_connection):
        self.protocol = OtpProtocol(otp_connection)
        self.version = Version.from_bytes(self.protocol.read_status()[:3])
        if (1, 0, 0) <= self.version < (3, 0, 0):
            raise ApplicationNotAvailableError()

    def close(self):
        self.protocol.close()

    def set_mode(self, data):
        self.protocol.send_and_receive(SLOT_DEVICE_CONFIG, data)

    def read_config(self):
        response = self.protocol.send_and_receive(SLOT_YK4_CAPABILITIES)
        r_len = response[0]
        if check_crc(response[: r_len + 1 + 2]):
            return response[: r_len + 1]
        raise BadResponseError("Invalid checksum")

    def write_config(self, config):
        self.protocol.send_and_receive(SLOT_YK4_SET_DEVICE_INFO, config)


INS_READ_CONFIG = 0x1D
INS_WRITE_CONFIG = 0x1C
INS_SET_MODE = 0x16
P1_DEVICE_CONFIG = 0x11


class _ManagementSmartCardBackend(_Backend):
    def __init__(self, smartcard_connection):
        self.protocol = SmartCardProtocol(smartcard_connection)
        select_str = self.protocol.select(AID.MANAGEMENT).decode()
        self.version = Version.from_string(select_str)

    def close(self):
        self.protocol.close()

    def set_mode(self, data):
        if self.version[0] == 3:
            # Use the OTP Application to set mode
            self.protocol.select(AID.OTP)
            self.protocol.send_apdu(0, 0x01, SLOT_DEVICE_CONFIG, 0, data)
            # Workaround to "de-select" on NEO
            self.protocol.connection.send_and_receive(b"\xa4\x04\x00\x08")
            self.protocol.select(AID.MGMT)
        else:
            self.protocol.send_apdu(0, INS_SET_MODE, P1_DEVICE_CONFIG, 0, data)

    def read_config(self):
        return self.protocol.send_apdu(0, INS_READ_CONFIG, 0, 0)

    def write_config(self, config):
        self.protocol.send_apdu(0, INS_WRITE_CONFIG, 0, 0, config)


CTAP_VENDOR_FIRST = 0x40
CTAP_YUBIKEY_DEVICE_CONFIG = CTAP_VENDOR_FIRST
CTAP_READ_CONFIG = CTAP_VENDOR_FIRST + 2
CTAP_WRITE_CONFIG = CTAP_VENDOR_FIRST + 3


class _ManagementCtapBackend(_Backend):
    def __init__(self, fido_connection):
        self.ctap = fido_connection
        version = fido_connection.device_version
        if version[0] < 4:  # Prior to YK4 this was not firmware version
            if not (version[0] == 0 and fido_connection.capabilities & CAPABILITY.CBOR):
                version = (3, 0, 0)  # Guess that it's a NEO
        self.version = Version(*version)

    def close(self):
        self.ctap.close()

    def set_mode(self, data):
        self.ctap.call(CTAP_YUBIKEY_DEVICE_CONFIG, data)

    def read_config(self):
        return self.ctap.call(CTAP_READ_CONFIG)

    def write_config(self, config):
        self.ctap.call(CTAP_WRITE_CONFIG, config)


class ManagementSession:
    def __init__(
        self, connection: Union[OtpConnection, SmartCardConnection, FidoConnection]
    ):
        if isinstance(connection, OtpConnection):
            self.backend: _Backend = _ManagementOtpBackend(connection)
        elif isinstance(connection, SmartCardConnection):
            self.backend = _ManagementSmartCardBackend(connection)
        elif isinstance(connection, FidoConnection):
            self.backend = _ManagementCtapBackend(connection)
        else:
            raise TypeError("Unsupported connection type")

    def close(self) -> None:
        self.backend.close()

    @property
    def version(self) -> Version:
        return self.backend.version

    def read_device_info(self) -> DeviceInfo:
        require_version(self.version, (4, 1, 0))
        return DeviceInfo.parse(self.backend.read_config(), self.version)

    def write_device_config(
        self,
        config: Optional[DeviceConfig] = None,
        reboot: bool = False,
        cur_lock_code: Optional[bytes] = None,
        new_lock_code: Optional[bytes] = None,
    ) -> None:
        require_version(self.version, (5, 0, 0))
        config = config or DeviceConfig({}, None, None, None)
        self.backend.write_config(
            config.get_bytes(reboot, cur_lock_code, new_lock_code)
        )

    def set_mode(
        self, mode: Mode, chalresp_timeout: int = 0, auto_eject_timeout: int = 0
    ) -> None:
        if self.version >= (5, 0, 0):
            # Translate into DeviceConfig
            usb_enabled = APPLICATION(0)
            if USB_INTERFACE.OTP in mode.interfaces:
                usb_enabled |= APPLICATION.OTP
            if USB_INTERFACE.CCID in mode.interfaces:
                usb_enabled |= APPLICATION.OATH | APPLICATION.PIV | APPLICATION.OPENPGP
            if USB_INTERFACE.FIDO in mode.interfaces:
                usb_enabled |= APPLICATION.U2F | APPLICATION.FIDO2
            self.write_device_config(
                DeviceConfig(
                    {TRANSPORT.USB: usb_enabled},
                    auto_eject_timeout,
                    chalresp_timeout,
                    None,
                )
            )
        else:
            self.backend.set_mode(
                struct.pack(">BBH", mode.code, chalresp_timeout, auto_eject_timeout)
            )
