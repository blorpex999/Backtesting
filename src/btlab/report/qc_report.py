"""Self-contained HTML quality report (one file per instrument, in French)."""

from __future__ import annotations

import html
from pathlib import Path

import pandas as pd

from btlab.data.instruments import Instrument
from btlab.data.quality import ANOMALY_LABELS, EXCLUDED_STATUSES, STATUS_LABELS, QualityResult
from btlab.report.charts import coverage_heatmap, status_by_year

_CSS = """
:root { color-scheme: light; }
body { font-family: system-ui, -apple-system, "Segoe UI", sans-serif; background: #f9f9f7;
       color: #0b0b0b; margin: 0; }
main { max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 32px 0 8px; border-bottom: 1px solid #e1e0d9; padding-bottom: 4px; }
p, li { line-height: 1.5; }
.meta { color: #52514e; font-size: 14px; }
.warn { background: #fff6e0; border-left: 4px solid #fab219; padding: 8px 12px; margin: 8px 0; }
.ok { background: #eaf6ea; border-left: 4px solid #0ca30c; padding: 8px 12px; margin: 8px 0; }
table { border-collapse: collapse; font-size: 13px; margin: 8px 0; width: 100%;
        font-variant-numeric: tabular-nums; }
th, td { border-bottom: 1px solid #e1e0d9; padding: 4px 8px; text-align: left;
         vertical-align: top; }
th { background: #f0efec; }
.scroll { max-height: 480px; overflow: auto; }
.card { background: #fcfcfb; border: 1px solid rgba(11,11,11,0.10); border-radius: 8px;
        padding: 8px; margin: 12px 0; }
"""


def _table(df: pd.DataFrame, scroll: bool = False) -> str:
    table = df.to_html(index=False, escape=True, border=0, na_rep="—")
    return f'<div class="scroll">{table}</div>' if scroll else table


def _pct(x) -> str:
    return "—" if x is None or pd.isna(x) else f"{x:.1%}"


