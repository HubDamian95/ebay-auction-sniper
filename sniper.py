import asyncio
import os
import re
import sys
from datetime import datetime

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from dotenv import load_dotenv

load_dotenv()

ITEM_URL = os.getenv("ITEM_URL", "")
MAX_BID = float(os.getenv("MAX_BID", "120.00"))
SNIPE_WINDOW = int(os.getenv("SNIPE_WINDOW", "8"))  # seconds before end to fire
EBAY_EMAIL = os.getenv("EBAY_EMAIL")
EBAY_PASSWORD = os.getenv("EBAY_PASSWORD")


def parse_time_left(text: str) -> int | None:
    """Convert eBay time text into seconds. Handles '2d 3h', '4h 23m', '12m 34s', 'HH:MM:SS'."""
    text = text.strip()

    # HH:MM:SS or MM:SS live countdown format
    colon = re.search(r"(\d+):(\d{2}):(\d{2})", text)
    if colon:
        return int(colon.group(1)) * 3600 + int(colon.group(2)) * 60 + int(colon.group(3))
    mmss = re.search(r"(\d+):(\d{2})$", text.strip())
    if mmss:
        return int(mmss.group(1)) * 60 + int(mmss.group(2))

    text = text.lower()
    total = 0
    days = re.search(r"(\d+)\s*d", text)
    hours = re.search(r"(\d+)\s*h", text)
    mins = re.search(r"(\d+)\s*m(?!s)", text)
    secs = re.search(r"(\d+)\s*s", text)
    if days:
        total += int(days.group(1)) * 86400
    if hours:
        total += int(hours.group(1)) * 3600
    if mins:
        total += int(mins.group(1)) * 60
    if secs:
        total += int(secs.group(1))
    return total if total > 0 else None


async def get_current_price(page) -> float | None:
    # eBay UK price selectors — adjust if eBay redesigns
    for selector in [
        "#prcIsum",
        '[itemprop="price"]',
        ".x-price-primary span",
        "#vi-price span",
        ".notranslate",
    ]:
        try:
            el = page.locator(selector).first
            if await el.count():
                text = await el.inner_text()
                match = re.search(r"[\d,]+\.?\d*", text.replace(",", ""))
                if match:
                    return float(match.group())
        except Exception:
            continue
    return None


async def get_time_left_seconds(page) -> int | None:
    # Primary: scan page text for "Ends in Xd Xh" / "Ends in Xh Xm" etc.
    try:
        raw = await page.evaluate(r"""
            () => {
                const text = document.body.innerText;
                const m = text.match(/Ends\s+in\s+((?:\d+\s*[dhms]\s*)+|\d+:\d{2}:\d{2}|\d+:\d{2})/i);
                return m ? m[1] : null;
            }
        """)
        if raw:
            seconds = parse_time_left(raw)
            if seconds is not None:
                return seconds
    except Exception:
        pass

    # Fallback: known eBay countdown element IDs/classes
    for selector in [
        "#vi-cdown_btn",
        ".vi-countdown",
        '[class*="countdown"]',
        '[class*="time-left"]',
        "#vi-ends-txt",
        ".timeMs",
    ]:
        try:
            el = page.locator(selector).first
            if await el.count():
                text = await el.inner_text()
                seconds = parse_time_left(text)
                if seconds is not None:
                    return seconds
        except Exception:
            continue

    return None


async def login(page) -> None:
    print("[login] Signing in to eBay UK...")
    await page.goto("https://signin.ebay.co.uk/ws/eBayISAPI.dll?SignIn")
    await page.wait_for_load_state("domcontentloaded")

    await page.fill("#userid", EBAY_EMAIL)
    await page.click("#signin-continue-btn")
    await page.wait_for_timeout(1500)

    await page.fill("#pass", EBAY_PASSWORD)
    await page.click("#sgnBt")

    try:
        await page.wait_for_url("**ebay.co.uk/**", timeout=15000)
    except PlaywrightTimeout:
        pass

    # Handle 2FA / security check / post-login account prompts (authn-register, acctsec, etc.)
    blocked_paths = ["challenge", "verify", "2fa", "otp", "authn-register", "acctsec"]
    if any(x in page.url for x in blocked_paths):
        print(f"[login] Post-login prompt detected — please complete or dismiss it in the browser window (2 min timeout).")
        try:
            await page.wait_for_function(
                "() => !['challenge','verify','2fa','otp','authn-register','acctsec']"
                ".some(x => window.location.href.includes(x))",
                timeout=120_000,
            )
        except PlaywrightTimeout:
            print("[login] Timed out waiting for post-login prompt. Exiting.")
            sys.exit(1)

    print("[login] Signed in successfully.")


