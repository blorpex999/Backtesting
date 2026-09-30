"""Streamlit entry point (``btlab ui``). Local only: bound to 127.0.0.1."""

import streamlit as st

st.set_page_config(page_title="backtest-lab", page_icon="📈", layout="wide")

navigation = st.navigation(
    [
        st.Page("views/home.py", title="Accueil", icon="🏠", default=True),
        st.Page("views/data.py", title="Données", icon="🗄️"),
    ]
)
navigation.run()
