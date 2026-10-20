"""Run as root on the prepared server before migration; no phone, model or QQ messages."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

HOME = Path('/home/littlewhale')
UNIT = 'littlewhale-core-check.service'
UNIT_FILE = Path('/run/systemd/system') / UNIT


def state():
    output = subprocess.check_output(
        ['systemctl', 'show', UNIT, '-p', 'MainPID', '-p', 'ActiveState'], text=True)
    return dict(line.split('=', 1) for line in output.splitlines())


def healthy():
    try:
        with urllib.request.urlopen('http://127.0.0.1:8767/health', timeout=2) as response:
            return json.loads(response.read(20000)).get('service') == 'LittleWhale'
    except OSError:
        return False


def run():
    assert not (HOME / '.littlewhale/cloud-migration-ready').exists(), 'Already migrated; do not run this preparation check'
    for service in ('littlewhale', 'littlewhale-qq'):
        output = subprocess.check_output(
            ['systemctl', 'show', service, '-p', 'ActiveState', '-p', 'ConditionResult'], text=True)
        assert 'ActiveState=inactive' in output and 'ConditionResult=no' in output, service
    with tempfile.TemporaryDirectory(prefix='.cloud-log-check-', dir=HOME) as directory:
        folder = Path(directory)
        subprocess.run(['chown', 'littlewhale:littlewhale', directory], check=True)
        log = folder / 'console.log'
        log.write_bytes(b'rotation fixture\n')
        log.chmod(0o600)
        subprocess.run(['chown', 'littlewhale:littlewhale', str(log)], check=True)
        config = Path('/etc/logrotate.d/littlewhale').read_text()
        config = config.replace('/home/littlewhale/.littlewhale-napcat/logs/console.log', str(log))
        test_config = folder / 'logrotate.conf'
        test_config.write_text(config)
        subprocess.run(['logrotate', '--state', str(folder / 'state'), '--force', str(test_config)], check=True)
        import gzip
        assert log.read_bytes() == b''
        rotated = folder / 'console.log.1.gz'
        assert gzip.decompress(rotated.read_bytes()) == b'rotation fixture\n'
        assert rotated.stat().st_mode & 0o777 == 0o600
    assert not UNIT_FILE.exists(), 'Another check owns this temporary unit'
    with tempfile.TemporaryDirectory(prefix='.cloud-check-', dir=HOME) as directory:
        subprocess.run(['chown', 'littlewhale:littlewhale', directory], check=True)
        original = Path('/etc/systemd/system/littlewhale.service').read_text()
        unit = '\n'.join(line for line in original.splitlines() if not line.startswith('ConditionPathExists=')) + '\n'
        unit = unit.replace('--home /home/littlewhale/.littlewhale ', '--home ' + directory + ' ')
        try:
            UNIT_FILE.write_text(unit)
            subprocess.run(['systemctl', 'daemon-reload'], check=True)
            subprocess.run(['systemctl', 'start', UNIT], check=True)
            for _ in range(20):
                if healthy():
                    break
                time.sleep(1)
            assert healthy(), 'Isolated core startup failed'
            before = state()['MainPID']
            subprocess.run(['systemctl', 'kill', '--kill-whom=main', '--signal=SIGKILL', UNIT], check=True)
            for _ in range(30):
                after = state()
                if after['MainPID'] not in ('0', before) and after['ActiveState'] == 'active' and healthy():
                    break
                time.sleep(1)
            else:
                raise AssertionError('Automatic crash recovery failed')
        finally:
            subprocess.run(['systemctl', 'stop', UNIT], check=True)
            UNIT_FILE.unlink(missing_ok=True)
            subprocess.run(['systemctl', 'daemon-reload'], check=True)
    assert not (HOME / '.littlewhale/memory.sqlite3').exists(), 'Production data must remain untouched'
    print('PASS: private log rotation, production migration gates, isolated HTTP startup and automatic crash recovery; no real model or QQ login tested')


if __name__ == '__main__':
    run()
