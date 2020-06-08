"""Evohome serial."""

from datetime import datetime as dt, timedelta
from typing import Optional, Union

from .const import (
    DOMAIN_TYPE_MAP,
    FAULT_DEVICE_CLASS,
    FAULT_STATE,
    FAULT_TYPE,
    SYSTEM_MODE_MAP,
    ZONE_MODE_MAP,
)
from .entity import dev_hex_to_id
from .opentherm import OPENTHERM_MESSAGES, OPENTHERM_MSG_TYPE, ot_msg_value, parity

CODES_WITH_ZONE_IDX = [
    "0004",
    "0008",
    "0009",
    "000C",
    "1030",
    "1060",
    "12B0",
    "2349",
    "3150",
]
# DES_SANS_ZONE_IDX = ["0002", "2E04"]  # not sure about "0016", "22C9"


def parser_decorator(func):
    """Decode the payload (or meta-data) of any message with useful information.

    Also includes some basic payload validation via ASSERTs (e.g payload length).
    """

    def wrapper(*args, **kwargs) -> Optional[dict]:
        """Determine which packets shouldn't be sent through their parser."""

        payload = args[0]
        msg = args[1]

        if msg.verb == " W":  # TODO: WIP
            # these are OK to parse Ws:
            if msg.code in ["0001"]:
                return func(*args, **kwargs)
            if msg.code in ["2309", "2349"] and msg.dev_from[:2] in ["12", "22", "34"]:
                assert int(payload[:2], 16) < 12
                return func(*args, **kwargs)
            # TODO: these are WIP
            if msg.code == "1F09":
                assert payload[:2] == "F8"
                return func(*args, **kwargs)
            if msg.code in ["1FC9"]:
                return func(*args, **kwargs)
            assert payload[:2] in ["00", "FC"]  # ["1100", "2309", "2349"]
            return func(*args, **kwargs)

        if msg.verb != "RQ":  # i.e. in [" I", "RP"]
            result = func(*args, **kwargs)
            if isinstance(result, list):
                return result
            return {**_idx(payload[:2], msg), **result}

        # TRV will RQ zone_name *sans* payload (reveals parent_idx)
        if msg.code == "0004":
            assert msg.len == 2
            return {**_idx(payload[:2], msg)}

        if msg.code == "0005":
            assert len(payload) / 2 == 2
            return {"zone_id": payload[:2]}  # zone_id, not _idx

        if msg.code == "0009":
            # assert len(payload) / 2 == 2  # never, ever got an RP
            return {**_idx(payload[:2], msg)}

        # STA will RQ zone_config, setpoint *sans* payload...
        if msg.code in ["000A", "2309"] and msg.dev_from[:2] == "34":
            assert len(payload) / 2 == 1
            return {**_idx(payload[:2], msg)}

        # THM will RQ zone_config, setpoint *with* a payload...
        if msg.code in ["000A", "2309"] and msg.dev_from[:2] in ["12", "22"]:
            assert len(payload) / 2 == 6 if msg.code == "000A" else 3
            return {**_idx(payload[:2], msg)}

        if msg.code in ["000C", "12B0", "2349"] + ["000A", "2309", "30C9"]:
            assert int(payload[:2], 16) < 12
            assert msg.len < 3  # if msg.code == "0004" else 2
            return {**_idx(payload[:2], msg)}

        if msg.code == "0016" and msg.dev_from[:2] in ["12", "22"]:
            assert len(payload) / 2 == 2
            assert int(payload[:2], 16) < 12
            return {**_idx(payload[:2], msg), **func(*args, **kwargs)}

        if msg.code == "0100":  # 04: will RQ language
            assert len(payload) / 2 in [1, 5]  # len(RQ) = 5, but 00 accepted
            return func(*args, **kwargs)  # no context

        if msg.code == "0404":
            return {**_idx(payload[:2], msg), **func(*args, **kwargs)}

        if msg.code == "0418":
            assert len(payload) / 2 == 3
            assert payload[:4] == "0000"
            assert int(payload[4:6], 16) <= 63
            return {"log_idx": payload[4:6]}

        if msg.code == "10A0" and msg.dev_from[:2] == "07":  # DHW
            return func(*args, **kwargs)

        if msg.code == "1100":
            assert payload[:2] in ["00", "FC"]
            if msg.len > 2:  # these RQs have payloads!
                return func(*args, **kwargs)
            return {**_idx(payload[:2], msg)}

        if msg.code in ["12B0", "2E04"]:  # TODO: check 12B0
            return {}

        if msg.code in ["1F09"]:
            # 061 RQ --- 04:189082 01:145038 --:------ 1F09 001 00
            assert payload in ["00", "99"]
            return {}

        if msg.code == "3220":  # CTL -> OTB (OpenTherm)
            return func(*args, **kwargs)

        if msg.code in ["31D9", "31DA"]:  # ventilation
            # 047 RQ --- 32:168090 30:082155 --:------ 31DA 001 21
            assert msg.len == 1
            return {**_idx(payload[:2], msg)}

        if msg.code == "3EF1":
            assert payload == "0000"
            return {}

        if payload == "00":  # TODO: WIP
            return {}

        assert True or payload in ["FF", "FC"]
        return func(*args, **kwargs)  # All other RQs

    return wrapper


