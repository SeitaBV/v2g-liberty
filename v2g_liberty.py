from datetime import datetime, timedelta
import isodate
import time
import pytz
from v2g_globals import time_round
import math
from itertools import accumulate
from typing import AsyncGenerator, List, Optional
import constants as c
from v2g_globals import V2GLibertyGlobals

import appdaemon.plugins.hass.hassapi as hass

from wallbox_client import WallboxModbusMixin


class V2Gliberty(hass.Hass, WallboxModbusMixin):
    """ This class manages the communication with the Wallbox Quasar charger and
    the FlexMeasures platform (which delivers the charging schedules).
    """

    # CONSTANTS
    # Fail-safe for processing schedules that might have schedule with too high update frequency
    MIN_RESOLUTION: timedelta
    CAR_AVERAGE_WH_PER_KM: int
    ADMIN_MOBILE_NAME: str

    # Utility variables for preventing a frozen app. Call set_next_action at least every x seconds
    timer_handle_set_next_action: str  # ToDo: Should be a general object instead of string
    call_next_action_atleast_every: int
    scheduling_timer_handles: List[AsyncGenerator]

    # A SoC of 0 means: unknown/car not connected.
    connected_car_soc: int
    connected_car_soc_kwh: float

    # This is a target datetime at which te SoC that is above the max_soc must return back to or below this value.
    # It is dependant on the user setting for allowed duration above max soc.
    back_to_max_soc: datetime

    # Variable to store charger_state for comparison for change
    current_charger_state: int
    in_boost_to_reach_min_soc: bool

    # To keep track of duration of charger in error state.
    charger_in_error_since: datetime
    # initially charger_in_error_since is set to this date reference.
    # If charger_in_error_since is not equal to this date we know timeing has started.
    date_reference: datetime

    # For handling no_schedule_errors
    no_schedule_errors: dict
    notification_timer_handle: ""
    user_was_notified_of_no_schedule: bool

    # For notifying users
    PRIORITY_NOTIFICATION_CONFIG: list
    recipients: list

    # Ignore soc changes and charger_state changes.
    try_get_new_soc_in_process: bool

    def initialize(self):
        self.log("Initializing V2Gliberty")

        self.MIN_RESOLUTION = timedelta(minutes=c.FM_EVENT_RESOLUTION_IN_MINUTES)
        self.CAR_AVERAGE_WH_PER_KM = int(float(self.args["car_average_wh_per_km"]))

        # If this variable is None it means the current SoC is below the max-soc.
        self.back_to_max_soc = None

        # Show the settings in the UI
        self.set_value("input_text.v2g_liberty_version", c.V2G_LIBERTY_VERSION)
        self.set_value("input_text.optimisation_mode", c.OPTIMISATION_MODE)
        self.set_value("input_text.utility_display_name", c.UTILITY_CONTEXT_DISPLAY_NAME)

        self.ADMIN_MOBILE_NAME = self.args["admin_mobile_name"].lower()
        self.PRIORITY_NOTIFICATION_CONFIG = {}
        self.recipients = []
        self.init_notification_configuration()

        self.in_boost_to_reach_min_soc = False
        self.try_get_new_soc_in_process = False
        self.call_next_action_atleast_every = 15 * 60
        self.timer_handle_set_next_action = ""
        self.connected_car_soc = 0
        self.connected_car_soc_kwh = 0
        # Force change event at initialisation
        self.current_charger_state = -1

        # For checking how long the charger has been in error
        self.date_reference = datetime(2000, 1, 1)
        self.charger_in_error_since = self.date_reference

        # For handling no_schedule errors
        self.no_schedule_errors = {
            "invalid_schedule": False,
            "timeouts_on_schedule": False,
            "no_communication_with_fm": False
        }

        self.notification_timer_handle = None
        self.no_schedule_notification_is_planned = False

        self.client = self.configure_charger_client()
        self.log_errors()
        self.get_app("flexmeasures-client").authenticate_with_fm()

        self.listen_state(self.update_charge_mode, "input_select.charge_mode", attribute="all")
        self.listen_state(self.handle_charger_state_change, "sensor.charger_charger_state", attribute="all")
        self.listen_event(self.disconnect_charger, "DISCONNECT_CHARGER")

        self.listen_state(self.handle_soc_change, "sensor.charger_connected_car_state_of_charge", attribute="all")
        self.listen_state(self.schedule_charge_point, "input_text.chargeschedule", attribute="all")
        self.scheduling_timer_handles = []

        # Set to initial 'empty' values, makes rendering of graph faster.
        self.set_soc_prognosis_boost_in_ui()
        self.set_soc_prognosis_in_ui()

        if self.is_car_connected():
            self.log("Car is connected. Trying to get a reliable SoC reading.")
            self.try_get_new_soc()

        # When to ask FlexMeasures for a new charging schedule is determined by the charge mode
        self.set_next_action()  # on initializing the app
        if self.in_boost_to_reach_min_soc:
            # FNC0816
            # Test whether restarting the app executes boost mode when boost mode is needed (below min. SoC)
            # Executing self.set_next_action() once may not do it, and executing it twice may be needed
            # (we are not sure why yet)

            # if we went into boost mode, actually execute boost mode
            self.log("actually execute boost mode")
            # self.cancel_charging_timers()
            # self.start_max_charge_now()
            self.set_next_action()

        self.log("Completed Initializing V2Gliberty")

    def init_notification_configuration(self):
        # List of all the recipients to notify
        # Check if Admin is configured correctly
        # Warn user about bad config with persistent notification in UI.
        self.log("Initializing notification configuration")

        self.recipients.clear()
        # Service "mobile_app_" seems more reliable than using get_trackers,
        # as these names do not always match with the service.
        for service in self.list_services():
            if service["service"].startswith("mobile_app_"):
                self.recipients.append(service["service"].replace("mobile_app_", ""))
        self.log(f"Recipients for notifications: {self.recipients}.")

        message = ""
        if len(self.recipients) == 0:
            message = f"No mobile devices (e.g. phone, tablet, etc.) have been registered in Home Assistant " \
                      f"for notifications.<br/>" \
                      f"It is highly recommended to do so. Please install the HA companion app on your mobile device " \
                      f"and connect it to Home Assistant."
            self.log(f"Configuration error: {message}.")
        elif self.ADMIN_MOBILE_NAME not in self.recipients:
            alternative_admin = self.recipients[0]
            message = f"The admin mobile name ***{self.ADMIN_MOBILE_NAME}*** in configuration is not a registered " \
                      f"recipient.<br/>" \
                      f"Please use one of the following: {self.recipients}.<br/>" \
                      f"Now, ***{alternative_admin}*** will be used for high-priority/technical notifications with " \
                      f"the assumption it is a iOS device."
            self.log(f"Configuration error: The admin_mobile_name '{self.ADMIN_MOBILE_NAME}' in configuration not a "
                     f"registered recipient.")
            self.ADMIN_MOBILE_NAME = self.recipients[0]
        else:
            # Only check platform config if admin mobile name is valid.
            platform = self.args["admin_mobile_platform"].lower()
            if platform == "ios":
                self.PRIORITY_NOTIFICATION_CONFIG = {
                    "push": {"sound": {"critical": 1, "name": "default", "volume": 0.9}}}
            elif platform == "android":
                self.PRIORITY_NOTIFICATION_CONFIG = {"ttl": 0, "priority": "high"}
            else:
                message = f"The admin_mobile_platform in configuration: '{platform}' is unknown."
                self.log(f"Configuration error: {message}")

        if message != "":
            # TODO: Research if showing this only to admin users is possible.
            self.call_service('persistent_notification/create', title="Configuration error", message=message,
                              notification_id="notification_config_error")

        self.log("Completed Initializing notification configuration")

    def disconnect_charger(self, *args, **kwargs):
        """ Function te disconnect the charger.
        Reacts to button in UI that fires DISCONNECT_CHARGER event.
        """
        self.log("************* Disconnect charger requested *************")
        self.reset_no_new_schedule()
        self.set_charger_action("stop")
        # Control is not given to user, this is only relevant if charge_mode is "Off" (stop).
        # ToDo: Remove all schedules?
        self.notify_user(
            message     = "Charger is disconnected",
            title       = None,
            tag         = "charger_disconnected",
            critical    = False,
            send_to_all = True,
            ttl         = 5 * 60
        )

    def notify_user(self,
                    message: str,
                    title: Optional[str] = None,
                    tag: Optional[str] = None,
                    critical: bool = False,
                    send_to_all: bool = False,
                    ttl: Optional[int] = 0
                    ):
        """ Utility function to send notifications to the user
            - critical    : send with high priority to Admin only. Always delivered and sound is play. Use with caution.
            - send_to_all : send to all users (can't be combined with critical), default = only send to Admin.
            - tag         : id that can be used to replace or clear a previous message
            - ttl         : time to live in seconds, after that the message will be cleared. 0 = do not clear.
                            A tag is required.

            We assume there always is an ADMIN and there might be several other users that need to be notified.
            When a new call to this function with the same tag is made, the previous message will be overwritten
            if it still exists.
        """

        self.log(f"Notifying user..")

        if title:
            # Use abbreviation to make more room for title itself.
            title = "V2G-L: " + title
        else:
            title = "V2G Liberty"

        # All notifications always get sent to admin
        to_notify = [self.ADMIN_MOBILE_NAME]
        notification_data = {}

        # critical trumps send_to_all
        if critical:
            notification_data = self.PRIORITY_NOTIFICATION_CONFIG

        if send_to_all and not critical:
            to_notify = self.recipients

        if tag:
            notification_data["tag"] = tag

        self.log(f"Notifying recipients: {to_notify} with message: '{message[0:15]}...' data: {notification_data}.")
        for recipient in to_notify:
            service = "notify/mobile_app_" + recipient
            try:
                if notification_data:
                    self.call_service(service, title=title, message=message, data=notification_data)
                else:
                    self.call_service(service, title=title, message=message)
            except:
                self.log(f"Could not notify: exception on {recipient}.")

            if ttl > 0 and tag and not critical:
                # Remove the notification after a time-to-live
                # A tag is required for clearing.
                # Critical notifications should not auto clear.
                self.run_in(self.clear_notification, ttl, recipient=recipient, tag=tag)

    def clear_notification_for_all_recipients(self, tag: str):
        for recipient in self.recipients:
            identification = { "recipient": recipient, "tag": tag }
            self.clear_notification(identification)

    def clear_notification(self, identification: dict):
        self.log(f"Clearing notification. Data: {identification}")
        recipient = identification["recipient"]
        if recipient == "" or recipient is None:
            self.log(f"Cannot clear notification, recipient is empty '{recipient}'.")
            return
        tag = identification["tag"]
        if tag == "" or tag is None:
            self.log(f"Cannot clear notification, tag is empty '{tag}'.")
            return

        # Clear the notification
        try:
            self.call_service(
                "notify/mobile_app_" + recipient,
                message="clear_notification",
                data={"tag": tag}
            )
        except:
            self.log(f"Could not clear notification: exception on {recipient}.")


    def reset_no_new_schedule(self):
        """ Sets all errors to False and removes notification / UI messages

        To be used when the car gets disconnected, so that while it stays in this state there is no
        unneeded "alarming" message/notification.
        Also, when the car returns with an SoC below the minimum no new schedule is retrieved and
        in that case the message / notification would remain without a need.
        """

        for error_name in self.no_schedule_errors:
            self.no_schedule_errors[error_name] = False
        self.notify_no_new_schedule(reset = True)


    def handle_no_new_schedule(self, error_name: str, error_state: bool):
        """ Keep track of situations where no new schedules are available:
            - invalid schedule
            - timeouts on schedule
            - no communication with FM
            They can occur simultaneously/overlapping, so they are accumulated in
            the dictionary self.no_schedule_errors.
        """

        if error_name in self.no_schedule_errors:
            self.log(f"handle_no_valid_schedule called with {error_name}: {error_state}.")
        else:
            self.log(f"handle_no_valid_schedule called unknown error_name: '{error_name}'.")
            return
        self.no_schedule_errors[error_name] = error_state
        self.notify_no_new_schedule()

    def notify_no_new_schedule(self, reset: Optional[bool] = False):
        """ Check if notification of user about no new schedule available is needed,
            based on self.no_schedule_errors. The administration for the errors is done by
            handle_no_new_schedule().

            When error_state = True of any of the errors:
                Set immediately in UI
                Notify once if remains for an hour
            When error state = False:
                If all errors are solved:
                    Remove from UI immediately
                    If notification has been sent:
                        Notify user the situation has been restored.

            Parameters
            ----------
            reset : bool, optional
                    Reset is meant for the situation where the car gets disconnected and all
                    notifications can be cancelled and messages in UI removed.
                    Then also no "problems are solved" notification is sent.

        """

        if reset:
            self.cancel_timer(self.notification_timer_handle, True)
            self.no_schedule_notification_is_planned = False
            self.clear_notification_for_all_recipients(tag = "no_new_schedule")
            self.set_state("input_boolean.error_no_new_schedule_available", state="off")
            return

        any_errors = False
        for error_name in self.no_schedule_errors:
            if self.no_schedule_errors[error_name]:
                any_errors = True
                break

        if any_errors:
            self.set_state("input_boolean.error_no_new_schedule_available", state="on")
            if not self.no_schedule_notification_is_planned:
                # Plan a notification in case the error situation remains for more than an hour
                self.notification_timer_handle = self.run_in(self.no_new_schedule_notification, 60 * 60)
                self.no_schedule_notification_is_planned = True
        else:
            self.set_state("input_boolean.error_no_new_schedule_available", state="off")
            canceled_before_run = self.cancel_timer(self.notification_timer_handle)
            if self.no_schedule_notification_is_planned and not canceled_before_run:
                # Only send this message if "no_schedule_notification" was actually sent
                title = "Schedules available again"
                message = f"The problems with schedules have been solved. " \
                          f"If you've set charging via the chargers app, " \
                          f"consider to end that and use automatic charging again."
                self.notify_user(
                    message     = message,
                    title       = title,
                    tag         = "no_new_schedule",
                    critical    = False,
                    send_to_all = True,
                    ttl         = 30 * 60
                )
            self.no_schedule_notification_is_planned = False

    def no_new_schedule_notification(self):
        # Work-around to have this in a separate function (without arguments) and not inline in handle_no_new_schedule
        # This is needed because self.run_in() with kwargs does not really work well and results in this app crashing
        title = "No new schedules available"
        message = f"The current schedule will remain active." \
                  f"Usually this problem is solved automatically in an hour or so." \
                  f"If the schedule does not fit your needs, consider charging manually via the chargers app."
        self.notify_user(
            message     = message,
            title       = title,
            tag         = "no_new_schedule",
            critical    = False,
            send_to_all = True
        )
        self.log("Notification 'No new schedules' sent.")

    def decide_whether_to_ask_for_new_schedule(self):
        """
        This function is meant to be called upon:
        - SOC updates
        - charger state updates
        - every 15 minutes if none of the above
        """
        self.log("Deciding whether to ask for a new schedule...")

        # Check whether we're in automatic mode
        mode = self.get_state("input_select.charge_mode")
        if mode != "Automatic":
            self.log(f"Not getting new schedule. Charge mode is not 'Automatic' but '{mode}'.")
            return

        # Check whether we're not in boost mode
        if self.in_boost_to_reach_min_soc:
            self.log(f"Not getting new schedule. SoC below minimum, boosting to reach that first.")
            return

        self.get_app("flexmeasures-client").get_new_schedule(self.connected_car_soc_kwh, self.back_to_max_soc)

    def cancel_charging_timers(self):
        for h in self.scheduling_timer_handles:
            self.cancel_timer(h, True)
        # Also remove any visible schedule from the graph in the UI..
        self.set_soc_prognosis_in_ui(None)

    def set_charging_timers(self, handles):
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
        schedule = schedule["attributes"]
        values = schedule["values"]
        duration = isodate.parse_duration(schedule["duration"])
        resolution = duration / len(values)
        start = isodate.parse_datetime(schedule["start"])

        # Check against expected control signal resolution
        if resolution < self.MIN_RESOLUTION:
            self.log(f"Stopped processing schedule; the resolution ({resolution}) is below "
                     f"the set minimum ({self.MIN_RESOLUTION}).")
            self.handle_no_new_schedule("invalid_schedule", True)
            return

        # Detect invalid schedules
        # If a fallback schedule is sent assume that the schedule is invalid if all values (usually 0) are the same
        is_fallback = (schedule["scheduler_info"]["scheduler"] == "StorageFallbackScheduler")
        if is_fallback and (all(val == values[0] for val in values)):
            self.log(f"Invalid fallback schedule, all values are the same: {values[0]}. Stopped processing.")
            self.handle_no_new_schedule("invalid_schedule", True)
            # Skip processing this schedule to keep the previous
            return
        else:
            self.handle_no_new_schedule("invalid_schedule", False)

        # Cancel previous scheduling timers
        self.cancel_charging_timers()

        # Create new scheduling timers, to send a control signal for each value
        handles = []
        now = self.get_now()
        timer_datetimes = [start + i * resolution for i in range(len(values))]
        for t, value in zip(timer_datetimes, values):
            if t > now:
                # AJO 17-10-2021
                # ToDo: If value is the same as previous, combine them so we have less timers and switching moments?
                h = self.run_at(self.send_control_signal, t, charge_rate=value * 1000)  # convert from MW to kW
                handles.append(h)
            else:
                self.log(f"Cannot time a charging scheduling in the past, specifically, at {t}."
                         f" Setting it immediately instead.")
                self.send_control_signal(kwargs=dict(charge_rate=value * 1000))

        self.set_charging_timers(handles)
        self.log(f"{len(handles)} charging timers set.")

        # Keep track of the expected SoC by adding each scheduled value to the current SoC
        soc = float(self.get_state("input_number.car_state_of_charge", attribute="state"))
        if int(soc) != int(self.connected_car_soc):
            # todo: consider calling try_get_new_soc() and then using accumulate(self.connected_car_soc) below instead
            self.log(f"input_number.car_state_of_charge ({soc}) is not equal to self.connected_car_soc"
                     f" ({self.connected_car_soc}), consider calling try_get_new_soc()")

        exp_soc_values = list(accumulate([soc] + convert_MW_to_percentage_points(values,
                                                                                 resolution,
                                                                                 c.CAR_MAX_CAPACITY_IN_KWH,
                                                                                 c.CHARGER_PLUS_CAR_ROUNDTRIP_EFFICIENCY)))
        exp_soc_datetimes = [start + i * resolution for i in range(len(exp_soc_values))]
        expected_soc_based_on_scheduled_charges = [dict(time=t.isoformat(), soc=round(v, 2)) for v, t in
                                                   zip(exp_soc_values, exp_soc_datetimes)]
        self.set_soc_prognosis_in_ui(expected_soc_based_on_scheduled_charges)

    def set_soc_prognosis_in_ui(self, records: Optional[dict] = None):
        """Write or remove SoC prognosis in graph via HA entity input_text.soc_prognosis

            If records = None the SoC line will be removed from the graph,
            e.g. when the car gets disconnected and the SoC prognosis is not relevant (anymore)

            Parameters:
                records(Optional[dict] = None): a dictionary of time (isoformat) + SoC (%) records

            Returns:
                Nothing
        """
        if records is None:
            # There seems to be no way to hide the SoC series from the graph,
            # so it is filled with "empty" data, one record of 0.
            # Set it at a week from now, so it's not visible in the default view.
            records = [dict(time=(self.get_now() + timedelta(days=7)).isoformat(), soc=0.0)]

        # To make sure the new attributes are treated as new we set a new state as well
        new_state = "SoC prognosis based on schedule available at " + self.get_now().isoformat()
        result = dict(records=records)
        self.set_state("input_text.soc_prognosis", state=new_state, attributes=result)

    def set_soc_prognosis_boost_in_ui(self, records: Optional[dict] = None):
        """Write or remove SoC prognosis boost in graph via HA entity input_text.soc_prognosis_boost
            Boost is in action when SoC is below minimum.
            The only difference with normal SoC prognosis is the line color.
            We do not use APEX chart color_threshold feature on SoC prognosis as
            it is experimental and the min_soc is a setting and can change.

            If records = None the SoC boost line will be removed from the graph,
            e.g. when the car gets disconnected and the SoC prognosis boost is not relevant (anymore)

            Parameters:
                records(Optional[dict] = None): a dictionary of time (isoformat) + SoC (%) records

            Returns:
                Nothing
        """
        if records is None:
            # There seems to be no way to hide the SoC series from the graph,
            # so it is filled with "empty" data, one record of 0.
            # Set it at a week from now, so it's not visible in the default view.
            records = [dict(time=(self.get_now() + timedelta(days=7)).isoformat(), soc=0.0)]

        # To make sure the new attributes are treated as new we set a new state as well
        new_state = "SoC prognosis boost based on boost 'schedule' available at " + self.get_now().isoformat()
        result = dict(records=records)
        self.set_state("input_text.soc_prognosis_boost", state=new_state, attributes=result)

    def update_charge_mode(self, entity, attribute, old, new, kwargs):
        """Function to handle updates in the charge mode"""
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

        # TODO: check if old_state != 'Stop' is still needed.
        if old_state != 'Stop' and new_state == 'Stop':
            # New mode "Stop" is handled by set_next_action
            self.log("Stop charging (if in action) and give control based on chargemode = Stop")
            # Cancel previous scheduling timers
            self.cancel_charging_timers()
            self.in_boost_to_reach_min_soc = False
            self.set_power_setpoint(0)  # this will also stop the charger.
            self.set_charger_control("give")

        self.set_next_action()

    def restart_set_next_action_time_based(self, *arg):
        """Helper function to trace the time based calls of set_next_action"""
        self.log("restart_set_next_action_time_based")
        self.set_next_action()

    def set_next_action(self):
        """The function determines what action should be taken next based on current SoC, Charge_mode, Charger_state

        This function is meant to be called upon:
        - SOC updates
        - calendar updates
        - charger state updates
        - every 15 minutes if none of the above
        """

        # Make sure this function gets called every x seconds to prevent a "frozen" app.
        if self.timer_handle_set_next_action:
            self.cancel_timer(self.timer_handle_set_next_action, True)
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

        # If the SoC of the car is higher than the max-soc (intended for battery protection)
        # a target is to return to the max-soc within the ALLOWED_DURATION_ABOVE_MAX_SOC
        if (self.back_to_max_soc is None) and (self.connected_car_soc_kwh > c.CAR_MAX_SOC_IN_KWH):
            self.back_to_max_soc = time_round((self.get_now() + timedelta(hours=c.ALLOWED_DURATION_ABOVE_MAX_SOC)), self.MIN_RESOLUTION)
            self.log(f"SoC above max-soc, aiming to schedule with target {c.CAR_MAX_SOC_IN_PERCENT}% at {self.back_to_max_soc}.")
        elif self.connected_car_soc_kwh <= c.CAR_MAX_SOC_IN_KWH:
            self.back_to_max_soc = None
            self.log(f"SoC was below max-soc, has been restored.")

        charge_mode = self.get_state("input_select.charge_mode", attribute="state")
        self.log(f"Setting next action based on charge_mode '{charge_mode}'.")

        if charge_mode == "Automatic":
            self.set_charger_control("take")
            if self.connected_car_soc < c.CAR_MIN_SOC_IN_PERCENT and not self.in_boost_to_reach_min_soc:
                # Intended for the situation where the car returns from a trip with a low battery.
                # An SoC below the minimum SoC is considered "unhealthy" for the battery,
                # this is why the battery should be charged to this minimum asap.
                # Cancel previous scheduling timers as they might have discharging instructions as well
                self.cancel_charging_timers()
                self.start_max_charge_now()
                self.in_boost_to_reach_min_soc = True

                # Create a minimal schedule to show in graph that gives user an estimation of when the min. SoC will
                # be reached. The schedule starts now with current SoC
                boost_schedule = [dict(time=(self.get_now()).isoformat(), soc=self.connected_car_soc)]

                # How much energy (wh) is needed, taking roundtrip efficiency into account
                # For % /100, for kwh to wh * 1000 results in *10..
                delta_to_min_soc_wh = (c.CAR_MIN_SOC_IN_PERCENT - self.connected_car_soc) * c.CAR_MAX_CAPACITY_IN_KWH * 10
                delta_to_min_soc_wh = delta_to_min_soc_wh / (c.CHARGER_PLUS_CAR_ROUNDTRIP_EFFICIENCY ** 0.5)

                # How long will it take to charge this amount with max power, we use ceil to avoid 0 minutes as
                # this would not show in graph.
                minutes_to_reach_min_soc = int(math.ceil((delta_to_min_soc_wh / c.CHARGER_MAX_CHARGE_POWER * 60)))
                expected_min_soc_time = (self.get_now() + timedelta(minutes=minutes_to_reach_min_soc)).isoformat()
                boost_schedule.append(dict(time=expected_min_soc_time, soc=c.CAR_MIN_SOC_IN_PERCENT))
                self.set_soc_prognosis_boost_in_ui(boost_schedule)

                message = f"Car battery state of charge ({self.connected_car_soc}%) is too low. " \
                          f"Charging with maximum power until minimum of ({c.CAR_MIN_SOC_IN_PERCENT}%) is reached. " \
                          f"This is expected around {expected_min_soc_time}."
                self.notify_user(
                    message     = message,
                    title       = "Car battery is too low",
                    tag         = "battery_too_low",
                    critical    = False,
                    send_to_all = True,
                    ttl         = minutes_to_reach_min_soc * 60
                )
                return

            if self.connected_car_soc > c.CAR_MIN_SOC_IN_PERCENT and self.in_boost_to_reach_min_soc:
                self.log(f"Stopping max charge now, SoC above minimum ({c.CAR_MIN_SOC_IN_PERCENT}%) again.")
                self.in_boost_to_reach_min_soc = False
                self.set_power_setpoint(0)
                # Remove "boost schedule" from graph.
                self.set_soc_prognosis_boost_in_ui(None)
            elif self.connected_car_soc <= (c.CAR_MIN_SOC_IN_PERCENT + 1) and self.is_discharging():
                # Failsafe, this should not happen...
                self.log(f"Stopped discharging as SoC has reached minimum ({c.CAR_MIN_SOC_IN_PERCENT}%).")
                self.set_power_setpoint(0)

            # Not checking for > max charge (97%) because we could also want to discharge based on schedule

            # Check for discharging below minimum done in the function for setting the (dis)charge_current.
            self.decide_whether_to_ask_for_new_schedule()

        elif charge_mode == "Max boost now":
            self.set_charger_control("take")
            # If charger_state = "not connected", the UI shows an (error) message.

            if self.connected_car_soc >= 100:
                self.log(f"Reset charge_mode to 'Automatic' because max_charge is reached.")
                # ToDo: Maybe do this after 20 minutes or so..
                self.set_chargemode_in_ui("Automatic")
            else:
                self.log("Starting max charge now based on charge_mode = Max boost now")
                self.start_max_charge_now()

        elif charge_mode == "Stop":
            self.log("ChargeMode = Stop")
            self.set_power_setpoint(0)  # this will also stop the charger.
            self.set_charger_control("give")

            # Stopping charger and giving control is also done in the callback function update_charge_mode

        else:
            raise ValueError(f"Unknown option for set_next_action: {charge_mode}")

        return

    def set_chargemode_in_ui(self, setting: str):
        """ This function sets the charge mode in the UI to setting.
        By setting the UI switch an event will also be fired. So other code will run due to this setting.

        Parameters:
        setting (str): Automatic, MaxBoostNow or Stop (=Off))

        Returns:
        nothing.
        """

        res = False
        if setting == "Automatic":
            # Used when car gets disconnected and ChargeMode was MaxBoostNow.
            res = self.turn_on("input_boolean.chargemodeautomatic")
        elif setting == "MaxBoostNow":
            # Not used for now, just here for completeness.
            # The situation with SoC below the set minimum is handled without setting the UI to MaxBoostNow
            res = self.turn_on("input_boolean.chargemodemaxboostnow")
        elif setting == "Stop":
            # Used when charger crashes to stop further processing
            res = self.turn_on("input_boolean.chargemodeoff")
        else:
            self.log(f"In valid charge_mode in UI setting: '{setting}'.")
            return

        if not res is True:
            self.log(f"Failed to set charge_mode in UI to '{setting}'. Home Assistant responded with: {res}")
        else:
            self.log(f"Successfully set charge_mode in UI to '{setting}'.")


def convert_MW_to_percentage_points(
        values_in_MW,
        resolution: timedelta,
        max_soc_in_kWh: float,
        round_trip_efficiency: float,
):
    """
    For example, if a 62 kWh battery produces at 0.00575 MW for a period of 15 minutes,
    its SoC increases by just over 2.3%.
    """
    e = round_trip_efficiency ** 0.5
    scalar = resolution / timedelta(hours=1) * 1000 * 100 / max_soc_in_kWh
    lst = []
    for v in values_in_MW:
        if v >= 0:
            lst.append(v * scalar * e)
        else:
            lst.append(v * scalar / e)
    return lst
