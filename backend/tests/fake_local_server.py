"""Portable fake llama-server for lifetime and loopback authorization tests."""
import argparse,json
from http.server import BaseHTTPRequestHandler,HTTPServer
parser=argparse.ArgumentParser()
parser.add_argument("--port",type=int,required=True)
parser.add_argument("--key-file",required=True)
args=parser.parse_args()
key=open(args.key_file,encoding="utf-8").read().strip()
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        self.send_response(200);self.end_headers();self.wfile.write(b'{"status":"ok"}')
    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer "+key:
            self.send_response(401);self.end_headers();return
        data=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text=data["messages"][-1]["content"]
        self.send_response(200);self.send_header("Content-Type","text/event-stream");self.end_headers()
        for piece in ["## ",text]:
            self.wfile.write(("data: "+json.dumps({"choices":[{"delta":{"content":piece},"finish_reason":None}]})+"\n\n").encode())
        self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
HTTPServer(("127.0.0.1",args.port),Handler).serve_forever()
