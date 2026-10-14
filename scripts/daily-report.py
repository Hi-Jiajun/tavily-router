#!/usr/bin/python3
"""tavily-router 每日分析：消耗量 / 并发形态 / 429 / 缓存，并判断号池与配置要不要调。

产出：
  1) reports/YYYY-MM-DD.md    当日报告（人读）
  2) reports/history.csv      逐日趋势（机读，含次日可算的增量）
  3) 触发"需要加号"等条件时发邮件（SMTP 或 Mail.app，见下方“邮件通道”）

退出码：0=正常（含"已发信"）；3=需要动作但邮件未配置（让上层告警可见）；1=采集失败。

发信触发（--email-on，默认 pool,429）：
  pool = 额度不足撑到重置（建议加号）；429 = 出现 429（要么加号、要么调限速）；
  还可选 cost / key / sync / all。其余动作只写报告不发信。

邮件通道（按序尝试，--no-email 可跳过）：
  ① SMTP：凭据放 Keychain，条目 service=smtp-notify，account=<发件地址>，值=应用专用密码；
     可选附加属性：security add-generic-password -a <发件地址> -s smtp-notify -w '<app-password>' \
       -l smtp-host=<host> -l smtp-port=<port> -A -U
  ② Mail.app：用 AppleScript 经指定账号发出（默认发件人 TVR_MAIL_SENDER，本机为 3138947285@qq.com；
     收件人默认同发件人，可用 TVR_NOTIFY_TO 覆盖）。2026-10-13 实测已成功发出。

用法（TVR_SSH 必填，其余按需）：
  TVR_SSH=user@host scripts/daily-report.py                        # 采集 → 写报告 → 需要时发信
  TVR_SSH=user@host scripts/daily-report.py --dry-run --no-email    # 只打印结论，不落盘不发信
  TVR_SSH=user@host scripts/daily-report.py --test-email            # 只测邮件通道

环境变量：TVR_SSH / TVR_KEYCHAIN_SERVICE / TVR_NOTIFY_TO / TVR_MAIL_SENDER / TVR_EMAIL_ON /
TVR_SMTP_SERVICE / TVR_SMTP_HOST / TVR_SMTP_PORT，以及四个阈值 TVR_RESERVE_RATIO /
TVR_PEAK_CONC_WARN / TVR_COST_PER_REQ_WARN / TVR_CACHE_IDLE_REQUESTS。
"""
import argparse
import csv
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPORTS = os.path.join(ROOT, "reports")
HISTORY = os.path.join(REPORTS, "history.csv")

VM = os.environ.get("TVR_SSH", "")
ENTRY = os.environ.get("TVR_ENTRY", "http://192.168.1.60:8080")
KEYCHAIN_SERVICE = os.environ.get("TVR_KEYCHAIN_SERVICE", "tavily-router-master-key")
NOTIFY_TO = os.environ.get("TVR_NOTIFY_TO", "")
SMTP_SERVICE = os.environ.get("TVR_SMTP_SERVICE", "smtp-notify")
MAIL_SENDER = os.environ.get("TVR_MAIL_SENDER", "3138947285@qq.com")

# 阈值（可在环境变量里覆盖）
TH = {
    "reserve_ratio": float(os.environ.get("TVR_RESERVE_RATIO", 0.20)),   # 重置前要留的额度余量
    "peak_conc_warn": int(os.environ.get("TVR_PEAK_CONC_WARN", 12)),     # 峰值并发告警线
    "cost_per_req_warn": float(os.environ.get("TVR_COST_PER_REQ_WARN", 1.5)),
    "cache_idle_requests": int(os.environ.get("TVR_CACHE_IDLE_REQUESTS", 50)),
}

