"""Local-only integration fixture. Never forwards requests or logs credentials."""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        anthropic = self.path.endswith('/messages')
        valid_auth = (self.headers.get('x-api-key') == 'fixture-key' and self.headers.get('anthropic-version') == '2023-06-01') if anthropic else self.headers.get('Authorization') == 'Bearer fixture-key'
        if not valid_auth:
            self.send_error(401)
            return
        if body['model'] == 'slow':
            time.sleep(3)
        if body['model'] == 'redirect':
            self.send_response(307)
            self.send_header('Location', '/v1/messages')
            self.end_headers()
            return
        system = body.get('system', '') if anthropic else body['messages'][0]['content']
        text = json.dumps({'summary': '这是本地测试摘要。', 'score': 87, 'reason': '主题匹配。'}, ensure_ascii=False) if '只输出 JSON' in system else '本地测试报告：方法、实验依据和局限。'
        if body['model'] == 'bad-json':
            text = 'not screening JSON'
        response = {'content': [{'type': 'text', 'text': text}], 'stop_reason': 'end_turn'} if anthropic else {'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}]}
        data = json.dumps(response, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass

server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
with open(sys.argv[1], 'w') as output:
    output.write(str(server.server_port))
server.serve_forever()
