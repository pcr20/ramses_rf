#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
"""RAMSES RF - payload processors.

  NOTES: aspirations on a consistent Schema, going forward:

    :mode/state: | :bool:  | :mutex (infinitive. vs -ing):      | :flags:
  mode (config.) | enabled | disabled, heat, cool, heat_cool... | ch_enabled, dhw_enabled
  state (action) | active  | idle, heating, cooling...          | is_heating, is_cooling

  - prefer: enabled: True over xx_enabled: True (if only ever 1 flag)
  - prefer:  active: True over is_heating: True (if only ever 1 flag)
  - avoid: is_enabled, is_active
"""

from __future__ import annotations

import logging
import re
from datetime import datetime as dt
from datetime import timedelta as td
from typing import Callable

from .address import NON_DEV_ADDR, hex_id_to_dev_id
from .const import (
    DEV_ROLE,
    DEV_ROLE_MAP,
    DEV_TYPE_MAP,
    FAULT_DEVICE_CLASS,
    FAULT_STATE,
    FAULT_TYPE,
    SYS_MODE_MAP,
    SZ_ACTUATOR,
    SZ_BINDINGS,
    SZ_CHANGE_COUNTER,
    SZ_DATETIME,
    SZ_DEVICE_CLASS,
    SZ_DEVICE_ID,
    SZ_DEVICE_ROLE,
    SZ_DEVICES,
    SZ_DOMAIN_ID,
    SZ_DURATION,
    SZ_FAN_MODE,
    SZ_FRAG_LENGTH,
    SZ_FRAG_NUMBER,
    SZ_FRAGMENT,
    SZ_INDOOR_HUMIDITY,
    SZ_IS_DST,
    SZ_LANGUAGE,
    SZ_MODE,
    SZ_NAME,
    SZ_OUTDOOR_HUMIDITY,
    SZ_PAYLOAD,
    SZ_PRESSURE,
    SZ_RELAY_DEMAND,
    SZ_SETPOINT,
    SZ_SYSTEM_MODE,
    SZ_TEMPERATURE,
    SZ_TOTAL_FRAGS,
    SZ_UFH_IDX,
    SZ_UNKNOWN,
    SZ_UNTIL,
    SZ_VALUE,
    SZ_WINDOW_OPEN,
    SZ_ZONE_CLASS,
    SZ_ZONE_IDX,
    SZ_ZONE_MASK,
    SZ_ZONE_TYPE,
    ZON_MODE_MAP,
    ZON_ROLE_MAP,
    __dev_mode__,
)
from .exceptions import InvalidPayloadError
from .fingerprints import check_signature
from .helpers import (
    hex_to_bool,
    hex_to_date,
    hex_to_dtm,
    hex_to_dts,
    hex_to_flag8,
    hex_to_percent,
    hex_to_str,
    hex_to_temp,
    parse_air_quality,
    parse_bypass_position,
    parse_capabilities,
    parse_co2_level,
    parse_exhaust_fan_speed,
    parse_exhaust_flow,
    parse_exhaust_temp,
    parse_fan_info,
    parse_indoor_humidity,
    parse_indoor_temp,
    parse_outdoor_humidity,
    parse_outdoor_temp,
    parse_post_heater,
    parse_pre_heater,
    parse_remaining_time,
    parse_supply_fan_speed,
    parse_supply_flow,
    parse_supply_temp,
    parser_valve_demand,
)
from .opentherm import EN, MSG_DESC, MSG_ID, MSG_NAME, MSG_TYPE, OtMsgType, decode_frame
from .ramses import _2411_PARAMS_SCHEMA
from .version import VERSION

# Kudos & many thanks to:
# - Evsdd: 0404 (wow!)
# - Ierlandfan: 3150, 31D9, 31DA, others
# - ReneKlootwijk: 3EF0
# - brucemiranda: 3EF0, others
# - janvken: 10D0, 1470, 1F70, 22B0, 2411, several others
# - tomkooij: 3110


# skipcq: PY-W2000
from .const import (  # noqa: F401, isort: skip, pylint: disable=unused-import
    I_,
    RP,
    RQ,
    W_,
    F6,
    F8,
    F9,
    FA,
    FB,
    FC,
    FF,
)

_2411_TABLE = {k: v["description"] for k, v in _2411_PARAMS_SCHEMA.items()}

_INFORM_DEV_MSG = "Support the development of ramses_rf by reporting this packet"

LOOKUP_PUZZ = {
    "10": "engine",  # .    # version str, e.g. v0.14.0
    "11": "impersonating",  # pkt header, e.g. 30C9| I|03:123001 (15 characters, packed)
    "12": "message",  # .   # message only, max len is 16 ascii characters
    "13": "message",  # .   # message only, but without a timestamp, max len 22 chars
    "7F": "null",  # .      # packet is null / was nullified: payload to be ignored
}  # "00" is reserved

DEV_MODE = __dev_mode__ and False

_LOGGER = _PKT_LOGGER = logging.getLogger(__name__)
if DEV_MODE:
    _LOGGER.setLevel(logging.DEBUG)


def parser_decorator(fnc) -> Callable:
    def wrapper(payload, msg, **kwargs):
        result = fnc(payload, msg, **kwargs)
        if isinstance(result, dict) and msg.seqn.isnumeric():  # 22F1/3
            result["seqx_num"] = msg.seqn
        return result

    return wrapper


@parser_decorator  # rf_unknown
def parser_0001(payload, msg) -> dict:
    # When in test mode, a 12: will send a W ?every 6 seconds:
    # 12:39:56.099 061  W --- 12:010740 --:------ 12:010740 0001 005 0000000501
    # 12:40:02.098 061  W --- 12:010740 --:------ 12:010740 0001 005 0000000501
    # 12:40:08.099 058  W --- 12:010740 --:------ 12:010740 0001 005 0000000501

    # sent by a THM when is signal strength test mode (0505, except 1st pkt)
    # 13:48:38.518 080  W --- 12:010740 --:------ 12:010740 0001 005 0000000501
    # 13:48:45.518 074  W --- 12:010740 --:------ 12:010740 0001 005 0000000505
    # 13:48:50.518 077  W --- 12:010740 --:------ 12:010740 0001 005 0000000505

    # sent by a CTL before a rf_check
    # 15:12:47.769 053  W --- 01:145038 --:------ 01:145038 0001 005 FC00000505
    # 15:12:47.869 053 RQ --- 01:145038 13:237335 --:------ 0016 002 00FF
    # 15:12:47.880 053 RP --- 13:237335 01:145038 --:------ 0016 002 0017

    # 12:30:18.083 047  W --- 01:145038 --:------ 01:145038 0001 005 0800000505
    # 12:30:23.084 049  W --- 01:145038 --:------ 01:145038 0001 005 0800000505

    # 15:03:33.187 054  W --- 01:145038 --:------ 01:145038 0001 005 FC00000505
    # 15:03:38.188 063  W --- 01:145038 --:------ 01:145038 0001 005 FC00000505
    # 15:03:43.188 064  W --- 01:145038 --:------ 01:145038 0001 005 FC00000505
    # 15:13:19.757 053  W --- 01:145038 --:------ 01:145038 0001 005 FF00000505
    # 15:13:24.758 054  W --- 01:145038 --:------ 01:145038 0001 005 FF00000505
    # 15:13:29.758 068  W --- 01:145038 --:------ 01:145038 0001 005 FF00000505
    # 15:13:34.759 063  W --- 01:145038 --:------ 01:145038 0001 005 FF00000505

    # sent by a CTL
    # 16:49:46.125 057  W --- 04:166090 --:------ 01:032820 0001 005 0100000505
    # 16:53:34.635 058  W --- 04:166090 --:------ 01:032820 0001 005 0100000505

    # loopback (not Tx'd) by a HGI80 whenever its button is pressed
    # 00:22:41.540 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF02FF
    # 00:22:41.757 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF0200
    # 00:22:43.320 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF02FF
    # 00:22:43.415 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF0200

    # From a CM927:
    # W/--:/--:/12:/00-0000-0501 = Test transmit
    # W/--:/--:/12:/00-0000-0505 = Field strength

    if payload[2:6] in ("2000", "8000", "A000"):
        mode = "hvac"
    elif payload[2:6] in ("0000", "FFFF"):
        mode = "heat"
    else:
        mode = "heat"

    if mode == "hvac":
        assert payload[:2] == "00", payload[:2]
        # assert payload[2:4] in ("20", "80", "A0"), payload[2:4]
        # assert payload[4:6] == "00", payload[4:6]
        assert payload[8:10] in ("00", "04", "10", "20", "FF"), payload[8:10]

        result = {"payload": payload, "slot_num": payload[6:8]}
        if msg.len >= 6:
            result.update({"param_num": payload[10:12]})
        if msg.len >= 7:
            result.update({"next_slot_num": payload[12:14]})
        if msg.len >= 8:
            result.update(
                {
                    "boolean_14": None
                    if payload[14:16] == "FF"
                    else bool(int(payload[14:16]))
                }
            )
        return result

    assert payload[2:6] in ("0000", "FFFF"), payload[2:6]
    assert payload[8:10] in ("00", "02", "05"), payload[8:10]

    return {
        SZ_PAYLOAD: "-".join((payload[:2], payload[2:6], payload[6:8], payload[8:])),
    }


@parser_decorator  # outdoor_sensor (outdoor_weather / outdoor_temperature)
def parser_0002(payload, msg) -> dict:
    # seen with: 03:125829, 03:196221, 03:196196, 03:052382, 03:201498, 03:201565:
    # .I 000 03:201565 --:------ 03:201565 0002 004 03020105  # no zone_idx, domain_id

    # is it CODE_IDX_COMPLEX:
    #  - 02...... for outside temp?
    #  - 03...... for other stuff?

    if msg.src.type == DEV_TYPE_MAP.HCW:  # payload[2:] == DEV_TYPE_MAP.HCW, DEX
        assert payload == "03020105"
        return {f"_{SZ_UNKNOWN}": payload}

    # if payload[6:] == "02":  # msg.src.type == DEV_TYPE_MAP.OUT:
    return {
        SZ_TEMPERATURE: hex_to_temp(payload[2:6]),
        f"_{SZ_UNKNOWN}": payload[6:],
    }


@parser_decorator  # zone_name
def parser_0004(payload, msg) -> dict:
    # RQ payload is zz00; limited to 12 chars in evohome UI? if "7F"*20: not a zone

    return {} if payload[4:] == "7F" * 20 else {SZ_NAME: hex_to_str(payload[4:])}


@parser_decorator  # system_zones (add/del a zone?)
def parser_0005(payload, msg) -> dict | list[dict]:  # TODO: needs a cleanup
    # .I --- 01:145038 --:------ 01:145038 0005 004 00000100
    # RP --- 02:017205 18:073736 --:------ 0005 004 0009001F
    # .I --- 34:064023 --:------ 34:064023 0005 012 000A0000-000F0000-00100000

    def _parser(seqx) -> dict:
        if msg.src.type == DEV_TYPE_MAP.UFC:  # DEX, or use: seqx[2:4] == ...
            zone_mask = hex_to_flag8(seqx[6:8], lsb=True)
        elif msg.len == 3:  # ATC928G1000 - 1st gen monochrome model, max 8 zones
            zone_mask = hex_to_flag8(seqx[4:6], lsb=True)
        else:
            zone_mask = hex_to_flag8(seqx[4:6], lsb=True) + hex_to_flag8(
                seqx[6:8], lsb=True
            )
        zone_class = ZON_ROLE_MAP.get(seqx[2:4], DEV_ROLE_MAP[seqx[2:4]])
        return {
            SZ_ZONE_TYPE: seqx[2:4],  # TODO: ?remove & keep zone_class?
            SZ_ZONE_MASK: zone_mask,
            SZ_ZONE_CLASS: zone_class,  # TODO: ?remove & keep zone_type?
        }

    if msg.verb == RQ:  # RQs have a context: zone_type
        return {SZ_ZONE_TYPE: payload[2:4], SZ_ZONE_CLASS: DEV_ROLE_MAP[payload[2:4]]}

    if msg._has_array:
        assert (
            msg.verb == I_ and msg.src.type == DEV_TYPE_MAP.RND
        ), f"{msg!r} # expecting I/{DEV_TYPE_MAP.RND}:"  # DEX
        return [_parser(payload[i : i + 8]) for i in range(0, len(payload), 8)]

    return _parser(payload)


@parser_decorator  # schedule_sync (any changes?)
def parser_0006(payload, msg) -> dict:
    """Return the total number of changes to the schedules, including the DHW schedule.

    An RQ is sent every ~60s by a RFG100, an increase will prompt it to send a run of
    RQ/0404s (it seems to assume only the zones may have changed?).
    """
    # 16:10:34.288 053 RQ --- 30:071715 01:145038 --:------ 0006 001 00
    # 16:10:34.291 053 RP --- 01:145038 30:071715 --:------ 0006 004 00050008

    if payload[2:] == "FFFFFF":  # RP to an invalid RQ
        return {}

    assert payload[2:4] == "05"

    return {
        SZ_CHANGE_COUNTER: int(payload[4:], 16),
        "_header": payload[:4],
    }


@parser_decorator  # relay_demand (domain/zone/device)
def parser_0008(payload, msg) -> dict:
    # https://www.domoticaforum.eu/viewtopic.php?f=7&t=5806&start=105#p73681
    # e.g. Electric Heat Zone

    # .I --- 01:145038 --:------ 01:145038 0008 002 0314
    # .I --- 01:145038 --:------ 01:145038 0008 002 F914
    # .I --- 01:054173 --:------ 01:054173 0008 002 FA00
    # .I --- 01:145038 --:------ 01:145038 0008 002 FC14

    # RP --- 13:109598 18:199952 --:------ 0008 002 0000
    # RP --- 13:109598 18:199952 --:------ 0008 002 00C8

    if msg.src.type == DEV_TYPE_MAP.JST and msg.len == 13:  # Honeywell Japser, DEX
        assert msg.len == 13, "expecting length 13"
        return {
            "ordinal": f"0x{payload[2:8]}",
            "blob": payload[8:],
        }

    return {SZ_RELAY_DEMAND: hex_to_percent(payload[2:4])}  # 3EF0[2:4], 3EF1[10:12]


@parser_decorator  # relay_failsafe
def parser_0009(payload, msg) -> dict | list:
    """The relay failsafe mode.

    The failsafe mode defines the relay behaviour if the RF communication is lost (e.g.
    when a room thermostat stops communicating due to discharged batteries):
        False (disabled) - if RF comms are lost, relay will be held in OFF position
        True  (enabled)  - if RF comms are lost, relay will cycle at 20% ON, 80% OFF

    This setting may need to be enabled to ensure prost protect mode.
    """
    # can get: 003 or 006, e.g.: FC01FF-F901FF or FC00FF-F900FF
    # .I --- 23:100224 --:------ 23:100224 0009 003 0100FF  # 2-zone ST9520C
    # .I --- 10:040239 01:223036 --:------ 0009 003 000000

    def _parser(seqx) -> dict:
        assert seqx[:2] in (F9, FC) or int(seqx[:2], 16) < 16
        return {
            SZ_DOMAIN_ID if seqx[:1] == "F" else SZ_ZONE_IDX: seqx[:2],
            "failsafe_enabled": {"00": False, "01": True}.get(seqx[2:4]),
            f"{SZ_UNKNOWN}_0": seqx[4:],
        }

    if msg._has_array:
        return [_parser(payload[i : i + 6]) for i in range(0, len(payload), 6)]

    return {
        "failsafe_enabled": {"00": False, "01": True}.get(payload[2:4]),
        f"{SZ_UNKNOWN}_0": payload[4:],
    }


