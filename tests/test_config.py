"""Config file: template defaults, private user file, secret masking."""

import os
import stat

from pystarlight import config


def test_defaults_without_file():
    cfg = config.load()                    # conftest: PYSTARLIGHT_CONFIG=none
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


def test_project_root_config_and_none_switch(tmp_path, monkeypatch):
    monkeypatch.setenv('PYSTARLIGHT_CONFIG', 'none')
    assert config.find_config() is None
    monkeypatch.delenv('PYSTARLIGHT_CONFIG')
    root = config.project_root()
    assert root is not None and (root / 'pyproject.toml').is_file()
    monkeypatch.chdir(tmp_path)
    f = tmp_path / 'pystarlight.toml'                    # ./pystarlight.toml wins over the root one
    f.write_text('[observer]\nname = "here"\n')
    assert config.find_config() == f


def test_relative_paths_are_relative_to_the_config_file(tmp_path, monkeypatch):
    (tmp_path / 'clips').mkdir()
    f = tmp_path / 'pystarlight.toml'
    f.write_text('[calibration]\ndark = "clips/dark.ravf"\n')
    monkeypatch.setenv('PYSTARLIGHT_CONFIG', str(f))
    monkeypatch.chdir('/')                                  # started from elsewhere
    cfg = config.load()
    assert config.resolve_path(cfg, cfg['calibration']['dark']) == tmp_path / 'clips' / 'dark.ravf'
    assert config.resolve_path(cfg, '/abs/x.ravf') == config.Path('/abs/x.ravf')
    assert config.resolve_path(cfg, '') is None
