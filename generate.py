#!/usr/bin/env python3
"""天気予報フィード生成スクリプト。

気象庁の予報 JSON（府県予報区ごと）を取得し、一次細分区域（142地点）ごとに
短期予報＋週間予報をマージした7日分を iCal / RSS / JSON で出力する。
あわせて一覧ページ（index.html）と OPML を生成する。

使い方: python3 generate.py --out _site
"""
import argparse
import json
import shutil
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
BASE = "https://nidoneru.github.io/jma-weather-feed"
AREA_JSON = "https://www.jma.go.jp/bosai/common/const/area.json"
FORECAST = "https://www.jma.go.jp/bosai/forecast/data/forecast/{}.json"
UA = {"User-Agent": "jma-weather-feed (https://github.com/nidoneru/jma-weather-feed)"}
WEEKDAYS = "月火水木金土日"
SOURCE_LINE = "出典：気象庁の天気予報データを加工して作成"

ROOT = Path(__file__).resolve().parent


def fetch_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def dstr(dt_iso):
    return dt_iso[:10]


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class Data:
    def __init__(self):
        self.codes = json.loads((ROOT / "data/weather_codes.json").read_text(encoding="utf-8"))
        self.points = json.loads((ROOT / "data/points.json").read_text(encoding="utf-8"))
        self.weekly_areas = json.loads((ROOT / "data/weekly_areas.json").read_text(encoding="utf-8"))
        self.area_points = {}
        for pc, p in self.points.items():
            self.area_points.setdefault(p["area"], []).append(pc)
        for a, pcs in self.area_points.items():
            pcs.sort(key=lambda pc: (not self.points[pc]["representative"], pc))
        self.area_weekly = {}
        for wcode, w in self.weekly_areas.items():
            for a in w["areas"]:
                self.area_weekly[a] = wcode
        self.unknown_codes = set()

    def weather_text(self, code):
        t = self.codes.get(code)
        if t is None:
            self.unknown_codes.add(code)
            return ""
        return t


def parse_forecasts(area):
    """全予報区 JSON を取得し、短期（区域ごと）と週間（週間区域ごと）に整理する。"""
    short = {}   # area_code -> {weather:{date:code}, wind:{date:str}, pops:{date:[(label,int)]}, temps:{point:{date:(min,max)}}}
    weekly = {}  # weekly_code -> {weather:{date:(code,pop)}}
    wtemps = {}  # point_code -> {date:(min,max)}（週間予報の気温。短期の3日目の補完にも使う）
    for oc in area["offices"]:
        try:
            fc = fetch_json(FORECAST.format(oc))
        except Exception:
            continue  # 014030, 460040 は単独ファイルがなく、親ファイル側に含まれる
        rep_s, rep_w = fc[0], fc[1]
        ts = rep_s["timeSeries"]
        dates0 = [dstr(d) for d in ts[0]["timeDefines"]]
        for a in ts[0]["areas"]:
            e = short.setdefault(a["area"]["code"], {"weather": {}, "wind": {}, "pops": {}, "temps": {}})
            for d, c in zip(dates0, a.get("weatherCodes", [])):
                e["weather"][d] = c
            for d, w in zip(dates0, a.get("winds", [])):
                e["wind"][d] = w.replace("　", " ").strip()
        for a in ts[1]["areas"]:
            e = short.setdefault(a["area"]["code"], {"weather": {}, "wind": {}, "pops": {}, "temps": {}})
            for td, v in zip(ts[1]["timeDefines"], a.get("pops", [])):
                iv = to_int(v)
                if iv is None:
                    continue
                h = int(td[11:13])
                e["pops"].setdefault(dstr(td), []).append((f"{h}-{h + 6}時", iv))
        for a in ts[2]["areas"]:
            pc = a["area"]["code"]
            # 気温は地点単位で別枠に集める（地点と区域の対応は data/points.json）
            for td, v in zip(ts[2]["timeDefines"], a.get("temps", [])):
                iv = to_int(v)
                if iv is None:
                    continue
                hour = int(td[11:13])
                slot = "min" if hour == 0 else "max" if hour == 9 else None
                if slot is None:
                    continue
                short.setdefault("__temps__", {}).setdefault(pc, {}).setdefault(dstr(td), {})[slot] = iv
        tw = rep_w["timeSeries"]
        wdates = [dstr(d) for d in tw[0]["timeDefines"]]
        for a in tw[0]["areas"]:
            e = weekly.setdefault(a["area"]["code"], {"weather": {}})
            for d, c, p in zip(wdates, a.get("weatherCodes", []), a.get("pops", [])):
                e["weather"][d] = (c, to_int(p))
        tdates = [dstr(d) for d in tw[1]["timeDefines"]]
        for a in tw[1]["areas"]:
            pc = a["area"]["code"]
            for d, mn, mx in zip(tdates, a.get("tempsMin", []), a.get("tempsMax", [])):
                wtemps.setdefault(pc, {})[d] = (to_int(mn), to_int(mx))
    temps_all = short.pop("__temps__", {})
    return short, weekly, temps_all, wtemps


