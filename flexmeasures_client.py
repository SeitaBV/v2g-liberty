from datetime import datetime, timedelta
import time
import json
import math
import re
import requests
import isodate
import constants as c
from v2g_globals import time_round

import appdaemon.plugins.hass.hassapi as hass


class FlexMeasuresClient(hass.Hass):
    """ This class manages the communication with the FlexMeasures platform, which delivers the charging schedules.

    - Gets input from car calendar (see config setting: fm_car_reservation_calendar)
    - Saves charging schedule locally (input_text.chargeschedule)
    - Reports on errors via v2g_liberty module handle_no_schedule()

    """

    # Constants
    FM_URL: str
    FM_TIGGER_URL: str
    FM_OPTIMISATION_CONTEXT: dict
    FM_SCHEDULE_DURATION: str
    FM_USER_EMAIL: str
    FM_USER_PASSWORD: str
    MAX_NUMBER_OF_REATTEMPTS: int
    DELAY_FOR_INITIAL_ATTEMPT: int  # number of seconds
    DELAY_FOR_REATTEMPTS: int  # number of seconds
    CAR_RESERVATION_CALENDAR: str

    # Battery protection boundaries ##
    # A hard setting that is always respected (and used for Max_Charge_Now when
    # car is connected with a SoC below this value)
    CAR_MIN_SOC_IN_KWH: float

    # A 'soft' setting, that is respected during normal cycling but is ignored when
    # a calendar item requires a higher SoC.
    CAR_MAX_SOC_IN_KWH: float

    # A slack for the constraint_relaxation_window in minutes
    WINDOW_SLACK: int = 60

    # FM Authentication token
    fm_token: str
    # Helper to prevent parallel calls to FM for getting a schedule
    fm_busy_getting_schedule: bool
    # Helper to prevent blocking the sequence of getting schedules.
    # Sometimes the previous bool is not reset (why we don't know), then it needs a timed reset.
    # stores the date_time of the last successful received schedule
    fm_date_time_last_schedule: datetime
    fm_max_seconds_between_schedules: int

    # Helper to see if FM connection/ping has too many errors
    connection_error_counter: int
    handle_for_repeater: str
    connection_ping_interval: int
    errored_connection_ping_interval: int

    def initialize(self):
        self.log("Initializing FlexMeasuresClient")

        self.fm_busy_getting_schedule = False
        self.log(f"Init, fm_busy_getting_schedule: {self.fm_busy_getting_schedule}.")
        self.fm_date_time_last_schedule = self.get_now()

        base_url = c.FM_SCHEDULE_URL + str(c.FM_ACCOUNT_POWER_SENSOR_ID)
        self.FM_URL = base_url + c.FM_SCHEDULE_SLUG
        self.FM_TIGGER_URL = base_url + c.FM_SCHEDULE_TRIGGER_SLUG
        self.FM_SCHEDULE_DURATION = self.args["fm_schedule_duration"]
        self.FM_USER_EMAIL = self.args["fm_user_email"]
        self.FM_USER_PASSWORD = self.args["fm_user_password"]
        self.DELAY_FOR_REATTEMPTS = int(self.args["delay_for_reattempts_to_retrieve_schedule"])
        self.MAX_NUMBER_OF_REATTEMPTS = int(self.args["max_number_of_reattempts_to_retrieve_schedule"])
        self.DELAY_FOR_INITIAL_ATTEMPT = int(self.args["delay_for_initial_attempt_to_retrieve_schedule"])

        # Add an extra attempt to prevent the last attempt not being able to finish.
        self.fm_max_seconds_between_schedules = \
            self.DELAY_FOR_REATTEMPTS * (self.MAX_NUMBER_OF_REATTEMPTS + 1) + self.DELAY_FOR_INITIAL_ATTEMPT
        self.CAR_RESERVATION_CALENDAR = self.args["fm_car_reservation_calendar"]

        self.CAR_MIN_SOC_IN_KWH = c.CAR_MAX_CAPACITY_IN_KWH * c.CAR_MIN_SOC_IN_PERCENT / 100
        self.CAR_MAX_SOC_IN_KWH = c.CAR_MAX_CAPACITY_IN_KWH * c.CAR_MAX_SOC_IN_PERCENT / 100
        self.log(f"Car_max_soc: {self.CAR_MAX_SOC_IN_KWH} kWh.")

        if c.OPTIMISATION_MODE == "price":
            self.FM_OPTIMISATION_CONTEXT = {"consumption-price-sensor": c.FM_PRICE_CONSUMPTION_SENSOR_ID,
                                            "production-price-sensor": c.FM_PRICE_PRODUCTION_SENSOR_ID}
        else:
            # Assumed optimisation = emissions
            self.FM_OPTIMISATION_CONTEXT = {"consumption-price-sensor": c.FM_EMISSIONS_SENSOR_ID,
                                            "production-price-sensor": c.FM_EMISSIONS_SENSOR_ID}
        self.log(f"Optimisation context: {self.FM_OPTIMISATION_CONTEXT}")

        # Ping every half hour. If offline a separate process will run to increase polling frequency.
        self.connection_error_counter = 0
        self.run_every(self.ping_server, "now", 30 * 60)
        self.handle_for_repeater = ""

        self.log("Completed initializing FlexMeasuresClient")

    def ping_server(self, *args):
        """ Ping function to check if server is alive """
        url = c.FM_PING_URL

        res = requests.get(url)
        if res.status_code == 200:
            if self.connection_error_counter > 0:
                # There was an error before as the counter > 0
                # So a timer must be running, but it is not needed anymore, so cancel it.
                self.cancel_timer(self.handle_for_repeater)
                self.get_app("v2g_liberty").handle_no_new_schedule("no_communication_with_fm", False)
            self.connection_error_counter = 0
        else:
            self.connection_error_counter += 1

        if self.connection_error_counter == 1:
            # A first error occurred, retry in every minute now
            self.handle_for_repeater = self.run_every(self.ping_server, "now+60", 60)
            self.log("No communication with FM! Increase tracking frequency.")
            self.get_app("v2g_liberty").handle_no_new_schedule("no_communication_with_fm", True)

    def authenticate_with_fm(self):
        """Authenticate with the FlexMeasures server and store the returned auth token.

        Hint:
        the lifetime of the token is limited, so also call this method whenever the server returns a 401 status code.
        """
        self.log(f"Authenticating with FlexMeasures on URL '{c.FM_AUTHENTICATION_URL}'.")
        url = c.FM_AUTHENTICATION_URL
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
            seconds_since_last_schedule = int((self.get_now() - self.fm_date_time_last_schedule).total_seconds())
            if seconds_since_last_schedule > self.fm_max_seconds_between_schedules:
                self.log("Retrieving previous schedule is taking too long,"
                         " assuming call got 'lost'. Getting new schedule.")
            else:
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
        if res.status_code == 303:
            new_url = res.headers.get("location")
            if new_url is not None:
                self.log(f"Redirecting from {url} to {new_url}")
                url = new_url
                res = requests.get(
                    url,
                    params=message,
                    headers={"Authorization": self.fm_token},
                )

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
                self.get_app("v2g_liberty").handle_no_new_schedule("timeouts_on_schedule", True)

            return

        self.log(f"GET schedule success: retrieved {res.status_code}")
        self.fm_busy_getting_schedule = False
        self.get_app("v2g_liberty").handle_no_new_schedule("timeouts_on_schedule", False)
        self.fm_date_time_last_schedule = self.get_now()

        schedule = res.json()
        self.log(f"Schedule {schedule}")
        # To trigger state change we add the date to the state. State change is not triggered by attributes.
        self.set_state("input_text.chargeschedule",
                       state="ChargeScheduleAvailable" + self.fm_date_time_last_schedule.isoformat(),
                       attributes=schedule)

    def trigger_schedule(self, *args, **fnc_kwargs):
        """Request a new schedule to be generated by calling the schedule triggering endpoint, while
        POSTing flex constraints.
        Return the schedule id for later retrieval of the asynchronously computed schedule.
        """

        # Prepare the SoC measurement to be sent along with the scheduling request
        current_soc_kwh = fnc_kwargs["current_soc_kwh"]
        self.log(f"trigger_schedule called with current_soc_kwh: {current_soc_kwh} kWh.")

        # Snap to sensor resolution
        soc_datetime = self.get_now()
        resolution = timedelta(minutes=c.FM_EVENT_RESOLUTION_IN_MINUTES)
        soc_datetime = time_round(soc_datetime, resolution).isoformat()

        url = self.FM_TIGGER_URL

        # AJO 2022-02-26:
        # ToDo: Getting target should be in v2g_liberty module.
        # AJO 2023-03-31:
        # ToDo: Would it be more efficient to determine the target every 15/30/60? minutes instead of at every schedule
        # Set default target_soc to 100% one week from now
        target_datetime = (time_round(self.get_now(), resolution) + timedelta(days=7))
        # By default, we assume no calendar item so no relaxation window is needed
        start_relaxation_window = target_datetime
        target_soc = c.CAR_MAX_CAPACITY_IN_KWH

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
                # Prepare for date parsing
                TZ = isodate.parse_tzinfo(self.get_timezone())
                calendar_item_start = calendar_item_start.replace(" ", "T")
                calendar_item_start = isodate.parse_datetime(calendar_item_start).astimezone(TZ)
                self.log(f"calendar_item_start: {calendar_item_start}.")
                if calendar_item_start < target_datetime:
                    # There is a relevant calendar item with a start date less than a week in the future.
                    # Set the calendar_item_start as the target for the schedule
                    target_datetime = time_round(calendar_item_start, resolution)

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
                            target_soc = round(float(found_target_soc_in_percentage) / 100 * c.CAR_MAX_CAPACITY_IN_KWH,
                                               2)
                    # ToDo: Add possibility to set target in km

                    # Prevent target_soc above max_capacity
                    if target_soc > c.CAR_MAX_CAPACITY_IN_KWH:
                        self.log(f"Target SoC from calendar too high: {target_soc}, "
                                 f"adjusted to {c.CAR_MAX_CAPACITY_IN_KWH}kWh.")
                        target_soc = c.CAR_MAX_CAPACITY_IN_KWH
                    elif target_soc < self.CAR_MIN_SOC_IN_KWH:
                        self.log(f"Target SoC from calendar too low: {target_soc}, "
                                 f"adjusted to {self.CAR_MIN_SOC_IN_KWH}kWh.")
                        target_soc = self.CAR_MIN_SOC_IN_KWH

                    # The relaxation window is the period before a calendar item where no
                    # soc_maxima should be sent to allow the schedule to reach a target higher
                    # than the CAR_MAX_SOC_IN_KWH.
                    if target_soc > self.CAR_MAX_SOC_IN_KWH:
                        window_duration = math.ceil((target_soc - self.CAR_MAX_SOC_IN_KWH) / (c.CHARGER_MAX_CHARGE_POWER / 1000) * 60) + self.WINDOW_SLACK
                        start_relaxation_window = time_round((target_datetime - timedelta(minutes=window_duration)), resolution)
                        self.log(f"Lifting the soc-maxima due to upcoming target, start_relaxation_window: {start_relaxation_window.isoformat()}.")

        rounded_now = time_round(self.get_now(), resolution)

        # This is when the target SoC cannot be reached before the calendar-item_start,
        # the start of the relaxation window would have to be in the past. We thus simply start asap: now.
        if start_relaxation_window < rounded_now:
            start_relaxation_window = rounded_now

        message = {
            "start": soc_datetime,
            "flex-model": {
                "soc-at-start": current_soc_kwh,
                "soc-unit": "kWh",
                "soc-min": self.CAR_MIN_SOC_IN_KWH,
                "soc-max": c.CAR_MAX_CAPACITY_IN_KWH,
                "soc-minima": [
                    {
                        "value": target_soc,
                        "datetime": target_datetime.isoformat(),
                    }
                ],
                "soc-maxima": [
                    {
                        "value": self.CAR_MAX_SOC_IN_KWH,
                        "datetime": dt.isoformat(),
                    } for dt in [rounded_now + x * resolution for x in range(0, (start_relaxation_window - rounded_now) // resolution)]
                ],
                "roundtrip-efficiency": c.CHARGER_PLUS_CAR_ROUNDTRIP_EFFICIENCY,
                "power-capacity": str(c.CHARGER_MAX_CHARGE_POWER) + "W"
            },
            "flex-context": self.FM_OPTIMISATION_CONTEXT,
        }

        res = requests.post(
            url,
            json=message,
            headers={"Authorization": self.fm_token},
        )

        tmp = str(message)
        self.log(f"Trigger_schedule on url '{url}', with message: '{tmp[0:275]} . . . . . {tmp[-275:]}'.")

        self.check_deprecation_and_sunset(url, res)

        if res.status_code == 401:
            self.log_failed_response(res, url)
            self.try_solve_authentication_error(res, url, self.trigger_schedule, *args, **fnc_kwargs)
            return None

        schedule_id = None
        if res.status_code == 200:
            schedule_id = res.json()["schedule"]  # can still be None in case something went wong

        if schedule_id is None:
            self.log_failed_response(res, url)
            self.get_app("v2g_liberty").handle_no_new_schedule("timeouts_on_schedule", True)
            return None

        self.log(f"Successfully triggered schedule. Schedule id: {schedule_id}")
        self.get_app("v2g_liberty").handle_no_new_schedule("timeouts_on_schedule", False)
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
