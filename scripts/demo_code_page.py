"""A tiny test page for the verification-code demo (not part of the app): a sign-in screen that asks for the 6-digit code it "texted" you and accepts any 6 digits.
It exists so the demo can show a phone task reaching a real code screen without creating an account anywhere. It says so on the page.

Run: .venv/bin/python scripts/demo_code_page.py   (port 8099). A virtual phone reaches it at http://10.0.2.2:8099.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Northwind sign-in</title>
<style>body{font:18px system-ui,sans-serif;margin:0;background:#f4f6fb;color:#16213a}main{max-width:420px;margin:0 auto;padding:28px 22px}
h1{font-size:26px;margin:18px 0 6px}p{color:#4b5877;line-height:1.45}input{width:100%;box-sizing:border-box;font-size:30px;letter-spacing:.35em;text-align:center;padding:14px;border:2px solid #9aa8c7;border-radius:12px;margin:14px 0}
button{width:100%;font-size:19px;padding:15px;border:0;border-radius:12px;background:#1f4fd8;color:#fff;font-weight:600}small{display:block;margin-top:26px;color:#7a86a3}
.ok{display:none;background:#e3f6ea;border:2px solid #34a853;border-radius:12px;padding:18px;margin-top:18px}</style>
<main><b>Northwind</b><h1>Verify it&rsquo;s you</h1><p>We sent a 6-digit code by text to the phone ending in <b>&bull;&bull;12</b>. Enter it below.</p>
<input id=c inputmode=numeric maxlength=6 placeholder="------" autocomplete=one-time-code><button id=b>Verify</button>
<div class=ok id=ok><b>&#10003; Verified.</b> You are signed in to Northwind.</div>
<small>Test page for a demo. No account, no real service.</small></main>
<script>b.onclick=()=>{if(/^\\d{6}$/.test(c.value)){ok.style.display='block';b.style.display='none'}else{c.style.borderColor='#d93025'}}</script>"""


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8099), H).serve_forever()
