
from playwright.sync_api import sync_playwright
import time
from agno.agent import Agent
from dotenv import load_dotenv
from agno.models.groq import Groq
from datetime import datetime
import json
import streamlit as st
import uuid
import os
import shutil
import pandas as pd
from sqlalchemy import create_engine
import psycopg2
import asyncio
import re
from datetime import datetime
from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeoutError
from typing import Dict, List, Optional, Any
import logging
import codecs
import unicodedata
from urllib.parse import quote, urljoin, urlparse
from playwright.async_api import TimeoutError

load_dotenv()

# =====================================================================================================
# STEALTH CHROME  (SeleniumBase "Stealthy Playwright Mode")  +  CLOUDFLARE CHALLENGE HELPER
# -----------------------------------------------------------------------------------------------------
# pip install seleniumbase playwright        (no "playwright install" needed - your real Chrome is used)
#
# Every place that used to do  chromium.launch(...)  now opens your installed Chrome through SeleniumBase
# and Playwright attaches to it with connect_over_cdp().  The scraping logic itself is untouched.
#
# Cloudflare: after each page load, handle_challenge(page) checks for a Cloudflare / Turnstile screen.
#   1) it first gives the challenge a few seconds to clear by itself,
#   2) then tries SeleniumBase's solve_captcha() (clicks the Turnstile checkbox),
#   3) if it is still there, it WAITS for YOU: click the checkbox in the Chrome window and the script
#      continues automatically the moment the challenge disappears (no Enter key needed).
# =====================================================================================================
import random
from seleniumbase import sb_cdp, cdp_driver

try:
    _STEALTH_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _STEALTH_BASE_DIR = os.getcwd()

STEALTH_CHROME_OPTIONS = {
    # Persistent Chrome profile: keeps the Cloudflare "cf_clearance" cookie between launches, so once a
    # challenge is solved the next medicines usually load without any challenge. Delete the folder to reset.
    "user_data_dir": os.path.join(_STEALTH_BASE_DIR, "stealth_chrome_profile"),
    # If Chrome is not found automatically, uncomment and set the path:
    # "binary_location": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    # "proxy": "user:pass@host:port",
    # "locale": "pt-BR",
}

CF_GRACE_SECONDS = 6            # let the challenge clear on its own before touching it
CF_AUTO_SOLVE_TRIES = 2         # how many times to try solve_captcha() automatically
CF_MANUAL_WAIT_SECONDS = 300    # how long to wait for you to click it yourself
PAUSE_BETWEEN_ITEMS = (3, 6)    # random pause (seconds) between medicines in the main loop; (0, 0) = off
QUERY_RETRIES = 2               # attempts per URL before giving up and not writing a JSON

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
        for step in (lambda: self.browser.close(),
                     lambda: self.playwright.stop(),
                     lambda: self.sb.quit()):
            try:
                step()
            except Exception:
                pass
        _wait_chrome_exit(self._user_data_dir)