async def place_bid(page, amount: float) -> None:
    print(f"[bid] Firing bid of £{amount:.2f} at {datetime.now():%H:%M:%S}...")
    await page.goto(ITEM_URL)
    await page.wait_for_load_state("load", timeout=30_000)
    await page.wait_for_timeout(1000)

    # Find bid input
    bid_input = None
    for selector in ["#MaxBidId", '[name="maxbid"]', 'input[type="text"][id*="bid"]']:
        el = page.locator(selector).first
        if await el.count():
            bid_input = el
            break

    if not bid_input:
        print("[bid] ERROR: Could not find bid input field. Screenshot saved.")
        await page.screenshot(path="bid_error.png")
        return

    await bid_input.fill(str(amount))

    # Click "Place bid"
    for selector in ["#bidBtn_btn", 'a[id*="bid"]', 'button:text("Place bid")', 'a:text("Place bid")']:
        el = page.locator(selector).first
        if await el.count():
            await el.click()
            break

    await page.wait_for_load_state("domcontentloaded")

    # Confirm on review page if present
    for selector in ["#confirmBid_btn", '[data-testid="CONFIRM-BID"]', 'button:text("Confirm bid")']:
        el = page.locator(selector).first
        if await el.count():
            await el.click()
            await page.wait_for_load_state("domcontentloaded")
            print("[bid] Confirmed!")
            break
    else:
        print("[bid] No confirmation page encountered — bid may have been placed directly.")

    await page.screenshot(path="bid_result.png")
    print("[bid] Done. Screenshot saved to bid_result.png")


async def monitor_and_snipe(page) -> None:
    print(f"[sniper] Item : {ITEM_URL}")
    print(f"[sniper] Max  : £{MAX_BID:.2f}")
    print(f"[sniper] Fire : {SNIPE_WINDOW}s before auction end")
    print()

    while True:
        await page.goto(ITEM_URL)
        await page.wait_for_load_state("load", timeout=30_000)
        await page.wait_for_timeout(2000)  # let JS countdown render

        price = await get_current_price(page)

        # Retry up to 3 times if the countdown hasn't rendered yet
        seconds_left = None
        for _ in range(3):
            seconds_left = await get_time_left_seconds(page)
            if seconds_left is not None:
                break
            await page.wait_for_timeout(2000)

        now_str = datetime.now().strftime("%H:%M:%S")

        if price is not None and price >= MAX_BID:
            print(f"[{now_str}] Current price £{price:.2f} >= max £{MAX_BID:.2f}. Stopping — won't win.")
            return

        if seconds_left is None:
            print(f"[{now_str}] Could not read time left after retries. Retrying in 30s...")
            await asyncio.sleep(30)
            continue

        if seconds_left <= 0:
            print(f"[{now_str}] Auction has ended.")
            return

        price_str = f"£{price:.2f}" if price else "unknown"
        print(f"[{now_str}] Price: {price_str} | Time left: {seconds_left}s")

        fire_in = seconds_left - SNIPE_WINDOW

        if fire_in <= 2:
            await place_bid(page, MAX_BID)
            return
        elif fire_in <= 60:
            print(f"[{now_str}] Sniping in {fire_in}s — standing by...")
            await asyncio.sleep(fire_in)
            await place_bid(page, MAX_BID)
            return
        elif fire_in <= 300:
            # Check every 20s when close
            await asyncio.sleep(min(fire_in - 30, 20))
        else:
            # Check every 2 minutes when far away
            await asyncio.sleep(min(fire_in - 120, 120))


async def main() -> None:
    if not EBAY_EMAIL or not EBAY_PASSWORD or not ITEM_URL:
        print("ERROR: Set EBAY_EMAIL, EBAY_PASSWORD, and ITEM_URL in a .env file (see .env.example).")
        sys.exit(1)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,  # keep visible so you can see what's happening + handle prompts
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        await login(page)
        await monitor_and_snipe(page)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
