from __future__ import annotations

from pathlib import Path
from typing import Union

import yaml
from pydantic import BaseModel, Field

CONFIG_PATH = Path("config.yaml")

ALL_CENTRES = ["Cochin", "Chennai", "Bangalore", "Puducherry", "Hyderabad", "Goa", "Pune", "Mumbai",
               "Kolkata", "Ahmedabad", "Jaipur", "Gurugram", "New Delhi", "Chandigarh", "Jalandhar"]

KNOWN_CATEGORIES = {
    "D visa": ["Long Stay D visa"],
    "Business": ["Business Visa"],
    "Schengen Visa- Less than 90 days": ["Airport Transit Visa", "Conference Visa", "Medical Visit", "Private Visit",
                                         "Sports/ Cultural/ Artistic/ Scientific visa/ Education Visa", "Tourist Visa",
                                         "Transit Visa"],
    "Seasonal worker": [],
}


class CentreConfig(BaseModel):
    name: str
    enabled: bool = True


class BurstConfig(BaseModel):
    enabled: bool = True
    weekday: int = 0
    nth_week: int = 2
    start: str = "13:50"
    end: str = "15:00"
    interval_seconds: int = 45
    timezone: str = "Asia/Kolkata"


class ScheduleConfig(BaseModel):
    """When the watcher is allowed to run. Outside the window it sleeps (session kept alive)."""
    date_from: str = ""          # YYYY-MM-DD, "" = no limit
    date_to: str = ""
    hours_from: str = "00:00"    # HH:MM local
    hours_to: str = "23:59"
    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])  # 0=Mon
    timezone: str = "Asia/Kolkata"


class NotifyConfig(BaseModel):
    cooldown_minutes: int = 30   # don't re-alert the same availability within this window
    heartbeat_hours: int = 6     # "still watching" message to bot-issue contacts (0 = off)
    alert_top_n: int = 0         # only alert for the first N centres in priority order (0 = all)
    quiet_hours_from: str = ""   # e.g. "23:00" — no voice calls between these (messages still go)
    quiet_hours_to: str = ""     # e.g. "07:00"


class WhatsAppContact(BaseModel):
    """One person to reach over WhatsApp Web. `chat` is the exact chat title in WhatsApp."""
    chat: str
    enabled: bool = True
    mode: str = "message_call"   # "message" = text only | "message_call" = text, then call until they ack
    bot_issues: bool = False     # also receives OTP / needs-human / error / heartbeat notices
    call_attempts: int = 0       # 0 = use the global default
    ack_keyword: str = ""        # "" = use the global default


class WhatsAppWebConfig(BaseModel):
    """Alerting over the office's own logged-in WhatsApp Web — the only alert channel."""
    enabled: bool = True
    contacts: list[WhatsAppContact] = Field(default_factory=list)
    ack_keyword: str = "ok"
    call_attempts: int = 3
    call_interval_seconds: int = 120   # wait this long for an ack between call rounds
    ring_seconds: int = 25
    stop_all_on_first_ack: bool = True # one person acking is enough: stop calling everyone
    profile_dir: str = "whatsapp-profile"
    chat: str = ""                     # legacy single contact; migrated into `contacts` on load

    def active_contacts(self) -> list[WhatsAppContact]:
        return [c for c in self.contacts if c.enabled and c.chat.strip()]


class BrowserConfig(BaseModel):
    headless: bool = False
    profile_dir: str = "browser-profile"
    executable: str = ""


class RotationConfig(BaseModel):
    """Login mode: sign in with a different account (own proxy/IP + browser profile) on every sweep,
    then close the browser. Accounts are managed on the dashboard (data/accounts.json)."""
    enabled: bool = True
    login_wait_minutes: int = 3        # give up on Cloudflare/OTP after this and move to the next account
    cooloff_hours: float = 2.0         # base cool-off for an account whose login stalled/blocked (doubles per fail)
    retry_minutes: int = 5             # wait this long before trying the next account after a failure
    verify_ip: bool = True             # look up the public IP through the proxy before logging in
    require_proxy: bool = False        # refuse to log in when an account has no proxy configured
    ip_check_url: str = "https://api.ipify.org?format=json"


