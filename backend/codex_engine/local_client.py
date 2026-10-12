"""Authenticated loopback client for the bundled llama.cpp runtime."""
import json
import urllib.error
import urllib.request
from .ai_format import AIUnavailable, AICancelled


class LocalClient:
    def __init__(self, config, token):
        self.config, self.token = config, token
        self.cache_model = config.model
        # Ignore system proxy settings for our own loopback server.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def list_models(self):
        return [self.cache_model]

    def chat(self, model, messages, max_tokens=None, temperature=None, on_text=None, should_stop=None):
        if should_stop and should_stop():
            raise AICancelled("Cancelled")
        payload = {"model": self.cache_model, "messages": messages, "stream": True,
                   "temperature": self.config.temperature if temperature is None else temperature}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        req = urllib.request.Request(self.config.url + "/v1/chat/completions", method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token})
        parts, written = [], 0
        done = False
        try:
            with self.opener.open(req, timeout=self.config.timeout) as response:
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
                        raise AIUnavailable("The local model could not complete this reply. Try again.")
                    for choice in event.get("choices", []):
                        piece = choice.get("delta", {}).get("content") or ""
                        parts.append(piece)
                        written += len(piece)
                        if written > 2 * 1024 * 1024:
                            raise AIUnavailable("The model reply exceeded the safety limit. The page was not saved.")
                        if piece and on_text:
                            on_text(written)
                        reason = choice.get("finish_reason")
                        if reason == "length":
                            raise AIUnavailable("The local model reply was truncated. The page was not saved.")
                        if reason == "stop":
                            done = True
        except (urllib.error.URLError, OSError, ValueError, TypeError, AttributeError):
            if should_stop and should_stop():
                raise AICancelled("Cancelled")
            raise AIUnavailable("Could not read a complete reply from the local model. Try again or check the local AI log.") from None
        if should_stop and should_stop():
            raise AICancelled("Cancelled")
        if not done:
            raise AIUnavailable("The local model connection ended before its reply was complete.")
        return "".join(parts)
