#!/usr/bin/env python3
"""simulator.py v2 - быстрый batch predict"""
import sys, os, json, pickle, argparse, time, warnings
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass
import numpy as np
import pandas as pd

BASE_DIR   = Path(__file__).parent
DATA_DIR   = BASE_DIR / "historical_data"
MODELS_DIR = BASE_DIR / "ml" / "models"

BUY_FEE           = 0.0018
SELL_FEE          = 0.0010
BREAKEVEN_TRIGGER = 1.5   # % роста для активации BE (было 0.5 — слишком рано)
TRAILING_PCT      = 1.5   # % отступ SL от пика  (было 1.0)
MAX_HOLD_BARS     = 1440
WARMUP_BARS       = 500
POSITION_USDT     = 300.0

# Конфигурации для сравнения (--compare режим)
CONFIGS = {
    "A_current":  {"be": 0.5, "trail": 1.0, "label": "Текущий  BE=0.5% Trail=1.0%"},
    "B_moderate": {"be": 1.5, "trail": 1.5, "label": "Умерен.  BE=1.5% Trail=1.5%"},
    "C_wide":     {"be": 2.0, "trail": 1.0, "label": "Широкий  BE=2.0% Trail=1.0%"},
    "D_no_trail": {"be": 99.0,"trail": 0.0, "label": "Без трей (только TP/SL)"},
}

PAIRS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT",
    "AVAXUSDT","APTUSDT","WUSDT","OPUSDT",
    "TIAUSDT","ATOMUSDT","WIFUSDT","ARBUSDT","XAUTUSDT",
    "SUIUSDT","INJUSDT","STXUSDT",
]

NEW_PAIRS = ["SUIUSDT","INJUSDT","STXUSDT"]

def load_pair_params():
    p = BASE_DIR / "pair_params.json"
    if not p.exists(): return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return {k:v for k,v in d.items() if not k.startswith("_")}
    except: return {}

def get_tpslconf(sym, pp):
    p  = pp.get(sym, {})
    tp = float(p.get("tp_pct", 3.0))
    sl = float(p.get("sl_pct", 2.0))
    mc = float(p.get("min_conf", 70))
    if mc > 1: mc /= 100.0
    return tp, sl, mc

def load_model(symbol):
    pkl = MODELS_DIR / f"{symbol}_model.pkl"
    if not pkl.exists(): return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with open(pkl,"rb") as f: data = pickle.load(f)
        if isinstance(data, dict):
            model = data.get("model") or data.get("clf")
            le    = data.get("label_encoder") or data.get("le")
            feats = data.get("features", [])
        else:
            model, le, feats = data, None, []
        if model is None or not hasattr(model,"predict_proba"): return None
        meta_f = MODELS_DIR / f"{symbol}_meta.json"
        meta   = json.loads(meta_f.read_text(encoding="utf-8")) if meta_f.exists() else {}
        if not feats: feats = meta.get("features",[])
        return {"model":model,"le":le,"features":feats}
    except Exception as e:
        print(f"  pkl err: {e}"); return None

def load_csv(symbol):
    for s in ["_indicators_labeled.csv","_30d.csv","_90d.csv"]:
        p = DATA_DIR / f"{symbol}{s}"
        if p.exists():
            try:
                df = pd.read_csv(p, parse_dates=["timestamp"])
                df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
                return df.sort_values("timestamp").reset_index(drop=True)
            except: pass
    return None

