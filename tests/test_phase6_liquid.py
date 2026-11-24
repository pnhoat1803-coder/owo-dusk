"""Browser checks for the flat look (prompt 10, which replaced the Liquid Glass look of prompt 06): liquid.css, liquid.js,
the WebGL wallpaper, the one fixed data-fx value, the Layout popover without an Effects row, and the login page.

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
PORT = int(os.environ.get("DUSK_LG_PORT", "18141"))
BASE = "http://127.0.0.1:%d" % PORT

# Counts the elements that still have a backdrop filter (hidden ones too: a rule must not be missing just because the
# element is not on screen right now)
JS_BLURRED = """() => Array.from(document.querySelectorAll('*')).filter(el => {
  const cs = getComputedStyle(el);
  const v = cs.backdropFilter || cs.webkitBackdropFilter || 'none';
  return v !== 'none';
}).length"""


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
    log = open(tmp_path_factory.mktemp("lg6") / "dashboard.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "website.server", "--port", str(PORT), "--host", "127.0.0.1",
         "--refresh-interval", "3", "--version", "6-test", "--parent-pid", str(os.getpid())],
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
    page.on("console", lambda msg: page._lg_console.append((msg.type, msg.text)))
    page.on("pageerror", lambda err: page._lg_console.append(("pageerror", str(err))))
    page._lg_console = []
    _sign_in(page)
    yield page
    context.close()


def _open_layout(page):
    if page.is_hidden("#nav-layout-pop"):
        page.click("#nav-layout")
        page.wait_for_selector("#nav-layout-pop:not([hidden])")


def test_wallpaper_is_fixed(page):
    # glass-wallpaper.js swaps the div for a canvas (same class, same fixed position); without WebGL the div stays
    wall = page.locator(".lg-wallpaper")
    assert wall.count() == 1
    assert wall.evaluate("el => getComputedStyle(el).position") == "fixed"


def test_wallpaper_script_is_loaded(page):
    assert page.locator('script[src*="glass-wallpaper.js"]').count() == 1
    assert page.evaluate("typeof window.DuskWallpaper") == "object"
    page.wait_for_timeout(300)
    # a canvas where the browser has WebGL, the flat div where it has not: one of the two, never both, never none
    assert page.locator("canvas.lg-wallpaper--gl, div.lg-wallpaper").count() == 1


def test_nav_has_glass_classes(page):
    classes = page.get_attribute("#sidebar", "class").split()
    assert "lg" in classes and "lg-nav" in classes
    assert page.locator("#tabs .lg-nav-link").count() == 7
    assert page.locator("#tabs [aria-current=page]").count() == 1


def test_liquid_assets_are_loaded(page):
    assert page.locator('link[href*="liquid.css"]').count() == 1
    assert page.evaluate("window.DuskLiquid.version") == 1


def test_effects_attribute_is_always_solid(page):
    # was test_effects_default_is_liquid: there is one look now, so data-fx is fixed at "solid"
    assert page.evaluate("document.documentElement.getAttribute('data-fx')") == "solid"


def test_layout_popover_has_no_effects_row(page):
    # was test_effects_toggle_changes_data_fx: the Liquid / Frosted / Solid selector is gone, and nothing writes dusk.fx
    _open_layout(page)
    assert page.locator("[data-fx-pick]").count() == 0
    assert page.locator("#nav-fx-label").count() == 0
    assert page.evaluate("typeof window.duskNav.setFx") == "undefined"
    for pick in ("light", "dark"):
        page.click('[data-theme-pick="%s"]' % pick)
    assert page.evaluate("document.documentElement.getAttribute('data-fx')") == "solid"
    assert page.evaluate("localStorage.getItem('dusk.fx')") is None


def test_old_saved_effects_value_is_ignored(browser, dashboard):
    # was test_old_saved_value_glass_means_frosted: whatever an older version stored, the page stays solid
    for stored in ("glass", "liquid", "frosted"):
        context = browser.new_context(viewport={"width": 1366, "height": 768})
        context.add_init_script("try { localStorage.setItem('dusk.fx', '%s'); } catch (e) {}" % stored)
        page = context.new_page()
        _sign_in(page)
        assert page.evaluate("document.documentElement.getAttribute('data-fx')") == "solid", stored
        assert page.evaluate("window.duskNav.read().fx") == "solid"
        context.close()


def test_edge_lens_is_off(page):
    # was test_edge_lens_is_active_in_liquid_mode: the lens code is still in liquid.js, but nothing starts it
    page.wait_for_timeout(600)
    assert page.evaluate("window.DuskLiquid.lensActive()") is False
    assert page.evaluate("window.DuskLiquid.lensCount()") == 0
    page.evaluate("window.DuskLiquid.refresh()")
    assert page.evaluate("window.DuskLiquid.lensCount()") == 0
    assert page.locator("#lg-defs").count() == 0                      # no SVG filter container was built
    assert page.locator("[data-lg-lens]").count() == 0
    assert page.evaluate("document.querySelectorAll('.lg-lens').length") >= 1   # the class is still in the page, inert


def test_no_element_has_a_backdrop_filter(page):
    # was test_solid_mode_has_no_blur (it clicked Solid and looked at the sidebar): now every element, all the time
    value = page.evaluate("getComputedStyle(document.getElementById('sidebar')).backdropFilter")
    assert value in ("", "none")
    assert page.evaluate(JS_BLURRED) == 0
    _open_layout(page)
    assert page.evaluate(JS_BLURRED) == 0                              # the open popover as well


def test_panels_are_solid_with_no_rim(page):
    css = page.evaluate("""() => {
      const el = document.querySelector('#view-overview .ov-hero');
      const cs = getComputedStyle(el), after = getComputedStyle(el, '::before');
      return { bg: cs.backgroundColor, image: cs.backgroundImage, rim: after.content, shadow: cs.boxShadow };
    }""")
    assert css["bg"].startswith("rgb(")        # an opaque colour: the computed value would be rgba(...) with an alpha below 1
    assert css["image"] == "none"
    assert css["rim"] in ("none", "normal")
    assert "inset" not in css["shadow"]


def test_theme_switch_fades_for_about_400_ms(page):
    # a small page (the Overview of a dashboard with no accounts): the colour fade, and the theme is on the page at once
    assert page.evaluate(DRAWN) <= 600
    page.evaluate("document.documentElement.removeAttribute('data-theme-transition')")
    _open_layout(page)
    start = page.evaluate("document.documentElement.getAttribute('data-theme')")
    page.click('[data-theme-pick="%s"]' % ("light" if start == "dark" else "dark"))
    assert page.evaluate("document.documentElement.hasAttribute('data-theme-transition')") is True
    page.wait_for_timeout(700)
    assert page.evaluate("document.documentElement.hasAttribute('data-theme-transition')") is False
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") != start


# 800 extra elements inside the view that is shown (nav-boot.js counts the elements of the shown views)
BLOAT = """() => { const box = document.createElement('div'); box.id = 'lg-bloat';
  box.style.cssText = 'position:absolute;left:-9999px;top:0;width:1px';
  for (let i = 0; i < 800; i++) box.appendChild(document.createElement('i'));
  (document.querySelector('.view:not([hidden])') || document.body).appendChild(box); }"""
DRAWN = "() => Array.from(document.body.getElementsByTagName('*')).filter(e => e.getClientRects().length).length"


def _wait_until(page, js, seconds=3.0):
    """Polls an arrow function until it returns something truthy. (wait_for_function with a string needs eval, which the
    CSP of this page forbids, so the test polls with evaluate)"""
    end = time.time() + seconds
    while time.time() < end:
        if page.evaluate(js):
            return True
        page.wait_for_timeout(50)
    return False


def test_big_page_crossfades_a_snapshot_instead_of_fading_every_element(page):
    # The colour fade animates each drawn element: fine for the Overview, a freeze of most of a second for the account
    # list. Above 600 drawn elements nav-boot.js uses a View Transition, which costs the same on any page size.
    if page.evaluate("typeof document.startViewTransition") != "function":
        pytest.skip("this Chromium has no View Transitions")
    page.evaluate(BLOAT)
    assert page.evaluate(DRAWN) > 600
    page.evaluate("() => { window.__tc = 0; window.addEventListener('dusk:themechange', () => { window.__tc++; }); }")
    start = page.evaluate("document.documentElement.getAttribute('data-theme')")
    want = "light" if start == "dark" else "dark"
    _open_layout(page)
    page.click('[data-theme-pick="%s"]' % want)
    assert page.evaluate("document.documentElement.hasAttribute('data-theme-transition')") is False   # no colour fade
    assert _wait_until(page, "() => document.documentElement.getAttribute('data-theme') === '%s'" % want)
    assert _wait_until(page, "() => window.__tc >= 1")                                               # the charts are told, after the update
    assert _wait_until(page, "() => !document.documentElement.matches(':active-view-transition')")


def test_big_page_without_view_transitions_switches_at_once(page):
    page.evaluate(BLOAT)
    page.evaluate("() => { document.startViewTransition = undefined; }")
    start = page.evaluate("document.documentElement.getAttribute('data-theme')")
    want = "light" if start == "dark" else "dark"
    _open_layout(page)
    page.click('[data-theme-pick="%s"]' % want)
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == want            # at once
    assert page.evaluate("document.documentElement.hasAttribute('data-theme-transition')") is False


def test_reduced_motion_theme_switch_has_no_fade(browser, dashboard):
    context = browser.new_context(viewport={"width": 1366, "height": 768}, reduced_motion="reduce")
    page = context.new_page()
    _sign_in(page)
    page.evaluate(BLOAT)                                                                            # big page: would use a View Transition
    page.evaluate("() => { window.__vt = 0; const real = document.startViewTransition; "
                  "document.startViewTransition = function () { window.__vt++; return real.apply(document, arguments); }; }")
    start = page.evaluate("document.documentElement.getAttribute('data-theme')")
    want = "light" if start == "dark" else "dark"
    _open_layout(page)
    page.click('[data-theme-pick="%s"]' % want)
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == want            # at once
    assert page.evaluate("window.__vt") == 0
    assert page.evaluate("document.documentElement.hasAttribute('data-theme-transition')") is False
    context.close()


def test_navigation_has_only_the_layout_button(page):
    assert page.locator("#nav-layout").count() == 1
    assert page.locator("#quick-theme, #quick-lang").count() == 0
    # the popover still holds the theme and the language choices
    assert page.locator("#nav-layout-pop [data-theme-pick]").count() == 3
    assert page.locator("#nav-layout-pop [data-lang-pick]").count() == 2


def test_navigation_scrollbar_is_hidden(page):
    assert page.evaluate("getComputedStyle(document.getElementById('sidebar')).scrollbarWidth") == "none"


def test_reduced_motion_switches_the_animations_off(browser, dashboard):
    context = browser.new_context(viewport={"width": 1366, "height": 768}, reduced_motion="reduce")
    page = context.new_page()
    _sign_in(page)
    names = page.evaluate("Array.from(document.querySelectorAll('[data-lg-enter]')).map(e => getComputedStyle(e).animationName)")
    assert names and all(name == "none" for name in names)
    # the wallpaper stands still, so the page shows its flat colour (no frozen black canvas)
    assert page.evaluate("Array.from(document.querySelectorAll('canvas.lg-wallpaper--gl')).every(c => getComputedStyle(c).display === 'none')")
    context.close()


def test_login_page_renders(browser, dashboard):
    if not _site_password():
        pytest.skip("no dashboard password is set: /login redirects to the dashboard")
    context = browser.new_context(viewport={"width": 1366, "height": 768})
    page = context.new_page()
    errors = []
    page.on("console", lambda msg: errors.append((msg.type, msg.text)))
    page.goto(BASE + "/login")
    assert page.locator(".lg-wallpaper").count() == 1
    assert page.locator('script[src*="glass-wallpaper.js"]').count() == 1
    assert page.locator("input.lg-field").count() >= 1
    assert "lg-btn--primary" in page.get_attribute("button[type=submit]", "class").split()
    assert "lg" in page.get_attribute("main.login", "class").split()
    assert [m for m in errors if m[0] == "error"] == []
    context.close()


def test_phone_page_has_the_wallpaper_and_no_effects_buttons(browser, dashboard):
    context = browser.new_context(viewport={"width": 390, "height": 800})
    page = context.new_page()
    page.goto(BASE + "/m")
    if "/login" in page.url:
        page.fill("#pw", _site_password())
        page.click("button[type=submit]")
        page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
        page.goto(BASE + "/m")
    page.wait_for_selector("#m-toast", state="attached")
    assert page.locator('script[src*="glass-wallpaper.js"]').count() == 1
    assert page.locator(".lg-wallpaper").count() == 1
    assert page.locator("[data-fx-pick]").count() == 0
    assert page.evaluate("document.documentElement.getAttribute('data-fx')") == "solid"
    assert page.evaluate(JS_BLURRED) == 0
    # the theme and language buttons of the More tab are still there
    assert page.locator("#v-more [data-theme-pick]").count() == 3
    assert page.locator("#v-more [data-lang-pick]").count() == 2
    context.close()


def test_no_console_errors(page):
    page.wait_for_timeout(1500)
    _open_layout(page)
    for pick in ("light", "dark", "auto"):
        page.click('[data-theme-pick="%s"]' % pick)
    page.wait_for_timeout(500)
    errors = [m for m in page._lg_console if m[0] in ("error", "pageerror")]
    assert errors == []


def test_no_horizontal_scroll_at_1366(page):
    page.wait_for_timeout(500)
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
