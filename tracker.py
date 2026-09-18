"""機票價格追蹤：抓 Travelpayouts 價格 → 存歷史 → 低於目標價寄 Email。

環境變數：
  TP_TOKEN            Travelpayouts API token（必填）
  GMAIL_USER          寄件 Gmail 帳號（選填，沒設就不寄信）
  GMAIL_APP_PASSWORD  Gmail 應用程式密碼
  NOTIFY_TO           收件人，預設同 GMAIL_USER
"""

import json
import os
import smtplib
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
HISTORY_FILE = DATA / "history.json"
LATEST_FILE = DATA / "latest.json"
NOTIFIED_FILE = DATA / "notified.json"

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
TW = timezone(timedelta(hours=8))
HISTORY_MAX_DAYS = 365
OFFERS_KEPT = 10


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, obj):
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def target_months(cfg):
    if cfg.get("months"):
        return cfg["months"]
    today = date.today()
    months = []
    y, m = today.year, today.month
    for _ in range(cfg.get("months_ahead", 3)):
        m += 1
        if m > 12:
            y, m = y + 1, 1
        months.append(f"{y}-{m:02d}")
    return months


def fetch_offers(cfg, dest, month, token):
    params = {
        "origin": cfg["origin"],
        "destination": dest,
        "departure_at": month,
        "one_way": str(cfg.get("one_way", False)).lower(),
        "direct": str(cfg.get("direct_only", False)).lower(),
        "currency": cfg.get("currency", "twd"),
        "sorting": "price",
        "unique": "false",
        "limit": 100,
        "token": token,
    }
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("success"):
        raise RuntimeError(f"API 回傳失敗：{body}")
    return body.get("data", [])


def trip_days(offer):
    if not offer.get("return_at"):
        return None
    dep = datetime.fromisoformat(offer["departure_at"]).date()
    ret = datetime.fromisoformat(offer["return_at"]).date()
    return (ret - dep).days


def keep_offer(cfg, offer):
    if cfg.get("one_way", False):
        return True
    days = trip_days(offer)
    if days is None:
        return False
    return cfg.get("trip_days_min", 0) <= days <= cfg.get("trip_days_max", 99)


def simplify(offer):
    return {
        "price": offer["price"],
        "airline": offer.get("airline"),
        "flight_number": offer.get("flight_number"),
        "destination_airport": offer.get("destination_airport"),
        "departure_at": offer.get("departure_at"),
        "return_at": offer.get("return_at"),
        "transfers": offer.get("transfers", 0),
        "return_transfers": offer.get("return_transfers", 0),
        "link": "https://www.aviasales.com" + offer["link"] if offer.get("link") else None,
    }


def send_email(subject, html):
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not password:
        print("未設定 GMAIL_USER / GMAIL_APP_PASSWORD，略過寄信")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = os.environ.get("NOTIFY_TO") or user
    msg.set_content("請用支援 HTML 的信箱檢視此通知。")
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    print(f"已寄出通知：{subject}")
    return True


def build_email(alerts, currency):
    rows = []
    for a in alerts:
        o = a["offer"]
        dep = o["departure_at"][:10]
        ret = o["return_at"][:10] if o.get("return_at") else "單程"
        stops = "直飛" if o["transfers"] == 0 else f"轉機 {o['transfers']} 次"
        link = f'<a href="{o["link"]}">查看</a>' if o.get("link") else ""
        rows.append(
            f"<tr><td>{a['name']}</td><td><b>{o['price']:,}</b></td>"
            f"<td>{a['target']:,}</td><td>{dep} → {ret}</td>"
            f"<td>{o.get('airline') or ''} {stops}</td><td>{link}</td></tr>"
        )
    return (
        f"<p>以下航線低於目標價（{currency.upper()}）：</p>"
        '<table border="1" cellpadding="6" style="border-collapse:collapse">'
        "<tr><th>目的地</th><th>價格</th><th>目標</th><th>日期</th><th>航空</th><th></th></tr>"
        + "".join(rows)
        + "</table><p>價格為快取資料，下訂前請再確認即時價格。</p>"
    )


def main():
    token = os.environ.get("TP_TOKEN")
    if not token:
        sys.exit("缺少 TP_TOKEN 環境變數")

    cfg = load_json(ROOT / "config.json", None)
    months = target_months(cfg)
    now = datetime.now(TW).isoformat(timespec="minutes")

    history = load_json(HISTORY_FILE, [])
    notified = load_json(NOTIFIED_FILE, {})
    latest = {"updated_at": now, "currency": cfg.get("currency", "twd"), "routes": []}
    alerts = []
    errors = 0

    for d in cfg["destinations"]:
        for month in months:
            key = f"{cfg['origin']}-{d['code']}-{month}"
            try:
                raw = fetch_offers(cfg, d["code"], month, token)
            except Exception as e:  # 單一航線失敗不影響其他航線
                print(f"[{key}] 抓取失敗：{e}")
                errors += 1
                continue

            offers = sorted(
                (simplify(o) for o in raw if keep_offer(cfg, o)), key=lambda o: o["price"]
            )[:OFFERS_KEPT]
            best = offers[0] if offers else None
            print(f"[{key}] {len(raw)} 筆，符合條件 {len(offers)} 筆，最低 {best['price'] if best else '-'}")

            latest["routes"].append({
                "key": key, "code": d["code"], "name": d["name"], "month": month,
                "target_price": d["target_price"], "offers": offers,
            })
            if best:
                history.append({"t": now, "key": key, "code": d["code"], "month": month, "price": best["price"]})

            # 低於目標價，且比上次通知的價格更低才寄信，避免每次重複通知
            if best and best["price"] <= d["target_price"]:
                last = notified.get(key)
                if last is None or best["price"] < last:
                    alerts.append({"key": key, "name": f"{d['name']} {month}", "target": d["target_price"], "offer": best})
            elif key in notified and (not best or best["price"] > d["target_price"]):
                del notified[key]  # 價格回升，下次再跌破目標時重新通知

    cutoff = (datetime.now(TW) - timedelta(days=HISTORY_MAX_DAYS)).isoformat()
    history = [h for h in history if h["t"] >= cutoff]

    # 信真的寄出才記錄已通知，未設定 Gmail 時下次仍會通知
    if alerts:
        names = "、".join(a["name"] for a in alerts)
        if send_email(f"✈️ 機票降價：{names}", build_email(alerts, latest["currency"])):
            for a in alerts:
                notified[a["key"]] = a["offer"]["price"]

    save_json(LATEST_FILE, latest)
    save_json(HISTORY_FILE, history)
    save_json(NOTIFIED_FILE, notified)

    if errors and not latest["routes"]:
        sys.exit("所有航線都抓取失敗")


if __name__ == "__main__":
    main()
