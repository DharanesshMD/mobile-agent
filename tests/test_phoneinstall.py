import logging
import os
import tempfile
import time
import unittest
from pathlib import Path

from agent import logs
from agent.phoneinstall import ADVICE, InstallError, PACKAGE, PhoneInstaller, check_code, check_host, check_port
from test_chat import Spark, setup

DEVICES = """List of devices attached
emulator-5554          device product:sdk_gphone64_arm64 model:sdk_gphone64_arm64 device:emu64a transport_id:1
localhost:5565         device product:redroid model:redroid13 device:redroid transport_id:2
R5CN1234ABC            device usb:1-1 product:renoir model:21081111RG device:renoir transport_id:3
192.168.0.23:41957     device product:renoir model:Mi_11T_Pro device:renoir transport_id:4
adb-9999-abc._adb-tls-connect._tcp device product:x model:Pixel_8 device:husky transport_id:5
ZX1UNAUTH              unauthorized usb:1-2 transport_id:6
ZX1OFFLINE             offline transport_id:7
10.1.1.9:5555          device product:extra model:Extra_Phone device:x transport_id:8
127.0.0.1:6000         device product:tunnel model:Tunnel device:x transport_id:9
"""
LINK = "mobileagent://pair?server=http://192.168.0.5:8765&code=ABCD-EFGH"


class FakeAdb:
    """A scripted adb: `replies` maps a key (the arguments after the adb path, joined) to (exit code, output); it records every call."""
    def __init__(self, **replies):
        self.calls, self.replies = [], {"devices -l": (0, DEVICES), **replies}

    def __call__(self, argv, timeout=60, input=None):
        key = " ".join(argv[1:])
        self.calls.append((argv, input))
        for k, v in self.replies.items():
            if key.startswith(k):
                return v
        return 0, ""

    def sent(self, word):
        return [a for a, _ in self.calls if word in " ".join(a)]


def installer(adb, apk=True, link=lambda: LINK, managed=lambda: set()):
    d = Path(tempfile.mkdtemp())
    apk_path = d / "mobile-agent.apk"
    if apk:
        apk_path.write_bytes(b"PK\x03\x04fake apk")
    return PhoneInstaller(run=adb, apk_path=apk_path, link=link, managed=managed, sleep=lambda s: None)


