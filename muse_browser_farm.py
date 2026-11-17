#!/usr/bin/env python3
"""
muse_browser_farm.py — Browser-based Muse farm (manual-scheme, automated).

Reproduces the "How to Claim 30B Token MUSE AI" flow with camoufox:
  Hotmail (buy-ready, via OTP panel) -> enter email -> read OTP -> confirm
  -> "Finish creating your account" (random 2-letter name + DOB) -> waitlist
  -> VPN geo-bypass: switch to JP, click "Join waiting list", back to US
  -> disclosure "A few things to know" -> Get started -> Setting up Muse
  -> Settings -> General -> Redeem invitation code (YHWHD2) -> 1B tokens

Two pieces of infra are REQUIRED and not guessable by the script, so they
are read from config.json (next to this file):

  {
    "otp_panel": {
      "type": "url_or_api",
      "base_url": "https://<your-get-otp-hotmail-panel>",
      "api": {
        "list_accounts": "/api/accounts",
        "get_otp": "/api/accounts/{email}/otp",
        "header_auth": "Bearer <token>"
      },
      "auth": "Bearer <token>"
    },
    "vpn": {
      "type": "openvpn_cli",
      "config_dir": "/home/agentuser/vpn_configs",
      "profiles": {
        "us": "us.ovpn",
        "jp": "jp.ovpn"
      },
      "auth": { "user": "u", "pass": "p" },
      "settle_seconds": 8
    },
    "invite_code": "YHWHD2",
    "target_accounts": 30,
    "first_name": "",
    "last_name": "",
    "dob": "1995-06-03",
    "headless": false
  }

The OTP panel is the crux. If your "GET OTP HOTMAIL" panel has an API, point
`otp_panel.api` at it. If it has no API, set `otp_panel.type = "manual"` and
the script will PAUSE at each OTP and let you paste the 6-digit code from the
panel (slow but sure, matches the video).

Run:  python3 muse_browser_farm.py
"""
import json, os, sys, re, time, random, subprocess, datetime, argparse, getpass

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
RESULTS = os.path.join(HERE, "muse_browser_results.json")

# ─────────────────────────────────────────────────────────────────────────────
# config
# ─────────────────────────────────────────────────────────────────────────────
def load_cfg():
    if not os.path.exists(CONFIG):
        print(f"BLOCKED: {CONFIG} not found.\n"
              "Create it (see the docstring at the top of this file) with your\n"
              "OTP panel + VPN details, then re-run. I will not invent these.")
        sys.exit(2)
    with open(CONFIG) as f:
        return json.load(f)

CFG = load_cfg()
INVITE = CFG.get("invite_code", "YHWHD2")
TARGET = int(CFG.get("target_accounts", 30))
HEADLESS = bool(CFG.get("headless", False))
DOB = CFG.get("dob", "1995-06-03")

OTP_PANEL = CFG.get("otp_panel", {})
VPN = CFG.get("vpn", {})

