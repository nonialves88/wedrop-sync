"""Cofre cifrado (AES-GCM + scrypt) e carregamento de canais fora do código."""
import json
import tempfile
import unittest
from pathlib import Path

from wedrop_sync import config, vault

KEY = "uma-senha-mestra-bem-longa-123456"


class VaultTest(unittest.TestCase):
    def test_ida_e_volta(self):
        texto = vault.encrypt_bytes(b'{"id": 123}', KEY)
        self.assertTrue(texto.startswith("WDV1."))
        self.assertNotIn("123", texto)
        self.assertEqual(vault.decrypt_bytes(texto, KEY), b'{"id": 123}')

    def test_cada_cifra_e_diferente(self):
        self.assertNotEqual(vault.encrypt_bytes(b"x", KEY), vault.encrypt_bytes(b"x", KEY))

    def test_senha_errada_falha_sem_vazar(self):
        texto = vault.encrypt_bytes(b"segredo", KEY)
        with self.assertRaises(vault.VaultError) as ctx:
            vault.decrypt_bytes(texto, "outra-senha-bem-longa-999999")
        self.assertNotIn("segredo", str(ctx.exception))

    def test_adulteracao_e_detectada(self):
        partes = vault.encrypt_bytes(b"dados", KEY).strip().split(".", 4)
        adulterado = ".".join(partes[:4] + [partes[4][:-6] + "AAAA=="])
        with self.assertRaises(vault.VaultError):
            vault.decrypt_bytes(adulterado, KEY)

    def test_cabecalho_e_autenticado(self):
        partes = vault.encrypt_bytes(b"dados", KEY).strip().split(".", 4)
        partes[2] = "9"  # troca o parâmetro r do scrypt
        with self.assertRaises(vault.VaultError):
            vault.decrypt_bytes(".".join(partes), KEY)

    def test_senha_fraca_ou_ausente_recusada(self):
        for ruim in (None, "", "curta"):
            with self.assertRaises(vault.VaultError):
                vault.encrypt_bytes(b"x", ruim)

    def test_arquivos(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, enc, out = Path(tmp, "a.json"), Path(tmp, "a.enc"), Path(tmp, "b.json")
            src.write_text('{"ok": true}', encoding="utf-8")
            vault.encrypt_file(src, enc, KEY)
            vault.decrypt_file(enc, out, KEY)
            self.assertEqual(out.read_text(encoding="utf-8"), '{"ok": true}')
            self.assertEqual(out.stat().st_mode & 0o077, 0)  # só o dono lê


class ChannelsTest(unittest.TestCase):
    def write(self, tmp, data):
        path = Path(tmp, "channels.json")
        path.write_text(json.dumps(data), encoding="utf-8")
        return {"WEDROP_CHANNELS_FILE": str(path)}

    def test_carrega_arquivo_e_normaliza_chaves(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self.write(tmp, {"channels": [
                {"key": "shopee", "name": "S", "id": "7", "marketplace": "shopee"},
                {"key": "ml-1", "name": "M", "id": 8, "marketplace": "mercadolivre"},
                {"key": "bling", "name": "B", "id": 9, "active": False},
            ]})
            channels, order, ignored, demo = config.load_channels(env)
        self.assertFalse(demo)
        self.assertEqual(channels["ml1"].integration_id, 8)
        self.assertEqual(order, ("shopee", "ml1"))
        self.assertEqual(ignored, ("bling",))

    def test_sem_arquivo_cai_na_demo_e_trava(self):
        with tempfile.TemporaryDirectory() as tmp:
            channels, _, _, demo = config.load_channels({"WEDROP_HOME": tmp})
        self.assertTrue(demo)
        self.assertTrue(all(c.integration_id >= 10000 for c in channels.values()))

    def test_arquivo_apontado_mas_inexistente_e_erro(self):
        with self.assertRaises(config.ConfigError):
            config.load_channels({"WEDROP_CHANNELS_FILE": "/nao/existe.json"})

    def test_json_invalido(self):
        with tempfile.TemporaryDirectory() as tmp:
            for ruim in ({"channels": [{"key": "a"}]}, {"channels": [{"key": "a", "id": "x"}]}, {"channels": []}):
                with self.assertRaises(config.ConfigError):
                    config.load_channels(self.write(tmp, ruim))

    def test_trava_contra_demo(self):
        original = config.USING_DEMO_CHANNELS
        config.USING_DEMO_CHANNELS = True
        try:
            with self.assertRaises(config.ConfigError):
                config.require_real_channels({})
            config.require_real_channels({config.ALLOW_DEMO_ENV: "1"})
        finally:
            config.USING_DEMO_CHANNELS = original


if __name__ == "__main__":
    unittest.main()
