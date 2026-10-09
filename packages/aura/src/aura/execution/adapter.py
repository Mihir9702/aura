"""Paper adapter protocol and the SHADOW fixture adapter identity.

No vendor, endpoint, credential or live path exists. The fixture identity names the synthetic
adapter account whose receipts the fixture harness reports; it is not configuration.
"""

from typing import Protocol

# One simulated adapter account serves every SHADOW fixture portfolio, so a receipt from it that
# names no known order cannot be attributed to a single portfolio.
FIXTURE_ADAPTER_ID = "FIXTURE_SHADOW_ADAPTER"
FIXTURE_ACCOUNT_ID = "FIXTURE_SHADOW_ACCOUNT"


class PaperAdapter(Protocol):
    def submit(self, client_order_id: str) -> str: ...
    def query(self, client_order_id: str) -> str | None: ...
