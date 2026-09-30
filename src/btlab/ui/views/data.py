from datetime import date

import pandas as pd
import streamlit as st

from btlab.config import ConfigError, parse_model
from btlab.data import service
from btlab.data.download import DownloadError
from btlab.data.instruments import Instrument, save_instrument
from btlab.data.quality import (
    ANOMALY_LABELS,
    EXCLUDED_STATUSES,
    STATUS_LABELS,
    load_anomalies,
    load_days,
    load_heatmap,
    load_summary,
    quality_state,
)
from btlab.report.charts import coverage_heatmap, status_by_year
from btlab.ui.common import job_manager, load_context, research_only_banner

ctx = load_context()
jobs = job_manager(str(ctx.paths.root))

st.title("Données")
research_only_banner()
st.warning(
    "Les prix Dukascopy peuvent différer légèrement de ceux de FTMO, surtout sur les indices "
    "CFD (sous-jacent, horaires, spread). Les horaires de séance marqués « provisoires » sont "
    "des hypothèses à remplacer par les caractéristiques des symboles dans MT5."
)

# --- download tool ------------------------------------------------------------------
source = service.make_source(ctx)
with st.expander("Outil de téléchargement (dukascopy-node)", expanded=not source.is_installed()):
    node_ok = True
    try:
        st.write(f"Node.js : {source.node_version()}")
    except DownloadError as err:
        node_ok = False
        st.error(str(err))
    if source.is_installed():
        st.success(f"dukascopy-node {source.version} installé dans {source.package_dir}")
    else:
        st.info(f"dukascopy-node {source.version} n'est pas encore installé.")
        if node_ok and st.button("Installer dukascopy-node", disabled=jobs.any_running()):
            jobs.start("Installation de dukascopy-node", ["data", "setup"])
            st.rerun()

# --- coverage -----------------------------------------------------------------------
st.subheader("Couverture par instrument")
st.dataframe(service.coverage_table(ctx), hide_index=True, width="stretch")

# --- actions ------------------------------------------------------------------------
st.subheader("Téléchargement et contrôle qualité")
selected = st.multiselect(
    "Instruments", sorted(ctx.instruments), default=service.downloaded_symbols(ctx)
)
busy = jobs.any_running()
c1, c2, c3 = st.columns(3)
if c1.button("Télécharger / mettre à jour", disabled=not selected or busy, type="primary"):
    jobs.start(f"Téléchargement {', '.join(selected)}", ["data", "download", *selected])
    st.rerun()
if c2.button("Contrôle qualité", disabled=not selected or busy):
    jobs.start(f"Contrôle qualité {', '.join(selected)}", ["data", "qc", *selected])
    st.rerun()
if c3.button("Rafraîchir"):
    st.rerun()
st.caption(
    "Téléchargement incrémental : seuls les mois manquants ou incomplets sont récupérés ; "
    "une interruption reprend là où elle s'est arrêtée. Le contrôle qualité est relancé "
    "automatiquement après un téléchargement. Les journaux sont dans data/logs/jobs/."
)


@st.fragment(run_every=2 if busy else None)
def show_jobs() -> None:
    for job in jobs.jobs()[:5]:
        with st.container(border=True):
            st.markdown(f"**{job.label}** — {job.status} (lancé à {job.started_at:%H:%M:%S})")
            st.code(job.tail() or "…", language=None)


show_jobs()

# --- quality details ----------------------------------------------------------------
st.subheader("Détail du contrôle qualité")
available = [s for s in sorted(ctx.instruments) if load_summary(ctx.paths.quality, s)]
if not available:
    st.info("Aucun contrôle qualité disponible pour l'instant.")
