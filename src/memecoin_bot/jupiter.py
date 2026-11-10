from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from solders.pubkey import Pubkey

from .constants import WSOL_MINT
from .models import RouteQuote


class JupiterError(RuntimeError):
    """Raised when Jupiter returns an invalid or unsafe response."""


class JupiterClient:
    BASE_URL = "https://api.jup.ag"

    def __init__(self, api_key: str | None, *, timeout_seconds: float = 12.0) -> None:
        headers = {"Accept": "application/json", "User-Agent": "memecoin-bot/0.1"}
        if api_key:
            headers["x-api-key"] = api_key
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds, connect=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> JupiterClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.get(path, **kwargs)
        except httpx.RequestError as exc:
            raise JupiterError("Jupiter transport is unavailable") from exc

    def _post(self, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.post(path, **kwargs)
        except httpx.RequestError as exc:
            raise JupiterError("Jupiter transport is unavailable") from exc

    @staticmethod
    def _parse_json(response: httpx.Response) -> dict[str, Any]:
        if len(response.content) > 5_000_000:
            raise JupiterError("Jupiter response exceeded the 5 MB safety limit")
        try:
            data = response.json()
        except ValueError as exc:
            raise JupiterError(f"Jupiter request failed with HTTP {response.status_code}") from exc
        if not isinstance(data, dict):
            raise JupiterError("Jupiter returned a non-object response")
        if response.is_error or data.get("error") or data.get("errorCode"):
            detail = data.get("errorMessage") or data.get("error") or data.get("errorCode")
            if not isinstance(detail, str) or not detail.strip():
                detail = f"HTTP {response.status_code}"
            detail = detail.strip()[:300]
            raise JupiterError(f"Jupiter rejected the request: {detail}")
        return data

    @staticmethod
    def _price_impact_bps(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return max(0, int(Decimal(str(value)) * 10_000))
        except (InvalidOperation, ValueError):
            raise JupiterError("Jupiter returned an invalid price impact") from None

    def sol_usd_reference(self) -> tuple[Decimal, int]:
        """Return Jupiter's SOL/USD reference and its on-chain block ID."""
        response = self._get("/price/v3", params={"ids": WSOL_MINT})
        data = self._parse_json(response)
        sol = data.get(WSOL_MINT)
        if (
            not isinstance(sol, dict)
            or type(sol.get("decimals")) is not int
            or sol["decimals"] != 9
        ):
            raise JupiterError("Jupiter SOL price metadata is invalid")
        block_id = sol.get("blockId")
        if type(block_id) is not int or block_id <= 0:
            raise JupiterError("Jupiter SOL price block ID is invalid")
        value = sol.get("usdPrice")
        if value is None or isinstance(value, bool):
            raise JupiterError("Jupiter SOL price is invalid")
        try:
            price = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise JupiterError("Jupiter SOL price is invalid") from exc
        if not price.is_finite() or not 0 < price < 1_000_000:
            raise JupiterError("Jupiter SOL price is invalid")
        return price, block_id

    def quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int = 50,
    ) -> RouteQuote:
        if amount <= 0:
            raise JupiterError("Quote amount must be positive")
        if not 1 <= slippage_bps <= 2_000:
            raise JupiterError("Quote slippage must be 1-2000 basis points")
        for value in (input_mint, output_mint):
            try:
                Pubkey.from_string(value)
            except ValueError as exc:
                raise JupiterError("Quote mints must be valid Solana public keys") from exc
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
        }
        api_version = "v2"
        try:
            data = self._parse_json(self._get("/swap/v2/order", params=params))
        except JupiterError as v2_error:
            try:
                data = self._parse_json(self._get("/swap/v1/quote", params=params))
                api_version = "v1"
            except JupiterError as v1_error:
                raise JupiterError(
                    f"Jupiter quote unavailable (v2: {v2_error}; v1: {v1_error})"
                ) from v1_error
        try:
            in_amount = int(data["inAmount"])
            out_amount = int(data["outAmount"])
        except (KeyError, TypeError, ValueError) as exc:
            raise JupiterError("Jupiter quote omitted valid amounts") from exc
        if in_amount != amount or out_amount <= 0:
            raise JupiterError("Jupiter quote amount did not match the request")
        return RouteQuote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=in_amount,
            out_amount=out_amount,
            price_impact_bps=self._price_impact_bps(data.get("priceImpactPct")),
            router=str(data["router"]) if data.get("router") else None,
            fee_bps=int(data["feeBps"]) if data.get("feeBps") is not None else None,
            raw=data,
            api_version=api_version,
        )

    @classmethod
    def quote_from_build(
        cls,
        data: dict[str, Any],
        *,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int,
    ) -> RouteQuote:
        """Use the output of the fee-free Metis transaction actually built."""
        if (
            data.get("inputMint") != input_mint
            or data.get("outputMint") != output_mint
            or str(data.get("inAmount")) != str(amount)
        ):
            raise JupiterError("Jupiter build response changed the swap identity")
        try:
            out_amount = int(data["outAmount"])
            returned_slippage = int(data.get("slippageBps", slippage_bps))
        except (KeyError, TypeError, ValueError) as exc:
            raise JupiterError("Jupiter build response omitted valid quote values") from exc
        if out_amount <= 0 or not 1 <= returned_slippage <= slippage_bps:
            raise JupiterError("Jupiter build response has an invalid output or slippage")
        if not isinstance(data.get("swapInstruction"), dict):
            raise JupiterError("Jupiter build omitted the swap instruction")
        if data.get("tipInstruction") is not None:
            raise JupiterError("Unexpected tip instruction in self-managed RPC mode")
        return RouteQuote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=amount,
            out_amount=out_amount,
            price_impact_bps=cls._price_impact_bps(data.get("priceImpactPct")),
            router="metis",
            fee_bps=0,
            raw=data,
            api_version="v2-build",
        )

    def router_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        *,
        taker: str,
        slippage_bps: int = 50,
    ) -> RouteQuote:
        """Get a read-only quote from the same fee-free router used for builds."""
        if amount <= 0 or not 1 <= slippage_bps <= 2_000:
            raise JupiterError("Router quote amount or slippage is invalid")
        for value in (input_mint, output_mint, taker):
            try:
                Pubkey.from_string(value)
            except ValueError as exc:
                raise JupiterError("Router quote addresses must be valid public keys") from exc
        data = self._parse_json(
            self._get(
                "/swap/v2/build",
                params={
                    "inputMint": input_mint,
                    "outputMint": output_mint,
                    "amount": str(amount),
                    "taker": taker,
                    "slippageBps": str(slippage_bps),
                    "platformFeeBps": "0",
                    "transactionVersion": "0",
                    "wrapAndUnwrapSol": "true",
                    "computeUnitPricePercentile": "high",
                },
            )
        )
        return self.quote_from_build(
            data,
            input_mint=input_mint,
            output_mint=output_mint,
            amount=amount,
            slippage_bps=slippage_bps,
        )

    def build(
        self,
        *,
        input_mint: str,
        output_mint: str,
        amount: int,
        taker: str,
        slippage_bps: int,
        quote: RouteQuote,
        max_priority_fee_lamports: int,
    ) -> dict[str, Any]:
        if quote.input_mint != input_mint or quote.output_mint != output_mint:
            raise JupiterError("Quote mints do not match the build request")
        if quote.in_amount != amount:
            raise JupiterError("Quote amount does not match the build request")
        if quote.api_version == "v2-build":
            self.quote_from_build(
                quote.raw,
                input_mint=input_mint,
                output_mint=output_mint,
                amount=amount,
                slippage_bps=slippage_bps,
            )
            return quote.raw
        if quote.api_version == "v1":
            response = self._post(
                "/swap/v1/swap-instructions",
                json={
                    "quoteResponse": quote.raw,
                    "userPublicKey": taker,
                    "wrapAndUnwrapSol": True,
                    "dynamicComputeUnitLimit": False,
                    "prioritizationFeeLamports": {
                        "priorityLevelWithMaxLamports": {
                            "maxLamports": max_priority_fee_lamports,
                            "priorityLevel": "high",
                            "global": False,
                        }
                    },
                },
            )
            data = self._parse_json(response)
            if not isinstance(data.get("swapInstruction"), dict):
                raise JupiterError("Jupiter v1 omitted the swap instruction")
            data["_expectedInputAmount"] = amount
            data["_expectedInputMint"] = input_mint
            return data

        response = self._get(
            "/swap/v2/build",
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "taker": taker,
                "slippageBps": str(slippage_bps),
                "platformFeeBps": "0",
                "transactionVersion": "0",
                "wrapAndUnwrapSol": "true",
                "computeUnitPricePercentile": "high",
            },
        )
        data = self._parse_json(response)
        expected = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "inAmount": str(amount),
        }
        for field, value in expected.items():
            if str(data.get(field)) != value:
                raise JupiterError(f"Jupiter build response changed {field}")
        if not isinstance(data.get("swapInstruction"), dict):
            raise JupiterError("Jupiter build omitted the swap instruction")
        if data.get("tipInstruction") is not None:
            raise JupiterError("Unexpected tip instruction in self-managed RPC mode")
        if int(data.get("slippageBps", slippage_bps)) > slippage_bps:
            raise JupiterError("Jupiter build exceeded the requested slippage")
        data["_expectedInputAmount"] = amount
        data["_expectedInputMint"] = input_mint
        return data