class ProxyAutoConfig(BaseModel):
    """Automatic per-account proxies: one tiny cloud server per account (SSH SOCKS tunnel).
    API token lives in data/proxy_provider.json; everything else is automatic."""
    enabled: bool = False
    provider: str = "digitalocean"   # digitalocean | vultr
    region: str = ""                 # "" = provider default (blr1 Bangalore / bom Mumbai)
    plan: str = ""                   # "" = cheapest default


class IpRotateConfig(BaseModel):
    """Get a fresh public IP before every login by running a command — the free way is an Android
    phone on USB tethering: toggling its mobile data gives a new carrier IP (the kind of IP
    Cloudflare trusts most). Works with any VPN CLI too (e.g. "protonvpn-cli c -r")."""
    enabled: bool = False
    command: str = "adb shell svc data disable && sleep 4 && adb shell svc data enable && sleep 10"
    require_change: bool = True      # refuse to log in if the IP did not change from the last login
    timeout_seconds: int = 90


class Config(BaseModel):
    mode: str = "public"   # "public" = no-login earliest-date endpoint (recommended); "login" = old flow
    base_url: str = "https://visa.vfsglobal.com/ind/en/bgr"
    centre: str = ""
    centres: list[Union[str, CentreConfig]] = Field(default_factory=lambda: [CentreConfig(name=c) for c in ALL_CENTRES])
    category: str = "D visa"
    subcategory: str = ""
    centre_pause_seconds: float = 8
    # VFS allows only ~7 CheckIsSlotAvailable calls per login session (then HTTP 429 + logout), so each
    # login checks this many centres and the next login continues round the priority list.
    login_centres_per_session: int = 6
    api_replay: bool = False   # replaying the SPA's API call directly gets 401 (per-request signed header); keep off
    interval_seconds: int = 300          # login mode: one login per this interval
    jitter_seconds: int = 45
    public_interval_seconds: int = 300   # public (no-login) mode: refresh the earliest-date data this often
    public_jitter_seconds: int = 60
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    burst: BurstConfig = Field(default_factory=BurstConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    whatsapp_web: WhatsAppWebConfig = Field(default_factory=WhatsAppWebConfig)
    rotation: RotationConfig = Field(default_factory=RotationConfig)
    proxy_auto: ProxyAutoConfig = Field(default_factory=ProxyAutoConfig)
    ip_rotate: IpRotateConfig = Field(default_factory=IpRotateConfig)
    passport_file: str = "documents/passport_bio.jpg"
    passport_auto_continue: bool = False   # first login of an account: bot presses Continue after selecting the passport
    passport_wait_minutes: int = 10        # ...otherwise how long to wait for a human to press it
    team: str = "4indegree · AAI (Anas and Amal Intelligence)"

    @property
    def centre_list(self) -> list[str]:
        out = []
        for c in self.centres:
            if isinstance(c, str):
                out.append(c)
            elif c.enabled:
                out.append(c.name)
        return out or ([self.centre] if self.centre else [""])

    @classmethod
    def load(cls, path: str | Path = CONFIG_PATH) -> "Config":
        p = Path(path)
        data = yaml.safe_load(p.read_text()) if p.exists() else {}
        cfg = cls.model_validate(data or {})
        w = cfg.whatsapp_web
        if w.chat and not w.contacts:   # pre-contacts config: one chat that got message + calls
            w.contacts = [WhatsAppContact(chat=w.chat, mode="message_call", bot_issues=True)]
            w.chat = ""
        return cfg

    def save(self, path: str | Path = CONFIG_PATH) -> None:
        Path(path).write_text(yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, allow_unicode=True))
