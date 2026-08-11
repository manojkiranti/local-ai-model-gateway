"""Nepal Rastra Bank integration (official NRB APIs).

Currently the Forex API only. The base URL is application config
(`NRB_API_BASE_URL`) and the paths are hardcoded — the model never supplies a
host, so this is deliberately not a second `fetch_url`.
"""

from .client import ForexDay, NRBError, Rate, fetch_forex_rates

__all__ = ["ForexDay", "NRBError", "Rate", "fetch_forex_rates"]