def _idx(seqx, msg) -> dict:
    """Determine if a payload has an index, usually a zone idx or a domain id.

    The challenge is that payloads starting with (e.g.):
    - "00" are often not a zone idx, and
    - "01" *may not* be a zone idx

    Anything in the range F0-FF appears to be a domain id (no false +ve/-ves).
    """
    # STEP 1: identify the index name, if any

    if seqx in DOMAIN_TYPE_MAP:  # no false +ve/-ves
        idx_name = "domain_id"

    elif msg.code == "0418":
        # 0418 has a domain_id/ zone_idx, but it is actually indexed by log_idx
        assert int(seqx, 16) < 64
        return {"log_idx": seqx}

    # new: WIP
    elif msg.code == "0016" and {"12", "22"} & set([d[:2] for d in msg.dev_addr]):
        assert int(seqx, 16) < 12
        idx_name = "zone_idx" if msg.dev_from[:2] == "01" else "parent_idx"

    elif msg.code == "2E04":  # blacklist: never _idx, although some are != "00"
        return {}

    elif msg.code == "22C9" and msg.dev_from[:2] == "02":  # ufh_setpoint
        assert int(seqx, 16) < 8  # this can be a "00"
        idx_name = "ufh_idx"

    elif msg.code in CODES_WITH_ZONE_IDX + ["000A", "2309", "30C9", "0404", "1FC9"]:
        assert int(seqx, 16) < 12  # whitelist: this can be a "00"; can hometronic > 11?
        idx_name = (
            "zone_idx" if msg.dev_from[:2] in ["01", "02", "18"] else "parent_idx"
        )

    elif msg.code in ["31D9", "31DA"]:  # ventilation
        assert seqx in ["00", "21"]
        return {"vent_id": seqx}

    elif not int(seqx, 16) < 12:
        idx_name = "other_id"  # this can be (e.g.) "21"

    else:
        assert seqx == "00"
        return {}

    # STEP 2: determine if there is an index at all
    if msg.dev_from[:2] == "18":  # and msg.verb == "RQ":
        result = {idx_name: seqx}

    elif msg.code in ["1FC9"]:  # exceptions
        # 1FC9 dict is encoded in a way that id/idx not currently used
        result = {}

    elif "01" == msg.dev_from[:2] and msg.dev_from == msg.dev_dest:
        result = {idx_name: seqx}  # either an array, or domain=Fx

    elif "01" in [msg.dev_from[:2], msg.dev_dest[:2]] and msg.dev_from != msg.dev_dest:
        # TODO: is this proof that controller is sensor for zone 0?
        # 060 RP --- 01:145038 18:013393 --:------ 1FC9 012 0010E006368E001FC906368E
        result = {idx_name: seqx}  # a zone / parent_zone?

    elif msg.dev_from[:2] in ["02"]:
        result = {idx_name: seqx}

    # elif msg.dev_from[:2] in ["02", "10", "12", "22"]:
    #     _idx = {"02": "ufh_idx", "10": "otb_idx"}.get(msg.dev_from[:2], "other_id")
    #     result = {_idx: seqx}

    else:
        return {}

    return result


def _bool(seqx) -> Optional[bool]:  # either 00 or C8
    assert seqx in ["00", "C8", "FF"]
    return {"00": False, "C8": True}.get(seqx)


def _dtm(seqx) -> str:
    """Return a local datetime string in isoformat."""
    #        00141B0A07E3  (...HH:MM:00)    for system_mode, zone_mode (schedules?)
    #      0400041C0A07E3  (...HH:MM:SS)    for sync_datetime

    assert len(seqx) in [12, 14]
    if len(seqx) == 12:
        seqx = f"00{seqx}"
    return dt(
        year=int(seqx[10:14], 16),
        month=int(seqx[8:10], 16),
        day=int(seqx[6:8], 16),
        hour=int(seqx[4:6], 16) & 0b11111,  # 1st 3 bits: DayOfWeek
        minute=int(seqx[2:4], 16),
        second=int(seqx[:2], 16) & 0b1111111,  # 1st bit: DST
    ).strftime("%Y-%m-%d %H:%M:%S")


def _date(seqx) -> Optional[str]:
    assert len(seqx) == 8
    if seqx == "FFFFFFFF":
        return None
    return dt(
        year=int(seqx[4:8], 16),
        month=int(seqx[2:4], 16),
        day=int(seqx[:2], 16) & 0b11111,  # 1st 3 bits: DayOfWeek
    ).strftime("%Y-%m-%d")


def _percent(seqx) -> Optional[float]:  # usually a percentage 0-100% (0.0 to 1.0)
    assert len(seqx) == 2
    if seqx == "FF":
        return None

    assert int(seqx, 16) <= 200
    return int(seqx, 16) / 200


def _str(seqx) -> Optional[str]:  # printable
    _string = bytearray([x for x in bytearray.fromhex(seqx) if 31 < x < 127])
    return _string.decode("ascii") if _string else None


def _temp(seqx) -> Optional[float]:
    """Temperatures are two's complement numbers."""
    assert len(seqx) == 4
    if seqx == "7FFF":  # also: FFFF?
        return None
    if seqx == "7EFF":  # TODO: possibly this is only for setpoints?
        return False
    temp = int(seqx, 16)
    return (temp if temp < 2 ** 15 else temp - 2 ** 16) / 100