@parser_decorator  # zone_params (zone_config)
def parser_000a(payload, msg) -> dict | list:
    # RQ --- 34:044203 01:158182 --:------ 000A 001 08
    # RP --- 01:158182 34:044203 --:------ 000A 006 081001F409C4
    # RQ --- 22:017139 01:140959 --:------ 000A 006 080001F40DAC
    # RP --- 01:140959 22:017139 --:------ 000A 006 081001F40DAC

    def _parser(seqx) -> dict:  # null_rp: "007FFF7FFF"
        bitmap = int(seqx[2:4], 16)
        return {
            "min_temp": hex_to_temp(seqx[4:8]),
            "max_temp": hex_to_temp(seqx[8:]),
            "local_override": not bool(bitmap & 1),
            "openwindow_function": not bool(bitmap & 2),
            "multiroom_mode": not bool(bitmap & 16),
            f"_{SZ_UNKNOWN}_bitmap": f"0b{bitmap:08b}",  # TODO: try W with this
        }  # cannot determine zone_type from this information

    if msg._has_array:  # NOTE: these arrays can span 2 pkts!
        return [
            {
                SZ_ZONE_IDX: payload[i : i + 2],
                **_parser(payload[i : i + 12]),
            }
            for i in range(0, len(payload), 12)
        ]

    if msg.verb == RQ and msg.len <= 2:  # some RQs have a payload (why?)
        return {}

    assert msg.len == 6, f"{msg!r} # expecting length 006"
    return _parser(payload)


@parser_decorator  # zone_devices
def parser_000c(payload, msg) -> dict:
    # .I --- 34:092243 --:------ 34:092243 000C 018 00-0A-7F-FFFFFF 00-0F-7F-FFFFFF 00-10-7F-FFFFFF  # noqa: E501
    # RP --- 01:145038 18:013393 --:------ 000C 006 00-00-00-10DAFD
    # RP --- 01:145038 18:013393 --:------ 000C 012 01-00-00-10DAF5 01-00-00-10DAFB

    def complex_idx(seqx, msg) -> dict:  # complex index
        """domain_id, zone_idx, or ufx_idx|zone_idx."""

        # TODO: 000C to a UFC should be ufh_ifx, not zone_idx
        if msg.src.type == DEV_TYPE_MAP.UFC:  # DEX
            assert int(seqx, 16) < 8, f"invalid ufh_idx: '{seqx}' (0x00)"
            return {
                SZ_UFH_IDX: seqx,
                SZ_ZONE_IDX: None if payload[4:6] == "7F" else payload[4:6],
            }

        if payload[2:4] in (DEV_ROLE_MAP.DHW, DEV_ROLE_MAP.HTG):
            assert (
                int(seqx, 16) < 1 if payload[2:4] == DEV_ROLE_MAP.DHW else 2
            ), f"invalid _idx: '{seqx}' (0x01)"
            return {SZ_DOMAIN_ID: FA if payload[:2] == "00" else F9}

        if payload[2:4] == DEV_ROLE_MAP.APP:
            assert int(seqx, 16) < 1, f"invalid _idx: '{seqx}' (0x02)"
            return {SZ_DOMAIN_ID: FC}

        assert int(seqx, 16) < 16, f"invalid zone_idx: '{seqx}' (0x03)"
        return {SZ_ZONE_IDX: seqx}

    def _parser(seqx) -> dict:  # TODO: assumption that all id/idx are same is wrong!
        assert (
            seqx[:2] == payload[:2]
        ), f"idx != {payload[:2]} (seqx = {seqx}), short={is_short_000C(payload)}"
        assert int(seqx[:2], 16) < 16
        assert seqx[4:6] == "7F" or seqx[6:] != "F" * 6, f"Bad device_id: {seqx[6:]}"
        return {hex_id_to_dev_id(seqx[6:12]): seqx[4:6]}

    def is_short_000C(payload) -> bool:
        """Return True if it is a short 000C (element length is 5, not 6)."""

        if (pkt_len := len(payload)) != 72:
            return pkt_len % 12 != 0

        # 0608-001099C3 0608-001099C5 0608-001099BF 0608-001099BE 0608-001099BD 0608-001099BC  # len(element) = 6
        # 0508-00109901 0800-10990208 0010-99030800 1099-04080010 9905-08001099 0608-00109907  # len(element) = 5
        elif all(payload[i : i + 4] == payload[:4] for i in range(12, pkt_len, 12)):
            return False  # len(element) = 6 (12)

        # 06 08-001099C3 06-08001099 C5-06080010 99-BF060800 10-99BE0608 00-1099BD06 08-001099BC  # len(element) = 6
        # 05 08-00109901 08-00109902 08-00109903 08-00109904 08-00109905 08-00109906 08-00109907  # len(element) = 5
        elif all(payload[i : i + 2] == payload[2:4] for i in range(12, pkt_len, 10)):
            return True  # len(element) = 5 (10)

        raise InvalidPayloadError("Unable to determine element length")  # return None

    if payload[2:4] == DEV_ROLE_MAP.HTG and payload[:2] == "01":
        dev_role = DEV_ROLE_MAP[DEV_ROLE.HT1]
    else:
        dev_role = DEV_ROLE_MAP[payload[2:4]]

    result = {
        SZ_ZONE_TYPE: payload[2:4],
        **complex_idx(payload[:2], msg),
        SZ_DEVICE_ROLE: dev_role,
    }
    if msg.verb == RQ:  # RQs have a context: index, zone_type, payload is iitt
        return result

    # NOTE: Both these are valid! So collision when len = 036!
    # RP --- 01:239474 18:198929 --:------ 000C 012 06-00-00119A99 06-00-00119B21
    # RP --- 01:069616 18:205592 --:------ 000C 011 01-00-00121B54    00-00121B52
    # RP --- 01:239700 18:009874 --:------ 000C 018 07-08-001099C3 07-08-001099C5 07-08-001099BF
    # RP --- 01:059885 18:010642 --:------ 000C 016 00-00-0011EDAA    00-0011ED92    00-0011EDA0

    devs = (
        [_parser(payload[:2] + payload[i : i + 10]) for i in range(2, len(payload), 10)]
        if is_short_000C(payload)
        else [_parser(payload[i : i + 12]) for i in range(0, len(payload), 12)]
    )

    return {
        **result,
        SZ_DEVICES: [k for d in devs for k, v in d.items() if v != "7F"],
    }


@parser_decorator  # unknown_000e, from STA
def parser_000e(payload, msg) -> dict:
    assert payload in ("000000", "000014"), _INFORM_DEV_MSG

    return {
        SZ_PAYLOAD: payload,
    }


@parser_decorator  # rf_check
def parser_0016(payload, msg) -> dict:
    # TODO: does 0016 include parent_idx?, but RQ/07:/0000?
    # RQ --- 22:060293 01:078710 --:------ 0016 002 0200
    # RP --- 01:078710 22:060293 --:------ 0016 002 021E
    # RQ --- 12:010740 01:145038 --:------ 0016 002 0800
    # RP --- 01:145038 12:010740 --:------ 0016 002 081E
    # RQ --- 07:031785 01:063844 --:------ 0016 002 0000
    # RP --- 01:063844 07:031785 --:------ 0016 002 002A

    if msg.verb == RQ:  # and msg.len == 1:  # TODO: some RQs have a payload
        return {}

    rf_value = int(payload[2:4], 16)
    return {
        "rf_strength": min(int(rf_value / 5) + 1, 5),
        "rf_value": rf_value,
    }


@parser_decorator  # language (of device/system)
def parser_0100(payload, msg) -> dict:
    if msg.verb == RQ and msg.len == 1:  # some RQs have a payload
        return {}

    return {
        SZ_LANGUAGE: hex_to_str(payload[2:6]),
        f"_{SZ_UNKNOWN}_0": payload[6:],
    }


@parser_decorator  # unknown_0150, from OTB
def parser_0150(payload, msg) -> dict:
    assert payload == "000000", _INFORM_DEV_MSG

    return {
        SZ_PAYLOAD: payload,
    }


@parser_decorator  # unknown_01d0, from a HR91 (when its buttons are pushed)
def parser_01d0(payload, msg) -> dict:
    # 23:57:28.869 045  W --- 04:000722 01:158182 --:------ 01D0 002 0003
    # 23:57:28.931 045  I --- 01:158182 04:000722 --:------ 01D0 002 0003
    # 23:57:31.581 048  W --- 04:000722 01:158182 --:------ 01E9 002 0003
    # 23:57:31.643 045  I --- 01:158182 04:000722 --:------ 01E9 002 0000
    # 23:57:31.749 050  W --- 04:000722 01:158182 --:------ 01D0 002 0000
    # 23:57:31.811 045  I --- 01:158182 04:000722 --:------ 01D0 002 0000

    assert payload[2:] in ("00", "03"), _INFORM_DEV_MSG
    return {
        f"{SZ_UNKNOWN}_0": payload[2:],
    }


@parser_decorator  # unknown_01e9, from a HR91 (when its buttons are pushed)
def parser_01e9(payload, msg) -> dict:
    # 23:57:31.581348 048  W --- 04:000722 01:158182 --:------ 01E9 002 0003
    # 23:57:31.643188 045  I --- 01:158182 04:000722 --:------ 01E9 002 0000

    assert payload[2:] in ("00", "03"), _INFORM_DEV_MSG
    return {
        f"{SZ_UNKNOWN}_0": payload[2:],
    }


@parser_decorator  # unknown_01ff, to/from a Itho Spider/Thermostat
def parser_01ff(payload, msg) -> dict:
    # see: https://github.com/zxdavb/ramses_rf/issues/73

    assert payload[:4] == "0080", f"{_INFORM_DEV_MSG} ({payload[:4]})"
    assert payload[12:14] == "00", f"{_INFORM_DEV_MSG} ({payload[12:14]})"
    assert payload[16:22] == "00143C", f"{_INFORM_DEV_MSG} ({payload[16:22]})"
    assert payload[26:30] == "0000", f"{_INFORM_DEV_MSG} ({payload[26:30]})"
    assert payload[34:46] == "80800280FF80", f"{_INFORM_DEV_MSG} ({payload[34:46]})"
    assert payload[48:] == "0000", f"{_INFORM_DEV_MSG} ({payload[48:]})"

    if msg.verb in (I_, RQ):  # from Spider thermostat to gateway
        assert payload[14:16] == "80", f"{_INFORM_DEV_MSG} ({payload[14:16]})"
        assert payload[22:26] == "2840", f"{_INFORM_DEV_MSG} ({payload[22:26]})"
        assert payload[30:34] == "0104", f"{_INFORM_DEV_MSG} ({payload[30:34]})"
        assert payload[46:48] == "07", f"{_INFORM_DEV_MSG} ({payload[46:48]})"

    if msg.verb in (RP, W_):  # from Spider gateway to thermostat
        assert payload[4:6] == "80", f"{_INFORM_DEV_MSG} ({payload[4:6]})"
        assert payload[6:8] == payload[8:10], f"{_INFORM_DEV_MSG} ({payload[8:10]})"
        assert payload[14:16] == "00", f"{_INFORM_DEV_MSG} ({payload[14:16]})"
        assert payload[22:26] == "8080", f"{_INFORM_DEV_MSG} ({payload[22:26]})"
        assert payload[30:34] == "3100", f"{_INFORM_DEV_MSG} ({payload[30:34]})"
        assert payload[46:48] == "04", f"{_INFORM_DEV_MSG} ({payload[46:48]})"

    setpoint_bounds = (
        int(payload[6:8], 16) / 2,  # as: 22C9[2:6] and [6:10] ???
        None if msg.verb in (RP, W_) else int(payload[8:10], 16) / 2,
    )

    return {
        "temperature": None if msg.verb in (RP, W_) else int(payload[4:6], 16) / 2,
        "setpoint_bounds": setpoint_bounds,
        "time_planning": not bool(int(payload[10:12], 16) & 1 << 6),
        "temp_adjusted": bool(int(payload[10:12], 16) & 1 << 5),
        "_flags_10": payload[10:12],  #
    }


@parser_decorator  # zone_schedule (fragment)
def parser_0404(payload, msg) -> dict:
    # Retreival of Zone schedule (NB: 200008)
    # RQ --- 30:185469 01:037519 --:------ 0404 007 00-200008-00-0100
    # RP --- 01:037519 30:185469 --:------ 0404 048 00-200008-29-0103-6E2...
    # RQ --- 30:185469 01:037519 --:------ 0404 007 00-200008-00-0203
    # RP --- 01:037519 30:185469 --:------ 0404 048 00-200008-29-0203-4FD...
    # RQ --- 30:185469 01:037519 --:------ 0404 007 00-200008-00-0303
    # RP --- 01:037519 30:185469 --:------ 0404 038 00-200008-1F-0303-C10...

    # Retreival of DHW schedule (NB: 230008)
    # RQ --- 30:185469 01:037519 --:------ 0404 007 00-230008-00-0100
    # RP --- 01:037519 30:185469 --:------ 0404 048 00-230008-29-0103-618...
    # RQ --- 30:185469 01:037519 --:------ 0404 007 00-230008-00-0203
    # RP --- 01:037519 30:185469 --:------ 0404 048 00-230008-29-0203-ED6...
    # RQ --- 30:185469 01:037519 --:------ 0404 007 00-230008-00-0303
    # RP --- 01:037519 30:185469 --:------ 0404 014 00-230008-07-0303-13F...

    # Write a Zone schedule...
    # .W --- 30:042165 01:076010 --:------ 0404 048 08-200808-29-0104-688...
    # .I --- 01:076010 30:042165 --:------ 0404 007 08-200808-29-0104
    # .W --- 30:042165 01:076010 --:------ 0404 048 08-200808-29-0204-007...
    # .I --- 01:076010 30:042165 --:------ 0404 007 08-200808-29-0204
    # .W --- 30:042165 01:076010 --:------ 0404 048 08-200808-29-0304-8DD...
    # .I --- 01:076010 30:042165 --:------ 0404 007 08-200808-29-0304
    # .W --- 30:042165 01:076010 --:------ 0404 048 08-200808-11-0404-970...
    # .I --- 01:076010 30:042165 --:------ 0404 007 08-200808-11-0400

    # RP --- 01:145038 18:013393 --:------ 0404 007 00-230008-00-01FF  # no schedule

    assert payload[4:6] in ("00", payload[:2]), _INFORM_DEV_MSG

    if int(payload[8:10], 16) * 2 != (frag_length := len(payload[14:])) and (
        msg.verb != I_ or frag_length != 0
    ):
        raise InvalidPayloadError(f"Incorrect fragment length: 0x{payload[8:10]}")

    if msg.verb == RQ:  # have a ctx: idx|frag_idx
        return {
            SZ_FRAG_NUMBER: int(payload[10:12], 16),
            SZ_TOTAL_FRAGS: None if payload[12:14] == "00" else int(payload[12:14], 16),
        }

    if msg.verb == I_:  # have a ctx: idx|frag_idx
        return {
            SZ_FRAG_NUMBER: int(payload[10:12], 16),
            SZ_TOTAL_FRAGS: int(payload[12:14], 16),
            SZ_FRAG_LENGTH: None if payload[8:10] == "00" else int(payload[8:10], 16),
        }

    if payload[12:14] == FF:
        return {
            SZ_FRAG_NUMBER: int(payload[10:12], 16),
            SZ_TOTAL_FRAGS: None,
        }

    return {
        SZ_FRAG_NUMBER: int(payload[10:12], 16),
        SZ_TOTAL_FRAGS: int(payload[12:14], 16),
        SZ_FRAG_LENGTH: None if payload[8:10] == "FF" else int(payload[8:10], 16),
        SZ_FRAGMENT: payload[14:],
    }


