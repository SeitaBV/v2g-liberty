# V2G Liberty: A HomeAssistant-based app to optimize the charging of your EV (using the Wallbox Quasar)

This integration lets you add smart charge control to your Wallbox Quasar. It implements a nice local app in [HomeAssistant](https://www.home-assistant.io/) and uses the smart EMS [FlexMeasures](https://flexmeasures.io) for optimized schedules.

The idea is to optimize the upcoming hours with an eye on energy prices, your solar generation or the CO₂ content of the grid
(for now: only European energy prices, the rest is to come).

![The V2G Liberty Dashboard](https://positive-design.nl/wp-content/uploads/2022/04/V2GL-1-1024x549.png)

You can read more about the project and its vision [here](https://positive-design.nl/) and [here](https://seita.nl/project/v2ghome-living-lab/).

In practice, you can do the following via V2G Liberty:

- Switch the charge mode between user and automatic
- In automatic mode, FlexMeasures is periodically asked to generate schedules, which Home Assistant translates into set points which it sends to the Wallbox Quasar via modbus.
- Set targets (e.g. be charged 100% at 7am tomorrow) which prompts FlexMeasures to update its schedules.

This integration is a Python app and uses:

- FlexMeasures for optimizing charging schedules.
- Home Assistant for automating local control over your Wallbox Quasar.
- The AppDaemon plugin for Home Assistant for running the Python app.
- A Modbus specification for communicating with the Wallbox Quasar (currently not publicly available). 

# Installation

The installation tutorial assumes you have already installed Home Assistant, including the AppDaemon 4 add-on.
It also assumes you have a FlexMeasures server set up with a sensor for the Quasar (copy its entity address into the relevant Home Assistant setting), as well as a sensor and data for day-ahead prices.

In Home Assistant, look for the AppDaemon configuration (`Supervisor -> AppDaemon 4 -> Configuration`) and add the following Python packages:

```yaml
python_packages:
  - isodate
  - pyModbusTCP
```

In your Home Assistant file editor, go to `/config/appdaemon/apps/` and add `fm_ha_integration.py`.

In the same directory, add (or extend) `apps.yaml` with (replacing secrets as required for your custom setting):

```yaml
---
flexmeasures-client:
  module: flexmeasures_client
  class: FlexMeasuresClient
  fm_api: https://flexmeasures.seita.nl/api
  fm_api_version: v2_0
  fm_user_email: !secret fm_user_email
  fm_user_password: !secret fm_user_password
  fm_schedule_duration: "PT12H"
  fm_car_max_soc_in_kwh: 62
  fm_car_reservation_calendar: calendar.car_reservation
  fm_car_reservation_calendar_timezone: Europe/Amsterdam
  fm_quasar_entity_address: !secret fm_quasar_entity_address
  fm_quasar_soc_event_resolution_in_minutes: 5

  reschedule_on_soc_changes_only: false # Whether to skip requesting a new schedule when the SOC has been updated, but hasn't changed
  max_number_of_reattempts_to_retrieve_device_message: 4
  delay_for_reattempts_to_retrieve_device_message: 30
  delay_for_initial_attempt_to_retrieve_device_message: 5

  # ToDo: dependent on temp. for now is fixed.
  wallbox_plus_car_roundtrip_efficiency: 0.85


wallbox-client:
  module: wallbox_client
  class: RegisterModule


flexmeasures-home-assistant:
  module: fm_ha_integration
  class: FlexMeasuresWallboxQuasar
  dependencies:
    - flexmeasures-client
    - wallbox-client
  fm_car_reservation_calendar: calendar.car_reservation
  fm_car_max_soc_in_kwh: 62
  fm_quasar_soc_event_resolution_in_minutes: 5
  fm_schedule_duration: "PT12H"  # ISO 8601 period
  wait_between_charger_write_actions: 5000  # allow for some processing time after changing a charger setting
  timeout_charger_write_actions: 20000
  wallbox_host: !secret wallbox_host
  wallbox_port: !secret wallbox_port
  wallbox_modbus_registers: !include /config/wallbox_modbus_registers.yaml
  wallbox_current_power_ratio: 230  # in Volt
  wallbox_max_charging_current: 25  # in Amp
  wallbox_max_discharging_current: 25  # in Amp
```

The Wallbox Modbus registers are documented in the Quasar Modbus specification, which is not part of this repository.

In `/config/configuration.yaml` add the following Modbus sensor to get a signal of your car's state of charge, and some input fields to store a clean SoC signal, a selected charge mode and charging schedules:

```yaml

modbus:
  - name: quasar
    delay: 5
    timeout: 4
    type: tcp
    host: !secret wallbox_host
    port: !secret wallbox_port
    sensors:
      - name: charger_connected_car_state_of_charge
        address: !secret wallbox_register_get_state_of_charge
        input_type: holding
        data_type: int16
        scan_interval: 120
        unit_of_measurement: "%"
        slave: 1
input_number:
  car_state_of_charge_wh:
    name: Car State of Charge
    icon: mdi:battery-medium
    min: 0
    max: 62000
    step: 1
    unit_of_measurement: Wh
input_select:
  charge_mode:
    name: Charge mode
    icon: mdi:battery-charging-medium
    options:
      - Automatic
      - Forced ON
      - Forced OFF
input_text:
  chargeschedule:
    name: ChargeSchedule
    icon: mdi:calendar-multiselect
```

In `/config/automations.yaml` add:

```yaml
- id: '1626364003549'
  alias: Clean up SoC change as reported by Quasar
  description: "Goal: make the technical state of charge (from Modbus YAML in configuration)\
    \ better readable.\nThe Quasar returns an SoC of 0% if the charger is not connected\
    \ /paused. This is not the true state of charge.\nThis is why we fill another (input)\
    \ variable with the most recent non-zero value.\nThe Modbus YAML code also frequently\
    \ returns \"unavailable\". We also ignore that here.\nTo better estimate the true state\
    \ of charge, we also store the datetime of a (correct) change, as another input number."
  trigger:
  - platform: state
    entity_id: sensor.charger_connected_car_state_of_charge
  condition:
  - condition: numeric_state
    entity_id: sensor.charger_connected_car_state_of_charge
    above: '1'
  - condition: not
    conditions:
    - condition: state
      entity_id: sensor.charger_connected_car_state_of_charge
      state: unavailable
  action:
  - service: input_number.set_value
    target:
      entity_id: input_number.car_state_of_charge_wh
    data:
      value: '{{states(''sensor.charger_connected_car_state_of_charge * 620'')}}'
  mode: single
```

In `/config/scripts.yaml` add:

```yaml
disconnectnow:
  alias: DIsconnectNow
  sequence:
  - service: input_text.set_value
    target:
      entity_id: input_text.chargeschedule
    data:
      value: reset
  - service: input_text.set_value
    target:
      entity_id: input_text.chargeschedule
    data:
      value: DisconnectNow
  mode: single
```
