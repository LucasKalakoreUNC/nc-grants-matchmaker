"""Wake Azure hourly without copying mail credentials into GitHub Actions."""
import json
import secrets
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def az(*arguments):
    result = subprocess.run(['az', *arguments, '--output', 'json', '--only-show-errors'],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout) if result.stdout.strip() else None


def main():
    group = az('webapp', 'list', '--query', "[?name=='ncgrantfinder'].resourceGroup | [0]")
    if not group:
        raise RuntimeError('Could not find the ncgrantfinder App Service')
    token = az('webapp', 'config', 'appsettings', 'list', '--name', 'ncgrantfinder', '--resource-group', group,
               '--query', "[?name=='ADMIN_TOKEN'].value | [0]")
    if not token:
        token = secrets.token_urlsafe(48)
        print('::add-mask::' + token, flush=True)
        az('webapp', 'config', 'appsettings', 'set', '--name', 'ncgrantfinder', '--resource-group', group,
           '--settings', 'ADMIN_TOKEN=' + token)
    else:
        print('::add-mask::' + token, flush=True)
    host = az('webapp', 'show', '--name', 'ncgrantfinder', '--resource-group', group, '--query', 'defaultHostName')
    request = Request('https://' + host + '/api/notifications/run', data=b'{}',
                      headers={'Content-Type':'application/json', 'X-Admin-Token': token}, method='POST')
    for attempt in range(8):
        try:
            with urlopen(request, timeout=180) as response:
                result = json.loads(response.read())
            print(json.dumps(result))
            if result.get('failed'):
                raise RuntimeError('Some mail deliveries failed and were queued for retry')
            return
        except (HTTPError, URLError, TimeoutError):
            if attempt == 7:
                raise RuntimeError('Notification endpoint did not become available') from None
            time.sleep(10)


if __name__ == '__main__':
    main()
