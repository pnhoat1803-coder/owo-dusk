"""Browser checks for the dashboard pages and animations (prompt 07, flat since prompt 10): panel classes on every
dashboard view, the stagger entrance (data-lg-enter), the page crossfade (view-enter), the hover lift (liquid.css section
13; the glint sweep of prompt 07 is switched off, only an inert ::after is left), the scroll fade and the toast.

Starts only the dashboard process (website.server, port 18141) with --parent-pid, so the bot is never started, and drives
it with Chromium (Playwright). The password is read from config/global_settings.json in memory only: it is not printed,
not written anywhere, and the config is not changed. Skipped when Playwright or Chromium is not installed.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("DUSK_LG7_PORT", "18142"))
BASE = "http://127.0.0.1:%d" % PORT


def _site_password():
    try:
        with open(ROOT / "config" / "global_settings.json", encoding="utf-8") as handle:
            site = json.load(handle).get("website") or {}
    except (OSError, ValueError):
        return ""
    value = site.get("password")
    return value if isinstance(value, str) else ""


def _port_in_use():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", PORT)) == 0


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory):
    if _port_in_use():
        pytest.skip("port %d is already in use" % PORT)
    log = open(tmp_path_factory.mktemp("lg7") / "dashboard.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "website.server", "--port", str(PORT), "--host", "127.0.0.1",
         "--refresh-interval", "3", "--version", "7-test", "--parent-pid", str(os.getpid())],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)
    ready = False
    end = time.time() + 45
    while time.time() < end and proc.poll() is None:
        try:
            with urllib.request.urlopen(BASE + "/login", timeout=2) as reply:
                ready = reply.status == 200
        except Exception:
            ready = False
        if ready:
            break
        time.sleep(0.4)
    if not ready:
        proc.terminate()
        log.close()
        pytest.fail("the dashboard did not answer on %s" % BASE)
    yield BASE
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch()
        except Exception as exc:  # the browser is not installed
            pytest.skip("Chromium cannot start: %s" % str(exc)[:80])
        yield chromium
        chromium.close()


def _sign_in(page):
    """Opens the dashboard; when a password is set, signs in through the form"""
    page.goto(BASE + "/")
    if "/login" in page.url:
        page.fill("#pw", _site_password())
        page.click("button[type=submit]")
        page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
    page.wait_for_selector("#sidebar")
    page.wait_for_function("window.DuskLiquid !== undefined")


@pytest.fixture
def page(browser, dashboard):
    context = browser.new_context(viewport={"width": 1366, "height": 768})
    page = context.new_page()
    page.on("console", lambda msg: page._lg7_console.append((msg.type, msg.text)))
    page.on("pageerror", lambda err: page._lg7_console.append(("pageerror", str(err))))
    page._lg7_console = []
    _sign_in(page)
    yield page
    context.close()


def _open_layout(page):
    if page.is_hidden("#nav-layout-pop"):
        page.click("#nav-layout")
        page.wait_for_selector("#nav-layout-pop:not([hidden])")


GLASS = ".glass, .glass-lite"


def _go(page, view):
    page.click('#tabs [data-view="%s"]' % view)
    page.wait_for_selector("#view-%s:not([hidden])" % view)
    page.wait_for_timeout(300)


def test_overview_glass_panels_have_entrance(page):
    count = page.locator("#view-overview :is(.glass, .glass-lite)[data-lg-enter]").count()
    assert count >= 2


def test_hover_lift_transition_includes_transform(page):
    value = page.evaluate("getComputedStyle(document.querySelector('#view-overview .ov-hero')).transitionProperty")
    assert "transform" in value


def test_glint_pseudo_element_exists(page):
    content = page.evaluate("getComputedStyle(document.querySelector('#view-overview .ov-hero'), '::after').content")
    assert content != "none"


def test_no_specular_rim_pseudo_element(page):
    # new in prompt 10: the specular rim (::before) of the glass look is gone
    content = page.evaluate("getComputedStyle(document.querySelector('#view-overview .ov-hero'), '::before').content")
    assert content in ("none", "normal")


def test_hover_lift_moves_the_panel_after_the_entrance(page):
    page.wait_for_timeout(1200)   # the entrance (0.5 s plus the delay) is over
    hero = page.locator("#view-overview .ov-hero").first
    assert hero.evaluate("el => getComputedStyle(el).transform") == "none"
    hero.hover()
    page.wait_for_timeout(600)
    assert hero.evaluate("el => getComputedStyle(el).transform") != "none"


def test_navigation_bar_does_not_lift(page):
    page.wait_for_timeout(600)
    page.hover("#sidebar")
    page.wait_for_timeout(600)
    assert page.evaluate("getComputedStyle(document.getElementById('sidebar')).transform") == "none"


def test_accounts_page_has_glass(page):
    _go(page, "accounts")
    assert page.locator("#view-accounts :is(.glass, .glass-lite)").count() >= 1


def test_inspect_page_has_glass(page):
    page.evaluate("location.hash = '#account/phase7-probe'")
    page.wait_for_selector("#view-account:not([hidden])")
    assert page.locator("#view-account .glass").count() >= 1
    assert "lg-fade" in page.get_attribute("#view-account", "class").split()


def test_config_page_has_glass(page):
    _go(page, "config")
    assert page.locator("#view-config :is(.glass, .glass-lite)").count() >= 1


def test_every_tab_page_has_a_glass_panel(page):
    for view in ("events", "network", "logs", "runtime"):
        _go(page, view)
        assert page.locator("#view-%s .glass" % view).count() >= 1, view


def test_logs_page_has_scroll_fade(page):
    _go(page, "logs")
    assert "lg-fade" in page.get_attribute("#console", "class").split()


def test_page_switch_replays_the_entrance(page):
    _go(page, "accounts")
    _go(page, "overview")
    names = page.evaluate("Array.from(document.querySelectorAll('#view-overview [data-lg-enter]')).map(e => getComputedStyle(e).animationName)")
    assert len(names) >= 2
    assert all(name != "none" for name in names)
    # the replay leaves no inline animation behind
    assert page.evaluate("Array.from(document.querySelectorAll('[data-lg-enter]')).every(e => e.style.animation === '')")


def test_solid_mode_disables_glint(page):
    page.evaluate("document.documentElement.setAttribute('data-fx', 'solid')")
    display = page.evaluate("getComputedStyle(document.querySelector('#view-overview .ov-hero'), '::after').display")
    assert display == "none"


def test_page_switch_plays_the_crossfade(page):
    # new in prompt 10: the view that is shown gets .view-enter (lg-fade-in), the outgoing one is hidden at once
    _go(page, "accounts")
    classes = page.get_attribute("#view-accounts", "class").split()
    assert "view-enter" in classes
    assert page.evaluate("getComputedStyle(document.getElementById('view-accounts')).animationName") == "lg-fade-in"
    assert page.is_hidden("#view-overview")
    # the animation leaves no transform behind (a finished transform would trap fixed children): after it, none
    page.wait_for_timeout(700)
    assert page.evaluate("getComputedStyle(document.getElementById('view-accounts')).transform") == "none"


def test_drawer_does_not_use_the_crossfade(page):
    # the account drawer has its own slide, and the list behind it must not flash when it opens or closes
    _go(page, "accounts")
    page.wait_for_timeout(500)
    page.evaluate("document.getElementById('view-accounts').classList.remove('view-enter')")
    page.evaluate("location.hash = '#account/phase7-probe'")
    page.wait_for_selector("#view-account:not([hidden])")
    assert "view-enter" not in page.get_attribute("#view-account", "class").split()
    assert "view-enter" not in page.get_attribute("#view-accounts", "class").split()
    page.evaluate("location.hash = '#accounts'")
    page.wait_for_timeout(300)
    assert "view-enter" not in page.get_attribute("#view-accounts", "class").split()


def test_hover_lift_is_for_cards_and_tiles_only(page):
    page.wait_for_timeout(1200)
    page.hover("#sidebar")
    page.wait_for_timeout(500)
    assert page.evaluate("getComputedStyle(document.querySelector('#view-overview .ov-status')).transform") == "none"
    page.hover("#view-overview .ov-status")
    page.wait_for_timeout(500)
    assert page.evaluate("getComputedStyle(document.querySelector('#view-overview .ov-status')).transform") == "none"


def test_entrance_is_staggered_by_the_attribute_value(page):
    # data-lg-enter="N" in the page becomes --lg-i N: a 30 ms step per value, no longer 60 ms
    delays = page.evaluate("""Array.from(document.querySelectorAll('#view-overview [data-lg-enter]')).map(
      e => [e.getAttribute('data-lg-enter'), getComputedStyle(e).animationDelay])""")
    assert delays
    for value, delay in delays:
        assert delay.endswith("s") and not delay.endswith("ms"), (value, delay)
        assert abs(float(delay[:-1]) - int(value or 0) * 0.03) < 0.005, (value, delay)


def test_phone_toast_uses_the_material(browser, dashboard):
    context = browser.new_context(viewport={"width": 390, "height": 800})
    page = context.new_page()
    page.goto(BASE + "/m")
    if "/login" in page.url:
        page.fill("#pw", _site_password())
        page.click("button[type=submit]")
        page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
        page.goto(BASE + "/m")
    page.wait_for_selector("#m-toast", state="attached")
    classes = page.get_attribute("#m-toast", "class").split()
    assert "lg" in classes and "glass-lite" in classes and "lg-strong" in classes
    assert page.get_attribute("#m-toast", "data-lg-enter") == "0"
    context.close()


def test_no_horizontal_scroll_at_1366(page):
    page.wait_for_timeout(500)
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


def test_no_console_errors(page):
    for view in ("accounts", "events", "network", "logs", "runtime", "config", "overview"):
        _go(page, view)
        if view == "accounts" and page.locator("#cards .card .btn-primary").count():
            page.click("#cards .card .btn-primary >> nth=0")   # a real account: the Inspect sheet
            page.wait_for_selector("#view-account:not([hidden])")
            page.wait_for_timeout(800)
            page.evaluate("location.hash = '#accounts'")
    page.wait_for_timeout(500)
    errors = [m for m in page._lg7_console if m[0] in ("error", "pageerror")]
    assert errors == []
