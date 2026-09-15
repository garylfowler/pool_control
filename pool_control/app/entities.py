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

# Definity IC60 salt chlorinator via Tuya Local (custom profile definity_ic60_chlorinator)
CHLORINATOR = {
    "flow": "sensor.pool_salt_chlorinator_flow",                       # "Flow" when water is moving
    "salt": "sensor.pool_salt_chlorinator_salt_level",                 # "Normal" / "Low" / "High"
    "efficiency": "select.pool_salt_chlorinator_chlorination_efficiency",  # percent, settable
    "boost": "switch.pool_salt_chlorinator_super_chlorine_mode",
}

# the cell's firmware only accepts these output percentages (from the Smart Life app)
CHLORINATOR_OUTPUT_OPTIONS = [0, 2, 4, 6, 8, 10, 20, 40, 80, 100]

ALL_ENTITY_IDS = [*SWITCHES.values(), *LIGHTS.values(), *SENSORS.values(), *CHLORINATOR.values()]
