"""Явная локальная конфигурация; чтение не создаёт каталоги."""
from pathlib import Path
import os
import re
import tomllib


def load(path=None):
    path = Path(path or os.environ.get('ALICE_ROOT_CONFIG', 'config.local.toml')).expanduser().absolute()
    value = tomllib.loads(path.read_text()) if path.exists() else {}
    for section in ('paths', 'station', 'soundcloud'):
        if section in value and not isinstance(value[section], dict):
            raise ValueError('Раздел '+section+' должен быть TOML-таблицей')
    for section in ('paths', 'soundcloud'):
        for key, item in value.get(section, {}).items():
            if not isinstance(item, str) or not Path(item).expanduser().is_absolute() or '..' in Path(item).parts:
                raise ValueError('Путь '+section+'.'+key+' должен быть абсолютной строкой без ..')
    home = Path.home()
    data = Path(os.environ.get('XDG_DATA_HOME', home / '.local/share'))
    state = Path(os.environ.get('XDG_STATE_HOME', home / '.local/state'))
    cache = Path(os.environ.get('XDG_CACHE_HOME', home / '.cache'))
    conf = Path(os.environ.get('XDG_CONFIG_HOME', home / '.config'))
    defaults = dict(data=data/'alice-root', state=state/'alice-root', cache=cache/'alice-root',
                    bridge=data/'alisa-voice', bridge_state=state/'alisa-voice',
                    bin=home/'.local/bin', unit=conf/'systemd/user/alisa-voice-bridge.service')
    overrides = value.get('paths', {})
    for key, default in defaults.items():
        defaults[key] = Path(overrides.get(key, default)).expanduser().absolute()
    defaults['backup'] = Path(overrides.get('backup', defaults['state']/'backups')).expanduser().absolute()
    value['paths'] = defaults
    value['_file'] = path
    return value


def station(c):
    s = c.get('station', {})
    if (not isinstance(s.get('host'), str) or s['host'].startswith('-') or
            not re.fullmatch(r'[a-zA-Z0-9._:-]+', s['host']) or s['host']=='STATION_HOST'):
        raise ValueError('Заполните station.host в локальной конфигурации')
    if type(s.get('port',2222)) is not int or not 1<=s.get('port',2222)<=65535:
        raise ValueError('Недопустимый SSH port')
    if not isinstance(s.get('user','root'), str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', s.get('user','root')):
        raise ValueError('Недопустимый SSH user')
    if s.get('model','yandexmini_2') != 'yandexmini_2':
        raise ValueError('Поддерживается только yandexmini_2')
    if s.get('abi','armhf')!='armhf':
        raise ValueError('Для Quasar требуется armhf')
    if s.get('bluetooth_mac') is not None and (not isinstance(s['bluetooth_mac'],str) or not re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}',s['bluetooth_mac'])):
        raise ValueError('Недопустимый Bluetooth MAC')
    if s.get('identity') is not None and not isinstance(s['identity'], str):
        raise ValueError('Недопустимый identity path')
    return s


def sc_paths(c):
    s = c.get('soundcloud', {})
    base = c['paths']['state']
    return {
        'profile': Path(s.get('profile', base/'soundcloud-profile')).expanduser().absolute(),
        'state': Path(os.environ.get('ALICE_SC_CONTROL_DIR', s.get('state', base/'soundcloud-control'))).expanduser().absolute(),
    }
