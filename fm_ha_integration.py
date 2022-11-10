from datetime import datetime, timedelta
from itertools import accumulate
import time
import pytz
from typing import AsyncGenerator, List

import appdaemon.plugins.hass.hassapi as hass
import isodate

from wallbox_client import WallboxModbusMixin


class FlexMeasuresWallboxQuasar(hass.Hass, WallboxModbusMixin):
    """ This class manages the communication with the Wallbox Quasar charger and
    the FlexMeasures platform (which delivers the charging schedules).
    """

    # Utility variables for preventing a frozen app. Call set_next_action at least every x seconds
    timer_handle_set_next_action: str  # ToDo: Should be a general object instead of string
    call_next_action_atleast_every: int
    scheduling_timer_handles: List[AsyncGenerator]

    # A SoC of 0 means: unknown/car not connected.
    connected_car_soc: int
    # Variable to store charger_state for comparison for change
    current_charger_state: int
    in_boost_to_reach_min_soc: bool

    # To keep track of duration of charger in error state.
    charger_in_error_since: datetime
    #initially charger_in_error_since is set to this date reference.
    #If charger_in_error_since is not equal to this date we know timeing has started.
    date_reference: datetime

    # Ignore soc changes and charger_state changes.
    try_get_new_soc_in_process: bool

    def initialize(self):
        self.log("Initializing FlexMeasures integration for the Wallbox Quasar")
        self.in_boost_to_reach_min_soc = False
        self.try_get_new_soc_in_process = False
        self.call_next_action_atleast_every = 15 * 60
        self.timer_handle_set_next_action = ""
        self.connected_car_soc = 0
        # Force change event at initialisation
        self.current_charger_state = -1

        # For checking how long the charger has been in error
        self.date_reference = datetime(2000, 1, 1)
        self.charger_in_error_since = self.date_reference

        self.client = self.configure_charger_client()
        self.log_errors()
        self.get_app("flexmeasures-client").authenticate_with_fm()

        self.listen_state(self.update_charge_mode, "input_select.charge_mode", attribute="all")
        self.listen_state(self.handle_charger_state_change, "sensor.charger_charger_state", attribute="all")

        self.listen_state(self.handle_soc_change, "sensor.charger_connected_car_state_of_charge", attribute="all")
        self.listen_state(self.handle_calendar_change, self.args["fm_car_reservation_calendar"], attribute="all")
        #Not firing??
        self.listen_state(self.schedule_charge_point, "input_text.chargeschedule", attribute="all")
        self.scheduling_timer_handles = []

        if self.is_car_connected():
            self.log("Car is connected. Trying to get a reliable SoC reading.")
            self.try_get_new_soc()

        # When to ask FlexMeasures for a new charging schedule is determined by the charge mode
        self.set_next_action()  # on initializing the app
        if self.in_boost_to_reach_min_soc:

            # FNC0816
            # Test whether restarting the app executes boost mode when boost mode is needed (below 20% SoC)
            # Executing self.set_next_action() once may not do it, and executing it twice may be needed (we are not sure why yet)

            # if we went into boost mode, actually execute boost mode
            self.log("actually execute boost mode")
            # self.cancel_charging_timers()
            # self.start_max_charge_now()
            self.set_next_action()

        self.log("Done setting up")

    # FNC0816
    # Timers live with the app, so that part should not be needed
    # Test whether it still makes sense to stop the charger and give control back to the user
    # def terminate(self):
    #     """Stop charging/discharging and give charger control back to the user."""
    #     self.log("Closing down the app. Stopping the charger and setting mode to user control. Bye!")
    #     # todo: cancel timers?
    #     self.set_power_setpoint(0)  # this will also stop the charger.
    #     self.set_charger_control("give")
    #     self.stop_app(self.name)  # todo: needed?
    #     raise RuntimeError("")  # todo: needed?

    def handle_calendar_change(self, *args, **fnc_kwargs):
        self.log("Calendar update detected.")
        self.decide_whether_to_ask_for_new_schedule()

    def decide_whether_to_ask_for_new_schedule(self):
        """
        This function is meant to be called upon:
        - SOC updates
        - calendar updates
        - charger state updates
        - every 15 minutes if none of the above
        """
        self.log("Deciding whether to ask for a new schedule...")

        # Check whether we're in automatic mode
        mode = self.get_state("input_select.charge_mode")
        if mode != "Automatic":
            self.log(f"Not posting UDI event. Charge mode is not 'Automatic' but '{mode}'.")
            return

        # Check whether we're not in boost mode
        if self.in_boost_to_reach_min_soc:
            self.log(f"Not posting UDI event. SoC below minimum, boosting to reach that first.")
            return

        # Get the SoC in Wh
        soc_entity = self.get_state("input_number.car_state_of_charge_wh", attribute="all")

        # Check whether the most recent SOC update represents a state change
        if self.args.get("reschedule_on_soc_changes_only", True) and soc_entity["last_changed"] != soc_entity[
            "last_updated"]:
            self.log(f"Not posting UDI event. SoC Wh state update but not a state change")
            # A state update but not a state change
            # https://data.home-assistant.io/docs/states/
            return

        # Prepare the SoC measurement to be sent along with the scheduling request
        soc = float(soc_entity["state"]) / 1000  # to kWh
        soc_datetime = datetime.now(tz=pytz.utc)  # soc_entity["last_changed"]

        self.log("getting new schedule")
        # Otherwise, ask for a new schedule
        self.get_app("flexmeasures-client").get_new_schedule()

    def cancel_charging_timers(self):
        # todo: save outside of the app, otherwise, in case the app crashes, we lose track of old handles
        for h in self.scheduling_timer_handles:
            self.cancel_timer(h)

    def set_charging_timers(self, handles):
        # todo: save outside of the app, otherwise, in case the app crashes, we lose track of old handles
        self.scheduling_timer_handles = handles

    def schedule_charge_point(self, entity, attribute, old, new, kwargs):
        """Process a schedule by setting timers to send new control signals to the Charge Point.

        If appropriate, also sends a new control signal right away.
        Finally, the expected SoC (given the schedule) is calculated and saved to input_text.soc_prognosis.
        """
        self.log("Schedule_charge_point called, triggerd by change in input_text.chargeschedule.")
        
        if not self.is_car_connected():
            self.log("Stopped processing schedule; car is not connected")
            return

        schedule = self.get_state("input_text.chargeschedule", attribute="all")

        if schedule["state"] in ("DisconnectNow", "reset"):
            self.log("Stopped processing schedule; DisconnectNow requested, stop charging and give control to user.")
            # Tell charger to stop charging and set control to user
            self.set_charger_action("stop")
            self.set_charger_control("give")
            # ToDo: Remove all schedules?
            return

        schedule = schedule["attributes"]
        self.log(schedule)

        values = schedule["values"]
        duration = isodate.parse_duration(schedule["duration"])
        resolution = duration / len(values)
        start = isodate.parse_datetime(schedule["start"])

        # Check against expected control signal resolution
        min_resolution = timedelta(minutes=self.args["fm_quasar_soc_event_resolution_in_minutes"])
        if resolution < min_resolution:
            self.log(
                f"Stopped processing schedule; the resolution ({resolution}) is below the set minimum ({min_resolution})."
            )
            return

        # Cancel previous scheduling timers
        self.cancel_charging_timers()

        # Create new scheduling timers, to send a control signal for each value
        handles = []
        now = self.get_now()
        timer_datetimes = [start + i * resolution for i in range(len(values))]
        for t, value in zip(timer_datetimes, values):
            if t > now:
                # AJO 17-10-2021 ToDo: If value is the same as previous, combine them so we have less timers and switching moments?
                h = self.run_at(self.send_control_signal, t, charge_rate=value * 1000)  # convert from MW to kW
                handles.append(h)
            else:
                self.log(
                    f"Cannot time a charging scheduling in the past, specifically, at {t}. Setting it immediately instead.")
                self.send_control_signal(kwargs=dict(charge_rate=value * 1000))
        self.set_charging_timers(handles)
        self.log(f"{len(handles)} charging timers set.")

        # Keep track of the expected SoC by adding each scheduled value to the current SoC
        soc = float(self.get_state("input_number.car_state_of_charge", attribute="state"))
        if int(soc) != int(self.connected_car_soc):
            # todo: consider calling try_get_new_soc() and then using accumulate(self.connected_car_soc) below instead
            self.log(
                f"input_number.car_state_of_charge ({soc}) is not equal to self.connected_car_soc ({self.connected_car_soc}), consider calling try_get_new_soc()")
        exp_soc_values = list(
            accumulate([soc] + convert_MW_to_percentage_points(values, resolution, self.args["fm_car_max_soc_in_kwh"])))
        exp_soc_datetimes = [start + i * resolution for i in range(len(exp_soc_values))]
        expected_soc_based_on_scheduled_charges = [dict(time=t.isoformat(), soc=round(v, 2)) for v, t in
                                                   zip(exp_soc_values, exp_soc_datetimes)]
        # self.log(f"Expected soc values: {self.expected_soc_based_on_scheduled_charges}")
        new_state = "SoC prognosis based on schedule available at " + now.isoformat()
        result = dict(records=expected_soc_based_on_scheduled_charges)
        self.set_state("input_text.soc_prognosis", state=new_state, attributes=result)

    def update_charge_mode(self, entity, attribute, old, new, kwargs):
        new_state = new["state"]
        old_state = old.get("state")
        self.log(f"Charge mode has changed from '{old_state}' to '{new_state}'")

        # As the next statements might take control of charging we need to interrupt try_get_new_soc.
        self.try_stop_get_new_soc()

        if old_state == 'Max boost now' and new_state == 'Automatic':
            # When mode goes from "Max boost now" to "Automatic" charging needs to be stopped.
            # Let schedule (later) decide if starting is needed
            self.set_charger_control("take")
            self.set_charger_action("stop")

        if old_state != 'Stop' and new_state == 'Stop':
            # New mode "Stop" is handled by set_next_action
            self.log("Stop charging (if in action) and give control based on chargemode = Stop")
            # Cancel previous scheduling timers
            self.cancel_charging_timers()
            self.set_power_setpoint(0)  # this will also stop the charger.
            self.set_charger_control("give")

        self.set_next_action()

    def restart_set_next_action_time_based(self, *arg):
        self.log("restart_set_next_action_time_based")
        self.set_next_action()

    def set_next_action(self):
        # Make sure this function gets called every x seconds to prevent a "frozen" app.
        if self.timer_handle_set_next_action:
            self.log("Cancel current timer")
            self.cancel_timer(self.timer_handle_set_next_action)
        else:
            self.log("There is no timer to cancel")

        self.timer_handle_set_next_action = self.run_in(
            self.restart_set_next_action_time_based,
            self.call_next_action_atleast_every,
        )

        if not self.is_car_connected():
            self.log("No car connected or error, stopped setting next action.")
            return

        if self.connected_car_soc == 0:
            self.log("SoC is 0, stopped setting next action.")
            # Maybe (but it is dangerous) do try_get_soc??
            return

        charge_mode = self.get_state("input_select.charge_mode", attribute="state")
        self.log(f"Setting next action based on charge_mode '{charge_mode}'.")

        if charge_mode == "Automatic":
            self.set_charger_control("take")

            # Chargemode = Off (Stop) must be really Off so only check low SoC in automatic.
            if self.connected_car_soc < 19 and not self.in_boost_to_reach_min_soc:
                # Intended for the situation where the car returns from a trip with a low battery.
                # An SoC below 20% is considered "unhealthy" for the battery,
                # this is why the battery should be charged to this minimum asap.

                self.log("Starting max charge now and not requesting schedule based on SoC below minimum (20%).")
                # Cancel previous scheduling timers as they might have discharging instructions as well
                self.cancel_charging_timers()
                self.start_max_charge_now()
                self.in_boost_to_reach_min_soc = True
                return
            elif self.connected_car_soc > 20 and self.in_boost_to_reach_min_soc:
                self.log("Stopping max charge now, SoC above minimum (20%) again.")
                self.in_boost_to_reach_min_soc = False
                self.set_power_setpoint(0)

            # Not checking for > max charge (97%) because we could also want to discharge based on schedule

            # Check for discharging below 30% done in the function for setting the (dis)charge_current.
            self.decide_whether_to_ask_for_new_schedule()

        elif charge_mode == "Max boost now":
            self.set_charger_control("take")
            # If charger_state = "not connected", the UI shows an (error) message.

            if self.connected_car_soc >= 100:
                self.log(f"Reset charge_mode to 'Automatic' because max_charge is reached.")
                # This is a somewhat clumsy way to set the mode to automatic but HA does not support radio buttons,
                # so the UI needed to be setup rather complicated. Setting the charge mode to automatic is not doing the job.
                # Then the UI does not match the actual status.
                # Further the set_state needs to re-apply the icon and friendly name for some odd reason. Otherwise, the icon changes to the default toggle...
                res = self.set_state("input_boolean.chargemodeautomatic", state="on",
                                     attributes={'friendly_name': 'ChargeModeAutomatic',
                                                 'icon': 'mdi:battery-charging-80'})
                # ToDo: Maybe do this after 20 minutes or so..

                if not res is True:
                    self.log(f"Failed to reset charge_mode to 'Automatic'. Home Assistant responded with: {res}")
            else:
                self.log("Starting max charge now based on chargemode = Max boost now")
                self.start_max_charge_now()

        elif charge_mode == "Stop":
            self.log("ChargeMode = Stop")
            self.set_power_setpoint(0)  # this will also stop the charger.
            self.set_charger_control("give")

            # Stopping charger and giving control is also done in the callback function update_charge_mode

        else:
            raise ValueError(f"Unknown option for set_next_action: {charge_mode}")

        return


def convert_MW_to_percentage_points(values_in_MW, resolution, max_soc_in_kWh):
    """
    For example, if a 62 kWh battery produces at 0.00575 MW for a period of 15 minutes,
    its SoC increases by just over 2.3%.
    """
    scalar = resolution / timedelta(hours=1) * 1000 * 100 / max_soc_in_kWh
    return [v * scalar for v in values_in_MW]