@parser_decorator  # system_fault
def parser_0418(payload, msg) -> dict:
    # RP --- 01:145038 18:013393 --:------ 0418 022 000000B006F604000000711607697FFFFF7000348A86  # COMMS FAULT, CHANGEOVER
    # RP --- 01:145038 18:013393 --:------ 0418 022 000000B0000000000000000000007FFFFF7000000000  # noqa: E501
    # RP --- 01:145038 18:013393 --:------ 0418 022 000036B0010000000000108000007FFFFF7000000000  # noqa: E501
    # RP --- 01:145038 18:013393 --:------ 0418 022 000000B00401010000008694A3CC7FFFFF70000ECC8A  # noqa: E501
    # .I --- 01:037519 --:------ 01:037519 0418 022 000000B0050000000000239581877FFFFF7000000001  # Evotouch Battery Error  # noqa: E501
    # RP --- 01:037519 18:140805 --:------ 0418 022 004024B0060006000000CB94A112FFFFFF70007AD47D  # noqa: E501
    #                                                 0     0   1     1            3        3
    #                                                 2     8   2     8            0        8

    # assert int(payload[4:6], 16) < 64, f"Unexpected log_idx: 0x{payload[4:6]}"

    if hex_to_dts(payload[18:30]) is None:  # a null log entry
        return {"log_entry": None}

    try:
        assert payload[2:4] in FAULT_STATE, f"fault state: {payload[2:4]}"
        assert payload[8:10] in FAULT_TYPE, f"fault type: {payload[8:10]}"
        assert payload[12:14] in FAULT_DEVICE_CLASS, f"device class: {payload[12:14]}"
        # 1C: 'Comms fault, Actuator': seen with boiler relays
        assert int(payload[10:12], 16) < 16 or (
            payload[10:12] in ("1C", F6, F9, FA, FC)
        ), f"domain id: {payload[10:12]}"
    except AssertionError as exc:
        _LOGGER.warning(
            f"{msg!r} < {_INFORM_DEV_MSG} ({exc}), with a photo of your fault log"
        )

    result = {
        "timestamp": hex_to_dts(payload[18:30]),
        "state": FAULT_STATE.get(payload[2:4], payload[2:4]),
        "type": FAULT_TYPE.get(payload[8:10], payload[8:10]),
        SZ_DEVICE_CLASS: FAULT_DEVICE_CLASS.get(payload[12:14], payload[12:14]),
    }

    if payload[10:12] == FC and result[SZ_DEVICE_CLASS] == SZ_ACTUATOR:
        result[SZ_DEVICE_CLASS] = DEV_ROLE_MAP[DEV_ROLE.APP]  # actual evohome UI
    elif payload[10:12] == FA and result[SZ_DEVICE_CLASS] == SZ_ACTUATOR:
        result[SZ_DEVICE_CLASS] = DEV_ROLE_MAP[DEV_ROLE.HTG]  # speculative
    elif payload[10:12] == F9 and result[SZ_DEVICE_CLASS] == SZ_ACTUATOR:
        result[SZ_DEVICE_CLASS] = DEV_ROLE_MAP[DEV_ROLE.HT1]  # speculative

    if payload[12:14] != "00":  # TODO: Controller
        key_name = SZ_ZONE_IDX if int(payload[10:12], 16) < 16 else SZ_DOMAIN_ID
        result.update({key_name: payload[10:12]})

    if payload[38:] == "000002":  # "00:000002 for Unknown?
        result.update({SZ_DEVICE_ID: None})
    elif payload[38:] not in ("000000", "000001"):  # "00:000001 for Controller?
        result.update({SZ_DEVICE_ID: hex_id_to_dev_id(payload[38:])})

    result.update(
        {
            f"_{SZ_UNKNOWN}_3": payload[6:8],  # B0 ?priority
            f"_{SZ_UNKNOWN}_7": payload[14:18],  # 0000
            f"_{SZ_UNKNOWN}_15": payload[30:38],  # FFFF7000/1/2
        }
    )

    return {"log_entry": [v for k, v in result.items() if k != "log_idx"]}


@parser_decorator  # unknown_042f, from STA, VMS
def parser_042f(payload, msg) -> dict:
    # .I --- 34:064023 --:------ 34:064023 042F 008 00-0000-0023-0023-F5
    # .I --- 34:064023 --:------ 34:064023 042F 008 00-0000-0024-0024-F5
    # .I --- 34:064023 --:------ 34:064023 042F 008 00-0000-0025-0025-F5
    # .I --- 34:064023 --:------ 34:064023 042F 008 00-0000-0026-0026-F5
    # .I --- 34:092243 --:------ 34:092243 042F 008 00-0001-0021-0022-01
    # .I   34:011469 --:------ 34:011469 042F 008 00-0001-0003-0004-BC

    # .I --- 32:168090 --:------ 32:168090 042F 009 00-0000100F00105050
    # .I --- 32:166025 --:------ 32:166025 042F 009 00-050E0B0C00111470

    return {
        "counter_1": f"0x{payload[2:6]}",
        "counter_3": f"0x{payload[6:10]}",
        "counter_5": f"0x{payload[10:14]}",
        f"{SZ_UNKNOWN}_7": f"0x{payload[14:]}",
    }


@parser_decorator  # TODO: unknown_0b04, from THM (only when its a CTL?)
def parser_0b04(payload, msg) -> dict:
    # .I --- --:------ --:------ 12:207082 0B04 002 00C8  # batch of 3, every 24h

    return {
        f"{SZ_UNKNOWN}_1": payload[2:],
    }


@parser_decorator  # mixvalve_config (zone), FAN
def parser_1030(payload, msg) -> dict:
    # .I --- 01:145038 --:------ 01:145038 1030 016 0A-C80137-C9010F-CA0196-CB0100-CC0101
    # .I --- --:------ --:------ 12:144017 1030 016 01-C80137-C9010F-CA0196-CB010F-CC0101
    # RP --- 32:155617 18:005904 --:------ 1030 007 00-200100-21011F

    def _parser(seqx) -> dict:
        assert seqx[2:4] == "01", seqx[2:4]

        param_name = {
            "20": "unknown_20",  # HVAC
            "21": "unknown_21",  # HVAC
            "C8": "max_flow_setpoint",  # 55 (0-99) C
            "C9": "min_flow_setpoint",  # 15 (0-50) C
            "CA": "valve_run_time",  # 150 (0-240) sec, aka actuator_run_time
            "CB": "pump_run_time",  # 15 (0-99) sec
            "CC": "boolean_cc",  # ?boolean?
        }[seqx[:2]]

        return {param_name: int(seqx[4:], 16)}

    assert (msg.len - 1) / 3 in (2, 5), msg.len
    # assert payload[30:] in ("00", "01"), payload[30:]

    params = [_parser(payload[i : i + 6]) for i in range(2, len(payload), 6)]
    return {k: v for x in params for k, v in x.items()}


@parser_decorator  # device_battery (battery_state)
def parser_1060(payload, msg) -> dict:
    """Return the battery state.

    Some devices (04:) will also report battery level.
    """
    # 06:48:23.948 049  I --- 12:010740 --:------ 12:010740 1060 003 00FF01
    # 16:18:43.515 051  I --- 12:010740 --:------ 12:010740 1060 003 00FF00
    # 16:14:44.180 054  I --- 04:056057 --:------ 04:056057 1060 003 002800
    # 17:34:35.460 087  I --- 04:189076 --:------ 01:145038 1060 003 026401

    assert msg.len == 3, msg.len
    assert payload[4:6] in ("00", "01")

    return {
        "battery_low": payload[4:] == "00",
        "battery_level": hex_to_percent(payload[2:4]),
    }


@parser_decorator  # max_ch_setpoint (supply high limit)
def parser_1081(payload, msg) -> dict:
    return {SZ_SETPOINT: hex_to_temp(payload[2:])}


@parser_decorator  # unknown_1090 (non-Evohome, e.g. ST9520C)
def parser_1090(payload, msg) -> dict:
    # 14:08:05.176 095 RP --- 23:100224 22:219457 --:------ 1090 005 007FFF01F4
    # 18:08:05.809 095 RP --- 23:100224 22:219457 --:------ 1090 005 007FFF01F4

    # this is an educated guess
    assert msg.len == 5, _INFORM_DEV_MSG
    assert int(payload[:2], 16) < 2, _INFORM_DEV_MSG

    return {
        f"{SZ_TEMPERATURE}_0": hex_to_temp(payload[2:6]),
        f"{SZ_TEMPERATURE}_1": hex_to_temp(payload[6:10]),
    }


@parser_decorator  # unknown_1098, from OTB
def parser_1098(payload, msg) -> dict:
    assert payload == "00C8", _INFORM_DEV_MSG

    return {
        f"_{SZ_PAYLOAD}": payload,
        f"_{SZ_VALUE}": {"00": False, "C8": True}.get(
            payload[2:], hex_to_percent(payload[2:])
        ),
    }


@parser_decorator  # dhw (cylinder) params  # FIXME: a bit messy
def parser_10a0(payload, msg) -> dict:
    # RQ --- 07:045960 01:145038 --:------ 10A0 006 00-1087-00-03E4  # RQ/RP, every 24h
    # RP --- 01:145038 07:045960 --:------ 10A0 006 00-109A-00-03E8
    # RP --- 10:048122 18:006402 --:------ 10A0 003 00-1B58

    # these may not be reliable...
    # RQ --- 01:136410 10:067219 --:------ 10A0 002 0000
    # RQ --- 07:017494 01:078710 --:------ 10A0 006 00-1566-00-03E4

    # RQ --- 07:045960 01:145038 --:------ 10A0 006 00-31FF-00-31FF  # null
    # RQ --- 07:045960 01:145038 --:------ 10A0 006 00-1770-00-03E8
    # RQ --- 07:045960 01:145038 --:------ 10A0 006 00-1374-00-03E4
    # RQ --- 07:030741 01:102458 --:------ 10A0 006 00-181F-00-03E4
    # RQ --- 07:036831 23:100224 --:------ 10A0 006 01-1566-00-03E4  # non-evohome

    # these from a RFG...
    # RQ --- 30:185469 01:037519 --:------ 0005 002 000E
    # RP --- 01:037519 30:185469 --:------ 0005 004 000E0300  # two DHW valves
    # RQ --- 30:185469 01:037519 --:------ 10A0 001 01 (01 )

    if msg.verb == RQ and msg.len == 1:  # some RQs have a payload (why?)
        # 045 RQ --- 07:045960 01:145038 --:------ 10A0 006 0013740003E4
        # 037 RQ --- 18:013393 01:145038 --:------ 10A0 001 00
        # 054 RP --- 01:145038 18:013393 --:------ 10A0 006 0013880003E8
        return {}

    assert msg.len in (1, 3, 6), msg.len  # OTB uses 3, evohome uses 6
    assert payload[:2] in ("00", "01"), payload[:2]  # can be two DHW valves/system

    result = {}
    if msg.len >= 2:
        setpoint = hex_to_temp(payload[2:6])  # 255 for OTB? iff no DHW?
        result = {SZ_SETPOINT: None if setpoint == 255 else setpoint}  # 30.0-85.0 C
    if msg.len >= 4:
        result["overrun"] = int(payload[6:8], 16)  # 0-10 minutes
    if msg.len >= 6:
        result["differential"] = hex_to_temp(payload[8:12])  # 1.0-10.0 C

    return result


@parser_decorator  # unknown_10b0, from OTB
def parser_10b0(payload, msg) -> dict:
    assert payload == "0000", _INFORM_DEV_MSG

    return {
        f"_{SZ_PAYLOAD}": payload,
        f"_{SZ_VALUE}": {"00": False, "C8": True}.get(
            payload[2:], hex_to_percent(payload[2:])
        ),
    }


@parser_decorator  # filter_change, HVAC
def parser_10d0(payload, msg) -> dict:
    # 2022-07-03T22:52:34.571579 045  W --- 37:171871 32:155617 --:------ 10D0 002 00FF
    # 2022-07-03T22:52:34.596526 066  I --- 32:155617 37:171871 --:------ 10D0 006 0047B44F0000
    # then...
    # 2022-07-03T23:14:23.854089 000 RQ --- 37:155617 32:155617 --:------ 10D0 002 0000
    # 2022-07-03T23:14:23.876088 084 RP --- 32:155617 37:155617 --:------ 10D0 006 00B4B4C80000

    # 00-FF resets the counter, 00-47-B4-4F-0000 is the value (71 180 79).
    # Default is 180 180 200. The returned value is the amount of days (180),
    # total amount of days till change (180), percentage (200)

    if msg.verb == W_:
        result = {"reset_counter": payload[2:4] == "FF"}
    else:
        result = {"days_remaining": int(payload[2:4], 16)}

    if msg.len >= 3:
        result.update({"days_lifetime": int(payload[4:6], 16)})
    if msg.len >= 4:
        result.update({"percent_remaining": hex_to_percent(payload[6:8])})

    return result


@parser_decorator  # device_info
def parser_10e0(payload, msg) -> dict:
    if payload == "00":  # some HVAC devices wil RP|10E0|00
        return {}

    assert msg.len in (19, 28, 29, 30, 36, 38), msg.len  # >= 19, msg.len

    payload = re.sub("(00)*$", "", payload)  # remove trailing 00s
    assert len(payload) >= 18 * 2

    # if DEV_MODE:  # TODO
    try:  # DEX
        check_signature(msg.src.type, payload[2:20])
    except ValueError as exc:
        _LOGGER.warning(
            f"{msg!r} < {_INFORM_DEV_MSG}, with the make/model of device: {msg.src} ({exc})"
        )

    description, _, unknown = payload[36:].partition("00")
    assert msg.verb == RP or not unknown, f"{unknown}"

    result = {
        "date_2": hex_to_date(payload[20:28]) or "0000-00-00",  # firmware?
        "date_1": hex_to_date(payload[28:36]) or "0000-00-00",  # hardware?
        # "manufacturer_group": payload[2:6],  # 0001/0002
        "manufacturer_sub_id": payload[6:8],
        "product_id": payload[8:10],  # if CH/DHW: matches device_type (sometimes)
        # "software_ver_id": payload[10:12],
        # "list_ver_id": payload[12:14],  # if FF/01 is CH/DHW, then 01/FF
        "oem_code": payload[14:16],  # 00/FF is CH/DHW, 01/6x is HVAC
        # # "additional_ver_a": payload[16:18],
        # # "additional_ver_b": payload[18:20],
        "description": bytearray.fromhex(description).decode(),
    }
    if msg.verb == RP and unknown:  # TODO: why only OTBs do this?
        result[f"_{SZ_UNKNOWN}"] = unknown
    return result


@parser_decorator  # device_id
def parser_10e1(payload, msg) -> dict:
    return {SZ_DEVICE_ID: hex_id_to_dev_id(payload[2:])}


@parser_decorator  # unknown_10e2 - HVAC
def parser_10e2(payload, msg) -> dict:
    # .I --- --:------ --:------ 20:231151 10E2 003 00AD74  # every 2 minutes

    assert payload[:2] == "00", _INFORM_DEV_MSG
    assert len(payload) == 6, _INFORM_DEV_MSG

    return {
        "counter": int(payload[2:], 16),
    }


@parser_decorator  # tpi_params (domain/zone/device)  # FIXME: a bit messy
def parser_1100(payload, msg) -> dict:
    def complex_idx(seqx) -> dict:
        return {SZ_DOMAIN_ID: seqx} if seqx[:1] == "F" else {}  # only FC

    if msg.src.type == DEV_TYPE_MAP.JIM:  # Honeywell Japser, DEX
        assert msg.len == 19, msg.len
        return {
            "ordinal": f"0x{payload[2:8]}",
            "blob": payload[8:],
        }

    if msg.verb == RQ and msg.len == 1:  # some RQs have a payload (why?)
        return complex_idx(payload[:2])

    assert int(payload[2:4], 16) / 4 in range(1, 13), payload[2:4]
    assert int(payload[4:6], 16) / 4 in range(1, 31), payload[4:6]
    assert int(payload[6:8], 16) / 4 in range(0, 16), payload[6:8]

    # for:             TPI              // heatpump
    #  - cycle_rate:   6 (3, 6, 9, 12)  // ?? (1-9)
    #  - min_on_time:  1 (1-5)          // ?? (1, 5, 10,...30)
    #  - min_off_time: 1 (1-?)          // ?? (0, 5, 10, 15)

    def _parser(seqx) -> dict:
        return {
            "cycle_rate": int(int(payload[2:4], 16) / 4),  # cycles/hour
            "min_on_time": int(payload[4:6], 16) / 4,  # min
            "min_off_time": int(payload[6:8], 16) / 4,  # min
            f"_{SZ_UNKNOWN}_0": payload[8:10],  # always 00, FF?
        }

    result = _parser(payload)

    if msg.len > 5:
        assert (
            payload[10:14] == "7FFF" or 1.5 <= hex_to_temp(payload[10:14]) <= 3.0
        ), f"unexpected value for PBW: {payload[10:14]}"

        result.update(
            {
                "proportional_band_width": hex_to_temp(payload[10:14]),
                f"_{SZ_UNKNOWN}_1": payload[14:],  # always 01?
            }
        )

    return {
        **complex_idx(payload[:2]),
        **result,
    }


@parser_decorator  # unknown_11f0, from heatpump relay
def parser_11f0(payload, msg) -> dict:
    assert payload == "000009000000000000", _INFORM_DEV_MSG

    return {
        SZ_PAYLOAD: payload,
    }


@parser_decorator  # dhw cylinder temperature
def parser_1260(payload, msg) -> dict:
    return {SZ_TEMPERATURE: hex_to_temp(payload[2:])}


