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


def decide_signal(r_sh, r_cy, r_nd):
    """统一的信号判定（实时与回测共用）。返回 BUY_CY / BUY_ND / EMPTY。"""
    if (r_cy >= r_sh and r_cy >= r_nd
            and (r_cy - r_nd) >= THRESHOLD and r_cy > 0):
        return "BUY_CY"
    if (r_nd >= r_sh and r_nd >= r_cy
            and (r_nd - r_cy) >= THRESHOLD and r_nd > 0):
        return "BUY_ND"
    return "EMPTY"


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
    signal = decide_signal(r_sh, r_cy, r_nd)
    if signal == "BUY_CY":
        action = "买入创业板50"
        reason = (f"创业板50涨幅最大且为正（{r_cy:+.2f}%），领先纳指ETF "
                  f"{r_cy - r_nd:.2f}% ≥ {THRESHOLD}%，买入创业板50。")
    elif signal == "BUY_ND":
        action = "买入纳指国泰ETF"
        reason = (f"纳指ETF涨幅最大且为正（{r_nd:+.2f}%），领先创业板50 "
                  f"{r_nd - r_cy:.2f}% ≥ {THRESHOLD}%，买入纳指国泰ETF。")
    else:
        action = "空仓"
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


# ---------------------------------------------------------------------------
# 回测：2014 -> 最新交易日，按相同规则每日调仓
# ---------------------------------------------------------------------------

def _pct(x):
    return round(x * 100, 2) if x is not None else None


def _fetch_tencent_full(qt, end, count=1000):
    """腾讯行情全量日K。优先 qfq；若 qfq 数据被截断/缺失，自动补取 raw day 以拿到完整历史。"""
    def _one(qfq):
        p = f"{qt},day,,{end},{count},{qfq}"
        u = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=" + p
        r = requests.get(u, headers=HEADERS, timeout=20)
        r.raise_for_status()
        node = r.json()["data"][qt]
        return node.get("qfqday") or node.get("day")
    last_err = None
    for attempt in range(3):
        try:
            days_q = _one("qfq")
            days = days_q
            # 513100 等 qfq 可能被截断，补取 raw day 取更长序列
            if not days_q or len(days_q) < 900:
                days_r = _one("")
                if days_r and len(days_r) > (len(days_q) or 0):
                    days = days_r
            if not days:
                last_err = "空数据"; time.sleep(2 + attempt * 2); continue
            return [(row[0], float(row[2])) for row in days]
        except Exception as e:
            last_err = e
            print(f"[tencent-full {qt}] 第{attempt + 1}次失败: {e}")
            time.sleep(2 + attempt * 2)
    print(f"tencent-full {qt} 最终失败: {last_err}")
    return None


def _fetch_eastmoney_full(secid, end, count=1000):
    """东方财富全量日K（qfq），历史最完整，作为回测首选数据源。"""
    params = {"secid": secid, "klt": "101", "fqt": "1",
              "fields1": "f1,f2,f3", "fields2": "f51,f53",
              "end": (end or "20500101"), "lmt": str(count)}
    for attempt in range(2):
        for url in KLINE_URLS:
            try:
                r = requests.get(url, params=params, headers=HEADERS, timeout=20)
                r.raise_for_status()
                kl = r.json()["data"]["klines"]
                return [(ln.split(",")[0], float(ln.split(",")[1])) for ln in kl]
            except Exception as e:
                print(f"[eastmoney-full {secid}] {url.split('/')[2]} 失败: {e}")
        time.sleep(2 + attempt * 2)
    return None


