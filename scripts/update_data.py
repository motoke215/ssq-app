#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
彩票开奖数据自动更新脚本（双色球 + 大乐透，供 GitHub Actions 定时运行，也可本地跑）。

每个彩种两个数据源交叉验证：
  双色球：500彩票网历史页（主源） + 中国福利彩票官网接口（核对源）
  大乐透：500彩票网历史页（主源） + 中国体育彩票官网接口（核对源）
规则：
  - 同一期号多源号码不一致 = 冲突，立刻报错，该彩种不写文件（宁可不更新，不写错数据）。
  - 新期号只有单源有时，先不收，等另一源跟上（两源都可达时）；只有一源可达时单源更新，
    但会在 data 文件里标注 sources / verified 字段，不装作双源。
  - 每条记录做格式校验（号码个数、不重复、范围）。
  - 一个彩种失败不影响另一个；两个都失败才以非 0 退出。
2026-10-28 本机实测：500彩票网两彩种均可抓且数据与独立报道核对一致；
福彩官网对机房 IP 返回 403、体彩官网接口返回 567，核对源在 GitHub Actions 上能否连通待实测，
故核对源只做「能连上就核对」，连不上不阻断主源更新（但会标注单源）。
只用 Python 标准库。
"""
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = ROOT / "index.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.cwl.gov.cn/ygkj/wqkjgg/",
}

GAMES = {
    "ssq": {"fk": 6, "fn": 33, "bk": 1, "bn": 16, "base_name": "BASE_SSQ",
            "files": ["data-ssq.json", "data.json"]},
    "dlt": {"fk": 5, "fn": 35, "bk": 2, "bn": 12, "base_name": "BASE_DLT",
            "files": ["data-dlt.json"]},
}


def fetch(url, timeout=30, headers=None):
    req = urllib.request.Request(url, headers=headers or HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def valid(g, issue, date, front, back):
    return (
        len(issue) == 5
        and len(front) == g["fk"] and len(set(front)) == g["fk"]
        and all(1 <= x <= g["fn"] for x in front)
        and len(back) == g["bk"] and len(set(back)) == g["bk"]
        and all(1 <= x <= g["bn"] for x in back)
        and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""))
    )


# ---------- 双色球源 ----------
def parse_500_ssq(raw):
    text = raw.decode("gb2312", errors="ignore")
    out = []
    for row in re.findall(r'<tr class="t_tr1">(.*?)</tr>', text, re.S):
        im = re.search(r"<td>(\d{5})</td>", row)
        reds = [int(x) for x in re.findall(r'class="t_cfont2">(\d+)</td>', row)]
        bm = re.search(r'class="t_cfont4">(\d+)</td>', row)
        dm = re.search(r"<td>(\d{4}-\d{2}-\d{2})</td>\s*$", row.strip())
        if im and len(reds) == 6 and bm and dm:
            r = (im.group(1), dm.group(1), sorted(reds), [int(bm.group(1))])
            if valid(GAMES["ssq"], *r):
                out.append(r)
    return out


def parse_official_ssq(raw):
    doc = json.loads(raw.decode("utf-8"))
    out = []
    for item in doc.get("result", []):
        code = str(item.get("code", "")).strip()
        issue = code[-5:] if len(code) >= 5 else code
        date = re.sub(r"\(.*?\)", "", str(item.get("date", ""))).strip()[:10]
        reds = sorted(int(x) for x in str(item.get("red", "")).split(",") if x.strip())
        back = [int(item.get("blue", "0"))]
        r = (issue, date, reds, back)
        if valid(GAMES["ssq"], *r):
            out.append(r)
    return out


# ---------- 大乐透源 ----------
def parse_500_dlt(raw):
    text = raw.decode("gb2312", errors="ignore")
    out = []
    parts = re.split(r'<td class="t_tr1">(\d{5})</td>', text)
    for i in range(1, len(parts) - 1, 2):
        issue, rest = parts[i], parts[i + 1]
        front = [int(x) for x in re.findall(r'class="cfont2">(\d+)</td>', rest)][:5]
        back = [int(x) for x in re.findall(r'class="cfont4">(\d+)</td>', rest)][:2]
        dm = re.search(r"(\d{4}-\d{2}-\d{2})", rest)
        if len(front) == 5 and len(back) == 2 and dm:
            r = (issue, dm.group(1), sorted(front), sorted(back))
            if valid(GAMES["dlt"], *r):
                out.append(r)
    return out


def parse_sporttery_dlt(raw):
    doc = json.loads(raw.decode("utf-8"))
    out = []
    for item in (doc.get("value", {}) or {}).get("list", []) or []:
        code = str(item.get("lotteryDrawNum", "")).strip()
        issue = code[-5:] if len(code) >= 5 else code
        date = str(item.get("lotteryDrawTime", ""))[:10]
        nums = [int(x) for x in str(item.get("lotteryDrawResult", "")).split() if x.strip()]
        if len(nums) == 7:
            r = (issue, date, sorted(nums[:5]), sorted(nums[5:]))
            if valid(GAMES["dlt"], *r):
                out.append(r)
    return out


SOURCES = {
    "ssq": [
        ("500彩票网",
         "https://datachart.500.com/ssq/history/newinc/history.php?limit=30",
         parse_500_ssq, None),
        ("福彩官网",
         "https://www.cwl.gov.cn/cwl_admin/front/cwlkj/search/kjxx/findDrawNotice"
         "?name=ssq&issueCount=30&issueStart=&issueEnd=&dayStart=&dayEnd="
         "&pageNo=1&pageSize=30&week=&systemType=PC",
         parse_official_ssq, 15),
    ],
    "dlt": [
        ("500彩票网",
         "https://datachart.500.com/dlt/history/newinc/history.php?limit=30",
         parse_500_dlt, None),
        ("体彩官网",
         "https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry"
         "?gameNo=85&provinceId=0&pageSize=30&isVerify=1&pageNo=1",
         parse_sporttery_dlt, 15),
    ],
}


def compact_of(g, issue, date, front, back):
    f = "".join(f"{x:02d}" for x in front)
    b = str(back[0]) if g["bk"] == 1 else "".join(f"{x:02d}" for x in back)
    return f"{issue}|{date}|{f}|{b}"


def parse_compact(g, raw):
    records = {}
    for chunk in raw.split(";"):
        if not chunk.strip():
            continue
        issue, date, fstr, bstr = chunk.split("|")
        front = [int(fstr[i:i + 2]) for i in range(0, 2 * g["fk"], 2)]
        back = [int(bstr)] if g["bk"] == 1 else [int(bstr[i:i + 2]) for i in range(0, 2 * g["bk"], 2)]
        records[issue] = (date, front, back)
    return records


def load_existing(gid):
    g = GAMES[gid]
    data_file = ROOT / g["files"][0]
    if data_file.exists():
        return parse_compact(g, json.loads(data_file.read_text(encoding="utf-8")).get("records", ""))
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(rf"const {g['base_name']}=`([^`]*)`", html, re.S)
    if not m:
        raise RuntimeError(f"找不到内置 {g['base_name']} 数据")
    return parse_compact(g, m.group(1))


def update_game(gid):
    g = GAMES[gid]
    print(f"===== {gid} =====")
    records = load_existing(gid)
    before_count = len(records)

    sources = {}
    for name, url, parser, timeout in SOURCES[gid]:
        try:
            rows = parser(fetch(url, timeout=timeout or 30))
            if rows:
                sources[name] = {i: (d, tuple(f), tuple(b)) for i, d, f, b in rows}
                print(f"{name}：{len(rows)} 期，最新 {rows[0][0]} 期")
        except Exception as e:
            print(f"{name} 不可达（{e}），跳过")
    if not sources:
        raise RuntimeError("所有数据源都抓不到")

    all_issues = set()
    for src in sources.values():
        all_issues |= set(src)
    conflicts, accepted, deferred = [], {}, []
    for issue in sorted(all_issues):
        values = {src[issue] for src in sources.values() if issue in src}
        present_in = sum(1 for src in sources.values() if issue in src)
        if len(values) > 1:
            conflicts.append(issue)
        elif issue not in records and len(sources) >= 2 and present_in < len(sources):
            deferred.append(issue)
        else:
            accepted[issue] = next(iter(values))
    if conflicts:
        raise RuntimeError(f"多源冲突，拒绝写入！冲突期号：{conflicts}")
    if deferred:
        print(f"新期号暂未被全部数据源确认，先不收：{deferred}")
    verified = len(sources) >= 2
    print(f"交叉验证：{len(sources)} 个源可用" + ("，重叠期号全部一致" if verified else "（单源更新，已标注）"))

    added = []
    for issue, (d, f, b) in accepted.items():
        if issue not in records:
            added.append(issue)
        records[issue] = (d, list(f), list(b))

    issues_sorted = sorted(records)
    compact = ";".join(compact_of(g, i, *records[i]) for i in issues_sorted)
    old_records = ""
    data_file = ROOT / g["files"][0]
    if data_file.exists():
        try:
            old_records = json.loads(data_file.read_text(encoding="utf-8")).get("records", "")
        except Exception:
            old_records = ""
    if compact == old_records:
        print(f"无新开奖：共 {before_count} 期，最新 {issues_sorted[-1]} 期，文件不变")
        return True
    beijing = datetime.now(timezone(timedelta(hours=8)))
    doc = {
        "name": gid,
        "updated": beijing.strftime("%Y-%m-%d %H:%M"),
        "count": len(issues_sorted),
        "latest": issues_sorted[-1],
        "sources": sorted(sources),
        "verified": verified,
        "records": compact,
    }
    text = json.dumps(doc, ensure_ascii=False)
    for fname in g["files"]:
        (ROOT / fname).write_text(text, encoding="utf-8")
    print(f"已更新 {g['files']}：{before_count} 期 -> {len(issues_sorted)} 期，新增 {sorted(added)}，最新 {issues_sorted[-1]} 期")
    return True


def main():
    results = {}
    for gid in GAMES:
        try:
            results[gid] = update_game(gid)
        except Exception as e:
            print(f"{gid} 更新失败：{e}")
            results[gid] = False
    print(f"结果：{results}")
    return 0 if any(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
