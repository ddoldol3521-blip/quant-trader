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
