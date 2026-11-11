#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 每日自动签到 / 领取 100 积分（零第三方依赖，Python 3.8+）

设计要点
--------
* 只用标准库，云端函数 / GitHub Actions / 本地计划任务都能直接跑。
* 幂等：先查今天签没签，签过就直接退出，绝不重复调用领取接口。
* 可核验：领取成功后再查一次状态，用服务端返回的 streak_days 做交叉验证。
* 凭证只从环境变量 / 配置文件读取，日志里永远脱敏。

常用命令
--------
    python wb_checkin.py                     # 查状态 + 领取（推荐日常用法）
    python wb_checkin.py --status            # 只查今天状态，不领取
    python wb_checkin.py --from-curl a.txt   # 从 DevTools 复制的 cURL 里提取凭证并写入 config.json
    python wb_checkin.py --dry-run           # 演练：只查状态，不真正领取
    python wb_checkin.py --notify-test       # 发一条测试通知

退出码
------
    0 成功 / 今天已经签过（都算正常）
    2 配置缺失或不合法
    3 登录态失效（token 过期 / 无权限）—— 需要重新抓一次 token
    4 网络或服务端异常（已重试仍失败）
    5 业务失败（接口返回了非 0 的业务码）
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

# Windows 控制台默认是 GBK，遇到生僻字符会抛 UnicodeEncodeError 直接把程序打挂；
# 这里把编码错误降级成替换字符，保证签到本身不受影响。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

# ---------------------------------------------------------------- 常量

VERSION = "1.0.0"

DEFAULT_API_BASE = "https://www.workbuddy.cn"
STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CLAIM_PATH = "/v2/billing/meter/daily-checkin"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) WorkBuddy/1.0 Chrome/124.0.0.0 Safari/537.36"
)

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_AUTH = 3
EXIT_NET = 4
EXIT_BIZ = 5

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

# 本地时间显示用东八区（服务端的"今天"按北京时间切）
TZ_CN = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 输出


def now_cn() -> datetime:
    return datetime.now(TZ_CN)


def log(level: str, message: str) -> None:
    """统一格式打印，方便在 GitHub Actions 日志里 grep。"""
    stamp = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] [{level}] {message}", flush=True)


def mask(secret: str, keep: int = 10) -> str:
    if not secret:
        return "(空)"
    if len(secret) <= keep + 6:
        return secret[:keep] + "…"
    return f"{secret[:keep]}…{secret[-4:]}(共{len(secret)}字符)"


# ---------------------------------------------------------------- 配置


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def load_config(path: str | None) -> dict:
    """读取 config.json；文件不存在就返回空配置。"""
    candidates = []
    if path:
        candidates.append(path)
    if _env("WB_CONFIG"):
        candidates.append(_env("WB_CONFIG"))
    candidates.append(DEFAULT_CONFIG_PATH)

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8-sig") as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    raise ValueError("配置文件根节点必须是对象")
                data["__path__"] = os.path.abspath(candidate)
                return data
            except Exception as exc:  # noqa: BLE001
                log("ERROR", f"读取配置文件失败 {candidate}: {exc}")
                raise SystemExit(EXIT_CONFIG)
    return {}


def resolve_auth(cfg: dict, args: argparse.Namespace) -> tuple[str, str]:
    """按 命令行 > 环境变量 > 配置文件 的优先级取凭证。"""
    auth = cfg.get("auth") or {}
    token = (
        (getattr(args, "token", None) or "").strip()
        or _env("WB_ACCESS_TOKEN")
        or str(auth.get("access_token") or "").strip()
    )
    user_id = (
        (getattr(args, "user_id", None) or "").strip()
        or _env("WB_USER_ID")
        or str(auth.get("user_id") or "").strip()
    )
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token, user_id


