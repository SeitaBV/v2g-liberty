from datetime import datetime, timedelta
import json
import pytz
import re
import requests
import time
from typing import AsyncGenerator, List

import appdaemon.plugins.hass.hassapi as hass
import isodate
from pyModbusTCP.client import ModbusClient


class FlexMeasuresWallboxQuasar(hass.Hass):
    client: ModbusClient
    fm_token: str
    udi_event_id: int
    scheduling_timer_handles: List[AsyncGenerator]
    previous_control_value: str
    previous_setpoint_type: str

    def initialize(self):
        self.log("Initializing FlexMeasures integration for the Wallbox Quasar")
        self.configure_client()
        self.authenticate_with_fm()
        self.listen_state(self.update_charge_mode, "input_select.charge_mode", attribute="all")
        self.listen_state(self.post_udi_event, "input_number.car_state_of_charge_wh", attribute="all")
        self.listen_state(self.schedule_charge_point, "input_text.chargeschedule", attribute="events")
        self.scheduling_timer_handles = []

        # To keep the charging process reliable it is needed for now to keep control within this app
        # So setting control = remote (= from this app and not from Wallbox app/Charger)
        # Only exception is the charge_mode Off.
        self.set_control("remote")

        # For the same reason it is needed to set the "start charging on GUN-connected" to disable
        self.set_charger_start_charging_on_ev_gun_connected("disable")

        self.log("Done setting up")

    def schedule_charge_point(self, entity, attribute, old, new, kwargs):
        """Send a new control signal (specifically, a charging rate) to the Charge Point,
        and schedule the next moment to send a control signal.
        """
        schedule = self.get_state("input_text.chargeschedule", attribute="all")
        
        if schedule["state"] == "DisconnectNow":
            self.log(f"DisconnectNow requested")
            # Tell charger to stop charging and set control to user 
            self.set_charger_action("stop")
            self.set_control("user")
            return
        else:
            schedule = schedule["attributes"]

        self.log(schedule)

        values = schedule["values"]
        duration = isodate.parse_duration(schedule["duration"])
        resolution = duration / len(values)
        start = isodate.parse_datetime(schedule["start"])

        # Cancel previous scheduling timers
        for h in self.scheduling_timer_handles:
            self.cancel_timer(h)

        # Create new scheduling timers, to send a control signal for each value
        handles = []
        now = self.get_now()
        for i, value in enumerate(values):
            t = start + i * resolution
            if t > now:
                h = self.run_at(self.send_control_signal, t, charge_rate=value * 1000)  # convert from MW to kW
                handles.append(h)
            else:
                self.log(f"Cannot time a charging scheduling in the past, specifically, at {t}")
        self.scheduling_timer_handles = handles

    def send_control_signal(self, kwargs: dict, *args, **fnc_kwargs):
        """
        The kwargs dict should contain a "charge_rate" key with a value in kW.
        """
        charge_rate = round(kwargs[
                                "charge_rate"] * 1000)  # todo: convert total power to power per phase (but multiplying with 3**0.5 doesn't seem to work out exactly)
        self.log(f"Sending control signal to Wallbox Quasar: set charge rate to {charge_rate / 1000} kW")
        self.set_power_setpoint(charge_rate)

    def set_power_setpoint(self, charge_rate: int):
        register = self.args["wallbox_register_set_power_setpoint"]
        res = self.client.write_single_register(register, charge_rate)
        if res is not True:
            self.log(f"Failed to set charge rate to {charge_rate}. Charge Point responded with: {res}")

    def set_current_setpoint(self, charge_rate: int):
        max_current = self.args["wallbox_max_charging_current"]
        if charge_rate > max_current:
            self.log(f"Requested charge rate {charge_rate}A too high. Changed charge rate to maximum: {max_current}A.")
            charge_rate = max_current

        # also check negative values
        elif abs(charge_rate) > max_current:
            self.log(
                f"Requested discharge rate {charge_rate}A too high. Changed discharge rate to maximum: {max_current}A.")
            charge_rate = -max_current

        self.set_setpoint_type("current")

        register = self.args["wallbox_register_set_current_setpoint"]
        res = self.client.write_single_register(register, charge_rate)
        if res is not True:
            self.log(f"Failed to set current charge rate to {charge_rate}. Charge Point responded with: {res}")
        else:
            self.log(f"Charge rate set to {charge_rate}A successfully.")

    def set_charger_start_charging_on_ev_gun_connected(self, setting: str):
        if setting == "enable":
            value = self.args["wallbox_register_set_start_charging_on_ev_gun_connected_value_enabled"]
        elif setting == "disable":
            value = self.args["wallbox_register_set_start_charging_on_ev_gun_connected_value_disabled"]
        else:
            raise ValueError(f"Unknown setting for 'start charging on EV-Gun connected': {setting}")

        # Set charge on EV-Gun connect to enable/disable
        register = self.args["wallbox_register_set_start_charging_on_ev_gun_connected"]
        res = self.client.write_single_register(register, value)
        if res is not True:
            self.log(f"Failed to set 'start charging on EV-Gun connected' to {action}. Charge Point responded with: {res}")
        else:
            self.log(f"Set 'start charging on EV-Gun connected' to {action} succeeded")

    def set_charger_action(self, action: str):
        if action == "start":
            value = self.args["wallbox_register_set_action_value_start_charging"]
        elif action == "stop":
            value = self.args["wallbox_register_set_action_value_stop_charging"]
        else:
            raise ValueError(f"Unknown option for action '{action}'")

        # Set action to start/stop charging
        register = self.args["wallbox_register_set_action"]
        res = self.client.write_single_register(register, value)
        if res is not True:
            self.log(f"Failed to set action to {action}. Charge Point responded with: {res}")
        else:
            self.log(f"Charger {action} succeeded")

    def set_control(self, user_or_remote: str):
        register = self.args["wallbox_register_set_control"]
        
        # Remember previous control mode
        previous_control_value = self.client.read_holding_registers(register)[0]
        if previous_control_value == self.args["wallbox_register_set_control_value_user"]:
            self.previous_control = "user"
        elif previous_control_value == self.args["wallbox_register_set_control_value_remote"]:
            self.previous_control = "remote"
        else:
            raise ValueError(f"unknown previous control value: {previous_control_value}")
        
        # Set new control mode
        if user_or_remote == "user":
            res = self.client.write_single_register(register, self.args["wallbox_register_set_control_value_user"])
        elif user_or_remote == "remote":
            res = self.client.write_single_register(register, self.args["wallbox_register_set_control_value_remote"])
        else:
            raise ValueError(f"unknown option for user_or_remote: {user_or_remote}")
        if res is not True:
            self.log(f"Failed to set control to {user_or_remote}. Charge Point responded with: {res}")

    def set_setpoint_type(self, current_or_power_by_phase: str):
        register = self.args["wallbox_register_set_setpoint_type"]
        
        # Remember previous setpoint type
        previous_setpoint_type_value = self.client.read_holding_registers(register)[0]
        if previous_setpoint_type_value == self.args["wallbox_register_set_setpoint_type_value_current"]:
            self.previous_setpoint_type = "current"
        elif previous_setpoint_type_value == self.args["wallbox_register_set_setpoint_type_value_power_by_phase"]:
            self.previous_setpoint_type = "power_by_phase"
        else:
            raise ValueError(f"unknown previous setpoint type value: {previous_control_value}")
        
        # Set new setpoint type
        if current_or_power_by_phase == "current":
            res = self.client.write_single_register(register, self.args["wallbox_register_set_setpoint_type_value_current"])
        elif current_or_power_by_phase == "power_by_phase":
            res = self.client.write_single_register(register, self.args["wallbox_register_set_setpoint_type_value_power_by_phase"])
        else:
            raise ValueError(f"unknown option for current_or_power_by_phase: {current_or_power_by_phase}")
        if not res is True:
            self.log(f"Failed to set setpoint type to {current_or_power_by_phase}. Charge Point responded with: {res}")

    def get_device_message(self, kwargs, *args, **fnc_kwargs):
        """GET a device message based on the most recent UDI event,
        and store it as a charging schedule.

        This function uses self.udi_event_id as the most recent UDI event.
        """
        url = self.args["fm_api"] + "/" + self.args["fm_api_version"] + "/getDeviceMessage"
        udi_event_id = self.udi_event_id
        message = {
            "type": "GetDeviceMessageRequest",
            "event": self.args["fm_quasar_entity_address"] + ":" + str(udi_event_id) + ":soc",
        }
        res = requests.get(
            url,
            params=message,
            headers={"Authorization": self.fm_token},
        )
        self.handle_response_errors(message, res, "GET device message", self.get_device_message, kwargs, *args,
                                    **fnc_kwargs)
        if res.json().get("status", None) == "UNKNOWN_SCHEDULE":
            s = self.args["delay_for_reattempts_to_retrieve_device_message"]
            self.log("kwargs")
            self.log(kwargs)
            attempts_left = kwargs.get("attempts_left",
                                       self.args["max_number_of_reattempts_to_retrieve_device_message"])
            if attempts_left >= 1:
                self.log(f"Reattempting to get device message in {s} seconds (attempts left: {attempts_left})")
                self.run_in(self.get_device_message, delay=int(s), attempts_left=attempts_left - 1)
            else:
                self.log("Device message cannot be retrieved. Any previous charging schedule will keep being followed.")

        schedule = res.json()
        self.set_state("input_text.chargeschedule", state="ChargeScheduleAvailable", attributes=schedule)

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

    def post_udi_event(self, *args, **fnc_kwargs):
        """POST a UDI event and keep around the UDI event id for later retrieval of a device message.

        This function is meant to be used as callback for self.listen_state on the following:
        - SOC updates (use an SOC measuring entity)
        - calendar updates
        - charger state updates
        
        For example:

            self.listen_state(self.post_udi_event, "input_number.car_state_of_charge_wh", attribute="all")

        """
        soc_entity = self.get_state("input_number.car_state_of_charge_wh", attribute="all")

        if self.args.get("reschedule_on_soc_changes_only", True) and soc_entity["last_changed"] != soc_entity["last_updated"]:
            # A state update but not a state change
            # https://data.home-assistant.io/docs/states/
            return
        soc = float(soc_entity["state"]) / 1000  # to kWh
        soc_datetime = soc_entity["last_changed"]

        # Snap to sensor resolution
        soc_datetime = isodate.parse_datetime(soc_datetime)
        resolution = timedelta(minutes=self.args["fm_quasar_soc_event_resolution_in_minutes"])
        soc_datetime = time_round(soc_datetime, resolution).isoformat()

        url = self.args["fm_api"] + "/" + self.args["fm_api_version"] + "/postUdiEvent"
        udi_event_id = int(time.time())  # we use this as our UDI event id
        self.log(f"Posting UDI event {udi_event_id} to {url}")

        # Retrieve target SOC
        car_reservation = self.get_state(self.args["fm_car_reservation_calendar"], attribute="all")
        if car_reservation is None or "description" not in car_reservation["attributes"]:
            # Set default target to 100% one week from now
            target = self.args["fm_car_max_soc_in_kwh"]
            target_datetime = (time_round(datetime.now(tz=pytz.utc), resolution) + timedelta(days=7)).isoformat()
        else:
            target = search_for_kwh_target(car_reservation["attributes"]["description"])
            if target is None:
                target = self.args["fm_car_max_soc_in_kwh"]
            target_datetime = isodate.parse_datetime(car_reservation["attributes"]["start_time"].replace(" ", "T")).astimezone(pytz.timezone("Europe/Amsterdam")).isoformat()
            target_datetime = time_round(isodate.parse_datetime(target_datetime), resolution).isoformat()

        message = {
            "type": "PostUdiEventRequest",
            "event": self.args["fm_quasar_entity_address"] + ":" + str(udi_event_id) + ":soc-with-targets",  # todo: relay flow constraints with new UDI event type ":soc-with-target-and-flow-constraints"
            "value": soc,
            "unit": "kWh",
            "datetime": soc_datetime,
            "targets": [
                {
                    "value": target,
                    "datetime": target_datetime,
                }
            ]
        }
        res = requests.post(
            url,
            json=message,
            headers={"Authorization": self.fm_token},
        )
        if res.status_code != 200:
            self.handle_response_errors(message, res, "POST UDI event", self.post_udi_event, *args, **fnc_kwargs)
            return
        self.udi_event_id = udi_event_id
        s = self.args["delay_for_initial_attempts_to_retrieve_device_message"]
        self.log(f"Attempting to get device message in {s} seconds")
        self.run_in(self.get_device_message, delay=int(s))

    def handle_response_errors(self, message, res, description, fnc, *args, **fnc_kwargs):
        if fnc_kwargs.get("retry_auth_once", True) and res.status_code == 401:
            self.log(
                f"Failed to {description} on authorization (possibly the token expired); attempting to reauthenticate once")
            self.authenticate_with_fm()
            fnc_kwargs["retry_auth_once"] = False
            fnc(*args, **fnc_kwargs)
        else:
            self.log(f"Failed to {description} (status {res.status_code}): {res.json()} as response to {message}")

    def configure_client(self):
        # Configuration
        host = self.args["wallbox_host"]
        port = self.args["wallbox_port"]
        self.log(f"Configuring Modbus client at {host}:{port}")
        self.client = ModbusClient(
            host=host,
            port=port,
            auto_open=True,
            auto_close=True,
        )

    def update_charge_mode(self, entity, attribute, old, new, kwargs):
        # todo: better remember previous setpoints and convert back to those
        if new["state"] == "Automatic":
            self.log("Setting up Charge Point to accept setpoints by remote (in W).")
            self.set_control("remote")
            self.set_charger_start_charging_on_ev_gun_connected("disable")
            self.set_setpoint_type("power_by_phase")
        elif new["state"] == "Max boost now":
            self.set_control("remote")
            self.set_charger_start_charging_on_ev_gun_connected("disable")
            # Prevent overloading of phase
            # If powerboost is available the charger will handle preventing overloading
            max_current = self.args["wallbox_max_charging_current"]
            self.set_current_setpoint(max_current)
            self.set_charger_action("start")
        elif new["state"] == "Off":
            self.set_charger_action("stop")
            self.set_charger_start_charging_on_ev_gun_connected("enable")
            self.set_control("user")


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
