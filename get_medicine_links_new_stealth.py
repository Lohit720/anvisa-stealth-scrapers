"""
ANVISA Medicine Scraper — Robust Rewrite
=========================================
Strategy:
  Phase 1 — Harvest all detail URLs from the listing pages WITHOUT ever
             navigating away from the listing.  We read the Angular scope
             data (scope.produto) directly via JS, build the detail URL
             ourselves, and only use pagination clicks between pages.
             No per-row navigation → the "50-per-page" state is never lost.

  Filter   — Two-layer Processo Matriz exclusion:
               Layer 1: Angular scope check in the listing rows (fast,
                        best-effort — used only for logging/visibility).
               Layer 2: DOM check on EVERY detail page. This is the
                        authoritative filter — every URL is opened once,
                        and on that same page load we both extract the
                        product name AND check for Processo Matriz.

  Phase 2 — For each collected URL open a NEW tab, extract "Nome do Produto",
             skip if Processo Matriz is detected, close the tab.
             The listing tab is never touched again.

NOTE: Earlier version short-circuited Phase 2 for any link whose name
was already known from the listing scope ("has_name" bucket), which
meant Layer-2 never ran on those links at all (0 excluded, always).
This version removes that shortcut: every link is opened and checked,
since that's the only reliable place Processo Matriz shows up.
"""

from playwright.sync_api import sync_playwright
import time, os, xlsxwriter
import pandas as pd
import os

# =====================================================================================================
# STEALTH CHROME  (SeleniumBase "Stealthy Playwright Mode")  +  CLOUDFLARE CHALLENGE HELPER
# -----------------------------------------------------------------------------------------------------
# pip install seleniumbase playwright        (no "playwright install" needed - your real Chrome is used)
# This block is the ONLY addition to the script (plus the launch line and handle_challenge(...) calls
# right after page loads).  Playwright now attaches to your real, installed Chrome via connect_over_cdp().
#
# Cloudflare: handle_challenge(page) checks for a Cloudflare / Turnstile screen after a page load.
#   1) it first gives the challenge a few seconds to clear by itself,
#   2) then tries SeleniumBase's solve_captcha() (clicks the Turnstile checkbox),
#   3) if it is still there it WAITS for YOU: click the checkbox in the Chrome window and the script
#      continues automatically as soon as the challenge disappears (no Enter key needed).
# =====================================================================================================
from seleniumbase import sb_cdp

try:
    _STEALTH_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _STEALTH_BASE_DIR = os.getcwd()

STEALTH_CHROME_OPTIONS = {
    # Persistent Chrome profile: keeps the Cloudflare "cf_clearance" cookie between launches (and between
    # your scripts if they sit in the same folder). Delete the folder to reset.
    "user_data_dir": os.path.join(_STEALTH_BASE_DIR, "stealth_chrome_profile"),
    # If Chrome is not found automatically, uncomment and set the path:
    # "binary_location": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    # "proxy": "user:pass@host:port",
    # "locale": "pt-BR",
}

CF_GRACE_SECONDS = 6            # let the challenge clear on its own before touching it
CF_AUTO_SOLVE_TRIES = 2         # how many times to try solve_captcha() automatically
CF_MANUAL_WAIT_SECONDS = 300    # how long to wait for you to click it yourself

_ACTIVE_STEALTH = {}

_CF_DETECT_JS = """() => {
  const title = (document.title || '').toLowerCase();
  const body = ((document.body && document.body.innerText) || '').slice(0, 3000).toLowerCase();
  const sels = ['#challenge-form', '#cf-challenge-running', '#challenge-running', '#challenge-stage',
                '#cf-wrapper', '#turnstile-wrapper', '.cf-turnstile',
                'iframe[src*="challenges.cloudflare.com"]'];
  if (sels.some(s => document.querySelector(s))) return true;
  if (['just a moment', 'attention required', 'um momento', 'verificação de segurança',
       'security check'].some(k => title.includes(k))) return true;
  return ['verify you are human', 'verifying you are human', 'checking your browser',
          'verificando se você é humano', 'verifique se você é humano',
          'needs to review the security of your connection'].some(k => body.includes(k));
}"""


