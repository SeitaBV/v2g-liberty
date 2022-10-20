from datetime import datetime, timedelta
import time
import json
import math
import re
import requests
from typing import AsyncGenerator, List, Optional
import appdaemon.plugins.hass.hassapi as hass

from wallbox_client import WallboxModbusMixin


# ToDo:
# When SoC is below 20% (forced charging is in place), this time period should also be regarded as unavailable.
# Start times of Posting data seem incorrect, it is recommended to research them.

class SetFMdata(hass.Hass, WallboxModbusMixin):
    """
    App accounts and sends results to FM hourly for intervals @ resolution, eg. 1/12th of an hour:
    + Charged power in kWh
    + Availability of car and charger for automatic charging (% of time)
    + SoC of the car battery

    App receives power changes at irregular intervals. Usually about 15 seconds apart but sometimes hours.
    We want to create power usages in regular intervals of e.g. 1/12th of an hour.
    Power changes: |    |    |  |    |     |               | (called periods)
    Intervals:        |        |        |        |        |        |

    The availability is how much of the time of an interval (again 1/12th of an hour or 5min)
    the charger and car where available for automatic (dis-)charging.

    The State of Charge is a % that is a momentary measure, no calculations are performed as
    the SoC does not change very often in an interval.
    """
    # Access token for FM
    fm_token: str

    # At what time intervals is the chargepower stored (minutes)
    readings_resolution: int
    hourly_power_readings_since: datetime
    hourly_availability_readings_since: datetime
    hourly_soc_readings_since: datetime

    # Variables to help calculate average power over the last readings_resolution minutes
    current_power_since: datetime
    current_power: int
    # Duration between two changes in power in seconds
    power_period_duration: int
    period_power_x_duration: int
    # Holds the weighted_averaged power readings of the last hour until sent to backend.
    power_readings: List[AsyncGenerator]

    # Total seconds that charger and car have been available in the current hour.
    current_availability: bool
    availability_duration_in_current_interval: int
    un_availability_duration_in_current_interval: int
    current_availability_since: datetime
    availability_readings: List[AsyncGenerator]

    # State of Charge (SoC) of connected car battery. If not connected set to -1.
    soc_readings: List[AsyncGenerator]
    connected_car_soc: int

    
    def initialize(self):
        self.readings_resolution = self.args["fm_chargepower_resolution_in_minutes"]
        self.client = self.configure_charger_client()
        #hjhh
        local_now = self.get_now()

        # Power related initialization
        self.current_power_since = local_now
        self.current_power = 0
        self.power_period_duration = 0
        self.period_power_x_duration = 0
        self.power_readings = []

        self.listen_state(self.handle_charge_power_change, "sensor.charger_real_charging_power", attribute="all")

        # Availability related
        self.availability_duration_in_current_interval = 0
        self.un_availability_duration_in_current_interval = 0
        self.availability_readings = []
        self.current_availability = self.is_available()
        self.current_availability_since = local_now
        self.record_availability(True)

        # SoC related
        self.connected_car_soc = None
        self.soc_readings = []

        self.listen_state(self.handle_charger_state_change, "sensor.charger_charger_state", attribute="all")
        self.listen_state(self.handle_charge_mode_change, "input_select.charge_mode", attribute="all")
        self.listen_state(self.handle_soc_change, "sensor.charger_connected_car_state_of_charge", attribute="all")

        # Most likely this first run will not be a complete cycle (for resolution and hour)
        # so this is ignored, that's why the start of the power_readings is set at the
        # end of the initial period conclusion
        resolution = timedelta(minutes=self.readings_resolution)
        runtime = time_ceil(local_now, resolution)
        # self.log(f"Runtime = {runtime.isoformat()}.")
        self.hourly_power_readings_since = runtime
        self.hourly_availability_readings_since = runtime
        self.hourly_soc_readings_since = runtime
        self.run_every(self.conclude_interval, runtime, self.readings_resolution * 60)

        # Reuse variables for starting hourly "send data to FM"
        resolution = timedelta(minutes=60)
        runtime = time_ceil(runtime, resolution)
        self.run_hourly(self.try_send_data, runtime)
        self.log("setFMdata, done setting up.")

    def handle_soc_change(self, entity, attribute, old, new, kwargs):
        reported_soc = new["state"]
        self.log(f"Handle_soc_change called with raw SoC: {reported_soc}")
        if isinstance(reported_soc, str):
            if not reported_soc.isnumeric():
                # Sometimes the charger returns "Unknown" or "Undefined" or "Unavailable"
                # self.log("SoC set to None")
                self.connected_car_soc = None
                return
            reported_soc = int(round(float(reported_soc), 0))

        if reported_soc == 0:
            # self.log("SoC set to None")
            self.connected_car_soc = None
            return

        self.log(f"Processed reported SoC, self.connected_car_soc is now set to: {reported_soc}%.")
        self.connected_car_soc = reported_soc
        return

    def handle_charge_mode_change(self, entity, attribute, old, new, kwargs):
        old = old['state']
        new = new['state']
        # self.log(f"Charge_mode changed from '{ old }' to '{ new }'.")
        self.record_availability()


    def handle_charger_state_change(self, entity, attribute, old, new, kwargs):
        old = old['state']
        new = new['state']
        if old == "unavailable" or new == "unavailable":
            # Ignore state changes related to unavailable
            return
        # self.log(f"Charger_state changed from '{ old }' to '{ new }'.")
        self.record_availability()


    def record_availability(self, conclude_interval = False):
        # Called at chargemode_change and charger_status_change
        # Record (non_)availability durations of time in current interval.
        # Use conclude_interval to conclude an interval (without chening the availablity)
        # TODO: How to take an upcomming calendar item in to account?

        if self.current_availability != self.is_available() or conclude_interval:
            local_now = self.get_now()
            duration = int((local_now - self.current_availability_since).total_seconds() * 1000)

            if conclude_interval:
                self.log("Conclude interval for availability")
            else:
                self.log("Availability changed, process it.")

            if self.current_availability:
                self.availability_duration_in_current_interval += duration
            else:
                self.un_availability_duration_in_current_interval += duration

            if conclude_interval is False:
                self.current_availability = not self.current_availability

            self.log(f"Availability: {self.current_availability}. Last period, duration: {duration}, since: {self.current_availability_since.isoformat()}. This interval so far, un_/availability: {self.un_availability_duration_in_current_interval}/{self.availability_duration_in_current_interval} ms.")
            self.current_availability_since = local_now

        else:
            self.log("Availability not changed: do nothing")
            return
        return


    def handle_charge_power_change(self, entity, attribute, old, new, kwargs):
        """Handle a state change in the power sensor."""
        power = new['state']
        if power == "unavailable":
            # Ignore a state change to 'unavailable' 
            return
        power = int(float(power))
        self.proces_power_change(power)

    def proces_power_change(self, power):
        """Keep track of updated power changes within a regular interval."""
        local_now = self.get_now()
        duration = int((local_now - self.current_power_since).total_seconds())
        self.period_power_x_duration += (duration * power)
        self.power_period_duration += duration
        self.current_power_since = local_now
        self.current_power = power

    def conclude_interval(self, *args):
        """Conclude a regular interval."""
        # Call every self.readings_resolution minutes
        self.proces_power_change(self.current_power)
        self.record_availability(True)

        # At initialise there might be an incomplete period,
        # duration must be not more than 5% smaller than readings_resolution * 60
        total_interval_duration = self.availability_duration_in_current_interval + self.un_availability_duration_in_current_interval
        if total_interval_duration > (self.readings_resolution * 60 * 0.95):
            # Power related processing
            # Conversion from Watt to MegaWatt
            average_period_power = round((self.period_power_x_duration / self.power_period_duration)/1000000, 5)
            self.power_readings.append(average_period_power)

            # Availability related processing
            self.log(f"Concluded availability interval, un_/availability was: {self.un_availability_duration_in_current_interval} / {self.availability_duration_in_current_interval} ms.")
            percentile_availability = round(100 * (self.availability_duration_in_current_interval/(total_interval_duration)), 2)
            if percentile_availability > 100.00:
                percentile_availability = 100.00
                #self.log(f"Calculated availability over 100.00%. New percentile: {percentile_availability}%.")
            self.availability_readings.append(percentile_availability)

            # SoC related processing
            # SoC does not change very quickly so we just read it at conclude time and do not do any calculation
            self.soc_readings.append(self.connected_car_soc)

            self.log(f"Conclude called. Average power in this period: {average_period_power} MW, Availability: {percentile_availability}%, SoC: {self.connected_car_soc}%.")

        else:
            self.log(f"Period duration too short: {self.power_period_duration} s, discarding this reading.")

        # Reset power values
        self.period_power_x_duration = 0
        self.power_period_duration = 0
        # Reset availability values
        self.availability_duration_in_current_interval = 0
        self.un_availability_duration_in_current_interval = 0

        return


    def try_send_data(self, *args):
        # Called every hour
        local_now = self.get_now()

        resolution = timedelta(minutes=self.readings_resolution)
        start_from = time_round(local_now, resolution)
        self.authenticate_with_fm()
        res = self.post_power_data()
        if res is True:
            self.log(f"Power data successfully sent, resetting readings")
            self.hourly_power_readings_since = start_from
            self.power_readings.clear()

        res = self.post_availability_data()
        if res is True:
            self.log(f"Availability data successfully sent, resetting readings")
            self.hourly_availability_readings_since = start_from
            self.availability_readings.clear()

        res = self.post_soc_data()
        if res is True:
            self.log(f"SoC data successfully sent, resetting readings")
            self.hourly_soc_readings_since = start_from
            self.soc_readings.clear()

        return

    def log_result(self, res, endpoint: str):
        """Log failed result for a given endpoint."""
        try:
            self.log(f"{endpoint} failed ({res.status_code}) with JSON response {res.json()}")
        except json.decoder.JSONDecodeError:
            self.log(f"{endpoint} failed ({res.status_code}) with response {res}")

    def post_soc_data(self, *args, **kwargs):
        self.log(f"post_soc_data called, soc readings so far: {self.soc_readings}")
        if len(self.soc_readings) == 0:
            self.log("List of soc readings is 0 length..")
            return False

        duration = len(self.soc_readings) * self.readings_resolution
        hours = math.floor(duration/60)
        minutes = duration - hours*60
        str_duration = "PT" + str(hours) + "H" + str(minutes) + "M"
        url = self.args["fm_data_api"] + self.args["fm_data_api_post_sensor_data"]

        message = {
            "type": "PostSensorDataRequest",
            "sensor": self.args["fm_soc_entity_address"],
            "values": self.soc_readings,
            "start": self.hourly_soc_readings_since.isoformat(),
            "duration": str_duration,
            "unit": "%"
        }
        self.log(f"Post_soc_data message: {message}")
        res = requests.post(
            url,
            json=message,
            headers={"Authorization": self.fm_token},
        )
        if res.status_code != 200:
            self.log_result(res, "PostSensorData for SoC")
            return False

        return True


    def post_availability_data(self, *args, **kwargs):
        # If self.availability_readings is empty there is nothing to send.
        if len(self.availability_readings) == 0:
            self.log("List of availability readings is 0 length..")
            return False

        # self.log(f"post_availability_data called, availability readings so far: {self.availability_readings}")

        duration = len(self.availability_readings) * self.readings_resolution
        hours = math.floor(duration/60)
        minutes = duration - hours*60
        str_duration = "PT" + str(hours) + "H" + str(minutes) + "M"

        url = self.args["fm_data_api"] + self.args["fm_data_api_post_sensor_data"]

        message = {
            "type": "PostSensorDataRequest",
            "sensor": self.args["fm_availability_entity_address"],
            "values": self.availability_readings,
            "start": self.hourly_availability_readings_since.isoformat(),
            "duration": str_duration,
            "unit": "%"
        }
        self.log(f"Post_availability_data message: {message}")
        res = requests.post(
            url,
            json=message,
            headers={"Authorization": self.fm_token},
        )
        if res.status_code != 200:
            self.log_result(res, "PostSensorData for Availability")
            return False
        return True


    def post_power_data(self, *args, **kwargs):
        # If self.power_readings is empty there is nothing to send.
        if len(self.power_readings) == 0:
            self.log("List of power readings is 0 length..")
            return False

        # self.log(f"post_power_data called, power readings so far: {self.power_readings}")

        duration = len(self.power_readings) * self.readings_resolution
        hours = math.floor(duration/60)
        minutes = duration - hours*60
        str_duration = "PT" + str(hours) + "H" + str(minutes) + "M"

        url = self.args["fm_data_api"] + self.args["fm_data_api_post_meter_data"]

        message = {
            "type": "PostMeterDataRequest",
            "connection": self.args["fm_power_entity_address"],
            "values": self.power_readings,
            "start": self.hourly_power_readings_since.isoformat() + "+01:00",
            "duration": str_duration,
            "unit": "MW"
        }
        self.log(message)
        res = requests.post(
            url,
            json=message,
            headers={"Authorization": self.fm_token},
        )
        if res.status_code != 200:
            self.log_result(res, "PostSensorData for Power")
            return False
        return True


    def is_available(self):
        # Check if car and charger are available for automatic charging.
        # TODO: How to take an upcoming calendar item in to account?

        charge_mode = self.get_state("input_select.charge_mode")
        if self.is_car_connected() and charge_mode == "Automatic":
            # self.log("is_available: returning True")
            return True
        else:
            # self.log("is_available: returning False")
            return False


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
            self.log_result(res, "requestAuthToken")
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


    def notify(self, log_text):
        # Sets a message in helper entity which is monitored by an automation to notify user.
        self.set_textvalue("input_text.epex_log", log_text)
        return



def time_mod(time, delta, epoch=None):
    """From https://stackoverflow.com/a/57877961/13775459"""
    if epoch is None:
        epoch = datetime(1970, 1, 1, tzinfo=time.tzinfo)
    return (time - epoch) % delta

def time_round(time, delta, epoch=None):
    """From https://stackoverflow.com/a/57877961/13775459"""
    mod = time_mod(time, delta, epoch)
    if mod < (delta / 2):
       return time - mod
    return time + (delta - mod)

def time_ceil(time, delta, epoch=None):
    """From https://stackoverflow.com/a/57877961/13775459"""
    mod = time_mod(time, delta, epoch)
    if mod:
        return time + (delta - mod)
    return time