@parser_decorator
def parser_0001(payload, msg) -> Optional[dict]:  # rf_unknown
    # 15:03:43.184 064  W --- 01:145038 --:------ 01:145038 0001 005 FC00000505
    # 15:13:19.756 053  W --- 01:145038 --:------ 01:145038 0001 005 FF00000505

    # sent by a CTL before a rf_check
    # 15:12:47.769 053  W --- 01:145038 --:------ 01:145038 0001 005 FC00000505
    # 15:12:47.869 053 RQ --- 01:145038 13:237335 --:------ 0016 002 00FF
    # 15:12:47.880 053 RP --- 13:237335 01:145038 --:------ 0016 002 0017

    # sent by a THM every 5s when is signal strength test mode (0505, except 1st pkt)
    # 13:48:38.518 080  W --- 12:010740 --:------ 12:010740 0001 005 0000000501
    # 13:48:45.518 074  W --- 12:010740 --:------ 12:010740 0001 005 0000000505
    # 13:48:50.518 077  W --- 12:010740 --:------ 12:010740 0001 005 0000000505

    # sent by a HGI80 whenever its button is pressed
    # 00:22:41.540 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF02FF
    # 00:22:41.757 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF0200
    # 00:22:43.320 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF02FF
    # 00:22:43.415 ---  I --- --:------ --:------ --:------ 0001 005 00FFFF0200

    assert msg.verb in [" I", " W"]
    assert len(payload) / 2 == 5
    assert payload[:2] in ["00", "FC", "FF"]
    assert payload[2:6] in ["0000", "FFFF"]
    assert payload[6:8] in ["02", "05"]
    return {
        "other_id": payload[:2],
        "unknown_0": payload[2:6],
        "unknown_1": payload[6:8],
        "unknown_3": payload[8:],
    }


@parser_decorator
def parser_0002(payload, msg) -> Optional[dict]:  # sensor_weather
    assert len(payload) / 2 == 4

    return {"temperature": _temp(payload[2:6]), "unknown_0": payload[6:]}


@parser_decorator
def parser_0004(payload, msg) -> Optional[dict]:  # zone_name
    # RQ payload is zz00; limited to 12 chars in evohome UI? if "7F"*20: not a zone

    assert len(payload) / 2 == 22
    assert int(payload[:2], 16) < 12
    assert payload[2:4] == "00"

    # return {"name": _str(payload[4:])}
    return {**_idx(payload[:2], msg), "name": _str(payload[4:])}


@parser_decorator
def parser_0005(payload, msg) -> Optional[dict]:  # system_zone (add/del a zone?)
    # RQ payload is xx00, controller wont respond to a xx

    assert msg.verb in [" I", "RP"]
    if msg.dev_from[:2] == "34":
        assert len(payload) / 2 == 12  # or % 4?

    else:
        assert msg.dev_from[:2] == "01"
        assert len(payload) / 2 == 4
        assert payload[2:4] in ["00", "0D", "0F"]  # TODO: 00=Radiator, 0D=Electric?

    return {"device_id": msg.dev_from, "payload": payload}


@parser_decorator
def parser_0006(payload, msg) -> Optional[dict]:  # schedule_sync (any changes?)
    assert len(payload) / 2 == 4
    assert payload[2:] in ["050000", "FFFFFF"]

    return {"payload": payload}


@parser_decorator
def parser_0008(payload, msg) -> Optional[dict]:  # relay_demand (domain/zone/device)
    # https://www.domoticaforum.eu/viewtopic.php?f=7&t=5806&start=105#p73681
    # e.g. Electric Heat Zone
    assert len(payload) / 2 == 2

    if payload[:2] not in ["F9", "FA", "FC"]:
        assert int(payload[:2], 16) < 12  # TODO: when 0, when FC, when zone

    return {**_idx(payload[:2], msg), "relay_demand": _percent(payload[2:])}


@parser_decorator
def parser_0009(payload, msg) -> None:  # list or dict:  # relay_failsafe
    # seems there can only be max one relay per domain/zone
    # can get: 003 or 006: FC01FF-F901FF or FC00FF-F900FF
    def _parser(seqx) -> dict:
        assert seqx[:2] in ["F9", "FC"] or int(seqx[:2], 16) < 12
        assert seqx[2:4] in ["00", "01"]
        assert seqx[4:] in ["00", "FF"]

        return {
            **_idx(seqx[:2], msg),
            "failsafe_enabled": {"00": False, "01": True}.get(seqx[2:4]),
        }

    if msg.is_array:
        assert msg.len >= 3 and msg.len % 3 == 0  # assuming not RQ
        return [_parser(payload[i : i + 6]) for i in range(0, len(payload), 6)]

    assert msg.len == 3
    return _parser(payload)


@parser_decorator
def parser_000a(payload, msg) -> Union[dict, list, None]:  # zone_config (zone/s)
    def _parser(seqx) -> dict:
        assert int(seqx[:2], 16) < 12
        # if seqx[2:] == "007FFF7FFF":  # a null zone

        bitmap = int(seqx[2:4], 16)
        return {
            **_idx(seqx[:2], msg),
            "min_temp": _temp(seqx[4:8]),
            "max_temp": _temp(seqx[8:]),
            "local_override": not bool(bitmap & 1),
            "openwindow_function": not bool(bitmap & 2),
            "multiroom_mode": not bool(bitmap & 16),
            "unknown_bitmap": f"0b{bitmap:08b}",
        }  # cannot determine zone_type from this information

    if msg.is_array:  # TODO: these msgs can require 2 pkts!
        assert msg.len >= 6 and msg.len % 6 == 0  # assuming not RQ
        return [_parser(payload[i : i + 12]) for i in range(0, len(payload), 12)]

    assert msg.len == 6
    return _parser(payload)


@parser_decorator
def parser_000c(payload, msg) -> Optional[dict]:  # zone_actuators (not sensors)
    # RQ payload is zz00, NOTE: aggregation of parsing taken here
    def _parser(seqx) -> dict:
        assert seqx[:2] == payload[:2]
        # assert seqx[2:4] in ["00", "0A", "0F", "10"] # usus. 00 - subzone?
        assert seqx[4:6] in ["00", "7F"]  # TODO: what does 7F means

        return {dev_hex_to_id(seqx[6:12]): seqx[4:6]}

    assert msg.len >= 6 and msg.len % 6 == 0  # assuming not RQ
    devices = [_parser(payload[i : i + 12]) for i in range(0, len(payload), 12)]

    return {
        **_idx(payload[:2], msg),
        "actuators": [k for d in devices for k, v in d.items() if v != "7F"],
    }


