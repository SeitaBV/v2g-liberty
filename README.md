# V2G Liberty: optimised vehicle-to-grid charging of your EV

This integration lets you add full automatic and price optimized control over Vehicle to grid (V2G) charging. It has a 
practical local app in [HomeAssistant](https://www.home-assistant.io/) and uses the smart EMS [FlexMeasures](https://flexmeasures.io) for optimized schedules.

The schedules are optimized on day-ahead energy prices, so this works best with an electricity contract with dynamic (hourly) prices[^1].
We intend to add optimisation for your solar generation in the near future.

[^1]: For now: most Dutch energy suppliers are listed and all European energy prices (EPEX) are available for optimisation. There also is an option to upload your own prices, if you have an interest in this, please [contact us](https://v2g-liberty.eu/) to see what the options are.

![The V2G Liberty Dashboard](https://positive-design.nl/wp-content/uploads/2022/04/V2GL-1-1024x549.png)

You can read more about the project and its vision [here](https://v2g-liberty.eu/) and [here](https://seita.nl/project/v2ghome-living-lab/).

In practice, V2G Liberty does the following:
- In automatic mode: No worries, just plugin when you return home and let the system automatically optimize charging. 
- Set targets (e.g. be charged 100% at 7am tomorrow) which prompts FlexMeasures to update its schedules.
- Override the system and set charging to "Max Boost Now" mode in cases where you need as much battery SoC a possible quickly.

This integration is a Python app and uses:

- FlexMeasures for optimizing charging schedules. FlexMeasures is periodically asked to generate optimized charging schedules.
- Home Assistant for automating local control over your Wallbox Quasar. V2G Liberty translates this into set points which it sends to the Wallbox Quasar via modbus.
- The AppDaemon plugin for Home Assistant for running the Python app.

![V2G Liberty Architecture](https://user-images.githubusercontent.com/6270792/216368533-aa07dfa7-6e20-47cb-8778-aa2b8ba8b6e1.png)

### Prerequisites
 
At the time of writing, 2024-01, only the [Wallbox Quasar 1 charger](https://wallbox.com/en_uk/quasar-dc-charger) is supported.
This is a [CHAdeMO](https://www.chademo.com/) compatible charger.
Compatible cars that can do V2G with this protocol are the [Nissan Leaf](https://ev-database.org/car/1657/Nissan-Leaf-eplus) (also earlier models) and [Nissan Evalia](https://ev-database.org/car/1117/Nissan-e-NV200-Evalia).
When the Quasar 2 will be available in the EU we expect V2G Liberty to be compatible with this hardware right away.
Then also CCS V2G capable cars can be managed with V2G Liberty.
Hopefully other chargers will follow soon.

## Preparation

Before installing or activation of V2G Liberty, please make sure that charging and discharging with the car and charger works properly.
Test this with the app supplied with the charger.
This is important because V2G Liberty will 'take control' over the charger.
Operating the charger either through the screen of the charger or the app will not work (reliably) anymore when V2G Liberty is active.

### FlexMeasures

The cloud backend [FlexMeasures](https://github.com/FlexMeasures/flexmeasures) provides the smart schedules needed for optimizing the charging and discharging.
You can run your own instance of FlexMeasures, but you can also make use of an instance run by Seita.
If you prefer this option, please [contact us](https://v2g-liberty.eu).

### Home assistant

As said, Home Assistant (from now on we’ll abbreviate this to HA) forms the basis of V2G Liberty.
So before you can start with V2G Liberty you'll need a running HA in your network. Both HA and the charger need to be on the same network and must be able to communicate.
HA typically runs on a small computer, e.g. a Raspberry PI, but it can also run on a NAS or an old laptop.
HA has some [suggestions for hardware](https://www.home-assistant.io/blog/2022/04/16/device-to-run-home-assistant/).
For the installation of HA on your (edge) computer, [guides](https://www.home-assistant.io/getting-started/) can be found online and help can be sought in many active forums.

### An electricity contract with dynamic prices

As said, the software optimizes for EPEX day-ahead prices, so a contract of this type is the best option.
There is no rush, though.
You can try out V2G Liberty first and later on get the dynamic contract.
In the Netherlands there are several suppliers, o.a. ANWB, Energy Zero, Tibber, Zonneplan, etc.
This changes the way your electricity is priced and billed, so it is wise to find information and make sure you really understand what this means for your situation before making this change.

### Get a GitHub account

You’ll need a GitHub account for getting the V2G Liberty source code and for setting up HACS (see later).
So go ahead and [get a GitHub account](https://github.com/signup) (it is free) if you do not have this yet.

### Get an online calendar

For the situations where you would like the car to be fully charged, e.g. for a longer trip, V2G Liberty optimizes on a dedicated digital (online) calendar.
An online/digital calendar is mandatory, without it V2G Liberty cannot work.

It is of course most useful if the calendar is integrated with your normal calendar and if you can easily edit the calendar items on your smartphone (outside HA / V2G Liberty).
You make the appointments on your phone or laptop directly, not through HA.
Home Assistant only reads the appointments from the online calendar.
Options are, for example:
- A CalDav compatible calendar. E.g. NextCloud or OwnCloud if you’d like an open-source solution
- iCloud, this can be reached through CalDav (or through the HA integration – no examples yet)
- Google calendar. This works fine with the Google Calendar integration in Home Assistant (not to be confused with Google Assistant)
- Office 365. Via non-official O365-HomeAssistant integration, see GitHub
- ICS Calendar (or iCalendar) integration, also non-official. It can be found on HACS.

We recommend a separate calendar for the car reservations.
Filtering only the car reservations is also an option.
The result must be that in Home Assistant only the events meant for the car are present.
Preferably name the calendar (`car_reservation`). If you name(d) it otherwise, update the calendar name in the 
configuration of V2G Liberty secrets.yaml.
[Read more here](#car-reservations) about how car reservations work in practice.

---

## Installation

Now that you have a running Home Assistant, you're ready to install V2G Liberty in Home Assistant.
We'll take you through the process step by step.

### Install HACS

The Home Assistant Community Store (HACS) has loads of integrations that are not in the official store of HA.
You'll need two of these and that's why you also need to install this add-on.
It is explained on the [HACS installation pages](https://hacs.xyz).
As a reference you might find this [instruction video](https://www.youtube.com/watch?v=D6ZlhE-Iv9E) a handy resource.

### Add modules to HA via HACS

Add the following modules to HA through HACS:
- [ApexChart-card](https://github.com/RomRider/apexcharts-card)<br>
  This is needed for graphs in the user interface.
- [Custom-card](https://github.com/thomasloven/lovelace-card-mod)<br>
  This is needed for a better UI.

There are other modules that might look interesting, like Leaf Spy, but you do not need any of these for V2G-L.
After installation the reference to these resources has to be added through menu:
1. Make sure, advanced mode is enabled in your user profile (click on your username to get there)
2. Navigate to Settings -> Dashboards -> from the top right menu (&vellip;) select resources.
3. Click (+ ADD RESOURCE) button and enter URL `/hacsfiles/apexcharts-card/apexcharts-card.js` and select type "JavaScript Module".
4. Repeat for `/hacsfiles/lovelace-card-mod/card-mod.js`
5. Restart Home Assistant.


## Configure HA

This is not complicated, but you'll need to work precise. The configuration of HA is stored in .yaml files, these can be edited in the HA file editor (in the left main menu).

  > For version 0.2.0 the folder structure has changed significantly due to AppDaemon requirements, please check carefully.

After completion of this step you'll end up with a these files and folders (others might be there but are not shown here). Some might be already present and only need editing. Others files or folders might need to added. The files you'll have to edit are marked with *.

```
. (root = HA config folder)
├── packages (possibly this need to be created)
│   └── v2g-liberty
│       ├── table_style.yaml
│       ├── v2g-liberty-dashboard.yaml
│       ├── v2g-liberty-package.yaml
│       └── v2g_liberty_ui_module_stats.yaml
├── configuration.yaml *
└── secrets.yaml *

. (root = AppDaemon addon_configs folder, usually called a0d7b954_appdaemon)
├── apps
│   ├── v2g-liberty
│   │   ├── constants.py
│   │   ├── flexmeasures_client.py
│   │   ├── get_fm_data.py
│   │   ├── LICENSE
│   │   ├── README.md
│   │   ├── set_fm_data.py
│   │   ├── v2g_globals.py
│   │   ├── v2g_liberty.py
│   │   ├── wallbox_client.py
│   │   └── wallbox_modbus_registers.yaml
│   └ apps.yaml *
├── logs (possibly this need to be created)
└ appdaemon.yaml *
```

### Secrets

HA stores secrets in the file `secrets.yaml` and V2G Liberty expects this file to be in the default location, the config folder.
We store both secrets and configuration values in this file as this is the most convenient way for storing these.
Open this file in the HA file editor and add the following code. You'll need to replace secrets/values for your custom setting.
If you have installed the Studio Code Server addon (not mandatory), you can use that.


```yaml
################################################################################
#                                                                              #
#    V2G Liberty Configuration                                                 #
#    Contains all settings that need to be set for you, usually secrets        #
#    such as passwords, usernames, ip-addresses and entity addresses.          #
#                                                                              #
#    It is also used for storing variables for use in the app configuration.   #
#                                                                              #
#    After changes have been saved restart HA and AppDaemon.                   #
#                                                                              #
################################################################################

#############   BASIC HA CONFIG   ##############################################
## ALWAYS CHANGE ##
# Provide the coordinates of the location.
# Typical: lat. 52.xxxxxx,  lon. 4.xxxxxx, elevation in meters.
# ToDo: use these settings from Home Assistant instead
ha_latitude: xx.xxxxxxx
ha_longitude: x.xxxxxxx
ha_elevation: x
ha_time_zone: Europe/Amsterdam

# To which mobile device must (critical) platform notifications be sent.
# Can be found in the home assistant companion app under:
# Settings > Companion App > (Top item) your name and server > Device name
# Replace any spaces, minus (-), dots(.) with underscores (_)
admin_mobile_name: "your_device_name"
# Should be iOS or Android, others are not supported.
admin_mobile_platform: "your platform name: iOS or Android"

#############   FLEXMEASURES CONFIGURATION   ###################################

## ALWAYS CHANGE ##
fm_user_email: "your FM e-mail here (use quotes)"
fm_user_password: "your FM password here (use quotes)"

fm_account_power_sensor_id: XX
fm_account_availability_sensor_id: XX
fm_account_soc_sensor_id: XX
fm_account_cost_sensor_id: XX

# For electricity_provider the choices are:
#   nl_generic * †
#   no_generic * †
# Or one of the Dutch energy companies (VAT and markup are set in FlexMeasures):
#   nl_anwb_energie
#   nl_next_energy
#   nl_tibber
# If your energy company is missing, please let us know and we'll add it to the list.
# If you send your own prices (and emissions) data to FM through the API then use.
#   self_provided †
#
#  * In these cases it is assumed consumption and production price are the same.
#  † For these you can/should provide VAT and Markup (see further down).
electricity_provider: "nl_generic"

# How would you'd like the charging / discharging to be optimised?
# Choices are price or emission
fm_optimisation_mode: "price"

# For option "own-prices" the FM account has it's onw sensor_id's
fm_own_price_production_sensor_id: pp
fm_own_price_consumption_sensor_id: cc
fm_own_emissions_sensor_id: ee
fm_own_context_display_name: "Own Prices and Emissions"

# ****** Pricing data ********
# Pricing data only needs to be provided if a generic electricity provider is used
# For transforming the raw price data (from FM) to net price to be shown in UI.
# Calculation:
# (market_price_per_kwh + markup_per_kwh) * VAT

# Value Added Tax.
# This is only taken into account for electricity_providers marked with †
# Use a calculation factor (100 + VAT / 100).
# E.g. for NL VAT is 21%, so factor is 1.21. Use dot (.) not comma (,).
# If you'd like to effectively "not use VAT" you can set it to 1
VAT: 1.21

# Markup per kWh
# This is only taken into account for electricity_providers marked with †
# This usually includes energy tax and supplier markup
# Energy tax per kWh excluding VAT.
# Markup in €ct/kWh, Use dot (.) not comma (,).
# If you'd like to effectively "not use a markup" you can set it to 0
markup_per_kwh: 14.399


## VERY RARELY CHANGE ##
fm_base_entity_address_power: "ea1.2022-03.nl.seita.flexmeasures:fm1."
fm_base_entity_address_availability: "ea1.2022-03.nl.seita.flexmeasures:fm1."
fm_base_entity_address_soc: "ea1.2022-03.nl.seita.flexmeasures:fm1."

# This represents how far ahead the schedule should look. Keep at this setting.
fm_schedule_duration: "PT27H"

#############   CHARGER CONFIGURATION   ########################################

## ALWAYS CHANGE ##
# This usually is an IP address but can be a named URL as well.
wallbox_host: "your charger host here"
# Usually 502
wallbox_port: 502

## ALWAYS CHECK / SOME TIMES CHANGE ##
# Research shows the roundtrip efficient is around 85 % for a typical EV + charger.
# This number is taken into account when calculating the optimal schedule.
# Use an integer between 50 and 100.
charger_plus_car_roundtrip_efficiency: 85

#############   CAR & POWER-CONNECTION CONFIGURATION   #########################
## ALWAYS CHECK/CHANGE ##

# The usable energy storage capacity of the battery of the car, as an integer.
# For the Nissan Leaf this is usually 21, 39 or 59 (advertised as 24, 40 and 62).
# See https://ev-database.org.
# Use an integer between 10 and 200.
car_max_capacity_in_kwh: 59

# What would you like to be the minimum charge in your battery?
# The scheduling will not discharge below this value and if the car returns with
# and SoC below this value, the battery will be charged to this minimum asap
# before regular scheduling.
# A high value results in always having a greater driving range available, even
# when not planned, but less capacity available for dis-charge and so lesser
# earnings.
# A lower value results in sometimes a smaller driving range available for
# un-planned drives but there is always more capacity for discharge and so more
# earnings.
# Some research suggests battery life is shorter if min SoC is below 15%.
# In some cars the SoC sometimes skips a number, e.g. from 21 to 19%,
# skipping 20%. This might result in toggling charging behaviour around this
# minimum SoC. If this happens try a value 1 higher or lower.
# The setting must be an integer (without the % sign) between 10 and 30, default is 20.
car_min_soc_in_percent: 20

# What would you like to be the maximum charge in your car battery?
# The schedule will use this for regular scheduling. It can be used to further
# protect the battery from degradation as a 100% charge (for longer periods) may
# reduce battery health/lifetime.
# When a calendar item is present, the schedule will ignore this setting and
# try to charge to 100% (or if the calendar item has a target use that).
# A low setting reduces schedule flexibility and so the capability to earn
# money and reduce emissions.
# The setting must be an integer value between 60 and 100, default is 80.
car_max_soc_in_percent: 80

# When the car connects with a SoC higher than car_max_soc_in_percent
# how long may the schedule take to bring the SoC back to this maximum?
# A longer duration gives opportunity for higher gains but might have a (minor)
# degradation effect on the battery.
# This duration is excluding the (minimum) time it takes to get back to the
# desired maximum under normal cycling conditions.
# The setting must be an integer value between 2 and 36, default is 12.
allowed_duration_above_max_soc_in_hrs: 12

# What is the average electricity usage of your car in watt-hour (Wh) per km?
# In most cars you can find historical data in the menu's. Normally this is somewhere
# between 140 (very efficient!) and 300 (rather in-efficient vans).
# Make sure you use the right "unit of measure": Wh.
# The setting must be an integer value.
car_average_wh_per_km: 174

# Max (dis-)charge_power in Watt
#   Be safe:
#   Please consult a certified electrician what max power can be set on
#   the charger. Electric safety must be provided by the hardware. Limits for over
#   powering must be guarded by hardware.
#   This software should not be the only fail-safe.
#   It is recommended to use a load balancer.
#
# If a load balancer (power-boost for WB) is used:
# Set this to "Amperage setting in charger" * grid voltage.
# E.g. 25A * 230V = 5750W.
# If there is no load balancer in use, use a lower setting.
# Usually the discharge power is the same but in some cases the charger or
# (gird operator) regulations require a different (lower) dis-charge power.
wallbox_max_charging_power: XXXX
wallbox_max_discharging_power: XXXX

#############   CALENDAR CONFIGURATION   #######################################

# Configuration for the calendar for making reservations for the car #
# This is mandatory
# It can be a Google calendar (please use the Google calendar integration for HomeAssistant)
car_calendar_name: calendar.car_reservation
# This normally matches the ha_time_zone setting.
car_calendar_timezone: Europe/Amsterdam

## Remove these if another calendar is used.
## Please also remove the calendar related entities in v2g_liberty_package.yaml
caldavUN: "your caldav username here (use quotes)"
caldavPWD: "your caldav password here (use quotes)"
caldavURL: "your caldav URL here (use quotes)"

```

### Copy & edit files

In your Home Assistant file editor, go to `/config/appdaemon/apps/` and create a new folder `v2g-liberty`.
Within the v2g-liberty folder create a new folder `app-config`.
From this GitHub project copy all files to the respective folders.

## Install the AppDaemon 4 add-on

AppDaemon is an official add-on for HA and thus can be installed from within HA.
Please go to Settings -> Add-ons -> Add-on store and find the AppDaemon add-on.

***Unfortunately V2G Liberty does currently only work with version 0.14.0, we are doing our best to work with newer versions.***

When installed AppDaemon needs to be configured, look for (`Supervisor -> AppDaemon 4 -> Configuration`) and add the following Python packages:

```yaml
python_packages:
  - isodate
  - pyModbusTCP
```
### AppDaemon configuration

To configure AppDaemon you'll need to add the following to the appdaemon.yaml file.

```yaml
---
secrets: /homeassistant/secrets.yaml

appdaemon:
  latitude: !secret ha_latitude
  longitude: !secret ha_longitude
  elevation: !secret ha_elevation
  time_zone: !secret ha_time_zone
  production_mode: True
  exclude_dirs:
    - app_config
  plugins:
    HASS:
      type: hass

http:
  url: http://127.0.0.1:5050
admin:
api:
hadashboard:

```

### Apps.yaml

In the same directory, add (or extend) `apps.yaml` with the following.
Usually there is no need to change any of the values as all personal settings are referenced from the secrets file.

```yaml
---
v2g-globals:
  module: v2g_globals
  class: V2GLibertyGlobals
  # This needs to load before all other modules
  priority: 10

  charger_plus_car_roundtrip_efficiency: !secret charger_plus_car_roundtrip_efficiency
  charger_max_charging_power: !secret wallbox_max_charging_power
  charger_max_discharging_power: !secret wallbox_max_discharging_power

  car_max_capacity_in_kwh: !secret car_max_capacity_in_kwh
  car_min_soc_in_percent: !secret car_min_soc_in_percent
  car_max_soc_in_percent: !secret car_max_soc_in_percent
  allowed_duration_above_max_soc_in_hrs: !secret allowed_duration_above_max_soc_in_hrs

  fm_account_power_sensor_id: !secret fm_account_power_sensor_id
  fm_account_availability_sensor_id: !secret fm_account_availability_sensor_id
  fm_account_soc_sensor_id: !secret fm_account_soc_sensor_id
  fm_account_cost_sensor_id: !secret fm_account_cost_sensor_id

  fm_optimisation_mode: !secret fm_optimisation_mode
  electricity_provider: !secret electricity_provider

  # If electricity_provider is set to "self-provided"
  fm_own_price_production_sensor_id: !secret fm_own_price_production_sensor_id
  fm_own_price_consumption_sensor_id: !secret fm_own_price_consumption_sensor_id
  fm_own_emissions_sensor_id: !secret fm_own_emissions_sensor_id
  fm_own_context_display_name: !secret fm_own_context_display_name

v2g_liberty:
  module: v2g_liberty
  class: V2Gliberty
  priority: 50
  dependencies:
    - v2g-globals
    - flexmeasures-client
    - wallbox-client

  admin_mobile_name: !secret admin_mobile_name
  admin_mobile_platform: !secret admin_mobile_platform

  car_average_wh_per_km: !secret car_average_wh_per_km

  fm_car_reservation_calendar: calendar.car_reservation
  wallbox_modbus_registers: !include /config/apps/v2g-liberty/wallbox_modbus_registers.yaml

  wallbox_host: !secret wallbox_host
  wallbox_port: !secret wallbox_port

  # The Wallbox Quasar needs processing time after a setting is done
  # This is a waiting time between the actions in milliseconds
  wait_between_charger_write_actions: 5000
  timeout_charger_write_actions: 20000

flexmeasures-client:
  module: flexmeasures_client
  class: FlexMeasuresClient
  priority: 50
  dependencies:
    - v2g-globals

  fm_user_email: !secret fm_user_email
  fm_user_password: !secret fm_user_password
  fm_schedule_duration: !secret fm_schedule_duration

  reschedule_on_soc_changes_only: false # Whether to skip requesting a new schedule when the SOC has been updated, but hasn't changed
  max_number_of_reattempts_to_retrieve_schedule: 6
  delay_for_reattempts_to_retrieve_schedule: 15
  delay_for_initial_attempt_to_retrieve_schedule: 20

  fm_car_reservation_calendar: !secret car_calendar_name
  fm_car_reservation_calendar_timezone: !secret car_calendar_timezone

wallbox-client:
  module: wallbox_client
  class: RegisterModule
  priority: 50
  # The Wallbox Quasar needs processing time after a setting is done
  # This is a waiting time between the actions in milliseconds
  wait_between_charger_write_actions: 5000
  timeout_charger_write_actions: 20000

get_fm_data:
  module: get_fm_data
  class: FlexMeasuresDataImporter
  priority: 100
  fm_data_user_email: !secret fm_user_email
  fm_data_user_password: !secret fm_user_password
  VAT: !secret VAT
  markup_per_kwh: !secret markup_per_kwh

set_fm_data:
  module: set_fm_data
  class: SetFMdata
  priority: 100
  dependencies:
    - wallbox-client
    - v2g-globals

  fm_data_user_email: !secret fm_user_email
  fm_data_user_password: !secret fm_user_password

  fm_base_entity_address_power: !secret fm_base_entity_address_power
  fm_base_entity_address_availability: !secret fm_base_entity_address_availability
  fm_base_entity_address_soc: !secret fm_base_entity_address_soc

  wallbox_host: !secret wallbox_host
  wallbox_port: !secret wallbox_port
  wallbox_modbus_registers: !include /config/apps/v2g-liberty/wallbox_modbus_registers.yaml

```

## Configure HA to use v2g-liberty

This (nearly last) step will combine the work you've done so far: it adds V2G Liberty to HA.
In your Home Assistant file editor, go to `/config/configuration.yaml` and add the following to the top of the file:

```yaml
homeassistant:
  packages:
    v2g_pack: !include packages/v2g-liberty/v2g_liberty_package.yaml

```

### Convenient HA optimisations

These are "out of the box" super handy HA features that are highly recommended.

#### Add users

This is optional but is highly recommended.
This lets all persons in the household operate the charger.

#### Install the HA app on your mobile

This is optional but highly recommended.
You can get it from the official app store of your phone platform.
If you’ve logged in, the mobile can later be used to receive notifications.

#### Make V2G Liberty your default dashboard

After the restart (next step) you'll find the V2G Liberty dashboard in the sidebar. 
Probably underneath "Overview", which then likely is the current default dashboard. To make the V2G Liberty dashboard 
your default go to `Settings > Dashboards`. Select the V2G Liberty dashboard row and click th link "SET AS DEFAULT IN THIS DEVICE".



## Start it up
Now that so many files have changed/been added a restart of both Home Assistant and AppDaemon is needed.
HA can be restarted by `Settings > System > Restart (top right)`.
AppDaemon can be (re-)started via `Settings > Add-ons > AppDaemon > (Re-)start`.

Now the system needs 5 to 10 minutes before it runs nicely. If a car is connected you should see a schedule comming in soon after.
<!-- <style 
  type="text/css">
  body {
    max-width: 50em;
    margin: 4em;
  }
</style> -->


### Car reservations

Reserving a car matters when you want to make sure your car has sufficient charge for the trip.
Depending on when you planned your trip, your car battery may be near its minimum charge (20% by default).
This is most often the case at the end of the morning and at the end of the evening, but it all depends on the day's market prices.
The 20% charge should be enough for small trips, but sometimes you'll need more.
In that case, make a calendar item in your [calendar for car reservations](#get-an-online-calendar).

By default, the car will be fully charged at the start of the calendar item.
To allow some scheduling flexibility and increase the capability of V2G Liberty to earn money and reduce emissions, simply mention a minimum charge target in the calendar description.
Such a target must mention the unit (either kWh or %). For example:

- *25 kWh* (not case-sensitive, and tolerates spaces between the numbers and unit)
- *40%*

In case you need more control, it's possible to add one of the following modifiers: >, < or =. For example:

- *=25 kWh* (indicates that the battery charge should be exactly 25 kWh at the start of the reservation)
- *<40%* (indicates that the battery charge should be less than 40% at the start of the reservation)
- *>60%* (same as *60%*, indicating a minimum charge of 60%)
