"""Pacote de testes do wedrop-sync (stdlib `unittest`)."""
import os

# Os testes usam canais fictícios (config._DEMO_CHANNELS) contra uma API falsa.
os.environ.setdefault("WEDROP_ALLOW_DEMO_CHANNELS", "1")