# 在网关主机上执行的采集脚本（loopback 管理面；只回传聚合，不回传正文）
COLLECTOR = r'''
import json,urllib.request,datetime,collections,statistics,sys
MK=sys.argv[1]
def get(p,t=25):
    r=urllib.request.Request("http://127.0.0.1:8080"+p,headers={"Authorization":"Bearer "+MK})
    try:
        with urllib.request.urlopen(r,timeout=t) as x: return json.loads(x.read())
    except Exception as e: return {"_error":str(e)[:80]}
out={}
out["stats"]=get("/api/stats")
out["cache"]=get("/api/cache/stats")
out["sync"]=get("/api/settings/auto-sync")
keys=get("/api/keys"); out["keys"]=keys if isinstance(keys,list) else keys.get("items",[])
rows=[]; page=1
while page<=15:
    d=get("/api/logs?page=%d&page_size=200"%page)
    items=d.get("items") if isinstance(d,dict) else None
    if not items: break
    rows+=items
    if len(items)<200: break
    page+=1
def parse(r):
    try: return datetime.datetime.fromisoformat(str(r.get("created_at")).replace("Z","+00:00"))
    except Exception: return None
cut=datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(hours=24)
recent=sorted([(parse(r),r) for r in rows if parse(r) and parse(r)>=cut],key=lambda x:x[0])
out["window_hours"]=24
out["requests_24h"]=len(recent)
out["status_24h"]=dict(collections.Counter(int(r.get("status_code") or 0) for _,r in recent))
out["endpoints_24h"]=dict(collections.Counter(r.get("endpoint") for _,r in recent))
hourly=collections.Counter(t.strftime("%m-%d %H") for t,_ in recent)
out["hourly_24h"]=dict(sorted(hourly.items()))
if recent:
    ev=[]
    for t,r in recent:
        lat=(r.get("latency") or 0)/1000.0
        ev.append((t,1)); ev.append((t+datetime.timedelta(seconds=lat),-1))
    ev.sort(key=lambda x:(x[0],x[1]))
    cur=peak=0
    for _,d in ev:
        cur+=d; peak=max(peak,cur)
    out["peak_concurrency_24h"]=peak
    def maxwin(sec):
        best=0;j=0
        for i in range(len(recent)):
            while (recent[i][0]-recent[j][0]).total_seconds()>sec: j+=1
            best=max(best,i-j+1)
        return best
    out["max_rps_1s"]=maxwin(1); out["max_rps_10s"]=round(maxwin(10)/10.0,2)
    lats=[(r.get("latency") or 0) for _,r in recent]
    out["latency_ms"]={"p50":int(statistics.median(lats)),"p95":sorted(lats)[max(0,int(len(lats)*0.95)-1)],"max":max(lats)}
print(json.dumps(out))
'''


def master_key():
    p = subprocess.run(["/usr/bin/security", "find-generic-password", "-a", os.environ.get("USER", ""),
                        "-s", KEYCHAIN_SERVICE, "-w"], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def collect():
    if not VM:
        raise SystemExit("ERR: 需要 TVR_SSH（管理面所在主机，例如 user@192.0.2.10）")
    key = master_key()
    if not key:
        raise SystemExit("ERR: 取不到网关 master key（Keychain: %s）" % KEYCHAIN_SERVICE)
    cmd = "python3 - %s" % key
    p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", VM, cmd],
                       input=COLLECTOR, capture_output=True, text=True, timeout=120)
    if p.returncode != 0 or not p.stdout.strip():
        raise SystemExit("ERR: 采集失败: %s" % (p.stderr.strip()[:200] or "no output"))
    return json.loads(p.stdout.strip().splitlines()[-1])


