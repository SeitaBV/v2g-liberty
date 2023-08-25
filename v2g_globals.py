from datetime import datetime, timedelta
import time

import appdaemon.plugins.hass.hassapi as hass

import constants as c

class V2GLibertyGlobals(hass.Hass):

    def initialize(self):
        self.log("Initializing V2GLibertyGlobals")

        efficiency = int(float(self.args["charger_plus_car_roundtrip_efficiency"]))
        # Make sure this value is between 50 en 100
        tmp = max(min(100, efficiency), 50)
        if efficiency != tmp:
            self.log(f"Roundtrip efficienty is changed from {efficiency} to {tmp} to stay within boundries.")
            efficiency = tmp
        # set constant so that it can be used in calculations
        c.CHARGER_PLUS_CAR_ROUNDTRIP_EFFICIENCY = efficiency/100
        self.log(f"v2g_globals roundtrip-efficiency: {c.CHARGER_PLUS_CAR_ROUNDTRIP_EFFICIENCY}.")

        max_capacity = int(float(self.args["car_max_capacity_in_kwh"]))
        # Make sure this value is between 10 en 200
        tmp = max(min(200, max_capacity), 10)
        if max_capacity != tmp:
            self.log(f"Max_car_capacity is changed from {max_capacity} to {tmp} to stay within boundries.")
            max_capacity = tmp
        c.CAR_MAX_CAPACITY_IN_KWH = max_capacity
        self.log(f"v2g_globals max-car-capacity: {c.CAR_MAX_CAPACITY_IN_KWH} kWh.")

        car_min_soc = int(float(self.args["car_min_soc_in_percent"]))
        # Make sure this value is between 10 en 30
        tmp = max(min(30, car_min_soc), 10)
        if car_min_soc != tmp:
            self.log(f"car_min_soc is changed from {car_min_soc} to {tmp} to stay within boundries.")
            car_min_soc = tmp
        c.CAR_MIN_SOC_IN_PERCENT = car_min_soc
        self.log(f"v2g_globals car-min-soc: {c.CAR_MIN_SOC_IN_PERCENT} %.")

        car_max_soc = int(float(self.args["car_max_soc_in_percent"]))
        # Make sure this value is between 60 en 100
        tmp = max(min(100, car_max_soc), 60)
        if car_max_soc != tmp:
            self.log(f"car_max_soc is changed from {car_max_soc} to {tmp} to stay within boundries.")
            car_max_soc = tmp
        c.CAR_MAX_SOC_IN_PERCENT = car_max_soc
        self.log(f"v2g_globals car-max-soc: {c.CAR_MAX_SOC_IN_PERCENT} %.")

        c.FM_ACCOUNT_POWER_SENSOR_ID = int(float(self.args["fm_account_power_sensor_id"]))
        self.log(f"v2g_globals FM_ACCOUNT_POWER_SENSOR_ID: {c.FM_ACCOUNT_POWER_SENSOR_ID}.")
        c.FM_ACCOUNT_AVAILABILITY_SENSOR_ID = int(float(self.args["fm_account_availability_sensor_id"]))
        self.log(f"v2g_globals FM_ACCOUNT_AVAILABILITY_SENSOR_ID: {c.FM_ACCOUNT_AVAILABILITY_SENSOR_ID}.")
        c.FM_ACCOUNT_SOC_SENSOR_ID = int(float(self.args["fm_account_soc_sensor_id"]))
        self.log(f"v2g_globals FM_ACCOUNT_SOC_SENSOR_ID: {c.FM_ACCOUNT_SOC_SENSOR_ID}.")

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


        #For later PR..
        #c.CAR_AVARAGE_WH_PER_KM = int(float(self.args["car_avarage_wh_per_km"]))
        #self.log(f"v2g_globals CAR_AVARAGE_WH_PER_KM: {c.CAR_AVARAGE_WH_PER_KM}.")
        self.log("Completed initializing V2GLibertyGlobals")



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