@parser_decorator  # HVAC: outdoor humidity
def parser_1280(payload, msg) -> dict:
    # educated guess - this packet never seen in the wild

    rh = hex_to_percent(payload[2:4], high_res=False)
    if msg.len == 2:
        return {SZ_OUTDOOR_HUMIDITY: rh}

    return {
        SZ_OUTDOOR_HUMIDITY: rh,
        SZ_TEMPERATURE: hex_to_temp(payload[4:8]),
        "dewpoint_temp": hex_to_temp(payload[8:12]),
    }


@parser_decorator  # outdoor temperature
def parser_1290(payload, msg) -> dict:
    # evohome responds to an RQ, also from OTB
    return parse_outdoor_temp(payload[2:])


@parser_decorator  # HVAC: co2_level, see: 31DA[6:10]
def parser_1298(payload: str, _) -> dict:
    return parse_co2_level(payload[2:6])


@parser_decorator  # HVAC: indoor_humidity
def parser_12a0(payload, msg) -> dict:
    FAULT_CODES_RHUM = {}  # relative humidity sensor

    assert payload[2:4] in FAULT_CODES_RHUM or int(payload[2:4], 16) <= 100
    if fault := FAULT_CODES_RHUM.get(payload[2:4]):
        return {"sensor_fault": fault}

    rh = hex_to_percent(payload[2:4], high_res=False)
    if msg.len == 2:
        return {SZ_INDOOR_HUMIDITY: rh}

    return {
        SZ_INDOOR_HUMIDITY: rh,
        SZ_TEMPERATURE: hex_to_temp(payload[4:8]),
        "dewpoint_temp": hex_to_temp(payload[8:12]),
    }


@parser_decorator  # window_state (of a device/zone)
def parser_12b0(payload, msg) -> dict:
    assert payload[2:] in ("0000", "C800", "FFFF"), payload[2:]  # "FFFF" means N/A

    return {
        SZ_WINDOW_OPEN: hex_to_bool(payload[2:4]),
    }


@parser_decorator  # displayed temperature (on a TR87RF bound to a RFG100)
def parser_12c0(payload, msg) -> dict:
    if payload[2:4] == "80":
        temp = None
    elif payload[4:] == "00":  # units are 1.0 F
        temp = int(payload[2:4], 16)
    else:  # if payload[4:] == "01":  # units are 0.5 C
        temp = int(payload[2:4], 16) / 2

    return {
        SZ_TEMPERATURE: temp,
        "units": {"00": "Fahrenheit", "01": "Celsius"}[payload[4:6]],
        f"_{SZ_UNKNOWN}_6": payload[6:],
    }


@parser_decorator  # HVAC: air_quality (and air_quality_basis), see: 31DA[2:6]
def parser_12c8(payload: str, _) -> dict:
    return parse_air_quality(payload[2:6])


@parser_decorator  # dhw_flow_rate
def parser_12f0(payload, msg) -> dict:
    return {"dhw_flow_rate": hex_to_temp(payload[2:])}


@parser_decorator  # ch_pressure
def parser_1300(payload, msg) -> dict:
    return {SZ_PRESSURE: hex_to_temp(payload[2:])}  # is 2's complement still


@parser_decorator  # programme_scheme, HVAC
def parser_1470(payload, msg) -> dict:
    # Seen on Orcon: see 1470, 1F70, 22B0

    SCHEDULE_SCHEME = {
        "9": "one_per_week",
        "A": "two_per_week",  # week_day, week_end
        "B": "one_each_day",  # seven_per_week (default?)
    }

    assert payload[8:10] == "80", _INFORM_DEV_MSG
    assert msg.verb == W_ or payload[4:8] == "0E60", _INFORM_DEV_MSG
    assert msg.verb == W_ or payload[10:] == "2A0108", _INFORM_DEV_MSG
    assert msg.verb != W_ or payload[4:] == "000080000000", _INFORM_DEV_MSG

    # schedule...
    # [2:3] - 1, every/all days, 1&6, weekdays/weekends, 1-7, each individual day
    # [3:4] - # setpoints/day (default 3)
    assert payload[2:3] in SCHEDULE_SCHEME and (
        payload[3:4] in ("2", "3", "4", "5", "6")
    ), _INFORM_DEV_MSG

    return {
        "scheme": SCHEDULE_SCHEME.get(payload[2:3], f"unknown_{payload[2:3]}"),
        "daily_setpoints": payload[3:4],
        f"_{SZ_VALUE}_4": payload[4:8],
        f"_{SZ_VALUE}_8": payload[8:10],
        f"_{SZ_VALUE}_10": payload[10:],
    }


@parser_decorator  # system_sync
def parser_1f09(payload, msg) -> dict:
    # 22:51:19.287 067  I --- --:------ --:------ 12:193204 1F09 003 010A69
    # 22:51:19.318 068  I --- --:------ --:------ 12:193204 2309 003 010866
    # 22:51:19.321 067  I --- --:------ --:------ 12:193204 30C9 003 0108C3

    assert msg.len == 3, f"length is {msg.len}, expecting 3"
    assert payload[:2] in ("00", "01", F8, FF)  # W/F8

    seconds = int(payload[2:6], 16) / 10
    next_sync = msg.dtm + td(seconds=seconds)

    return {
        "remaining_seconds": seconds,
        "_next_sync": dt.strftime(next_sync, "%H:%M:%S"),
    }


@parser_decorator  # dhw_mode
def parser_1f41(payload, msg) -> dict:
    # 053 RP --- 01:145038 18:013393 --:------ 1F41 006 00FF00FFFFFF  # no stored DHW
    assert payload[4:6] in ZON_MODE_MAP, f"{payload[4:6]} (0xjj)"
    assert (
        payload[4:6] == ZON_MODE_MAP.TEMPORARY or msg.len == 6
    ), f"{msg!r}: expected length 6"
    assert (
        payload[4:6] != ZON_MODE_MAP.TEMPORARY or msg.len == 12
    ), f"{msg!r}: expected length 12"
    assert (
        payload[6:12] == "FFFFFF"
    ), f"{msg!r}: expected FFFFFF instead of '{payload[6:12]}'"

    result = {SZ_MODE: ZON_MODE_MAP.get(payload[4:6])}
    if payload[2:4] != "FF":
        result["active"] = {"00": False, "01": True, "FF": None}[payload[2:4]]
    # if payload[4:6] == ZON_MODE_MAP.COUNTDOWN:
    #     result[SZ_UNTIL] = dtm_from_hex(payload[6:12])
    if payload[4:6] == ZON_MODE_MAP.TEMPORARY:
        result[SZ_UNTIL] = hex_to_dtm(payload[12:24])

    return result


@parser_decorator  # programme_config, HVAC
def parser_1f70(payload, msg) -> dict:
    # Seen on Orcon: see 1470, 1F70, 22B0

    try:
        assert payload[:2] == "00", f"expected 00, not {payload[:2]}"
        assert payload[2:4] in ("00", "01"), f"expected (00|01), not {payload[2:4]}"
        assert payload[4:8] == "0800", f"expected 0800, not {payload[4:8]}"
        assert payload[10:14] == "0000", f"expected 0000, not {payload[10:14]}"
        assert msg.verb in (RQ, W_) or payload[14:16] == "15"
        assert msg.verb in (I_, RP) or payload[14:16] == "00"
        assert msg.verb == RQ or payload[22:24] == "60"
        assert msg.verb != RQ or payload[22:24] == "00"
        assert msg.verb == RQ or payload[24:26] in ("E4", "E5", "E6"), _INFORM_DEV_MSG
        assert msg.verb == RP or payload[26:] == "000000"
        assert msg.verb != RP or payload[26:] == "008000"

    except AssertionError as exc:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({exc})")

        # assert int(payload[16:18], 16) < 7, _INFORM_DEV_MSG

    return {
        "day_idx": payload[16:18],  # depends upon 1470[3:4]?
        "setpoint_idx": payload[8:10],  # needs to be mod 1470[3:4]?
        "start_time": f"{int(payload[18:20], 16):02d}:{int(payload[20:22], 16):02d}",
        "fan_speed_wip": payload[24:26],  # # E4/E5/E6   / 00(RQ)
        f"_{SZ_VALUE}_02": payload[2:4],  # # 00/01      / 00(RQ)
        f"_{SZ_VALUE}_04": payload[4:8],  # # 0800
        f"_{SZ_VALUE}_10": payload[10:14],  # 0000
        f"_{SZ_VALUE}_14": payload[14:16],  # 15(RP,I)   / 00(RQ,W)
        f"_{SZ_VALUE}_22": payload[22:24],  # 60         / 00(RQ)
        f"_{SZ_VALUE}_26": payload[26:],  # # 008000(RP) / 000000(I/RQ/W)
    }


@parser_decorator  # rf_bind
def parser_1fc9(payload, msg) -> list:
    # .I --- 01:145038 --:------ 01:145038 1FC9 012 F6-2D49-06368E F6-1FC9-06368E

    # .I is missing?
    # .W --- 10:048122 01:145038 --:------ 1FC9 006 003EF028BBFA
    # .I --- 01:145038 10:048122 --:------ 1FC9 006 00FFFF06368E

    # .I --- 07:045960 --:------ 07:045960 1FC9 012 0012601CB388001FC91CB388
    # .W --- 01:145038 07:045960 --:------ 1FC9 006 0010A006368E
    # .I --- 07:045960 01:145038 --:------ 1FC9 006 0012601CB388

    # .I --- 01:145038 --:------ 01:145038 1FC9 018 FA000806368EFC3B0006368EFA1FC906368E
    # .W --- 13:081807 01:145038 --:------ 1FC9 006 003EF0353F8F
    # .I --- 01:145038 13:081807 --:------ 1FC9 006 00FFFF06368E

    # this is an array of codes
    # 049  I --- 01:145038 --:------ 01:145038 1FC9 018 07-0008-06368E FC-3B00-06368E                07-1FC9-06368E  # noqa: E501
    # 047  I --- 01:145038 --:------ 01:145038 1FC9 018 FA-0008-06368E FC-3B00-06368E                FA-1FC9-06368E  # noqa: E501
    # 065  I --- 01:145038 --:------ 01:145038 1FC9 024 FC-0008-06368E FC-3150-06368E FB-3150-06368E FC-1FC9-06368E  # noqa: E501

    # HW valve binding:
    # 063  I --- 01:145038 --:------ 01:145038 1FC9 018 FA-0008-06368E FC-3B00-06368E FA-1FC9-06368E  # noqa: E501
    # CH valve binding:
    # 071  I --- 01:145038 --:------ 01:145038 1FC9 018 F9-0008-06368E FC-3B00-06368E F9-1FC9-06368E  # noqa: E501
    # ZoneValve zone binding
    # 045  W --- 13:106039 01:145038 --:------ 1FC9 012 00-3EF0-359E37 00-3B00-359E37
    # DHW binding..
    # 045  W --- 13:163733 01:145038 --:------ 1FC9 012 00-3EF0-367F95 00-3B00-367F95

    # 049  I --- 01:145038 --:------ 01:145038 1FC9 018 F9-0008-06368E FC-3B00-06368E F9-1FC9-06368E  # noqa: E501

    # the new (heatpump-aware) BDR91:
    # 045 RP --- 13:035462 18:013393 --:------ 1FC9 018 00-3EF0-348A86 00-11F0-348A86 90-7FE1-DD6ABD  # noqa: E501

    def _parser(seqx) -> list:
        if seqx[:2] not in ("90",):
            assert (
                seqx[6:] == payload[6:12]
            ), f"{seqx[6:]} != {payload[6:12]}"  # all with same controller
        if seqx[:2] not in (
            "21",  # HVAC, Nuaire
            "63",  # HVAC
            "66",  # HVAC, Vasco?
            "67",  # HVAC
            "6C",  # HVAC
            "90",  # HEAT
            F6,
            F9,
            FA,
            FB,
            FC,
            FF,
        ):  # or: not in DOMAIN_TYPE_MAP: ??
            assert int(seqx[:2], 16) < 16, _INFORM_DEV_MSG
        return [seqx[:2], seqx[2:6], hex_id_to_dev_id(seqx[6:])]

    if msg.verb == I_ and msg.src is msg.dst:
        bind_phase = "offer"
    elif msg.verb == W_ and msg.src is not msg.dst:
        bind_phase = "accept"
    elif msg.verb == I_:
        bind_phase = "confirm"  # len(payload) could be 2 (e.g. 00, 21)
    else:
        bind_phase = None  # unknown

    if len(payload) == 2 and bind_phase == "confirm":
        return {"phase": bind_phase, SZ_BINDINGS: [[payload]]}

    assert msg.len >= 6 and msg.len % 6 == 0, msg.len  # assuming not RQ
    assert msg.verb in (I_, W_, RP), msg.verb  # devices will respond to a RQ!
    # assert (
    #     msg.src.id == hex_id_to_dev_id(payload[6:12])
    # ), f"{payload[6:12]} ({hex_id_to_dev_id(payload[6:12])})"  # NOTE: use_regex
    bindings = [
        _parser(payload[i : i + 12])
        for i in range(0, len(payload), 12)
        # if payload[i : i + 2] != "90"  # TODO: WIP, what is 90?
    ]
    return {"phase": bind_phase, SZ_BINDINGS: bindings}


@parser_decorator  # unknown_1fca, HVAC?
def parser_1fca(payload, msg) -> list:
    # .W --- 30:248208 34:021943 --:------ 1FCA 009 00-01FF-7BC990-FFFFFF  # sent x2

    return {
        f"_{SZ_UNKNOWN}_0": payload[:2],
        f"_{SZ_UNKNOWN}_1": payload[2:6],
        "device_id_0": hex_id_to_dev_id(payload[6:12]),
        "device_id_1": hex_id_to_dev_id(payload[12:]),
    }


@parser_decorator  # unknown_1fd0, from OTB
def parser_1fd0(payload, msg) -> dict:
    assert payload == "0000000000000000", _INFORM_DEV_MSG

    return {
        SZ_PAYLOAD: payload,
    }


@parser_decorator  # opentherm_sync, otb_sync
def parser_1fd4(payload, msg) -> dict:
    return {"ticker": int(payload[2:], 16)}


@parser_decorator  # WIP: unknown, HVAC
def parser_2210(payload, msg) -> dict:
    # RP --- 32:153258 18:005904 --:------ 2210 042 00FF 00FFFFFF0000000000FFFFFFFFFF 00FFFFFF0000000000FFFFFFFFFF FFFFFF000000000000000800
    # RP --- 32:153258 18:005904 --:------ 2210 042 00FF 00FFFF960000000003FFFFFFFFFF 00FFFF960000000003FFFFFFFFFF FFFFFF000000000000000800

    assert payload in (
        "00FF" + "00FFFFFF0000000000FFFFFFFFFF" * 2 + "FFFFFF000000000000000800",
        "00FF" + "00FFFF960000000003FFFFFFFFFF" * 2 + "FFFFFF000000000000000800",
    ), _INFORM_DEV_MSG

    return {}


@parser_decorator  # now_next_setpoint - Programmer/Hometronics
def parser_2249(payload, msg) -> dict:
    # see: https://github.com/jrosser/honeymon/blob/master/decoder.cpp#L357-L370
    # .I --- 23:100224 --:------ 23:100224 2249 007 00-7EFF-7EFF-FFFF

    def _parser(seqx) -> dict:
        minutes = int(seqx[10:], 16)
        next_setpoint = msg.dtm + td(minutes=minutes)
        return {
            "setpoint_now": hex_to_temp(seqx[2:6]),
            "setpoint_next": hex_to_temp(seqx[6:10]),
            "minutes_remaining": minutes,
            "_next_setpoint": dt.strftime(next_setpoint, "%H:%M:%S"),
        }

    # the ST9520C can support two heating zones, so: msg.len in (7, 14)?
    if msg._has_array:
        return [
            {
                SZ_ZONE_IDX: payload[i : i + 2],
                **_parser(payload[i + 2 : i + 14]),
            }
            for i in range(0, len(payload), 14)
        ]

    return _parser(payload)