def load_prev_total_used():
    if not os.path.exists(HISTORY):
        return None, None
    last = None
    with open(HISTORY, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            last = row
    if not last:
        return None, None
    try:
        return int(last["total_used"]), last["date"]
    except Exception:
        return None, None


def analyze(data, prev_used):
    now = datetime.datetime.now(datetime.timezone.utc)
    month_end = (now.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    days_left = max(1, (month_end - now).days)
    stats = data.get("stats") or {}
    total = stats.get("total_quota") or 0
    used = stats.get("total_used") or 0
    remaining = stats.get("total_remaining") or 0
    reqs = data.get("requests_24h") or 0
    status = data.get("status_24h") or {}
    cache = data.get("cache") or {}
    sync = data.get("sync") or {}
    keys = data.get("keys") or []

    delta = None
    if prev_used is not None and used >= prev_used:
        delta = used - prev_used
    burn = delta if delta is not None else None
    per_req = round(burn / reqs, 2) if (burn is not None and reqs) else None
    days_to_empty = round(remaining / burn, 1) if burn else None

    findings, actions = [], []

    def act(tag, text):
        actions.append({"tag": tag, "text": text})
    if burn is None:
        findings.append("首次运行：还没有上一日快照，今天算不出日消耗（明天起有）")
    if days_to_empty is not None:
        if days_to_empty < days_left * (1 + TH["reserve_ratio"]):
            act("pool", "额度紧张：按当前 %.0f credits/天，预计 %.1f 天后耗尽，而距重置还有 %d 天 → **建议加号**" % (burn, days_to_empty, days_left))
        else:
            findings.append("额度充裕：按当前 %s credits/天，可支撑 %.1f 天（重置还有 %d 天）" % (burn, days_to_empty, days_left))
    if status.get(429):
        act("429", "昨日出现 429 共 %d 次 → 突发超过上游容忍度：要么加号（更多 key 摊并发），要么调小限速/加大 burst" % status[429])
    peak = data.get("peak_concurrency_24h") or 0
    # 峰值并发只作记录：真实上限约 12 rps 稳态，但瞬时峰值高不等于有问题
    # （压测/合成流量会把它推高），真正可靠的信号是 429 计数。
    findings.append("24h 峰值并发 %d（≥%d 时留意；若当天跑过压测或批量脚本，请按时间段核对）"
                    % (peak, TH["peak_conc_warn"]))
    if peak > TH["peak_conc_warn"] and not status.get(429):
        findings.append("峰值并发偏高但未出现 429 → 入口队列吸收了突发，暂无需调整")
    if per_req is not None and per_req > TH["cost_per_req_warn"]:
        act("cost", "每次请求均耗 %.2f credits（>%.1f）→ 检查是否有人用了 search_depth=advanced / include_raw_content" % (per_req, TH["cost_per_req_warn"]))
    if reqs >= TH["cache_idle_requests"] and not cache.get("total_hits"):
        findings.append("缓存 24h 零命中（请求 %d 次）→ 查询基本不重复，TTL 可维持 5 分钟或直接关掉省内存" % reqs)
    bad = [k.get("alias") for k in keys if k.get("is_invalid") or not k.get("is_active")]
    if bad:
        act("key", "存在失效/停用 key：%s → 清理或替换" % ", ".join(str(b) for b in bad))
    if not sync.get("enabled"):
        act("sync", "额度自动同步被关闭 → 选路依据会变旧，建议重新开启")
    return {
        "days_left": days_left, "total": total, "used": used, "remaining": remaining,
        "burn": burn, "per_req": per_req, "days_to_empty": days_to_empty,
        "findings": findings, "actions": actions,
    }


def write_report(day, data, res, prev_date):
    os.makedirs(REPORTS, exist_ok=True)
    keys = data.get("keys") or []
    lines = []
    lines.append("# %s 号池日报" % day)
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    lines.append("| 池容量 / 已用 / 剩余 | %s / %s / **%s** |" % (res["total"], res["used"], res["remaining"]))
    lines.append("| 24h 请求数 | %s |" % (data.get("requests_24h")))
    lines.append("| 昨日消耗（对比 %s） | %s |" % (prev_date or "无快照", res["burn"] if res["burn"] is not None else "—"))
    lines.append("| 每次请求均耗 | %s |" % (res["per_req"] if res["per_req"] is not None else "—"))
    lines.append("| 距重置天数 / 预计耗尽天数 | %s / %s |" % (res["days_left"], res["days_to_empty"] if res["days_to_empty"] else "—"))
    lines.append("| 24h 峰值并发 | %s |" % data.get("peak_concurrency_24h"))
    lines.append("| 1s / 10s 最大速率 | %s / %s rps |" % (data.get("max_rps_1s"), data.get("max_rps_10s")))
    lines.append("| 状态分布 | %s |" % (data.get("status_24h")))
    lines.append("| 端点分布 | %s |" % (data.get("endpoints_24h")))
    lines.append("| 延迟 p50/p95/max (ms) | %s |" % ((data.get("latency_ms") or {})))
    lines.append("| 缓存 | 条目 %s，命中 %s |" % ((data.get("cache") or {}).get("entry_count"), (data.get("cache") or {}).get("total_hits")))
    lines.append("")
    lines.append("## 逐小时请求（近 24h）")
    lines.append("")
    for h, c in (data.get("hourly_24h") or {}).items():
        lines.append("- %s: %s" % (h, c))
    lines.append("")
    lines.append("## 各 key")
    lines.append("")
    lines.append("| key | 已用 | 上限 | 活跃 |")
    lines.append("| --- | --- | --- | --- |")
    for k in keys:
        lines.append("| %s | %s | %s | %s |" % (k.get("alias"), k.get("used_quota"), k.get("total_quota"), k.get("is_active")))
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    for f in res["findings"]:
        lines.append("- %s" % f)
    if res["actions"]:
        lines.append("")
        lines.append("**需要动作：**")
        for a in res["actions"]:
            lines.append("- `%s` %s" % (a["tag"], a["text"]))
    else:
        lines.append("- 无需调整")
    path = os.path.join(REPORTS, "%s.md" % day)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    # 同一天重复运行只保留一行（幂等覆盖），避免历史里出现重复日期
    header = ["date", "total_quota", "total_used", "requests_24h", "peak_concurrency", "status_429", "cache_hits"]
    old = []
    if os.path.exists(HISTORY):
        with open(HISTORY, newline="", encoding="utf-8") as fh:
            old = [r for r in csv.DictReader(fh) if r.get("date") and r.get("date") != day]
    row = {"date": day, "total_quota": res["total"], "total_used": res["used"],
           "requests_24h": data.get("requests_24h"),
           "peak_concurrency": data.get("peak_concurrency_24h"),
           "status_429": (data.get("status_24h") or {}).get(429, 0),
           "cache_hits": (data.get("cache") or {}).get("total_hits", 0)}
    with open(HISTORY, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=header)
        w.writeheader()
        for r in old:
            w.writerow({k: r.get(k, "") for k in header})
        w.writerow(row)
    return path


def smtp_send(subject, body):
    """SMTP 通道：凭据在 Keychain（service=smtp-notify）。返回 True 表示已发出。"""
    p = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", SMTP_SERVICE, "-w"],
                       capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout.strip():
        return False
    password = p.stdout.strip()
    meta = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", SMTP_SERVICE],
                          capture_output=True, text=True).stdout
    sender = ""
    for line in meta.splitlines():
        if '"acct"' in line:
            sender = line.split("=", 1)[1].strip().strip('"')
    host = os.environ.get("TVR_SMTP_HOST", "smtp.qq.com")
    port = int(os.environ.get("TVR_SMTP_PORT", 465))
    to = NOTIFY_TO or sender
    if not (sender and to):
        return False
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL(host, port, timeout=20) as s:
            s.login(sender, password)
            s.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        print("WARN: SMTP 发送失败: %s" % exc, file=sys.stderr)
        return False


def mailapp_send(subject, body, sender=None, to=None):
    """Mail.app 通道：指定账号发出（默认 TVR_MAIL_SENDER 自发自收）。

    正文经临时文件交给 AppleScript 读取，避免引号/换行/中文的转义问题。
    返回 True 表示 AppleScript 无错误（= 已交给 Mail.app 外发）。
    """
    probe = subprocess.run(["/usr/bin/osascript", "-e", 'tell application "Mail" to count accounts'],
                           capture_output=True, text=True, timeout=20)
    if probe.stdout.strip() in ("", "0"):
        return False
    sender = sender or MAIL_SENDER
    to = to or NOTIFY_TO or sender
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(body)
        body_path = fh.name
    os.chmod(body_path, 0o600)
    def esc(s):  # 只用于单行字段
        return s.replace("\\", "\\\\").replace('"', '\\"')
    script = '''
set msgBody to (read POSIX file "%s" as «class utf8»)
tell application "Mail"
    set msg to make new outgoing message with properties {subject:"%s", content:msgBody, visible:false}
    tell msg
        set sender to "%s"
        make new to recipient at end of to recipients with properties {address:"%s"}
    end tell
    send msg
end tell
''' % (body_path, esc(subject), esc(sender), esc(to))
    try:
        p = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=90)
        if p.returncode != 0:
            print("WARN: Mail.app 发送失败: %s" % ((p.stderr or p.stdout).strip()[:200]), file=sys.stderr)
            return False
        return True
    finally:
        try:
            os.unlink(body_path)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--dry-run", action="store_true", help="只打印结论，不落盘不发信")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--to", default="", help="覆盖收件地址（默认 TVR_NOTIFY_TO 或发件地址自身）")
    ap.add_argument("--test-email", action="store_true", help="只发一封通道自测邮件，不采集")
    ap.add_argument("--email-on", default=os.environ.get("TVR_EMAIL_ON", "pool,429"),
                    help="哪些动作类触发发信，逗号分隔：pool(建议加号) / 429 / cost / key / sync / all（默认 pool,429）")
    args = ap.parse_args()
    global NOTIFY_TO
    if args.to:
        NOTIFY_TO = args.to

    if args.test_email:
        subject = "[tavily-router] 邮件通道自测"
        body = ("这是一封自测邮件：由 ~/Documents/tavily-router/scripts/daily-report.py 发出。\n"
                "发件地址：%s\n收件地址：%s\n时间：%s\n" % (MAIL_SENDER, NOTIFY_TO or MAIL_SENDER,
                                                    datetime.datetime.now().isoformat(timespec="seconds")))
        if smtp_send(subject, body):
            print("邮件已发送（SMTP）")
        elif mailapp_send(subject, body):
            print("邮件已发送（Mail.app，发件 %s）" % MAIL_SENDER)
        else:
            print("EMAIL_NOT_CONFIGURED: SMTP 与 Mail.app 都不可用", file=sys.stderr)
            return 3
        return 0

    data = collect()
    # JSON 往返会把 int 键变成字符串（如 {"429": 160}），这里统一还原，否则阈值判断永远不触发
    if isinstance(data.get("status_24h"), dict):
        data["status_24h"] = {int(k): v for k, v in data["status_24h"].items()}
    prev_used, prev_date = load_prev_total_used()
    if prev_date == args.date:      # 同日重复运行不重复计增量
        prev_used = None
    res = analyze(data, prev_used)

    print("池 %s/%s 剩余；24h 请求 %s；峰值并发 %s；429 %s；日消耗 %s；预计 %s 天后耗尽（距重置 %s 天）" % (
        res["remaining"], res["total"], data.get("requests_24h"), data.get("peak_concurrency_24h"),
        (data.get("status_24h") or {}).get(429, 0), res["burn"], res["days_to_empty"], res["days_left"]))
    for a in res["actions"]:
        print("ACTION[%s]: %s" % (a["tag"], a["text"]))

    if args.dry_run:
        return 0

    path = write_report(args.date, data, res, prev_date)
    print("报告: %s" % path)

    triggers = {t.strip() for t in args.email_on.split(",") if t.strip()}
    due = [a for a in res["actions"] if "all" in triggers or a["tag"] in triggers]
    if not due or args.no_email:
        if res["actions"] and not due:
            print("提示：有动作项但按 --email-on=%s 不触发发信（报告已记录）" % args.email_on)
        return 0
    subject = "[tavily-router] %s %s" % (args.date, due[0]["text"][:50])
    body = "日期：%s\n\n池：%s/%s 剩余；24h 请求 %s；峰值并发 %s；429 %s；日消耗 %s；预计 %s 天后耗尽（距重置 %s 天）\n\n需要动作：\n%s\n\n报告：%s\n" % (
        args.date, res["remaining"], res["total"], data.get("requests_24h"),
        data.get("peak_concurrency_24h"), (data.get("status_24h") or {}).get(429, 0),
        res["burn"], res["days_to_empty"], res["days_left"],
        "\n".join("- [%s] %s" % (a["tag"], a["text"]) for a in res["actions"]), path)
    sent = smtp_send(subject, body)
    channel = "SMTP"
    if not sent:
        sent = mailapp_send(subject, body)
        channel = "Mail.app(%s)" % MAIL_SENDER
    if sent:
        print("邮件已发送（%s）" % channel)
        return 0
    print("EMAIL_NOT_CONFIGURED: 有动作项但邮件通道不可用（SMTP Keychain %s / Mail.app 账号均不可用）" % SMTP_SERVICE,
          file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
