"""Helpers shared by the Streamlit views."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from btlab.config import ConfigError
from btlab.context import DataContext
from btlab.paths import Paths
from btlab.ui.jobs import JobManager


def load_context() -> DataContext:
    try:
        return DataContext.load()
    except ConfigError as err:
        st.error(f"Configuration invalide :\n\n```\n{err}\n```")
        st.stop()


@st.cache_resource
def job_manager(root: str) -> JobManager:
    paths = Paths.from_root(Path(root))
    return JobManager(paths.root, paths.logs / "jobs")


def research_only_banner() -> None:
    st.caption(
        "Outil de recherche uniquement : aucun ordre réel n'est jamais passé. "
        "Aucun résultat n'est présenté sans les coûts."
    )