class StealthChromeAsync:
    """Async format: real Chrome via cdp_driver + Playwright async API (connect_over_cdp).
    Use:  async with StealthChromeAsync(slow_mo=80) as browser:  page = await browser.new_page() ..."""

    def __init__(self, headless=False, slow_mo=None, **launch_options):
        self._headless = headless
        self._slow_mo = slow_mo
        self._launch_options = launch_options
        self._user_data_dir = _stealth_launch_kwargs(headless, launch_options).get("user_data_dir")
        self._closed = False
        self._claimed_default = False
        self._free_pages = []
        self.driver = None
        self.playwright = None
        self.browser = None

    async def start(self):
        try:
            self.driver = await cdp_driver.start_async(
                **_stealth_launch_kwargs(self._headless, self._launch_options))
            self.endpoint_url = self.driver.get_endpoint_url()
            self.playwright = await async_playwright().start()
            kw = {"slow_mo": self._slow_mo} if self._slow_mo else {}
            self.browser = await self.playwright.chromium.connect_over_cdp(self.endpoint_url, **kw)
            self.context = self.browser.contexts[0]                 # default context keeps the stealth setup
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            self.default_page = self.page
        except Exception:
            await self.close()
            raise
        return self

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def new_page(self):
        if self._free_pages:
            return self._free_pages.pop()
        if not self._claimed_default:
            self._claimed_default = True
            return self.default_page
        return await self.context.new_page()

    async def close_page(self, page):
        try:
            if len(self.context.pages) <= 1:
                await page.goto("about:blank")
                if page not in self._free_pages:
                    self._free_pages.append(page)
            else:
                await page.close()
        except Exception:
            pass

    async def goto(self, page, url, **kwargs):
        """page.goto() that tolerates a 'networkidle' timeout (real Chrome / Cloudflare pages often never go
        idle) and then runs the Cloudflare check. The calling code's own waits/selectors do the rest."""
        try:
            await page.goto(url, **kwargs)
        except PlaywrightTimeoutError as e:
            print(f"⚠️ goto timed out ({str(e).splitlines()[0]}) - checking the page anyway")
        await self.handle_challenge(page)

    # ------------------------------ Cloudflare ------------------------------
    async def _challenge_present(self, page):
        try:
            return bool(await page.evaluate(_CF_DETECT_JS))
        except Exception:
            return False

    async def _try_solve(self, page):
        tab = None
        try:
            for t in self.driver.tabs:
                if _same_page(getattr(t, "url", ""), page.url):
                    tab = t
                    break
        except Exception:
            pass
        await (tab or self.driver.main_tab).solve_captcha()

    async def handle_challenge(self, page, label=""):
        """Returns True if a challenge was seen (and cleared), False if none / not cleared."""
        if not await self._challenge_present(page):
            return False
        print(f"🛡️ Cloudflare challenge detected {label}- trying to clear it ...")
        await page.wait_for_timeout(CF_GRACE_SECONDS * 1000)
        cleared = not await self._challenge_present(page)
        tries = 0
        while not cleared and tries < CF_AUTO_SOLVE_TRIES:
            tries += 1
            try:
                await self._try_solve(page)
            except Exception as e:
                print(f"   solve_captcha attempt {tries} failed: {e}")
            await page.wait_for_timeout(4000)
            cleared = not await self._challenge_present(page)
        if not cleared:
            print("\a👉 Please click the Cloudflare checkbox in the Chrome window. "
                  f"Waiting up to {CF_MANUAL_WAIT_SECONDS}s - the script continues by itself once it clears.")
            waited = 0
            while waited < CF_MANUAL_WAIT_SECONDS:
                await page.wait_for_timeout(2000)
                waited += 2
                if not await self._challenge_present(page):
                    cleared = True
                    break
        if cleared:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            await page.wait_for_timeout(2000)
            print("✅ Cloudflare challenge cleared.")
            return True
        print("⚠️ Cloudflare challenge still present - continuing anyway.")
        return False

    # -------------------------------- cleanup --------------------------------
    async def close(self):
        """Disconnect Playwright, then quit Chrome. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        for step in (lambda: self.browser.close() if self.browser else None,
                     lambda: self.playwright.stop() if self.playwright else None):
            try:
                result = step()
                if result is not None:
                    await result
            except Exception:
                pass
        try:
            if self.driver:
                self.driver.stop()
        except Exception:
            pass
        _wait_chrome_exit(self._user_data_dir)


def _query_has_data(result):
    """True only if the scrape returned something worth saving (query() returns None on failure)."""
    if not isinstance(result, dict):
        return False
    main = result.get("main_process") or {}
    return bool(result.get("petitions") or result.get("company_info")
                or any(k != "historico" for k in main))

# =====================================================================================================


#llm = Groq(id="meta-llama/llama-4-scout-17b-16e-instruct", temperature=0.3)

curr_date = str(datetime.today().date())

#print(curr_date)

# if os.path.exists("medicine_json_files") and os.path.isdir("medicine_json_files"):
#     # Remove the folder and all contents
#     shutil.rmtree("medicine_json_files")
    
# os.makedirs("medicine_json_files")

# if os.path.exists("new_medicine_json_files") and os.path.isdir("new_medicine_json_files"):
#     # Remove the folder and all contents
#     shutil.rmtree("new_medicine_json_files")
# os.makedirs("new_medicine_json_files")

# if os.path.exists("gmp_json_files") and os.path.isdir("gmp_json_files"):
#     # Remove the folder and all contents
#     shutil.rmtree("gmp_json_files")
    
# os.makedirs("gmp_json_files")


if os.path.exists("under_review_medicine_json_files") and os.path.isdir("under_review_medicine_json_files"):
    # Remove the folder and all contents
    shutil.rmtree("under_review_medicine_json_files")
    
os.makedirs("under_review_medicine_json_files")


# if os.path.exists("pack_inserts_json_files") and os.path.isdir("pack_inserts_json_files"):
#     # Remove the folder and all contents
#     shutil.rmtree("pack_inserts_json_files")
    
# os.makedirs("pack_inserts_json_files")


def extract_text_by_label(page, label_text):
    try:
        label = page.locator(f"th:has-text('{label_text}')")
        cell = label.locator("xpath=following-sibling::td").first
        return cell.inner_text().strip()
    except:
        return "N/A"

def scrape_clone_owner_name(url):
    
    with StealthChromeSync(slow_mo=30) as browser:
        page = browser.new_page()

        print("🔄 Loading page...")
        page.goto(url, timeout=20000)
        browser.handle_challenge(page)
        page.wait_for_timeout(20000)

        product_data = {
            "Regularization Due Date": extract_text_by_label(page, "Vencimento da Regularização"),
            "Company Holding Regularization": extract_text_by_label(page, "Empresa Detentora da Regularização")
        }

        browser.close()
        return product_data


# def scrape_ref_drug_for_matrix(url):
#     with sync_playwright() as p:
#         browser = p.chromium.launch(headless=False, slow_mo=30)
#         page = browser.new_page()

#         print("🔄 Loading page...")
#         page.goto(url, timeout=30000)
#         page.wait_for_timeout(30000)

#         product_data = {
#             "Product Name": extract_text_by_label(page, "Nome do Produto"),
#             "Reference drug": extract_text_by_label(page, "Medicamento de referência"),
#         }

#         return product_data


def scrape_anvisa_full_flow(url):
    with StealthChromeSync(slow_mo=30) as browser:
        page = browser.new_page()

        print("🔄 Loading page...")
        page.goto(url, timeout=20000)
        browser.handle_challenge(page)
        page.wait_for_timeout(20000)

        product_data = {
            "Product Name": extract_text_by_label(page, "Nome do Produto"),
            "Company Holding the Registration": extract_text_by_label(page, "Empresa Detentora da Regularização"),
            "Prioritization Type": extract_text_by_label(page, "Tipo de Priorização"),
            "Regularization Date": extract_text_by_label(page, "Data da Regularização"),
            "Regularization Due Date": extract_text_by_label(page, "Vencimento da Regularização"),
            "Regularization Number": extract_text_by_label(page, "Número da Regularização"),
            "Case Number": extract_text_by_label(page, "Número do Processo"),
            "CNPJ": extract_text_by_label(page, "CNPJ"),
            "Clone Details": "N/A",
        }

        # Step 1: Expand presentations
        try:
            print("🔘 Scrolling to bottom...")
            for _ in range(5):
                page.mouse.wheel(0, 8000)
                page.wait_for_timeout(20000)

            print("🔘 Clicking 'Expandir todas'...")
            expand_button = page.locator("a:has-text('Expandir todas')")
            expand_button.wait_for(state="visible", timeout=20000)
            expand_button.click()
            page.wait_for_timeout(20000)
        except Exception as e:
            print(f"⚠️ Expandir error: {e}")

        print("📦 Extracting presentations...")
        rows = page.locator("tbody[ng-repeat='apresentacao in produto.apresentacoes | orderBy:\\'numero\\'']")
        total = rows.count()

        for i in range(total):
            pres = rows.nth(i)

            def safe_get(css):
                try:
                    return pres.locator(css).inner_text().strip()
                except:
                    return "N/A"

            presentation_data = {
              #  "Presentation Number": safe_get("td:nth-child(1)"),
              #  "Presentation Name": safe_get("td:nth-child(2)"),
              #  "Registry Code": safe_get("td:nth-child(3)"),
              #  "Pharmaceutical Form": safe_get("td:nth-child(4)"),
              #  "Publication Date": safe_get("td:nth-child(5)"),
              #  "Validity": safe_get("td:nth-child(6)"),
                "Manufacturing Location": safe_get("tr:has(th:has-text('Local de Fabricação')) td"),
               # "Destination": safe_get("tr:has(th:has-text('Destinação')) td"),
               # "Tarja": safe_get("tr:has(th:has-text('Tarja')) td"),
               # "Fractional Presentation": safe_get("tr:has(th:has-text('Apresentação fracionada')) td")
            }

#            product_data["Presentations"].append(presentation_data)

        # Step 2: Clone extraction
        

        try:
            clone_btn = page.locator('a[modal-anvisa="processoClone"]')
            if clone_btn.count() > 0 and clone_btn.first.is_visible():
                print("🧬 Opening clone modal...")
                clone_btn.first.click()
                page.wait_for_selector("div.modal-content table.table-hover tbody tr", timeout=30000)
                page.wait_for_timeout(500)  # allow Angular render

                clone_rows = page.locator("div.modal-content table.table-hover tbody tr")
                clone_details = []
                clone_urls = []

                for i in range(clone_rows.count()):
                    row = clone_rows.nth(i)
                    tds = row.locator("td")
                    if tds.count() < 2:
                        continue

                    clone_name = tds.nth(0).inner_text().strip()
                    clone_process_number = tds.nth(1).inner_text().strip()

                    clone_link_element = tds.nth(0).locator("a")
                    if clone_link_element.count() == 0:
                        clone_url = "N/A"
                    else:
                        with page.context.expect_page() as new_page_info:
                            clone_link_element.first.click()
                        clone_page = new_page_info.value
                        clone_page.wait_for_load_state()

                        clone_url = clone_page.url
                        clone_page.close()
                        page.bring_to_front()

                    clone_details.append(
                        f"{i+1}. Clone Product Name: {clone_name}\n   Clone Process Number: {clone_process_number}\n   Clone URL: {clone_url}"
                    )
                    clone_urls.append({
                        "Name": clone_name,
                        "Process Number": clone_process_number,
                        "Clone URL": clone_url
                    })

                product_data["Clone Details"] = "\n".join(clone_details)
                product_data["Clones"] = clone_urls

                # Close the clone modal
                close_btn = page.locator("div.modal-content button.close")
                if close_btn.is_visible():
                    close_btn.click()
                    page.wait_for_timeout(1000)
            else:
                print("ℹ️ No clone modal available on this page.")
        except Exception as e:
            print(f"⚠️ Clone modal error: {e}")

        browser.close()

        complete_medicine_data = {**product_data, **presentation_data}
        
        
        return complete_medicine_data
        
        
        # try:
        #     clone_link = page.locator('a[modal-anvisa="processoClone"]')
        #     if clone_link and clone_link.is_visible():
        #         print("🧬 Opening clone modal...")
        #         clone_link.click()
        #         page.wait_for_selector("div.modal-content table.table-hover tbody tr", timeout=30000)

        #         rows = page.query_selector_all("div.modal-content table.table-hover tbody tr")
        #         clone_lines = []

        #         for idx, row in enumerate(rows, start=1):
        #             cols = row.query_selector_all("td")
        #             if len(cols) >= 2:
        #                 name = cols[0].inner_text().strip()
        #                 number = cols[1].inner_text().strip()
        #                 clone_lines.append(f"{idx}. Clone Product Name: {name}\n   Clone Process Number: {number}")

        #         product_data["Clone Details"] = "\n" + "\n".join(clone_lines)

        #         # Close the modal
        #         close_btn = page.locator("div.modal-content button.close")
        #         if close_btn.is_visible():
        #             close_btn.click()
        #             page.wait_for_timeout(1000)
        # except Exception as e:
        #     print(f"⚠️ Clone modal error: {e}")

        # browser.close()
        
        
        # complete_medicine_data = {**product_data, **presentation_data}
        
        
        # return complete_medicine_data



# def analyze_product_from_anvisa(medicine_product_data, curr_date):
    
    
#     medicine_product_data = json.dumps(medicine_product_data)
    
#     llm = Groq(id="meta-llama/llama-4-scout-17b-16e-instruct", temperature=0.1)
    
# #    llm = Groq(id="deepseek-r1-distill-llama-70b", temperature=0.1)

#     output_template= {
#     "ProductName":{ "type": "string", "description": "product name" },
#     "RegularizationNumber":{ "type": "string", "description": "regularization number" },
#     "RegularizationDate":{ "type": "string", "description": "regularization date" },
#     "CNPJ":{ "type": "string", "description": "CNPJ" },
#     "CaseNumber":{ "type": "string", "description": "case number" },
#     "RegularizationDueDate":{ "type": "string", "description": "regularization due date" },
#     "CloneDetails":{ "type": "string", "description": "clone details" },
#     "ActiveIngredient":{ "type": "string", "description": "active ingredient" },
#     "Packaging":{ "type": "string", "description": "packaging" }, 
#     "ManufacturingLocation":{ "type": "string", "description": "manufacturing location" }, 
#     "RouteofAdministration":{ "type": "string", "description": "route of administration" }, 
#     "Conservation":{ "type": "string", "description": "conservation" }, 
#     "PrescriptionRestriction":{ "type": "string", "description": "prescription restriction" }, 
#     "Restrictionofuse":{ "type": "string", "description": "restriction of use" }, 
#     }


#     regulatory_agent = Agent(
#         name = "regulatory analyzer for medicines",
#         role = "analyzing regulatory compliance for medicines",
#         model = llm,
#         show_tool_calls = True,
#         markdown=True,
#         instructions=["You are an expert in regulatory compliance for medicines. Your task is to go through the extracted medicine product data from Anvisa site and understand the content and generate the Output in only json format with the given Schema (output_template) only. Output should contain only the correct json data and nothing else. Don't hallucinate any data. In case of clones, please put all the clones details in Clone Details field in string format without missing anything. medicine product data: " + medicine_product_data + ". Schema: " + str(output_template)],
#         debug_mode = True     
        
#     )
    
    
#     output =  regulatory_agent.run("Follow the instructions carefully, execute and get a structured json output.", stream=False)
#     json_data = output.content

#     return json_data


# def scrape_gmp_certificate(url):
#     with sync_playwright() as p:
#         browser = p.chromium.launch(headless=False, slow_mo=80)
#         page = browser.new_page()
        
#         print("🔄 Navigating to the certificate page...")
#         page.goto(url, timeout=60000)
#         # Wait until the main container loads; adjust timeout as needed.
#         page.wait_for_selector("#gridDetalheCertificado", timeout=300000)
#         page.wait_for_timeout(30000)  # extra wait for dynamic content

#         # Map each row (by order) to the desired field name.
#         mapping = {
#             0: "Certified Company",                # Empresa Certificada
#             1: "Code. Unique / Certified CNPJ",      # Cód. Único / CNPJ Certificada
#             2: "Certified Company Address",          # Endereço (Certified)
#             3: "Country",                            # País
#             4: "Requesting Company",                 # Empresa Solicitante
#             5: "CNPJ (Requesting Company)",          # CNPJ (Requesting Company)
#             6: "Requesting Company Address",         # Endereço (Requesting)
#             7: "City / State",                       # Cidade / UF
#             8: "Subject",                            # Assunto
#             9: "Certificate Type",                   # Tipo de Certificado
#             10: "Expiration Date",                   # Data de Validade
#             11: "Publication Date",                  # Data de Publicação
#             12: "Date of Resolution",                # Data da Resolução
#             13: "Resolution",                        # Resolução
#             14: "Certificate Issued by",             # Certificado Emitido por
#             15: "N.DOU",                             # N.DOU
#             16: "File Number"                     # File Number
#         }

#         data = {}
       
#         rows = page.locator("#gridDetalheCertificado > div")
#         total = rows.count()
        
#         print(f"📝 Found {total} rows in the certificate details.")

#         for i in range(total):
#             row = rows.nth(i)
#             try:
#                 label = row.locator("label").inner_text().strip()
#             except Exception as e:
#                 label = "N/A"
#             try:
        
#                 value = row.locator("div.ng-binding, a.ng-binding").inner_text().strip()
#             except Exception as e:
#                 value = "N/A"
#             if i in mapping:
#                 key = mapping[i]
#                 data[key] = value
               
        
#         browser.close()
#         return data

#----------------------------Web Scraping for GMP Data---------------------------------------------------------


def scrape_gmp_certificate(url, headless=False, slow_mo=80):
    """
    Full chained flow:
      1) Scrape GMP certificate page (original mapping).
      2) Extract File Number (numeric) and open expediente page.
      3) Extract only "Processo" (exact label match).
      4) Build Processo Document URL and open it.
      5) Scan peticoes top->down, find first where Assunto contains
         both 'MEDICAMENTOS' and 'RENOVAÇÃO', and extract its 'Data do Expediente'
         (explicitly selecting the 'Data do Expediente' field, not 'Expediente').
    """
    with StealthChromeSync(headless=headless, slow_mo=slow_mo) as browser:

        # ---------------------------
        # 1) Open GMP certificate page
        # ---------------------------
        page = browser.new_page()
        print("🔄 Navigating to the certificate page...")
        page.goto(url, timeout=60000)
        browser.handle_challenge(page)

        page.wait_for_selector("#gridDetalheCertificado", timeout=30000)
        page.wait_for_timeout(30000)

        mapping = {
            0: "Certified Company",
            1: "Code. Unique / Certified CNPJ",
            2: "Certified Company Address",
            3: "Country",
            4: "Requesting Company",
            5: "CNPJ (Requesting Company)",
            6: "Requesting Company Address",
            7: "City / State",
            8: "Subject",
            9: "Certificate Type",
            10: "Expiration Date",
            11: "Publication Date",
            12: "Date of Resolution",
            13: "Resolution",
            14: "Certificate Issued by",
            15: "N.DOU",
            16: "File Number"
        }

        data = {}
        rows = page.locator("#gridDetalheCertificado > div")
        total = rows.count()
        print(f"📝 Found {total} certificate rows.")

        for i in range(total):
            row = rows.nth(i)
            try:
                value = row.locator("div.ng-binding, a.ng-binding").inner_text().strip()
            except Exception:
                value = "N/A"
            if i in mapping:
                data[mapping[i]] = value

        # Extract numeric file number
        file_number_raw = data.get("File Number", "")
        file_number = re.sub(r"\D", "", file_number_raw or "")
        data["File Number (numeric)"] = file_number
        print(f"\n➡️ Extracted File Number: {file_number}")

        expediente_url = f"https://consultas.anvisa.gov.br/#/documentos/tecnicos/expediente/{file_number}/"
        data["Expediente URL"] = expediente_url

        # Close GMP page before opening expediente page
        print("🛑 Closing GMP certificate page before navigating to expediente...")
        browser.close_page(page)

        # ---------------------------
        # 2) Open expediente page
        # ---------------------------
        expediente_page = browser.new_page()
        print(f"🔗 Opening expediente page: {expediente_url}")
        expediente_page.goto(expediente_url, timeout=60000)
        browser.handle_challenge(expediente_page)
        expediente_page.wait_for_timeout(3500)

        # ---------------------------
        # 3) Extract ONLY the Processo field (exact label match)
        # ---------------------------
        try:
            container = expediente_page.locator(
                "div.col-sm-3",
                has=expediente_page.locator("label:text-is('Processo')")
            ).first

            if container.count() == 0:
                raise Exception("Processo container not found with exact label match.")

            processo_raw = container.locator("div.ng-binding").inner_text().strip()
            processo_numeric = re.sub(r"\D", "", processo_raw)

            data["Processo"] = processo_raw
            data["Processo (numeric)"] = processo_numeric

            print(f"✅ Processo extracted: {processo_raw}")
            print(f"➡️ Processo (numeric only): {processo_numeric}")

        except Exception as e:
            print("⚠️ Processo not found:", str(e))
            data["Processo"] = None
            data["Processo (numeric)"] = None
            browser.close_page(expediente_page)
            browser.close()
            return data

        # Close expediente page before opening processo document page
        print("🛑 Closing expediente page...")
        browser.close_page(expediente_page)

        # ---------------------------
        # 4) Build Processo Document URL and open it
        # ---------------------------
        processo_link = f"https://consultas.anvisa.gov.br/#/documentos/tecnicos/{processo_numeric}/"
        data["Processo Document URL"] = processo_link

        print(f"🔗 Opening Processo Document Page: {processo_link}")
        process_page = browser.new_page()
        process_page.goto(processo_link, timeout=60000)
        browser.handle_challenge(process_page)
        process_page.wait_for_timeout(30000)

        # ---------------------------
        # 5) Scan peticoes top->down and find Assunto with MEDICAMENTOS + RENOVAÇÃO
        #    and extract Data do Expediente (explicit selector for Data do Expediente)
        # ---------------------------
        matched_assunto = None
        matched_data_do_expediente = None

        try:
            count = process_page.locator("tbody tr").count()
            print(f"🔎 Found {count} rows inside tbody to scan for peticoes.")

            for idx in range(count):
                tr = process_page.locator("tbody tr").nth(idx)

                # locate Assunto inside this tr
                assunto_loc = tr.locator("div:has(label:text-is('Assunto')) >> div.ng-binding")
                if assunto_loc.count() == 0:
                    continue

                assunto_raw = assunto_loc.first.inner_text().strip()
                assunto_text = re.sub(r"\s+", " ", assunto_raw).strip()
                assunto_lower = assunto_text.lower()

                if "medicamentos" in assunto_lower and "renovação" in assunto_lower:
                    matched_assunto = assunto_text

                    # ===== EXPLICIT: select the Data do Expediente field (not Expediente) =====
                    data_expediente_loc = tr.locator(
                        "div.col-sm-3:has(label:text-is('Data do Expediente')) >> div.ng-binding"
                    )

                    if data_expediente_loc.count() > 0:
                        data_expediente_raw = data_expediente_loc.first.inner_text().strip()
                        m = re.search(r"\d{2}/\d{2}/\d{4}", data_expediente_raw)
                        if m:
                            matched_data_do_expediente = m.group(0)
                        else:
                            matched_data_do_expediente = data_expediente_raw.strip()
                    else:
                        matched_data_do_expediente = None

                    print(f"✅ Matched Assunto: {matched_assunto}")
                    print(f"➡️ Data do Expediente (captured): {matched_data_do_expediente}")
                    break

            data["Assunto Matched"] = matched_assunto
            data["Data do Expediente"] = matched_data_do_expediente

            if not matched_assunto:
                print("⚠️ No peticao with Assunto containing both 'MEDICAMENTOS' and 'RENOVAÇÃO' was found on the page.")

        except Exception as e:
            print("⚠️ Error while scanning peticoes for Assunto match:", str(e))
            data["Assunto Matched"] = None
            data["Data do Expediente"] = None

        # small visual pause
        time.sleep(1)

        browser.close()
        return data 


#----------------Web Scraping for Query Management Tracker (Under Review Processes)--------------------------------


# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class ANVISAScraper:
    def __init__(self, headless: bool = False, timeout: int = 30000):
        self.headless = headless
        self.timeout = timeout
        self.page = None
        self.browser = None
        
    async def __aenter__(self):
        # real Chrome through SeleniumBase (stealth); the fake Chrome/91 user agent and fixed viewport are
        # intentionally gone - a user agent that doesn't match the real browser is itself a bot signal
        self.playwright = None
        self.browser = await StealthChromeAsync(headless=self.headless).start()
        self.page = await self.browser.new_page()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    def clean_text(self, text: str) -> str:
        """Clean and normalize text content"""
        if not text:
            return ""
        return re.sub(r'\s+', ' ', text.strip())

    def parse_date(self, date_str: str) -> Optional[str]:
        """Parse date string to ISO format"""
        if not date_str or date_str.strip() == "":
            return None
        
        date_str = re.sub(r'\s+', ' ', date_str.strip())
        
        date_match = re.search(r'(\d{2}/\d{2}/\d{4})', date_str)
        if date_match:
            try:
                date_obj = datetime.strptime(date_match.group(1), '%d/%m/%Y')
                return date_obj.strftime('%Y-%m-%d')
            except ValueError:
                pass
        
        return date_str

    async def wait_for_page_load(self) -> None:
        """Wait for the page to fully load"""
        try:
            await self.page.wait_for_selector('.ng-scope', timeout=self.timeout)
            await self.page.wait_for_selector('form.ng-scope', timeout=self.timeout)
            await self.page.wait_for_selector('.dw-loading', state='hidden', timeout=5000)
            await asyncio.sleep(2)
        except PlaywrightTimeoutError:
            logger.warning("Page load timeout - proceeding anyway")

    async def extract_company_info(self) -> Dict[str, str]:
        """Extract company information from the header table"""
        company_info = {}
        
        try:
            company_table = await self.page.query_selector('table.table-bordered.table-static')
            if company_table:
                rows = await company_table.query_selector_all('tr')
                for row in rows:
                    cells = await row.query_selector_all('th, td')
                    if len(cells) >= 4:
                        empresa_th = await cells[0].inner_text()
                        if 'Empresa' in empresa_th:
                            company_info['empresa'] = self.clean_text(await cells[1].inner_text())
                        
                        cnpj_th = await cells[2].inner_text()
                        if 'CNPJ' in cnpj_th:
                            company_info['cnpj'] = self.clean_text(await cells[3].inner_text())
                            
        except Exception as e:
            logger.error(f"Error extracting company info: {e}")
            
        return company_info

    async def extract_main_process_info(self) -> Dict[str, Any]:
        """Extract main process information (including historic data)"""
        process_info = {}
        
        try:
            main_panel = await self.page.query_selector('.panel-body')
            if not main_panel:
                return process_info
                
            rows = await main_panel.query_selector_all('.row')
            
            for row in rows:
                cols = await row.query_selector_all('.col-sm-3, .col-sm-6')
                
                for col in cols:
                    try:
                        label_elem = await col.query_selector('label')
                        value_elem = await col.query_selector('div:not(.btn)')
                        
                        if label_elem and value_elem:
                            label = self.clean_text(await label_elem.inner_text())
                            value = self.clean_text(await value_elem.inner_text())
                            
                            if 'Processo' in label:
                                process_info['processo'] = value
                            elif 'Data do Processo' in label:
                                process_info['data_processo'] = self.parse_date(value)
                            elif 'Protocolo' in label:
                                process_info['protocolo'] = value
                            elif 'Expediente' in label and 'Data' not in label:
                                process_info['expediente'] = value
                            elif 'Assunto' in label:
                                process_info['assunto'] = value
                            elif 'Situação atual' in label:
                                process_info['situacao_atual'] = value
                            elif 'Encontra-se na' in label:
                                lines = value.split('\n')
                                if lines:
                                    process_info['localizacao'] = self.clean_text(lines[0])
                                    if len(lines) > 1 and 'Desde:' in lines[1]:
                                        date_match = re.search(r'Desde:\s*(\d{2}/\d{2}/\d{4})', lines[1])
                                        if date_match:
                                            process_info['localizacao_desde'] = self.parse_date(date_match.group(1))
                            elif 'Publicação' in label:
                                process_info['dados_publicacao'] = value
                                
                    except Exception as e:
                        logger.error(f"Error extracting from column: {e}")
            
            # ✅ Extract Histórico da Situação for Main Process
            logger.info("Attempting to extract historic data for main process")
            historic_data = await self.click_historic_button(main_panel)
            process_info['historico'] = historic_data if historic_data else []
            logger.info(f"Extracted {len(process_info['historico'])} historic records for main process")
                                
        except Exception as e:
            logger.error(f"Error extracting main process info: {e}")
            
        return process_info

    async def click_historic_button(self, petition_row) -> Optional[List[Dict[str, str]]]:
        """Click historic button and extract historic data"""
        try:
            historic_button = await petition_row.query_selector('a[modal-anvisa="modalHistSituacao"]')
            if not historic_button:
                return None
                
            logger.info("Clicking historic button...")
            await historic_button.click()
            await asyncio.sleep(2)
            
            modal_selector = '.modal-dialog, .modal-content, [modal-anvisa="modalHistSituacao"]'
            try:
                await self.page.wait_for_selector(modal_selector, timeout=20000)
                await asyncio.sleep(1)
                
                historic_data = await self.extract_historic_data_from_modal()
                await self.close_modal()
                return historic_data
                
            except PlaywrightTimeoutError:
                logger.warning("Modal did not appear - trying alternative approach")
                await self.close_modal()
                return None
                
        except Exception as e:
            logger.error(f"Error clicking historic button: {e}")
            await self.close_modal()
            return None

    async def extract_historic_data_from_modal(self) -> List[Dict[str, str]]:
        """Extract historic data from the opened modal"""
        historic_data = []
        
        try:
            await asyncio.sleep(1)
            modal_tables = await self.page.query_selector_all('.modal-content table, .modal-body table, table')
            
            for table in modal_tables:
                table_text = await table.inner_text()
                if any(keyword in table_text.lower() for keyword in ['data', 'situação', 'histórico', 'tramitação']):
                    rows = await table.query_selector_all('tr')
                    headers = []
                    
                    for i, row in enumerate(rows):
                        cells = await row.query_selector_all('th, td')
                        if i == 0:
                            for cell in cells:
                                header = self.clean_text(await cell.inner_text())
                                headers.append(header)
                        else:
                            if len(cells) > 0:
                                row_data = {}
                                for j, cell in enumerate(cells):
                                    cell_text = self.clean_text(await cell.inner_text())
                                    header = headers[j] if j < len(headers) else f"col_{j}"
                                    row_data[header] = cell_text
                                if row_data and any(row_data.values()):
                                    historic_data.append(row_data)
                                    
        except Exception as e:
            logger.error(f"Error extracting historic data from modal: {e}")
            
        return historic_data

    async def close_modal(self) -> None:
        """Close any open modal"""
        try:
            close_selectors = [
                '.modal .close',
                '.modal-header .close',
                'button[data-dismiss="modal"]',
                '.btn-default:has-text("Fechar")',
                '.btn:has-text("Voltar")'
            ]
            
            for selector in close_selectors:
                try:
                    close_button = await self.page.query_selector(selector)
                    if close_button and await close_button.is_visible():
                        await close_button.click()
                        await asyncio.sleep(1)
                        break
                except:
                    continue
                    
            await self.page.keyboard.press('Escape')
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"Error closing modal: {e}")

    async def extract_petition_data(self, petition_row) -> Dict[str, Any]:
        """Extract data from a single petition row"""
        petition_data = {}
        
        try:
            rows = await petition_row.query_selector_all('.row')
            
            for row in rows:
                cols = await row.query_selector_all('.col-sm-3, .col-sm-6')
                
                for col in cols:
                    try:
                        label_elem = await col.query_selector('label')
                        value_elem = await col.query_selector('div:not(.btn):not(.text-right)')
                        
                        if label_elem and value_elem:
                            label = self.clean_text(await label_elem.inner_text())
                            value = self.clean_text(await value_elem.inner_text())
                            
                            if 'Expediente' in label and 'Data' not in label:
                                petition_data['expediente'] = value
                            elif 'Data do Expediente' in label:
                                petition_data['data_expediente'] = self.parse_date(value)
                            elif 'Protocolo' in label:
                                petition_data['protocolo'] = value
                            elif 'Situação atual' in label:
                                petition_data['situacao_atual'] = value
                            elif 'Assunto' in label:
                                petition_data['assunto'] = value
                            elif 'Publicação' in label:
                                petition_data['dados_publicacao'] = value
                            elif 'Encontra-se na' in label:
                                lines = value.split('\n')
                                if lines:
                                    petition_data['localizacao'] = self.clean_text(lines[0])
                                    if len(lines) > 1 and 'Desde' in lines[1]:
                                        date_match = re.search(r'Desde\s*(\d{2}/\d{2}/\d{4})', lines[1])
                                        if date_match:
                                            petition_data['localizacao_desde'] = self.parse_date(date_match.group(1))
                                            
                    except Exception as e:
                        logger.error(f"Error extracting petition column data: {e}")
            
            logger.info(f"Attempting to extract historic data for petition {petition_data.get('expediente', 'unknown')}")
            historic_data = await self.click_historic_button(petition_row)
            if historic_data:
                petition_data['historico'] = historic_data
                logger.info(f"Extracted {len(historic_data)} historic records")
            else:
                petition_data['historico'] = []
                logger.info("No historic data found or accessible")
                
        except Exception as e:
            logger.error(f"Error extracting petition data: {e}")
            
        return petition_data

    async def extract_all_petitions(self) -> List[Dict[str, Any]]:
        """Extract all petition data from the petitions table"""
        petitions = []
        
        try:
            petitions_table = await self.page.query_selector('table.table-static.table-bordered.table-striped')
            if not petitions_table:
                logger.info("No petitions table found")
                return petitions
                
            petition_rows = await petitions_table.query_selector_all('tr')
            data_rows = petition_rows[1:] if len(petition_rows) > 1 else []
            
            logger.info(f"Found {len(data_rows)} petition rows to process")
            
            for i, row in enumerate(data_rows):
                logger.info(f"Processing petition {i+1}/{len(data_rows)}")
                try:
                    petition_data = await self.extract_petition_data(row)
                    if petition_data:
                        petitions.append(petition_data)
                        logger.info(f"Successfully extracted petition: {petition_data.get('expediente', 'unknown')}")
                    await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Error processing petition row {i+1}: {e}")
                    continue
                    
        except Exception as e:
            logger.error(f"Error extracting petitions: {e}")
            
        return petitions

    async def scrape_document(self, url: str) -> Dict[str, Any]:
        """Main scraping method"""
        logger.info(f"Starting scrape of: {url}")
        
        try:
            await self.browser.goto(self.page, url, wait_until='networkidle', timeout=self.timeout)
            await self.wait_for_page_load()
            
            result = {
                'url': url,
                'scraped_at': datetime.now().isoformat(),
                'company_info': await self.extract_company_info(),
                'main_process': await self.extract_main_process_info(),
                'petitions': await self.extract_all_petitions()
            }
            
            logger.info(f"Scraping completed. Found {len(result['petitions'])} petitions")
            return result
            
        except Exception as e:
            logger.error(f"Error during scraping: {e}")
            raise


    async def scrape_document1(self, url: str) -> Dict[str, Any]:
        """Main scraping method"""
        logger.info(f"Starting scrape of: {url}")
        
        try:
            await self.browser.goto(self.page, url, wait_until='networkidle', timeout=self.timeout)
            await self.wait_for_page_load()
            
            result = {
                'main_process': await self.extract_main_process_info(),
            }
            
            print(result)
            
#            logger.info(f"Scraping completed. Found {len(result['petitions'])} petitions")
            return result
            
        except Exception as e:
            logger.error(f"Error during scraping: {e}")
            raise


async def query1(url):
    """Main execution function"""
#    url = "https://consultas.anvisa.gov.br/#/documentos/tecnicos/25351549180202298/"
    
    async with ANVISAScraper(headless=False, timeout=20000) as scraper1:
        try:
            result = await scraper1.scrape_document1(url)

            return result
        except Exception as e:
            logger.error(f"Scraping failed: {e}")
            print(f"❌ Scraping failed: {e}")


async def query(url):
    """Main execution function"""
#    url = "https://consultas.anvisa.gov.br/#/documentos/tecnicos/25351549180202298/"
    
    async with ANVISAScraper(headless=False, timeout=20000) as scraper:
        try:
            result = await scraper.scrape_document(url)

            
            # output_filename = f"anvisa_document_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            # with open(output_filename, 'w', encoding='utf-8') as f:
            #     json.dump(result, f, ensure_ascii=False, indent=2)
                
            # print(f"\n✅ Scraping completed successfully!")
            # print(f"📄 Results saved to: {output_filename}")
            # print(f"🏢 Company: {result['company_info'].get('empresa', 'N/A')}")
            # print(f"📋 Main Process: {result['main_process'].get('processo', 'N/A')}")
            # print(f"📝 Petitions found: {len(result['petitions'])}")
            
            # for i, petition in enumerate(result['petitions'], 1):
            #     historic_count = len(petition.get('historico', []))
            #     print(f"   {i}. Expediente: {petition.get('expediente', 'N/A')} - Historic records: {historic_count}")
        
            
            return result
        except Exception as e:
            logger.error(f"Scraping failed: {e}")
            print(f"❌ Scraping failed: {e}")

#---------------------------Pack Insert------------------------------------------------------------------------

# ----------------- CONFIG -----------------
#PRODUCT_URL = "https://consultas.anvisa.gov.br/#/medicamentos/1100510?cnpj=03978166000175&situacaoRegistro=V"
SEARCH_URL = "https://consultas.anvisa.gov.br/#/bulario/q/?nomeProduto={q}"
PER_PAGE_TIMEOUT = 30000
# ------------------------------------------

def sanitize_url(raw: str) -> str:
    if raw is None:
        raise ValueError("No URL provided")
    u = str(raw).strip()
    if (u.startswith('"') and u.endswith('"')) or (u.startswith("'") and u.endswith("'")):
        u = u[1:-1].strip()
    parsed = urlparse(u)
    if not parsed.scheme:
        u = "https://" + u.lstrip('/')
    parsed = urlparse(u)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid URL after sanitization: {u!r}")
    return u


# ----------------- NEW: extract_text_by_label -----------------
async def extract_text_by_label_async(page, label_text):
    """Generic helper to extract the value from <th> labelText → <td> value."""
    try:
        th = page.locator(f"th:has-text('{label_text}')")
        if await th.count() == 0:
            return "N/A"
        td = th.locator("xpath=following-sibling::td").first
        txt = (await td.inner_text()).strip()
        return " ".join(txt.split()) if txt else "N/A"
    except:
        return "N/A"


# ----------------- helpers -----------------
async def extract_reference_drug(page):
    candidates = [
        "Medicamento de referência",
        "Medicamento de Referência",
        "Medicamento de referencia",
        "Medicamento"
    ]
    for cand in candidates:
        try:
            th = page.locator(f"th:has-text('{cand}')")
            if await th.count() > 0:
                td = th.locator("xpath=following-sibling::td").first
                text = (await td.inner_text()).strip()
                if text:
                    return text
        except:
            continue
    return None


def normalize_key(s: str) -> str:
    if not s:
        return ""
    nk = unicodedata.normalize("NFKD", s)
    nk = "".join(ch for ch in nk if not unicodedata.combining(ch))
    return " ".join(nk.lower().split())


async def extract_detalhar_fields(page, timeout=25000):
    result = {
        "Nome Comercial": "N/A",
        "Data de Publicação": "N/A",
        "all_fields": {}
    }

    try:
        await page.wait_for_selector("table.table.table-bordered", timeout=timeout)
    except TimeoutError:
        pass

    try:
        rows = await page.query_selector_all("table.table.table-bordered tr")
        for row in rows:
            cols = await row.query_selector_all("th, td")
            texts = [" ".join((await col.inner_text()).split()).strip() for col in cols]
            for i in range(0, len(texts) - 1, 2):
                result["all_fields"][texts[i]] = texts[i + 1]
    except Exception as e:
        result["all_fields"]["_error"] = str(e)

    norm_map = {normalize_key(k): k for k in result["all_fields"].keys()}

    # Nome Comercial
    for v in ["Nome Comercial", "Nome comercial", "Nome do Produto"]:
        nk = normalize_key(v)
        if nk in norm_map:
            result["Nome Comercial"] = result["all_fields"][norm_map[nk]]
            break

    # Data de Publicação
    for v in ["Data de Publicação", "Data de Publicacao", "Data Publicacao"]:
        nk = normalize_key(v)
        if nk in norm_map:
            result["Data de Publicação"] = result["all_fields"][norm_map[nk]]
            break

    return result


# ----------------- main flow -----------------
async def scrap_pack_insert_anvisa(PRODUCT_URL):
    try:
        product_url = sanitize_url(PRODUCT_URL)
    except Exception as e:
        print({"Product Name": "N/A", "error": str(e)})
        return

    async with StealthChromeAsync(slow_mo=80) as browser:
        context = browser.context

        product_page = await browser.new_page()
        for _ in range(3):
            try:
                await product_page.goto(product_url, timeout=PER_PAGE_TIMEOUT)
                break
            except:
                await product_page.wait_for_timeout(2000)

        await browser.handle_challenge(product_page)
        await product_page.wait_for_timeout(6000)

        # ----------------- NEW: Extract Product Name -----------------
        product_name = await extract_text_by_label_async(product_page, "Nome do Produto")

        # Extract reference drug (unchanged)
        ref_drug = await extract_reference_drug(product_page)
        await browser.close_page(product_page)

        if not ref_drug:
            print({
                "Nome do Produto": product_name,
                "Medicamento de referência": "N/A",
                "Data de Publicação": "N/A"
            })
            await browser.close()
            return

        # Search for reference drug in Bulario
        search_url = SEARCH_URL.format(q=quote(ref_drug))
        search_page = await browser.new_page()
        try:
            await search_page.goto(search_url, timeout=PER_PAGE_TIMEOUT)
        except:
            pass

        await browser.handle_challenge(search_page)
        await search_page.wait_for_timeout(30000)

        try:
            await search_page.wait_for_selector("table.table tbody tr", timeout=30000)
        except TimeoutError:
            print({
                "Nome do Produto": product_name,
                "Medicamento de referência": "N/A",
                "Data de Publicação": "N/A"
            })
            await browser.close()
            return

        rows = search_page.locator("table.table tbody tr")
        total = await rows.count()
        target_row = None

        for i in range(total):
            row = rows.nth(i)
            tds = row.locator("td")
            if await tds.count() < 2:
                continue
            med = (await tds.nth(1).inner_text()).strip()
            if normalize_key(med) == normalize_key(ref_drug):
                target_row = row
                break

        if not target_row:
            print({
                "Nome do Produto": product_name,
                "Medicamento de referência": "N/A",
                "Data de Publicação": "N/A"
            })
            await browser.close()
            return

        detalhar = target_row.locator("a:has-text('DETALHAR')")
        detalhar_page = None

        try:
            async with context.expect_page() as new_page_info:
                await detalhar.first.click()
            detalhar_page = await new_page_info.value
            await detalhar_page.wait_for_load_state("domcontentloaded")
        except:
            await detalhar.first.click()
            await search_page.wait_for_load_state("domcontentloaded")
            detalhar_page = search_page

        await browser.handle_challenge(detalhar_page)
        await detalhar_page.wait_for_timeout(30000)

        extracted = await extract_detalhar_fields(detalhar_page)

        # ----------------- FINAL OUTPUT (Product Name added) -----------------
        final = {
            "Nome do Produto": product_name or "N/A",
            "Medicamento de referência": extracted.get("Nome Comercial", "N/A") or "N/A",
            "Data de Publicação": extracted.get("Data de Publicação", "N/A") or "N/A"
        }

        return final
        print(final)
        await browser.close()



#-------------------------------------------------------------------------------------------------------
# async def scrap_pack_insert_anvisa(url):
# #    url = "https://consultas.anvisa.gov.br/#/bulario/detalhe/618480?nomeProduto=vidaza"

#     async with async_playwright() as p:
#         browser = await p.chromium.launch(headless=False)  # True = headless
#         page = await browser.new_page()

#         print(f"Opening {url}")
#         await page.goto(url, timeout=30000)
#         await page.wait_for_load_state("networkidle")

#         # === Detalhe da Bula do Produto ===
#         await page.wait_for_selector("table.table.table-bordered")
#         product_rows = await page.query_selector_all("table.table.table-bordered tr")

#         detalhe_data = []
#         for row in product_rows:
#             cols = await row.query_selector_all("th, td")
#             values = [await col.inner_text() for col in cols]

#             # skip unwanted rows
#             if any("Bula Atual do Paciente" in v or "Bula Atual do Profissional" in v for v in values):
#                 continue

#             detalhe_data.append(values)

#         # === Histórico de Bulas do Produto (only first row) ===
#         historico_first_row = []
#         history_rows = await page.query_selector_all("table.ng-table tbody tr")
#         if history_rows:
#             first_data_row = history_rows[0]
#             cols = await first_data_row.query_selector_all("td, th")
#             historico_first_row = [await col.inner_text() for col in cols]

#         await browser.close()
#         return detalhe_data, historico_first_row

#-------------------------------------------------------------------------------------------------------------------
#---------------Actual Matrix update date and actual clone update dates extraction----------------------------------

# Get current year for filtering
curr_month = datetime.now().month

if ((int(curr_month) == 1) or (int(curr_month) == 2) or (int(curr_month) == 3)):
    CURRENT_YEAR = datetime.now().year
    CURRENT_YEAR_prev = CURRENT_YEAR - 1
else:
    CURRENT_YEAR = datetime.now().year
    CURRENT_YEAR_prev = CURRENT_YEAR


class AnvisaMedicinePetitionScraper:
    """
    ANVISA Medicine Petition Scraper - Optimized for Essential Data Extraction
    
    Extracts only the core petition fields and filters by current year.
    """
    
    def __init__(self, headless: bool = False, timeout: int = 30000):
        self.headless = headless
        self.timeout = timeout
        self.page = None
        self.browser = None
        self.context = None

    async def __aenter__(self):
        """Async context manager entry"""
        self.playwright = None
        self.browser = await StealthChromeAsync(headless=self.headless).start()
        self.context = self.browser.context
        self.page = await self.browser.new_page()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    def clean_text(self, text: str) -> str:
        """Clean and normalize text content"""
        if not text:
            return ""
        return re.sub(r'\s+', ' ', text.strip())

    def extract_year_from_date(self, date_str: str) -> Optional[int]:
        """Extract year from DD/MM/YYYY date string"""
        try:
            if not date_str:
                return None
            year_match = re.search(r'\d{2}/\d{2}/(\d{4})', date_str)
            if year_match:
                return int(year_match.group(1))
            return None
        except Exception as e:
            logger.error(f"Error extracting year from '{date_str}': {e}")
            return None

    async def wait_for_page_load(self, additional_wait: int = 3) -> None:
        """Wait for page to fully load"""
        try:
            await self.page.wait_for_selector('.ng-scope', timeout=self.timeout)
            await self.page.wait_for_selector('form.ng-scope', timeout=self.timeout)
            try:
                await self.page.wait_for_selector('.dw-loading', state='hidden', timeout=5000)
            except PlaywrightTimeoutError:
                pass
            await asyncio.sleep(additional_wait)
        except PlaywrightTimeoutError:
            logger.warning(f"Page load timeout after {self.timeout}ms - proceeding anyway")

    async def find_and_click_process_link(self) -> Optional[str]:
        """Find and click the 'Número do Processo' link"""
        try:
            logger.info("Looking for 'Número do Processo' link...")
            
            selectors = [
                "th:has-text('Número do Processo') + td a",
                "td:has-text('Processo') a",
                "a[href*='documentos/tecnicos']",
                "table a[href*='#/']"
            ]
            
            process_number = None
            process_link = None
            
            for selector in selectors:
                try:
                    elements = await self.page.locator(selector).all()
                    if elements:
                        process_link = elements[0]
                        process_number = (await process_link.inner_text()).strip()
                        logger.info(f"Found Process Link: {process_number}")
                        break
                except Exception:
                    continue
            
            if not process_link:
                logger.warning("No 'Número do Processo' link found")
                return None
            
            logger.info(f"Clicking process link for: {process_number}")
            
            async with self.context.expect_page() as new_page_info:
                await process_link.click()
                
            process_page = await new_page_info.value
            await process_page.wait_for_load_state("domcontentloaded", timeout=30000)
            await self.browser.handle_challenge(process_page)
            
            self.page = process_page
            logger.info(f"Successfully opened process page: {process_page.url}")
            
            await self.wait_for_page_load()
            return process_number
            
        except Exception as e:
            logger.error(f"Error finding/clicking process link: {e}")
            return None

    async def extract_petition_from_row(self, row) -> Optional[Dict[str, str]]:
        """Extract petition data from a single table row"""
        try:
            petition_data = {}
            
            # Find all divs with col-sm-* classes that contain label/value pairs
            columns = await row.query_selector_all('.col-sm-3, .col-sm-6')
            
            for col in columns:
                try:
                    label_elem = await col.query_selector('label')
                    if not label_elem:
                        continue
                        
                    label_text = self.clean_text(await label_elem.inner_text())
                    
                    # Find the value div (next sibling or child div)
                    value_elem = await col.query_selector('div:not(.btn):not(.text-right)')
                    if not value_elem:
                        continue
                        
                    value_text = self.clean_text(await value_elem.inner_text())
                    
                    # Map labels to our standardized field names
                    if 'Expediente' in label_text and 'Data' not in label_text:
                        petition_data['Expediente'] = value_text
                    elif 'Data do Expediente' in label_text:
                        petition_data['Data do Expediente'] = value_text
                    elif 'Protocolo' in label_text:
                        petition_data['Nº do Protocolo'] = value_text
                    elif 'Situação atual' in label_text:
                        petition_data['Situação atual'] = value_text
                    elif 'Assunto' in label_text:
                        petition_data['Assunto'] = value_text
                    elif 'Publicação' in label_text:
                        petition_data['Dados da Publicação'] = value_text
                    elif 'Encontra-se na' in label_text:
                        # Extract location and date
                        lines = value_text.split('\n')
                        if lines:
                            location = self.clean_text(lines[0])
                            since_info = ""
                            
                            # Look for "Desde" information
                            for line in lines[1:]:
                                if 'Desde' in line:
                                    since_match = re.search(r'Desde\s*(\d{2}/\d{2}/\d{4})', line)
                                    if since_match:
                                        since_info = f" **Desde** {since_match.group(1)}"
                                elif 'Enc.' in line:
                                    enc_match = re.search(r'Enc\.\s*(\d{2}/\d{2}/\d{4})', line)
                                    if enc_match:
                                        since_info = f" **Enc.** {enc_match.group(1)}"
                            
                            petition_data['Encontra-se na'] = location + since_info
                        
                except Exception as e:
                    logger.error(f"Error extracting from column: {e}")
                    continue
            
            # Check if we have essential data and if it's from current year
            if not petition_data.get('Expediente') and not petition_data.get('Nº do Protocolo'):
                return None
                
            # Filter by current year
            data_expediente = petition_data.get('Data do Expediente', '')
            if data_expediente:
                year = self.extract_year_from_date(data_expediente)
                if (year) and ((year != CURRENT_YEAR) and (year != CURRENT_YEAR_prev)):
                    logger.debug(f"Skipping petition from {year} (not current year {CURRENT_YEAR})")
                    return None
            
            return petition_data
            
        except Exception as e:
            logger.error(f"Error extracting petition from row: {e}")
            return None

    async def extract_all_petitions(self) -> List[Dict[str, str]]:
        """Extract all petitions from the process page"""
        petitions = []
        
        try:
            logger.info("Looking for petitions table...")
            
            # Look for the petitions table
            table_selector = 'table.table-static.table-bordered.table-striped'
            table = await self.page.query_selector(table_selector)
            
            if not table:
                logger.warning("Petitions table not found")
                return petitions
            
            # Get all petition rows (skip header row)
            rows = await table.query_selector_all('tr')
            data_rows = rows[1:] if len(rows) > 1 else rows
            
            logger.info(f"Found {len(data_rows)} petition rows to process")
            
            for i, row in enumerate(data_rows):
                try:
                    logger.info(f"Processing petition {i+1}/{len(data_rows)}")
                    
                    petition_data = await self.extract_petition_from_row(row)
                    
                    if petition_data:
                        petitions.append(petition_data)
                        expediente = petition_data.get('Expediente', 'N/A')
                        logger.info(f"Extracted petition: {expediente}")
                    else:
                        logger.debug(f"Skipped petition row {i+1}")
                        
                except Exception as e:
                    logger.error(f"Error processing petition row {i+1}: {e}")
                    continue
            
            logger.info(f"Successfully extracted {len(petitions)} petitions from current year ({CURRENT_YEAR})")
            
        except Exception as e:
            logger.error(f"Error extracting petitions: {e}")
        
        return petitions

    async def scrape_medicine_page(self, url: str) -> List[Dict[str, str]]:
        """Main scraping method"""
        try:
            logger.info(f"Starting scrape of medicine page: {url}")
            
            # Load the medicine page
            logger.info("Loading medicine page...")
            await self.browser.goto(self.page, url, wait_until='networkidle', timeout=self.timeout)
            await self.wait_for_page_load()
            
            # Find and click process link
            process_number = await self.find_and_click_process_link()
            if not process_number:
                logger.error("Could not find or click process link")
                return []
            
            # Extract petitions from process page
            petitions = await self.extract_all_petitions()
            
            logger.info(f"Successfully extracted {len(petitions)} petitions")
            return petitions
            
        except Exception as e:
            logger.error(f"Error in main scraping process: {e}")
            raise

    async def save_to_excel(self, petitions: List[Dict[str, str]], filename: Optional[str] = None) -> str:
        """Save petition data to Excel file"""
        try:
            if not filename:
                filename = f"matrix_update_dates_{CURRENT_YEAR}.xlsx"
            
            if not petitions:
                logger.warning("No petitions to save")
                # Create empty file with headers
                df = pd.DataFrame(columns=[
                    "Expediente", "Data do Expediente", "Nº do Protocolo", 
                    "Situação atual", "Assunto", "Dados da Publicação", "Encontra-se na"
                ])
            else:
                df = pd.DataFrame(petitions)
            
            # df.to_excel(filename, index=False, engine='openpyxl')
            # logger.info(f"Saved {len(df)} petition records to {filename}")
            
            return df
            
        except Exception as e:
            logger.error(f"Error saving to Excel: {e}")
            raise

async def actual_matrix_clone_update_date(url):
    """Main execution function"""
#    medicine_url = "https://consultas.anvisa.gov.br/#/medicamentos/1144493"
    
    try:
        async with AnvisaMedicinePetitionScraper(headless=False, timeout=30000) as scraper:
            
            print("=" * 60)
            print("ANVISA Medicine Petition Scraper")
            print(f"Filtering for current year: {CURRENT_YEAR}")
            print("=" * 60)
            
            # Scrape petitions
            petitions = await scraper.scrape_medicine_page(url)
            
            print(petitions)
            
            # Save to Excel
            df = await scraper.save_to_excel(petitions)
            
            actual_matrix_update_date = ""
            
            print("df = {}".format(df))
            
            other_matrix_changes_list = []
            
            for idx in range(df.shape[0]):
                
                print(idx, str(df.loc[idx, "Assunto"]))
                
                if "HMP" in str(df.loc[idx, "Assunto"]):
                    actual_matrix_update_date = str(df.loc[idx, "Data do Expediente"]).replace("?", "")
                    
                else:
                    other_matrix_change_date = str(df.loc[idx, "Data do Expediente"]).replace("?", "")
                    other_matrix_changes_list.append({"date": other_matrix_change_date, "subject": str(df.loc[idx, "Assunto"])})
                  
#            other_matrix_change_dates_set = set(other_matrix_changes_list)
            
            df = pd.DataFrame(other_matrix_changes_list)
            other_matrix_changes_unique_list = df.drop_duplicates().to_dict(orient="records")

            return actual_matrix_update_date, other_matrix_changes_unique_list
                
            
            # Display results
            # print("\n" + "=" * 60)
            # print("SCRAPING RESULTS")
            # print("=" * 60)
            # print(f"Source URL: {url}")
            # print(f"Output File: {output_file}")
            # print(f"Total Petitions: {len(petitions)}")
            # print(f"Year Filter: {CURRENT_YEAR}")
            # print("=" * 60)
            
            # # Display petition summary in requested format
            # if petitions:
            #     print(f"\nPETITIONS FROM {CURRENT_YEAR}:")
            #     print("-" * 80)
            #     for i, petition in enumerate(petitions, 1):
            #         print(f"\nPETITION {i}:")
            #         print(f"**Expediente**")
            #         print(petition.get('Expediente', 'N/A'))
            #         print(f"**Data do Expediente**")
            #         print(petition.get('Data do Expediente', 'N/A'))
            #         print(f"**Nº do Protocolo**")
            #         print(petition.get('Nº do Protocolo', 'N/A'))
            #         print(f"**Situação atual**")
            #         print(petition.get('Situação atual', 'N/A'))
            #         print(f"**Assunto**")
            #         print(petition.get('Assunto', 'N/A'))
            #         print(f"**Dados da Publicação**")
            #         print(petition.get('Dados da Publicação', 'N/A'))
            #         print(f"**Encontra-se na**")
            #         print(petition.get('Encontra-se na', 'N/A'))
            #         print("-" * 80)
            # else:
            #     print(f"\nNo petitions found from {CURRENT_YEAR}")
                
            print("\nScraping completed successfully for matrix/clone update date!")
            
    except Exception as e:
        logger.error(f"Fatal error in main execution: {e}")
        print(f"\nScraping of matrix/clone update date failed: {e}")
        print("Check the logs above for detailed error information.")


#--------------------------------------------------------------------------------------------------------------------
#anvisa_url_list = ["https://consultas.anvisa.gov.br/#/medicamentos/2269655?cnpj=03978166000175", "https://consultas.anvisa.gov.br/#/medicamentos/590869?cnpj=03978166000175", "https://consultas.anvisa.gov.br/#/medicamentos/1100510", "https://consultas.anvisa.gov.br/#/medicamentos/1100227", "https://consultas.anvisa.gov.br/#/medicamentos/1242474", "https://consultas.anvisa.gov.br/#/medicamentos/1242474?cnpj=03978166000175&situacaoRegistro=V"]


#anvisa_url_list = ["https://consultas.anvisa.gov.br/#/medicamentos/2269655?cnpj=03978166000175", "https://consultas.anvisa.gov.br/#/medicamentos/1242474?cnpj=03978166000175&nomeProduto=DANPEZIL&situacaoRegistro=V", "https://consultas.anvisa.gov.br/#/medicamentos/511603?cnpj=03978166000175", "https://consultas.anvisa.gov.br/#/medicamentos/1100510?cnpj=03978166000175", "https://consultas.anvisa.gov.br/#/medicamentos/1403916"]


#anvisa_url_list = ["https://consultas.anvisa.gov.br/#/medicamentos/1231075?cnpj=03978166000175&nomeProduto=Metoprolol", "https://consultas.anvisa.gov.br/#/medicamentos/1145622?cnpj=03978166000175&nomeProduto=Bortezomib"]

#---------------Get all active medicine links from Ansiva-------------------------------------------------

from playwright.sync_api import sync_playwright, TimeoutError
import time, csv, os, re, urllib.parse, xlsxwriter

START_URL = "https://consultas.anvisa.gov.br/#/medicamentos/q/?cnpj=03978166000175&situacaoRegistro=V"

PER_PAGE_50 = '.ng-table-counts button:has-text("50")'
PAGE_NUMBER_LINK = 'ul.pagination a:has-text("{}")'
NEXT_ARROW = 'ul.pagination a:has-text("»")'
ACTIVE_PAGE_LOCATOR = 'ul.pagination li.active a, ul.pagination li.active span'
ROW_SELECTOR = "tbody tr[ng-repeat], tbody tr.ng-scope"

OUT_EXCEL = "medicine_links.xlsx"

SHORT_WAIT = 0.6
NAV_POLL = 0.4
NAV_TRIES = 12
BACK_WAIT = 1.0
ROW_WAIT_TIMEOUT = 20  # seconds
MAX_ADVANCES = 200
LONG_WAIT = 1.2

# JS helpers
INVOKE_DETAIL_JS = """(idx) => { try { const rows = document.querySelectorAll('tbody tr[ng-repeat], tbody tr.ng-scope'); if(!rows||idx<0||idx>=rows.length) return {status:'row-missing'}; const el=rows[idx]; if(window.angular&&window.angular.element){const ngEl=window.angular.element(el); const scope = ngEl.scope() || (ngEl.isolateScope && ngEl.isolateScope()); if(scope && typeof scope.detail === 'function'){ try{ scope.$apply(function(){ scope.detail(scope.produto); }); return {status:'invoked'}; }catch(e){return {status:'invoked-except', error:String(e)} } } } const clickable = el.querySelector('td[ng-click^=\"detail\"], td[ng-click]'); try{ if(clickable){ clickable.click(); return {status:'clicked-cell'} } el.click(); return {status:'clicked-row'} }catch(e){ try{ el.dispatchEvent(new MouseEvent('click',{bubbles:true})); return {status:'dispatched'} }catch(e2){} return {status:'no-action', error:String(e)} } }catch(e){ return {status:'error', error:String(e)} } }"""

READ_SCOPE_JS = """(idx) => { try { const rows = document.querySelectorAll('tbody tr[ng-repeat], tbody tr.ng-scope'); if(!rows||idx<0||idx>=rows.length) return null; const el = rows[idx]; if(!(window.angular && window.angular.element)) return null; const ngEl = window.angular.element(el); const s = ngEl.scope() || (ngEl.isolateScope && ngEl.isolateScope()); if(!s || !s.produto) return null; const p = s.produto; const out = {}; out.produto_id = p.produto && (p.produto.id || p.produto.codigo || p.produto.codigoExterno) || null; out.processo_numero = p.processo && (p.processo.numero || p.processo.id) || null; out.nome = p.produto && (p.produto.nome || p.produto.nomeComercial) || (p.nome || null); return out; } catch(e) { return null; } }"""

def debug(msg): print("[DEBUG]", msg)

def wait_for_render(page, timeout=8000):
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except TimeoutError:
        pass
    time.sleep(SHORT_WAIT)

def try_click_selector(page, selector, timeout=30000):
    try:
        page.wait_for_selector(selector, timeout=timeout)
        el = page.query_selector(selector)
        if not el:
            return False
        box = el.bounding_box()
        if box is None:
            return False
        el.click(timeout=5000)
        return True
    except Exception:
        return False

def get_active_page(page):
    try:
        el = page.query_selector(ACTIVE_PAGE_LOCATOR)
        if not el: return None
        txt = (el.text_content() or "").strip()
        return int(txt)
    except Exception:
        return None

def normalize_link(link, listing_url):
    if not link: return ""
    link = link.strip()
    m = re.match(r'(https?://[^/]+)#(.*)', link)
    if m: return m.group(1) + "/#" + m.group(2)
    if link.startswith("#"):
        base = listing_url.split("#")[0].rstrip("/")
        if link.startswith("#/"): return base + "/" + link
        return base + "/#" + link.lstrip("#")
    if link.startswith("/"):
        base = listing_url.split("#")[0].rstrip("/")
        return base + link
    base = listing_url.split("#")[0].rstrip("/")
    return urllib.parse.urljoin(base + "/", link)

def is_valid_detail_url(listing_url, candidate_url):
    if not candidate_url: return False
    if candidate_url == listing_url: return False
    if "#/medicamentos/" in candidate_url:
        after = candidate_url.split("#/medicamentos/",1)[1]
        if after and not after.startswith("?") and len(after.strip())>0: return True
    if "/medicamentos/" in candidate_url:
        after = candidate_url.split("/medicamentos/",1)[1]
        if after and not after.startswith("?") and len(after.strip())>0: return True
    return False

def wait_for_navigation_or_modal(page, listing_url, attempts=12, poll=0.4):
    for _ in range(attempts):
        cur = page.url
        if is_valid_detail_url(listing_url, cur):
            return normalize_link(cur, listing_url), False
        try:
            a = page.query_selector('a[href*="/#/medicamentos/"], a[href*="/medicamentos/"], .modal a[href]')
            if a:
                href = a.get_attribute("href") or ""
                href = normalize_link(href, listing_url)
                if is_valid_detail_url(listing_url, href):
                    return href, True
        except Exception:
            pass
        time.sleep(poll)
    return "", False

def construct_fallback_from_scope(listing_url, scope_info):
    if not scope_info: return ""
    proc = scope_info.get("processo_numero")
    pid = scope_info.get("produto_id")
    for c in (proc, pid):
        if c:
            base = listing_url.split("#")[0].rstrip("/")
            return f"{base}#/medicamentos/{urllib.parse.quote(str(c))}"
    return ""

def extract_modal_text_fallback(page, listing_url):
    try:
        body = page.query_selector(".modal .modal-body")
        text = body.text_content() if body else ""
        if not text:
            el = page.query_selector(".panel-body, .modal-content, .container")
            text = el.text_content() if el else ""
        if not text: return ""
        m = re.search(r'(\d{6,})', text)
        if m:
            base = listing_url.split("#")[0].rstrip("/")
            return f"{base}#/medicamentos/{urllib.parse.quote(m.group(1))}"
    except Exception: pass
    return ""

def ensure_on_page(page, target_page, timeout_s=8):
    start = time.time()
    while time.time() - start < timeout_s:
        active = get_active_page(page)
        if active == target_page: return True
        sel = PAGE_NUMBER_LINK.format(target_page)
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                try: el.click()
                except: page.evaluate("(el)=>el.click()", el)
                time.sleep(0.6)
                continue
        except Exception: pass
        # small wait, then try again
        time.sleep(0.4)
    return get_active_page(page) == target_page

def return_and_wait_rows(page, expected_count, timeout_s=15):
    try:
        page.evaluate("window.history.back()")
    except Exception:
        try: page.go_back()
        except: pass
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            rows = page.query_selector_all(ROW_SELECTOR)
            if rows and (len(rows) >= expected_count or len(rows) > 0):
                time.sleep(0.6)
                return True
        except Exception: pass
        time.sleep(0.4)
    return False

def extract_link_for_row_with_persistence(page, current_page, idx):
    # This helper does not click the "50" control itself - it expects the caller to manage that
    ok = ensure_on_page(page, current_page, timeout_s=8)
    if not ok:
        debug(f"Could not ensure page {current_page} before row {idx+1}, proceeding anyway.")

    listing_url = page.url
    try:
        res = page.evaluate(INVOKE_DETAIL_JS, idx)
    except Exception as e:
        res = {"status":"evaluate-error", "error": str(e)}
    captured, was_modal = wait_for_navigation_or_modal(page, listing_url, attempts=12)
    link = ""
    if captured and is_valid_detail_url(listing_url, captured):
        link = captured
    else:
        try:
            scope_info = page.evaluate(READ_SCOPE_JS, idx)
        except Exception:
            scope_info = None
        link = construct_fallback_from_scope(listing_url, scope_info)
        if not link:
            link = extract_modal_text_fallback(page, listing_url)
        if not link:
            try:
                rows = page.query_selector_all(ROW_SELECTOR)
                if idx < len(rows):
                    row = rows[idx]
                    tds = row.query_selector_all("td")
                    if len(tds) >= 2:
                        try: tds[1].click()
                        except: page.evaluate("(el)=>el.click()", tds[1])
                        captured2, was_modal2 = wait_for_navigation_or_modal(page, listing_url, attempts=8)
                        if captured2 and is_valid_detail_url(listing_url, captured2):
                            link = captured2
            except Exception: pass

    if link:
        link = normalize_link(link, listing_url)

    returned = return_and_wait_rows(page, expected_count=1, timeout_s=15)
    ensure_on_page(page, current_page, timeout_s=6)
    return link or ""

# --- NEW function: click 50 helper that uses try_click_selector (keeps original style) ---
def click_50_once(page):
    try:
        return try_click_selector(page, PER_PAGE_50)
    except Exception:
        return False

def create_medicine_links():
    if os.path.exists(OUT_EXCEL): os.remove(OUT_EXCEL)
    links = set()
    with StealthChromeSync() as browser:
        page = browser.new_page()
        print("Opening:", START_URL)
        page.goto(START_URL, wait_until="domcontentloaded")
        browser.handle_challenge(page)
        wait_for_render(page)

        # Click 50-per-page once initially (if available)
        print("Attempting to click '50' per-page button...")
        clicked_50 = click_50_once(page)
        if clicked_50:
            print("Clicked 50 — waiting for render.")
            wait_for_render(page)
        else:
            print("Could not click 50-per-page (continuing).")

        current = get_active_page(page)
        if current is None:
            try_click_selector(page, PAGE_NUMBER_LINK.format(1))
            wait_for_render(page)
            current = get_active_page(page)
        if current is None:
            current = 1

        safety = 0
        while safety < MAX_ADVANCES:
            print(f"\nProcessing page {current} ...")
            wait_for_render(page)
            rows = page.query_selector_all(ROW_SELECTOR)
            total_rows = len(rows)
            print(f"Found {total_rows} rows on page {current}.")

            # 1) Extract first 10 medicines normally (indexes 0..9)
            first_block = min(10, total_rows)
            print(f"Extracting first {first_block} medicines (normal flow)...")
            for i in range(first_block):
                try:
                    link = extract_link_for_row_with_persistence(page, current, i)
                except Exception as e:
                    print(f"  row#{i+1}: extraction error: {e}")
                    link = ""
                if link:
                    if link not in links:
                        links.add(link)
                        print(f"  row#{i+1}: link found -> {link}")
                    else:
                        print(f"  row#{i+1}: duplicate link -> {link}")
                else:
                    print(f"  row#{i+1}: no link captured")

            # 2) Extract medicines 11..50 with click-50-before-each (indexes 10..49)
            print("Now extracting medicines 11..50 by clicking '50' before each extraction...")
            # Re-query rows (because DOM may have changed)
            rows_after = page.query_selector_all(ROW_SELECTOR)
            total_after = len(rows_after)
            # maximum index we should attempt is min(total_after, 50) - 1
            max_index = min(total_after, 50) - 1
            if max_index < 10:
                print("No medicines in 11..50 range on this page. Skipping to next page.")
            else:
                for idx in range(10, max_index + 1):
                    print(f" Preparing to extract index {idx+1} (click 50 first).")
                    # ensure on correct page
                    ensure_on_page(page, current, timeout_s=6)
                    # click 50 before extraction
                    ok50 = click_50_once(page)
                    if not ok50:
                        print("  Warning: could not click 50 control before extraction; proceeding anyway.")
                    # wait rows
                    if not return_and_wait_rows(page, expected_count=1, timeout_s=8):
                        # attempt to re-wait rows more generally
                        if not page.query_selector_all(ROW_SELECTOR):
                            print("  Rows not present after clicking 50; breaking 11..50 loop.")
                            break
                    # re-get rows list
                    rows_now = page.query_selector_all(ROW_SELECTOR)
                    if idx >= len(rows_now):
                        print(f"  index {idx+1} not present after clicking 50 (found {len(rows_now)} rows). Stopping 11..50 loop.")
                        break
                    # scroll into view
                    try:
                        page.evaluate("(el)=>el.scrollIntoView({block:'center'})", rows_now[idx])
                        time.sleep(0.25)
                    except Exception:
                        pass

                    try:
                        link = extract_link_for_row_with_persistence(page, current, idx)
                    except Exception as e:
                        print(f"  row#{idx+1}: extraction error: {e}")
                        link = ""

                    # after return, re-click 50 to maintain state for next extraction
                    try:
                        click_50_once(page)
                        time.sleep(0.4)
                    except Exception:
                        pass

                    if link:
                        if link not in links:
                            links.add(link)
                            print(f"  row#{idx+1}: link found -> {link}")
                        else:
                            print(f"  row#{idx+1}: duplicate link -> {link}")
                    else:
                        print(f"  row#{idx+1}: no link captured")

            # Advance to next page (numeric then next arrow fallback)
            next_page = current + 1
            print(f"Attempting to advance to page {next_page} ... (current {current})")
            clicked = try_click_selector(page, PAGE_NUMBER_LINK.format(next_page))
            if clicked:
                wait_for_render(page)
                new_active = get_active_page(page)
                if new_active is None or new_active == current:
                    time.sleep(LONG_WAIT)
                    new_active = get_active_page(page)
                if new_active == next_page:
                    current = new_active
                    safety += 1
                    continue
                else:
                    print(f"Clicked numeric link but active page not {next_page} (active={new_active}). Stopping.")
                    break

            print("Numeric next not available/clickable — trying Next arrow (»).")
            clicked_next = try_click_selector(page, NEXT_ARROW)
            if clicked_next:
                wait_for_render(page)
                new_active = get_active_page(page)
                if new_active is None or new_active == current:
                    time.sleep(LONG_WAIT)
                    new_active = get_active_page(page)
                if new_active and new_active > current:
                    current = new_active
                    safety += 1
                    continue
                else:
                    print(f"Clicked Next but didn't advance (active={new_active}). Stopping.")
                    break
            else:
                print("No way to advance further (Next arrow not clickable). Ending.")
                break

        # save CSV
        print(f"\nCollected {len(links)} unique links. Writing to {OUT_EXCEL} ...")
        
        with xlsxwriter.Workbook(OUT_EXCEL) as workbook:
            worksheet = workbook.add_worksheet()
        
            # Write header
            worksheet.write(0, 0, "Medicine Anvisa URL")
        
            # Write rows
            row = 1
            for l in sorted(links):
                worksheet.write(row, 0, l)
                row += 1        
        
        
        # with open(OUT_EXCEL, "w", newline="", encoding="utf-8") as f:
        #     writer = xlsxwriter.Workbook(f)
        #     writer.writerow(["Medicine Anvisa URL"])
        #     for l in sorted(links):
        #         writer.writerow([l])

        print("Done.")
        browser.close()


#-----------------------------------------------------------------------------------------------
#Get all the active GMP links from Anvisa-------------------------------------------------------

# scraping_links_final_v4.py
# Robust scraper for ANVISA "Certificados de Boas PrÃ¡ticas (medicamento)" listing.
# v4 â strict anchor detection, modal-first logic, pre-click angular-scope read
# URL construction preserves the listing fragment query and ensures '/#/' path
# Usage: python scraping_links_final_v4.py

from playwright.sync_api import sync_playwright, TimeoutError
import time, csv, os, re, urllib.parse

# -------------------------
# Configuration
# -------------------------
# START_URL_CBPDA = (
#     "https://consultas.anvisa.gov.br/#/certificadosdeboaspraticas-medicamento/c/?cnpjSolicitante=03978166000175&tipoCertificado=2"
# )


# START_URL_CBPF = (
#     "https://consultas.anvisa.gov.br/#/certificadosdeboaspraticas-medicamento/c/?cnpjSolicitante=03978166000175&tipoCertificado=1&internacional=true&status=0"
# )

PER_PAGE_50 = '.ng-table-counts button:has-text("50")'
PAGE_NUMBER_LINK = 'ul.pagination a:has-text("{}")'
NEXT_ARROW = 'ul.pagination a:has-text("Â»")'
ACTIVE_PAGE_LOCATOR = 'ul.pagination li.active a, ul.pagination li.active span'
ROW_SELECTOR = "tbody tr.linha_certificado.ng-scope, tbody tr[ng-repeat], tbody tr.ng-scope"

#OUT_CSV = "gmp_CBPDA_anvisa_list.csv"

SHORT_WAIT = 0.6
NAV_POLL = 0.35
NAV_TRIES = 18
BACK_WAIT = 0.8
ROW_WAIT_TIMEOUT = 18
MAX_ADVANCES = 200
LONG_WAIT = 1.2

# -------------------------
# JS snippets executed in page
# -------------------------
INVOKE_DETAIL_JS = """(idx) => {
  try {
    const rows = document.querySelectorAll('tbody tr.linha_certificado.ng-scope, tbody tr[ng-repeat], tbody tr.ng-scope');
    if(!rows || idx<0 || idx>=rows.length) return {status:'row-missing'};
    const el = rows[idx];
    if(window.angular && window.angular.element){
      const ngEl = window.angular.element(el);
      const scope = ngEl.scope() || (ngEl.isolateScope && ngEl.isolateScope());
      if(scope && typeof scope.detail === 'function'){
        try {
          scope.$apply(function(){ scope.detail(scope.produto || scope.empresa || scope.item || scope); });
          return {status:'invoked'};
        } catch(e){ return {status:'invoked-except', error:String(e)} }
      }
    }
    const clickable = el.querySelector('td[ng-click^="detail"], td[ng-click]');
    try {
      if(clickable){ clickable.click(); return {status:'clicked-cell'} }
      el.click();
      return {status:'clicked-row'};
    } catch(e) {
      try { el.dispatchEvent(new MouseEvent('click',{bubbles:true})); return {status:'dispatched'} } catch(e2){}
      return {status:'no-action', error:String(e)};
    }
  } catch(e) { return {status:'error', error:String(e)} }
}"""

READ_SCOPE_JS = """(idx) => {
  try {
    const rows = document.querySelectorAll('tbody tr.linha_certificado.ng-scope, tbody tr[ng-repeat], tbody tr.ng-scope');
    if(!rows || idx<0 || idx>=rows.length) return null;
    const el = rows[idx];
    if(!(window.angular && window.angular.element)) return null;
    const ngEl = window.angular.element(el);
    const s = ngEl.scope() || (ngEl.isolateScope && ngEl.isolateScope());
    if(!s) return null;
    const out = {};
    const cand = (obj, keys) => {
      for(const k of keys){ if(obj && (k in obj) && obj[k] != null) return obj[k]; }
      return null;
    };
    const empresa = s.empresa || s.produto || s.item || s;
    out.idCertificado = cand(empresa, ['idCertificado','id','codigo','codigoCertificado','numero','numeroCertificado','nCertificado','certificadoId','id_certificado']);
    out.produto_id = cand(s.produto || {}, ['id','codigo','codigoExterno','codigoProduto']);
    out.processo_numero = cand(s.processo || {}, ['numero','id']);
    out.nome = cand(empresa, ['nome','nomeComercial','descricao']);
    out._keys = Object.keys(empresa||{}).slice(0,50);
    return out;
  } catch(e) { return null; }
}"""

# -------------------------
# Helpers
# -------------------------
def debug(msg):
    print("[DEBUG]", msg)

def wait_for_render(page, timeout=8000):
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except TimeoutError:
        pass
    time.sleep(SHORT_WAIT)

def try_click_selector(page, selector, timeout=30000):
    try:
        page.wait_for_selector(selector, timeout=timeout)
        el = page.query_selector(selector)
        if not el:
            return False
        box = el.bounding_box()
        if box is None:
            page.evaluate("(sel)=>{ const e=document.querySelector(sel); if(e) e.click(); }", selector)
            return True
        el.click(timeout=5000)
        return True
    except Exception as e:
        debug(f"try_click_selector({selector}) failed: {e}")
        try:
            page.evaluate("(sel)=>{ const e=document.querySelector(sel); if(e) e.click(); }", selector)
            return True
        except Exception:
            return False

def _extract_fragment_query(listing_url):
    """Return the fragment's query string including the leading '?' if present."""
    try:
        if '#' not in listing_url:
            return ''
        frag = listing_url.split('#', 1)[1]
        if '?' in frag:
            return frag[frag.index('?'):]
    except Exception:
        pass
    return ''

