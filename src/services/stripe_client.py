"""AgentCore Platform v1.0"""

# StripeClient — CMN-C1-596
# Read a charge and create a single refund over the Stripe REST API.
# - Credentials come from the bound secret provider ONLY
#   (current_secrets().require("STRIPE_API_KEY")) — never from state or os.environ.
# - S-3 egress guard: only the api.stripe.com host is permitted, so a misconfiguration
#   cannot exfiltrate the key off-platform.
# - The refund write sends an Idempotency-Key header (POST only) so a retry of the
#   same logical refund returns the original refund instead of creating a second one.
# Constructor-injected into ChargeVerifyNode and RefundExecuteNode.
# MUST NOT be instantiated inside execute().

from __future__ import annotations

from typing import Any

from urllib.parse import urlsplit

import requests

from framework.secrets.context import current_secrets

_STRIPE_HOST = "api.stripe.com"
_TIMEOUT = 15  # seconds


class StripeClientError(RuntimeError):
    """Raised on a non-recoverable Stripe API error."""


class StripeClient:
    """Thin Stripe REST client for charge verification + refund creation.

    All traffic is pinned to ``https://api.stripe.com`` by the S-3 egress guard so the
    secret key cannot leak off-platform.
    """

    def __init__(self, base_url: str = "https://api.stripe.com", timeout: int = _TIMEOUT) -> None:
        self._base_url = (base_url or "https://api.stripe.com").rstrip("/")
        self._timeout = timeout
        self._guard_egress(self._base_url)

    @staticmethod
    def _guard_egress(url: str) -> None:
        host = urlsplit(url).hostname or ""
        if host != _STRIPE_HOST:
            raise StripeClientError(f"S-3 egress guard: host '{host}' is not permitted (only {_STRIPE_HOST}).")

    @staticmethod
    def credential_available() -> bool:
        """True when the Stripe API key is provisioned in the bound secret provider.

        Non-raising probe (``get`` returns ``None`` on a miss) used by the caller to
        FAIL-CLOSED before any live HTTP call when no credential is present — e.g. the
        STG smoke, which has no ``STRIPE_API_KEY`` and must not attempt a real Stripe
        call. The key is never cached on the instance; this only checks presence.
        """
        return bool(current_secrets().get("STRIPE_API_KEY"))

    def _headers(self, idempotency_key: str = "") -> dict[str, Any]:
        # Key fetched per call from the bound provider; never cached on self.
        key = current_secrets().require("STRIPE_API_KEY")
        headers: dict[str, Any] = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _url(self, path: str) -> str:
        url = f"{self._base_url}{path}"
        self._guard_egress(url)
        return url

    # ── Read ────────────────────────────────────────────────────────────────
    def get_charge(self, charge_id: str) -> dict[str, Any]:
        """GET /v1/charges/{id} → normalized charge facts."""
        url = self._url(f"/v1/charges/{charge_id}")
        try:
            resp = requests.get(url, headers=self._headers(), timeout=self._timeout)
        except requests.RequestException as exc:
            # Timeout/ConnectionError etc. must surface as StripeClientError so the
            # nodes' `except StripeClientError` handles it (a graceful read failure),
            # not escape as an uncaught crash.
            raise StripeClientError(f"GET charge transport error: {exc}") from exc
        if resp.status_code == 404:
            raise StripeClientError(f"charge '{charge_id}' not found (404).")
        if not resp.ok:
            raise StripeClientError(f"GET charge failed ({resp.status_code}).")
        return self.normalize_charge(resp.json())

    # ── Write ─────────────────────────────────────────────────────────────────
    def create_refund(
        self,
        idempotency_key: str,
        charge_id: str = "",
        payment_intent: str = "",
        amount: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """POST /v1/refunds — create a single refund (idempotent via header)."""
        body: dict[str, Any] = {}
        if charge_id:
            body["charge"] = charge_id
        elif payment_intent:
            body["payment_intent"] = payment_intent
        else:
            raise StripeClientError("create_refund requires a charge or payment_intent.")
        if amount is not None:
            body["amount"] = int(amount)
        if reason:
            body["reason"] = reason

        url = self._url("/v1/refunds")
        try:
            resp = requests.post(
                url, headers=self._headers(idempotency_key=idempotency_key), data=body, timeout=self._timeout
            )
        except requests.RequestException as exc:
            # Transport failure on the refund POST: raise StripeClientError so the
            # caller renders a definite failure (operator can reconcile) rather than
            # an uncaught crash that leaves the refund state unknown.
            raise StripeClientError(f"POST refund transport error: {exc}") from exc
        if not resp.ok:
            raise StripeClientError(f"POST refund failed ({resp.status_code}).")
        data = resp.json()
        return {
            "refund_id": str(data.get("id", "")),
            "amount": int(data.get("amount", 0)),
            "currency": str(data.get("currency", "")),
            "status": str(data.get("status", "")),
        }

    # ── Mapping helper (pure — unit-testable without HTTP) ───────────────────
    @staticmethod
    def normalize_charge(raw: dict[str, Any]) -> dict[str, Any]:
        """Map a Stripe charge object to our canonical dimensions.

        ``qualified_invoice_issued`` reflects the charge metadata flag used by the
        Japan invoice check; ``None`` means the flag was not present (conservative
        path).
        """
        meta = raw.get("metadata") or {}
        flag = meta.get("qualified_invoice_issued")
        if flag is None:
            qual = None
        else:
            qual = str(flag).strip().lower() in ("true", "1", "yes")
        return {
            "found": True,
            "amount": int(raw.get("amount", 0)),
            "amount_refunded": int(raw.get("amount_refunded", 0)),
            "currency": str(raw.get("currency", "")).lower(),
            "refunded": bool(raw.get("refunded", False)),
            "status": str(raw.get("status", "")),
            "qualified_invoice_issued": qual,
        }
