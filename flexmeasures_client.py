from datetime import datetime, timedelta
import json
import pytz
import re
import requests
import isodate
from util_functions import time_round

import appdaemon.plugins.hass.hassapi as hass


class FlexMeasuresClient(hass.Hass):
    """ This class manages the communication with the FlexMeasures platform, which delivers the charging schedules.

    - Gets input from car calendar (see config setting: fm_car_reservation_calendar)
    - Saves charging schedule locally (input_text.chargeschedule)
    - Reports on errors locally (input_boolean.error_schedule_cannot_be_retrieved)
    """

    # Constants
    FM_API: str
    FM_URL: str
    FM_SCHEDULE_DURATION: str
    FM_USER_EMAIL: str
    FM_USER_PASSWORD: str
    MAX_NUMBER_OF_REATTEMPTS: int
    DELAY_FOR_INITIAL_ATTEMPT: int  # number of seconds
    DELAY_FOR_REATTEMPTS: int  # number of seconds
    CAR_RESERVATION_CALENDAR: str
    CAR_MAX_CAPACITY_IN_KWH: float
    CAR_MIN_SOC_IN_PERCENT: int
    CAR_MAX_SOC_IN_PERCENT: int
    CAR_MIN_SOC_IN_KWH: float
    CAR_MAX_SOC_IN_KWH: float
    WALLBOX_PLUS_CAR_ROUNDTRIP_EFFICIENCY: float

    # FM Authentication token
    fm_token: str
    # Helper to prevent parallel calls to FM for getting a schedule
    fm_busy_getting_schedule: bool
    # Helper to prevent sending the same trigger message twice.
    previous_trigger_message: str

    def initialize(self):
        self.previous_trigger_message = ""
        self.fm_busy_getting_schedule = False

        self.FM_API = self.args["fm_api"]
        self.FM_URL = self.FM_API + "/" + \
                      self.args["fm_api_version"] + "/sensors/" + \
                      str(self.args["fm_quasar_sensor_id"]) + "/schedules/"
        self.log(f"The FM_URL is: {self.FM_URL}.")
        self.FM_SCHEDULE_DURATION = self.args["fm_schedule_duration"]
        self.FM_USER_EMAIL = self.args["fm_user_email"]
        self.FM_USER_PASSWORD = self.args["fm_user_password"]
        self.DELAY_FOR_REATTEMPTS = int(self.args["delay_for_reattempts_to_retrieve_schedule"])
        self.MAX_NUMBER_OF_REATTEMPTS = int(self.args["max_number_of_reattempts_to_retrieve_schedule"])
        self.DELAY_FOR_INITIAL_ATTEMPT = int(self.args["delay_for_initial_attempt_to_retrieve_schedule"])
        self.CAR_RESERVATION_CALENDAR = self.args["fm_car_reservation_calendar"]

        self.CAR_MAX_CAPACITY_IN_KWH = float(self.args["car_max_capacity_in_kwh"])

        # ToDo: AJO 2022-12-30: This code is copied in several modules: combine!
        self.CAR_MIN_SOC_IN_PERCENT = int(float(self.args["car_min_soc_in_percent"]))
        # Make sure this value is between 10 en 30
        self.CAR_MIN_SOC_IN_PERCENT = max(min(30, self.CAR_MIN_SOC_IN_PERCENT), 10)

        self.CAR_MAX_SOC_IN_PERCENT = int(float(self.args["car_max_soc_in_percent"]))
        # Make sure this value is between 60 en 100
        self.CAR_MAX_SOC_IN_PERCENT = max(min(100, self.CAR_MAX_SOC_IN_PERCENT), 60)

        self.CAR_MIN_SOC_IN_KWH = self.CAR_MAX_CAPACITY_IN_KWH * self.CAR_MIN_SOC_IN_PERCENT / 100
        self.CAR_MAX_SOC_IN_KWH = self.CAR_MAX_CAPACITY_IN_KWH * self.CAR_MAX_SOC_IN_PERCENT / 100

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
        self.check_deprecation_and_sunset(url, res)
        if not res.status_code == 200:
            self.log_failed_response(res, url)
        self.fm_token = res.json()["auth_token"]

    def log_failed_response(self, res, endpoint: str):
        """Log failed response for a given endpoint."""
        try:
            self.log(f"{endpoint} failed ({res.status_code}) with JSON response {res.json()}")
        except json.decoder.JSONDecodeError:
            self.log(f"{endpoint} failed ({res.status_code}) with response {res}")

    def check_deprecation_and_sunset(self, url, res):
        """Log deprecation and sunset headers, along with info links.

        Reference
        ---------
        https://flexmeasures.readthedocs.io/en/latest/api/introduction.html#deprecation-and-sunset
        """
        warnings = res.headers.get("Deprecation") or res.headers.get("Sunset")
        if warnings:
            message = f"Your request to {url} returned a warning."
            # Go through the response headers in their given order
            for header, content in res.headers.items():
                if header == "Deprecation":
                    message += f"\nDeprecation: {content}."
                elif header == "Sunset":
                    message += f"\nSunset: {content}."
                elif header == "Link" and ('rel="deprecation";' in content or 'rel="sunset";' in content):
                    message += f" Link for further info: {content}"
            self.log(message)

    def get_new_schedule(self, current_soc_kwh):
        """Get a new schedule from FlexMeasures.
           But not if still busy with getting previous schedule.
        Trigger a new schedule to be computed and set a timer to retrieve it, by its schedule id.
        """
        if self.fm_busy_getting_schedule:
            self.log("Not getting new schedule, still processing previous request.")
            return

        # This has to be set here instead of in get_schedule because that function is called with a delay
        # and during this delay this get_new_schedule could be called.
        self.fm_busy_getting_schedule = True

        # Ask to compute a new schedule by posting flex constraints while triggering the scheduler
        schedule_id = self.trigger_schedule(current_soc_kwh=current_soc_kwh)
        if schedule_id is None:
            self.log("Failed to trigger new schedule, schedule ID is None. Cannot call get_schedule")
            self.fm_busy_getting_schedule = False
            return

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
        # Just to be sure also set this her, it's primary point for setting to true is in get_new_schedule
        self.fm_busy_getting_schedule = True

        schedule_id = kwargs["schedule_id"]
        url = self.FM_URL + schedule_id
        message = {
            "duration": self.FM_SCHEDULE_DURATION,
        }
        res = requests.get(
            url,
            params=message,
            headers={"Authorization": self.fm_token},
        )
        self.check_deprecation_and_sunset(url, res)
        if (res.status_code != 200) or (res.json is None):
            self.log_failed_response(res, url)
            s = self.DELAY_FOR_REATTEMPTS
            attempts_left = kwargs.get("attempts_left", self.MAX_NUMBER_OF_REATTEMPTS)
            if attempts_left >= 1:
                self.log(f"Reattempting to get schedule in {s} seconds (attempts left: {attempts_left})")
                self.run_in(self.get_schedule, delay=s, attempts_left=attempts_left - 1,
                            schedule_id=schedule_id)
            else:
                self.log("Schedule cannot be retrieved. Any previous charging schedule will keep being followed.")
                self.fm_busy_getting_schedule = False
            return

        self.log(f"GET schedule success: retrieved {res.status_code}")
        self.fm_busy_getting_schedule = False

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

        url = self.FM_URL + "trigger"

        # TODO AJO 2022-02-26: would it be better to have this in v2g_liberty module?
        # Set default target_soc to 100% one week from now
        target_datetime = (time_round(datetime.now(tz=pytz.utc), resolution) + timedelta(days=7)).isoformat()
        target_soc = self.CAR_MAX_CAPACITY_IN_KWH

        # Check if calendar has a relevant item that is within one week (*) from now.
        # (*) 7 days is the setting in v2g_liberty_package.yaml
        # If so try to retrieve target_soc
        car_reservation = self.get_state(self.CAR_RESERVATION_CALENDAR, attribute="all")
        # This should get the first item from the calendar. If no item is found (i.e. items are too far into the future)
        # it returns a general entity that does not contain a start_time, message or description.

        if car_reservation is None:
            self.log("No calendar item found, no calendar configured?")
        else:
            self.log(f"Calender: {car_reservation}")
            calendar_item_start = car_reservation["attributes"].get("start_time", None)
            if calendar_item_start is not None:
                calendar_item_start = isodate.parse_datetime(calendar_item_start.replace(" ", "T")).astimezone(
                    pytz.timezone("Europe/Amsterdam")).isoformat()
                if calendar_item_start < target_datetime:
                    # There is a relevant calendar item with a start date less than a week in the future.
                    # Set the calendar_item_start as the target for the schedule
                    target_datetime = time_round(isodate.parse_datetime(calendar_item_start), resolution).isoformat()

                    # Now try to retrieve target_soc.
                    # Depending on the type of calendar the description or message contains the possible target_soc.
                    m = car_reservation["attributes"]["message"]
                    d = car_reservation["attributes"]["description"]
                    # Prevent concatenation of possible None values
                    text_to_search_in = " ".join(filter(None, [m, d]))

                    # First try searching for a number in kWh
                    found_target_soc_in_kwh = search_for_soc_target("kWh", text_to_search_in)
                    if found_target_soc_in_kwh is not None:
                        self.log(f"Target SoC from calendar: {found_target_soc_in_kwh} kWh.")
                        target_soc = found_target_soc_in_kwh
                    else:
                        # No kWh number found, try searching for a number in %
                        found_target_soc_in_percentage = search_for_soc_target("%", text_to_search_in)
                        if found_target_soc_in_percentage is not None:
                            self.log(f"Target SoC from calendar: {found_target_soc_in_percentage} %.")
                            target_soc = round(float(found_target_soc_in_percentage) / 100 * self.CAR_MAX_CAPACITY_IN_KWH, 2)

                    # Prevent target_soc above max_capacity
                    if target_soc > self.CAR_MAX_CAPACITY_IN_KWH:
                        self.log(f"Target SoC from calendar too high: {target_soc}, "
                                 f"adjusted to {self.CAR_MAX_CAPACITY_IN_KWH}kWh.")
                        target_soc = self.CAR_MAX_CAPACITY_IN_KWH
                    else:
                        # A targets in a calendar item below 30% are not acceptable (not relevant)
                        min_acceptable_target_in_percent = 30
                        min_acceptable_target_in_kwh = self.CAR_MAX_CAPACITY_IN_KWH * min_acceptable_target_in_percent / 100
                        if target_soc < min_acceptable_target_in_kwh:
                            self.log(f"Target SoC from calendar too low: {target_soc}, "
                                     f"adjusted to {min_acceptable_target_in_kwh}kWh.")
                            target_soc = min_acceptable_target_in_kwh

        message = {
            "start": soc_datetime,
            "flex-model": {
                "soc-at-start": current_soc_kwh,
                "soc-unit": "kWh",
                "soc-targets": [
                    {
                        "value": target_soc,
                        "datetime": target_datetime,
                    }
                ],
                "roundtrip-efficiency": self.WALLBOX_PLUS_CAR_ROUNDTRIP_EFFICIENCY
            }
        }

        # Prevent triggering the same message twice. This sometimes happens when ??
        if message == self.previous_trigger_message:
            self.log(f"Not triggering schedule, message is exactly the same as previous.")
            return None
        else:
            self.log(f"Trigger_schedule on url '{url}', with message: '{message}'.")
            self.previous_trigger_message = message

        res = requests.post(
            url,
            json=message,
            headers={"Authorization": self.fm_token},
        )
        self.check_deprecation_and_sunset(url, res)
        schedule_id = None
        if res.status_code == 200:
            schedule_id = res.json()["schedule"]  # can still be None in case something went wong

        if schedule_id is None:
            self.log_failed_response(res, url)
            self.try_solve_authentication_error(res, url, self.trigger_schedule, *args, **fnc_kwargs)
            self.set_state("input_boolean.error_schedule_cannot_be_retrieved", state="on")
            return None

        self.log(f"Successfully triggered schedule. Schedule id: {schedule_id}")
        self.set_state("input_boolean.error_schedule_cannot_be_retrieved", state="off")
        return schedule_id

    def try_solve_authentication_error(self, res, url, fnc, *fnc_args, **fnc_kwargs):
        if fnc_kwargs.get("retry_auth_once", True) and res.status_code == 401:
            self.log(f"Call to {url} failed on authorization (possibly the token expired); attempting to "
                     f"reauthenticate once.")
            self.authenticate_with_fm()
            fnc_kwargs["retry_auth_once"] = False
            fnc(*fnc_args, **fnc_kwargs)


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