def _build_cert_url(listing_url, cert_id):
    """Construct final URL in exact format required and preserve fragment query."""
    base = listing_url.split('#')[0].rstrip('/')
    frag_q = _extract_fragment_query(listing_url)
    # Ensure cert_id is string and no accidental quoting
    return f"{base}/#/certificadosdeboaspraticas-medicamento/{cert_id}/{frag_q}"

def normalize_link(link, listing_url):
    if not link:
        return ""
    link = link.strip()
    m = re.match(r'(https?://[^/]+)#(.*)', link)
    if m:
        return m.group(1) + "/#" + m.group(2)
    if link.startswith("#"):
        base = listing_url.split("#")[0].rstrip("/")
        if link.startswith("#/"):
            return base + "/" + link
        return base + "/#" + link.lstrip("#")
    if link.startswith("/"):
        base = listing_url.split("#")[0].rstrip("/")
        return base + link
    base = listing_url.split("#")[0].rstrip("/")
    return urllib.parse.urljoin(base + "/", link)

def wait_for_url_change(page, original_url, contains=None, attempts=NAV_TRIES, poll=NAV_POLL):
    for _ in range(attempts):
        try:
            cur = page.url
        except Exception:
            cur = ""
        if cur and cur != original_url:
            if contains:
                if contains in cur:
                    return cur
            else:
                return cur
        time.sleep(poll)
    return ""

