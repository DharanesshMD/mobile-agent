"""Checking an APK: the file checks, the static read, the verdict rules, the throwaway-phone run, the upload endpoint and the chat action."""
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from agent import apkcheck
from agent.artifacts import Artifacts
from test_chat import Spark, Worker, act, setup, until
from test_cloudtools import CloudPhone

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android" android:versionCode="3" android:versionName="1.2" package="com.example.tool">
  <uses-sdk android:minSdkVersion="24" android:targetSdkVersion="{target}" />
  {perms}
  <application android:debuggable="{debuggable}"><activity android:name=".Main"/><service android:name=".S"/><receiver android:name=".R"/></application>
</manifest>"""


def manifest(perms=(), target="34", debuggable="false"):
    return MANIFEST.format(perms="\n".join(f'<uses-permission android:name="android.permission.{p}"/>' for p in perms), target=target, debuggable=debuggable)


def fake_apk(path, manifest_present=True):
    with zipfile.ZipFile(path, "w") as z:
        if manifest_present:
            z.writestr("AndroidManifest.xml", b"\x03\x00binary")
        z.writestr("classes.dex", b"dex\n035\x00")
    return path


class ApkPhone(CloudPhone):
    """A cloud phone that can install, launch, show crashes and uninstall."""
    def __init__(self):
        super().__init__()
        self.installed, self.crash_log, self.front, self.log = [], [], "", []

    def packages(self): return list(self.installed)
    def install_apks(self, paths): self.log.append(("install", paths)); self.installed.append("com.example.tool")
    def open_app(self, package): self.log.append(("open", package)); self.front = package
    def foreground(self): return self.front
    def crashes(self): return list(self.crash_log)
    def uninstall(self, package): self.log.append(("uninstall", package)); self.installed.remove(package)


class FileChecks(unittest.TestCase):
    def test_only_a_zip_with_a_manifest_passes(self):
        with tempfile.TemporaryDirectory() as d:
            text = Path(d, "a.apk"); text.write_text("hello")
            with self.assertRaises(apkcheck.ApkError) as cm:
                apkcheck.looks_like_apk(text)
            self.assertIn("not a ZIP", str(cm.exception))
            with self.assertRaises(apkcheck.ApkError) as cm:
                apkcheck.looks_like_apk(fake_apk(Path(d, "b.apk"), manifest_present=False))
            self.assertIn("no AndroidManifest.xml", str(cm.exception))
            apkcheck.looks_like_apk(fake_apk(Path(d, "c.apk")))  # no error

    def test_the_manifest_is_parsed(self):
        info = apkcheck.parse_manifest(manifest(["INTERNET", "READ_SMS"], target="26", debuggable="true"))
        self.assertEqual((info["package"], info["version"], info["target_sdk"], info["components"], info["debuggable"]), ("com.example.tool", "1.2", "26", 3, True))
        self.assertEqual(info["permissions"], ["android.permission.INTERNET", "android.permission.READ_SMS"])
        with self.assertRaises(apkcheck.ApkError):
            apkcheck.parse_manifest("not xml")

    def test_certificates_are_read_from_apksigner_output(self):
        out = "Signer #1 certificate DN: CN=x\nSigner #1 certificate SHA-256 digest: " + "ab" * 32 + "\nSigner #1 certificate SHA-1 digest: cd"
        self.assertEqual(apkcheck.parse_certs(out), ["ab" * 32])


class Verdict(unittest.TestCase):
    def info(self, perms=(), **kw):
        return {**apkcheck.parse_manifest(manifest(perms)), "certs": ["ab" * 32], **kw}

    def test_a_plain_app_has_nothing_found(self):
        self.assertEqual(apkcheck.judge(self.info(["INTERNET", "CAMERA"])), ("nothing_found", []))

    def test_several_powerful_permissions_make_it_suspicious_and_say_why(self):
        level, reasons = apkcheck.judge(self.info(["READ_SMS", "SEND_SMS", "BIND_ACCESSIBILITY_SERVICE"]))
        self.assertEqual(level, "suspicious")
        self.assertIn("read sms", reasons[0])
        self.assertIn("text messages", reasons[0])

    def test_a_missing_signature_or_a_debug_build_is_a_reason(self):
        self.assertEqual(apkcheck.judge(self.info(certs=[]))[0], "suspicious")
        self.assertEqual(apkcheck.judge({**self.info(), "debuggable": True})[0], "suspicious")

    def test_a_static_read_never_says_harmful(self):
        self.assertNotEqual(apkcheck.judge(self.info(["READ_SMS", "SEND_SMS", "RECEIVE_SMS", "BIND_DEVICE_ADMIN"]))[0], "harmful")


@unittest.skipUnless(Path("android/app/build/outputs/apk/debug/app-debug.apk").exists() and Path(apkcheck.sdk_tool.__globals__["os"].environ.get("ANDROID_HOME", "/opt/homebrew/share/android-commandlinetools"), "cmdline-tools/latest/bin/apkanalyzer").exists(),
                     "needs the Android SDK tools and a built APK")
class RealTools(unittest.TestCase):
    def test_our_own_app_is_read_without_being_run(self):
        info = apkcheck.analyze("android/app/build/outputs/apk/debug/app-debug.apk")
        self.assertEqual(info["package"], "ai.mobileagent.client")
        self.assertTrue(info["certs"])
        self.assertEqual(apkcheck.judge(info)[0], "suspicious")  # it draws over other apps, sees every installed app, and is a debug build


class SandboxRun(unittest.TestCase):
    INFO = {"package": "com.example.tool"}

    def run_it(self, phone, art=None, watch=0.2):
        return apkcheck.sandbox_run(phone, art or Artifacts(), "/x/app.apk", self.INFO, watch_s=watch, sleep=lambda s: None)

    def test_a_healthy_app_is_installed_launched_watched_and_removed(self):
        phone, art = ApkPhone(), Artifacts()
        notes = self.run_it(phone, art)
        self.assertEqual([k for k, *_ in phone.log], ["install", "open", "uninstall"])
        self.assertIn("no crash", notes[0])
        self.assertEqual(phone.installed, [])
        self.assertGreaterEqual(len(art.captures), 2)

    def test_a_crash_while_launching_is_reported_and_the_app_is_still_removed(self):
        phone = ApkPhone()
        phone.open_app = lambda p: (phone.log.append(("open", p)), phone.crash_log.append((p, "99")))[0]
        notes = self.run_it(phone)
        self.assertIn("crashed", notes[0])
        self.assertEqual(phone.installed, [])

    def test_an_app_that_was_there_before_is_never_touched(self):
        phone = ApkPhone()
        phone.installed.append("com.example.tool")
        with self.assertRaises(apkcheck.ApkError):
            self.run_it(phone)
        self.assertEqual(phone.installed, ["com.example.tool"])
        self.assertFalse(any(k == "uninstall" for k, *_ in phone.log))

    def test_an_install_failure_leaves_nothing_to_remove(self):
        phone = ApkPhone()
        phone.install_apks = lambda paths: (_ for _ in ()).throw(ValueError("install failed: INSTALL_FAILED_OLDER_SDK"))
        with self.assertRaises(ValueError):
            self.run_it(phone)
        self.assertFalse(any(k == "uninstall" for k, *_ in phone.log))


class InChat(unittest.TestCase):
    def upload(self, c, bot, path, name="tool.apk"):
        return c.post(f"/api/bots/{bot}/apk?name={name}", content=Path(path).read_bytes())

    def test_an_upload_must_be_an_apk(self):
        c, _, bot, app = setup(Spark(act(type="reply", text="ok")))
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d, "x.apk"); bad.write_text("not an apk")
            self.assertEqual(self.upload(c, bot, bad).status_code, 400)
            good = fake_apk(Path(d, "y.apk"))
            r = self.upload(c, bot, good, "../../evil name.apk")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["name"], "evil name.apk")  # a path in the name is dropped
            self.assertEqual(app.state.db.get_attachment(r.json()["id"])["mime"], apkcheck.APK_MIME)

    def test_the_model_sees_a_note_not_the_file_and_never_an_image_block(self):
        spark = Spark(act(type="reply", text="ok"))
        c, _, bot, app = setup(spark)
        with tempfile.TemporaryDirectory() as d:
            aid = self.upload(c, bot, fake_apk(Path(d, "y.apk")), "tool.apk").json()["id"]
        c.post(f"/api/bots/{bot}/messages", json={"text": "is this safe?", "attachments": [aid]})
        until(lambda: spark.calls)
        content = spark.calls[0][1][-1]["content"]
        self.assertIsInstance(content, str)  # plain text: no image_url block for a file
        self.assertIn(f"attached APK file, id {aid}", content)
        self.assertIn("use check_apk", content)

    def test_check_apk_reads_tries_and_reports_then_deletes_the_file(self):
        spark = Spark()
        c, _, bot, app = setup(spark)
        phone = ApkPhone()
        app.state.jobs.device = phone
        with tempfile.TemporaryDirectory() as d:
            aid = self.upload(c, bot, fake_apk(Path(d, "y.apk")), "tool.apk").json()["id"]
        path = app.state.db.get_attachment(aid)["path"]
        spark.replies = [json.dumps({"thought": "t", "action": {"type": "check_apk", "attachment": aid}})]
        info = {**apkcheck.parse_manifest(manifest(["READ_SMS", "SEND_SMS"])), "certs": ["ab" * 32], "size_mb": 0.1}
        with mock.patch.object(apkcheck, "analyze", return_value=info), mock.patch.object(apkcheck, "WATCH_S", 0.2):
            c.post(f"/api/bots/{bot}/messages", json={"text": "is this app ok?", "attachments": [aid]})
            until(lambda: any(m["kind"] == "report" for m in c.get(f"/api/bots/{bot}/state").json()["messages"]), timeout=30)
        card = next(m for m in c.get(f"/api/bots/{bot}/state").json()["messages"] if m["kind"] == "report")
        self.assertEqual(card["meta"]["verdict"], "suspicious")
        self.assertEqual(card["meta"]["report"], "apk")
        self.assertEqual([k for k, *_ in phone.log], ["install", "open", "uninstall"])
        self.assertFalse(Path(path).exists())  # the uploaded file is not kept
        self.assertIsNone(app.state.db.get_attachment(aid))

    def test_when_the_first_phone_already_has_the_app_the_other_cloud_phone_is_used(self):
        spark = Spark()
        c, _, bot, app = setup(spark)
        emu, games = ApkPhone(), ApkPhone()
        games.installed.append("com.example.tool")  # Cloud phone 2 has it already
        app.state.jobs.device = emu
        app.state.jobs.virtuals["games"] = (games, None)
        with tempfile.TemporaryDirectory() as d:
            aid = self.upload(c, bot, fake_apk(Path(d, "y.apk")), "tool.apk").json()["id"]
        spark.replies = [json.dumps({"thought": "t", "action": {"type": "check_apk", "attachment": aid}})]  # no "on": Cloud phone 2 is the default
        info = {**apkcheck.parse_manifest(manifest(["INTERNET"])), "certs": ["ab" * 32], "size_mb": 0.1}
        with mock.patch.object(apkcheck, "analyze", return_value=info), mock.patch.object(apkcheck, "WATCH_S", 0.2):
            c.post(f"/api/bots/{bot}/messages", json={"text": "check it", "attachments": [aid]})
            until(lambda: any(m["kind"] == "report" for m in c.get(f"/api/bots/{bot}/state").json()["messages"]), timeout=30)
        self.assertEqual([k for k, *_ in emu.log], ["install", "open", "uninstall"])
        self.assertEqual(games.log, [])  # untouched
        self.assertEqual(games.installed, ["com.example.tool"])

    def test_a_file_that_cannot_be_read_is_explained_and_still_deleted(self):
        spark = Spark()
        c, _, bot, app = setup(spark)
        app.state.jobs.device = ApkPhone()
        with tempfile.TemporaryDirectory() as d:
            aid = self.upload(c, bot, fake_apk(Path(d, "y.apk")), "broken.apk").json()["id"]
        spark.replies = [json.dumps({"thought": "t", "action": {"type": "check_apk", "attachment": aid}})]
        with mock.patch.object(apkcheck, "analyze", side_effect=apkcheck.ApkError("I could not read that APK's manifest.")):
            c.post(f"/api/bots/{bot}/messages", json={"text": "check it", "attachments": [aid]})
            until(lambda: any("I could not check broken.apk" in m["content"] for m in c.get(f"/api/bots/{bot}/state").json()["messages"]), timeout=30)
        until(lambda: app.state.db.get_attachment(aid) is None)

    def test_an_apk_of_another_bot_cannot_be_checked(self):
        spark = Spark()
        c, _, bot, app = setup(spark)
        other = app.state.db.create_bot("Other", "")["id"]
        with tempfile.TemporaryDirectory() as d:
            aid = self.upload(c, other, fake_apk(Path(d, "y.apk"))).json()["id"]
        spark.replies = [json.dumps({"thought": "t", "action": {"type": "check_apk", "attachment": aid}})]
        c.post(f"/api/bots/{bot}/messages", json={"text": "check my file"})
        until(lambda: any("can't find that file" in m["content"] for m in c.get(f"/api/bots/{bot}/state").json()["messages"]))
        self.assertIsNotNone(app.state.db.get_attachment(aid))  # not touched


if __name__ == "__main__":
    unittest.main()