def render_qc_report(result: QualityResult, inst: Instrument, source: str) -> str:
    s = result.summary
    esc = html.escape
    rows = f"{s['rows']:,}".replace(",", "\u202f")
    parts = [
        "<!doctype html><html lang='fr'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>Contrôle qualité {esc(inst.symbol)}</title>",
        f"<style>{_CSS}</style></head><body><main>",
        f"<h1>Contrôle qualité — {esc(inst.symbol)}</h1>",
        f"<p class='meta'>{esc(inst.description)} · Dukascopy {esc(inst.dukascopy.name)} · "
        f"source {esc(source)} · contrôle du {esc(s['generated_at'])} · "
        f"btlab {esc(s['btlab_version'])}</p>",
        f"<p class='meta'>Données du {esc(s['first_ts'])} au {esc(s['last_ts'])} "
        f"({rows} minutes stockées). Jours définis en heure de "
        f"{esc(s['settings']['day_timezone'])} ; séances en heure de "
        f"{esc(s['session_timezone'])}.</p>",
        "<div class='ok'>Aucune donnée n'est réparée. Les jours invalides, les jours de "
        "faible liquidité et les jours incomplets sont exclus par le chargeur, avec leur "
        "raison. Seule intervention, faite à la construction et comptée ici : les doublons "
        "strictement identiques sont retirés ; les doublons contradictoires sont retirés et "
        "invalident leur jour.</div>",
    ]
    if not s["sessions_verified"]:
        parts.append(
            "<div class='warn'><b>Hypothèse à remplacer :</b> les horaires de séance de cet "
            "instrument sont provisoires. Comparez la carte de couverture observée à la carte "
            "attendue ci-dessous, et aux caractéristiques du symbole dans MT5 (FTMO).</div>"
        )
    for note in inst.notes:
        parts.append(f"<div class='warn'>{esc(note)}</div>")

    counts = pd.DataFrame(
        [{"statut": STATUS_LABELS[k], "jours": v} for k, v in s["status_counts"].items() if v]
    )
    parts += ["<h2>Résumé</h2>", _table(counts)]

    per_year = pd.DataFrame(s["per_year"])
    if not per_year.empty:
        view = pd.DataFrame(
            {
                "année": per_year["year"],
                "jours": per_year["days"],
                **{STATUS_LABELS[k]: per_year[k] for k in STATUS_LABELS if k in per_year},
                "couverture médiane": per_year["median_coverage"].map(_pct),
                f"spread médian ({inst.unit_name}s)": per_year["median_spread"].map(
                    lambda v: "—" if v is None or pd.isna(v) else f"{v:.2f}"
                ),
            }
        )
        parts += [
            "<h2>Par année</h2>",
            "<div class='card'>",
            status_by_year(s["per_year"]).to_html(full_html=False, include_plotlyjs=True),
            "</div>",
            _table(view),
        ]

    excl = result.days[result.days["status"].isin(EXCLUDED_STATUSES)]
    parts.append(f"<h2>Jours exclus ({len(excl)})</h2>")
    if len(excl):
        parts.append(
            _table(
                pd.DataFrame(
                    {
                        "jour": excl["day"].astype(str),
                        "statut": excl["status"].map(STATUS_LABELS),
                        "raisons": excl["reasons"],
                        "couverture": excl["coverage"].map(_pct),
                        "trou max (min)": excl["max_gap"],
                    }
                ),
                scroll=True,
            )
        )
    else:
        parts.append("<p>Aucun jour exclu.</p>")

    parts.append("<h2>Anomalies</h2>")
    if s["anomaly_counts"]:
        parts.append(
            _table(
                pd.DataFrame(
                    [
                        {
                            "type": ANOMALY_LABELS.get(k, k),
                            "occurrences": v,
                            "minutes": s["anomaly_minutes"].get(k, 0),
                        }
                        for k, v in s["anomaly_counts"].items()
                    ]
                )
            )
        )
        top = result.anomalies.head(500)
        parts.append(f"<p class='meta'>Détail (500 premières sur {len(result.anomalies)}) :</p>")
        parts.append(
            _table(
                pd.DataFrame(
                    {
                        "début (UTC)": pd.DatetimeIndex(top["start_utc"]).strftime(
                            "%Y-%m-%d %H:%M"
                        ),
                        "fin (UTC)": pd.DatetimeIndex(top["end_utc"]).strftime("%Y-%m-%d %H:%M"),
                        "type": top["kind"].map(lambda k: ANOMALY_LABELS.get(k, k)),
                        "minutes": top["minutes"],
                        "détail": top["detail"],
                    }
                ),
                scroll=True,
            )
        )
    else:
        parts.append("<p>Aucune anomalie.</p>")

    tz = inst.sessions.timezone
    parts += [
        "<h2>Carte de couverture (heure de la semaine)</h2>",
        "<p class='meta'>Part des semaines où l'instrument cote à chaque heure, en heure locale "
        "du marché de référence. Comparer « observé » et « attendu » vérifie les horaires "
        "configurés.</p>",
        "<div class='card'>",
        coverage_heatmap(result.heatmap, "observed_share", "Observé", tz).to_html(
            full_html=False, include_plotlyjs=False
        ),
        "</div><div class='card'>",
        coverage_heatmap(result.heatmap, "expected_share", "Attendu (configuration)", tz).to_html(
            full_html=False, include_plotlyjs=False
        ),
        "</div>",
        "<h2>Paramètres du contrôle</h2>",
        _table(
            pd.DataFrame([{"paramètre": k, "valeur": str(v)} for k, v in s["settings"].items()])
        ),
        f"<p class='meta'>Empreinte des données : {esc(s['data_fingerprint'])}</p>",
        "</main></body></html>",
    ]
    return "\n".join(parts)


def write_qc_report(result: QualityResult, inst: Instrument, source: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{inst.symbol}.html"
    path.write_text(render_qc_report(result, inst, source), encoding="utf-8")
    return path