# -------------------------
# Anchor / modal scanning
# -------------------------
def find_any_anchor_with_certificados(page, listing_url):
    """Scan whole page for anchors that look like certificados detail links."""
    try:
        anchors = page.query_selector_all("a[href*='certificadosdeboaspraticas-medicamento/'], a[href*='certificadosdeboaspraticas-medicamento?']")
        for a in anchors:
            href = (a.get_attribute("href") or "").strip()
            if not href:
                continue
            nh = normalize_link(href, listing_url)
            # require numeric id or numero param
            if re.search(r'/certificadosdeboaspraticas-medicamento/\d+', nh) or re.search(r'[?&](numero|id|nCertificado)=\d+', nh):
                debug(f"find_any_anchor: strict anchor -> {nh}")
                # ensure final form
                m = re.search(r'(\d{5,12})', nh)
                if m:
                    return _build_cert_url(listing_url, m.group(1))
        anchors2 = page.query_selector_all("a[href*='certificadosdeboaspraticas'], a[href*='/certificados']")
        for a in anchors2:
            href = (a.get_attribute("href") or "").strip()
            if not href:
                continue
            nh = normalize_link(href, listing_url)
            if re.search(r'\d{5,12}', nh):
                debug(f"find_any_anchor: broad anchor -> {nh}")
                m = re.search(r'(\d{5,12})', nh)
                if m:
                    return _build_cert_url(listing_url, m.group(1))
    except Exception as e:
        debug(f"find_any_anchor error: {e}")
    return ""

