"""Looking for other ways: search and read pages politely, show the options with the risks that were really read, and do nothing until the owner picks."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import gamepolicy, options, websearch
from agent.device import Element
from agent.graph import run
from test_chat import Spark, act, say, setup, texts, until
from test_escalate import Emu, Extra, GamesP, assistant, build, said
from test_gamepolicy import BlockedPhone, page
from test_loop import FakeModel, reply

DDG = """<html><body>
<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.easyfun.gg%2Fgames%2Fclashofclans&amp;rut=abc">EasyFun - Play Clash of Clans online</a>
<a class="result__snippet" href="x">Play Android games in your browser, no download.</a></div>
<div class="result"><a class="result__a" href="https://supercell.com/en/games/clashofclans/">Clash of Clans | Supercell</a>
<div class="result__snippet">The official page of the game.</div></div>
<div class="result"><a class="result__a" href="http://insecure.example/x">Plain http result</a><a class="result__snippet">dropped</a></div>
</body></html>"""

EASYFUN_HOME = """<html><head><script>var secret=1</script></head><body><h1>EasyFun.gg cloud gaming</h1>
<p>Log in with your Supercell ID e-mail code to play. Free users get 30 minutes per day.</p>
<p>EasyFun is operated by FunEase Network Technology Limited.</p>
<a href="/terms">Terms of Service</a> <a href="https://www.easyfun.gg/privacy">Privacy Policy</a> <a href="https://evil.example/terms">Terms</a></body></html>"""
EASYFUN_PRIVACY = "<html><body><p>We may share your data with marketing partners and advertising networks.</p></body></html>"
EASYFUN_TERMS = "<html><body><p>You use the service at your own risk.</p></body></html>"  # silent on bots and bans
WIKI = "<html><body><p>Clash of Clans is a freemium mobile game.</p></body></html>"


class FakeProvider:
    def __init__(self, results):
        self.results, self.queries = results, []

    def search(self, query):
        self.queries.append(query)
        return list(self.results)


def fake_fetch(pages):
    def fetch(url):
        if url not in pages:
            raise OSError("not found")
        return websearch.html_text(pages[url])
    return fetch


RESULTS = [{"title": "EasyFun - Play Clash of Clans online", "url": "https://www.easyfun.gg/games/clashofclans", "snippet": "Play Android games in your browser."},
           {"title": "Clash of Clans - Wikipedia", "url": "https://en.wikipedia.org/wiki/Clash_of_Clans", "snippet": "A freemium mobile game."}]
PAGES = {"https://www.easyfun.gg/games/clashofclans": EASYFUN_HOME, "https://www.easyfun.gg/terms": EASYFUN_TERMS, "https://www.easyfun.gg/privacy": EASYFUN_PRIVACY,
         "https://en.wikipedia.org/wiki/Clash_of_Clans": WIKI}
MODEL_OPTIONS = json.dumps({"options": [
    {"name": "EasyFun.gg", "url": "https://www.easyfun.gg/games/clashofclans", "what": "Plays Android games in a browser.", "unofficial": True},
    {"name": "A very long page title about playing Clash of Clans on a PC with controls and tips", "url": "https://en.wikipedia.org/wiki/Clash_of_Clans", "what": "An article.", "unofficial": True},
    {"name": "Invented site", "url": "https://made-up.example/play", "what": "not in the results", "unofficial": True}]})


class Search(unittest.TestCase):
    def test_duckduckgo_results_are_parsed_and_unwrapped(self):
        r = websearch.parse_ddg(DDG)
        self.assertEqual([x["url"] for x in r], ["https://www.easyfun.gg/games/clashofclans", "https://supercell.com/en/games/clashofclans/"])  # http results are dropped
        self.assertEqual(r[0]["title"], "EasyFun - Play Clash of Clans online")
        self.assertEqual(r[0]["snippet"], "Play Android games in your browser, no download.")

    def test_a_query_never_carries_the_owners_data(self):
        self.assertEqual(websearch.clean_query("play clash of clans, my mail is me@example.com, token abcdefghijklmnopqrstuvwxyz123456, id 123456789"), "play clash of clans, my mail is token , id")
        self.assertEqual(websearch.plan_queries("my card is 4111 1111 1111 1111"), [])  # a card number: nothing is searched at all
        q = websearch.plan_queries("play clash of clans online")
        self.assertEqual(len(q), 3)
        self.assertTrue(all("@" not in x for x in q))
        self.assertEqual(websearch.plan_queries("ab"), [])

    def test_only_public_https_pages_are_read(self):
        for url in ("http://example.com/", "https://127.0.0.1/", "https://localhost/x", "https://user:pw@example.com/", "file:///etc/passwd", "https://192.168.1.5/"):
            with self.assertRaises(Exception, msg=url):
                websearch.fetch_page(url)

    def test_page_text_skips_scripts_and_finds_the_sites_own_policy_pages(self):
        text, links = websearch.html_text(EASYFUN_HOME)
        self.assertIn("30 minutes per day", text)
        self.assertNotIn("secret=1", text)
        self.assertEqual(websearch.policy_links("https://www.easyfun.gg/games/clashofclans", links), ["https://www.easyfun.gg/terms", "https://www.easyfun.gg/privacy"])  # not the other site's

    def test_the_provider_is_polite(self):
        with mock.patch("agent.websearch.time.sleep") as sleep, mock.patch("agent.websearch.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = DDG.encode()
            ddg = websearch.DuckDuckGo(gap_s=10)
            websearch.DuckDuckGo._last = 0
            ddg.search("a b c")
            ddg.search("d e f")
            req = urlopen.call_args[0][0]
            self.assertIn("MobileAgent", req.headers["User-agent"])
            self.assertTrue(sleep.called)  # the second query waited out its gap
            self.assertEqual(urlopen.call_args[1]["timeout"], websearch.TIMEOUT_S)
        with mock.patch.dict("os.environ", {"SEARCH_API_KEY": "k"}):
            self.assertEqual(websearch.default_provider().name, "brave")


class Facts(unittest.TestCase):
    def test_in_game_purchases_are_not_the_sites_cost_and_an_apk_download_address_is_a_risk(self):
        facts, _ = options.extract_facts(["These allow you to buy gems, a premium currency that can speed up construction."], ["https://www.example-apk.com/mobilegames/download/latest/com.supercell.clashofclans"])
        tags = [t for t, _ in facts]
        self.assertNotIn("time limits or a cost", tags)
        self.assertIn("offers app (APK) downloads from outside the official store", tags)

    def test_the_second_search_looks_for_cloud_and_browser_ways(self):
        q = websearch.plan_queries("find another way to play clash of clans online")
        self.assertIn("cloud gaming", q[1])
        self.assertIn("official", q[2])


class Providers(unittest.TestCase):
    def test_the_model_providers_own_search_is_used_when_there_is_no_key(self):
        class M:
            def search(self, q):
                return {"text": "x", "sources": [{"title": "EasyFun", "url": "https://www.easyfun.gg/", "snippet": "cloud"}, {"title": "", "url": "http://insecure.example"}]}
        with mock.patch.dict("os.environ", {"SEARCH_API_KEY": ""}):
            p = websearch.choose_provider(M())
            self.assertEqual(p.name, "model")
            self.assertEqual([r["url"] for r in p.search("q")], ["https://www.easyfun.gg/"])  # https only
            self.assertEqual(websearch.choose_provider(None).name, "duckduckgo")
        with mock.patch.dict("os.environ", {"SEARCH_API_KEY": "k"}):
            self.assertEqual(websearch.choose_provider(M()).name, "brave")

    def test_a_search_site_that_asks_for_a_human_check_is_not_worked_around_and_the_card_says_so(self):
        class Blocked:
            name = "duckduckgo"

            def search(self, q):
                raise websearch.SearchBlocked("asked for a human check")
        notes = []
        found = options.research("play clash of clans online", "x", FakeModel2("{}"), Blocked(), fake_fetch(PAGES), "Clash of Clans", notes=notes)
        self.assertTrue(all(o["official"] for o in found))  # only the official routes remain
        self.assertIn("did not work", notes[0])
        text = options.card_text(found, "Other ways:", notes)
        self.assertIn("Note: the web search (duckduckgo) did not work", text)
        self.assertIn("only the official routes", text)

    def test_the_ddg_page_with_a_captcha_raises_instead_of_returning_nothing(self):
        class R:
            status = 202

            def read(self, n): return b"<html>anomaly-modal captcha</html>"

            def __enter__(self): return self

            def __exit__(self, *a): return False
        with mock.patch("agent.websearch.urllib.request.urlopen", return_value=R()), mock.patch("agent.websearch.time.sleep"):
            with self.assertRaises(websearch.SearchBlocked):
                websearch.DuckDuckGo(gap_s=0).search("a b c")


class Research(unittest.TestCase):
    def go(self, model_reply=MODEL_OPTIONS, game="Clash of Clans", pages=None):
        prov = FakeProvider(RESULTS)
        found = options.research("play clash of clans online", "another way to use Clash of Clans", FakeModel2(model_reply), prov, fake_fetch(PAGES if pages is None else pages), game)
        return found, prov

    def test_official_routes_come_first_and_third_party_sites_are_marked_unofficial(self):
        found, prov = self.go()
        self.assertEqual(len(prov.queries), 3)
        self.assertTrue(all(o["official"] for o in found[:3]))
        self.assertIn("Google Play (official)", found[0]["name"])
        self.assertIn("Google Play Games on PC", [o["name"] for o in found if "PC" in o["name"]][0])
        easy = next(o for o in found if "easyfun" in o["url"])
        self.assertFalse(easy["official"])

    def test_risk_tags_come_only_from_what_was_read_and_missing_checks_are_stated(self):
        found, _ = self.go()
        easy = next(o for o in found if "easyfun" in o["url"])
        tags = dict(easy["facts"])
        self.assertIn("login with a third-party account or a game ID", tags)
        self.assertIn("Supercell ID", tags["login with a third-party account or a game ID"])
        self.assertIn("shares data with marketing or advertising partners", tags)  # from the privacy page it linked
        self.assertIn("time limits or a cost", tags)
        self.assertIn("30 minutes per day", tags["time limits or a cost"])
        self.assertIn("FunEase Network Technology Limited", tags["operator"])
        self.assertNotIn("the terms mention bots or automation", tags)  # the terms were silent: no invented risk
        self.assertIn("the terms mention bots or automation", easy["missing"])
        self.assertIn("the terms put no liability on the site for bans or account loss", easy["missing"])
        self.assertIn("https://www.easyfun.gg/privacy", easy["sources"])

    def test_long_titles_are_cut_at_a_word_and_name_the_site(self):
        found, _ = self.go()
        wiki = next(o for o in found if "wikipedia" in o["url"])
        self.assertTrue(wiki["name"].endswith("(en.wikipedia.org)"))
        self.assertLessEqual(len(wiki["name"]), 70)
        self.assertNotIn("PC with", wiki["name"])

    def test_a_service_the_owner_names_is_read_at_its_own_address_and_junk_results_are_dropped(self):
        junk = [{"title": "Easyfun review", "url": "https://blog.example/easyfun-review", "snippet": "an article"},
                {"title": "COC guide", "url": "https://ftp.example.it/book-search/x/coc_guide.pdf", "snippet": "a pdf"}]
        prov = FakeProvider(junk)
        found = options.research("play clash of clans on easyfun", "x", FakeModel2("{}"), prov, fake_fetch(PAGES), "Clash of Clans")
        urls = [o["url"] for o in found]
        self.assertIn("https://www.easyfun.gg/", urls)
        self.assertFalse([u for u in urls if u.endswith(".pdf") or "ftp." in u])
        easy = next(o for o in found if "easyfun" in o["url"])
        self.assertFalse(easy["official"])

    def test_a_card_never_has_more_than_five_choices_and_always_keeps_none(self):
        many = [{"title": f"Site {i}", "url": f"https://site{i}.example/play", "snippet": "x"} for i in range(8)]
        found = options.research("play clash of clans online", "x", FakeModel2("{}"), FakeProvider(many), fake_fetch(PAGES), "Clash of Clans")
        self.assertLessEqual(len(found), options.MAX_SHOWN)
        self.assertTrue(all(o["official"] for o in found[:3]))

    def test_a_site_the_search_did_not_return_is_never_an_option(self):
        found, _ = self.go()
        self.assertNotIn("made-up.example", " ".join(o["url"] for o in found))

    def test_when_the_page_cannot_be_read_nothing_is_claimed(self):
        found, _ = self.go(pages={})
        easy = next(o for o in found if "easyfun" in o["url"])
        self.assertEqual(easy["facts"], [])
        self.assertIn("could not be read", " ".join(easy["missing"]))

    def test_a_model_that_fails_still_gives_the_top_results_plainly(self):
        found, _ = self.go(model_reply="I could not do that")
        self.assertTrue(any("easyfun" in o["url"] for o in found))

    def test_the_card_text_says_what_it_is_unofficial_risky_and_not_found(self):
        found, _ = self.go()
        text = options.card_text(found, "Other ways:")
        self.assertIn("UNOFFICIAL third party", text)
        self.assertIn("official", text)
        self.assertIn("Risks found:", text)
        self.assertIn("Not found in what I read:", text)
        self.assertIn("Nothing runs until you pick", text)
        self.assertIn("https://www.easyfun.gg/privacy", text)


class FakeModel2:
    def __init__(self, text):
        self.text, self.prompts = text, []

    def chat(self, system, messages, max_tokens=0):
        self.prompts.append(messages[-1]["content"])
        return self.text


def web_card(c, bot):
    items = [m for m in c.get(f"/api/bots/{bot}/state").json()["messages"] if m["kind"] == "action_item" and m["meta"].get("web")]
    return items[-1] if items else None


class Card(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "third.json"
        self.old, gamepolicy.THIRD_PARTY_FILE = gamepolicy.THIRD_PARTY_FILE, self.tmp

    def tearDown(self):
        gamepolicy.THIRD_PARTY_FILE = self.old

    def start(self):
        spark = Spark(act(type="web_options", query="find another way to play clash of clans online", goal="play Clash of Clans online"), MODEL_OPTIONS)
        c, phone, bot, app = setup(spark)
        app.state.orch.web_provider, app.state.orch.web_fetch = FakeProvider(RESULTS), fake_fetch(PAGES)
        opened = []
        phone.open_url = lambda url: opened.append(url)
        return c, phone, bot, app, opened

    def test_the_action_posts_a_card_and_nothing_runs_before_a_pick(self):
        c, phone, bot, app, opened = self.start()
        say(c, bot, "find another way for me to play clash of clans online")
        until(lambda: web_card(c, bot), timeout=30)
        card = web_card(c, bot)
        self.assertEqual(card["meta"]["kind"], "choice")
        self.assertEqual(card["meta"]["options"][-1], options.NONE_LABEL)
        self.assertIn("UNOFFICIAL", card["content"])
        self.assertIn("Google Play (official)", card["content"])
        self.assertEqual((opened, phone.taps, c.get(f"/api/bots/{bot}/jobs").json()), ([], [], []))  # nothing opened, tapped or queued

    def test_none_does_nothing(self):
        c, phone, bot, app, opened = self.start()
        say(c, bot, "find another way for me to play clash of clans online")
        until(lambda: web_card(c, bot), timeout=30)
        card = web_card(c, bot)
        c.post(f"/api/items/{card['meta']['item_id']}/answer", json={"choice": options.NONE_LABEL})
        until(lambda: any("Nothing was opened or changed" in t for t in texts(c, bot, "assistant")))
        self.assertEqual(opened, [])

    def test_a_pick_then_show_me_the_link_only_posts_the_link(self):
        c, phone, bot, app, opened = self.start()
        say(c, bot, "find another way for me to play clash of clans online")
        until(lambda: web_card(c, bot), timeout=30)
        first = web_card(c, bot)
        c.post(f"/api/items/{first['meta']['item_id']}/answer", json={"choice": "EasyFun.gg"})
        until(lambda: web_card(c, bot)["meta"]["item_id"] != first["meta"]["item_id"])
        second = web_card(c, bot)
        self.assertEqual(second["meta"]["options"][:2], ["Show me the link", "Open it on Cloud phone"])
        c.post(f"/api/items/{second['meta']['item_id']}/answer", json={"choice": "Show me the link"})
        until(lambda: any("https://www.easyfun.gg/games/clashofclans" in t and "Unofficial third party" in t for t in texts(c, bot, "assistant")))
        self.assertEqual(opened, [])

    def test_open_it_on_the_emulator_opens_the_page_and_hands_the_sign_in_to_the_owner(self):
        c, phone, bot, app, opened = self.start()
        say(c, bot, "find another way for me to play clash of clans online")
        until(lambda: web_card(c, bot), timeout=30)
        first = web_card(c, bot)
        c.post(f"/api/items/{first['meta']['item_id']}/answer", json={"choice": "EasyFun.gg"})
        until(lambda: web_card(c, bot)["meta"]["item_id"] != first["meta"]["item_id"])
        second = web_card(c, bot)
        c.post(f"/api/items/{second['meta']['item_id']}/answer", json={"choice": "Open it on Cloud phone"})
        until(lambda: opened == ["https://www.easyfun.gg/games/clashofclans"])
        until(lambda: any(m["kind"] == "action_item" and m["meta"]["kind"] == "handoff" for m in c.get(f"/api/bots/{bot}/state").json()["messages"]))
        handoff = [m for m in c.get(f"/api/bots/{bot}/state").json()["messages"] if m["meta"].get("kind") == "handoff"][0]
        self.assertIn("Sign in yourself", handoff["content"])
        say_ = " ".join(texts(c, bot, "assistant"))
        self.assertIn("I won't type, sign in or play on it", say_)
        self.assertIn("publisher bans bot accounts", say_)  # said once: the game guard stops play there
        self.assertEqual((phone.taps, getattr(phone, "typed", [])), ([], []))
        self.assertIn("easyfun.gg", json.loads(self.tmp.read_text()))  # from now on nothing is typed on that site


class NoLogins(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "third.json"
        self.old, gamepolicy.THIRD_PARTY_FILE = gamepolicy.THIRD_PARTY_FILE, self.tmp

    def tearDown(self):
        gamepolicy.THIRD_PARTY_FILE = self.old

    def go(self, els, *actions, front="com.android.chrome", vault=False):
        dev = BlockedPhone(front=front)
        dev.els = els

        class Vault:
            def labels(self):
                return ["supercell id"]

            def reveal(self, *a):
                raise AssertionError("a secret must never be typed on a third-party site")
        res = run("sign in", dev, FakeModel([reply(**a) for a in actions] + [reply(type="done", answer="stopped")]), settle=0,
                  secrets=Vault() if vault else None, handler=lambda p: {"approved": True})
        return dev, res

    def test_nothing_is_typed_on_a_cloud_gaming_homepage_but_reading_and_tapping_are_fine(self):
        els = page("easyfun.gg", "EasyFun.gg", "Log in", "Games", "Racing", "Puzzle", "Cooking", "Sports", "Action", "Cards", "Casual", clickable=("Log in",))
        dev, res = self.go(els, {"type": "type", "text": "me@example.com"}, {"type": "tap", "element": 2})
        self.assertEqual(getattr(dev, "typed", []), [])
        self.assertIn("I never type a login", "\n".join(res.history))
        self.assertEqual(len(dev.taps), 1)  # opening the login page to read it is not typing

    def test_a_saved_secret_is_refused_there_too(self):
        els = page("easyfun.gg", "EasyFun.gg", "Log in", "Games")
        dev, res = self.go(els, {"type_secret": 1, "type": "type_secret", "label": "supercell id", "app": "Chrome"}, vault=True)
        self.assertEqual(getattr(dev, "typed", []), [])
        self.assertEqual(res.status, "done")

    def test_repeated_attempts_end_the_task_with_a_plain_reason(self):
        els = page("easyfun.gg", "EasyFun.gg", "Log in")
        dev, res = self.go(els, *[{"type": "type", "text": "x"}] * 3)
        self.assertEqual(res.status, "denied")
        self.assertTrue(res.answer.startswith(gamepolicy.CREDS_PREFIX))
        self.assertEqual(getattr(dev, "typed", []), [])

    def test_a_picked_site_is_remembered_and_an_ordinary_site_is_not_affected(self):
        els = page("fun-streams.example", "Welcome", "Log in")
        dev, _ = self.go(els, {"type": "type", "text": "hello"})
        self.assertEqual(dev.typed if hasattr(dev, "typed") else [], ["hello"])  # not a known third-party site yet
        gamepolicy.mark_third_party("fun-streams.example")
        dev, _ = self.go(els, {"type": "type", "text": "hello"})
        self.assertEqual(getattr(dev, "typed", []), [])
        dev, _ = self.go(page("accounts.google.com", "Sign in"), {"type": "type", "text": "me"})
        self.assertEqual(dev.typed, ["me"])  # an official site: the normal rules (a secret needs the owner's approval each time)

    def test_a_cloud_site_that_hosts_a_blocked_game_blocks_play_as_well(self):
        els = page("cloudmoon.com/play/clash-of-clans", "Loading")
        dev, res = self.go(els, {"type": "tap", "x": 1, "y": 1}, {"type": "type", "text": "a"})
        self.assertEqual((dev.taps, getattr(dev, "typed", [])), ([], []))
        self.assertIn("blocked to protect your account", "\n".join(res.history))


class ChainLastStep(unittest.TestCase):
    def test_when_no_phone_can_run_the_app_the_last_step_is_an_options_card(self):
        from test_chat import Worker
        from test_escalate import CRASH, PKG
        emulator, games, pixel = Emu("emulator-5554", [PKG]), GamesP("localhost:5565"), Extra("R5", [])
        games.crash_on_open = pixel.crash_on_open = True
        spark = Spark(act(type="mobile_task", goal="open clash of clans", say="On it."), MODEL_OPTIONS)
        c, app, bots = build(spark, Worker(act(type="fail", reason=CRASH)), emulator=emulator, games=games, extras={"pixel": ("R5", pixel)})
        orch = app.state.orch
        orch.web_in_chain, orch.web_provider, orch.web_fetch = True, FakeProvider(RESULTS), fake_fetch(PAGES)
        say(c, assistant(bots), "open clash of clans")
        until(lambda: web_card(c, assistant(bots)), timeout=60)
        card = web_card(c, assistant(bots))
        self.assertIn("None of my phones can run Clash of Clans. Here are other ways, with their risks:", card["content"])
        self.assertIn("UNOFFICIAL", card["content"])
        self.assertTrue(said(c, assistant(bots), "I couldn't get Clash of Clans to run on any of your phones"))  # the honest summary comes first


class Wording(unittest.TestCase):
    def test_the_prompts_describe_the_action_and_its_limits(self):
        root = Path(__file__).resolve().parent.parent
        orch = (root / "prompts" / "orchestrator.md").read_text()
        self.assertIn('"type":"web_options"', orch)
        self.assertIn("official routes first", orch)
        self.assertIn("never act on a third-party service", orch.replace("You never act", "never act"))
        self.assertIn("never type anything", __import__("agent.loop", fromlist=["SYSTEM"]).SYSTEM.replace("never type", "never type"))


if __name__ == "__main__":
    unittest.main()