@parser_decorator
def parser_000e(payload, msg) -> Optional[dict]:  # unknown
    assert payload == "000014"  # rarely, from STA:xxxxxx
    return {"unknown_0": payload}


@parser_decorator
def parser_0016(payload, msg) -> Optional[dict]:  # rf_check
    # TODO: some RQs also contain a payload with data, zz00?
    # 046 RQ --- 22:060293 01:078710 --:------ 0016 002 0200
    # 064 RP --- 01:078710 22:060293 --:------ 0016 002 021E

    assert msg.verb in ["RQ", "RP"]
    assert len(payload) / 2 == 2  # for both RQ/RP, but RQ/00 will work
    # assert payload[:2] == "00"  # e.g. RQ/22:/0z00 (parent_zone), but RQ/07:/0000?

    if msg.verb == "RQ":
        return {}  # {"rf_request": msg.dev_dest}

    rf_value = int(payload[2:4], 16)
    return {
        #  "rf_source": msg.dev_dest,
        "rf_strength": min(int(rf_value / 5) + 1, 5),
        "rf_value": rf_value,
    }


@parser_decorator
def parser_0100(payload, msg) -> Optional[dict]:  # language (of device/system)
    if msg.verb == "RQ" and payload == "00":  # HACK: should be "00ssssFFFF"
        return {}

    assert len(payload) / 2 == 5
    assert payload[:2] == "00"
    assert payload[6:] == "FFFF"
    return {"language": _str(payload[2:6]), "unknown_0": payload[6:]}


@parser_decorator
def parser_0404(payload, msg) -> Optional[dict]:  # schedule - TODO
    def _header(seqx) -> dict:
        assert int(seqx[:2], 16) < 12
        assert seqx[2:8] == "200008"

        return {
            "frag_index": int(seqx[10:12], 16),
            "frag_total": int(seqx[12:], 16),
            "frag_length": int(seqx[8:10], 16),
        }

    assert msg.verb in ["RQ", "RP"]
    if msg.verb == "RQ":
        assert msg.len == 7
        return _header(payload[:14])

    if msg.verb == "RP":
        return {**_header(payload[:14]), "fragment": payload[14:]}


@parser_decorator
def parser_0418(payload, msg) -> Optional[dict]:  # system_fault
    """10 * 6 log entries in the UI, but 63 via RQs."""

    def _timestamp(seqx):
        """In the controller UI: YYYY-MM-DD HH:MM."""
        _seqx = int(seqx, 16)
        return dt(
            year=(_seqx & 0b1111111 << 24) >> 24,
            month=(_seqx & 0b1111 << 36) >> 36,
            day=(_seqx & 0b11111 << 31) >> 31,
            hour=(_seqx & 0b11111 << 19) >> 19,
            minute=(_seqx & 0b111111 << 13) >> 13,
            second=(_seqx & 0b111111 << 7) >> 7,
        ).strftime("%Y-%m-%d %H:%M:%S")

    #
    if payload == "000000B0000000000000000000007FFFFF7000000000":
        # a null log entry, (or: even payload[38:] == "000000")
        return {"log_idx": payload[4:6]}
    #
    if msg:
        assert msg.verb in [" I", "RP"]
    assert len(payload) / 2 == 22
    #
    assert payload[:2] == "00"  # unknown_0
    assert payload[2:4] in list(FAULT_STATE)  # C0 dont appear in the UI?
    assert int(payload[4:6], 16) <= 63  # TODO: upper limit is: 60? 63? more?
    assert payload[6:8] == "B0"  # unknown_1, ?priority
    assert payload[8:10] in list(FAULT_TYPE)
    assert int(payload[10:12], 16) < 12 or payload[10:12] in ["FA", "FC"]  # ?FB, etc.
    assert payload[12:14] in list(FAULT_DEVICE_CLASS)
    assert payload[14:18] == "0000"  # unknown_2
    assert payload[28:30] in ["7F", "FF"]  # last bit in dt field
    assert payload[30:38] == "FFFF7000"  # unknown_3
    #
    return {
        **_idx(payload[4:6], msg),  # "log_idx": ...
        "timestamp": _timestamp(payload[18:30]),
        "fault_state": FAULT_STATE.get(payload[2:4], payload[2:4]),
        "fault_type": FAULT_TYPE.get(payload[8:10], payload[8:10]),
        "zone_idx" if int(payload[10:12], 16) < 12 else "domain_id": payload[10:12],
        "device_class": FAULT_DEVICE_CLASS.get(payload[12:14], payload[12:14]),
        "device_id": dev_hex_to_id(payload[38:]),  # is "00:000001/2 for CTL?
    }


@parser_decorator
def parser_042f(payload, msg) -> Optional[dict]:  # unknown - WIP
    # 055  I --- 34:064023 --:------ 34:064023 042F 008 00000000230023F5
    # 063  I --- 34:064023 --:------ 34:064023 042F 008 00000000240024F5
    # 049  I --- 34:064023 --:------ 34:064023 042F 008 00000000250025F5
    # 045  I --- 34:064023 --:------ 34:064023 042F 008 00000000260026F5
    # 045  I --- 34:092243 --:------ 34:092243 042F 008 0000010021002201
    # 000  I     34:011469 --:------ 34:011469 042F 008 00000100030004BC

    assert len(payload) / 2 in [8, 9]  # non-evohome are 9
    assert payload[:2] == "00"

    return {
        "counter_1": int(payload[2:6], 16),
        "counter_2": int(payload[6:10], 16),
        "counter_total": int(payload[10:14], 16),
        "unknown_0": payload[14:],
    }