def wait_for_modal_anchor(page, listing_url, timeout=6.0):
    """Wait for modal and extract anchor or certificate number from modal text."""
    try:
        msel = ".modal-content, .modal, .ngdialog, .ui-dialog, .modal-body, .modal .modal-body"
        modal = page.wait_for_selector(msel, timeout=int(timeout*1000))
        if not modal:
            return ""
        try:
            a = modal.query_selector("a[href*='certificadosdeboaspraticas-medicamento/'], a[href*='certificadosdeboaspraticas-medicamento?']")
            if a:
                href = (a.get_attribute("href") or "").strip()
                nh = normalize_link(href, listing_url)
                m = re.search(r'(\d{5,12})', nh)
                if m:
                    debug(f"wait_for_modal_anchor: modal-anchor strict -> {nh}")
                    return _build_cert_url(listing_url, m.group(1))
            a2 = modal.query_selector_all("a[href]")
            for a3 in a2:
                href = (a3.get_attribute("href") or "").strip()
                if not href:
                    continue
                nh = normalize_link(href, listing_url)
                m = re.search(r'(\d{5,12})', nh)
                if m:
                    debug(f"wait_for_modal_anchor: modal-anchor-digit -> {nh}")
                    return _build_cert_url(listing_url, m.group(1))
        except Exception:
            pass
        text = modal.text_content() or ""
        m = re.search(r'(\d{5,12})', re.sub(r'[\.\-\s]', '', text))
        if m:
            debug(f"wait_for_modal_anchor: modal-text -> {m.group(1)}")
            return _build_cert_url(listing_url, m.group(1))
    except Exception as e:
        debug(f"wait_for_modal_anchor: {e}")
    return ""