def build_days(data: Data, area_code, short, weekly, temps_all, wtemps):
    """区域の7日分（短期優先マージ）を作る。"""
    s = short.get(area_code, {"weather": {}, "wind": {}, "pops": {}})
    wcode = data.area_weekly.get(area_code)
    w = weekly.get(wcode, {"weather": {}}) if wcode else {"weather": {}}

    def point_temp(pc, d):
        t = temps_all.get(pc, {}).get(d, {})
        if t:
            return (t.get("min"), t.get("max"))
        wt = wtemps.get(pc, {}).get(d)
        if wt and (wt[0] is not None or wt[1] is not None):
            return wt
        return None
    sdates = sorted(s["weather"])
    wdates = [d for d in sorted(w["weather"]) if d not in sdates]
    days = []
    for d in (sdates + wdates)[:7]:
        if d in sdates:
            code = s["weather"][d]
            slots = s["pops"].get(d, [])
            temps = []
            for pc in data.area_points.get(area_code, []):
                t = point_temp(pc, d)
                if t:
                    temps.append((data.points[pc]["name"], t[0], t[1]))
            rep_pc = next((pc for pc in data.area_points.get(area_code, []) if data.points[pc]["representative"]), None)
            tt = None
            if rep_pc:
                t = point_temp(rep_pc, d)
                if t:
                    tt = (data.points[rep_pc]["name"], t[0], t[1])
            if not temps and wcode:
                # 短期の気温は2日分しかない。3日目は週間予報の区域地点で補う（地点名つきで出す）
                for pc in data.weekly_areas[wcode]["points"]:
                    t = wtemps.get(pc, {}).get(d)
                    if t and (t[0] is not None or t[1] is not None):
                        temps.append((data.points[pc]["name"], t[0], t[1]))
                if temps:
                    tt = temps[0]
            days.append({
                "date": d, "kind": "short", "code": code, "weather": data.weather_text(code),
                "wind": s["wind"].get(d, ""), "slots": slots,
                "pop": max((v for _, v in slots), default=None),
                "temps": temps, "title_temps": tt,
            })
        else:
            code, pop = w["weather"][d]
            winfo = data.weekly_areas[wcode]
            temps = []
            for pc in winfo["points"]:
                t = wtemps.get(pc, {}).get(d)
                if t and (t[0] is not None or t[1] is not None):
                    temps.append((data.points[pc]["name"], t[0], t[1]))
            days.append({
                "date": d, "kind": "weekly", "code": code, "weather": data.weather_text(code),
                "wind": "", "slots": [], "pop": pop,
                "temps": temps, "title_temps": temps[0] if temps else None,
            })
    return days


def temp_segment(tt):
    if not tt:
        return ""
    _, mn, mx = tt
    if mn is not None and mx is not None:
        return f"{mx}℃/{mn}℃"
    if mx is not None:
        return f"最高{mx}℃"
    if mn is not None:
        return f"最低{mn}℃"
    return ""


