#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wb_checkin.py 的离线自测：用本地假接口把每条分支都跑一遍。

运行： python tests/test_wb_checkin.py
不需要联网，也不会碰真实账号。
"""

import base64
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import wb_checkin as wb  # noqa: E402


# ------------------------------------------------------------------ 假服务端

STATE = {
    "status_mode": "ok",       # ok | 401 | 500 | code_err
    "status_payload": {"active": True, "today_checked_in": False, "streak_days": 5,
                       "is_streak_day": False, "next_streak_day": 0,
                       "today_credit": 100, "daily_credit": 100, "total_credits": 500,
                       "streak_bonus_days": 0, "streak_bonus_credit": 0},
    "claim_mode": "ok",        # ok | already | 401 | 500 | bizerr
    "status_calls": 0,
    "claim_calls": 0,
    "last_headers": {},
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    def _send(self, code, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        STATE["last_headers"] = dict(self.headers)

        if self.path.endswith(wb.STATUS_PATH):
            STATE["status_calls"] += 1
            mode = STATE["status_mode"]
            if mode == "401":
                return self._send(401, {"code": 401, "msg": "unauthorized"})
            if mode == "500":
                return self._send(500, {"code": 500, "msg": "server error"})
            if mode == "code_err":
                return self._send(200, {"code": 10086, "msg": "账号异常"})
            return self._send(200, {"code": 0, "data": dict(STATE["status_payload"])})

        if self.path.endswith(wb.CLAIM_PATH):
            STATE["claim_calls"] += 1
            mode = STATE["claim_mode"]
            if mode == "401":
                return self._send(401, {"code": 401, "msg": "token expired"})
            if mode == "500":
                return self._send(500, {"code": 500, "msg": "server error"})
            if mode == "bizerr":
                return self._send(200, {"code": 40004, "msg": "活动已结束"})
            if mode == "already":
                # 官方真实行为：HTTP 400 + code 10001
                return self._send(400, {"code": 10001, "msg": "今天已签到，请明天再来"})
            return self._send(200, {"code": 0, "msg": "OK",
                                    "data": {"credit": 100, "streak_days": 6,
                                             "is_streak_day": False}})

        return self._send(404, {"code": 404, "msg": "not found"})


def make_jwt(exp_offset_days=30):
    def b64(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = b64({"alg": "HS256", "typ": "JWT"})
    payload = b64({
        "sub": "user-123",
        "nickname": "测试号",
        "exp": int(time.time()) + int(exp_offset_days * 86400),
    })
    return f"{header}.{payload}.signature"


# ------------------------------------------------------------------ 测试

class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 测试绝不能被本机真实的 config.json 干扰（里面有真 token 和真域名）
        cls._orig_default = wb.DEFAULT_CONFIG_PATH
        wb.DEFAULT_CONFIG_PATH = os.path.join(HERE, "no-such-config.json")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        wb.DEFAULT_CONFIG_PATH = cls._orig_default
        cls.server.shutdown()

    def setUp(self):
        STATE.update({
            "status_mode": "ok",
            "claim_mode": "ok",
            "status_calls": 0,
            "claim_calls": 0,
            "last_headers": {},
            "status_payload": {"active": True, "today_checked_in": False, "streak_days": 5,
                               "is_streak_day": False, "next_streak_day": 0,
                               "today_credit": 100, "daily_credit": 100,
                               "total_credits": 500, "streak_bonus_days": 0,
                               "streak_bonus_credit": 0},
        })
        os.environ["WB_API_BASE"] = f"http://127.0.0.1:{self.port}"
        os.environ["WB_ACCESS_TOKEN"] = make_jwt(30)
        os.environ["WB_USER_ID"] = "user-123"
        os.environ.pop("WB_CONFIG", None)

    def call(self, *argv):
        """跑 main()，把 SystemExit 也归一成退出码。"""
        try:
            return wb.main(list(argv) + ["--timeout", "5", "--retries", "0"])
        except SystemExit as exc:
            return int(exc.code or 0)

    def test_claim_success(self):
        code = self.call("--config", os.path.join(HERE, "no-such-config.json"))
        self.assertEqual(code, 0)
        self.assertEqual(STATE["claim_calls"], 1)
        self.assertEqual(STATE["status_calls"], 2)  # 查一次 + 复核一次

    def test_skip_when_already_checked(self):
        STATE["status_payload"]["today_checked_in"] = True
        code = self.call()
        self.assertEqual(code, 0)
        self.assertEqual(STATE["claim_calls"], 0, "已签到就不该再调领取接口")

    def test_claim_reports_already(self):
        STATE["claim_mode"] = "already"
        code = self.call()
        self.assertEqual(code, 0, "接口说已签到时算正常，不算失败")
        self.assertEqual(STATE["claim_calls"], 1)

    def test_claim_401_is_auth_error(self):
        STATE["claim_mode"] = "401"
        code = self.call()
        self.assertEqual(code, wb.EXIT_AUTH)
        self.assertEqual(STATE["claim_calls"], 1)

    def test_status_500_is_network_error(self):
        STATE["status_mode"] = "500"
        code = self.call()
        self.assertEqual(code, wb.EXIT_NET)
        self.assertEqual(STATE["claim_calls"], 0)

    def test_biz_error(self):
        STATE["claim_mode"] = "bizerr"
        code = self.call()
        self.assertEqual(code, wb.EXIT_BIZ)

    def test_status_code_error(self):
        STATE["status_mode"] = "code_err"
        code = self.call()
        self.assertEqual(code, wb.EXIT_BIZ)
        self.assertEqual(STATE["claim_calls"], 0)

    def test_dry_run_does_not_claim(self):
        code = self.call("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(STATE["claim_calls"], 0)

    def test_status_only(self):
        code = self.call("--status")
        self.assertEqual(code, 0)
        self.assertEqual(STATE["status_calls"], 1)
        self.assertEqual(STATE["claim_calls"], 0)

    def test_headers_sent(self):
        self.call()
        headers = {k.lower(): v for k, v in STATE["last_headers"].items()}
        self.assertTrue(headers.get("authorization", "").startswith("Bearer "))
        self.assertEqual(headers.get("x-user-id"), "user-123")
        self.assertEqual(headers.get("x-domain"), "127.0.0.1:%d" % self.port)
        self.assertTrue(headers.get("x-request-id"), "应该带上 X-Request-ID")

    def test_missing_token_exits_2(self):
        os.environ.pop("WB_ACCESS_TOKEN", None)
        os.environ["WB_CONFIG"] = os.path.join(HERE, "definitely-absent.json")
        try:
            code = self.call()
        finally:
            os.environ.pop("WB_CONFIG", None)
        self.assertEqual(code, wb.EXIT_CONFIG)

    def test_expired_token_refuses_early(self):
        os.environ["WB_ACCESS_TOKEN"] = make_jwt(-1)
        code = self.call()
        self.assertEqual(code, wb.EXIT_AUTH)
        self.assertEqual(STATE["status_calls"], 0, "过期就不该打接口")


class TestParsing(unittest.TestCase):
    def test_parse_curl_bash(self):
        text = """curl 'https://copilot.tencent.com/billing/meter/daily-checkin' \\
  -H 'authorization: Bearer abc123def456ghi789jkl012mno345' \\
  -H 'x-user-id: 1234567890' \\
  --data-raw '{}'"""
        creds = wb.parse_curl(text)
        self.assertEqual(creds["access_token"], "abc123def456ghi789jkl012mno345")
        self.assertEqual(creds["user_id"], "1234567890")

    def test_parse_curl_cmd_style(self):
        text = ('curl "https://copilot.tencent.com/billing/meter/checkin-status" '
                '-H "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjF9.sig" '
                '-H "X-User-Id: u_998877"')
        creds = wb.parse_curl(text)
        self.assertTrue(creds["access_token"].startswith("eyJ"))
        self.assertEqual(creds["user_id"], "u_998877")

    def test_parse_curl_rejects_garbage(self):
        with self.assertRaises(ValueError):
            wb.parse_curl("GET / HTTP/1.1\nHost: example.com")

    def test_jwt_expiry(self):
        exp = wb.token_expiry(make_jwt(5))
        self.assertIsNotNone(exp)
        self.assertGreater(exp.timestamp(), time.time())

    def test_jwt_garbage(self):
        self.assertIsNone(wb.token_expiry("not-a-jwt"))

    def test_mask_never_leaks_full_token(self):
        token = "supersecrettokenvalue1234567890"
        masked = wb.mask(token)
        self.assertNotIn(token, masked)
        self.assertIn("…", masked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
