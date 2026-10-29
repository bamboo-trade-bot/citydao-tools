"""良級懸賞 (citydao.world) 套組期望值計算

比值 = (剩餘各賞價值 × 剩餘數量，含小賞) / 剩餘抽數 / 單抽價(現價)

用法:
    python citydao_ev.py            # 全站所有進行中的套組
    python citydao_ev.py 33         # 只查某個 ipWorkId (33 = 蘋果家族)
    python citydao_ev.py 33 12      # 可一次查多個 ipWorkId
    python citydao_ev.py --set 2035 # 只算指定套組 (抽的過程中重跑用，幾秒就好)

最後另列「最佳停手」有正期望值的套組：每抽前判斷繼續抽的期望淨利是否 > 0，
追高價大賞、該停就停。假設只有你在抽。
"""
import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.request

BASE = "https://citydao.world/api/v1"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept": "application/json",
}


def get(path, retries=8):
    req = urllib.request.Request(BASE + path, headers=HEADERS)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # 請求太密集會被 Cloudflare 擋 (403/429)，要等一段時間才會解除
            if e.code not in (403, 429, 503) or attempt == retries - 1:
                raise
            wait = 15 * (attempt + 1)
            print(f"  被限流 ({e.code})，等 {wait} 秒再試…", file=sys.stderr)
            time.sleep(wait)


def list_sets(ip_work_id=None):
    """進行中的套組；ip_work_id 為 None 時抓全站。API 每頁上限 40 筆，翻頁參數是 current。"""
    base = "/lottery-set?status=1&pageSize=40"
    if ip_work_id:
        base += f"&ipWorkId={ip_work_id}"
    sets, page = [], 1
    while True:
        data = get(f"{base}&current={page}")
        batch = data.get("list") or []
        sets += batch
        if not batch or len(sets) >= data.get("total", 0):
            return sets
        page += 1


STATE_CAP = 64      # 追蹤的大賞狀態數上限 (各追蹤賞種 (剩餘數+1) 的乘積)
SIM_TRIALS = 2000


def optimal_stop(prizes, n_left, price, simulate=True):
    """最佳停手：每抽前判斷「繼續抽的期望淨利」是否 > 0，否則停。

    只追蹤價值最高的幾種大賞 (狀態數 <= STATE_CAP)，其餘獎項併成「填充」以平均值計。
    策略只看追蹤賞的剩餘狀態，所以算出的期望值是可實際達成的 (略保守的下界)。
    假設只有你在抽。ev <= 0 表示不值得開抽 (此時不做模擬)；沒有大賞則回傳 None。
    """
    grand = sorted((p for p in prizes if p["is_grand_prize"] and p["remaining_qty"]),
                   key=lambda p: p["points"], reverse=True)
    tracked, states = [], 1
    for p in grand:
        if states * (p["remaining_qty"] + 1) <= STATE_CAP:
            tracked.append(p)
            states *= p["remaining_qty"] + 1
    if not tracked or n_left <= 0:
        return None
    vals = [p["points"] for p in tracked]
    qtys = [p["remaining_qty"] for p in tracked]
    filler_n = n_left - sum(qtys)
    filler_total = sum(p["points"] * p["remaining_qty"] for p in prizes) - sum(
        v * q for v, q in zip(vals, qtys))
    fv = filler_total / filler_n if filler_n else 0.0

    # 狀態編碼 (mixed radix)：idx = sum(g_i * stride_i)
    strides, st = [], 1
    for q in qtys:
        strides.append(st)
        st *= q + 1
    counts = []
    for idx in range(states):
        g, x = [], idx
        for q in qtys:
            g.append(x % (q + 1))
            x //= q + 1
        counts.append(g)
    sums = [sum(g) for g in counts]

    prev = [0.0] * states
    go = [bytearray(states) for _ in range(n_left + 1)]   # go[n][idx]=1 表示該繼續抽
    for n in range(1, n_left + 1):
        cur = [0.0] * states
        row = go[n]
        for idx in range(states):
            f = n - sums[idx]
            if f < 0:
                continue
            c = -price
            if f:
                c += f / n * (fv + prev[idx])
            g = counts[idx]
            for i, gi in enumerate(g):
                if gi:
                    c += gi / n * (vals[i] + prev[idx - strides[i]])
            if c > 0:
                cur[idx] = c
                row[idx] = 1
            if n == n_left and idx == states - 1:
                start_c = c    # 開抽第一抽的期望淨利 (可為負，用來看差多遠)
        prev = cur
    top = states - 1
    chase = " ".join(f"{p['rank']}({p['points']:,})x{p['remaining_qty']}" for p in tracked)
    if start_c <= 0 or not simulate:
        return {"ev": start_c, "chase": chase}

    # 模擬：用實際獎項 (非平均) 跑策略，估勝率與花費分布
    pool = []
    for p in prizes:
        ti = next((i for i, t in enumerate(tracked) if t is p), -1)
        pool += [(p["points"], ti)] * p["remaining_qty"]
    pnls, draws = [], []
    for _ in range(SIM_TRIALS):
        items = pool[:]
        idx, got, k = top, 0, 0
        while k < n_left and go[n_left - k][idx]:
            j = random.randrange(k, n_left)
            items[k], items[j] = items[j], items[k]
            v, ti = items[k]
            got += v
            if ti >= 0:
                idx -= strides[ti]
            k += 1
        pnls.append(got - price * k)
        draws.append(k)
    draws.sort()
    return {
        "ev": start_c,
        "win": sum(x > 0 for x in pnls) / SIM_TRIALS,
        "avg_draws": sum(draws) / SIM_TRIALS,
        "p95_cost": draws[int(SIM_TRIALS * 0.95)] * price,
        "max_cost": n_left * price,
        "chase": chase,
    }


