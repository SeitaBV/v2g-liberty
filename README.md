<style>
  body {
    max-width: 50em;
    margin: 3em;
  }
</style>

# V2G Liberty: optimised vehicle-to-grid charging of your EV

This integration lets you add full automatic and price optimized control over Vehicle to grid (V2G) charging. It has a practical local app in [HomeAssistant](https://www.home-assistant.io/) and uses the smart EMS [FlexMeasures](https://flexmeasures.io) for optimized schedules.

The schedules are optimized on EPEX day-ahead energy prices, so this works best with an electricity contract with dynamic (hourly) prices<sup>†</sup>.
We intend to add optimisation for your solar generation or the CO₂ content of the grid in the near future.

† For now: only Dutch energy prices, if you have interest in this software and live outside the Netherlands, please [contact us](https://v2g-liberty.eu/) to see what the options are.

![The V2G Liberty Dashboard](https://positive-design.nl/wp-content/uploads/2022/04/V2GL-1-1024x549.png)

You can read more about the project and its vision [here](https://v2g-liberty.eu/) and [here](https://seita.nl/project/v2ghome-living-lab/).

In practice, V2G Liberty does the following:
- In automatic mode: Now worries, just plugin when you return home and let the system automatically optimize charging. 
- Set targets (e.g. be charged 100% at 7am tomorrow) which prompts FlexMeasures to update its schedules.
- Override the system and set charging to "Max Boost Now" mode in cases where you need as much battery SoC a possible quickly.

This integration is a Python app and uses:

- FlexMeasures for optimizing charging schedules. FlexMeasures is periodically asked to generate optimized charging schedules.
- Home Assistant for automating local control over your Wallbox Quasar. V2G Liberty translates this into set points which it sends to the Wallbox Quasar via modbus.
- The AppDaemon plugin for Home Assistant for running the Python app.

### Prerequisites
 
At the time of writing, 2022-11, only the [Wallbox Quasar 1 charger](https://wallbox.com/en_uk/quasar-dc-charger) is supported.
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

## Configure HA

This is not complicated, but you'll need to work precise. The configuration of HA is stored in .yaml files, these can be edited in the HA file editor (in the left main menu).

  > If you have installed V2G Liberty before, please remove any changes made during that installation to the .yaml files.

After completion of this step you'll end up with a these files and folders (others might be there but are not shown here). Some might be already present and only need editing. Others files or folders might need to added. The files you'll have to edit are marked with *.

```
. (root = HA config folder)
├── appdaemon
│   ├── apps
│   │   ├── v2g-liberty
│   │   │   ├── app_config
│   │   │   │   ├── v2g-liberty-package.yaml
│   │   │   │   └── wallbox_modbus_registers.yaml
│   │   │   ├── flexmeasures_client.py
│   │   │   ├── get_fm_data.py
│   │   │   ├── LICENSE
│   │   │   ├── README.md
│   │   │   ├── set_fm_data.py
│   │   │   ├── util_functions.py
│   │   │   ├── v2g_liberty.py
│   │   │   └── wallbox_client.py
│   │   └ apps.yaml *
│   └ appdaemon.yaml *
├── configuration.yaml *
└── secrets.yaml *
```

### Secrets

HA stores secrets in the file `secrets.yaml` and V2G Liberty expects this file to be in the default location, the config folder.
We store both secrets and configuration values in this file as this is the most conveniant way for storing these.
Open this file in the HA file editor and add the following code. You'll need to replace secrets/values for your custom setting.
If you have installed the Studio Code Server addon (not mandatory!), you can use that.


```yaml
################################################################################
#                                                                              #
#    V2G Liberty Configuration                                                 #
#    Contains all settings that need to be set for you, usually secrets        #
#    such as passwords, usernames, ip-addresses and entity addresses.          #
#                                                                              #
#    It is also used for storing variables for use in the app configuration    #
#                                                                              #
################################################################################

#############   BASIC HA CONFIG   ##############################################
## ALWAYS CHANGE ##
# Provide the coordinates of the location.
# Typical: lat. 52.xxxxxx lon. 4.xxxxxx, elevation in meters.
ha_latitude: xx.xxxxxxx
ha_longitude: x.xxxxxxx
ha_elevation: x
ha_time_zone: Europe/Amsterdam


#############   FLEXMEASURES CONFIGURATION   ###################################

## ALWAYS CHANGE ##
fm_user_email: "your FM e-mail here (use quotes)"
fm_user_password: "your FM password here (use quotes)"

# This looks like ea1.2022-03.nl.seita.flexmeasures:fmX.X 
fm_quasar_entity_address: "your FM entity adres here"

# This is an integer number
fm_quasar_sensor_id: X

# This looks like dev/sensor/XX/chart_data/
fm_data_api_epex: "your FM api epex here"

# These looks like ea1.2022-03.nl.seita.flexmeasures:fmX.X 
fm_availability_entity_address: "your FM availability entity adres here"
fm_soc_entity_address: "your FM soc entity adres here"

## VERY RARELY CHANGE ##
fm_api: https://flexmeasures.seita.nl/api
fm_data_api: https://flexmeasures.seita.nl/api/
fm_api_version: v3_0
fm_data_api_post_meter_data: v2_0/postMeterData
fm_data_api_post_sensor_data: v3_0/sensors/data

# This represents how far ahead the schedule should look. Keep at this setting.
fm_schedule_duration: "PT27H"
# This represents how often schedules should refresh. Keep at this setting.
fm_quasar_event_resolution_in_minutes: 5

#############   CHARGER CONFIGURATION   ########################################

## ALWAYS CHANGE ##
# This usually is an IP address but can be a named URL as well.
wallbox_host: "your charger host here"
wallbox_port: XXX

#############   CAR & POWER-CONNECTION CONFIGURATION   #########################
## ALWAYS CHECK/CHANGE ##
# The maximum capacity of the battery of the car, as an integer.
# For the Nissan Leaf this is usually 24, 40 or 62
car_max_capacity_in_kwh: 62

# Max (dis-)charge rate in Watt.
# If a load-balancer (Power Boost for WB) is installed it is safe to use maximum
# amperage of the phase * 233 (Volt). So for 25 A, this is 5825 W.
# If there is no load-balancer, please ask your installer what max power can be
# used by the wallbox. It's very rare that discharge- differs from charge power.
wallbox_max_charging_power: XXXX
wallbox_max_discharging_power: XXXX

# For transforming the raw EPEX (from FM) to net price to be shown in UI.
# For NL: Temporary VAT reduction per 2022-07-01 to 9%, normally 21%
VAT: 1.09
# FOR NL: 2022 ODE € 0,0305 and Energiebelasting € 0,036790 combined
markup_per_kWh: 0.067290

#############   CALENDAR CONFIGURATION   #######################################

# Configuration for the calendar for making reservations for the car #
# This is mandatory!
# It can be a Google calendar (please use the Google calendar integration for HomeAssistant)

car_calendar_name: calendar.car_reservation
# This normally matches the ha_time_zone setting.
car_calendar_timezone: Europe/Amsterdam

## Remove these if another calendar is used.
## Please also remove the calendar related entities in v2g_liberty_package.yaml
caldavUN: "your caldav username here (use quotes)"
caldavPWD: "your caldav password here (use quotes)"
caldavURL: "your caldav URL here (use quotes)"

git_ard_UN: "your github username here (use quotes)"
git_ard_PWD: "your github password here (use quotes)"

```

### Copy & edit files

In your Home Assistant file editor, go to `/config/appdaemon/apps/` and create a new folder `v2g-liberty`.
Within the v2g-liberty folder create a new folder `app-config`.
From this GitHub project copy all files to the respective folders.

## Install the AppDaemon 4 add-on

AppDaemon is an official add-on for HA and thus can be installed from within HA.
Please go to Settings -> Add-ons -> Add-on store and find the AppDaemon add-on.
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
secrets: /config/secrets.yaml

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

# Setting logging is optional but usefull. The software is in use for quite some
# time but not bullit-proof yet. So every now and then you'll need to see what
# happend.
log_thread_actions: 1
logs:
  main_log:
    filename: /config/appdaemon/logs/appdaemon_main.log
  error_log:
    filename: /config/appdaemon/logs/appdaemon_error.log
```

### Apps.yaml

In the same directory, add (or extend) `apps.yaml` with the following.
Usually there is no need to change any of the values as all personal settings are referenced from the secrets file.

```yaml
---
flexmeasures-client:
  module: flexmeasures_client
  class: FlexMeasuresClient
  dependencies:
    - util_functions
  fm_api: !secret fm_api
  fm_api_version: !secret fm_api_version
  fm_user_email: !secret fm_user_email
  fm_user_password: !secret fm_user_password
  fm_schedule_duration: !secret fm_schedule_duration
  fm_quasar_entity_address: !secret fm_quasar_entity_address
  fm_quasar_sensor_id: !secret fm_quasar_sensor_id

  reschedule_on_soc_changes_only: false # Whether to skip requesting a new schedule when the SOC has been updated, but hasn't changed
  fm_quasar_soc_event_resolution_in_minutes: !secret fm_quasar_event_resolution_in_minutes
  max_number_of_reattempts_to_retrieve_schedule: 4
  delay_for_reattempts_to_retrieve_schedule: 30
  delay_for_initial_attempt_to_retrieve_schedule: 10

  fm_car_max_soc_in_kwh: !secret car_max_capacity_in_kwh
  fm_car_reservation_calendar: !secret car_calendar_name
  fm_car_reservation_calendar_timezone: !secret car_calendar_timezone
  wallbox_plus_car_roundtrip_efficiency: 0.85

wallbox-client:
  module: wallbox_client
  class: RegisterModule
  # The Wallbox Quasar needs processing time after a setting is done
  # This is a waiting time between the actions in milliseconds
  wait_between_charger_write_actions: 5000
  timeout_charger_write_actions: 20000

util_functions:
  module: util_functions
  class: RegisterUtilModule

v2g_liberty:
  module: v2g_liberty
  class: V2Gliberty
  dependencies:
    - flexmeasures-client
    - wallbox-client

  fm_car_reservation_calendar: calendar.car_reservation
  fm_quasar_soc_event_resolution_in_minutes: !secret fm_quasar_event_resolution_in_minutes
  wallbox_modbus_registers: !include /config/appdaemon/apps/v2g-liberty/app_config/wallbox_modbus_registers.yaml
  fm_car_max_soc_in_kwh: !secret car_max_capacity_in_kwh
  wallbox_host: !secret wallbox_host
  wallbox_port: !secret wallbox_port

  # The Wallbox Quasar needs processing time after a setting is done
  # This is a waiting time between the actions in milliseconds
  wait_between_charger_write_actions: 5000
  timeout_charger_write_actions: 20000

  wallbox_max_charging_power: !secret wallbox_max_charging_power
  wallbox_max_discharging_power: !secret wallbox_max_discharging_power

get_fm_data:
  module: get_fm_data
  class: FlexMeasuresDataImporter
  fm_api: !secret fm_api
  fm_data_api: !secret fm_data_api
  fm_data_api_epex: !secret fm_data_api_epex
  fm_data_user_email: !secret fm_user_email
  fm_data_user_password: !secret fm_user_password
  fm_data_entity_address: !secret fm_quasar_entity_address
  VAT: !secret VAT
  markup_per_kWh: !secret markup_per_kWh

set_fm_data:
  module: set_fm_data
  class: SetFMdata
  dependencies:
    - wallbox-client
    - util_functions

  fm_api: !secret fm_api
  fm_data_api: !secret fm_data_api
  fm_data_api_post_meter_data: !secret fm_data_api_post_meter_data
  fm_data_api_post_sensor_data: !secret fm_data_api_post_sensor_data
  fm_data_user_email: !secret fm_user_email
  fm_data_user_password: !secret fm_user_password
  fm_power_entity_address: !secret fm_quasar_entity_address
  fm_availability_entity_address: !secret fm_availability_entity_address
  fm_soc_entity_address: !secret fm_soc_entity_address

  fm_chargepower_resolution_in_minutes: !secret fm_quasar_event_resolution_in_minutes

  wallbox_host: !secret wallbox_host
  wallbox_port: !secret wallbox_port
  wallbox_modbus_registers: !include /config/appdaemon/apps/v2g-liberty/app_config/wallbox_modbus_registers.yaml

```

## Configure HA to use v2g-liberty

This (nearly last) step will combine the work you've done so far: it adds V2G Liberty to HA.
In your Home Assistant file editor, go to `/config/configuration.yaml` and add the following to the top of the file:

```yaml
homeassistant:
  packages:
    v2g_pack: !include appdaemon/apps/v2g-liberty/app_config/v2g_liberty_package.yaml
```

<<TODO: Add the dashboard yaml>>

### Conveniant HA optimisations

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