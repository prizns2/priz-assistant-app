"""Установка / удаление агента рабочего ПК. Запускается из install.bat и uninstall.bat."""
import json
import os
import secrets
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, 'config.json')
AGENT = os.path.join(HERE, 'agent.py')
PID_FILE = os.path.join(HERE, 'agent.pid')
STARTUP_LNK_NAME = 'PRIZ PC Agent.lnk'


def ps(cmd):
    # Вывод PowerShell — в UTF-8, иначе русские пути (например «Рабочий стол» в OneDrive) приходят битыми
    return subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command',
                           '[Console]::OutputEncoding=[Text.Encoding]::UTF8; ' + cmd],
                          capture_output=True, text=True, encoding='utf-8', errors='replace')


# Папки Windows по KNOWNFOLDERID — напрямую у системы, без PowerShell и без проблем с кодировкой
KNOWN_FOLDERS = {
    'Desktop': '{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}',
    'CommonDesktopDirectory': '{C4AA340D-F20F-4863-AFEF-F87EF2E6BA25}',
    'Startup': '{B97D20BB-F46A-4C97-BA10-5E3608430854}',
    'StartMenu': '{625B53C3-AB48-4EC1-BA1F-A1EF4146FC19}',
    'CommonStartMenu': '{A4115719-D62E-491D-AA7C-E74B8BE3B067}',
}


def special_folder(name):
    import ctypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [('d1', ctypes.c_ulong), ('d2', ctypes.c_ushort), ('d3', ctypes.c_ushort), ('d4', ctypes.c_ubyte * 8)]

    try:
        u = uuid.UUID(KNOWN_FOLDERS[name])
        g = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) == 0:
            path = out.value
            ctypes.windll.ole32.CoTaskMemFree(out)
            if path and os.path.isdir(path):
                return path
    except Exception:  # noqa: BLE001
        pass
    return ps('[Environment]::GetFolderPath("%s")' % name).stdout.strip()


def shortcut(path, target, args, workdir, icon=None):
    """Ярлык .lnk. Возвращает True, если файл реально появился; иначе печатает причину."""
    q = lambda s: s.replace("'", "''")
    r = ps("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');$s.TargetPath='%s';$s.Arguments='%s';"
           "$s.WorkingDirectory='%s';%s$s.Save()" % (q(path), q(target), q(args), q(workdir),
                                                    ("$s.IconLocation='%s';" % q(icon)) if icon else ''))
    if os.path.exists(path):
        return True
    err = (r.stderr or r.stdout or '').strip().splitlines()
    print('   !! Не удалось создать ярлык %s%s' % (path, (': ' + err[0][:200]) if err else ''))
    return False


def cmd_fallback(path, target, args, workdir):
    """Запасной вариант, если ярлык создать нельзя: .cmd с тем же запуском (на миг мелькнёт окно)."""
    # cmd читает .cmd в OEM-кодировке (cp866 на русской Windows) — так русские пути не побьются
    with open(path, 'w', encoding='cp866', errors='replace', newline='\r\n') as f:
        f.write('@echo off\ncd /d "%s"\nstart "" "%s" %s\n' % (workdir, target, args))
    return path


def find_brave():
    cands = [os.path.expandvars(p) for p in (
        r'%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe',
        r'%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe',
        r'%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe')]
    for c in cands:
        if os.path.isfile(c):
            return c
    return ''


EXCLUDE = ('uninstall', 'деинстал', 'удален', 'удалить', 'compatibility', 'readme', 'справк', 'help', 'документац')


