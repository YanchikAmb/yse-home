DOMAIN = "yse_station"
DEFAULT_PORT = 18080
CONF_TOKEN = "token"
CONF_SERVER = "server"
CONF_AREA = "area"
DEFAULT_SERVER = ""
DISCOVER_PORT = 18760
PLATFORMS = ["media_player", "light", "select", "switch", "text", "sensor", "button", "number"]
EQ_BANDS = ["eq_60", "eq_230", "eq_910", "eq_3600", "eq_14000"]
EQ_PRESETS = {
    "flat": "0 0 0 0 0",
    "bass": "6 3 0 0 0",
    "voice": "-2 0 3 3 0",
    "treble": "0 0 0 3 6",
    "loud": "5 2 -1 2 5",
    "soft": "2 1 0 -2 -4",
    "less_bass": "-6 -3 0 0 0",
}
EFFECTS = ["wave", "breathe", "spin", "fill", "spark", "shift", "comet", "aurora", "twinkle"]
VOICES = ["alisa", "dmitry", "svetlana", "own_jarvis", "own_jarvis_new", "own_vihrov", "own_baya", "own_eugene"]
BRAINS = ["none", "yandex", "ha", "assist", "openai", "google"]
NEWS = ["yandex", "google", "openai"]
WAKE_WORDS = ["джарвис-локально", "джарвис", "алиса", "яндекс"]
STT_ENGINES = ["vosk-small", "vosk", "whisper", "gigaam", "yandex", "ha"]
WEATHER_ENTITY = "weather.yandex_pogoda"
