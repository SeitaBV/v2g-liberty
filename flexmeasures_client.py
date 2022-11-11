from datetime import datetime, timedelta
import pytz
import re
import requests
import time
import isodate
from typing import Optional

import appdaemon.plugins.hass.hassapi as hass


class FlexMeasuresClient(hass.Hass):
    """ This class manages the communication with the FlexMeasures platform, which delivers the charging schedules.

    - Gets input from car calendar (see config setting: fm_car_reservation_calendar)
    - Saves charging schedule locally (input_text.chargeschedule)
    - Reports on errors locally (input_boolean.error_schedule_cannot_be_retrieved)
    """

    fm_token: str

    def initialize(self):
        pass

    def authenticate_with_fm(self):
        """Authenticate with the FlexMeasures server and store the returned auth token.

        Hint: the lifetime of the token is limited, so also call this method whenever the server returns a 401 status code.
        """
        self.log("Authenticating with FlexMeasures")
        res = requests.post(
            self.args["fm_api"] + "/requestAuthToken",
            json=dict(
                email=self.args["fm_user_email"],
                password=self.args["fm_user_password"],
            ),
        )
        if not res.status_code == 200:
            self.log(f"Authentication failed with response {res.json()}")
        self.fm_token = res.json()["auth_token"]

    def get_new_schedule(self):
        """Get a new schedule from FlexMeasures.

        POST a UDI event and set a timer to GET a device message for the given UDI event id.
        """

        # Ask to compute a new schedule by posting a UDI event
        udi_event_id = self.post_udi_event()

        # Set a timer to get the device message
        s = self.args["delay_for_initial_attempt_to_retrieve_device_message"]
        self.log(f"Attempting to get device message in {s} seconds")
        self.run_in(self.get_device_message, delay=int(s), udi_event_id=udi_event_id)

    def get_device_message(self, kwargs, **fnc_kwargs):
        """GET a device message for a given UDI event, and store it as a charging schedule.

        Pass the UDI event id using kwargs["udi_event_id"]=<udi_event_id>.
        """
        udi_event_id = kwargs["udi_event_id"]
        url = self.args["fm_api"] + "/" + self.args["fm_api_version"] + "/sensors/" + str(self.args["fm_quasar_sensor_id"]) + "/schedules/" + udi_event_id
        message = {
            "duration": self.args["fm_schedule_duration"],
        }
        res = requests.get(
            url,
            params=message,
            headers={"Authorization": self.fm_token},
        )
        self.log(f"Result code: {res.status_code}")
        # if res.status_code != 200:
        #     self.log(f"GetDeviceMessage failed with response {res.json()}")
        #     self.handle_response_errors(message, res, "GET device message", self.get_device_message, kwargs,
        #                                 **fnc_kwargs)
        #     return
        self.log(f"GET device message success: retrieved {res.status_code}")
        if res.json().get("status", None) == "UNKNOWN_SCHEDULE":
            s = self.args["delay_for_reattempts_to_retrieve_device_message"]
            attempts_left = kwargs.get("attempts_left",
                                       self.args["max_number_of_reattempts_to_retrieve_device_message"])
            if attempts_left >= 1:
                self.log(f"Reattempting to get device message in {s} seconds (attempts left: {attempts_left})")
                self.run_in(self.get_device_message, delay=int(s), attempts_left=attempts_left - 1,
                            udi_event_id=udi_event_id)
            else:
                self.log("Device message cannot be retrieved. Any previous charging schedule will keep being followed.")

        schedule = res.json()
        self.log(f"Schedule {schedule}")
        # To trigger state change we add the date to the state. State change is not triggered by attriibutes.
        self.set_state("input_text.chargeschedule", state="ChargeScheduleAvailable" + datetime.now(tz=pytz.utc).isoformat(), attributes=schedule)

    def post_udi_event(self, *args, **fnc_kwargs):
        """POST a UDI event and return the UDI event id for later retrieval of a device message."""

        # Prepare the SoC measurement to be sent along with the scheduling request
        soc_entity = self.get_state("input_number.car_state_of_charge_wh", attribute="all")
        soc_value = float(soc_entity["state"]) / 1000  # to kWh
        soc_datetime = datetime.now(tz=pytz.utc)  # soc_entity["last_changed"]

        # Snap to sensor resolution
        # soc_datetime = isodate.parse_datetime(soc_datetime)
        resolution = timedelta(minutes=self.args["fm_quasar_soc_event_resolution_in_minutes"])
        soc_datetime = time_round(soc_datetime, resolution).isoformat()

        url = self.args["fm_api"] + "/" + self.args["fm_api_version"] + "/sensors/" + str(self.args["fm_quasar_sensor_id"]) + "/schedules/trigger"
        udi_event_id = int(time.time())  # we use this as our UDI event id
        self.log(f"Posting UDI event {udi_event_id} to {url}")

        # TODO AJO 2022-02-26: dit zou in fm_ha_module moeten zitten...
        # Retrieve target SOC
        car_reservation = self.get_state(self.args["fm_car_reservation_calendar"], attribute="all")
        self.log(f"Car_reservation: {car_reservation}")
        if car_reservation is None or "description" not in car_reservation["attributes"]:
            # Set default target to 100% one week from now
            target = self.args["fm_car_max_soc_in_kwh"]
            target_datetime = (time_round(datetime.now(tz=pytz.utc), resolution) + timedelta(days=7)).isoformat()
        else:
            target = search_for_kwh_target(car_reservation["attributes"]["description"])
            if target is None:
                target = self.args["fm_car_max_soc_in_kwh"]
            target_datetime = isodate.parse_datetime(
                car_reservation["attributes"]["start_time"].replace(" ", "T")).astimezone(
                pytz.timezone("Europe/Amsterdam")).isoformat()
            target_datetime = time_round(isodate.parse_datetime(target_datetime), resolution).isoformat()

        message = {
            # "type": "PostUdiEventRequest",
            # "event": self.args["fm_quasar_entity_address"] + ":" + str(udi_event_id) + ":soc-with-targets",
            # todo: relay flow constraints with new UDI event type ":soc-with-target-and-flow-constraints"
            "soc-at-start": soc_value,
            "soc-unit": "kWh",
            "start": soc_datetime,
            "soc-targets": [
                {
                    "value": target,
                    "datetime": target_datetime,
                }
            ],
            "roundtrip-efficiency": self.args["wallbox_plus_car_roundtrip_efficiency"]
        }
        self.log(message)
        res = requests.post(
            url,
            json=message,
            headers={"Authorization": self.fm_token},
        )
        if res.status_code != 200:
            self.log(f"PostUdiEvent failed with response {res.json()}")
            self.handle_response_errors(message, res, "POST UDI event", self.post_udi_event, **fnc_kwargs)
            self.set_state("input_boolean.error_schedule_cannot_be_retrieved", state="on")
            return
        else:
            self.set_state("input_boolean.error_schedule_cannot_be_retrieved", state="off")
        self.log(f"Successfully posted UDI event. Result: {res.status_code}.")
        udi_event_id = res.json()["schedule"]
        self.log(f"Uid_event_id: {udi_event_id}")
        return udi_event_id

    def handle_response_errors(self, message, res, description, fnc, **fnc_kwargs):
        if fnc_kwargs.get("retry_auth_once", True) and res.status_code == 401:
            self.log(
                f"Failed to {description} on authorization (possibly the token expired); attempting to reauthenticate once")
            self.authenticate_with_fm()
            fnc_kwargs["retry_auth_once"] = False
            fnc(*fnc, **fnc_kwargs)
            self.set_state("input_boolean.error_schedule_cannot_be_retrieved", state="off")
        else:
            self.set_state("input_boolean.error_schedule_cannot_be_retrieved", state="on")
            self.log(f"Failed to {description} (status {res.status_code}): {res.json()} as response to {message}")


# TODO AJO 2022-02-26: dit zou in fm_ha_module moeten zitten...
def search_for_kwh_target(description: Optional[str]) -> Optional[int]:
    """Search description for the first occurrence of some (integer) number of kWh.

    Forgives errors in incorrect capitalization of the unit and missing/double spaces.
    """
    if description is None:
        return None
    match = re.search("(?P<quantity>\d+) *kwh", description.lower())
    if match is None:
        return None
    return int(match.group("quantity"))


# TODO AJO 2022-02-26: dit zou in util module moeten zitten...
def time_mod(time, delta, epoch=None):
    """From https://stackoverflow.com/a/57877961/13775459"""
    if epoch is None:
        epoch = datetime(1970, 1, 1, tzinfo=time.tzinfo)
    return (time - epoch) % delta


# TODO AJO 2022-02-26: dit zou in util module moeten zitten...
def time_round(time, delta, epoch=None):
    """From https://stackoverflow.com/a/57877961/13775459"""
    mod = time_mod(time, delta, epoch)
    if mod < (delta / 2):
        return time - mod
    return time + (delta - mod)
