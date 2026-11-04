"""The names of the phones, in one place.

The owner sees phones only as "Phone", "Phone 2", "Phone 3" ... : what each is made of is never part of a name or of anything the agent says. The code still
tells them apart (internal keys "emulator", "games", "phone:<id>", lanes and serials do not change) because the safety rules depend on it; only the words do.
Older spellings ("cloud phone", "cloud phone 2", "emulator", "games") are still understood wherever a phone is named, so stored prompts, routines and tests keep working.
"""
import re

SHARED = "Phone"  # the shared Android (internal key "emulator")
SECOND = "Phone 2"  # the second Android (internal key "games")
KEY_SHARED, KEY_SECOND = "emulator", "games"
FIRST_PAIRED = 3  # phones the owner connects through the app are Phone 3, Phone 4 ... (1 and 2 are always reserved)

_SHARED_NAMES = {"emulator", "shared", "shared emulator", "main", "default", "cloud phone", "phone", "phone 1", "phone1", "first phone"}
_SECOND_NAMES = {"games", "game", "cloud phone 2", "second cloud phone", "virtual", "phone 2", "phone2", "second phone"}
_NUMBERED = re.compile(r"^phone (\d+)$", re.I)


def alias_key(name: str) -> str | None:
    """The internal key a spoken name stands for ("emulator" or "games"), or None when it is neither (a paired phone is found by its bot name)."""
    n = " ".join((name or "").strip().lower().split())
    if n in _SHARED_NAMES:
        return KEY_SHARED
    if n in _SECOND_NAMES:
        return KEY_SECOND
    return None


def label_for_key(key: str) -> str | None:
    return {KEY_SHARED: SHARED, KEY_SECOND: SECOND}.get(key)


def next_paired_name(existing: list[str]) -> str:
    """"Phone N" with the smallest N >= 3 that no bot already uses."""
    used = {int(m.group(1)) for n in existing if (m := _NUMBERED.match((n or "").strip()))}
    n = FIRST_PAIRED
    while n in used:
        n += 1
    return f"Phone {n}"


def looks_like_model_name(name: str, model: str = "", info_name: str = "") -> bool:
    """True when a paired phone's bot name is only the device's own model string (what the app reported), i.e. the owner never chose it."""
    n = (name or "").strip().lower()
    return bool(n) and n in {(model or "").strip().lower(), (info_name or "").strip().lower()} - {""}


# A phone reports its model code ("2107113SI"), which nobody recognises. The names people know for the codes we are sure about; any other phone keeps what it reports.
MARKET_NAMES = {
    "2107113si": "Xiaomi 11T Pro", "2107113sg": "Xiaomi 11T Pro", "21081111rg": "Xiaomi 11T",
    "sm-s918b": "Galaxy S23 Ultra", "sm-s916b": "Galaxy S23+", "sm-s911b": "Galaxy S23", "sm-s908b": "Galaxy S22 Ultra", "sm-s901b": "Galaxy S22",
    "sm-g991b": "Galaxy S21", "sm-g998b": "Galaxy S21 Ultra", "sm-a546b": "Galaxy A54", "sm-a536b": "Galaxy A53",
}


def market_name(model: str) -> str:
    """The name people know a phone by, or the model string itself when it is not one of the codes above."""
    m = (model or "").strip()
    return MARKET_NAMES.get(m.lower(), m)