def day_title(day):
    parts = [day["weather"], temp_segment(day["title_temps"])]
    if day["pop"] is not None:
        parts.append(f"{day['pop']}%")
    return " ".join(p for p in parts if p)


def day_lines(day, generated_str):
    lines = []
    if day["weather"]:
        lines.append(f"天気：{day['weather']}")
    if day["slots"]:
        lines.append("降水確率：" + "／".join(f"{label} {v}%" for label, v in day["slots"]))
    elif day["pop"] is not None:
        lines.append(f"降水確率：{day['pop']}%")
    if day["wind"]:
        lines.append(f"風：{day['wind']}")
    if day["temps"]:
        segs = []
        for name, mn, mx in day["temps"]:
            seg = name
            if mx is not None:
                seg += f" 最高{mx}℃"
            if mn is not None:
                seg += f" 最低{mn}℃"
            segs.append(seg)
        lines.append("気温：" + "／".join(segs))
    lines.append(SOURCE_LINE)
    lines.append(f"生成：{generated_str}")
    return lines


def ical_escape(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 75:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


def render_ical(area_code, area_name, days, generated_str, stamp):
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//nidoneru//jma-weather-feed//JA",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:天気予報フィード（{area_name}）",
        "X-WR-TIMEZONE:Asia/Tokyo",
    ]
    for day in days:
        d = datetime.strptime(day["date"], "%Y-%m-%d").date()
        lines += [
            "BEGIN:VEVENT",
            f"UID:{area_code}-{day['date'].replace('-', '')}@jma-weather-feed",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{ical_escape(day_title(day))}",
            f"DESCRIPTION:{ical_escape(chr(10).join(day_lines(day, generated_str)))}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def xml_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_rss(area_code, area_name, days, generated_str, gen_dt):
    items = []
    for day in days:
        d = datetime.strptime(day["date"], "%Y-%m-%d").date()
        title = f"{area_name} {d.month}月{d.day}日（{WEEKDAYS[d.weekday()]}）{day_title(day)}"
        pub = datetime(d.year, d.month, d.day, 5, 0, tzinfo=JST)
        items.append(
            "<item>"
            f"<title>{xml_escape(title)}</title>"
            f"<link>{BASE}/#{area_code}</link>"
            f"<guid isPermaLink=\"false\">{area_code}-{d.strftime('%Y%m%d')}</guid>"
            f"<pubDate>{pub.strftime('%a, %d %b %Y %H:%M:%S %z')}</pubDate>"
            f"<description>{xml_escape(chr(10).join(day_lines(day, generated_str)))}</description>"
            "</item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel>'
        f"<title>天気予報フィード {xml_escape(area_name)}</title>"
        f"<link>{BASE}/#{area_code}</link>"
        f"<description>気象庁の天気予報（{xml_escape(area_name)}）。{SOURCE_LINE}しています。</description>"
        "<language>ja</language>"
        f"<lastBuildDate>{gen_dt.strftime('%a, %d %b %Y %H:%M:%S %z')}</lastBuildDate>"
        + "".join(items) +
        "</channel></rss>\n"
    )


def render_json(area_code, area_name, office_name, days, generated_str):
    out_days = []
    for day in days:
        out_days.append({
            "date": day["date"], "kind": day["kind"],
            "weatherCode": day["code"], "weather": day["weather"],
            "wind": day["wind"] or None,
            "pops": ([{"slot": label, "value": v} for label, v in day["slots"]] if day["slots"]
                     else ([{"value": day["pop"]}] if day["pop"] is not None else [])),
            "temps": [{"point": n, "min": mn, "max": mx} for n, mn, mx in day["temps"]],
        })
    return json.dumps({
        "area": {"code": area_code, "name": area_name, "office": office_name},
        "generated": generated_str, "source": SOURCE_LINE,
        "days": out_days,
    }, ensure_ascii=False, indent=1) + "\n"


INDEX_CSS = """
:root{
  --primary:#0b57d0; --on-primary:#ffffff; --primary-container:#d3e3fd; --on-primary-container:#041e49;
  --surface:#f8fafd; --surface-container:#eef2f7; --card:#ffffff; --on-surface:#1f1f1f;
  --on-surface-variant:#444746; --outline:#c4c7c5;
}
@media (prefers-color-scheme: dark){
  :root{
    --primary:#a8c7fa; --on-primary:#0a305f; --primary-container:#004a77; --on-primary-container:#d3e3fd;
    --surface:#131314; --surface-container:#1e1f20; --card:#1e1f20; --on-surface:#e3e3e3;
    --on-surface-variant:#c4c7c5; --outline:#444746;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--surface);color:var(--on-surface);
  font-family:"Roboto","Noto Sans JP",sans-serif;line-height:1.7}
main{max-width:880px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:28px;font-weight:400;margin:8px 0 4px}
h2{font-size:20px;font-weight:500;margin:0 0 12px}
.lead{color:var(--on-surface-variant);margin:0 0 8px}
.meta{color:var(--on-surface-variant);font-size:13px;margin:0 0 24px}
.card{background:var(--card);border:1px solid var(--outline);border-radius:24px;
  padding:20px;margin:0 0 16px}
.sample-title{font-size:18px;font-weight:500;margin:0 0 8px}
.sample-body{white-space:pre-line;color:var(--on-surface-variant);margin:0;font-size:14px}
.area-row{display:flex;align-items:center;gap:8px;padding:8px 0;flex-wrap:wrap}
.area-name{min-width:9em;font-weight:500}
.btn{display:inline-block;padding:6px 16px;border-radius:999px;text-decoration:none;
  background:var(--primary-container);color:var(--on-primary-container);font-size:14px}
.btn:hover{background:var(--primary);color:var(--on-primary)}
ol{margin:0;padding-left:1.4em}
.small{font-size:13px;color:var(--on-surface-variant)}
a{color:var(--primary)}
"""


def render_index(area, grouped, sample, generated_str):
    office_cards = []
    for oc, areas in grouped:
        rows = []
        for code, name in areas:
            rows.append(
                '<div class="area-row">'
                f'<span class="area-name">{xml_escape(name)}</span>'
                f'<a class="btn" href="ical/{code}.ics">iCal</a>'
                f'<a class="btn" href="webcal://nidoneru.github.io/jma-weather-feed/ical/{code}.ics">webcal</a>'
                f'<a class="btn" href="rss/{code}.xml">RSS</a>'
                f'<a class="btn" href="json/{code}.json">JSON</a>'
                "</div>"
            )
        office_cards.append(
            f'<section class="card"><h2>{xml_escape(area["offices"][oc]["name"])}</h2>'
            + "".join(rows) + "</section>"
        )
    sample_html = ""
    if sample:
        sample_html = (
            '<section class="card"><h2>表示例（東京地方）</h2>'
            f'<p class="sample-title">{xml_escape(sample[0])}</p>'
            f'<p class="sample-body">{xml_escape(sample[1])}</p></section>'
        )
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>天気予報フィード</title>
<link rel="icon" type="image/svg+xml" href="favicon.svg">
<style>{INDEX_CSS}</style>
</head>
<body>
<main>
<h1>天気予報フィード</h1>
<p class="lead">気象庁が発表する天気予報を、一次細分区域（全国142地点）ごとに iCal・RSS・JSON で配布しています。短期予報と週間予報を、短期優先でつないだ7日分です。1日3回（朝・昼・夕）更新します。</p>
<p class="meta">生成：{generated_str}／{SOURCE_LINE}しています</p>
{sample_html}
<section class="card"><h2>カレンダーへの登録（Google カレンダー）</h2>
<ol>
<li>下の地点一覧で、自分の地域の「iCal」リンクの URL をコピーします</li>
<li>Google カレンダーの「設定」→「カレンダーを追加」→「URLで追加」に貼り付けます</li>
<li>スマホのカレンダーアプリ（Google カレンダーと同期するもの）に自動で届きます</li>
</ol>
<p class="small">webcal のリンクは、対応アプリで開くとそのまま購読登録に進みます。iPhone・Mac のカレンダーは webcal、または iCal の URL を「新規照会カレンダー」に入力してください。</p>
</section>
<section class="card"><h2>RSS リーダーへの取り込み</h2>
<ol>
<li>1地点だけなら、その地点の「RSS」リンクをリーダーに登録します</li>
<li>まとめて取り込むなら、<a href="feeds.opml">OPML ファイル</a>をリーダーに読み込ませます（予報区ごとのフォルダで入ります）</li>
</ol>
</section>
<h2 style="margin:24px 0 12px">地点一覧</h2>
{''.join(office_cards)}
<section class="card"><h2>出典と免責</h2>
<p>{SOURCE_LINE}しています。データは気象庁ホームページ（https://www.jma.go.jp/）で発表されたものです。</p>
<p>このサービスは個人が運営する非公式のものです。予報の正確性は保証しません。防災など重要な判断には、必ず気象庁の公式情報を確認してください。</p>
<p class="small">問い合わせ・不具合の報告は <a href="https://github.com/nidoneru/jma-weather-feed/issues">GitHub Issues</a> へ。</p>
</section>
</main>
</body>
</html>
"""


def render_opml(area, grouped):
    offices = []
    for oc, areas in grouped:
        rows = "".join(
            f'<outline text="{xml_escape(name)}" type="rss" '
            f'xmlUrl="{BASE}/rss/{code}.xml" htmlUrl="{BASE}/#{code}"/>'
            for code, name in areas
        )
        offices.append(f'<outline text="{xml_escape(area["offices"][oc]["name"])}">{rows}</outline>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<opml version="2.0"><head><title>天気予報フィード</title></head>'
        f'<body><outline text="天気予報フィード">{"".join(offices)}</outline></body></opml>\n'
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    for sub in ("ical", "rss", "json"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    gen_dt = datetime.now(JST)
    generated_str = gen_dt.strftime("%Y年%m月%d日 %H:%M")
    stamp = gen_dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    data = Data()
    area = fetch_json(AREA_JSON)
    short, weekly, temps_all, wtemps = parse_forecasts(area)

    grouped = []
    for oc in sorted(area["offices"]):
        areas = sorted(
            (code, info["name"]) for code, info in area["class10s"].items() if info["parent"] == oc
        )
        if areas:
            grouped.append((oc, areas))

    sample = None
    count = 0
    for oc, areas in grouped:
        office_name = area["offices"][oc]["name"]
        for code, name in areas:
            days = build_days(data, code, short, weekly, temps_all, wtemps)
            if not days:
                print(f"WARN no days for {code} {name}", file=sys.stderr)
                continue
            (out / "ical" / f"{code}.ics").write_text(
                render_ical(code, name, days, generated_str, stamp), encoding="utf-8", newline="")
            (out / "rss" / f"{code}.xml").write_text(
                render_rss(code, name, days, generated_str, gen_dt), encoding="utf-8")
            (out / "json" / f"{code}.json").write_text(
                render_json(code, name, office_name, days, generated_str), encoding="utf-8")
            if code == "130010" and len(days) > 1:
                sample = (day_title(days[1]), "\n".join(day_lines(days[1], generated_str)))
            count += 1
    (out / "index.html").write_text(render_index(area, grouped, sample, generated_str), encoding="utf-8")
    (out / "feeds.opml").write_text(render_opml(area, grouped), encoding="utf-8")
    favicon = ROOT / "favicon.svg"
    if favicon.exists():
        shutil.copy(favicon, out / "favicon.svg")
    print(f"generated {count} areas")
    if data.unknown_codes:
        print(f"WARN unknown weather codes: {sorted(data.unknown_codes)}", file=sys.stderr)


if __name__ == "__main__":
    main()
