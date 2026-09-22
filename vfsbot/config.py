from __future__ import annotations

from pathlib import Path
from typing import Union

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_PATH = Path("config.yaml")
ENV_PATH = Path(".env")

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
    telegram: bool = True
    email: bool = True
    call: bool = True
    whatsapp: bool = False
    cooldown_minutes: int = 30
    heartbeat_hours: int = 6
    alert_top_n: int = 0
    quiet_hours_from: str = ""   # e.g. "23:00" — no phone calls between these (messages still go)
    quiet_hours_to: str = ""     # e.g. "07:00"


class WhatsAppWebConfig(BaseModel):
    """Escalation over the office's own logged-in WhatsApp Web (no Twilio)."""
    enabled: bool = False
    chat: str = "Aslam 4indegree OH"
    ack_keyword: str = "ok"
    call_attempts: int = 3
    call_interval_seconds: int = 120
    ring_seconds: int = 25
    profile_dir: str = "whatsapp-profile"


class BrowserConfig(BaseModel):
    headless: bool = False
    profile_dir: str = "browser-profile"
    executable: str = ""


class RotationConfig(BaseModel):
    """Login mode: sign in with a different account (own proxy/IP + browser profile) on every sweep,
    then close the browser. Accounts are managed on the dashboard (state/accounts.json)."""
    enabled: bool = True
    login_wait_minutes: int = 3        # give up on Cloudflare/OTP after this and move to the next account
    cooloff_hours: float = 2.0         # base cool-off for an account whose login stalled/blocked (doubles per fail)
    retry_minutes: int = 5             # wait this long before trying the next account after a failure
    verify_ip: bool = True             # look up the public IP through the proxy before logging in
    require_proxy: bool = False        # refuse to log in when an account has no proxy configured
    ip_check_url: str = "https://api.ipify.org?format=json"


class Config(BaseModel):
    mode: str = "public"   # "public" = no-login earliest-date endpoint (recommended); "login" = old flow
    base_url: str = "https://visa.vfsglobal.com/ind/en/bgr"
    centre: str = ""
    centres: list[Union[str, CentreConfig]] = Field(default_factory=lambda: [CentreConfig(name=c) for c in ALL_CENTRES])
    category: str = "D visa"
    subcategory: str = ""
    centre_pause_seconds: float = 8
    interval_seconds: int = 300
    jitter_seconds: int = 45
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    burst: BurstConfig = Field(default_factory=BurstConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    whatsapp_web: WhatsAppWebConfig = Field(default_factory=WhatsAppWebConfig)
    rotation: RotationConfig = Field(default_factory=RotationConfig)
    passport_file: str = "documents/passport_bio.jpg"
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
        return cls.model_validate(data or {})

    def save(self, path: str | Path = CONFIG_PATH) -> None:
        Path(path).write_text(yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, allow_unicode=True))


class Secrets(BaseSettings):
    """Credentials from .env / environment."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    vfs_email: str = ""
    vfs_password: str = ""

    # auto-OTP: IMAP access to the watcher account's own inbox (Gmail -> App Password)
    imap_host: str = "imap.gmail.com"
    imap_user: str = ""
    imap_password: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_to: str = ""

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from: str = ""
    call_to: str = ""
    whatsapp_from: str = ""      # Twilio WhatsApp sender, e.g. whatsapp:+14155238886
    whatsapp_to: str = ""        # comma-separated, e.g. whatsapp:+91XXXXXXXXXX

    def save(self, path: str | Path = ENV_PATH) -> None:
        lines = [f"{k.upper()}={v}" for k, v in self.model_dump().items()]
        Path(path).write_text("\n".join(lines) + "\n")
