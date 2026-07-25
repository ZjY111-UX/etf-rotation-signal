# -*- coding: utf-8 -*-
"""
ETF 动量轮动信号 —— 每交易日 14:50 运行
比较：上证指数(1.000001) / 创业板50指数(0.399673) / 国泰纳指ETF 513100(1.513100)
近 20 个交易日涨跌幅，生成买入/空仓信号，写入 docs/data.json 并发送邮件。

规则：
  1) 创业板50最大 且 领先纳指ETF>=0.1% 且 涨幅>0 -> 买入创业板50
  2) 纳指ETF最大 且 领先创业板50>=0.1% 且 涨幅>0 -> 买入纳指国泰ETF
  3) 其他所有情况（上证最强/差距不足0.1%/涨幅为负） -> 空仓

环境变量（GitHub Secrets 注入）：
  MAIL_USER  发件 QQ 邮箱（如 12345@qq.com）
  MAIL_PASS  QQ 邮箱 SMTP 授权码
  MAIL_TO    收件邮箱（可与 MAIL_USER 相同）
命令行参数：
  --no-mail  只算不发邮件（本地调试用）
  --no-wait  不等待到 14:50，立即抓取
  --force    非交易日也强制统计（以最近一个交易日为准，用于手动测试）
"""

import json
import os
import sys
import time
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr

import requests

BJT = timezone(timedelta(hours=8))

SECURITIES = [
    {"key": "sh",  "secid": "1.000001", "qt": "sh000001", "name": "上证指数",      "code": "000001"},
    {"key": "cy",  "secid": "0.399673", "qt": "sz399673", "name": "创业板50指数",  "code": "399673"},
    {"key": "nd",  "secid": "1.513100", "qt": "sh513100", "name": "国泰纳指ETF",   "code": "513100"},
]

LOOKBACK = 20          # 近 20 个交易日
THRESHOLD = 0.1        # 领先阈值 0.1 个百分点
KLINE_URLS = [
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://push2delayhis.eastmoney.com/api/qt/stock/kline/get",
    "http://push2his.eastmoney.com/api/qt/stock/kline/get",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://quote.eastmoney.com/",
}


def now_bjt():
    return datetime.now(BJT)


def wait_until_1450():
    """GitHub Actions 定时可能提前触发，等到北京时间 14:50 再抓数据。"""
    target = now_bjt().replace(hour=14, minute=50, second=0, microsecond=0)
    remain = (target - now_bjt()).total_seconds()
    if 0 < remain <= 3600:
        print(f"等待 {int(remain)} 秒至北京时间 14:50 ...")
        time.sleep(remain)


def _fetch_eastmoney(secid, limit):
    params = {
        "secid": secid,
        "klt": "101",            # 日K
        "fqt": "1",              # 前复权（对指数无影响，对ETF消除分红缺口）
        "fields1": "f1,f2,f3",
        "fields2": "f51,f53",    # 日期, 收盘价
        "end": "20500101",
        "lmt": str(limit),
    }
    for url in KLINE_URLS:
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=15)
            r.raise_for_status()
            klines = r.json()["data"]["klines"]
            return [(ln.split(",")[0], float(ln.split(",")[1])) for ln in klines]
        except Exception as e:
            print(f"[eastmoney {secid}] {url.split('/')[2]} 失败: {e}")
    return None


def _fetch_tencent(qt_code, limit):
    """腾讯行情日K备用源。交易时段内包含当日实时K线。"""
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={qt_code},day,,,{limit},qfq")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        node = r.json()["data"][qt_code]
        days = node.get("qfqday") or node.get("day")
        return [(row[0], float(row[2])) for row in days]
    except Exception as e:
        print(f"[tencent {qt_code}] 失败: {e}")
        return None


def fetch_kline(sec, limit=LOOKBACK + 5):
    """抓取日K收盘价，多数据源互备。返回 [(date, close), ...] 升序。"""
    last_err = None
    for attempt in range(3):
        out = _fetch_eastmoney(sec["secid"], limit)
        if out is None:
            out = _fetch_tencent(sec["qt"], limit)
        if out and len(out) >= LOOKBACK + 1:
            return out
        last_err = f"数据不足或全部数据源失败 (attempt {attempt + 1})"
        time.sleep(3 + attempt * 3)
    raise RuntimeError(f"无法获取 {sec['name']} 的K线数据: {last_err}")


