from __future__ import annotations

from flexmeasures_client import FlexMeasuresClient as Client
import logging
import json

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


import asyncio

import asyncio
from flexmeasures_client import FlexMeasuresClient as Client

EMAIL = "toy-user@flexmeasures.io"
PASSWORD = "toy-password"

client = Client(
    email=EMAIL,
    password=PASSWORD,
    host="localhost:5000",
)


async def create_asset_and_sensors(
    account_id,
    asset: dict,
    sensors: list[dict],
    market_id: int,
):
    asset["account_id"] = account_id
    new_asset = await client.add_asset(**asset)
    new_sensors_list = []

    sensors_to_show = [market_id]
    for sensor in sensors:
        sensor["generic_asset_id"] = new_asset["id"]
        new_sensor = await client.add_sensor(**sensor)
        new_sensors_list.append(new_sensor)
        sensors_to_show.append(new_sensor["id"])

    asset_attributes = {
        "attributes": json.dumps(
            {
                "sensors_to_show": sensors_to_show,
            }
        )
    }

    updated_asset = await client.update_asset(new_asset["id"], asset_attributes)

    return updated_asset, new_sensors_list


account_id = 2
asset = dict(
    name="Test Name Asset14", generic_asset_type_id=5, latitude=51.999, longitude=4.4833
)

market_id = 14
power_sensor = dict(
    name="power",
    event_resolution="PT5M",
    unit="MW",
    timezone="Europe/Amsterdam",
    attributes=json.dumps(
        {
            "capacity_in_mw": 0.0070,
            "max_soc_in_mwh": 0.03,
            "min_soc_in_mwh": 0.010,
            "market_id": market_id,
        }
    ),
)
availability_sensor = dict(
    name="availability", event_resolution="PT5M", unit="%", timezone="Europe/Amsterdam"
)
state_of_charge_sensor = dict(
    name="state of charge",
    event_resolution="PT5M",
    unit="%",
    timezone="Europe/Amsterdam",
)

sensors = [power_sensor, availability_sensor, state_of_charge_sensor]

new_asset, new_sensor_list = await create_asset_and_sensors(
    account_id=account_id,
    asset=asset,
    sensors=sensors,
    market_id=market_id,
)