def _stealth_launch_kwargs(headless, extra):
    opts = dict(STEALTH_CHROME_OPTIONS)
    opts.update({k: v for k, v in (extra or {}).items() if v is not None})
    if "binary_location" in opts:                       # friendly aliases -> SeleniumBase names
        opts["browser_executable_path"] = opts.pop("binary_location")
    if "locale" in opts:
        opts["lang"] = opts.pop("locale")
    if headless:
        opts["headless"] = True
    return opts


def _same_page(url_a, url_b):
    return (url_a or "").split("#")[0].rstrip("/") == (url_b or "").split("#")[0].rstrip("/")


def _wait_chrome_exit(user_data_dir, timeout=10):
    """After quitting, wait until no Chrome process still holds the profile folder, so the next launch
    (which reuses the same profile) never collides with a Chrome that is still shutting down."""
    if not user_data_dir:
        return
    try:
        import psutil
    except ImportError:
        time.sleep(2)
        return
    key = os.path.abspath(user_data_dir).lower()
    end = time.time() + timeout
    while time.time() < end:
        alive = False
        for proc in psutil.process_iter(["cmdline"]):
            try:
                cmd = " ".join(proc.info["cmdline"] or []).lower()
                if "--user-data-dir" in cmd and key in cmd:
                    alive = True
                    break
            except Exception:
                continue
        if not alive:
            return
        time.sleep(0.3)


class StealthChromeSync:
    """Sync format: real Chrome via sb_cdp + Playwright sync API (connect_over_cdp).
    Use:  with StealthChromeSync(slow_mo=30) as browser:  page = browser.new_page() ... browser.close()"""

    def __init__(self, headless=False, slow_mo=None, **launch_options):
        self._closed = False
        self._claimed_default = False
        self._free_pages = []
        self.sb = None
        self.playwright = None
        self.browser = None
        launch_kwargs = _stealth_launch_kwargs(headless, launch_options)
        self._user_data_dir = launch_kwargs.get("user_data_dir")
        try:
            self.sb = sb_cdp.Chrome(**launch_kwargs)
            self.endpoint_url = self.sb.get_endpoint_url()
            self.playwright = sync_playwright().start()
            kw = {"slow_mo": slow_mo} if slow_mo else {}
            self.browser = self.playwright.chromium.connect_over_cdp(self.endpoint_url, **kw)
            self.context = self.browser.contexts[0]                 # default context keeps the stealth setup
            self.default_page = self.context.pages[0] if self.context.pages else self.context.new_page()
            _ACTIVE_STEALTH["browser"] = self
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def new_page(self):
        """Reuses the first / any blanked tab, otherwise opens a new tab in the same (stealth) context."""
        if self._free_pages:
            return self._free_pages.pop()
        if not self._claimed_default:
            self._claimed_default = True
            return self.default_page
        return self.context.new_page()

    def close_page(self, page):
        """Closing the LAST tab can make real Chrome exit, so the last tab is blanked and reused instead."""
        try:
            if len(self.context.pages) <= 1:
                page.goto("about:blank")
                if page not in self._free_pages:
                    self._free_pages.append(page)
            else:
                page.close()
        except Exception:
            pass

    # ------------------------------ Cloudflare ------------------------------
    def _challenge_present(self, page):
        try:
            return bool(page.evaluate(_CF_DETECT_JS))
        except Exception:
            return False

    def _try_solve(self, page):
        try:
            for tab in self.sb.get_tabs():
                if _same_page(getattr(tab, "url", ""), page.url):
                    self.sb.switch_to_tab(tab)
                    break
        except Exception:
            pass
        self.sb.solve_captcha()

    def handle_challenge(self, page, label=""):
        """Returns True if a challenge was seen (and cleared), False if none / not cleared."""
        if not self._challenge_present(page):
            return False
        print(f"🛡️ Cloudflare challenge detected {label}- trying to clear it ...")
        page.wait_for_timeout(CF_GRACE_SECONDS * 1000)
        cleared = not self._challenge_present(page)
        tries = 0
        while not cleared and tries < CF_AUTO_SOLVE_TRIES:
            tries += 1
            try:
                self._try_solve(page)
            except Exception as e:
                print(f"   solve_captcha attempt {tries} failed: {e}")
            page.wait_for_timeout(4000)
            cleared = not self._challenge_present(page)
        if not cleared:
            print("\a👉 Please click the Cloudflare checkbox in the Chrome window. "
                  f"Waiting up to {CF_MANUAL_WAIT_SECONDS}s - the script continues by itself once it clears.")
            waited = 0
            while waited < CF_MANUAL_WAIT_SECONDS:
                page.wait_for_timeout(2000)
                waited += 2
                if not self._challenge_present(page):
                    cleared = True
                    break
        if cleared:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
            print("✅ Cloudflare challenge cleared.")
            return True
        print("⚠️ Cloudflare challenge still present - continuing anyway.")
        return False

    # -------------------------------- cleanup --------------------------------
    def close(self):
        """Disconnect Playwright, then quit Chrome. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        if _ACTIVE_STEALTH.get("browser") is self:
            _ACTIVE_STEALTH.pop("browser", None)
        for step in (lambda: self.browser.close(),
                     lambda: self.playwright.stop(),
                     lambda: self.sb.quit()):
            try:
                step()
            except Exception:
                pass
        _wait_chrome_exit(self._user_data_dir)


def handle_challenge(page, label=""):
    """Module-level shortcut: check the page for a Cloudflare challenge using the active stealth browser."""
    b = _ACTIVE_STEALTH.get("browser")
    return b.handle_challenge(page, label) if b else False

# =====================================================================================================


def _soft_idle(page, timeout=10000):
    """ANVISA keeps making background requests, so 'networkidle' is never reached. A timeout here is NOT an
    error - the callers wait for the real page content instead."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass


