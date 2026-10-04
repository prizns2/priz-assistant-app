"""PRIZ Assistant — агент рабочего ПК.

Каждые poll_seconds спрашивает Apps Script, нет ли команды из Mini App
(«Начать работу» / «Завершить»), выполняет её и отчитывается результатом.
Открывает и закрывает только рабочий Brave (свой --user-data-dir);
пароли, логины и cookies не трогает.
"""
import ctypes
import json
import logging
import os
import socket
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from urllib.parse import quote, urlsplit, urlunsplit

import psutil
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, 'config.json')
BROWSER = os.path.join(HERE, 'browser.json')
TABS_JSON = os.path.join(HERE, 'extension', 'tabs.json')
BOOKMARKS_JSON = os.path.join(HERE, 'extension', 'bookmarks.json')
LOG_DIR = os.path.join(HERE, 'logs')
PID_FILE = os.path.join(HERE, 'agent.pid')

log = logging.getLogger('pc-agent')


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    h = RotatingFileHandler(os.path.join(LOG_DIR, 'agent.log'), maxBytes=500_000, backupCount=3, encoding='utf-8')
    h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def single_instance():
    """Второй экземпляр агента сразу выходит (именованный мьютекс Windows)."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, 'Local\\PRIZ_PcAgent')
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)
    return handle


BRAVE_PATHS = (r'%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe',
               r'%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe',
               r'%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe')


def load_config():
    with open(CONFIG, encoding='utf-8') as f:
        cfg = json.load(f)
    # Вкладки и закладки — в browser.json (общий для всех ПК, приезжает с обновлением),
    # config.json — настройки этого ПК (ключ, пути, программы) и при обновлении не трогается
    if os.path.exists(BROWSER):
        with open(BROWSER, encoding='utf-8') as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in ('tabs', 'bookmarks', 'bookmarks_account')})
    cfg.setdefault('tabs', [])
    # config.json мог приехать с другого ПК (другой пользователь Windows) — чужие пути заменяем своими
    profile = os.path.expandvars(cfg.get('profile_dir') or '')
    if not profile or not os.path.isdir(os.path.dirname(profile)):
        profile = os.path.join(os.environ['USERPROFILE'], 'Documents', 'wdata')
    cfg['profile_dir'] = profile
    if not os.path.isfile(cfg.get('brave_path') or ''):
        cfg['brave_path'] = next((p for p in map(os.path.expandvars, BRAVE_PATHS) if os.path.isfile(p)), cfg.get('brave_path') or '')
    return cfg


# ---------- вкладки ----------

YOUTUBE_HOSTS = ('music.youtube.com', 'www.youtube.com', 'youtube.com')


def with_account(url, account):
    """Ссылка под нужным аккаунтом. Google Диск/Таблицы/Apps Script понимают authuser=<email>
    (добавляем перед #…, из ссылок Drive убираем /u/0/). YouTube email в authuser не понимает —
    открываем через выбор аккаунта Google, он переключит аккаунт и перейдёт по ссылке."""
    url = url.replace('drive.google.com/drive/u/0/', 'drive.google.com/drive/')
    if not account:
        return url
    p = urlsplit(url)
    if p.netloc.lower() in YOUTUBE_HOSTS:
        return 'https://accounts.google.com/AccountChooser?Email=%s&continue=%s' % (quote(account), quote(url, safe=''))
    query = (p.query + '&' if p.query else '') + 'authuser=' + account
    return urlunsplit((p.scheme, p.netloc, p.path, query, p.fragment))


def match_key(url):
    p = urlsplit(url.replace('drive.google.com/drive/u/0/', 'drive.google.com/drive/'))
    return (p.netloc.lower() + p.path).rstrip('/')


def write_tabs(cfg):
    # match — сама страница без параметров: по нему расширение узнаёт уже открытую вкладку
    tabs = [{'url': with_account(t['url'], t.get('account', '')), 'match': match_key(t['url']),
             'pinned': bool(t.get('pinned'))} for t in cfg['tabs']]
    with open(TABS_JSON, 'w', encoding='utf-8') as f:
        json.dump(tabs, f, ensure_ascii=False, indent=2)
    write_bookmarks(cfg)


GOOGLE_HOSTS = ('docs.google.com', 'drive.google.com', 'script.google.com', 'sheets.google.com')


def write_bookmarks(cfg):
    """Закладки для расширения: ссылкам Google подставляем аккаунт bookmarks_account."""
    account = cfg.get('bookmarks_account', '')

    def conv(nodes):
        out = []
        for n in nodes or []:
            if 'children' in n:
                out.append({'title': n['title'], 'children': conv(n['children'])})
            else:
                url = n['url']
                if account and urlsplit(url).netloc.lower() in GOOGLE_HOSTS:
                    url = with_account(url, account)
                out.append({'title': n['title'], 'url': url})
        return out

    with open(BOOKMARKS_JSON, 'w', encoding='utf-8') as f:
        json.dump(conv(cfg.get('bookmarks')), f, ensure_ascii=False, indent=2)


# ---------- рабочий Brave ----------

def _norm(path):
    return os.path.normcase(os.path.normpath(os.path.expandvars(path.strip().strip('"'))))


def work_processes(cfg):
    """Процессы Brave, запущенные именно с рабочим профилем (--user-data-dir)."""
    profile = _norm(cfg['profile_dir'])
    out = []
    for p in psutil.process_iter(['name', 'cmdline']):
        try:
            if (p.info['name'] or '').lower() != 'brave.exe':
                continue
            for a in p.info['cmdline'] or []:
                if a.lower().startswith('--user-data-dir=') and _norm(a.split('=', 1)[1]) == profile:
                    out.append(p)
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def main_processes(procs):
    """Главный процесс браузера — без --type= (остальные — вкладки, GPU и т.п.)."""
    res = []
    for p in procs:
        try:
            if not any(a.startswith('--type=') for a in p.cmdline()):
                res.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return res


def is_running(cfg):
    return bool(main_processes(work_processes(cfg)))


def start_brave(cfg):
    if is_running(cfg):
        return True, 'Brave уже был открыт'
    write_tabs(cfg)
    os.makedirs(cfg['profile_dir'], exist_ok=True)
    subprocess.Popen([cfg['brave_path'], '--user-data-dir=' + cfg['profile_dir'],
                      '--no-first-run', '--no-default-browser-check'],
                     creationflags=DETACHED, close_fds=True)
    for _ in range(20):
        time.sleep(0.5)
        if is_running(cfg):
            return True, 'Brave запущен'
    return False, 'Brave не запустился за 10 с'


# ---------- остальные программы (config.json → apps) ----------

DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
NO_WINDOW = 0x08000000
# По этим именам процесс искать нельзя — так можно задеть чужие (в т.ч. самого агента)
GENERIC = {'python.exe', 'pythonw.exe', 'py.exe', 'cmd.exe', 'powershell.exe', 'wscript.exe', 'java.exe', 'javaw.exe',
           'explorer.exe', 'rundll32.exe', 'msedge.exe', 'chrome.exe', 'brave.exe'}


def app_matchers(app):
    """Как узнать процессы программы: имена exe (можно «префикс*») и куски пути/командной строки."""
    names, paths = [], []
    for x in app.get('processes') or []:
        x = x.strip().lower()
        (paths if ('\\' in x or '/' in x) else names).append(x)
    path = (app.get('path') or '').strip().strip('"')
    if not names and not paths and path.lower().endswith('.exe'):
        base = os.path.basename(path).lower()
        if base in GENERIC:
            # Скрипт через python/cmd — узнаём по пути скрипта в командной строке
            arg = (app.get('args') or '').strip().strip('"')
            if arg:
                paths.append(os.path.normcase(arg))
        else:
            paths.append(os.path.normcase(path))
    # Файл (.rdp, документ): программа открывает его с путём в командной строке — по нему и узнаём
    if path and not path.lower().endswith(('.exe', '.lnk')):
        paths.append(os.path.normcase(path))
    names = [n for n in names if n.rstrip('*') not in GENERIC]
    return names, paths


# Какие процессы агент сам запустил для каждой программы (pid + время создания — защита от повторного pid).
# Нужно для ярлыков и файлов (.lnk, .rdp), у которых заранее не известно, как называется процесс.
STATE = os.path.join(HERE, 'agent_state.json')
NOT_APP = {'brave.exe', 'taskkill.exe', 'conhost.exe'}


def _state():
    try:
        with open(STATE, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(s):
    try:
        with open(STATE, 'w', encoding='utf-8') as f:
            json.dump(s, f)
    except OSError:
        log.warning('Не удалось сохранить agent_state.json')


def tracked_processes(name):
    out = []
    for rec in _state().get(name, []):
        try:
            p = psutil.Process(rec['pid'])
            if abs(p.create_time() - rec['ct']) < 1:
                out.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
            continue
    return out


def app_processes(app):
    names, paths = app_matchers(app)
    me, found = os.getpid(), {}
    if names or paths:
        for p in psutil.process_iter(['pid', 'name', 'exe', 'cmdline']):
            try:
                if p.info['pid'] == me:
                    continue
                n = (p.info['name'] or '').lower()
                hit = any(n.startswith(x[:-1]) if x.endswith('*') else n == x for x in names)
                if not hit and paths:
                    hay = os.path.normcase((p.info['exe'] or '') + ' ' + ' '.join(p.info['cmdline'] or []))
                    hit = any(x in hay for x in paths)
                if hit:
                    found[p.pid] = p
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    # плюс то, что агент сам запускал для этой программы, и их дочерние процессы
    for p in tracked_processes(app['name']):
        found[p.pid] = p
        try:
            for c in p.children(recursive=True):
                found[c.pid] = c
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    found.pop(me, None)
    return list(found.values())


def launch_app(app):
    """Запуск программы; возвращает pid процессов, которые появились у агента сразу после запуска."""
    path = (app.get('path') or '').strip().strip('"')
    workdir = app.get('workdir') or os.path.dirname(path)
    t0 = time.time()
    if path.lower().endswith('.exe'):
        import shlex
        args = shlex.split(app.get('args') or '', posix=False)
        subprocess.Popen([path] + args, cwd=workdir if os.path.isdir(workdir) else None,
                         creationflags=DETACHED, close_fds=True)
    else:
        os.startfile(path)  # ярлык, .rdp, .bat, документ — как двойной щелчок
    pids = set()
    for _ in range(4):
        time.sleep(0.5)
        try:
            for c in psutil.Process().children():
                if c.create_time() >= t0 - 0.5 and c.name().lower() not in NOT_APP:
                    pids.add(c.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return pids


def remember_launched(launched, seconds=8):
    """Несколько секунд собираем дочерние процессы запущенного (лаунчер → сама программа) и запоминаем."""
    t_start = time.time()
    while time.time() - t_start < seconds:
        for p in psutil.process_iter(['pid', 'ppid', 'name']):
            for pids in launched.values():
                if p.info['ppid'] in pids and (p.info['name'] or '').lower() not in NOT_APP:
                    pids.add(p.info['pid'])
        time.sleep(1)
    s = _state()
    for name, pids in launched.items():
        recs = []
        for pid in pids:
            try:
                recs.append({'pid': pid, 'ct': psutil.Process(pid).create_time()})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        s[name] = recs
    _save_state(s)


def start_work(cfg):
    lines, ok_all = [], True
    ok, text = start_brave(cfg)
    ok_all &= ok
    lines.append(('✅ ' if ok else '⚠️ ') + text)
    launched = {}
    for app in cfg.get('apps') or []:
        path = (app.get('path') or '').strip().strip('"')
        if not path:
            lines.append('— ' + app['name'] + ' — не найдена на этом ПК (впиши path в config.json)')
            continue
        try:
            if not os.path.exists(path):
                ok, text = False, 'нет файла ' + path
            elif app_processes(app):
                ok, text = True, 'уже была открыта'
            else:
                launched[app['name']] = launch_app(app)
                ok, text = True, 'запущена'
        except Exception as e:  # noqa: BLE001
            ok, text = False, 'ошибка запуска: %s' % e
        ok_all &= ok
        lines.append(('✅ ' if ok else '⚠️ ') + app['name'] + ' — ' + text)
    if launched:
        remember_launched(launched)  # чтобы «Завершить» знал, что закрывать
    return ok_all, '\n'.join(lines)


def stop_work(cfg):
    """Закрывает рабочий Brave и программы с close=true: сначала мягко (как крестиком), что не закрылось
    за close_timeout_seconds — принудительно. Обычный Brave не трогается."""
    groups = [('Brave', lambda: work_processes(cfg), lambda: main_processes(work_processes(cfg)))]
    for app in cfg.get('apps') or []:
        # Только программы, которые установщик нашёл на этом ПК (есть path)
        if app.get('close', True) and app.get('path'):
            groups.append((app['name'], (lambda a: lambda: app_processes(a))(app), None))
    was = {name: bool(get()) for name, get, _ in groups}
    for name, get, mains in groups:
        for p in (mains() if mains else get()):
            subprocess.run(['taskkill', '/PID', str(p.pid)], capture_output=True, creationflags=NO_WINDOW)
    deadline = time.time() + int(cfg.get('close_timeout_seconds', 15))
    while time.time() < deadline and any(get() for _, get, _ in groups):
        time.sleep(0.5)
    forced = []
    for name, get, _ in groups:
        procs = get()
        if procs:
            forced.append(name)
            for p in procs:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(p.pid)], capture_output=True, creationflags=NO_WINDOW)
    time.sleep(1)
    lines, ok_all = [], True
    for name, get, _ in groups:
        if not was[name]:
            lines.append('— ' + name + ' не была открыта' if name != 'Brave' else '— Brave не был открыт')
        elif get():
            ok_all = False
            lines.append('⚠️ ' + name + ' — не удалось закрыть')
        else:
            done = ' закрыт' if name == 'Brave' else ' закрыта'
            lines.append('✅ ' + name + ' —' + done + (' принудительно' if name in forced else ''))
    return ok_all, '\n'.join(lines)


# ---------- связь с Apps Script ----------

def call(cfg, payload):
    body = {'pc': dict(payload, key=cfg['key'])}
    r = requests.post(cfg['web_app_url'], json=body, timeout=30)  # редирект Apps Script requests проходит сам
    r.raise_for_status()
    return r.json()


def main():
    setup_logging()
    _mutex = single_instance()
    with open(PID_FILE, 'w') as f:
        f.write(str(os.getpid()))
    host = socket.gethostname()
    log.info('Агент запущен (%s)', host)
    try:  # чтобы расширение получило вкладки и закладки сразу, а не только после «Начать работу»
        write_tabs(load_config())
    except Exception:  # noqa: BLE001
        log.exception('Не удалось записать tabs.json / bookmarks.json')
    done = set()
    offline = False
    while True:
        try:
            cfg = load_config()
            if not cfg.get('key'):
                log.warning('В config.json нет ключа — жду')
                time.sleep(30)
                continue
            res = call(cfg, {'op': 'poll', 'running': is_running(cfg), 'host': host})
            if offline:
                log.info('Связь восстановлена')
                offline = False
            if not res.get('ok'):
                log.warning('Сервер отказал: %s', res.get('error'))
                time.sleep(30)
                continue
            cmd = res.get('cmd')
            if cmd and cmd['id'] not in done:
                done.add(cmd['id'])
                log.info('Команда %s', cmd['cmd'])
                try:
                    ok, text = start_work(cfg) if cmd['cmd'] == 'start' else stop_work(cfg) if cmd['cmd'] == 'stop' \
                        else (False, 'Неизвестная команда')
                except Exception as e:  # noqa: BLE001 — ошибку отдаём в бот, агент не падает
                    log.exception('Ошибка выполнения')
                    ok, text = False, 'Ошибка на ПК: %s' % e
                log.info('Результат: %s', text)
                call(cfg, {'op': 'result', 'id': cmd['id'], 'cmd': cmd['cmd'], 'ok': ok, 'text': text,
                           'running': is_running(cfg), 'host': host})
            time.sleep(max(1, int(cfg.get('poll_seconds', 3))))
        except (requests.RequestException, ValueError) as e:
            if not offline:
                log.warning('Нет связи с сервером: %s', type(e).__name__)
                offline = True
            time.sleep(10)
        except Exception:  # noqa: BLE001
            log.exception('Непредвиденная ошибка')
            time.sleep(10)


if __name__ == '__main__':
    main()
