"""MaizeTix price monitor: Indiana vs Michigan football.

Every INTERVAL_MIN minutes, opens maizetix.com/games in a headless browser,
opens the Indiana game's listings, finds the cheapest displayed price, and
pushes it to an ntfy topic. Runs until STOP_AT (UTC ISO time) or until
MAX_RUNTIME_MIN elapses (GitHub Actions jobs are capped at 6 hours).
"""

import json
import os
import re
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

GAMES_URL = "https://www.maizetix.com/games"
OPPONENT = os.environ.get("OPPONENT", "Indiana")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "preston-maizetix-iu-x7k2")
INTERVAL_MIN = int(os.environ.get("INTERVAL_MIN", "10"))
STOP_AT = datetime.fromisoformat(os.environ.get("STOP_AT", "2026-10-24T11:35:00+00:00"))
MAX_RUNTIME_MIN = int(os.environ.get("MAX_RUNTIME_MIN", "345"))
DEBUG_DIR = Path("debug")
ET = ZoneInfo("America/Detroit")

# Ignore dollar amounts outside a plausible ticket range (fee text like "$3",
# marketing totals like "$2,038,000").
MIN_PRICE, MAX_PRICE = 5.0, 2000.0
PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?)")


def now_et() -> str:
    return datetime.now(ET).strftime("%-I:%M %p ET")