def fill_missing(df):
    d = df.copy()
    cl = d["close"] if "close" in d.columns else None
    hi = d["high"]  if "high"  in d.columns else cl
    lo = d["low"]   if "low"   in d.columns else cl
    vo = d["volume"] if "volume" in d.columns else None
    def add(col,val):
        if col not in d.columns: d[col]=val

    # ── Вычисляем dev_* и avg_deviation из сырых свечей если нет ──────────
    # Это то же что делает strategy_engine.py и compute_historical_indicators.py
    if cl is not None:
        for w, col in [(50,"dev_50"),(75,"dev_75"),(100,"dev_100"),
                       (150,"dev_150"),(200,"dev_200")]:
            if col not in d.columns:
                sma = cl.rolling(w, min_periods=w//2).mean().replace(0, 1)
                d[col] = (cl - sma) / sma * 100

        if "avg_deviation" not in d.columns:
            dev_cols = [c for c in ["dev_50","dev_75","dev_100","dev_150","dev_200"]
                        if c in d.columns]
            if dev_cols:
                d["avg_deviation"] = d[dev_cols].mean(axis=1)
                print(f"  ℹ️  avg_deviation вычислен из SMA ({len(dev_cols)} окон)")

        # buy_votes / sell_votes — сколько SMA показывают отклонение > 2%
        if "buy_votes"  not in d.columns and "avg_deviation" in d.columns:
            dev_cols = [c for c in ["dev_50","dev_75","dev_100","dev_150","dev_200"]
                        if c in d.columns]
            d["buy_votes"]  = (d[dev_cols] < -2.0).sum(axis=1).astype(float)
            d["sell_votes"] = (d[dev_cols] >  2.0).sum(axis=1).astype(float)

    if "avg_deviation" in d.columns:
        add("confidence",(d["avg_deviation"].abs()/2.0).clip(upper=1.0))
        add("dev_momentum",d["avg_deviation"].diff(5).fillna(0))
    dev_c=[c for c in ["dev_50","dev_75","dev_100","dev_150","dev_200"] if c in d.columns]
    add("dev_spread", d[dev_c].max(axis=1)-d[dev_c].min(axis=1) if dev_c else 0.0)
    if cl is not None:
        for n,col in [(1,"price_change_1m"),(5,"price_change_5m"),
                      (15,"price_change_15m"),(30,"price_change_30m"),(60,"price_change_60m")]:
            add(col, cl.pct_change(n).fillna(0)*100)
        add("pct_rank_200", cl.rolling(200,min_periods=1).rank(pct=True))
        add("pct_rank_500", cl.rolling(500,min_periods=1).rank(pct=True))
        for w,zk in [(200,"z_score_200"),(500,"z_score_500")]:
            m=cl.rolling(w,min_periods=1).mean(); s=cl.rolling(w,min_periods=1).std().replace(0,1)
            add(zk, ((cl-m)/s).clip(-4,4))
    if cl is not None and hi is not None and lo is not None:
        for w,pk in [(100,"don_pos_100"),(200,"don_pos_200")]:
            dh=hi.rolling(w,min_periods=1).max(); dl=lo.rolling(w,min_periods=1).min()
            add(pk, ((cl-dl)/(dh-dl+1e-9)).clip(0,1))
        dh=hi.rolling(100,min_periods=1).max(); dl=lo.rolling(100,min_periods=1).min()
        add("don_width_100", (dh-dl)/((dh+dl)/2+1e-9)*100)
        pw=1440
        p_h=hi.rolling(pw,min_periods=60).max(); p_l=lo.rolling(pw,min_periods=60).min()
        p_c=cl.rolling(pw,min_periods=60).mean(); pivot=(p_h+p_l+p_c)/3
        add("dist_to_r1", ((2*pivot-p_l-cl)/cl*100).fillna(0))
        add("dist_to_s1", ((cl-(2*pivot-p_h))/cl*100).fillna(0))
        add("above_pivot", (cl>pivot).astype(float))
        add("pivot_zone", 0.0)
        up=hi.diff().clip(lower=0); dn=(-lo.diff()).clip(lower=0)
        tr_=pd.concat([hi-lo,(hi-cl.shift()).abs(),(lo-cl.shift()).abs()],axis=1).max(axis=1)
        atr14=tr_.ewm(span=14,adjust=False).mean().replace(0,1)
        dip=100*up.ewm(span=14,adjust=False).mean()/atr14
        dim=100*dn.ewm(span=14,adjust=False).mean()/atr14
        add("di_plus",dip.clip(0,100)); add("di_minus",dim.clip(0,100))
        dx=100*abs(dip-dim)/(dip+dim+1e-9)
        add("adx_14",dx.clip(0,100))
        add("adx_trend",((dx>25)&(dip>dim)).astype(float)-((dx>25)&(dim>dip)).astype(float))
        sma4h=cl.rolling(240,min_periods=1).mean().replace(0,1)
        add("trend_4h", ((cl>sma4h).astype(float)*2-1))
        add("dev_4h",   (cl-sma4h)/sma4h*100)
        add("swing_hi_dist", (hi.rolling(50,min_periods=1).max()-cl)/cl*100)
        add("swing_lo_dist", (cl-lo.rolling(50,min_periods=1).min())/cl*100)
        add("market_structure", ((cl>hi.rolling(20,min_periods=1).max().shift(5)).astype(float)-
                                  (cl<lo.rolling(20,min_periods=1).min().shift(5)).astype(float)))
    if vo is not None:
        add("volume_last",vo)
        rv=vo.rolling(20,min_periods=1).mean().replace(0,1)
        add("vol_ratio",(vo/rv).clip(upper=10))
        add("vol_change",vo.pct_change(3).fillna(0).clip(-5,5))
        if cl is not None:
            direction=np.sign(cl.diff())
            obv=(vo*direction).fillna(0).cumsum()
            add("obv_trend",((obv.diff(10)>0).astype(float)*2-1))
            add("obv_slope",(obv.diff(10)/(vo.rolling(50,min_periods=1).mean().replace(0,1)*10)).clip(-5,5))
    if "rsi_14" in d.columns: add("rsi_14_change",d["rsi_14"].diff(3).fillna(0))
    if "atr_pct" in d.columns:
        ra=d["atr_pct"].rolling(20,min_periods=1).mean().replace(0,1)
        add("atr_ratio",(d["atr_pct"]/ra).clip(upper=5))
    if "bb_upper" in d.columns and "bb_lower" in d.columns:
        bm=d.get("bb_mid",(d["bb_upper"]+d["bb_lower"])/2).replace(0,1)
        add("bb_width",(d["bb_upper"]-d["bb_lower"])/bm*100)
        if "bb_width" in d.columns:
            rb=d["bb_width"].rolling(20,min_periods=1).mean()
            add("bb_squeeze",(d["bb_width"]<rb*0.8).astype(float))
    if "macd_hist" in d.columns:
        rs=d["macd_hist"].rolling(50,min_periods=1).std().replace(0,1)
        add("macd_hist_norm",(d["macd_hist"]/rs).clip(-5,5))
        add("macd_cross",(((d["macd_hist"]>0)&(d["macd_hist"].shift(1)<=0))|
                          ((d["macd_hist"]<0)&(d["macd_hist"].shift(1)>=0))).astype(float))
    for col in ["btc_change_5m","btc_change_15m","btc_change_60m",
                "btc_rsi_14","btc_dev_50","btc_atr_pct","pair_vs_btc_15m"]:
        add(col,0.0)
    return d

def add_btc(df, btc_df, symbol):
    if btc_df is None or symbol=="BTCUSDT": return df
    try:
        needed = [c for c in ["timestamp","close","rsi_14","atr_pct","dev_50"] if c in btc_df.columns]
        b=btc_df[needed].copy()
        rename={"close":"btc_close","rsi_14":"btc_rsi_14","atr_pct":"btc_atr_pct","dev_50":"btc_dev_50"}
        b=b.rename(columns={k:v for k,v in rename.items() if k in b.columns})
        merged=pd.merge_asof(df.sort_values("timestamp"),b.sort_values("timestamp"),
                             on="timestamp",direction="backward")
        bc=merged.get("btc_close",merged["close"])
        merged["btc_change_5m"] =bc.pct_change(5)*100
        merged["btc_change_15m"]=bc.pct_change(15)*100
        merged["btc_change_60m"]=bc.pct_change(60)*100
        if "price_change_15m" in merged.columns:
            merged["pair_vs_btc_15m"]=merged["price_change_15m"]-merged["btc_change_15m"]
        for c in ["btc_rsi_14","btc_dev_50","btc_atr_pct"]:
            if c not in merged.columns: merged[c]=0.0
        return merged.reset_index(drop=True)
    except Exception as e:
        print(f"  btc merge err: {e}"); return df

def batch_predict(df, payload):
    model=payload["model"]; le=payload.get("le"); feats=payload.get("features",[])
    if hasattr(model,"feature_names_in_"):
        feats=[str(f) for f in model.feature_names_in_]
    elif feats:
        feats=[str(f) for f in feats]
    X_df=pd.DataFrame(index=df.index)
    miss=[]
    for f in feats:
        if f in df.columns: X_df[f]=df[f]
        else: X_df[f]=0.0; miss.append(f)
    if miss: print(f"  нули для: {miss[:4]}{'...' if len(miss)>4 else ''}")
    X=X_df.fillna(0).values
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            proba=model.predict_proba(X)
        classes=list(model.classes_)
        if not any(isinstance(c,str) for c in classes):
            d2s={0:"BUY",1:"HOLD",2:"SELL"}
            if le and hasattr(le,"classes_"): d2s={i:c for i,c in enumerate(le.classes_)}
            classes=[d2s.get(int(c),str(c)) for c in classes]
        bi=classes.index("BUY")  if "BUY"  in classes else -1
        si=classes.index("SELL") if "SELL" in classes else -1
        hi=classes.index("HOLD") if "HOLD" in classes else -1
        signals=[]
        for row in proba:
            pb=float(row[bi]) if bi>=0 else 0.0
            ps=float(row[si]) if si>=0 else 0.0
            ph=float(row[hi]) if hi>=0 else 1.0
            if pb>ps and pb>ph: signals.append(("BUY",pb))
            elif ps>pb and ps>ph: signals.append(("SELL",ps))
            else: signals.append(None)
        n=len(signals)
        nb=sum(1 for s in signals if s and s[0]=="BUY")
        ns=sum(1 for s in signals if s and s[0]=="SELL")
        print(f"  BUY={nb}({nb/n*100:.1f}%) SELL={ns}({ns/n*100:.1f}%) HOLD={n-nb-ns}({(n-nb-ns)/n*100:.1f}%)")
        return signals
    except Exception as e:
        print(f"  predict err: {e}"); return [None]*len(df)

@dataclass
class Trade:
    symbol:str; t_open:str; t_close:str
    entry:float; exit_p:float; result:str
    pnl_pct:float; pnl_usdt:float; hold_bars:int
    conf:float; dev:float; rsi:float=50.0

def simulate(symbol,df,signals,tp_pct,sl_pct,min_conf,t_from,t_to,be_trig=None,trail_pct=None):
    trades=[]
    n=len(df)
    ts_arr=pd.to_datetime(df["timestamp"])
    mask_from=(ts_arr>=t_from)
    start_i=max(WARMUP_BARS, int(mask_from.idxmax()) if mask_from.any() else WARMUP_BARS)
    end_i=int((ts_arr<=t_to).sum())
    closes=df["close"].values.astype(float)
    highs =df["high"].values.astype(float) if "high" in df.columns else closes
    lows  =df["low"].values.astype(float)  if "low"  in df.columns else closes
    be_t=be_trig if be_trig is not None else BREAKEVEN_TRIGGER
    tr_p=trail_pct if trail_pct is not None else TRAILING_PCT
    in_pos=False; entry_b=0; entry_p=0.0; best_p=0.0
    sl_p=0.0; tp_p=0.0; trail=False
    ec=0.0; ed=0.0; er=50.0; last_ts=0.0
    for i in range(start_i,min(end_i,n)):
        cp=closes[i]; hi=highs[i]; lo=lows[i]
        sig=signals[i] if i<len(signals) else None
        if in_pos:
            if not trail and be_t<90 and cp>=entry_p*(1+be_t/100):
                trail=True; sl_p=entry_p
            if trail and cp>best_p:
                best_p=cp; sl_p=best_p*(1-tr_p/100)
            closed=False; res="END"; xp=cp
            if hi>=tp_p:   res,xp,closed="TP",tp_p,True
            elif lo<=sl_p: res,xp,closed=("TRAIL" if trail else "SL"),sl_p,True
            elif i-entry_b>=MAX_HOLD_BARS: res,xp,closed="END",cp,True
            if closed:
                pnl=(xp*(1-SELL_FEE)-entry_p*(1+BUY_FEE))/(entry_p*(1+BUY_FEE))*100
                trades.append(Trade(symbol,
                    str(df.iloc[entry_b]["timestamp"])[:16],
                    str(df.iloc[i]["timestamp"])[:16],
                    round(entry_p,6),round(xp,6),res,
                    round(pnl,4),round(POSITION_USDT*pnl/100,4),
                    i-entry_b,ec,ed,er))
                in_pos=trail=False
        if not in_pos and sig and sig[0]=="BUY":
            conf=sig[1]
            if conf<min_conf: continue
            avg_dev=float(df.iloc[i].get("avg_deviation",0) or 0)
            deb=30 if abs(avg_dev)>=3.5 else 60 if abs(avg_dev)>=3.0 else 120 if abs(avg_dev)>=2.5 else 300
            cur_ts=df.iloc[i]["timestamp"].timestamp()
            if cur_ts-last_ts<deb: continue
            entry_p=cp; best_p=cp
            tp_p=entry_p*(1+tp_pct/100); sl_p=entry_p*(1-sl_pct/100)
            entry_b=i; in_pos=True; trail=False; last_ts=cur_ts
            ec=conf; ed=avg_dev; er=float(df.iloc[i].get("rsi_14",50) or 50)
    return trades

def calc_stats(sym,trades,tp_pct,sl_pct,min_conf):
    n=len(trades)
    base={"symbol":sym,"n_trades":0,"tp_pct":tp_pct,"sl_pct":sl_pct,
          "min_conf_pct":round(min_conf*100,0),"win_rate":0,
          "total_pnl_pct":0,"total_pnl_usdt":0,"tp":0,"sl":0,"trail":0,"end":0,
          "avg_hold_h":0,"avg_conf":0,"avg_dev":0,"best":0,"worst":0,
          "max_dd":0,"sharpe":0,"profit_factor":0,"score":-999,
          "dev_above":0,"dev_below":0,"rsi_ob":0,"rsi_os":0,"trades":[]}
    if n==0: return base
    pnls=[t.pnl_pct for t in trades]
    wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<=0]
    wr=len(wins)/n; total=sum(pnls); usdt=sum(t.pnl_usdt for t in trades)
    pf=min(sum(wins)/max(abs(sum(losses)),1e-9),99.0) if losses else 99.0
    eq=[0.0]; [eq.append(eq[-1]+p) for p in pnls]
    peak=eq[0]; mdd=0.0
    for v in eq: peak=max(peak,v); mdd=min(mdd,v-peak)
    sh=(np.mean(pnls)/max(np.std(pnls),1e-9)*np.sqrt(min(n,252))) if n>1 else 0
    score=total*wr*(1/(1+abs(mdd)/10))*min(n,200)/200/max(1,n/50)
    return {**base,"n_trades":n,"win_rate":round(wr*100,2),
            "total_pnl_pct":round(total,3),"total_pnl_usdt":round(usdt,2),
            "tp":sum(1 for t in trades if t.result=="TP"),
            "sl":sum(1 for t in trades if t.result=="SL"),
            "trail":sum(1 for t in trades if t.result=="TRAIL"),
            "end":sum(1 for t in trades if t.result=="END"),
            "avg_hold_h":round(np.mean([t.hold_bars/60 for t in trades]),2),
            "avg_conf":round(np.mean([t.conf*100 for t in trades]),1),
            "avg_dev":round(np.mean([t.dev for t in trades]),3),
            "best":round(max(pnls),3),"worst":round(min(pnls),3),
            "max_dd":round(mdd,3),"sharpe":round(sh,3),
            "profit_factor":round(pf,2),"score":round(score,3),
            "dev_above":sum(1 for t in trades if t.dev>0),
            "dev_below":sum(1 for t in trades if t.dev<=0),
            "rsi_ob":sum(1 for t in trades if t.rsi>=70),
            "rsi_os":sum(1 for t in trades if t.rsi<=30),
            "trades":trades}