def api_base(cfg: dict) -> str:
    base = (getattr_strip(cfg, "api_base") or _env("WB_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    return base


def getattr_strip(cfg: dict, key: str) -> str:
    return str(cfg.get(key) or "").strip()


# ---------------------------------------------------------------- cURL 解析


def parse_curl(text: str) -> dict:
    """从「Copy as cURL」文本里抠出 access_token / user_id。

    同时兼容 bash 风格（-H 'A: b'）与 cmd 风格（-H "A: b" / ^"），
    因为浏览器给的两者格式差别很大，这里用正则兜底而不是严格解析。
    """
    if not text or not text.strip():
        raise ValueError("cURL 文本为空")

    # cmd 的转义符先去掉，避免影响后续正则
    cleaned = text.replace("^", "").replace("\\\r\n", " ").replace("\\\n", " ")

    token = ""
    m = re.search(r"[Bb]earer\s+([A-Za-z0-9._\-]{20,})", cleaned)
    if m:
        token = m.group(1)
    else:
        # 少数情况下只贴了头部的值，没有 Bearer 前缀
        m = re.search(r"authorization[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9._\-]{40,})", cleaned, re.I)
        if m:
            token = m.group(1)

    user_id = ""
    for pattern in (
        r"[Xx]-[Uu]ser-[Ii]d[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9._\-]+)",
        r"[Uu]ser-[Ii]d[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9._\-]+)",
    ):
        m = re.search(pattern, cleaned)
        if m:
            user_id = m.group(1).strip()
            break

    if not token:
        raise ValueError("没找到 Authorization: Bearer <token>，请确认复制的是签到请求的「Copy as cURL」")

    # 顺手把这个请求真正的域名记下来：万一官方换了 API 域名，脚本跟着走，不用改代码
    host_match = re.search(r"curl[\"']?\s+[\"']?(https?://[A-Za-z0-9.\-]+(?::\d+)?)", cleaned)
    if not host_match:
        host_match = re.search(r"--url\s+[\"']?(https?://[A-Za-z0-9.\-]+(?::\d+)?)", cleaned)
    base = host_match.group(1) if host_match else ""

    return {"access_token": token, "user_id": user_id, "api_base": base}


# ---------------------------------------------------------------- JWT


def jwt_payload(token: str) -> dict | None:
    """解析 JWT 的 payload（不校验签名，只用来读 exp / 昵称）。"""
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def token_expiry(token: str) -> datetime | None:
    data = jwt_payload(token)
    if not data:
        return None
    exp = data.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return datetime.fromtimestamp(int(exp), tz=TZ_CN)


# ---------------------------------------------------------------- HTTP


class Response:
    __slots__ = ("status", "body", "error")

    def __init__(self, status: int | None, body: bytes, error: str = ""):
        self.status = status
        self.body = body
        self.error = error

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        try:
            return json.loads(self.text)
        except Exception:  # noqa: BLE001
            return None


def http_request(
    url: str,
    headers: dict,
    payload: dict | None = None,
    timeout: float = 20.0,
    retries: int = 2,
    logger=log,
) -> Response:
    """带退避重试的 POST。401/403/4xx(非429) 不重试，直接返回给上层判断。"""
    body = json.dumps(payload if payload is not None else {}).encode("utf-8")
    attempts = retries + 1
    last_error = ""

    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return Response(resp.status, resp.read())
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read() or b""
            except Exception:  # noqa: BLE001
                pass
            if exc.code in (401, 403) or (400 <= exc.code < 500 and exc.code != 429):
                return Response(exc.code, raw, f"HTTP {exc.code}")
            last_error = f"HTTP {exc.code}"
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", None) or exc
            last_error = f"网络异常: {reason}"
        except Exception as exc:  # noqa: BLE001
            last_error = f"请求异常: {exc}"

        if attempt < attempts:
            # 指数退避 + 抖动，避免多账号/多任务同时打同一个接口
            delay = min(30.0, 2.0 * (2 ** (attempt - 1))) + random.uniform(0, 0.8)
            logger("WARN", f"第 {attempt}/{attempts} 次请求失败（{last_error}），{delay:.1f}s 后重试")
            time.sleep(delay)

    return Response(None, b"", last_error)


def build_headers(token: str, user_id: str, cfg: dict) -> dict:
    base = api_base(cfg)
    domain = str(cfg.get("api_domain_header") or urllib.parse.urlparse(base).netloc)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {token}",
        "X-Domain": domain,
        "X-Request-ID": uuid.uuid4().hex,
        "Origin": base,
        "Referer": base + "/",
        "User-Agent": str(cfg.get("user_agent") or DEFAULT_UA),
    }
    if user_id:
        headers["X-User-Id"] = user_id
    return headers


# ---------------------------------------------------------------- 业务


class Outcome:
    """一次签到的结果，方便上层决定退出码和通知文案。"""

    def __init__(self, kind: str, message: str = "", credit: int | None = None,
                 streak: int | None = None, code: int | str | None = None):
        self.kind = kind          # ok | already | auth | net | biz
        self.message = message
        self.credit = credit
        self.streak = streak
        self.code = code

    @property
    def exit_code(self) -> int:
        return {
            "ok": EXIT_OK,
            "already": EXIT_OK,
            "auth": EXIT_AUTH,
            "net": EXIT_NET,
            "biz": EXIT_BIZ,
        }[self.kind]


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_status(cfg: dict, token: str, user_id: str, timeout: float, retries: int) -> tuple[dict | None, Outcome | None]:
    url = api_base(cfg) + STATUS_PATH
    resp = http_request(url, build_headers(token, user_id, cfg), {}, timeout, retries)

    if resp.status is None:
        return None, Outcome("net", f"查询签到状态失败：{resp.error}")
    if resp.status in (401, 403):
        return None, Outcome("auth", f"查询签到状态被拒绝（HTTP {resp.status}），登录态可能已失效", code=resp.status)

    data = resp.json()
    if resp.status != 200 or not isinstance(data, dict):
        return None, Outcome("net", f"查询签到状态异常：HTTP {resp.status} {resp.text[:200]}", code=resp.status)
    if data.get("code") not in (0, None):
        return None, Outcome("biz", f"查询签到状态业务失败：code={data.get('code')} msg={data.get('msg') or data.get('message')}", code=data.get("code"))

    payload = data.get("data")
    if not isinstance(payload, dict):
        return None, Outcome("net", f"查询签到状态返回结构异常：{resp.text[:200]}")
    return payload, None


def do_claim(cfg: dict, token: str, user_id: str, timeout: float, retries: int) -> Outcome:
    url = api_base(cfg) + CLAIM_PATH
    resp = http_request(url, build_headers(token, user_id, cfg), {}, timeout, retries)

    if resp.status is None:
        return Outcome("net", f"领取请求失败：{resp.error}")
    if resp.status in (401, 403):
        return Outcome("auth", f"领取被拒绝（HTTP {resp.status}），登录态可能已失效", code=resp.status)

    data = resp.json()
    if not isinstance(data, dict):
        return Outcome("net", f"领取返回异常：HTTP {resp.status} {resp.text[:200]}", code=resp.status)

    code = data.get("code")
    msg = str(data.get("msg") or data.get("message") or "")

    if code == 0:
        payload = data.get("data")
        if not isinstance(payload, dict):
            payload = {}
        credit = _as_int(payload.get("credit") or payload.get("today_credit") or payload.get("daily_credit"))
        return Outcome(
            "ok",
            msg or "领取成功",
            credit=credit,
            streak=_as_int(payload.get("streak_days")),
            code=code,
        )

    # 官方在「今天已经领过」时返回 HTTP 400 + code 10001 + "今天已签到，请明天再来"
    lowered = msg.lower()
    if code == 10001 or any(w in msg for w in ("已签到", "已领取", "请明天再来", "重复")) or "already" in lowered:
        return Outcome("already", msg or "今日已领取", code=code)

    if resp.status >= 500:
        return Outcome("net", f"服务端异常：HTTP {resp.status} {msg}", code=code)

    return Outcome("biz", f"业务失败：code={code} msg={msg or '(无)'}", code=code)


def run(args: argparse.Namespace) -> tuple[Outcome, dict]:
    """主流程，返回 (结果, 上下文信息)。"""
    cfg = load_config(args.config)
    token, user_id = resolve_auth(cfg, args)

    if not token:
        log("ERROR", "没有找到 access_token。")
        log("INFO", "三种给法任选：① python wb_checkin.py --from-curl curl.txt ② 设置环境变量 WB_ACCESS_TOKEN ③ 写进 config.json 的 auth.access_token")
        raise SystemExit(EXIT_CONFIG)
    if not user_id:
        log("WARN", "没有 X-User-Id，部分账号会因此报 401；若失败请重新抓一次完整请求头。")

    expiry = token_expiry(token)
    days_left = None
    if expiry:
        days_left = (expiry - now_cn()).total_seconds() / 86400
        log("INFO", f"登录态有效期至 {expiry.strftime('%Y-%m-%d %H:%M')}（还剩 {days_left:.1f} 天）")
        if days_left <= 0:
            log("ERROR", "access_token 已过期，需要重新抓一次。")
            raise SystemExit(EXIT_AUTH)
        if days_left <= 7:
            log("WARN", f"登录态只剩 {days_left:.1f} 天，请尽快重新抓一次 token，否则自动签到会停。")
    else:
        log("INFO", "token 不是 JWT 或读不到 exp，跳过有效期检查。")

    timeout = float(args.timeout or cfg.get("timeout") or 20)
    retries = int(args.retries if args.retries is not None else cfg.get("retries", 2))

    status, err = fetch_status(cfg, token, user_id, timeout, retries)
    if err:
        return err, {"days_left": days_left, "expiry": expiry}

    checked = bool(status.get("today_checked_in"))
    streak = _as_int(status.get("streak_days"))
    today_credit = _as_int(status.get("today_credit") or status.get("daily_credit"))
    total_credits = _as_int(status.get("total_credits"))
    log("INFO", f"服务端状态：today_checked_in={checked} streak_days={streak} "
                f"today_credit={today_credit} total_credits={total_credits}")

    if status.get("active") is False:
        log("WARN", "服务端标记该账号 active=false，可能没有签到资格或被风控，请先在客户端确认。")

    if checked:
        log("OK", f"今天已经签过了（连签 {streak} 天），不重复领取。")
        return Outcome("already", "今天已经签过", credit=today_credit, streak=streak), {"days_left": days_left, "expiry": expiry}

    if args.dry_run:
        log("OK", "演练模式：确认今天还没签，但按 --dry-run 要求不真正领取。")
        return Outcome("ok", "演练：今天尚未签到，未实际领取", credit=today_credit, streak=streak), {"days_left": days_left, "expiry": expiry}

    outcome = do_claim(cfg, token, user_id, timeout, retries)
    info = {"days_left": days_left, "expiry": expiry}

    if outcome.kind == "auth":
        log("ERROR", outcome.message)
        return outcome, info

    if outcome.kind == "already":
        log("OK", f"接口说今天已经领过了：{outcome.message}")
        return outcome, info

    if outcome.kind != "ok":
        log("ERROR", outcome.message)
        return outcome, info

    log("OK", f"领取成功：+{outcome.credit if outcome.credit is not None else '?'} 积分，连签 {outcome.streak} 天")

    # 二次核验：用状态接口确认真的到账，避免只信一条成功响应
    verify, verr = fetch_status(cfg, token, user_id, timeout, min(retries, 1))
    if verify and verify.get("today_checked_in"):
        v_streak = _as_int(verify.get("streak_days"))
        v_credit = _as_int(verify.get("today_credit"))
        log("OK", f"复核通过：today_checked_in=true streak_days={v_streak} today_credit={v_credit}")
        if outcome.streak is None:
            outcome.streak = v_streak
        if outcome.credit is None:
            outcome.credit = v_credit
    elif verr:
        log("WARN", f"复核查询失败（不影响已领取的结果）：{verr.message}")
    else:
        log("WARN", "复核时服务端仍显示今日未签，可能状态同步有延迟；以领取响应为准。")

    return outcome, info


# ---------------------------------------------------------------- 通知


def build_notify_text(outcome: Outcome, info: dict) -> tuple[str, str]:
    today = now_cn().strftime("%Y-%m-%d")
    if outcome.kind == "ok":
        title = "WorkBuddy 签到成功"
        body = f"{today} 领取成功"
        if outcome.credit is not None:
            body += f"，+{outcome.credit} 积分"
        if outcome.streak is not None:
            body += f"，连签 {outcome.streak} 天"
    elif outcome.kind == "already":
        title = "WorkBuddy 今日已签到"
        body = f"{today} 无需重复领取"
        if outcome.streak is not None:
            body += f"，连签 {outcome.streak} 天"
    else:
        title = "WorkBuddy 签到失败"
        body = f"{today} {outcome.message}"
        if outcome.kind == "auth":
            body += "\n需要重新抓一次 token 并更新到 GitHub Secrets。"

    expiry = info.get("expiry")
    if expiry:
        body += f"\n登录态到期：{expiry.strftime('%Y-%m-%d')}"
    return title, body


def notify_config(cfg: dict) -> dict:
    """通知配置：优先 config.json，其次环境变量（GitHub Secrets 里更方便）。"""
    conf = cfg.get("notify") or {}
    if isinstance(conf, dict) and conf.get("type") and (conf.get("url") or conf.get("webhook")):
        return conf
    kind = _env("WB_NOTIFY_TYPE")
    url = _env("WB_NOTIFY_URL")
    if kind and url:
        return {"type": kind, "url": url, "chat_id": _env("WB_NOTIFY_CHAT_ID")}
    return conf if isinstance(conf, dict) else {}


def send_notify(cfg: dict, title: str, body: str, logger=log) -> bool:
    conf = notify_config(cfg)
    if not isinstance(conf, dict) or not conf.get("type"):
        return False
    kind = str(conf.get("type")).lower()
    url = str(conf.get("url") or conf.get("webhook") or "")
    if not url:
        logger("WARN", "通知配置里没有 url，跳过通知")
        return False

    text = f"{title}\n{body}"
    try:
        if kind in ("wecom", "wechat", "qyweixin", "dingtalk", "custom"):
            payload = {"msgtype": "text", "text": {"content": text}}
            if kind == "dingtalk":
                payload = {"msgtype": "text", "text": {"content": text}}
            if kind == "custom":
                payload = {"title": title, "text": body}
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        elif kind == "serverchan":
            data = urllib.parse.urlencode({"title": title, "desp": body}).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST")
        elif kind == "bark":
            target = url.rstrip("/") + "/" + urllib.parse.quote(title) + "/" + urllib.parse.quote(body)
            req = urllib.request.Request(target, method="GET")
        elif kind == "telegram":
            chat_id = str(conf.get("chat_id") or "")
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
            req = urllib.request.Request(url, data=data, method="POST")
        else:
            logger("WARN", f"未知的通知类型：{kind}")
            return False

        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        logger("OK", f"通知已发送（{kind}）")
        return True
    except Exception as exc:  # noqa: BLE001
        logger("WARN", f"通知发送失败（不影响签到结果）：{exc}")
        return False


# ---------------------------------------------------------------- 写回配置


def save_credentials(path: str, creds: dict) -> str:
    cfg = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                cfg = json.load(fh) or {}
        except Exception:  # noqa: BLE001
            cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    auth = cfg.get("auth") if isinstance(cfg.get("auth"), dict) else {}
    auth["access_token"] = creds["access_token"]
    if creds.get("user_id"):
        auth["user_id"] = creds["user_id"]
    cfg["auth"] = auth
    if creds.get("api_base"):
        cfg["api_base"] = creds["api_base"]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wb_checkin.py",
        description="WorkBuddy 每日自动签到（查状态 → 领取 → 复核）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="配置文件路径，默认脚本同目录 config.json")
    parser.add_argument("--token", help="access_token（一般不推荐命令行传，会留在历史里）")
    parser.add_argument("--user-id", dest="user_id", help="X-User-Id / accountUid")
    parser.add_argument("--status", action="store_true", help="只查今天签没签，不领取")
    parser.add_argument("--dry-run", action="store_true", help="演练：确认未签到后立即退出，不领取")
    parser.add_argument("--from-curl", dest="from_curl", metavar="FILE", help="从 cURL 文本文件提取凭证并写入 config.json")
    parser.add_argument("--notify-test", action="store_true", help="发一条测试通知")
    parser.add_argument("--timeout", type=float, help="单次请求超时秒数，默认 20")
    parser.add_argument("--retries", type=int, help="失败重试次数，默认 2")
    parser.add_argument("--version", action="version", version=f"wb_checkin {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # 1) 从 cURL 提取凭证
    if args.from_curl:
        path = args.from_curl
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            log("ERROR", f"读不到文件 {path}: {exc}")
            return EXIT_CONFIG
        try:
            creds = parse_curl(text)
        except ValueError as exc:
            log("ERROR", str(exc))
            return EXIT_CONFIG

        target = args.config or DEFAULT_CONFIG_PATH
        save_credentials(target, creds)
        log("OK", f"已写入 {target}")
        log("INFO", f"access_token = {mask(creds['access_token'])}")
        log("INFO", f"user_id      = {creds.get('user_id') or '(没抓到，稍后可能报 401)'}")
        if creds.get("api_base"):
            log("INFO", f"接口域名     = {creds['api_base']}")
        expiry = token_expiry(creds["access_token"])
        if expiry:
            log("INFO", f"有效期至 {expiry.strftime('%Y-%m-%d %H:%M')}")
        log("INFO", "下一步：python wb_checkin.py --status 先验证，再 python wb_checkin.py 正式领取")
        return EXIT_OK

    cfg = load_config(args.config)

    # 2) 测试通知
    if args.notify_test:
        ok = send_notify(cfg, "WorkBuddy 通知测试", "收到这条说明通知渠道配置正确。")
        return EXIT_OK if ok else EXIT_CONFIG

    # 3) 只查状态
    if args.status:
        token, user_id = resolve_auth(cfg, args)
        if not token:
            log("ERROR", "没有 access_token，先跑 --from-curl")
            return EXIT_CONFIG
        timeout = float(args.timeout or cfg.get("timeout") or 20)
        retries = int(args.retries if args.retries is not None else cfg.get("retries", 2))
        status, err = fetch_status(cfg, token, user_id, timeout, retries)
        if err:
            log("ERROR", err.message)
            return err.exit_code
        log("OK", "状态查询成功：")
        for key in (
            "active",
            "today_checked_in",
            "streak_days",
            "is_streak_day",
            "next_streak_day",
            "today_credit",
            "daily_credit",
            "total_credits",
            "streak_bonus_days",
            "streak_bonus_credit",
            "week_checkin_days",
            "checkin_dates",
            "end_time",
        ):
            if key in status:
                log("INFO", f"  {key} = {status[key]}")
        return EXIT_OK

    # 4) 正式流程
    outcome, info = run(args)
    if outcome.kind in ("ok", "already") and not outcome.message.startswith("演练"):
        send_notify(cfg, *build_notify_text(outcome, info))
    elif outcome.kind not in ("ok", "already"):
        send_notify(cfg, *build_notify_text(outcome, info))
    return outcome.exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        log("ERROR", "被手动中断")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        log("ERROR", f"未预期的异常：{type(exc).__name__}: {exc}")
        sys.exit(EXIT_NET)
