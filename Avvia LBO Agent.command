#!/bin/zsh
# Doppio clic per aprire LBO Agent. Questa finestra del Terminale deve restare aperta
# mentre usi l'app (puoi ridurla a icona); chiudendola si chiude anche l'app.
cd "$(dirname "$0")"
source "$HOME/.local/bin/env" 2>/dev/null
echo "Avvio LBO Agent… (la prima volta può richiedere qualche secondo)"
uv run python app.py
