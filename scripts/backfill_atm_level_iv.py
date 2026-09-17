"""過去バーの atm_level_iv（ATMの面の上下だけのIV）を遡及計算して書き戻す。

ライブでは ChainData.calculate_atm_level_iv() が板から直接スマイルを作って

    K_same        = K_atm / F_prev * F_now
    atm_level_iv  = IV_today(K_same)

を求めている。過去バーにはその瞬間のスマイルが残っていないので、ここでは
同じ式を次のように分解して計算する:

    atm_level_iv = atm_iv + [ S(K_same) - S(K_atm) ]

角括弧の中は「滑り」の符号反転で、数百円ぶんの行使価格差に対するIVの傾き
だけで決まる。水準（atm_iv）はそのバー自身の記録値をそのまま使うので、
形（S）だけを別のソースから借りればよい:

  chain  … 15分足のオプション各銘柄バー（iv 付き）から作った実際のスマイル。
           そのバー以前で直近のスナップショットを使う。形は分単位ではほとんど
           動かないので、これがほぼライブと同じ精度になる。
  sparse … 15分足チェーンが無い期間のフォールバック。先物バーに記録されて
           いる ATM / Δ0.1 / Δ0.02 の5点だけでスマイルを張る。行使価格の
           足場が2000〜5000円離れるため、ATM近傍の傾きが鈍り誤差が出る。

使い方:
    python backfill_atm_level_iv.py --validate      # 書き込まずに精度を測る
    python backfill_atm_level_iv.py                 # chain のみ書き込む
    python backfill_atm_level_iv.py --with-sparse   # 5点近似も書き込む
"""
from __future__ import annotations

import argparse
import json
import os
from bisect import bisect_right
from datetime import datetime, timedelta

import pymysql


# 15分足スナップショットをどこまで遡って使ってよいか。板が薄い時間帯は
# スナップショットが飛ぶので、1本ぶんより少し広く取る。
MAX_SNAPSHOT_AGE = timedelta(minutes=45)


def connect() -> pymysql.connections.Connection:
    """~/.vntrader/vt_setting.json の接続設定でつなぐ。"""
    path: str = os.path.expanduser("~/.vntrader/vt_setting.json")
    cfg: dict = json.load(open(path, encoding="utf-8"))
    return pymysql.connect(
        host=cfg["database.host"], port=int(cfg["database.port"]),
        user=cfg["database.user"], password=cfg["database.password"],
        database=cfg["database.database"], charset="utf8mb4",
    )


def interp(strikes: list[float], ivs: list[float], k: float) -> float | None:
    """スマイルを線形補間して行使価格 k のIVを返す。外挿はしない。"""
    if len(strikes) < 2 or k < strikes[0] or k > strikes[-1]:
        return None
    i: int = bisect_right(strikes, k)
    if i == 0:
        return ivs[0]
    if i >= len(strikes):
        return ivs[-1]
    a, b = strikes[i - 1], strikes[i]
    va, vb = ivs[i - 1], ivs[i]
    if b == a:
        return va
    return va + (vb - va) * (k - a) / (b - a)


def load_chain_snapshots(
    cur, month: str
) -> tuple[list[datetime], list[tuple[list[float], list[float]]]]:
    """15分足のオプションバーから (時刻, スマイル) の列を作る。

    同じ行使価格にコールとプットの両方があれば平均する（カーブ画面の
    前日比IV、およびライブの calculate_atm_level_iv と同じ作り方）。
    """
    cur.execute(
        """SELECT datetime, strike, iv FROM dbbardata
           WHERE symbol LIKE %s AND `interval`='15m' AND iv > 0 AND strike > 0
           ORDER BY datetime, strike""",
        (f"{month}-%",),
    )
    per_dt: dict[datetime, dict[float, list[float]]] = {}
    for dt, strike, iv in cur.fetchall():
        per_dt.setdefault(dt, {}).setdefault(float(strike), []).append(float(iv))

    times: list[datetime] = []
    smiles: list[tuple[list[float], list[float]]] = []
    for dt in sorted(per_dt):
        by_strike = per_dt[dt]
        if len(by_strike) < 2:
            continue
        ks: list[float] = sorted(by_strike)
        times.append(dt)
        smiles.append((ks, [sum(by_strike[k]) / len(by_strike[k]) for k in ks]))
    return times, smiles


def sparse_smile(row: dict) -> tuple[list[float], list[float]]:
    """先物バーに記録された5点（Δ0.02 / Δ0.1 / ATM）でスマイルを張る。"""
    points: dict[float, float] = {}
    for strike, iv in (
        (row["delta002_p_strike"], row["delta002_p_iv"]),
        (row["eris_p_strike"], row["eris_p_iv"]),
        (row["atm_strike"], row["atm_iv"]),
        (row["eris_c_strike"], row["eris_c_iv"]),
        (row["delta002_c_strike"], row["delta002_c_iv"]),
    ):
        if strike and iv:
            points[float(strike)] = float(iv)
    ks: list[float] = sorted(points)
    return ks, [points[k] for k in ks]


def slide(
    smile: tuple[list[float], list[float]], k_atm: float, k_same: float
) -> float | None:
    """S(K_same) - S(K_atm)。これを atm_iv に足すと面の上下だけのIVになる。"""
    ks, ivs = smile
    a = interp(ks, ivs, k_same)
    b = interp(ks, ivs, k_atm)
    if a is None or b is None:
        return None
    return a - b


