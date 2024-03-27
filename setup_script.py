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

MARKET_ID = 14
# Eg. Tibber = 14, ANWB = 15, etc. as administered in FlexMeasures
ASSET_NAME = "Asset of toy-user"
LATITUDE = 51.999
LONGITUDE = 4.4833
MAX_CHARGE_POWER_MW = 0.0070
CAR_BATTERY_CAPACITY_MWH = 0.03
MIN_CAR_BATTERY_CHARGE_MWH = 0.010

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
    name=ASSET_NAME, generic_asset_type_id=5, latitude=LATITUDE, longitude=LONGITUDE
)

market_id = 14
power_sensor = dict(
    name="power",
    event_resolution="PT5M",
    unit="MW",
    timezone="Europe/Amsterdam",
    attributes=json.dumps(
        {
            "capacity_in_mw": MAX_CHARGE_POWER_MW,
            "max_soc_in_mwh": CAR_BATTERY_CAPACITY_MWH,
            "min_soc_in_mwh": MIN_CAR_BATTERY_CHARGE_MWH,
            "market_id": MARKET_ID,
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
