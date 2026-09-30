import pandas as pd
import streamlit as st

from btlab.periods import PERIOD_LABELS
from btlab.ui.common import load_context, research_only_banner

ctx = load_context()

st.title("backtest-lab")
research_only_banner()

st.markdown(
    "Plateforme locale de backtest par blocs. Jalon 1 : données (téléchargement, contrôle "
    "qualité, couverture) et chargeur verrouillé sur l'IS."
)

st.subheader("Périodes de recherche")
p = ctx.periods.default
rows = []
for name in ("is", "oos", "holdout"):
    start, end = p.bounds(name)
    rows.append(
        {
            "période": PERIOD_LABELS[name],
            "début (UTC)": start.strftime("%Y-%m-%d"),
            "fin exclue (UTC)": end.strftime("%Y-%m-%d") if end is not None else "fin des données",
            "accès": "autorisé" if name == "is" else "verrouillé",
        }
    )
st.table(pd.DataFrame(rows))
st.caption(
    f"Chauffe des indicateurs à partir du {ctx.periods.warmup_start} (jamais de trade avant "
    "l'IS). Le verrou est appliqué par le chargeur de données lui-même : toute lecture qui "
    "touche l'OOS ou le HOLDOUT est refusée et journalisée. La validation OOS / HOLDOUT "
    "arrive au jalon 7."
)
if ctx.periods.overrides:
    st.info("Surcharges par instrument : " + ", ".join(sorted(ctx.periods.overrides)))

st.subheader("Journal des accès aux périodes protégées")
log = ctx.registry.period_access_log(limit=50)
if log:
    df = pd.DataFrame(log)[["ts_utc", "symbol", "period", "status", "reason", "caller"]]
    df.columns = ["date (UTC)", "symbole", "période", "statut", "raison", "appelant"]
    st.dataframe(df, hide_index=True, width="stretch")
else:
    st.write("Aucun accès ni tentative d'accès pour l'instant.")