def _front(page):
    """New tabs open in the background in real Chrome and get throttled; bring the tab to the front."""
    try:
        page.bring_to_front()
    except Exception:
        pass


def _dump_debug(page, url, attempt):
    """When a name lookup fails: save a screenshot + HTML to ./debug_detail and print what the page really
    shows (title, URL, table headers, text), so the cause is visible instead of a silent empty name."""
    try:
        import re
        os.makedirs("debug_detail", exist_ok=True)
        code = re.sub(r"[^0-9A-Za-z]+", "_", url.split("#/medicamentos/")[-1].split("?")[0])[:40] or "page"
        base = os.path.join("debug_detail", f"{code}_a{attempt}")
        try:
            page.screenshot(path=base + ".png")
        except Exception:
            pass
        try:
            with open(base + ".html", "w", encoding="utf-8") as fh:
                fh.write(page.content())
        except Exception:
            pass
        ths = page.evaluate("() => Array.from(document.querySelectorAll('th')).slice(0, 15).map(t => t.textContent.trim())")
        body = page.evaluate("() => ((document.body && document.body.innerText) || '').slice(0, 250)")
        print(f"      [debug] title={page.title()!r} url={page.url} th={ths} text={body!r}")
        print(f"      [debug] saved {base}.png / .html")
    except Exception as dbg_exc:
        print(f"      [debug] could not capture page: {dbg_exc}")


# ── URLs & selectors ────────────────────────────────────────────────────────
START_URL  = (
    "https://consultas.anvisa.gov.br/#/medicamentos/q/"
    "?cnpj=03978166000175&situacaoRegistro=V"
)
DETAIL_BASE   = "https://consultas.anvisa.gov.br/#/medicamentos/"
DETAIL_PARAMS = "?cnpj=03978166000175&situacaoRegistro=V"

PER_PAGE_50      = '.ng-table-counts button:has-text("50")'
ROW_SELECTOR     = "tbody tr[ng-repeat], tbody tr.ng-scope"
ACTIVE_PAGE_SEL  = "ul.pagination li.active a, ul.pagination li.active span"
NEXT_BTN_SEL     = 'ul.pagination a:has-text("»")'