# ─────────────────────────────────────────────────────────────────────────────
# VPN control (free desktop VPN on the VPS)
# ─────────────────────────────────────────────────────────────────────────────
class Vpn:
    """Minimal openvpn-cli profile switcher. If VPN.type is not 'openvpn_cli'
    and not 'none', this is a BLOCKER — I won't guess your VPN vendor."""

    def __init__(self):
        self.type = VPN.get("type", "none")
        self.dir = VPN.get("config_dir", "")
        self.profiles = VPN.get("profiles", {})
        self.auth = VPN.get("auth", {})
        self.settle = int(VPN.get("settle_seconds", 8))
        self._proc = None

    def _build_cmd(self, loc):
        f = self.profiles.get(loc)
        if not f:
            raise KeyError(f"vpn profile '{loc}' not in config.json vpn.profiles")
        path = os.path.join(self.dir, f)
        cmd = ["openvpn", "--config", path, "--daemon"]
        if self.auth.get("user"):
            cmd += ["--username", self.auth["user"], "--password", self.auth["pass"]]
        return cmd

    def switch(self, loc):
        if self.type == "none":
            print(f"  [vpn] type=none — assuming {loc} is your baseline. No switch.")
            return True
        if self.type != "openvpn_cli":
            print(f"  BLOCKED: vpn.type='{self.type}' unsupported. "
                  f"Support is 'openvpn_cli' or 'none'. Add your VPN's CLI in Vpn._build_cmd.")
            return False
        self._down()
        cmd = self._build_cmd(loc)
        print(f"  [vpn] -> {loc}  ({cmd[0]} ... {self.profiles[loc]})")
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(self.settle)
        ip = subprocess.run(["curl", "-s", "--max-time", "10", "http://ip-api.com/json"],
                            capture_output=True, text=True).stdout.strip()
        try:
            cc = json.loads(ip).get("countryCode", "?")
            print(f"  [vpn] {loc} active, egress IP={ip[:20]}.. cc={cc}")
            return True
        except Exception:
            print(f"  [vpn] {loc} — IP check failed ({ip[:40]}), continuing anyway")
            return True

    def _down(self):
        if self._proc and self._proc.poll() is None:
            subprocess.run(["pkill", "-f", "openvpn"], capture_output=True)
            time.sleep(2)
        self._proc = None

    def down(self):
        self._down()


VPN_OBJ = Vpn()

# ─────────────────────────────────────────────────────────────────────────────
# OTP panel client
# ─────────────────────────────────────────────────────────────────────────────
class OtpPanel:
    def __init__(self):
        self.t = OTP_PANEL.get("type", "manual")
        self.base = OTP_PANEL.get("base_url", "").rstrip("/")
        self.api = OTP_PANEL.get("api", {})
        self.auth = OTP_PANEL.get("auth", "")

    def list_accounts(self):
        """Return a list of buy-ready Hotmail addresses to farm.
        For 'url_or_api' type, call api.list_accounts. For 'manual', the
        operator types each email when prompted."""
        if self.t == "manual":
            return None  # caller will prompt
        import urllib.request
        url = self.api.get("list_accounts", "/api/accounts")
        if url.startswith("/"):
            url = self.base + url
        req = urllib.request.Request(url, headers={"Authorization": self.auth})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        # tolerate either a bare list or {"accounts": [...]}
        if isinstance(data, dict):
            data = data.get("accounts", data.get("data", []))
        emails = []
        for a in data:
            if isinstance(a, str):
                emails.append(a)
            elif isinstance(a, dict):
                emails.append(a.get("email") or a.get("address"))
        emails = [e for e in emails if e]
        print(f"  [otp-panel] {len(emails)} buy-ready accounts available")
        return emails

    def get_otp(self, email, timeout=120):
        """Fetch the 6-digit code for `email` from the panel's inbox.
        type 'manual' -> the operator pastes it. type 'url_or_api' -> poll API."""
        if self.t == "manual":
            print(f"\n  [manual] OTP panel open for {email}? Paste the 6-digit code "
                  f"(from security@account.meta.com / notifications@facebookmail.com):", end=" ")
            code = getpass.getpass(prompt="").strip()
            return code if re.fullmatch(r"\d{6}", code) else None
        import urllib.request
        ep = self.api.get("get_otp", "/api/accounts/{email}/otp")
        url = ep.format(email=email)
        if url.startswith("/"):
            url = self.base + url
        req = urllib.request.Request(url, headers={"Authorization": self.auth})
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    body = json.loads(r.read().decode())
                code = body.get("otp") or body.get("code") or (
                    re.search(r"code (\d{6})", json.dumps(body)) or [None, None])[1]
                if code and re.fullmatch(r"\d{6}", code):
                    return code
                last = body
            except Exception as e:
                last = f"err:{e}"
            time.sleep(5)
        print(f"  [otp-panel] no code for {email} within {timeout}s. last={last}")
        return None


OTP = OtpPanel()

