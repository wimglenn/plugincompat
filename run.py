"""
Reads index.json file containing plugins versions and descriptions, and
test them against current python version and pytest version selected by
$PYTEST_VERSION environment variable.

The plugins are tested using tox. If a plugin provides a tox.ini file,
that is used to test the plugin compatibility, otherwise we provide a simple
tox.ini that practically just tests that the plugin was installed successfully
by running `pytest --help`.

The pytest version to use is obtained by $PYTEST_VERSION, which is forced as
a dependency when invoking tox.

Once all results are obtained, they are posted to the plugincompat heroku app
which can then be visualized.
"""
from __future__ import print_function, with_statement, division

import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import traceback
from collections import namedtuple
from contextlib import contextmanager
from zipfile import ZipFile

import requests

import update_index

if sys.version_info[0] == 3:
    from urllib.request import urlretrieve
    from xmlrpc.client import ServerProxy
else:
    from urllib import urlretrieve
    from xmlrpclib import ServerProxy



# oh my, urlretrieve is not thread safe :(
_urlretrieve_lock = threading.Lock()

def download_package(client, name, version):
    for url_data in client.release_urls(name, version):
        basename = os.path.basename(url_data['url'])
        if url_data['packagetype'] == 'sdist':
            with _urlretrieve_lock:
                urlretrieve(url_data['url'], basename)
            return basename

    return None


def extract(basename):
    """
    Extracts the contents of the given archive into the current directory.

    :param basename: name of the archive related to the current directory
    :type basename: str

    :rtype: str
    :return: the name of the directory where the contents where extracted
    """
    from contextlib import closing

    extractors = {
        '.zip': ZipFile,
        '.tar.gz': tarfile.open,
        '.tgz': tarfile.open,
    }
    for ext, extractor in extractors.items():
        if basename.endswith(ext):
            with closing(extractor(basename)) as f:
                f.extractall('.')
            return basename[:-len(ext)]
    assert False, 'could not extract %s' % basename


def run_tox(directory, tox_env, pytest_version):
    """
    Runs tox on the given directory and return (exit code, output)
    """
    tox_file = os.path.join(directory, 'tox.ini')
    if not os.path.isfile(tox_file):
        with open(tox_file, 'w') as f:
            f.write(PLACEHOLDER_TOX)

    cmdline = 'tox --result-json=result.json -e %s --force-dep=pytest==%s'
    cmdline %= (tox_env, pytest_version)

    try:
        output = subprocess.check_output(
            cmdline, shell=True, stderr=subprocess.STDOUT, cwd=directory)
        result = 0
    except subprocess.CalledProcessError as e:
        result = e.returncode
        output = e.output

    return result, output.decode()

# tox.ini contents when downloaded package does not have a tox.ini file
# in this case we only display help information
PLACEHOLDER_TOX = '''\
[tox]

[testenv]
deps=pytest
commands=
    py.test --help
'''


def read_plugins_index(file_name):
    with open(file_name) as f:
        return json.load(f)


class PackageResult(
    namedtuple('PackageResult', 'name version status_code status output description elapsed')):
    pass


def process_package(tox_env, pytest_version, name, version, description):
    def get_elapsed():
        return time.time() - start

    start = time.time()

    # if we already results, skip testing this plugin
    url = os.environ.get('PLUGINCOMPAT_SITE')
    if url:
        params = dict(py=tox_env, pytest=pytest_version)
        response = requests.get('{}/output/{}-{}'.format(url, name, version), params=params)
        if response.status_code == 200:
            return PackageResult(name, version, 0, 'SKIPPED', 'Skipped', description,
                                 get_elapsed())

    client = ServerProxy('https://pypi.python.org/pypi')
    basename = download_package(client, name, version)
    if basename is None:
        status_code, output = 1, 'No sdist found'
        return PackageResult(name, version, status_code, 'NO SOURCE', output, description,
                             get_elapsed())
    directory = extract(basename)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    f = executor.submit(run_tox, directory, tox_env, pytest_version)
    try:
        status_code, output = f.result(timeout=5 * 60)
    except concurrent.futures.TimeoutError:
        f.cancel()
        status_code, output = 1, 'tox run timed out'
    except Exception:
        f.cancel()
        if sys.version_info[0] == 2:
            from StringIO import StringIO
        else:
            from io import StringIO
        stream = StringIO()
        traceback.print_exc(file=stream)
        status_code, output = 'error', 'traceback:\n%s' % stream.getvalue()
    finally:
        executor.shutdown(wait=False)
    output += '\n\nTime: %.1f seconds' % get_elapsed()
    status = 'PASSED' if status_code == 0 else 'FAILED'
    return PackageResult(name, version, status_code, status, output, description, get_elapsed())


@contextmanager
def working_dir(new_cwd):
    if os.path.isdir(new_cwd):
        shutil.rmtree(new_cwd)
    os.makedirs(new_cwd)
    old_cwd = os.getcwd()
    os.chdir(new_cwd)
    yield new_cwd
    os.chdir(old_cwd)


def main():
    pytest_version = os.environ['PYTEST_VERSION']
    secret = os.environ['POST_KEY']
    tox_env = 'py%d%d' % sys.version_info[:2]

    plugins = read_plugins_index(update_index.INDEX_FILE_NAME)

    test_results = {}
    overall_start = time.time()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=8)
    with working_dir('.work'), executor:
        fs = []
        for plugin in plugins:
            f = executor.submit(process_package, tox_env,
                                pytest_version, plugin['name'],
                                plugin['version'], plugin['description'])
            fs.append(f)

        print('Processing %d packages' % len(fs))
        for f in concurrent.futures.as_completed(fs):
            package_result = f.result()
            print('=' * 60)
            print('%s-%s' % (package_result.name, package_result.version))
            print('-> tox returned %s' % package_result.status_code)
            print('-> status:', package_result.status)
            print('-> time: %.1f seconds' % package_result.elapsed)
            if package_result.status != 'SKIPPED':
                test_results[(package_result.name, package_result.version)] = \
                    package_result.status_code, package_result.output, package_result.description

    print('\n\n')
    overall_elapsed = time.time() - overall_start
    elapsed_m = overall_elapsed // 60
    elapsed_s = overall_elapsed % 60
    print('=' * 60)
    print('Summary')
    print('Time: %dm %02ds' % (elapsed_m, elapsed_s))
    print('Skipped:', len(plugins) - len(test_results))
    print('Run:', len(test_results))
    print('=' * 60)
    results = []
    for (name, version) in sorted(test_results):
        result, output, description = test_results[(name, version)]
        if result == 0:
            status = 'ok'
        else:
            status = 'fail'
        package = '%s-%s' % (name, version)
        spaces = (50 - len(package)) * ' '
        print('%s%s%s' % (package, spaces, status))
        results.append(
            {'name': name,
             'version': version,
             'env': tox_env,
             'pytest': pytest_version,
             'status': status,
             'output': output,
             'description': description,
            }
        )
    post_url = os.environ.get('PLUGINCOMPAT_SITE')
    if post_url:
        data = {
            'secret': secret,
            'results': results,
        }
        headers = {'content-type': 'application/json'}
        response = requests.post(post_url, data=json.dumps(data),
                                 headers=headers)
        print('posted to {}; response={}'.format(post_url, response))
        if response.status_code != 200:
            print('Failed to post results:', response)
            return 1
    else:
        print('not posting, no $PLUGINCOMPAT_SITE defined: {}'.format(results))


if __name__ == '__main__':
    sys.exit(main() or 0)
