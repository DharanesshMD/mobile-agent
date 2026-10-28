import unittest

from agent.apps import SETTINGS_PAGES, match_apps, parse_launcher_packages
from agent.loop import execute, oscillating

PKGS = ["com.android.camera2", "com.android.chrome", "com.android.settings", "com.android.vending", "com.google.android.apps.messaging",
        "com.google.android.contacts", "com.google.android.deskclock", "com.google.android.dialer"]

DUMP = """19 activities found:
  Activity #0:
    priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true
    com.android.camera2/com.android.camera.CameraLauncher
  Activity #1:
    priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=true
    com.android.chrome/com.google.android.apps.chrome.Main
  Activity #2:
    com.android.chrome/.Other
"""


class Phone:
    def __init__(self):
        self.swipes, self.intents = [], []

    def swipe(self, *a):
        self.swipes.append(a)

    def launcher_apps(self):
        return PKGS

    def open_intent(self, action):
        self.intents.append(action)


class Matching(unittest.TestCase):
    def test_parse_launcher_packages(self):
        self.assertEqual(parse_launcher_packages(DUMP), ["com.android.camera2", "com.android.chrome"])

    def test_alias_prefix_and_not_installed(self):
        self.assertEqual(match_apps("play store", PKGS), ["com.android.vending"])
        self.assertEqual(match_apps("Messages", PKGS), ["com.google.android.apps.messaging"])  # messages ~ messaging
        self.assertEqual(match_apps("contacts", PKGS), ["com.google.android.contacts"])
        self.assertEqual(match_apps("calculator", PKGS), [])
        self.assertEqual(match_apps("google android", PKGS), [])  # generic words never match

    def test_alias_is_ignored_when_the_app_is_not_installed(self):
        self.assertEqual(match_apps("gmail", PKGS), [])


class Actions(unittest.TestCase):
    def run_act(self, act):
        phone = Phone()
        return phone, execute(phone, act, [], 2.0, (1080, 2400))

    def test_scroll_down_moves_the_finger_up_and_swipe_is_raw(self):
        phone, _ = self.run_act({"type": "scroll", "direction": "down"})
        x1, y1, x2, y2 = phone.swipes[0]
        self.assertGreater(y1, y2)  # finger moves up to reveal content below
        phone, _ = self.run_act({"type": "swipe", "direction": "down"})
        x1, y1, x2, y2 = phone.swipes[0]
        self.assertLess(y1, y2)  # raw swipe: finger moves down

    def test_find_app_reports_installed_or_not_installed(self):
        self.assertEqual(self.run_act({"type": "find_app", "query": "chrome"})[1], "installed: com.android.chrome")
        self.assertIn("not installed", self.run_act({"type": "find_app", "query": "calculator"})[1])

    def test_open_settings_uses_the_deep_link_and_rejects_unknown_pages(self):
        phone, info = self.run_act({"type": "open_settings", "page": "Dark Theme"} if False else {"type": "open_settings", "page": "display"})
        self.assertEqual(phone.intents, ["android.settings.DISPLAY_SETTINGS"])
        self.assertEqual(info, "opened display settings")
        phone, _ = self.run_act({"type": "open_settings", "page": "Add-Account"})
        self.assertEqual(phone.intents, ["android.settings.ADD_ACCOUNT_SETTINGS"])
        with self.assertRaises(ValueError) as e:
            self.run_act({"type": "open_settings", "page": "teleport"})
        self.assertIn("wifi", str(e.exception))
        self.assertTrue(all(a.startswith("android.settings.") for a in SETTINGS_PAGES.values()))

    def test_oscillation_counts_scroll_too(self):
        acts = [{"type": "scroll", "direction": d} for d in ("up", "down", "up", "down")]
        self.assertTrue(oscillating(acts))
        self.assertFalse(oscillating(acts[:3]))


if __name__ == "__main__":
    unittest.main()
