import os
import sys
import unittest

import fake_mcp
from agent import mcp

STDIO = f"{sys.executable} {fake_mcp.__file__}"


class HttpTransport(unittest.TestCase):
    def test_list_and_call_over_json_and_sse(self):
        for sse in (False, True):
            srv, url, calls, _ = fake_mcp.serve_http(sse=sse)
            self.addCleanup(srv.shutdown)
            c = mcp.open_client({"transport": "http", "url": url})
            tools = mcp.list_tools(c)  # two pages
            self.assertEqual([t["name"] for t in tools], [t["name"] for t in fake_mcp.TOOLS])
            by = {t["name"]: t for t in tools}
            self.assertTrue(by["echo"]["read_only"])
            self.assertFalse(by["add_note"]["read_only"])  # no hint: not trusted as read only
            self.assertFalse(by["wipe"]["read_only"])  # destructive beats read-only
            out = mcp.render(mcp.call_tool(c, "echo", {"text": "hi"}))
            self.assertIn('echo:{"text": "hi"}', out)
            self.assertIn("[image omitted]", out)
            c.close()

    def test_token_header_and_refusal(self):
        srv, url, _, seen = fake_mcp.serve_http(token="good")
        self.addCleanup(srv.shutdown)
        with self.assertRaisesRegex(mcp.McpError, "refused the key"):
            mcp.open_client({"transport": "http", "url": url, "headers": {"Authorization": "Bearer bad"}})
        c = mcp.open_client({"transport": "http", "url": url, "headers": {"Authorization": "Bearer good"}})
        self.assertTrue(all(h.get("Authorization") == "Bearer good" for h in seen[-2:]))
        self.assertEqual(seen[-1].get("MCP-Protocol-Version"), "2025-06-18")
        c.close()

    def test_lost_session_starts_a_fresh_one_once(self):
        srv, url, calls, _ = fake_mcp.serve_http(drop_session_after=3)  # initialize, notification, then the 3rd request is answered 404
        self.addCleanup(srv.shutdown)
        c = mcp.open_client({"transport": "http", "url": url})
        self.assertIn("echo:", mcp.render(mcp.call_tool(c, "echo", {"text": "a"})))
        self.assertEqual(calls, [("echo", {"text": "a"})])
        c.close()

    def test_only_https_or_local_http(self):
        for bad in ("http://example.com/mcp", "ftp://x/y", "file:///etc/passwd", "javascript:alert(1)", "https://", ""):
            with self.assertRaises(mcp.McpError, msg=bad):
                mcp.check_url(bad)
        self.assertEqual(mcp.check_url("https://api.example.com/mcp"), "https://api.example.com/mcp")
        self.assertEqual(mcp.check_url("http://localhost:9/mcp"), "http://localhost:9/mcp")

    def test_redirects_are_not_followed(self):
        srv, url, _, _ = fake_mcp.serve_http()
        self.addCleanup(srv.shutdown)
        with self.assertRaisesRegex(mcp.McpError, "redirected"):
            mcp.open_client({"transport": "http", "url": url.replace("/mcp", "/redirect")})

    def test_unreachable_and_garbage(self):
        with self.assertRaisesRegex(mcp.McpError, "could not reach"):
            mcp.open_client({"transport": "http", "url": "http://127.0.0.1:1/mcp"})


class Render(unittest.TestCase):
    def test_cut_and_error_marker(self):
        r = mcp.render({"content": [{"type": "text", "text": "y" * 100}]}, cap=20)
        self.assertTrue(r.startswith("y" * 20) and "cut: 80 more" in r)
        self.assertTrue(mcp.render({"isError": True, "content": [{"type": "text", "text": "no"}]}).startswith("ERROR FROM THE TOOL: no"))
        self.assertEqual(mcp.render({"content": []}), "(empty result)")
        self.assertEqual(mcp.render({"structuredContent": {"a": 1}}), '{"a": 1}')


class StdioTransport(unittest.TestCase):
    def test_list_call_and_clean_environment(self):
        os.environ["SOME_PARENT_SECRET"] = "must-not-leak"
        self.addCleanup(os.environ.pop, "SOME_PARENT_SECRET", None)
        c = mcp.open_client({"transport": "stdio", "command": STDIO, "env": {"MY_KEY": "k"}})
        self.addCleanup(c.close)
        self.assertEqual(len(mcp.list_tools(c)), len(fake_mcp.TOOLS))
        names = mcp.render(mcp.call_tool(c, "env", {})).split(",")
        self.assertIn("MY_KEY", names)
        self.assertNotIn("SOME_PARENT_SECRET", names)
        self.assertTrue(c.alive())

    def test_missing_command_and_dying_server(self):
        with self.assertRaisesRegex(mcp.McpError, "could not start"):
            mcp.open_client({"transport": "stdio", "command": "/no/such/binary"})
        with self.assertRaisesRegex(mcp.McpError, "stopped|closed"):
            mcp.open_client({"transport": "stdio", "command": f"{sys.executable} -c pass"})  # open_client stops it again

    def test_silent_server_times_out(self):
        c = mcp.StdioClient(f"{sys.executable} -c \"import time; time.sleep(30)\"", timeout=0.5)
        self.addCleanup(c.close)
        with self.assertRaisesRegex(mcp.McpError, "did not answer"):
            c.start()


if __name__ == "__main__":
    unittest.main()
