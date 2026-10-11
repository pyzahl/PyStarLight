"""Config file: template defaults, private user file, secret masking."""

import os
import stat

from pystarlight import config


def test_defaults_without_file():
    cfg = config.load()
    assert '_path' not in cfg
    assert cfg['analysis']['mode'] == 'global'
    assert set(cfg['apertures']['roles']) == {'target', 'reference', 'check', 'sky'}
    keys = [r['key'] for r in cfg['apertures']['roles'].values()]
    assert len(keys) == len(set(keys))


def test_init_is_private_and_overrides(tmp_path, monkeypatch):
    p = config.init(str(tmp_path / 'c.toml'))
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    p.write_text(p.read_text().replace('api_key = ""', 'api_key = "secret123"')
                 .replace('search = 15', 'search = 22'))
    monkeypatch.setenv('PYSTARLIGHT_CONFIG', str(p))
    cfg = config.load()
    assert cfg['astrometry']['api_key'] == 'secret123'
    assert cfg['analysis']['search'] == 22
    assert config.masked(cfg)['astrometry']['api_key'] == '***'
    assert 'secret123' not in repr(config.masked(cfg))


def test_partial_file_keeps_defaults(tmp_path, monkeypatch):
    p = tmp_path / 'c.toml'
    p.write_text('[observer]\nname = "X"\n')
    monkeypatch.setenv('PYSTARLIGHT_CONFIG', str(p))
    cfg = config.load()
    assert cfg['observer']['name'] == 'X' and cfg['apertures']['size'] == 51