def is_trading_day(sh_kline):
    """当日 14:50 若为交易日，上证指数日K最后一根就是今天。"""
    today = now_bjt().strftime("%Y-%m-%d")
    return sh_kline[-1][0] == today


def build_report(force=False):
    data = {}
    for sec in SECURITIES:
        kline = fetch_kline(sec)
        if len(kline) < LOOKBACK + 1:
            raise RuntimeError(f"{sec['name']} K线不足 {LOOKBACK + 1} 根")
        data[sec["key"]] = kline

    sh_kline = data["sh"]
    trading = is_trading_day(sh_kline)

    report = {"generated_at": now_bjt().strftime("%Y-%m-%d %H:%M:%S"),
              "trading_day": trading, "lookback": LOOKBACK,
              "threshold_pct": THRESHOLD, "securities": [], "daily": []}
    if not trading and not force:
        return report
    report["trading_day"] = True  # force 模式下按最近交易日统计

    # 每个标的：取最近 LOOKBACK+1 根（首根为基准），算累计涨跌与每日涨跌
    perf = {}
    dates = [d for d, _ in data["sh"][-LOOKBACK:]]
    for sec in SECURITIES:
        kline = data[sec["key"]][-(LOOKBACK + 1):]
        base = kline[0][1]
        last = kline[-1][1]
        cum = (last / base - 1) * 100
        dailies = []
        for i in range(1, len(kline)):
            prev, cur = kline[i - 1][1], kline[i][1]
            dailies.append({"date": kline[i][0], "pct": round((cur / prev - 1) * 100, 2)})
        perf[sec["key"]] = cum
        report["securities"].append({
            "key": sec["key"], "name": sec["name"], "code": sec["code"],
            "base_date": kline[0][0], "base_price": base,
            "last_price": last, "cum_pct": round(cum, 2),
        })

    # 合并每日明细（以上证的日期为准）
    per_daily = {}
    for sec in SECURITIES:
        kline = data[sec["key"]][-(LOOKBACK + 1):]
        m = {}
        for i in range(1, len(kline)):
            m[kline[i][0]] = round((kline[i][1] / kline[i - 1][1] - 1) * 100, 2)
        per_daily[sec["key"]] = m
    for d in dates:
        report["daily"].append({
            "date": d,
            "sh": per_daily["sh"].get(d),
            "cy": per_daily["cy"].get(d),
            "nd": per_daily["nd"].get(d),
        })

    # ---- 信号判定 ----
    r_sh, r_cy, r_nd = perf["sh"], perf["cy"], perf["nd"]
    if (r_cy >= r_sh and r_cy >= r_nd
            and (r_cy - r_nd) >= THRESHOLD and r_cy > 0):
        signal, action = "BUY_CY", "买入创业板50"
        reason = (f"创业板50涨幅最大且为正（{r_cy:+.2f}%），领先纳指ETF "
                  f"{r_cy - r_nd:.2f}% ≥ {THRESHOLD}%，买入创业板50。")
    elif (r_nd >= r_sh and r_nd >= r_cy
            and (r_nd - r_cy) >= THRESHOLD and r_nd > 0):
        signal, action = "BUY_ND", "买入纳指国泰ETF"
        reason = (f"纳指ETF涨幅最大且为正（{r_nd:+.2f}%），领先创业板50 "
                  f"{r_nd - r_cy:.2f}% ≥ {THRESHOLD}%，买入纳指国泰ETF。")
    else:
        signal, action = "EMPTY", "空仓"
        if r_sh >= r_cy and r_sh >= r_nd:
            why = f"上证指数涨幅最大（{r_sh:+.2f}%）"
        elif max(r_cy, r_nd) <= 0:
            why = f"创业板50（{r_cy:+.2f}%）与纳指ETF（{r_nd:+.2f}%）涨幅均未大于0"
        elif abs(r_cy - r_nd) < THRESHOLD:
            why = (f"创业板50（{r_cy:+.2f}%）与纳指ETF（{r_nd:+.2f}%）"
                   f"差距不足 {THRESHOLD}%")
        else:
            why = f"最强标的涨幅未大于0（创50 {r_cy:+.2f}% / 纳指 {r_nd:+.2f}%）"
        reason = f"{why}，不满足买入条件，按规则空仓。"

    report["signal"] = signal
    report["action"] = action
    report["reason"] = reason
    return report


def color(pct):
    """A股习惯：涨红跌绿"""
    if pct is None:
        return "#666"
    return "#d93025" if pct > 0 else ("#0f9d58" if pct < 0 else "#666")


