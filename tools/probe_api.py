#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在客户端页面里执行真实 API 调用，用来验证接口契约（不经过本机被封的网络）。

用法：
    python tools/probe_api.py status
    python tools/probe_api.py claim
"""

import json
import os
import sys

import cdp_eval as C

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.json")

PATHS = {
    "status": "/v2/billing/meter/checkin-activity-status",
    "claim": "/v2/billing/meter/daily-checkin",
    "refresh": "/v2/plugin/auth/token/refresh",
}


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "status"
    if which not in PATHS:
        print("用法: probe_api.py status|claim", file=sys.stderr)
        return 2

    with open(CONFIG, "r", encoding="utf-8-sig") as fh:
        cfg = json.load(fh)
    token = cfg["auth"]["access_token"]
    uid = cfg["auth"]["user_id"]
    base = cfg.get("api_base", "https://www.workbuddy.cn")
    domain = cfg.get("api_domain_header", "www.workbuddy.cn")
    url = base + PATHS[which]

    js = """
(async () => {
  const r = await fetch(%s, {
    method: 'POST',
    credentials: 'omit',
    headers: {
      'Content-Type': 'application/json',
      'Accept': 'application/json, text/plain, */*',
      'Authorization': 'Bearer %s',
      'X-User-Id': '%s',
      'X-Domain': '%s',
      'X-Request-ID': (crypto.randomUUID ? crypto.randomUUID().replace(/-/g,'') : String(Date.now()))
    },
    body: '{}'
  });
  const text = await r.text();
  return JSON.stringify({http: r.status, body: text});
})()
""" % (json.dumps(url), token, uid, domain)

    msg = C.evaluate(js, timeout=30)
    result = msg.get("result", {})
    if "exceptionDetails" in result:
        print("异常:", json.dumps(result["exceptionDetails"], ensure_ascii=False)[:500])
        return 1
    print(result.get("result", {}).get("value", "")[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
