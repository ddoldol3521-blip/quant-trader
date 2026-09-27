"""SOXL 트렌드 펄스 주문 계산기.

공개 자료를 바탕으로 독립 재구성한 전략이며 원작의 비공개 공식이 아니다.
오늘 주문에는 오늘 시가와 전일까지 확정된 일봉만 사용한다.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PulsePlan:
    mode: str
    mode_reason: str
    close: float
    peak50: float
    drawdown_pct: float
    ibs: float
    volatility60_pct: float
    risk_percentile: float | None
    weight_pct: float
    k: float | None
    breakout_price: float | None
    breakout_shares: int
    stop_pct: float | None
    stop_price: float | None
    loc_price: float | None
    loc_shares: int
    next_open_sell: bool


CANDIDATE_PRESETS = {
    "신규 수익형": {"cagr": 68.06, "mdd": -33.51, "vol_target": .325, "market_dd_scale": 1.0, "ma20_scale": None},
    "신규 기본형": {"cagr": 66.26, "mdd": -32.73, "vol_target": .30, "market_dd_scale": 1.0, "ma20_scale": None},
    "신규 균형형": {"cagr": 63.49, "mdd": -29.22, "vol_target": .30, "market_dd_scale": .75, "ma20_scale": None},
    "신규 방어형": {"cagr": 61.85, "mdd": -27.31, "vol_target": .30, "market_dd_scale": .50, "ma20_scale": None},
    "신규 초방어형": {"cagr": 58.09, "mdd": -24.86, "vol_target": .30, "market_dd_scale": .25, "ma20_scale": .75},
}


def _clean(data: pd.DataFrame) -> pd.DataFrame:
    rename={c:str(c).lower().replace(" ","_") for c in data.columns}
    x=data.rename(columns=rename).copy()
    need=("open","high","low","close")
    missing=[c for c in need if c not in x]
    if missing:
        raise ValueError(f"필요한 시세 열이 없습니다: {', '.join(missing)}")
    x=x[list(need)].apply(pd.to_numeric,errors="coerce").dropna()
    x=x[(x.open>0)&(x.high>0)&(x.low>0)&(x.close>0)].sort_index()
    if len(x)<1261:
        raise ValueError("모드 계산에는 최소 1,261거래일(약 5년)의 일봉이 필요합니다.")
    return x


def _base_mode(x: pd.DataFrame) -> np.ndarray:
    previous_close=x.close.shift(1)
    peak50=x.high.rolling(50).max().shift(1)
    return np.where(previous_close>=peak50*.70,2,1)  # 2 공격, 1 수비


def _features(x: pd.DataFrame) -> tuple[pd.Series,pd.Series]:
    ret=x.close.pct_change()
    vol60=ret.rolling(60).std().shift(1)
    ibs=((x.close-x.low)/(x.high-x.low).replace(0,np.nan)).shift(1)
    return vol60,ibs


def _trade_labels(x: pd.DataFrame, modes: np.ndarray) -> tuple[np.ndarray,np.ndarray]:
    previous_range=(x.high-x.low).shift(1).to_numpy()
    k=np.where(modes==2,.30,.20)
    trigger=x.open.to_numpy()+previous_range*k
    entered=x.high.to_numpy()>=trigger
    outcome=x.open.shift(-1).to_numpy()/trigger-1
    outcome[~entered]=np.nan
    return entered,outcome


def _risk_percentile(x: pd.DataFrame) -> tuple[float | None,bool]:
    """최근 1,260일의 변동성60+IBS 구간 기대값 하위 10% 여부."""
    modes=_base_mode(x)
    entered,outcome=_trade_labels(x,modes)
    vol,ibs=_features(x)
    end=len(x)-1
    start=max(0,end-1260)
    train=np.arange(len(x))
    ok=(train>=start)&(train<end)&entered&np.isfinite(outcome)&vol.notna().to_numpy()&ibs.notna().to_numpy()
    if ok.sum()<200 or not np.isfinite(vol.iloc[end]) or not np.isfinite(ibs.iloc[end]):
        return None,False

    vcuts=np.unique(np.nanquantile(vol.to_numpy()[ok],np.linspace(0,1,9)))
    icuts=np.unique(np.nanquantile(ibs.to_numpy()[ok],np.linspace(0,1,9)))
    if len(vcuts)<3 or len(icuts)<3:
        return None,False
    vcuts[0],vcuts[-1]=-np.inf,np.inf
    icuts[0],icuts[-1]=-np.inf,np.inf
    vb=np.digitize(vol.to_numpy(),vcuts[1:-1])
    ib=np.digitize(ibs.to_numpy(),icuts[1:-1])
    key=vb*8+ib
    global_mean=float(np.nanmean(outcome[ok]))
    table={}
    for cell in np.unique(key[ok]):
        vals=outcome[ok&(key==cell)]
        table[int(cell)]=float((np.nansum(vals)+20*global_mean)/(len(vals)+20))
    historical=np.array([table.get(int(cell),global_mean) for cell in key[ok]])
    current=table.get(int(key[end]),global_mean)
    percentile=float((historical<=current).mean()*100)
    return percentile,percentile<=10.0


def make_plan(data: pd.DataFrame,today_open: float,capital: float,
              consecutive_stops: int=0,whole_shares: bool=True) -> PulsePlan:
    """오늘 시가가 확정된 뒤 주문표를 만든다."""
    x=_clean(data)
    if today_open<=0 or capital<=0:
        raise ValueError("오늘 시가와 전략자금은 0보다 커야 합니다.")

    last=x.iloc[-1]
    peak50=float(x.high.tail(50).max())
    dd=float(last.close/peak50-1)
    base="공격" if dd>=-.30 else "수비"

    # 오늘 주문의 특징값은 마지막 확정 봉 자체다. _risk_percentile은 행 i가
    # 오늘이라고 가정하므로 오늘 시가만 붙인 빈 행을 하나 추가한다.
    today=pd.DataFrame({"open":[today_open],"high":[today_open],"low":[today_open],"close":[today_open]},
                       index=[x.index[-1]+pd.Timedelta(days=1)])
    calc=pd.concat([x,today])
    percentile,observe=_risk_percentile(calc)
    vol60=float(x.close.pct_change().tail(60).std()*100)
    day_range=float(last.high-last.low)
    ibs=float((last.close-last.low)/(last.high-last.low)) if last.high>last.low else .5

    if observe:
        return PulsePlan("관망","비슷한 변동성·종가 위치의 과거 성과가 하위 10%",float(last.close),
                         peak50,dd*100,ibs,vol60,percentile,0,None,None,0,None,None,None,0,False)

    if base=="공격":
        weight=.9071; k=.30; stop_pct=.09; loc=None
        reason="전일 종가가 최근 50일 고점의 70% 이상"
    else:
        nominal=(.25,.35,.45)[min(max(int(consecutive_stops),0),2)]
        weight=nominal*.9071; k=.20; stop_pct=.06; loc=today_open*.91
        reason="전일 종가가 최근 50일 고점보다 30% 넘게 하락"

    trigger=today_open+day_range*k
    stop=trigger*(1-stop_pct)
    def shares(amount,price):
        raw=amount/price
        return int(np.floor(raw)) if whole_shares else int(np.floor(raw))
    amount=capital*weight
    return PulsePlan(base,reason,float(last.close),peak50,dd*100,ibs,vol60,percentile,
                     weight*100,k,trigger,shares(amount,trigger),stop_pct*100,stop,
                     loc,shares(amount,loc) if loc else 0,True)


def _candidate_features(x: pd.DataFrame) -> pd.DataFrame:
    ibs=((x.close-x.low)/(x.high-x.low).replace(0,np.nan)).shift(1)
    low10=x.low.rolling(10).min().shift(1)
    rebound=x.close.shift(1)/low10-1
    return pd.DataFrame({"ibs":ibs,"rebound10":rebound},index=x.index)


def _candidate_map(x: pd.DataFrame, cutoff: pd.Timestamp):
    f=_candidate_features(x);train=x.index<=cutoff
    _,ei=pd.qcut(f.loc[train,"ibs"].dropna(),3,retbins=True,duplicates="drop")
    _,er=pd.qcut(f.loc[train,"rebound10"].dropna(),3,retbins=True,duplicates="drop")
    ei[0]=er[0]=-np.inf;ei[-1]=er[-1]=np.inf
    cell=pd.cut(f.ibs,ei,labels=False,include_lowest=True)*3+pd.cut(f.rebound10,er,labels=False,include_lowest=True)
    trigger=x.open*1.0075;entered=x.high>=trigger
    outcome=x.open.shift(-1)*(1-.0012)/(trigger*(1+.0012))-1
    ok=train&entered&outcome.notna()&cell.notna()
    g=pd.DataFrame({"cell":cell[ok],"r":outcome[ok]}).groupby("cell").r.agg(["mean","count"])
    overall=float(outcome[ok].mean());g["score"]=(g["mean"]*g["count"]+overall*25)/(g["count"]+25)
    order=list(g.score.sort_values(ascending=False).index)
    return cell,set(order[:max(1,round(len(order)*.4))]),set(order[-max(1,round(len(order)*.2)):]),outcome,entered


def _walkforward_returns(x: pd.DataFrame) -> pd.Series:
    pieces=[]
    for year in range(max(2014,int(x.index[0].year)+1),int(x.index[-1].year)+1):
        cell,attack,watch,outcome,entered=_candidate_map(x,pd.Timestamp(f"{year-1}-12-31"))
        weight=pd.Series(.35,index=x.index);weight[cell.isin(attack)]=1.;weight[cell.isin(watch)]=0
        daily=(weight*outcome).where(entered,0).fillna(0)
        pieces.append(daily.loc[f"{year}-01-01":f"{year}-12-31"])
    return pd.concat(pieces).sort_index() if pieces else pd.Series(dtype=float)


def _next_risk_scale(daily: pd.Series,vol_target: float) -> float:
    equity=1.;curve=[];realized=[]
    for value in daily.fillna(0).to_numpy():
        curve.append(equity);scale=1.;peak=max(curve)
        if equity/peak-1<=-.15:scale*=.75
        if len(realized)>=20:
            vol=float(np.std(realized[-20:],ddof=1)*np.sqrt(252))
            if vol>0:scale*=min(1.,vol_target/vol)
        ret=scale*float(value);realized.append(ret);equity*=1+ret
    scale=1.;peak=max(curve+[equity])
    if equity/peak-1<=-.15:scale*=.75
    if len(realized)>=20:
        vol=float(np.std(realized[-20:],ddof=1)*np.sqrt(252))
        if vol>0:scale*=min(1.,vol_target/vol)
    return scale


def make_candidate_plan(data: pd.DataFrame,today_open: float,capital: float,
                        preset_name: str="신규 균형형",whole_shares: bool=True) -> PulsePlan:
    """검증된 IBS+10일 반등 후보의 오늘 주문표."""
    x=_clean(data);preset=CANDIDATE_PRESETS.get(preset_name,CANDIDATE_PRESETS["신규 균형형"])
    if today_open<=0 or capital<=0:raise ValueError("오늘 시가와 전략자금은 0보다 커야 합니다.")
    last=x.iloc[-1];cutoff=pd.Timestamp(f"{int(x.index[-1].year)-1}-12-31")
    today=pd.DataFrame({"open":[today_open],"high":[today_open],"low":[today_open],"close":[today_open]},index=[x.index[-1]+pd.Timedelta(days=1)])
    calc=pd.concat([x,today]);cell,attack,watch,_,_=_candidate_map(calc,cutoff);current=cell.iloc[-1]
    if current in attack:mode="공격";base_weight=1.;reason="전일 IBS와 10일 저점 반등 조합이 과거 상위 상태"
    elif current in watch:mode="관망";base_weight=0.;reason="전일 IBS와 10일 저점 반등 조합이 과거 하위 상태"
    else:mode="수비";base_weight=.35;reason="전일 IBS와 10일 저점 반등 조합이 중간 상태"
    risk_scale=_next_risk_scale(_walkforward_returns(x),preset["vol_target"])
    market_scale=1.;peak20=float(x.high.tail(20).max());market_dd=float(last.close/peak20-1)
    if market_dd<=-.20:market_scale=float(preset["market_dd_scale"])
    ma20=float(x.close.tail(20).mean())
    if preset["ma20_scale"] is not None and last.close<ma20:market_scale=float(preset["ma20_scale"])
    weight=base_weight*risk_scale*market_scale;trigger=today_open*1.0075
    shares=int(np.floor(capital*weight/trigger))
    ibs=float((last.close-last.low)/(last.high-last.low)) if last.high>last.low else .5
    rebound=float(last.close/x.low.tail(10).min()-1)
    detail=f"{reason} · 위험축소 {risk_scale*100:.0f}% · 시장축소 {market_scale*100:.0f}%"
    return PulsePlan(mode,detail,float(last.close),peak20,market_dd*100,ibs,
                     float(x.close.pct_change().tail(60).std()*100),rebound*100,weight*100,
                     None,trigger,shares,None,None,None,0,True)