def fmt(pct):
    return "--" if pct is None else f"{pct:+.2f}%"


def render_mail_html(report):
    rows = ""
    for d in report["daily"]:
        rows += (f"<tr><td>{d['date']}</td>"
                 f"<td style='color:{color(d['sh'])}'>{fmt(d['sh'])}</td>"
                 f"<td style='color:{color(d['cy'])}'>{fmt(d['cy'])}</td>"
                 f"<td style='color:{color(d['nd'])}'>{fmt(d['nd'])}</td></tr>")
    cums = {s["key"]: s for s in report["securities"]}
    cum_row = "".join(
        f"<td style='font-weight:bold;color:{color(cums[k]['cum_pct'])}'>{fmt(cums[k]['cum_pct'])}</td>"
        for k in ("sh", "cy", "nd"))
    html = f"""
<div style="font-family:'Microsoft YaHei',sans-serif;max-width:640px;margin:auto">
  <h2 style="margin-bottom:4px">ETF 动量轮动信号 · {report['generated_at'][:10]}</h2>
  <p style="color:#888;margin-top:0">统计时点：{report['generated_at']}（北京时间）</p>
  <div style="background:#f5f7fa;border-left:6px solid #2b6cb0;padding:14px 18px;margin:16px 0">
    <div style="font-size:22px;font-weight:bold">今日操作：{report['action']}</div>
    <div style="color:#555;margin-top:6px">{report['reason']}</div>
  </div>
  <table border="0" cellpadding="6" cellspacing="0"
         style="border-collapse:collapse;width:100%;font-size:13px;text-align:center">
    <tr style="background:#2b6cb0;color:#fff">
      <th>日期</th><th>上证指数</th><th>创业板50</th><th>纳指ETF(513100)</th></tr>
    {rows}
    <tr style="background:#fff3e0"><td><b>近{report['lookback']}日累计</b></td>{cum_row}</tr>
  </table>
  <p style="color:#aaa;font-size:12px;margin-top:16px">
    基准日：{report['securities'][0]['base_date']}；累计涨跌 = 14:50 现价 / 基准日收盘 - 1。
    数据来源：东方财富。本邮件由 GitHub Actions 自动发送，仅供参考，不构成投资建议。</p>
</div>"""
    return html


def send_mail(report):
    user = os.environ.get("MAIL_USER", "").strip()
    pwd = os.environ.get("MAIL_PASS", "").strip()
    to = os.environ.get("MAIL_TO", "").strip() or user
    if not user or not pwd:
        print("未配置 MAIL_USER / MAIL_PASS，跳过邮件发送。")
        return

    msg = MIMEText(render_mail_html(report), "html", "utf-8")
    subject = f"【{report['action']}】ETF轮动信号 {report['generated_at'][:10]}"
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("ETF轮动信号", user))
    msg["To"] = to

    host = os.environ.get("SMTP_HOST", "smtp.qq.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, timeout=30) as server:
        server.login(user, pwd)
        server.sendmail(user, to.split(","), msg.as_string())
    print(f"邮件已发送至 {to}")


def main():
    no_mail = "--no-mail" in sys.argv
    no_wait = "--no-wait" in sys.argv
    force = "--force" in sys.argv or os.environ.get("FORCE_RUN") == "1"

    if not no_wait and not force:
        wait_until_1450()

    report = build_report(force=force)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data.json")
    if not report["trading_day"]:
        print("今天不是交易日（上证指数无当日K线），跳过统计与邮件。")
        return

    # 保留历史信号记录
    history = []
    if os.path.exists(out):
        try:
            with open(out, "r", encoding="utf-8") as f:
                old = json.load(f)
            history = old.get("history", [])
        except Exception:
            history = []
    today = report["generated_at"][:10]
    history = [h for h in history if h["date"] != today]
    history.append({"date": today, "action": report["action"],
                    "signal": report["signal"],
                    "sh": next(s["cum_pct"] for s in report["securities"] if s["key"] == "sh"),
                    "cy": next(s["cum_pct"] for s in report["securities"] if s["key"] == "cy"),
                    "nd": next(s["cum_pct"] for s in report["securities"] if s["key"] == "nd")})
    report["history"] = history[-60:]

    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"数据已写入 {out}")
    print(f"信号：{report['action']} —— {report['reason']}")

    if not no_mail:
        send_mail(report)


if __name__ == "__main__":
    main()