OUT_EXCEL  = "matrix_links_final_new.xlsx"
MAX_PAGES  = 200

# ── Timing ──────────────────────────────────────────────────────────────────
SHORT_WAIT     = 0.8
DETAIL_TIMEOUT = 30_000
RETRY_ATTEMPTS = 3


# ════════════════════════════════════════════════════════════════════════════
# JS helpers
# ════════════════════════════════════════════════════════════════════════════

EXTRACT_ROW_DATA_JS = """() => {
    const rows = document.querySelectorAll(
        'tbody tr[ng-repeat], tbody tr.ng-scope'
    );
    const results = [];
    rows.forEach(function(row) {
        try {
            const el    = window.angular.element(row);
            const scope = el.scope() || (el.isolateScope && el.isolateScope());
            const outer = scope && scope.produto;
            const inner = outer && outer.produto;
            if (inner && inner.codigo) {
                // Layer-1 is best-effort only now (logging) — Layer-2 on the
                // detail page is what actually decides inclusion/exclusion.
                const pm = inner.processoMatriz;
                const flaggedByScope = !!(pm && pm.nomeProduto);
                results.push({
                    codigo: inner.codigo,
                    nome:   inner.nome || "",
                    flaggedByScope: flaggedByScope
                });
            }
        } catch(e) {}
    });
    return results;
}"""

# JS run on each detail page to check for Processo Matriz presence.
# Returns true if the element exists (→ should be excluded).
DETAIL_HAS_PROCESSO_MATRIZ_JS = """() => {
    // The th is rendered by ng-if="produto.processoMatriz.nomeProduto"
    // When present it carries the translate="" attribute as well.
    const ths = document.querySelectorAll('th.ng-scope');
    for (const th of ths) {
        if (th.textContent.trim() === 'Processo Matriz') return true;
    }
    return false;
}"""


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def idle_wait(page, extra=SHORT_WAIT):
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:
        pass
    time.sleep(extra)


def ensure_50_rows(page):
    for attempt in range(3):
        try:
            page.wait_for_selector(PER_PAGE_50, timeout=6_000)
            page.click(PER_PAGE_50)
            _soft_idle(page, 10_000)
            time.sleep(1.5)
            rows = page.query_selector_all(ROW_SELECTOR)
            if len(rows) > 10:
                print(f"  [50-rows] confirmed {len(rows)} rows on attempt {attempt+1}")
                return True
            print(f"  [50-rows] still {len(rows)} rows, retrying…")
        except Exception as exc:
            print(f"  [50-rows] attempt {attempt+1} failed: {exc}")
        time.sleep(1)
    return False


def get_active_page(page):
    try:
        el = page.query_selector(ACTIVE_PAGE_SEL)
        return int(el.text_content().strip()) if el else None
    except Exception:
        return None


def build_detail_url(row: dict) -> str:
    codigo = row.get("codigo")
    if not codigo:
        return ""
    return f"{DETAIL_BASE}{codigo}{DETAIL_PARAMS}"


def go_to_next_page(page, current: int) -> int:
    numbered = f'ul.pagination a:has-text("{current + 1}")'
    try:
        page.click(numbered, timeout=3_000)
        idle_wait(page)
        new = get_active_page(page)
        if new and new > current:
            return new
    except Exception:
        pass
    try:
        page.click(NEXT_BTN_SEL, timeout=3_000)
        idle_wait(page)
        new = get_active_page(page)
        if new and new > current:
            return new
    except Exception:
        pass
    return -1


# ════════════════════════════════════════════════════════════════════════════
# Phase 1 — harvest all detail URLs
# ════════════════════════════════════════════════════════════════════════════