@parser_decorator  # program_enabled, HVAC
def parser_22b0(payload, msg) -> dict:
    # Seen on Orcon: see 1470, 1F70, 22B0

    # .W --- 37:171871 32:155617 --:------ 22B0 002 0005  # enable
    # .I --- 32:155617 37:171871 --:------ 22B0 002 0005

    # .W --- 37:171871 32:155617 --:------ 22B0 002 0006  # disable
    # .I --- 32:155617 37:171871 --:------ 22B0 002 0006

    return {
        "enabled": {"06": False, "05": True}.get(payload[2:4]),
    }


@parser_decorator  # ufh_setpoint, TODO: max length = 24?
def parser_22c9(payload, msg) -> list:
    # .I --- 02:001107 --:------ 02:001107 22C9 024 00-0834-0A28-01-0108340A2801-0208340A2801-0308340A2801  # noqa: E501
    # .I --- 02:001107 --:------ 02:001107 22C9 006 04-0834-0A28-01

    # .I --- 21:064743 --:------ 21:064743 22C9 006 00-07D0-0834-02
    # .W --- 21:064743 02:250708 --:------ 22C9 006 03-07D0-0834-02
    # .I --- 02:250708 21:064743 --:------ 22C9 008 03-07D0-7FFF-020203

    # Notes on 008|suffix: only seen as I, only when no array, only as 7FFF(0101|0202)03$

    def _parser(seqx) -> dict:
        assert seqx[10:] in ("01", "02"), f"is {seqx[10:]}, expecting 01 or 02"

        return {
            "mode": {"01": "heat", "02": "cool"}[seqx[10:]],  # TODO: or action?
            "setpoint_bounds": (hex_to_temp(seqx[2:6]), hex_to_temp(seqx[6:10])),
        }  # lower, upper setpoints

    if msg._has_array:
        return [
            {
                SZ_UFH_IDX: payload[i : i + 2],
                **_parser(payload[i : i + 12]),
            }
            for i in range(0, len(payload), 12)
        ]

    assert msg.len != 8 or payload[10:] in ("010103", "020203"), _INFORM_DEV_MSG

    return _parser(payload[:12])


@parser_decorator  # unknown_22d0, UFH system mode (heat/cool)
def parser_22d0(payload, msg) -> dict:
    def _parser(seqx) -> dict:
        # assert seqx[2:4] in ("00", "03", "10", "13", "14"), _INFORM_DEV_MSG
        assert seqx[4:6] == "00", _INFORM_DEV_MSG
        return {
            "idx": seqx[:2],
            "_flags": hex_to_flag8(seqx[2:4]),
            "cool_mode": bool(int(seqx[2:4], 16) & 0x02),
            "heat_mode": bool(int(seqx[2:4], 16) & 0x04),
            "is_active": bool(int(seqx[2:4], 16) & 0x10),
            "_unknown": payload[4:],
        }

    if len(payload) == 8:
        assert payload[6:] in ("00", "02", "0A"), _INFORM_DEV_MSG
    else:
        assert payload[4:] == "001E14030020", _INFORM_DEV_MSG

    return _parser(payload)


@parser_decorator  # desired boiler setpoint
def parser_22d9(payload, msg) -> dict:
    return {SZ_SETPOINT: hex_to_temp(payload[2:6])}


@parser_decorator  # WIP: unknown, HVAC
def parser_22e0(payload, msg) -> dict:
    # RP --- 32:155617 18:005904 --:------ 22E0 004 00-34-A0-1E
    # RP --- 32:153258 18:005904 --:------ 22E0 004 00-64-A0-1E
    def _parser(seqx) -> dict:
        assert int(seqx, 16) <= 200 or seqx == "E6"  # only for 22E0, not 22E5/22E9
        return int(seqx, 16) / 200

    try:
        return {
            f"percent_{i}": hex_to_percent(payload[i : i + 2])
            for i in range(2, len(payload), 2)
        }
    except ValueError:
        return {
            "percent_2": hex_to_percent(payload[2:4]),
            "percent_4": _parser(payload[4:6]),
            "percent_6": hex_to_percent(payload[6:8]),
        }


@parser_decorator  # WIP: unknown, HVAC
def parser_22e5(payload, msg) -> dict:
    # RP --- 32:153258 18:005904 --:------ 22E5 004 00-96-C8-14
    # RP --- 32:155617 18:005904 --:------ 22E5 004 00-72-C8-14

    return parser_22e0(payload, msg)


@parser_decorator  # WIP: unknown, HVAC
def parser_22e9(payload, msg) -> dict:
    # RP --- 32:153258 18:005904 --:------ 22E9 004 00C8C814
    # RP --- 32:155617 18:005904 --:------ 22E9 004 008CC814

    return parser_22e0(payload, msg)


@parser_decorator  # fan_speed (switch_mode), HVAC
def parser_22f1(payload, msg) -> dict:
    # Orcon wireless remote 15RF
    # .I --- 37:171871 32:155617 --:------ 22F1 003 000007  # Absent mode  // Afwezig (absence mode, aka: weg/away) - low & doesn't respond to sensors
    # .I --- 37:171871 32:155617 --:------ 22F1 003 000107  # Mode 1: Low  // Stand 1 (position low)
    # .I --- 37:171871 32:155617 --:------ 22F1 003 000207  # Mode 2: Med  // Stand 2 (position med)
    # .I --- 37:171871 32:155617 --:------ 22F1 003 000307  # Mode 3: High // Stand 3 (position high)
    # .I --- 37:171871 32:155617 --:------ 22F1 003 000407  # Auto
    # .I --- 37:171871 32:155617 --:------ 22F1 003 000507  # Auto
    # .I --- 37:171871 32:155617 --:------ 22F1 003 000607  # Party/boost
    # .I --- 37:171871 32:155617 --:------ 22F1 003 000707  # Off

    # .I 015 --:------ --:------ 39:159057 22F1 003 000004  # TBA: off/standby?
    # .I 015 --:------ --:------ 39:159057 22F1 003 000104  # TBA: trickle/min-speed?
    # .I 015 --:------ --:------ 39:159057 22F1 003 000204  # low
    # .I 016 --:------ --:------ 39:159057 22F1 003 000304  # medium
    # .I 017 --:------ --:------ 39:159057 22F1 003 000404  # high (aka boost if timer)

    # Scheme x: 0|x standby/off, 1|x min, 2+|x rate as % of max (Itho?)
    # Scheme 4: 0|4 standby/off, 1|4 auto, 2|4 low, 3|4 med, 4|4 high/boost
    # Scheme 7: only seen 000[2345]07 -- ? off, auto, rate x/4, +3 others?
    # Scheme A: only seen 000[239A]0A -- Normal, Boost (purge), HeaterOff & HeaterAuto

    try:
        assert payload[0:2] in ("00", "63")
        assert not payload[4:] or int(payload[2:4], 16) <= int(
            payload[4:], 16
        ), "mode_idx > mode_max"
    except AssertionError as exc:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({exc})")

    if msg._addrs[0] == NON_DEV_ADDR:  # and payload[4:6] == "04":
        from .ramses import _22F1_MODE_ITHO as _22F1_FAN_MODE  # TODO: only if 04

        _22f1_mode_set = ("", "04")
        _22f1_scheme = "itho"

    # elif msg._addrs[0] == NON_DEV_ADDR:  # and payload[4:6] == "04":
    #     _22F1_FAN_MODE = {
    #         f"{x:02X}": f"speed_{x}" for x in range(int(payload[4:6], 16) + 1)
    #     } | {"00": "off"}

    #     _22f1_mode_set = (payload[4:6], )
    #     _22f1_scheme = "itho_2"

    elif payload[4:6] == "0A":
        from .ramses import _22F1_MODE_NUAIRE as _22F1_FAN_MODE

        _22f1_mode_set = ("", "0A")
        _22f1_scheme = "nuaire"

    else:
        from .ramses import _22F1_MODE_ORCON as _22F1_FAN_MODE

        _22f1_mode_set = ("", "04", "07", "0B")  # 0B?
        _22f1_scheme = "orcon"

    try:
        assert payload[2:4] in _22F1_FAN_MODE, f"unknown fan_mode: {payload[2:4]}"
        assert payload[4:6] in _22f1_mode_set, f"unknown mode_set: {payload[4:6]}"
    except AssertionError as exc:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({exc})")

    return {
        SZ_FAN_MODE: _22F1_FAN_MODE.get(payload[2:4], f"unknown_{payload[2:4]}"),
        "_scheme": _22f1_scheme,
        "_mode_idx": f"{int(payload[2:4], 16) & 0x0F:02X}",
        "_mode_max": payload[4:6] or None,
        # "_payload": payload,
    }


@parser_decorator  # WIP: unknown, HVAC (flow rate?)
def parser_22f2(payload, msg) -> dict:
    # RP --- 32:155617 18:005904 --:------ 22F2 006 00-019B 01-0201
    # RP --- 32:155617 18:005904 --:------ 22F2 006 00-0174 01-0208
    # RP --- 32:155617 18:005904 --:------ 22F2 006 00-01E5 01-0201

    def _parser(seqx) -> dict:
        assert seqx[:2] in ("00", "01"), f"is {seqx[:2]}, expecting 00/01"

        return {
            "hvac_idx": seqx[:2],
            "measure": hex_to_temp(seqx[2:]),
        }

    return [_parser(payload[i : i + 6]) for i in range(0, len(payload), 6)]


@parser_decorator  # fan_boost, HVAC
def parser_22f3(payload, msg) -> dict:
    # .I 019 --:------ --:------ 39:159057 22F3 003 00000A  # 10 mins
    # .I 022 --:------ --:------ 39:159057 22F3 003 000014  # 20 mins
    # .I 026 --:------ --:------ 39:159057 22F3 003 00001E  # 30 mins
    # .I --- 29:151550 29:237552 --:------ 22F3 007 00023C-0304-0000  # 60 mins
    # .I --- 29:162374 29:237552 --:------ 22F3 007 00020F-0304-0000  # 15 mins
    # .I --- 29:162374 29:237552 --:------ 22F3 007 00020F-0304-0000  # 15 mins

    # NOTE: for boost timer for high
    try:
        # assert payload[2:4] in ("00", "02", "12", "x52"), f"byte 1: {flag8(payload[2:4])}"
        assert msg.len <= 7 or payload[14:] == "0000", f"byte 7: {payload[14:]}"
    except AssertionError as exc:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({exc})")

    new_speed = {  # from now, until timer expiry
        0x00: "fan_boost",  # #    set fan off, or 'boost' mode?
        0x01: "per_request",  # #  set fan as per payload[6:10]?
        0x02: "per_vent_speed",  # set fan as per current fan mode/speed?
    }.get(
        int(payload[2:4], 0x10) & 0x07
    )  # 0b0000-0111

    fallback_speed = {  # after timer expiry
        0x08: "fan_off",  # #      set fan off?
        0x10: "per_request",  # #  set fan as per payload[6:10], or payload[10:]?
        0x18: "per_vent_speed",  # set fan as per current fan mode/speed?
    }.get(
        int(payload[2:4], 0x10) & 0x38
    )  # 0b0011-1000

    units = {
        0x00: "minutes",
        0x40: "hours",
        0x80: "index",  # TODO: days, day-of-week, day-of-month?
    }.get(
        int(payload[2:4], 0x10) & 0xC0
    )  # 0b1100-0000

    duration = int(payload[4:6], 16) * 60 if units == "hours" else int(payload[4:6], 16)

    if msg.len >= 3:
        result = {
            "minutes" if units != "index" else "index": duration,
            "flags": hex_to_flag8(payload[2:4]),
            "_new_speed_mode": new_speed,
            "_fallback_speed_mode": fallback_speed,
        }

    if msg.len >= 5 and payload[6:10] != "0000":  # new speed?
        result["rate"] = parser_22f1(f"00{payload[6:10]}", msg).get("rate")

    if msg.len >= 7:  # fallback speed?
        result.update({f"_{SZ_UNKNOWN}_5": payload[10:]})

    return result


@parser_decorator  # WIP: unknown, HVAC
def parser_22f4(payload, msg) -> dict:
    # RP --- 32:155617 18:005904 --:------ 22F4 013 00-60E6-00000000000000-200000
    # RP --- 32:153258 18:005904 --:------ 22F4 013 00-60DD-00000000000000-200000
    # RP --- 32:155617 18:005904 --:------ 22F4 013 00-40B0-00000000000000-200000

    assert payload[:2] == "00"
    assert payload[6:] == "00000000000000200000"

    return {
        "value_02": payload[2:4],
        "value_04": payload[4:6],
    }


@parser_decorator  # bypass_mode, HVAC
def parser_22f7(payload, msg) -> dict:
    # RQ --- 37:171871 32:155617 --:------ 22F7 001 00
    # RP --- 32:155617 37:171871 --:------ 22F7 003 00FF00  # also: 000000, 00C8C8

    # .W --- 37:171871 32:155617 --:------ 22F7 003 0000EF  # bypass off
    # .I --- 32:155617 37:171871 --:------ 22F7 003 000000
    # .W --- 37:171871 32:155617 --:------ 22F7 003 00C8EF  # bypass on
    # .I --- 32:155617 37:171871 --:------ 22F7 003 00C800
    # .W --- 37:171871 32:155617 --:------ 22F7 003 00FFEF  # bypass auto
    # .I --- 32:155617 37:171871 --:------ 22F7 003 00FFC8

    result = {
        "bypass_mode": {"00": "off", "C8": "on", "FF": "auto"}.get(payload[2:4]),
    }
    if msg.verb != W_ or payload[4:] not in ("", "EF"):
        result["bypass_state"] = {"00": "off", "C8": "on"}.get(payload[4:])

    return result


@parser_decorator  # WIP: unknown_mode, HVAC
def parser_22f8(payload, msg) -> dict:
    # from: https://github.com/arjenhiemstra/ithowifi/blob/master/software/NRG_itho_wifi/src/IthoPacket.h

    # message command bytes specific for AUTO RFT (536-0150)
    # ithoMessageAUTORFTAutoNightCommandBytes[] = {0x22, 0xF8, 0x03, 0x63, 0x02, 0x03};
    # .W --- 32:111111 37:111111 --:------ 22F8 003 630203

    # message command bytes specific for DemandFlow remote (536-0146)
    # ithoMessageDFLowCommandBytes[] = {0x22, 0xF8, 0x03, 0x00, 0x01, 0x02};
    # ithoMessageDFHighCommandBytes[] = {0x22, 0xF8, 0x03, 0x00, 0x02, 0x02};

    return {
        "value_02": payload[2:4],
        "value_04": payload[4:6],
    }


@parser_decorator  # setpoint (of device/zones)
def parser_2309(payload, msg) -> dict | list:
    if msg._has_array:
        return [
            {
                SZ_ZONE_IDX: payload[i : i + 2],
                SZ_SETPOINT: hex_to_temp(payload[i + 2 : i + 6]),
            }
            for i in range(0, len(payload), 6)
        ]

    # RQ --- 22:131874 01:063844 --:------ 2309 003 020708
    if msg.verb == RQ and msg.len == 1:  # some RQs have a payload (why?)
        return {}

    return {SZ_SETPOINT: hex_to_temp(payload[2:])}


@parser_decorator  # zone_mode  # TODO: messy
def parser_2349(payload, msg) -> dict:
    # RQ --- 34:225071 30:258557 --:------ 2349 001 00
    # RP --- 30:258557 34:225071 --:------ 2349 013 007FFF00FFFFFFFFFFFFFFFFFF
    # RP --- 30:253184 34:010943 --:------ 2349 013 00064000FFFFFF00110E0507E5
    # .I --- 10:067219 --:------ 10:067219 2349 004 00000001

    if msg.verb == RQ and msg.len <= 2:  # some RQs have a payload (why?)
        return {}

    assert msg.len in (7, 13), f"expected len 7,13, got {msg.len}"

    assert payload[6:8] in ZON_MODE_MAP, f"{SZ_UNKNOWN} zone_mode: {payload[6:8]}"
    result = {
        SZ_MODE: ZON_MODE_MAP.get(payload[6:8]),
        SZ_SETPOINT: hex_to_temp(payload[2:6]),
    }

    if msg.len >= 7:  # has a dtm if mode == "04"
        if payload[8:14] == "FF" * 3:  # 03/FFFFFF OK if W?
            assert payload[6:8] != ZON_MODE_MAP.COUNTDOWN, f"{payload[6:8]} (0x00)"
        else:
            assert payload[6:8] == ZON_MODE_MAP.COUNTDOWN, f"{payload[6:8]} (0x01)"
            result[SZ_DURATION] = int(payload[8:14], 16)

    if msg.len >= 13:
        if payload[14:] == "FF" * 6:
            assert payload[6:8] in (
                ZON_MODE_MAP.FOLLOW,
                ZON_MODE_MAP.PERMANENT,
            ), f"{payload[6:8]} (0x02)"
            result[SZ_UNTIL] = None  # TODO: remove?
        else:
            assert payload[6:8] != ZON_MODE_MAP.PERMANENT, f"{payload[6:8]} (0x03)"
            result[SZ_UNTIL] = hex_to_dtm(payload[14:26])

    return result


