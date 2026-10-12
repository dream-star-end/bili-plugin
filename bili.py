#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bili — 哔哩哔哩查询 CLI（供 AI agent / 脚本调用）

数据源：B站公开 Web 接口（无需登录）：
  * 搜索   x/web-interface/wbi/search/all/v2（WBI 签名，本工具自动完成）
  * 视频   www.bilibili.com/video 页 __INITIAL_STATE__ 解析
           （view 接口对机房 IP 常 412，页面解析更稳；view 作为兜底）
  * 排行   x/web-interface/ranking/v2；热门 x/web-interface/popular
  * UP主   x/web-interface/card；评论 x/v2/reply（buvid 指纹自动获取）

默认 JSON 输出；全局 --table/--compact/--fields。错误带 code，退出码 1。
仅使用 Python 标准库。
"""
import argparse
import gzip
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "Chrome/126 Safari/537.36")
TIMEOUT = 25
VERSION = "1.0.0"
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".bili_cache")
MIXIN_TABLE = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
               27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
               37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
               22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]

_opener = None


def fail(msg, code="ERROR"):
    print(json.dumps({"error": msg, "code": code}, ensure_ascii=False),
          file=sys.stderr)
    sys.exit(1)


def opener():
    global _opener
    if _opener is None:
        os.makedirs(CACHE_DIR, exist_ok=True)
        jar = http.cookiejar.MozillaCookieJar(
            os.path.join(CACHE_DIR, "cookies.txt"))
        if os.path.exists(jar.filename):
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass
        _opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        _opener._jar = jar
    return _opener


def http_bytes(url, headers=None):
    h = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    last = None
    for attempt in range(3):
        try:
            with opener().open(req, timeout=TIMEOUT) as resp:
                b = resp.read()
            if b[:2] == b"\x1f\x8b":
                b = gzip.decompress(b)
            return b
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (502, 503, 504):
                raise
        except Exception as e:
            last = e
        if attempt < 2:
            time.sleep(0.7 * (attempt + 1))
    raise last


def http_json(url, headers=None):
    return json.loads(http_bytes(url, headers).decode("utf-8", "ignore"))


def ensure_buvid():
    jar = opener()._jar
    if any(c.name == "buvid3" for c in jar):
        return
    data = http_json("https://api.bilibili.com/x/frontend/finger/spi")
    d = data.get("data") or {}
    for name, val in (("buvid3", d.get("b_3")), ("buvid4", d.get("b_4"))):
        if val:
            jar.set_cookie(http.cookiejar.Cookie(
                0, name, val, None, False, ".bilibili.com", True, False,
                "/", True, False, None, False, None, None, {}))
    try:
        jar.save(ignore_discard=True, ignore_expires=True)
    except Exception:
        pass


# ---------------------------------------------------------------- WBI

def wbi_keys():
    path = os.path.join(CACHE_DIR, "wbi.json")
    today = time.strftime("%Y-%m-%d")
    if os.path.exists(path):
        try:
            saved = json.load(open(path, encoding="utf-8"))
            if saved.get("date") == today:
                return saved["img"], saved["sub"]
        except Exception:
            pass
    data = http_json("https://api.bilibili.com/x/web-interface/nav")
    wbi = (data.get("data") or {}).get("wbi_img") or {}
    img = wbi.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
    sub = wbi.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
    if not img or not sub:
        raise RuntimeError("WBI 密钥获取失败")
    os.makedirs(CACHE_DIR, exist_ok=True)
    json.dump({"date": today, "img": img, "sub": sub},
              open(path, "w", encoding="utf-8"))
    return img, sub


def wbi_sign(params):
    img, sub = wbi_keys()
    raw = img + sub
    mixin = "".join(raw[i] for i in MIXIN_TABLE)[:32]
    p = dict(params)
    p["wts"] = int(time.time())
    items = []
    for k in sorted(p):
        v = re.sub(r"[!'()*]", "", str(p[k]))
        items.append((k, v))
    query = urllib.parse.urlencode(items)
    p["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return p


# ---------------------------------------------------------------- video data

def fetch_video_data(ref):
    """BV 号/av 号/完整链接 -> videoData（页面解析为主，view 接口兜底）。"""
    ref = str(ref).strip()
    m = re.search(r"(BV[0-9A-Za-z]{10})", ref)
    avm = re.search(r"av(\d+)", ref)
    if m:
        bvid = m.group(1)
        query = f"bvid={bvid}"
        page_url = f"https://www.bilibili.com/video/{bvid}"
    elif avm or ref.isdigit():
        aid = avm.group(1) if avm else ref
        query = f"aid={aid}"
        page_url = f"https://www.bilibili.com/video/av{aid}"
    else:
        fail(f"无法识别的视频引用: {ref}（需 BV 号/av 号/链接）", code="INVALID")
    # 页面解析（view 接口对不少 IP 段 412，页面更稳）
    try:
        html = http_bytes(page_url).decode("utf-8", "ignore")
        mm = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\(", html, re.S)
        if mm:
            vd = json.loads(mm.group(1)).get("videoData")
            if vd:
                return vd
    except Exception:
        pass
    ensure_buvid()
    data = http_json(f"https://api.bilibili.com/x/web-interface/view?{query}")
    if data.get("code") != 0:
        fail(f"视频获取失败: {data.get('message')}", code="UPSTREAM")
    return data["data"]


def shape_video(vd):
    st = vd.get("stat") or {}
    return {"bvid": vd.get("bvid"), "aid": vd.get("aid"),
            "title": vd.get("title"),
            "author": (vd.get("owner") or {}).get("name"),
            "author_mid": (vd.get("owner") or {}).get("mid"),
            "pic": vd.get("pic"),
            "desc": (vd.get("desc") or "")[:500],
            "duration_sec": vd.get("duration"),
            "pubdate": time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(vd["pubdate"]))
            if vd.get("pubdate") else None,
            "category": vd.get("tname"),
            "stats": {"view": st.get("view"), "danmaku": st.get("danmaku"),
                      "reply": st.get("reply"), "favorite": st.get("favorite"),
                      "coin": st.get("coin"), "share": st.get("share"),
                      "like": st.get("like")},
            "pages": [{"cid": p.get("cid"), "part": p.get("part"),
                       "duration_sec": p.get("duration")}
                      for p in vd.get("pages", [])],
            "url": f"https://www.bilibili.com/video/{vd.get('bvid')}"}


def cmd_video(args):
    emit(shape_video(fetch_video_data(args.ref)), args)


def cmd_search(args):
    ensure_buvid()
    params = wbi_sign({"keyword": args.keyword, "page": 1,
                       "search_type": "video", "order": args.order})
    url = ("https://api.bilibili.com/x/web-interface/wbi/search/all/v2?"
           + urllib.parse.urlencode(params))
    data = http_json(url)
    if data.get("code") != 0:
        fail(f"搜索失败: {data.get('message')}", code="UPSTREAM")
    groups = (data.get("data") or {}).get("result") or []
    videos = []
    for g in groups:
        if g.get("result_type") == "video":
            videos = g.get("data") or []
    items = []
    for v in videos[:args.limit]:
        items.append({
            "bvid": v.get("bvid"),
            "title": re.sub(r"</?em[^>]*>", "", v.get("title") or ""),
            "author": v.get("author"), "mid": v.get("mid"),
            "play": v.get("play"), "danmaku": v.get("video_review"),
            "duration": v.get("duration"),
            "pubdate": time.strftime("%Y-%m-%d", time.localtime(v["pubdate"]))
            if v.get("pubdate") else None,
            "desc": (v.get("description") or "")[:200],
            "url": f"https://www.bilibili.com/video/{v.get('bvid')}"})
    emit({"keyword": args.keyword, "count": len(items), "results": items},
         args, cols=["bvid", "title", "author", "play"])


def _list_shape(v):
    st = v.get("stat") or {}
    return {"bvid": v.get("bvid"), "title": v.get("title"),
            "author": (v.get("owner") or {}).get("name"),
            "play": st.get("view"), "danmaku": st.get("danmaku"),
            "duration_sec": v.get("duration"),
            "url": f"https://www.bilibili.com/video/{v.get('bvid')}"}


def cmd_rank(args):
    data = http_json("https://api.bilibili.com/x/web-interface/ranking/v2"
                     f"?rid={args.rid}&type=all")
    if data.get("code") != 0:
        fail(f"排行榜获取失败: {data.get('message')}", code="UPSTREAM")
    items = [_list_shape(v) for v in
             (data.get("data") or {}).get("list", [])[:args.limit]]
    for i, it in enumerate(items, 1):
        it["rank"] = i
    emit({"rid": args.rid, "results": items}, args,
         cols=["rank", "title", "author", "play"])


def cmd_popular(args):
    data = http_json("https://api.bilibili.com/x/web-interface/popular"
                     f"?ps={min(args.limit, 50)}&pn=1")
    if data.get("code") != 0:
        fail(f"热门获取失败: {data.get('message')}", code="UPSTREAM")
    items = [_list_shape(v) for v in
             (data.get("data") or {}).get("list", [])[:args.limit]]
    for i, it in enumerate(items, 1):
        it["rank"] = i
    emit({"results": items}, args, cols=["rank", "title", "author", "play"])


def cmd_up(args):
    data = http_json("https://api.bilibili.com/x/web-interface/card"
                     f"?mid={args.mid}")
    if data.get("code") != 0:
        fail(f"UP主获取失败: {data.get('message')}", code="UPSTREAM")
    d = data.get("data") or {}
    card = d.get("card") or {}
    emit({"mid": args.mid, "name": card.get("name"),
          "sign": card.get("sign"), "level": card.get("level_info", {}).get(
              "current_level") if isinstance(card.get("level_info"), dict)
          else None,
          "fans": d.get("follower"), "following": card.get("attention"),
          "likes": d.get("like_num"),
          "archive_count": d.get("archive_count"),
          "url": f"https://space.bilibili.com/{args.mid}"}, args)


def cmd_comments(args):
    vd = fetch_video_data(args.ref)
    aid = vd.get("aid")
    ensure_buvid()
    sort = 2 if args.sort == "hot" else 0
    data = http_json("https://api.bilibili.com/x/v2/reply"
                     f"?type=1&oid={aid}&sort={sort}&pn=1&ps={args.limit}")
    if data.get("code") != 0:
        fail(f"评论获取失败: {data.get('message')}", code="UPSTREAM")
    d = data.get("data") or {}
    items = []
    for r in (d.get("replies") or [])[:args.limit]:
        c = r.get("content") or {}
        items.append({"user": (r.get("member") or {}).get("uname"),
                      "like": r.get("like"), "replies": r.get("rcount"),
                      "text": (c.get("message") or "")[:300],
                      "time": time.strftime(
                          "%Y-%m-%d %H:%M", time.localtime(r["ctime"]))
                      if r.get("ctime") else None})
    emit({"bvid": vd.get("bvid"), "title": vd.get("title"),
          "total_comments": (d.get("page") or {}).get("count"),
          "sort": args.sort, "results": items}, args,
         cols=["user", "like", "text"])


def cmd_doctor(args):
    checks = []
    for name, fn in (("nav_wbi", lambda: wbi_keys()),
                     ("popular", lambda: http_json(
                         "https://api.bilibili.com/x/web-interface/popular?ps=1")),
                     ("search_wbi", lambda: None),
                     ("video_page", lambda: fetch_video_data("BV1xx411c7mD"))):
        t0 = time.time()
        try:
            if name == "search_wbi":
                ensure_buvid()
                params = wbi_sign({"keyword": "测试", "page": 1,
                                   "search_type": "video"})
                d = http_json("https://api.bilibili.com/x/web-interface/"
                              "wbi/search/all/v2?" + urllib.parse.urlencode(params))
                if d.get("code") != 0:
                    raise RuntimeError(d.get("message"))
            else:
                fn()
            checks.append({"name": name, "ok": True,
                           "ms": int((time.time() - t0) * 1000)})
        except Exception as e:
            checks.append({"name": name, "ok": False,
                           "ms": int((time.time() - t0) * 1000),
                           "detail": str(e)[:160]})
    ok = all(c["ok"] for c in checks)
    emit({"ok": ok, "checks": checks}, args)
    if not ok:
        sys.exit(1)


COMMANDS_MANIFEST = [
    {"name": "search", "usage": "bili search <关键词> [--limit N] "
     "[--order totalrank|click|pubdate|dm|stow]",
     "desc": "视频搜索（WBI 签名自动处理）"},
    {"name": "video", "usage": "bili video <BV号|av号|链接>",
     "desc": "视频详情：标题/UP/播放弹幕评论收藏投币分享赞/分P/简介"},
    {"name": "rank", "usage": "bili rank [--rid 分区] [--limit N]",
     "desc": "全站排行榜（rid=0 全站）"},
    {"name": "popular", "usage": "bili popular [--limit N]", "desc": "热门视频"},
    {"name": "up", "usage": "bili up <mid>", "desc": "UP主信息（粉丝/获赞/稿件数）"},
    {"name": "comments", "usage": "bili comments <BV号> [--sort hot|time] [--limit N]",
     "desc": "热门/最新评论"},
    {"name": "doctor", "usage": "bili doctor", "desc": "通道健康检查"},
    {"name": "commands", "usage": "bili commands", "desc": "能力清单"},
]


def cmd_commands(args):
    emit({"commands": COMMANDS_MANIFEST,
          "conventions": "默认 JSON；--table/--compact/--fields 全局选项；"
                         "无需登录；字幕/弹幕正文不在本版范围；错误带 code"},
         args)


# ---------------------------------------------------------------- output

_LIST_KEYS = ("results", "checks")


def _pick_fields(obj, fields):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _LIST_KEYS and isinstance(v, list):
                out[k] = [{kk: vv for kk, vv in row.items() if kk in fields}
                          if isinstance(row, dict) else row for row in v]
            elif k in fields:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [{k: v for k, v in row.items() if k in fields}
                if isinstance(row, dict) else row for row in obj]
    return obj


def emit(data, args, cols=None):
    if getattr(args, "table", False):
        rows = data
        if isinstance(data, dict):
            for k in _LIST_KEYS:
                if k in data:
                    meta = {kk: vv for kk, vv in data.items() if kk != k}
                    if meta:
                        print(f"# {meta}")
                    rows = data[k]
                    break
            else:
                rows = [data]
        if isinstance(rows, dict):
            rows = [rows]
        cols = cols or (list(rows[0].keys()) if rows else [])
        print("\t".join(str(c) for c in cols))
        for r in rows:
            print("\t".join("" if r.get(c) is None else str(r.get(c))
                            for c in cols))
    else:
        fields = getattr(args, "fields", None)
        if fields:
            data = _pick_fields(data, set(fields))
        if getattr(args, "compact", False):
            print(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(prog="bili", description="哔哩哔哩查询 CLI（公开接口，免登录）")
    p.add_argument("--version", action="version", version=f"bili {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", help="视频搜索")
    sp.add_argument("keyword")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--order", default="totalrank")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("video", help="视频详情")
    sp.add_argument("ref")
    sp.set_defaults(func=cmd_video)

    sp = sub.add_parser("rank", help="全站排行榜")
    sp.add_argument("--rid", type=int, default=0)
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_rank)

    sp = sub.add_parser("popular", help="热门视频")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_popular)

    sp = sub.add_parser("up", help="UP主信息")
    sp.add_argument("mid")
    sp.set_defaults(func=cmd_up)

    sp = sub.add_parser("comments", help="视频评论")
    sp.add_argument("ref")
    sp.add_argument("--sort", default="hot", choices=["hot", "time"])
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_comments)

    sp = sub.add_parser("doctor", help="通道健康检查")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("commands", help="能力清单")
    sp.set_defaults(func=cmd_commands)

    argv = sys.argv[1:]
    table_flag = "--table" in argv
    compact_flag = "--compact" in argv
    fields, cleaned, i = None, [], 0
    while i < len(argv):
        a = argv[i]
        if a in ("--table", "--compact"):
            i += 1
        elif a == "--fields" and i + 1 < len(argv):
            fields = [x.strip() for x in argv[i + 1].split(",") if x.strip()]
            i += 2
        elif a.startswith("--fields="):
            fields = [x.strip() for x in a[9:].split(",") if x.strip()]
            i += 1
        else:
            cleaned.append(a)
            i += 1
    args = p.parse_args(cleaned)
    args.table, args.compact, args.fields = table_flag, compact_flag, fields
    try:
        args.func(args)
    except BrokenPipeError:
        pass
    except Exception as e:
        code = "INTERNAL"
        if isinstance(e, urllib.error.HTTPError):
            code = {404: "NOT_FOUND", 412: "BLOCKED",
                    429: "RATE_LIMIT"}.get(
                e.code, "UPSTREAM" if e.code >= 500 else "HTTP")
        elif isinstance(e, urllib.error.URLError):
            code = "NETWORK"
        elif isinstance(e, RuntimeError):
            code = "UPSTREAM"
        fail(str(e), code=code)


if __name__ == "__main__":
    main()