def harvest_links(page) -> list[tuple[str, str]]:
    """
    Walk every listing page, read Angular scope data, build detail URLs.
    Layer-1 here is best-effort/logging only — it does NOT exclude
    anything anymore. Every URL collected here still gets opened in
    Phase 2 and decided there by Layer-2 (the only reliable check).
    Returns deduplicated list of (url, nome).
    """
    seen   = set()
    links  = []

    print("Opening listing…")
    page.goto(START_URL)
    handle_challenge(page)
    idle_wait(page)

    if not ensure_50_rows(page):
        print("WARNING: Could not switch to 50 rows — proceeding with default.")

    current_pg = get_active_page(page) or 1

    for _safety in range(MAX_PAGES):
        idle_wait(page, extra=0.5)
        rows_data = page.evaluate(EXTRACT_ROW_DATA_JS)
        flagged_by_scope = sum(1 for r in rows_data if r.get("flaggedByScope"))
        print(f"  Page {current_pg}: {len(rows_data)} rows read "
              f"({flagged_by_scope} flagged by Layer-1 scope — informational only, "
              f"final decision happens in Phase 2 / Layer-2)")

        if not rows_data:
            # Angular scope empty — fallback anchor scan
            anchors = page.query_selector_all(f"{ROW_SELECTOR} a[href]")
            for a in anchors:
                href = a.get_attribute("href") or ""
                if "#/medicamentos/" in href and "#/medicamentos/q/" not in href:
                    full = "https://consultas.anvisa.gov.br/" + href.lstrip("/")
                    if full not in seen:
                        seen.add(full)
                        links.append((full, ""))
            print(f"    (anchor fallback: {len(anchors)} links — Layer-2 will filter)")
        else:
            for row in rows_data:
                url  = build_detail_url(row)
                nome = row.get("nome", "")
                if url and url not in seen:
                    seen.add(url)
                    links.append((url, nome))

        next_pg = go_to_next_page(page, current_pg)
        if next_pg == -1:
            print(f"  No more pages after page {current_pg}. Harvest complete.")
            break
        current_pg = next_pg

    print(f"\nPhase 1 done — {len(links)} candidate URLs collected.")
    return links


# ════════════════════════════════════════════════════════════════════════════
# Phase 2 — extract product name (with Layer-2 Processo Matriz check)
# ════════════════════════════════════════════════════════════════════════════

def extract_text_by_label(page, label_text: str) -> str:
    try:
        label = page.locator(f"th:has-text('{label_text}')")
        if label.count() == 0:
            return ""
        return label.first.locator("xpath=ancestor::tr/td").first.inner_text().strip()
    except Exception:
        return ""


def fetch_product_name(context, url: str) -> tuple[str, bool]:
    """
    Open detail URL in a fresh tab.
    Returns (name, should_skip) where should_skip=True means the page
    has a Processo Matriz field and must be excluded (Layer-2 filter).
    This is the ONLY place exclusion is actually decided.
    """
    for attempt in range(RETRY_ATTEMPTS):
        detail_page = context.new_page()
        try:
            _front(detail_page)
            detail_page.goto(url, timeout=DETAIL_TIMEOUT)
            handle_challenge(detail_page)
            _soft_idle(detail_page, 10_000)
            detail_page.wait_for_selector("th:has-text('Nome do Produto'), th:has-text('Product Name')", state="attached", timeout=60_000)   # wait for the real content
            time.sleep(1.5)

            # ── Layer 2 filter: DOM check on detail page ─────────────────
            has_pm = detail_page.evaluate(DETAIL_HAS_PROCESSO_MATRIZ_JS)
            if has_pm:
                detail_page.close()
                return ("", True)   # excluded

            name = extract_text_by_label(detail_page, "Nome do Produto") or extract_text_by_label(detail_page, "Product Name")
            if not name:
                _dump_debug(detail_page, url, 0)
            detail_page.close()
            return (name, False)
        except Exception as exc:
            print(f"    [name] attempt {attempt+1} failed for {url}: {exc}")
            _dump_debug(detail_page, url, attempt + 1)
            try:
                detail_page.close()
            except Exception:
                pass
            time.sleep(2)
    return ("", False)


# ════════════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════════════