def construct_certificado_url_from_scope(listing_url, scope_info):
    if not scope_info:
        return ""
    keys = ['idCertificado','id','codigo','codigoCertificado','numeroCertificado','nCertificado','certificadoId','numero','produto_id']
    for k in keys:
        v = scope_info.get(k) if isinstance(scope_info, dict) else None
        if v:
            return _build_cert_url(listing_url, v)
    try:
        s = str(scope_info)
        m = re.search(r'(\d{6,12})', s)
        if m:
            return _build_cert_url(listing_url, m.group(1))
    except Exception:
        pass
    return ""

# -------------------------
# Row extraction logic
# -------------------------
def return_and_wait_rows(page, expected_count=1, timeout_s=12):
    try:
        page.evaluate("window.history.back()")
    except Exception:
        try:
            page.go_back()
        except Exception:
            pass
    start = time.time()
    while time.time() - start < timeout_s:
        try:
            rows = page.query_selector_all(ROW_SELECTOR)
            if rows and (len(rows) >= expected_count or len(rows) > 0):
                time.sleep(0.45)
                return True
        except Exception:
            pass
        time.sleep(0.35)
    return False

def extract_link_for_row_certificados(page, listing_url, idx):
    """Certificados-specific extraction: tries pre-click scope -> nav -> modal -> scope fallback."""
    # pre-click scope (sometimes present before clicking)
    try:
        pre_scope = page.evaluate(READ_SCOPE_JS, idx)
        if pre_scope:
            debug(f"certificados: pre-click scope_info -> {pre_scope}")
            fallback = construct_certificado_url_from_scope(listing_url, pre_scope or {})
            if fallback:
                debug(f"certificados: pre-click scope-fallback -> {fallback}")
                return fallback
    except Exception as e:
        debug(f"certificados: pre-click scope eval error: {e}")

    # scroll into view
    try:
        page.evaluate("(idx)=>{ const rows=document.querySelectorAll('tbody tr.linha_certificado.ng-scope, tbody tr[ng-repeat], tbody tr.ng-scope'); if(rows && idx < rows.length) rows[idx].scrollIntoView({block:'center'}); }", idx)
    except Exception:
        pass

    rows = page.query_selector_all(ROW_SELECTOR)
    if idx >= len(rows):
        debug("certificados: index out of range")
        return ""

    row = rows[idx]
    # click the row
    try:
        try:
            row.click()
        except Exception:
            page.evaluate("(el)=>el.click()", row)
    except Exception as e:
        debug(f"certificados: row.click failed: {e}")
        return ""

    # small wait for DOM changes
    time.sleep(0.25)

    # 1) navigation detection
    new_url = wait_for_url_change(page, listing_url, contains="certificadosdeboaspraticas", attempts=12, poll=0.3)
    if new_url:
        debug(f"certificados: navigation detected -> {new_url}")
        try:
            page.go_back()
        except Exception:
            try:
                page.evaluate("window.history.back()")
            except Exception:
                pass
        m = re.search(r'(\d{5,12})', new_url)
        if m:
            return _build_cert_url(listing_url, m.group(1))
        return normalize_link(new_url, listing_url)

    # 2) modal anchor/text
    modal_href = wait_for_modal_anchor(page, listing_url, timeout=5.5)
    if modal_href:
        debug(f"certificados: modal anchor/text -> {modal_href}")
        try:
            close_btn = page.query_selector(".modal .close, .modal button.close, .modal .btn-close")
            if close_btn:
                try:
                    close_btn.click()
                except Exception:
                    page.evaluate("(el)=>el.click()", close_btn)
            else:
                page.keyboard.press("Escape")
        except Exception:
            pass
        try:
            return_and_wait_rows(page, expected_count=1, timeout_s=6)
        except Exception:
            pass
        return modal_href

    # 3) global anchor scan
    anchor = find_any_anchor_with_certificados(page, listing_url)
    if anchor:
        debug(f"certificados: global anchor found -> {anchor}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return anchor

    # 4) post-click scope fallback
    try:
        scope_info = page.evaluate(READ_SCOPE_JS, idx)
        debug(f"certificados: scope_info -> {scope_info}")
    except Exception as e:
        debug(f"certificados: scope eval error: {e}")
        scope_info = None
    fallback = construct_certificado_url_from_scope(listing_url, scope_info or {})
    if fallback:
        debug(f"certificados: scope-fallback -> {fallback}")
        try:
            page.evaluate("window.history.back()")
        except Exception:
            pass
        return fallback

    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    returned = return_and_wait_rows(page, expected_count=1, timeout_s=6)
    if not returned:
        debug("certificados: could not recover listing after click")
    return ""

def extract_link_for_row_generic(page, current_page, idx):
    listing_url = page.url
    rows = page.query_selector_all(ROW_SELECTOR)
    if idx >= len(rows):
        return ""

    if "certificadosdeboaspraticas" in listing_url or "certificados" in listing_url:
        return extract_link_for_row_certificados(page, listing_url, idx)

    # fallback for medicamentos
    try:
        page.evaluate("(idx)=>{ const rows=document.querySelectorAll('tbody tr[ng-repeat], tbody tr.ng-scope, tbody tr.linha_certificado'); if(rows && idx < rows.length) rows[idx].scrollIntoView({block:'center'}); }", idx)
    except Exception:
        pass
    try:
        page.evaluate(INVOKE_DETAIL_JS, idx)
    except Exception:
        pass
    new_url = wait_for_url_change(page, listing_url, contains="medicamentos", attempts=12, poll=0.35)
    if new_url:
        try:
            page.go_back()
        except Exception:
            try:
                page.evaluate("window.history.back()")
            except Exception:
                pass
        return normalize_link(new_url, listing_url)
    try:
        scope_info = page.evaluate(READ_SCOPE_JS, idx)
    except Exception:
        scope_info = None
    if scope_info and isinstance(scope_info, dict):
        proc = scope_info.get("processo_numero") or scope_info.get("produto_id") or scope_info.get("id")
        if proc:
            base = listing_url.split("#")[0].rstrip("/")
            frag_q = _extract_fragment_query(listing_url)
            return f"{base}/#/medicamentos/{urllib.parse.quote(str(proc))}/{frag_q}"
    return ""

# -------------------------
# Main flow
# -------------------------
def get_GMP_links(OUT_CSV, START_URL):
    if os.path.exists(OUT_CSV):
        os.remove(OUT_CSV)
    collected = set()

    with StealthChromeSync() as browser:
        page = browser.new_page()
        print("Opening:", START_URL)
        page.goto(START_URL, wait_until="domcontentloaded")
        browser.handle_challenge(page)
        wait_for_render(page)

        print("Attempting to click '50' per-page button...")
        if try_click_selector(page, PER_PAGE_50):
            print("Clicked 50 â waiting for rows/pagination to render.")
            wait_for_render(page)
        else:
            print("50-button not available or click failed â continuing without.")

        # determine current page
        current = None
        try:
            el = page.query_selector(ACTIVE_PAGE_LOCATOR)
            if el:
                txt = (el.text_content() or "").strip()
                current = int(txt) if txt.isdigit() else 1
        except Exception:
            current = 1
        if not current:
            current = 1

        safety = 0
        while safety < MAX_ADVANCES:
            print(f"--- PROCESSING LISTING PAGE {current} ---")
            wait_for_render(page)
            rows = page.query_selector_all(ROW_SELECTOR)
            total_rows = len(rows)
            print(f"Found {total_rows} rows on page {current}.")

            # 1) first 10 normal
            first_block = min(10, total_rows)
            print(f"Extracting first {first_block} rows...")
            for i in range(first_block):
                print(f" Processing row #{i+1} ...")
                try:
                    link = extract_link_for_row_generic(page, current, i)
                except Exception as e:
                    link = ""
                    debug(f"row#{i+1} exception: {e}")
                if link:
                    if link not in collected:
                        collected.add(link)
                        print(f"  row#{i+1}: found -> {link}")
                    else:
                        print(f"  row#{i+1}: duplicate -> {link}")
                else:
                    print(f"  row#{i+1}: no link captured")

            # 2) indexes 11..50 using click-50-before-each
            if total_rows > 10:
                print("Extracting indexes 11..50 with click-50-before-each...")
                rows_now = page.query_selector_all(ROW_SELECTOR)
                max_index = min(len(rows_now), 50) - 1
                for idx in range(10, max_index + 1):
                    print(f" Preparing index {idx+1} ... clicking 50 control then extracting")
                    try:
                        try_click_selector(page, PER_PAGE_50)
                    except Exception:
                        pass
                    time.sleep(0.35)
                    try:
                        link = extract_link_for_row_generic(page, current, idx)
                    except Exception as e:
                        link = ""
                        debug(f"index {idx+1} extract exception: {e}")
                    # re-click 50 to keep listing expanded
                    try:
                        try_click_selector(page, PER_PAGE_50)
                    except Exception:
                        pass
                    if link:
                        if link not in collected:
                            collected.add(link)
                            print(f"  row#{idx+1}: found -> {link}")
                        else:
                            print(f"  row#{idx+1}: duplicate -> {link}")
                    else:
                        print(f"  row#{idx+1}: no link captured")

            # Advance to next page
            next_page = current + 1
            print(f"Attempting to advance to page {next_page} ...")
            clicked = try_click_selector(page, PAGE_NUMBER_LINK.format(next_page))
            if clicked:
                wait_for_render(page)
                # recompute active page
                try:
                    el = page.query_selector(ACTIVE_PAGE_LOCATOR)
                    new_active = int((el.text_content() or "").strip()) if el and (el.text_content() or "").strip().isdigit() else None
                except Exception:
                    new_active = None
                if new_active is None or new_active == current:
                    time.sleep(LONG_WAIT)
                    try:
                        el = page.query_selector(ACTIVE_PAGE_LOCATOR)
                        new_active = int((el.text_content() or "").strip()) if el and (el.text_content() or "").strip().isdigit() else None
                    except Exception:
                        new_active = None
                if new_active == next_page:
                    current = new_active
                    safety += 1
                    continue
                else:
                    debug(f"Clicked numeric but active={new_active}")
            # fallback: click next arrow
            if try_click_selector(page, NEXT_ARROW):
                wait_for_render(page)
                try:
                    el = page.query_selector(ACTIVE_PAGE_LOCATOR)
                    new_active = int((el.text_content() or "").strip()) if el and (el.text_content() or "").strip().isdigit() else None
                except Exception:
                    new_active = None
                if new_active and new_active > current:
                    current = new_active
                    safety += 1
                    continue
                else:
                    print("Next arrow clicked but didn't advance. Ending.")
                    break
            print("No pagination advance possible â ending.")
            break

        # write CSV
        print(f"Collected {len(collected)} unique links. Writing to {OUT_CSV} ...")
        with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["GMP Anvisa URL"])
            for l in sorted(collected):
                writer.writerow([l])

        print("Done.")
        browser.close()

