#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

Decode/process a message (payload into JSON).
"""

import logging
from datetime import timedelta as td

from .const import DONT_CREATE_ENTITIES, DONT_UPDATE_ENTITIES, __dev_mode__
from .devices import Device, UfhController
from .protocol import (
    CODES_BY_DEV_KLASS,
    CODES_SCHEMA,
    CorruptStateError,
    InvalidAddrSetError,
    InvalidPacketError,
    Message,
)
from .protocol.const import DEV_KLASS_BY_TYPE
from .protocol.ramses import CODES_HVAC_ONLY

# skipcq: PY-W2000
from .protocol import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    DEVICE_SLUGS,
    DEV_TYPES,
    DEV_MAP,
    ZONE_MAP,
)

# skipcq: PY-W2000
from .protocol import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    _0001,
    _0002,
    _0004,
    _0005,
    _0006,
    _0008,
    _0009,
    _000A,
    _000C,
    _000E,
    _0016,
    _0100,
    _0150,
    _01D0,
    _01E9,
    _0404,
    _0418,
    _042F,
    _0B04,
    _1030,
    _1060,
    _1081,
    _1090,
    _1098,
    _10A0,
    _10B0,
    _10E0,
    _10E1,
    _1100,
    _11F0,
    _1260,
    _1280,
    _1290,
    _1298,
    _12A0,
    _12B0,
    _12C0,
    _12C8,
    _12F0,
    _1300,
    _1F09,
    _1F41,
    _1FC9,
    _1FCA,
    _1FD0,
    _1FD4,
    _2249,
    _22C9,
    _22D0,
    _22D9,
    _22F1,
    _22F3,
    _2309,
    _2349,
    _2389,
    _2400,
    _2401,
    _2410,
    _2420,
    _2D49,
    _2E04,
    _2E10,
    _30C9,
    _3110,
    _3120,
    _313F,
    _3150,
    _31D9,
    _31DA,
    _31E0,
    _3200,
    _3210,
    _3220,
    _3221,
    _3223,
    _3B00,
    _3EF0,
    _3EF1,
    _PUZZ,
)

__all__ = ["process_msg"]

CODE_NAMES = {k: v["name"] for k, v in CODES_SCHEMA.items()}

MSG_FORMAT_10 = "|| {:10s} | {:10s} | {:2s} | {:16s} | {:^4s} || {}"
MSG_FORMAT_18 = "|| {:18s} | {:18s} | {:2s} | {:16s} | {:^4s} || {}"

DEV_MODE = __dev_mode__ and False  # set True for useful Tracebacks

_LOGGER = logging.getLogger(__name__)
if DEV_MODE:
    _LOGGER.setLevel(logging.DEBUG)


def _create_devices_from_addrs(gwy, this: Message) -> None:
    """Discover and create any new devices using the packet addresses (not payload)."""

    # prefer Devices but can still use Addresses if required...
    this.src = gwy.device_by_id.get(this.src.id, this.src)
    this.dst = gwy.device_by_id.get(this.dst.id, this.dst)

    # Devices need to know their controller, ?and their location ('parent' domain)
    # NB: only addrs prcoessed here, packet metadata is processed elsewhere

    # Determinging bindings to a controller:
    #  - configury; As per any schema
    #  - discovery: If in 000C pkt, or pkt *to* device where src is a controller
    #  - eavesdrop: If pkt *from* device where dst is a controller

    # Determinging location in a schema (domain/DHW/zone):
    #  - configury; As per any schema
    #  - discovery: If in 000C pkt - unable for 10: & 00: (TRVs)
    #  - discovery: from packet fingerprint, excl. payloads (only for 10:)
    #  - eavesdrop: from packet fingerprint, incl. payloads

    if not isinstance(this.src, Device):
        this.src = gwy._get_device(this.src.id, msg=this)
        if this.dst.id == this.src.id:
            this.dst = this.src

    if (
        not gwy.config.enable_eavesdrop
        or this.dst.id in gwy._unwanted
        or this.src == gwy.hgi
    ):  # the above can't / shouldn't be eavesdropped for dst device
        return

    if not isinstance(this.dst, Device):
        this.dst = gwy._get_device(this.dst.id, msg=this)

    if getattr(this.dst, "_is_controller", False) and not isinstance(
        this.dst, UfhController
    ):  # HACK
        gwy._get_device(this.src.id, ctl_id=this.dst.id, msg=this)  # or _set_ctl?

    elif isinstance(this.dst, Device) and getattr(this.src, "_is_controller", False):
        gwy._get_device(this.dst.id, ctl_id=this.src.id, msg=this)  # or _set_ctl?


def _check_msg_addrs(msg: Message) -> None:
    """Validate the packet's address set.

    Raise InvalidAddrSetError if the meta data is invalid, otherwise simply return.
    """

    if msg.src.id != msg.dst.id and msg.src.type == msg.dst.type:
        # .I --- 18:013393 18:000730 --:------ 0001 005 00FFFF0200     # invalid
        # .I --- 01:078710 --:------ 01:144246 1F09 003 FF04B5         # invalid
        # .I --- 29:151550 29:237552 --:------ 22F3 007 00023C03040000 # valid? HVAC
        if msg.code not in CODES_HVAC_ONLY:
            raise InvalidAddrSetError(f"Invalid src/dst addr pair: {msg.src}/{msg.dst}")
        _LOGGER.warning(
            f"{msg!r} < Invalid src/dst addr pair: {msg.src}/{msg.dst}, is it HVAC?"
        )


def _check_msg_src(msg: Message, klass: str = None) -> None:
    """Validate the packet's source device class (type) against its verb/code pair.

    Raise InvalidPacketError if the meta data is invalid, otherwise simply return.
    """

    if klass is None:
        klass = getattr(msg.src, "_klass", None) or DEV_KLASS_BY_TYPE.get(msg.src.type)
    if klass in ("HGI", "DEV"):
        return

    if klass not in CODES_BY_DEV_KLASS:  # DEX_done, TODO: fingerprint dev class
        if msg.code != _10E0 and msg.code not in CODES_HVAC_ONLY:
            raise InvalidPacketError(f"Unknown src type: {msg.src}")
        _LOGGER.warning(f"{msg!r} < Unknown src type: {msg.src}, is it HVAC?")
        return

    #
    #

    #
    #

    if msg.code not in CODES_BY_DEV_KLASS[klass]:  # DEX_done
        if klass != "HGI":  # DEX_done
            raise InvalidPacketError(f"Invalid code for {msg.src} to Tx: {msg.code}")
        if msg.verb in (RQ, W_):
            return
        _LOGGER.warning(f"{msg!r} < Invalid code for {msg.src} to Tx: {msg.code}")
        return

    #
    #
    #
    #

    #
    # (code := CODES_BY_DEV_KLASS[klass][msg.code]) and msg.verb not in code:
    if msg.verb not in CODES_BY_DEV_KLASS[klass][msg.code]:  # DEX_done
        raise InvalidPacketError(
            f"Invalid verb/code for {msg.src} to Tx: {msg.verb}/{msg.code}"
        )


def _check_msg_dst(msg: Message, klass: str = None) -> None:
    """Validate the packet's destination device class (type) against its verb/code pair.

    Raise InvalidPacketError if the meta data is invalid, otherwise simply return.
    """

    if klass is None:
        klass = getattr(msg.dst, "_klass", None) or DEV_KLASS_BY_TYPE.get(msg.dst.type)
    if klass in (None, "HGI", "DEV") or (msg.dst is msg.src and msg.verb == I_):
        return

    if klass not in CODES_BY_DEV_KLASS:  # DEX_done, TODO: fingerprint dev class
        if msg.code not in CODES_HVAC_ONLY:
            raise InvalidPacketError(f"Unknown dst type: {msg.dst}")
        _LOGGER.warning(f"{msg!r} < Unknown dst type: {msg.dst}, is it HVAC?")
        return

    if msg.verb == I_:  # TODO: not common, unless src=dst
        return  # receiving an I isn't currently in the schema & cant yet be tested

    if f"{klass}/{msg.verb}/{msg.code}" in (f"CTL/{RQ}/{_3EF1}",):  # DEX_done
        return  # HACK: an exception-to-the-rule that need sorting

    if msg.code not in CODES_BY_DEV_KLASS[klass]:  # NOTE: not OK for Rx, DEX_done
        if klass != "HGI":  # NOTE: not yet needed because of 1st if, DEX_done
            raise InvalidPacketError(f"Invalid code for {msg.dst} to Rx: {msg.code}")
        if msg.verb == RP:
            return
        _LOGGER.warning(f"{msg!r} < Invalid code for {msg.dst} to Tx: {msg.code}")
        return

    if f"{msg.verb}/{msg.code}" in (f"{W_}/{_0001}",):
        return  # HACK: an exception-to-the-rule that need sorting
    if f"{klass}/{msg.verb}/{msg.code}" in (f"BDR/{RQ}/{_3EF0}",):  # DEX_done
        return  # HACK: an exception-to-the-rule that need sorting

    verb = {RQ: RP, RP: RQ, W_: I_}[msg.verb]
    # (code := CODES_BY_DEV_KLASS[klass][msg.code]) and verb not in code:
    if verb not in CODES_BY_DEV_KLASS[klass][msg.code]:  # DEX_done
        raise InvalidPacketError(
            f"Invalid verb/code for {msg.dst} to Rx: {msg.verb}/{msg.code}"
        )


def process_msg(msg: Message, prev_msg: Message = None) -> None:
    """Process the valid packet by decoding its payload."""

    # All methods require a valid message (payload), except create_devices(), which
    # requires a valid message only for 000C.

    def detect_array(this, prev) -> dict:
        """Return complete array if this pkt is the latter half of an array."""
        # This will work, even if the 2nd pkt._is_array == False as 1st == True
        #  I --- 01:158182 --:------ 01:158182 000A 048 001201F409C4011101F409C40...
        #  I --- 01:158182 --:------ 01:158182 000A 006 081001F409C4
        if (
            not prev
            or not prev._has_array
            or this.code not in (_000A, _22C9)
            or this.code != prev.code
            or this.verb != prev.verb != I_
            or this.src != prev.src
            or this.dtm >= prev.dtm + td(seconds=3)
        ):
            return this.payload

        this._pkt._force_has_array()
        payload = this.payload if isinstance(this.payload, list) else [this.payload]
        return prev.payload + payload

    gwy = msg._gwy  # pylint: disable=protected-access, skipcq: PYL-W0212

    # HACK:  if CLI, double-logging with client.py proc_msg() & setLevel(DEBUG)
    if (log_level := _LOGGER.getEffectiveLevel()) < logging.INFO:
        _LOGGER.info(msg)
    elif log_level <= logging.INFO and not (msg.verb == RQ and msg.src.type == "18"):
        _LOGGER.info(msg)

    msg._payload = detect_array(msg, prev_msg)  # HACK: messy, needs rethinking?

    try:  # process the packet payload

        _check_msg_addrs(msg)  # ? InvalidAddrSetError

        # TODO: any value in not creating a device unless the message is valid?
        if gwy.config.reduce_processing < DONT_CREATE_ENTITIES:
            _create_devices_from_addrs(gwy, msg)

        _check_msg_src(msg)  # ? InvalidPacketError
        _check_msg_dst(msg)  # ? InvalidPacketError

        if gwy.config.reduce_processing >= DONT_UPDATE_ENTITIES:
            return

        if isinstance(msg.src, Device):  # , HgiGateway)):  # could use DeviceBase
            msg.src._handle_msg(msg)

        if msg.code not in (_0008, _0009, _3B00, _3EF1):  # special case: are fakeable
            return

        #  I --- 01:054173 --:------ 01:054173 0008 002 03AA
        if msg.dst == msg.src:
            # this is needed for faked relays...
            # each device will have to decide if this packet is useful
            [
                d._handle_msg(msg)
                for d in msg.src.devices
                if getattr(d, "_is_faked", False)  # and d.xxx = "BDR"
            ]

        # RQ --- 18:006402 13:123456 --:------ 3EF1 001 00
        elif getattr(msg.dst, "_is_faked", False):
            msg.dst._handle_msg(msg)

    except (AssertionError, NotImplementedError) as exc:
        (_LOGGER.error if DEV_MODE else _LOGGER.warning)(
            "%s < %s", msg._pkt, f"{exc.__class__.__name__}({exc})"
        )
        raise

    except (AttributeError, LookupError, TypeError, ValueError) as exc:
        (_LOGGER.exception if DEV_MODE else _LOGGER.error)(
            "%s < %s", msg._pkt, f"{exc.__class__.__name__}({exc})"
        )
        raise

    except (CorruptStateError, InvalidPacketError) as exc:  # TODO: CorruptEvohomeError
        (_LOGGER.exception if DEV_MODE else _LOGGER.error)("%s < %s", msg._pkt, exc)
        raise  # TODO: bad pkt, or Schema
