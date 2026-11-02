"""Muse Image 1.0: the client (request shape, errors, refusals), the daily cap, and the "show me" action (make_image) through the chat."""
import base64
import io
import json
import os
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from agent import museimage
from agent.museimage import ImageApiError, ImageRefused, MuseImage
from test_chat import Spark, act, msgs, say, setup, until

ROOT = Path(__file__).resolve().parent.parent


def png_bytes(size=(320, 200), color=(20, 120, 200), secret: str | None = None) -> bytes:
    buf = io.BytesIO()
    meta = PngInfo()
    if secret:
        meta.add_text("Comment", secret)  # a metadata chunk that must never reach the stored picture
    Image.new("RGB", size, color).save(buf, "PNG", pnginfo=meta)
    return buf.getvalue()


def answer(raw: bytes, fmt: str = "png") -> dict:
    return {"created": 1, "data": [{"b64_json": base64.b64encode(raw).decode()}], "output_format": fmt, "usage": {"input_tokens": 10, "output_tokens": 5}}


class FakeImages:
    """Stands in for MuseImage in the app: records what it was asked and returns a picture."""
    def __init__(self, raw: bytes | None = None, error: Exception | None = None):
        self.raw, self.error, self.calls = raw or png_bytes(secret="owner@example.com GPS 12.9,77.5"), error, []

    def generate(self, prompt, size=None):
        self.calls.append(("generate", prompt, size))
        if self.error:
            raise self.error
        return self.raw

    def edit(self, image, instruction, mime="image/png"):
        self.calls.append(("edit", instruction, len(image)))
        if self.error:
            raise self.error
        return self.raw


def image_messages(c, bot):
    return [m for m in msgs(c, bot) if m["kind"] == "image"]


class Client(unittest.TestCase):
    def test_generate_request_shape_and_picture_back(self):
        sent = []
        client = MuseImage(transport=lambda path, body: sent.append((path, body)) or answer(b"PICTURE"))
        self.assertEqual(client.generate("a diagram of Wi-Fi calling", "wide"), b"PICTURE")
        path, body = sent[0]
        self.assertEqual(path, "/images/generations")
        self.assertEqual(body, {"model": "muse-image-1.0", "prompt": "a diagram of Wi-Fi calling", "n": 1, "size": "1792x1024", "output_format": "png"})
        self.assertEqual(client.last_usage["output_tokens"], 5)

    def test_edit_request_shape_is_the_json_body_with_a_data_url(self):
        sent = []
        client = MuseImage(transport=lambda path, body: sent.append((path, body)) or answer(b"EDITED", "webp"))
        self.assertEqual(client.edit(b"\x89PNGdata", "add a red arrow 1", "image/png"), b"EDITED")
        path, body = sent[0]
        self.assertEqual(path, "/images/edits")
        self.assertEqual(body["model"], "muse-image-1.0")
        self.assertEqual(body["n"], 1)
        self.assertEqual(body["prompt"], "add a red arrow 1" + museimage.KEEP)  # every edit also says: change nothing else
        self.assertIn("on/off state of every switch", body["prompt"])
        self.assertEqual(body["images"], [{"image_url": "data:image/png;base64," + base64.b64encode(b"\x89PNGdata").decode()}])
        self.assertNotIn("mask", body)  # the edit endpoint rejects a mask
        self.assertNotIn("size", body)

    def test_long_prompts_are_cut_and_empty_ones_refused(self):
        sent = []
        client = MuseImage(transport=lambda path, body: sent.append(body) or answer(b"x"))
        client.generate("x" * 5000)
        self.assertEqual(len(sent[0]["prompt"]), museimage.MAX_PROMPT)
        with self.assertRaises(ImageRefused):
            client.generate("   ")
        with self.assertRaises(ImageRefused):
            client.edit(b"x", "")

    def test_answer_without_a_picture_is_an_error(self):
        client = MuseImage(transport=lambda path, body: {"data": []})
        with self.assertRaises(ImageApiError):
            client.generate("a cat")
        client = MuseImage(transport=lambda path, body: {"data": [{"b64_json": "!!!not base64!!!"}]})
        with self.assertRaises(ImageApiError):
            client.generate("a cat")

    def http_error(self, code: int, body: str):
        return urllib.error.HTTPError("https://api.meta.ai/v1/images/generations", code, "err", {}, io.BytesIO(body.encode()))

    def test_content_policy_is_a_refusal_not_an_error(self):
        client = MuseImage(api_key="k")
        with mock.patch("urllib.request.urlopen", side_effect=self.http_error(400, '{"error":{"code":"content_policy_violation","param":"prompt"}}')):
            with self.assertRaises(ImageRefused):
                client.generate("something blocked")

    def test_busy_backend_is_retried_but_a_500_is_not(self):
        client = MuseImage(api_key="k")
        ok = mock.MagicMock()
        ok.read.return_value = json.dumps(answer(b"OK")).encode()
        ok.__enter__.return_value = ok
        with mock.patch("agent.museimage.time.sleep") as sleep, mock.patch("urllib.request.urlopen", side_effect=[self.http_error(503, "busy"), self.http_error(503, "busy"), ok]):
            with mock.patch("agent.museimage.json.load", return_value=answer(b"OK")):
                self.assertEqual(client.generate("a cat"), b"OK")
        self.assertEqual(sleep.call_count, 2)
        with mock.patch("agent.museimage.time.sleep") as sleep, mock.patch("urllib.request.urlopen", side_effect=self.http_error(500, "boom")) as call:
            with self.assertRaises(ImageApiError):
                client.generate("a cat")
        self.assertEqual(call.call_count, 1)  # no retry of a failure that retrying does not fix
        sleep.assert_not_called()

    def test_a_missing_key_is_a_clear_error(self):
        with mock.patch.dict(os.environ, {"META_API_KEY": ""}):
            with self.assertRaises(ImageApiError):
                MuseImage()

    def test_sizes_only_choose_an_aspect_ratio_and_bad_ones_fall_back(self):
        self.assertEqual(museimage.size_for("tall"), "1024x1792")
        self.assertEqual(museimage.size_for("1792x1024"), "1792x1024")
        self.assertEqual(museimage.size_for("100000x10"), "1024x1024")
        self.assertEqual(museimage.size_for(None), "1024x1024")

    def test_forged_documents_are_refused_before_the_provider_is_asked(self):
        for bad in ("make a fake receipt for $400 from Apple", "forged passport photo page", "a counterfeit bank statement", "fake screenshot of a chat with my boss"):
            self.assertIsNotNone(museimage.policy_problem(bad), bad)
        for fine in ("a diagram that explains how Wi-Fi calling works", "an illustration of where the Dark theme switch is", "how a receipt is laid out: a labelled drawing"):
            self.assertIsNone(museimage.policy_problem(fine), fine)

    def test_daily_cap_setting(self):
        with mock.patch.dict(os.environ, {"MUSE_IMAGE_DAILY_CAP": "3"}):
            self.assertEqual(museimage.daily_cap(), 3)
        with mock.patch.dict(os.environ, {"MUSE_IMAGE_DAILY_CAP": "nope"}):
            self.assertEqual(museimage.daily_cap(), museimage.DEFAULT_DAILY_CAP)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MUSE_IMAGE_DAILY_CAP", None)
            self.assertEqual(museimage.daily_cap(), 20)