def months(cur) -> list[str]:
    """atm_iv が入っている先物分足のある限月。"""
    cur.execute(
        """SELECT DISTINCT symbol FROM dbbardata
           WHERE `interval`='1m' AND atm_iv > 0 AND symbol REGEXP '^nk-[0-9]{4}$'
           ORDER BY symbol"""
    )
    return [r[0] for r in cur.fetchall()]


def process(conn, month: str, write: bool, with_sparse: bool, validate: bool,
            causal: bool = False) -> dict:
    cur = conn.cursor()
    chain_times, chain_smiles = load_chain_snapshots(cur, month)

    cur.execute(
        """SELECT datetime, exchange, close_price, pre_close, atm_iv,
                  eris_p_strike, eris_p_iv, eris_c_strike, eris_c_iv,
                  delta002_p_strike, delta002_p_iv, delta002_c_strike, delta002_c_iv
           FROM dbbardata
           WHERE symbol=%s AND `interval`='1m' AND atm_iv > 0 AND pre_close > 0
           ORDER BY datetime""",
        (month,),
    )
    cols = [
        "datetime", "exchange", "close_price", "pre_close", "atm_iv",
        "eris_p_strike", "eris_p_iv", "eris_c_strike", "eris_c_iv",
        "delta002_p_strike", "delta002_p_iv", "delta002_c_strike", "delta002_c_iv",
    ]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    stats = {
        "bars": len(rows), "chain": 0, "sparse": 0, "skipped": 0,
        "diffs": [], "chain_slides": [],
    }
    updates: list[tuple] = []

    for row in rows:
        close: float = float(row["close_price"])
        prev: float = float(row["pre_close"])
        atm_iv: float = float(row["atm_iv"])
        k_atm: float = round(close / 1000) * 1000      # チャートと同じATM行使価格
        row["atm_strike"] = k_atm
        k_same: float = k_atm / prev * close

        # --- 形は直近の15分足チェーンから
        chain_slide: float | None = None
        # 15分足バケツはその中で毎分 upsert されるので、中身はバケツ終了時刻の
        # スナップショットになる。--causal を付けると、バー時刻には既に
        # 閉じていたバケツだけを使う（先読みを完全に消す）。
        ref_dt: datetime = row["datetime"]
        if causal:
            ref_dt = ref_dt - timedelta(minutes=15)
        i: int = bisect_right(chain_times, ref_dt)
        if i:
            snap_dt = chain_times[i - 1]
            if row["datetime"] - snap_dt <= MAX_SNAPSHOT_AGE:
                chain_slide = slide(chain_smiles[i - 1], k_atm, k_same)

        sparse_slide_v: float | None = slide(sparse_smile(row), k_atm, k_same)

        if validate and chain_slide is not None and sparse_slide_v is not None:
            stats["chain_slides"].append(chain_slide)
            stats["diffs"].append(sparse_slide_v - chain_slide)

        if chain_slide is not None:
            value = atm_iv + chain_slide
            stats["chain"] += 1
        elif with_sparse and sparse_slide_v is not None:
            value = atm_iv + sparse_slide_v
            stats["sparse"] += 1
        else:
            stats["skipped"] += 1
            continue

        updates.append((value, month, row["exchange"], row["datetime"]))

    if write and updates:
        for i in range(0, len(updates), 2000):
            cur.executemany(
                """UPDATE dbbardata SET atm_level_iv=%s
                   WHERE symbol=%s AND exchange=%s AND `interval`='1m'
                     AND datetime=%s""",
                updates[i:i + 2000],
            )
        conn.commit()

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate", action="store_true",
                        help="書き込まず、5点近似の誤差を実チェーン基準で測る")
    parser.add_argument("--with-sparse", action="store_true",
                        help="15分足チェーンが無いバーも5点近似で埋める")
    parser.add_argument("--causal", action="store_true",
                        help="バー時刻に閉じていたチェーンだけを使う（先読み除去）")
    parser.add_argument("--month", action="append",
                        help="対象限月（例 nk-2610）。省略時は全限月")
    args = parser.parse_args()

    conn = connect()
    cur = conn.cursor()
    targets: list[str] = args.month or months(cur)

    total = {"bars": 0, "chain": 0, "sparse": 0, "skipped": 0}
    all_diffs: list[float] = []
    all_slides: list[float] = []

    for month in targets:
        s = process(conn, month, write=not args.validate,
                    with_sparse=args.with_sparse, validate=args.validate,
                    causal=args.causal)
        for k in total:
            total[k] += s[k]
        all_diffs += s["diffs"]
        all_slides += s["chain_slides"]
        print(f"{month}: bars={s['bars']:>6}  chain={s['chain']:>6}  "
              f"sparse={s['sparse']:>6}  skipped={s['skipped']:>6}")

    print(f"\n合計: {total}")

    if args.validate and all_diffs:
        import statistics as st
        abs_diffs = sorted(abs(d) * 100 for d in all_diffs)   # IVポイント
        slides = sorted(abs(v) * 100 for v in all_slides)
        n = len(abs_diffs)
        print(f"\n5点近似 vs 実チェーン（滑りの差, IVポイント, n={n}）")
        print(f"  中央値 {abs_diffs[n // 2]:.3f} / 90%点 {abs_diffs[int(n * 0.9)]:.3f}"
              f" / 最大 {abs_diffs[-1]:.3f}")
        print(f"  平均バイアス {st.mean(all_diffs) * 100:+.3f}")
        print(f"  参考: 実チェーンの滑りの大きさ 中央値 {slides[n // 2]:.3f}"
              f" / 90%点 {slides[int(n * 0.9)]:.3f}")

    conn.close()


if __name__ == "__main__":
    main()