def finish(inst, job, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        v = inst.job(job["id"])
        if v["state"] != "running":
            return v
        time.sleep(0.01)
    raise AssertionError("install did not finish")


class Devices(unittest.TestCase):
    def test_lists_only_the_owners_own_phones(self):
        got = installer(FakeAdb()).devices()
        self.assertEqual([d["serial"] for d in got], ["R5CN1234ABC", "192.168.0.23:41957", "adb-9999-abc._adb-tls-connect._tcp", "ZX1UNAUTH", "ZX1OFFLINE", "10.1.1.9:5555"])
        by = {d["serial"]: d for d in got}
        self.assertEqual((by["R5CN1234ABC"]["wireless"], by["R5CN1234ABC"]["model"]), (False, "21081111RG"))
        self.assertEqual((by["192.168.0.23:41957"]["wireless"], by["192.168.0.23:41957"]["model"]), (True, "Mi 11T Pro"))
        self.assertTrue(by["adb-9999-abc._adb-tls-connect._tcp"]["wireless"])
        self.assertEqual((by["ZX1UNAUTH"]["state"], by["ZX1OFFLINE"]["state"]), ("unauthorized", "offline"))

    def test_a_phone_the_app_already_manages_is_not_offered(self):
        got = installer(FakeAdb(), managed=lambda: {"10.1.1.9:5555"}).devices()
        self.assertNotIn("10.1.1.9:5555", [d["serial"] for d in got])

    def test_no_adb_means_an_empty_list(self):
        self.assertEqual(installer(FakeAdb(**{"devices -l": (127, "adb was not found")})).devices(), [])


class Validation(unittest.TestCase):
    def test_host_port_code(self):
        for ok in ("192.168.0.23", "fe80::1", "[fe80::1]", "phone.local", "my-phone"):
            check_host(ok)
        for bad in ("", "a b", "a;b", "$(reboot)", "a`b`", "-x", "a..b", "a\nb", "a|b", "a&b", "http://x", "a/b", "x" * 300):
            with self.assertRaises(InstallError, msg=bad):
                check_host(bad)
        self.assertEqual(check_port("37045"), 37045)
        for bad in (0, 65536, -1, "abc", None, "1e3", ""):
            with self.assertRaises(InstallError, msg=str(bad)):
                check_port(bad)
        self.assertEqual(check_code(" 123456 "), "123456")
        for bad in ("12345", "1234567", "12345a", "", "123 456", "123456;ls", None):
            with self.assertRaises(InstallError, msg=str(bad)):
                check_code(bad)


class Wifi(unittest.TestCase):
    def test_pair_sends_the_code_on_stdin_and_connects_by_discovery(self):
        adb = FakeAdb(**{"pair": (0, "Successfully paired to 192.168.0.23:37045 [guid=adb-x]"),
                         "mdns services": (0, "List of discovered mdns services\nadb-abc\t_adb-tls-connect._tcp.\t192.168.0.23:41957\nother\t_adb-tls-connect._tcp.\t192.168.0.99:4000\n"),
                         "connect": (0, "connected to 192.168.0.23:41957")})
        r = installer(adb).pair("192.168.0.23", 37045, "123456")
        self.assertEqual((r["ok"], r["serial"]), (True, "192.168.0.23:41957"))
        pair_call = [(a, i) for a, i in adb.calls if a[1] == "pair"][0]
        self.assertNotIn("123456", " ".join(pair_call[0]))  # never on the command line
        self.assertEqual(pair_call[1], "123456\n")

    def test_pair_without_discovery_asks_for_the_connect_port(self):
        adb = FakeAdb(**{"pair": (0, "Successfully paired to x"), "mdns services": (0, "List of discovered mdns services\n")})
        self.assertEqual(installer(adb).pair("192.168.0.23", 37045, "123456")["needs_port"], True)

    def test_pair_failures_are_plain_and_never_echo_the_code(self):
        for out, word in (("Failed: Wrong password or invalid code 123456", "wrong or has expired"), ("error: unable to connect to 192.168.0.23:37045", "could not reach")):
            r = installer(FakeAdb(**{"pair": (1, out)})).pair("192.168.0.23", 37045, "123456")
            self.assertFalse(r["ok"])
            self.assertIn(word, r["message"])
            self.assertNotIn("123456", r["message"])

    def test_connect(self):
        self.assertTrue(installer(FakeAdb(**{"connect": (0, "already connected to 1.2.3.4:5555")})).connect("1.2.3.4", 5555)["ok"])
        self.assertFalse(installer(FakeAdb(**{"connect": (0, "failed to connect to '1.2.3.4:5555': Connection refused")})).connect("1.2.3.4", 5555)["ok"])
        with self.assertRaises(InstallError):
            installer(FakeAdb()).connect("1.2.3.4; reboot", 5555)


class Install(unittest.TestCase):
    OK = {"-s R5CN1234ABC install": (0, "Performing Streamed Install\nSuccess"), "-s R5CN1234ABC shell appops": (0, ""), "-s R5CN1234ABC shell am start": (0, "Starting: Intent")}

    def test_the_whole_sequence(self):
        adb = FakeAdb(**self.OK)
        inst = installer(adb)
        job = finish(inst, inst.start("R5CN1234ABC"))
        self.assertEqual(job["state"], "done")
        self.assertEqual([s["state"] for s in job["steps"]], ["done"] * 5)
        self.assertIn("turn on Mobile Agent under Accessibility", job["final"])
        cmds = [" ".join(a[1:]) for a, _ in adb.calls if a[1] == "-s"]
        self.assertTrue(cmds[0].startswith("-s R5CN1234ABC install -r -d ") and cmds[0].endswith("mobile-agent.apk"))
        self.assertIn(f"appops set {PACKAGE} ACCESS_RESTRICTED_SETTINGS allow", cmds[1])
        self.assertIn(f"-n {PACKAGE}/.MainActivity -d '{LINK}'", cmds[2])  # the & stays inside quotes for the phone's shell
        self.assertIn("android.settings.ACCESSIBILITY_SETTINGS", cmds[3])
        self.assertFalse(adb.sent("enabled_accessibility_services") or adb.sent("settings put"))  # the owner turns it on, never the computer

    def test_no_pairing_link_while_the_door_is_closed(self):
        inst = installer(FakeAdb(**self.OK), link=lambda: None)
        job = finish(inst, inst.start("R5CN1234ABC"))
        steps = {s["key"]: s for s in job["steps"]}
        self.assertEqual((job["state"], steps["link"]["state"]), ("done", "skipped"))
        self.assertIn("Allow phones on this Wi-Fi", steps["link"]["detail"])

    def test_a_link_that_is_not_ours_is_never_sent(self):
        adb = FakeAdb(**self.OK)
        inst = installer(adb, link=lambda: "mobileagent://pair?server=http://x';reboot;'&code=A")
        finish(inst, inst.start("R5CN1234ABC"))
        self.assertFalse(adb.sent("reboot"))

    def test_each_failure_maps_to_advice(self):
        for out, key in (("Failure [INSTALL_FAILED_USER_RESTRICTED: Install canceled by user]", "INSTALL_FAILED_USER_RESTRICTED"),
                         ("Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: Package signatures do not match]", "INSTALL_FAILED_UPDATE_INCOMPATIBLE"),
                         ("Failure [INSTALL_FAILED_VERSION_DOWNGRADE]", "INSTALL_FAILED_VERSION_DOWNGRADE"),
                         ("Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]", "INSTALL_FAILED_INSUFFICIENT_STORAGE"),
                         ("adb: device unauthorized.", "unauthorized"), ("error: device offline", "offline"), ("error: no devices/emulators found", "no devices")):
            inst = installer(FakeAdb(**{"-s R5CN1234ABC install": (1, out)}))
            job = finish(inst, inst.start("R5CN1234ABC"))
            self.assertEqual(job["state"], "failed", out)
            self.assertEqual(job["advice"], ADVICE[key], out)
            steps = {s["key"]: s["state"] for s in job["steps"]}
            self.assertEqual((steps["install"], steps["restricted"], steps["settings"]), ("failed", "skipped", "skipped"))
        miui = ADVICE["INSTALL_FAILED_USER_RESTRICTED"]
        self.assertIn("Install via USB", miui)
        self.assertIn("MIUI optimization", miui)

    def test_an_unknown_failure_still_says_something_useful(self):
        inst = installer(FakeAdb(**{"-s R5CN1234ABC install": (1, "Failure [INSTALL_FAILED_WEIRD]")}))
        job = finish(inst, inst.start("R5CN1234ABC"))
        self.assertIn("INSTALL_FAILED_WEIRD", job["steps"][1]["detail"])
        self.assertIn("Play Protect", job["advice"])

    def test_missing_apk(self):
        inst = installer(FakeAdb(), apk=False)
        job = finish(inst, inst.start("R5CN1234ABC"))
        self.assertEqual(job["state"], "failed")
        self.assertIn("scripts/build_apk.sh", job["advice"])

    def test_restricted_settings_failure_is_not_fatal(self):
        inst = installer(FakeAdb(**{**self.OK, "-s R5CN1234ABC shell appops": (255, "Error: unknown op")}))
        job = finish(inst, inst.start("R5CN1234ABC"))
        self.assertEqual(job["state"], "done")
        self.assertEqual({s["key"]: s["state"] for s in job["steps"]}["restricted"], "skipped")

    def test_only_listed_phones_and_one_install_at_a_time(self):
        adb = FakeAdb(**self.OK)
        inst = installer(adb)
        for bad in ("emulator-5554", "localhost:5565", "not-a-phone", "R5CN1234ABC; reboot", "/etc/passwd"):
            with self.assertRaises(InstallError, msg=bad):
                inst.start(bad)
        with self.assertRaisesRegex(InstallError, "USB debugging"):
            inst.start("ZX1UNAUTH")
        self.assertFalse(adb.sent("install"))
        slow = FakeAdb(**self.OK)
        gate = {"go": False}
        orig = slow.__call__

        def blocking(argv, timeout=60, input=None):
            if "install" in argv:
                end = time.time() + 5
                while not gate["go"] and time.time() < end:
                    time.sleep(0.01)
            return orig(argv, timeout, input)
        inst2 = installer(blocking)
        first = inst2.start("R5CN1234ABC")
        with self.assertRaisesRegex(InstallError, "already running"):
            inst2.start("192.168.0.23:41957")
        gate["go"] = True
        self.assertEqual(finish(inst2, first)["state"], "done")

    def test_there_is_no_way_to_install_another_file(self):
        import inspect
        self.assertEqual(list(inspect.signature(PhoneInstaller.start).parameters), ["self", "serial"])
        adb = FakeAdb(**self.OK)
        inst = installer(adb)
        finish(inst, inst.start("R5CN1234ABC"))
        installs = adb.sent(" install ")
        self.assertEqual(len(installs), 1)
        self.assertTrue(installs[0][-1].endswith("mobile-agent.apk"))


class Logging(unittest.TestCase):
    def test_no_pairing_code_in_any_log_or_result(self):
        d = Path(tempfile.mkdtemp()) / "logs"
        logs.setup_logging(d, console=False)
        self.addCleanup(logs.teardown_logging)
        self.addCleanup(logging.getLogger("agent").setLevel, logging.NOTSET)
        adb = FakeAdb(**{"pair": (1, "Failed: wrong code 654321")})
        r = installer(adb).pair("192.168.0.23", 37045, "654321")
        inst = installer(FakeAdb(**Install.OK))
        job = finish(inst, inst.start("R5CN1234ABC"))
        everything = "".join((d / f"{n}.log").read_text() for n in logs.FILES) + str(r) + str(job)
        self.assertNotIn("654321", everything)
        self.assertNotIn("ABCD-EFGH", everything)  # the app's pairing code is never logged either
        self.assertIn("phone install started", (d / "audit.log").read_text())


class Routes(unittest.TestCase):
    def app(self):
        c, _, _, app = setup(Spark())
        fake = FakeAdb(**{**Install.OK, "pair": (0, "Successfully paired"), "mdns services": (0, "")})
        app.state.installer.run = fake
        app.state.installer.sleep = lambda s: None
        apk = Path(tempfile.mkdtemp()) / "a.apk"
        apk.write_bytes(b"PK")
        app.state.installer.apk = apk
        return c, app, fake

    def test_routes(self):
        c, app, fake = self.app()
        d = c.get("/api/install/devices").json()
        self.assertTrue(d["apk"])
        self.assertIn("R5CN1234ABC", [x["serial"] for x in d["devices"]])
        self.assertNotIn("emulator-5554", [x["serial"] for x in d["devices"]])
        self.assertEqual(c.post("/api/install/run", json={"serial": "emulator-5554"}).status_code, 422)
        self.assertEqual(c.post("/api/install/run", json={}).status_code, 422)
        self.assertEqual(c.post("/api/install/pair", json={"host": "1.2.3.4", "port": 5, "code": "12"}).status_code, 422)
        self.assertEqual(c.post("/api/install/pair", json={"host": "a;b", "port": 5, "code": "123456"}).status_code, 422)
        self.assertEqual(c.post("/api/install/connect", json={"host": "1.2.3.4", "port": 99999}).status_code, 422)
        self.assertEqual(c.get("/api/install/jobs/nope").status_code, 404)
        job = c.post("/api/install/run", json={"serial": "R5CN1234ABC"}).json()["job"]
        end = time.time() + 10
        while time.time() < end and c.get(f"/api/install/jobs/{job['id']}").json()["state"] == "running":
            time.sleep(0.02)
        self.assertEqual(c.get(f"/api/install/jobs/{job['id']}").json()["state"], "done")

    def test_the_pairing_link_comes_from_the_open_phone_door(self):
        c, app, fake = self.app()
        gw = app.state.gateway
        self.assertIsNone(app.state.installer.link())  # the door is closed
        gw.listener_factory = lambda *a, **k: type("L", (), {"start": lambda s: None, "stop": lambda s: None})()
        gw.lan = lambda: "192.168.0.5"
        gw.address = lambda: "192.168.0.5:8765"
        gw.open()
        link = app.state.installer.link()
        self.assertRegex(link, r"^mobileagent://pair\?server=http://192\.168\.0\.5:8765&code=[A-Z0-9-]+$")

    def test_auth_is_required_when_a_token_is_set(self):
        from fastapi.testclient import TestClient
        from agent.service import create_app
        from test_service import Phone
        app = create_app(Phone(), lambda n: Spark(), settle=0, token="owner-token", data_dir=tempfile.mkdtemp())
        c = TestClient(app)
        for method, path in (("get", "/api/install/devices"), ("post", "/api/install/run"), ("post", "/api/install/pair"), ("get", "/api/install/jobs/x")):
            self.assertEqual(getattr(c, method)(path).status_code, 401, path)


if __name__ == "__main__":
    unittest.main()
