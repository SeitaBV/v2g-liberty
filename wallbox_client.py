from datetime import timedelta
import adbase as ad
import time
import constants as c
import appdaemon.plugins.hass.hassapi as hass

from pyModbusTCP.client import ModbusClient


class WallboxModbusMixin:
    """ This class manages the communication with the Wallbox charger, using Modbus."""

    client: ModbusClient
    registers: dict
    NUM_MODBUS_PORTS = 65536
    last_restart: int = 0

    # Constant that holds the value for the charger_state that the charger returns when no car is connected.
    # It is the opposite of the "connected_states". It is used to detect connection and dis-connection.
    # See wallbox_modbus_registers.yaml for an overview of all the states like (dis-)charging, paused, error, etc.
    DISCONNECTED_STATE: int

    # A restart of the charger can take up to 5 minutes, so during this time do not request a restart again
    minimum_seconds_between_restarts = 300
    busy_getting_charger_state = False

    def configure_charger_client(self):
        """Configure the Wallbox Modbus client and return it."""
        # Assume that a restart of this code is the same as last restart of the charger.
        self.log("Initializing WallboxModbusMixin")

        self.last_restart = self.get_now()

        host = self.args["wallbox_host"]
        port = self.args["wallbox_port"]
        self.log(f"Configuring Modbus client at {host}:{port}")
        client = ModbusClient(
            host       = host,
            port       = port,
            auto_open  = True,
            auto_close = True,
        )
        # Make sure that after a restart of V2G Liberty (needed after a charger crash)
        # the error in the UI is removed.
        self.turn_off("input_boolean.charger_modbus_communication_fault")
        self.registers = self.args["wallbox_modbus_registers"]
        self.DISCONNECTED_STATE = self.registers["disconnected_state"]

        self.log("Completed Initializing WallboxModbusMixin")

        return client

    def get_charger_state(self) -> int:
        """Get state of the charger.

        The variable busy_getting_charger_state is used to effectively lock up this function,
        such that it can only run sequentially.
        """

        # Prevent this code running in parallel
        if self.busy_getting_charger_state:
            self.log(f"get_charger_state called while busy with getting a state, stopped processing request.")
            return
        self.busy_getting_charger_state = True

        register = self.registers["get_status"]
        charger_state = -1

        # Sometimes the charger returns None for a while, so keep reading until a proper reading is retrieved
        # In rare cases this situation remains for longer.
        # If the max number of attempts has been reached it is most likely the charger is
        # non-responsive in general and a (manual) restart (reboot) of the charger is the only way out.
        max_attempts = 30
        attempts = 0
        while charger_state == -1:
            if attempts > max_attempts:
                # Assume the charger has crashed.
                self.log(f"get_charger_state has reached max attempts ({attempts}), the charger probably crashed. ")
                title = "Critical error"
                message = "Automatic charging has been stopped. Please click this notification to open the V2G Liberty App and follow the steps to solve this problem."
                self.notify_user(
                    message     = message,
                    title       = title,
                    tag         = "critical_error",
                    critical    = True,
                    send_to_all = False,
                    actions     = (
                        {"title": "Restart charger", "action": self.restart_charger},
                        {"title": "Restart appdaemon", "action": self.restart_appdaemon},
                    )
                )
                self.turn_on("input_boolean.charger_modbus_communication_fault")
                self.set_chargemode_in_ui("Stop")
                # This is futile, Modbus has stopped so a restart will not work anyhow.
                # self.set_charger_action("restart")
                self.busy_getting_charger_state = False
                return
            cs = self.client.read_holding_registers(register)
            if cs is None:
                self.log(f"Charger returned state = None, wait 2 seconds and try again.")
                time.sleep(2)
                attempts += 1
                continue
            # Just in case the charger communication self-restores.
            self.turn_off("input_boolean.charger_modbus_communication_fault")
            cs = cs[0]

            if isinstance(cs, str) and not cs.isnumeric():
                self.log(f"Charger state not numeric: {cs}, wait half a second and try again.")
                time.sleep(1 / 2)
                continue
            charger_state = int(float(cs))
        self.busy_getting_charger_state = False
        return charger_state

    def is_charger_in_error(self) -> bool:
        """True if Charge Point returns an error state, False otherwise."""
        return self.get_charger_state() in self.registers["error_state"]

    def is_car_connected(self) -> bool:
        """True if EVSE is connected to Charge Point, False otherwise."""
        return self.get_charger_state() in self.registers["connected_states"]

    def is_charging(self) -> bool:
        """True if Charge Point is charging or discharging, False otherwise."""
        return self.get_charger_state() in self.registers["charging_states"]

    def is_discharging(self) -> bool:
        """True if Charge Point is discharging, False otherwise."""
        return self.get_charger_state() == self.registers["discharging_state"]

    def set_charger_to_autostart_on_connect(self, setting: str):
        """Enable or disable the setting to let the charger autostart on connect."""
        if not self.is_car_connected():
            self.log(f"Not changing the charger setting to {setting} for auto-starting on connect: No car connected.")
            return

        register = self.registers["set_charger_to_autostart_on_connect"]
        setting_in_charger = self.client.read_holding_registers(register)
        try:
            setting_in_charger = setting_in_charger[0]
        except TypeError:
            self.log(f"Modbus read setpoint_type seems not iterable: {setting_in_charger}.")

        try:
            new_setting_to_charger = self.registers["autostart_on_connect_setting"][setting]
        except KeyError:
            raise ValueError(f"Unknown setting for 'set_charger_to_autostart_on_connect': {setting}")

        # Prevent unnecessary writing (and waiting for processing of) same setting
        if setting_in_charger == new_setting_to_charger:
            return

        # Enable/disable charger to autostart on connect
        res = self.client.write_single_register(register, new_setting_to_charger)

        if res is not True:
            self.log(f"Failed to {setting} charger to autostart on connect. Charge Point responded with: {res}")
        else:
            self.log(f"Set 'start charging on EV-Gun connected' to {setting} succeeded")

        time.sleep(self.args["wait_between_charger_write_actions"] / 1000)

    def set_charger_action(self, action: str):
        """Set action to start/stop charging or restart the charger"""

        # Restart is called in problem situations and then is_connected is not reliable..
        if not self.is_car_connected() and action != "restart":
            self.log(f"Not performing charger action '{action}': No car connected.")
            return False

        if action == "start":
            if self.is_charging():
                self.log(f"Not performing charger action 'start': already charging")
                return True
            value = self.registers["actions"]["start_charging"]
        elif action == "stop":
            # AJO 2022-10-08
            # Stop needs to be very reliable, so we always perform this action, even if currently not charging.
            # Sometimes the charger starts charging after reconnect without haven gotten the instruction to do so.
            # To counter this we call "stop" from disconnect event.
            value = self.registers["actions"]["stop_charging"]
        elif action == "restart":
            # AJO 2023-11-30
            # This seems irrelevant code as the restart is (only) needed when the modbus module
            # of the charger has crashed and then it does not receive this instruction anymore.
            # Remove?
            if self.last_restart < (self.get_now() - timedelta(seconds=self.minimum_seconds_between_restarts)):
                self.log(f"Not restarting charger, a restart has been requested already in the "
                         f"last {self.minimum_seconds_between_restarts} seconds.")
                return
            self.log(f"Start RESTARTING charger...")
            self.restart_charger_and_appdaemon()
            self.last_restart = self.get_now()
        else:
            raise ValueError(f"Unknown option for action: '{action}'")

        register = self.registers["set_action"]
        res = False
        total_waiting_time = 0  # in seconds
        # convert from milliseconds to seconds
        wait_between_actions = self.args["wait_between_charger_write_actions"] / 1000
        timeout_charger_write_actions = self.args["timeout_charger_write_actions"] / 1000

        # Make sure the charger will stop/start even though it might sometimes need more than one attempt
        while res is not True:
            res = self.client.write_single_register(register, value)
            time.sleep(wait_between_actions)
            total_waiting_time += wait_between_actions
            # We need to stop at some point
            if total_waiting_time > timeout_charger_write_actions:
                self.log(f"Failed to set action to {action} due to timeout (after {total_waiting_time} seconds). "
                         f"Charge Point responded with: {res}")
                return False
        else:
            self.log(f"Charger {action} succeeded after waiting {total_waiting_time} seconds.")

    def set_charger_control(self, take_or_give_control: str):
        """Set charger control (take control from the user or give control back to the user).

        With giving user control:
        + the user can use the app for controlling the charger and
        + the charger will start charging automatically upon connection.

        :param take_or_give_control: "take" remote control or "give" user control
        """
        # ToDo: check if car is connected
        self.log(f"Control charger {take_or_give_control}n from/to user.")
        if take_or_give_control == "take":
            self.set_control("remote control")
            self.set_charger_to_autostart_on_connect("disable")
        elif take_or_give_control == "give":
            self.set_charger_to_autostart_on_connect("enable")
            self.set_control("user control")
        else:
            raise ValueError(f"Unknown option for take_or_give_control: {take_or_give_control}")

    def set_control(self, setting: str):
        if not self.is_car_connected():
            self.log(f"Not setting control to '{setting}': No car connected.")
            return

        register = self.registers["set_control"]

        # Prevent unnecessary writing (and waiting for processing of) same setting
        setting_in_charger = self.client.read_holding_registers(register)
        try:
            setting_in_charger = setting_in_charger[0]
        except TypeError:
            self.log(f"Modbus read setpoint_type seems not iterable: {setting_in_charger}.")

        if setting_in_charger == self.registers["user_control"] and setting == "enable":
            # Setting in charger is already "user control", no need to write.
            return
        elif setting_in_charger == self.registers["remote_control"] and setting == "disable":
            # Setting in charger is already "remote control", no need to write.
            return

        # Set new control mode
        if setting == "user control":
            res = self.client.write_single_register(register, self.registers["user_control"])
        elif setting == "remote control":
            res = self.client.write_single_register(register, self.registers["remote_control"])
        else:
            raise ValueError(f"unknown option for setting control: {setting}")

        if res is not True:
            self.log(f"Failed to set control to {setting}. Charge Point responded with: {res}")

        time.sleep(self.args["wait_between_charger_write_actions"] / 1000)

    def set_setpoint_type(self, setpoint_type: str):
        """Set setpoint type, such as 'power' or 'current'."""
        if not self.is_car_connected():
            self.log(f"Not setting setpoint_type to '{setpoint_type}': No car connected.")
            return

        register = self.registers["set_setpoint_type"]

        setting_in_charger = self.client.read_holding_registers(register)
        try:
            setting_in_charger = setting_in_charger[0]
        except TypeError:
            self.log(f"Modbus read setpoint_type seems not iterable: {setting_in_charger}.")

        # Prevent unnecessary writing (and waiting for processing of) same setting
        if setting_in_charger == self.registers["setpoint_types"][setpoint_type]:
            # Setting in charger is already set to the desired setpoint type, no need to write.
            self.log(f"Charger already has setpoint type set to {setpoint_type}.")
            return

        # Set new setpoint type
        try:
            setpoint_type = self.registers["setpoint_types"][setpoint_type]
        except KeyError:
            raise ValueError(f"Unknown option for setpoint_type: {setpoint_type}")

        retries = 0
        while retries < 10:
            res = self.client.write_single_register(register, setpoint_type)
            if res is None:
                retries += 1
                time.sleep(0.5)
            else:
                break
        else:
            self.log(f"Failed to set setpoint type to {setpoint_type}. Charge Point responded with: {res}")

        # time.sleep(self.args["wait_between_charger_write_actions"] / 1000)
        # if not res is True:
        #     self.log(f"Failed to set setpoint type to {setpoint_type}. Charge Point responded with: {res}")

    def send_control_signal(self, kwargs: dict, *args, **fnc_kwargs):
        """
        The kwargs dict should contain a "charge_rate" key with a value in kW.
        """
        # Check for automatic mode
        mode = self.get_state("input_select.charge_mode")
        if mode != "Automatic":
            self.log(f"Not sending control signal. Expected charge mode 'Automatic' instead of charge mode '{mode}'.")
            return

        charge_rate = round(kwargs["charge_rate"] * 1000)
        self.log(f"Sending control signal to Wallbox Quasar: set charge rate to {charge_rate / 1000} kW")

        # Prevent unnecessary starting (and with that unnecessary schedule refresh)
        if charge_rate != 0:
            self.set_charger_action("start")
        self.set_power_setpoint(charge_rate)

    def set_power_setpoint(self, charge_rate: int):
        self.log(f"set_power_setpoint called with charge rate {charge_rate} Watt.")

        if not self.is_car_connected():
            self.log(f"Not setting charge_rate to '{charge_rate}': No car connected.")
            return

        # Make sure that discharging does not occur below minimum SoC.
        if charge_rate < 0 and self.connected_car_soc <= c.CAR_MIN_SOC_IN_PERCENT:
            # Failsafe, this should never happen...
            self.log(f"A discharge is attempted while the current SoC is below the "
                     f"minimum ({c.CAR_MIN_SOC_IN_PERCENT})%. Stopping discharging.")
            charge_rate = 0

        # Clip values to min/max charging current
        if charge_rate > c.CHARGER_MAX_CHARGE_POWER:
            self.log(f"Requested charge rate {charge_rate} Watt too high. "
                     f"Changed charge rate to maximum: {c.CHARGER_MAX_CHARGE_POWER} Watt.")
            charge_rate = c.CHARGER_MAX_CHARGE_POWER
        elif abs(charge_rate) > c.CHARGER_MAX_DISCHARGE_POWER:
            self.log(f"Requested discharge rate {charge_rate} Watt too high. "
                     f"Changed discharge rate to maximum: {c.CHARGER_MAX_DISCHARGE_POWER} Watt.")
            charge_rate = -c.CHARGER_MAX_DISCHARGE_POWER

        if charge_rate < 0:
            # Modbus cannot handle negative values directly.
            # ToDo: We should be using modbus utils.get_2comp()
            charge_rate = self.NUM_MODBUS_PORTS + charge_rate

        # Stop charging if power = 0
        if charge_rate == 0:
            self.set_charger_action("stop")

        # If setting in charger is same as requested: do nothing, to prevent switching and waiting time
        register = self.registers["set_power_setpoint"]
        setting_in_charger = None
        total_time = 0
        while True:
            setting_in_charger = self.client.read_holding_registers(register)
            if setting_in_charger == None:
                total_time += 0.25
                # It is only here to prevent setting a duplicate value, not vital.
                if total_time > 2:
                    break
                time.sleep(0.25)
                continue
            else:
                setting_in_charger = setting_in_charger[0]
                setting_in_charger = int(float(setting_in_charger))
                break

        if setting_in_charger == charge_rate:
            # Recalculate for negative values
            if charge_rate > (self.NUM_MODBUS_PORTS / 2):
                charge_rate = charge_rate - self.NUM_MODBUS_PORTS
            self.log(f'New-charge-power-setting is same as current-charge-power-setting: {charge_rate} Watt. '
                     f'Not writing to charger.')
            return

        self.set_setpoint_type("power")
        res = self.client.write_single_register(register, charge_rate)
        time.sleep(self.args["wait_between_charger_write_actions"] / 1000)

        if res is not True:
            self.log(f"Failed to set charge power to {charge_rate} Watt. Charge Point responded with: {res}.")
            # If negative value result in false, check if grid code is set correct in charger.
        else:
            self.log(f"Charge power set to {charge_rate} Watt successfully.")

        return

    def handle_soc_change(self, entity, attribute, old, new, kwargs):
        # todo: move to main app?
        if self.try_get_new_soc_in_process:
            self.log("Handle_soc_change called while getting a soc reading and not really charging. Stop processing "
                     "the soc change")
            return
        reported_soc = new["state"]
        self.log(f"Handle_soc_change called with raw SoC: {reported_soc}")
        res = self.process_soc(reported_soc)
        if not res:
            return
        self.set_next_action()
        return

    def process_soc(self, reported_soc: str) -> bool:
        """Process the reported SoC by saving it to self.connected_car_soc (realistic values only).

        :param reported_soc: string representation of the SoC (in %) as reported by the charger (e.g. "42" denotes 42%)
        :returns: True if a realistic numeric SoC was reported, False otherwise.
        """
        # todo: move to main app?
        try:
            reported_soc = float(reported_soc)
            assert 0 < reported_soc <= 100
        except (TypeError, AssertionError, ValueError):
            self.log(f"New SoC '{reported_soc}' ignored.")
            return False
        self.connected_car_soc = round(reported_soc, 0)
        self.connected_car_soc_kwh = round(reported_soc * float(c.CAR_MAX_CAPACITY_IN_KWH / 100), 2)
        tmp = int(round((self.connected_car_soc_kwh*1000/self.CAR_AVERAGE_WH_PER_KM), 0))
        self.set_value("input_number.car_remaining_range", tmp)
        self.log(f"New SoC processed, self.connected_car_soc is now set to: {self.connected_car_soc}%.")
        self.log(f"New SoC processed, car_remaining_range is now set to: {tmp} km.")

        # Notify user of reaching 80% charge while charging (not dis-charging).
        # ToDo: Discuss with users if this is useful.
        if self.connected_car_soc == c.CAR_MAX_SOC_IN_PERCENT and self.is_charging():
            message = f"Car battery at {self.connected_car_soc} %, range ≈ {tmp} km."
            self.notify_user(
                message     = message,
                title       = None,
                tag         = "battery_max_soc_reached",
                critical    = False,
                send_to_all = True,
                ttl         = 60*15
            )
        return True

    def handle_charger_in_error(self):
        # If charger remains in error state more than 7 minutes restart the charger.
        # We wait 7 minutes as the charger might be return an error state up until 5 minutes after a restart.
        # The default charger_in_error_since is filled with the reference date.
        # At the registration of an error charger_in_error_since is set to now.
        # This way we know "checking for error" is in progress if the charger_in_error_since is
        # not equal to the reference date.

        if not self.is_charger_in_error():
            self.log("handle_charger_in_error, charger not in error_state anymore (due to restart?), cancel further "
                     "error processing.")
            self.charger_in_error_since = self.date_reference
            return

        if self.charger_in_error_since == self.date_reference:
            self.log("handle_charger_in_error, new charger_error_state detected, check if error persists for next 7 "
                     "minutes, then preform restart.")
            self.charger_in_error_since = self.get_now()

        if (self.get_now() - self.charger_in_error_since).total_seconds() > 7 * 60:
            self.log("handle_charger_in_error, check if error_state persists for next 7 minutes, then preform restart.")
            self.charger_in_error_since = self.date_reference
            self.set_charger_action("restart")
        else:
            self.log("handle_charger_in_error, recheck error_state in 30 seconds.")
            self.run_in(self.handle_charger_in_error, 30)

    def handle_charger_state_change(self, entity, attribute, old, new, kwargs):

        # Ignore SoC state change when the app is in the process of getting a SoC reading
        if self.try_get_new_soc_in_process:
            # self.log(
            #     f"The handle_charger_state_change called while getting a soc reading and not really charging. Stop processing the state change to {new['state']}")
            return

        new_charger_state = new["state"]
        if isinstance(new_charger_state, str):
            if not new_charger_state.isnumeric():
                return
            new_charger_state = int(float(new_charger_state))

        if self.current_charger_state == new_charger_state:
            # Nothing has changed really. Update but not a change.
            # self.log(f"It now appears the Charger state has not changed at all.")
            return
        self.log(f"Charger state changed from {self.current_charger_state} to {new_charger_state}.")

        # We do not use the oldstate from arguments as this also includes states with "unavailable" etc.
        old_charger_state = self.current_charger_state
        self.current_charger_state = new_charger_state

        # **** Handle Power Boost queue
        # The charger will lower the charging power if the power demand from the house becomes too big for one phase.
        if new_charger_state == self.registers["in_queue_state"]:
            self.log("Charger state has changed to 'Connected: in queue by Power Boost'")
            # We just notify FM?
            # We just wait for queue to resolve, then the status will return to paused/waiting for car demand
            return

        # ****Handle error
        if new_charger_state == self.registers["error_state"]:
            self.log("Charger_state is: error. Charger can remain in this state up to 5 min. after reboot.")
            self.log_errors()
            self.handle_charger_in_error()
            return

        # **** Handle disconnect:
        # Goes to this status when the plug is removed from the socket (not when disconnect is requested from the UI)
        if new_charger_state == self.DISCONNECTED_STATE:
            self.log("Charger state has changed to 'Disconnected'")

            # Reset any possible target for discharge due to SoC > max-soc
            self.back_to_max_soc = None

            # Cancel current scheduling timers
            self.cancel_charging_timers()

            # This might seem strange but sometimes the charger starts charging when
            # reconnected even though it has not received an instruction to do so.
            self.set_charger_action("stop")

            # Setting charge_mode set to automatic (was Max boost Now) as car is disconnected.
            mode = self.get_state("input_select.charge_mode", None)
            if mode == "Max boost now":
                self.set_chargemode_in_ui("Automatic")
                self.notify_user(
                    message     = "Chargemode set from 'Max charge now' to 'Automatic' as car is disconnected.",
                    title       = None,
                    tag         = "charge_mode_change",
                    critical    = False,
                    send_to_all = True,
                    ttl         = 15*60
                )
            return

        # **** Handle connected:
        if new_charger_state in self.registers["idle_states"]:
            self.log("Charger state has changed to an idle state")

            if old_charger_state == self.DISCONNECTED_STATE:
                self.log('From disconnected to connected: try to refresh the SoC')
                self.try_get_new_soc()

            self.set_next_action()
            return

        # **** Handle (dis)charging:
        if new_charger_state in self.registers["charging_states"]:
            self.log("Charger state has changed to (dis)charging)")
            return

        self.log(f"Charger state changed, but was not processed due to unknown state: {new['state']}.")

    def log_errors(self):
        """Log all errors."""
        for i, register in enumerate(self.registers["error_registers"], 1):
            error_code = self.client.read_holding_registers(register)
            try:
                error_code = error_code[0]
            except TypeError:
                self.log(f"Modbus read setpoint_type seems not iterable: {error_code}.")
            self.log(f"Error code {i} is: {error_code}")

    def try_get_new_soc(self):
        # With a connect the SoC does not update automatically.
        # If read at this point it normally (always?) returns a 0.
        # So we need to start a charge with minimal power, try to read the SoC and asap stop the charge.
        # The side effects are possible soc changes and charger state changes.
        # When we observe such changes we ignore them while we are in the process of obtaining a SoC reading

        self.try_get_new_soc_in_process = True
        is_currently_charging = self.is_charging()
        # If currently charging then reading the SoC should be possible
        # Then keep charging rate as it was, otherwise start charging with minimal
        # power to be able to read the SoC.
        if not is_currently_charging:
            self.log(f"Reading SoC, starting charging so a SoC can be read.")
            # Set minimal charging power 1 Watt
            self.set_power_setpoint(1)
            self.set_charger_action("start")
        register = self.registers["get_car_state_of_charge"]

        # The idea is the start will make the real SoC available.
        reported_soc = 0
        total_time = 0

        # If the real SoC is not available yet, keep trying for max. two minutes
        while reported_soc == 0:
            # Keep the waiting time between reads short. Charging might trigger a SoC change and then we get conflicting actions.
            time.sleep(0.25)
            reported_soc = self.client.read_holding_registers(register)
            if reported_soc == None or reported_soc == "unavailable":
                reported_soc = 0
            else:
                try:
                    reported_soc = reported_soc[0]
                    reported_soc = int(float(reported_soc))
                except TypeError:
                    self.log(f"Modbus read object seems not iterable or cannot covert to int: {reported_soc}.")
                    reported_soc = 0
            total_time += 0.25

            # We need to stop at some point
            if total_time > 120:  # todo: refactor these to config settings
                self.log(f"Reading SoC timed out. After {total_time} seconds still no relevant SoC was retrieved.")
                break
            if self.try_get_new_soc_in_process is False:
                # Function try_stop_get_new_soc can set this to false to stop the processing here
                self.log(f"Try_get_new_soc externally stopped.")
                break
        else:
            self.log(f"Read SoC from car (poked charger by starting minimal charge): '{reported_soc}', "
                     f"time before relevant SoC was retrieved: {total_time}seconds.")

        if not is_currently_charging:
            self.set_charger_action("stop")
            self.set_power_setpoint(0)
        self.try_get_new_soc_in_process = False
        self.process_soc(reported_soc)

    def try_stop_get_new_soc(self):
        # When switching to chargemode it is needed to interrupt try_get_new_soc()
        self.try_get_new_soc_in_process = False

    def start_max_charge_now(self):
        """Set the charger to charge at maximal rate.

        Note that Power Boost may in practice curtail the maximal rate to prevent overloading.
        """
        self.log("start_max_charge_now called")
        self.set_charger_control("take")
        self.set_power_setpoint(c.CHARGER_MAX_CHARGE_POWER)
        self.set_charger_action("start")

    def restart_charger_and_appdaemon(self):
        """Restart the charger via Wallbox API."""
        self.restart_charger()
        self.restart_appdaemon(delay=3 * 60)

    def restart_appdaemon(self, delay: int):
        """Restart appdaemon in delay seconds."""
        kwargs = dict(service="app/restart", app="V2Gliberty", namespace="admin")
        self.run_in(self.call_service, delay, **kwargs)

    def restart_charger(self):
        """Restart the charger via Wallbox API."""
        self.log("restart_charger called")
        from wallbox import Wallbox, Statuses

        user = self.args["wallbox_api_user"]
        pw = self.args["wallbox_api_pw"]
        w = Wallbox(user, pw)

        # Authenticate with the credentials above
        w.authenticate()

        # Verify that the account has a single charger
        charger_ids = w.getChargersList()
        assert len(charger_ids) == 1, "Charger restart functionality expects a single charger on your account."
        charger_id = charger_ids[0]

        # Get charger data for all chargers in the list, then lock and unlock chargers
        charger_status = w.getChargerStatus(charger_id)
        self.log(f"Charger Status before sending restart instruction: {charger_status}")
        w.restartCharger(charger_id)
        charger_status = w.getChargerStatus(charger_id)
        self.log(f"Charger Status after sending restart instruction: {charger_status}")


class RegisterModule(hass.Hass):
    """Just here to make sure AppDaemon refreshes this module upon saving the code."""

    def initialize(self):
        pass
