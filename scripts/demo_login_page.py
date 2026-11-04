"""A tiny test sign-in page for the vault demo (not part of the app): email + password, accepts only the demo password stored as the vault entry "Demo login".
No account, no real service; it says so on the page. Run: .venv/bin/python scripts/demo_login_page.py   (port 8098). A virtual phone reaches it at http://10.0.2.2:8098.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Northwind sign-in</title>
<style>body{font:18px system-ui,sans-serif;margin:0;background:#f4f6fb;color:#16213a}main{max-width:420px;margin:0 auto;padding:28px 22px}
h1{font-size:26px;margin:18px 0 6px}p{color:#4b5877}input{width:100%;box-sizing:border-box;font-size:20px;padding:14px;border:2px solid #9aa8c7;border-radius:12px;margin:8px 0}
button{width:100%;font-size:19px;padding:15px;border:0;border-radius:12px;background:#1f4fd8;color:#fff;font-weight:600;margin-top:8px}small{display:block;margin-top:26px;color:#7a86a3}
.ok{display:none;background:#e3f6ea;border:2px solid #34a853;border-radius:12px;padding:18px;margin-top:18px}.bad{display:none;color:#d93025;margin-top:8px}</style>
<main><b>Northwind</b><h1>Sign in</h1><p>Use your Northwind account.</p>
<input id=e type=email placeholder="Email" value="demo@northwind.test"><input id=p type=password placeholder="Password"><button id=b>Sign in</button>
<div class=bad id=bad>Wrong password.</div><div class=ok id=ok><b>&#10003; Signed in.</b> Welcome back to Northwind.</div>
<small>Test page for a demo. No account, no real service.</small></main>
<script>b.onclick=()=>{if(p.value==='demo-not-a-real-password'){ok.style.display='block';bad.style.display='none';b.style.display='none'}else{bad.style.display='block'}}</script>"""


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
    HTTPServer(("127.0.0.1", 8098), H).serve_forever()
