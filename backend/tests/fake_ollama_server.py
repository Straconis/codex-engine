"""Stands in for `ollama serve` in manager tests. Honours OLLAMA_HOST / OLLAMA_MODELS like the real one."""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

host, port = os.environ["OLLAMA_HOST"].rsplit(":", 1)
models_dir = os.environ.get("OLLAMA_MODELS", "")
pulled = Path(models_dir or ".") / "pulled.txt"

if "--exit-immediately" in sys.argv:
    sys.exit(3)

# Real `ollama serve` starts model runner subprocesses; mimic one so tests can check they get stopped too.
runner_pid = None
if "--spawn-runner" in sys.argv:
    import subprocess

    # A plain native program, like Ollama's runners. (Not Python: a Microsoft Store Python
    # child is launched by Windows outside our Job Object, which real runners aren't.)
    cmd = ["ping", "-n", "120", "127.0.0.1"] if sys.platform.startswith("win") else ["sleep", "120"]
    runner_pid = subprocess.Popen(cmd, stdout=subprocess.DEVNULL).pid


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/version":
            self._json({"version": "fake", "models_dir": models_dir, "pid": os.getpid(), "runner_pid": runner_pid})
        elif self.path == "/api/tags":
            names = pulled.read_text().split() if pulled.exists() else []
            self._json({"models": [{"name": n} for n in names]})
        else:
            self.send_error(404)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/chat":  # "formats" by returning the section unchanged
            self._json({"message": {"role": "assistant", "content": req["messages"][-1]["content"]}})
            return
        if self.path != "/api/pull":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        if req["model"] == "missing:1b":
            self.wfile.write(b'{"error":"pull model manifest: file does not exist"}\n')
            return
        for done in (0, 50, 100):
            self.wfile.write(json.dumps({"status": "downloading", "total": 100, "completed": done}).encode() + b"\n")
        with pulled.open("a") as fh:
            fh.write(req["model"] + "\n")
        self.wfile.write(b'{"status":"success"}\n')

    def log_message(self, *args):
        pass


ThreadingHTTPServer((host, int(port)), Handler).serve_forever()