def print_summary(all_stats,t_from,t_to):
    print("\n"+"="*80)
    print(f"  РЕЗУЛЬТАТЫ  {str(t_from)[:10]} → {str(t_to)[:10]}")
    print("="*80)
    valid=[s for s in all_stats if s["n_trades"]>0]
    if not valid: print("  ❌ Нет сделок"); return
    print(f"\n  Сделок: {sum(s['n_trades'] for s in valid)}  "
          f"PnL: {sum(s['total_pnl_pct'] for s in valid):+.2f}%  "
          f"USDT: {sum(s['total_pnl_usdt'] for s in valid):+.2f}  "
          f"WR avg: {np.mean([s['win_rate'] for s in valid]):.1f}%\n")
    print(f"  {'Пара':12} {'N':>5} {'WR%':>6} {'PnL%':>8} {'USDT':>8} "
          f"{'TP':>4} {'SL':>4} {'Tr':>4} {'End':>4} "
          f"{'Hold_h':>7} {'Sh':>6} {'PF':>6} {'Score':>7} {'Conf':>6} {'Dev':>7}")
    print("  "+"-"*90)
    for s in sorted(valid,key=lambda x:x["score"],reverse=True):
        pc="+" if s["total_pnl_pct"]>=0 else ""
        uc="+" if s["total_pnl_usdt"]>=0 else ""
        print(f"  {s['symbol']:12} {s['n_trades']:>5} "
              f"{s['win_rate']:>5.1f}% {pc}{s['total_pnl_pct']:>7.2f}% "
              f"{uc}{s['total_pnl_usdt']:>7.2f} "
              f"{s['tp']:>4} {s['sl']:>4} {s['trail']:>4} {s['end']:>4} "
              f"{s['avg_hold_h']:>6.1f}ч {s['sharpe']:>6.2f} {s['profit_factor']:>5.2f} "
              f"{s['score']:>7.2f} {s['avg_conf']:>5.0f}% {s['avg_dev']:>+6.2f}%")
    ab=sum(s.get("dev_above",0) for s in valid)
    bw=sum(s.get("dev_below",0) for s in valid)
    ctx=ab+bw
    if ctx>0:
        print(f"\n  Dev>0 (выше SMA): {ab} ({ab/ctx*100:.0f}%) {'⚠️' if ab/ctx>0.4 else '✅'}")
        print(f"  Dev<0 (ниже SMA): {bw} ({bw/ctx*100:.0f}%)")
    no=[s["symbol"] for s in all_stats if s["n_trades"]==0]
    if no: print(f"\n  ⚠️  Нет сделок: {', '.join(no)}")
    print("="*80)