#if __name__ == "__main__":
    

#----------------------------------medicines links, lists etc.--------------------------------------------------------------

#med_links_file = "medicine_links.xlsx"
# prev_med_links_file = "medicine_links_prev.xlsx"

# if os.path.exists(med_links_file):
#     try:
#         os.rename(med_links_file, prev_med_links_file)
#         print(f"File has been successfully renamed.")
#     except OSError as e:
#         print(f"Error while renaming file: {e}")
# else:
#     print(f"File does not exist.")

# Create all active medicine links from Anvisa site----------------------------
#create_medicine_links()

# df_med_list = pd.read_excel("medicine_links.xlsx")
# #df_med_list_prev = pd.read_excel("medicine_links_prev.xlsx")

# med_links_list = list(df_med_list['Medicine Anvisa URL'])

# med_id_list = list(df_med_list['ID'])


#med_links_list_prev = list(df_med_list_prev['Medicine Anvisa URL'])

#new_medicines_found = list(set(med_links_list) - set(med_links_list_prev))

# if len(new_medicines_found) > 0:
    
#     with open("new_medicine_links.csv", "w", newline="", encoding="utf-8") as f:
#         writer = csv.writer(f)
#         # write header
#         writer.writerow(["Medicine Anvisa URL"])
#         # write list items
#         for item in new_medicines_found:
#             writer.writerow([item])
    