@parser_decorator
def parser_1030(payload, msg) -> Optional[dict]:  # mixvalve_config (zone)
    def _parser(seqx) -> dict:
        assert seqx[2:4] == "01"

        param_name = {
            "C8": "max_flow_temp",
            "C9": "pump_rum_time",
            "CA": "actuator_run_time",
            "CB": "min_flow_temp",
            "CC": "unknown_0",  # ?boolean?
        }[seqx[:2]]

        return {param_name: int(seqx[4:], 16)}

    assert len(payload) / 2 == 1 + 5 * 3
    assert int(payload[:2], 16) < 12
    assert payload[30:] == "01"

    params = [_parser(payload[i : i + 6]) for i in range(2, len(payload), 6)]
    return {**_idx(payload[:2], msg), **{k: v for x in params for k, v in x.items()}}


@parser_decorator
def parser_1060(payload, msg) -> Optional[dict]:  # device_battery (battery_state)
    """Return the battery state.

    Some devices (04:) will also report battery level.
    """
    # 06:48:23.948 049  I --- 12:010740 --:------ 12:010740 1060 003 00FF01
    # 16:18:43.515 051  I --- 12:010740 --:------ 12:010740 1060 003 00FF00
    # 16:14:44.180 054  I --- 04:056057 --:------ 04:056057 1060 003 002800
    # 17:34:35.460 087  I --- 04:189076 --:------ 01:145038 1060 003 026401

    assert len(payload) / 2 == 3
    assert payload[4:6] in ["00", "01"]

    return {"low_battery": payload[4:] == "00", "battery_level": _percent(payload[2:4])}


@parser_decorator
def parser_10a0(payload, msg) -> Optional[dict]:  # dhw_params
    # DHW sends a RQ (not an I) with payload!
    assert len(payload) / 2 == 6
    assert payload[:2] == "00"  # all DHW pkts have no domain

    return {
        "setpoint": _temp(payload[2:6]),  # 30.0-85.0 C
        "overrun": int(payload[6:8], 16),  # 0-10 minutes
        "differential": _temp(payload[8:12]),  # 1.0-10.0 C
    }


@parser_decorator
def parser_10e0(payload, msg) -> Optional[dict]:  # device_info
    assert len(payload) / 2 in [30, 36, 38]  # a non-evohome seen with 30

    return {  # TODO: add version?
        "description": _str(payload[36:]),
        "firmware": _date(payload[20:28]),  # could be 'FFFFFFFF'
        "manufactured": _date(payload[28:36]),
        "unknown": payload[:20],
    }


@parser_decorator
def parser_1100(payload, msg) -> Optional[dict]:  # tpi_params (domain/zone/device)
    assert len(payload) / 2 in [5, 8]
    assert payload[:2] in ["00", "FC"]
    assert payload[2:4] in ["0C", "18", "24", "30"]
    assert payload[4:6] in ["04", "08", "0C", "10", "14"]
    assert payload[6:8] in ["00", "04", "08", "0C", "10", "14"]
    assert payload[8:10] in ["00", "FF"]

    def _parser(seqx) -> dict:
        return {
            **_idx(seqx[:2], msg),
            "cycle_rate": int(payload[2:4], 16) / 4,  # in cycles/hour
            "minimum_on_time": int(payload[4:6], 16) / 4,  # in minutes
            "minimum_off_time": int(payload[6:8], 16) / 4,  # in minutes
            "unknown_0": payload[8:10],  # always 00, FF?
        }

    if len(payload) / 2 == 5:
        return _parser(payload)

    assert payload[14:] == "01"
    return {
        **_parser(payload[:10]),
        "proportional_band_width": _temp(payload[10:14]),  # in degrees C
        "unknown_1": payload[14:],  # always 01?
    }


@parser_decorator
def parser_1260(payload, msg) -> Optional[dict]:  # dhw_temp
    assert len(payload) / 2 == 3
    assert payload[:2] == "00"  # all DHW pkts have no domain

    return {"temperature": _temp(payload[2:])}


@parser_decorator
def parser_1290(payload, msg) -> Optional[dict]:  # outdoor_temp
    # evohome responds to an RQ
    assert len(payload) / 2 == 3
    assert payload[:2] == "00"  # no domain

    return {"temperature": _temp(payload[2:])}


@parser_decorator
def parser_12a0(payload, msg) -> Optional[dict]:  # indoor_humidity (Nuaire RH sensor)
    assert len(payload) / 2 == 6
    assert payload[:2] == "00"  # domain?

    return {
        "relative_humidity": int(payload[2:4], 16) / 100,  # is not /200
        "temperature": _temp(payload[4:8]),
        "dewpoint_temp": _temp(payload[8:12]),
    }


@parser_decorator
def parser_12b0(payload, msg) -> Optional[dict]:  # window_state (of a device/zone)
    assert int(payload[:2], 16) < 12  # also for device state
    assert payload[2:] in ["0000", "C800", "FFFF"]  # "FFFF" means N/A

    # TODO: zone.open_window = any(TRV.open_windows)?
    return {
        **_idx(payload[:2], msg),
        "window_open": _bool(payload[2:4]),
        "unknown_0": payload[4:],
    }


@parser_decorator
def parser_1f09(payload, msg) -> Optional[dict]:  # sync_cycle
    # TODO: Try RQ/1F09/"F8-FF" (CTL will RP to a RQ/00)
    assert len(payload) / 2 == 3
    assert payload[:2] in ["00", "F8", "FF"]  # W uses F8, non-Honeywell devices use 00

    seconds = int(payload[2:6], 16) / 10
    next_sync = msg.dtm + timedelta(seconds=seconds)

    return {
        "remaining_seconds": seconds,
        "_next_sync": dt.strftime(next_sync, "%H:%M:%S"),
    }