def analyze(set_id):
    d = get(f"/lottery-set/{set_id}")
    s, prizes = d["lotterySet"], d["prizes"]
    value = sum(p["points"] * p["remaining_qty"] for p in prizes)
    remaining, price = s["remainingSets"], s["clientPrice"]
    ev = value / remaining if remaining else 0
    grand_left = sum(p["remaining_qty"] for p in prizes if p["is_grand_prize"])
    hit = grand_left / remaining if remaining else 0
    grand = " ".join(f"{p['rank']}x{p['remaining_qty']}"
                     for p in prizes if p["is_grand_prize"] and p["remaining_qty"])
    name = s["name"] + (f" ({s['titleSuffix']})" if s.get("titleSuffix") else "")
    return {"id": s["id"], "name": name, "price": price, "remaining": remaining,
            "value": value, "ev": ev, "ratio": ev / price if price else 0, "grand": grand, "hit": hit,
            "stop": optimal_stop(prizes, remaining, price), "prizes": prizes}


def breakeven_price(prizes, n_left, price, iters=12):
    """單抽價降到多少，最佳停手才開始有正期望值 (二分搜尋)。"""
    lo, hi = 0.0, float(price)
    for _ in range(iters):
        mid = (lo + hi) / 2
        r = optimal_stop(prizes, n_left, mid, simulate=False)
        if r and r["ev"] > 0:
            lo = mid
        else:
            hi = mid
    return lo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ip_ids", nargs="*", help="ipWorkId；不給就是全站")
    ap.add_argument("--set", dest="set_ids", nargs="+", type=int, help="只算指定套組 ID (網址最後的數字)")
    args = ap.parse_args()
    if args.set_ids:
        ids = args.set_ids
    else:
        ip_ids = args.ip_ids or [None]
        ids = list(dict.fromkeys(s["id"] for ip in ip_ids for s in list_sets(ip)))
    rows = []
    for i, set_id in enumerate(ids, 1):
        rows.append(analyze(set_id))
        print(f"\r  抓取中 {i}/{len(ids)}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    rows.sort(key=lambda r: r["ratio"], reverse=True)

    print(f"{'#':>2}  {'比值':>6}  {'單抽價':>6}  {'剩餘抽':>6}  {'剩餘總值':>9}  {'每抽期望':>8}  "
          f"{'大賞機率':>8}  {'約N抽1中':>8}  套組 / 剩餘大賞 / 網址")
    for i, r in enumerate(rows, 1):
        every = f"{1 / r['hit']:.0f}" if r["hit"] else "-"
        print(f"{i:>2}  {r['ratio']:>6.3f}  {r['price']:>6}  {r['remaining']:>6}  "
              f"{r['value']:>9,}  {r['ev']:>8.2f}  {r['hit']:>8.3%}  {every:>8}  "
              f"{r['name']}  {r['grand']}  https://citydao.world/lottery/{r['id']}")

    good = sorted((r for r in rows if r["stop"] and r["stop"]["ev"] > 0),
                  key=lambda r: r["stop"]["ev"], reverse=True)
    print(f"\n=== 最佳停手有正期望值的套組：{len(good)} 套 (假設只有你在抽、資金夠抽到底) ===")
    if good:
        print(f"{'#':>2}  {'期望淨利':>8}  {'勝率':>6}  {'平均抽數':>8}  {'95%花費內':>9}  {'最壞花費':>9}  追的賞 / 套組 / 網址")
    for i, r in enumerate(good, 1):
        t = r["stop"]
        print(f"{i:>2}  {t['ev']:>+8,.0f}  {t['win']:>6.1%}  {t['avg_draws']:>8.1f}  "
              f"{t['p95_cost']:>9,}  {t['max_cost']:>9,}  {t['chase']}  {r['name']}  "
              f"https://citydao.world/lottery/{r['id']}")
    if good:
        print("\n用法：每抽一批後用 --set <ID> 重跑，該套仍列在這裡就繼續，消失了就停。")
    else:
        cand = sorted((r for r in rows if r["stop"]), key=lambda r: r["ratio"], reverse=True)[:30]
        for r in cand:
            r["be"] = breakeven_price(r["prizes"], r["remaining"], r["price"])
        near = sorted(cand, key=lambda r: r["be"] / r["price"], reverse=True)[:10]
        print("最接近的 10 套：單抽價要降到多少，抽到中為止才有正期望值")
        print(f"  {'現價':>6}  {'需降到':>6}  {'需降':>5}  追的賞 / 套組 / 網址")
        for r in near:
            print(f"  {r['price']:>6}  {r['be']:>6.0f}  {1 - r['be'] / r['price']:>5.0%}  "
                  f"{r['stop']['chase']}  {r['name']}  https://citydao.world/lottery/{r['id']}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