def run_compare(pairs, t_from, t_to):
    """Прогоняет все CONFIGS и выводит сравнительную таблицу."""
    pp = load_pair_params()
    btc_df = load_csv("BTCUSDT")
    print("\n" + "="*70)
    print("  📊 СРАВНЕНИЕ КОНФИГУРАЦИЙ TRAILING")
    print("="*70)
    results = {}
    for cfg_key, cfg in CONFIGS.items():
        print(f"\n  ── {cfg['label']} ──")
        totals = {"n":0,"pnl":0.,"usdt":0.,"wins":0,"tp":0,"sl":0,"trail":0}
        for symbol in pairs:
            payload=load_model(symbol)
            if payload is None: continue
            df=load_csv(symbol)
            if df is None: continue
            df=fill_missing(df)
            df=add_btc(df,btc_df,symbol)
            signals=batch_predict(df,payload)
            tp_pct,sl_pct,min_conf=get_tpslconf(symbol,pp)
            trades=simulate(symbol,df,signals,tp_pct,sl_pct,min_conf,t_from,t_to,
                           be_trig=cfg["be"],trail_pct=cfg["trail"])
            if trades:
                totals["n"]    += len(trades)
                totals["pnl"]  += sum(t.pnl_pct for t in trades)
                totals["usdt"] += sum(t.pnl_usdt for t in trades)
                totals["wins"] += sum(1 for t in trades if t.pnl_pct>0)
                totals["tp"]   += sum(1 for t in trades if t.result=="TP")
                totals["sl"]   += sum(1 for t in trades if t.result=="SL")
                totals["trail"]+= sum(1 for t in trades if t.result=="TRAIL")
        wr = totals["wins"]/totals["n"]*100 if totals["n"] else 0
        results[cfg_key] = {**totals,"wr":wr,"cfg":cfg}
        print(f"     N={totals['n']:>4d}  WR={wr:>5.1f}%  "
              f"PnL={totals['pnl']:>+8.2f}%  USDT={totals['usdt']:>+8.2f}  "
              f"TP={totals['tp']} SL={totals['sl']} Trail={totals['trail']}")
    # Итоговая таблица
    print("\n" + "="*70)
    print(f"  {'Конфигурация':40s}  {'N':>5}  {'WR%':>6}  {'PnL%':>9}  {'USDT':>9}")
    print("  "+"-"*70)
    for k,r in sorted(results.items(), key=lambda x: x[1]["pnl"], reverse=True):
        best = " ← ЛУЧШИЙ" if k==list(sorted(results.keys(),key=lambda x:results[x]["pnl"],reverse=True))[0] else ""
        pc = "+" if r["pnl"]>=0 else ""
        uc = "+" if r["usdt"]>=0 else ""
        print(f"  {r['cfg']['label']:40s}  {r['n']:>5d}  {r['wr']:>5.1f}%  "
              f"{pc}{r['pnl']:>8.2f}%  {uc}{r['usdt']:>8.2f}{best}")
    print("="*70)
    # Сохраняем
    ts=t_from.strftime("%Y-%m-%d"); te=t_to.strftime("%Y-%m-%d")
    lines=[f"СРАВНЕНИЕ TRAILING {ts}→{te}","="*65]
    for k,r in sorted(results.items(),key=lambda x:x[1]["pnl"],reverse=True):
        pc="+" if r["pnl"]>=0 else ""
        lines.append(f"{r['cfg']['label']:40s}  N={r['n']:>4}  WR={r['wr']:>5.1f}%  "
                     f"PnL={pc}{r['pnl']:>+8.2f}%  USDT={r['usdt']:>+8.2f}  "
                     f"TP={r['tp']} SL={r['sl']} Trail={r['trail']}")
    out=BASE_DIR/f"compare_trailing_{ts}_{te}.txt"
    out.write_text("\n".join(lines),encoding="utf-8")
    print(f"\n  📋 {out}")