@parser_decorator
def parser_1f41(payload, msg) -> Optional[dict]:  # dhw_mode
    assert len(payload) / 2 in [6, 12]
    assert payload[:2] == "00"  # all DHW pkts have no domain

    # 053 RP --- 01:145038 18:013393 --:------ 1F41 006 00FF00FFFFFF  # no stored DHW
    assert payload[2:4] in ["00", "01", "FF"]
    assert payload[4:6] in list(ZONE_MODE_MAP)
    if payload[4:6] == "04":
        assert len(payload) / 2 == 12
        assert payload[6:12] == "FFFFFF"

    return {
        "active": {"00": False, "01": True, "FF": None}[payload[2:4]],
        "mode": ZONE_MODE_MAP.get(payload[4:6]),
        "until": _dtm(payload[12:24]) if payload[4:6] == "04" else None,
    }


@parser_decorator
def parser_1fc9(payload, msg) -> Optional[dict]:  # bind_device
    # this is an array of codes
    # 049  I --- 01:145038 --:------ 01:145038 1FC9 018 07-000806368E FC-3B0006368E               07-1FC906368E  # noqa: E501
    # 047  I --- 01:145038 --:------ 01:145038 1FC9 018 FA-000806368E FC-3B0006368E               FA-1FC906368E  # noqa: E501
    # 065  I --- 01:145038 --:------ 01:145038 1FC9 024 FC-000806368E FC-315006368E FB-315006368E FC-1FC906368E  # noqa: E501

    def _parser(seqx) -> dict:
        assert seqx[6:] == payload[6:12]
        if seqx[:2] not in ["FA", "FB", "FC"]:  # or: not in DOMAIN_TYPE_MAP: ??
            assert int(seqx[:2], 16) < 12
        return {seqx[:2]: seqx[2:6]}  # NOTE: codes is many:many (domain:code)

    if msg.verb == " W":  # TODO: just leave an an array?
        assert msg.len == 6
        assert msg.dev_from == dev_hex_to_id(payload[6:12])
        return _parser(payload)

    assert msg.verb in [" I", " W", "RP"]  # devices will respond to a RQ!
    assert msg.len >= 6 and msg.len % 6 == 0  # assuming not RQ
    assert msg.dev_from == dev_hex_to_id(payload[6:12])
    return [_parser(payload[i : i + 12]) for i in range(0, len(payload), 12)]


@parser_decorator
def parser_1fd4(payload, msg) -> Optional[dict]:  # opentherm_sync
    assert msg.verb in " I"
    assert len(payload) / 2 == 3
    assert payload[:2] == "00"

    return {"ticker": int(payload[2:], 16)}


@parser_decorator
def parser_22c9(payload, msg) -> Optional[dict]:  # ufh_setpoint, TODO: max length = 24?
    def _parser(seqx) -> dict:
        assert int(seqx[:2], 16) < 12
        assert seqx[10:] == "01"

        return {
            **_idx(seqx[:2], msg),
            "temp_low": _temp(seqx[2:6]),
            "temp_high": _temp(seqx[6:10]),
            "unknown_0": seqx[10:],
        }

    assert len(payload) % 12 == 0
    return [_parser(payload[i : i + 12]) for i in range(0, len(payload), 12)]


@parser_decorator
def parser_22d0(payload, msg) -> Optional[dict]:  # message_22d0
    assert payload == "00000002"

    return {"unknown": payload}


@parser_decorator
def parser_22d9(payload, msg) -> Optional[dict]:  # boiler_setpoint
    assert len(payload) / 2 == 3
    assert payload[:2] == "00"

    return {"boiler_setpoint": _temp(payload[2:6])}


@parser_decorator
def parser_22f1(payload, msg) -> Optional[dict]:  # ???? (Nuaire 4-way switch)
    assert len(payload) / 2 == 3
    assert payload[:2] == "00"  # has no domain
    assert payload[4:6] == "0A"

    bitmap = int(payload[2:4], 16)

    _bitmap = {"_bitmap": bitmap}

    if bitmap in [2, 3]:
        _action = {"fan_mode": "normal" if bitmap == 2 else "boost"}
    elif bitmap in [9, 10]:
        _action = {"heater_mode": "auto" if bitmap == 10 else "off"}
    else:
        _action = {}

    return {**_action, **_bitmap}


@parser_decorator
def parser_2309(payload, msg) -> Union[dict, list, None]:  # setpoint (of device/zones)
    def _parser(seqx) -> dict:
        assert int(seqx[:2], 16) < 12
        # if seqx[2:] == "FFFF":  # ???

        return {**_idx(seqx[:2], msg), "setpoint": _temp(seqx[2:])}

    # 055 RQ --- 12:010740 13:163733 --:------ 2309 003 0007D0
    # 046 RQ --- 12:010740 01:145038 --:------ 2309 003 03073A

    assert msg.verb in [" I", "RP", " W"]

    if msg.is_array:
        assert msg.len >= 3 and msg.len % 3 == 0  # assuming not RQ
        return [_parser(payload[i : i + 6]) for i in range(0, len(payload), 6)]

    assert msg.len == 3
    return _parser(payload)


@parser_decorator
def parser_2349(payload, msg) -> Optional[dict]:  # zone_mode
    assert msg.verb in [" I", "RP", " W"]
    assert len(payload) / 2 in [7, 13]  # has a dtm if mode == "04"
    assert payload[6:8] in list(ZONE_MODE_MAP)
    assert payload[8:14] == "FFFFFF"

    return {
        **_idx(payload[:2], msg),
        "setpoint": _temp(payload[2:6]),
        "mode": ZONE_MODE_MAP.get(payload[6:8]),
        "until": _dtm(payload[14:26]) if payload[6:8] == "04" else None,
    }


