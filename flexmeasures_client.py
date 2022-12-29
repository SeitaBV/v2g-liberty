from datetime import datetime, timedelta
import pytz
import re
import requests
import time
import isodate
from typing import Optional
from util_functions import time_mod, time_round

import appdaemon.plugins.hass.hassapi as hass


class FlexMeasuresClient(hass.Hass):
    """ This class manages the communication with the FlexMeasures platform, which delivers the charging schedules.

    - Gets input from car calendar (see config setting: fm_car_reservation_calendar)
    - Saves charging schedule locally (input_text.chargeschedule)
    - Reports on errors locally (input_boolean.error_schedule_cannot_be_retrieved)
    """

    # Constants
    FM_API: str
    FM_API_VERSION: str
    FM_QUASAR_SENSOR_ID: str
    FM_SCHEDULE_DURATION: str
    FM_USER_EMAIL: str
    FM_USER_PASSWORD: str
    MAX_NUMBER_OF_REATTEMPTS: int
    DELAY_FOR_INITIAL_ATTEMPT: int  # number of seconds
    DELAY_FOR_REATTEMPTS: int  # number of seconds
    CAR_RESERVATION_CALENDAR: str
    CAR_MAX_SOC_IN_KWH: float
    WALLBOX_PLUS_CAR_ROUNDTRIP_EFFICIENCY: float

    # Variables
    fm_token: str

    def initialize(self):
        self.FM_API = self.args["fm_api"]
        self.FM_API_VERSION = self.args["fm_api_version"]
        self.FM_QUASAR_SENSOR_ID = str(self.args["fm_quasar_sensor_id"])
        self.FM_SCHEDULE_DURATION = self.args["fm_schedule_duration"]
        self.FM_USER_EMAIL = self.args["fm_user_email"]
        self.FM_USER_PASSWORD = self.args["fm_user_password"]
        self.DELAY_FOR_REATTEMPTS = int(self.args["delay_for_reattempts_to_retrieve_schedule"])
        self.MAX_NUMBER_OF_REATTEMPTS = int(self.args["max_number_of_reattempts_to_retrieve_schedule"])
        self.DELAY_FOR_INITIAL_ATTEMPT = int(self.args["delay_for_initial_attempt_to_retrieve_schedule"])
        self.CAR_RESERVATION_CALENDAR = self.args["fm_car_reservation_calendar"]
        self.CAR_MAX_SOC_IN_KWH = float(self.args["fm_car_max_soc_in_kwh"])
        self.WALLBOX_PLUS_CAR_ROUNDTRIP_EFFICIENCY = float(self.args["wallbox_plus_car_roundtrip_efficiency"])

    def authenticate_with_fm(self):
        """Authenticate with the FlexMeasures server and store the returned auth token.

        Hint:
        the lifetime of the token is limited, so also call this method whenever the server returns a 401 status code.
        """
        self.log("Authenticating with FlexMeasures")
        url = self.FM_API + "/requestAuthToken"
        res = requests.post(
            url,
            json=dict(
                email=self.FM_USER_EMAIL,
                password=self.FM_USER_PASSWORD,
            ),
        )
        if not res.status_code == 200:
            self.log_failed_response(res, url)
        self.fm_token = res.json()["auth_token"]

    def log_failed_response(self, res, endpoint: str):
        """Log failed response for a given endpoint."""
        try:
            self.log(f"{endpoint} failed ({res.status_code}) with JSON response {res.json()}")
        except json.decoder.JSONDecodeError:
            self.log(f"{endpoint} failed ({res.status_code}) with response {res}")

    def get_new_schedule(self, current_soc_kwh):
        """Get a new schedule from FlexMeasures.

        Trigger a new schedule to be computed and set a timer to retrieve it, by its schedule id.
        """

        # Ask to compute a new schedule by posting flex constraints while triggering the scheduler
        schedule_id = self.trigger_schedule(current_soc_kwh=current_soc_kwh)

        # Set a timer to get the schedule a little later
        s = self.DELAY_FOR_INITIAL_ATTEMPT
        self.log(f"Attempting to get schedule in {s} seconds")
        self.run_in(self.get_schedule, delay=s, schedule_id=schedule_id)

    def get_schedule(self, kwargs, **fnc_kwargs):
        """GET a schedule message that has been requested by trigger_schedule.
           The ID for this is schedule_id.
           Then store the retrieved schedule.

        Pass the schedule id using kwargs["schedule_id"]=<schedule_id>.
        """
        schedule_id = kwargs["schedule_id"]
        url = self.FM_API + "/" + self.FM_API_VERSION + "/sensors/" + self.FM_QUASAR_SENSOR_ID + "/schedules/" + schedule_id
        message = {
            "duration": self.FM_SCHEDULE_DURATION,
        }
        res = requests.get(
            url,
            params=message,
            headers={"Authorization": self.fm_token},
        )
        if res.status_code != 200:
            self.log_failed_response(res, url)
        else:
            self.log(f"GET schedule success: retrieved {res.status_code}")
        if res.json().get("status", None) == "UNKNOWN_SCHEDULE":
            s = self.DELAY_FOR_REATTEMPTS
            attempts_left = kwargs.get("attempts_left", self.MAX_NUMBER_OF_REATTEMPTS)
            if attempts_left >= 1:
                self.log(f"Reattempting to get schedule in {s} seconds (attempts left: {attempts_left})")
                self.run_in(self.get_schedule, delay=s, attempts_left=attempts_left - 1,
                            schedule_id=schedule_id)
            else:
                self.log("Schedule cannot be retrieved. Any previous charging schedule will keep being followed.")
            return

        schedule = res.json()
        self.log(f"Schedule {schedule}")
        # To trigger state change we add the date to the state. State change is not triggered by attributes.
        self.set_state("input_text.chargeschedule",
                       state="ChargeScheduleAvailable" + datetime.now(tz=pytz.utc).isoformat(), attributes=schedule)

    def trigger_schedule(self, *args, **fnc_kwargs):
        """Request a new schedule to be generated by calling the schedule triggering endpoint, while
        POSTing flex constraints.
        Return the schedule id for later retrieval of the asynchronously computed schedule.
        """

        # Prepare the SoC measurement to be sent along with the scheduling request
        current_soc_kwh = fnc_kwargs["current_soc_kwh"]
        self.log(f"trigger_schedule called with current_soc_kwh: {current_soc_kwh} kWh.")

        # Snap to sensor resolution
        soc_datetime = datetime.now(tz=pytz.utc)
        resolution = timedelta(minutes=self.args["fm_quasar_soc_event_resolution_in_minutes"])
        soc_datetime = time_round(soc_datetime, resolution).isoformat()

        url = self.FM_API + "/" + self.FM_API_VERSION + "/sensors/" + self.FM_QUASAR_SENSOR_ID + "/schedules/trigger"
        self.log(f"Triggering schedule by calling {url}")

        # TODO AJO 2022-02-26: would it be better to have this in v2g_liberty module?
        # Retrieve target SOC
        car_reservation = self.get_state(self.CAR_RESERVATION_CALENDAR, attribute="all")
        self.log(f"Car_reservation: {car_reservation}")
        if car_reservation is None or \
                ("description" not in car_reservation["attributes"] and
                 "message" not in car_reservation["attributes"]):
            # Set default target to 100% one week from now
            target = self.CAR_MAX_SOC_IN_KWH
            target_datetime = (time_round(datetime.now(tz=pytz.utc), resolution) + timedelta(days=7)).isoformat()
        else:
            # Depending on the type of calendar the description or message contains the possible target.
            text_to_search_in = car_reservation["attributes"]["message"] + " " + car_reservation["attributes"][
                "description"]

            # First try searching for a number in kWh
            target = search_for_soc_target("kWh", text_to_search_in)
            if target is None:
                # No kWh number found, try searching for a number in %
                target = search_for_soc_target("%", text_to_search_in)
                if target is None:
                    target = self.CAR_MAX_SOC_IN_KWH
                else:
                    target = round(float(target) / 100 * self.CAR_MAX_SOC_IN_KWH, 2)

            if target > self.CAR_MAX_SOC_IN_KWH:
                target = self.CAR_MAX_SOC_IN_KWH
            else:
                MIN_SOC_PERCENT = 30
                min_soc_kwh = round(float(self.CAR_MAX_SOC_IN_KWH * MIN_SOC_PERCENT / 100), 2)
                if target < min_soc_kwh:
                    target = min_soc_kwh

            self.log(f"Target SoC from calendar: {target} kWh.")

            target_datetime = isodate.parse_datetime(
                car_reservation["attributes"]["start_time"].replace(" ", "T")).astimezone(
                pytz.timezone("Europe/Amsterdam")).isoformat()
            target_datetime = time_round(isodate.parse_datetime(target_datetime), resolution).isoformat()

        message = {
            "flex-model": {
                "soc-at-start": current_soc_kwh,
                "soc-unit": "kWh",
                "start": soc_datetime,
                "soc-targets": [
                    {
                        "value": target,
                        "datetime": target_datetime,
                    }
                ],
                "roundtrip-efficiency": self.WALLBOX_PLUS_CAR_ROUNDTRIP_EFFICIENCY
            }
        }
        self.log(f"Trigger_schedule with message: {message}.")
        res = requests.post(
            url,
            json=message,
            headers={"Authorization": self.fm_token},
        )
        schedule_id = None
        if res.status_code == 200:
            schedule_id = res.json()["schedule"]  # can still be None in case something went wong

        if schedule_id is None:
            self.log_failed_response(res, url)
            self.handle_response_errors(message, res, url, self.trigger_schedule, *args, **fnc_kwargs)
            self.set_state("input_boolean.error_schedule_cannot_be_retrieved", state="on")
            return

        self.log(f"Successfully triggered schedule. Schedule id: {schedule_id}")
        self.set_state("input_boolean.error_schedule_cannot_be_retrieved", state="off")
        return schedule_id

    def handle_response_errors(self, message, res, description, fnc, **fnc_kwargs):
        if fnc_kwargs.get("retry_auth_once", True) and res.status_code == 401:
            self.log(f"Failed to {description} on authorization (possibly the token expired); attempting to "
                     f"reauthenticate once.")
            self.authenticate_with_fm()
            fnc_kwargs["retry_auth_once"] = False
            fnc(**fnc_kwargs)
            self.set_state("input_boolean.error_schedule_cannot_be_retrieved", state="off")
        else:
            self.set_state("input_boolean.error_schedule_cannot_be_retrieved", state="on")
            self.log(f"Failed to {description} (status {res.status_code}): {res.json()} as response to {message}")


# TODO AJO 2022-02-26: would it be better to have this in v2g_liberty module?
def search_for_soc_target(search_unit: str, string_to_search_in: str) -> int:
    """Search description for the first occurrence of some (integer) number of the search_unit.

    Parameters:
        search_unit (int): The unit to search for, typically % or kWh, found directly following the number
        string_to_search_in (str): The string in which the soc in searched
    Returns:
        integer number or None if nothing is found

    Forgives errors in incorrect capitalization of the unit and missing/double spaces.
    """
    if string_to_search_in is None:
        return None
    string_to_search_in = string_to_search_in.lower()
    pattern = re.compile(rf"(?P<quantity>\d+) *{search_unit.lower()}")
    match = pattern.search(string_to_search_in)
    if match:
        return int(float(match.group("quantity")))

    return None