@parser_decorator  # unknown_2389, from 03:
def parser_2389(payload, msg) -> dict:
    return {
        f"_{SZ_UNKNOWN}": hex_to_temp(payload[2:6]),
    }


@parser_decorator  # unknown_2400, from OTB, FAN
def parser_2400(payload, msg) -> dict:
    # RP --- 32:155617 18:005904 --:------ 2400 045 00001111-1010929292921110101020110010000080100010100000009191111191910011119191111111111100  # Orcon FAN
    # RP --- 10:048122 18:006402 --:------ 2400 004 0000000F
    # assert payload == "0000000F", _INFORM_DEV_MSG

    return {
        SZ_PAYLOAD: payload,
    }


@parser_decorator  # unknown_2401, from OTB
def parser_2401(payload, msg) -> dict:
    try:
        assert payload[2:4] == "00", f"byte 1: {payload[2:4]}"
        assert (
            int(payload[4:6], 16) & 0b11110000 == 0
        ), f"byte 2: {hex_to_flag8(payload[4:6])}"
        assert int(payload[6:], 0x10) <= 200, f"byte 3: {payload[6:]}"
    except AssertionError as exc:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({exc})")

    return {
        SZ_PAYLOAD: payload,
        "_value_2": int(payload[4:6], 0x10),
        "_flags_2": hex_to_flag8(payload[4:6]),
        "_percent_3": hex_to_percent(payload[6:]),
    }


@parser_decorator  # unknown_2410, from OTB, FAN
def parser_2410(payload, msg) -> dict:
    # RP --- 10:048122 18:006402 --:------ 2410 020 00-00000000-00000000-00000001-00000001-00000C  # OTB
    # RP --- 32:155617 18:005904 --:------ 2410 020 00-00003EE8-00000000-FFFFFFFF-00000000-1002A6  # Orcon Fan

    def unstuff(seqx: str) -> tuple:
        val = int(seqx, 16)
        # if val & 0x40:
        #     raise TypeError
        signed = bool(val & 0x80)
        length = (val >> 3 & 0x07) or 1
        d_type = {0b000: "a", 0b001: "b", 0b010: "c", 0b100: "d"}.get(
            val & 0x07, val & 0x07
        )
        return signed, length, d_type

    assert payload[:6] == "00" * 3, _INFORM_DEV_MSG
    assert payload[10:18] == "00" * 4, _INFORM_DEV_MSG
    assert payload[18:26] in ("00000001", "FFFFFFFF"), _INFORM_DEV_MSG
    assert payload[26:34] in ("00000001", "00000000"), _INFORM_DEV_MSG

    return {
        "tail": payload[34:],
        "xxx_34": unstuff(payload[34:36]),
        "xxx_36": unstuff(payload[36:38]),
        "xxx_38": unstuff(payload[38:]),
        "cur_value": payload[2:10],
        "min_value": payload[10:18],
        "max_value": payload[18:26],
        "oth_value": payload[26:34],
    }


@parser_decorator  # fan_params, HVAC
def parser_2411(payload, msg) -> dict:
    # There is a relationship between 0001 and 2411
    # RQ --- 37:171871 32:155617 --:------ 0001 005 0020000A04
    # RP --- 32:155617 37:171871 --:------ 0001 008 0020000A004E0B00  # 0A -> 2411|4E
    # RQ --- 37:171871 32:155617 --:------ 2411 003 00004E            # 11th menu option (i.e. 0x0A)
    # RP --- 32:155617 37:171871 --:------ 2411 023 00004E460000000001000000000000000100000001A600

    def counter(x) -> int:
        return int(x, 16)

    def centile(x) -> float:
        return int(x, 16) / 10

    _2411_DATA_TYPES = {
        "00": (2, counter),  # 4E (0-1), 54 (15-60)
        "01": (2, centile),  # 52 (0.0-25.0) (%)
        "0F": (2, hex_to_percent),  # xx (0.0-1.0) (%)
        "10": (4, counter),  # 31 (0-1800) (days)
        "92": (4, hex_to_temp),  # 75 (0-30) (C)
    }  # TODO: _2411_TYPES.get(payload[8:10], (8, no_op))

    assert (
        payload[4:6] in _2411_TABLE
    ), f"param {payload[4:6]} is unknown"  # _INFORM_DEV_MSG
    description = _2411_TABLE.get(payload[4:6], "Unknown")

    result = {
        "parameter": payload[4:6],
        "description": description,
    }

    if msg.verb == RQ:
        return result

    assert (
        payload[8:10] in _2411_DATA_TYPES
    ), f"param {payload[4:6]} has unknown data_type: {payload[8:10]}"  # _INFORM_DEV_MSG
    length, parser = _2411_DATA_TYPES.get(payload[8:10], (8, lambda x: x))

    result |= {
        "value": parser(payload[10:18][-length:]),
        f"_{SZ_VALUE}_06": payload[6:10],
    }

    if msg.len == 9:
        return result

    return result | {
        "min_value": parser(payload[18:26][-length:]),
        "max_value": parser(payload[26:34][-length:]),
        "precision": parser(payload[34:42][-length:]),
        f"_{SZ_VALUE}_42": payload[42:],
    }


@parser_decorator  # unknown_2420, from OTB
def parser_2420(payload, msg) -> dict:
    assert payload == "00000010" + "00" * 34, _INFORM_DEV_MSG

    return {
        SZ_PAYLOAD: payload,
    }


@parser_decorator  # _state (of cooling?), from BDR91T, hometronics CTL
def parser_2d49(payload, msg) -> dict:
    assert payload[2:] in ("0000", "00FF", "C800", "C8FF"), _INFORM_DEV_MSG

    return {
        "state": hex_to_bool(payload[2:4]),
    }


@parser_decorator  # system_mode
def parser_2e04(payload, msg) -> dict:
    # if msg.verb == W_:

    # .I --— 01:020766 --:------ 01:020766 2E04 016 FFFFFFFFFFFFFF0007FFFFFFFFFFFF04  # Manual          # noqa: E501
    # .I --— 01:020766 --:------ 01:020766 2E04 016 FFFFFFFFFFFFFF0000FFFFFFFFFFFF04  # Automatic/times # noqa: E501

    if msg.len == 8:  # evohome
        assert payload[:2] in SYS_MODE_MAP, f"Unknown system mode: {payload[:2]}"

    elif msg.len == 16:  # hometronics, lifestyle ID:
        assert 0 <= int(payload[:2], 16) <= 15 or payload[:2] == FF, payload[:2]
        assert payload[16:18] in (SYS_MODE_MAP.AUTO, SYS_MODE_MAP.CUSTOM), payload[
            16:18
        ]
        assert payload[30:32] == SYS_MODE_MAP.DAY_OFF, payload[30:32]
        # assert False

    else:
        # msg.len in (8, 16)  # evohome 8, hometronics 16
        assert False, f"Packet length is {msg.len} (expecting 8, 16)"

    result = {SZ_SYSTEM_MODE: SYS_MODE_MAP[payload[:2]]}
    if payload[:2] not in (
        SYS_MODE_MAP.AUTO,
        SYS_MODE_MAP.HEAT_OFF,
        SYS_MODE_MAP.AUTO_WITH_RESET,
    ):
        result.update(
            {SZ_UNTIL: hex_to_dtm(payload[2:14]) if payload[14:16] != "00" else None}
        )
    return result  # TODO: double-check the final "00"


@parser_decorator  # presence_detect, HVAC sensor
def parser_2e10(payload, msg) -> dict:
    assert payload in ("0001", "000100"), _INFORM_DEV_MSG

    return {
        "presence_detected": bool(payload[2:4]),
        f"_{SZ_UNKNOWN}_4": payload[4:],
    }


@parser_decorator  # current temperature (of device, zone/s)
def parser_30c9(payload, msg) -> dict:
    if msg._has_array:
        return [
            {
                SZ_ZONE_IDX: payload[i : i + 2],
                SZ_TEMPERATURE: hex_to_temp(payload[i + 2 : i + 6]),
            }
            for i in range(0, len(payload), 6)
        ]

    return {SZ_TEMPERATURE: hex_to_temp(payload[2:])}


@parser_decorator  # ufc_demand, HVAC (Itho autotemp / spider)
def parser_3110(payload, msg) -> dict:
    # .I --- 02:250708 --:------ 02:250708 3110 004 0000C820  # cooling, 100%
    # .I --- 21:042656 --:------ 21:042656 3110 004 00000010  # heating, 0%

    SZ_COOLING = "cooling"
    SZ_DISABLE = "disabled"
    SZ_HEATING = "heating"
    SZ_UNKNOWN = "unknown"

    try:
        assert payload[2:4] == "00", f"byte 1: {payload[2:4]}"  # ?circuit_idx?
        assert int(payload[4:6], 16) <= 200, f"byte 2: {payload[4:6]}"
        assert payload[6:] in ("00", "10", "20"), f"byte 3: {payload[6:]}"
        assert (
            payload[6:] in ("10", "20") or payload[4:6] == "00"
        ), f"byte 3: {payload[6:]}"
    except AssertionError as exc:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({exc})")

    mode = {
        0x00: SZ_DISABLE,
        0x10: SZ_HEATING,
        0x20: SZ_COOLING,
    }.get(int(payload[6:8], 16) & 0x30, SZ_UNKNOWN)

    if mode not in (SZ_COOLING, SZ_HEATING):
        return {"mode": mode}

    return {"mode": mode, "demand": hex_to_percent(payload[4:6])}


@parser_decorator  # unknown_3120, from STA, FAN
def parser_3120(payload, msg) -> dict:
    # .I --- 34:136285 --:------ 34:136285 3120 007 0070B0000000FF  # every ~3:45:00!
    # RP --- 20:008749 18:142609 --:------ 3120 007 0070B000009CFF
    # .I --- 37:258565 --:------ 37:258565 3120 007 0080B0010003FF

    try:
        assert payload[:2] == "00", f"byte 0: {payload[:2]}"
        assert payload[2:4] in ("00", "70", "80"), f"byte 1: {payload[2:4]}"
        assert payload[4:6] == "B0", f"byte 2: {payload[4:6]}"
        assert payload[6:8] in ("00", "01"), f"byte 3: {payload[6:8]}"
        assert payload[8:10] == "00", f"byte 4: {payload[8:10]}"
        assert payload[10:12] in ("00", "03", "0A", "9C"), f"byte 5: {payload[10:12]}"
        assert payload[12:] == "FF", f"byte 6: {payload[12:]}"
    except AssertionError as exc:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({exc})")

    return {
        f"{SZ_UNKNOWN}_0": payload[2:10],
        f"{SZ_UNKNOWN}_5": payload[10:12],
        f"{SZ_UNKNOWN}_2": payload[12:],
    }


@parser_decorator  # WIP: unknown, HVAC
def parser_313e(payload, msg) -> dict:
    # 11:00:59.412 RP --- 32:153258 18:005904 --:------ 313E 011 00-0000007937-003C80-0000
    # 11:02:23.961 RP --- 32:153258 18:005904 --:------ 313E 011 00-0000007B14-003C80-0000
    # 11:03:32.193 RP --- 32:153258 18:005904 --:------ 313E 011 00-0000007C1C-003C80-0000

    assert payload[:2] == "00"
    assert payload[12:] == "003C800000"

    return {
        "value_02": payload[2:12],
        "value_12": payload[12:18],
        "value_18": payload[18:],
    }


@parser_decorator  # datetime
def parser_313f(payload, msg) -> dict:  # TODO: look for TZ
    # 2020-03-28T03:59:21.315178 045 RP --- 01:158182 04:136513 --:------ 313F 009 00FC3500A41C0307E4  # noqa: E501
    # 2020-03-29T04:58:30.486343 045 RP --- 01:158182 04:136485 --:------ 313F 009 00FC8400C51D0307E4  # noqa: E501
    # 2022-09-20T20:50:32.800676 065 RP --- 01:182924 18:068640 --:------ 313F 009 00F9203234140907E6
    # 2020-05-31T11:37:50.351511 056  I --- --:------ --:------ 12:207082 313F 009 0038021ECB1F0507E4  # noqa: E501

    # https://www.automatedhome.co.uk/vbulletin/showthread.php?5085-My-HGI80-equivalent-Domoticz-setup-without-HGI80&p=36422&viewfull=1#post36422
    # every day at ~4am TRV/RQ->CTL/RP, approx 5-10secs apart (CTL respond at any time)

    assert msg.src.type != DEV_TYPE_MAP.CTL or payload[2:4] in (
        "F0",
        "F9",
        "FC",
    ), f"{payload[2:4]} unexpected for CTL"  # DEX
    assert (
        msg.src.type not in (DEV_TYPE_MAP.DTS, DEV_TYPE_MAP.DT2) or payload[2:4] == "38"
    ), f"{payload[2:4]} unexpected for DTS"  # DEX
    # assert (
    #     msg.src.type != DEV_TYPE_MAP.FAN or payload[2:4] == "7C"
    # ), f"{payload[2:4]} unexpected for FAN"  # DEX
    assert (
        msg.src.type != DEV_TYPE_MAP.RFG or payload[2:4] == "60"
    ), "{payload[2:4]} unexpected for RFG"  # DEX

    return {
        SZ_DATETIME: hex_to_dtm(payload[4:18]),
        SZ_IS_DST: True if bool(int(payload[4:6], 16) & 0x80) else None,
        f"_{SZ_UNKNOWN}_0": payload[2:4],
    }


@parser_decorator  # heat_demand (of device, FC domain) - valve status (%open)
def parser_3150(payload, msg) -> dict | list:
    # event-driven, and periodically; FC domain is maximum of all zones
    # TODO: all have a valid domain will UFC/CTL respond to an RQ, for FC, for a zone?

    # .I --- 04:136513 --:------ 01:158182 3150 002 01CA < often seen CA, artefact?

    def complex_idx(seqx, msg) -> dict:
        # assert seqx[:2] == FC or (int(seqx[:2], 16) < MAX_ZONES)  # <5, 8 for UFC
        idx_name = "ufx_idx" if msg.src.type == DEV_TYPE_MAP.UFC else SZ_ZONE_IDX  # DEX
        return {SZ_DOMAIN_ID if seqx[:1] == "F" else idx_name: seqx[:2]}

    if msg._has_array:
        return [
            {
                **complex_idx(payload[i : i + 2], msg),
                **parser_valve_demand(payload[i + 2 : i + 4]),
            }
            for i in range(0, len(payload), 4)
        ]

    return parser_valve_demand(payload[2:])  # TODO: check UFC/FC is == CTL/FC


@parser_decorator  # fan state (ventilation status), HVAC
def parser_31d9(payload, msg) -> dict:
    # NOTE: I have a suspicion that Itho use 0x00-C8 for %, whilst Nuaire use 0x00-64
    try:
        assert (
            payload[4:6] == "FF" or int(payload[4:6], 16) <= 200
        ), f"byte 2: {payload[4:6]}"
    except AssertionError as exc:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({exc})")

    bitmap = int(payload[2:4], 16)

    # NOTE: 31D9[4:6] is fan_rate (itho?) *or* fan_mode (orcon?)
    result = {
        **parse_exhaust_fan_speed(payload[4:6]),  # itho
        SZ_FAN_MODE: payload[4:6],  # orcon
        "passive": bool(bitmap & 0x02),
        "damper_only": bool(bitmap & 0x04),  # i.e. valve only
        "filter_dirty": bool(bitmap & 0x20),
        "frost_cycle": bool(bitmap & 0x40),
        "has_fault": bool(bitmap & 0x80),
        "_flags": hex_to_flag8(payload[2:4]),
    }

    if msg.len == 3:  # usu: I -->20: (no seq#)
        return result

    try:
        assert payload[6:8] in ("00", "07", "0A", "FE"), f"byte 3: {payload[6:8]}"
    except AssertionError as exc:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({exc})")

    result.update({f"_{SZ_UNKNOWN}_3": payload[6:8]})

    if msg.len == 4:  # usu: I -->20: (no seq#)
        return result

    try:
        assert payload[8:32] in ("00" * 12, "20" * 12), f"byte 4: {payload[8:32]}"
        assert payload[32:] in ("00", "04", "08"), f"byte 16: {payload[32:]}"
    except AssertionError as exc:
        _LOGGER.warning(f"{msg!r} < {_INFORM_DEV_MSG} ({exc})")

    return {
        **result,
        f"_{SZ_UNKNOWN}_4": payload[8:32],
        f"{SZ_UNKNOWN}_16": payload[32:],
    }


