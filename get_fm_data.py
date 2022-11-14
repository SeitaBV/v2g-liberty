from datetime import datetime, timedelta
import json
import pytz
import re
import requests
import time
from typing import AsyncGenerator, List, Optional

import appdaemon.plugins.hass.hassapi as hass
import isodate


class data_importer(hass.Hass):
    fm_token: str
    first_try_time: str
    second_try_time: str

    def initialize(self):
        """Daily get epex prices purely for display in the UI.

        Try to get EPEX price data from the FM server on a daily basis.
        Normally the prices are avaialable around 14:35.
        When this fails retry at 18:30. These times are related to the
        attempts in the server for retrieving EPEX proce data.

        The retrieved data is written to the HA input_text.epex_prices,
        HA handles this to render the price data in the UI (chart).
        """

        self.log(f"get_fm_data, start setup")

        self.first_try_time = "14:35:29"
        self.second_try_time = "18:30:48"

        # Should normally be available just after 13:00 when data can be
        # retrieved from its original source (ENTSO-E) but sometimes there
        # is a delay of several hours.
        handle = self.run_daily(self.daily_kickoff, self.first_try_time)

        # At init also run this as (re-) start is not always around self.first_try_time
        self.get_epex_prices()

        self.log(f"get_fm_data, done setting up: Start checking daily from {self.first_try_time} for new EPEX prices in FM.")


    def notify_user(self, message: str):
        """ Utility function to notify the user
        """
        self.notify(message, title="V2G Liberty")


    def daily_kickoff(self, *args):
        """ This sets off the daily routine to check for new prices.

        The attemps for today are reset.
        """

        self.log("FMdata, daily kickoff: start checking new EPEX prices in FM.")
        self.attempts_today = 0
        self.get_epex_prices()

    def log_failed_response(self, res, endpoint: str):
        """Log failed response for a given endpoint."""
        try:
            self.log(f"{endpoint} failed ({res.status_code}) with JSON response {res.json()}")
        except json.decoder.JSONDecodeError:
            self.log(f"{endpoint} failed ({res.status_code}) with response {res}")

    def get_epex_prices(self, *args, **kwargs):
        """ Communicate with FM server and check the results.

        Request prices from the server
        Make prices available in HA by setting them in input_text.epex_prices
        Notify user if there will be negative prices for next day
        """
        self.log(f"FMdata, get_epex_prices.")

        self.authenticate_with_fm()
        now = self.get_now()
        # Getting prices since start of yesterday so that user can look back a little furter than just current window.
        startEPEX = str((now +timedelta(days=-1)).date())

        url = self.args["fm_data_api"] + self.args["fm_data_api_epex"]
        url_params = {
            "event_starts_after": startEPEX + "T00:00:00.000Z",
        }
        res = requests.get(
            url,
            params=url_params,
            headers={"Authorization": self.fm_token},
        )

        # Authorisation error, retry authoristion.
        if res.status_code == 401:
            self.handle_response_errors(message, res, "get EPEX prices", self.get_epex_prices, *args, **kwargs)
            return

        if res.status_code != 200:
            self.log_failed_response(res, "Get FM EPEX data")

            # Only retry once at second_try_time.
            if self.now_is_between(self.first_try_time, self.second_try_time):
                self.log(f"Retry at {self.second_try_time}.")
                self.run_at(self.get_epex_prices, self.second_try_time)
            else:
                self.log(f"Retry tomorrow.")
                self.notify_user("Getting EPEX price data failed, retry tomorrow.")
            return

        prices = res.json()

        # From FM format (€/MWh) to user desired format (€ct/kWh) 
        # = * 100/1000 = 1/10. Also include VAT
        VAT = float(self.args["VAT"])
        conversion = 1/10 * VAT
        # For NL electricity is a markup for transport and sustainability
        markup = float(self.args["markup_per_kWh"])
        epex_price_points = []
        has_negative_prices = False
        for price in prices:
            data_point = {}
            data_point['time'] = datetime.fromtimestamp(price['event_start']/1000).isoformat()
            data_point['price'] = round((price['event_value'] * conversion) + markup, 2)
            if data_point['price'] < 0:
                has_negative_prices = True
            epex_price_points.append(data_point)

        # To make sure HA considers this as new info a datetime is added
        new_state = "APEX prices collected at " + now.isoformat()
        result = {}
        result['records'] = epex_price_points
        self.set_state("input_text.epex_prices", state=new_state, attributes=result)

        # FM returns all the prices it has, sometimes it has not retrieved new
        # prices yet, than it communicates the prices it does have.
        date_latest_price = datetime.fromtimestamp(prices[-1].get('event_start')/1000).isoformat()
        date_tomorrow = (now + timedelta(days=1)).isoformat()
        if date_latest_price < date_tomorrow:
            self.log(f"FM EPEX prices seem not renewed yet, latest price at: {date_latest_price}, Retry at {self.second_try_time}.")
            self.run_at(self.get_epex_prices, self.second_try_time)
        else:
            if has_negative_prices:
                self.notify_user("Negative electricity prices for tomorrow. Consider to check times in the app to optimising electricity usage.")
            self.attempts_today = 0
            self.log(f"FM EPEX prices successfully retrieved. Latest price at: {date_latest_price}.")


    def authenticate_with_fm(self):
        """Authenticate with the FlexMeasures server and store the returned auth token.
        Hint: the lifetime of the token is limited, so also call this method whenever the server returns a 401 status code.
        """
        self.log("Authenticating with FlexMeasures")
        res = requests.post(
            self.args["fm_data_api"] + "requestAuthToken",
            json=dict(
                email=self.args["fm_data_user_email"],
                password=self.args["fm_data_user_password"],
            ),
        )
        if not res.status_code == 200:
            self.log_failed_response(res, "requestAuthToken")
        self.fm_token = res.json()["auth_token"]

    def handle_response_errors(self, message, res, description, fnc, *args, **fnc_kwargs):
        if fnc_kwargs.get("retry_auth_once", True) and res.status_code == 401:
            self.log(
                f"Failed to {description} on authorization (possibly the token expired); attempting to reauthenticate once")
            self.authenticate_with_fm()
            fnc_kwargs["retry_auth_once"] = False
            fnc(*args, **fnc_kwargs)
        else:
            self.log(f"Failed to {description} (status {res.status_code}): {res} as response to {message}")
            self.log(f"Failed to {description} (status {res.status_code}): {res.json()} as response to {message}")