# ─────────────────────────────────────────────────────────────────────────────
# Browser farm (camoufox)
# ─────────────────────────────────────────────────────────────────────────────
class MuseBrowser:
    """Drives muse.ai registration with camoufox (Incognito-like fresh
    profile per account). Every method returns (ok, detail)."""

    def __init__(self, headless=False):
        import camoufox.sync_api as cfx
        self.cfx = cfx
        self.browser = cfx.Camoufox(headless=headless, locale="en-US",
                                     geoip="US", proxy=None)
        self.ctx = None
        self.page = None

    def fresh_profile(self):
        self.new_tab()

    def new_tab(self):
        self.page = self.browser.new_page()
        return self.page

    def open(self, url):
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)

    def js(self, expr):
        return self.page.evaluate(expr)

    def text(self, sel):
        try:
            el = self.page.query_selector(sel)
            return el.inner_text() if el else ""
        except Exception:
            return ""

    def close(self):
        try:
            self.page.close()
        except Exception:
            pass

    # ---- registration flow -------------------------------------------------
    def enter_email(self, email):
        """muse.ai landing -> email field -> 'Enter your code to sign up'."""
        self.open("https://muse.ai")
        self.page.wait_for_selector("input[type='email']", timeout=30000)
        self.page.type("input[type='email']", email, delay=60)
        # submit (Continue / arrow)
        for sel in ["button:has-text('Continue')", "button[type='submit']", "button"]:
            try:
                self.page.click(sel, timeout=8000)
                break
            except Exception:
                continue
        self.page.wait_for_timeout(3000)
        return self.is_otp_page()

    def is_otp_page(self):
        return ("your code" in (self.page.url or "").lower()
                or bool(self.page.query_selector("text=/Enter your code to sign up/i")))

    def confirm_otp(self, code):
        # 6 boxes
        boxes = self.page.query_selector_all("input[maxlength='1']")
        if not boxes:
            boxes = self.page.query_selector_all("input[type='text'][maxlength]")
        for i, ch in enumerate(code[:6]):
            try:
                boxes[i].fill("")
                boxes[i].type(ch, delay=50)
            except Exception:
                pass
        time.sleep(1)
        for sel in ["button:has-text('Confirm')", "button[type='submit']"]:
            try:
                self.page.click(sel, timeout=8000)
                break
            except Exception:
                continue
        self.page.wait_for_timeout(4000)
        return not self.has_error("Something went wrong")

    def has_error(self, needle):
        try:
            el = self.page.query_selector(f"text=/{re.escape(needle)}/i")
            return el is not None
        except Exception:
            return False

    def choose_create_new(self):
        """'Choose an account to continue' -> 'Create new account'."""
        for sel in ["text=Create new account", "button:has-text('Create new')",
                    "role=button:has-text('new account')"]:
            try:
                self.page.click(sel, timeout=6000)
                self.page.wait_for_timeout(2000)
                return True
            except Exception:
                continue
        return False

    def finish_create_account(self, first, last, dob):
        """'Finish creating your account' -> name + DOB dropdowns -> Confirm.
        Returns 'ok' | 'waitlist' | 'error'."""
        self.page.wait_for_timeout(1500)
        # names (2-letter random per video)
        for sel in ["input[placeholder*='First name' i]",
                    "input[name*='first' i]", "input[type='text']"]:
            try:
                el = self.page.query_selector(sel)
                if el:
                    el.fill(first)
                    break
            except Exception:
                continue
        # last name if present
        self.page.wait_for_timeout(300)
        try:
            self.page.fill("input[name*='last' i]", last, timeout=3000)
        except Exception:
            pass
        # DOB: three <select> (day, month, year)
        y, m, d = (dob or "1995-06-03").split("-")
        selects = self.page.query_selector_all("select")
        try:
            if len(selects) >= 3:
                selects[0].select_option(d)            # day
                selects[1].select_option(label=m.title().ljust(0) or "June")
                selects[2].select_option(y)            # year
        except Exception:
            # fallback: type into day/month/year text inputs
            for inp, val in zip(
                    self.page.query_selector_all("input[maxlength='1']"),
                    [d, m, y]):
                try:
                    inp.fill(val)
                except Exception:
                    pass
        self.page.wait_for_timeout(300)
        for sel in ["button:has-text('Confirm')", "button:has-text('Continue')",
                    "button[type='submit']"]:
            try:
                self.page.click(sel, timeout=8000)
                break
            except Exception:
                continue
        self.page.wait_for_timeout(4000)
        if self.is_waitlist():
            return "waitlist"
        if self.has_error("Something went wrong") or self.has_error("can't create"):
            return "error"
        return "ok"

    def is_waitlist(self):
        try:
            el = self.page.query_selector("text=/Join the waitlist for Muse/i")
            return el is not None
        except Exception:
            return False

    def join_waitlist_geo_bypass(self):
        """Video trick: VPN -> JP, click 'Join waiting list', VPN -> US.
        Returns True if the waitlist gate cleared (disclosure appeared)."""
        self._click_join()
        VPN_OBJ.switch("jp")
        # re-trigger the gate on JP
        try:
            self.page.reload(wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        self.page.wait_for_timeout(4000)
        if self.is_waitlist():
            self._click_join()
        VPN_OBJ.switch("us")
        try:
            self.page.reload(wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        self.page.wait_for_timeout(4000)
        cleared = not self.is_waitlist()
        if cleared:
            # advance disclosure "A few things to know" -> Get started
            self._advance_disclosure()
        return cleared

    def _click_join(self):
        for sel in ["button:has-text('Join')", "button:has-text('waitlist')",
                    "button:has-text('Join waiting list')"]:
            try:
                self.page.click(sel, timeout=6000)
                self.page.wait_for_timeout(2000)
                return True
            except Exception:
                continue
        return False

    def _advance_disclosure(self):
        for label in ["A few things to know", "Get started", "Setting up Muse"]:
            try:
                self.page.click(f"text={label}", timeout=6000)
                self.page.wait_for_timeout(2500)
            except Exception:
                continue

    def redeem(self, code):
        """Settings -> General -> Redeem invitation code -> YHWHD2.
        Returns True on 'You're in'."""
        # open Settings
        for sel in ["text=Settings", "role=button:has-text('Settings')",
                    "button[aria-label*='setting' i]"]:
            try:
                self.page.click(sel, timeout=8000)
                self.page.wait_for_timeout(1500)
                break
            except Exception:
                continue
        # General -> Redeem invitation code
        for sel in ["text=General", "text=Redeem invitation code",
                    "text=invitation code", "text=Redeem"]:
            try:
                self.page.click(sel, timeout=6000)
                self.page.wait_for_timeout(1000)
            except Exception:
                continue
        # type the code into the invitation input
        for sel in ["input[placeholder*='invitation' i]", "input[placeholder*='code' i]",
                    "input[type='text']"]:
            try:
                el = self.page.query_selector(sel)
                if el:
                    el.fill(code)
                    break
            except Exception:
                continue
        self.page.wait_for_timeout(300)
        for sel in ["button:has-text('Redeem')", "button:has-text('Confirm')",
                    "button[type='submit']"]:
            try:
                self.page.click(sel, timeout=6000)
                self.page.wait_for_timeout(4000)
                break
            except Exception:
                continue
        body = self.page.inner_text("body")
        ok = ("You're in" in body) or ("was redeemed" in body) or ("redeemed" in body)
        tokens = re.search(r"Additional tokens\s*([0-9.]+B?|[\d,]+)", body, re.I)
        return ok, (tokens.group(1) if tokens else "unknown")

    def status_tokens(self):
        try:
            body = self.page.inner_text("body")
        except Exception:
            return "unknown"
        m = re.search(r"Additional tokens\s*([0-9.]+B?|[\d,]+)", body, re.I)
        return m.group(1) if m else "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# per-account orchestrator
# ─────────────────────────────────────────────────────────────────────────────
def two_letters():
    return "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=2))


def farm_one(mb, email, idx):
    rec = {"idx": idx, "email": email, "steps": {}, "redeemed": False,
           "tokens": None, "ts": datetime.datetime.now().isoformat()}
    print(f"\n=== account #{idx}  {email} ===")

    # VPN baseline = US before we start (video starts on US)
    VPN_OBJ.switch("us")
    mb.new_tab()

    # 1) enter email
    ok = mb.enter_email(email)
    rec["steps"]["enter_email"] = ok
    print(f"  enter_email -> otp_page={ok}")
    if not ok:
        rec["fail"] = "no_otp_page"
        mb.close(); return rec

    # 2) OTP
    code = OTP.get_otp(email, timeout=120)
    rec["steps"]["otp"] = bool(code)
    print(f"  otp={'yes' if code else 'NO'}")
    if not code:
        rec["fail"] = "no_otp"
        mb.close(); return rec

    # 3) confirm OTP
    ok = mb.confirm_otp(code)
    rec["steps"]["confirm_otp"] = ok
    print(f"  confirm_otp -> {ok}")
    if not ok:
        # video: first attempt can hit 'Something went wrong' -> retry with new OTP
        code2 = OTP.get_otp(email, timeout=120)
        if code2 and mb.confirm_otp(code2):
            ok, rec["steps"]["confirm_otp"] = True, True
        else:
            rec["fail"] = "confirm_otp_failed"
            mb.close(); return rec

    # 4) 'Choose an account' -> Create new account
    mb.choose_create_new()

    # 5) finish creating account (name + DOB)
    fn, ln = CFG.get("first_name") or two_letters(), CFG.get("last_name") or two_letters()
    state = mb.finish_create_account(fn, ln, DOB)
    rec["steps"]["finish_create"] = state
    print(f"  finish_create -> {state}  (name={fn}/{ln} dob={DOB})")
    if state == "error":
        rec["fail"] = "account_create_error"
        mb.close(); return rec

    # 6) waitlist -> JP geo-bypass
    if state == "waitlist" or mb.is_waitlist():
        cleared = mb.join_waitlist_geo_bypass()
        rec["steps"]["waitlist_bypass"] = cleared
        print(f"  waitlist_bypass -> {cleared}")
        if not cleared:
            rec["fail"] = "waitlist_not_cleared"
            mb.close(); return rec

    # 7) redeem YHWHD2
    ok, tok = mb.redeem(INVITE)
    rec["steps"]["redeem"] = ok
    rec["redeemed"] = bool(ok)
    rec["tokens"] = tok
    print(f"  redeem {INVITE} -> ok={ok} tokens={tok}")

    mb.close()
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", type=int, default=0)
    ap.add_argument("--count", type=int, default=None)
    ap.add_argument("--manual-emails", help="comma-separated emails, skip panel list")
    args = ap.parse_args()

    count = args.count or TARGET
    if args.manual_emails:
        emails = [e.strip() for e in args.manual_emails.split(",") if e.strip()]
        source = "manual"
    else:
        print("Fetching buy-ready Hotmail accounts from OTP panel...")
        emails = OTP.list_accounts() or []
        source = "panel"
        if not emails:
            print("BLOCKED: no accounts from panel. Use --manual-emails or fix config.")
            sys.exit(2)

    emails = emails[args.start: args.start + count]
    print(f"\nPlan: farm {len(emails)} accounts ({source}), code={INVITE}, target_total={TARGET}")

    mb = MuseBrowser(headless=HEADLESS)
    results = []
    try:
        for i, email in enumerate(emails, start=1):
            rec = farm_one(mb, email, i)
            results.append(rec)
            save_results(results)
            if not rec["redeemed"]:
                print(f"  !! account #{i} NOT redeemed (fail={rec.get('fail','?')}) — continuing")
    finally:
        VPN_OBJ.down()
        try:
            mb.browser.close()
        except Exception:
            pass

    print_summary(results)


def save_results(results):
    with open(RESULTS, "w") as f:
        json.dump(results, f, indent=2)


def print_summary(results):
    redeemed = [r for r in results if r["redeemed"]]
    print(f"\n{'='*52}\nSUMMARY: {len(redeemed)}/{len(results)} redeemed")
    for r in results:
        flag = "OK " if r["redeemed"] else "xx "
        print(f" {flag} #{r['idx']:>2} {r['email']:<38} tokens={r['tokens']} "
              f"fail={r.get('fail','')}")
    print(f" results -> {RESULTS}")


if __name__ == "__main__":
    main()