#anvisa_url_list = med_links_list

#-----------------------------------------------------------------------------------------------------------------------------

df_under_review_process_list = pd.read_excel("query_links_final.xlsx", dtype=str)

df_under_review_process_list['link'] = ""

for idx in range(df_under_review_process_list.shape[0]):
    df_under_review_process_list.loc[idx, 'link'] = "https://consultas.anvisa.gov.br/#/documentos/tecnicos/" + str(df_under_review_process_list.loc[idx, 'Process No.']) + "/"
    
anvisa_under_review_url_list = list(df_under_review_process_list['link'])
process_list = list(df_under_review_process_list['Process No.'])



#-----------------------------------------------------------------------------------------------------------------------------------    

print("Web-scraping Query data of under-process medicines from Anvisa ...............")

for idx, under_review_url in enumerate(anvisa_under_review_url_list):

    if idx > 0 and PAUSE_BETWEEN_ITEMS and PAUSE_BETWEEN_ITEMS[1] > 0:
        time.sleep(random.uniform(*PAUSE_BETWEEN_ITEMS))   # slower, human-like pace helps avoid rate limits

    process_no = process_list[idx]
    
    try:

        # query() swallows its own errors and returns None, which used to be dumped as "null" (or the run
        # looked like it saved nothing). Retry, and only write a file when there is real data.
        json_data_under_review = None
        for attempt in range(1, QUERY_RETRIES + 1):
            result_under_review = asyncio.run(query(under_review_url))
            if _query_has_data(result_under_review):
                json_data_under_review = result_under_review
                break
            print("⚠️ No usable data for process {} (attempt {}/{})".format(process_no, attempt, QUERY_RETRIES))

        if json_data_under_review is None:
            print("❌ Nothing saved for process {} - scraping returned no data: {}".format(process_no, under_review_url))
            continue
    
        under_process_file_name = "query_process_" + str(process_no) + ".json"
        
        os.makedirs("under_review_medicine_json_files", exist_ok=True)
        under_process_file_path = os.path.join("under_review_medicine_json_files", under_process_file_name)
        tmp_file_path = under_process_file_path + ".tmp"
    
        # write to a temp file first, then rename, so a crash never leaves a half-written/corrupt JSON
        with open(tmp_file_path, "w", encoding="utf-8") as f:
            json.dump(json_data_under_review, f, ensure_ascii=False, indent=4, default=str)
        os.replace(tmp_file_path, under_process_file_path)
        print("💾 Saved {}".format(os.path.abspath(under_process_file_path)))

    except Exception as ex:
        print("Errors found with the web-scraping of under process query data for this url: {}, Errors = {}".format(under_review_url, ex))    