def notify(title: str, body: str, tags: str = "ticket", priority: str = "default") -> None:
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        method="POST",
        headers={
            "Title": title.encode("utf-8").decode("latin-1", "ignore"),
            "Tags": tags,
            "Priority": priority,
            "Click": GAMES_URL,
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(f"ntfy -> {resp.status}: {title} | {body}")


def prices_in(text: str) -> list[float]:
    out = []
    for m in PRICE_RE.finditer(text):
        v = float(m.group(1).replace(",", ""))
        if MIN_PRICE <= v <= MAX_PRICE:
            out.append(v)
    return out


def json_prices(obj, found: list[float]) -> None:
    """Walk JSON looking for numeric fields whose key mentions price."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if re.search(r"price|amount|cost", str(k), re.I) and isinstance(v, (int, float, str)):
                try:
                    f = float(str(v).replace("$", "").replace(",", ""))
                    # Some APIs use cents.
                    if f > MAX_PRICE and f / 100 <= MAX_PRICE:
                        f /= 100
                    if MIN_PRICE <= f <= MAX_PRICE:
                        found.append(f)
                except ValueError:
                    pass
            else:
                json_prices(v, found)
    elif isinstance(obj, list):
        for v in obj:
            json_prices(v, found)


OPEN_GAME_JS = """
(opp) => {
  // Find the smallest element that contains both the opponent name and "See Tickets".
  const all = Array.from(document.querySelectorAll('body *'));
  const rows = all.filter(el => {
    const t = el.innerText || '';
    return t.includes(opp) && /see tickets/i.test(t);
  });
  if (!rows.length) return {ok: false, reason: 'no row with opponent + See Tickets'};
  rows.sort((a, b) => a.innerText.length - b.innerText.length);
  const row = rows[0];
  const btn = Array.from(row.querySelectorAll('a,button,[role=button],span,div'))
    .filter(el => /see tickets/i.test(el.innerText || ''))
    .sort((a, b) => a.innerText.length - b.innerText.length)[0] || row;
  const link = btn.closest('a') || row.querySelector('a[href]') || row.closest('a');
  const href = link && link.href && !link.href.endsWith('#') ? link.href : null;
  btn.scrollIntoView();
  btn.click();
  return {ok: true, href, rowText: row.innerText.slice(0, 200)};
}
"""

LISTING_TEXT_JS = """
() => {
  // Prefer a visible modal/dialog if the listings open in one.
  const cands = Array.from(document.querySelectorAll(
    '[role=dialog], .modal.show, .modal[style*="display: block"], dialog[open], .offcanvas.show'));
  const vis = cands.filter(el => el.offsetParent !== null || getComputedStyle(el).position === 'fixed');
  if (vis.length) return {scope: 'modal', text: vis.map(e => e.innerText).join('\\n')};
  return {scope: 'page', text: document.body.innerText};
}
"""


def check_once(browser, iteration: int) -> dict:
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    )
    page = ctx.new_page()
    captured: list = []
    clicked = {"done": False}

    def on_response(resp):
        if not clicked["done"]:
            return
        if "json" in (resp.headers.get("content-type") or ""):
            try:
                captured.append({"url": resp.url, "body": resp.json()})
            except Exception:
                pass

    page.on("response", on_response)
    try:
        page.goto(GAMES_URL, wait_until="networkidle", timeout=60_000)
        page.wait_for_function(
            "() => /see tickets/i.test(document.body.innerText)", timeout=30_000)

        clicked["done"] = True
        res = page.evaluate(OPEN_GAME_JS, OPPONENT)
        if not res.get("ok"):
            raise RuntimeError(res.get("reason"))
        if res.get("href") and page.url.rstrip("/") == GAMES_URL:
            # Click may not navigate (JS handler); follow the link explicitly.
            try:
                page.wait_for_url(lambda u: u.rstrip("/") != GAMES_URL, timeout=5_000)
            except Exception:
                page.goto(res["href"], wait_until="networkidle", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:
            pass
        page.wait_for_timeout(3_000)

        lt = page.evaluate(LISTING_TEXT_JS)
        text_prices = prices_in(lt["text"])
        api_prices: list[float] = []
        for c in captured:
            json_prices(c["body"], api_prices)

        info = {
            "url": page.url,
            "scope": lt["scope"],
            "row": res.get("rowText"),
            "text_prices": sorted(text_prices)[:20],
            "text_count": len(text_prices),
            "api_prices": sorted(api_prices)[:20],
            "api_urls": [c["url"] for c in captured][:20],
        }
        print(json.dumps(info, indent=1))

        if iteration == 0:
            DEBUG_DIR.mkdir(exist_ok=True)
            (DEBUG_DIR / "listing_text.txt").write_text(lt["text"])
            (DEBUG_DIR / "info.json").write_text(json.dumps(info, indent=1))
            (DEBUG_DIR / "api.json").write_text(json.dumps(captured, indent=1)[:2_000_000])
            page.screenshot(path=str(DEBUG_DIR / "listing.png"), full_page=True)
        return info
    except Exception:
        DEBUG_DIR.mkdir(exist_ok=True)
        try:
            page.screenshot(path=str(DEBUG_DIR / f"error_{iteration}.png"), full_page=True)
            (DEBUG_DIR / f"error_{iteration}.txt").write_text(page.evaluate("document.body.innerText"))
        except Exception:
            pass
        raise
    finally:
        ctx.close()


def main() -> int:
    started = time.time()
    deadline = min(STOP_AT.timestamp(), started + MAX_RUNTIME_MIN * 60)
    last_price = None
    fails = 0
    i = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        while True:
            tick = time.time()
            if tick >= STOP_AT.timestamp():
                notify("Monitor finished", f"12-hour window ended at {now_et()}.", tags="checkered_flag")
                return 0
            try:
                info = check_once(browser, i)
                prices = info["text_prices"] or info["api_prices"]
                if not prices:
                    raise RuntimeError("no prices found on listings page")
                cheapest = min(prices)
                change = ""
                if last_price is not None and cheapest != last_price:
                    d = cheapest - last_price
                    change = f" ({'down' if d < 0 else 'up'} ${abs(d):.0f})"
                notify(
                    f"IU @ Michigan: ${cheapest:.0f}{change}",
                    f"Cheapest MaizeTix ticket: ${cheapest:.2f} (fees incl.) as of {now_et()}.",
                    tags="football,chart_with_downwards_trend" if change.startswith(" (down") else "football",
                )
                last_price = cheapest
                fails = 0
            except Exception as e:
                traceback.print_exc()
                fails += 1
                if fails in (1, 3) or fails % 6 == 0:
                    notify("MaizeTix monitor: check failed",
                           f"{type(e).__name__}: {str(e)[:200]} ({now_et()}, failure #{fails})",
                           tags="warning")
            i += 1
            next_tick = tick + INTERVAL_MIN * 60
            if next_tick >= deadline:
                break
            time.sleep(max(0, next_tick - time.time()))
        browser.close()
    if deadline >= STOP_AT.timestamp():
        notify("Monitor finished", f"12-hour window ended ({now_et()}). No more updates.",
               tags="checkered_flag")
    return 0


if __name__ == "__main__":
    sys.exit(main())
