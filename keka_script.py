import os
import sys
import json
import time
import re
import base64
import email
import imaplib
import smtplib
import traceback
import urllib.request
import urllib.error
from io import BytesIO
from datetime import datetime
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from PIL import Image
import pytesseract
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from dotenv import load_dotenv

# load .env sitting next to this script, regardless of the working directory
# (matters for schedulers like launchd/cron that run with a different CWD)
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

# Dedicated Chrome profile keeps the Keka session cookie between runs,
# so most runs go straight to the dashboard and never hit the captcha.
CHROME_PROFILE_DIR = os.path.expanduser('~/.keka-chrome-profile')

class Keka:
    EMAIL = os.getenv('KEKA_EMAIL')
    PASSWORD = os.getenv('KEKA_PASSWORD')
    URL = os.getenv('KEKA_URL')
    CHECK = os.getenv('KEKA_CHECK', 'in')
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
    # free tier throttles heavily (503) / quota-caps (429); try these in order
    GEMINI_MODELS = [m.strip() for m in os.getenv(
        'GEMINI_MODELS',
        'gemini-2.0-flash,gemini-3.5-flash,gemini-flash-latest').split(',') if m.strip()]
    MAX_LOGIN_ATTEMPTS = 5

    # status-email settings (defaults to Gmail SMTP; SMTP_USER/SMTP_PASSWORD required to actually send)
    NOTIFY_EMAIL = os.getenv('NOTIFY_EMAIL', 'vansh@hudle.in')
    SMTP_HOST = os.getenv('SMTP_HOST', 'smtp.gmail.com')
    SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
    SMTP_USER = os.getenv('SMTP_USER')
    SMTP_PASSWORD = os.getenv('SMTP_PASSWORD')

    # 2FA OTP: the runner reads the Keka login OTP from an IMAP mailbox.
    # Defaults to the same Gmail as SMTP above (Gmail app passwords work for IMAP too).
    IMAP_HOST = os.getenv('IMAP_HOST', 'imap.gmail.com')
    IMAP_USER = os.getenv('IMAP_USER') or os.getenv('SMTP_USER')
    IMAP_PASSWORD = os.getenv('IMAP_PASSWORD') or os.getenv('SMTP_PASSWORD')
    OTP_SUBJECT = os.getenv('OTP_SUBJECT', 'One-Time Password')
    OTP_TIMEOUT = int(os.getenv('OTP_TIMEOUT', '150'))

    def wait_for_login_form(self, browser):
        # current Keka shows the form directly; older Keka needs the "keka password" chooser clicked first
        try:
            WebDriverWait(browser, 10).until(EC.element_to_be_clickable((By.ID, "email")))
        except TimeoutException:
            browser.find_element(By.XPATH, '//button/div/p[text()="keka password"]').click()
            WebDriverWait(browser, 10).until(EC.element_to_be_clickable((By.ID, "email")))

    def get_captcha_image(self, browser):
        # extract captcha image (id=imgCaptcha on current Keka, class=imgCaptcha on older) and flatten onto white
        captcha_element = browser.find_element(By.CSS_SELECTOR, 'img#imgCaptcha, img.imgCaptcha')
        captcha_image_url = captcha_element.get_attribute('src')
        captcha_image = Image.open(BytesIO(base64.b64decode(captcha_image_url.split(',')[1]))).convert('RGBA')
        white_background = Image.new('RGB', captcha_image.size, (255, 255, 255))
        white_background.paste(captcha_image, mask=captcha_image.split()[3])
        return white_background

    def _gemini_call(self, model, b64png):
        payload = {"contents": [{"parts": [
            {"inline_data": {"mime_type": "image/png", "data": b64png}},
            {"text": f"Read the distorted captcha in this image. It contains exactly "
                     f"{self.CAPTCHA_LENGTH} characters (uppercase letters and digits). "
                     f"Reply with only those characters, nothing else."},
        ]}]}
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "x-goog-api-key": self.GEMINI_API_KEY},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        text = ''.join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        return ''.join(ch for ch in text if ch.isalnum()).upper()

    def gemini_captcha(self, captcha_image):
        buffered = BytesIO()
        captcha_image.save(buffered, format="PNG")
        b64png = base64.b64encode(buffered.getvalue()).decode()
        # free tier returns 503 (overloaded) / 429 (quota); try each model with
        # one short backoff retry, then let the caller fall back to Tesseract
        for model in self.GEMINI_MODELS:
            for attempt in range(2):
                try:
                    text = self._gemini_call(model, b64png)
                    if text:
                        return text
                    break
                except urllib.error.HTTPError as e:
                    print(f"Gemini {model}: HTTP {e.code}")
                    if e.code in (429, 503) and attempt == 0:
                        time.sleep(6)
                        continue
                    break
                except Exception as e:
                    print(f"Gemini {model}: {e}")
                    break
        return ''

    def solve_captcha_auto(self, captcha_image):
        # Gemini (if configured) reads captchas far more reliably than Tesseract
        if self.GEMINI_API_KEY:
            text = self.gemini_captcha(captcha_image)
            if len(text) == self.CAPTCHA_LENGTH:
                print(f"Gemini read captcha as {text}")
                return text
        text = self.ocr_captcha(captcha_image)
        if text:
            print(f"Tesseract read captcha as {text}")
        return text

    def ocr_captcha(self, captcha_image):
        # grayscale -> 3x upscale -> binarize, then OCR as a single uppercase alphanumeric word
        img = captcha_image.convert('L')
        img = img.resize((img.width * 3, img.height * 3), Image.LANCZOS)
        img = img.point(lambda p: 0 if p < 180 else 255)
        text = pytesseract.image_to_string(
            img,
            config='--psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
        )
        return ''.join(ch for ch in text if ch.isalnum()).upper()

    CAPTCHA_LENGTH = 5

    def login(self, browser):
        interactive = sys.stdin.isatty()
        for attempt in range(1, self.MAX_LOGIN_ATTEMPTS + 1):
            if attempt > 1:
                # full navigation (not refresh - the page came from a form POST) for a clean form and fresh captcha
                browser.get(f"{self.URL}/#/home/dashboard")
                time.sleep(5)
            self.wait_for_login_form(browser)
            captcha_text = self.solve_captcha_auto(self.get_captcha_image(browser))
            # a human takes over after a failed automatic attempt, when a terminal is available
            if (not captcha_text or attempt > 1) and interactive:
                captcha_text = input("Type the captcha shown in the Chrome window: ").strip().upper()
            if len(captcha_text) != self.CAPTCHA_LENGTH and not interactive:
                # implausible read - grab a fresh captcha instead of burning a failed login
                print(f"attempt {attempt}: read doesn't look like a {self.CAPTCHA_LENGTH}-char captcha, retrying")
                continue
            email_field = browser.find_element(By.ID, "email")
            email_field.clear()
            email_field.send_keys(self.EMAIL)
            password_field = browser.find_element(By.ID, "password")
            password_field.clear()
            password_field.send_keys(self.PASSWORD)
            captcha_field = browser.find_element(By.ID, "captcha")
            captcha_field.clear()
            captcha_field.send_keys(captcha_text)
            # submit the login form; remember when, so we only accept an OTP
            # email that arrives after this attempt (codes are single-use)
            otp_requested_at = time.time()
            browser.find_element(By.XPATH, '//button[text()="Login"]').click()
            time.sleep(5)
            # logged straight in (trusted session / no 2FA challenge this run)
            if '/Account/' not in browser.current_url:
                return
            # 2FA: Keka emails a 6-digit OTP and shows an OTP entry page
            if self.on_otp_page(browser):
                self.handle_otp(browser, otp_requested_at)
                time.sleep(5)
                if '/Account/' not in browser.current_url:
                    return
                raise RuntimeError("entered OTP but login did not complete")
            print(f"Login attempt {attempt} failed")
        raise RuntimeError(f"Login failed after {self.MAX_LOGIN_ATTEMPTS} attempts")

    # --- 2FA OTP handling -------------------------------------------------

    def on_otp_page(self, browser):
        """True if the current page is the 2FA OTP entry step (not the login form)."""
        if [e for e in browser.find_elements(By.ID, 'email') if e.is_displayed()]:
            return False  # credentials form still showing -> not the OTP step
        if self.find_otp_inputs(browser):
            return True
        try:
            txt = browser.find_element(By.TAG_NAME, 'body').text.lower()
        except Exception:
            txt = ''
        return any(s in txt for s in ('one-time password', 'one time password',
                                      'verification code', 'otp'))

    def find_otp_inputs(self, browser):
        """The visible OTP input(s): one field, or several single-char boxes."""
        single = [
            'input#otp', 'input[name="otp"]', 'input[formcontrolname="otp"]',
            'input#code', 'input[name="code"]', 'input[autocomplete="one-time-code"]',
            'input[placeholder*="OTP" i]', 'input[placeholder*="code" i]',
        ]
        for sel in single:
            els = [e for e in browser.find_elements(By.CSS_SELECTOR, sel) if e.is_displayed()]
            if els:
                return els
        boxes = [e for e in browser.find_elements(
            By.CSS_SELECTOR, 'input[maxlength="1"], input[type="tel"]') if e.is_displayed()]
        return boxes if len(boxes) >= 4 else []

    def handle_otp(self, browser, requested_at):
        print("2FA OTP page detected; fetching the code from email...")
        code = self.fetch_otp_from_email(requested_at)
        if not code:
            self._dump_page(browser, 'otp_page_nocode')
            raise RuntimeError(
                f"no OTP email arrived within {self.OTP_TIMEOUT}s "
                f"(is Keka OTP forwarding to {self.IMAP_USER} set up?)")
        print(f"got OTP {code[0]}****{code[-1]} from email; entering it")
        inputs = self.find_otp_inputs(browser)
        if not inputs:
            self._dump_page(browser, 'otp_page_nofield')
            raise RuntimeError("OTP page detected but no OTP field found (page saved to logs/)")
        if len(inputs) == 1:
            inputs[0].clear()
            inputs[0].send_keys(code)
        else:
            for el, ch in zip(inputs, code):
                el.clear()
                el.send_keys(ch)
        self._submit_otp(browser)

    def _submit_otp(self, browser):
        for xp in ('//button[normalize-space()="Verify"]',
                   '//button[normalize-space()="Submit"]',
                   '//button[normalize-space()="Login"]',
                   '//button[normalize-space()="Continue"]',
                   '//button[normalize-space()="Confirm"]'):
            els = [e for e in browser.find_elements(By.XPATH, xp)
                   if e.is_displayed() and e.is_enabled()]
            if els:
                els[0].click()
                return
        # some OTP forms auto-submit once the final digit is entered
        inputs = self.find_otp_inputs(browser)
        if inputs:
            inputs[-1].send_keys(Keys.ENTER)

    def fetch_otp_from_email(self, requested_at):
        """Poll the IMAP mailbox for a fresh Keka OTP; return the 6-digit code or None."""
        if not (self.IMAP_USER and self.IMAP_PASSWORD):
            raise RuntimeError("IMAP not configured (set IMAP_USER/IMAP_PASSWORD or SMTP_USER/SMTP_PASSWORD)")
        pattern = re.compile(r'OTP\s*:?\s*([0-9]{6})')
        deadline = time.time() + self.OTP_TIMEOUT
        while time.time() < deadline:
            try:
                m = imaplib.IMAP4_SSL(self.IMAP_HOST)
                m.login(self.IMAP_USER, self.IMAP_PASSWORD)
                m.select('INBOX')
                _, data = m.search(None, f'(SUBJECT "{self.OTP_SUBJECT}")')
                for msg_id in reversed(data[0].split()[-10:]):
                    _, md = m.fetch(msg_id, '(RFC822)')
                    if not md or not md[0]:
                        continue
                    msg = email.message_from_bytes(md[0][1])
                    ts = self._msg_epoch(msg)
                    if ts and ts < requested_at - 180:
                        continue  # stale OTP from an earlier login
                    hit = pattern.search(self._email_text(msg))
                    if hit:
                        self._imap_logout(m)
                        return hit.group(1)
                self._imap_logout(m)
            except Exception as e:
                print(f"IMAP poll error: {e}")
            time.sleep(5)
        return None

    @staticmethod
    def _email_text(msg):
        chunks = []
        for part in (msg.walk() if msg.is_multipart() else [msg]):
            if part.get_content_type() in ('text/plain', 'text/html'):
                payload = part.get_payload(decode=True)
                if payload:
                    chunks.append(payload.decode(part.get_content_charset() or 'utf-8', 'ignore'))
        return '\n'.join(chunks)

    @staticmethod
    def _msg_epoch(msg):
        raw = msg.get('Date')
        if not raw:
            return None
        try:
            return parsedate_to_datetime(raw).timestamp()
        except Exception:
            return None

    @staticmethod
    def _imap_logout(m):
        try:
            m.logout()
        except Exception:
            pass

    def _dump_page(self, browser, tag):
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', f'{tag}.html')
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                f.write(browser.page_source)
            print(f"page saved to {path} for debugging")
        except Exception as e:
            print(f"could not dump page: {e}")

    # locators for the current attendance state
    CLOCK_IN_LINK = (By.XPATH, '//a[normalize-space()="Web Clock-In"]')
    CLOCK_OUT_BTN = (By.XPATH, '//*[self::a or self::button][normalize-space()="Web Clock-out"]')

    def clock(self, browser):
        """Perform the clock action and return a human-readable status string.
        Raises on a hard failure (button never appeared, etc.)."""
        browser.get(f"{self.URL}/#/me/attendance/logs")
        time.sleep(5)  # Wait for the page to load

        if self.CHECK.lower() == 'in':
            if browser.find_elements(*self.CLOCK_OUT_BTN):
                return "already clocked in - no action taken"
            WebDriverWait(browser, 10).until(EC.element_to_be_clickable(self.CLOCK_IN_LINK)).click()
            time.sleep(4)
            if browser.find_elements(*self.CLOCK_OUT_BTN):
                return "clocked IN (confirmed)"
            return "clicked Web Clock-In but could not confirm the clocked-in state"

        # clock out
        if browser.find_elements(*self.CLOCK_IN_LINK):
            return "already clocked out - no action taken"
        WebDriverWait(browser, 10).until(EC.element_to_be_clickable(self.CLOCK_OUT_BTN)).click()
        try:
            # some Keka tenants show a confirm modal; others clock out on the first click
            WebDriverWait(browser, 8).until(
                EC.element_to_be_clickable((By.XPATH, '//button[normalize-space()="Clock-out"]'))
            ).click()
        except TimeoutException:
            pass
        time.sleep(4)
        if browser.find_elements(*self.CLOCK_IN_LINK):
            return "clocked OUT (confirmed)"
        return "clicked Web Clock-out but could not confirm the clocked-out state"

    def notify(self, status, error):
        action = self.CHECK.lower()
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z').strip()
        ok = error is None
        subject = f"Keka clock-{action}: {'OK' if ok else 'FAILED'}"
        body = f"Time:   {ts}\nAction: clock-{action}\n"
        body += f"Status: {status}\n" if ok else f"Status: FAILED\n\n{error}"
        print(subject)
        print(body)
        if not (self.SMTP_USER and self.SMTP_PASSWORD):
            print("email not configured (set SMTP_USER and SMTP_PASSWORD in .env) - skipping send")
            return
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = self.SMTP_USER
        msg['To'] = self.NOTIFY_EMAIL
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.SMTP_HOST, self.SMTP_PORT, timeout=30) as s:
                s.starttls()
                s.login(self.SMTP_USER, self.SMTP_PASSWORD)
                s.send_message(msg)
            print(f"status email sent to {self.NOTIFY_EMAIL}")
        except Exception as e:
            print(f"failed to send status email: {e}")

    def start(self):
        status, error = None, None
        try:
            options = webdriver.ChromeOptions()
            options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
            if os.getenv('KEKA_HEADLESS') == '1':
                # headless is fine for scheduled runs: the session is trusted and Gemini reads any captcha
                options.add_argument('--headless=new')
                options.add_argument('--window-size=1400,900')
                # needed on CI runners (no sandbox, small /dev/shm)
                options.add_argument('--no-sandbox')
                options.add_argument('--disable-dev-shm-usage')
                options.add_argument('--disable-gpu')
            browser = webdriver.Chrome(options=options)
            try:
                browser.get(f"{self.URL}/#/home/dashboard")
                time.sleep(5)
                # check if redirected to login page (app.keka.com/Account/KekaLogin on current Keka)
                if '/Account/' in browser.current_url:
                    self.login(browser)
                status = self.clock(browser)
            finally:
                browser.quit()
        except Exception:
            error = traceback.format_exc()
        self.notify(status, error)
        if error:
            sys.exit(1)

if __name__ == '__main__':
    Keka().start()
