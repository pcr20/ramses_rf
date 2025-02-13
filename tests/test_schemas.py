#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
"""RAMSES RF - a RAMSES-II protocol decoder & analyser.

Test the Schema processor.
"""

import json
from pathlib import Path

import pytest

from ramses_rf import Gateway
from ramses_rf.helpers import shrink
from ramses_rf.schemas import load_schema
from tests.helpers import gwy  # noqa: F401
from tests.helpers import TEST_DIR, shuffle_dict

WORK_DIR = f"{TEST_DIR}/schemas"


@pytest.mark.parametrize(
    "f_name", [f.stem for f in Path(f"{WORK_DIR}/log_files").glob("*.log")]
)
async def test_schema_discover_from_log(f_name):
    with open(f"{WORK_DIR}/log_files/{f_name}.log") as f:
        gwy = Gateway(None, input_file=f, config={})  # noqa: F811
        gwy.config.disable_sending = True
        await gwy.start()

    with open(f"{WORK_DIR}/log_files/{f_name}.json") as f:
        schema = json.load(f)

        assert shrink(gwy.schema) == shrink(schema)

        gwy.ser_name = "/dev/null"  # HACK: needed to pause engine
        schema, packets = gwy._get_state(include_expired=True)
        packets = shuffle_dict(packets)
        await gwy._set_state(packets)

        assert shrink(gwy.schema) == shrink(schema)

    await gwy.stop()


@pytest.mark.parametrize(
    "f_name", [f.stem for f in Path(f"{WORK_DIR}/jsn_files").glob("*.json")]
)
async def test_schema_load_from_json(gwy, f_name):  # noqa: F811
    with open(f"{WORK_DIR}/jsn_files/{f_name}.json") as f:
        schema = json.load(f)

    load_schema(gwy, **schema)

    # print(json.dumps(schema, indent=4))
    # print(json.dumps(self.gwy.schema, indent=4))

    assert shrink(gwy.schema) == shrink(schema)

    # # HACK: await self.gwy._set_state({})
    # gwy._tcs = None
    # gwy.devices = []
    # gwy.device_by_id = {}
