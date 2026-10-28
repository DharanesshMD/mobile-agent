import http.client
import json
import unittest
import urllib.error
from unittest import mock

from agent.model import MetaClient, ModelError


class FakeResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self, *a):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


OK = {"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
CACHED = {"choices": [{"message": {"content": "hi"}}],
          "usage": {"prompt_tokens": 10, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 7}}}


class Retries(unittest.TestCase):
    def client(self):
        return MetaClient("muse-spark-1.3", api_key="k", base="http://x/v1")

    def test_dropped_connection_is_retried(self):
        calls = [http.client.RemoteDisconnected("closed"), ConnectionResetError(), FakeResponse(OK)]

        def urlopen(*a, **k):
            r = calls.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch("urllib.request.urlopen", urlopen), mock.patch("time.sleep"):
            c = self.client()
            self.assertEqual(c.chat("s", [{"role": "user", "content": "x"}]), "hi")
            self.assertEqual(c.tokens, {"prompt": 3, "completion": 1, "cached": 0})

    def test_cached_prompt_tokens_are_counted(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(CACHED)):
            c = self.client()
            c.chat("s", [{"role": "user", "content": "x"}])
            self.assertEqual(c.tokens["cached"], 7)

    def test_gives_up_after_three_attempts_with_a_model_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=http.client.RemoteDisconnected("closed")) as m, mock.patch("time.sleep"):
            with self.assertRaises(ModelError):
                self.client().chat("s", [{"role": "user", "content": "x"}])
            self.assertEqual(m.call_count, 3)

    def test_rate_limit_is_retried_but_a_bad_request_is_not(self):
        def http_error(code):
            return urllib.error.HTTPError("http://x", code, "err", {}, None)

        seq = [http_error(429), FakeResponse(OK)]

        def urlopen(*a, **k):
            r = seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch("urllib.request.urlopen", urlopen), mock.patch("time.sleep"):
            self.assertEqual(self.client().chat("s", [{"role": "user", "content": "x"}]), "hi")
        with mock.patch("urllib.request.urlopen", side_effect=http_error(400)) as m, mock.patch("time.sleep"):
            with self.assertRaises(ModelError):
                self.client().chat("s", [{"role": "user", "content": "x"}])
            self.assertEqual(m.call_count, 1)


if __name__ == "__main__":
    unittest.main()


class SearchParsing(unittest.TestCase):
    def test_search_returns_the_last_message_and_deduplicated_sources(self):
        from agent.model import MetaClient
        m = MetaClient("muse-spark-1.3", api_key="k")
        seen = {}

        def fake_post(path, body):
            seen["path"], seen["body"] = path, body
            return {"usage": {"input_tokens": 100, "output_tokens": 10, "input_tokens_details": {"cached_tokens": 40}},
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "I'll search.", "annotations": []}]},
                               {"type": "web_search_call", "results": [{"title": "A", "url": "https://a.example", "snippet": "about a"},
                                                                       {"title": "B", "url": "https://b.example", "snippet": "about b"}]},
                               {"type": "message", "content": [{"type": "output_text", "text": "The answer.", "annotations": [
                                   {"type": "url_citation", "url": "https://b.example", "title": "B cited"}]}]}]}
        m._post = fake_post
        r = m.search("q")
        self.assertEqual(r["text"], "The answer.")
        self.assertEqual([s["url"] for s in r["sources"]], ["https://b.example", "https://a.example"])  # cited first
        self.assertEqual(r["sources"][0]["snippet"], "about b")
        self.assertEqual(seen["path"], "/responses")
        self.assertEqual(seen["body"]["tools"], [{"type": "web_search", "search_context_size": "low"}])
        self.assertEqual(m.tokens, {"prompt": 100, "completion": 10, "cached": 40})
