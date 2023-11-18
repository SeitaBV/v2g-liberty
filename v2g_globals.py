from datetime import datetime, timedelta
import time

import appdaemon.plugins.hass.hassapi as hass

import constants as c

class V2GLibertyGlobals(hass.Hass):

    def initialize(self):
        self.log("Initializing V2GLibertyGlobals")

        c.CHARGER_PLUS_CAR_ROUNDTRIP_EFFICIENCY = self.read_and_process_int_setting("charger_plus_car_roundtrip_efficiency", 50, 100)/100
        self.log(f"v2g_globals roundtrip-efficiency: {c.CHARGER_PLUS_CAR_ROUNDTRIP_EFFICIENCY}.")

        c.CHARGER_MAX_CHARGE_POWER = self.read_and_process_int_setting("charger_max_charging_power", 1380, 22000)
        self.log(f"v2g_globals max charge power: {c.CHARGER_MAX_CHARGE_POWER} Watt.")

        c.CHARGER_MAX_DISCHARGE_POWER = self.read_and_process_int_setting("charger_max_discharging_power", 1380, 22000)
        self.log(f"v2g_globals max dis-charge power: {c.CHARGER_MAX_DISCHARGE_POWER}.")

        c.CAR_MAX_CAPACITY_IN_KWH = self.read_and_process_int_setting("car_max_capacity_in_kwh", 10, 200)
        self.log(f"v2g_globals max-car-capacity: {c.CAR_MAX_CAPACITY_IN_KWH} kWh.")

        c.CAR_MIN_SOC_IN_PERCENT = self.read_and_process_int_setting("car_min_soc_in_percent", 10, 30)
        self.log(f"v2g_globals car-min-soc: {c.CAR_MIN_SOC_IN_PERCENT} %.")

        c.CAR_MAX_SOC_IN_PERCENT = self.read_and_process_int_setting("car_max_soc_in_percent", 60, 100)
        self.log(f"v2g_globals car-max-soc: {c.CAR_MAX_SOC_IN_PERCENT} %.")

        c.FM_ACCOUNT_POWER_SENSOR_ID = int(float(self.args["fm_account_power_sensor_id"]))
        self.log(f"v2g_globals FM_ACCOUNT_POWER_SENSOR_ID: {c.FM_ACCOUNT_POWER_SENSOR_ID}.")
        c.FM_ACCOUNT_AVAILABILITY_SENSOR_ID = int(float(self.args["fm_account_availability_sensor_id"]))
        self.log(f"v2g_globals FM_ACCOUNT_AVAILABILITY_SENSOR_ID: {c.FM_ACCOUNT_AVAILABILITY_SENSOR_ID}.")
        c.FM_ACCOUNT_SOC_SENSOR_ID = int(float(self.args["fm_account_soc_sensor_id"]))
        self.log(f"v2g_globals FM_ACCOUNT_SOC_SENSOR_ID: {c.FM_ACCOUNT_SOC_SENSOR_ID}.")
        c.FM_ACCOUNT_COST_SENSOR_ID = int(float(self.args["fm_account_cost_sensor_id"]))
        self.log(f"v2g_globals FM_ACCOUNT_COST_SENSOR_ID: {c.FM_ACCOUNT_COST_SENSOR_ID}.")

        c.OPTIMISATION_MODE = self.args["fm_optimisation_mode"].strip().lower()
        self.log(f"v2g_globals OPTIMISATION_MODE: {c.OPTIMISATION_MODE}.")
        c.ELECTRICITY_PROVIDER = self.args["electricity_provider"].strip().lower()
        self.log(f"v2g_globals ELECTRICITY_PROVIDER: {c.ELECTRICITY_PROVIDER}.")


        # The utility prrovides the electricity, if the price and emissions data is provided to FM
        # by V2G Liberty this is labeled as "self_provided".
        if c.ELECTRICITY_PROVIDER == "self_provided":
            c.FM_PRICE_PRODUCTION_SENSOR_ID = int(float(self.args["fm_own_price_production_sensor_id"]))
            c.FM_PRICE_CONSUMPTION_SENSOR_ID = int(float(self.args["fm_own_price_consumption_sensor_id"]))
            c.FM_EMISSIONS_SENSOR_ID = int(float(self.args["fm_own_emissions_sensor_id"]))
            c.UTILITY_CONTEXT_DISPLAY_NAME = self.args["fm_own_context_display_name"]
        else:
            context = c.DEFAULT_UTILITY_CONTEXTS.get(
                c.ELECTRICITY_PROVIDER,
                c.DEFAULT_UTILITY_CONTEXTS["nl_generic"],
            )
            #ToDo: Notify user if fallback "nl_generic" is used..
            c.FM_PRICE_PRODUCTION_SENSOR_ID = context["production-sensor"]
            c.FM_PRICE_CONSUMPTION_SENSOR_ID = context["consumption-sensor"]
            c.FM_EMISSIONS_SENSOR_ID = context["emisions-sensor"]
            c.UTILITY_CONTEXT_DISPLAY_NAME = context["display-name"]
        self.log(f"v2g_globals FM_PRICE_PRODUCTION_SENSOR_ID: {c.FM_PRICE_PRODUCTION_SENSOR_ID}.")
        self.log(f"v2g_globals FM_PRICE_CONSUMPTION_SENSOR_ID: {c.FM_PRICE_CONSUMPTION_SENSOR_ID}.")
        self.log(f"v2g_globals FM_EMISSIONS_SENSOR_ID: {c.FM_EMISSIONS_SENSOR_ID}.")
        self.log(f"v2g_globals UTILITY_CONTEXT_DISPLAY_NAME: {c.UTILITY_CONTEXT_DISPLAY_NAME}.")

        self.log("Completed initializing V2GLibertyGlobals")

    def read_and_process_int_setting(self, setting_name: str, lower_limit: int, upper_limit: int) -> int:
        """Read and integer setting_name from HASS and guard the lower and upper limit"""
        reading = int(float(self.args[setting_name]))
        # Make sure this value is between lower_limit and upper_limit
        tmp = max(min(upper_limit, reading), lower_limit)
        if reading != tmp:
            self.log(f"{setting_name} is changed from {reading} to {tmp} to stay within boundaries.")
            reading = tmp
        return reading


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
