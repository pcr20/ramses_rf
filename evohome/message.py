"""Message processor."""

import logging
from typing import Optional

from . import parsers
from .const import (
    __dev_mode__,
    COMMAND_MAP,
    DEVICE_TYPES,
    DOMAIN_MAP,
    MSG_FORMAT_10,
    MSG_FORMAT_18,
    NON_DEV_ID,
)
from .entity import DEVICE_CLASS_MAP, Device, Domain, Zone

_LOGGER = logging.getLogger(__name__)


class Message:
    """The message class."""

    def __init__(self, pkt, gateway) -> None:
        """Create a message, assumes a valid packet."""
        self._gwy = gateway
        self._evo = gateway.evo
        self._pkt = packet = pkt.packet

        self.date = pkt.date
        self.time = pkt.time

        self.rssi = packet[0:3]
        self.verb = packet[4:6]  # -I, RP, RQ, or -W
        self.seq_no = packet[7:10]  # sequence number (as used by 31D9)?

        self.device_id = {}
        for dev, i in enumerate(range(11, 32, 10)):
            self.device_id[dev] = packet[i : i + 9]  # noqa: E203

        if self.device_id[0] != NON_DEV_ID:
            self.device_from = self.device_id[0]
            if self.device_id[1] != NON_DEV_ID:
                self.device_dest = self.device_id[1]
            elif self.device_id[2] == self.device_id[0]:
                self.device_dest = self.device_id[2]
            elif self.device_id[2] != NON_DEV_ID:
                self.device_dest = self.device_id[2]
            else:
                assert False  # TODO: never seen in the wild

        elif self.device_id[1] != NON_DEV_ID:
            assert False  # TODO: never seen in the wild

        elif self.device_id[2] != NON_DEV_ID:
            self.device_from = self.device_id[2]
            self.device_dest = ">uni-cast<"

        else:
            self.device_from = ">nameless<"
            self.device_dest = ">brodcast<"

        self.code = packet[41:45]

        self.payload_length = int(packet[46:49])
        self.raw_payload = packet[50:]
        self._payload = None

        self._repr = None

        self._is_valid = None
        self._is_valid = self.is_valid

    def __repr__(self) -> str:
        """Represent the entity as a string."""

        def display_name(device_id) -> str:
            """Return a friendly device name, of length 10 characters."""
            if device_id.startswith(">"):
                return f"{'':<10}"

            if device_id.startswith("63"):
                return ">null dev<"  # Null device ID

            if device_id in self._gwy.known_devices:
                if self._gwy.known_devices[device_id].get("friendly_name"):
                    return self._gwy.known_devices[device_id]["friendly_name"]

            device_type = DEVICE_TYPES.get(device_id[:2], f"{device_id[:2]:>3}")
            return f"{device_type}:{device_id[3:]}"

        if self._repr:
            return self._repr

        # TODO: the following is a bit dodgy & needs fixing
        if len(self.raw_payload) < 7:
            raw_payload1 = self.raw_payload
            raw_payload2 = ""
        else:
            raw_payload1 = f"{self.raw_payload[:5]}..."[:9]
            raw_payload2 = self.raw_payload
        # raw_payload2 = self.raw_payload if len(self.raw_payload) > 8 else ""

        if self._gwy.config.get("known_devices"):
            msg_format = MSG_FORMAT_18
        else:
            msg_format = MSG_FORMAT_10

        xxx = self._payload if self._payload else raw_payload2  # TODO: a hack
        xxx = {} if self._payload == {} else xxx

        self._repr = msg_format.format(
            display_name(self.device_from),
            display_name(self.device_dest),
            self.verb,
            COMMAND_MAP.get(self.code, f"unknown_{self.code}"),
            raw_payload1,
            xxx,
        )

        return self._repr

    @property
    def payload(self) -> Optional[dict]:
        """Return the payload."""
        return self._payload

    @property
    def is_valid(self) -> bool:  # Main code here
        """Return True if the message payload is valid.

        All exceptions are to be trapped, and logged appropriately.
        """

        if self._is_valid is not None:
            return self._is_valid

        # STATE: update system state (find controller ID)
        if self._evo.ctl_id is None:
            if self.device_from[:2] == "01":
                self._evo.ctl_id = self.device_from
            elif self.device_dest[:2] == "01":
                self._evo.ctl_id = self.device_dest

        # STATE: update system state (how many zones?)
        if self._evo._num_zones is None:
            if self.device_from == self._evo.ctl_id and self._evo._prev_code == "1F09":
                if self.code == "2309" and self.verb == " I":  # TODO: and 3C09 too?
                    assert len(self.raw_payload) % 6 == 0
                    self._evo._num_zones = len(self.raw_payload) / 6
                if self.code == "000A" and self.verb == " I":
                    assert len(self.raw_payload) % 12 == 0
                    self._evo._num_zones = len(self.raw_payload) / 12

        try:  # determine which parser to use
            payload_parser = getattr(parsers, f"parser_{self.code}".lower())
        except AttributeError:  # there's no parser for this command code!
            payload_parser = getattr(parsers, "parser_unknown")

        try:  # run the parser
            self._payload = payload_parser(self.raw_payload, self)  # TODO: messy
            assert self._payload is not None  # should be a dict or a list
        except AssertionError:  # for development only?
            # beware: HGI80 can send parseable but 'odd' packets +/- get invalid reply
            if self.device_from[:2] == "18":  # TODO: should be a warning
                _LOGGER.exception("%s", self._pkt, extra=self.__dict__)
            else:
                _LOGGER.exception("%s", self._pkt, extra=self.__dict__)
            return False

        # STATE: update system state (how many zones?)
        if self.device_from == self._evo.ctl_id:
            self._evo._prev_code = self.code if self.verb == " I" else None

        # for dev_id in self.device_id:  # TODO: leave in, or out?
        #     assert dev_id[:2] in DEVICE_TYPES  # incl. "--", "63"

        # any remaining messages are valid, so: log them
        _LOGGER.info("%s", self, extra=self.__dict__)
        return True  # self._is_valid = True

    def _create_entities(self) -> None:
        """Discover and create new devices, domains and zones."""
        # contains true, programmer's checking asserts, which are OK to -O

        def _ent(ent_cls, ent_id, ent_by_id, ents) -> None:
            try:  # does the system already know about this entity?
                _ = ent_by_id[ent_id]
            except KeyError:  # this is a new entity, so create it
                ent_by_id.update({ent_id: ent_cls(ent_id, self._gwy)})
                ents.append(ent_by_id[ent_id])

        def get_device(dev_id) -> None:
            """Get a Device, create it if required."""
            assert dev_id[:2] in DEVICE_TYPES
            dev_cls = DEVICE_CLASS_MAP.get(dev_id[:2], Device)
            _ent(dev_cls, dev_id, self._evo.device_by_id, self._evo.devices)

        def get_domain(domain_id) -> None:
            """Get a Domain, create it if required."""
            assert domain_id in DOMAIN_MAP
            _ent(Domain, domain_id, self._evo.domain_by_id, self._evo.domains)

        def get_zone(zone_idx) -> None:
            """Get a Zone, create it if required."""  # TODO: other zone types?
            assert int(zone_idx, 16) < 12  # TODO: > 11 not for Hometronic
            zone_cls = Zone  # TODO: DhwZone if zone_idx == "HW" else Zone?
            _ent(zone_cls, zone_idx, self._evo.zone_by_id, self._evo.zones)

        for dev in range(3):  # discover devices
            if self.device_id[dev][:2] not in ["18", "63", "--"]:
                assert self.device_id[dev][:2] in DEVICE_TYPES
                get_device(self.device_id[dev])

        if isinstance(self._payload, dict):  # discover zones and domains
            if self._payload.get("domain_id"):  # is not None:
                get_domain(self._payload["domain_id"])

            if self._payload.get("zone_idx"):  # is not None:  # TODO: parent_zone too?
                get_zone(self._payload["zone_idx"])

        elif isinstance(self._payload, list):  # discover zones
            if self.device_from[:2] == "01" and self.verb == " I":
                if self.code in ["2309", "30C9"]:  # almost all sync cycles
                    for i in range(0, len(self.raw_payload), 6):
                        get_zone(self.raw_payload[i : i + 2])

                elif self.code == "000A":  # the few remaining sync cycles
                    for i in range(0, len(self.raw_payload), 12):
                        get_zone(self.raw_payload[i : i + 2])

    def _update_entities(self) -> None:  # TODO: needs work
        """Update the system state with the message data."""

        def _update_entity(data: dict) -> None:
            if "domain_id" in data:
                self._evo.domain_by_id[data["domain_id"]].update(self)
            elif "zone_idx" in data:
                self._evo.zone_by_id[data["zone_idx"]].update(self)
            else:
                self._evo.device_by_id[self.device_from].update(self)

        # STEP 1: check parent_zone_idx huerestics
        # if __dev_mode__ or self.device_from not in self._gwy.known_devices:
        #     assert self.device_from in self._gwy.known_devices, "dev not in k_d DB"
        #     return

        if __dev_mode__ and isinstance(self.payload, dict):
            if self.device_from in self._gwy.known_devices:
                zone_idx = self._gwy.known_devices[self.device_from].get("zone_idx")
            else:
                zone_idx = None

            if zone_idx:
                key = "parent_zone" if int(zone_idx, 16) < 0x10 else "domain"
                if "parent_zone_idx" in self.payload:
                    # check the zone against the data in known_devices.json
                    assert self.payload["parent_zone_idx"] == zone_idx, "z_idx!= k_d DB"

                if "parent_zone_aaa" in self.payload:
                    assert self.payload[f"{key}_aaa"] == zone_idx
                if "parent_zone_bbb" in self.payload:
                    assert self.payload[f"{key}_bbb"] == zone_idx
                if "parent_zone_ccc" in self.payload:
                    assert self.payload[f"{key}_ccc"] == zone_idx

            if "parent_zone_idx" in self.payload:
                a = "parent_zone_aaa" in self.payload
                b = "parent_zone_bbb" in self.payload
                c = "parent_zone_ccc" in self.payload
                assert any([a, b, c]), "parent_zone_idx, but no _xxx"

        assert self.payload is not None  # TODO: this should have been done before?
        if not self.payload:  # should be {} (possibly empty) or [] (never empty)
            return

        # STEP 2: who was the message from? There's one special (non-evohome) case...
        self._evo.device_by_id[self.device_from].update(self)

        # STEP 3: what was the message about: system, domain, or zone?
        if isinstance(self.payload, list):
            if self.code in ["000A", "2309", "30C9"]:  # array of zones
                # [_update_entity(zone) for zone in self.payload]  # TODO: is bad idea?
                return
            if self.code in ["0009"]:  # array of domains
                [_update_entity(domain) for domain in self.payload]
                return
            if self.code in ["22C9", "3150"]:  # array of UFH zones
                return  # TODO: something
            if self.code in ["1FC9"]:  # TODO: array of codes
                return
            assert False  # should never reach here

        if "zone_idx" in self.payload:
            if self.code == "0418":
                return
            elif self.code == "0008":
                return
            # assert self.code in ["12B0", "2309", "3150"]
            _update_entity(self.payload)

        elif "domain_id" in self.payload:
            _update_entity(self.payload)

        elif self.code in ["1FD4", "22D9", "3220"]:  # is for opentherm...
            _update_entity(self.payload)  # TODO: needs checking

        elif "parent_zone_idx" in self.payload:  # is from/to a device...
            _update_entity(self.payload)  # TODO; do I need this and step 2?

        else:  # is for a device...
            _update_entity(self.payload)