def resolve_shortcuts(folders):
    """Все ярлыки из папок (с подпапками) → [{lnk, target, args, workdir}] одним вызовом PowerShell."""
    lst = ",".join("'%s'" % f.replace("'", "''") for f in folders if f and os.path.isdir(f))
    if not lst:
        return []
    cmd = ("$sh=New-Object -ComObject WScript.Shell; $r=@(); foreach($d in @(%s)){ Get-ChildItem -LiteralPath $d -Recurse "
           "-Filter *.lnk -ErrorAction SilentlyContinue | ForEach-Object { $l=$sh.CreateShortcut($_.FullName); "
           "$r+=[pscustomobject]@{lnk=$_.FullName;target=$l.TargetPath;args=$l.Arguments;workdir=$l.WorkingDirectory} } }; "
           "[Console]::OutputEncoding=[Text.Encoding]::UTF8; ConvertTo-Json -InputObject $r -Compress" % lst)
    out = ps(cmd).stdout.strip()
    try:
        data = json.loads(out) if out else []
    except ValueError:
        return []
    return data if isinstance(data, list) else [data]


def resolve_one(path):
    """Путь, который перетащили в окно: ярлык → {target, args, workdir}; .exe или файл — как есть."""
    path = path.strip().strip('"').strip()
    if not path or not os.path.exists(path):
        return None
    if path.lower().endswith('.lnk'):
        q = path.replace("'", "''")
        out = ps("$l=(New-Object -ComObject WScript.Shell).CreateShortcut('%s'); [Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                 "ConvertTo-Json -Compress @{target=$l.TargetPath;args=$l.Arguments;workdir=$l.WorkingDirectory}" % q).stdout.strip()
        try:
            d = json.loads(out)
        except ValueError:
            d = {}
        if d.get('target') and os.path.exists(d['target']):
            return {'path': d['target'], 'args': d.get('args') or '', 'workdir': d.get('workdir') or ''}
        return {'path': path, 'args': '', 'workdir': ''}
    return {'path': path, 'args': '', 'workdir': ''}


def ask_missing(apps):
    """Для ненайденных программ — спросить ярлык: его можно просто перетащить мышкой в это окно."""
    if not sys.stdin or not sys.stdin.isatty():
        return
    for app in apps:
        cur = (app.get('path') or '').strip().strip('"')
        if cur and os.path.exists(cur):
            continue
        print()
        print('❌ %s не найдена.' % app['name'])
        while True:
            try:
                ans = input('   Перетащи сюда её ярлык (или .exe) и нажми Enter. Просто Enter — пропустить: ')
            except EOFError:
                return
            if not ans.strip():
                print('   Пропущено — %s не будет открываться и закрываться.' % app['name'])
                break
            got = resolve_one(ans)
            if got:
                app.update(got)
                print('   ✅ %s — %s %s' % (app['name'], got['path'], got['args']))
                break
            print('   Не нашёл такой файл, попробуй ещё раз.')


def find_apps(cfg, write):
    """Ищет программы из cfg['apps'] по ярлыкам: сначала рабочий стол, потом «Пуск». Печатает ✅/❌."""
    apps = cfg.get('apps') or []
    if not apps:
        return
    desk = resolve_shortcuts([special_folder('Desktop'), special_folder('CommonDesktopDirectory')])
    menu = resolve_shortcuts([special_folder('StartMenu'), special_folder('CommonStartMenu')])
    print()
    print('Программы:')
    for app in apps:
        cur = (app.get('path') or '').strip().strip('"')
        if cur and os.path.exists(cur):
            print('  ✅ %s — %s (уже вписана)' % (app['name'], cur))
            continue
        terms = [t.lower() for t in app.get('find') or [app['name']]]
        hit = None
        for sc in desk + menu:
            name = os.path.splitext(os.path.basename(sc['lnk']))[0].lower()
            if any(t in name for t in terms) and not any(x in name for x in EXCLUDE):
                hit = sc
                break
        if not hit:
            print('  ❌ %s — ярлык не найден' % app['name'])
            continue
        target = (hit.get('target') or '').strip()
        if target and os.path.exists(target):
            path, args, wd = target, hit.get('args') or '', hit.get('workdir') or ''
        else:  # ярлык без обычного пути (магазинные и т.п.) — запускаем сам ярлык
            path, args, wd = hit['lnk'], '', ''
        print('  ✅ %s — %s %s' % (app['name'], path, args))
        if write:
            app['path'], app['args'], app['workdir'] = path, args, wd


def stop_agent():
    try:
        import psutil
    except ImportError:
        return
    me = os.getpid()
    for p in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if p.info['pid'] != me and (p.info['name'] or '').lower() in ('python.exe', 'pythonw.exe') \
                    and any(os.path.normcase(a) == os.path.normcase(AGENT) for a in (p.info['cmdline'] or [])):
                p.terminate()
                p.wait(5)
                print('Агент остановлен (PID %d)' % p.info['pid'])
        except Exception:  # noqa: BLE001
            pass
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)


