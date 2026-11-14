"""Fedora и Music: интеграция в текущий upstream без эталонных хешей."""
import ast
from pathlib import Path
from ..transactions import safe, atomic

BROWSER_FILES = (
    'alisa_cli/login_browser.py', 'alisa_cli/doctor.py',
    'engines/bridge/browser_control.py', 'engines/bridge/browser_native_control.py',
)
INTEGRATION_FILES = ('alisa_cli/autostart.py', *BROWSER_FILES, 'engines/bridge/voice.py')


def once(text, old, new):
    if new in text:
        return text
    if text.count(old) != 1:
        raise ValueError('STOP_UPSTREAM_STRUCTURE: нужный участок исходника не найден')
    return text.replace(old, new, 1)


def integrate_python(name, source):
    text = source.decode()
    if name=='alisa_cli/autostart.py':
        text = once(text, "'WorkingDirectory='+systemd_arg(ROOT)",
                    "'WorkingDirectory='+str(ROOT).replace('%','%%')")
    elif name=='alisa_cli/doctor.py':
        text = once(text,
                    "missing=[tool for tool in ['ssh','chromium'] if not shutil.which(tool)]",
                    "missing=[tool for tool in ['ssh','chromium'] if not (shutil.which(tool) or (tool=='chromium' and shutil.which('chromium-browser')))]")
    elif name in BROWSER_FILES:
        text = once(text, "shutil.which('chromium')",
                    "(shutil.which('chromium') or shutil.which('chromium-browser'))")
        if name=='alisa_cli/login_browser.py':
            text = once(text, "name not in ('chromium','chrome')",
                        "name not in ('chromium','chrome','chromium-browser')")
    elif name=='engines/bridge/voice.py':
        text = once(text, 'from playback_guard import PlaybackGuard',
                    'from playback_guard import PlaybackGuard\nfrom verity_music import Music')
        text = once(text, '    async def send_native(command):',
                    '    soundcloud = Music(status, save)\n\n    async def send_native(command):')
        text = once(text, "                elif event == 'heartbeat_timeout':",
                    "                elif event == 'verity_music':\n"
                    "                    if not native_chat_mode and not opening_chat:\n"
                    "                        soundcloud.submit(value)\n"
                    "                elif event == 'heartbeat_timeout':")
        text = once(text, "'focus_yields', 'focus_reacquires', 'tts_gate_enabled']}",
                    "'focus_yields', 'focus_reacquires', 'tts_gate_enabled', 'verity_music_version']}")
        text = once(text, '    finally:\n        music_watcher.cancel()',
                    '    finally:\n        await soundcloud.close()\n        music_watcher.cancel()')
    else:
        raise ValueError('STOP_UNKNOWN_INTEGRATION_FILE')
    ast.parse(text, filename=name)
    return text.encode()



def integrate_agent(source):
    if b"verity_music.h" in source and b"VERITY_MUSIC_PROBE" in source:
        return source
    text = source.decode()
    text = once(text, '#include "custom_commands.h"', '#include "custom_commands.h"\n#include "verity_music.h"')
    text = once(text, '    int custom=configured_command(value);', '    if (verity_music_prefix(value)) return 5;\n    int custom=configured_command(value);')
    text = once(text, r'\"tts_gate_enabled\":%d}\n', r'\"tts_gate_enabled\":%d,\"verity_music_version\":1}\n')
    text = once(text, '    int action = alice_native_command(text.data, text.size);',
        '    int action = alice_native_command(text.data, text.size);\n'
        '    /* GPT retains all conversation text, including music mentions. */\n'
        '    if (action == 5 && a->chat) action = 0;')
    text = once(text, 'if (a->chat || action == 1 || action == 2 || action == 4)',
        'if (a->chat || action == 1 || action == 2 || action == 4 || action == 5)')
    text = once(text, '    if (action) command_event(a, action);',
        '    if (action == 5) verity_music_event(a, text.data, text.size);\n'
        '    else if (action) command_event(a, action);')
    text = once(text, '__attribute__((constructor)) static void run_agent(void) {',
        '__attribute__((constructor)) static void run_agent(void) {\n'
        '    const char *probe = getenv("VERITY_MUSIC_PROBE");\n'
        '    if (probe && !strcmp(probe, "1")) {\n'
        '        const char message[] = "VERITY_MUSIC_MODULE_OK\\n";\n'
        '        write(1, message, sizeof(message)-1); _exit(0);\n'
        '    }')
    return text.encode()



def require_agent(data):
    if (data[:6] != b'\x7fELF\x01\x01' or data[18:20] != b'\x28\x00' or
            len(data)<40 or int.from_bytes(data[36:40], 'little') & 0xFF000400 != 0x05000400):
        raise ValueError('STOP_AGENT_ARMHF_REQUIRED')


def integrate_tree(root, include_music=False, agent_binary=None):
    root = safe(root)
    planned = {}
    for name in INTEGRATION_FILES:
        if name=='engines/bridge/voice.py' and not include_music:
            continue
        planned[name] = integrate_python(name, safe(root/name).read_bytes())
    if include_music:
        if agent_binary is None: raise ValueError('STOP_MUSIC_AGENT_REQUIRED')
        data = agent_binary if isinstance(agent_binary, bytes) else safe(agent_binary).read_bytes()
        require_agent(data)
        planned['engines/bridge/build/native_agent.so'] = data
        planned['engines/bridge/verity_music.py'] = Path(__file__).with_name('verity_music.py').read_bytes()
    # All source transformations and syntax checks precede any write.
    for name, data in planned.items():
        atomic(safe(root/name), data)
    return list(planned)