@parser_decorator  # ventilation state (extended), HVAC
def parser_31da(payload, msg) -> dict:
    return {
        **parse_fan_info(payload[36:38]),  # 22F3-ish
        #
        **parse_air_quality(payload[2:6]),  # 12C8[2:6]
        **parse_co2_level(payload[6:10]),  # 1298[2:6]
        **parse_indoor_humidity(payload[10:12]),  # 12A0?
        **parse_outdoor_humidity(payload[12:14]),
        **parse_exhaust_temp(payload[14:18]),  # to outside
        **parse_supply_temp(payload[18:22]),  # to home
        **parse_indoor_temp(payload[22:26]),  # in home
        **parse_outdoor_temp(payload[26:30]),  # 1290?
        **parse_capabilities(payload[30:34]),
        **parse_bypass_position(payload[34:36]),  # 22F7-ish
        **parse_exhaust_fan_speed(payload[38:40]),  # maybe 31D9[4:6] for some?
        **parse_supply_fan_speed(payload[40:42]),
        **parse_remaining_time(payload[42:46]),  # mins, ~22F3[2:6]
        **parse_post_heater(payload[46:48]),
        **parse_pre_heater(payload[48:50]),
        **parse_supply_flow(payload[50:54]),  # NOTE: is supply, not exhaust
        **parse_exhaust_flow(payload[54:58]),  # NOTE: order switched from others
    }

    # From an Orcon 15RF Display
    #  1 Software version
    #  4 RH value in home (%)                 SZ_INDOOR_HUMIDITY
    #  5 RH value supply air (%)              SZ_OUTDOOR_HUMIDITY
    #  6 Exhaust air temperature out (°C)     SZ_EXHAUST_TEMPERATURE
    #  7 Supply air temperature to home (°C)  SZ_SUPPLY_TEMPERATURE
    #  8 Temperature from home (°C)           SZ_INDOOR_TEMPERATURE
    #  9 Temperature outside (°C)             SZ_OUTDOOR_TEMPERATURE
    # 10 Bypass position                      SZ_BYPASS_POSITION
    # 11 Exhaust fan speed (%)                SZ_EXHAUST_FAN_SPEED
    # 12 Fan supply speed (%)                 SZ_SUPPLY_FAN_SPEED
    # 13 Remaining after run time (min.)      SZ_REMAINING_TIME - for humidity scenario
    # 14 Preheater control (MaxComfort) (%)   SZ_PRE_HEAT
    # 16 Actual supply flow rate (m3/h)       SZ_SUPPLY_FLOW (Orcon is m3/h, data is L/s)
    # 17 Current discharge flow rate (m3/h)   SZ_EXHAUST_FLOW


@parser_decorator  # vent_demand, HVAC
def parser_31e0(payload, msg) -> dict:
    """Notes are.

    van means “of”.
    - 0 = min. van min. potm would be:
    - 0 = minimum of minimum potentiometer

    See: https://www.industrialcontrolsonline.com/honeywell-t991a
    - modulates air temperatures in ducts

    case 0x31E0:  ' 12768:
    {
        string str4;
        unchecked
        {
            result.Fan = Conversions.ToString((double)(int)data[checked(start + 1)] / 2.0);
            str4 = "";
        }
        str4 = (data[start + 2] & 0xF) switch
        {
            0 => str4 + "0 = min. potm. ",
            1 => str4 + "0 = min. van min. potm ",
            2 => str4 + "0 = min. fan ",
            _ => "",
        };
        switch (data[start + 2] & 0xF0)
        {
        case 16:
            str4 += "100 = max. potm";
            break;
        case 32:
            str4 += "100 = max. van max. potm ";
            break;
        case 48:
            str4 += "100 = max. fan ";
            break;
        }
        result.Data = str4;
        break;
    }
    """

    # .I --- 37:005302 32:132403 --:------ 31E0 008 00-0000-00 01-0064-00  # RF15 CO2 to Orcon HRC400 series SmartComfort Valve

    # .I --- 29:146052 32:023459 --:------ 31E0 003 00-0000
    # .I --- 29:146052 32:023459 --:------ 31E0 003 00-00C8

    # .I --- 32:168240 30:079129 --:------ 31E0 004 00-0000-FF
    # .I --- 32:168240 30:079129 --:------ 31E0 004 00-0000-FF
    # .I --- 32:166025 --:------ 30:079129 31E0 004 00-0000-00

    # .I --- 32:168090 30:082155 --:------ 31E0 004 00-00C8-00
    # .I --- 37:258565 37:261128 --:------ 31E0 004 00-0001-00

    def _parser(seqx) -> dict:
        assert seqx[6:] in ("", "00", "FF")
        return {
            # "hvac_idx": seqx[:2],
            "flags": seqx[2:4],
            "vent_demand": hex_to_percent(seqx[4:6]),
            f"_{SZ_UNKNOWN}_3": payload[6:],
        }

    if len(payload) > 8:
        return [_parser(payload[x : x + 8]) for x in range(0, len(payload), 8)]
    return _parser(payload)


@parser_decorator  # supplied boiler water (flow) temp
def parser_3200(payload, msg) -> dict:
    return {SZ_TEMPERATURE: hex_to_temp(payload[2:])}


@parser_decorator  # return (boiler) water temp
def parser_3210(payload, msg) -> dict:
    return {SZ_TEMPERATURE: hex_to_temp(payload[2:])}


@parser_decorator  # opentherm_msg, from OTB
def parser_3220(payload, msg) -> dict:
    try:
        ot_type, ot_id, ot_value, ot_schema = decode_frame(payload[2:10])
    except AssertionError as exc:
        raise AssertionError(f"OpenTherm: {exc}") from exc
    except ValueError as exc:
        raise InvalidPayloadError(f"OpenTherm: {exc}") from exc

    # NOTE: Unknown-DataId isn't an invalid payload & is useful to train the OTB device
    if ot_schema is None and ot_type != OtMsgType.UNKNOWN_DATAID:
        raise InvalidPayloadError(f"OpenTherm: Unknown data-id: {ot_id}")

    result = {
        MSG_ID: ot_id,
        MSG_TYPE: ot_type,
        MSG_NAME: ot_value.pop(MSG_NAME, None),
    }

    if msg.verb == RQ:  # RQs have a context: msg_id (and a payload)
        assert (
            ot_type != OtMsgType.READ_DATA
            or payload[6:10] == "0000"  # likely true for RAMSES
        ), f"OpenTherm: Invalid msg-type|data-value: {ot_type}|{payload[6:10]}"

        if ot_type != OtMsgType.READ_DATA:
            assert ot_type in (
                OtMsgType.WRITE_DATA,
                OtMsgType.INVALID_DATA,
            ), f"OpenTherm: Invalid msg-type for RQ: {ot_type}"

            result.update(ot_value)  # TODO: find some of these packets to review

    else:  # if msg.verb == RP:
        _LIST = (OtMsgType.DATA_INVALID, OtMsgType.UNKNOWN_DATAID, OtMsgType.RESERVED)
        assert ot_type not in _LIST or payload[6:10] in (
            "0000",
            "FFFF",
        ), f"OpenTherm: Invalid msg-type|data-value: {ot_type}|{payload[6:10]}"

        if ot_type not in _LIST:
            assert ot_type in (
                OtMsgType.READ_ACK,
                OtMsgType.WRITE_ACK,
            ), f"OpenTherm: Invalid msg-type for RP: {ot_type}"

            result.update(ot_value)

        try:
            assert ot_id != 0 or (
                [result[SZ_VALUE][i] for i in (2, 3, 4, 5, 6, 7)] == [0] * 6
            ), result[SZ_VALUE]

            assert ot_id != 0 or (
                [result[SZ_VALUE][8 + i] for i in (0, 4, 5, 6, 7)] == [0] * 5
            ), result[SZ_VALUE]
        except AssertionError:
            _LOGGER.warning(
                f"{msg!r} < {_INFORM_DEV_MSG}, with a description of your system"
            )

    result[MSG_DESC] = ot_schema.get(EN)
    return result


@parser_decorator  # unknown_3221, from OTB, FAN
def parser_3221(payload, msg) -> dict:
    # RP --- 10:052644 18:198151 --:------ 3221 002 000F
    # RP --- 10:048122 18:006402 --:------ 3221 002 0000
    # RP --- 32:155617 18:005904 --:------ 3221 002 000A

    assert int(payload[2:], 16) <= 0xC8, _INFORM_DEV_MSG

    return {
        f"_{SZ_PAYLOAD}": payload,
        SZ_VALUE: int(payload[2:], 16),
    }


@parser_decorator  # WIP: unknown, HVAC
def parser_3222(payload, msg) -> dict:
    # 06:30:14.322 RP --- 32:155617 18:005904 --:------ 3222 004 00-00-01-00
    # 00:09:26.263 RP --- 32:155617 18:005904 --:------ 3222 005 00-00-02-0009
    # 02:42:27.090 RP --- 32:155617 18:005904 --:------ 3222 007 00-06-04-            000F100E
    # 22:06:45.771 RP --- 32:155617 18:005904 --:------ 3222 011 00-02-08-    0009000F000F100E
    # 13:30:26.792 RP --- 32:155617 18:005904 --:------ 3222 012 00-01-09-  090009000F000F100E
    # 06:29:40.767 RP --- 32:155617 18:005904 --:------ 3222 013 00-00-0A-00090009000F000F100E

    assert payload[:2] == "00"

    if msg.len == 3:
        assert payload[4:] == "00"
        return {"percentage": hex_to_percent(payload[2:4])}

    return {
        "start": payload[2:4],
        "length": payload[4:6],
        "data": f"{'..' * int(payload[2:4])}{payload[6:]}",
    }


@parser_decorator  # unknown_3223, from OTB
def parser_3223(payload, msg) -> dict:
    assert int(payload[2:], 16) <= 0xC8, _INFORM_DEV_MSG

    return {
        f"_{SZ_PAYLOAD}": payload,
        SZ_VALUE: int(payload[2:], 16),
    }


@parser_decorator  # actuator_sync (aka sync_tpi: TPI cycle sync)
def parser_3b00(payload, msg) -> dict:
    # system timing master: the device that sends I/FCC8 pkt controls the heater relay
    """Decode a 3B00 packet (actuator_sync).

    The heat relay regularly broadcasts a 3B00 at the end(?) of every TPI cycle, the
    frequency of which is determined by the (TPI) cycle rate in 1100.

    The CTL subsequently broadcasts a 3B00 (i.e. at the start of every TPI cycle).

    The OTB does not send these packets, but the CTL sends a regular broadcast anyway
    for the benefit of any zone actuators (e.g. zone valve zones).
    """

    # 053  I --- 13:209679 --:------ 13:209679 3B00 002 00C8
    # 045  I --- 01:158182 --:------ 01:158182 3B00 002 FCC8
    # 052  I --- 13:209679 --:------ 13:209679 3B00 002 00C8
    # 045  I --- 01:158182 --:------ 01:158182 3B00 002 FCC8

    # 063  I --- 01:078710 --:------ 01:078710 3B00 002 FCC8
    # 064  I --- 01:078710 --:------ 01:078710 3B00 002 FCC8

    def complex_idx(payload, msg) -> dict:  # has complex idx
        if (
            msg.verb == I_
            and msg.src.type in (DEV_TYPE_MAP.CTL, DEV_TYPE_MAP.PRG)
            and msg.src is msg.dst
        ):  # DEX
            assert payload[:2] == FC
            return {SZ_DOMAIN_ID: FC}
        assert payload[:2] == "00"
        return {}

    assert msg.len == 2, msg.len
    assert payload[:2] == {
        DEV_TYPE_MAP.CTL: FC,
        DEV_TYPE_MAP.BDR: "00",
        DEV_TYPE_MAP.PRG: FC,
    }.get(
        msg.src.type, "00"
    )  # DEX
    assert payload[2:] == "C8", payload[2:]  # Could it be a percentage?

    return {
        **complex_idx(payload[:2], msg),
        "actuator_sync": hex_to_bool(payload[2:]),
    }


@parser_decorator  # actuator_state
def parser_3ef0(payload, msg) -> dict:
    if msg.src.type == DEV_TYPE_MAP.JIM:  # Honeywell Jasper
        assert msg.len == 20, f"expecting len 20, got: {msg.len}"
        return {
            "ordinal": f"0x{payload[2:8]}",
            "blob": payload[8:],
        }

    # TODO: These two should be picked up by the regex
    assert msg.len in (3, 6, 9), f"Invalid payload length: {msg.len}"
    # assert payload[:2] == "00", f"Invalid payload context: {payload[:2]}"

    if msg.len == 3:  # I|BDR|003 (the following are the only two payloads ever seen)
        # .I --- 13:042805 --:------ 13:042805 3EF0 003 0000FF
        # .I --- 13:023770 --:------ 13:023770 3EF0 003 00C8FF
        assert payload[2:4] in ("00", "C8"), f"byte 1: {payload[2:4]} (not 00/C8)"
        assert payload[4:6] == "FF", f"byte 2: {payload[4:6]} (not FF)"
        mod_level = hex_to_percent(payload[2:4])  # , high_res=True)

    else:  # msg.len >= 6:  # RP|OTB|006 (to RQ|CTL/HGI/RFG)
        # RP --- 10:004598 34:003611 --:------ 3EF0 006 0000100000FF
        # RP --- 10:004598 34:003611 --:------ 3EF0 006 0000110000FF
        # RP --- 10:138822 01:187666 --:------ 3EF0 006 0064100C00FF
        # RP --- 10:138822 01:187666 --:------ 3EF0 006 0064100200FF
        assert payload[4:6] in ("00", "10", "11"), f"byte 2: {payload[4:6]}"
        mod_level = hex_to_percent(payload[2:4], high_res=False)  # 00-64 (or FF)

    result = {
        "modulation_level": mod_level,  # 0008[2:4], 3EF1[10:12]
        "_flags_2": payload[4:6],
    }

    if msg.len >= 6:  # RP|OTB|006 (to RQ|CTL/HGI/RFG)
        # RP --- 10:138822 01:187666 --:------ 3EF0 006 000110FA00FF  # ?corrupt

        # for OTB (there's no reliable) modulation_level <-> flame_state)

        result.update(
            {
                "_flags_3": hex_to_flag8(payload[6:8]),
                "ch_active": bool(int(payload[6:8], 0x10) & 1 << 1),
                "dhw_active": bool(int(payload[6:8], 0x10) & 1 << 2),
                "cool_active": bool(int(payload[6:8], 0x10) & 1 << 4),
                "flame_on": bool(int(payload[6:8], 0x10) & 1 << 3),  # flame_on
                "_unknown_4": payload[8:10],  # FF, 00, 01, 0A
                "_unknown_5": payload[10:12],  # FF, 1C, ?others
            }
        )

    if msg.len >= 9:  # I/RP|OTB|009 (R8820A only?)
        assert int(payload[12:14], 16) & 0b11111100 == 0, f"byte 6: {payload[12:14]}"
        assert int(payload[12:14], 16) & 0b00000010 == 2, f"byte 6: {payload[12:14]}"
        assert 10 <= int(payload[14:16], 16) <= 90, f"byte 7: {payload[14:16]}"
        assert int(payload[16:18], 16) in (0, 100), f"byte 8: {payload[18:]}"

        result.update(
            {
                "_flags_6": hex_to_flag8(payload[12:14]),
                "ch_enabled": bool(int(payload[12:14], 0x10) & 1 << 0),
                "ch_setpoint": int(payload[14:16], 0x10),
                "max_rel_modulation": hex_to_percent(payload[16:18], high_res=False),
            }
        )

    try:  # Trying to decode flags...
        # assert payload[4:6] != "11" or (
        #     payload[2:4] == "00"
        # ), f"bytes 1+2: {payload[2:6]}"  # 97% is 00 when 11, but not always

        assert payload[4:6] in ("00", "10", "11", "FF"), f"byte 2: {payload[4:6]}"

        assert "_flags_3" not in result or (
            payload[6:8] == "FF" or int(payload[6:8], 0x10) & 0b10100000 == 0
        ), f'byte 3: {result["_flags_3"]}'
        # only 10:040239 does 0b01000000, only Itho Autotemp does 0b00010000

        assert "_unknown_4" not in result or (
            payload[8:10] in ("FF", "00", "01", "02", "04", "0A")
        ), f"byte 4: {payload[8:10]}"
        # only 10:040239 does 04

        assert "_unknown_5" not in result or (
            payload[10:12] in ("00", "1C", "FF")
        ), f"byte 5: {payload[10:12]}"

        assert "_flags_6" not in result or (
            int(payload[12:14], 0x10) & 0b11111100 == 0
        ), f'byte 6: {result["_flags_6"]}'

    except AssertionError as exc:
        _LOGGER.warning(
            f"{msg!r} < {_INFORM_DEV_MSG} ({exc}), with a description of your system"
        )

    return result