def main():
    CHECKPOINT = "anvisa_checkpoint.txt"

    already_done: dict[str, str] = {}   # url -> name  (name == "__SKIP__" means excluded)
    if os.path.exists(CHECKPOINT):
        with open(CHECKPOINT, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t", 1)
                if len(parts) == 2 and parts[0] != "":   # ignore junk empty-name entries from failed runs
                    already_done[parts[1]] = parts[0]
        print(f"Resuming — {len(already_done)} URLs already processed from checkpoint.")

    with StealthChromeSync() as browser:
        context = browser.context
        listing_page = context.new_page()

        # ── Phase 1 ───────────────────────────────────────────────────────
        all_links = harvest_links(listing_page)   # list of (url, nome)

        # ── Phase 2 ───────────────────────────────────────────────────────
        collected: list[tuple[str, str]] = []
        layer2_excluded = 0

        # Seed from checkpoint (skip "__SKIP__" entries)
        for url, name in already_done.items():
            if name != "__SKIP__":
                collected.append((name, url))

        done_urls = set(already_done.keys())
        # Every remaining URL must be opened — that's the only place
        # Processo Matriz exclusion is actually decided (Layer-2).
        todo = [(u, n) for u, n in all_links if u not in done_urls]

        print(f"\nPhase 2: {len(todo)} URLs to open "
              f"(name extraction + Layer-2 Processo Matriz check on each)")

        with open(CHECKPOINT, "a", encoding="utf-8") as ckpt:
            for i, (url, _listing_nome) in enumerate(todo, 1):
                print(f"  [{i}/{len(todo)}] {url}")
                name, skip = fetch_product_name(context, url)
                if skip:
                    layer2_excluded += 1
                    print(f"    ✗ excluded by Layer-2 (Processo Matriz on detail page)")
                    ckpt.write(f"__SKIP__\t{url}\n")
                else:
                    print(f"    ✓ {name or '(no name found)'}")
                    collected.append((name, url))
                    if name:
                        ckpt.write(f"{name}\t{url}\n")
                ckpt.flush()

        browser.close()

    print(f"\n── Summary ────────────────────────────────────────────────")
    print(f"  Total candidates from listing : {len(all_links)}")
    print(f"  Excluded by Layer-2 (detail)  : {layer2_excluded}")
    print(f"  Final records written         : {len(collected)}")

    # ── Write Excel ───────────────────────────────────────────────────────
    print(f"\nWriting {len(collected)} records to {OUT_EXCEL}…")

    with xlsxwriter.Workbook(OUT_EXCEL) as wb:
        ws = wb.add_worksheet("Medicines")
        bold = wb.add_format({"bold": True})
        ws.write(0, 0, "Product", bold)
        ws.write(0, 1, "Medicine Anvisa URL", bold)
        ws.set_column(0, 0, 40)
        ws.set_column(1, 1, 80)
        for r, (name, url) in enumerate(collected, 1):
            ws.write(r, 0, name)
            ws.write(r, 1, url)

    print("Done →", OUT_EXCEL)
    
    df_matrix_links_old = pd.read_excel("matrix_links_final_old.xlsx", dtype=str)

    df_matrix_links_old['Product'] = df_matrix_links_old['Product'].apply(lambda x: str(x).strip())
    
    df_matrix_links_new = pd.read_excel("matrix_links_final_new.xlsx", dtype=str)
    df_matrix_links_new['Product'] = df_matrix_links_new['Product'].apply(lambda x: str(x).strip())
    
    df_matrix_links_new['ID'] = ""
    
    try:
    
        for idx in range(df_matrix_links_new.shape[0]):
            
            for indx in range(df_matrix_links_old.shape[0]):
                
                if str(df_matrix_links_new.loc[idx, 'Product']).lower() == str(df_matrix_links_old.loc[indx, 'Product']).lower():
                    
                    df_matrix_links_new.loc[idx, 'ID'] = df_matrix_links_old.loc[indx, 'ID']
                    
        df_matrix_links_new.to_excel("matrix_links_final_new.xlsx", index=False)   

        print("Successfully done with IDs")         
        
                
    except:
        print("errors found with IDs.........................")
        
    

    if os.path.exists(CHECKPOINT):
        os.remove(CHECKPOINT)


if __name__ == "__main__":
    main()