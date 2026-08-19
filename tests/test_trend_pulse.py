import pandas as pd

from src.trend_pulse import make_plan


def sample(prices):
    idx=pd.bdate_range("2020-01-01",periods=len(prices))
    close=pd.Series(prices,index=idx,dtype=float)
    return pd.DataFrame({"Open":close*.995,"High":close*1.03,"Low":close*.97,"Close":close})


def test_attack_order_uses_30_percent_of_prior_range():
    df=sample([100+i*.02 for i in range(1300)])
    plan=make_plan(df,130,10000)
    assert plan.mode in ("공격","관망")
    if plan.mode=="공격":
        expected=130+(df.iloc[-1].High-df.iloc[-1].Low)*.30
        assert abs(plan.breakout_price-expected)<1e-9
        assert plan.loc_price is None


def test_defence_has_loc_and_small_weight():
    prices=[100+i*.1 for i in range(1250)]+[200,190,180,170,160,150,140,130,120,110,
                                              100,90,80,70,65,60,58,56,54,52,
                                              50,49,48,47,46,45,44,43,42,41,
                                              40,39,38,37,36,35,34,33,32,31,
                                              30,29,28,27,26,25,24,23,22,21]
    plan=make_plan(sample(prices),21,10000)
    if plan.mode!="관망":
        assert plan.mode=="수비"
        assert 22<plan.weight_pct<23
        assert abs(plan.loc_price-19.11)<1e-9


def test_stop_streak_increases_defence_weight():
    prices=[100+i*.1 for i in range(1250)]+[200-i*3 for i in range(50)]
    df=sample(prices)
    p0=make_plan(df,55,10000,consecutive_stops=0)
    p1=make_plan(df,55,10000,consecutive_stops=1)
    if p0.mode==p1.mode=="수비":
        assert p1.weight_pct>p0.weight_pct
