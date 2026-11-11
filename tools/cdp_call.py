#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调用任意 CDP 方法，用于读 cookie、抓网络请求等。

用法：
    python tools/cdp_call.py Network.getAllCookies {}
    python tools/cdp_call.py Network.enable {}
"""

import json
import sys

import cdp_eval as C


def call(method: str, params: dict):
    page = C.pick_page()
    ws = C.WS(page["webSocketDebuggerUrl"])
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                return msg
    finally:
        ws.close()


if __name__ == "__main__":
    method = sys.argv[1] if len(sys.argv) > 1 else "Runtime.evaluate"
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    out = call(method, params)
    print(json.dumps(out, ensure_ascii=False, indent=2)[:6000])