def fetch_full_kline(sec, start="2014-01-01", max_pages=30):
    """抓取从 start 到今天的全量日K收盘价（升序、去重）。

    优先东方财富一次性拉全量（lmt 足够大时一次到位，最省请求）；
    若被截断或失败，则分页（东财分页 / 腾讯回退）。
    """
    # 1) 东方财富一次性全量
    one = _fetch_eastmoney_full(sec["secid"], "", count=6000)
    if one and len(one) > 100 and one[0][0] <= start:
        return [(d, c) for d, c in one if d >= start]
    # 2) 分页回退
    collected = []
    end = ""
    for _ in range(max_pages):
        chunk = _fetch_eastmoney_full(sec["secid"], end) or _fetch_tencent_full(sec["qt"], end)
        if not chunk:
            break
        chunk = [c for c in chunk if c[0] >= start]
        if not chunk:
            break
        collected = chunk + collected          # 更早的页放前面，保持升序
        earliest = chunk[0][0]
        if earliest <= start:
            break
        ed = datetime.strptime(earliest, "%Y-%m-%d") - timedelta(days=3)
        end = ed.strftime("%Y-%m-%d")
        time.sleep(1.5)                        # 避免频繁请求被限流
    out = {}
    for d, c in collected:
        out[d] = c
    return sorted(out.items())