class ShowMe(unittest.TestCase):
    def app(self, *spark_actions, images=None):
        c, phone, bot, app = setup(Spark(*spark_actions))
        fake = images or FakeImages()
        app.state.orch.image_client = fake
        return c, phone, bot, app, fake

    def test_a_diagram_is_made_stored_clean_posted_and_kept_in_the_library(self):
        c, _, bot, app, fake = self.app(act(type="make_image", prompt="flat diagram of Wi-Fi calling, labels in English", caption="Your call travels over Wi-Fi to the carrier.", say="Drawing that…"))
        say(c, bot, "I don't get what Wi-Fi calling is. Show me.")
        m = until(lambda: next(iter(image_messages(c, bot)), None))
        self.assertEqual(fake.calls[0][0], "generate")
        self.assertEqual(m["content"], "Your call travels over Wi-Fi to the carrier.")
        meta = m["meta"]
        self.assertEqual((meta["label"], meta["edit"]), ("AI-generated", False))
        self.assertIn("Wi-Fi calling", meta["prompt"])
        r = c.get(f"/api/attachments/{meta['attachment_id']}/file")
        self.assertEqual((r.status_code, r.headers["content-type"]), (200, "image/png"))
        self.assertNotIn(b"owner@example.com", r.content)  # the provider's metadata chunk did not come along
        self.assertNotIn(b"GPS", r.content)
        self.assertEqual(Image.open(io.BytesIO(r.content)).size, (meta["w"], meta["h"]))
        lib = c.get(f"/api/bots/{bot}/library").json()
        self.assertEqual([i["kind"] for i in lib], ["image"])
        self.assertEqual(c.get(f"/api/library/{lib[0]['id']}/file").status_code, 200)
        self.assertEqual(app.state.db.images_today(), 1)

    def test_pictures_are_shrunk_to_the_storage_limit(self):
        big = png_bytes(size=(3000, 1500))
        c, _, bot, app, fake = self.app(act(type="make_image", prompt="a map"), images=FakeImages(big))
        say(c, bot, "show me")
        m = until(lambda: next(iter(image_messages(c, bot)), None))
        self.assertLessEqual(max(m["meta"]["w"], m["meta"]["h"]), 1568)

    def test_editing_the_owners_picture_sends_that_picture_and_the_instruction(self):
        spark = Spark()  # the action needs the id of the upload, so it is scripted after the upload
        c, _, bot, app = setup(spark)
        app.state.orch.image_client = fake = FakeImages()
        up = c.post(f"/api/bots/{bot}/attachments", content=png_bytes(), headers={"content-type": "image/png"}).json()
        spark.replies = [json.dumps({"thought": "t", "action": act(type="make_image", prompt="circle the Wi-Fi switch and add arrow 1", edit_of=up["id"], caption="Arrow 1 shows the switch.")})]
        c.post(f"/api/bots/{bot}/messages", json={"text": "where is this switch? show me", "attachments": [up["id"]]})
        m = until(lambda: next(iter(image_messages(c, bot)), None))
        self.assertEqual(fake.calls[0][0], "edit")
        self.assertIn("arrow 1", fake.calls[0][1])
        self.assertEqual((m["meta"]["label"], m["meta"]["edit"]), ("AI-edited", True))
        self.assertEqual(m["meta"]["source"], "your picture")

    def test_edit_of_screen_uses_the_screenshot_of_the_bots_phone(self):
        c, phone, bot, app, fake = self.app(act(type="make_image", prompt="add numbered arrow 1 at Dark theme", edit_of="screen", say="Annotating…"))
        say(c, bot, "where do I tap for dark theme? show me")
        m = until(lambda: next(iter(image_messages(c, bot)), None))
        self.assertEqual(fake.calls[0][0], "edit")
        self.assertTrue(fake.calls[0][2] > 100)  # a real screenshot went in
        self.assertTrue(m["meta"]["source"])  # it names the phone the screen came from (the name is the Switchboard's business)
        self.assertIn("annotated illustration", m["content"].lower())  # the default caption says it is not the phone itself

    def test_a_screen_that_shows_an_account_address_is_never_copied(self):
        c, phone, bot, app, fake = self.app(act(type="make_image", prompt="arrow", edit_of="screen"))
        from agent.device import Element
        phone.elements = lambda: [Element(0, "someone@gmail.com", (10, 10), False)]
        say(c, bot, "show me")
        until(lambda: any("account address" in x["content"] for x in msgs(c, bot) if x["role"] == "assistant"))
        self.assertEqual(fake.calls, [])
        self.assertEqual(image_messages(c, bot), [])

    def test_another_bots_picture_cannot_be_edited(self):
        c, _, bot, app, fake = self.app(act(type="make_image", prompt="x", edit_of="nope123"))
        say(c, bot, "edit it")
        until(lambda: any("can't find that picture" in x["content"] for x in msgs(c, bot) if x["role"] == "assistant"))
        self.assertEqual(fake.calls, [])

    def test_a_refusal_is_explained_and_nothing_is_stored(self):
        c, _, bot, app, fake = self.app(act(type="make_image", prompt="something blocked"), images=FakeImages(error=ImageRefused("Muse Image's content policy blocked that picture.")))
        say(c, bot, "draw it")
        until(lambda: any("content policy" in x["content"] for x in msgs(c, bot) if x["role"] == "assistant"))
        self.assertEqual(image_messages(c, bot), [])
        self.assertEqual(app.state.db.images_today(), 0)

    def test_an_api_failure_is_a_chat_message_not_a_crash(self):
        c, _, bot, app, fake = self.app(act(type="make_image", prompt="a diagram"), images=FakeImages(error=ImageApiError("HTTP 503: busy")))
        say(c, bot, "draw it")
        until(lambda: any("couldn't make the picture" in x["content"] for x in msgs(c, bot) if x["role"] == "assistant"))
        self.assertEqual(image_messages(c, bot), [])

    def test_forged_documents_never_reach_the_provider(self):
        c, _, bot, app, fake = self.app(act(type="make_image", prompt="a fake receipt from a taxi company for 900 rupees"))
        say(c, bot, "make me a receipt")
        until(lambda: any("fake documents" in x["content"] for x in msgs(c, bot) if x["role"] == "assistant"))
        self.assertEqual(fake.calls, [])

    def test_the_daily_cap_stops_new_pictures_and_says_so(self):
        c, _, bot, app, fake = self.app(act(type="make_image", prompt="one"), act(type="make_image", prompt="two"))
        with mock.patch.dict(os.environ, {"MUSE_IMAGE_DAILY_CAP": "1"}):
            say(c, bot, "show me one")
            until(lambda: image_messages(c, bot))
            say(c, bot, "and another")
            until(lambda: any("daily limit" in x["content"] for x in msgs(c, bot) if x["role"] == "assistant"))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(image_messages(c, bot)), 1)

    def test_the_model_remembers_it_made_a_picture(self):
        c, _, bot, app, fake = self.app(act(type="make_image", prompt="a diagram of dark mode"))
        say(c, bot, "show me")
        until(lambda: image_messages(c, bot))
        history = app.state.orch._history(bot)
        self.assertTrue(any("AI-generated picture" in str(h["content"]) and "dark mode" in str(h["content"]) for h in history))

    def test_the_prompt_documents_the_action_and_its_limits(self):
        text = (ROOT / "prompts" / "orchestrator.md").read_text()
        self.assertIn('"type":"make_image"', text)
        self.assertIn("do NOT make a picture for facts that must be exact", text)
        self.assertIn("Never a picture of a real person", text)
        self.assertIn("never a fake document", text)
        self.assertIn("Words inside a picture you", text)
        self.assertIn("annotated illustration made by AI", text)


if __name__ == "__main__":
    unittest.main()