@parser_decorator
def parser_2e04(payload, msg) -> Optional[dict]:  # system_mode
    # if msg.verb == " W":
    # RQ/2E04/FF

    assert len(payload) / 2 == 8
    assert payload[:2] in list(SYSTEM_MODE_MAP)  # TODO: check AutoWithReset

    return {
        "mode": SYSTEM_MODE_MAP.get(payload[:2]),
        "until": _dtm(payload[2:14]) if payload[14:] != "00" else None,
    }


@parser_decorator
def parser_30c9(payload, msg) -> Optional[dict]:  # temp (of device, zone/s)
    def _parser(seqx) -> dict:
        assert int(seqx[:2], 16) < 12
        # if seqx[2:] == "FFFF":

        return {**_idx(seqx[:2], msg), "temperature": _temp(seqx[2:])}

    if msg.is_array:
        assert msg.len >= 3 and msg.len % 3 == 0  # assuming not RQ
        return [_parser(payload[i : i + 6]) for i in range(0, len(payload), 6)]

    assert msg.len == 3
    return _parser(payload)


@parser_decorator
def parser_3120(payload, msg) -> Optional[dict]:  # unknown - WIP
    # sent by STAs every ~3:45:00, why?
    assert msg.dev_from[:3] == "34:"
    assert payload == "0070B0000000FF"
    return {"unknown_3120": payload}


@parser_decorator
def parser_313f(payload, msg) -> Optional[dict]:  # sync_datetime
    # 2020-03-28T03:59:21.315178 045 RP --- 01:158182 04:136513 --:------ 313F 009 00FC3500A41C0307E4  # noqa: E501
    # 2020-03-29T04:58:30.486343 045 RP --- 01:158182 04:136485 --:------ 313F 009 00FC8400C51D0307E4  # noqa: E501
    # 2020-05-31T11:37:50.351511 056  I --- --:------ --:------ 12:207082 313F 009 0038021ECB1F0507E4  # noqa: E501

    # https://www.automatedhome.co.uk/vbulletin/showthread.php?5085-My-HGI80-equivalent-Domoticz-setup-without-HGI80&p=36422&viewfull=1#post36422
    # every day at ~4am TRV/RQ->CTL/RP, approx 5-10secs apart (CTL respond at any time)
    assert len(payload) / 2 == 9
    assert payload[:2] == "00"  # evohome is always "00FC"?
    return {
        "datetime": _dtm(payload[4:18]),
        "is_dst": True if bool(int(payload[4:6], 16) & 0x80) else None,
        "unknown_0": payload[2:4],
    }


@parser_decorator
def parser_3150(payload, msg) -> Optional[dict]:  # heat_demand (of device, FC domain)
    # event-driven, and periodically; FC domain is highest of all TRVs
    # TODO: all have a valid domain will UFH/CTL respond to an RQ, for FC, for a zone?

    def _parser(seqx) -> dict:
        assert seqx[:2] == "FC" or (int(seqx[:2], 16) < 12)  # <5, 8 for UFH
        return {**_idx(seqx[:2], msg), "heat_demand": _percent(seqx[2:])}

    if msg.dev_from[:2] == "02" and msg.is_array:  # TODO: hometronics only?
        return [_parser(payload[i : i + 4]) for i in range(0, len(payload), 4)]

    assert msg.len == 2  # msg.dev_from[:2] in ["01","02","10","04"]
    return _parser(payload)  # TODO: check UFH/FC is == CTL/FC


@parser_decorator
def parser_31d9(payload, msg) -> Optional[dict]:
    assert len(payload) / 2 == 17  # usu: I 30:-->30:, with a seq#!
    assert payload[:2] in ["00", "21"]
    assert payload[2:4] in ["00", "06"]
    assert payload[6:8] == "00"
    assert payload[8:32] in ["000000000000000000000000", "202020202020202020202020"]

    return {
        **_idx(payload[:2], msg),
        "percent_1": _percent(payload[4:6]),
        "unknown_0": payload[2:4],
        "unknown_2": payload[6:8],
        "unknown_3": payload[8:32],
        "unknown_4": payload[32:],
    }


@parser_decorator
def parser_31da(payload, msg) -> Optional[dict]:  # UFH HCE80 (Nuaire humidity)
    assert len(payload) / 2 == 29  # usu: I CTL-->CTL

    assert payload[2:10] == "EF007FFF"
    assert payload[12:30] == "EF7FFF7FFF7FFF7FFF"
    assert payload[34:36] == "EF"
    assert payload[42:44] == "00"
    assert payload[46:48] in ["00", "EF"]
    assert payload[48:] in ["EF7FFF7FFF", "EF7FFFFFFF"]

    rh = int(payload[10:12], 16) / 100 if payload[10:12] != "EF" else None  # not /200!

    return {
        **_idx(payload[:2], msg),
        "relative_humidity": rh,
        "unknown_1": payload[30:32],
        "unknown_2": payload[32:34],
        "unknown_3": payload[36:38],
        "unknown_4": payload[38:40],
        "unknown_5": payload[44:46],
    }


