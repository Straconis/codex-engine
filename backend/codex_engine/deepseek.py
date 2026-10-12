"""User-funded DeepSeek formatting. Only explicit formatting sends book text."""
import json
import urllib.error
import urllib.request
from .ai_format import AIUnavailable, AICancelled

BASE_URL = "https://api.deepseek.com"
ERRORS = {
    401: "DeepSeek rejected the API key. Replace it in Settings.",
    402: "DeepSeek API balance is insufficient. Manage billing or top up at platform.deepseek.com.",
    429: "DeepSeek rate limit reached. Try again later.",
}


class DeepSeekClient:
    def __init__(self, key: str, model: str):
        self.key = key
        self.model = model
        self.cache_model = "deepseek/" + model

    def balance(self):
        self.list_models()
        req = urllib.request.Request(BASE_URL + "/user/balance", headers={"Authorization": "Bearer " + self.key})
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                data = json.loads(response.read())
            return {"available": bool(data.get("is_available")),
                    "balance": "; ".join(f"{b.get('currency', '')} {b.get('total_balance', '')}" for b in data.get("balance_infos", []))}
        except urllib.error.HTTPError as exc:
            raise AIUnavailable(ERRORS.get(exc.code, f"DeepSeek balance check failed (HTTP {exc.code}).")) from None
        except (urllib.error.URLError, OSError, ValueError, TypeError, AttributeError):
            raise AIUnavailable("Could not check DeepSeek balance. Check your internet connection and try again.") from None

    def list_models(self):
        # No background request or charge; validation happens on explicit use.
        if not self.key:
            raise AIUnavailable("Add your DeepSeek API key in Settings.")
        return [self.cache_model]

    def chat(self, model, messages, max_tokens=None, temperature=None,
             on_text=None, should_stop=None):
        if should_stop and should_stop():
            raise AICancelled("Cancelled")
        self.list_models()
        payload = {"model": self.model, "messages": messages, "stream": True,
                   "temperature": 0.0 if temperature is None else temperature,
                   "thinking": {"type": "disabled"}}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        req = urllib.request.Request(BASE_URL + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.key})
        parts = []
        written = 0
        done = False
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                for raw in iter(lambda: response.readline(65536), b""):
                    if len(raw) == 65536 and not raw.endswith(b"\n"):
                        raise AIUnavailable("The model returned an oversized stream event. The page was not saved.")
                    if should_stop and should_stop():
                        raise AICancelled("Cancelled")
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done = True
                        break
                    event = json.loads(data)
                    if event.get("error"):
                        raise AIUnavailable("DeepSeek returned an API error. Check your account and try again.")
                    for choice in event.get("choices", []):
                        piece = choice.get("delta", {}).get("content") or ""
                        parts.append(piece)
                        written += len(piece)
                        if written > 2 * 1024 * 1024:
                            raise AIUnavailable("The model reply exceeded the safety limit. The page was not saved.")
                        if piece and on_text:
                            on_text(written)
                        if choice.get("finish_reason") == "stop":
                            done = True
                        if choice.get("finish_reason") == "length":
                            raise AIUnavailable("DeepSeek truncated the reply. The page was not saved; try a smaller section.")
        except urllib.error.HTTPError as exc:
            # Never echo a response body or request headers containing credentials.
            raise AIUnavailable(ERRORS.get(exc.code, f"DeepSeek request failed (HTTP {exc.code}). Try again later.")) from None
        except (urllib.error.URLError, OSError, ValueError, TypeError, AttributeError) as exc:
            raise AIUnavailable("Could not reach DeepSeek or read its reply. Check your internet connection and try again.") from None
        if should_stop and should_stop():
            raise AICancelled("Cancelled")
        if not done:
            raise AIUnavailable("DeepSeek disconnected before its reply was complete. The page was not saved.")
        return "".join(parts)
