"""Local stand-in for an H Chat (Claude messages) gateway, to try `mode: hchat` without a real key.

  python tools/mock_hchat.py --port 8999 --key local-test-key
  # then run the Ops server with:
  #   H_CHAT_API_KEY=local-test-key  H_CHAT_BASE_URL=http://127.0.0.1:8999/claude/messages

Options: --latency SEC (per request), --fail-every N (every Nth request returns HTTP 500, to see retries),
--chatty (reviewer wraps its JSON in prose, like real models do).
The reviewer's score goes 5 -> 7 -> 9 across review requests; usage numbers are derived from text length.
"""
import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

lock = threading.Lock()
counts = {"req": 0, "review": 0, "write": 0}


def make_handler(args):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            print(f"[mock-hchat] {self.address_string()} {fmt % a}")

        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            with lock:
                counts["req"] += 1
                n = counts["req"]
            auth = self.headers.get("Authorization", "")
            if args.key and auth not in (f"Bearer {args.key}", args.key):
                return self._send(401, {"error": "unauthorized"})
            if auth == args.key and args.require_bearer:
                return self._send(401, {"error": "Bearer prefix required"})
            if args.fail_every and n % args.fail_every == 0:
                return self._send(500, {"error": "mock failure"})
            time.sleep(args.latency)

            prompt = req["messages"][0]["content"]
            if "심사위원" in prompt:
                with lock:
                    i = counts["review"]
                    counts["review"] += 1
                score = [5, 7, 9][min(i, 2)]
                names = re.findall(r"^- (.+?) \(가중치", prompt, re.M)
                js = json.dumps({"criteria": [{"name": nm, "score": score, "feedback": f"{nm}: 목표 {score}점 (mock)"} for nm in names]}, ensure_ascii=False)
                text = f"평가 결과입니다.\n{js}\n도움이 되길 바랍니다." if args.chatty else js
            else:
                with lock:
                    counts["write"] += 1
                    w = counts["write"]
                m = re.search(r"\[현재 초안\]\n(.*?)\n\[피드백\]", prompt, re.S)
                text = f"{(m.group(1) if m else '').strip()}\n[mock 개선 {w}]"
            usage = {"input_tokens": max(1, len(prompt) // 2), "output_tokens": max(1, len(text) // 2)}
            self._send(200, {"content": [{"type": "thinking", "thinking": "(hidden)"}, {"type": "text", "text": text}],
                             "usage": usage, "stop_reason": "end_turn", "model": req.get("model")})

    return H


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8999)
    ap.add_argument("--key", default="", help="expected API key (empty = accept anything)")
    ap.add_argument("--latency", type=float, default=0.3)
    ap.add_argument("--fail-every", type=int, default=0)
    ap.add_argument("--chatty", action="store_true")
    ap.add_argument("--require-bearer", action="store_true", help="reject keys sent without the 'Bearer ' prefix")
    a = ap.parse_args()
    print(f"[mock-hchat] http://127.0.0.1:{a.port}/claude/messages  key={'(any)' if not a.key else '(required)'}")
    ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a)).serve_forever()