@parser_decorator  # actuator_cycle
def parser_3ef1(payload, msg) -> dict:
    if msg.src.type == DEV_TYPE_MAP.JIM:  # Honeywell Jasper, DEX
        assert msg.len == 18, f"expecting len 18, got: {msg.len}"
        return {
            "ordinal": f"0x{payload[2:8]}",
            "blob": payload[8:],
        }

    if (
        msg.src.type == DEV_TYPE_MAP.JST
    ):  # and msg.len == 12:  # or (12, 20) Japser, DEX
        assert msg.len == 12, f"expecting len 12, got: {msg.len}"
        return {
            "ordinal": f"0x{payload[2:8]}",
            "blob": payload[8:],
        }

    if payload[12:] == "FF":  # is BDR
        # assert (
        #     re.compile(r"^00[0-9A-F]{10}FF").match(payload)
        # ), "doesn't match: " + r"^00[0-9A-F]{10}FF"
        assert int(payload[2:6], 16) <= 7200, f"byte 1: {payload[2:6]}"
        # assert payload[6:10] in ("87B3", "9DFA", "DCE1", "E638", "F8F7") or (
        #     int(payload[6:10], 16) <= 7200
        # ), f"byte 3: {payload[6:10]}"
        assert hex_to_percent(payload[10:12]) in (0, 1), f"byte 5: {payload[10:12]}"

    else:  # is OTB
        # assert (
        #     re.compile(r"^00[0-9A-F]{10}10").match(payload)
        # ), "doesn't match: " + r"^00[0-9A-F]{10}10"
        assert payload[2:6] == "7FFF", f"byte 1: {payload[2:6]}"
        assert payload[6:10] == "003C", f"byte 3: {payload[6:10]}"  # 60 seconds
        assert hex_to_percent(payload[10:12]) <= 1, f"byte 5: {payload[10:12]}"

    cycle_countdown = None if payload[2:6] == "7FFF" else int(payload[2:6], 16)

    return {
        "modulation_level": hex_to_percent(payload[10:12]),  # 0008[2:4], 3EF0[2:4]
        "actuator_countdown": int(payload[6:10], 16),
        "cycle_countdown": cycle_countdown,
        f"_{SZ_UNKNOWN}_0": payload[12:],
    }


@parser_decorator  # timestamp, HVAC
def parser_4401(payload, msg) -> dict:
    if msg.verb == RP:
        return {}

    # 2022-07-28T14:21:38.895354 095  W --- 37:010164 37:010151 --:------ 4401 020 10  7E-E99E90C8  00-E99E90C7-3BFF  7E-E99E90C8-000B
    # 2022-07-28T14:21:57.414447 076 RQ --- 20:225479 20:257336 --:------ 4401 020 10  2E-E99E90DB  00-00000000-0000  00-00000000-000B
    # 2022-07-28T14:21:57.625474 045  I --- 20:257336 20:225479 --:------ 4401 020 10  2E-E99E90DB  00-E99E90DA-F0FF  BD-00000000-000A
    # 2022-07-28T14:22:02.932576 088 RQ --- 37:010188 20:257336 --:------ 4401 020 10  22-E99E90E0  00-00000000-0000  00-00000000-000B
    # 2022-07-28T14:22:03.053744 045  I --- 20:257336 37:010188 --:------ 4401 020 10  22-E99E90E0  00-E99E90E0-75FF  BD-00000000-000A
    # 2022-07-28T14:22:20.516363 045 RQ --- 20:255710 20:257400 --:------ 4401 020 10  0B-E99E90F2  00-00000000-0000  00-00000000-000B
    # 2022-07-28T14:22:20.571640 085  I --- 20:255251 20:229597 --:------ 4401 020 10  39-E99E90F1  00-E99E90F1-5CFF  40-00000000-000A
    # 2022-07-28T14:22:20.648696 058  I --- 20:257400 20:255710 --:------ 4401 020 10  0B-E99E90F2  00-E99E90F1-D4FF  DA-00000000-000B

    def hex_to_epoch(seqx: str) -> None | dt:  # seconds since 1-1-1970
        if seqx == "00" * 4:
            return None
        return str(
            dt.fromtimestamp(int(seqx, 16))
        )  # - int(payload[22:26], 16) * 15 * 60))

    # 10 7E-E99E90C8 00-E99E90C7-3BFF 7E-E99E90C8-000B
    # hex(int(dt.fromisoformat("2022-07-28T14:21:38.895354").timestamp())).upper()
    # '0x62E20ED2'

    assert payload[:2] == "10"
    assert payload[12:14] == "00"
    assert payload[36:38] == "00"

    assert msg.verb != I_ or payload[24:26] in ("7C", "FF"), payload[24:26]
    assert msg.verb != W_ or payload[24:26] in ("7C", "FF"), payload[24:26]
    assert msg.verb != RQ or payload[24:26] == "00", payload[24:26]

    assert msg.verb != RQ or payload[14:22] == "00" * 4, payload[14:22]
    assert msg.verb != W_ or payload[28:36] != "00" * 4, payload[28:36]

    assert payload[38:40] in ("08", "09", "0A", "0B"), payload[38:40]

    # assert payload[2:4] == payload[26:28], f"{payload[2:4]}, {payload[26:24]}"

    return {
        "last_update_dst": payload[2:4],
        "time_dst": hex_to_epoch(payload[4:12]),
        "_unknown_12": payload[12:14],  # usu.00
        "time_src": hex_to_epoch(payload[14:22]),
        "offset": payload[22:24],  # *15 mins?
        "_unknown_24": payload[24:26],
        "last_update_src": payload[26:28],
        "time_dst_receive_src": hex_to_epoch(payload[28:36]),
        "_unknown_36": payload[36:38],  # usu.00
        "hops_dst_src": payload[38:40],
    }


@parser_decorator  # temperatures (see: 4e02) - Itho spider/autotemp
def parser_4e01(payload, msg) -> dict:
    # .I --- 02:248945 02:250708 --:------ 4E01 018 00-7FFF7FFF7FFF09077FFF7FFF7FFF7FFF-00  # 23.11, 8-group
    # .I --- 02:250984 02:250704 --:------ 4E01 018 00-7FFF7FFF7FFF7FFF08387FFF7FFF7FFF-00  # 21.04

    num_groups = int((msg.len - 2) / 2)  # e.g. (18 - 2) / 2
    assert (
        num_groups * 2 == msg.len - 2
    ), _INFORM_DEV_MSG  # num_groups: len 018 (8-group, 2+8*4), or 026 (12-group, 2+12*4)

    x, y = 0, 2 + num_groups * 4

    assert payload[x : x + 2] == "00", _INFORM_DEV_MSG
    assert payload[y : y + 2] == "00", _INFORM_DEV_MSG

    return {
        "temperatures": [hex_to_temp(payload[i : i + 4]) for i in range(2, y, 4)],
    }


@parser_decorator  # setpoint_bounds (see: 4e01) - Itho spider/autotemp
def parser_4e02(payload, msg) -> dict:  # sent a triplets, 1 min apart
    # .I --- 02:248945 02:250708 --:------ 4E02 034 00-7FFF7FFF7FFF07D07FFF7FFF7FFF7FFF-02-7FFF7FFF7FFF08347FFF7FFF7FFF7FFF  # 20.00-21.00
    # .I --- 02:250984 02:250704 --:------ 4E02 034 00-7FFF7FFF7FFF076C7FFF7FFF7FFF7FFF-02-7FFF7FFF7FFF07D07FFF7FFF7FFF7FFF  #

    num_groups = int((msg.len - 2) / 4)  # e.g. (34 - 2) / 4
    assert (
        num_groups * 4 == msg.len - 2
    ), _INFORM_DEV_MSG  # num_groups: len 034 (8-group, 2+8*4), or 050 (12-group, 2+12*4)

    x, y = 0, 2 + num_groups * 4

    assert payload[x : x + 2] == "00", _INFORM_DEV_MSG  # expect no context
    assert payload[y : y + 2] in ("02", "04"), _INFORM_DEV_MSG  # mode: cool/heat?

    setpoints = [
        (hex_to_temp(payload[x + i :][:4]), hex_to_temp(payload[y + i :][:4]))
        for i in range(2, y, 4)
    ]  # lower, upper setpoints

    return {
        "mode": {"02": "cool", "04": "heat"}[payload[y : y + 2]],
        "setpoint_bounds": [s if s != (None, None) else None for s in setpoints],
    }


@parser_decorator  # hvac_4e04
def parser_4e04(payload, msg) -> dict:
    assert payload[2:4] in ("00", "01", "02")  # off/heat/cool?
    assert int(payload[4:], 16) < 0x3C or payload[4:] in (
        "FB",  # error code?
        "FC",  # error code?
        "FD",  # error code?
        "FE",  # error code?
        "FF",  # N/A?
    )

    return {
        "mode": payload[2:4],
        "_unknown_2": payload[4:],
    }


@parser_decorator  # WIP: AT outdoor low - Itho spider/autotemp
def parser_4e0d(payload, msg) -> dict:
    # .I --- 02:250704 02:250984 --:------ 4E0D 002 0100  # Itho Autotemp: only(?) master -> slave
    # .I --- 02:250704 02:250984 --:------ 4E0D 002 0101  # why does it have a context?

    assert payload in ("0100", "0101"), _INFORM_DEV_MSG

    return {
        "_payload": payload,
    }


@parser_decorator  # AT fault circulation - Itho spider/autotemp
def parser_4e14(payload, msg) -> dict:
    """
    result = "AT fault circulation";
    result = (((payload[2:] & 0x01) != 0x01) ? " Fault state : no fault "                : " Fault state : fault ")
    result = (((payload[2:] & 0x02) != 0x02) ? (text4 + "Circulation state : no fault ") : (text4 + " Circulation state : fault "))
    """
    return {}


@parser_decorator  # wpu_state (hvac state) - Itho spider/autotemp
def parser_4e15(payload, msg) -> dict:
    # .I --- 21:034158 02:250676 --:------ 4E15 002 0000  # WPU "off" (maybe heating, but compressor off)
    # .I --- 21:064743 02:250708 --:------ 4E15 002 0001  # WPU cooling active
    # .I --- 21:057565 02:250677 --:------ 4E15 002 0002  # WPU heating, compressor active
    # .I --- 21:064743 02:250708 --:------ 4E15 002 0004  # WPU in "DHW mode" boiler active
    # .I --- 21:033160 02:250704 --:------ 4E15 002 0005  # 0x03, and 0x06 not seen in the wild

    if int(payload[2:], 16) & 0xF0:
        pass

    # If none of these, then is 'Off'
    SZ_COOLING = "is_cooling"
    SZ_DHW_ING = "is_dhw_ing"
    SZ_HEATING = "is_heating"
    # SZ_PUMPING = "is_pumping"

    assert (
        int(payload[2:], 16) & 0xF8 == 0x00
    ), _INFORM_DEV_MSG  # check for uknown bit flags
    if int(payload[2:], 16) & 0x03 == 0x03:  # is_cooling *and* is_heating (+/- DHW)
        raise TypeError  # TODO: Use local exception & ?Move to higher layer
    assert int(payload[2:], 16) & 0x07 != 0x06, _INFORM_DEV_MSG  # cant heat and DHW

    return {
        "_flags": hex_to_flag8(payload[2:]),
        # SZ_PUMPING: bool(int(payload[2:], 16) & 0x08),
        SZ_DHW_ING: bool(int(payload[2:], 16) & 0x04),
        SZ_HEATING: bool(int(payload[2:], 16) & 0x02),
        SZ_COOLING: bool(int(payload[2:], 16) & 0x01),
    }


@parser_decorator  # TODO: hvac_4e16 - Itho spider/autotemp
def parser_4e16(payload, msg) -> dict:
    # .I --- 02:250984 02:250704 --:------ 4E16 007 00000000000000  # Itho Autotemp: slave -> master

    assert payload == "00000000000000", _INFORM_DEV_MSG

    return {
        "_payload": payload,
    }


@parser_decorator  # TODO: Fan characteristics - Itho
def parser_4e20(payload, msg) -> dict:
    """
    result = "Fan characteristics: "
    result += [C[ABC][210] hex_to_sint32[i:i+4] for i in range(2, 34, 4)]
    """
    return {}


@parser_decorator  # TODO: Potentiometer control - Itho
def parser_4e21(payload, msg) -> dict:
    """
    result = "Potentiometer control: "
    result += "Rel min: "        + hex_to_sint16(data[2:4])  # 16 bit, 2's complement
    result += "Min of rel min: " + hex_to_sint16(data[4:6])
    result += "Abs min: "        + hex_to_sint16(data[6:8])
    result += "Rel max: "        + hex_to_sint16(data[8:10])
    result += "Max rel: "        + hex_to_sint16(data[10:12])
    result += "Abs max: "        + hex_to_sint16(data[12:14]))
    """
    return {}


# @parser_decorator  # faked puzzle pkt shouldn't be decorated
def parser_7fff(payload, msg) -> dict:
    if payload[:2] != "00":
        _LOGGER.debug("Invalid/deprecated Puzzle packet")
        return {
            "msg_type": payload[:2],
            SZ_PAYLOAD: hex_to_str(payload[2:]),
        }

    if payload[2:4] not in LOOKUP_PUZZ:
        _LOGGER.debug("Invalid/deprecated Puzzle packet")
        return {
            "msg_type": payload[2:4],
            "message": hex_to_str(payload[4:]),
        }

    result: dict[str, None | str] = {}
    if payload[2:4] != "13":
        dtm = dt.fromtimestamp(int(payload[4:16], 16) / 1000)  # TZ-naive
        result["datetime"] = dtm.isoformat(timespec="milliseconds")

    msg_type = LOOKUP_PUZZ.get(payload[2:4], SZ_PAYLOAD)

    if payload[2:4] == "11":
        msg = hex_to_str(payload[16:])
        result[msg_type] = f"{msg[:4]}|{msg[4:6]}|{msg[6:]}"

    elif payload[2:4] == "13":
        result[msg_type] = hex_to_str(payload[4:])

    elif payload[2:4] == "7F":
        result[msg_type] = payload[4:]

    else:
        result[msg_type] = hex_to_str(payload[16:])

    return {**result, "parser": f"v{VERSION}"}


@parser_decorator
def parser_unknown(payload, msg) -> dict:
    # TODO: it may be useful to generically search payloads for hex_ids, commands, etc.

    # These are generic parsers
    if msg.len == 2 and payload[:2] == "00":
        return {
            f"_{SZ_PAYLOAD}": payload,
            f"_{SZ_VALUE}": {"00": False, "C8": True}.get(
                payload[2:], int(payload[2:], 16)
            ),
        }

    if msg.len == 3 and payload[:2] == "00":
        return {
            f"_{SZ_PAYLOAD}": payload,
            f"_{SZ_VALUE}": hex_to_temp(payload[2:]),
        }

    raise NotImplementedError


PAYLOAD_PARSERS = {
    k[7:].upper(): v
    for k, v in locals().items()
    if callable(v) and k.startswith("parser_") and len(k) == 11
}
