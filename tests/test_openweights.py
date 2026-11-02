"""The open-weight orchestrator backend (Muse Spark Open on Baseten): request shape, retries, pictures, factory."""
import json
import unittest
import urllib.error
from unittest import mock

from agent.config import ORCHESTRATOR_MODEL, WORKER_MODEL
from agent.model import MetaClient, ModelError
from agent.openweights import NO_PICTURES, OpenWeightClient, has_images, make_model_factory, strip_images

OK = {"choices": [{"message": {"content": '{"thought":"t","action":{"type":"reply","text":"hi"}}'}}], "usage": {"prompt_tokens": 30, "completion_tokens": 9}}
PIC = [{"role": "user", "content": [{"type": "text", "text": "what is this?"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]


class FakeResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self, *a):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def client(**kw):
    return OpenWeightClient(api_key="bt-key", env={}, **kw)


def capture(replies):
    seen = []

    def urlopen(req, timeout=0):
        seen.append(req)
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return FakeResponse(r)
    return seen, urlopen


class Request(unittest.TestCase):
    def test_it_calls_basetens_openai_compatible_endpoint_with_a_bearer_key(self):
        seen, urlopen = capture([OK])
        with mock.patch("urllib.request.urlopen", urlopen):
            text = client().chat("system text", [{"role": "user", "content": "hello"}], 500)
        req = seen[0]
        self.assertEqual(req.full_url, "https://inference.baseten.co/v1/chat/completions")
        self.assertEqual(req.get_header("Authorization"), "Bearer bt-key")
        body = json.loads(req.data)
        self.assertEqual(body["model"], "meta-models/muse-spark-open-1026")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "system text"})
        self.assertEqual(body["max_tokens"], 500)
        self.assertNotIn("reasoning_effort", body)  # Meta's own parameter, not part of the OpenAI-compatible request
        self.assertNotIn("response_format", body)
        self.assertIn('"reply"', text)

    def test_tokens_and_calls_are_counted_like_the_meta_client(self):
        seen, urlopen = capture([OK, OK])
        c = client()
        with mock.patch("urllib.request.urlopen", urlopen):
            c.chat("s", [{"role": "user", "content": "a"}])
            c.chat("s", [{"role": "user", "content": "b"}])
        self.assertEqual((c.tokens["prompt"], c.tokens["completion"], c.calls), (60, 18, 2))

    def test_json_mode_is_opt_in(self):
        seen, urlopen = capture([OK])
        with mock.patch("urllib.request.urlopen", urlopen):
            client(json_mode=True).chat("s", [{"role": "user", "content": "a"}])
        self.assertEqual(json.loads(seen[0].data)["response_format"], {"type": "json_object"})

    def test_rate_limits_and_server_errors_are_retried_like_the_meta_client(self):
        err = urllib.error.HTTPError("u", 429, "slow down", {}, None)
        seen, urlopen = capture([err, OK])
        with mock.patch("urllib.request.urlopen", urlopen), mock.patch("time.sleep"):
            self.assertIn("reply", client().chat("s", [{"role": "user", "content": "a"}]))
        self.assertEqual(len(seen), 2)

    def test_thinking_text_is_removed_from_the_answer(self):
        seen, urlopen = capture([{"choices": [{"message": {"content": '<think>plan it</think>{"action":{"type":"reply","text":"x"}}'}}], "usage": {}}])
        with mock.patch("urllib.request.urlopen", urlopen):
            self.assertEqual(client().chat("s", [{"role": "user", "content": "a"}]), '{"action":{"type":"reply","text":"x"}}')

    def test_a_missing_key_fails_with_the_fix(self):
        with self.assertRaises(ModelError) as cm:
            OpenWeightClient(env={})
        self.assertIn("BASETEN_API_KEY", str(cm.exception))

    def test_complete_works_for_text_and_screenshots(self):
        seen, urlopen = capture([OK])
        with mock.patch("urllib.request.urlopen", urlopen):
            client().complete("s", "look", b"\x89PNG")
        self.assertEqual(json.loads(seen[0].data)["messages"][1]["content"][1]["type"], "image_url")