DAYS_RU = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
DAYS_EN = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]


def analyze_time(all_stats: list[dict], t_from, t_to) -> str:
    """Анализ активности по датам, дням недели, часам."""
    from collections import defaultdict

    # Собираем все сделки
    all_trades = []
    for s in all_stats:
        for t in s.get("trades", []):
            all_trades.append(t)

    if not all_trades:
        return "  ⚠️  Нет сделок для анализа"

    lines = []
    A = lines.append

    # ── Парсим временные метки ──────────────────────────────────────────────
    from datetime import datetime as dt
    parsed = []
    for t in all_trades:
        try:
            ts = dt.strptime(t.t_open[:16], "%Y-%m-%d %H:%M")
            parsed.append((ts, t))
        except Exception:
            pass

    total = len(parsed)
    if total == 0:
        return "  ⚠️  Не удалось распарсить временные метки"

    # ── По датам (активные/неактивные дни) ──────────────────────────────────
    by_date   = defaultdict(list)
    for ts, t in parsed:
        by_date[ts.date()].append(t)

    period_days = (t_to.date() - t_from.date()).days + 1
    active_days = len(by_date)
    inactive    = period_days - active_days
    trades_active = [len(v) for v in by_date.values()]
    pnl_by_date   = {d: sum(t.pnl_pct for t in ts) for d, ts in by_date.items()}

    A("\n" + "═"*62)
    A("  📅 АНАЛИЗ ВРЕМЕННОЙ АКТИВНОСТИ")
    A("═"*62)
    A(f"\n  Период        : {t_from.date()} → {t_to.date()} ({period_days}д)")
    A(f"  Активных дней : {active_days} из {period_days} ({active_days/period_days*100:.0f}%)")
    A(f"  Дней без сделок: {inactive} ({inactive/period_days*100:.0f}%)")
    A(f"  Сделок в активный день: "
      f"avg={sum(trades_active)/len(trades_active):.1f}  "
      f"min={min(trades_active)}  max={max(trades_active)}")

    # Топ-5 лучших и худших дней
    sorted_dates = sorted(pnl_by_date.items(), key=lambda x: x[1], reverse=True)
    A(f"\n  🏆 Топ-5 лучших дней:")
    for d, pnl in sorted_dates[:5]:
        n = len(by_date[d])
        wr = sum(1 for t in by_date[d] if t.pnl_pct>0)/n*100
        pairs_today = set(t.symbol for t in by_date[d])
        A(f"    {d}  {DAYS_RU[d.weekday()]}  N={n:>3}  "
          f"PnL={pnl:>+7.2f}%  WR={wr:.0f}%  "
          f"Пары: {','.join(sorted(pairs_today))}")
    A(f"\n  💀 Топ-5 худших дней:")
    for d, pnl in sorted_dates[-5:]:
        n = len(by_date[d])
        wr = sum(1 for t in by_date[d] if t.pnl_pct>0)/n*100
        pairs_today = set(t.symbol for t in by_date[d])
        A(f"    {d}  {DAYS_RU[d.weekday()]}  N={n:>3}  "
          f"PnL={pnl:>+7.2f}%  WR={wr:.0f}%  "
          f"Пары: {','.join(sorted(pairs_today))}")

    # ── По дням недели ───────────────────────────────────────────────────────
    by_dow   = defaultdict(list)  # 0=Пн..6=Вс
    for ts, t in parsed:
        by_dow[ts.weekday()].append(t)

    A(f"\n  📆 По дням недели:")
    A(f"  {'День':4s} {'N':>5} {'WR%':>6} {'PnL%':>8} {'$/сд':>7} {'Дней':>5}")
    A("  " + "─"*42)
    # сколько каждого дня недели в периоде
    dow_count = defaultdict(int)
    d = t_from.date()
    while d <= t_to.date():
        dow_count[d.weekday()] += 1
        from datetime import timedelta as td2
        d += td2(days=1)

    for dow in range(7):
        ts_list = by_dow[dow]
        if not ts_list:
            A(f"  {DAYS_RU[dow]:4s} {'─':>5}")
            continue
        n   = len(ts_list)
        wr  = sum(1 for t in ts_list if t.pnl_pct>0)/n*100
        pnl = sum(t.pnl_pct for t in ts_list)
        upd = sum(t.pnl_usdt for t in ts_list)
        cnt = dow_count[dow]  # сколько таких дней в периоде
        bar_n = "█" * min(int(n/max(len(all_trades)/70,1)), 20)
        A(f"  {DAYS_RU[dow]:4s} {n:>5d} {wr:>5.1f}% {pnl:>+7.2f}% "
          f"{upd/n:>+6.2f} {cnt:>5d}д  {bar_n}")

    # Лучший/худший день недели
    dow_pnl = {d: sum(t.pnl_usdt for t in ts)/max(dow_count[d],1)
               for d, ts in by_dow.items()}
    best_dow = max(dow_pnl, key=dow_pnl.get)
    worst_dow= min(dow_pnl, key=dow_pnl.get)
    A(f"\n  Лучший день: {DAYS_RU[best_dow]} ({dow_pnl[best_dow]:>+.2f} USDT/нед.день)")
    A(f"  Худший день: {DAYS_RU[worst_dow]} ({dow_pnl[worst_dow]:>+.2f} USDT/нед.день)")

    # ── По часам (UTC) ───────────────────────────────────────────────────────
    by_hour = defaultdict(list)
    for ts, t in parsed:
        by_hour[ts.hour].append(t)

    A(f"\n  🕐 По часам (UTC):")
    A(f"  {'Час':>4} {'N':>4} {'WR%':>5} {'$/сд':>6}  График N")
    A("  " + "─"*48)
    max_n_hour = max(len(v) for v in by_hour.values()) if by_hour else 1
    for h in range(24):
        ts_list = by_hour.get(h, [])
        if not ts_list:
            A(f"  {h:02d}:xx   0")
            continue
        n   = len(ts_list)
        wr  = sum(1 for t in ts_list if t.pnl_pct>0)/n*100
        upd = sum(t.pnl_usdt for t in ts_list)
        bar = "█" * max(1, int(n/max_n_hour*20))
        sign = "+" if upd>=0 else ""
        A(f"  {h:02d}:xx {n:>4d} {wr:>4.0f}% {sign}{upd/n:>+5.2f}  {bar}")

    # Топ-3 часа
    hour_usdt = {h: sum(t.pnl_usdt for t in ts)/len(ts)
                 for h, ts in by_hour.items()}
    top_hours = sorted(hour_usdt.items(), key=lambda x: x[1], reverse=True)[:3]
    A(f"\n  Топ-3 часа по avg$/сделку:")
    for h, u in top_hours:
        A(f"    {h:02d}:00 UTC  {u:>+.3f} USDT/сд  N={len(by_hour[h])}")

    # ── Прогноз ──────────────────────────────────────────────────────────────
    # Ср. сделок в активный день
    avg_trades_active = sum(trades_active)/len(trades_active)
    avg_pnl_usdt_day  = sum(t.pnl_usdt for _, t in parsed) / period_days
    avg_pnl_active    = sum(t.pnl_usdt for _, t in parsed) / active_days

    A(f"\n  📈 ПРОГНОЗ ДЛЯ БОТА:")
    A(f"  Вероятность сделки в день : {active_days/period_days*100:.0f}%")
    A(f"  Сделок в активный день    : {avg_trades_active:.1f}")
    A(f"  PnL USDT за активный день : {avg_pnl_active:>+.2f}")
    A(f"  PnL USDT в среднем за день: {avg_pnl_usdt_day:>+.2f}  (вкл. нулевые дни)")
    A(f"  С поправкой maxPos=3 (÷2) : {avg_pnl_usdt_day/2:>+.2f} USDT/день")
    A("═"*62)

    return "\n".join(lines)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--days",type=int,default=90)
    ap.add_argument("--from",dest="date_from",default=None)
    ap.add_argument("--to",dest="date_to",default=None)
    ap.add_argument("--pair",default=None)
    ap.add_argument("--no-html",action="store_true")
    ap.add_argument("--compare",action="store_true",help="Сравнить 4 конфигурации trailing")
    ap.add_argument("--new",action="store_true",help="Только новые пары SUIUSDT INJUSDT STXUSDT")
    ap.add_argument("--time",action="store_true",help="Анализ активности по датам/дням/часам")
    args=ap.parse_args()
    now=datetime.now()
    t_from=(datetime.strptime(args.date_from,"%Y-%m-%d") if args.date_from
            else datetime(now.year,now.month,now.day)-timedelta(days=args.days-1))
    t_to=(datetime.strptime(args.date_to,"%Y-%m-%d").replace(hour=23,minute=59)
          if args.date_to else now)
    pairs=[args.pair] if args.pair else (NEW_PAIRS if getattr(args,"new",False) else PAIRS)
    print("="*65)
    print("  ⚡ СИМУЛЯТОР — реальные ML модели (batch predict)")
    print("="*65)
    print(f"  Период : {t_from.date()} → {t_to.date()}")
    print(f"  Пары   : {len(pairs)}")
    pp=load_pair_params()
    print(f"  Params : {'pair_params.json loaded' if pp else 'defaults'}")
    # Если --compare — прогоняем все конфигурации
    if getattr(args,"compare",False):
        run_compare(pairs,t_from,t_to)
        return

    btc_df=load_csv("BTCUSDT")
    all_stats=[]; t0=time.time()
    for symbol in pairs:
        print(f"\n{'─'*50}")
        print(f"  {symbol}")
        payload=load_model(symbol)
        if payload is None:
            print("  ❌ модель не найдена")
            all_stats.append(calc_stats(symbol,[],*get_tpslconf(symbol,pp))); continue
        df=load_csv(symbol)
        if df is None:
            print("  ❌ данные не найдены")
            all_stats.append(calc_stats(symbol,[],*get_tpslconf(symbol,pp))); continue
        print(f"  📊 {len(df):,} строк ({(df['timestamp'].max()-df['timestamp'].min()).days}д)")
        tp_pct,sl_pct,min_conf=get_tpslconf(symbol,pp)
        print(f"  TP={tp_pct}% SL={sl_pct}% conf≥{min_conf*100:.0f}%")
        t1=time.time()
        df=fill_missing(df)
        df=add_btc(df,btc_df,symbol)
        print(f"  Фичи: {time.time()-t1:.1f}с",end=" ",flush=True)
        t1=time.time()
        signals=batch_predict(df,payload)
        print(f"predict:{time.time()-t1:.1f}с")
        trades=simulate(symbol,df,signals,tp_pct,sl_pct,min_conf,t_from,t_to)
        stats=calc_stats(symbol,trades,tp_pct,sl_pct,min_conf)
        all_stats.append(stats)
        if stats["n_trades"]>0:
            pc="+" if stats["total_pnl_pct"]>=0 else ""
            print(f"  ✅ N={stats['n_trades']}  WR={stats['win_rate']:.1f}%  "
                  f"PnL={pc}{stats['total_pnl_pct']:.2f}%  "
                  f"Sh={stats['sharpe']:.2f}  TP={stats['tp']} SL={stats['sl']} Trail={stats['trail']}")
        else: print("  ⚠️  нет сделок")
    print_summary(all_stats,t_from,t_to)

    # Временной анализ (--time или всегда)
    if getattr(args,"time",False) or True:  # всегда показываем краткий
        time_report = analyze_time(all_stats,t_from,t_to)
        print(time_report)

    ts=t_from.strftime("%Y-%m-%d"); te=t_to.strftime("%Y-%m-%d")
    lines=[f"СИМУЛЯЦИЯ {ts}→{te} (реальные ML модели + логика бота)","="*65]
    valid=[s for s in all_stats if s["n_trades"]>0]
    for s in sorted(valid,key=lambda x:x["score"],reverse=True):
        lines.append(
            f"{s['symbol']:12} N={s['n_trades']:>4}  WR={s['win_rate']:>5.1f}%  "
            f"PnL={s['total_pnl_pct']:>+7.2f}%  USDT={s['total_pnl_usdt']:>+7.2f}  "
            f"TP={s['tp']} SL={s['sl']} Trail={s['trail']}  "
            f"Hold={s['avg_hold_h']:.1f}ч  Sh={s['sharpe']:.2f}  Sc={s['score']:.2f}  "
            f"devAbove={s.get('dev_above',0)} devBelow={s.get('dev_below',0)}")
    no=[s["symbol"] for s in all_stats if s["n_trades"]==0]
    if no: lines.append(f"НЕТ СДЕЛОК: {', '.join(no)}")
    txt=BASE_DIR/f"simulation_summary_{ts}_{te}.txt"
    all_lines = lines + [time_report]
    txt.write_text("\n".join(all_lines),encoding="utf-8")
    print(f"\n  📋 {txt}")
    print(f"  ⏱️  {time.time()-t0:.0f}с")

if __name__=="__main__":
    main()