def run_backtest(start="2014-01-01"):
    """回测策略：每个交易日依据近20日涨跌幅调仓（买创50/纳指ETF/空仓）。

    说明：
      - “买入创业板50” 以创业板50指数(399673)日收益作为持仓收益（ETF 159949
        2016 年才上市，用指数作为全程一致的代理）。
      - “买入纳指ETF” 以 513100 日收益作为持仓收益。
      - 空仓收益 = 0。
      - 以收盘价每日再平衡（当日 14:50 决策，持有至次日收盘）。
    """
    series = {sec["key"]: fetch_full_kline(sec, start) for sec in SECURITIES}
    for sec in SECURITIES:
        if len(series[sec["key"]]) < LOOKBACK + 2:
            raise RuntimeError(f"{sec['name']} 历史K线不足，回测无法进行")
    sh = series["sh"]
    dates = [d for d, _ in sh]
    raw = {k: dict(v) for k, v in series.items()}

    # 以 上证 交易日历为基准，缺失日向前填充
    def align(full):
        out, last = {}, None
        for d in dates:
            if d in full:
                last = full[d]
            out[d] = last
        return out
    c_sh, c_cy, c_nd = align(raw["sh"]), align(raw["cy"]), align(raw["nd"])

    N = len(dates)
    L = LOOKBACK
    r_cy = [0.0] * N
    r_nd = [0.0] * N
    for i in range(1, N):
        r_cy[i] = c_cy[dates[i]] / c_cy[dates[i - 1]] - 1
        r_nd[i] = c_nd[dates[i]] / c_nd[dates[i - 1]] - 1

    nav = [None] * N
    nav[L] = 1.0
    positions = [None] * N
    for i in range(L + 1, N):
        j = i - 1
        # 与实时信号一致：近20日累计涨跌幅以“百分数”参与判定
        cum_sh = (c_sh[dates[j]] / c_sh[dates[j - L]] - 1) * 100
        cum_cy = (c_cy[dates[j]] / c_cy[dates[j - L]] - 1) * 100
        cum_nd = (c_nd[dates[j]] / c_nd[dates[j - L]] - 1) * 100
        sig = decide_signal(cum_sh, cum_cy, cum_nd)
        positions[i] = sig
        if sig == "BUY_CY":
            r = r_cy[i]
        elif sig == "BUY_ND":
            r = r_nd[i]
        else:
            r = 0.0
        nav[i] = nav[i - 1] * (1 + r)

    s_i, e_i = L, N - 1

    def ann(v0, v1, d0, d1):
        yrs = (datetime.strptime(d1, "%Y-%m-%d") - datetime.strptime(d0, "%Y-%m-%d")).days / 365.25
        if yrs <= 0:
            return None
        return (v1 / v0) ** (1 / yrs) - 1

    def _window_stats(a, b):
        """区间 [a, b] 内策略净值：返回 (最大涨幅%, 最大回撤%)。
        最大涨幅 = 相对区间起点的最大累计涨幅（运行中最高累计收益）。
        最大回撤 = 区间内最大峰谷回撤（正数表示跌幅幅度，返回值取负）。"""
        vals = [nav[i] for i in range(a, b + 1) if nav[i] is not None]
        if len(vals) < 2:
            return None, None
        start = vals[0]
        peak = vals[0]
        max_gain = 0.0
        max_dd = 0.0
        for v in vals:
            if v > peak:
                peak = v
            g = v / start - 1
            if g > max_gain:
                max_gain = g
            dd = v / peak - 1
            if dd < max_dd:
                max_dd = dd
        return round(max_gain * 100, 2), round(max_dd * 100, 2)

    def _sub_start(years):
        target = datetime.strptime(dates[e_i], "%Y-%m-%d") - timedelta(days=years * 365.25)
        ts = target.strftime("%Y-%m-%d")
        return next((k for k in range(s_i, N) if dates[k] >= ts), None)

    def bh_vals(c):
        s = c[dates[s_i]]; e = c[dates[e_i]]
        return e / s - 1, ann(s, e, dates[s_i], dates[e_i])

    bh_cy, bh_nd, bh_sh = bh_vals(c_cy), bh_vals(c_nd), bh_vals(c_sh)

    # 各区间：年化 / 累计 / 最大涨幅 / 最大回撤
    metrics = {}
    for key, label, years in (
        ("full", "2014→今", 0),
        ("y3", "近3年", 3),
        ("y5", "近5年", 5),
        ("y10", "近10年", 10),
    ):
        ki = s_i if years == 0 else _sub_start(years)
        if ki is None:
            metrics[key] = {"label": label, "annualized": None,
                            "total": None, "max_gain": None, "max_dd": None}
            continue
        ann_v = ann(nav[ki], nav[e_i], dates[ki], dates[e_i])
        total_v = nav[e_i] / nav[ki] - 1
        mg, mdd = _window_stats(ki, e_i)
        metrics[key] = {
            "label": label,
            "annualized": _pct(ann_v),
            "total": round(total_v * 100, 2),
            "max_gain": mg,
            "max_dd": mdd,
            "start": dates[ki], "end": dates[e_i],
        }

    step = max(1, (e_i - s_i) // 160)
    curve_dates = sorted(set([dates[i] for i in range(s_i, e_i + 1, step)] + [dates[e_i]]))
    idx = {d: k for k, d in enumerate(dates)}
    strat = [round(nav[idx[d]] * 100, 2) for d in curve_dates]
    bcy = [round(c_cy[d] / c_cy[dates[s_i]] * 100, 2) for d in curve_dates]
    bnd = [round(c_nd[d] / c_nd[dates[s_i]] * 100, 2) for d in curve_dates]
    bsh = [round(c_sh[d] / c_sh[dates[s_i]] * 100, 2) for d in curve_dates]

    from collections import Counter
    cnt = Counter(p for p in positions[s_i + 1:] if p)

    return {
        "generated_at": now_bjt().strftime("%Y-%m-%d %H:%M:%S"),
        "start_date": dates[s_i], "end_date": dates[e_i],
        "lookback": LOOKBACK, "threshold_pct": THRESHOLD,
        "metrics": metrics,
        "benchmarks": {
            "cy": {"total": round(bh_cy[0] * 100, 2), "annualized": _pct(bh_cy[1])},
            "nd": {"total": round(bh_nd[0] * 100, 2), "annualized": _pct(bh_nd[1])},
            "sh": {"total": round(bh_sh[0] * 100, 2), "annualized": _pct(bh_sh[1])},
        },
        "positions": {
            "BUY_CY": cnt.get("BUY_CY", 0), "BUY_ND": cnt.get("BUY_ND", 0),
            "EMPTY": cnt.get("EMPTY", 0), "total_days": sum(cnt.values()),
        },
        "curve": {
            "dates": [d[5:] for d in curve_dates],
            "strategy": strat, "cy": bcy, "nd": bnd, "sh": bsh,
        },
    }


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

    # 回测（2014 -> 今天），写入 docs/backtest.json
    try:
        bt = run_backtest()
        bt_out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "backtest.json")
        with open(bt_out, "w", encoding="utf-8") as f:
            json.dump(bt, f, ensure_ascii=False, indent=2)
        print(f"回测已写入 {bt_out}（{bt['start_date']} ~ {bt['end_date']}，"
              f"年化 {bt['metrics']['full']['annualized']}%，"
              f"最大回撤 {bt['max_drawdown']}%）")
    except Exception as e:
        print("回测生成失败（不影响当日信号）：", repr(e))

    if not no_mail:
        send_mail(report)


if __name__ == "__main__":
    main()