@parser_decorator
def parser_31e0(payload, msg) -> Optional[dict]:  # ???? (Nuaire on/off)
    # cat pkts.log | grep 31DA | grep -v ' I ' (event-driven ex 168090, humidity sensor)
    # 11:09:49.973 045  I --- VNT:168090 GWY:082155  --:------ 31E0 004 00 00 00 00
    # 11:14:46.168 045  I --- VNT:168090 GWY:082155  --:------ 31E0 004 00 00 C8 00
    # TODO: track humidity against 00/C8, OR HEATER?

    assert len(payload) / 2 == 4  # usu: I VNT->GWY
    assert payload[:4] == "0000"  # domain?
    assert payload[4:] in ["0000", "C800"]

    return {
        "state_31e0": _bool(payload[4:6]),
        "unknown_0": payload[:4],
        "unknown_1": payload[6:],
    }


@parser_decorator
def parser_3220(payload, msg) -> Optional[dict]:  # opentherm_msg
    assert len(payload) / 2 == 5
    assert payload[:2] == "00"

    # these are OpenTherm-specific assertions
    assert int(payload[2:4], 16) // 0x80 == parity(int(payload[2:], 16) & 0x7FFFFFFF)

    ot_msg_type = int(payload[2:4], 16) & 0x70
    assert ot_msg_type in OPENTHERM_MSG_TYPE

    assert int(payload[2:4], 16) & 0x0F == 0

    ot_msg_id = int(payload[4:6], 16)
    assert str(ot_msg_id) in OPENTHERM_MESSAGES["messages"]

    message = OPENTHERM_MESSAGES["messages"].get(str(ot_msg_id))

    result = {"id": ot_msg_id, "msg_type": OPENTHERM_MSG_TYPE[ot_msg_type]}

    if not message:
        return {**result, "value_raw": payload[6:]}

    if msg.verb == "RQ":
        assert ot_msg_type < 48
        assert payload[6:10] == "0000"
        return {
            **result,
            # "description": message["en"]
        }

    assert ot_msg_type > 48

    if isinstance(message["var"], dict):
        if isinstance(message["val"], dict):
            result["value_hb"] = ot_msg_value(
                payload[6:8], message["val"].get("hb", message["val"])
            )
            result["value_lb"] = ot_msg_value(
                payload[8:10], message["val"].get("lb", message["val"])
            )
        else:
            result["value_hb"] = ot_msg_value(payload[6:8], message["val"])
            result["value_lb"] = ot_msg_value(payload[8:10], message["val"])

    else:
        if message["val"] in ["flag8", "u8", "s8"]:
            result["value"] = ot_msg_value(payload[6:8], message["val"])
        else:
            result["value"] = ot_msg_value(payload[6:10], message["val"])

    return {
        **result,
        # "description": message["en"],
    }


@parser_decorator
def parser_3b00(payload, msg) -> Optional[dict]:  # sync_tpi (TPI cycle HB/sync)
    # https://www.domoticaforum.eu/viewtopic.php?f=7&t=5806&start=105#p73681
    # TODO: alter #cycles/hour & check interval between 3B00/3EF0 changes
    """Decode a 3B00 packet (sync_tpi).

    The boiler relay regularly broadcasts a 3B00 at the start (or the end?) of every TPI
    cycle, the frequency of which is determined by the (TPI) cycle rate in 1100.

    The CTL subsequently broadcasts a 3B00 (i.e. at the start of every TPI cycle).

    The OTB does not send these packets, but the CTL sends a regular broadcasty
     anyway.
    """

    # 053  I --- 13:209679 --:------ 13:209679 3B00 002 00C8
    # 045  I --- 01:158182 --:------ 01:158182 3B00 002 FCC8
    # 052  I --- 13:209679 --:------ 13:209679 3B00 002 00C8
    # 045  I --- 01:158182 --:------ 01:158182 3B00 002 FCC8

    # 063  I --- 01:078710 --:------ 01:078710 3B00 002 FCC8
    # 064  I --- 01:078710 --:------ 01:078710 3B00 002 FCC8

    assert len(payload) / 2 == 2
    assert payload[:2] in {"01": "FC", "13": "00"}.get(msg.dev_from[:2])
    assert payload[2:] == "C8"  # Could it be a percentage?

    return {**_idx(payload[:2], msg), "sync_tpi": _bool(payload[2:])}


@parser_decorator
def parser_3ef0(payload, msg) -> dict:  # actuator_enabled (state)
    if msg.dev_from[:2] == "10":  # OTB
        assert len(payload) / 2 == 6
        assert payload[4:6] in ["10", "11"]
    else:
        assert len(payload) / 2 == 3

    assert payload[:2] == "00"
    assert payload[-2:] == "FF"

    if msg.dev_from[:2] == "10":
        return {
            **_idx(payload[:2], msg),
            "modulation_level": int(payload[2:4], 16) / 100,  # should be /200?
            "flame_active": {"0A": True}.get(payload[2:4], False),
            "flame_status": payload[2:4],
        }

    return {**_idx(payload[:2], msg), "actuator_enabled": _bool(payload[2:4])}


@parser_decorator
def parser_3ef1(payload, msg) -> Optional[dict]:  # actuator_state
    assert msg.verb == "RP"
    assert len(payload) / 2 == 7
    assert payload[:2] == "00"
    assert payload[10:] in ["00FF", "C8FF"]
    assert payload[-2:] == "FF"

    return {
        **_idx(payload[:2], msg),
        "actuator_state": _percent(payload[2:4]),
        "unknown_1": int(payload[4:6], 16),  # modulation level? /200?
        "unknown_2": int(payload[6:10], 16),  # /200?
        "unknown_3": _bool(payload[10:12]),
    }


@parser_decorator
def parser_unknown(payload, msg) -> Optional[dict]:
    # TODO: it may be useful to search payloads for hex_ids, commands, etc.
    if msg.code in ["01D0", "01E9"]:  # HR91s
        return {"unknown": payload}
    raise NotImplementedError