def install():
    with open(CONFIG, encoding='utf-8') as f:
        cfg = json.load(f)

    brave = cfg.get('brave_path') if os.path.isfile(cfg.get('brave_path') or '') else find_brave()
    if not brave:
        print('!! Не найден brave.exe. Установи Brave или впиши путь в config.json (brave_path) и запусти install.bat снова.')
        sys.exit(1)
    cfg['brave_path'] = brave
    print('Brave: ' + brave)

    profile = os.path.join(os.environ['USERPROFILE'], 'Documents', 'wdata')
    os.makedirs(profile, exist_ok=True)
    subprocess.run(['attrib', '+h', profile], capture_output=True)
    cfg['profile_dir'] = profile
    print('Профиль (скрытая папка): ' + profile)

    find_apps(cfg, write=True)
    ask_missing(cfg.get('apps') or [])

    new_key = False
    if not cfg.get('key'):
        cfg['key'] = secrets.token_urlsafe(48)
        new_key = True
    with open(CONFIG, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    desktop = special_folder('Desktop')
    brave_args = '--user-data-dir="%s" --no-first-run --no-default-browser-check' % profile
    if shortcut(os.path.join(desktop, 'Работа.lnk'), brave, brave_args, os.path.dirname(brave), brave):
        print('Ярлык «Работа» на рабочем столе: ' + desktop)
    else:
        print('Вместо ярлыка — файл ' + cmd_fallback(os.path.join(desktop, 'Работа.cmd'), brave, brave_args, os.path.dirname(brave)))

    pythonw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    if not os.path.isfile(pythonw):
        pythonw = sys.executable
    startup = special_folder('Startup')
    if shortcut(os.path.join(startup, STARTUP_LNK_NAME), pythonw, '"%s"' % AGENT, HERE):
        print('Автозагрузка: ' + os.path.join(startup, STARTUP_LNK_NAME))
    else:
        print('Автозагрузка (запасной вариант): ' +
              cmd_fallback(os.path.join(startup, 'PRIZ PC Agent.cmd'), pythonw, '"%s"' % AGENT, HERE))

    stop_agent()
    subprocess.Popen([pythonw, AGENT], cwd=HERE, creationflags=0x00000008 | 0x00000200, close_fds=True)
    time.sleep(2)
    print('Агент запущен. Лог: ' + os.path.join(HERE, 'logs', 'agent.log'))
    if new_key:
        print()
        print('!! Создан НОВЫЙ ключ агента (config.json → key). Его нужно вписать в Apps Script:')
        print('   Настройки проекта → Свойства скрипта → PC_KEY = значение key из config.json.')


def uninstall():
    stop_agent()
    lnk = os.path.join(special_folder('Startup'), STARTUP_LNK_NAME)
    if os.path.exists(lnk):
        os.remove(lnk)
        print('Убран из автозагрузки')
    print('Папка рабочего профиля и ярлык «Работа» не удалены.')


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        subprocess.run('chcp 65001', shell=True, capture_output=True)
    except Exception:  # noqa: BLE001
        pass
    if '--uninstall' in sys.argv:
        uninstall()
    elif '--find' in sys.argv:  # только показать, что нашлось бы, ничего не записывая
        with open(CONFIG, encoding='utf-8') as f:
            find_apps(json.load(f), write=False)
    else:
        install()