else:
    symbol = st.selectbox("Instrument contrôlé", available)
    inst = ctx.instrument(symbol)
    summary = load_summary(ctx.paths.quality, symbol)
    if quality_state(ctx.paths.quality, ctx.store, symbol) == "stale":
        st.warning("Les données ont changé depuis ce contrôle : relancez le contrôle qualité.")
    counts = summary["status_counts"]
    m = st.columns(5)
    m[0].metric("Jours contrôlés", summary["days_total"])
    m[1].metric("Valides", counts["valid"])
    m[2].metric("Invalides", counts["invalid"])
    m[3].metric("Exclus (liquidité)", counts["excluded_low_liquidity"])
    m[4].metric("Fériés (sous-jacent)", counts["holiday"])
    st.caption(
        f"Contrôle du {summary['generated_at']} · données du {summary['first_ts'][:10]} au "
        f"{summary['last_ts'][:16]} · jours en heure de {summary['settings']['day_timezone']}"
    )
    if not summary["sessions_verified"]:
        st.warning("Horaires de séance provisoires (hypothèse à remplacer).")

    tab_year, tab_excl, tab_anom, tab_heat, tab_report = st.tabs(
        ["Par année", "Jours exclus", "Anomalies", "Carte de couverture", "Rapport HTML"]
    )
    with tab_year:
        st.plotly_chart(status_by_year(summary["per_year"]), width="stretch")
        st.dataframe(pd.DataFrame(summary["per_year"]), hide_index=True, width="stretch")
    with tab_excl:
        days = load_days(ctx.paths.quality, symbol)
        excl = days[days["status"].isin(EXCLUDED_STATUSES)]
        st.write(f"{len(excl)} jour(s) exclu(s) par le chargeur.")
        st.dataframe(
            pd.DataFrame(
                {
                    "jour": excl["day"].astype(str),
                    "statut": excl["status"].map(STATUS_LABELS),
                    "raisons": excl["reasons"],
                    "couverture": excl["coverage"],
                    "trou max (min)": excl["max_gap"],
                }
            ),
            hide_index=True,
            width="stretch",
            column_config={"couverture": st.column_config.NumberColumn(format="percent")},
        )
    with tab_anom:
        anomalies = load_anomalies(ctx.paths.quality, symbol)
        if anomalies.empty:
            st.write("Aucune anomalie.")
        else:
            kinds = st.multiselect(
                "Types",
                sorted(anomalies["kind"].unique()),
                format_func=lambda k: ANOMALY_LABELS.get(k, k),
            )
            view = anomalies[anomalies["kind"].isin(kinds)] if kinds else anomalies
            st.dataframe(
                pd.DataFrame(
                    {
                        "début (UTC)": view["start_utc"],
                        "fin (UTC)": view["end_utc"],
                        "type": view["kind"].map(lambda k: ANOMALY_LABELS.get(k, k)),
                        "minutes": view["minutes"],
                        "détail": view["detail"],
                    }
                ),
                hide_index=True,
                width="stretch",
            )
    with tab_heat:
        heat = load_heatmap(ctx.paths.quality, symbol)
        tz = inst.sessions.timezone
        left, right = st.columns(2)
        left.plotly_chart(coverage_heatmap(heat, "observed_share", "Observé", tz), width="stretch")
        right.plotly_chart(
            coverage_heatmap(heat, "expected_share", "Attendu (configuration)", tz), width="stretch"
        )
    with tab_report:
        report = ctx.paths.qc_reports / f"{symbol}.html"
        if report.is_file():
            st.write(f"Rapport autonome : `{report}`")
            st.download_button(
                "Télécharger le rapport HTML",
                data=report.read_bytes(),
                file_name=report.name,
                mime="text/html",
            )
        else:
            st.write("Pas de rapport HTML (relancez le contrôle qualité).")

# --- add an instrument --------------------------------------------------------------
st.subheader("Ajouter un instrument")
with st.expander("Nouvel instrument"):
    if source.is_installed():
        lookup = st.text_input("Chercher un identifiant dukascopy-node (ex. xagusd)")
        if lookup:
            try:
                entry = source.catalog().get(lookup.strip().lower())
            except DownloadError as err:
                entry = None
                st.error(str(err))
            if entry:
                st.write(
                    f"{entry['name']} — {entry.get('description', '')} · 1re bougie M1 : "
                    f"{entry['startDayForMinuteCandles'][:10]}"
                )
            else:
                st.write("Identifiant introuvable dans le catalogue dukascopy-node.")
    with st.form("new_instrument"):
        a, b = st.columns(2)
        symbol_in = a.text_input("Symbole (ex. XAGUSD)")
        description = b.text_input("Description")
        asset_class = a.selectbox("Classe d'actif", ["forex", "index", "metal", "energy"])
        template = b.selectbox("Copier les horaires de séance de", sorted(ctx.instruments))
        dk_id = a.text_input("Identifiant dukascopy-node (ex. xagusd)")
        dk_name = b.text_input("Nom Dukascopy (ex. XAG/USD)")
        first_m1 = a.date_input(
            "1re bougie M1 annoncée",
            value=date(2010, 1, 1),
            min_value=date(1990, 1, 1),
            max_value=date.today(),
        )
        quote = b.text_input("Devise de cotation", value="USD")
        base = a.text_input("Devise de base (vide pour un indice)")
        unit = b.selectbox("Unité", ["pip", "point"])
        pip_size = a.number_input(
            "Taille du pip / point", min_value=0.0, value=0.0001, format="%.6f", step=0.0001
        )
        decimals = b.number_input("Décimales des prix", min_value=0, max_value=8, value=5)
        submitted = st.form_submit_button("Enregistrer l'instrument")
    if submitted:
        sessions = ctx.instrument(template).sessions.model_dump(mode="json")
        sessions["verified"] = False
        raw = {
            "symbol": symbol_in.strip().upper(),
            "description": description.strip() or symbol_in.strip().upper(),
            "asset_class": asset_class,
            "quote_currency": quote.strip().upper(),
            "base_currency": base.strip().upper() or None,
            "unit_name": unit,
            "pip_size": pip_size,
            "price_decimals": int(decimals),
            "dukascopy": {
                "instrument_id": dk_id.strip().lower(),
                "name": dk_name.strip(),
                "first_m1": first_m1.isoformat(),
            },
            "sessions": sessions,
            "notes": [f"Horaires copiés de {template} : à vérifier."],
        }
        try:
            inst_new = parse_model(Instrument, raw, "nouvel instrument")
            path = save_instrument(ctx.paths.instruments_dir, inst_new)
        except ConfigError as err:
            st.error(str(err))
        else:
            st.success(f"Instrument enregistré : {path}. Pensez à le versionner avec git.")
