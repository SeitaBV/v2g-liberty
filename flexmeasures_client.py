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

    # Conatants
    FM_API = str
    FM_API_VERSION = str
    FM_QUASAR_SENSOR_ID = str
    FM_SCHEDUAL_DURATION = str
    FM_USER_EMAIL = str
    FM_USER_PASSWORD = str
    DELAY_FOR_REATTEMPTS = str
    CAR_RESERVATION_CALENDAR = str
    CAR_MAX_SOC_IN_KWH = str
    WALLBOX_PLUS_CAR_ROUNDTRIP_EFFICIENCY =str

    # Variables
    fm_token: str

    def initialize(self):
        self.FM_API = self.args["fm_api"]
        self.FM_API_VERSION = self.args["fm_api_version"]
        self.FM_QUASAR_SENSOR_ID = str(self.args["fm_quasar_sensor_id"])
        self.FM_SCHEDUAL_DURATION = self.args["fm_schedule_duration"]
        self.FM_USER_EMAIL = self.args["fm_user_email"]
        self.FM_USER_PASSWORD = self.args["fm_user_password"]
        self.DELAY_FOR_REATTEMPTS = self.args["delay_for_reattempts_to_retrieve_schedule"]
        self.MAX_NUMBER_OF_REATTEMPTS = self.args["max_number_of_reattempts_to_retrieve_schedule"]
        self.DELAY_FOR_INITIAL_ATTEMPT = self.args["delay_for_initial_attempt_to_retrieve_schedule"]
        self.CAR_RESERVATION_CALENDAR = self.args["fm_car_reservation_calendar"]
        self.CAR_MAX_SOC_IN_KWH = self.args["fm_car_max_soc_in_kwh"]
        self.WALLBOX_PLUS_CAR_ROUNDTRIP_EFFICIENCY = self.args["wallbox_plus_car_roundtrip_efficiency"]


    def authenticate_with_fm(self):
        """Authenticate with the FlexMeasures server and store the returned auth token.

        Hint: the lifetime of the token is limited, so also call this method whenever the server returns a 401 status code.
        """
        self.log("Authenticating with FlexMeasures")
        url = self.FM_API + "/requestAuthToken",
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

    def get_new_schedule(self):
        """Get a new schedule from FlexMeasures.

        Trigger a new schedule to be computed and set a timer to retrieve it, by its schedule id.
        """

        # Ask to compute a new schedule by posting flex constraints while triggering the scheduler
        schedule_id = self.trigger_schedule()

        # Set a timer to get the schedule a little later
        s = self.DELAY_FOR_INITIAL_ATTEMPT
        self.log(f"Attempting to get schedule in {s} seconds")
        self.run_in(self.get_schedule, delay=int(s), schedule_id=schedule_id)

    def get_schedule(self, kwargs, **fnc_kwargs):
        """GET a schedule message that has been requested by trigger_schedule.
           The ID for this is schedule_id.
           Then store the retrieved schedule.

        Pass the schedule id using kwargs["schedule_id"]=<schedule_id>.
        """
        schedule_id = kwargs["schedule_id"]
        url = self.FM_API + "/" + self.FM_API_VERSION + "/sensors/" + self.FM_QUASAR_SENSOR_ID + "/schedules/" + schedule_id
        message = {
            "duration": self.FM_SCHEDUAL_DURATION,
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
                self.run_in(self.get_schedule, delay=int(s), attempts_left=attempts_left - 1,
                            schedule_id=schedule_id)
            else:
                self.log("Schedule cannot be retrieved. Any previous charging schedule will keep being followed.")
            return

        schedule = res.json()
        self.log(f"Schedule {schedule}")
        # To trigger state change we add the date to the state. State change is not triggered by attributes.
        self.set_state("input_text.chargeschedule", state="ChargeScheduleAvailable" + datetime.now(tz=pytz.utc).isoformat(), attributes=schedule)

    def trigger_schedule(self, *args, **fnc_kwargs):
        """Request a new schedule to be generated by calling the schedule triggering endpoint, while
        POSTing flex constraints.
        Return the schedule id for later retrieval of the asynchronously computed schedule.
        """

        # Prepare the SoC measurement to be sent along with the scheduling request
        soc_entity = self.get_state("input_number.car_state_of_charge_wh", attribute="all")
        soc_value = float(soc_entity["state"]) / 1000  # to kWh
        soc_datetime = datetime.now(tz=pytz.utc)  # soc_entity["last_changed"]

        # Snap to sensor resolution
        # soc_datetime = isodate.parse_datetime(soc_datetime)
        resolution = timedelta(minutes=self.args["fm_quasar_soc_event_resolution_in_minutes"])
        soc_datetime = time_round(soc_datetime, resolution).isoformat()

        url = self.FM_API + "/" + self.FM_API_VERSION + "/sensors/" + self.FM_QUASAR_SENSOR_ID + "/schedules/trigger"
        self.log(f"Triggering schedule by calling {url}")

        # TODO AJO 2022-02-26: dit zou in fm_ha_module moeten zitten...
        # Retrieve target SOC
        car_reservation = self.get_state(self.CAR_RESERVATION_CALENDAR, attribute="all")
        self.log(f"Car_reservation: {car_reservation}")
        if car_reservation is None or "description" not in car_reservation["attributes"]:
            # Set default target to 100% one week from now
            target = self.CAR_MAX_SOC_IN_KWH
            target_datetime = (time_round(datetime.now(tz=pytz.utc), resolution) + timedelta(days=7)).isoformat()
        else:
            target = search_for_kwh_target(car_reservation["attributes"]["description"])
            self.log(f"Target SoC from calendar: {target} kWh.")
            if target is None:
                target = self.CAR_MAX_SOC_IN_KWH
            target_datetime = isodate.parse_datetime(
                car_reservation["attributes"]["start_time"].replace(" ", "T")).astimezone(
                pytz.timezone("Europe/Amsterdam")).isoformat()
            target_datetime = time_round(isodate.parse_datetime(target_datetime), resolution).isoformat()

        message = {
            "soc-at-start": soc_value,
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
        self.log(message)
        res = requests.post(
            url,
            json=message,
            headers={"Authorization": self.fm_token},
        )
        if res.status_code != 200:
            self.log_failed_response(res, url)
            self.handle_response_errors(message, res, url, self.trigger_schedule, **fnc_kwargs)
            self.set_state("input_boolean.error_schedule_cannot_be_retrieved", state="on")
            return
        else:
            self.set_state("input_boolean.error_schedule_cannot_be_retrieved", state="off")
        self.log(f"Successfully triggered schedule. Result: {res.status_code}.")
        schedule_id = res.json()["schedule"]
        self.log(f"Schedule id: {schedule_id}")
        return schedule_id

    def handle_response_errors(self, message, res, description, fnc, **fnc_kwargs):
        if fnc_kwargs.get("retry_auth_once", True) and res.status_code == 401:
            self.log(
                f"Failed to {description} on authorization (possibly the token expired); attempting to reauthenticate once")
            self.authenticate_with_fm()
            fnc_kwargs["retry_auth_once"] = False
            fnc(**fnc_kwargs)
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
