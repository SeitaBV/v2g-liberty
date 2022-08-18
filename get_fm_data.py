from datetime import datetime, timedelta
import json
import pytz
import re
import requests
import time
from typing import AsyncGenerator, List, Optional

import appdaemon.plugins.hass.hassapi as hass
import isodate


class FMdata(hass.Hass):
    fm_token: str
    attempts_today: int
    max_attempts: int

    def initialize(self):
        """Daily get epex prices purely for display in the UI.

        Try to get EPEX price data from the FM server on a daily basis.
        Normally the prices are avaialable around 13:30. But sometimes
        this is later, so we retry several time with a growing gap
        inbetween.
        The retrieved data is written to the HA input_text.apex_prices
        (should be renamed to epex_prices), HA handles this to render
        this data in the UI (chart).
        """

        self.log(f"get_fm_data, start setup")

        # Counter for number of retries
        self.attempts_today = 0

        # Maximum number of retries per day
        self.max_attempts = 12

        # Should normally be available just after 13:00 when data can be
        # retrieved from it's original source -Entsoe- but sometimes there
        # is a delay of several hours.
        dailyKickoffTime = "14:00:41"
        handle = self.run_daily(self.daily_kickoff, dailyKickoffTime)

        # At init also run this as (re-) start is not always around dailyKickoffTime
        # TODO: Does this not sometimes start two threads parallel??
        self.get_epex_prices()

        self.log(f"get_fm_data, done setting up: Start checking daily from {dailyKickoffTime} for new EPEX prices in FM.")


    def notify(self, log_text: str):
        """ Utility function to notify the user

        Sets a message in helper entity which is monitored by an automation
        to notify user. This is more straight forward than the offcial
        aapdaemon notify.

        TODO: Not sure why i called it log_text and not just message.
        Same for naming of the input_text...
        """

        self.set_textvalue("input_text.epex_log", log_text)
        return


    def daily_kickoff(self, *args):
        """ This sets off the daily routine to check for new prices.

        The attemps for today are reset.
        """

        self.log("FMdata, daily kickoff: start checking new EPEX prices in FM.")
        self.attempts_today = 0
        self.get_epex_prices()


    def get_epex_prices(self, *args, **kwargs):
        """ Communicate with FM server and check the results.

        Request prices from the server
        Make prices available in HA by setting them in input_text.apex_prices
        Notify user if there will be negative prices for next day
        """

        # A max_number of retries (with a growing time gap) is used as it seems
        # unlikely the data will become available after this period.
        if self.attempts_today > self.max_attempts:
            self.notify(f"Failed to get EPEX prices for {str((self.get_now() + timedelta(days=+1)).date())} from FM. Attempted ({self.attempts_today}) times today, wait for retry till tomorrow.")
            return
        self.log(f"FMdata, get_epex_prices, attempt {self.attempts_today}.")

        self.authenticate_with_fm()
        now = self.get_now()
        startEPEX = str((now +timedelta(days=-1)).date())
        #startEPEX = "2021-12-13"

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
        # Do not count authoristion errors int he attempts.
        self.attempts_today += 1

        # A bit prematurely calculate a delay in seconds in which to call
        # this  function again incase something goes wrong.
        # The delays total to 11h30m with max 12 attempts.
        delay = (5 + self.attempts_today * 8) * 60

        if res.status_code != 200:
            self.log(f"Get FM EPEX data failed with status_code: {res.status_code}, full response was {res.json()}. Retry in {delay} minutes.")
            self.run_in(self.get_epex_prices, delay)
            return

        prices = res.json()

        # We might get older data, but if the UI has no data yet it is still
        # nice to show this older data anayway, so process it.

        # Not so usefull checks??
        # epex_entity = self.get_state("input_text.apex_prices", attribute='records')
        # if epex_entity != None:
        #     #There are records already no need to process data
        #     return
        # self.log("Eventhough prices are not new, there are currently no prices registered at all (due to restart?), so process data anyway.")

        # From FM format (€/MWh) to user desired format (€ct/kWh) 
        # = * 100/1000 = 1/10. Also include VAT
        VAT = float(self.args["VAT_NL"])
        conversion = 1/10 * VAT
        # For NL electricity is a markup for sustainability 
        markup = float(self.args["markup_per_kWh_NL"])
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
        self.set_state("input_text.apex_prices", state=new_state, attributes=result)

        # FM returns all the prices it has, sometimes it has not retreived new
        # prices yet, than it communicates the prices it does have.
        date_latest_price = datetime.fromtimestamp(prices[-1].get('event_start')/1000).isoformat()
        date_tomorrow = (now + timedelta(days=1)).isoformat()
        if date_latest_price < date_tomorrow:
            self.log(f"FM Epex prices seem not renewed yet, latest price at: {date_latest_price}, retry in {delay} minutes.")
            self.run_in(self.get_epex_prices, delay)
        else:
            if has_negative_prices == True:
                self.notify("Negative electricity prices for tomorrow. Consider"
                " to check times in the app to optimising electricity usage.")
            self.attempts_today = 0
            self.log(f"FM Epex prices succesfully retrieved. Latest price at: {date_latest_price}.")
        
        return


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
            self.log(f"Authentication failed with response {res.json()}")
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
