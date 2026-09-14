"""Home Assistant entity ids used by the add-on. Change here only."""

SWITCHES = {
    "filter_pump": "switch.pool_pump",
    "spa": "switch.spa_pump",
    "spa_heat": "switch.spa_heater",
    "jet_pump": "switch.jet_pump",
    "pool_heat": "switch.pool_heater",
}

LIGHTS = {
    "light_shallow": "light.pool_pool_light_shallow_end",
    "light_middle": "light.pool_pool_light_middle",
    "light_deep": "light.pool_pool_light_deep_end",
}

SENSORS = {
    "pool_temp": "sensor.pool_temp",
    "spa_temp": "sensor.spa_temp",
}

ALL_ENTITY_IDS = [*SWITCHES.values(), *LIGHTS.values(), *SENSORS.values()]
