"""
Settings loader — reads config.yaml and merges environment variables.
Call settings.reload() to hot-reload without restarting.
"""
import os
import yaml
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def _load() -> dict:
    with open(CONFIG_PATH, "r") as f:
        cfg = yaml.safe_load(f)

    # Ensure required sections exist
    cfg.setdefault("telegram", {})
    cfg.setdefault("mt5", {})
    cfg.setdefault("gemini", {})
    cfg.setdefault("backtest", {})
    cfg.setdefault("reporting", {})
    cfg.setdefault("broker", {})
    cfg.setdefault("mtf", {})
    cfg.setdefault("session", {})
    cfg.setdefault("trade_memory", {})
    cfg.setdefault("trailing_stop", {})

    # Env vars override config.yaml
    # Tier 2 must use its own bot token so it cannot conflict with Tier 1 polling.
    cfg["telegram"]["token"] = os.getenv("FOREX_PLUS_TELEGRAM_BOT_TOKEN", "")
    cfg["telegram"]["admin_chat_id"] = os.getenv(
        "FOREX_PLUS_ADMIN_CHAT_ID", cfg["telegram"].get("admin_chat_id", "")
    )
    cfg["gemini_api_key"] = os.getenv("GEMINI_API_KEY", "")
    cfg["openai_api_key"] = os.getenv("OPENAI_API_KEY", "")

    mt5 = cfg["mt5"]
    # Account access may only come from protected environment values.
    mt5["login"]    = os.getenv("MT5_LOGIN", "")
    mt5["password"] = os.getenv("MT5_PASSWORD", "")
    mt5["server"]   = os.getenv("MT5_SERVER") or mt5.get("server", "")

    return cfg


class Settings:
    def __init__(self):
        self._cfg = _load()

    def reload(self):
        self._cfg = _load()
        print("[Settings] Config reloaded from disk.")

    # ── Broker helpers ─────────────────────────────────────────
    @property
    def symbol_suffix(self) -> str:
        return self._cfg.get("broker", {}).get("symbol_suffix", "")

    def apply_suffix(self, pair: str) -> str:
        """Append broker symbol suffix if not already present."""
        sfx = self.symbol_suffix
        if sfx and not pair.endswith(sfx):
            return pair + sfx
        return pair

    def strip_suffix(self, symbol: str) -> str:
        """Remove broker symbol suffix for display/memory keys."""
        sfx = self.symbol_suffix
        if sfx and symbol.endswith(sfx):
            return symbol[: -len(sfx)]
        return symbol

    @property
    def pairs_with_suffix(self) -> list[str]:
        """All configured pairs with broker suffix applied."""
        return [self.apply_suffix(p) for p in self._cfg.get("pairs", [])]

    # ── Generic attribute access ───────────────────────────────
    def __getattr__(self, name):
        try:
            return self._cfg[name]
        except KeyError:
            raise AttributeError(f"Setting '{name}' not found in config.yaml")

    def get(self, key, default=None):
        return self._cfg.get(key, default)


settings = Settings()
