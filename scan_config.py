"""Settings come from environment variables (GitHub Actions) or
Streamlit secrets (Streamlit Cloud)."""

import os


def get(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value:
        return value
    try:                                    # only available inside Streamlit
        import streamlit as st
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return default
