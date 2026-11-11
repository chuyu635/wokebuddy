#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键抓取 WorkBuddy 登录态。

流程：
  1. 确认调试端口 9222 可用；没开就用 --remote-debugging-port 拉起客户端
  2. 在页面里注入 fetch/XHR 钩子并重载，让它自己发出带 Authorization 的请求
  3. 取 token + user_id 写入 config.json
  4. 把 token 复制到剪贴板，方便贴进 GitHub Secrets
"""

import json
import os
import subprocess
import sys
import time

import cdp_eval as C

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.json")
EXE = r"D:\wokebuddy\WorkBuddy\WorkBuddy.exe"
PORT = 9222

HOOK = r"""
(function(){
  if (window.__cap) return;
  window.__cap = [];
  function norm(h){
    var out = {};
    if (!h) return out;
    try {
      if (typeof h.forEach === 'function' && !Array.isArray(h)) { h.forEach(function(v,k){ out[k]=v; }); }
      else if (Array.isArray(h)) { h.forEach(function(p){ out[p[0]]=p[1]; }); }
      else { Object.keys(h).forEach(function(k){ out[k]=String(h[k]); }); }
    } catch(e) {}
    return out;
  }
  var of = window.fetch;
  if (of) {
    window.fetch = function(input, init){
      try {
        var url = (input && input.url) ? input.url : String(input);
        var headers = norm(init && init.headers);
        if (Object.keys(headers).length === 0 && input && input.headers) headers = norm(input.headers);
        window.__cap.push({kind:'fetch', url: url, headers: headers});
      } catch(e) {}
      return of.apply(this, arguments);
    };
  }
  var op = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.setRequestHeader = function(k, v){
    try { this.__h = this.__h || {}; this.__h[k] = v; } catch(e) {}
    return op.apply(this, arguments);
  };
  var oo = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(m, u){
    var self = this;
    self.__u = u;
    self.addEventListener('loadend', function(){
      try { window.__cap.push({kind:'xhr', url:String(self.__u), headers:self.__h||{}, status:self.status}); } catch(e) {}
    });
    return oo.apply(this, arguments);
  };
})();
"""


def port_open():
    import socket
    s = socket.socket()
    s.settimeout(1.0)
    try:
        s.connect(("127.0.0.1", PORT))
        return True
    except Exception:
        return False
    finally:
        s.close()


def launch():
    if not os.path.isfile(EXE):
        print(f"[!] 找不到客户端：{EXE}")
        return False
    print("[*] 启动 WorkBuddy（调试模式）...")
    try:
        subprocess.Popen([EXE, f"--remote-debugging-port={PORT}"], close_fds=True)
    except Exception as exc:
        print(f"[!] 启动失败：{exc}")
        return False
    return True


def main():
    print("=" * 56)
    print(" WorkBuddy 登录态抓取")
    print("=" * 56)

    if not port_open():
        if not launch():
            return 1
        for _ in range(60):
            time.sleep(1)
            if port_open():
                break

    if not port_open():
        print()
        print("[!] 调试端口没起来。最常见的原因：WorkBuddy 已经在运行（普通方式启动的），")
        print("    单实例机制会让新启动的进程直接退出，端口就不会打开。")
        print("    解决办法：右下角托盘图标右键 → 退出，然后重新运行本脚本。")
        return 1

    print("[+] 调试端口已就绪")

    page = C.pick_page()
    ws = C.WS(page["webSocketDebuggerUrl"])
    counter = [0]

    def cmd(method, params=None):
        counter[0] += 1
        mid = counter[0]
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == mid:
                return msg

    try:
        cmd("Page.enable")
        cmd("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK})
        print("[*] 注入钩子并重载客户端页面（界面会闪一下，正常）...")
        cmd("Page.reload", {"ignoreCache": False})

        token = ""
        for _ in range(25):
            time.sleep(1)
            res = cmd("Runtime.evaluate", {
                "expression": "JSON.stringify((window.__cap||[]).map(function(x){"
                              "var a=x.headers&&(x.headers.Authorization||x.headers.authorization);"
                              "return a||'';}).filter(function(s){return s.indexOf('Bearer ')==0;}))",
                "returnByValue": True,
            })
            val = res.get("result", {}).get("result", {}).get("value") or "[]"
            arr = json.loads(val)
            if arr:
                token = arr[0][7:].strip()
                break
    finally:
        ws.close()

    if not token:
        print("[!] 没抓到 token。请确认客户端已经登录，然后重试。")
        return 1

    parts = token.split(".")
    payload = {}
    if len(parts) >= 2:
        import base64
        p = parts[1] + "=" * (-len(parts[1]) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(p.encode()).decode("utf-8"))
        except Exception:
            payload = {}

    uid = payload.get("sub", "")
    exp = payload.get("exp")

    cfg = {}
    if os.path.isfile(CONFIG):
        try:
            with open(CONFIG, "r", encoding="utf-8-sig") as fh:
                cfg = json.load(fh) or {}
        except Exception:
            cfg = {}
    cfg.setdefault("auth", {})
    cfg["auth"]["access_token"] = token
    cfg["auth"]["user_id"] = uid
    cfg["api_base"] = "https://www.workbuddy.cn"
    cfg["api_domain_header"] = "www.workbuddy.cn"
    with open(CONFIG, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    # 复制到剪贴板（token 是纯 ASCII，用 clip 最省事）
    try:
        subprocess.run("clip", input=token.encode("ascii"), check=False, shell=True)
        copied = True
    except Exception:
        copied = False

    import datetime
    exp_txt = datetime.datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M") if exp else "未知"

    print()
    print("[+] 抓取成功，已写入", CONFIG)
    print("    昵称     :", payload.get("nickname", ""))
    print("    user_id  :", uid)
    print("    token    :", token[:12] + "…" + token[-4:], f"({len(token)} 字符)")
    print("    到期时间 :", exp_txt)
    print()
    if copied:
        print("[+] token 已复制到剪贴板，直接粘贴到 GitHub 的 WB_ACCESS_TOKEN 即可")
    print("    下一步：双击 2-试跑签到.bat 做一次真实领取验证")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