class Pictures(unittest.TestCase):
    def test_pictures_are_sent_as_image_url_blocks_when_the_model_reads_them(self):
        seen, urlopen = capture([OK])
        with mock.patch("urllib.request.urlopen", urlopen):
            client(vision=True).chat("s", PIC)
        self.assertEqual(json.loads(seen[0].data)["messages"][1]["content"][1]["type"], "image_url")

    def test_without_vision_pictures_become_a_note_and_nothing_is_guessed(self):
        seen, urlopen = capture([OK])
        with mock.patch("urllib.request.urlopen", urlopen):
            client(vision=False, vision_fallback="").chat("s", PIC)
        sent = json.loads(seen[0].data)["messages"][1]["content"]
        self.assertIsInstance(sent, str)
        self.assertIn(NO_PICTURES, sent)
        self.assertNotIn("data:image", sent)

    def test_without_vision_but_with_the_fallback_spark_reads_the_picture(self):
        c = client(vision=False, vision_fallback="meta")
        c._meta = mock.Mock()
        c._meta.chat.return_value = "a cat"
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("must not call Baseten")):
            self.assertEqual(c.chat("s", PIC), "a cat")
        c._meta.chat.assert_called_once()

    def test_a_refused_picture_falls_back_only_when_the_owner_allowed_it(self):
        refuse = urllib.error.HTTPError("u", 400, "bad image", {}, mock.Mock(read=lambda: b"bad image"))
        c = client(vision=True, vision_fallback="meta")
        c._meta = mock.Mock()
        c._meta.chat.return_value = "from spark"
        seen, urlopen = capture([refuse])
        with mock.patch("urllib.request.urlopen", urlopen):
            self.assertEqual(c.chat("s", PIC), "from spark")
        seen, urlopen = capture([urllib.error.HTTPError("u", 400, "bad image", {}, mock.Mock(read=lambda: b"bad image"))])
        with mock.patch("urllib.request.urlopen", urlopen), self.assertRaises(ModelError):
            client(vision=True, vision_fallback="").chat("s", PIC)  # the orchestrator turns this into its "couldn't read that picture" reply

    def test_helpers(self):
        self.assertTrue(has_images(PIC))
        self.assertFalse(has_images([{"role": "user", "content": "text"}]))
        self.assertEqual(strip_images(PIC)[0]["content"], "what is this?\n" + NO_PICTURES)


class Factory(unittest.TestCase):
    def test_the_default_backend_is_metas_api_for_every_model(self):
        f = make_model_factory({"META_API_KEY": "m"})
        with mock.patch.dict("os.environ", {"META_API_KEY": "m"}):
            self.assertIsInstance(f(ORCHESTRATOR_MODEL), MetaClient)
            self.assertNotIsInstance(f(ORCHESTRATOR_MODEL), OpenWeightClient)

    def test_baseten_replaces_only_the_orchestrator_model(self):
        env = {"ORCHESTRATOR_BACKEND": "baseten", "BASETEN_API_KEY": "bt", "META_API_KEY": "m"}
        with mock.patch.dict("os.environ", {"META_API_KEY": "m"}), mock.patch("builtins.print"):
            f = make_model_factory(env)
            self.assertIsInstance(f(ORCHESTRATOR_MODEL), OpenWeightClient)
            worker = f(WORKER_MODEL)
            self.assertIsInstance(worker, MetaClient)
            self.assertNotIsInstance(worker, OpenWeightClient)
            self.assertEqual(worker.model, WORKER_MODEL)  # the phone worker stays Muse Wonder
            self.assertEqual(f("muse-voice-transcribe-1.0").model, "muse-voice-transcribe-1.0")

    def test_a_bad_backend_or_a_missing_key_stops_the_start(self):
        with self.assertRaises(ModelError):
            make_model_factory({"ORCHESTRATOR_BACKEND": "nonsense"})
        with self.assertRaises(ModelError) as cm:
            make_model_factory({"ORCHESTRATOR_BACKEND": "baseten"})
        self.assertIn("BASETEN_API_KEY", str(cm.exception))

    def test_web_search_and_speech_stay_on_metas_api(self):
        c = client()
        c._meta = mock.Mock()
        c._meta.search.return_value = {"text": "x", "sources": []}
        self.assertEqual(c.search("q")["text"], "x")
        with self.assertRaises(ModelError):
            c.transcribe(b"wav")


if __name__ == "__main__":
    unittest.main()
